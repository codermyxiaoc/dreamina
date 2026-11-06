from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> dict[str, str]:
    values = {}
    env_path = path or ROOT / '.env'
    if env_path.exists():
        for line in env_path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"').strip("'")
    values.update(os.environ)
    return values


@dataclass(repr=False)
class Settings:
    session_id: str = ''
    cookie: str = ''
    region: str = 'US'
    proxy: str = ''
    workspace_id: str = ''
    api_key: str = ''
    signing: str = 'none'
    ms_token: str = ''
    device_id: str = ''
    api_origin: str = ''
    account_id: str = ''
    egress: object | None = None

    @classmethod
    def load(cls):
        e = load_env()
        return cls(session_id=e.get('DREAMINA_SESSION_ID', ''),
                   cookie=e.get('DREAMINA_COOKIE', ''),
                   region=e.get('DREAMINA_REGION', 'US').upper(),
                   proxy=e.get('DREAMINA_PROXY', ''),
                   workspace_id=e.get('DREAMINA_WORKSPACE_ID', ''),
                   api_key=e.get('LOCAL_API_KEY', ''),
                   signing=e.get('DREAMINA_SIGNING', 'offline'),
                   ms_token=e.get('DREAMINA_MS_TOKEN', ''),
                   device_id=e.get('DREAMINA_DEVICE_ID', ''),
                   api_origin=e.get('DREAMINA_API_ORIGIN', ''))
