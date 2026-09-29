# BYA local agent studio — technical preview 0.2

Two runnable guided templates: incident briefing and monitoring metric forecasting.
This is a local Python application with a web interface, not a signed desktop installer.
The hosted builder runs synthetic examples. Real integrations and models run only here.

## Architecture (0.2)

```
server.py            HTTP layer only: loopback, session token, Origin checks, routes
bya/pipeline.py      Templates = ordered steps bound to builder blocks; runs them and records the trace
bya/validation.py    Blocking checks: agent config, SSOT identity and freshness, telemetry quality
bya/forecasting.py   TimesFM / demo trend, three holdouts, seasonal-naive gate
bya/adapters.py      Ports: monitoring (PRTG | sample), model (local | sample template), Slack delivery
bya/prompts.py       Every model instruction, versioned (PROMPT_VERSION is stored with each run)
bya/guards.py        Deterministic checks on model text before a human reviews it
bya/store.py         Run history and the delivery state machine
evals/               Fixed-evidence evaluation of incident briefs and forecast explanations
bya/graph/           Diagram runtime: block catalogue, validator, executor, tools (built-in, HTTP, MCP), memory
diagrams/            Example diagrams
knowledge/           Documents for the document-search memory block (sample included)
```

Flow of an incident brief: SSOT → Knowledge → Monitoring → Analysis (model) → Output checks → Delivery policy → human approval → Slack.

What changed from 0.1 and why:

1. **The pipeline is declared, not hard-coded.** 0.1 checked that the builder's blocks were present, then ran a fixed `if template == ...` function. Each template is now a list of steps, each bound to a block ID. The required blocks come from that list, and every trace entry records its block and duration in milliseconds.
2. **Sample and live runs share one code path.** 0.1 had six `if demo` branches, so sample runs never exercised the prompt. Sample mode now plugs in a sample monitoring adapter and a deterministic template model. The same steps, prompts and output checks run in both modes.
3. **Model output is checked before review.** The reviewer used to be the only control between the model and Slack. `guards.py` now blocks drafts that cite runbooks not mapped to the asset, claim an action was taken, assert a root cause or state an outage probability. A blocked draft becomes `evaluation_only` and cannot be sent; the reasons are shown to the reviewer.
4. **Evals for the model step.** `evals/run_evals.py` runs 8 cases on fixed evidence: 4 incident briefs and 4 forecast explanations, including prompt injection in an alert and in a runbook, and a series that must fail the baseline gate. Incident briefs are graded on output checks, owner, service, runbook citation and stated uncertainty. Forecasts are graded on output checks, delivery status, threshold crossing, runbook citation and a suggested check. Run it against your local model before trusting a new model or prompt.
5. **Provenance on every run.** Each result records the template, model, monitoring adapter, prompt version and any output-check violations.
6. **Explicit delivery state machine.** Runs move only forward: `awaiting_approval → sending → sent | delivery_unknown`. Database connections are now closed after use.
7. **Smaller fixes.** The triage template no longer requires forecast settings. A malformed local-model response gives a clear error instead of a `KeyError`. Errors name the step that failed (for example `SSOT: ...`). Sample drafts no longer quote free-text alert messages, which may contain injected instructions.

The model still never chooses steps, calls tools or picks recipients. Deliberately, this is a workflow with a model in it, not an autonomous agent: the steps are known in advance and a wrong action is costly.

## Diagrams (preview)

An agent can be described as a **diagram**: blocks, **attachments** (resources an agent may use) and **flow wires** (what happens in order). The runtime validates the diagram, then runs it.

**New agent** in the studio starts from the job, not the canvas: the engineer picks the kind of job (investigate an alert, regular report, review before a change, answer when asked), writes it in one sentence, and ticks what the agent may read and where the result goes. `bya/graph/builder.py` turns those answers into a diagram by fixed rules (no model involved): output check, approval and limits always included, instructions drafted from the sentence, and three starter eval cases. `POST /api/builder/preview` returns the diagram and its reach while the form is filled in.

