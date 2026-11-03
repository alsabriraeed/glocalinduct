"""Experience memory (guide §6): reference observations and actual attempts as
distinct variants, append-only, with controller-owned eligibility.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import AttemptExperience, ReferenceExperience
from ..hashing import deterministic_id, payload_hash
from .store import Repository


@dataclass
class ExperienceView:
    experience_id: str
    kind: str
    group_id: str
    phase: str
    outcome: str | None
    eligible: bool
    record: dict[str, Any]
    retrieval: dict[str, Any]      # source_text_hash, arabic_text_hash, domains, genre, scope, summary, transformations


class ExperienceMemory:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------- admission
    def admit_reference(self, record: ReferenceExperience, retrieval_meta: dict[str, Any], run_id: str,
                        parents: list[str] | None = None, phase: str = 'seed') -> str:
        payload = {'record': record.model_dump(mode='json'), 'retrieval': retrieval_meta}
        with self.repo.transaction() as c:
            rid = self.repo.put_object('experience', payload, run_id, parents,
                                       record_id=deterministic_id('exp', payload_hash(payload)))
            c.execute('INSERT INTO experiences VALUES(?,?,?,?,?,?,?)',
                      (rid, 'reference_example', record.group_id, None, phase, None, int(record.eligible)))
        return rid

    def admit_attempt(self, record: AttemptExperience, retrieval_meta: dict[str, Any], run_id: str,
                      parents: list[str] | None = None, supports: list[str] | None = None) -> str:
        payload = {'record': record.model_dump(mode='json'), 'retrieval': retrieval_meta}
        with self.repo.transaction() as c:
            rid = self.repo.put_object('experience', payload, run_id, parents,
                                       record_id=deterministic_id('exp', payload_hash(payload)))
            c.execute('INSERT INTO experiences VALUES(?,?,?,?,?,?,?)',
                      (rid, 'agent_attempt', record.group_id, record.task_id, record.phase, record.outcome,
                       int(record.eligible)))
            for s in supports or []:
                if s == rid:
                    raise ValueError('an attempt cannot cite itself as support')
                c.execute('INSERT OR IGNORE INTO experience_support VALUES(?,?)', (rid, s))
        return rid

    # ---------------------------------------------------------------- lookup
    def get(self, experience_id: str) -> ExperienceView | None:
        row = self.repo.execute('SELECT * FROM experiences WHERE experience_id=?', (experience_id,)).fetchone()
        if row is None:
            return None
        payload = self.repo.payload(experience_id)
        return ExperienceView(experience_id, row['kind'], row['group_id'], row['phase'], row['outcome'],
                              bool(row['eligible']), payload['record'], payload['retrieval'])

    def all(self, eligible_only: bool = False) -> list[ExperienceView]:
        sql = 'SELECT experience_id FROM experiences' + (' WHERE eligible=1' if eligible_only else '') + ' ORDER BY rowid'
        return [self.get(r['experience_id']) for r in self.repo.query(sql)]

    def for_task(self, task_id: str) -> list[ExperienceView]:
        """All attempt experiences of one task (indexed lookup, avoids loading every experience)."""
        return [self.get(r['experience_id']) for r in self.repo.query(
            "SELECT experience_id FROM experiences WHERE kind='agent_attempt' AND task_id=? ORDER BY rowid", (task_id,))]

    def references_in_group(self, group_id: str) -> list[ExperienceView]:
        """Reference experiences of one near-duplicate group."""
        return [self.get(r['experience_id']) for r in self.repo.query(
            "SELECT experience_id FROM experiences WHERE kind='reference_example' AND group_id=? ORDER BY rowid",
            (group_id,))]

    def eligible_ids(self) -> list[str]:
        return [r['experience_id'] for r in self.repo.query('SELECT experience_id FROM experiences WHERE eligible=1 ORDER BY rowid')]

    def supports_of(self, experience_id: str) -> list[str]:
        return [r['support_experience_id'] for r in self.repo.query(
            'SELECT support_experience_id FROM experience_support WHERE experience_id=? ORDER BY support_experience_id',
            (experience_id,))]

    def groups_of(self, experience_ids: list[str]) -> set[str]:
        out = set()
        for e in experience_ids:
            v = self.get(e)
            if v:
                out.add(v.group_id)
        return out

    def domains_of(self, experience_ids: list[str]) -> set[str]:
        out: set[str] = set()
        for e in experience_ids:
            v = self.get(e)
            if v:
                out.update(d for d in v.retrieval.get('domains', []) if d and d != 'general')
        return out
