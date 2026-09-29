# Test Checklist

Test in this order. Each stage adds one new dependency, so a failure points to one cause.
Stop at the first failing stage and fix it before moving on.

| Stage | Tests | You need | Time |
|-------|-------|----------|------|
| 1 | Code, evals, web UI with sample data | Python 3.11+ | 10 min |
| 2 | Example agent against a real model | Any OpenAI-compatible endpoint: local (Ollama, vLLM) or hosted with an API key | 15 min |
| 3 | Your local model's drafts | Ollama or vLLM with an installed model | 1 hour |
| 4 | PRTG connection | Read-only PRTG API token, one real asset | Half a day |
| 5 | Slack approval flow | Slack bot token, one **test** channel | 1 hour |
| 6 | TimesFM forecasting (optional) | PyTorch, TimesFM 2.5 checkpoint, 3+ days of clean history | Half a day |
| 7 | Read one real device (read-only SSH) | One lab or non-critical device, a read-only SSH account | 1 hour |

Commands are for macOS/Linux. On Windows PowerShell, replace `export NAME=value` with `$env:NAME="value"`.
All BYA commands run from the `bya-runtime/` directory.

---

## Stage 1 — Code, evals and UI with sample data

```sh
cd bya-runtime
python -m unittest discover -s tests -v
python evals/run_evals.py
python server.py
```

Open `http://127.0.0.1:8787` and keep the terminal open.

- [ ] All tests pass (100 at the time of writing).
- [ ] Evals: 8/8 pass (4 incident, 4 forecast).
- [ ] **Incident brief** template → **Run test** with *Sample data*: the trace shows SSOT, Knowledge, Monitoring, Analysis, Output checks, Delivery policy.
- [ ] **Capacity forecast** template → **Run test**: a chart appears and the draft starts with `[SAMPLE / trend baseline]`.
- [ ] **Approve & send** is blocked with "Sample drafts cannot send messages."
- [ ] **Source of truth** view lists the 3 sample assets.
- [ ] Open `http://127.0.0.1:8787/studio.html`. Choose **Start from… → Incident brief**; the status shows **Ready to run**.
- [ ] Drag from the agent's right-hand port to the **Save to file** block. The canvas refuses, and explains that an output needs a check and an approval first.
- [ ] Set the agent's model endpoint to your model, open **Evals**, tick **Also test the Python export** and click **Run evals**. Each case shows pass or fail per check, and the export line says whether it matches the diagram.
- [ ] **Export Python** downloads `incident-brief.py`. From `bya-runtime/`, `PYTHONPATH=. BYA_RUNTIME=. python ~/Downloads/incident-brief.py` runs it with approvals in the terminal.
- [ ] **Start from… → Capacity forecast**, **Config review** and **Interface check** each show **Ready to run**. With your model set on the agent, each passes its evals (3/3).
- [ ] **Interface check → Reach** shows 2 devices readable, 0 change paths and 0 device config sessions.

Stop the server with `Ctrl+C`.

---

## Stage 2 — Example agent against a real model

The example uses only the Python standard library. Point it at any OpenAI-compatible endpoint whose model supports tool calling:

```sh
cd examples
export LLM_BASE_URL=http://127.0.0.1:11434/v1   # Ollama; or your provider's /v1 URL
export LLM_MODEL=llama3.1:8b                     # exact model ID
export LLM_API_KEY=...                           # hosted providers only
python agent.py
```

- [ ] JSON trace lines appear for `model_call` and `tool_call` events.
- [ ] The agent calls `get_interface_status` and `search_runbooks` before concluding.
- [ ] If it proposes `restart_interface`, you get an `[APPROVAL]` prompt. Answer `N` and confirm it continues without restarting.
- [ ] It ends with a summary, evidence, recommended action and confidence, within 8 steps.

If the model answers without calling any tool, it probably does not support tool calling. Try another model before changing the code.

---

## Stage 3 — Your local model

