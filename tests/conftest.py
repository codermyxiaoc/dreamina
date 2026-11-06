import pytest


@pytest.fixture(autouse=True)
def disable_live_background_reconciliation(monkeypatch):
    # Unit tests must not poll credentials from the user's live local instance.
    monkeypatch.setenv('ENABLE_RECONCILER', '0')
    monkeypatch.setenv('WORKER_MODE','both')
    monkeypatch.setenv('DISABLE_LOGIN_MANAGER_LIFECYCLE','1')
