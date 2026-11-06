import concurrent.futures
from copy import deepcopy
import json
import threading

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from dreamina_api.admin_api import COOKIE_NAME, create_admin_router
from dreamina_api.config import Settings
from dreamina_api.platform_store import PlatformError, PlatformStore, accounts, api_keys, sessions


MODEL = 'seedance-2.0-mini'


@pytest.fixture
def store(tmp_path):
    value = PlatformStore('sqlite:///' + (tmp_path / 'platform.db').as_posix(),
                          encryption_key=Fernet.generate_key())
    yield value
    value.engine.dispose()


def account(store, ident='a', **extra):
    return store.create_account(dict(id=ident, name=ident, capabilities=[MODEL], session_id='private-session-' + ident, **extra))


def key(store, **extra):
    created = store.create_key(dict(name='Test key', **extra))
    return created, store.authenticate_key(created['key'])


def reserve(store, principal, **kwargs):
    return store.reserve_task(principal, 'video', MODEL, {'prompt': 'A red sailboat'}, **kwargs)


def test_account_credentials_encrypted_and_never_in_public_output(store):
    created = account(store)
    assert created['secret_configured']
    assert 'session_id' not in created
    assert 'private-session' not in json.dumps(store.list_accounts())
    with store.engine.connect() as conn:
        blob = conn.execute(select(accounts.c.secret_encrypted)).scalar_one()
    assert 'private-session' not in blob
    assert store.settings_for_account('a').session_id == 'private-session-a'


def test_wrong_encryption_key_cannot_read_existing_credentials(store):
    account(store)
    wrong = PlatformStore(str(store.engine.url), encryption_key=Fernet.generate_key())
    with pytest.raises(PlatformError, match='cannot be decrypted'):
        wrong.settings_for_account('a')
    wrong.engine.dispose()


def test_credentials_update_merges_and_explicit_empty_removes(store):
    account(store)
    store.update_account('a', {'workspace_id': '12345'})
    assert store.settings_for_account('a').session_id == 'private-session-a'
    assert store.settings_for_account('a').workspace_id == '12345'
    store.update_account('a', {'session_id': ''})
    assert store.settings_for_account('a').session_id == ''


def test_import_default_does_not_overwrite_existing_account(store):
    store.import_default(Settings(session_id='first', cookie='sessionid=first'))
    store.import_default(Settings(session_id='second'))
    assert store.settings_for_account('default').session_id == 'first'
    assert store.get_account('default')['capabilities'] == [MODEL]


def test_api_key_only_returned_once_and_hash_persisted(store):
    created, principal = key(store)
    assert created['key'].startswith('sk-dr-')
    assert principal['id'] == created['item']['id']
    assert created['key'] not in json.dumps(store.list_keys())
    with store.engine.connect() as conn:
        digest = conn.execute(select(api_keys.c.key_hash)).scalar_one()
    assert len(digest) == 64 and created['key'] not in digest
    store.update_key(principal['id'], {'enabled': False})
    with pytest.raises(PlatformError) as error:
        store.authenticate_key(created['key'])
    assert error.value.status == 401


def test_expired_key_denied(store):
    created = store.create_key({'name': 'Expired', 'expires_at': 1})
    with pytest.raises(PlatformError) as error:
        store.authenticate_key(created['key'])
    assert error.value.status == 401


def test_key_scope_and_account_scope_enforced(store):
    account(store, 'a')
    account(store, 'b')
    _, principal = key(store, account_ids=['a'])
    with pytest.raises(PlatformError) as error:
        reserve(store, principal, account_id='b')
    assert error.value.code == 'account_not_allowed'
    restricted, p = key(store, allowed_models=['other-model'])
    with pytest.raises(PlatformError) as error:
        reserve(store, p)
    assert error.value.code == 'model_not_allowed'


def test_tasks_are_bound_to_selected_account_and_key(store):
    account(store, 'a', priority=10)
    account(store, 'b', priority=1)
    _, principal = key(store)
    result = reserve(store, principal)
    assert result['account_id'] == 'a'
    _, outsider = key(store)
    with pytest.raises(PlatformError) as error:
        store.get_task(result['id'], outsider)
    assert error.value.status == 404
    assert store.list_tasks(outsider) == []
    with pytest.raises(PlatformError):
        store.update_task(result['id'], account_id='b')


def test_scheduler_skips_disabled_and_busy_but_defers_zero_balance_to_live_quote(store):
    account(store, 'disabled', enabled=False, priority=100)
    account(store, 'empty', priority=90)
    store.record_account_check('empty', status='ready', balance=0)
    account(store, 'busy', priority=80)
    account(store, 'free', priority=1)
    _, first = key(store)
    # Zero balance may still cover an account-specific free quote. The native
    # preflight, not a cached balance cutoff, decides whether to activate.
    assert reserve(store, first)['account_id'] == 'empty'
    _, second = key(store)
    assert reserve(store, second)['account_id'] == 'busy'
    _, third = key(store)
    assert reserve(store, third)['account_id'] == 'free'


