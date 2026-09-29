"""Read-only device access: a command filter, device scope, and transports.

The model never holds a device session. It asks for one command on one device; the filter decides.
A command runs only when all of these hold:
  - the device is inside the block's scope (resolved from the asset register, NetBox or a list)
  - the command starts with a read verb (show, display, ping, traceroute), written in full
  - it matches one of the block's allow patterns
  - it has no command separators, redirects or output pipes other than read-only filters
  - it matches nothing in the always-deny list
Sample mode never contacts a device: it replays recorded outputs from lab/<device>/.
Live mode runs the command over the jump host's OpenSSH client (its keys, config and known_hosts), or, for a
containerlab lab, with `docker exec` into the node's container and its own CLI (sr_cli, vtysh, Cli).
"""
import fnmatch
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

from .. import core

READ_VERBS = ('show', 'display', 'ping', 'traceroute')
PIPE_FILTERS = {'include', 'exclude', 'begin', 'section', 'count', 'i', 'e', 'b', 's', 'inc', 'incl', 'exc', 'excl',
                'beg', 'sec', 'match', 'except', 'find', 'last', 'no-more', 'json', 'grep', 'egrep'}
ALWAYS_DENY_WORDS = ('configure', 'reload', 'reboot', 'write', 'copy', 'delete', 'erase', 'format', 'debug', 'undebug',
                     'clear', 'commit', 'rollback', 'shutdown', 'tclsh', 'bash', 'guestshell', 'redirect', 'tee',
                     'append', 'save')
ALWAYS_DENY = re.compile(r'\b(' + '|'.join(ALWAYS_DENY_WORDS) + r')\b', re.I)
SAFE_CHARS = re.compile(r'^[A-Za-z0-9 _./:,@\-|"\'()\[\]*+?^=]+$')
MAX_COMMAND, MAX_OUTPUT, MAX_DEVICES = 200, 20_000, 500
MAX_REPEAT = 10  # ping/traceroute repeat or count
SECRET_IN_OUTPUT = re.compile(
    r'(?i)\b(password|secret|community|key-string|pre-shared-key|authentication-key|md5|key)(\s+\d)?\s+\S+')


class CommandRefused(Exception):
    """The command filter refused a command; the agent is told why and may try another."""


def normalize(command):
    return re.sub(r'\s+', ' ', str(command)).strip()


def pattern_problems(pattern):
    """Why an allow pattern is unsafe (empty list = fine). Used by the validator."""
    p = normalize(pattern)
    first = p.split(' ')[0].lower() if p else ''
    if first not in READ_VERBS:
        return [f'Allow pattern {pattern!r} must start with one of: {", ".join(READ_VERBS)} (written in full).']
    problems = []
    if ALWAYS_DENY.search(p):
        problems.append(f'Allow pattern {pattern!r} contains a word that is always denied.')
    if re.search(r'[;&<>`$\\\n\r]', str(pattern)):
        problems.append(f'Allow pattern {pattern!r} contains a separator or redirect.')
    return problems


def check_command(command, allow):
    """Return the normalized command, or raise CommandRefused with the reason."""
    raw = str(command)
    if not raw.strip():
        raise CommandRefused('Empty command.')
    if len(raw) > MAX_COMMAND:
        raise CommandRefused(f'Command is longer than {MAX_COMMAND} characters.')
    if not SAFE_CHARS.match(raw):
        raise CommandRefused('Command contains characters that are not allowed (separators, redirects or control characters).')
    cmd = normalize(raw)
    words = cmd.split(' ')
    if words[0].lower() not in READ_VERBS:
        raise CommandRefused(f'Only read commands are allowed; start with one of: {", ".join(READ_VERBS)} (written in full).')
    head, *pipes = cmd.split('|')
    for pipe in pipes:
        verb = pipe.strip().split(' ')[0].lower()
        if verb not in PIPE_FILTERS:
            raise CommandRefused(f'Output pipe "| {verb}" is not allowed; use include, exclude, begin, section or count.')
    if ALWAYS_DENY.search(head):
        raise CommandRefused(f'"{ALWAYS_DENY.search(head).group(0)}" is never allowed.')
    if words[0].lower() in ('ping', 'traceroute'):
        for m in re.finditer(r'\b(repeat|count|size|datagram-size|rapid)\s+(\d+)', head, re.I):
            limit = MAX_REPEAT if m.group(1).lower() in ('repeat', 'count') else 1500
            if int(m.group(2)) > limit:
                raise CommandRefused(f'"{m.group(1)} {m.group(2)}" is over the limit of {limit}.')
    if not any(fnmatch.fnmatchcase(cmd.lower(), normalize(p).lower()) for p in allow):
        raise CommandRefused(f'Not in this agent\'s allowlist ({", ".join(allow)}).')
    return cmd


