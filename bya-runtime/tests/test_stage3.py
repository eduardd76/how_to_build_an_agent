"""Approval inbox (write tools pause and resume), memory blocks, permission policy and redaction."""
import json
import os
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_graph import FAKE_MCP, GOOD_BRIEF, GraphTestCase, Scripted, calls, final  # noqa: E402
from bya.graph import StepFailed, load, resume, run, validate  # noqa: E402
import server  # noqa: E402


def user_content(request):
    return json.loads(request['messages'][1]['content'])


def tool_results(request):
    return [m['content'] for m in request['messages'] if m.get('role') == 'tool']


class Stage3Case(GraphTestCase):
    def setUp(self):
        super().setUp()
        self.memory = self.out / 'memory.sqlite3'

    def ctx(self, **kw):
        kw.setdefault('memory_path', self.memory)
        kw.setdefault('knowledge_dir', self.out / 'knowledge')
        return super().ctx(**kw)

    def with_blocks(self, *blocks, attach=(), doc=None):
        d = doc or self.doc()
        d['blocks'].extend(blocks)
        d['attachments'].extend(['triage', b] for b in attach)
        return d

    def mcp_write(self):
        log = self.out / 'mcp.log'
        os.environ['FAKE_MCP_LOG'] = str(log)
        self.addCleanup(os.environ.pop, 'FAKE_MCP_LOG', None)
        block = {'id': 'net', 'type': 'tool.mcp', 'config': {'command': FAKE_MCP, 'access': 'read',
                 'write_tools': ['restart_interface'], 'allow_tools': ['restart_interface'],
                 'env_from': {'FAKE_MCP_LOG': 'FAKE_MCP_LOG'}}}
        return block, log


