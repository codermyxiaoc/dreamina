import json
import math
import uuid

from cryptography.fernet import Fernet
from sqlalchemy import insert

from dreamina_api.platform_store import PlatformStore, tasks, task_events
from dreamina_api.reliability import reliability_snapshot, wilson_lower


def make_store():
    store=PlatformStore('sqlite:///:memory:',encryption_key=Fernet.generate_key())
    store.create_account({'id':'one','name':'fixture','capabilities':['seedance-2.0-mini']})
    key=store.create_key({'name':'fixture'})
    return store,key['item']['id']


def add_tasks(store,key,statuses,*,model='seedance-2.0-mini',kind='video',start=0,error=None):
    rows=[]
    for index,status in enumerate(statuses,start):
        rows.append({'id':f'{kind}_{index}_{model}','kind':kind,'model':model,'status':status,'account_id':'one','key_id':key,
                     'request_json':'{"prompt":"private-prompt-never-return"}','request_hash':'synthetic','dispatch_claimed':False,
                     'progress':0,'error_json':json.dumps(error) if error is not None else None,
                     'created_at':1000+index,'updated_at':1000+index})
    with store.transaction() as conn:conn.execute(insert(tasks),rows)
    return [row['id'] for row in rows]


def event(store,task_id,stage):
    with store.transaction() as conn:
        conn.execute(insert(task_events).values(id=uuid.uuid4().hex,task_id=task_id,created_at=2000,stage=stage,
                                               detail_json='{"cookie":"never-return-secret"}'))


def test_no_samples_is_unknown_not_zero_or_99_percent():
    store,_=make_store();r=reliability_snapshot(store)
    assert r['target_percent']==99 and r['target_is_guarantee'] is False
    assert r['overall']['accepted']==0
    for field in ['closed_success_rate_percent','accepted_completion_rate_percent','wilson_95_lower_percent',
                  'automatic_closed_success_rate_percent','automatic_wilson_95_lower_percent']:
        assert r['overall'][field] is None
    assert r['overall']['sample_supports_target'] is False
    assert r['by_model']==[] and r['first_task_at'] is None


def test_every_video_outcome_remains_in_explicit_denominators_and_images_are_excluded():
    store,key=make_store()
    add_tasks(store,key,['completed','failed','cancelled','queued','claimed','submitted','in_progress','uncertain','future_state'])
    add_tasks(store,key,['completed']*10,kind='image')
    r=reliability_snapshot(store);o=r['overall']
    assert {k:o[k] for k in ['accepted','completed','failed','cancelled','active','uncertain','unknown','terminal','outstanding']}=={
        'accepted':9,'completed':1,'failed':1,'cancelled':1,'active':4,'uncertain':1,'unknown':1,'terminal':3,'outstanding':6}
    assert o['closed_success_rate_percent']==33.3333
    assert o['accepted_completion_rate_percent']==11.1111
    assert o['sample_supports_target'] is False
    assert sum(item['count'] for item in r['failure_categories'])==1


def test_older_failures_are_counted_beyond_latest_100_rows():
    store,key=make_store()
    add_tasks(store,key,['failed'])
    add_tasks(store,key,['completed']*130,start=1)
    store.list_tasks=lambda **_: (_ for _ in ()).throw(AssertionError('Metrics must not use a paginated task listing'))
    o=reliability_snapshot(store)['overall']
    assert o['accepted']==131 and o['failed']==1 and o['completed']==130
    assert o['closed_success_rate_percent']<100


def test_recovery_events_do_not_multiply_rows_or_hide_permission_failures():
    store,key=make_store()
    recovered,blocked,pending=add_tasks(store,key,['completed','failed','uncertain'],error={'message':'private-secret'})
    event(store,recovered,'manual_reconciliation');event(store,recovered,'manual_reconciliation')
    event(store,blocked,'account_plan_required');event(store,pending,'manual_reconciliation')
    r=reliability_snapshot(store);o=r['overall']
    assert o['accepted']==3 and o['failed']==1
    assert o['manual_reconciled']==2 and o['manual_reconciled_completed']==1
    assert o['completed']==1 and o['automatic_completed']==0
    assert o['closed_success_rate_percent']==50 and o['automatic_closed_success_rate_percent']==0
    assert o['failure_categories']['account_plan']==1
    encoded=json.dumps(r)
    assert 'private-secret' not in encoded and 'private-prompt-never-return' not in encoded and 'never-return-secret' not in encoded
    assert recovered not in encoded and key not in encoded


