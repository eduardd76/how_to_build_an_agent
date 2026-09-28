"""Checks a diagram before it runs. Every violation names the block to highlight on the canvas.

Safety rules:
  one-trigger, no-cycles       exactly one trigger; loops only inside an agent
  type, one-input              wires connect compatible flow types; one incoming wire per block
  output-needs-approval        every output sits behind an output check and a human approval
  write-needs-approval         write tools can never opt out of per-call approval
  agent-limits                 every agent has a step limit, token budget and timeout
  no-secrets                   credentials are referenced by environment-variable name only
  no-sample-live               sample data sources cannot run in live mode
plus structural rules (known-type, reference, attachment, flow-wire, unreachable, unattached, config).
"""
import re
from dataclasses import asdict, dataclass

from .catalog import AGENT_LIMITS, BUILTIN_FUNCTIONS, SPECS

ENV_NAME = re.compile(r'^[A-Z_][A-Z0-9_]*$')
TOOL_NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
SECRET_KEY = re.compile(r'token|secret|password|passwd|api_?key|authorization|credential', re.I)
SECRET_VALUE = re.compile(r'^(Bearer\s|Basic\s|xox[abp]-|sk-|ghp_|github_pat_|glpat-)', re.I)


@dataclass
class Violation:
    block: str   # block id to highlight, or None for diagram-level problems
    rule: str
    message: str

    def to_dict(self):
        return asdict(self)


def validate(diagram, mode='sample'):
    out = []

    def add(block, rule, message):
        out.append(Violation(block, rule, message))

    blocks = diagram.blocks
    for b in blocks.values():
        if b.spec is None:
            add(b.id, 'known-type', f'Unknown block type "{b.type}".')
    known = {k: b for k, b in blocks.items() if b.spec}

    # References and the two kinds of connection.
    for kind, pairs in (('attachment', diagram.attachments), ('flow', diagram.flow)):
        for a, b in pairs:
            for end in (a, b):
                if end not in blocks:
                    add(None, 'reference', f'{kind.capitalize()} refers to missing block "{end}".')
    for a, b in diagram.attachments:
        if a in known and known[a].type != 'agent':
            add(a, 'attachment', 'Only an agent can have attachments.')
        if b in known and not known[b].spec.attachable:
            add(b, 'attachment', f'"{known[b].type}" connects by flow wire, not attachment.')
    for a, b in diagram.flow:
        for end in (a, b):
            if end in known and known[end].spec.attachable:
                add(end, 'flow-wire', f'"{known[end].type}" must be attached to an agent, not wired into the flow.')
    flow = [(a, b) for a, b in diagram.flow if a in known and b in known
            and not known[a].spec.attachable and not known[b].spec.attachable]

    # One trigger, no cycles, compatible types, one input per block.
    triggers = [b for b in known.values() if b.spec.category == 'trigger']
    if len(triggers) != 1:
        add(triggers[1].id if len(triggers) > 1 else None, 'one-trigger',
            f'A diagram needs exactly one trigger; found {len(triggers)}.')
    for a, b in flow:
        produced, accepted = known[a].spec.output, known[b].spec.inputs
        if produced not in accepted:
            add(b, 'type', f'"{known[b].type}" cannot take "{produced or "nothing"}" from "{a}"; '
                           f'it accepts {", ".join(sorted(accepted)) or "no input"}.')
    incoming = {k: [a for a, b in flow if b == k] for k in known}
    for k, b in known.items():
        if b.spec.attachable:
            continue
        n = len(incoming[k])
        if b.spec.category == 'trigger' and n:
            add(k, 'one-input', 'A trigger cannot have incoming wires.')
        elif b.spec.category != 'trigger' and n != 1:
            add(k, 'one-input', f'Needs exactly one incoming wire; has {n}.')
    if _has_cycle(known, flow):
        add(None, 'no-cycles', 'Flow wires form a loop. Loops belong inside an agent (its step limit bounds them).')

    # Everything reachable; every resource attached.
    if len(triggers) == 1:
        reach, todo = set(), [triggers[0].id]
        while todo:
            k = todo.pop()
            if k not in reach:
                reach.add(k)
                todo.extend(b for a, b in flow if a == k)
        for k, b in known.items():
            if not b.spec.attachable and k not in reach:
                add(k, 'unreachable', 'Not connected to the trigger; it would never run.')
    attached = {b for a, b in diagram.attachments}
    for k, b in known.items():
        if b.spec.attachable and k not in attached:
            add(k, 'unattached', 'Attach this to an agent, or remove it.')

    # Outputs behind a check and an approval (explicit, even though flow types already imply it).
    for k, b in known.items():
        if b.spec.category != 'output':
            continue
        chain, cur, seen = [], k, set()
        while incoming.get(cur) and len(incoming[cur]) == 1 and cur not in seen:
            seen.add(cur)
            cur = incoming[cur][0]
            chain.append(known[cur].type)
        order = list(reversed(chain))
        if not ('guard.output_check' in order and 'guard.approval' in order
                and order.index('guard.output_check') < order.index('guard.approval')):
            add(k, 'output-needs-approval', 'An output must come after an output check, then a human approval.')

    for k, b in known.items():
        cfg = b.config
        if b.type == 'agent':
            for key, (lo, hi) in AGENT_LIMITS.items():
                v = cfg.get(key)
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    add(k, 'agent-limits', f'"{key}" must be a whole number between {lo} and {hi}.')
            if not isinstance(cfg.get('instructions'), str) or not cfg['instructions'].strip():
                add(k, 'config', 'An agent needs instructions.')
        if b.spec.category == 'tool':
            if cfg.get('access') not in ('read', 'write'):
                add(k, 'config', 'Set "access" to "read" or "write".')
            writes = cfg.get('access') == 'write' or bool(cfg.get('write_tools'))
            if writes and cfg.get('require_approval', True) is not True:
                add(k, 'write-needs-approval', 'Write tools always need approval; "require_approval" cannot be turned off.')
        _check_config(b, add, mode)
        for path, value in _secrets(cfg):
            add(k, 'no-secrets', f'"{path}" looks like a credential. Reference an environment variable name instead '
                                 f'(for example "{path.split(".")[-1]}_env": "MY_VAR").')
    return out