**Diagram Studio** (`http://127.0.0.1:8787/studio.html`, or **Diagram studio** in the left menu) is the canvas for this:
- Add blocks from the palette by clicking or dragging.
- Drag from a block's right-hand port to create a flow wire, or from an agent's bottom port to attach a tool.
- Edit settings in the right-hand panel.
- Run the diagram and approve the draft in the run panel.

The canvas refuses connections the rules never allow, and says why (for example, an agent wired straight to an output). Every other problem is listed below the canvas and marked on its block. Diagrams save to `diagrams/`, and can be imported and exported as JSON.

```
[Trigger] ──▶ [Agent] ──▶ [Output check] ──▶ [Human approval] ──▶ [Output]
                 ┊
        attached: tools (built-in, HTTP, MCP)
```

| Category | Block types |
|---|---|
| Trigger | `trigger.manual`, `trigger.webhook`, `trigger.alert` (sample or PRTG) |
| Agent | `agent`: instructions, model endpoint, `max_steps`, `token_budget`, `timeout_s` |
| Tool (attached) | `tool.builtin` (calculator, time, asset lookup, runbook search, metric forecast, config check), `tool.device` (read-only commands on network devices), `tool.http`, `tool.mcp` (local MCP servers over stdio) |
| Memory (attached) | `memory.kv` (remember/recall facts across runs), `memory.conversation` (inputs and results of the last completed runs), `memory.documents` (keyword search over `knowledge/`) |
| Guardrail | `guard.policy` (attached: denied tools, tools that always need approval, max tool calls), `guard.redact` (removes secrets, and optionally emails and IPs, from the draft), `guard.output_check`, `guard.approval` |
| Output | `output.file`, `output.webhook`, `output.slack` |

**Templates** (in `diagrams/`, offered under **Start from…**), each with three eval cases:

| Template | Flow | Notes |
|---|---|---|
| Incident brief | alert → agent → output check → approval → file | Asset lookup and runbook search; cites only runbooks the tools returned |
| Capacity forecast | request → agent → output check → approval → file | `metric_forecast` backtests against a seasonal-naive baseline and returns a one-sentence summary (crossing and baseline verdict) that the agent repeats; run history memory compares with earlier runs. Sample mode uses a trend baseline; live mode uses TimesFM and never falls back |
| Config review | file name → agent → redact → output check → approval → file | `config_lint` reads one file from `configs/`, checks 10 IOS-style rules, masks secrets and returns a ready-made report that the agent must repeat verbatim; document search explains each rule from `knowledge/config-standards.md`; the output check blocks a "fully compliant" claim |
| Interface check | alert → agent → output check → approval → file | Reads the alerting device with `device_command` (interface counters, logs) in addition to the asset register and runbooks. Sample mode replays `lab/` recordings |

The config rules and the standard are samples: replace `knowledge/config-standards.md` with your own standard, and put configuration backups in `configs/`.

Why the tools return ready-made text: in testing, a 3B model dropped findings, inverted a fix and misread a baseline comparison when it had to interpret raw tool output. Deterministic conclusions come from the tool; the model explains them.

**The validator refuses to run a diagram that breaks a safety rule:**
- exactly one trigger, no loops in the flow
- only compatible blocks can be wired together
- every output comes after an output check and then a human approval
- write tools always need approval for each call
- every agent has step, token and time limits
- credentials appear only as environment-variable names
- no sample data in live mode

Every problem names the block it belongs to.

**While it runs:**
- Tool results are passed to the model as data, never as instructions.
- A write tool that isn't approved is refused.
- The output check can require that runbook citations come only from what the tools actually returned.
- Every step and tool call is recorded in the trace.

**Try it:**

```sh
python -m bya.graph validate diagrams/incident-brief.json
LLM_MODEL=llama3.1:8b python -m bya.graph run diagrams/incident-brief.json   # approvals asked in the terminal
```