# --- scope ----------------------------------------------------------------------

def resolve_scope(cfg, ctx):
    """Return {device_name: address} for the block's scope."""
    source, filt = cfg.get('scope_source', 'ssot'), cfg.get('scope_filter') or {}
    if source == 'list':
        devices = {str(d): str(d) for d in cfg.get('devices', [])}
    elif source == 'ssot':
        devices = {}
        for a in ctx.ssot.get('assets', []):
            if a.get('sample') and ctx.mode == 'live':
                continue  # sample records never reach a live run
            if all(str(a.get(k, '')).lower() == str(v).lower() for k, v in filt.items()):
                devices[a['name']] = a.get('mgmt_address') or a['name']
    elif source == 'netbox':
        devices = _netbox_devices(cfg, filt)
    elif source == 'containerlab':
        devices = {name: node['container'] for name, node in clab_nodes(cfg, ctx).items()}
    else:
        raise ValueError(f'Unknown scope source "{source}".')
    if len(devices) > MAX_DEVICES:
        raise ValueError(f'The scope matches {len(devices)} devices; the limit is {MAX_DEVICES}. Narrow the filter.')
    return devices


# --- containerlab -------------------------------------------------------------------

CLAB_CLI = {  # node kind -> how to run one read-only command inside its container (no shell involved)
    'nokia_srlinux': lambda c, cmd: ['docker', 'exec', c, 'sr_cli', cmd],
    'srl': lambda c, cmd: ['docker', 'exec', c, 'sr_cli', cmd],
    'arista_ceos': lambda c, cmd: ['docker', 'exec', c, 'Cli', '-c', cmd],
    'ceos': lambda c, cmd: ['docker', 'exec', c, 'Cli', '-c', cmd],
    'linux': lambda c, cmd: ['docker', 'exec', c, 'vtysh', '-c', cmd],   # FRR images
}


def runtime_root(ctx):
    return Path(ctx.knowledge_dir).parent


def clab_nodes(cfg, ctx):
    """Nodes of a deployed containerlab lab, from the topology-data.json containerlab writes next to the lab.
    Returns {short name: {"container", "kind", "mgmt"}}; an optional scope_filter {"kind": ...} narrows it."""
    path = Path(str(cfg.get('clab_topology', '')))
    path = path if path.is_absolute() else runtime_root(ctx) / path
    if not path.is_file():
        raise ValueError(f'No containerlab topology data at "{cfg.get("clab_topology")}". Deploy the lab first '
                         f'(it writes clab-<lab>/topology-data.json).')
    doc = json.loads(path.read_text())
    want = {k: str(v).lower() for k, v in (cfg.get('scope_filter') or {}).items()}
    nodes = {}
    for key, n in (doc.get('nodes') or {}).items():
        name = n.get('shortname') or key
        kind = str(n.get('kind', '')).lower()
        if want.get('kind') and kind != want['kind']:
            continue
        nodes[name] = {'container': n.get('longname') or f'clab-{doc.get("name", "lab")}-{name}', 'kind': kind,
                       'mgmt': n.get('mgmt-ipv4-address') or n.get('mgmt-ipv4') or ''}
    return nodes


