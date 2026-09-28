# How to Build an AI Agent — A Practical Guide

**Conclusion first:** an agent is an LLM running in a loop, choosing tools, observing results, and stopping when a goal is met. Building a *reliable* one is mostly systems engineering — tool design, context management, evaluation, and guardrails — not model selection.

Three rules drive everything below:

1. **Start with the simplest thing that works.** Most tasks need one LLM call or a fixed workflow, not an agent.
2. **Measure before you scale.** No evals = no idea whether a change helped.
3. **Constrain what the agent can do.** Autonomy without permissions, limits, and review is a liability.

Examples use Python's standard library and any OpenAI-compatible chat endpoint: a local model through Ollama or vLLM, or a hosted provider. The small client in [`examples/llm.py`](examples/llm.py) is configured with `LLM_BASE_URL`, `LLM_MODEL` and, for hosted providers, `LLM_API_KEY`. The concepts transfer to any provider or framework.

---

## The Path at a Glance

| # | Phase | What you get | Added vs. the common 10-step roadmap |
|---|-------|--------------|--------------------------------------|
| 0 | Decide if you need an agent | Avoid over-engineering | **New** |
| 1 | Fundamentals | Correct mental model | Sharpened |
| 2 | Core components + the loop | Architecture | Added the loop and stop conditions |
| 3 | Prompting → context engineering | Predictable behavior | Added structured outputs |
| 4 | First simple agent | Working loop | Concrete code |
| 5 | Tools & APIs | Real actions | **Moved before memory**; added tool design, MCP |
| 6 | Memory & context | Continuity | Vector DB is optional, not default |
| 7 | Evaluation | Evidence it works | **New** |
| 8 | Guardrails & safety | Controlled autonomy | **New** |
| 9 | Production workflow | Observability, error handling | Added tracing |
| 10 | Multi-agent | Scale for parallel work | Added "when *not* to" |
| 11 | Deploy, monitor, optimize cost | Stable service | Added cost levers |
| 12 | Stay current | Relevance | Trimmed |

---

## Phase 0 — Decide If You Need an Agent

An agent trades cost, latency, and predictability for flexibility. Use this ladder and stop at the first rung that works:

| Rung | Pattern | Use when | Example |
|------|---------|----------|---------|
| 1 | Single LLM call | One input → one output | Classify a support ticket |
| 2 | Workflow (code controls steps) | Steps are known in advance | Extract invoice → validate → write to ERP |
| 3 | Agent (model controls steps) | Steps can't be specified up front | "Investigate why checkout latency spiked" |

Build an agent only if **all four** are true:

- **Complexity** — the task is multi-step and hard to script.
- **Value** — the outcome justifies higher cost and latency.
- **Viability** — the model is actually capable at this task type (test it).
- **Recoverable errors** — mistakes can be caught (tests, review, rollback).

---

## Phase 1 — Fundamentals

| | Script | Chatbot | Agent |
|---|---|---|---|
| Who decides the next step | Your code | Nobody (one reply) | The model |
| Takes actions | Yes, fixed | No | Yes, chosen at runtime |
| Handles the unexpected | No | Partially | Yes, within limits |
| Failure mode | Crashes | Wrong answer | Wrong *actions* — more expensive |

**The LLM's role:** it is the reasoning engine that decides *what to do next*. It is not a database (it hallucinates facts), not a calculator (use a tool), and not a security boundary (it can be manipulated).

**Where agents deliver today:** coding assistants, IT/network operations triage, research and report synthesis, customer support with account actions, data analysis over internal systems.

---

## Phase 2 — Core Components and the Loop

| Component | Role | Common mistake |
|-----------|------|----------------|
| Model | Reasoning, planning, tool selection | Picking the cheapest model before proving the task works |
| Instructions | Goal, constraints, output contract | Vague goals, no stop condition |
| Tools | Actions on the outside world | Too many overlapping tools |
| Memory / context | What the model sees each turn | Dumping everything into the prompt |
| Environment | Where actions execute (sandbox, APIs) | Running with production credentials |
| **Loop + stop conditions** | Think → act → observe → repeat | No max-iteration or budget limit |

