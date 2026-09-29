"""Export a diagram as a readable Python script.

The script owns the flow and the agent loop as plain code you can edit. Tool, check and memory
implementations are imported from the `bya` package (Apache-2.0), which you can vendor.
It exposes `run(...)` with the same result shape as the diagram runtime, so the same evals grade both.
"""
import datetime as dt
import pprint
import re

from .validator import validate

HELPERS = r'''

# ---------------------------------------------------------------------------
# Building blocks. Edit freely: this file is yours.
# ---------------------------------------------------------------------------

class Blocked(Exception):
    """An output check stopped the flow."""


class Stop(Exception):
    """The flow ends here (waiting for approval, or rejected)."""


class Context:
    """Settings the tool implementations read (asset register, runbooks, memory and knowledge folders)."""

    def __init__(self, mode, memory_path, knowledge_dir):
        self.mode = mode
        self.ssot = json.loads((RUNTIME / 'ssot.json').read_text())
        self.runbooks = json.loads((RUNTIME / 'runbooks.json').read_text())
        self.memory_path = memory_path
        self.knowledge_dir = knowledge_dir
        self.configs_dir = RUNTIME / 'configs'
        self.lab_dir = RUNTIME / 'lab'
        self.monitoring = None


def trace(state, block_id, detail, **extra):
    state['trace'].append({'block': block_id, 'detail': detail, **extra})


def call_tool(state, agent_id, tools, policy, call, approve_tool):
    fn = call.get('function', {})
    name, tool, status = fn.get('name', ''), None, 'ok'
    try:
        args = json.loads(fn.get('arguments') or '{}')
        if not isinstance(args, dict):
            raise ValueError('Tool arguments must be a JSON object.')
        tool = tools.get(name)
        if tool is None:
            result, status = f'Error: unknown tool "{name}". Use only the tools provided.', 'error'
        elif name in policy.get('deny_tools', []):
            result, status = f'Denied by policy: "{name}" is not allowed. Do not retry.', 'denied'
        elif (tool.access == 'write' or name in policy.get('require_approval_tools', [])) and not (
                approve_tool and approve_tool(agent_id, name, args)):
            result, status = f'Denied: "{name}" was not approved. Do not retry; report instead.', 'denied'
        else:
            try:
                result = str(tool.call(args))
            except CommandRefused as e:  # the command filter said no; the agent may try another command
                result, status = f'Refused by the command filter: {e}', 'dropped'
            except Exception as e:  # the model gets the error and can recover
                result, status = f'Error: {e}', 'error'
    except (json.JSONDecodeError, ValueError) as e:
        result, status = f'Error: invalid arguments ({e}).', 'error'
    result = result[:8000]
    state['seen_citations'] = sorted(set(state['seen_citations']) | set(guards.RUNBOOK_ID.findall(result)))
    trace(state, agent_id, f'Tool {name}: {status}', tool=name, status=status,
          **({'args': args} if tool is not None and tool.trace_args else {}))
    return result


def agent(ctx, state, agent_id, cfg, attached, value, approve_tool, model_factory, memory_writes):
    """The agent loop: the model reasons, calls attached tools, and stops within its limits."""
    policy = next((b.config for b in attached if b.type == 'guard.policy'), {})
    history = [b.config for b in attached if b.type == 'memory.conversation']
    tools, closers = {}, []
    try:
        for b in attached:
            found, close = open_tools(b, ctx)
            closers.append(close)
            tools.update({t.name: t for t in found})
        content = {'input': value}
        if history:
            store = MemoryStore(ctx.memory_path)
            content['previous_runs'] = [r for h in history for r in store.recent_runs(h['namespace'], h.get('max_items', 5))]
        messages = [{'role': 'system', 'content': cfg['instructions'] + SAFETY_SUFFIX},
                    {'role': 'user', 'content': json.dumps(content, default=str)}]
        model, schemas = model_factory(cfg), [t.schema() for t in tools.values()]
        started, used, calls_made = time.monotonic(), 0, 0
        for step in range(1, cfg['max_steps'] + 1):
            if time.monotonic() - started > cfg['timeout_s']:
                raise ValueError(f'Agent timed out after {cfg["timeout_s"]}s.')
            choice = model.chat(messages, schemas)
            message = choice['message']
            used += choice['usage'].get('total_tokens') or (len(json.dumps(messages)) + len(json.dumps(message))) // 4
            messages.append(message)
            if used > cfg['token_budget']:
                raise ValueError(f'Agent exceeded its token budget ({used} > {cfg["token_budget"]}).')
            calls = message.get('tool_calls') or []
            if not calls:
                if choice.get('finish_reason') == 'length':
                    raise ValueError('Model output was cut off (length limit).')
                text = (message.get('content') or '').strip()
                trace(state, agent_id, f'Draft ready after {step} step(s), {used} tokens')
                memory_writes += [{'namespace': h['namespace'], 'input': value, 'output': text} for h in history]
                return text
            for call in calls:
                calls_made += 1
                if calls_made > policy.get('max_tool_calls', 10_000):
                    raise ValueError(f'Permission policy: more than {policy["max_tool_calls"]} tool calls in one run.')
                messages.append({'role': 'tool', 'tool_call_id': call.get('id', ''),
                                 'content': call_tool(state, agent_id, tools, policy, call, approve_tool)})
        raise ValueError(f'Agent reached its limit of {cfg["max_steps"]} steps without a final answer.')
    finally:
        for close in closers:
            close()


def output_check(state, block_id, cfg, draft):
    allowed = cfg.get('allowed_citations')
    if allowed == 'seen_in_tool_results':
        allowed = state['seen_citations']
    problems = guards.check_draft(draft, allowed, cfg.get('require_citation', False), cfg.get('block_patterns', []))
    if problems:
        state.update(status='blocked', result=draft, violations=problems)
        trace(state, block_id, 'Blocked: ' + ' '.join(problems))
        raise Blocked()
    trace(state, block_id, 'Passed output checks')
    return draft


def redact(state, block_id, cfg, draft):
    text, counts = guards.redact(str(draft), cfg.get('emails', True), cfg.get('ipv4', False), cfg.get('patterns', []))
    trace(state, block_id, ('Redacted ' + ', '.join(f'{n} {k}' for k, n in counts.items())) if counts else 'Nothing to redact')
    return text


def approval(state, block_id, draft, approve):
    if approve is None:
        state.update(status='awaiting_approval', result=draft, pending={'kind': 'draft', 'block': block_id, 'value': draft})
        trace(state, block_id, 'Waiting for human approval.')
        raise Stop()
    if not approve(block_id, draft):
        state['status'] = 'rejected'
        trace(state, block_id, 'Rejected by reviewer; nothing was sent.')
        raise Stop()
    trace(state, block_id, 'Approved by reviewer.')
    return draft


def output_file(state, block_id, cfg, draft, output_dir):
    base = Path(output_dir).resolve()
    target = (base / cfg['path']).resolve()
    if base != target and base not in target.parents:
        raise ValueError('Output path escapes the output folder.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(draft) + '\n')
    trace(state, block_id, f'Wrote {target.relative_to(base)}')


def output_webhook(state, block_id, cfg, draft):
    headers = {h: os.environ[v] for h, v in cfg.get('headers_env', {}).items()}
    core.http(cfg['url'], headers, {'text': str(draft)})
    trace(state, block_id, 'Posted to webhook')


def output_slack(state, block_id, cfg, draft):
    ts = adapters.SlackDelivery({'slack_channel': os.environ[cfg['channel_env']]}).send(str(draft))
    trace(state, block_id, f'Posted to Slack ({ts})')
'''

