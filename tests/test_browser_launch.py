import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from dreamina_api.account_login import AccountLoginManager, LoginSession
from dreamina_api.browser_launch import COMPATIBILITY_ARGS, parse_browser_args
from dreamina_api.browser_worker import Worker, safe_config
from dreamina_api.managed_workers import ManagedWorkers


REQUESTED_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-web-security',
    '--disable-features=IsolateOrigins,site-per-process',
    '--disable-dev-shm-usage',
    '--disable-accelerated-2d-canvas',
    '--disable-gpu',
]


def test_requested_argument_strings_are_preserved_in_order():
    assert isinstance(COMPATIBILITY_ARGS, tuple)
    assert list(COMPATIBILITY_ARGS) == REQUESTED_ARGS
    for value in (REQUESTED_ARGS, json.dumps(REQUESTED_ARGS)):
        assert parse_browser_args(value) == REQUESTED_ARGS
    original = REQUESTED_ARGS.copy()
    parsed = parse_browser_args(original)
    parsed.pop()
    assert original == REQUESTED_ARGS


@pytest.mark.parametrize('value', [None, '', [], '[]'])
def test_absent_configuration_does_not_add_browser_flags(value):
    assert parse_browser_args(value) == []


@pytest.mark.parametrize('value', [
    12, True, {}, (), 'null', 'true', '12', '{}', '"--disable-gpu"',
    '--disable-gpu', ['--disable-gpu', '--disable-gpu'], [None], [True], [12],
    ['--user-data-dir=private-profile'], ['--remote-debugging-port=9222'],
    ['--disable-gpu\n--no-sandbox'], ['--disable-gpu --no-sandbox'],
])
def test_invalid_or_unapproved_flags_fail_closed(value):
    with pytest.raises(ValueError):
        parse_browser_args(value)


def test_validation_error_does_not_echo_configuration_values():
    for value in ['["private-synthetic-secret"', ['--proxy-server=private-synthetic-secret']]:
        with pytest.raises(ValueError) as error:
            parse_browser_args(value)
        assert 'private-synthetic-secret' not in str(error.value)


def worker_config(tmp_path, args):
    path = tmp_path / 'worker.json'
    path.write_text(json.dumps({
        'server_url': 'http://127.0.0.1:8787',
        'worker_key': 'synthetic-worker-key-' + 'x' * 32,
        'accounts': [{'id': 'one', 'profile_dir': 'profiles/one', 'headless': True,
                      'channel': 'chromium', 'args': args}],
    }), encoding='utf-8')
    return path


def test_worker_config_retains_each_argument_as_one_list_item(tmp_path):
    cfg = safe_config(worker_config(tmp_path, REQUESTED_ARGS), 'one')
    assert cfg['args'] == REQUESTED_ARGS
    assert cfg['profile_dir'] == tmp_path / 'profiles' / 'one'
    with pytest.raises(ValueError):
        safe_config(worker_config(tmp_path, ['--user-data-dir=private-profile']), 'one')


def test_worker_passes_requested_arguments_to_browser_launch(tmp_path):
    async def run():
        cfg = safe_config(worker_config(tmp_path, REQUESTED_ARGS), 'one')
        worker = Worker(cfg)
        calls = []

        async def noop(*args, **kwargs):
            return None

        context = SimpleNamespace(pages=[SimpleNamespace(goto=noop)], close=noop)

        async def launch(profile, **options):
            calls.append((profile, options))
            return context

        worker.heartbeat = noop
        try:
            await worker.open(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)))
        finally:
            await worker.close()
        assert len(calls) == 1
        assert calls[0][0] == str(tmp_path / 'profiles' / 'one')
        assert calls[0][1]['args'] == REQUESTED_ARGS+['--disk-cache-size=268435456']
        assert calls[0][1]['headless'] is True

    asyncio.run(run())


class FakeStore:
    def get_account(self, account_id):
        return {'id': account_id, 'enabled': True, 'status': 'ready',
                'browser_profile_saved': True, 'active_tasks': 0}

    def settings_for_account(self, account_id):
        return SimpleNamespace(proxy=None)

    def profile_cipher_key(self, account_id):
        return b'synthetic-per-account-key'


def test_login_context_receives_the_same_configured_arguments(tmp_path):
    class LaunchCaptured(Exception):
        pass

    async def run():
        calls = []

        async def capture(account, profile, options):
            calls.append((account, profile, options))
            raise LaunchCaptured()

        manager = AccountLoginManager(FakeStore(), None, None, root=tmp_path,
            config={'LOGIN_BROWSER_ARGS': json.dumps(REQUESTED_ARGS)}, context_factory=capture)
        session = LoginSession('test-session', 'one', 'owner', time.time(), time.time() + 900)
        try:
            with pytest.raises(LaunchCaptured):
                await manager._open(session)
        finally:
            await manager._close_context(session)
        assert len(calls) == 1
        assert calls[0][0] == 'one'
        assert calls[0][1] == tmp_path / 'profiles' / 'one'
        assert calls[0][2]['args'] == REQUESTED_ARGS+['--disk-cache-size=268435456']
        assert not (tmp_path / 'profiles' / 'one' / '.dreamina-worker.lock').exists()

    asyncio.run(run())


def test_supervisor_serializes_arguments_for_its_worker(tmp_path):
    async def run():
        calls = []

        def spawn(command, **options):
            calls.append(command)
            return SimpleNamespace(pid=321001, poll=lambda: None)

        supervisor = ManagedWorkers(FakeStore(), None, root=tmp_path, config={
            'CHROME_BRIDGE_KEY': 'synthetic-worker-key-' + 'x' * 32,
            'LOGIN_BROWSER_ARGS': json.dumps(REQUESTED_ARGS),
        }, process_factory=spawn, alive=lambda pid: False)
        paths = supervisor.paths('one')
        paths.auth.parent.mkdir(parents=True)
        paths.auth.write_bytes(b'synthetic-encrypted-state')
        result = await supervisor.start('one')
        assert result['state'] == 'starting'
        assert len(calls) == 1
        assert safe_config(paths.config, 'one')['args'] == REQUESTED_ARGS

    asyncio.run(run())
