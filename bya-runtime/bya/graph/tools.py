"""Tool blocks attached to an agent. Each block yields callable tools with a name, a JSON Schema and an
access level (read or write). Write tools are gated by the executor, never here."""
import ast
import datetime as dt
import json
import operator
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .. import core, forecasting, validation
from .mcp import McpClient
from .memory import MemoryStore, search_documents

MAX_RESULT = 8_000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    access: str
    call: object       # function(args: dict) -> str
    block_id: str

    def schema(self):
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description, 'parameters': self.parameters}}


def open_tools(block, ctx):
    """Return (tools, close) for one attached tool block."""
    if block.type == 'tool.builtin':
        return [_builtin(name, block, ctx) for name in block.config['functions']], (lambda: None)
    if block.type == 'tool.http':
        return [_http(block)], (lambda: None)
    if block.type == 'tool.mcp':
        return _mcp(block)
    if block.type == 'memory.kv':
        return _memory_kv(block, ctx), (lambda: None)
    if block.type == 'memory.documents':
        return [_documents(block, ctx)], (lambda: None)
    return [], (lambda: None)  # memory.conversation and guard.policy are handled by the executor


# --- memory --------------------------------------------------------------------
# Memory writes stay inside the agent's own store, so they are not external write actions.

def _memory_kv(block, ctx):
    cfg, store = block.config, MemoryStore(ctx.memory_path)
    ns, cap = cfg['namespace'], cfg.get('max_entries', 200)
    key = {'type': 'string', 'description': 'Short key, e.g. "preferred_region".'}
    return [
        Tool('remember', f'Store a fact for future runs (memory "{ns}").',
             {'type': 'object', 'properties': {'key': key, 'value': {'type': 'string'}}, 'required': ['key', 'value']},
             'memory', lambda a: store.remember(ns, str(a.get('key', '')), a.get('value', ''), cap), block.id),
        Tool('recall', f'Read facts stored in memory "{ns}". Omit key to list all.',
             {'type': 'object', 'properties': {'key': key}}, 'memory',
             lambda a: store.recall(ns, a.get('key')), block.id),
        Tool('forget', f'Delete a fact from memory "{ns}".',
             {'type': 'object', 'properties': {'key': key}, 'required': ['key']}, 'memory',
             lambda a: store.forget(ns, str(a.get('key', ''))), block.id),
    ]


def _documents(block, ctx):
    cfg = block.config
    folder = (Path(ctx.knowledge_dir) / cfg.get('folder', '.')).resolve()
    base = Path(ctx.knowledge_dir).resolve()
    if folder != base and base not in folder.parents:
        raise ValueError('Document folder must be inside the knowledge folder.')
    return Tool('search_documents', 'Keyword search over local reference documents. Returns file names and snippets.',
                {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}, 'read',
                lambda a: search_documents(folder, a.get('query', ''), cfg.get('max_results', 3)), block.id)


# --- built-ins -----------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc(node):
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _calc(node.left), _calc(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError('Exponent too large.')
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_calc(node.operand))
    raise ValueError('Only numbers and + - * / % ** ( ) are allowed.')


def calculator(args, ctx):
    expr = str(args.get('expression', ''))
    if len(expr) > 200:
        raise ValueError('Expression too long.')
    value = _calc(ast.parse(expr, mode='eval'))
    return str(round(value, 10) if isinstance(value, float) else value)


def time_now(args, ctx):
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _stale(asset, ctx):
    age = (dt.datetime.now(core.UTC) - core.date(asset['verified_at'])).total_seconds()
    stale = age < -validation.MAX_CLOCK_SKEW or age > validation.MAX_SSOT_AGE
    return stale and not (asset.get('sample') and ctx.mode == 'sample')  # sample records never go stale in demos


def asset_lookup(args, ctx):
    """Exact match only: asset id, asset name, or mapped sensor id. No fuzzy identity."""
    query = str(args.get('query', '')).strip()
    doc = validation.validate_ssot(ctx.ssot)
    matches = [a for a in doc['assets']
               if query in (a['id'], a['name']) or query in [str(s['id']) for s in a['sensors']]]
    if len(matches) != 1:
        return f'No unique asset matches "{query}". Use an exact asset id, name or sensor id.'
    asset = matches[0]
    if asset.get('sample') and ctx.mode == 'live':
        raise ValueError('This asset is sample data; live runs need reviewed real records.')
    if _stale(asset, ctx):
        return f'Asset "{asset["name"]}" has a stale or future verification date; ask the owner to reverify it.'
    keep = ('id', 'name', 'site', 'owner', 'service', 'source', 'verified_at', 'runbook_ids', 'sensors')
    return json.dumps({k: asset[k] for k in keep if k in asset})


def runbook_search(args, ctx):
    ids = args.get('ids') or []
    query = str(args.get('query', '')).lower().strip()
    hits = [r for r in ctx.runbooks
            if (ids and r['id'] in ids) or (query and query in (r['title'] + ' ' + ' '.join(r['steps'])).lower())]
    return json.dumps(hits[:5]) if hits else 'No matching runbook.'


