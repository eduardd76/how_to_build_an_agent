"""Templates: capacity forecast and config review, their built-in tools, and their evals (diagram and export)."""
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters  # noqa: E402
from bya.core import stamp  # noqa: E402
from bya.graph import Context, evals, load, load_file, validate  # noqa: E402
from bya.graph.export import export_python  # noqa: E402
from bya.graph.tools import config_lint, metric_forecast  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ['incident-brief', 'capacity-forecast', 'config-review']


def call(name, args, n=0):
    return {'id': f'c{n}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


class TemplateModel(BaseHTTPRequestHandler):
    """Scripted model: calls the template's tools, then writes a draft from what the tools returned.
    Set LEAK to make it copy a secret into the draft (to prove redaction)."""
    LEAK = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        msgs, tools = body['messages'], {t['function']['name'] for t in body.get('tools', [])}
        user = msgs[1]['content']
        results = [m['content'] for m in msgs if m['role'] == 'tool']
        if 'config_lint' in tools:
            msg = self.config_review(user, results)
        elif 'metric_forecast' in tools:
            msg = self.capacity(user, results)
        else:
            msg = {'role': 'assistant', 'content': 'No template matched.'}
        raw = json.dumps({'choices': [{'message': msg, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 80}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)

    def config_review(self, user, results):
        if not results:
            name = re.search(r'[\w.-]+\.cfg', user).group(0)
            return {'role': 'assistant', 'content': None, 'tool_calls': [call('config_lint', {'file': name})]}
        if len(results) == 1:
            return {'role': 'assistant', 'content': None, 'tool_calls': [call('search_documents', {'query': 'CFG-MGMT-01 telnet'}, 1)]}
        lint = json.loads(re.search(r'\{.*\}', results[0], re.S).group(0))
        lines = [f"- {f['rule']} ({f['severity']}), line {f['line']}: {f['title']}." for f in lint['findings']]
        verdict = f"{len(lint['findings'])} findings." if lines else 'No findings against the configuration rules.'
        text = f"Review of {lint['file']}: {verdict}\n" + '\n'.join(lines)
        if self.LEAK:
            text += '\nLine 4 reads: enable password Sample-Only-1'
        return {'role': 'assistant', 'content': text}

    def capacity(self, user, results):
        if not results:
            sensor = re.search(r'sensor (\d+)', user).group(1)
            threshold = float(re.search(r'(\d+(?:\.\d+)?) ?(?:%|GB)', user).group(1))
            direction = 'below' if re.search(r'\bbelow\b', user) else 'above'
            return {'role': 'assistant', 'content': None, 'tool_calls': [
                call('metric_forecast', {'sensor_id': sensor, 'threshold': threshold, 'direction': direction})]}
        body = re.search(r'\{.*\}', results[0], re.S)
        if not body:
            return {'role': 'assistant', 'content': 'The forecast could not run: the sensor is not in the asset register.'}
        f = json.loads(body.group(0))
        when = f'crosses the threshold in {f["crossing_in_minutes"]} minutes' if f['crossing_in_minutes'] else 'does not cross the threshold in this horizon'
        return {'role': 'assistant', 'content': (
            f"{f['asset']} ({f['service']}, owner {f['owner']}): {f['channel']} is {f['latest_value']} {f['unit']} and "
            f"{when}. Holdout error {f['holdout_mae']} vs seasonal baseline {f['seasonal_baseline_mae']}. "
            f"Next check: {f['runbook_ids'][0]}.")}


def ssot():
    doc = json.loads((ROOT / 'ssot.json').read_text())
    for row in doc['assets']:
        row['verified_at'] = stamp()
    return doc


def tool_ctx(**kw):
    return Context(ssot=ssot(), monitoring=adapters.SampleMonitoring(), knowledge_dir=ROOT / 'knowledge', **kw)


class ToolTests(unittest.TestCase):
    def test_metric_forecast_reports_baseline_and_owner(self):
        out = json.loads(metric_forecast({'sensor_id': '1001', 'threshold': 80}, tool_ctx()))
        self.assertEqual((out['asset'], out['owner'], out['backend'], out['horizon_minutes']),
                         ('munich-edge-01', 'Network Operations', 'demo-trend', 240))
        self.assertTrue(out['beats_baseline'])
        self.assertIn('not an outage probability', out['note'])
        crossing = json.loads(metric_forecast({'sensor_id': '1001', 'threshold': 72, 'horizon_steps': 96}, tool_ctx()))
        self.assertIsNotNone(crossing['crossing_in_minutes'])

    def test_metric_forecast_refuses_unknown_sample_and_bad_input(self):
        with self.assertRaisesRegex(ValueError, 'No unique asset'):
            metric_forecast({'sensor_id': '9999', 'threshold': 80}, tool_ctx())
        with self.assertRaisesRegex(ValueError, 'sample data'):
            metric_forecast({'sensor_id': '1001', 'threshold': 80}, tool_ctx(mode='live'))
        with self.assertRaisesRegex(ValueError, 'direction'):
            metric_forecast({'sensor_id': '1001', 'threshold': 80, 'direction': 'sideways'}, tool_ctx())
        stale = tool_ctx()
        stale.ssot['assets'][0].update(sample=False, verified_at='2020-01-01T00:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'stale'):
            metric_forecast({'sensor_id': '1001', 'threshold': 80}, stale)

    def test_config_lint_findings_masking_and_comments(self):
        edge = json.loads(config_lint({'file': 'sample-branch-edge.cfg'}, tool_ctx()))
        rules = [f['rule'] for f in edge['findings']]
        self.assertEqual(rules[:4], ['CFG-AUTH-02', 'CFG-ACL-01', 'CFG-SNMP-01', 'CFG-MGMT-01'])  # high first, by line
        self.assertEqual(len(rules), 9)
        self.assertNotIn('Sample-Only', json.dumps(edge))
        self.assertNotIn('public', json.dumps(edge))
        self.assertEqual(json.loads(config_lint({'file': 'configs/sample-dc-core.cfg'}, tool_ctx()))['findings'], [])
        injected = json.loads(config_lint({'file': 'sample-injected.cfg'}, tool_ctx()))
        self.assertEqual([f['rule'] for f in injected['findings']], ['CFG-MGMT-01'])
        commented = json.loads(config_lint({'text': '! transport input telnet\nservice password-encryption\n'
                                                     'logging host 10.0.0.1\nntp server 10.0.0.2\n'}, tool_ctx()))
        self.assertEqual(commented['findings'], [])

    def test_config_lint_stays_in_configs_folder(self):
        for name in ('../ssot.json', '/etc/passwd', 'missing.cfg'):
            self.assertIn('No config file', config_lint({'file': name}, tool_ctx()))
        with self.assertRaises(ValueError):
            config_lint({}, tool_ctx())
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'r1.cfg').write_text('ip http server\n')
            out = json.loads(config_lint({'file': 'r1.cfg'}, tool_ctx(configs_dir=Path(tmp))))
            self.assertIn('CFG-MGMT-02', [f['rule'] for f in out['findings']])


class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = ThreadingHTTPServer(('127.0.0.1', 0), TemplateModel)
        threading.Thread(target=cls.model.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.model.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.model.shutdown()
        cls.model.server_close()

    def tearDown(self):
        TemplateModel.LEAK = False

    def doc(self, name):
        d = json.loads((ROOT / 'diagrams' / f'{name}.json').read_text())
        for b in d['blocks']:
            if b['type'] == 'agent':
                b['config'].update(model_url=self.url, model='scripted')
        return d

    def ctx(self):
        return Context(ssot=ssot(), runbooks=json.loads((ROOT / 'runbooks.json').read_text()),
                       monitoring=adapters.SampleMonitoring(), knowledge_dir=ROOT / 'knowledge')

    def test_templates_are_valid_and_carry_evals(self):
        for name in TEMPLATES:
            diagram = load_file(ROOT / 'diagrams' / f'{name}.json')
            self.assertEqual(validate(diagram, 'sample'), [], name)
            doc = json.loads((ROOT / 'diagrams' / f'{name}.json').read_text())
            self.assertGreaterEqual(len(doc['evals']), 3, name)
            self.assertEqual(evals.validate_cases(doc['evals']), [], name)
            self.assertTrue(set(doc['layout']) == {b['id'] for b in doc['blocks']}, name)

    def test_new_templates_pass_their_evals_as_diagram_and_export(self):
        for name in ('capacity-forecast', 'config-review'):
            doc = self.doc(name)
            diagram = load(doc)
            a = evals.run_evals(doc['evals'], evals.diagram_runner(diagram, self.ctx()))
            module = evals.load_module(export_python(diagram), ROOT, name=f'exp_{name.replace("-", "_")}')
            b = evals.run_evals(doc['evals'], evals.module_runner(module, adapters.SampleMonitoring()))
            self.assertEqual((a['passed'], a['total']), (3, 3), (name, a['results']))
            self.assertEqual([(r['passed'], r['draft']) for r in a['results']], [(r['passed'], r['draft']) for r in b['results']])

    def test_config_review_redacts_a_leaked_secret(self):
        TemplateModel.LEAK = True
        doc = self.doc('config-review')
        report = evals.run_evals(doc['evals'][:1], evals.diagram_runner(load(doc), self.ctx()))
        self.assertTrue(report['results'][0]['passed'], report['results'][0])
        self.assertIn('[REDACTED', report['results'][0]['draft'])

    def test_config_review_blocks_a_compliance_claim(self):
        doc = self.doc('config-review')
        cases = [{'id': 'claim', 'input': 'Review sample-dc-core.cfg', 'expect': {'status': 'blocked'}}]
        original = TemplateModel.config_review

        def claims(handler, user, results):
            msg = original(handler, user, results)
            if msg.get('content'):
                msg['content'] = 'The configuration is fully compliant.'
            return msg
        TemplateModel.config_review = claims
        self.addCleanup(setattr, TemplateModel, 'config_review', original)
        report = evals.run_evals(cases, evals.diagram_runner(load(doc), self.ctx()))
        self.assertTrue(report['results'][0]['passed'], report['results'][0])

    def test_capacity_forecast_remembers_completed_runs(self):
        doc = self.doc('capacity-forecast')
        with tempfile.TemporaryDirectory() as tmp:
            module = evals.load_module(export_python(load(doc)), ROOT, name='exp_capacity_memory')
            for _ in range(2):
                state = module.run(None, approve=lambda b, v: True, output_dir=Path(tmp), memory_path=Path(tmp) / 'm.db')
                self.assertEqual(state['status'], 'completed')
            from bya.graph.memory import MemoryStore
            self.assertEqual(len(MemoryStore(Path(tmp) / 'm.db').recent_runs('capacity', 5)), 2)
            self.assertTrue((Path(tmp) / 'briefs' / 'capacity-forecast.md').exists())


if __name__ == '__main__':
    unittest.main()
