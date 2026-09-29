"""Record real device output from a containerlab lab into lab/<node>/, so sample mode replays real output.

    python -m bya.graph record containerlab/clab-bya/topology-data.json [--commands containerlab/record-commands.json]

Every command still passes the read-only command filter, runs with `docker exec` and the node's own CLI,
and is saved with passwords, secrets, keys and SNMP communities masked.
"""
import json
from pathlib import Path

from .devices import check_command, clab_nodes, docker_exec_output, mask_secrets, slug

DEFAULT_COMMANDS = {
    'nokia_srlinux': ['show interface ethernet-1/1', 'show interface ethernet-1/2', 'show interface ethernet-1/1 detail',
                      'show network-instance default protocols bgp neighbor', 'show network-instance default route-table'],
    'linux': ['show bgp summary', 'show interface eth1', 'show interface eth2', 'show ip route'],
    'arista_ceos': ['show interfaces', 'show ip bgp summary', 'show logging last 50'],
}


def record(topology, lab_dir, commands=None, runtime_dir='.', run=docker_exec_output):
    """Returns [(node, command, status)]; status is "saved" or the reason it was not."""
    commands = commands or DEFAULT_COMMANDS
    nodes = clab_nodes({'clab_topology': str(topology)}, type('Ctx', (), {'knowledge_dir': Path(runtime_dir) / 'knowledge'})())
    report = []
    for name, node in sorted(nodes.items()):
        folder = Path(lab_dir) / name
        index = folder / 'commands.json'
        recorded = json.loads(index.read_text()) if index.is_file() else []
        for raw in commands.get(node['kind'], []):
            try:
                cmd = check_command(raw, [raw])
                out = run(node['container'], node['kind'], cmd)
            except Exception as e:  # one failed command never stops the recording
                report.append((name, raw, f'not saved: {e}'))
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f'{slug(cmd)}.txt').write_text(mask_secrets(out))
            if cmd not in recorded:
                recorded.append(cmd)
            report.append((name, cmd, 'saved'))
        if recorded:
            index.write_text(json.dumps(recorded, indent=2) + '\n')
    return report