def _sensor(sensor_id, ctx):
    doc = validation.validate_ssot(ctx.ssot)
    hits = [(a, s) for a in doc['assets'] for s in a['sensors'] if str(s['id']) == sensor_id]
    if len(hits) != 1:
        raise ValueError(f'No unique asset maps sensor "{sensor_id}". Use an exact monitoring sensor id.')
    asset, sensor = hits[0]
    if asset.get('sample') and ctx.mode == 'live':
        raise ValueError('This sensor belongs to sample data; live runs need reviewed real records.')
    if _stale(asset, ctx):
        raise ValueError(f'Asset "{asset["name"]}" has a stale or future verification date; ask the owner to reverify it.')
    return asset, sensor


FORECAST_INTERVAL, FORECAST_PERIOD = 300, 288  # 5-minute samples; the seasonal baseline repeats the previous day


def metric_forecast(args, ctx):
    """Forecast one metric from its monitoring history, backtested against a seasonal-naive baseline."""
    sensor_id = str(args.get('sensor_id', '')).strip()
    direction = args.get('direction', 'above')
    if direction not in ('above', 'below'):
        raise ValueError('"direction" must be "above" or "below".')
    threshold = float(args['threshold'])
    horizon = max(1, min(int(args.get('horizon_steps', 48)), 96))
    asset, sensor = _sensor(sensor_id, ctx)
    if getattr(ctx, 'monitoring', None) is None:
        raise ValueError('No monitoring adapter configured.')
    now = dt.datetime.now(core.UTC)
    values = validation.series_check(ctx.monitoring.history(sensor, FORECAST_INTERVAL, now), FORECAST_INTERVAL, now)
    backend = 'demo-trend' if ctx.mode == 'sample' else 'timesfm'  # live never falls back to the demo trend
    f = forecasting.forecast(values, horizon, threshold, direction, backend, FORECAST_PERIOD)
    step = f['crossing_step']
    return json.dumps({
        'asset': asset['name'], 'service': asset['service'], 'owner': asset['owner'],
        'channel': sensor['channel'], 'unit': sensor['unit'], 'backend': backend,
        'latest_value': round(values[-1], 2), 'forecast_end_value': round(f['point'][-1], 2),
        'horizon_minutes': horizon * FORECAST_INTERVAL // 60, 'threshold': threshold, 'direction': direction,
        'crossing_in_minutes': step * FORECAST_INTERVAL // 60 if step else None,
        'holdout_mae': round(f['mae'], 3), 'seasonal_baseline_mae': round(f['baseline_mae'], 3),
        'beats_baseline': f['beats_baseline'], 'runbook_ids': asset.get('runbook_ids', []),
        'note': ('Metric estimate, not an outage probability. ' + f['interval_note'] + '.'
                 + ('' if f['beats_baseline'] else ' The model did not beat the seasonal baseline: report it as unreliable.')),
    })


# Configuration rules for IOS-style configs. Each finding names the rule, the line and a masked excerpt.
MAX_CONFIG = 200_000
SECRET_AFTER = re.compile(r'(?i)\b(password|secret|community|key|key-string)(\s+\d)?\s+\S+')
LINE_RULES = [
    ('CFG-MGMT-01', 'high', 'Telnet allowed on VTY lines', re.compile(r'^\s*transport input .*\b(telnet|all)\b', re.I)),
    ('CFG-MGMT-02', 'medium', 'Plain HTTP management server enabled', re.compile(r'^\s*ip http server\s*$', re.I)),
    ('CFG-SNMP-01', 'high', 'Default SNMP community string', re.compile(r'^\s*snmp-server community (public|private)\b', re.I)),
    ('CFG-SNMP-02', 'high', 'SNMP read-write community', re.compile(r'^\s*snmp-server community \S+ RW\b', re.I)),
    ('CFG-AUTH-02', 'high', 'Enable password instead of enable secret', re.compile(r'^\s*enable password\b', re.I)),
    ('CFG-AUTH-03', 'medium', 'Local user with a reversible password', re.compile(r'^\s*username \S+ (privilege \d+ )?password\b', re.I)),
    ('CFG-ACL-01', 'high', 'ACL permits any source to any destination', re.compile(r'^\s*(\d+\s+)?(access-list \d+ )?permit ip any any\b', re.I)),
]
ABSENT_RULES = [
    ('CFG-AUTH-01', 'medium', 'Password encryption service not enabled', re.compile(r'^\s*service password-encryption\b', re.I | re.M)),
    ('CFG-LOG-01', 'low', 'No remote syslog server', re.compile(r'^\s*logging (host )?\d', re.I | re.M)),
    ('CFG-NTP-01', 'low', 'No NTP server', re.compile(r'^\s*ntp server\b', re.I | re.M)),
]


def config_files_dir(ctx):
    return Path(getattr(ctx, 'configs_dir', None) or Path(ctx.knowledge_dir).parent / 'configs')


