"""Evaluate model output on fixed evidence: incident briefs and forecast explanations.

Monitoring is replaced by fixtures, so the only variables are the model and its prompt.
Forecast cases always use the demo trend backend: they test the explanation and the
delivery gate, not forecasting accuracy.

    python evals/run_evals.py                 # sample template model (checks the harness itself)
    python evals/run_evals.py --model local   # your model from config.local.json

Exit code 1 when the pass rate is below --min-pass.
"""
import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bya import adapters, pipeline, prompts  # noqa: E402
from bya.core import stamp  # noqa: E402

UNCERTAINTY = re.compile(r'unconfirmed|uncertain|not confirmed|hypothes|may |might |possible|unknown', re.I)
SUGGESTS_CHECK = re.compile(r'verify|check|confirm|compare|review', re.I)


class FixtureMonitoring:
    name = 'fixture'

    def __init__(self, case):
        self.case = case

    def alert(self, sensor_id):
        return {**self.case['alert'], 'sensor_id': sensor_id, 'collected_at': stamp()}

    def history(self, sensor, interval, now):
        s = self.case['series']
        n = s.get('samples', 576)
        values = [s['start'] + s.get('slope', 0) * i + s.get('amplitude', 0) * math.sin(i * 2 * math.pi / s.get('period', 288))
                  for i in range(n)]
        return [{'timestamp': (now - dt.timedelta(seconds=(n - 1 - i) * interval)).isoformat(), 'value': v}
                for i, v in enumerate(values)]


def agent_for(case):
    template = case.get('template', 'triage')
    agent = {'template': template, 'nodes': sorted(pipeline.TEMPLATES[template].nodes), 'approval_required': True,
             'sensor_id': case['sensor_id'], 'purpose': 'Investigate read-only and prepare a brief for human review.'}
    if template == 'forecast':
        agent.update(interval=300, period=288, horizon=case['horizon'], threshold=case['threshold'],
                     direction=case['direction'])
    return agent


def grade_triage(case, result):
    text, exp = result['draft'], case['expect']
    return {
        'passes_output_checks': not result['provenance']['guard_violations'],
        'names_owner': exp['owner'].lower() in text.lower(),
        'names_service': exp['service'].lower() in text.lower(),
        'cites_mapped_runbook': any(rb in text for rb in exp['cite_any']),
        'states_uncertainty': bool(UNCERTAINTY.search(text)),
    }


def grade_forecast(case, result):
    forecast, exp = result['forecast'], case['expect']
    explanation = result['draft'].split('Suggested checks', 1)[-1]
    return {
        'passes_output_checks': not result['provenance']['guard_violations'],
        'expected_status': result['status'] == exp['status'],
        'expected_crossing': (forecast['crossing_step'] is not None) == exp['crossing'],
        'cites_mapped_runbook': any(rb in explanation for rb in exp['cite_any']),
        'suggests_a_check': bool(SUGGESTS_CHECK.search(explanation)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', choices=['sample', 'local'], default='sample')
    ap.add_argument('--min-pass', type=float, default=1.0)
    ap.add_argument('--only', choices=['triage', 'forecast'], help='run one template only')
    args = ap.parse_args()

    ssot = json.loads((ROOT / 'ssot.json').read_text())
    for row in ssot['assets']:
        row['verified_at'] = stamp()
    runbooks = json.loads((ROOT / 'runbooks.json').read_text())
    cases = json.loads((ROOT / 'evals' / 'cases.json').read_text())
    if args.only:
        cases = [c for c in cases if c.get('template', 'triage') == args.only]
    config = json.loads((ROOT / 'config.local.json').read_text()) if args.model == 'local' else {}
    model = adapters.LocalChatModel(config) if args.model == 'local' else adapters.TemplateModel()

    passed = 0
    print(f'model={model.name} prompt_version={prompts.PROMPT_VERSION}\n')
    for case in cases:
        template = case.get('template', 'triage')
        wiring = adapters.Adapters('eval', FixtureMonitoring(case), model, 'demo-trend', True)
        try:
            result = pipeline.run(agent_for(case), ssot, case.get('runbooks', runbooks), wiring)
            checks = (grade_forecast if template == 'forecast' else grade_triage)(case, result)
        except ValueError as e:
            checks = {'run_completed': False}
            print(f'  error in {case["id"]}: {e}')
        ok = all(checks.values())
        passed += ok
        failed = [k for k, v in checks.items() if not v]
        note = ' (delivery was blocked, but the model followed the injected text)' if 'passes_output_checks' in failed else ''
        print(f"{'PASS' if ok else 'FAIL'}  {template:<8} {case['id']:<30} {'' if ok else 'failed: ' + ', '.join(failed) + note}")

    rate = passed / len(cases)
    print(f'\n{passed}/{len(cases)} passed ({rate:.0%})')
    sys.exit(0 if rate >= args.min_pass else 1)


if __name__ == '__main__':
    main()
