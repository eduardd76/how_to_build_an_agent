"""Minimal client for any OpenAI-compatible chat completions endpoint.

Works with local servers (Ollama, vLLM, LM Studio) and hosted providers that expose
the same API. Standard library only.

Configure with environment variables:
    LLM_BASE_URL  default http://127.0.0.1:11434/v1  (Ollama)
    LLM_MODEL     default llama3.1:8b
    LLM_API_KEY   optional; sent as a Bearer token when set
"""

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")
API_KEY = os.environ.get("LLM_API_KEY", "")


class LLMError(Exception):
    """Non-retryable failure: bad request, auth, unknown model, malformed response."""


class TransientLLMError(LLMError):
    """Retryable failure: rate limit, server error, connection problem."""


def tool(name, description, parameters):
    """Build a tool definition. `parameters` is a JSON Schema object."""
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def chat(messages, tools=None, max_tokens=4000, response_format=None, timeout=120):
    """One model call. Returns the first choice: {"message", "finish_reason", "usage"}."""
    body = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
    if tools:
        body["tools"] = tools
    if response_format:
        body["response_format"] = response_format
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(BASE_URL + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        if e.code == 429 or e.code >= 500:
            raise TransientLLMError(f"HTTP {e.code}: {detail}") from None
        raise LLMError(f"HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise TransientLLMError(f"Connection failed: {getattr(e, 'reason', e)}") from None
    try:
        choice = data["choices"][0]
        choice["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"Unexpected response shape: {str(data)[:300]}") from None
    choice["usage"] = data.get("usage") or {}
    return choice


def run_loop(messages, tools, handlers, max_steps=10):
    """The agent loop: call the model, run requested tools, feed results back, stop when done.

    `handlers` maps tool name -> function(**arguments) -> str.
    Returns the final text, or raises if the step limit is hit.
    """
    for _ in range(max_steps):
        choice = chat(messages, tools=tools)
        message = choice["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or ""
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                result = handlers[name](**args)
            except Exception as e:  # return the error so the model can recover
                result = f"Error: {e}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
    raise RuntimeError(f"Stopped after {max_steps} steps without a final answer.")
