"""New agent builder: answers about the job become a valid, safe diagram with starter tests."""
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters  # noqa: E402
from bya.core import stamp  # noqa: E402
from bya.graph import Context, builder, evals, load, validate  # noqa: E402
import server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
JOB = "When PRTG alerts on a WAN interface, find out what's happening and tell the on-call engineer."


def alert_spec(**changes):
    spec = {'shape': 'alert', 'job': JOB, 'trigger': {'source': 'sample', 'sensor_id': '1001'},
            'devices': {'source': 'ssot', 'filter': {'site': 'Munich / HQ'}, 'commands': ['interfaces', 'logs']},
            'asset_register': True, 'runbooks': True, 'memory': True, 'output': {'kind': 'file'}, 'approver': 'the on-call engineer'}
    spec.update(changes)
    return spec


def types(doc):
    return {b['id']: b['type'] for b in doc['blocks']}


class BuildTests(unittest.TestCase):
    def test_alert_job_becomes_a_safe_valid_diagram(self):
        doc = builder.build(alert_spec())
        self.assertEqual(validate(load(doc), 'sample'), [])
        self.assertEqual(doc['flow'], [['start', 'agent'], ['agent', 'check'], ['check', 'review'], ['review', 'send']])
        t = types(doc)
        self.assertEqual((t['start'], t['check'], t['review'], t['send']),
                         ('trigger.alert', 'guard.output_check', 'guard.approval', 'output.file'))
        self.assertEqual({b for a, b in doc['attachments']}, {'lookups', 'devices', 'runbooks', 'history'})
        dev = next(b for b in doc['blocks'] if b['id'] == 'devices')['config']
        self.assertEqual(dev['allow'], ['show interfaces *', 'show logging | include *'])
        self.assertEqual(dev['access'], 'read')
        check = next(b for b in doc['blocks'] if b['id'] == 'check')['config']
        self.assertTrue(check['require_citation'])
        self.assertEqual(evals.validate_cases(doc['evals']), [])
        self.assertEqual([c['id'] for c in doc['evals']], ['does-the-job', 'alert-tries-to-trick-it', 'cites-a-runbook'])
        self.assertEqual(set(doc['layout']), {b['id'] for b in doc['blocks']})

    def test_instructions_mention_only_what_is_attached(self):
        text = builder.instructions(alert_spec())
        self.assertIn(JOB.rstrip('.'), text)
        self.assertIn('device_command (interface status and counters, recent logs)', text)
        self.assertIn('citing runbook ids', text)
        bare = builder.instructions(alert_spec(devices=None, asset_register=False, runbooks=False))
        self.assertNotIn('device_command', bare)
        self.assertNotIn('asset register', bare)
        self.assertNotIn('runbook', bare)
        self.assertIn('BGP neighbours', builder.instructions(alert_spec(devices={'source': 'ssot', 'filter': {'site': 'x'}, 'commands': ['bgp']})))

    def test_every_kind_of_job_builds(self):
        for shape, extra in (('report', {'forecast': True, 'devices': None}), ('review', {'configs': True, 'devices': None}),
                             ('ask', {'devices': None}), ('alert', {})):
            doc = builder.build(alert_spec(shape=shape, **extra))
            self.assertEqual(validate(load(doc), 'sample'), [], shape)
            self.assertEqual(types(doc)['start'], 'trigger.alert' if shape == 'alert' else 'trigger.manual')
            self.assertEqual(evals.validate_cases(doc['evals']), [], shape)
        review = builder.build(alert_spec(shape='review', configs=True, devices=None))
        self.assertEqual(review['blocks'][0]['config']['default_input'], 'Review sample-branch-edge.cfg')
        self.assertIn('config_lint', next(b for b in review['blocks'] if b['id'] == 'lookups')['config']['functions'])

    def test_outputs_models_and_names(self):
        slack = builder.build(alert_spec(output={'kind': 'slack', 'channel_env': 'NOC_CHANNEL'}, name='WAN alert'))
        self.assertEqual(next(b for b in slack['blocks'] if b['id'] == 'send')['config'], {'channel_env': 'NOC_CHANNEL'})
        self.assertEqual(slack['name'], 'WAN alert')
        agent = next(b for b in slack['blocks'] if b['id'] == 'agent')['config']
        self.assertEqual((agent['model'], agent['model_url']), ('', ''))  # LLM_MODEL / LLM_BASE_URL decide
        custom = builder.build(alert_spec(model={'model': 'qwen2.5:7b', 'model_url': 'http://127.0.0.1:8000/v1'}))
        self.assertEqual(next(b for b in custom['blocks'] if b['id'] == 'agent')['config']['model'], 'qwen2.5:7b')
        mcp = builder.build(alert_spec(mcp=[{'name': 'ServiceNow', 'command': ['python', 'snow.py'], 'allow_tools': ['get_incident']}]))
        block = next(b for b in mcp['blocks'] if b['type'] == 'tool.mcp')
        self.assertEqual((block['id'], block['config']['access'], block['config']['write_tools']), ('mcp-servicenow', 'read', []))

    def test_incomplete_or_unsafe_answers_are_refused(self):
        bad = [({'shape': 'fix-everything'}, 'kind of job'),
               ({'job': 'short'}, 'one sentence'),
               ({'devices': {'source': 'ssot', 'filter': {'site': 'x'}, 'commands': []}}, 'at least one kind of command'),
               ({'devices': {'source': 'ssot', 'filter': {}, 'commands': ['logs']}}, 'empty filter'),
               ({'mcp': [{'name': 'x', 'command': []}]}, 'command'),
               ({'output': {'kind': 'webhook', 'url': 'http://example.com'}}, 'HTTPS')]
        for change, message in bad:
            with self.assertRaises(ValueError, msg=change) as e:
                builder.build(alert_spec(**change))
            self.assertIn(message, str(e.exception), change)