The loop is the part most roadmaps skip:

```
          ┌──────────────────────────────┐
 goal ──► │  Model decides next step      │
          └──────────┬───────────────────┘
                     │ tool call?
           yes ◄─────┴─────► no → final answer (stop)
            │
            ▼
     Execute tool (your code, with permissions)
            │
            ▼
     Append result to context ──► back to the model
```

**Stop conditions you always need:** goal reached (`end_turn`), max iterations, token/cost budget, wall-clock timeout, and a human-escalation path.

---

## Phase 3 — Prompting → Context Engineering

Prompting is the entry point; the real discipline is **context engineering** — deciding what the model sees at each step.

A system prompt structure that works:

```text
# Role
You are a network operations assistant for ACME's data-center team.

# Goal
Diagnose connectivity incidents and propose a remediation. You do not apply changes.

# Tools
- get_interface_status: read-only. Use first to scope the problem.
- search_runbooks: use before proposing any fix.

# Constraints
- Never guess device names; list them with a tool if unsure.
- If evidence is insufficient after 5 tool calls, stop and say what is missing.

# Output
Return: summary (2 sentences), evidence (bullets with tool output references),
recommended action, confidence (low/medium/high).
```

Key practices:

- **Explain *why* behind rules.** "Keep answers short because they appear in a mobile alert" works better than "BE BRIEF!!!".
- **Few-shot examples** for format and edge cases — 2–3 diverse examples beat 10 similar ones.
- **Structured outputs** when code consumes the result. Don't parse free text:

```python
import json
from pydantic import BaseModel
from llm import chat

class Diagnosis(BaseModel):
    summary: str
    root_cause: str
    confidence: str  # "low" | "medium" | "high"

choice = chat(
    [{"role": "user", "content": "Interface Gi0/1 shows 12% CRC errors. Diagnose."}],
    response_format={"type": "json_schema",
                     "json_schema": {"name": "diagnosis", "schema": Diagnosis.model_json_schema()}},
)
diagnosis = Diagnosis.model_validate_json(choice["message"]["content"])  # fails loudly if malformed
print(diagnosis)
```

Not every server supports `json_schema`. If yours doesn't, use `{"type": "json_object"}` and keep the validation step.

---

## Phase 4 — Build Your First Simple Agent

Start with **one task, one tool, one success criterion**. The loop below is ~30 lines and shows every moving part:

```python
import json
from llm import chat, tool

tools = [tool(
    "calculator",
    "Evaluate an arithmetic expression. Use for any math instead of computing mentally.",
    {
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "e.g. '(1200 * 0.18) / 12'"}},
        "required": ["expression"],
    },
)]

def calculator(expression: str) -> str:
    # Demo only: restrict eval to digits/operators. Use a real parser in production.
    if not set(expression) <= set("0123456789+-*/(). "):
        return "Error: unsupported characters"
    return str(eval(expression))

messages = [{"role": "user", "content": "A $1,200 subscription gets 18% off, paid over 12 months. Monthly cost?"}]

for step in range(10):                        # stop condition: max iterations
    message = chat(messages, tools=tools)["message"]
    messages.append(message)

    calls = message.get("tool_calls") or []
    if not calls:                             # stop condition: model is done
        break

    for call in calls:
        args = json.loads(call["function"]["arguments"])
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": calculator(**args)})

print(message["content"])
```

Once you understand the loop, reuse it. `run_loop` in [`examples/llm.py`](examples/llm.py) is the same loop as a function, and it also returns tool errors to the model:

```python
from llm import run_loop

answer = run_loop(
    [{"role": "user", "content": "Monthly cost of $1,200/yr at 18% off?"}],
    tools=tools,
    handlers={"calculator": calculator},
)
```

---

## Phase 5 — Connect Tools & APIs

Tools come **before** memory: an LLM + loop + tools is already an agent; memory is an upgrade.

**Tool design is interface design for the model.** Most agent failures are tool failures.

