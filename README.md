# How to Build an AI Agent

[![CI](https://github.com/eduardd76/how_to_build_an_agent/actions/workflows/ci.yml/badge.svg)](https://github.com/eduardd76/how_to_build_an_agent/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Building an agent is easy. Knowing it works and knowing it can't cause harm is the real job.

This repository has two parts:

- **[The guide](GUIDE.md):** 13 phases from "do you even need an agent?" to production, with runnable examples. It covers the steps most roadmaps skip: stop conditions, tool design, evaluation and guardrails.
- **[BYA runtime](bya-runtime/):** a worked case study. A local operations assistant turns a PRTG alert into an evidence-backed incident brief or a capacity forecast, checks the model's output, and sends it to Slack only after a human approves.

All code is provider-neutral: it works with any OpenAI-compatible endpoint, local (Ollama, vLLM) or hosted.

## Status

| Part | State |
|------|-------|
| Guide and examples | Complete. Examples tested against a scripted endpoint in CI |
| BYA runtime | **Pre-1.0 reference implementation.** Tested on synthetic data (unit tests and evals). Not yet validated against live PRTG, Slack or TimesFM. See [`TESTING.md`](TESTING.md) |

## Quick start

Requires Python 3.11+. No dependencies for the examples or the base runtime.

**Example agent** against a local model:

```sh
ollama pull llama3.1:8b
export LLM_MODEL=llama3.1:8b
python examples/agent.py
```

**BYA runtime** with sample data (no model or credentials needed):

```sh
cd bya-runtime
python server.py        # open http://127.0.0.1:8787
```

**Draw your own agent:** with the runtime running, open `http://127.0.0.1:8787/studio.html`. Start from a template (incident brief, capacity forecast, config review) or drag blocks onto the canvas, connect them, test them with evals, and export the result as Python. The canvas won't let you wire an output without an output check and a human approval in front of it.

New here? **[Build your first agent in 30 minutes](FIRST_AGENT.md)**, including connecting one of your own tools.

**Tests and evals:**

```sh
python -m unittest discover -s examples
cd bya-runtime && python -m unittest discover -s tests && python evals/run_evals.py
```

## Repository map

```
GUIDE.md            The 13-phase guide
FIRST_AGENT.md      Build, test and export your first agent in the studio
TESTING.md          Staged checklist: sample data → real model → PRTG → Slack → TimesFM
examples/
  llm.py            Minimal OpenAI-compatible client and agent loop (stdlib only)
  agent.py          Complete example agent: approval gate, limits, tracing, error recovery
bya-runtime/
  bya/              Pipeline, adapters, output checks, forecasting, delivery state machine
  bya/graph/        Diagram runtime: validator, executor, built-in/HTTP/MCP tools
  diagrams/         Templates: incident brief, capacity forecast, config review
  knowledge/        Local documents for the document-search memory block
  configs/          Device configurations for the config check (samples included)
  evals/            Fixed-evidence evals, including prompt-injection cases
  tests/            Unit and HTTP tests
  web/              Local builder UI
```

## Contributing and security

Live test results and real (anonymised) eval cases are the most useful contributions right now. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
Report security issues privately; see [`SECURITY.md`](SECURITY.md).

## License

[Apache License 2.0](LICENSE). Copyright 2026 Eduard Dulharu.

TimesFM model weights are not included. If you install them, review their license separately; see [`bya-runtime/README.md`](bya-runtime/README.md).
