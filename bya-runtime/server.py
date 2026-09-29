"""Loopback-only BYA runtime. The Settings page can store credentials in a local file, but never reads them back."""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import threading
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bya import adapters, checks, core, graph, pipeline, prompts, settings, validation
from bya.core import UTC, date
from bya.graph import builder, evals
from bya.graph.catalog import catalog
from bya.graph.export import export_python
from bya.graph.reach import reach
from bya.store import DiagramRunStore, RunStore
from bya.watch import FixedAlert, PrtgWatcher

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
APPROVAL_TTL = 3600
settings.apply(ROOT)   # saved settings fill in whatever the environment did not set
WATCHER = None


def read(name):
    return json.loads((ROOT / name).read_text())


def config():
    return read('config.local.json') if (ROOT / 'config.local.json').exists() else {}


def store():
    return RunStore(ROOT / 'bya.sqlite3')


def diagram_store():
    return DiagramRunStore(ROOT / 'bya.sqlite3')


DIAGRAM_FILE = re.compile(r'^[a-z0-9][a-z0-9-]{0,60}\.json$')


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
        if path == '/api/catalog':
            return self.send_json({'types': catalog()})
        if path == '/api/settings':
            return self.send_json(settings_view())
        if path == '/api/builder/options':
            return self.send_json(builder_options())
        if path == '/api/diagram/pending':
            return self.send_json({'pending': diagram_store().list_paused()})
        if path == '/api/diagrams':
            files = sorted((ROOT / 'diagrams').glob('*.json'))
            return self.send_json({'diagrams': [{'file': f.name, 'name': _diagram_name(f)} for f in files]})
        if path.startswith('/api/diagrams/'):
            name = path.rsplit('/', 1)[-1]
            if not DIAGRAM_FILE.match(name) or not (ROOT / 'diagrams' / name).exists():
                return self.send_json({'error': 'Unknown diagram'}, 404)
            return self.send_json(json.loads((ROOT / 'diagrams' / name).read_text()))
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


def _diagram_name(path):
    try:
        return json.loads(path.read_text()).get('name', path.stem)
    except (ValueError, OSError):
        return path.stem


def _diagram_context(mode):
    cfg = config()
    return graph.Context(
        mode=mode, ssot=read('ssot.json'), runbooks=read('runbooks.json'),
        monitoring=adapters.PrtgMonitoring(cfg) if mode == 'live' else adapters.SampleMonitoring(),
        approve=None,                  # pause at approval blocks; the reviewer decides in the canvas
        pause_for_tool_approval=True,  # write tools pause the run until approved in the inbox
        output_dir=ROOT / 'outputs', memory_path=ROOT / 'bya.sqlite3', knowledge_dir=ROOT / 'knowledge',
        lab_dir=ROOT / 'lab', configs_dir=ROOT / 'configs')


def _mode(data):
    mode = data.get('mode', 'sample')
    if mode not in ('sample', 'live'):
        raise ValueError('Mode must be "sample" or "live".')
    return mode


def diagram_validate(data):
    diagram = graph.load(data.get('diagram'))
    return {'violations': [v.to_dict() for v in graph.validate(diagram, _mode(data))]}


def diagram_run(data):
    doc, mode = data.get('diagram'), _mode(data)
    diagram = graph.load(doc)
    if not LOCK.acquire(blocking=False):
        raise ValueError('Another run is active. Wait until it finishes.')
    try:
        try:
            state = graph.run(diagram, _diagram_context(mode), data.get('input'))
        except graph.DiagramInvalid as e:
            return {'status': 'invalid', 'violations': [v.to_dict() for v in e.violations]}
        except graph.StepFailed as e:
            state = e.state
            state['error'] = str(e)
    finally:
        LOCK.release()
    rid = secrets.token_hex(12)
    diagram_store().save(rid, doc, state)
    return {'id': rid, **state}


def diagram_approve(data):
    rid = str(data.get('id', ''))
    if not isinstance(data.get('approved'), bool):
        raise ValueError('Send "approved": true or false.')
    runs = diagram_store()
    doc, state = runs.claim_paused(rid)
    try:
        state = graph.resume(graph.load(doc), state, data['approved'], _diagram_context(state.get('mode', 'sample')))
    except graph.StepFailed as e:
        state = e.state
        state['error'] = str(e)
    runs.update(rid, state)
    return {'id': rid, **state}


