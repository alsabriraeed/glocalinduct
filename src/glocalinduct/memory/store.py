"""SQLite repository (pack `storage/schema.sql`) and content-addressed text stores
(guide §15). Only the controller writes. One serialised writer, parameterised SQL,
foreign keys on every connection.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..hashing import canonical_json, payload_hash, new_id, utc_now, sha256_text
from ..ingestion.normalize import NormalizedText, build_normalized_text, Block

SCHEMA_SQL = Path(__file__).resolve().parents[3] / 'storage' / 'schema.sql'
SCHEMA_VERSION = '1.0'


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if str(self.db_path) != ':memory:':
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA foreign_keys=ON')
        self._conn.executescript(SCHEMA_SQL.read_text(encoding='utf-8'))
        self._conn.execute('PRAGMA foreign_keys=ON')
        self._in_tx = 0

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE; nested calls join the outer transaction."""
        with self._lock:
            if self._in_tx == 0:
                self._conn.execute('BEGIN IMMEDIATE')
            self._in_tx += 1
            try:
                yield self._conn
            except BaseException:
                self._in_tx -= 1
                if self._in_tx == 0:
                    self._conn.execute('ROLLBACK')
                raise
            else:
                self._in_tx -= 1
                if self._in_tx == 0:
                    self._conn.execute('COMMIT')

    # ---------------------------------------------------------------- objects
    def put_object(self, record_type: str, payload: dict[str, Any], run_id: str,
                   parents: list[str] | None = None, record_id: str | None = None) -> str:
        rid = record_id or new_id(record_type[:4])
        js = canonical_json(payload)
        with self.transaction() as c:
            c.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                      (rid, record_type, SCHEMA_VERSION, run_id, utc_now(), payload_hash(payload), js))
            for p in parents or []:
                c.execute('INSERT OR IGNORE INTO object_parents VALUES(?,?)', (rid, p))
        return rid

    def get_object(self, record_id: str) -> dict[str, Any] | None:
        row = self._conn.execute('SELECT * FROM objects WHERE record_id=?', (record_id,)).fetchone()
        if row is None:
            return None
        return {'record_id': row['record_id'], 'record_type': row['record_type'],
                'schema_version': row['schema_version'], 'run_id': row['run_id'],
                'created_at': row['created_at'], 'payload_hash': row['payload_hash'],
                'payload': json.loads(row['payload_json'])}

    def payload(self, record_id: str) -> dict[str, Any]:
        obj = self.get_object(record_id)
        if obj is None:
            raise KeyError(record_id)
        return obj['payload']

    def objects_of_type(self, record_type: str) -> list[dict[str, Any]]:
        rows = self._conn.execute('SELECT * FROM objects WHERE record_type=? ORDER BY created_at, record_id',
                                  (record_type,)).fetchall()
        return [{'record_id': row['record_id'], 'record_type': row['record_type'],
                 'schema_version': row['schema_version'], 'run_id': row['run_id'],
                 'created_at': row['created_at'], 'payload_hash': row['payload_hash'],
                 'payload': json.loads(row['payload_json'])} for row in rows]

    def parents(self, record_id: str) -> list[str]:
        return [r['parent_id'] for r in self._conn.execute(
            'SELECT parent_id FROM object_parents WHERE record_id=? ORDER BY parent_id', (record_id,))]

    # ------------------------------------------------------------ checkpoints
    def checkpoint(self, run_id: str, stage_key: str, cache_key: str, record_id: str) -> None:
        with self.transaction() as c:
            c.execute('INSERT OR IGNORE INTO run_checkpoints VALUES(?,?,?,?)', (run_id, stage_key, cache_key, record_id))

    def find_checkpoint(self, run_id: str, stage_key: str) -> str | None:
        row = self._conn.execute('SELECT record_id FROM run_checkpoints WHERE run_id=? AND stage_key=?',
                                 (run_id, stage_key)).fetchone()
        return row['record_id'] if row else None

    def find_by_cache_key(self, cache_key: str) -> str | None:
        """Resume a completed immutable stage from any run whose full cache key matches."""
        row = self._conn.execute('SELECT record_id FROM run_checkpoints WHERE cache_key=? ORDER BY rowid LIMIT 1',
                                 (cache_key,)).fetchone()
        return row['record_id'] if row else None

    def checkpoints(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute('SELECT * FROM run_checkpoints WHERE run_id=? ORDER BY rowid',
                                                    (run_id,))]

    # ------------------------------------------------------------- raw access
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()


class TextStore:
    """Content-addressed store for normalised texts. Online bundle builders may
    resolve texts here; they must never resolve the private reference store."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, nt: NormalizedText, kind: str = 'source') -> str:
        path = self.root / f'{nt.text_hash}.json'
        if not path.exists():
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'kind': kind, 'text_hash': nt.text_hash,
                                       'blocks': [b.to_dict() for b in nt.blocks]}, ensure_ascii=False), 'utf-8')
            tmp.replace(path)
        return nt.text_hash

    def get(self, text_hash: str) -> NormalizedText:
        d = json.loads((self.root / f'{text_hash}.json').read_text('utf-8'))
        blocks = [Block(b['block_id'], b['kind'], b['text'], b['order'], b.get('level', 0), tuple(b.get('cells', [])))
                  for b in d['blocks']]
        nt = build_normalized_text(blocks)
        if nt.text_hash != text_hash:
            raise ValueError(f'text store corruption for {text_hash}')
        return nt

    def has(self, text_hash: str) -> bool:
        return (self.root / f'{text_hash}.json').exists()


class PrivateReferenceStore(TextStore):
    """Separate access path for current Arabic references (guide §15). Only A1
    pair analysis, A5 reference_quality and A5 reference_comparison callers use it."""
