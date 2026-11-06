import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dreamina_api.browser_worker import safe_config
from dreamina_api.managed_workers import ManagedWorkers
from dreamina_api.platform_store import PlatformError


class FakeStore:
    def __init__(self):
        self.accounts = {
            name: dict(id=name, enabled=True, status='ready', browser_profile_saved=True,
                       active_tasks=0, last_login_at=10, balance=42)
            for name in ('first', 'second')
        }
        self.checked = []

    def get_account(self, account_id):
        return self.accounts[account_id].copy()

    def list_accounts(self):
        return [a.copy() for a in self.accounts.values()]

    def settings_for_account(self, account_id):
        return SimpleNamespace(proxy=None)

    def profile_cipher_key(self, account_id):
        return b'test-only-derived-account-key'

    def record_account_check(self, account_id, **values):
        self.checked.append((account_id, values))
        self.accounts[account_id].update(values)


class FakeBridge:
    def __init__(self):
        self.records = []

    def accounts(self):
        return self.records


class FakeProcess:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        return self.returncode

    def terminate(self):
        raise AssertionError('Workers must not be terminated.')

    def kill(self):
        raise AssertionError('Workers must not be killed.')


class Harness:
    def __init__(self, root, config=None):
        self.store, self.bridge = FakeStore(), FakeBridge()
        self.calls, self.processes = [], {}
        self.time, self.on_sleep = 100.0, None
        self.config = {'CHROME_BRIDGE_KEY': 'synthetic-worker-key-' + 'a' * 32,
                       'MANAGED_STOP_TIMEOUT': 1} | (config or {})
        self.worker = self.supervisor(root)
        for name in self.store.accounts:
            path = self.worker.paths(name).auth
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'test-only-encrypted-snapshot')

    def spawn(self, command, **options):
        process = FakeProcess(1001 + len(self.calls))
        self.calls.append((command, options))
        self.processes[process.pid] = process
        return process

    def alive(self, pid):
        return pid in self.processes and self.processes[pid].poll() is None

    async def sleep(self, seconds):
        self.time += seconds
        if self.on_sleep:
            self.on_sleep()

    def supervisor(self, root=None):
        return ManagedWorkers(self.store, self.bridge, config=self.config,
                              root=root or self.worker.root,
                              process_factory=self.spawn, alive=self.alive,
                              process_identity=self.identity,
                              clock=lambda: self.time, sleep=self.sleep)

    def identity(self,pid):
        index=pid-1001
        if 0<=index<len(self.calls):return {'argv':self.calls[index][0],'identity':'fixture-birth-'+str(pid)}
        return None

    def report(self, account_id='first', state='ready', **changes):
        paths = self.worker.paths(account_id)
        record = json.loads(paths.registry.read_text())
        value = dict(account_id=account_id, pid=record['pid'], instance_id=record['instance_id'],
                     state=state, updated_at=self.time, reason='sensitive raw diagnostic') | changes
        paths.status.write_text(json.dumps(value), encoding='utf-8')


def run(awaitable):
    return asyncio.run(awaitable)


def test_start_uses_only_dedicated_paths_and_filtered_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_PASSWORD', 'must-not-be-inherited')
    monkeypatch.setenv('PLATFORM_ENCRYPTION_KEY', 'must-not-be-inherited')
    monkeypatch.setenv('DREAMINA_COOKIE', 'must-not-be-inherited')
    monkeypatch.setenv('DREAMINA_SESSION_ID', 'must-not-be-inherited')
    monkeypatch.setenv('UNRELATED_API_KEY', 'must-not-be-inherited')
    h = Harness(tmp_path)
    paths = h.worker.paths('first')
    paths.state.mkdir(parents=True)
    paths.stop.write_text('old stop request')
    other = paths.state / 'must-remain.txt'
    other.write_text('retain')
    result = run(h.worker.start('first'))
    assert result['state'] == 'starting'
    assert not paths.stop.exists() and other.exists()
    command, options = h.calls[0]
    assert command[1:4] == ['-m', 'dreamina_api.browser_worker', 'run']
    assert command[-2:] == ['--account', 'first']
    assert options['close_fds'] and options['stdin'] < 0
    assert options['stdout'].name == str(paths.directory / 'worker.stdout.log')
    assert options['stderr'].name == str(paths.directory / 'worker.stderr.log')
    assert options['env']['DREAMINA_PROFILE_KEY'] == 'test-only-derived-account-key'
    assert options['env'].get('PATH') == os.environ.get('PATH')
    assert not any(value == 'must-not-be-inherited' for value in options['env'].values())
    if os.name == 'nt':
        assert options['creationflags']
    else:
        assert options['start_new_session']
        assert paths.config.stat().st_mode & 0o777 == 0o600
    cfg = safe_config(paths.config, 'first')
    assert cfg['profile_dir'] == tmp_path / 'profiles' / 'first'
    assert cfg['state_dir'] == tmp_path / 'workers' / 'first' / 'worker-state' / 'first'
    assert cfg['encrypted_state_file'] == tmp_path / 'auth' / 'first.enc'
    assert cfg['headless'] is True
    assert cfg['instance_id']
    assert 'worker_key' not in result and 'DREAMINA_PROFILE_KEY' not in json.dumps(result)