**Approvals:**
- In the studio, a call to a write tool (or to any tool the permission policy names) pauses the whole run. The agent resumes exactly where it stopped once you decide.
- **Inbox** in the top bar lists everything waiting across runs: tool calls with their arguments, and drafts.
- A denied tool call doesn't end the run; the agent is told and continues.
- Time spent waiting for a person doesn't count against the agent's timeout.
- In the terminal, you're asked per call instead.

**Memory:**
- Memory stays local, in `bya.sqlite3`, and is bounded.
- Run history is written only for runs that completed, so blocked, rejected or failed drafts are never remembered.
- Whatever is read back from memory is passed to the model as data.

**Evals:**
- Test cases live in the diagram file (`"evals"`), so they travel with it. **Evals** in the top bar edits and runs them.
- A case gives an input or an alert, and what the draft must satisfy: `status`, `contains`, `not_contains`, `tools_called`, `tools_not_called`, `cites_any`.
- Evals never act: every run stops at the first approval, write tools are denied, and memory and outputs are isolated per case.
- Run each case up to 5 times to see how stable a model is.

**Export to Python:**
- **Export Python** downloads the diagram as one readable script: the settings, the flow as straight-line code, and a `run()` function.
- The script uses the same runtime library, guardrails and approvals. It asks for approvals in the terminal.
- Tick **Also test the Python export** in the evals panel to run the same cases against the script and confirm it behaves like the diagram.

```sh
python -m bya.graph eval diagrams/incident-brief.json --export      # diagram and export, same cases
python -m bya.graph export diagrams/incident-brief.json -o agent.py
python agent.py                                                      # needs BYA_RUNTIME if moved elsewhere
```

**Reading devices safely (`tool.device`):**
- The model never holds a device session. It asks for one command on one device through the `device_command` tool, and a command filter decides.
- A command runs only if the device is in the block's scope, the command starts with `show`, `display`, `ping` or `traceroute` written in full, and it matches one of the block's allow patterns (for example `show interfaces *`).
- Separators, redirects and control characters (`;`, `&`, `>`, `$`, newlines) are refused, and output pipes are limited to read-only filters (`include`, `exclude`, `begin`, `section`, `count`). Words such as `configure`, `reload`, `write`, `copy`, `delete`, `debug`, `clear` and `redirect` are always refused. Abbreviations such as `sh` or `conf t` are refused.
- A refused command is shown as **DROP** in the run trace; the agent is told why and carries on.
- Scope comes from the asset register (`ssot.json`), NetBox (`/api/dcim/devices/` filtered by site, role or tag) or a fixed list. The validator refuses an empty filter, a wildcard allowlist, and write access.
- Passwords, secrets, keys and SNMP communities are masked in device output before the model sees it. Each run has a command budget, and live mode paces commands per device.
- Sample mode never contacts a device: it replays `lab/<device>/<command>.txt`. Live mode runs `ssh -T -o BatchMode=yes -- <device> <command>` with the jump host's own OpenSSH keys, config and `known_hosts`; host keys stay checked and no shell is involved on this side.
- The filter is built for network OS command lines (IOS, IOS-XE, NX-OS, EOS, Junos, VRP). On Linux-based devices the SSH command runs in a remote shell, so also give BYA an account that is read-only on the device (a restricted shell or an SSH forced command). Use a read-only device account everywhere: it is the second layer if the filter ever misses something.

**Reach** in the top bar, or `python -m bya.graph reach <diagram>`, lists everything a diagram's agents could touch before it runs: devices and allowed commands, read tools, change paths (each approved), memory, outputs, and whether prompts go to a remote model.

Current limit: MCP servers must be local (stdio); remote MCP isn't supported yet.

**Imported diagrams can start local programs.** An MCP block runs its command when the diagram runs, and the studio shows that command on the block's settings panel. Only run diagrams and MCP servers you trust.

## Start