def test_account_capability_is_not_implicitly_all_models(store):
    store.create_account({'id': 'a', 'name': 'a', 'session_id': 'credential'})
    _, principal = key(store)
    with pytest.raises(PlatformError) as error:
        reserve(store, principal)
    assert error.value.code == 'no_available_account'


def test_workspace_without_auth_is_not_a_configured_credential(store):
    item = store.create_account({'id': 'a', 'name': 'a', 'workspace_id': '12345', 'capabilities': [MODEL]})
    assert item['secret_configured'] is False
    _, principal = key(store)
    with pytest.raises(PlatformError) as error:
        reserve(store, principal)
    assert error.value.code == 'no_available_account'


def test_observed_task_id_cannot_be_erased(store):
    account(store)
    _, principal = key(store)
    task = reserve(store, principal)
    store.update_task(task['id'], submit_id='observed-id', status='submitted')
    with pytest.raises(PlatformError, match='identifier cannot be replaced'):
        store.update_task(task['id'], submit_id=None)


def test_worker_must_be_online_and_ready_when_worker_provider_present(store):
    account(store)
    _, principal = key(store)
    store.worker_status = lambda: [{'account_id': 'a', 'workers': [{'online': True, 'ready': False}]}]
    with pytest.raises(PlatformError) as error:
        reserve(store, principal)
    assert error.value.code == 'no_available_account'
    store.worker_status = lambda: [{'account_id': 'a', 'workers': [{'online': True, 'ready': True}]}]
    assert reserve(store, principal)['account_id'] == 'a'


def test_idempotency_replays_same_task_and_does_not_consume_quota(store):
    account(store)
    _, principal = key(store, request_limit=1)
    first = reserve(store, principal, idempotency_key='once')
    replay = reserve(store, principal, idempotency_key='once')
    assert first['_created'] is True and replay['_created'] is False
    assert first['id'] == replay['id']
    assert store.list_keys()[0]['used_requests'] == 1
    with pytest.raises(PlatformError) as error:
        store.reserve_task(principal, 'video', MODEL, {'prompt': 'different'}, idempotency_key='once')
    assert error.value.code == 'idempotency_conflict'


def test_parallel_idempotency_and_dispatch_are_single_claim(store):
    account(store)
    _, principal = key(store)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: reserve(store, principal, idempotency_key='parallel'), range(10)))
    assert len({r['id'] for r in results}) == 1
    assert sum(r['_created'] for r in results) == 1
    task_id = results[0]['id']
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(lambda _: store.claim_dispatch(task_id), range(10)))
    assert sum(claims) == 1


def test_uncertain_tasks_keep_slot_and_cannot_be_requeued(store):
    account(store, max_concurrent=5, max_inflight=5)
    _, principal = key(store, max_concurrent=10)
    task = reserve(store, principal)
    store.update_task(task['id'], status='uncertain')
    with pytest.raises(PlatformError):
        store.update_task(task['id'], status='queued')
    # The uncertain task keeps its own slot, not the whole account. Other
    # independently guarded slots may proceed until the account is full.
    others=[reserve(store,principal) for _ in range(4)]
    assert all(other['id']!=task['id'] for other in others)
    assert store.get_task(task['id'])['status']=='uncertain'
    with pytest.raises(PlatformError) as error:
        reserve(store, principal)
    assert error.value.code == 'no_available_account'


def test_terminal_state_releases_slots_but_never_resubmits(store):
    account(store)
    _, principal = key(store)
    task = reserve(store, principal)
    store.update_task(task['id'], status='completed', result={'videos': [{'url': 'https://example.test/video'}]})
    assert reserve(store, principal)['id'] != task['id']
    with pytest.raises(PlatformError):
        store.update_task(task['id'], status='queued')


def test_concurrency_request_and_rate_limits_are_distinct(store):
    account(store, max_concurrent=8, max_inflight=8)
    _, p = key(store, max_concurrent=1)
    first = reserve(store, p)
    with pytest.raises(PlatformError) as error:
        reserve(store, p)
    assert error.value.code == 'concurrency_limit'
    store.update_task(first['id'], status='failed')
    _, limited = key(store, request_limit=0)
    with pytest.raises(PlatformError) as error:
        reserve(store, limited)
    assert error.value.code == 'insufficient_quota'
    _, rate = key(store, rate_limit_per_minute=1, max_concurrent=10)
    reserve(store, rate)
    with pytest.raises(PlatformError) as error:
        reserve(store, rate)
    assert error.value.code == 'rate_limit_exceeded'


