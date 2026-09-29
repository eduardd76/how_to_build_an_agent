"""containerlab: lab nodes as a device source, docker exec per node kind, and recording real output."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bya.graph import Context, builder, load, validate  # noqa: E402
from bya.graph import devices  # noqa: E402
from bya.graph.devices import CommandRefused, DeviceReader  # noqa: E402
from bya.graph.reach import reach  # noqa: E402
from bya.graph.record import record  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'clab-topology-data.json'
TOPOLOGY = 'tests/fixtures/clab-topology-data.json'   # relative to the runtime folder, as a diagram would give it
ALLOW = ['show interface *', 'show network-instance * protocols bgp *', 'show bgp *']


def ctx(**kw):
    return Context(knowledge_dir=ROOT / 'knowledge', **kw)


def lab_cfg(**changes):
    cfg = {'scope_source': 'containerlab', 'clab_topology': TOPOLOGY, 'allow': ALLOW, 'min_interval_s': 0, 'access': 'read'}
    cfg.update(changes)
    return cfg


class ScopeTests(unittest.TestCase):
    def test_nodes_come_from_topology_data(self):
        nodes = devices.clab_nodes(lab_cfg(), ctx())
        self.assertEqual(nodes['srl1'], {'container': 'clab-bya-srl1', 'kind': 'nokia_srlinux', 'mgmt': '172.20.20.2'})
        self.assertEqual(sorted(nodes), ['frr1', 'srl1', 'srl2'])
        self.assertEqual(sorted(devices.clab_nodes(lab_cfg(scope_filter={'kind': 'linux'}), ctx())), ['frr1'])
        self.assertEqual(devices.resolve_scope(lab_cfg(clab_topology=str(FIXTURE)), ctx())['frr1'], 'clab-bya-frr1')  # absolute path

    def test_missing_lab_is_explained(self):
        with self.assertRaisesRegex(ValueError, 'Deploy the lab first'):
            devices.clab_nodes(lab_cfg(clab_topology='containerlab/clab-nope/topology-data.json'), ctx())


class TransportTests(unittest.TestCase):
    def run_live(self, device, command):
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, stdout='state up\n  password secret123\n', stderr='')
        with mock.patch.object(subprocess, 'run', fake_run):
            out = DeviceReader(lab_cfg(), ctx(mode='live'))({'device': device, 'command': command})
        return out, seen

    def test_docker_exec_uses_each_nodes_own_cli_without_a_shell(self):
        out, seen = self.run_live('srl1', 'show interface ethernet-1/1')
        self.assertEqual(seen[0][0], ['docker', 'exec', 'clab-bya-srl1', 'sr_cli', 'show interface ethernet-1/1'])
        self.assertNotIn('shell', seen[0][1])
        self.assertIn('password ****', out)
        self.assertNotIn('secret123', out)
        _, seen = self.run_live('frr1', 'show bgp summary')
        self.assertEqual(seen[0][0], ['docker', 'exec', 'clab-bya-frr1', 'vtysh', '-c', 'show bgp summary'])

    def test_filter_runs_before_docker(self):
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('docker called')):
            for cmd in ('enter candidate', 'show interface e1 ; reboot', 'show version'):
                with self.assertRaises(CommandRefused, msg=cmd):
                    DeviceReader(lab_cfg(), ctx(mode='live'))({'device': 'srl1', 'command': cmd})
            with self.assertRaisesRegex(CommandRefused, 'outside'):
                DeviceReader(lab_cfg(), ctx(mode='live'))({'device': 'clab-bya-srl1', 'command': 'show interface e1'})

    def test_unknown_kind_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'no read-only command line'):
            devices.docker_exec_output('clab-x-r1', 'juniper_vjunos', 'show version')

    def test_sample_mode_replays_recordings_and_never_runs_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'srl1').mkdir()
            (Path(tmp) / 'srl1' / 'show_interface_ethernet_1_1.txt').write_text('ethernet-1/1 is up, speed 25G\n')
            with mock.patch.object(subprocess, 'run', side_effect=AssertionError('docker in sample mode')):
                out = DeviceReader(lab_cfg(), ctx(lab_dir=Path(tmp)))({'device': 'srl1', 'command': 'show interface ethernet-1/1'})
        self.assertIn('ethernet-1/1 is up', out)


class RecordTests(unittest.TestCase):
    def test_record_saves_masked_output_and_an_index(self):
        calls = []

        def fake(container, kind, cmd):
            calls.append((container, kind, cmd))
            if 'route-table' in cmd:
                raise ValueError('docker exec clab-bya-srl1 failed: timeout')
            return f'{cmd} output\nsnmp community public ro\n'
        with tempfile.TemporaryDirectory() as tmp:
            commands = {'nokia_srlinux': ['show interface ethernet-1/1', 'show network-instance default route-table', 'configure terminal'],
                        'linux': ['show bgp summary']}
            report = record(TOPOLOGY, tmp, commands, ROOT, run=fake)
            saved = sorted((n, c) for n, c, s in report if s == 'saved')
            self.assertEqual(saved, [('frr1', 'show bgp summary'), ('srl1', 'show interface ethernet-1/1'), ('srl2', 'show interface ethernet-1/1')])
            reasons = {c: s for n, c, s in report if n == 'srl1' and s != 'saved'}
            self.assertIn('timeout', reasons['show network-instance default route-table'])
            self.assertIn('Only read commands', reasons['configure terminal'])   # the filter still applies
            self.assertNotIn(('clab-bya-srl1', 'nokia_srlinux', 'configure terminal'), calls)
            text = (Path(tmp) / 'srl1' / 'show_interface_ethernet_1_1.txt').read_text()
            self.assertIn('community ****', text)
            self.assertNotIn('public', text)
            self.assertEqual(json.loads((Path(tmp) / 'frr1' / 'commands.json').read_text()), ['show bgp summary'])


class PlainHttpOptInTests(unittest.TestCase):
    def test_plain_http_only_to_listed_hosts(self):
        from bya import core
        with mock.patch.dict('os.environ', {'BYA_ALLOW_HTTP_HOSTS': ''}):
            with self.assertRaisesRegex(ValueError, 'BYA_ALLOW_HTTP_HOSTS'):
                core.http('http://host.orb.internal:11434/v1/models')
        with mock.patch.dict('os.environ', {'BYA_ALLOW_HTTP_HOSTS': 'host.orb.internal, 10.0.0.5'}), \
                mock.patch('urllib.request.OpenerDirector.open', side_effect=RuntimeError('reached the network')):
            with self.assertRaisesRegex(RuntimeError, 'reached the network'):
                core.http('http://host.orb.internal:11434/v1/models')
            with self.assertRaisesRegex(ValueError, 'require HTTPS'):
                core.http('http://other.example:11434/v1/models')


class DiagramTests(unittest.TestCase):
    def test_validator_builder_and_reach(self):
        doc = builder.build({'shape': 'ask', 'job': 'Tell me the state of every BGP session in my lab.', 'runbooks': False,
                             'devices': {'source': 'containerlab', 'clab_topology': TOPOLOGY, 'filter': {}, 'commands': ['bgp', 'interfaces']}})
        self.assertEqual(validate(load(doc), 'sample'), [])
        dev = next(b for b in doc['blocks'] if b['type'] == 'tool.device')['config']
        self.assertIn('show network-instance * protocols bgp *', dev['allow'])
        r = reach(load(doc), ctx())
        self.assertEqual(r['agents'][0]['devices'][0]['devices'], ['frr1', 'srl1', 'srl2'])
        bad = copy.deepcopy(doc)
        next(b for b in bad['blocks'] if b['type'] == 'tool.device')['config']['clab_topology'] = ''
        self.assertIn('clab_topology', ' '.join(v.message for v in validate(load(bad), 'sample')))


if __name__ == '__main__':
    unittest.main()
