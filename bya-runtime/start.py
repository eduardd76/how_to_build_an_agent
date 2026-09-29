"""Start BYA after checking what it needs. Run from bya-runtime/:  python3 start.py [--port 8787]

Checks Python, the model endpoint, PRTG, NetBox and a containerlab lab (whichever are set up), says what is
missing and how to fix it, then starts the studio. Settings come from the Settings page (config.local.json and
secrets.local.json) or the environment.
"""
import argparse
import socket
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f'BYA needs Python 3.11 or newer; this is {sys.version.split()[0]}.')

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bya import checks, settings  # noqa: E402


def mark(ok):
    return 'OK  ' if ok else 'TODO'


def main(argv=None):
    p = argparse.ArgumentParser(prog='python3 start.py')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--check', action='store_true', help='only run the checks')
    args = p.parse_args(argv)

    values = settings.apply(ROOT)
    print(f'{mark(True)} Python {sys.version.split()[0]}')
    ok, detail = checks.model(values)
    print(f'{mark(ok)} Model: {detail}')
    for name, configured in (('prtg', values.get('prtg_url')), ('netbox', values.get('netbox_url'))):
        if configured:
            ok, detail = checks.CHECKS[name](values)
            print(f'{mark(ok)} {name.upper() if name == "prtg" else "NetBox"}: {detail}')
    if list(ROOT.glob('containerlab/clab-*/topology-data.json')):
        ok, detail = checks.lab(ROOT)
        print(f'{mark(ok)} Lab: {detail}')
    print('      Change any of these in the studio: Settings (top bar).')
    if args.check:
        return 0

    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1', args.port)) == 0:
            print(f'Port {args.port} is already in use, probably by another BYA. Stop it, or start with --port {args.port + 1} '
                  f'(the studio keeps agents per address, so they will not show on the new port).')
            return 1
    import server
    server.serve(args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
