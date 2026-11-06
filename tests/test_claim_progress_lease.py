from dreamina_api.chrome_bridge import BridgeStore


def claimed(tmp_path):
    now=[1000.0]
    store=BridgeStore(tmp_path,clock=lambda:now[0],claim_timeout=60,heartbeat_timeout=180)
    job=store.enqueue({'model':'seedance-2.0-mini','prompt':'fixture','duration':4,'resolution':'720p','ratio':'16:9','mode':'omni'})
    store.heartbeat('default','worker',ready=True,page_url='https://dreamina.capcut.com/ai-tool/generate',protocol_version=4,driver='native')
    claim=store.claim('default','worker')
    return store,now,job,claim


def test_new_preparation_progress_renews_live_lease_but_duplicate_does_not(tmp_path):
    store,now,job,claim=claimed(tmp_path)
    now[0]=1040
    values={'stage':'uploading_references','observed_at':1040}
    result=store.telemetry(job['id'],'worker',claim['claim_token'],values)
    assert result['claim_expires_at']==1100
    now[0]=1050
    result=store.telemetry(job['id'],'worker',claim['claim_token'],values)
    assert result['claim_expires_at']==1100
    now[0]=1070
    assert store.get(job['id'])['status']=='claimed'


def test_progress_cannot_revive_an_expired_claim(tmp_path):
    store,now,job,claim=claimed(tmp_path)
    now[0]=1061
    result=store.telemetry(job['id'],'worker',claim['claim_token'],{'stage':'parameters_ready','observed_at':1061})
    assert result['status']=='uncertain'
    assert result['claim_expires_at']==1060