def test_models_and_failure_categories_are_complete_without_raw_error_echo():
    store,key=make_store()
    errors=[('account_plan','Selected model requires a plan upgrade.'),('authentication','Login required.'),
            ('reference_input','Reference upload failed'),('network_timeout','TimeoutError during execution'),
            ('upstream_rejected','Generation rejected by upstream: ret=-6'),('browser_interface','browser_failed'),('unknown','unrecognized synthetic issue')]
    for i,(category,message) in enumerate(errors):
        add_tasks(store,key,['failed'],start=i,error={'code':'fixture','message':message+' private-token'},model='seedance-2.0-fast')
    add_tasks(store,key,['completed'],model='seedance-2.0-mini')
    r=reliability_snapshot(store)
    assert r['overall']['accepted']==8
    assert all(r['overall']['failure_categories'][category]==1 for category,_ in errors)
    models={row['model']:row for row in r['by_model']}
    assert models['seedance-2.0-fast']['failed']==7
    assert models['seedance-2.0-mini']['closed_success_rate_percent']==100
    assert models['seedance-2.0-mini']['sample_supports_target'] is False
    assert 'private-token' not in json.dumps(r)


def test_bad_error_json_is_a_counted_unclassified_failure():
    store,key=make_store();ids=add_tasks(store,key,['failed']*4)
    from sqlalchemy import update
    with store.transaction() as conn:
        for task_id,raw in zip(ids,['{bad','null','[]','['*1100+']'*1100]):
            conn.execute(update(tasks).where(tasks.c.id==task_id).values(error_json=raw))
    o=reliability_snapshot(store)['overall']
    assert o['failure_categories']['unknown']==4 and o['closed_success_rate_percent']==0


def test_wilson_is_two_sided_95_percent_and_target_is_not_claimed_from_small_sample():
    assert wilson_lower(0,0) is None
    assert wilson_lower(0,10)==0
    assert math.isclose(wilson_lower(10,10),10/(10+1.959963984540054**2))
    for number,meets in [(3,False),(380,False),(381,True)]:
        store,key=make_store();add_tasks(store,key,['completed']*number)
        r=reliability_snapshot(store)
        assert r['overall']['sample_supports_target'] is meets
        assert r['overall']['closed_success_rate_percent']==100
        assert r['target_is_guarantee'] is False
        assert r['evidence']['minimum_all_success_terminal_samples_for_target']==381


def test_outstanding_tasks_prevent_sample_support_label_even_after_many_successes():
    store,key=make_store();add_tasks(store,key,['completed']*500);add_tasks(store,key,['uncertain'],start=500)
    o=reliability_snapshot(store)['overall']
    assert o['wilson_95_lower_percent']>99
    assert o['sample_supports_target'] is False
    assert o['accepted_completion_rate_percent']<100


def test_manual_completion_cannot_establish_automatic_reliability_target():
    store,key=make_store();ids=add_tasks(store,key,['completed']*381)
    for task_id in ids:event(store,task_id,'manual_reconciliation')
    r=reliability_snapshot(store);o=r['overall']
    assert o['completed']==381 and o['closed_success_rate_percent']==100
    assert o['wilson_95_lower_percent']>=99
    assert o['manual_reconciled_completed']==381 and o['automatic_completed']==0
    assert o['automatic_closed_success_rate_percent']==0 and o['automatic_wilson_95_lower_percent']==0
    assert o['sample_supports_target'] is False
    assert r['evidence']['target_basis']=='automatic_closed_success_rate'
    assert r['by_model'][0]['automatic_completed']==0


def test_automatic_query_recovery_stays_automatic_but_never_overrides_task_outcome():
    store,key=make_store();manual,automatic,pending=add_tasks(store,key,['completed','completed','uncertain'])
    event(store,manual,'manual_reconciliation')
    event(store,automatic,'upstream_query_recovered');event(store,pending,'upstream_query_recovered')
    o=reliability_snapshot(store)['overall']
    assert o['completed']==2 and o['automatic_completed']==1
    assert o['manual_reconciled_completed']==1 and o['uncertain']==1
    assert o['closed_success_rate_percent']==100 and o['automatic_closed_success_rate_percent']==50
    assert o['sample_supports_target'] is False