def test_duplicate_start_reuses_process_and_restart_verifies_instance(tmp_path):
    h = Harness(tmp_path)
    first = run(h.worker.start('first'))
    assert run(h.worker.start('first'))['pid'] == first['pid']
    assert len(h.calls) == 1
    recovered = h.supervisor()
    assert recovered.status('first')['state'] == 'running_unverified'
    h.report()
    assert recovered.status('first')['state'] == 'running'
    assert run(recovered.start('first'))['pid'] == first['pid']
    assert len(h.calls) == 1
    h.report(instance_id='different-process-instance')
    assert recovered.status('first')['state'] == 'running_unverified'
    assert recovered.status('first')['worker_state'] is None


def test_windows_chrome_install_paths_survive_environment_filter_without_proxy_or_secrets(tmp_path, monkeypatch):
    install_paths={
        'ProgramFiles':r'C:\Synthetic Program Files',
        'ProgramFiles(x86)':r'C:\Synthetic Program Files (x86)',
        'ProgramW6432':r'C:\Synthetic Native Program Files',
    }
    for name,value in install_paths.items():monkeypatch.setenv(name,value)
    private_names=('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','ADMIN_PASSWORD',
                   'PLATFORM_ENCRYPTION_KEY','DREAMINA_COOKIE','DREAMINA_SESSION_ID','UNRELATED_API_KEY')
    for name in private_names:monkeypatch.setenv(name,'synthetic-must-not-inherit')
    h=Harness(tmp_path)
    assert run(h.worker.start('first'))['state']=='starting'
    inherited={name.upper():value for name,value in h.calls[0][1]['env'].items()}
    for name,value in install_paths.items():assert inherited[name.upper()]==value
    assert all(name not in inherited for name in private_names)
    assert 'synthetic-must-not-inherit' not in inherited.values()
    assert inherited['DREAMINA_PROFILE_KEY']=='test-only-derived-account-key'


def test_worker_capacity_keeps_profile_saved_without_claiming_ready(tmp_path):
    h = Harness(tmp_path, {'MANAGED_MAX_WORKERS': 1})
    async def simultaneous():
        return await asyncio.gather(h.worker.start('first'), h.worker.start('second'))
    results = run(simultaneous())
    assert [result['state'] for result in results] == ['starting', 'saved_capacity_wait']
    assert results[1]['profile_saved'] is True and 'pid' not in results[1]
    assert len(h.calls) == 1
    recovered = h.supervisor()
    assert run(recovered.start('second'))['state'] == 'saved_capacity_wait'
    assert len(h.calls) == 1


@pytest.mark.parametrize('values,code', [
    ({'browser_profile_saved': False}, 'profile_missing'),
    ({'status': 'login_pending'}, 'login_busy'),
])
def test_start_refuses_unsaved_or_currently_editing_profiles(tmp_path, values, code):
    h = Harness(tmp_path)
    h.store.accounts['first'].update(values)
    with pytest.raises(PlatformError) as error:
        run(h.worker.start('first'))
    assert error.value.code == code and error.value.status == 409
    assert not h.calls


def test_missing_snapshot_and_disabled_account_do_not_start(tmp_path):
    h = Harness(tmp_path)
    h.worker.paths('first').auth.unlink()
    with pytest.raises(PlatformError, match='snapshot'):
        run(h.worker.start('first'))
    h.store.accounts['first']['enabled'] = False
    assert run(h.worker.start('first'))['state'] == 'disabled'
    assert not h.calls


@pytest.mark.parametrize('config,code', [
    ({'CHROME_BRIDGE_KEY': ''}, 'worker_key_missing'),
    ({'INTERNAL_API_URL': 'http://external.example'}, 'worker_config_invalid'),
])
def test_invalid_worker_configuration_prevents_process_start(tmp_path, config, code):
    h = Harness(tmp_path, config)
    with pytest.raises(PlatformError) as error:
        run(h.worker.start('first'))
    assert error.value.code == code
    assert not h.calls


@pytest.mark.parametrize('bridge_state', ['queued', 'claimed', 'submitted', 'uncertain'])
def test_stop_preserves_unfinished_browser_work(tmp_path, bridge_state):
    h = Harness(tmp_path)
    run(h.worker.start('first'))
    h.bridge.records = [{'account_id': 'first', 'jobs': {bridge_state: 1}}]
    with pytest.raises(PlatformError) as error:
        run(h.worker.stop('first'))
    assert error.value.code == 'account_busy'
    assert not h.worker.paths('first').stop.exists()


