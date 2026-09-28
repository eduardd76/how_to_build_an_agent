"""Run history and the delivery state machine.

awaiting_approval → sending → sent | delivery_unknown
evaluation_only is terminal. No state ever moves backwards, so a message is posted at most once.
"""
import json
import sqlite3
from contextlib import closing

from .core import stamp

FINAL_STATES = ('sent', 'delivery_unknown')


class RunStore:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        c = sqlite3.connect(self.path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, body TEXT, state TEXT)')
        return c

    def save(self, result):
        with closing(self._connect()) as c:
            c.execute('INSERT INTO runs VALUES (?,?,?,?)', (result['id'], stamp(), json.dumps(result), result['status']))

    def claim(self, rid, check):
        """Atomically move a run from awaiting_approval to sending. `check(result, created)` may veto."""
        with closing(self._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                row = c.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
                if not row or row['state'] != 'awaiting_approval':
                    raise ValueError('Run is not awaiting approval or was already processed.')
                result = json.loads(row['body'])
                check(result, row['created'])
                c.execute('UPDATE runs SET state=? WHERE id=?', ('sending', rid))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
        return result

    def finish(self, rid, state):
        if state not in FINAL_STATES:
            raise ValueError('Invalid final delivery state.')
        with closing(self._connect()) as c:
            c.execute('UPDATE runs SET state=? WHERE id=? AND state=?', (state, rid, 'sending'))

    def state(self, rid):
        with closing(self._connect()) as c:
            row = c.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
        return row['state'] if row else None


class DiagramRunStore:
    """Diagram runs, including paused state waiting at an approval block."""

    def __init__(self, path):
        self.path = path

    def _connect(self):
        c = sqlite3.connect(self.path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS diagram_runs '
                  '(id TEXT PRIMARY KEY, created TEXT, diagram TEXT, state TEXT, status TEXT)')
        return c

    def save(self, rid, diagram, state):
        with closing(self._connect()) as c:
            c.execute('INSERT INTO diagram_runs VALUES (?,?,?,?,?)',
                      (rid, stamp(), json.dumps(diagram), json.dumps(state), state['status']))

    def claim_paused(self, rid):
        """Atomically take a paused run so one approval cannot be applied twice."""
        with closing(self._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                row = c.execute('SELECT * FROM diagram_runs WHERE id=?', (rid,)).fetchone()
                if not row or row['status'] != 'awaiting_approval':
                    raise ValueError('This run is not waiting for approval.')
                c.execute('UPDATE diagram_runs SET status=? WHERE id=?', ('resuming', rid))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
        return json.loads(row['diagram']), json.loads(row['state'])

    def update(self, rid, state):
        with closing(self._connect()) as c:
            c.execute('UPDATE diagram_runs SET state=?, status=? WHERE id=?', (json.dumps(state), state['status'], rid))
