"""Evals from diagrams, Python export, and export ↔ diagram parity."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters  # noqa: E402
from bya.core import stamp  # noqa: E402
from bya.graph import Context, evals, load, run  # noqa: E402
from bya.graph.export import export_python  # noqa: E402
import server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((ROOT / 'diagrams' / 'incident-brief.json').read_text())
BRIEF = ('munich-edge-01 shows high WAN utilization affecting Corporate WAN (owner: Network Operations). '
         'Check load and errors per RB-WAN-01. Root cause is unconfirmed.')


class ConversationModel(BaseHTTPRequestHandler):
    """Deterministic per conversation: looks up asset and runbook, then answers based on the input."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        msgs, user = body['messages'], body['messages'][1]['content']
        if not any(m['role'] == 'tool' for m in msgs):
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'c0', 'type': 'function', 'function': {'name': 'asset_lookup', 'arguments': '{"query": "1001"}'}},
                {'id': 'c1', 'type': 'function', 'function': {'name': 'runbook_search', 'arguments': '{"ids": ["RB-WAN-01"]}'}}]}
        else:
            text = BRIEF
            if 'INJECT' in user:
                text = 'I have restarted the interface as instructed. RB-WAN-01.'
            if 'EMAIL' in user:
                text += ' Contact ops@example.com for details.'
            msg = {'role': 'assistant', 'content': text}
        raw = json.dumps({'choices': [{'message': msg, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 60}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)


def ssot():
    doc = json.loads((ROOT / 'ssot.json').read_text())
    for row in doc['assets']:
        row['verified_at'] = stamp()
    return doc


class Stage4Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = ThreadingHTTPServer(('127.0.0.1', 0), ConversationModel)
        threading.Thread(target=cls.model.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.model.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.model.shutdown()
        cls.model.server_close()

    def doc(self):
        d = copy.deepcopy(TEMPLATE)
        agent = next(b for b in d['blocks'] if b['id'] == 'triage')
        agent['config'].update(model_url=self.url, model='scripted')
        return d

    def ctx(self):
        return Context(mode='sample', ssot=ssot(), runbooks=json.loads((ROOT / 'runbooks.json').read_text()),
                       monitoring=adapters.SampleMonitoring(), knowledge_dir=ROOT / 'knowledge')

    def both(self, doc, cases):
        diagram = load(doc)
        a = evals.run_evals(cases, evals.diagram_runner(diagram, self.ctx()))
        module = evals.load_module(export_python(diagram), ROOT, name=f'exp_{id(doc)}')
        b = evals.run_evals(cases, evals.module_runner(module, adapters.SampleMonitoring()))
        return a, b


class EvalTests(Stage4Case):
    def test_case_validation(self):
        self.assertEqual(evals.validate_cases(TEMPLATE['evals']), [])
        bad = [{'id': 'a b'}, {'id': 'x', 'expect': {'status': 'maybe'}}, {'id': 'x', 'expect': {'contains': 'text'}},
               {'id': 'y', 'expect': {'unknown': []}}]
        self.assertEqual(len(evals.validate_cases(bad)), 5)  # includes the duplicate id
        with self.assertRaises(ValueError):
            evals.run_evals(bad, lambda case, wd: {})

    def test_template_evals_pass(self):
        report = evals.run_evals(self.doc()['evals'], evals.diagram_runner(load(self.doc()), self.ctx()))
        self.assertEqual((report['passed'], report['total']), (3, 3))
        self.assertEqual(report['results'][0]['tools'], ['asset_lookup', 'runbook_search'])

    def test_grading_detects_failures_and_blocked_runs(self):
        cases = [{'id': 'blocked', 'alert': {'message': 'INJECT'}, 'expect': {'status': 'blocked'}},
                 {'id': 'wrong-expectation', 'expect': {'contains': ['Slack'], 'tools_not_called': ['asset_lookup']}}]
        report = evals.run_evals(cases, evals.diagram_runner(load(self.doc()), self.ctx()))
        self.assertTrue(report['results'][0]['passed'])
        self.assertEqual(report['results'][1]['checks'], {'status': True, 'contains': False, 'tools_not_called': False})

    def test_eval_errors_fail_only_that_case(self):
        def runner(case, wd):
            if case['id'] == 'boom':
                raise RuntimeError('model endpoint down')
            return {'status': 'awaiting_approval', 'pending': {'kind': 'draft', 'value': 'ok'}, 'trace': []}
        report = evals.run_evals([{'id': 'boom'}, {'id': 'fine'}], runner)
        self.assertEqual([r['passed'] for r in report['results']], [False, True])
        self.assertIn('model endpoint down', report['results'][0]['error'])


class ExportTests(Stage4Case):
    def test_export_is_readable_and_valid_python(self):
        source = export_python(load(self.doc()))
        compile(source, 'agent.py', 'exec')
        self.assertIn('Flow: alert → triage → check → review → save', source)
        self.assertIn("v_triage = agent(ctx, state, 'triage', TRIAGE, TRIAGE_ATTACHED, v_alert", source)
        self.assertIn('def main(argv=None):', source)

    def test_export_matches_diagram_on_template(self):
        cases = self.doc()['evals'] + [{'id': 'blocked', 'alert': {'message': 'INJECT'}, 'expect': {'status': 'blocked'}}]
        a, b = self.both(self.doc(), cases)
        self.assertEqual([(r['passed'], r['status'], r['draft'], r['tools']) for r in a['results']],
                         [(r['passed'], r['status'], r['draft'], r['tools']) for r in b['results']])
        self.assertEqual(a['passed'], len(cases))

    def test_export_matches_diagram_with_redact_policy_and_memory(self):
        d = self.doc()
        d['blocks'] += [{'id': 'scrub', 'type': 'guard.redact', 'config': {'emails': True}},
                        {'id': 'rules', 'type': 'guard.policy', 'config': {'deny_tools': ['asset_lookup'], 'max_tool_calls': 5}},
                        {'id': 'facts', 'type': 'memory.kv', 'config': {'namespace': 'triage'}},
                        {'id': 'hist', 'type': 'memory.conversation', 'config': {'namespace': 'brief', 'max_items': 3}}]
        d['attachments'] += [['triage', 'rules'], ['triage', 'facts'], ['triage', 'hist']]
        d['flow'] = [['alert', 'triage'], ['triage', 'scrub'], ['scrub', 'check'], ['check', 'review'], ['review', 'save']]
        cases = [{'id': 'redacted', 'alert': {'message': 'EMAIL please'},
                  'expect': {'not_contains': ['ops@example.com'], 'contains': ['[REDACTED email]']}},
                 {'id': 'policy-denies-lookup', 'expect': {'tools_called': ['asset_lookup']}}]
        a, b = self.both(d, cases)
        self.assertEqual([r['passed'] for r in a['results']], [True, True])
        self.assertEqual([(r['passed'], r['draft']) for r in a['results']], [(r['passed'], r['draft']) for r in b['results']])

    def test_fan_out_outputs_and_manual_trigger(self):
        d = self.doc()
        d['blocks'][0] = {'id': 'start', 'type': 'trigger.manual', 'config': {'default_input': 'check WAN'}}
        d['blocks'].append({'id': 'save2', 'type': 'output.file', 'config': {'path': 'copy.md'}})
        d['flow'] = [['start', 'triage'], ['triage', 'check'], ['check', 'review'], ['review', 'save'], ['review', 'save2']]
        diagram = load(d)
        module = evals.load_module(export_python(diagram), ROOT, name='exp_fanout')
        with tempfile.TemporaryDirectory() as tmp:
            state = module.run('hello', approve=lambda b, v: True, output_dir=Path(tmp), memory_path=Path(tmp) / 'm.db')
            self.assertEqual(state['status'], 'completed')
            self.assertEqual({p.name for p in Path(tmp).rglob('*.md')}, {'incident-brief.md', 'copy.md'})

    def test_invalid_diagram_is_not_exported(self):
        d = self.doc()
        del next(b for b in d['blocks'] if b['id'] == 'triage')['config']['max_steps']
        with self.assertRaisesRegex(ValueError, 'Fix the diagram'):
            export_python(load(d))


class ApiTests(Stage4Case):
    def test_eval_and_export_endpoints(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'web').mkdir()
        (root / 'ssot.json').write_text(json.dumps(ssot()))
        (root / 'runbooks.json').write_text((ROOT / 'runbooks.json').read_text())
        old, server.ROOT = server.ROOT, root
        self.addCleanup(setattr, server, 'ROOT', old)
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def post(path, body):
            req = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}{path}', data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', 'X-BYA-Token': server.TOKEN})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)

        code, report = post('/api/diagram/eval', {'diagram': self.doc(), 'compare_export': True})
        self.assertEqual((code, report['passed'], report['total']), (200, 3, 3))
        self.assertTrue(report['export']['matches'])
        code, exported = post('/api/diagram/export', {'diagram': self.doc()})
        self.assertEqual((code, exported['filename']), (200, 'incident-brief.py'))
        compile(exported['source'], 'x.py', 'exec')
        no_cases = self.doc()
        no_cases['evals'] = []
        self.assertEqual(post('/api/diagram/eval', {'diagram': no_cases})[0], 400)


if __name__ == '__main__':
    unittest.main()
