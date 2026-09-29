"""HTTP API behind Diagram Studio: catalogue, templates, validate, run → approve, save."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya.core import stamp
import server

ROOT = Path(__file__).resolve().parents[1]
BRIEF = 'munich-edge-01: high load on Corporate WAN (owner Network Operations). Check per RB-WAN-01. Root cause is unconfirmed.'


class Model(BaseHTTPRequestHandler):
    """Looks up the runbook once, then answers."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not any(m['role'] == 'tool' for m in body['messages']):
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {
                'name': 'runbook_search', 'arguments': json.dumps({'ids': ['RB-WAN-01']})}}]}
        else:
            msg = {'role': 'assistant', 'content': BRIEF}
        raw = json.dumps({'choices': [{'message': msg, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 50}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)


class StudioApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_root, server.ROOT = server.ROOT, Path(cls.tmp.name)
        (server.ROOT / 'web').mkdir()
        (server.ROOT / 'web' / 'studio.html').write_text('studio')
        shutil.copytree(ROOT / 'diagrams', server.ROOT / 'diagrams')
        ssot = json.loads((ROOT / 'ssot.json').read_text())
        for row in ssot['assets']:
            row['verified_at'] = stamp()
        (server.ROOT / 'ssot.json').write_text(json.dumps(ssot))
        shutil.copy(ROOT / 'runbooks.json', server.ROOT / 'runbooks.json')
        cls.model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        for s in (cls.model, cls.http):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        for s in (cls.model, cls.http):
            s.shutdown()
            s.server_close()
        server.ROOT = cls.old_root
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.load(r)

    def post(self, path, body, token=True):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-BYA-Token'] = server.TOKEN
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def diagram(self):
        d = self.get('/api/diagrams/incident-brief.json')
        agent = next(b for b in d['blocks'] if b['id'] == 'triage')
        agent['config'].update(model_url=f'http://127.0.0.1:{self.model.server_port}/v1', model='scripted')
        return d

    def test_catalog_and_templates(self):
        types = {t['type']: t for t in self.get('/api/catalog')['types']}
        self.assertEqual(len(types), 18)
        self.assertTrue(types['tool.mcp']['attachable'])
        self.assertEqual(types['output.slack']['inputs'], ['approved_draft'])
        self.assertIn('instructions', [f['key'] for f in types['agent']['fields']])
        self.assertIn('incident-brief.json', [d['file'] for d in self.get('/api/diagrams')['diagrams']])

    def test_validate(self):
        self.assertEqual(self.post('/api/diagram/validate', {'diagram': self.diagram()})[1]['violations'], [])
        broken = self.diagram()
        broken['flow'] = [p for p in broken['flow'] if p[1] != 'review']
        rules = {v['rule'] for v in self.post('/api/diagram/validate', {'diagram': broken})[1]['violations']}
        self.assertIn('one-input', rules)
        self.assertEqual(self.post('/api/diagram/validate', {'diagram': 'nope'})[0], 400)

    def test_run_pause_approve_once(self):
        code, run = self.post('/api/diagram/run', {'diagram': self.diagram(), 'mode': 'sample'})
        self.assertEqual((code, run['status']), (200, 'awaiting_approval'))
        self.assertEqual(run['pending']['value'], BRIEF)
        code, done = self.post('/api/diagram/approve', {'id': run['id'], 'approved': True})
        self.assertEqual((code, done['status']), (200, 'completed'))
        self.assertEqual((server.ROOT / 'outputs' / 'briefs' / 'incident-brief.md').read_text().strip(), BRIEF)
        self.assertEqual(self.post('/api/diagram/approve', {'id': run['id'], 'approved': True})[0], 400)

    def test_run_invalid_diagram_reports_violations(self):
        d = self.diagram()
        del next(b for b in d['blocks'] if b['id'] == 'triage')['config']['max_steps']
        code, res = self.post('/api/diagram/run', {'diagram': d})
        self.assertEqual((code, res['status']), (200, 'invalid'))
        self.assertIn('agent-limits', {v['rule'] for v in res['violations']})

    def test_save_and_session_token(self):
        self.assertEqual(self.post('/api/diagram/save', {'file': '../evil.json', 'diagram': self.diagram()})[0], 400)
        self.assertEqual(self.post('/api/diagram/save', {'file': 'my-agent.json', 'diagram': self.diagram()})[0], 200)
        self.assertTrue((server.ROOT / 'diagrams' / 'my-agent.json').exists())
        self.assertEqual(self.post('/api/diagram/run', {'diagram': self.diagram()}, token=False)[0], 403)


if __name__ == '__main__':
    unittest.main()
