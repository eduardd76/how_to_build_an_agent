"""Evals for diagrams: test cases stored in the diagram file, graded the same way for any runner
(the diagram itself, or an exported agent.py).

A case:
    {"id": "wan-alert",
     "input": "text or JSON for a manual/webhook trigger",           # optional
     "alert": {"message": "...", "status": "Warning", "lastvalue": "91 %"},  # optional, for alert triggers
     "expect": {"status": "awaiting_approval",       # default: ready for human review
                "contains": ["Network Operations"], "not_contains": ["restarted"],
                "tools_called": ["asset_lookup"], "tools_not_called": ["restart_interface"],
                "cites_any": ["RB-WAN-01"]}}

Evals never execute write tools or outputs: write calls are denied and every run stops at the first approval.
Memory is isolated per case in a temporary store, so evals never read or change real memory.
"""
import copy
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from ..core import stamp

CASE_ID = re.compile(r'^[A-Za-z0-9_-]{1,60}$')
EXPECT_KEYS = {'status', 'contains', 'not_contains', 'tools_called', 'tools_not_called', 'cites_any'}
STATUSES = {'awaiting_approval', 'blocked', 'completed', 'failed', 'rejected'}
MAX_CASES, MAX_REPEAT = 50, 5


def validate_cases(cases):
    """Return a list of problems with the eval cases (empty = fine)."""
    problems = []
    if not isinstance(cases, list):
        return ['"evals" must be a list of cases.']
    if len(cases) > MAX_CASES:
        problems.append(f'At most {MAX_CASES} eval cases.')
    seen = set()
    for i, case in enumerate(cases):
        where = f'Case {i + 1}'
        if not isinstance(case, dict):
            problems.append(f'{where}: must be an object.')
            continue
        cid = case.get('id', '')
        if not CASE_ID.match(str(cid)):
            problems.append(f'{where}: "id" must be 1–60 letters, digits, "_" or "-".')
        elif cid in seen:
            problems.append(f'{where}: duplicate id "{cid}".')
        seen.add(cid)
        if 'alert' in case and not isinstance(case['alert'], dict):
            problems.append(f'{where}: "alert" must be an object.')
        expect = case.get('expect', {})
        if not isinstance(expect, dict) or set(expect) - EXPECT_KEYS:
            problems.append(f'{where}: "expect" keys must be among {", ".join(sorted(EXPECT_KEYS))}.')
            continue
        if 'status' in expect and expect['status'] not in STATUSES:
            problems.append(f'{where}: "status" must be one of {", ".join(sorted(STATUSES))}.')
        for key in EXPECT_KEYS - {'status'}:
            if key in expect and not (isinstance(expect[key], list) and all(isinstance(x, str) for x in expect[key])):
                problems.append(f'{where}: "{key}" must be a list of strings.')
    return problems


class CaseMonitoring:
    """Monitoring stand-in that returns the case's alert (or a sample alert)."""
    name = 'eval'

    def __init__(self, alert, fallback):
        self.alert_data, self.fallback = alert, fallback

    def alert(self, sensor_id):
        if self.alert_data is None:
            return self.fallback.alert(sensor_id)
        return {'sensor_id': str(sensor_id), 'collected_at': stamp(), **self.alert_data}

    def history(self, sensor, interval, now):
        return self.fallback.history(sensor, interval, now)


def draft_of(state):
    pending = state.get('pending') or {}
    if pending.get('kind', 'draft') == 'draft' and 'value' in pending:
        return str(pending['value'])
    return '' if state.get('result') is None else str(state['result'])


def grade(case, state):
    expect = case.get('expect', {})
    text = draft_of(state)
    low = text.lower()
    called = {t['tool'] for t in state.get('trace', []) if t.get('tool')}
    checks = {'status': state.get('status') == expect.get('status', 'awaiting_approval')}
    if 'contains' in expect:
        checks['contains'] = all(s.lower() in low for s in expect['contains'])
    if 'not_contains' in expect:
        checks['not_contains'] = not any(s.lower() in low for s in expect['not_contains'])
    if 'tools_called' in expect:
        checks['tools_called'] = set(expect['tools_called']) <= called
    if 'tools_not_called' in expect:
        checks['tools_not_called'] = not (set(expect['tools_not_called']) & called)
    if 'cites_any' in expect:
        checks['cites_any'] = any(c in text for c in expect['cites_any'])
    return checks


def run_evals(cases, runner, repeat=1):
    """runner(case, workdir) -> final state. Returns per-run results and totals."""
    problems = validate_cases(cases)
    if problems:
        raise ValueError(' '.join(problems))
    repeat = max(1, min(int(repeat), MAX_REPEAT))
    results = []
    for case in cases:
        for n in range(1, repeat + 1):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    state = runner(copy.deepcopy(case), Path(tmp))
                    error = state.get('error')
                except Exception as e:  # any error fails this case only, never the whole eval run
                    state, error = getattr(e, 'state', None) or {'status': 'failed', 'trace': []}, f'{type(e).__name__}: {e}'
            checks = grade(case, state)
            results.append({'id': case['id'], 'run': n, 'passed': all(checks.values()), 'checks': checks,
                            'status': state.get('status'), 'draft': draft_of(state)[:2000], 'error': error,
                            'tools': [t['tool'] for t in state.get('trace', []) if t.get('tool')]})
    passed = sum(r['passed'] for r in results)
    return {'passed': passed, 'total': len(results), 'results': results}


def diagram_runner(diagram, base_ctx):
    """Runner for a diagram: stop at the first approval, deny writes, isolate memory and outputs."""
    from .executor import run

    def runner(case, workdir):
        ctx = replace(base_ctx, approve=None, approve_tool=lambda *a: False, pause_for_tool_approval=False,
                      monitoring=CaseMonitoring(case.get('alert'), base_ctx.monitoring),
                      memory_path=workdir / 'memory.sqlite3', output_dir=workdir / 'outputs')
        return run(diagram, ctx, case.get('input'))
    return runner


def module_runner(module, fallback_monitoring):
    """Runner for an exported agent script (a module with run(...)); same isolation as diagram_runner."""
    def runner(case, workdir):
        return module.run(case.get('input'), approve=None, approve_tool=None,
                          monitoring=CaseMonitoring(case.get('alert'), fallback_monitoring),
                          memory_path=workdir / 'memory.sqlite3', output_dir=workdir / 'outputs')
    return runner


def load_module(source, runtime_dir, name='exported_agent'):
    """Import generated source as a module (to evaluate an export before you save it).
    runtime_dir holds ssot.json, runbooks.json and knowledge/."""
    import importlib.util
    import sys
    path = Path(tempfile.mkdtemp()) / f'{name}.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.RUNTIME = Path(runtime_dir)
    return module
