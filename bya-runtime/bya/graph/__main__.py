"""Command line for diagrams: validate and run them from the terminal.

    python -m bya.graph validate diagrams/incident-brief.json [--live]
    python -m bya.graph run diagrams/incident-brief.json [--live] [--input TEXT]

Run from the bya-runtime directory. Approvals are asked for in the terminal.
"""
import argparse
import json
import sys
from pathlib import Path

from .. import adapters
from . import Context, DiagramError, DiagramInvalid, StepFailed, load_file, run, validate

ROOT = Path(__file__).resolve().parents[2]


def _read(name):
    return json.loads((ROOT / name).read_text())


def _ask(question):
    try:
        return input(question + ' [y/N] ').strip().lower() == 'y'
    except EOFError:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(prog='python -m bya.graph')
    p.add_argument('command', choices=['validate', 'run'])
    p.add_argument('diagram')
    p.add_argument('--live', action='store_true', help='live mode: real data sources only')
    p.add_argument('--input', default=None, help='input for a manual trigger')
    args = p.parse_args(argv)
    mode = 'live' if args.live else 'sample'

    try:
        diagram = load_file(args.diagram)
    except (DiagramError, OSError) as e:
        print(f'Cannot load diagram: {e}')
        return 2

    if args.command == 'validate':
        problems = validate(diagram, mode)
        for v in problems:
            print(f'  [{v.rule}] {v.block or "diagram"}: {v.message}')
        print(f'{len(problems)} problem(s) in "{diagram.name}" ({mode} mode).' if problems else f'"{diagram.name}" is valid ({mode} mode).')
        return 1 if problems else 0

    config = _read('config.local.json') if (ROOT / 'config.local.json').exists() else {}
    ctx = Context(
        mode=mode, ssot=_read('ssot.json'), runbooks=_read('runbooks.json'),
        monitoring=adapters.PrtgMonitoring(config) if args.live else adapters.SampleMonitoring(),
        approve=lambda block, draft: _ask(f'\n--- Draft ---\n{draft}\n--- End ---\nApprove at "{block.id}"?'),
        approve_tool=lambda block_id, tool, a: _ask(f'\nAgent "{block_id}" wants to call write tool "{tool}" with {json.dumps(a)}. Allow?'),
        output_dir=ROOT / 'outputs', memory_path=ROOT / 'bya.sqlite3', knowledge_dir=ROOT / 'knowledge',
    )
    try:
        state = run(diagram, ctx, args.input)
    except DiagramInvalid as e:
        print(e)
        return 1
    except StepFailed as e:
        state = e.state
        print(f'Run failed: {e}')
    for t in state['trace']:
        print(f'  {t["block"]:<12} {t["ms"]:>6} ms  {t["detail"]}')
    print(f'Status: {state["status"]}')
    return 0 if state['status'] == 'completed' else 1


if __name__ == '__main__':
    sys.exit(main())