MAIN = r'''

def _ask(question):
    try:
        return input(question + ' [y/N] ').strip().lower() == 'y'
    except EOFError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--input', default=None, help='input for a manual trigger')
    parser.add_argument('--live', action='store_true', help='live mode: real data sources only')
    args = parser.parse_args(argv)
    state = run(args.input, mode='live' if args.live else 'sample',
                approve=lambda block, draft: _ask(f'\n--- Draft ---\n{draft}\n--- End ---\nApprove at "{block}"?'),
                approve_tool=lambda agent_id, tool, a: _ask(f'\n"{agent_id}" wants to call "{tool}" with {json.dumps(a)}. Allow?'))
    for t in state['trace']:
        print(f'  {t["block"]:<12} {t["detail"]}')
    print('Status:', state['status'])
    return 0 if state['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
'''


def _ident(block_id):
    name = re.sub(r'\W', '_', block_id).upper()
    return name if name[0].isalpha() else 'B_' + name


def _literal(value, indent=0):
    text = pprint.pformat(value, width=100, sort_dicts=False)
    return text.replace('\n', '\n' + ' ' * indent)


def export_python(diagram, doc=None):
    """Return the source of a runnable, readable agent script for a valid diagram."""
    problems = validate(diagram)
    if problems:
        raise ValueError('Fix the diagram before exporting: ' + '; '.join(f'{v.block or "diagram"}: {v.message}' for v in problems))
    blocks, trigger = diagram.blocks, diagram.of_category('trigger')[0]
    order = []

    def walk(block_id):
        order.append(block_id)
        for nxt in diagram.successors(block_id):
            walk(nxt)
    walk(trigger.id)
    flow_text = ' → '.join(order)

    settings = []
    for bid in order:
        b = blocks[bid]
        settings.append(f'# {b.type}: {bid}\n{_ident(bid)} = {_literal(b.config)}')
        if b.type == 'agent':
            attached = diagram.attached(bid)
            items = ',\n    '.join(f'Block({a.id!r}, {a.type!r}, {_literal(a.config, 4)})' for a in attached)
            settings.append(f'{_ident(bid)}_ATTACHED = [\n    {items},\n]' if attached else f'{_ident(bid)}_ATTACHED = []')

    # The flow, as straight-line code. Each block takes its one input from the block before it.
    body, var = [], {}

    def emit(bid, indent):
        b, pad = blocks[bid], ' ' * indent
        preds = diagram.predecessors(bid)
        src = var.get(preds[0]) if preds else None
        v = 'v_' + re.sub(r'\W', '_', bid)
        var[bid] = v
        c = _ident(bid)
        if b.type == 'trigger.manual':
            body.append(f"{pad}{v} = '' if payload is None else str(payload)")
            body.append(f"{pad}{v} = {v} or {c}.get('default_input', '')")
            body.append(f"{pad}trace(state, {bid!r}, f'Manual input ({{len({v})}} characters)')")
        elif b.type == 'trigger.webhook':
            body.append(f'{pad}{v} = payload')
            body.append(f"{pad}trace(state, {bid!r}, 'Webhook payload received')")
        elif b.type == 'trigger.alert':
            body.append(f"{pad}{v} = monitoring.alert({c}['sensor_id'])")
            body.append(f"{pad}trace(state, {bid!r}, 'Alert: ' + str({v}.get('message', '')))")
        elif b.type == 'agent':
            body.append(f'{pad}{v} = agent(ctx, state, {bid!r}, {c}, {c}_ATTACHED, {src}, approve_tool, model_factory, memory_writes)')
        elif b.type == 'guard.redact':
            body.append(f'{pad}{v} = redact(state, {bid!r}, {c}, {src})')
        elif b.type == 'guard.output_check':
            body.append(f'{pad}{v} = output_check(state, {bid!r}, {c}, {src})')
        elif b.type == 'guard.approval':
            body.append(f'{pad}{v} = approval(state, {bid!r}, {src}, approve)')
        elif b.type == 'output.file':
            body.append(f'{pad}output_file(state, {bid!r}, {c}, {src}, output_dir)')
        elif b.type == 'output.webhook':
            body.append(f'{pad}output_webhook(state, {bid!r}, {c}, {src})')
        elif b.type == 'output.slack':
            body.append(f'{pad}output_slack(state, {bid!r}, {c}, {src})')
        for nxt in diagram.successors(bid):
            emit(nxt, indent)
    emit(trigger.id, 8)

    header = f'''"""{diagram.name}: agent exported from BYA Diagram Studio ({dt.date.today().isoformat()}).

Flow: {flow_text}
Run:  python {re.sub(r"[^a-z0-9]+", "-", diagram.name.lower()).strip("-") or "agent"}.py [--input TEXT] [--live]

Needs the `bya` package (bya-runtime/bya, Apache-2.0) importable, and BYA_RUNTIME pointing at a folder
with ssot.json, runbooks.json and knowledge/ (defaults to this file's folder).
The flow and the agent loop below are plain Python: change them as you like.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from bya import adapters, core, guards
from bya.graph.devices import CommandRefused
from bya.graph.diagram import Block
from bya.graph.executor import SAFETY_SUFFIX
from bya.graph.memory import MemoryStore
from bya.graph.model import ChatModel
from bya.graph.tools import open_tools

HERE = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get('BYA_RUNTIME', HERE))

# ---------------------------------------------------------------------------
# Block settings, copied from the diagram.
# ---------------------------------------------------------------------------

'''
    run_fn = f'''

# ---------------------------------------------------------------------------
# The flow: {flow_text}
# ---------------------------------------------------------------------------

def run(payload=None, *, mode='sample', approve=None, approve_tool=None, monitoring=None, memory_path=None,
        knowledge_dir=None, output_dir=None, model_factory=ChatModel.from_agent_config):
    """Run the flow once.

    approve(block_id, draft) -> bool; None stops at the first approval (what evals use).
    approve_tool(agent_id, tool, args) -> bool; None denies every write tool.
    Returns the same result shape as the diagram runtime: status, trace, pending, result.
    """
    ctx = Context(mode, memory_path or RUNTIME / 'bya.sqlite3', knowledge_dir or RUNTIME / 'knowledge')
    monitoring = monitoring or (adapters.SampleMonitoring() if mode == 'sample' else adapters.PrtgMonitoring(
        json.loads((RUNTIME / 'config.local.json').read_text()) if (RUNTIME / 'config.local.json').exists() else {{}}))
    output_dir = output_dir or HERE / 'outputs'
    ctx.monitoring = monitoring
    state = {{'status': 'running', 'trace': [], 'seen_citations': [], 'violations': [], 'pending': None, 'result': None}}
    memory_writes = []
    try:
{chr(10).join(body)}
    except (Blocked, Stop):
        return state
    except ValueError as e:
        state.update(status='failed', error=str(e))
        return state
    state['status'] = 'completed'
    store = MemoryStore(ctx.memory_path)
    for w in memory_writes:  # only completed runs are remembered
        store.add_run(w['namespace'], w['input'], w['output'])
    return state
'''
    return header + '\n\n'.join(settings) + '\n' + HELPERS + run_fn + MAIN
