import pytest
from cryptography.fernet import Fernet
from dreamina_api.platform_store import PlatformStore,PlatformError

def test_bootstrap_default_policy_remains_twelve_but_explicit_deployment_override_works(tmp_path):
    store=PlatformStore('sqlite:///'+str(tmp_path/'db.sqlite3'),encryption_key=Fernet.generate_key())
    with pytest.raises(PlatformError,match='12 to 1024'):
        store.bootstrap_admin('admin','example9x')
    assert store.bootstrap_admin('admin','example9x',minimum_length=8)
    assert store.login_admin('admin','example9x')['token']
    assert store.bootstrap_admin('admin','different-password',minimum_length=8) is False
    assert store.login_admin('admin','example9x')['token']

@pytest.mark.parametrize('minimum',[0,7,129,True,'8',8.0])
def test_invalid_deployment_policy_fails_without_admin_creation(tmp_path,minimum):
    store=PlatformStore('sqlite:///'+str(tmp_path/'db.sqlite3'),encryption_key=Fernet.generate_key())
    with pytest.raises(PlatformError,match='minimum password length'):
        store.bootstrap_admin('admin','a-long-enough-password',minimum_length=minimum)
    assert store.bootstrap_admin('admin','a-long-enough-password')
