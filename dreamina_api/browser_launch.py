"""Explicit launch arguments for dedicated account browsers only."""
import json
import os
import re

PRIVATE_BROWSER_ENV=('DREAMINA_EGRESS','DREAMINA_PROFILE_KEY','DREAMINA_WORKER_KEY')
IGNORED_DEFAULT_ARGS=('--disable-background-timer-throttling',)
BROWSER_SYSTEM_ENV={
    'PATH','PATHEXT','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP',
    'PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','LOCALAPPDATA','APPDATA','USERPROFILE',
    'HOME','USER','USERNAME','LANG','LC_ALL','LC_CTYPE','TZ','DISPLAY','XAUTHORITY',
    'XDG_RUNTIME_DIR','XDG_CONFIG_HOME','XDG_CACHE_HOME','DBUS_SESSION_BUS_ADDRESS',
    'PLAYWRIGHT_BROWSERS_PATH','PLAYWRIGHT_NODEJS_PATH','LD_LIBRARY_PATH','VIRTUAL_ENV',
    'SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','FONTCONFIG_PATH','FONTCONFIG_FILE',
}


def browser_environment():
    # Construct a child-only environment; API process credentials remain intact
    # for concurrent requests but are never inherited by login Chromium.
    return {key:value for key,value in os.environ.items() if key.upper() in BROWSER_SYSTEM_ENV}


def is_browser_proxy_error(error):
    return bool(re.search(r'\bERR_(?:PROXY_CONNECTION_FAILED|TUNNEL_CONNECTION_FAILED|NO_SUPPORTED_PROXIES|PROXY_CERTIFICATE_INVALID|SOCKS_CONNECTION_FAILED|MANDATORY_PROXY_CONFIGURATION_FAILED|PROXY_AUTH_UNSUPPORTED|PROXY_AUTH_REQUESTED)\b',str(error)))


COMPATIBILITY_ARGS = (
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-web-security',
    '--disable-features=IsolateOrigins,site-per-process',
    '--disable-dev-shm-usage',
    '--disable-accelerated-2d-canvas',
    '--disable-gpu',
)

SAFETY_ARGS = (
    '--disk-cache-size=268435456',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--webrtc-ip-handling-policy=disable_non_proxied_udp',
)


def parse_browser_args(value=None):
    """Accept a bounded opt-in list; profile/debugging flags cannot be overridden."""
    if value is None or value == '':
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValueError('LOGIN_BROWSER_ARGS must be a JSON array of supported switches.') from error
    if not isinstance(value, list) or len(value) > len(COMPATIBILITY_ARGS + SAFETY_ARGS):
        raise ValueError('Browser args must be a list of supported switches.')
    if any(not isinstance(item, str) or item not in COMPATIBILITY_ARGS + SAFETY_ARGS for item in value):
        raise ValueError('Browser args contain an unsupported switch.')
    if len(set(value)) != len(value):
        raise ValueError('Browser args must not contain duplicate switches.')
    return list(value)


def managed_browser_args(value=None, *, proxied=False):
    result=parse_browser_args(value)
    for flag in SAFETY_ARGS[:1]+(SAFETY_ARGS[1:] if proxied else ()):
        if flag not in result:result.append(flag)
    return result
