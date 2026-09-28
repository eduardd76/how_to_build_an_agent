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
