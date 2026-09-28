"""A tiny MCP server over stdio for tests. Logs every tools/call to $FAKE_MCP_LOG (one JSON line each)."""
import json
import os
import sys

TOOLS = [
    {'name': 'get_device', 'description': 'Read device facts.',
     'inputSchema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}},
    {'name': 'restart_interface', 'description': 'Bounce an interface.',
     'inputSchema': {'type': 'object', 'properties': {'interface': {'type': 'string'}}, 'required': ['interface']}},
    {'name': 'env_keys', 'description': 'List environment variable names (test only).',
     'inputSchema': {'type': 'object', 'properties': {}}},
]


def reply(rid, result=None, error=None):
    msg = {'jsonrpc': '2.0', 'id': rid}
    msg.update({'error': error} if error else {'result': result})
    sys.stdout.write(json.dumps(msg) + '\n')
    sys.stdout.flush()


print('fake server starting (a log line on stdout the client must skip)', flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get('method'), msg.get('id')
    if method == 'initialize':
        reply(rid, {'protocolVersion': msg['params']['protocolVersion'], 'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'fake', 'version': '1'}})
    elif method == 'notifications/initialized':
        pass
    elif method == 'tools/list':
        reply(rid, {'tools': TOOLS})
    elif method == 'tools/call':
        name, args = msg['params']['name'], msg['params'].get('arguments', {})
        with open(os.environ['FAKE_MCP_LOG'], 'a') as f:
            f.write(json.dumps({'name': name, 'arguments': args}) + '\n')
        if name == 'get_device':
            reply(rid, {'content': [{'type': 'text', 'text': json.dumps({'name': args.get('name'), 'os': 'IOS-XE'})}]})
        elif name == 'env_keys':
            reply(rid, {'content': [{'type': 'text', 'text': json.dumps(sorted(os.environ))}]})
        elif name == 'restart_interface':
            reply(rid, {'content': [{'type': 'text', 'text': f'restarted {args.get("interface")}'}]})
        else:
            reply(rid, {'content': [{'type': 'text', 'text': 'no such tool'}], 'isError': True})
    elif rid is not None:
        reply(rid, error={'code': -32601, 'message': 'unknown method'})