def diagram_eval(data):
    doc, mode = data.get('diagram'), _mode(data)
    diagram = graph.load(doc)
    cases = (doc or {}).get('evals') or []
    if not cases:
        raise ValueError('Add at least one eval case first.')
    problems = graph.validate(diagram, mode)
    if problems:
        return {'status': 'invalid', 'violations': [v.to_dict() for v in problems]}
    if not LOCK.acquire(blocking=False):
        raise ValueError('Another run is active. Wait until it finishes.')
    try:
        ctx = _diagram_context(mode)
        report = evals.run_evals(cases, evals.diagram_runner(diagram, ctx), data.get('repeat', 1))
        if data.get('compare_export'):
            module = evals.load_module(export_python(diagram), ROOT)
            exported = evals.run_evals(cases, evals.module_runner(module, ctx.monitoring), data.get('repeat', 1))
            report['export'] = {'passed': exported['passed'], 'total': exported['total'],
                                'matches': [r['passed'] for r in report['results']] == [r['passed'] for r in exported['results']]}
    finally:
        LOCK.release()
    return report


def diagram_export(data):
    diagram = graph.load(data.get('diagram'))
    name = re.sub(r'[^a-z0-9]+', '-', diagram.name.lower()).strip('-') or 'agent'
    return {'filename': f'{name[:50]}.py', 'source': export_python(diagram)}


def diagram_reach(data):
    diagram = graph.load(data.get('diagram'))
    return reach(diagram, _diagram_context(_mode(data)))


def builder_options():
    """What the New agent form can offer on this machine: sites, sensors, NetBox, config files."""
    assets = read('ssot.json').get('assets', [])
    out = {**builder.options(),
           'asset_sites': sorted({a['site'] for a in assets if a.get('site')}),
           'sensors': [{'id': str(s['id']), 'label': f"{a['name']} · {s['channel']}", 'site': a.get('site', '')}
                       for a in assets for s in a.get('sensors', [])],
           'configs': sorted(p.name for p in (ROOT / 'configs').glob('*.cfg')),
           'labs': sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'containerlab').glob('clab-*/topology-data.json')),
           'default_model': os.environ.get('LLM_MODEL', ''), 'default_model_url': os.environ.get('LLM_BASE_URL', ''),
           'prtg': {'configured': bool(config().get('prtg_url') and os.environ.get('PRTG_API_TOKEN')), 'alarms': []},
           'netbox': {'configured': bool(os.environ.get('NETBOX_URL') and os.environ.get('NETBOX_TOKEN')), 'sites': [], 'roles': []}}
    if out['prtg']['configured']:
        try:
            out['prtg']['alarms'] = [{'id': a['sensor_id'], 'label': f"{a['device']} · {a['sensor']} ({a['status']})"}
                                     for a in adapters.PrtgMonitoring(config()).alarms(limit=50)]
        except ValueError as e:
            out['prtg']['error'] = str(e)
    if out['netbox']['configured']:
        base, auth = os.environ['NETBOX_URL'].rstrip('/'), {'Authorization': f"Token {os.environ['NETBOX_TOKEN']}"}
        try:
            for key, path in (('sites', 'dcim/sites'), ('roles', 'dcim/device-roles')):
                rows = core.http(f'{base}/api/{path}/?limit=200', headers=auth).get('results', [])
                out['netbox'][key] = [{'slug': r['slug'], 'name': r['name']} for r in rows if r.get('slug')]
        except ValueError as e:
            out['netbox']['error'] = str(e)
    return out


def builder_preview(data):
    """Build the diagram from the form's answers and say what it could touch. Incomplete answers are not an error."""
    try:
        doc = builder.build(data.get('spec'))
    except ValueError as e:
        return {'problem': str(e)}
    r = reach(graph.load(doc), _diagram_context('sample'))
    devices = sorted({d for a in r['agents'] for scope in a['devices'] for d in scope['devices']})
    scope_errors = [scope['error'] for a in r['agents'] for scope in a['devices'] if scope.get('error')]
    return {'diagram': doc, 'summary': r['summary'], 'devices': devices, 'scope_errors': scope_errors}


def settings_view():
    cfg = config()
    watched = set(cfg.get('prtg_watch', []))
    candidates = []
    for f in sorted((ROOT / 'diagrams').glob('*.json')):
        try:
            doc = json.loads(f.read_text())
        except ValueError:
            continue
        trig = next((b for b in doc.get('blocks', []) if b.get('type') == 'trigger.alert'), None)
        if trig and (trig.get('config') or {}).get('source') == 'prtg':
            candidates.append({'file': f.name, 'name': doc.get('name', f.stem), 'sensor_id': str(trig['config'].get('sensor_id', '')),
                               'watched': f.name in watched})
    return {'fields': settings.describe(ROOT), 'watch': {
        'agents': candidates, 'running': bool(WATCHER and WATCHER._thread and WATCHER._thread.is_alive()),
        'events': list(WATCHER.events) if WATCHER else [], 'poll_s': int(cfg.get('prtg_poll_s') or 60)}}


