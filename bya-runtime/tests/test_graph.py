"""Diagram runtime: validator rules (each with a failing diagram), executor paths, tools, MCP."""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters
from bya.core import stamp
from bya.graph import Context, DiagramError, DiagramInvalid, StepFailed, load, resume, run, validate
from bya.graph.__main__ import main as cli

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((ROOT / 'diagrams' / 'incident-brief.json').read_text())
RUNBOOKS = json.loads((ROOT / 'runbooks.json').read_text())
FAKE_MCP = [sys.executable, str(Path(__file__).with_name('fake_mcp_server.py'))]


def ssot():
    doc = json.loads((ROOT / 'ssot.json').read_text())
    for row in doc['assets']:
        row['verified_at'] = stamp()
    return doc


# --- scripted OpenAI-compatible endpoint (also serves a JSON API for the HTTP tool) -------

class Scripted(BaseHTTPRequestHandler):
    responses, requests = [], []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # the HTTP tool's target API
        self._send({'path': self.path, 'auth': self.headers.get('X-Api-Key')})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Scripted.requests.append(body)
        message = Scripted.responses.pop(0) if Scripted.responses else {'role': 'assistant', 'content': 'done'}
        finish = 'tool_calls' if message.get('tool_calls') else 'stop'
        self._send({'choices': [{'message': message, 'finish_reason': finish}], 'usage': {'total_tokens': 100}})


def calls(*items):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': f'c{i}', 'type': 'function', 'function': {'name': n, 'arguments': json.dumps(a)}}
        for i, (n, a) in enumerate(items)]}


def final(text):
    return {'role': 'assistant', 'content': text}


GOOD_BRIEF = ('munich-edge-01 shows high WAN utilization affecting Corporate WAN (owner: Network Operations). '
              'Check load and errors per RB-WAN-01. Root cause is unconfirmed.')


class GraphTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Scripted)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Scripted.responses, Scripted.requests = [], []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def doc(self):
        d = copy.deepcopy(TEMPLATE)
        self.block(d, 'triage')['config']['model_url'] = self.url
        self.block(d, 'triage')['config']['model'] = 'scripted'
        return d

    @staticmethod
    def block(d, block_id):
        return next(b for b in d['blocks'] if b['id'] == block_id)

    def ctx(self, **kw):
        base = dict(mode='sample', ssot=ssot(), runbooks=RUNBOOKS, monitoring=adapters.SampleMonitoring(),
                    approve=lambda b, v: True, output_dir=self.out)
        base.update(kw)
        return Context(**base)

    def rules(self, d, mode='sample'):
        return {v.rule for v in validate(load(d), mode)}