1. Install Python 3.11 or newer. Extract this ZIP to a writable directory.
2. In that directory run `python server.py` (on some systems, `python3 server.py`).
3. Open `http://127.0.0.1:8787` in your browser. Keep the terminal open.
4. Use Download agent → Import agent JSON to load your exported configuration, or use either built-in template.
5. Run Sample data first. These examples are synthetic and **do not use a language model or TimesFM**.

No Python dependencies are needed for the builder, sample runs, PRTG HTTP connector, Slack delivery or an existing local model endpoint.
Live mode needs the following one-time configuration. An IT administrator should perform it for nontechnical users.

## Configure live connections

Copy `config.example.json` to `config.local.json` and edit only on the local machine.
Keep credentials in environment variables. Never put them in agent JSON, SSOT JSON or a shared ZIP.
Restart the runtime after configuration changes so the interface reflects readiness.

### Local open-weight language model

Provide an installed model via an OpenAI-compatible chat completions endpoint.
Examples of endpoint forms: Ollama `http://127.0.0.1:11434/v1`; vLLM `http://127.0.0.1:8000/v1`.
Set `model` to the exact served model ID; there is no automatic model download, model selection or cloud fallback.
Choose a model whose licence and hardware requirements fit your use. Local open-weight does not automatically mean unrestricted redistribution.

The model can refine agent instructions in the builder, summarize incident evidence, and explain forecast verification steps.
It does not choose recipients, execute commands, edit SSOT records, or bypass approvals.
Changing the natural-language purpose affects the model's instructions; the two templates' execution topology remains fixed and validated.

### SSOT — authoritative identity and ownership

Use Source of truth → Export JSON to see the schema, then import your reviewed asset inventory or replace `ssot.json`.
Each asset needs stable ID, name, service, owner, source, timezone-qualified `verified_at`, and sensor mappings.
Each mapping needs a **unique PRTG sensor ID**, exact channel caption, and measurement unit.
Add `runbook_ids` and replace the sample procedures in `runbooks.json` with approved content.
Set `sample` to false only after replacing and verifying sample records with real facts.

Records older than 30 days, dates more than five minutes in the future, duplicate IDs, missing owners and unmapped sensors block execution.
There is no fuzzy hostname matching or automatic authority assignment. Monitoring observations never overwrite intended state.
Nautobot/NetBox may be the upstream authority; use a reviewed export transformed into this schema.
Direct synchronization with these systems, conflict reconciliation, and richer dependency graphs are not implemented yet.

### PRTG

Set `prtg_url` to the HTTPS origin, `prtg_timezone` to the PRTG server's IANA timezone,
and `PRTG_API_TOKEN` in the runtime process environment to a read-only API token.
The adapter uses classic API `table.json` and `historicdata.json` endpoints with `apitoken` authentication.
It reads raw numeric channel values via `<exact channel caption>_raw` and `datetime_raw`.
Confirm the field names and date convention on your PRTG deployment before production use.
It refuses redirects, incomplete sampling and ambiguous sensor identity. Certificates must validate; no insecure TLS mode.

History reads are limited to one every 12 seconds (5/minute). Default history is 72 hours; configurable from 12 to 168 hours.
Forecasting rejects missing buckets, unordered samples, nonfinite values, stale data and declared maintenance periods.
The PRTG adapter does not yet fetch maintenance calendars; exclude such periods upstream and review inputs.
Do not use raw cumulative counters without converting to a valid rate or gauge upstream.
Each template currently reads one sensor/channel per run; no fleet polling scheduler is included.
PRTG history timestamps are converted from the server timezone. Around daylight-saving changes the local clock repeats or skips an hour, so a run spanning the change is blocked by the time-bucket check instead of guessing. Confirm on your deployment whether `datetime_raw` is local or UTC.

### TimesFM 2.5 — metric forecasting

Create a Python virtual environment if desired, then install optional requirements:

```sh
python -m pip install -r requirements-forecast.txt
```

Download the `google/timesfm-2.5-200m-pytorch` checkpoint through your approved process and set
`BYA_TIMESFM_PATH` to its local directory. The runtime does not silently download weights or substitute a baseline.
If using a fully offline machine, stage both Python wheels and the checkpoint beforehand. Set `HF_HUB_OFFLINE=1`.

