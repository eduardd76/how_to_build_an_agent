"""Settings page storage, connection checks, and PRTG alarms starting watched agents."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters, checks, settings  # noqa: E402
from bya.core import stamp  # noqa: E402
from bya.graph.model import ChatModel  # noqa: E402
from bya.watch import PrtgWatcher  # noqa: E402
import server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = ['LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY', 'BYA_ALLOW_HTTP_HOSTS', 'PRTG_API_TOKEN', 'NETBOX_URL', 'NETBOX_TOKEN', 'SLACK_BOT_TOKEN']


def serve(handler):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    httpd.hits = []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class FakePrtg(BaseHTTPRequestHandler):
    """Answers /api/table.json the way PRTG does (a "sensors" list with objid, device, status, message_raw...)."""
    ALARMS = [{'objid': 2143, 'device': 'mun-edge-01', 'group': 'WAN', 'sensor': 'Traffic Gi0/1', 'status': 'Down',
               'message_raw': 'No response', 'lastvalue': '0 kbit/s', 'lastcheck': '45563.5'}]

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if q.get('apitoken') != ['prtg-secret']:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps({'prtg-version': '24.4', 'treesize': len(self.ALARMS), 'sensors': self.ALARMS}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # /v1/models
        self.server.hits.append(('GET', self.path, self.headers.get('Authorization')))
        self._send({'data': [{'id': 'qwen2.5:7b'}, {'id': 'llama3.1:8b'}]})

    def do_POST(self):  # /v1/chat/completions
        self.rfile.read(int(self.headers['Content-Length']))
        self.server.hits.append(('POST', self.path, self.headers.get('Authorization')))
        self._send({'choices': [{'message': {'role': 'assistant', 'content': 'mun-edge-01 Traffic Gi0/1 is down (No response). '
                                                                               'Cause not confirmed.'}, 'finish_reason': 'stop'}],
                    'usage': {'total_tokens': 20}})

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


class EnvCase(unittest.TestCase):
    def setUp(self):
        saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        patcher = mock.patch.object(settings, 'ORIGINAL_ENV', {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class SettingsTests(EnvCase):
    def test_secrets_are_stored_privately_and_never_shown(self):
        settings.save(self.root, {'model': 'qwen2.5:7b', 'prtg_url': 'https://prtg.example.com', 'prtg_api_token': 'tok-123'})
        secrets_file = self.root / 'secrets.local.json'
        self.assertEqual(stat.S_IMODE(secrets_file.stat().st_mode), 0o600)
        self.assertEqual(json.loads(secrets_file.read_text()), {'prtg_api_token': 'tok-123'})
        self.assertNotIn('tok-123', (self.root / 'config.local.json').read_text())
        view = json.dumps(settings.describe(self.root))
        self.assertNotIn('tok-123', view)
        token = next(f for f in settings.describe(self.root) if f['key'] == 'prtg_api_token')
        self.assertEqual((token['set'], token['source']), (True, 'settings'))
        self.assertEqual(os.environ['PRTG_API_TOKEN'], 'tok-123')     # applied for the runtime
        self.assertEqual(os.environ['LLM_MODEL'], 'qwen2.5:7b')

    def test_empty_secret_keeps_and_clear_removes(self):
        settings.save(self.root, {'prtg_api_token': 'tok-123'})
        settings.save(self.root, {'prtg_api_token': ''})
        self.assertEqual(settings.load(self.root)['prtg_api_token'], 'tok-123')
        settings.save(self.root, {}, clear=['prtg_api_token'])
        self.assertNotIn('prtg_api_token', settings.load(self.root))
        self.assertNotIn('PRTG_API_TOKEN', os.environ)

    def test_environment_wins(self):
        with mock.patch.object(settings, 'ORIGINAL_ENV', {'LLM_MODEL': 'from-env'}), mock.patch.dict('os.environ', {'LLM_MODEL': 'from-env'}):
            settings.save(self.root, {'model': 'from-settings'})
            self.assertEqual(os.environ['LLM_MODEL'], 'from-env')
            field = next(f for f in settings.describe(self.root) if f['key'] == 'model')
            self.assertEqual((field['value'], field['source']), ('from-env', 'environment'))

    def test_bad_values_are_refused(self):
        for values in ({'nope': 'x'}, {'prtg_url': 'prtg.example.com'}, {'prtg_poll_s': '5'}, {'model': 'a\nb'}, {'model': 3}):
            with self.assertRaises(ValueError, msg=values):
                settings.save(self.root, values)

    def test_plain_http_to_another_host_must_be_allowed(self):
        with self.assertRaisesRegex(ValueError, 'Add host.orb.internal'):
            settings.save(self.root, {'model_url': 'http://host.orb.internal:11434/v1'})
        settings.save(self.root, {'model_url': 'http://host.orb.internal:11434/v1', 'allow_http_hosts': 'host.orb.internal'})
        self.assertEqual(os.environ['BYA_ALLOW_HTTP_HOSTS'], 'host.orb.internal')
        settings.save(self.root, {'model_url': 'http://127.0.0.1:11434/v1', 'allow_http_hosts': ''})   # loopback needs nothing

    def test_saved_api_key_is_sent_to_the_model(self):
        model = serve(FakeModel)
        self.addCleanup(model.shutdown)
        settings.save(self.root, {'model_api_key': 'sk-local-test-000000'})
        ChatModel(f'http://127.0.0.1:{model.server_port}/v1', 'qwen2.5:7b').chat([{'role': 'user', 'content': 'hi'}], [])
        self.assertEqual(model.hits[-1][2], 'Bearer sk-local-test-000000')


class CheckTests(EnvCase):
    def test_model_check(self):
        model = serve(FakeModel)
        self.addCleanup(model.shutdown)
        url = f'http://127.0.0.1:{model.server_port}/v1'
        self.assertTrue(checks.model({'model_url': url, 'model': 'qwen2.5:7b'})[0])
        ok, detail = checks.model({'model_url': url, 'model': 'qwen2.5:14b'})
        self.assertFalse(ok)
        self.assertIn('llama3.1:8b', detail)
        self.assertIn('no model name', checks.model({'model_url': url})[1])
        self.assertIn('Connector request failed', checks.model({'model_url': 'http://127.0.0.1:9/v1', 'model': 'x'})[1])

    def test_prtg_alarms_and_check(self):
        prtg = serve(FakePrtg)
        self.addCleanup(prtg.shutdown)
        cfg = {'prtg_url': f'http://127.0.0.1:{prtg.server_port}'}
        self.assertIn('Set the PRTG URL', checks.prtg(cfg)[1])
        os.environ['PRTG_API_TOKEN'] = 'prtg-secret'
        alarms = adapters.PrtgMonitoring(cfg).alarms()
        self.assertEqual(alarms[0]['sensor_id'], '2143')
        self.assertEqual(alarms[0]['message'], 'No response')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(prtg.hits[-1]).query)
        self.assertEqual(query['filter_status'], ['4', '5', '10', '14'])   # repeated, not a stringified list
        ok, detail = checks.prtg(cfg)
        self.assertTrue(ok)
        self.assertIn('1 sensors in alarm: 2143 mun-edge-01', detail)
        os.environ['PRTG_API_TOKEN'] = 'wrong'
        self.assertFalse(checks.prtg(cfg)[0])


class WatcherTests(unittest.TestCase):
    def test_one_run_per_alarm_state(self):
        alarms, started = [], []
        doc = {'blocks': [{'id': 'alert', 'type': 'trigger.alert', 'config': {'source': 'prtg', 'sensor_id': '2143'}}]}
        w = PrtgWatcher(lambda: alarms, lambda: [('wan.json', doc)], lambda f, d, a: started.append(a['status']) or 'run1')
        self.assertEqual(w.tick(), [])                      # nothing in alarm
        alarms[:] = [{'sensor_id': '2143', 'status': 'Warning', 'device': 'r1', 'sensor': 's'}]
        self.assertEqual(w.tick(), ['run1'])
        self.assertEqual(w.tick(), [])                      # same alarm: not again
        alarms[0]['status'] = 'Down'
        self.assertEqual(w.tick(), ['run1'])                # worse: runs again
        alarms.clear()
        w.tick()                                            # back to normal: re-armed
        alarms[:] = [{'sensor_id': '2143', 'status': 'Down', 'device': 'r1', 'sensor': 's'}]
        self.assertEqual(w.tick(), ['run1'])
        self.assertEqual(started, ['Warning', 'Down', 'Down'])

    def test_errors_are_logged_not_raised(self):
        doc = {'blocks': [{'id': 'alert', 'type': 'trigger.alert', 'config': {'source': 'prtg', 'sensor_id': '7'}}]}

        def broken_fetch():
            raise ValueError('PRTG unreachable')
        w = PrtgWatcher(broken_fetch, lambda: [('a.json', doc)], lambda *a: 'x')
        self.assertEqual(w.tick(), [])
        self.assertIn('PRTG unreachable', w.events[-1]['text'])
        w = PrtgWatcher(lambda: [{'sensor_id': '7', 'status': 'Down', 'device': 'd', 'sensor': 's'}], lambda: [('a.json', doc)],
                        lambda *a: (_ for _ in ()).throw(RuntimeError('model down')))
        self.assertEqual(w.tick(), [])
        self.assertIn('model down', w.events[-1]['text'])


class EndToEndTests(EnvCase):
    """Settings via the API, then a PRTG alarm starts a saved agent whose draft waits for approval."""

    def test_settings_api_and_watched_agent(self):
        prtg, model = serve(FakePrtg), serve(FakeModel)
        self.addCleanup(prtg.shutdown)
        self.addCleanup(model.shutdown)
        for name in ('ssot.json', 'runbooks.json'):
            (self.root / name).write_text((ROOT / name).read_text())
        (self.root / 'diagrams').mkdir()
        (self.root / 'web').mkdir()
        doc = {'schema_version': '1.0', 'name': 'WAN alarm', 'blocks': [
            {'id': 'alert', 'type': 'trigger.alert', 'config': {'source': 'prtg', 'sensor_id': '2143'}},
            {'id': 'agent', 'type': 'agent', 'config': {'instructions': 'Brief on-call about the alarm.', 'max_steps': 3,
                                                        'token_budget': 5000, 'timeout_s': 30}},
            {'id': 'check', 'type': 'guard.output_check', 'config': {}},
            {'id': 'review', 'type': 'guard.approval', 'config': {}},
            {'id': 'save', 'type': 'output.file', 'config': {'path': 'briefs/wan.md'}}],
            'attachments': [], 'flow': [['alert', 'agent'], ['agent', 'check'], ['check', 'review'], ['review', 'save']]}
        (self.root / 'diagrams' / 'wan-alarm.json').write_text(json.dumps(doc))
        with mock.patch.object(server, 'ROOT', self.root), mock.patch.object(server, 'WATCHER', None):
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            base = f'http://127.0.0.1:{httpd.server_port}'

            def post(path, body):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                             headers={'Content-Type': 'application/json', 'X-BYA-Token': server.TOKEN})
                with urllib.request.urlopen(req) as r:
                    return json.load(r)
            view = post('/api/settings', {'values': {
                'model_url': f'http://127.0.0.1:{model.server_port}/v1', 'model': 'qwen2.5:7b',
                'prtg_url': f'http://127.0.0.1:{prtg.server_port}', 'prtg_api_token': 'prtg-secret', 'prtg_poll_s': '3600'}})
            self.assertNotIn('prtg-secret', json.dumps(view))
            self.assertEqual(view['watch']['agents'][0]['file'], 'wan-alarm.json')
            self.assertTrue(post('/api/settings/test', {'target': 'prtg'})['ok'])
            self.assertTrue(post('/api/settings/test', {'target': 'model'})['ok'])
            view = post('/api/watch', {'diagrams': ['wan-alarm.json']})
            self.assertTrue(view['watch']['running'])
            paused = []
            for _ in range(100):                        # the watcher polls as soon as it starts
                paused = server.diagram_store().list_paused()
                if paused:
                    break
                threading.Event().wait(0.1)
            server.WATCHER.stop()
            self.assertEqual(len(paused), 1)
            self.assertIn('mun-edge-01 Traffic Gi0/1 is down', paused[0]['draft'])
            self.assertEqual(server.WATCHER.tick(), [])  # same alarm: no second run
            self.assertFalse((self.root / 'outputs').exists())   # nothing sent without approval


class StartTests(unittest.TestCase):
    def test_check_only(self):
        done = subprocess.run([sys.executable, 'start.py', '--check'], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={**os.environ, 'LLM_BASE_URL': 'http://127.0.0.1:9/v1', 'LLM_MODEL': 'x'})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('Python', done.stdout)
        self.assertIn('TODO Model', done.stdout)


if __name__ == '__main__':
    unittest.main()