Install [Ollama](https://ollama.com) (or run vLLM), then pull a model that supports chat. Example:

```sh
ollama pull llama3.1:8b
curl -s http://127.0.0.1:11434/v1/models
```

The `curl` output lists the exact model ID to use. Configure the runtime:

```sh
cd bya-runtime
cp config.example.json config.local.json
# edit config.local.json: set "model" to the exact ID from the curl output
python evals/run_evals.py --model local
```

- [ ] All 8 cases pass. If not, note which checks fail:
  - `passes_output_checks` fails on a `prompt-injection-*` case → the model followed injected text. Treat this as a hard fail for that model.
  - `cites_mapped_runbook` or `states_uncertainty` fails → try a stronger model before editing the prompt.
- [ ] Run it 3 times. A model that passes only some runs is not reliable enough.
- [ ] Record the model ID and results in the log at the bottom of this file.

Optional: start `python server.py`, switch *Run mode* to **Live**, and use the builder's instruction drafting to check the `/api/assist` path.

---

## Stage 4 — PRTG connection

**4a. Confirm the raw field names before touching the runtime.** Use a real sensor ID (here `1234`):

```sh
export PRTG_API_TOKEN=...          # read-only token
export PRTG=https://prtg.example.internal

curl -s "$PRTG/api/table.json?content=sensors&columns=objid,device,sensor,status,message,lastvalue&filter_objid=1234&apitoken=$PRTG_API_TOKEN" | python -m json.tool

curl -s "$PRTG/api/historicdata.json?id=1234&avg=300&sdate=2026-09-27-00-00-00&edate=2026-09-28-00-00-00&usecaption=1&apitoken=$PRTG_API_TOKEN" | python -m json.tool | head -40
```

- [ ] `table.json` returns exactly one sensor.
- [ ] Each `histdata` row has `datetime_raw`, `coverage_raw` and `<Exact Channel Caption>_raw`, e.g. `Traffic Total_raw`.
- [ ] Note the exact channel caption and unit. Find out whether `datetime_raw` is server-local time or UTC.

**4b. Configure the runtime.**

- In `config.local.json` set `prtg_url` (HTTPS origin only) and `prtg_timezone` (IANA name, e.g. `Europe/Berlin`).
- In `ssot.json`, replace one sample asset with the real one:
  - `sample: false`
  - `verified_at`: now, with a timezone, e.g. `2026-09-28T10:00:00+00:00`
  - `sensors[0].id`, `channel` and `unit` exactly as seen in 4a
  - `runbook_ids` pointing to entries in `runbooks.json`

Then:

```sh
python -m unittest discover -s tests   # confirm nothing broke
python server.py
```

In the UI, set the sensor ID and run **Incident brief** in **Live** mode.

- [ ] The trace shows the real alert message; the draft names the real owner and service.
- [ ] **Negative checks.** Each one must block the run with a clear message, not produce a draft:
  - [ ] a sensor ID not in `ssot.json`: "no unique SSOT mapping"
  - [ ] a wrong channel caption in `ssot.json`: "does not match PRTG raw fields" (forecast template)
  - [ ] `verified_at` older than 30 days: "stale"
  - [ ] two forecast runs within 12 seconds: "limited to one every 12 seconds"
  - [ ] a forecast run before Stage 6: "TimesFM is not installed". This confirms there is no silent fallback.

---

## Stage 5 — Slack approval flow

Create a Slack app with the `chat:write` bot scope, install it, and invite it to a **test** channel (`/invite @your-bot`).
Copy the channel ID from the channel details.

```sh
export SLACK_BOT_TOKEN=xoxb-...
# config.local.json: set "slack_channel" to the test channel ID
python server.py
```

Run a live **Incident brief**, then **Approve & send**.

- [ ] The Slack message matches the draft in the UI exactly.
- [ ] Clicking **Approve & send** again is refused ("already processed").
- [ ] **Failure path:** set `slack_channel` to an invalid ID, restart, run and approve. The UI reports "Delivery failed or is uncertain" and nothing is retried.
- [ ] **Expiry (optional):** wait 61 minutes after a run, then approve. It is refused with "Approval expired".

Check recorded delivery states:

```sh
python -c "import sqlite3; c=sqlite3.connect('bya.sqlite3'); [print(r) for r in c.execute('select id, created, state from runs order by created desc limit 5')]"
```

- [ ] States read `sent`, `delivery_unknown` or `evaluation_only`. None is left in `sending`.

---

## Stage 6 — TimesFM forecasting (optional)

```sh
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-forecast.txt
pip install -U "huggingface_hub[cli]"
huggingface-cli download google/timesfm-2.5-200m-pytorch --local-dir ~/models/timesfm-2.5
export BYA_TIMESFM_PATH=~/models/timesfm-2.5
python -c "import timesfm; print(timesfm.TimesFM_2p5_200M_torch)"
python server.py
```

Pick a gauge sensor (not a raw counter) with at least 3 days of complete 5-minute history, then run **Capacity forecast** in **Live** mode.

- [ ] The trace shows "Local TimesFM 2.5" and the chart shows q10–q90 bands.
- [ ] The metrics show holdout MAE against the seasonal baseline MAE.
- [ ] If TimesFM does not beat the baseline, the draft says delivery is blocked and **Approve & send** is disabled.

What this stage does **not** prove: that the forecasts are useful. That needs several weeks of held-out data and real threshold events.

**Clock change:** clocks in Europe go back on 25 October 2026. A forecast run in the 7 days after that includes the repeated hour. Expected result: blocked with "Duplicate, unordered or missing time buckets", not a forecast.

---

## Stage 7 — Read one real device (read-only SSH)

Use a lab or non-critical device and an account that is read-only on the device itself (privilege level 1, or a read-only role). BYA's filter is one layer; the device account is the second.

```sh
# 1. SSH works non-interactively from this machine (keys, known_hosts):
ssh -T -o BatchMode=yes <device> "show clock"

# 2. Point the Interface check template at it: in the studio, select the Device commands block and set
#    Devices from = list, Devices = <device>, SSH user variable = BYA_SSH_USER (or leave empty to use ~/.ssh/config).
export BYA_SSH_USER=<read-only user>

# 3. Check the reach before running:
python -m bya.graph reach diagrams/<your-copy>.json --live
```

- [ ] Step 1 prints the time without asking for a password or a host-key confirmation.
- [ ] Reach lists exactly your one device, the allowed commands, and 0 device config sessions.
- [ ] A live run shows each command in the trace as `<device># show …`, with real output in the draft's evidence.
- [ ] Ask the agent (in the alert text or the instructions) to "clear the counters". The trace shows the command as **DROP** and the device's counters are unchanged (`show interfaces` before and after).
- [ ] `show running-config | include snmp` returns `community ****`; the real community never appears in the trace or the draft.
- [ ] Your device's own AAA or syslog shows only `show` commands from the BYA account.

---

## Results log

| Date | Stage | Model / system | Result | Notes |
|------|-------|----------------|--------|-------|
| | 1 | sample | | |
| | 2 | Example agent | | |
| | 3 | | /8 | |
| | 4 | PRTG | | |
| | 5 | Slack | | |
| | 6 | TimesFM 2.5 | | |
