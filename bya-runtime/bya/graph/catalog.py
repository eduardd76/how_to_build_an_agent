"""Block types the canvas offers, and the data types that flow between them.

Flow types make the safety rules structural: an output accepts only an approved draft,
an approval accepts only a checked draft, and a check accepts only an agent's draft.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BlockSpec:
    type: str
    category: str                    # trigger | agent | tool | memory | guard | output
    inputs: frozenset = frozenset()  # flow types this block accepts; empty = no flow input
    output: str = None               # flow type this block emits; None = end of flow
    attachable: bool = False         # connects to an agent by attachment, never by flow


SPECS = {spec.type: spec for spec in (
    BlockSpec('trigger.manual', 'trigger', output='text'),
    BlockSpec('trigger.webhook', 'trigger', output='json'),
    BlockSpec('trigger.alert', 'trigger', output='alert'),
    BlockSpec('agent', 'agent', inputs=frozenset({'text', 'json', 'alert'}), output='draft'),
    BlockSpec('tool.builtin', 'tool', attachable=True),
    BlockSpec('tool.http', 'tool', attachable=True),
    BlockSpec('tool.mcp', 'tool', attachable=True),
    BlockSpec('tool.device', 'tool', attachable=True),
    BlockSpec('memory.kv', 'memory', attachable=True),
    BlockSpec('memory.conversation', 'memory', attachable=True),
    BlockSpec('memory.documents', 'memory', attachable=True),
    BlockSpec('guard.policy', 'guard', attachable=True),
    BlockSpec('guard.redact', 'guard', inputs=frozenset({'draft'}), output='draft'),
    BlockSpec('guard.output_check', 'guard', inputs=frozenset({'draft'}), output='checked_draft'),
    BlockSpec('guard.approval', 'guard', inputs=frozenset({'checked_draft'}), output='approved_draft'),
    BlockSpec('output.file', 'output', inputs=frozenset({'approved_draft'})),
    BlockSpec('output.webhook', 'output', inputs=frozenset({'approved_draft'})),
    BlockSpec('output.slack', 'output', inputs=frozenset({'approved_draft'})),
)}

BUILTIN_FUNCTIONS = ('calculator', 'time_now', 'asset_lookup', 'runbook_search', 'metric_forecast', 'config_lint')
NAMESPACE_LIMITS = {'max_entries': (1, 1000), 'max_items': (1, 20), 'max_results': (1, 10), 'max_tool_calls': (1, 200),
                    'max_commands': (1, 200), 'min_interval_s': (0, 60)}

AGENT_LIMITS = {
    'max_steps': (1, 50),
    'token_budget': (1_000, 1_000_000),
    'timeout_s': (5, 3_600),
}


# --- Canvas metadata: labels and settings forms (the validator is the source of truth for rules) ---

def _f(key, label, kind='text', **extra):
    return {'key': key, 'label': label, 'kind': kind, **extra}


UI = {
    'trigger.manual': ('Manual start', 'Start a run by hand, with optional text input.',
                       [_f('default_input', 'Default input', 'textarea')]),
    'trigger.webhook': ('Webhook', 'Start a run with a JSON payload.', []),
    'trigger.alert': ('Monitoring alert', 'Start from one monitoring sensor.', [
        _f('source', 'Source', 'select', options=['sample', 'prtg'], default='sample'),
        _f('sensor_id', 'Sensor ID', default='1001')]),
    'agent': ('Agent', 'A model that reasons in a loop and calls its attached tools.', [
        _f('instructions', 'Instructions', 'textarea', default='Describe the job, the evidence to gather and the answer format.'),
        _f('model_url', 'Model endpoint', default='http://127.0.0.1:11434/v1', help='OpenAI-compatible URL (Ollama, vLLM, llama.cpp).'),
        _f('model', 'Model', help='Leave empty to use the LLM_MODEL environment variable.'),
        _f('api_key_env', 'API key variable', help='Name of an environment variable; never the key itself.'),
        _f('max_steps', 'Max steps', 'number', default=8, min=1, max=50),
        _f('token_budget', 'Token budget', 'number', default=40000, min=1000, max=1000000),
        _f('timeout_s', 'Timeout (seconds)', 'number', default=180, min=5, max=3600)]),
    'tool.builtin': ('Built-in tools', 'Calculator, time, asset lookup, runbook search, metric forecast and config check. Read-only.', [
        _f('functions', 'Functions', 'multiselect', options=list(BUILTIN_FUNCTIONS), default=['asset_lookup', 'runbook_search']),
        _f('access', 'Access', 'select', options=['read'], default='read')]),
    'tool.http': ('HTTP tool', 'Call a JSON API. Headers come from environment variables.', [
        _f('name', 'Tool name', default='get_data'),
        _f('description', 'When to use it', 'textarea', default='Describe when the agent should call this.'),
        _f('method', 'Method', 'select', options=['GET', 'POST'], default='GET'),
        _f('url', 'URL', default='https://', help='Use {param} for path parameters.'),
        _f('access', 'Access', 'select', options=['read', 'write'], default='read'),
        _f('parameters', 'Parameters (JSON Schema)', 'json', default={'type': 'object', 'properties': {}}),
        _f('headers_env', 'Headers from env (JSON)', 'json', default={})]),
    'tool.mcp': ('MCP server', 'Tools from a local MCP server (stdio).', [
        _f('command', 'Command', 'list', default=['python', 'server.py'], help='One argument per line.'),
        _f('access', 'Access', 'select', options=['read', 'write'], default='read'),
        _f('allow_tools', 'Only these tools', 'list', default=[], help='Empty = all tools the server offers.'),
        _f('write_tools', 'Write tools', 'list', default=[], help='Tools that change state; each call needs approval.'),
        _f('env_from', 'Environment (JSON)', 'json', default={}, help='{"SERVER_VAR": "YOUR_ENV_VAR"}')]),
    'tool.device': ('Device commands', 'Read-only commands on network devices over SSH, through a command filter.', [
        _f('scope_source', 'Devices from', 'select', options=['ssot', 'netbox', 'list'], default='ssot',
           help='Asset register, NetBox, or a fixed list.'),
        _f('scope_filter', 'Device filter (JSON)', 'json', default={'site': 'Munich / HQ'},
           help='Asset register: site, owner, service, name. NetBox: site, role, tag (slugs).'),
        _f('devices', 'Devices (list source)', 'list', default=[]),
        _f('allow', 'Allowed commands', 'list', default=['show *'],
           help='Patterns such as "show interfaces *". Only show, display, ping and traceroute.'),
        _f('max_commands', 'Max commands per run', 'number', default=20, min=1, max=200),
        _f('min_interval_s', 'Seconds between commands per device', 'number', default=2, min=0, max=60),
        _f('timeout_s', 'Command timeout (seconds)', 'number', default=20, min=5, max=120),
        _f('username_env', 'SSH user variable', default='', help='Empty = your SSH config decides.'),
        _f('netbox_url_env', 'NetBox URL variable', default='NETBOX_URL'),
        _f('netbox_token_env', 'NetBox token variable', default='NETBOX_TOKEN'),
        _f('access', 'Access', 'select', options=['read'], default='read')]),
    'memory.kv': ('Fact memory', 'Lets the agent remember and recall facts across runs (remember / recall tools).', [
        _f('namespace', 'Namespace', default='facts', help='Agents sharing a namespace share facts.'),
        _f('max_entries', 'Max facts', 'number', default=200, min=1, max=1000)]),
    'memory.conversation': ('Run history', 'Gives the agent the inputs and results of its last completed runs.', [
        _f('namespace', 'Namespace', default='history'),
        _f('max_items', 'Runs to remember', 'number', default=5, min=1, max=20)]),
    'memory.documents': ('Document search', 'Keyword search over .md and .txt files in the knowledge folder.', [
        _f('folder', 'Folder inside knowledge/', default='.', help='Relative path; files never leave your machine.'),
        _f('max_results', 'Results per search', 'number', default=3, min=1, max=10)]),
    'guard.policy': ('Permission policy', 'Limits what the attached agent may call.', [
        _f('deny_tools', 'Never allow these tools', 'list', default=[]),
        _f('require_approval_tools', 'Always ask before these tools', 'list', default=[],
           help='Read tools that should still need approval.'),
        _f('max_tool_calls', 'Max tool calls per run', 'number', default=20, min=1, max=200)]),
    'guard.redact': ('Redact', 'Removes secrets (tokens, keys, passwords) and optionally emails and IPs from the draft.', [
        _f('emails', 'Redact email addresses', 'checkbox', default=True),
        _f('ipv4', 'Redact IPv4 addresses', 'checkbox', default=False),
        _f('patterns', 'Also redact patterns', 'list', default=[], help='Regular expressions, one per line.')]),
    'guard.output_check': ('Output check', 'Blocks drafts that claim actions, assert root causes or cite unknown sources.', [
        _f('allowed_citations', 'Allowed citations', 'select', options=['', 'seen_in_tool_results'], default='seen_in_tool_results'),
        _f('require_citation', 'Require a citation', 'checkbox', default=False),
        _f('block_patterns', 'Also block patterns', 'list', default=[], help='Regular expressions, one per line.')]),
    'guard.approval': ('Human approval', 'Pauses the run until a person approves the exact draft.', [
        _f('expires_s', 'Expires after (seconds)', 'number', default=3600, min=60, max=86400)]),
    'output.file': ('Save to file', 'Write the approved draft to a file in the output folder.', [
        _f('path', 'Path', default='briefs/output.md')]),
    'output.webhook': ('Webhook out', 'POST the approved draft to an HTTPS endpoint.', [
        _f('url', 'URL', default='https://'), _f('headers_env', 'Headers from env (JSON)', 'json', default={})]),
    'output.slack': ('Slack', 'Post the approved draft to one Slack channel.', [
        _f('channel_env', 'Channel variable', default='SLACK_CHANNEL')]),
}


def catalog():
    """Everything the canvas needs to render the palette, ports and settings forms."""
    return [{'type': s.type, 'category': s.category, 'label': UI[s.type][0], 'description': UI[s.type][1],
             'inputs': sorted(s.inputs), 'output': s.output, 'attachable': s.attachable, 'fields': UI[s.type][2]}
            for s in SPECS.values()]
