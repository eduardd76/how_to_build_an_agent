"""Block types the canvas offers, and the data types that flow between them.

Flow types make the safety rules structural: an output accepts only an approved draft,
an approval accepts only a checked draft, and a check accepts only an agent's draft.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BlockSpec:
    type: str
    category: str                    # trigger | agent | tool | guard | output
    inputs: frozenset = frozenset()  # flow types this block accepts; empty = no flow input
    output: str = None               # flow type this block emits; None = end of flow
    attachable: bool = False         # connects to an agent by attachment, never by flow


SPECS = {spec.type: spec for spec in (
    BlockSpec('trigger.manual', 'trigger', output='text'),
    BlockSpec('trigger.webhook', 'trigger', output='json'),
    BlockSpec('trigger.alert', 'trigger', output='alert'),
    BlockSpec('agent', 'agent', inputs=frozenset({'text', 'json', 'alert'}), output='draft'),
    BlockSpec('tool.builtin', 'tool', attachable=True),
    BlockSpec('tool.http', 'tool', attachable=True),
    BlockSpec('tool.mcp', 'tool', attachable=True),
    BlockSpec('guard.output_check', 'guard', inputs=frozenset({'draft'}), output='checked_draft'),
    BlockSpec('guard.approval', 'guard', inputs=frozenset({'checked_draft'}), output='approved_draft'),
    BlockSpec('output.file', 'output', inputs=frozenset({'approved_draft'})),
    BlockSpec('output.webhook', 'output', inputs=frozenset({'approved_draft'})),
    BlockSpec('output.slack', 'output', inputs=frozenset({'approved_draft'})),
)}

BUILTIN_FUNCTIONS = ('calculator', 'time_now', 'asset_lookup', 'runbook_search')

AGENT_LIMITS = {
    'max_steps': (1, 50),
    'token_budget': (1_000, 1_000_000),
    'timeout_s': (5, 3_600),
}
