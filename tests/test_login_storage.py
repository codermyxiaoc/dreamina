"""Synthetic login-store tests: temporary databases only, no browser or account I/O."""
from concurrent.futures import ThreadPoolExecutor
import base64
import json
import sqlite3
import threading

from cryptography.fernet import Fernet, InvalidToken
import pytest
from sqlalchemy import select

from dreamina_api.platform_store import PlatformError, PlatformStore, SECRET_FIELDS, accounts


MODEL = 'seedance-2.0-mini'


class Clock:
    def __init__(self):
        self.now = 1800000000.0
    def __call__(self):
        return self.now


def ready_workers():
    return [{'account_id': name, 'workers': [{'online': True, 'ready': True}]}
            for name in ('first', 'second')]


@pytest.fixture
def store(tmp_path):
    key = Fernet.generate_key()
    db = tmp_path / 'login.sqlite'
    value = PlatformStore('sqlite:///' + db.as_posix(), encryption_key=key,
                          worker_status=ready_workers, clock=Clock())
    value.test_key = key
    value.test_db = db
    yield value
    value.engine.dispose()


def account(store, ident='first'):
    return store.create_account({'id': ident, 'name': ident, 'region': 'US',
                                 'capabilities': [MODEL], 'session_id': 'synthetic-old-' + ident,
                                 'cookie': 'sessionid=synthetic-old-' + ident})


def credentials(ident='new'):
    return {'session_id': 'synthetic-session-' + ident,
            'cookie': 'sessionid=synthetic-session-' + ident + '; store-idc=useast5',
            'api_origin': 'https://dreamina-api.us.capcut.com',
            'workspace_id': ''}


def finish(store, account_id='first', identity='upstream-111', label='new'):
    return store.complete_browser_login(account_id, credentials=credentials(label),
                                        identity=identity, browser_profile='managed/' + account_id,
                                        balance=123.0)


def principal(store):
    return store.authenticate_key(store.create_key({'name': 'Synthetic test key'})['key'])


def ciphertext(store, account_id):
    with store.engine.connect() as connection:
        return connection.execute(select(accounts.c.secret_encrypted).where(accounts.c.id == account_id)).scalar_one()


