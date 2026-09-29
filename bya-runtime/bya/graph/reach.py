"""Reach: everything a diagram's agents could touch, computed before anything runs.

Per agent: the model endpoint, what it can read, the devices and commands it can use, what it can change
(each change needs approval), and where approved output goes. No block grants a device configuration
session, so "device_config_paths" is always 0; it is reported so a reviewer can see it.
"""
import os
from urllib.parse import urlsplit

from .devices import describe_scope
from .tools import BUILTINS

LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')


def reach(diagram, ctx):
    agents = []
    for agent in diagram.of_category('agent'):
        cfg = agent.config
        url = cfg.get('model_url') or os.environ.get('LLM_BASE_URL', 'http://127.0.0.1:11434/v1')  # as ChatModel resolves it
        host = urlsplit(str(url)).hostname or ''
        entry = {'agent': agent.id, 'model': cfg.get('model') or '(LLM_MODEL)',
                 'model_location': 'local' if host in LOCAL_HOSTS else f'remote: {host}',
                 'read': [], 'change': [], 'devices': [], 'memory': [], 'limits': {}}
        for b in diagram.attached(agent.id):
            c = b.config
            if b.type == 'tool.builtin':
                entry['read'] += [{'block': b.id, 'system': 'built-in', 'what': f} for f in c.get('functions', []) if f in BUILTINS]
            elif b.type == 'tool.http':
                item = {'block': b.id, 'system': urlsplit(str(c.get('url', ''))).hostname or c.get('url', ''), 'what': c.get('name', '')}
                entry['change' if c.get('access') == 'write' else 'read'].append(item)
            elif b.type == 'tool.mcp':
                server = ' '.join(c.get('command', []))
                writes = c.get('write_tools') or []
                if c.get('access') == 'write' and not writes:
                    entry['change'].append({'block': b.id, 'system': f'MCP: {server}', 'what': 'every tool it offers'})
                else:
                    entry['change'] += [{'block': b.id, 'system': f'MCP: {server}', 'what': t} for t in writes]
                    entry['read'].append({'block': b.id, 'system': f'MCP: {server}',
                                          'what': ', '.join(c.get('allow_tools') or []) or 'all read tools it offers'})
            elif b.type == 'tool.device':
                entry['devices'].append({'block': b.id, **describe_scope(c, ctx)})
            elif b.type.startswith('memory.'):
                entry['memory'].append({'block': b.id, 'type': b.type, 'namespace': c.get('namespace', c.get('folder', ''))})
            elif b.type == 'guard.policy':
                entry['limits'] = {'deny_tools': c.get('deny_tools', []), 'always_ask': c.get('require_approval_tools', []),
                                   'max_tool_calls': c.get('max_tool_calls')}
        agents.append(entry)
    outputs = [{'block': b.id, 'type': b.type,
                'where': b.config.get('path') or urlsplit(str(b.config.get('url', ''))).hostname or b.config.get('channel_env', '')}
               for b in diagram.of_category('output')]
    devices = sorted({d for a in agents for scope in a['devices'] for d in scope['devices']})
    return {
        'agents': agents, 'outputs': outputs,
        'summary': {
            'devices_readable': len(devices),
            'read_tools': sum(len(a['read']) for a in agents),
            'change_paths': sum(len(a['change']) for a in agents),
            'outputs_after_approval': len(outputs),
            'device_config_paths': 0,
            'remote_models': sorted({a['model_location'] for a in agents if a['model_location'] != 'local'}),
        },
    }
