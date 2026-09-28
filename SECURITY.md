# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: **Security → Report a vulnerability** on this repository.
Include what you found, how to reproduce it, and the impact you expect.

You will get an acknowledgement within 5 working days. This is a small project maintained in spare time;
there is no bug bounty.

## Scope

In scope:

- `bya-runtime/`: the local server, its session/origin checks, the approval and delivery flow,
  output checks, and handling of PRTG, local-model and Slack credentials.
- `examples/`: the LLM client and example agent.

Especially relevant:

- A way to send a Slack message without human approval, or to send different text than was approved.
- A way for a web page, alert text or runbook text to make the runtime act (prompt injection that bypasses the output checks).
- Credential exposure: tokens in logs, responses, exports or the web UI.
- Reaching the runtime from outside the local machine.

## Status

This is pre-1.0 software. `bya-runtime` is a reference implementation for a single trusted operator on one machine.
It has no multi-user authorization and no encryption at rest. Do not expose it to a network.
