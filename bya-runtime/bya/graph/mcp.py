"""Minimal MCP client for local servers over stdio (JSON-RPC 2.0, one message per line). Standard library only.

The server process gets a minimal environment (PATH, HOME, locale) plus only the variables the diagram maps
explicitly, so a third-party server never sees your other credentials.
"""
import json
import os
import queue
import subprocess
import threading

PROTOCOL_VERSION = '2025-06-18'
BASE_ENV = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'TEMP', 'TMP')


class McpError(ValueError):
    pass


class McpClient:
    def __init__(self, command, env_from=None, timeout=30):
        self.command = command
        self.env_from = env_from or {}
        self.timeout = timeout
        self.proc = None
        self._lines = queue.Queue()
        self._next_id = 0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
        for name, source in self.env_from.items():
            if source not in os.environ:
                raise McpError(f'Environment variable {source} is not set (needed by the MCP server).')
            env[name] = os.environ[source]
        try:
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        except OSError as e:
            raise McpError(f'Could not start MCP server {self.command[0]!r}: {e.strerror}.') from None
        threading.Thread(target=self._read, daemon=True).start()
        result = self._request('initialize', {
            'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
            'clientInfo': {'name': 'bya-agent-studio', 'version': '0.3'}})
        if 'protocolVersion' not in result:
            raise McpError('MCP server returned an invalid initialize response.')
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def list_tools(self):
        tools, cursor = [], None
        while True:
            result = self._request('tools/list', {'cursor': cursor} if cursor else {})
            tools.extend(result.get('tools', []))
            cursor = result.get('nextCursor')
            if not cursor:
                return tools

    def call_tool(self, name, arguments):
        result = self._request('tools/call', {'name': name, 'arguments': arguments})
        parts = []
        for item in result.get('content', []):
            if item.get('type') == 'text':
                parts.append(item.get('text', ''))
            else:
                parts.append(f'[{item.get("type", "unknown")} content omitted]')
        if not parts and 'structuredContent' in result:
            parts.append(json.dumps(result['structuredContent']))
        return '\n'.join(parts), bool(result.get('isError'))

    # --- transport ---

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self._lines.put(line)
        self._lines.put(None)

    def _send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise McpError('MCP server closed its input.') from None

    def _request(self, method, params):
        self._next_id += 1
        rid = self._next_id
        self._send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
        while True:
            try:
                line = self._lines.get(timeout=self.timeout)
            except queue.Empty:
                raise McpError(f'MCP server did not answer "{method}" within {self.timeout}s.') from None
            if line is None:
                raise McpError('MCP server exited.')
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # servers sometimes print logs to stdout; skip them
            if 'method' in msg and 'id' in msg:  # a request from the server (sampling, roots): not supported
                self._send({'jsonrpc': '2.0', 'id': msg['id'],
                            'error': {'code': -32601, 'message': 'Method not supported by this client'}})
                continue
            if msg.get('id') != rid:
                continue  # notifications and stale responses
            if 'error' in msg:
                raise McpError(f'MCP error on "{method}": {msg["error"].get("message", msg["error"])}')
            return msg.get('result', {})
