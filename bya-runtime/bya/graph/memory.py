"""Agent memory, local and bounded: facts (key-value), run history, and keyword search over local documents.

Everything read back from memory is passed to the model as data, never as instructions.
"""
import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from ..core import stamp

KEY = re.compile(r'^[A-Za-z0-9_.:-]{1,80}$')
MAX_VALUE = 2_000
MAX_DOC_BYTES = 1_000_000
MAX_DOCS = 500
WORD = re.compile(r'[a-z0-9][a-z0-9_-]+')


class MemoryStore:
    def __init__(self, path):
        self.path = path

    def _connect(self):
        c = sqlite3.connect(self.path, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS memory_facts (namespace TEXT, key TEXT, value TEXT, updated TEXT, '
                  'PRIMARY KEY (namespace, key))')
        c.execute('CREATE TABLE IF NOT EXISTS memory_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT, '
                  'created TEXT, input TEXT, output TEXT)')
        return c

    # --- facts ---

    def remember(self, namespace, key, value, max_entries):
        if not KEY.match(key or ''):
            raise ValueError('Key must be 1–80 letters, digits or _ . : -')
        value = str(value)
        if len(value) > MAX_VALUE:
            raise ValueError(f'Value longer than {MAX_VALUE} characters.')
        with closing(self._connect()) as c:
            exists = c.execute('SELECT 1 FROM memory_facts WHERE namespace=? AND key=?', (namespace, key)).fetchone()
            count = c.execute('SELECT COUNT(*) FROM memory_facts WHERE namespace=?', (namespace,)).fetchone()[0]
            if not exists and count >= max_entries:
                raise ValueError(f'Memory "{namespace}" is full ({max_entries} facts). Forget something first.')
            c.execute('INSERT OR REPLACE INTO memory_facts VALUES (?,?,?,?)', (namespace, key, value, stamp()))
        return f'Remembered "{key}".'

    def recall(self, namespace, key=None):
        with closing(self._connect()) as c:
            if key:
                row = c.execute('SELECT value FROM memory_facts WHERE namespace=? AND key=?', (namespace, key)).fetchone()
                return row['value'] if row else f'Nothing remembered under "{key}".'
            rows = c.execute('SELECT key, value FROM memory_facts WHERE namespace=? ORDER BY key', (namespace,)).fetchall()
        return json.dumps({r['key']: r['value'] for r in rows})

    def forget(self, namespace, key):
        with closing(self._connect()) as c:
            c.execute('DELETE FROM memory_facts WHERE namespace=? AND key=?', (namespace, key))
        return f'Forgot "{key}".'

    # --- run history ---

    def add_run(self, namespace, input_value, output_text, keep=20):
        with closing(self._connect()) as c:
            c.execute('INSERT INTO memory_runs (namespace, created, input, output) VALUES (?,?,?,?)',
                      (namespace, stamp(), json.dumps(input_value, default=str)[:1_000], str(output_text)[:MAX_VALUE]))
            c.execute('DELETE FROM memory_runs WHERE namespace=? AND id NOT IN '
                      '(SELECT id FROM memory_runs WHERE namespace=? ORDER BY id DESC LIMIT ?)', (namespace, namespace, keep))

    def recent_runs(self, namespace, limit):
        with closing(self._connect()) as c:
            rows = c.execute('SELECT created, input, output FROM memory_runs WHERE namespace=? ORDER BY id DESC LIMIT ?',
                             (namespace, limit)).fetchall()
        return [{'when': r['created'], 'input': r['input'], 'result': r['output']} for r in reversed(rows)]


def search_documents(folder, query, max_results):
    """Rank .md/.txt files by keyword overlap (TF-IDF-like); return file name and a snippet around the best match."""
    root = Path(folder)
    if not root.is_dir():
        return f'Knowledge folder "{root.name}" does not exist.'
    files = [p for p in sorted(root.rglob('*')) if p.suffix.lower() in ('.md', '.txt') and p.is_file()][:MAX_DOCS]
    terms = set(WORD.findall(str(query).lower()))
    if not terms:
        return 'Give a search query with at least one word.'
    docs = {}
    for p in files:
        if p.stat().st_size <= MAX_DOC_BYTES:
            docs[p] = p.read_text(errors='replace')
    df = Counter(t for text in docs.values() for t in terms if t in text.lower())
    scored = []
    for p, text in docs.items():
        words = Counter(WORD.findall(text.lower()))
        score = sum((1 + math.log(words[t])) * math.log(1 + len(docs) / df[t]) for t in terms if words[t])
        if score:
            scored.append((score, p, text))
    if not scored:
        return 'No document matches.'
    results = []
    for score, p, text in sorted(scored, key=lambda x: -x[0])[:max_results]:
        low = text.lower()
        at = min((low.find(t) for t in terms if t in low), default=0)
        snippet = text[max(0, at - 150): at + 350].replace('\n', ' ').strip()
        results.append({'file': str(p.relative_to(root)), 'snippet': snippet})
    return json.dumps(results)