def test_old_database_is_migrated_without_losing_account_or_secret(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    key = Fernet.generate_key()
    secret = Fernet(key).encrypt(json.dumps({'session_id': 'legacy-synthetic-session'}).encode()).decode()
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE platform_accounts (
          id VARCHAR(64) PRIMARY KEY, name VARCHAR(120) NOT NULL, region VARCHAR(8) NOT NULL,
          enabled BOOLEAN NOT NULL, priority INTEGER NOT NULL, max_concurrent INTEGER NOT NULL,
          capabilities_json TEXT NOT NULL, secret_encrypted TEXT, status VARCHAR(40) NOT NULL,
          balance FLOAT, last_checked_at FLOAT, last_used_at FLOAT,
          created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL)''')
        db.execute('INSERT INTO platform_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('first', 'Legacy account', 'US', True, 2, 1, json.dumps([MODEL]), secret,
                    'ready', 42, 100, None, 10, 100))
    migrated = PlatformStore('sqlite:///' + path.as_posix(), encryption_key=key)
    try:
        public = migrated.get_account('first')
        assert public['name'] == 'Legacy account'
        assert public['balance'] == 42 and public['status'] == 'ready'
        assert public['browser_profile_saved'] is False
        assert public['identity_hint'] is None and public['last_login_at'] is None
        assert migrated.settings_for_account('first').session_id == 'legacy-synthetic-session'
        assert ciphertext(migrated, 'first') == secret
        with migrated.engine.connect() as connection:
            columns = {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(platform_accounts)')}
            indexes = {row[1] for row in connection.exec_driver_sql('PRAGMA index_list(platform_accounts)')}
        assert {'identity_digest', 'browser_profile', 'last_login_at'} <= columns
        assert 'uq_platform_account_identity' in indexes
    finally:
        migrated.engine.dispose()


def test_login_pending_is_not_schedulable_even_with_ready_worker(store):
    account(store)
    caller = principal(store)
    result = store.begin_browser_login('first')
    assert result['status'] == 'login_pending'
    with pytest.raises(PlatformError) as failure:
        store.reserve_task(caller, 'video', MODEL, {'prompt': 'Synthetic scene'})
    assert failure.value.code == 'no_available_account'
    assert store.list_keys()[0]['used_requests'] == 0


@pytest.mark.parametrize('state', ['queued', 'claimed', 'submitted', 'in_progress', 'uncertain'])
def test_any_unfinished_task_blocks_new_login(store, state):
    account(store)
    task = store.reserve_task(principal(store), 'video', MODEL, {'prompt': 'Synthetic scene'})
    if state != 'queued':
        store.update_task(task['id'], status=state)
    with pytest.raises(PlatformError) as failure:
        store.begin_browser_login('first')
    assert failure.value.code == 'account_busy'
    assert store.get_account('first')['status'] != 'login_pending'


def test_second_login_start_does_not_steal_existing_login(store):
    account(store)
    store.begin_browser_login('first')
    with pytest.raises(PlatformError) as failure:
        store.begin_browser_login('first')
    assert failure.value.code == 'login_busy'


@pytest.mark.parametrize('field', sorted(SECRET_FIELDS))
def test_ordinary_credential_updates_cannot_release_login_ownership(store, field):
    account(store)
    before = ciphertext(store, 'first')
    store.begin_browser_login('first')
    with pytest.raises(PlatformError) as failure:
        store.update_account('first', {field: 'synthetic-interfering-value'})
    assert failure.value.code == 'login_busy' and failure.value.status == 409
    assert store.get_account('first')['status'] == 'login_pending'
    assert ciphertext(store, 'first') == before


@pytest.mark.parametrize('status', ['ready', 'unverified', 'login_required', 'error'])
def test_late_health_check_cannot_release_login_ownership(store, status):
    account(store)
    store.record_account_check('first', status='ready', balance=42)
    before = store.get_account('first')
    store.begin_browser_login('first')
    store.clock.now += 30
    with pytest.raises(PlatformError) as failure:
        store.record_account_check('first', status=status, balance=999)
    assert failure.value.code == 'login_busy' and failure.value.status == 409
    after = store.get_account('first')
    assert after['status'] == 'login_pending'
    assert after['balance'] == before['balance']
    assert after['last_checked_at'] == before['last_checked_at']


def test_non_secret_account_edits_preserve_login_ownership(store):
    account(store)
    store.begin_browser_login('first')
    result = store.update_account('first', {'name': 'Renamed during login', 'enabled': False})
    assert result['name'] == 'Renamed during login' and result['enabled'] is False
    assert result['status'] == 'login_pending'


def test_verified_login_sync_is_encrypted_and_public_output_contains_no_secrets(store):
    account(store)
    before = ciphertext(store, 'first')
    store.begin_browser_login('first')
    store.clock.now += 20
    public = finish(store)
    saved = store.settings_for_account('first')
    assert saved.session_id == credentials()['session_id']
    assert saved.cookie == credentials()['cookie']
    assert saved.workspace_id == ''
    assert public['status'] == 'ready' and public['browser_profile_saved'] is True
    assert public['last_login_at'] == store.clock.now
    assert public['last_checked_at'] == store.clock.now
    assert public['balance'] == 123
    assert len(public['identity_hint']) == 8
    serialized = json.dumps(store.list_accounts())
    for value in (credentials()['session_id'], credentials()['cookie'], 'upstream-111'):
        assert value not in serialized
        assert value not in ciphertext(store, 'first')
    assert ciphertext(store, 'first') != before


def test_duplicate_website_identity_does_not_overwrite_either_account(store):
    account(store, 'first'); account(store, 'second')
    store.begin_browser_login('first'); finish(store)
    first_before = ciphertext(store, 'first'); second_before = ciphertext(store, 'second')
    store.begin_browser_login('second')
    with pytest.raises(PlatformError) as failure:
        finish(store, 'second', label='must-not-save')
    assert failure.value.code == 'duplicate_identity'
    assert ciphertext(store, 'first') == first_before
    assert ciphertext(store, 'second') == second_before
    assert store.get_account('second')['status'] == 'login_pending'
    assert not store.get_account('second')['browser_profile_saved']


def test_relogin_with_different_identity_preserves_old_cookie_and_binding(store):
    account(store); store.begin_browser_login('first'); finish(store)
    before = ciphertext(store, 'first'); hint = store.get_account('first')['identity_hint']
    store.begin_browser_login('first')
    with pytest.raises(PlatformError) as failure:
        finish(store, identity='different-account', label='wrong')
    assert failure.value.code == 'identity_mismatch'
    assert ciphertext(store, 'first') == before
    assert store.get_account('first')['identity_hint'] == hint
    assert store.settings_for_account('first').session_id == credentials()['session_id']


def test_aborting_failed_relogin_preserves_existing_binding_and_credentials(store):
    account(store); store.begin_browser_login('first'); finish(store)
    before_secret = ciphertext(store, 'first')
    before = store.get_account('first')
    store.begin_browser_login('first')
    with pytest.raises(PlatformError) as failure:
        finish(store, identity='wrong-upstream-account', label='must-not-save')
    assert failure.value.code == 'identity_mismatch'
    assert store.get_account('first')['status'] == 'login_pending'
    result = store.abort_browser_login('first')
    assert result['status'] == 'login_required'
    assert ciphertext(store, 'first') == before_secret
    for field in ('browser_profile_saved', 'identity_hint', 'last_login_at'):
        assert result[field] == before[field]
    with pytest.raises(PlatformError) as unavailable:
        store.reserve_task(principal(store), 'video', MODEL, {'prompt': 'Synthetic scene'})
    assert unavailable.value.code == 'no_available_account'


def test_abort_is_idempotent_and_does_not_downgrade_completed_login(store):
    account(store)
    store.begin_browser_login('first')
    assert store.abort_browser_login('first')['status'] == 'login_required'
    assert store.abort_browser_login('first')['status'] == 'login_required'
    store.begin_browser_login('first'); finish(store)
    before = store.get_account('first')
    before_secret = ciphertext(store, 'first')
    assert store.abort_browser_login('first') == before
    assert ciphertext(store, 'first') == before_secret


def test_relogin_same_identity_refreshes_secret_and_time(store):
    account(store); store.begin_browser_login('first'); finish(store)
    old_login = store.get_account('first')['last_login_at']
    hint = store.get_account('first')['identity_hint']
    store.clock.now += 50
    store.begin_browser_login('first')
    result = finish(store, label='refreshed')
    assert result['last_login_at'] > old_login
    assert result['identity_hint'] == hint
    assert store.settings_for_account('first').session_id == credentials('refreshed')['session_id']


@pytest.mark.parametrize('has_previous_login', [False, True])
def test_failed_browser_state_persistence_rolls_back_login_without_overwriting_account(store, has_previous_login):
    account(store)
    if has_previous_login:
        store.begin_browser_login('first')
        finish(store)
    store.begin_browser_login('first')
    with store.engine.connect() as connection:
        before = dict(connection.execute(select(accounts).where(accounts.c.id == 'first')).mappings().one())
    store.clock.now += 100
    attempts = []

    def fail_to_persist():
        attempts.append('persist')
        raise OSError('Synthetic state snapshot write failed')

    with pytest.raises(OSError, match='Synthetic state snapshot write failed'):
        store.complete_browser_login('first', credentials=credentials('must-not-save'),
                                     identity='upstream-111', browser_profile='replacement-profile',
                                     balance=999, persist_state=fail_to_persist)

    assert attempts == ['persist']
    with store.engine.connect() as connection:
        after = dict(connection.execute(select(accounts).where(accounts.c.id == 'first')).mappings().one())
    assert after == before
    assert after['status'] == 'login_pending'
    assert store.settings_for_account('first').session_id == (
        credentials()['session_id'] if has_previous_login else 'synthetic-old-first')
    result = store.abort_browser_login('first')
    assert result['status'] == 'login_required'
    assert result['browser_profile_saved'] is has_previous_login
    assert ciphertext(store, 'first') == before['secret_encrypted']


def test_completion_requires_active_login_ownership_state(store):
    account(store)
    with pytest.raises(PlatformError) as failure:
        finish(store)
    assert failure.value.code == 'login_conflict'


def test_profile_keys_are_account_scoped_and_stable_across_restart(store):
    key_a = store.profile_cipher_key('first')
    key_b = store.profile_cipher_key('second')
    assert key_a != key_b
    assert len(base64.urlsafe_b64decode(key_a)) == 32
    encoded = Fernet(key_a).encrypt(b'synthetic-cookie-state')
    with pytest.raises(InvalidToken):
        Fernet(key_b).decrypt(encoded)
    reopened = PlatformStore('sqlite:///' + store.test_db.as_posix(), encryption_key=store.test_key)
    try:
        assert reopened.profile_cipher_key('first') == key_a
        assert Fernet(reopened.profile_cipher_key('first')).decrypt(encoded) == b'synthetic-cookie-state'
    finally:
        reopened.engine.dispose()
    with pytest.raises(PlatformError):
        store.profile_cipher_key('../../other')


def test_recover_interrupted_login_never_marks_ready_or_loses_old_credentials(store):
    account(store); before = ciphertext(store, 'first')
    store.begin_browser_login('first')
    reopened = PlatformStore('sqlite:///' + store.test_db.as_posix(), encryption_key=store.test_key)
    try:
        reopened.recover_pending_logins()
        assert reopened.get_account('first')['status'] == 'login_required'
        assert ciphertext(reopened, 'first') == before
        assert reopened.settings_for_account('first').session_id == 'synthetic-old-first'
    finally:
        reopened.engine.dispose()


def test_concurrent_login_start_and_task_reservation_have_only_one_winner(store):
    account(store)
    caller = principal(store)
    second = PlatformStore('sqlite:///' + store.test_db.as_posix(), encryption_key=store.test_key,
                           worker_status=ready_workers)
    barrier = threading.Barrier(2)
    def begin():
        barrier.wait()
        try:
            store.begin_browser_login('first'); return 'login'
        except PlatformError as error:
            return error.code
    def reserve():
        barrier.wait()
        try:
            second.reserve_task(caller, 'video', MODEL, {'prompt': 'Synthetic scene'}); return 'task'
        except PlatformError as error:
            return error.code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(begin); other = pool.submit(reserve)
            results = {first.result(), other.result()}
        assert results in ({'login', 'no_available_account'}, {'task', 'account_busy'})
    finally:
        second.engine.dispose()


def test_concurrent_duplicate_identity_completions_only_save_one_account(store):
    account(store, 'first'); account(store, 'second')
    store.begin_browser_login('first'); store.begin_browser_login('second')
    barrier = threading.Barrier(2)
    def complete(account_id):
        barrier.wait()
        try:
            finish(store, account_id, label=account_id); return 'saved'
        except PlatformError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, ['first', 'second']))
    assert sorted(results) == ['duplicate_identity', 'saved']
    assert sum(item['browser_profile_saved'] for item in store.list_accounts()) == 1
