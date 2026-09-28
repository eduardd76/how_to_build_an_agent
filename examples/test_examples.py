"""Runs the example agent and the shared loop against a scripted OpenAI-compatible server.

This proves the plumbing (requests, tool calls, results, stop conditions), not model quality.
Run: python -m unittest discover -s examples
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ScriptedServer(BaseHTTPRequestHandler):
    """Requests each offered tool once, in order, then answers with the collected tool results."""

    SCRIPT = {
        "get_interface_status": {"device": "core-sw-01", "interface": "Gi0/1"},
        "search_runbooks": {"query": "CRC errors"},
        "calculator": {"expression": "1200 * 0.82 / 12"},
    }

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        offered = [t["function"]["name"] for t in body.get("tools", [])]
        answered = {m.get("tool_call_id") for m in body["messages"] if m["role"] == "tool"}
        pending = [n for n in offered if n in self.SCRIPT and f"call_{n}" not in answered]
        if pending:
            name = pending[0]
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{name}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(self.SCRIPT[name])}}]}
            finish = "tool_calls"
        else:
            results = [m["content"] for m in body["messages"] if m["role"] == "tool"]
            message, finish = {"role": "assistant", "content": "FINAL: " + " | ".join(results)}, "stop"
        raw = json.dumps({"choices": [{"message": message, "finish_reason": finish}],
                          "usage": {"total_tokens": 50}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{cls.server.server_port}/v1"
        os.environ["LLM_MODEL"] = "scripted"
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        global llm, agent
        import llm
        import agent

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_agent_gathers_evidence_then_answers(self):
        answer = agent.run_agent("Users behind core-sw-01 Gi0/1 report slow transfers.")
        self.assertTrue(answer.startswith("FINAL:"))
        self.assertIn("crc_errors", answer)   # interface status tool ran
        self.assertIn("RB-114", answer)       # runbook tool ran

    def test_run_loop_executes_tools(self):
        tools = [llm.tool("calculator", "Evaluate arithmetic.",
                          {"type": "object", "properties": {"expression": {"type": "string"}},
                           "required": ["expression"]})]
        answer = llm.run_loop([{"role": "user", "content": "Monthly cost?"}], tools,
                              {"calculator": lambda expression: str(round(eval(expression), 2))})
        self.assertIn("82.0", answer)

    def test_run_loop_returns_tool_errors_to_the_model(self):
        tools = [llm.tool("calculator", "Evaluate arithmetic.", {"type": "object", "properties": {}})]

        def broken(**kwargs):
            raise ValueError("calculator offline")

        answer = llm.run_loop([{"role": "user", "content": "Monthly cost?"}], tools, {"calculator": broken})
        self.assertIn("Error: calculator offline", answer)

    def test_unreachable_endpoint_is_transient(self):
        old = llm.BASE_URL
        llm.BASE_URL = "http://127.0.0.1:9/v1"
        try:
            with self.assertRaises(llm.TransientLLMError):
                llm.chat([{"role": "user", "content": "hi"}], timeout=2)
        finally:
            llm.BASE_URL = old


if __name__ == "__main__":
    unittest.main()
