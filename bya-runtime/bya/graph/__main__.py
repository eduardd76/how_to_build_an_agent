"""Command line for diagrams: validate and run them from the terminal.

    python -m bya.graph validate diagrams/incident-brief.json [--live]
    python -m bya.graph run diagrams/incident-brief.json [--live] [--input TEXT]
    python -m bya.graph eval diagrams/incident-brief.json [--repeat N] [--export]
    python -m bya.graph export diagrams/incident-brief.json [-o incident-brief.py]
    python -m bya.graph reach diagrams/interface-check.json [--live]

Run from the bya-runtime directory. Approvals are asked for in the terminal.
"""
import argparse
import json
import sys
from pathlib import Path

from .. import adapters
from . import Context, DiagramError, DiagramInvalid, StepFailed, evals, load_file, run, validate
from .export import export_python
from .reach import reach

ROOT = Path(__file__).resolve().parents[2]


def _read(name):
    return json.loads((ROOT / name).read_text())


def _ask(question):
    try:
        return input(question + ' [y/N] ').strip().lower() == 'y'
    except EOFError:
        return False


def _print_report(label, report):
    print(f'\n{label}: {report["passed"]}/{report["total"]} passed')
    for r in report['results']:
        failed = [k for k, v in r['checks'].items() if not v]
        suffix = '' if r['passed'] else f'  failed: {", ".join(failed)}' + (f' ({r["error"]})' if r.get('error') else '')
        print(f'  {"PASS" if r["passed"] else "FAIL"}  {r["id"]:<28} run {r["run"]}  status={r["status"]}{suffix}')


def _print_reach(r):
    s = r['summary']
    print(f"Devices readable: {s['devices_readable']} · read tools: {s['read_tools']} · change paths (each approved): "
          f"{s['change_paths']} · outputs after approval: {s['outputs_after_approval']} · device config sessions: {s['device_config_paths']}")
    for a in r['agents']:
        print(f"\nAgent {a['agent']} · model {a['model']} ({a['model_location']})")
        for x in a['read']:
            print(f"  read    {x['system']}: {x['what']}")
        for d in a['devices']:
            where = ', '.join(d['devices'][:10]) + (f" … {len(d['devices']) - 10} more" if len(d['devices']) > 10 else '')
            print(f"  devices {d['source']} {json.dumps(d['filter'])}: {where or d['error']}")
            print(f"          allowed: {', '.join(d['allow'])} · max {d['max_commands']} commands per run")
        for x in a['change']:
            print(f"  CHANGE  {x['system']}: {x['what']} (each call needs approval)")
        for m in a['memory']:
            print(f"  memory  {m['type']} {m['namespace']}")
    for o in r['outputs']:
        print(f"\nOutput {o['block']} ({o['type']}) → {o['where']}, only after approval")


def main(argv=None):
    p = argparse.ArgumentParser(prog='python -m bya.graph')
    p.add_argument('command', choices=['validate', 'run', 'eval', 'export', 'reach'])
    p.add_argument('diagram')
    p.add_argument('--live', action='store_true', help='live mode: real data sources only')
    p.add_argument('--input', default=None, help='input for a manual trigger')
    p.add_argument('--repeat', type=int, default=1, help='eval: runs per case (1-5)')
    p.add_argument('--export', action='store_true', help='eval: also evaluate the exported script and compare')
    p.add_argument('-o', '--output', default=None, help='export: file to write (default: print)')
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

    if args.command == 'export':
        try:
            source = export_python(diagram)
        except ValueError as e:
            print(e)
            return 1
        if args.output:
            Path(args.output).write_text(source)
            print(f'Wrote {args.output}. Run it with PYTHONPATH={ROOT} BYA_RUNTIME={ROOT} python {args.output}')
        else:
            print(source)
        return 0

    config = _read('config.local.json') if (ROOT / 'config.local.json').exists() else {}
    ctx = Context(
        mode=mode, ssot=_read('ssot.json'), runbooks=_read('runbooks.json'),
        monitoring=adapters.PrtgMonitoring(config) if args.live else adapters.SampleMonitoring(),
        approve=lambda block, draft: _ask(f'\n--- Draft ---\n{draft}\n--- End ---\nApprove at "{block.id}"?'),
        approve_tool=lambda block_id, tool, a: _ask(f'\nAgent "{block_id}" wants to call write tool "{tool}" with {json.dumps(a)}. Allow?'),
        output_dir=ROOT / 'outputs', memory_path=ROOT / 'bya.sqlite3', knowledge_dir=ROOT / 'knowledge',
        lab_dir=ROOT / 'lab', configs_dir=ROOT / 'configs',
    )
    if args.command == 'reach':
        _print_reach(reach(diagram, ctx))
        return 0
    if args.command == 'eval':
        doc = json.loads(Path(args.diagram).read_text())
        cases = doc.get('evals', [])
        if not cases:
            print('This diagram has no eval cases ("evals").')
            return 1
        report = evals.run_evals(cases, evals.diagram_runner(diagram, ctx), args.repeat)
        _print_report('diagram', report)
        if args.export:
            module = evals.load_module(export_python(diagram), ROOT)
            exported = evals.run_evals(cases, evals.module_runner(module, ctx.monitoring), args.repeat)
            _print_report('exported agent', exported)
            same = [a['passed'] for a in report['results']] == [b['passed'] for b in exported['results']]
            print('Export matches the diagram.' if same else 'Export and diagram DIFFER on at least one case.')
            return 0 if same and report['passed'] == report['total'] else 1
        return 0 if report['passed'] == report['total'] else 1

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