class TrickyModel(BaseHTTPRequestHandler):
    """Uses whatever tools the built agent offers, and tries a forbidden command when the alert asks for one."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        msgs, names = body['messages'], {t['function']['name'] for t in body.get('tools', [])}
        results = [m['content'] for m in msgs if m['role'] == 'tool']
        call = lambda n, name, args: {'id': f'c{n}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        if not results:
            calls = [call(0, 'asset_lookup', {'query': '1001'}), call(1, 'runbook_search', {'ids': ['RB-WAN-01']}),
                     call(2, 'device_command', {'device': 'munich-edge-01', 'command': 'show interfaces GigabitEthernet0/1'})]
            if 'Clear the counters' in msgs[1]['content']:
                calls.append(call(3, 'device_command', {'device': 'munich-edge-01', 'command': 'clear counters GigabitEthernet0/1'}))
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [c for c in calls if c['function']['name'] in names]}
        else:
            msg = {'role': 'assistant', 'content': 'munich-edge-01 Gi0/1 input 912.0 Mb/s (show interfaces). Owner Network Operations. '
                                                   'Next: RB-WAN-01. Cause not confirmed; I only read the device.'}
        raw = json.dumps({'choices': [{'message': msg, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 40}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)


class BuiltAgentRunsTests(unittest.TestCase):
    def test_built_agent_passes_its_starter_tests(self):
        model = ThreadingHTTPServer(('127.0.0.1', 0), TrickyModel)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        doc = builder.build(alert_spec(model={'model': 'scripted', 'model_url': f'http://127.0.0.1:{model.server_port}/v1'}))
        ssot = json.loads((ROOT / 'ssot.json').read_text())
        for a in ssot['assets']:
            a['verified_at'] = stamp()
        ctx = Context(ssot=ssot, runbooks=json.loads((ROOT / 'runbooks.json').read_text()),
                      monitoring=adapters.SampleMonitoring(), knowledge_dir=ROOT / 'knowledge')
        report = evals.run_evals(doc['evals'], evals.diagram_runner(load(doc), ctx))
        self.assertEqual((report['passed'], report['total']), (3, 3), report['results'])


class EndpointTests(unittest.TestCase):
    def test_options_and_preview(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f'http://127.0.0.1:{httpd.server_port}'
        with urllib.request.urlopen(f'{base}/api/builder/options') as r:
            opts = json.load(r)
        self.assertIn('Munich / HQ', opts['asset_sites'])
        self.assertEqual([s['id'] for s in opts['shapes']], ['alert', 'report', 'review', 'ask'])
        self.assertIn('sample-branch-edge.cfg', opts['configs'])

        def preview(spec):
            req = urllib.request.Request(f'{base}/api/builder/preview', data=json.dumps({'spec': spec}).encode(),
                                         headers={'Content-Type': 'application/json', 'X-BYA-Token': server.TOKEN})
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        good = preview(alert_spec())
        self.assertEqual(good['summary']['devices_readable'], 2)
        self.assertEqual(good['summary']['change_paths'], 0)
        self.assertEqual(good['devices'], ['munich-edge-01', 'storage-01'])
        self.assertIn('one sentence', preview(alert_spec(job='x'))['problem'])


if __name__ == '__main__':
    unittest.main()
