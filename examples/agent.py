"""A complete, minimal production-shaped agent.

Shows: system prompt, tools with least privilege, approval gate, stop conditions,
error handling, tracing, and finish-reason checks.

Works with any OpenAI-compatible endpoint; see llm.py for configuration.
Run:  export LLM_MODEL=<model id> && python agent.py
"""

import json
import time
import uuid

from llm import LLMError, TransientLLMError, chat, tool

MAX_STEPS = 8
MAX_TOTAL_TOKENS = 100_000

SYSTEM = """You are a network operations assistant.

Goal: diagnose the reported issue and propose a fix. Use tools to gather evidence
before concluding; never guess device names or statuses.

Constraints:
- restart_interface changes production state; only call it when evidence clearly
  supports it. A human must approve it.
- If evidence is insufficient after 5 tool calls, stop and state what is missing.

Output: summary, evidence (bullets), recommended action, confidence (low/medium/high)."""

TOOLS = [
    tool(
        "get_interface_status",
        "Read-only. Returns status and error counters for one interface on one device. Use first to scope a problem.",
        {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device hostname, e.g. 'core-sw-01'."},
                "interface": {"type": "string", "description": "Interface name, e.g. 'Gi0/1'."},
            },
            "required": ["device", "interface"],
        },
    ),
    tool(
        "search_runbooks",
        "Search internal runbooks. Use before recommending any remediation.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
    tool(
        "restart_interface",
        "Bounce an interface (shut/no shut). Changes production state. Requires human approval.",
        {
            "type": "object",
            "properties": {
                "device": {"type": "string"},
                "interface": {"type": "string"},
                "reason": {"type": "string", "description": "Evidence-based justification."},
            },
            "required": ["device", "interface", "reason"],
        },
    ),
]


# --- Tool implementations (replace the stubs with real integrations) ---------

def get_interface_status(device: str, interface: str) -> str:
    fake = {("core-sw-01", "Gi0/1"): {"status": "up", "crc_errors": 18423, "input_errors_pct": 12.4}}
    data = fake.get((device, interface))
    if data is None:
        raise ValueError(f"Unknown device/interface '{device} {interface}'. Check the hostname and interface name.")
    return json.dumps(data)


def search_runbooks(query: str) -> str:
    return "RB-114 High CRC errors: 1) check optic light levels 2) reseat/replace cable or SFP 3) bounce interface only after physical checks."


def restart_interface(device: str, interface: str, reason: str) -> str:
    answer = input(f"\n[APPROVAL] Restart {device} {interface}? Reason: {reason}\nApprove? [y/N] ")
    if answer.strip().lower() != "y":
        return "Denied by operator. Recommend next steps without restarting."
    return f"{interface} on {device} restarted."


HANDLERS = {
    "get_interface_status": get_interface_status,
    "search_runbooks": search_runbooks,
    "restart_interface": restart_interface,
}


# --- Tracing -----------------------------------------------------------------

def trace(run_id: str, event: str, **fields) -> None:
    print(json.dumps({"run_id": run_id, "event": event, "ts": round(time.time(), 3), **fields}))


# --- The loop ----------------------------------------------------------------

def run_agent(task: str) -> str:
    run_id = uuid.uuid4().hex[:8]
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
    total_tokens = 0

    for step in range(1, MAX_STEPS + 1):
        started = time.time()
        try:
            choice = chat(messages, tools=TOOLS, max_tokens=8000)
        except TransientLLMError as e:  # rate limit, server error, connection: back off and retry
            trace(run_id, "transient_error", step=step, error=str(e))
            time.sleep(min(2 ** step, 30))
            continue
        except LLMError as e:           # bad request, auth, unknown model: retrying won't help
            return f"Stopped: model call failed ({e})."

        message, finish = choice["message"], choice.get("finish_reason")
        total_tokens += choice["usage"].get("total_tokens", 0)
        trace(run_id, "model_call", step=step, finish_reason=finish,
              tokens=total_tokens, latency_s=round(time.time() - started, 2))

        messages.append(message)

        if finish == "content_filter":
            return "The model declined this request."
        if finish == "length":
            return "Stopped: response hit max_tokens. Increase the limit or narrow the task."
        calls = message.get("tool_calls") or []
        if not calls:
            return message.get("content") or ""

        if total_tokens > MAX_TOTAL_TOKENS:
            return f"Stopped: token budget exceeded ({total_tokens})."

        for call in calls:
            name, raw_args = call["function"]["name"], call["function"]["arguments"] or "{}"
            try:
                args = json.loads(raw_args)
                output, is_error = HANDLERS[name](**args), False
            except Exception as e:  # return errors to the model so it can recover
                args, output, is_error = raw_args, f"Error: {e}", True
            trace(run_id, "tool_call", tool=name, input=args, is_error=is_error)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})

    return f"Stopped: reached {MAX_STEPS} steps without finishing. Escalate to a human."


if __name__ == "__main__":
    print(run_agent("Users behind core-sw-01 Gi0/1 report slow file transfers. Investigate."))
