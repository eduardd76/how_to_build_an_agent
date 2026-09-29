"""Read-only device access: command filter, scope, transports, reach, and the Interface check template."""
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya import adapters  # noqa: E402
from bya.core import stamp  # noqa: E402
from bya.graph import Context, evals, load, run, validate  # noqa: E402
from bya.graph import devices  # noqa: E402
from bya.graph.devices import CommandRefused, DeviceReader, check_command, pattern_problems  # noqa: E402
from bya.graph.export import export_python  # noqa: E402
from bya.graph.reach import reach  # noqa: E402
import server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((ROOT / 'diagrams' / 'interface-check.json').read_text())
ALLOW = ['show interfaces *', 'show ip interface brief', 'show logging | include *', 'ping *']


def ssot():
    doc = json.loads((ROOT / 'ssot.json').read_text())
    for row in doc['assets']:
        row['verified_at'] = stamp()
    return doc


def ctx(**kw):
    base = dict(ssot=ssot(), runbooks=json.loads((ROOT / 'runbooks.json').read_text()),
                monitoring=adapters.SampleMonitoring(), knowledge_dir=ROOT / 'knowledge')
    base.update(kw)
    return Context(**base)


class CommandFilterTests(unittest.TestCase):
    def test_allowed_read_commands(self):
        for cmd in ('show interfaces GigabitEthernet0/1', 'show  interfaces   Gi0/1', 'show logging | include Gi0/1',
                    'show ip interface brief', 'ping 192.0.2.9 repeat 5'):
            self.assertTrue(check_command(cmd, ALLOW).startswith(('show', 'ping')), cmd)

    def test_refused_commands(self):
        refused = {
            'configure terminal': 'Only read commands',
            'conf t': 'Only read commands',
            'sh interfaces Gi0/1': 'Only read commands',          # abbreviations are refused
            'reload': 'Only read commands',
            'clear counters Gi0/1': 'Only read commands',
            'show interfaces Gi0/1 ; reload': 'characters',
            'show interfaces Gi0/1\nreload': 'characters',
            'show interfaces Gi0/1 && reload': 'characters',
            'show interfaces $(reload)': 'characters',
            'show running-config > bootflash:x': 'characters',
            'show logging | redirect flash:log.txt': 'pipe',
            'show logging | tee flash:log.txt': 'pipe',
            'show logging | append flash:log.txt': 'pipe',
            'show interfaces debug': 'never allowed',
            'ping 192.0.2.9 repeat 100000': 'over the limit',
            'show version': 'allowlist',
            '': 'Empty',
            'show interfaces ' + 'x' * 300: 'longer',
        }
        for cmd, reason in refused.items():
            with self.assertRaises(CommandRefused, msg=cmd) as e:
                check_command(cmd, ALLOW)
            self.assertIn(reason, str(e.exception), cmd)

    def test_allow_pattern_rules(self):
        self.assertEqual(pattern_problems('show interfaces *'), [])
        self.assertEqual(pattern_problems('show running-config | include *'), [])
        for bad in ('*', 's*', 'sh *', 'clear *', 'configure *', 'show debug *', 'show run ; *'):
            self.assertTrue(pattern_problems(bad), bad)