def config_lint(args, ctx):
    """Check a device configuration against the built-in rules. Read-only; secrets are masked in findings."""
    text, name = args.get('text'), args.get('file')
    if name:
        base = config_files_dir(ctx).resolve()
        path = (base / str(name).removeprefix('configs/')).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            return f'No config file "{name}" in the configs folder.'
        text = path.read_text(errors='replace')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Give a config "file" from the configs folder, or the config "text".')
    if len(text) > MAX_CONFIG:
        raise ValueError(f'Config is larger than {MAX_CONFIG} characters.')
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('!'):
            continue  # comments never trigger or suppress rules
        for rule, severity, title, rx in LINE_RULES:
            if rx.search(line):
                excerpt = SECRET_AFTER.sub(lambda m: f'{m.group(1)} ****', line.strip())[:120]
                findings.append({'rule': rule, 'severity': severity, 'title': title, 'line': n, 'excerpt': excerpt})
    active = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('!'))
    findings += [{'rule': rule, 'severity': severity, 'title': title, 'line': None, 'excerpt': 'not configured'}
                 for rule, severity, title, rx in ABSENT_RULES if not rx.search(active)]
    order = {'high': 0, 'medium': 1, 'low': 2}
    findings.sort(key=lambda f: (order[f['severity']], f['line'] or 10**9))
    return json.dumps({'file': name, 'lines': len(text.splitlines()), 'findings': findings,
                       'rules_checked': len(LINE_RULES) + len(ABSENT_RULES)})


BUILTINS = {
    'calculator': (calculator, 'Evaluate an arithmetic expression. Use for any maths.',
                   {'type': 'object', 'properties': {'expression': {'type': 'string'}}, 'required': ['expression']}),
    'time_now': (time_now, 'Current UTC date and time.', {'type': 'object', 'properties': {}}),
    'asset_lookup': (asset_lookup, 'Look up a device in the verified asset register by exact asset id, name or '
                                   'monitoring sensor id. Returns owner, service, site and mapped runbook ids.',
                     {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}),
    'runbook_search': (runbook_search, 'Fetch approved runbooks by id, or search their text. '
                                       'Cite runbook ids exactly as returned.',
                       {'type': 'object', 'properties': {'ids': {'type': 'array', 'items': {'type': 'string'}},
                                                         'query': {'type': 'string'}}}),
    'metric_forecast': (metric_forecast, 'Forecast one monitored metric by exact sensor id: when it may cross a '
                                         'threshold, backtested against a seasonal baseline. Read-only.',
                        {'type': 'object', 'properties': {
                            'sensor_id': {'type': 'string'}, 'threshold': {'type': 'number'},
                            'direction': {'type': 'string', 'enum': ['above', 'below']},
                            'horizon_steps': {'type': 'integer', 'description': '5-minute steps, 1-96 (default 48)'}},
                         'required': ['sensor_id', 'threshold']}),
    'config_lint': (config_lint, 'Check a device configuration against the configuration rules. Pass a file name '
                                 'from the configs folder, or the config text. Returns findings with rule ids.',
                    {'type': 'object', 'properties': {'file': {'type': 'string'}, 'text': {'type': 'string'}}}),
}


def _builtin(name, block, ctx):
    fn, description, params = BUILTINS[name]
    return Tool(name, description, params, 'read', lambda args, f=fn: f(args, ctx), block.id)


# --- HTTP ----------------------------------------------------------------------

def _headers(block):
    out = {}
    for header, env in block.config.get('headers_env', {}).items():
        if env not in os.environ:
            raise ValueError(f'Environment variable {env} is not set (header {header}).')
        out[header] = os.environ[env]
    return out


def _http(block):
    cfg = block.config
    method = cfg.get('method', 'GET')

    def call(args):
        args = dict(args)
        url = cfg['url']
        for key in re.findall(r'{(\w+)}', url):
            if key not in args:
                raise ValueError(f'Missing parameter "{key}".')
            url = url.replace('{' + key + '}', urllib.parse.quote(str(args.pop(key)), safe=''))
        if method == 'GET':
            if args:
                url += ('&' if '?' in url else '?') + urllib.parse.urlencode(args)
            data = core.http(url, _headers(block))
        else:
            data = core.http(url, _headers(block), args)
        return json.dumps(data)

    params = cfg.get('parameters', {'type': 'object', 'properties': {}})
    return Tool(cfg['name'], cfg['description'], params, cfg['access'], call, block.id)


# --- MCP -----------------------------------------------------------------------

def _mcp(block):
    cfg = block.config
    client = McpClient(cfg['command'], cfg.get('env_from'), timeout=cfg.get('timeout_s', 30))
    client.start()
    try:
        allow = set(cfg.get('allow_tools') or [])
        writes = set(cfg.get('write_tools') or [])
        tools = []
        for t in client.list_tools():
            name = t.get('name', '')
            if allow and name not in allow:
                continue

            def call(args, n=name):
                text, is_error = client.call_tool(n, args)
                return ('Error: ' + text) if is_error else text
            access = 'write' if cfg['access'] == 'write' or name in writes else 'read'
            tools.append(Tool(name, t.get('description', ''), t.get('inputSchema') or {'type': 'object'},
                              access, call, block.id))
    except Exception:
        client.close()
        raise
    return tools, client.close