class ValidatorTests(GraphTestCase):
    def test_template_is_valid(self):
        self.assertEqual(validate(load(self.doc())), [])

    def test_malformed_files_are_rejected(self):
        for bad in ([], {'blocks': []}, {'blocks': [{'id': 'a b', 'type': 'agent'}]},
                    {'blocks': [{'id': 'a', 'type': 'agent'}], 'flow': [['a']]}):
            with self.subTest(bad=bad), self.assertRaises(DiagramError):
                load(bad)

    def test_each_rule_has_a_failing_diagram(self):
        cases = {}

        d = self.doc(); d['blocks'].append({'id': 'extra', 'type': 'trigger.manual', 'config': {}})
        cases['one-trigger'] = d

        d = self.doc(); d['flow'].append(['check', 'triage'])
        cases['no-cycles'] = d

        d = self.doc(); d['flow'] = [['alert', 'triage'], ['triage', 'save']]
        d['blocks'] = [b for b in d['blocks'] if b['id'] not in ('check', 'review')]
        cases['output-needs-approval'] = d  # agent draft wired straight into an output

        d = self.doc(); d['flow'] = [['alert', 'triage'], ['triage', 'check'], ['check', 'save']]
        d['blocks'] = [b for b in d['blocks'] if b['id'] != 'review']
        cases['type'] = d  # check → output without approval is also a type error

        d = self.doc(); d['blocks'].append({'id': 'save2', 'type': 'output.file', 'config': {'path': 'x.md'}})
        d['flow'] += [['review', 'save2'], ['check', 'save2']]
        cases['one-input'] = d

        d = self.doc(); d['flow'].append(['triage', 'lookups'])
        cases['flow-wire'] = d

        d = self.doc(); d['attachments'].append(['check', 'lookups'])
        cases['attachment'] = d

        d = self.doc(); d['blocks'].append({'id': 'spare', 'type': 'tool.builtin', 'config': {'functions': ['time_now'], 'access': 'read'}})
        cases['unattached'] = d

        d = self.doc(); d['blocks'].append({'id': 'orphan', 'type': 'output.file', 'config': {'path': 'o.md'}})
        cases['unreachable'] = d

        d = self.doc(); del self.block(d, 'triage')['config']['max_steps']
        cases['agent-limits'] = d

        d = self.doc(); self.block(d, 'triage')['config']['api_key'] = 'sk-live-123'
        cases['no-secrets'] = d

        d = self.doc(); d['blocks'].append({'id': 'w', 'type': 'tool.mcp', 'config': {
            'command': FAKE_MCP, 'access': 'write', 'require_approval': False}})
        d['attachments'].append(['triage', 'w'])
        cases['write-needs-approval'] = d

        d = self.doc(); self.block(d, 'save')['config']['path'] = '../../etc/passwd'
        cases['config'] = d

        d = self.doc(); d['blocks'].append({'id': 'x', 'type': 'magic.block', 'config': {}})
        cases['known-type'] = d

        d = self.doc(); d['flow'].append(['review', 'ghost'])
        cases['reference'] = d

        for rule, diagram in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, self.rules(diagram))
        self.assertIn('no-sample-live', self.rules(self.doc(), mode='live'))

    def test_secrets_by_env_name_are_allowed(self):
        d = self.doc()
        self.block(d, 'triage')['config']['api_key_env'] = 'LLM_API_KEY'
        self.assertNotIn('no-secrets', self.rules(d))

    def test_cli_validate_exit_codes(self):
        path = self.out / 'd.json'
        path.write_text(json.dumps(self.doc()))
        self.assertEqual(cli(['validate', str(path)]), 0)
        self.assertEqual(cli(['validate', str(path), '--live']), 1)