def test_stop_preserves_active_platform_tasks(tmp_path):
    h = Harness(tmp_path)
    run(h.worker.start('first'))
    h.store.accounts['first']['active_tasks'] = 1
    with pytest.raises(PlatformError) as error:
        run(h.worker.stop('first'))
    assert error.value.code == 'account_busy'
    assert not h.worker.paths('first').stop.exists()


def test_stop_graceful_and_login_pending_does_not_count_as_active_task(tmp_path):
    h = Harness(tmp_path)
    result = run(h.worker.start('first'))
    h.store.accounts['first']['status'] = 'login_pending'
    def exit_when_requested():
        assert h.worker.paths('first').stop.is_file()
        h.processes[result['pid']].returncode = 0
    h.on_sleep = exit_when_requested
    assert run(h.worker.stop('first'))['state'] == 'stopped'
    assert h.worker.status('first')['state'] == 'stopped'
    assert run(h.worker.stop('first'))['state'] == 'stopped'


def test_unverified_registry_stop_timeout_never_kills_a_pid(tmp_path):
    h = Harness(tmp_path)
    result = run(h.worker.start('first'))
    recovered = h.supervisor()
    with pytest.raises(PlatformError) as error:
        run(recovered.stop('first'))
    assert error.value.code == 'worker_stop_pending' and error.value.status == 409
    assert h.processes[result['pid']].returncode is None
    assert recovered.status('first')['state'] == 'running_unverified'
    assert recovered.paths('first').stop.exists()


def test_status_does_not_expose_raw_errors_or_configuration(tmp_path):
    h = Harness(tmp_path)
    run(h.worker.start('first'))
    h.report(state='worker_error', reason='Cookie=session-secret', error='key=profile-key')
    value = json.dumps(h.worker.status('first'))
    assert all(secret not in value for secret in ['Cookie', 'session-secret', 'profile-key', 'synthetic-worker-key'])
    assert 'execution problem' in value


@pytest.mark.parametrize('state', ['login_required', 'verification_required'])
def test_scan_marks_only_verified_explicit_login_loss_and_deduplicates(tmp_path, state):
    h = Harness(tmp_path)
    run(h.worker.start('first'))
    h.report(state=state)
    run(h.worker.scan())
    run(h.worker.scan())
    assert h.store.checked == [('first', {'status': 'login_required', 'balance': 42})]


@pytest.mark.parametrize('state', ['worker_error', 'page_unavailable', 'attention_required', 'starting', 'ready'])
def test_scan_does_not_mislabel_execution_failures_as_expired_login(tmp_path, state):
    h = Harness(tmp_path)
    run(h.worker.start('first'))
    h.report(state=state)
    run(h.worker.scan())
    assert not h.store.checked


@pytest.mark.parametrize('change', ['new_login', 'login_pending', 'wrong_instance'])
def test_scan_does_not_demote_new_login_or_unverified_worker(tmp_path, change):
    h = Harness(tmp_path)
    run(h.worker.start('first'))
    h.report(state='login_required')
    if change == 'new_login':
        h.store.accounts['first']['last_login_at'] = h.time + 1
    elif change == 'login_pending':
        h.store.accounts['first']['status'] = 'login_pending'
    else:
        h.report(state='login_required', instance_id='wrong-instance')
    run(h.worker.scan())
    assert not h.store.checked


def test_corrupt_registry_prevents_start_without_touching_other_processes(tmp_path):
    h = Harness(tmp_path)
    paths = h.worker.paths('first')
    paths.directory.mkdir(parents=True)
    paths.registry.write_text('{invalid')
    with pytest.raises(PlatformError) as error:
        run(h.worker.start('first'))
    assert error.value.code == 'worker_registry_invalid'
    assert not h.calls


@pytest.mark.parametrize('account_id', ['../outside', 'a/b', '..', 'a\\b', ''])
def test_account_id_cannot_escape_managed_paths(tmp_path, account_id):
    h = Harness(tmp_path)
    with pytest.raises(PlatformError) as error:
        h.worker.paths(account_id)
    assert error.value.code == 'invalid_account'


def test_rejects_path_symlinks_outside_managed_root(tmp_path):
    h = Harness(tmp_path / 'managed')
    outside = tmp_path / 'outside'
    outside.mkdir()
    profile = h.worker.paths('first').profile
    profile.parent.mkdir(parents=True)
    try:
        profile.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating symlinks is unavailable on this host.')
    with pytest.raises(PlatformError) as error:
        h.worker.paths('first')
    assert error.value.code == 'invalid_worker_path'


@pytest.mark.parametrize('config', [
    {'MANAGED_MAX_WORKERS': 0}, {'MANAGED_MAX_WORKERS': True},
    {'MANAGED_STOP_TIMEOUT': 120}, {'LOGIN_BROWSER_HEADLESS': 'perhaps'},
])
def test_invalid_supervisor_limits_fail_early(tmp_path, config):
    with pytest.raises(ValueError):
        Harness(tmp_path, config)
