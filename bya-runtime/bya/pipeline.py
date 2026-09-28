"""Declarative templates: each is an ordered list of steps bound to the builder's blocks.

The pipeline is fixed and validated; the model drafts text inside one step and never picks
the next step, calls tools or chooses recipients. That is deliberate: this is a workflow
with a model in it, not an autonomous agent.
"""
import datetime as dt
import time
from dataclasses import dataclass, field

from . import forecasting, guards, prompts, validation
from .core import UTC, stamp


class StepFailed(ValueError):
    pass


@dataclass
class Run:
    agent: dict
    ssot: dict
    all_runbooks: list
    adapters: object
    now: dt.datetime = field(default_factory=lambda: dt.datetime.now(UTC))
    asset: dict = None
    sensor: dict = None
    runbooks: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    points: list = None
    values: list = None
    forecast: dict = None
    draft: str = ''
    model_text: str = ''
    status: str = 'awaiting_approval'
    violations: list = field(default_factory=list)
    trace: list = field(default_factory=list)


# --- Steps: each takes the run, updates it, and returns a one-line trace detail ---

def resolve_asset(run):
    asset, sensor = validation.resolve(run.ssot, run.agent['sensor_id'], run.now)
    if asset.get('sample', False) and not run.adapters.allow_sample_assets:
        raise ValueError('Replace the sample SSOT with reviewed real assets before a live run.')
    run.asset, run.sensor = asset, sensor
    return f"{asset['name']} → {asset['service']} · {asset['owner']}"


def match_runbooks(run):
    ids = run.asset.get('runbook_ids', [])
    run.runbooks = [r for r in run.all_runbooks if r['id'] in ids]
    run.evidence.update(asset=run.asset, sensor=run.sensor, runbooks=run.runbooks, collected_at=stamp())
    return f'{len(run.runbooks)} approved runbooks matched'


def read_alert(run):
    event = run.adapters.monitoring.alert(run.agent['sensor_id'])
    run.evidence['alert'] = event
    return str(event.get('message'))


def draft_brief(run):
    model = run.adapters.model
    run.draft = run.model_text = model.complete(prompts.BRIEF_SYSTEM, prompts.brief_payload(run.agent.get('purpose'), run.evidence))
    return f'Draft prepared by {model.name}; no tools delegated to the model'


def read_history(run):
    run.points = run.adapters.monitoring.history(run.sensor, int(run.agent['interval']), run.now)
    return f'{len(run.points)} readings from {run.adapters.monitoring.name}'


def check_quality(run):
    interval = int(run.agent['interval'])
    run.values = validation.series_check(run.points, interval, run.now)
    run.evidence['telemetry'] = {'count': len(run.values), 'first': run.points[0]['timestamp'],
                                 'last': run.points[-1]['timestamp'], 'interval_seconds': interval,
                                 'unit': run.sensor['unit']}
    return f'{len(run.values)} ordered, complete samples'


def run_forecast(run):
    a, unit, interval = run.agent, run.sensor['unit'], int(run.agent['interval'])
    backend = run.adapters.forecast_backend
    f = forecasting.forecast(run.values, a['horizon'], a['threshold'], a['direction'], backend, int(a['period']))
    run.forecast = {**f, 'history': run.values[-96:], 'interval_seconds': interval, 'threshold': a['threshold'], 'unit': unit}
    when = f['crossing_step']
    crossing = (f"Point forecast crosses {a['threshold']} {unit} in {when * interval / 60:g} minutes."
                if when else 'No point-forecast threshold crossing within this horizon.')
    run.draft = (f"{'[SAMPLE / trend baseline] ' if backend == 'demo-trend' else ''}"
                 f"{run.asset['name']} · {run.sensor['channel']}: {crossing} "
                 f"Service: {run.asset['service']}; owner: {run.asset['owner']}. "
                 f"Holdout MAE {f['mae']:.2f}, seasonal baseline MAE {f['baseline_mae']:.2f}. "
                 'This forecasts a metric, not an outage or root cause.')
    if not f['beats_baseline']:
        run.status = 'evaluation_only'
        run.draft += ' Model did not beat the baseline; external delivery blocked.'
    name = 'Demonstration trend baseline' if backend == 'demo-trend' else 'Local TimesFM 2.5'
    return f'{name}; three chronological holdouts vs seasonal naive'


def explain_forecast(run):
    model = run.adapters.model
    run.model_text = model.complete(prompts.EXPLAIN_SYSTEM, prompts.explain_payload(run.agent.get('purpose'), run.draft, run.runbooks))
    run.draft += f'\n\nSuggested checks ({model.name}): {run.model_text}'
    return f'Explanation added by {model.name}; numbers stay deterministic'


def guard_output(run):
    triage = run.agent['template'] == 'triage'
    run.violations = guards.check(run.model_text, [r['id'] for r in run.runbooks], require_citation=triage)
    if not run.violations:
        return 'Model text passed output checks'
    run.status = 'evaluation_only'
    run.draft += '\n\nBlocked by output checks: ' + ' '.join(run.violations)
    return f'{len(run.violations)} violation(s); delivery blocked'


def delivery_policy(run):
    return 'Awaiting approval; no message sent' if run.status == 'awaiting_approval' else 'Evaluation only; delivery blocked'


# --- Templates ------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    node: str
    label: str
    fn: object


@dataclass(frozen=True)
class Template:
    nodes: frozenset
    steps: tuple


BASE_NODES = frozenset({'trigger', 'ssot', 'knowledge', 'model', 'policy', 'output'})

TEMPLATES = {
    'triage': Template(BASE_NODES, (
        Step('ssot', 'SSOT', resolve_asset),
        Step('knowledge', 'Knowledge', match_runbooks),
        Step('trigger', 'Monitoring', read_alert),
        Step('model', 'Analysis', draft_brief),
        Step('policy', 'Output checks', guard_output),
        Step('output', 'Delivery policy', delivery_policy),
    )),
    'forecast': Template(BASE_NODES | {'history', 'quality', 'forecast'}, (
        Step('ssot', 'SSOT', resolve_asset),
        Step('knowledge', 'Knowledge', match_runbooks),
        Step('history', 'History', read_history),
        Step('quality', 'Data quality', check_quality),
        Step('forecast', 'Forecast', run_forecast),
        Step('model', 'Explanation', explain_forecast),
        Step('policy', 'Output checks', guard_output),
        Step('output', 'Delivery policy', delivery_policy),
    )),
}


def validate_agent(agent):
    template = TEMPLATES.get(agent.get('template'))
    if template is None:
        raise ValueError('Unknown template.')
    validation.validate_agent(agent, template.nodes)
    return template


def execute(run):
    template = validate_agent(run.agent)
    for step in template.steps:
        started = time.perf_counter()
        try:
            detail = step.fn(run)
        except ValueError as e:
            raise StepFailed(f'{step.label}: {e}') from None
        run.trace.append({'step': step.label, 'node': step.node, 'detail': detail,
                          'ms': round((time.perf_counter() - started) * 1000)})
    result = {
        'mode': run.adapters.label, 'status': run.status, 'draft': run.draft,
        'trace': run.trace, 'evidence': run.evidence,
        'provenance': {'template': run.agent['template'], 'model': run.adapters.model.name,
                       'monitoring': run.adapters.monitoring.name, 'prompt_version': prompts.PROMPT_VERSION,
                       'guard_violations': run.violations},
    }
    if run.forecast is not None:
        result['forecast'] = run.forecast
    return result


def run(agent, ssot, runbooks, adapters):
    return execute(Run(agent, ssot, runbooks, adapters))