def docker_exec_output(container, kind, command, timeout_s=20):
    build = CLAB_CLI.get(kind)
    if build is None:
        raise ValueError(f'BYA has no read-only command line for containerlab kind "{kind}"; use SSH for it.')
    try:
        done = subprocess.run(build(container, command), capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        raise ValueError('The docker command is not installed on this host.') from None
    except subprocess.TimeoutExpired:
        raise ValueError(f'No answer from {container} within {timeout_s} s.') from None
    if done.returncode != 0 and not done.stdout:
        raise ValueError(f'docker exec {container} failed: {done.stderr.strip()[:300] or "exit " + str(done.returncode)}')
    return done.stdout


def _netbox_devices(cfg, filt):
    base = os.environ.get(cfg.get('netbox_url_env', 'NETBOX_URL'), '').rstrip('/')
    token = os.environ.get(cfg.get('netbox_token_env', 'NETBOX_TOKEN'), '')
    if not base or not token:
        raise ValueError('Set the NetBox URL and token environment variables named on the device block.')
    query = urllib.parse.urlencode({**{k: str(v) for k, v in filt.items()}, 'limit': MAX_DEVICES + 1})
    doc = core.http(f'{base}/api/dcim/devices/?{query}', headers={'Authorization': f'Token {token}', 'Accept': 'application/json'})
    out = {}
    for d in doc.get('results', []):
        ip = ((d.get('primary_ip') or {}).get('address') or '').split('/')[0]
        if d.get('name'):
            out[d['name']] = ip or d['name']
    return out


# --- transports -------------------------------------------------------------------

def slug(command):
    return re.sub(r'[^a-z0-9]+', '_', normalize(command).lower()).strip('_')[:120]


IFACE_ABBREV = [(r'\bgi(?:g|gabitethernet)?(?=\d)', 'gigabitethernet'), (r'\bte(?:n|ngigabitethernet)?(?=\d)', 'tengigabitethernet'),
                (r'\bfa(?:stethernet)?(?=\d)', 'fastethernet'), (r'\beth?(?:ernet)?(?=\d)', 'ethernet'),
                (r'\bpo(?:rt-channel)?(?=\d)', 'port-channel'), (r'\blo(?:opback)?(?=\d)', 'loopback')]


def _canonical(command):
    c = normalize(command).lower()
    for pattern, full in IFACE_ABBREV:
        c = re.sub(pattern, full, c)
    return c


def sample_output(lab_dir, device, command):
    """Replay a recording. Interface abbreviations match (Gi0/1 = GigabitEthernet0/1); a command that extends a
    recorded one gets that recording, labelled as the closest match."""
    folder = Path(lab_dir) / device
    direct = folder / f'{slug(command)}.txt'
    if direct.is_file():  # recorded under exactly this command
        return direct.read_text(errors='replace')
    index = folder / 'commands.json'
    recorded = json.loads(index.read_text()) if index.is_file() else []
    wanted = _canonical(command)
    exact = [r for r in recorded if _canonical(r) == wanted]
    prefix = sorted((r for r in recorded if wanted.startswith(_canonical(r) + ' ')), key=len, reverse=True)
    for match, note in ((exact, ''), (prefix, 'Sample data: no recording for this exact command; closest recording is "{}".\n')):
        if match and (folder / f'{slug(match[0])}.txt').is_file():
            return note.format(match[0]) + (folder / f'{slug(match[0])}.txt').read_text(errors='replace')
    hint = f' Recorded commands: {"; ".join(recorded)}.' if recorded else ' No recordings for this device.'
    return f'No recorded output for "{command}" on {device} in sample data.{hint}'


def openssh_output(address, command, username_env='', timeout_s=20):
    argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={min(timeout_s, 15)}', '-o', 'ServerAliveInterval=5']
    user = os.environ.get(username_env, '') if username_env else ''
    if user:
        argv += ['-l', user]
    argv += ['--', address, command]   # no shell on this side; the filter already vetted the command
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        raise ValueError('The OpenSSH client ("ssh") is not installed on this host.') from None
    except subprocess.TimeoutExpired:
        raise ValueError(f'No answer from {address} within {timeout_s} s.') from None
    if done.returncode != 0 and not done.stdout:
        raise ValueError(f'SSH to {address} failed: {done.stderr.strip()[:300] or "exit " + str(done.returncode)}')
    return done.stdout


# --- the tool -----------------------------------------------------------------------

class DeviceReader:
    """One device block's command tool. Keeps per-run call counts and per-device pacing."""

    def __init__(self, cfg, ctx):
        self.cfg, self.ctx = cfg, ctx
        self.allow = [normalize(p) for p in cfg.get('allow', [])]
        self.devices = resolve_scope(cfg, ctx)
        self.kinds = ({n: node['kind'] for n, node in clab_nodes(cfg, ctx).items()}
                      if cfg.get('scope_source') == 'containerlab' else {})
        self.calls, self.last = 0, {}

    def __call__(self, args):
        device, command = str(args.get('device', '')).strip(), args.get('command', '')
        if device not in self.devices:
            raise CommandRefused(f'"{device}" is outside this agent\'s device scope.')
        cmd = check_command(command, self.allow)
        self.calls += 1
        if self.calls > int(self.cfg.get('max_commands', 20)):
            raise CommandRefused(f'This run has used its {self.cfg.get("max_commands", 20)} device commands.')
        wait = float(self.cfg.get('min_interval_s', 2)) - (time.monotonic() - self.last.get(device, -1e9))
        if wait > 0 and self.ctx.mode == 'live':
            time.sleep(wait)
        self.last[device] = time.monotonic()
        if self.ctx.mode == 'sample':
            out = sample_output(lab_dir(self.ctx), device, cmd)
        elif self.kinds:
            out = docker_exec_output(self.devices[device], self.kinds[device], cmd, int(self.cfg.get('timeout_s', 20)))
        else:
            out = openssh_output(self.devices[device], cmd, self.cfg.get('username_env', ''), int(self.cfg.get('timeout_s', 20)))
        return f'{device}# {cmd}\n{parsed_summary(cmd, out)}{mask_secrets(out)[:MAX_OUTPUT]}'

    def description(self):
        names = sorted(self.devices)
        shown = ', '.join(names[:20]) + (f' and {len(names) - 20} more' if len(names) > 20 else '')
        return (f'Run one read-only command on one network device and return its output. '
                f'Allowed commands: {", ".join(self.allow)}. Write commands in full (no abbreviations). '
                f'Devices: {shown or "none"}.')


def _num(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def parsed_summary(command, output):
    """A plain-language line for outputs whose numbers models misread (IOS/EOS style "show interfaces")."""
    if not _canonical(command).startswith('show interfaces') or 'input rate' not in output:
        return ''
    bw_kbit = _num(r'BW (\d+) Kbit', output)
    parts = []
    for label in ('input', 'output'):
        bps = _num(label + r' rate (\d+) bits/sec', output)
        if bps is not None:
            util = f' ({bps / (bw_kbit * 1000):.0%} of {bw_kbit / 1000:g} Mb/s)' if bw_kbit else ''
            parts.append(f'5-minute {label} rate {bps / 1e6:,.1f} Mb/s{util}')
    for label, pattern in (('input errors', r'(\d+) input errors'), ('CRC errors', r'(\d+) CRC'),
                           ('output drops', r'Total output drops: (\d+)'), ('interface resets', r'(\d+) interface resets')):
        n = _num(pattern, output)
        if n is not None:
            parts.append(f'{n:,} {label}')
    return ('Parsed by BYA: ' + '; '.join(parts) + '.\n') if parts else ''


def mask_secrets(text):
    """Device output reaches the model only with passwords, secrets, keys and SNMP communities masked."""
    return SECRET_IN_OUTPUT.sub(lambda m: f'{m.group(1)} ****', text)


def lab_dir(ctx):
    return Path(getattr(ctx, 'lab_dir', None) or Path(ctx.knowledge_dir).parent / 'lab')


def describe_scope(cfg, ctx):
    """For the reach view: devices (or the reason they can't be resolved) and command rules."""
    try:
        devices, error = sorted(resolve_scope(cfg, ctx)), None
    except ValueError as e:
        devices, error = [], str(e)
    if cfg.get('scope_source') == 'containerlab':
        cfg = {**cfg, 'scope_filter': {'lab': cfg.get('clab_topology', ''), **(cfg.get('scope_filter') or {})}}
    return {'source': cfg.get('scope_source', 'ssot'), 'filter': cfg.get('scope_filter') or {},
            'devices': devices, 'error': error, 'allow': cfg.get('allow', []),
            'always_denied': list(ALWAYS_DENY_WORDS),
            'max_commands': cfg.get('max_commands', 20)}
