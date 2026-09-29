# Build your first agent in 30 minutes

You draw the agent as a diagram, test it, connect one of your own tools, and export it as Python.
Everything runs on your machine. Nothing leaves it unless you add an output and approve a draft.

You need Python 3.11+ and a model endpoint that speaks the OpenAI chat API: local (Ollama, vLLM, llama.cpp) or hosted.
Pick a model with tool calling. In testing, Qwen2.5 3B passed 26 of 27 template eval runs but still misreads numbers now and then; use a 7B–8B model if you can.

## 1. Start the studio (2 minutes)

```sh
cd bya-runtime
python server.py
```

Open `http://127.0.0.1:8787/studio.html`.

## 2. Start from a template (3 minutes)

**Start from…** offers four templates. Each one is read-only and ends in a human approval.

| Template | Starts from | Tools | What it writes |
|---|---|---|---|
| Incident brief | A monitoring alert | Asset lookup, runbook search | What is observed, who owns it, read-only checks with runbook ids |
| Capacity forecast | A request, for example "sensor 1001 against 80 %" | Metric forecast, asset lookup, runbook search; remembers its last runs | When a metric may cross a threshold, and whether the forecast beat a seasonal baseline |
| Config review | A config file name, for example `sample-branch-edge.cfg` | Config check, document search over your standard | Findings by severity with rule ids and fixes; secrets redacted |
| Interface check | A monitoring alert | Device commands (read-only `show` commands through a filter), asset lookup, runbook search | What the device shows, quoted with the command it came from |

The status in the top bar says **Ready to run** when the diagram passes every safety rule.

## 3. Point it at your model (2 minutes)

Click the **Agent** block. In the settings panel, set **Model endpoint** (for example `http://127.0.0.1:11434/v1` for Ollama) and **Model**.
For a hosted endpoint, put the API key in an environment variable and enter only its name under **API key variable**. The validator refuses keys typed into a diagram.

## 4. Run it (3 minutes)

Click **▷ Run**. The run panel shows every step and tool call. The run stops at **Human approval** and shows the draft.
Approve it and the output block writes the file; reject it and nothing is written.

## 5. Test it (5 minutes)

Click **Evals**. Each template has three cases, including one that tries to trick the model (an instruction hidden in an alert or a config comment).
Click **Run evals**. A failing case lists the check that failed.

Add a case from your own work: **Add case**, give it an input, and say what the draft must and must not contain.
Evals never act: runs stop at the first approval, write tools are denied, and memory is isolated.

## 6. Connect one of your own tools (10 minutes)

Pick a tool you already have. Two ways to connect it:

- **HTTP tool:** any JSON API, for example NetBox (`GET https://netbox.example.com/api/dcim/devices/?name={name}`).
  Headers come from environment variables: `{"Authorization": "NETBOX_AUTH"}`, with `NETBOX_AUTH` set to `Token <your token>`.
- **Device commands:** read-only `show` commands on your devices over SSH. Set the device filter (asset register, NetBox site/role/tag, or a list) and the allowed commands, then open **Reach** to check exactly which devices are in scope. Sample mode replays `lab/` recordings; live mode uses your SSH keys and config.
- **MCP server:** any local MCP server over stdio, for example a Git or ticketing server.
  List the tools that change something under **Write tools**; each call to one pauses the run until you allow it in the **Inbox**.

Drag the block onto the canvas, then drag from the agent's bottom port to the tool to attach it.
Mention the tool in the agent's instructions, run the evals again, and add a case that needs the new tool (**Tools that must be called**).

A tool that can change something must be marked **write**. The validator won't run a diagram that could call it without approval.

## 7. Export it (2 minutes)

Tick **Also test the Python export** and run the evals: the report says whether the exported script behaves like the diagram.
Then click **Export Python**. You get one readable script with a `run()` function, the same guardrails, and approvals in the terminal:

```sh
PYTHONPATH=. BYA_RUNTIME=. python ~/Downloads/incident-brief.py   # run from bya-runtime/
```

The script is yours to edit, version and schedule.

## What stops a mistake

- The canvas refuses a wire from an agent straight to an output: every output needs an output check and a human approval in front of it.
- Every agent has step, token and time limits.
- Tool results, memory and documents reach the model as data, never as instructions.
- The output check blocks drafts that claim an action, assert a root cause, state an outage probability or cite a runbook no tool returned.

## Tell us how it went

If you're piloting BYA, note how long each step took and where you got stuck. That feedback decides what gets built next.
Open an issue, or reply to the person who sent you this guide.
