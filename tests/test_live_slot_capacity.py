"""Configured capacity never admits more tasks than the currently live slots."""
import uuid

from cryptography.fernet import Fernet
import pytest

from dreamina_api.platform_store import PlatformStore,PlatformError

MODEL='seedance-2.0-mini'


def slot(index,**changes):
    return {'worker_id':'slot-'+str(index),'protocol_version':5,'owner_id':'owner','slot_index':index,'slot_count':4,'online':True,'ready':True,**changes}


def setup(tmp_path,workers):
    store=PlatformStore('sqlite:///'+(tmp_path/'slots.db').as_posix(),encryption_key=Fernet.generate_key(),
                        account_capacity_limit=4,worker_status=lambda:[{'account_id':'one','workers':workers}])
    store.create_account({'id':'one','name':'Synthetic','session_id':'synthetic','capabilities':[MODEL],'max_concurrent':4,'max_inflight':4})
    principal=store.authenticate_key(store.create_key({'name':'Caller','max_concurrent':20})['key'])
    return store,principal


def reserve(store,principal):return store.reserve_task(principal,'video',MODEL,{'prompt':'Synthetic'})


def test_capacity_increase_waits_for_slots_to_exist_and_does_not_prevent_idle_restart(tmp_path):
    workers=[slot(1,slot_count=1)];store,key=setup(tmp_path,workers)
    account=store.get_account('one')
    assert account['effective_max_concurrent']==4 and account['current_slot_capacity']==1
    first=reserve(store,key)
    with pytest.raises(PlatformError) as error:reserve(store,key)
    assert error.value.code=='no_available_account'
    store.update_task(first['id'],status='failed')
    workers[:]=[slot(index) for index in range(1,5)]
    assert store.get_account('one')['current_slot_capacity']==4
    tasks=[reserve(store,key) for _ in range(4)]
    assert len({task['id'] for task in tasks})==4
    with pytest.raises(PlatformError):reserve(store,key)


def test_busy_and_attention_slots_count_but_their_unfinished_tasks_keep_capacity(tmp_path):
    workers=[slot(1),slot(2)];store,key=setup(tmp_path,workers)
    first=reserve(store,key);store.update_task(first['id'],status='uncertain',submit_id=str(uuid.uuid4()),error={'code':'generation_timeout'})
    workers[0]['ready']=False
    account=store.get_account('one');assert account['current_slot_capacity']==2 and account['active_tasks']==1
    assert reserve(store,key)
    with pytest.raises(PlatformError):reserve(store,key)


@pytest.mark.parametrize('workers,capacity',[
    ([slot(1),slot(1,worker_id='duplicate')],1),
    ([slot(1,online=False)],0),
    ([{'worker_id':'legacy-a','online':True,'ready':True},{'worker_id':'legacy-b','online':True,'ready':True}],1),
    ([slot(1,owner_id=None)],0),([slot(True)],0),
])
def test_invalid_duplicate_offline_or_legacy_metadata_cannot_inflate_capacity(tmp_path,workers,capacity):
    store,key=setup(tmp_path,workers)
    assert store.get_account('one')['current_slot_capacity']==capacity
    if capacity:reserve(store,key)
    with pytest.raises(PlatformError):reserve(store,key)