def settings_save(data):
    values, clear = data.get('values') or {}, data.get('clear') or []
    if not isinstance(values, dict) or not isinstance(clear, list):
        raise ValueError('Send "values" as an object and "clear" as a list.')
    settings.save(ROOT, values, [str(c) for c in clear])
    restart_watcher()
    return settings_view()


def settings_test(data):
    target = data.get('target')
    if target == 'lab':
        ok, detail = checks.lab(ROOT)
    elif target in checks.CHECKS:
        ok, detail = checks.CHECKS[target](config())
    else:
        raise ValueError('Test "model", "prtg", "netbox" or "lab".')
    return {'ok': ok, 'detail': detail}


def watch_save(data):
    files = data.get('diagrams')
    if not isinstance(files, list) or not all(isinstance(f, str) and DIAGRAM_FILE.match(f) for f in files):
        raise ValueError('Send "diagrams" as a list of saved diagram file names.')
    cfg = config()
    cfg['prtg_watch'] = sorted(set(files))
    tmp = ROOT / 'config.pending.json'
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, ROOT / 'config.local.json')
    restart_watcher()
    return settings_view()


def watch_start_run(file, doc, alarm):
    """A PRTG alarm starts a saved agent in live mode. It stops at its human approval, like any run."""
    diagram = graph.load(doc)
    ctx = _diagram_context('live')
    ctx.monitoring = FixedAlert(alarm, ctx.monitoring)
    if not LOCK.acquire(timeout=300):
        raise ValueError('another run was still active after 5 minutes')
    try:
        try:
            state = graph.run(diagram, ctx, None)
        except graph.DiagramInvalid as e:
            raise ValueError('the agent does not pass live checks: ' + '; '.join(v.message for v in e.violations)) from None
        except graph.StepFailed as e:
            state = {**e.state, 'error': str(e)}
    finally:
        LOCK.release()
    state['started_by'] = f'PRTG alarm on sensor {alarm["sensor_id"]} ({alarm["status"]})'
    rid = secrets.token_hex(12)
    diagram_store().save(rid, doc, state)
    return rid


def _watched():
    out = []
    for f in config().get('prtg_watch', []):
        path = ROOT / 'diagrams' / f
        if DIAGRAM_FILE.match(f) and path.exists():
            out.append((f, json.loads(path.read_text())))
    return out


def restart_watcher():
    """Run the PRTG watcher while PRTG is configured and at least one agent watches it."""
    global WATCHER
    cfg = config()
    if WATCHER:
        WATCHER.stop()
    if not (cfg.get('prtg_url') and os.environ.get('PRTG_API_TOKEN') and cfg.get('prtg_watch')):
        return
    events = WATCHER.events if WATCHER else []
    WATCHER = PrtgWatcher(lambda: adapters.PrtgMonitoring(config()).alarms(), _watched, watch_start_run,
                          poll_s=int(cfg.get('prtg_poll_s') or 60))
    WATCHER.events = events
    WATCHER.log(f'Watching PRTG every {WATCHER.poll_s} s for {len(cfg["prtg_watch"])} agent(s).')
    WATCHER.start()


def diagram_save(data):
    name = str(data.get('file', ''))
    if not DIAGRAM_FILE.match(name):
        raise ValueError('File name: lower-case letters, digits and "-", ending in .json.')
    graph.load(data.get('diagram'))  # shape check; rule problems are allowed in a saved draft
    target = ROOT / 'diagrams' / name
    tmp = target.with_suffix('.pending')
    tmp.write_text(json.dumps(data['diagram'], indent=2))
    os.replace(tmp, target)
    return {'saved': name}


ROUTES = {'/api/assist': assist, '/api/ssot': save_ssot, '/api/run': run_agent, '/api/approve': approve,
          '/api/diagram/validate': diagram_validate, '/api/diagram/run': diagram_run,
          '/api/diagram/approve': diagram_approve, '/api/diagram/save': diagram_save,
          '/api/diagram/eval': diagram_eval, '/api/diagram/export': diagram_export,
          '/api/diagram/reach': diagram_reach, '/api/builder/preview': builder_preview,
          '/api/settings': settings_save, '/api/settings/test': settings_test, '/api/watch': watch_save}


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=8787)
    args = p.parse_args()
    serve(args.port)


def serve(port):
    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    restart_watcher()
    print(f'BYA is running at http://127.0.0.1:{port}/studio.html. Keep this window open.')
    httpd.serve_forever()