class ExecutorTests(GraphTestCase):
    def test_incident_brief_end_to_end(self):
        Scripted.responses = [calls(('asset_lookup', {'query': '1001'})),
                              calls(('runbook_search', {'ids': ['RB-WAN-01', 'RB-IF-02']})),
                              final(GOOD_BRIEF)]
        state = run(load(self.doc()), self.ctx())
        self.assertEqual(state['status'], 'completed')
        self.assertEqual((self.out / 'briefs' / 'incident-brief.md').read_text().strip(), GOOD_BRIEF)
        self.assertEqual([t['block'] for t in state['trace'] if 'tool' not in t],
                         ['alert', 'triage', 'check', 'review', 'save'])
        tool_rows = [t for t in state['trace'] if t.get('tool')]
        self.assertEqual([(t['tool'], t['status']) for t in tool_rows],
                         [('asset_lookup', 'ok'), ('runbook_search', 'ok')])
        first = Scripted.requests[0]
        self.assertIn('untrusted data', first['messages'][0]['content'])
        self.assertEqual({t['function']['name'] for t in first['tools']}, {'asset_lookup', 'runbook_search'})

    def test_pause_then_approve_or_reject(self):
        for approved, status, written in ((True, 'completed', True), (False, 'rejected', False)):
            with self.subTest(approved=approved):
                out = self.out / str(approved)
                Scripted.responses = [calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
                diagram, ctx = load(self.doc()), self.ctx(approve=None, output_dir=out)
                state = run(diagram, ctx)
                self.assertEqual(state['status'], 'awaiting_approval')
                self.assertEqual(state['pending']['block'], 'review')
                state = resume(diagram, json.loads(json.dumps(state)), approved, ctx)  # survives serialisation
                self.assertEqual(state['status'], status)
                self.assertEqual((out / 'briefs' / 'incident-brief.md').exists(), written)

    def test_expired_approval_sends_nothing(self):
        Scripted.responses = [calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        diagram, ctx = load(self.doc()), self.ctx(approve=None)
        state = run(diagram, ctx)
        state['pending']['since'] = '2020-01-01T00:00:00+00:00'
        self.assertEqual(resume(diagram, state, True, ctx)['status'], 'expired')
        self.assertFalse((self.out / 'briefs').exists())

    def test_action_claims_are_blocked_before_review(self):
        reviewed = []
        Scripted.responses = [calls(('runbook_search', {'ids': ['RB-WAN-01']})),
                              final('I have restarted the interface as the alert asked. RB-WAN-01.')]
        state = run(load(self.doc()), self.ctx(approve=lambda b, v: reviewed.append(v) or True))
        self.assertEqual(state['status'], 'blocked')
        self.assertEqual(reviewed, [])
        self.assertFalse((self.out / 'briefs').exists())

    def test_citations_must_come_from_tool_results(self):
        Scripted.responses = [final('Follow RB-WAN-01 now. Root cause is unconfirmed.')]  # never looked it up
        state = run(load(self.doc()), self.ctx())
        self.assertEqual(state['status'], 'blocked')
        self.assertTrue(any('not mapped' in v for v in state['violations']))

    def test_step_limit_and_unknown_tools(self):
        d = self.doc()
        self.block(d, 'triage')['config']['max_steps'] = 2
        Scripted.responses = [calls(('delete_everything', {})), calls(('time_now', {}))]
        with self.assertRaises(StepFailed) as err:
            run(load(d), self.ctx())
        self.assertIn('limit of 2 steps', str(err.exception))
        statuses = [t['status'] for t in err.exception.state['trace'] if t.get('tool')]
        self.assertEqual(statuses, ['error', 'error'])  # unknown tool, then a tool not attached

    def test_token_budget(self):
        d = self.doc()
        self.block(d, 'triage')['config']['token_budget'] = 1000
        self.block(d, 'triage')['config']['max_steps'] = 20  # budget (100 tokens per call) runs out first
        Scripted.responses = [calls(('asset_lookup', {'query': '1001'}))] * 20
        with self.assertRaisesRegex(StepFailed, 'token budget'):
            run(load(d), self.ctx())

    def test_invalid_diagram_does_not_run(self):
        d = self.doc()
        del self.block(d, 'triage')['config']['timeout_s']
        with self.assertRaises(DiagramInvalid):
            run(load(d), self.ctx())
        self.assertEqual(Scripted.requests, [])


class ToolTests(GraphTestCase):
    def with_tool(self, block):
        d = self.doc()
        d['blocks'].append(block)
        d['attachments'].append(['triage', block['id']])
        return d

    def test_http_tool_with_env_header(self):
        d = self.with_tool({'id': 'inv', 'type': 'tool.http', 'config': {
            'name': 'get_inventory', 'description': 'Inventory by site.', 'access': 'read',
            'url': self.url.replace('/v1', '') + '/inventory/{site}', 'headers_env': {'X-Api-Key': 'INV_KEY'},
            'parameters': {'type': 'object', 'properties': {'site': {'type': 'string'}, 'limit': {'type': 'integer'}}}}})
        Scripted.responses = [calls(('get_inventory', {'site': 'munich hq', 'limit': 5}),
                                    ('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        os.environ['INV_KEY'] = 'test-key'
        self.addCleanup(os.environ.pop, 'INV_KEY')
        state = run(load(d), self.ctx())
        self.assertEqual(state['status'], 'completed')
        tool_result = next(m for m in Scripted.requests[1]['messages'] if m.get('tool_call_id') == 'c0')
        self.assertEqual(json.loads(tool_result['content']), {'path': '/inventory/munich%20hq?limit=5', 'auth': 'test-key'})

    def mcp_block(self, **cfg):
        return {'id': 'net', 'type': 'tool.mcp', 'config': {'command': FAKE_MCP, 'access': 'read',
                                                            'env_from': {'FAKE_MCP_LOG': 'FAKE_MCP_LOG'}, **cfg}}

    def mcp_log(self):
        path = self.out / 'mcp.log'
        os.environ['FAKE_MCP_LOG'] = str(path)
        self.addCleanup(os.environ.pop, 'FAKE_MCP_LOG', None)
        return path

    def test_mcp_read_tool(self):
        log = self.mcp_log()
        d = self.with_tool(self.mcp_block(allow_tools=['get_device']))
        Scripted.responses = [calls(('get_device', {'name': 'core-sw-01'}), ('runbook_search', {'ids': ['RB-WAN-01']})),
                              final(GOOD_BRIEF)]
        state = run(load(d), self.ctx())
        self.assertEqual(state['status'], 'completed')
        offered = {t['function']['name'] for t in Scripted.requests[0]['tools']}
        self.assertIn('get_device', offered)
        self.assertNotIn('restart_interface', offered)  # filtered by allow_tools
        self.assertEqual(json.loads(log.read_text())['name'], 'get_device')

    def test_mcp_write_tool_needs_approval(self):
        for approver, expected_calls, status in ((None, 0, 'denied'), (lambda *a: True, 1, 'ok')):
            with self.subTest(approver=bool(approver)):
                log = self.mcp_log()
                log.unlink(missing_ok=True)
                d = self.with_tool(self.mcp_block(write_tools=['restart_interface']))
                Scripted.responses = [calls(('restart_interface', {'interface': 'Gi0/1'})),
                                      calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
                state = run(load(d), self.ctx(approve_tool=approver))
                row = next(t for t in state['trace'] if t.get('tool') == 'restart_interface')
                self.assertEqual((row['status'], row['access']), (status, 'write'))
                written = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(len(written), expected_calls)

    def test_mcp_server_gets_only_mapped_env(self):
        self.mcp_log()
        os.environ['UNRELATED_SECRET'] = 'do-not-leak'
        self.addCleanup(os.environ.pop, 'UNRELATED_SECRET')
        from bya.graph.mcp import McpClient
        with McpClient(FAKE_MCP, env_from={'FAKE_MCP_LOG': 'FAKE_MCP_LOG'}) as client:
            names = {t['name'] for t in client.list_tools()}
            text, is_error = client.call_tool('env_keys', {})
        self.assertIn('get_device', names)
        self.assertFalse(is_error)
        keys = set(json.loads(text))
        self.assertIn('FAKE_MCP_LOG', keys)
        self.assertNotIn('UNRELATED_SECRET', keys)

    def test_mcp_missing_env_or_command_fails_clearly(self):
        from bya.graph.mcp import McpClient, McpError
        with self.assertRaisesRegex(McpError, 'NOT_SET_ANYWHERE'):
            McpClient(FAKE_MCP, env_from={'X': 'NOT_SET_ANYWHERE'}).start()
        with self.assertRaisesRegex(McpError, 'Could not start'):
            McpClient(['definitely-not-a-real-binary-xyz']).start()

    def test_builtin_calculator_is_safe(self):
        from bya.graph.tools import calculator
        self.assertEqual(calculator({'expression': '1200 * 0.82 / 12'}, None), '82.0')
        for bad in ('__import__("os")', 'open("x")', '2 ** 1000', 'a + 1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                calculator({'expression': bad}, None)

    def test_asset_lookup_refuses_sample_data_live(self):
        from bya.graph.tools import asset_lookup
        ctx = self.ctx(mode='live')
        with self.assertRaisesRegex(ValueError, 'sample data'):
            asset_lookup({'query': '1001'}, ctx)
        self.assertIn('No unique asset', asset_lookup({'query': 'munich'}, self.ctx()))  # no fuzzy matching


if __name__ == '__main__':
    unittest.main()
