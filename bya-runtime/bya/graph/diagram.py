"""The diagram file the canvas saves: blocks, attachments (agent → resource) and flow wires (a → b)."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import SPECS

BLOCK_ID = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


class DiagramError(ValueError):
    """The file is not a diagram at all (wrong shape). Semantic problems are Violations instead."""


@dataclass
class Block:
    id: str
    type: str
    config: dict

    @property
    def spec(self):
        return SPECS.get(self.type)


@dataclass
class Diagram:
    name: str
    blocks: dict
    attachments: list
    flow: list
    layout: dict = field(default_factory=dict)

    def successors(self, block_id):
        return [b for a, b in self.flow if a == block_id]

    def predecessors(self, block_id):
        return [a for a, b in self.flow if b == block_id]

    def attached(self, agent_id):
        return [self.blocks[b] for a, b in self.attachments if a == agent_id and b in self.blocks]

    def of_category(self, category):
        return [b for b in self.blocks.values() if b.spec and b.spec.category == category]


def _pairs(doc, key):
    pairs = doc.get(key, [])
    if not isinstance(pairs, list) or not all(
            isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in pairs):
        raise DiagramError(f'"{key}" must be a list of [from, to] block-id pairs.')
    return [tuple(p) for p in pairs]


def load(doc):
    if not isinstance(doc, dict):
        raise DiagramError('A diagram must be a JSON object.')
    rows = doc.get('blocks')
    if not isinstance(rows, list) or not rows:
        raise DiagramError('A diagram needs a non-empty "blocks" list.')
    blocks = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not isinstance(row.get('type'), str):
            raise DiagramError('Each block needs a string "id" and "type".')
        if not BLOCK_ID.match(row['id']):
            raise DiagramError(f'Block id "{row["id"]}" must be 1–40 letters, digits, "_" or "-".')
        if row['id'] in blocks:
            raise DiagramError(f'Duplicate block id "{row["id"]}".')
        config = row.get('config', {})
        if not isinstance(config, dict):
            raise DiagramError(f'Block "{row["id"]}": "config" must be an object.')
        blocks[row['id']] = Block(row['id'], row['type'], config)
    return Diagram(name=str(doc.get('name', 'Untitled')), blocks=blocks,
                   attachments=_pairs(doc, 'attachments'), flow=_pairs(doc, 'flow'),
                   layout=doc.get('layout', {}) if isinstance(doc.get('layout', {}), dict) else {})


def load_file(path):
    try:
        return load(json.loads(Path(path).read_text()))
    except json.JSONDecodeError as e:
        raise DiagramError(f'Not valid JSON: {e}') from None
