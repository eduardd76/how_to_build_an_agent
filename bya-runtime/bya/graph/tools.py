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

from .. import core, validation
from .mcp import McpClient

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
    raise ValueError(f'Unsupported tool block "{block.type}".')


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
    age = (dt.datetime.now(core.UTC) - core.date(asset['verified_at'])).total_seconds()
    stale = age < -validation.MAX_CLOCK_SKEW or age > validation.MAX_SSOT_AGE
    if stale and not (asset.get('sample') and ctx.mode == 'sample'):  # sample records never go stale in demos
        return f'Asset "{asset["name"]}" has a stale or future verification date; ask the owner to reverify it.'
    keep = ('id', 'name', 'site', 'owner', 'service', 'source', 'verified_at', 'runbook_ids', 'sensors')
    return json.dumps({k: asset[k] for k in keep if k in asset})


def runbook_search(args, ctx):
    ids = args.get('ids') or []
    query = str(args.get('query', '')).lower().strip()
    hits = [r for r in ctx.runbooks
            if (ids and r['id'] in ids) or (query and query in (r['title'] + ' ' + ' '.join(r['steps'])).lower())]
    return json.dumps(hits[:5]) if hits else 'No matching runbook.'


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