def _has_cycle(known, flow):
    graph = {k: [b for a, b in flow if a == k] for k in known}
    state = {}

    def visit(k):
        state[k] = 1
        for n in graph[k]:
            if state.get(n) == 1 or (n not in state and visit(n)):
                return True
        state[k] = 2
        return False
    return any(k not in state and visit(k) for k in graph)


def _secrets(cfg, prefix=''):
    for key, value in cfg.items():
        path = f'{prefix}{key}'
        if isinstance(value, dict):
            if key.endswith('_env'):
                continue
            yield from _secrets(value, path + '.')
        elif isinstance(value, str) and value:
            if SECRET_VALUE.match(value) or (SECRET_KEY.search(key) and not key.endswith('_env')):
                yield path, value


def _env_map(value):
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) and ENV_NAME.match(v)
                                           for k, v in value.items())


def _check_config(b, add, mode):
    cfg, k = b.config, b.id
    if b.type == 'trigger.alert':
        if cfg.get('source') not in ('sample', 'prtg'):
            add(k, 'config', 'Set "source" to "sample" or "prtg".')
        if not str(cfg.get('sensor_id', '')).isdigit():
            add(k, 'config', 'Set a numeric "sensor_id".')
        if mode == 'live' and cfg.get('source') == 'sample':
            add(k, 'no-sample-live', 'Sample monitoring data cannot be used in a live run.')
    elif b.type == 'tool.builtin':
        fns = cfg.get('functions')
        if not isinstance(fns, list) or not fns or any(f not in BUILTIN_FUNCTIONS for f in fns):
            add(k, 'config', f'"functions" must list any of: {", ".join(BUILTIN_FUNCTIONS)}.')
        elif cfg.get('access') == 'write':
            add(k, 'config', 'Built-in functions are read-only; set "access" to "read".')
    elif b.type == 'tool.http':
        if not TOOL_NAME.match(str(cfg.get('name', ''))):
            add(k, 'config', '"name" must be 1–64 letters, digits, "_" or "-".')
        if not str(cfg.get('description', '')).strip():
            add(k, 'config', 'Describe when the agent should use this tool ("description").')
        if cfg.get('method', 'GET') not in ('GET', 'POST'):
            add(k, 'config', '"method" must be GET or POST.')
        if not str(cfg.get('url', '')).startswith(('https://', 'http://127.0.0.1', 'http://localhost')):
            add(k, 'config', '"url" must be HTTPS (plain HTTP only for localhost).')
        if not isinstance(cfg.get('parameters', {'type': 'object'}), dict):
            add(k, 'config', '"parameters" must be a JSON Schema object.')
        if 'headers_env' in cfg and not _env_map(cfg['headers_env']):
            add(k, 'config', '"headers_env" must map header names to environment variable names.')
    elif b.type == 'tool.mcp':
        cmd = cfg.get('command')
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
            add(k, 'config', '"command" must be a list, e.g. ["python", "server.py"].')
        for key in ('allow_tools', 'write_tools'):
            if key in cfg and not (isinstance(cfg[key], list) and all(isinstance(t, str) for t in cfg[key])):
                add(k, 'config', f'"{key}" must be a list of tool names.')
        if 'env_from' in cfg and not _env_map(cfg['env_from']):
            add(k, 'config', '"env_from" must map variable names to environment variable names.')
    elif b.type == 'guard.output_check':
        allowed = cfg.get('allowed_citations')
        if allowed is not None and allowed != 'seen_in_tool_results' and not isinstance(allowed, list):
            add(k, 'config', '"allowed_citations" must be a list or "seen_in_tool_results".')
        for pattern in cfg.get('block_patterns', []):
            try:
                re.compile(pattern)
            except (re.error, TypeError):
                add(k, 'config', f'Invalid pattern in "block_patterns": {pattern!r}.')
    elif b.type == 'guard.approval':
        if not isinstance(cfg.get('expires_s', 3600), int) or not 60 <= cfg.get('expires_s', 3600) <= 86_400:
            add(k, 'config', '"expires_s" must be between 60 and 86400.')
    elif b.type == 'output.file':
        path = str(cfg.get('path', ''))
        if not path or path.startswith(('/', '\\')) or '..' in re.split(r'[\\/]', path) or ':' in path:
            add(k, 'config', '"path" must be a relative path inside the output folder.')
    elif b.type == 'output.webhook':
        if not str(cfg.get('url', '')).startswith('https://'):
            add(k, 'config', '"url" must be HTTPS.')
        if 'headers_env' in cfg and not _env_map(cfg['headers_env']):
            add(k, 'config', '"headers_env" must map header names to environment variable names.')
    elif b.type == 'output.slack':
        if not ENV_NAME.match(str(cfg.get('channel_env', ''))):
            add(k, 'config', '"channel_env" must name the environment variable holding the channel ID.')
