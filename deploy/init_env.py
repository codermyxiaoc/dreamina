"""Create a fresh self-hosted environment file without printing credentials."""
import argparse
from pathlib import Path
import secrets

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(Path(__file__).with_name('server.env')))
    args = parser.parse_args()
    path = Path(args.output).resolve()
    if path.exists():
        parser.error('The destination exists; it will not be overwritten.')
    values = {
        'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD': secrets.token_urlsafe(24),
        'PLATFORM_ENCRYPTION_KEY': Fernet.generate_key().decode(),
        'CHROME_BRIDGE_KEY': secrets.token_urlsafe(32),
        'CHROME_BRIDGE_EXTENSION_ID': 'eicelfibiacmlhfepgoiiemfgcbdfafm',
        'POSTGRES_PASSWORD': secrets.token_urlsafe(24),
        'LOCAL_API_KEY': 'internal-' + secrets.token_urlsafe(32),
        'FORWARDED_ALLOW_IPS': '127.0.0.1',
    }
    with path.open('x', encoding='utf-8') as handle:
        handle.write('# Generated local secrets. Keep this file private.\n')
        handle.write('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
    try: path.chmod(0o600)
    except OSError: pass
    print(f'Created {path}. Review proxy trust settings before deployment. No credential values displayed.')


if __name__ == '__main__':
    main()
