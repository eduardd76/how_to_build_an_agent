"""Deterministic checks on model output, run before a human ever sees the draft.

Human approval stays the final control; these checks make sure the reviewer is never
the only thing between a hallucinated or injected claim and a Slack channel.
"""
import re

MAX_CHARS = 3000
RUNBOOK_ID = re.compile(r'\bRB-[A-Z0-9]+(?:-[A-Z0-9]+)*\b')
ACTION_CLAIM = re.compile(
    r'\b(?:i|we)\s+(?:have\s+|just\s+)?(?:restarted|rebooted|reset|changed|applied|executed|deleted|'
    r'shut|disabled|enabled|reconfigured|rolled back|bounced|fixed)\b',
    re.I,
)
ROOT_CAUSE = re.compile(r'\broot cause (?:is|was)\s+(?!unconfirmed|unknown|not\b|undetermined|unclear)', re.I)
PROBABILITY = re.compile(
    r'\b\d+(?:\.\d+)?\s*%\s+(?:chance|probability|likelihood)\b|\bprobability of (?:an? )?outage\b', re.I
)


def check(text, allowed_runbooks, require_citation=False):
    """Return a list of human-readable violations; empty means the text may go to review."""
    violations = []
    if not text or not text.strip():
        return ['Model returned an empty draft.']
    if len(text) > MAX_CHARS:
        violations.append(f'Draft exceeds {MAX_CHARS} characters.')
    cited = set(RUNBOOK_ID.findall(text))
    unknown = sorted(cited - set(allowed_runbooks))
    if unknown:
        violations.append('Cites runbooks not mapped to this asset: ' + ', '.join(unknown) + '.')
    if require_citation and allowed_runbooks and not cited:
        violations.append('Does not cite any mapped runbook.')
    if ACTION_CLAIM.search(text):
        violations.append('Claims an action was taken; this agent is read-only.')
    if ROOT_CAUSE.search(text):
        violations.append('Asserts a root cause without confirmation.')
    if PROBABILITY.search(text):
        violations.append('States an outage probability; forecasts are metric estimates only.')
    return violations
