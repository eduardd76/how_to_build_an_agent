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
    """Return a list of human-readable violations; empty means the text may go to review.

    `allowed_runbooks=None` skips the citation checks (for flows that do not use runbook IDs).
    """
    violations = []
    if not text or not text.strip():
        return ['Model returned an empty draft.']
    if len(text) > MAX_CHARS:
        violations.append(f'Draft exceeds {MAX_CHARS} characters.')
    if allowed_runbooks is not None:
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


REDACTIONS = [
    ('token', re.compile(r'(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}')),
    ('key', re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}'
                       r'|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,})')),
    ('secret', re.compile(r'(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S+')),
]
EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
IPV4 = re.compile(r'\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b')


def redact(text, emails=True, ipv4=False, patterns=()):
    """Replace secrets (and optionally emails / IPv4 addresses / custom patterns). Returns (text, counts)."""
    rules = list(REDACTIONS)
    if emails:
        rules.append(('email', EMAIL))
    if ipv4:
        rules.append(('ip', IPV4))
    rules += [('pattern', re.compile(p)) for p in patterns]
    counts = {}
    for label, rx in rules:
        text, n = rx.subn(f'[REDACTED {label}]', text)
        if n:
            counts[label] = counts.get(label, 0) + n
    return text, counts


def check_draft(text, allowed_citations=None, require_citation=False, block_patterns=()):
    """Output check used by diagrams and exported agents: built-in rules plus extra blocked patterns."""
    problems = check(str(text), allowed_citations, require_citation=require_citation)
    problems += [f'Matches blocked pattern {p!r}.' for p in block_patterns if re.search(p, str(text), re.I)]
    return problems
