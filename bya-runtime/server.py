"""Loopback-only BYA runtime. No credentials are accepted by the browser."""
import argparse
import datetime as dt
import json
import os
import secrets
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bya import adapters, pipeline, prompts, validation
from bya.core import UTC, date
from bya.store import RunStore

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
APPROVAL_TTL = 3600


def read(name):
    return json.loads((ROOT / name).read_text())


def config():
    return read('config.local.json') if (ROOT / 'config.local.json').exists() else {}


def store():
    return RunStore(ROOT / 'bya.sqlite3')


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / 'web'), **kw)

    def log_message(self, *a):
        pass

    def trusted(self):
        port = str(self.server.server_port)
        return self.headers.get('Host') in ('127.0.0.1:' + port, 'localhost:' + port)

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.trusted():
            return self.send_json({'error': 'Invalid host'}, 403)
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/status':
            c = config()
            return self.send_json({
                'local': True, 'token': TOKEN, 'model': c.get('model'),
                'prtg_configured': bool(c.get('prtg_url') and os.environ.get('PRTG_API_TOKEN')),
                'slack_configured': bool(os.environ.get('SLACK_BOT_TOKEN') and c.get('slack_channel')),
                'timesfm_configured': bool(os.environ.get('BYA_TIMESFM_PATH')),
                'prompt_version': prompts.PROMPT_VERSION,
            })
        if path == '/api/ssot':
            return self.send_json(read('ssot.json'))
        if path.startswith('/api/'):
            return self.send_json({'error': 'Unknown endpoint'}, 404)
        super().do_GET()

    def do_POST(self):
        if not self.trusted() or self.headers.get('X-BYA-Token') != TOKEN:
            return self.send_json({'error': 'Invalid local session'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            return self.send_json({'error': 'Cross-origin requests are blocked'}, 403)
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.send_json({'error': 'JSON required'}, 415)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 1_000_000:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(size))
            route = ROUTES.get(urllib.parse.urlsplit(self.path).path)
            if route is None:
                return self.send_json({'error': 'Unknown endpoint'}, 404)
            return self.send_json(route(data))
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json({'error': str(e)}, 400)
        except Exception:
            return self.send_json({'error': 'Run failed. Check local configuration and installed dependencies. No automatic external action was taken.'}, 500)


def assist(data):
    goal = str(data.get('goal', ''))[:4000]
    if not goal.strip():
        raise ValueError('Describe the agent job first.')
    return {'instructions': adapters.LocalChatModel(config()).complete(prompts.ASSIST_SYSTEM, goal)}


def save_ssot(data):
    doc = validation.validate_ssot(data)
    tmp = ROOT / 'ssot.pending.json'
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, ROOT / 'ssot.json')
    return {'saved': True}


def run_agent(data):
    wiring = adapters.sample_adapters() if data.get('mode') == 'sample' else adapters.live_adapters(config())
    if not LOCK.acquire(blocking=False):
        raise ValueError('Another run is active. Wait until it finishes.')
    try:
        result = pipeline.run(data, read('ssot.json'), read('runbooks.json'), wiring)
    finally:
        LOCK.release()
    result['id'] = secrets.token_hex(12)
    store().save(result)
    return result


def approve(data):
    rid = str(data.get('id', ''))
    if data.get('confirm') != 'send':
        raise ValueError('Explicit send confirmation required.')
    slack = adapters.SlackDelivery(config())

    def check(result, created):
        if result['mode'] != 'live':
            raise ValueError('Sample runs cannot send real messages.')
        if (dt.datetime.now(UTC) - date(created)).total_seconds() > APPROVAL_TTL:
            raise ValueError('Approval expired. Run again with fresh evidence.')
        slack.require_ready()

    runs = store()
    result = runs.claim(rid, check)
    # Never automatically retry an ambiguous write: a lost response may still mean it was posted.
    try:
        ts = slack.send(result['draft'])
    except Exception:
        runs.finish(rid, 'delivery_unknown')
        raise ValueError('Delivery failed or is uncertain. Verify Slack before attempting another run.') from None
    runs.finish(rid, 'sent')
    return {'status': 'sent', 'timestamp': ts}


ROUTES = {'/api/assist': assist, '/api/ssot': save_ssot, '/api/run': run_agent, '/api/approve': approve}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8787)
    args = p.parse_args()
    print(f'BYA is running at http://127.0.0.1:{args.port}. Keep this window open.')
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