| Principle | Bad | Good |
|-----------|-----|------|
| Clear name + purpose | `do_query` | `search_tickets_by_customer` |
| Description says *when* to use it | "Searches." | "Use to find open tickets for a customer. Returns max 20, newest first." |
| Few, non-overlapping tools | `get_user`, `fetch_user`, `lookup_user` | One `get_user` |
| Actionable errors | `500` | `"Customer ID not found. IDs look like C-12345. Try search_customers first."` |
| Bounded output | Returns 50,000 rows | Paginates, returns a summary + count |
| Least privilege | `run_sql(query)` on prod | `get_order_status(order_id)` read-only |
| Idempotent writes | `create_refund` retried = double refund | Accepts an idempotency key |

**Standardize integrations with MCP (Model Context Protocol).** Instead of hand-wiring each API into each agent, expose systems as MCP servers once and reuse them across agents and clients.

**Parallel calls:** the model may request several tools in one turn. Execute them concurrently and return **all** results in one message.

---

## Phase 6 — Add Memory & Context Management

| Type | What it holds | Implementation | Use when |
|------|---------------|----------------|----------|
| Working context | Current conversation + tool results | The `messages` list | Always |
| Compaction | Summary of older turns | Summarize when near the context limit | Long sessions |
| Structured memory | Facts, preferences, task state | Files, key-value store, SQL | Most "remember across sessions" needs |
| Semantic memory | Large unstructured knowledge | Vector DB + retrieval (RAG) | Searching thousands of documents |

Correction to a common assumption: **a vector database is not the default for memory.** For user preferences or task progress, a JSON file or a table is simpler, cheaper, and debuggable. Add embeddings when you need similarity search over a large corpus.

Minimal structured memory as a tool:

```python
import json, pathlib
from llm import tool

MEMORY = pathlib.Path("memory.json")

def remember(key: str, value: str) -> str:
    data = json.loads(MEMORY.read_text()) if MEMORY.exists() else {}
    data[key] = value
    MEMORY.write_text(json.dumps(data, indent=2))
    return f"Saved {key}."

def recall() -> str:
    return MEMORY.read_text() if MEMORY.exists() else "{}"

memory_tools = [
    tool("remember", "Store a durable fact about the user or task for future sessions.",
         {"type": "object",
          "properties": {"key": {"type": "string", "description": "Short identifier, e.g. 'preferred_region'."},
                         "value": {"type": "string", "description": "The fact to store."}},
          "required": ["key", "value"]}),
    tool("recall", "Return all stored facts. Call at the start of a task.",
         {"type": "object", "properties": {}}),
]
memory_handlers = {"remember": remember, "recall": recall}
```

**Context hygiene:** trim or clear old tool outputs, keep the system prompt stable (it enables prompt caching), and put volatile data (timestamps, IDs) at the end.

---

## Phase 7 — Evaluate (the missing step)

Without evals you are guessing. Build the eval set **before** adding features.

1. **Collect 20–50 real tasks** with known-good outcomes (from logs, tickets, or experts).
2. **Choose graders:**
   - Code checks for deterministic outcomes (correct value, valid JSON, right tool called).
   - LLM-as-judge with a written rubric for open-ended output.
   - Human review on a sample to calibrate the judge.
3. **Grade the trajectory, not only the answer:** Did it call the right tools? How many steps? Did it attempt anything forbidden?
4. **Run on every change** — prompt, tool, or model — and compare against the baseline.

```python
from llm import run_loop

cases = [
    {"input": "Monthly cost of $1,200/yr at 18% off?", "expect": "82"},
    {"input": "Split a $90 bill 3 ways with 20% tip", "expect": "36"},
]

def run_agent(prompt: str) -> str:
    return run_loop([{"role": "user", "content": prompt}], tools=tools, handlers={"calculator": calculator})

passed = sum(c["expect"] in run_agent(c["input"]) for c in cases)
print(f"{passed}/{len(cases)} passed")
```

Track: task success rate, steps per task, cost per **completed** task, latency, and safety violations.

---

## Phase 8 — Guardrails & Safety (the other missing step)