def test_dashboard_uses_real_task_counts(store):
    account(store)
    _, p = key(store)
    task = reserve(store, p)
    store.update_task(task['id'], status='completed', result={})
    summary = store.dashboard()
    assert summary['summary']['tasks_total'] == 1
    assert summary['summary']['success_rate'] == 100
    assert summary['summary']['tasks_completed'] == 1
    assert sum(x['requests'] for x in summary['daily_usage']) == 1


def test_password_and_session_tokens_hashed(store):
    assert store.bootstrap_admin('admin', 'strong-test-password')
    assert not store.bootstrap_admin('other', 'another-password')
    login = store.login_admin('admin', 'strong-test-password')
    assert store.authenticate_admin(login['token'])['user']['username'] == 'admin'
    with store.engine.connect() as conn:
        hashed = conn.execute(select(sessions.c.token_hash)).scalar_one()
    assert login['token'] != hashed
    store.logout_admin(login['token'])
    with pytest.raises(PlatformError):
        store.authenticate_admin(login['token'])


def test_failed_login_rate_limit_commits_attempts(store):
    store.bootstrap_admin('admin', 'strong-test-password')
    for _ in range(10):
        with pytest.raises(PlatformError) as error:
            store.login_admin('admin', 'wrong', client_id='ip')
        assert error.value.status == 401
    with pytest.raises(PlatformError) as error:
        store.login_admin('admin', 'strong-test-password', client_id='ip')
    assert error.value.status == 429


@pytest.fixture
def admin_client(store):
    store.bootstrap_admin('admin', 'strong-test-password')
    app = FastAPI()
    async def check(account_id, settings):
        assert settings.session_id
        return {'status': 'ready', 'authenticated': True, 'balance': 100, 'cookie': 'MUST-NOT-RETURN'}
    app.include_router(create_admin_router(store, account_check=check))
    with TestClient(app) as client:
        yield client


def login(client):
    response = client.post('/api/admin/login', json={'username': 'admin', 'password': 'strong-test-password'})
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    return response.json()['csrf_token']


def test_admin_requires_session_and_csrf(admin_client):
    assert admin_client.get('/api/admin/accounts').status_code == 401
    csrf = login(admin_client)
    assert admin_client.get('/api/admin/me').status_code == 200
    assert admin_client.post('/api/admin/keys', json={'name': 'unsafe'}).status_code == 403
    response = admin_client.post('/api/admin/keys', json={'name': 'safe'}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 201 and response.json()['key'].startswith('sk-dr-')


def test_cross_origin_admin_actions_rejected(admin_client):
    assert admin_client.post('/api/admin/login', json={'username': 'admin', 'password': 'strong-test-password'},
                             headers={'Origin': 'https://evil.example'}).status_code == 403
    csrf = login(admin_client)
    assert admin_client.post('/api/admin/keys', json={'name': 'x'},
                             headers={'X-CSRF-Token': csrf, 'Origin': 'https://evil.example'}).status_code == 403


def test_admin_account_crud_check_and_key_revoke(admin_client):
    csrf = login(admin_client)
    headers = {'X-CSRF-Token': csrf}
    response = admin_client.post('/api/admin/accounts', json={'id': 'a', 'name': 'Account', 'session_id': 'private-value', 'capabilities': [MODEL]}, headers=headers)
    assert response.status_code == 201
    assert 'private-value' not in response.text
    checked = admin_client.post('/api/admin/accounts/a/check', headers=headers)
    assert checked.status_code == 200 and checked.json()['account']['balance'] == 100
    assert 'MUST-NOT-RETURN' not in checked.text
    created = admin_client.post('/api/admin/keys', json={'name': 'test', 'account_ids': ['a']}, headers=headers).json()
    assert admin_client.get('/api/admin/keys').json()['items'][0]['id'] == created['item']['id']
    revoked = admin_client.delete('/api/admin/keys/' + created['item']['id'], headers=headers)
    assert revoked.status_code == 200 and not revoked.json()['enabled']


def test_admin_logout_revokes_session(admin_client):
    csrf = login(admin_client)
    assert admin_client.post('/api/admin/logout', headers={'X-CSRF-Token': csrf}).status_code == 200
    assert admin_client.get('/api/admin/me').status_code == 401


def test_admin_settings_reject_secret_configuration(admin_client):
    csrf = login(admin_client)
    assert admin_client.patch('/api/admin/settings', json={'ADMIN_PASSWORD': 'anything'}, headers={'X-CSRF-Token': csrf}).status_code == 400
    result = admin_client.patch('/api/admin/settings', json={'platform_name': 'My Gateway', 'default_image_timeout': 120}, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200 and result.json()['platform_name'] == 'My Gateway'