The adapter loads local TimesFM 2.5, forecasts numeric series, retains q10–q90 output, and runs three chronological holdouts.
It compares MAE with a seasonal-naive baseline. A forecast that does not outperform that baseline cannot be delivered externally.
The gate is an initial validation check, not evidence of general forecasting accuracy. Evaluate more windows and real incidents before operational use.
Prediction intervals are uncalibrated until evaluated on your data. A threshold crossing is **not** an outage probability or root-cause finding.
Forecast horizon: 1–96 samples. Default: 24 at 300 seconds (two hours). Default baseline: 288 samples (one day).
At least `max(period,48) + 3*min(horizon,12)` complete samples are required. Local model context is capped at 2048.
For decreasing capacity such as free disk space, select Below threshold and use the mapped channel's units.

Google's repository states that code and weights through 2.5 are Apache-2.0, while the current 3.0 pretrained weights have separate non-commercial/non-production terms.
This package contains no model weights and uses 2.5. Review any replacement checkpoint's terms independently.

### Slack delivery

Set `SLACK_BOT_TOKEN` in the local process environment and `slack_channel` in local configuration.
Use a bot with the required posting permission and membership in the one approved destination.
A run creates a stored draft. Approve & send posts that exact stored text, never arbitrary browser-provided text.
Approval expires after one hour. Sample runs and failed forecasting evaluations cannot send.
Delivery attempts are recorded before the request. Ambiguous failures are not retried automatically; check Slack before any new run.
This is designed for one trusted local operator. It does not implement enterprise multiuser authorization.

## Storage and security boundaries

- Server binds to loopback only. Host allowlist, per-process session token and Origin checks protect write endpoints.
- Secrets are environment-only; no connector credential inputs exist in the hosted interface.
- Browser agent drafts are local to that browser; export them to move machines.
- SSOT and runbooks are local JSON files. Run evidence and delivery states are recorded in `bya.sqlite3`.
- The runtime serves only `web/`, never its configuration, database or source directory.
- Local model calls use the endpoint set by the operator. HTTPS is required for remote endpoints; loopback HTTP is supported.
- No shell/SSH tools, automatic remediation, arbitrary plugins, background telemetry uploads or default cloud model calls.
- Logs and run history may contain sensitive operational data. Protect the directory with OS permissions. Encryption at rest, backup automation and retention jobs are not included.

## Test and limitations

Run `python -m unittest discover -s tests -v`.

Evaluate model output: `python evals/run_evals.py` (sample model) or `python evals/run_evals.py --model local` (your configured model). Add `--only triage` or `--only forecast` to run one template. Add cases from real, reviewed incidents to `evals/cases.json`. The staged live-test checklist is in [`../TESTING.md`](../TESTING.md).
Tests use synthetic data and mocked external responses. No Slack messages are sent.
The PRTG, Slack, local LLM and TimesFM integrations must be verified against your own endpoints and installed weights.
This build environment did not have customer credentials or installed model weights, so live end-to-end inference was not verified.
This release has guided template graphs, node movement, configuration, import/export, local persistence and local execution.
It is not a general arbitrary-DAG runtime, desktop installer, OAuth onboarding service or complete autonomous NOC platform.

## Primary references

- https://github.com/google-research/timesfm
- https://github.com/google-research/timesfm/blob/master/timesfm-forecasting/references/api_reference.md
- https://www.paessler.com/manuals/prtg/http_api
- https://www.paessler.com/manuals/prtg/historic_data
- https://docs.slack.dev/reference/methods/chat.postMessage

## Next implementation milestones

1. Validate live PRTG field names, channel units and timezones with a representative export.
2. Connect one real SSOT asset and one local model; verify the incident draft against approved runbooks.
3. Benchmark TimesFM on several weeks of held-out telemetry; measure false alerts and useful warning lead time.
4. Add authenticated SSOT synchronization, background scheduling and a signed desktop installer.