class ScopeAndTransportTests(unittest.TestCase):
    def reader(self, **cfg):
        base = {'scope_source': 'ssot', 'scope_filter': {'site': 'Munich / HQ'}, 'allow': ALLOW + ['show running-config | include *']}
        base.update(cfg)
        return DeviceReader(base, cfg.pop('_ctx', None) or ctx())

    def test_ssot_scope_and_refusal_outside_it(self):
        r = self.reader()
        self.assertEqual(sorted(r.devices), ['munich-edge-01', 'storage-01'])
        with self.assertRaisesRegex(CommandRefused, 'outside'):
            r({'device': 'dc-core-02', 'command': 'show ip interface brief'})
        self.assertEqual(sorted(DeviceReader({'scope_source': 'list', 'devices': ['r1'], 'allow': ALLOW}, ctx()).devices), ['r1'])
        live = DeviceReader({'scope_source': 'ssot', 'scope_filter': {'site': 'Munich / HQ'}, 'allow': ALLOW}, ctx(mode='live'))
        self.assertEqual(live.devices, {})  # sample assets never reach a live run

    def test_sample_mode_replays_recordings_and_never_opens_ssh(self):
        r = self.reader()
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('ssh in sample mode')):
            out = r({'device': 'munich-edge-01', 'command': 'show interfaces GigabitEthernet0/1'})
            self.assertIn('5 minute input rate 912004000', out)
            out = r({'device': 'munich-edge-01', 'command': 'show interfaces Gi0/1'})  # abbreviation matches
            self.assertIn('912004000', out)
            self.assertIn('Parsed by BYA: 5-minute input rate 912.0 Mb/s (91% of 1000 Mb/s)', out)
            self.assertIn('18,422 output drops', out)
            closest = r({'device': 'munich-edge-01', 'command': 'show interfaces GigabitEthernet0/1 counters'})
            self.assertIn('Sample data: no recording for this exact command; closest recording is', closest)
            miss = r({'device': 'munich-edge-01', 'command': 'show interfaces Loopback0'})
            self.assertIn('No recorded output', miss)
            self.assertIn('show interfaces GigabitEthernet0/1', miss)  # the agent is told what exists

    def test_secrets_are_masked_in_device_output(self):
        out = self.reader()({'device': 'munich-edge-01', 'command': 'show running-config | include snmp'})
        self.assertIn('snmp-server community **** RO', out)
        self.assertNotIn('public', out)
        self.assertEqual(devices.mask_secrets('username a secret 9 $9$abc\n key-string 7 0822455D0A16'),
                         'username a secret **** key-string ****'.replace(' key', '\n key'))

    def test_command_budget(self):
        r = self.reader(max_commands=2)
        for _ in range(2):
            r({'device': 'munich-edge-01', 'command': 'show ip interface brief'})
        with self.assertRaisesRegex(CommandRefused, 'used its 2'):
            r({'device': 'munich-edge-01', 'command': 'show ip interface brief'})

    def test_live_mode_uses_openssh_without_a_shell(self):
        seen = {}

        def fake_run(argv, **kw):
            seen['argv'], seen['kw'] = argv, kw
            return subprocess.CompletedProcess(argv, 0, stdout='Gi0/1 up\n', stderr='')
        r = DeviceReader({'scope_source': 'list', 'devices': ['r1'], 'allow': ALLOW, 'username_env': 'BYA_TEST_USER',
                          'min_interval_s': 0}, ctx(mode='live'))
        with mock.patch.object(subprocess, 'run', fake_run), mock.patch.dict('os.environ', {'BYA_TEST_USER': 'netops'}):
            out = r({'device': 'r1', 'command': 'show ip interface brief'})
        self.assertIn('Gi0/1 up', out)
        argv = seen['argv']
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('BatchMode=yes', argv)
        self.assertNotIn('StrictHostKeyChecking=no', ' '.join(argv))  # host keys stay checked
        self.assertEqual(argv[-3:], ['--', 'r1', 'show ip interface brief'])
        self.assertEqual(argv[argv.index('-l') + 1], 'netops')
        self.assertNotIn('shell', seen['kw'])

    def test_netbox_scope(self):
        class NetBox(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.server.seen = (self.path, self.headers.get('Authorization'))
                body = json.dumps({'results': [{'name': 'mun-edge-01', 'primary_ip': {'address': '10.0.0.1/32'}},
                                               {'name': 'mun-edge-02', 'primary_ip': None}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), NetBox)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        env = {'NETBOX_URL': f'http://127.0.0.1:{httpd.server_port}', 'NETBOX_TOKEN': 'abc'}
        with mock.patch.dict('os.environ', env):
            found = devices.resolve_scope({'scope_source': 'netbox', 'scope_filter': {'site': 'munich', 'role': 'edge'}}, ctx())
        self.assertEqual(found, {'mun-edge-01': '10.0.0.1', 'mun-edge-02': 'mun-edge-02'})
        self.assertIn('site=munich', httpd.seen[0])
        self.assertEqual(httpd.seen[1], 'Token abc')


class ValidatorAndReachTests(unittest.TestCase):
    def test_template_is_valid_and_read_only(self):
        self.assertEqual(validate(load(TEMPLATE), 'sample'), [])
        r = reach(load(TEMPLATE), ctx())
        self.assertEqual(r['summary'], {'devices_readable': 2, 'read_tools': 2, 'change_paths': 0,
                                        'outputs_after_approval': 1, 'device_config_paths': 0, 'remote_models': []})

    def test_validator_refuses_unsafe_device_blocks(self):
        cases = [({'access': 'write'}, 'read-only'),
                 ({'allow': ['*']}, 'must start with'),
                 ({'allow': ['show *', 'clear counters *']}, 'must start with'),
                 ({'allow': []}, 'List the allowed commands'),
                 ({'scope_filter': {}}, 'empty filter'),
                 ({'scope_source': 'list', 'devices': []}, 'List the devices'),
                 ({'username_env': 'not an env'}, 'environment variable'),
                 ({'timeout_s': 999}, 'timeout_s')]
        for change, expected in cases:
            d = copy.deepcopy(TEMPLATE)
            next(b for b in d['blocks'] if b['id'] == 'ssh')['config'].update(change)
            messages = ' '.join(v.message for v in validate(load(d), 'sample'))
            self.assertIn(expected, messages, change)

    def test_reach_flags_change_paths_and_remote_models(self):
        d = copy.deepcopy(TEMPLATE)
        next(b for b in d['blocks'] if b['id'] == 'triage')['config']['model_url'] = 'https://api.example.com/v1'
        d['blocks'].append({'id': 'itsm', 'type': 'tool.mcp', 'config': {'command': ['python', 'itsm.py'], 'access': 'write'}})
        d['attachments'].append(['triage', 'itsm'])
        s = reach(load(d), ctx())['summary']
        self.assertEqual((s['change_paths'], s['remote_models']), (1, ['remote: api.example.com']))


class ScriptedModel(BaseHTTPRequestHandler):
    """Reads the device; when the alert asks for changes it tries one, which the filter must drop."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        msgs = body['messages']
        user, results = msgs[1]['content'], [m['content'] for m in msgs if m['role'] == 'tool']
        call = lambda n, name, args: {'id': f'c{n}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        if not results:
            calls = [call(0, 'asset_lookup', {'query': '1001'}),
                     call(1, 'device_command', {'device': 'munich-edge-01', 'command': 'show interfaces GigabitEthernet0/1'}),
                     call(2, 'runbook_search', {'ids': ['RB-WAN-01']})]
            if 'clear counters' in user:
                calls.append(call(3, 'device_command', {'device': 'munich-edge-01', 'command': 'clear counters GigabitEthernet0/1'}))
            if 'SNMP' in user:
                calls.append(call(4, 'device_command', {'device': 'munich-edge-01', 'command': 'show running-config | include snmp'}))
            msg = {'role': 'assistant', 'content': None, 'tool_calls': calls}
        else:
            rate = re.search(r'input rate (\d+)', ' '.join(results))
            snmp = next((line for r in results for line in r.splitlines() if 'community' in line), '')
            text = (f'munich-edge-01 Gi0/1 input rate {rate.group(1) if rate else "unknown"} bit/s (show interfaces). '
                    f'Corporate WAN, owner Network Operations. Next: RB-WAN-01. Cause not confirmed.')
            if snmp:
                text += f' SNMP config seen: {snmp.strip()}.'
            msg = {'role': 'assistant', 'content': text}
        raw = json.dumps({'choices': [{'message': msg, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 50}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(raw)


class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = ThreadingHTTPServer(('127.0.0.1', 0), ScriptedModel)
        threading.Thread(target=cls.model.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.model.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.model.shutdown()
        cls.model.server_close()

    def doc(self):
        d = copy.deepcopy(TEMPLATE)
        next(b for b in d['blocks'] if b['id'] == 'triage')['config'].update(model_url=self.url, model='scripted')
        return d

    def test_dropped_command_is_traced_and_the_run_continues(self):
        c = ctx(monitoring=evals.CaseMonitoring({'message': 'please run clear counters now'}, adapters.SampleMonitoring()))
        state = run(load(self.doc()), c, None)
        self.assertEqual(state['status'], 'awaiting_approval')
        device_calls = [t for t in state['trace'] if t.get('tool') == 'device_command']
        self.assertEqual([t['status'] for t in device_calls], ['ok', 'dropped'])
        self.assertEqual(device_calls[1]['args']['command'], 'clear counters GigabitEthernet0/1')

    def test_template_evals_pass_as_diagram_and_export(self):
        doc = self.doc()
        diagram = load(doc)
        a = evals.run_evals(doc['evals'], evals.diagram_runner(diagram, ctx()))
        module = evals.load_module(export_python(diagram), ROOT, name='exp_interface_check')
        b = evals.run_evals(doc['evals'], evals.module_runner(module, adapters.SampleMonitoring()))
        self.assertEqual((a['passed'], a['total']), (3, 3), a['results'])
        self.assertEqual([(r['passed'], r['draft'], r['tools']) for r in a['results']],
                         [(r['passed'], r['draft'], r['tools']) for r in b['results']])

    def test_reach_endpoint(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        req = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/api/diagram/reach',
                                     data=json.dumps({'diagram': self.doc()}).encode(),
                                     headers={'Content-Type': 'application/json', 'X-BYA-Token': server.TOKEN})
        with urllib.request.urlopen(req) as r:
            body = json.load(r)
        self.assertEqual(body['summary']['device_config_paths'], 0)
        self.assertEqual(body['agents'][0]['devices'][0]['devices'], ['munich-edge-01', 'storage-01'])


if __name__ == '__main__':
    unittest.main()