| Risk | Control |
|------|---------|
| Destructive actions | Human approval gate for writes, payments, deletes, config changes |
| Prompt injection (malicious text in web pages, emails, tickets) | Treat tool output as data, never as instructions; restrict what tools can do after reading untrusted content |
| Over-privileged access | Scoped, read-only credentials by default; sandbox code execution |
| Runaway loops / cost | Max iterations, token budgets, timeouts |
| Data leakage | Redact secrets and PII before they enter context; log access |
| Silent failure | Require the agent to state confidence and escalate when evidence is thin |

Approval gate inside a tool:

```python
def restart_service(service: str) -> str:
    """Restart a service. Requires human approval."""
    answer = input(f"Agent wants to restart '{service}'. Approve? [y/N] ")
    if answer.lower() != "y":
        return "Denied by operator. Propose an alternative or stop."
    return f"{service} restarted."  # call your real automation here
```

---

## Phase 9 — Build the Complete Production Workflow

Combine the pieces: **instructions → context/memory → tools → loop → validated output**, then add:

- **Tracing:** log every model call and tool call with inputs, outputs, tokens, latency, and a run ID. You debug agents by reading trajectories.
- **Error handling:** retry transient API errors (429, 5xx) with backoff; return tool errors to the model as tool results (for example `Error: device not found`) so it can recover, instead of crashing.
- **Finish-reason handling:** check `finish_reason` (`stop`, `tool_calls`, `length`, `content_filter`) before reading content.
- **End-to-end tests:** run the eval set from Phase 7 in CI.

A complete, runnable version is in [`examples/agent.py`](examples/agent.py).

For a larger worked case, see [`bya-runtime/`](bya-runtime/): a local operations assistant (PRTG, a local LLM, TimesFM, Slack) built as a declared pipeline, with output checks, evals, provenance and an approval state machine. [`TESTING.md`](TESTING.md) lists the staged tests, with exact commands, for taking it from sample data to live systems.

---

## Phase 10 — Multi-Agent Systems (only when justified)

Multi-agent setups cost several times more tokens than a single agent and add coordination failures. Use them when:

- Work is **parallelizable** (research 10 vendors, review 50 files).
- A single context would overflow with reading (sub-agents return summaries).
- Independent **review** adds measurable quality (a reviewer with a different prompt catches the executor's mistakes).

Common patterns:

| Pattern | Structure | Example |
|---------|-----------|---------|
| Orchestrator–workers | Lead plans, workers execute in parallel, lead merges | Competitive research report |
| Planner–executor–reviewer | Sequential with a quality gate | Code change → tests → review |
| Router | Classifier sends task to a specialist | Support: billing vs. technical |

Rule: prove a single agent hits its limit on your evals before splitting.

---

## Phase 11 — Deploy, Monitor, Optimize Cost

Monitor in production:

- **Quality:** task success rate (sampled evals on live traffic), escalation rate, user corrections.
- **Reliability:** error and timeout rates, loops hitting max iterations.
- **Cost/latency:** tokens and dollars per completed task, p50/p95 latency.
- **Safety:** blocked actions, approval denials, injection attempts.

Cost levers, cheapest first:

1. **Prompt caching** — keep the system prompt and tool list stable.
2. **Context hygiene** — trim old tool results, bound tool outputs.
3. **Batch processing** for non-urgent work (roughly half price).
4. **Lower reasoning effort / smaller model** on routes where evals show quality holds.

Roll out changes behind flags, compare against the eval baseline, and keep a rollback path.

---

## Phase 12 — Stay Current

The ecosystem moves monthly. Keep a small habit:

- Re-run your evals when a new model ships — upgrades often change behavior, not just quality.
- Follow provider changelogs and the MCP ecosystem.
- Publish what worked (and what failed) — failure write-ups are the scarcest content in this field.

---

## Checklist Before You Call It Production-Ready

- [ ] Phase 0 justification written down (why an agent, not a workflow)
- [ ] Tools are few, well-described, least-privilege, with actionable errors
- [ ] Stop conditions: max steps, budget, timeout
- [ ] Eval set of 20+ real cases, run in CI
- [ ] Approval gates on irreversible actions
- [ ] Tool output treated as untrusted data
- [ ] Full tracing of model and tool calls
- [ ] Cost per completed task measured and within target