class InboxTests(Stage3Case):
    def test_write_tool_pauses_then_resumes_on_approval(self):
        block, log = self.mcp_write()
        diagram = load(self.with_blocks(block, attach=['net']))
        ctx = self.ctx(approve=None, pause_for_tool_approval=True)
        Scripted.responses = [calls(('restart_interface', {'interface': 'Gi0/1'})),
                              calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        state = run(diagram, ctx)
        self.assertEqual(state['status'], 'awaiting_approval')
        self.assertEqual((state['pending']['kind'], state['pending']['tool']), ('tool', 'restart_interface'))
        self.assertFalse(log.exists())  # nothing ran before the decision
        state = resume(diagram, json.loads(json.dumps(state)), True, ctx)
        self.assertIn('restarted Gi0/1', tool_results(Scripted.requests[1]))
        self.assertEqual((state['status'], state['pending']['kind'] if state['pending'] else None), ('awaiting_approval', 'draft'))
        state = resume(diagram, state, True, ctx)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertTrue((self.out / 'briefs' / 'incident-brief.md').exists())

    def test_denied_write_tool_is_reported_to_the_agent(self):
        block, log = self.mcp_write()
        diagram = load(self.with_blocks(block, attach=['net']))
        ctx = self.ctx(approve=None, pause_for_tool_approval=True)
        Scripted.responses = [calls(('restart_interface', {'interface': 'Gi0/1'})),
                              calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        state = resume(diagram, run(diagram, ctx), False, ctx)
        self.assertTrue(tool_results(Scripted.requests[1])[0].startswith('Denied'))
        self.assertEqual(state['pending']['kind'], 'draft')  # the run continues to the draft approval
        self.assertFalse(log.exists())

    def test_expired_tool_approval(self):
        block, _ = self.mcp_write()
        diagram = load(self.with_blocks(block, attach=['net']))
        ctx = self.ctx(approve=None, pause_for_tool_approval=True)
        Scripted.responses = [calls(('restart_interface', {'interface': 'Gi0/1'}))]
        state = run(diagram, ctx)
        state['pending']['since'] = '2020-01-01T00:00:00+00:00'
        self.assertEqual(resume(diagram, state, True, ctx)['status'], 'expired')


class PolicyTests(Stage3Case):
    def policy(self, **cfg):
        return {'id': 'rules', 'type': 'guard.policy', 'config': cfg}

    def test_deny_tools(self):
        d = self.with_blocks(self.policy(deny_tools=['asset_lookup']), attach=['rules'])
        Scripted.responses = [calls(('asset_lookup', {'query': '1001'}), ('runbook_search', {'ids': ['RB-WAN-01']})),
                              final(GOOD_BRIEF)]
        state = run(load(d), self.ctx())
        rows = {t['tool']: t['status'] for t in state['trace'] if t.get('tool')}
        self.assertEqual(rows, {'asset_lookup': 'denied', 'runbook_search': 'ok'})

    def test_require_approval_for_a_read_tool(self):
        d = self.with_blocks(self.policy(require_approval_tools=['asset_lookup']), attach=['rules'])
        Scripted.responses = [calls(('asset_lookup', {'query': '1001'}))]
        state = run(load(d), self.ctx(approve=None, pause_for_tool_approval=True))
        self.assertEqual((state['status'], state['pending']['tool']), ('awaiting_approval', 'asset_lookup'))

    def test_max_tool_calls(self):
        d = self.with_blocks(self.policy(max_tool_calls=1), attach=['rules'])
        Scripted.responses = [calls(('asset_lookup', {'query': '1001'}), ('runbook_search', {'ids': ['RB-WAN-01']}))]
        with self.assertRaisesRegex(StepFailed, 'more than 1 tool calls'):
            run(load(d), self.ctx())

    def test_one_policy_per_agent(self):
        d = self.with_blocks(self.policy(), {'id': 'rules2', 'type': 'guard.policy', 'config': {}}, attach=['rules', 'rules2'])
        self.assertIn('one-policy', self.rules(d))


class MemoryTests(Stage3Case):
    def test_facts_persist_across_runs(self):
        d = self.with_blocks({'id': 'facts', 'type': 'memory.kv', 'config': {'namespace': 'triage'}}, attach=['facts'])
        Scripted.responses = [calls(('remember', {'key': 'preferred_site', 'value': 'Munich HQ'}),
                                    ('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        self.assertEqual(run(load(d), self.ctx())['status'], 'completed')
        Scripted.requests = []
        Scripted.responses = [calls(('recall', {'key': 'preferred_site'}), ('runbook_search', {'ids': ['RB-WAN-01']})),
                              final(GOOD_BRIEF)]
        run(load(d), self.ctx())
        self.assertEqual(tool_results(Scripted.requests[1])[0], 'Munich HQ')

    def test_fact_memory_is_bounded(self):
        d = self.with_blocks({'id': 'facts', 'type': 'memory.kv', 'config': {'namespace': 'tiny', 'max_entries': 1}},
                             attach=['facts'])
        Scripted.responses = [calls(('remember', {'key': 'a', 'value': '1'}), ('remember', {'key': 'b', 'value': '2'}),
                                    ('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        run(load(d), self.ctx())
        self.assertIn('is full', tool_results(Scripted.requests[1])[1])

    def test_run_history_only_remembers_completed_runs(self):
        d = self.with_blocks({'id': 'hist', 'type': 'memory.conversation', 'config': {'namespace': 'brief', 'max_items': 5}},
                             attach=['hist'])
        # 1. blocked run: must not be remembered
        Scripted.responses = [final('I have restarted the interface. RB-WAN-01.')]
        self.assertEqual(run(load(d), self.ctx())['status'], 'blocked')
        # 2. completed run: remembered
        Scripted.responses = [calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        self.assertEqual(run(load(d), self.ctx())['status'], 'completed')
        # 3. the next run sees only the completed one
        Scripted.requests = []
        Scripted.responses = [final(GOOD_BRIEF)]
        run(load(d), self.ctx())
        previous = user_content(Scripted.requests[0])['previous_runs']
        self.assertEqual([p['result'] for p in previous], [GOOD_BRIEF])

    def test_document_search(self):
        kdir = self.out / 'knowledge'
        kdir.mkdir()
        (kdir / 'wan.md').write_text('Escalate sustained WAN utilization above 85% to the on-call engineer.')
        (kdir / 'other.txt').write_text('Printer toner replacement steps.')
        d = self.with_blocks({'id': 'docs', 'type': 'memory.documents', 'config': {'folder': '.', 'max_results': 2}},
                             attach=['docs'])
        Scripted.responses = [calls(('search_documents', {'query': 'WAN escalation utilization'}),
                                    ('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        run(load(d), self.ctx())
        hits = json.loads(tool_results(Scripted.requests[1])[0])
        self.assertEqual(hits[0]['file'], 'wan.md')
        self.assertEqual(len(hits), 1)
        d['blocks'][-1]['config']['folder'] = '../../etc'
        self.assertIn('config', self.rules(d))


class RedactTests(Stage3Case):
    def test_secrets_and_emails_are_redacted_before_approval(self):
        d = self.doc()
        d['blocks'].append({'id': 'scrub', 'type': 'guard.redact', 'config': {'emails': True}})
        d['flow'] = [['alert', 'triage'], ['triage', 'scrub'], ['scrub', 'check'], ['check', 'review'], ['review', 'save']]
        leaky = GOOD_BRIEF + ' Contact ops@example.com. Use Bearer abcdefghijklmnop and password=hunter2.'
        seen = []
        Scripted.responses = [calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(leaky)]
        state = run(load(d), self.ctx(approve=lambda b, v: seen.append(v) or True))
        self.assertEqual(state['status'], 'completed')
        for secret in ('ops@example.com', 'abcdefghijklmnop', 'hunter2'):
            self.assertNotIn(secret, seen[0])
        self.assertIn('[REDACTED email]', seen[0])
        self.assertIn('Redacted', next(t['detail'] for t in state['trace'] if t['block'] == 'scrub'))


class InboxApiTests(Stage3Case):
    def test_pending_list_and_tool_approval_over_http(self):
        block, log = self.mcp_write()
        root = self.out / 'root'
        (root / 'web').mkdir(parents=True)
        for name in ('ssot.json', 'runbooks.json'):
            (root / name).write_text(json.dumps(json.loads((Path(server.__file__).parent / name).read_text())))
        ssot = json.loads((root / 'ssot.json').read_text())
        for row in ssot['assets']:
            row['verified_at'] = __import__('bya.core', fromlist=['stamp']).stamp()
        (root / 'ssot.json').write_text(json.dumps(ssot))
        old_root, server.ROOT = server.ROOT, root
        self.addCleanup(setattr, server, 'ROOT', old_root)
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f'http://127.0.0.1:{httpd.server_port}'

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json', 'X-BYA-Token': server.TOKEN})
            try:
                with urllib.request.urlopen(req) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                return json.load(e)

        Scripted.responses = [calls(('restart_interface', {'interface': 'Gi0/1'})),
                              calls(('runbook_search', {'ids': ['RB-WAN-01']})), final(GOOD_BRIEF)]
        run1 = post('/api/diagram/run', {'diagram': self.with_blocks(block, attach=['net'])})
        self.assertEqual(run1['status'], 'awaiting_approval')
        with urllib.request.urlopen(base + '/api/diagram/pending') as r:
            pending = json.load(r)['pending']
        self.assertEqual([(p['id'], p['kind'], p['tool']) for p in pending], [(run1['id'], 'tool', 'restart_interface')])
        step2 = post('/api/diagram/approve', {'id': run1['id'], 'approved': True})
        self.assertEqual((step2['status'], step2['pending']['kind']), ('awaiting_approval', 'draft'))
        self.assertEqual(len(log.read_text().splitlines()), 1)
        done = post('/api/diagram/approve', {'id': run1['id'], 'approved': True})
        self.assertEqual(done['status'], 'completed')
        with urllib.request.urlopen(base + '/api/diagram/pending') as r:
            self.assertEqual(json.load(r)['pending'], [])


if __name__ == '__main__':
    import unittest
    unittest.main()
