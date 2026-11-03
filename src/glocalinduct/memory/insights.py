"""Insight memory and reviewed update transactions (guide §7, storage README).

Operations follow ExpeL's importance mechanism (ADD=2, UPVOTE +1, DOWNVOTE −1,
EDIT +1 with a new version). The controller adds admission gates: positive
importance, every A5 review check passing, >= 2 independent support groups
(>= 2 domains for universal scope), and no unresolved material counterevidence.
Every state change creates an immutable version; one committed operation per
(comparison_id, insight_id) across the lineage; stale expected versions reject
the whole batch atomically.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (InsightContent, InsightRecord, InsightReviewPayload, InsightUpdateBatchPayload,
                         InsightOperation)
from ..hashing import payload_hash, new_id, deterministic_id
from .experience import ExperienceMemory
from .store import Repository

COMPARISON_POLICY_VERSION = 'comparison-1.0'


def comparison_id(kind: str, experience_ids: list[str], payload_hashes: list[str]) -> str:
    pairs = sorted(zip(experience_ids, payload_hashes))
    return deterministic_id('cmp', kind, *[f'{e}:{h}' for e, h in pairs], COMPARISON_POLICY_VERSION)


def add_fingerprint(content: InsightContent) -> str:
    return payload_hash({'lesson': content.lesson.strip().casefold(),
                         'scope': content.scope.model_dump(mode='json')})


@dataclass
class ApplyResult:
    committed: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    replayed: bool = False


class StaleBatch(Exception):
    pass


class InsightMemory:
    def __init__(self, repo: Repository, experiences: ExperienceMemory, min_support_groups: int = 2,
                 universal_min_domains: int = 2, initial_importance: int = 2, immediate_admission: bool = False):
        self.repo = repo
        self.experiences = experiences
        self.min_support_groups = min_support_groups
        self.universal_min_domains = universal_min_domains
        self.initial_importance = initial_importance
        self.immediate_admission = immediate_admission   # ExpeL: no provisional state, importance <= 0 removes

    # ----------------------------------------------------------------- reads
    def head(self, insight_id: str) -> InsightRecord | None:
        row = self.repo.execute('SELECT version FROM insight_heads WHERE insight_id=?', (insight_id,)).fetchone()
        if row is None:
            return None
        return self.version(insight_id, row['version'])

    def version(self, insight_id: str, version: int) -> InsightRecord | None:
        row = self.repo.execute('SELECT record_id FROM insight_versions WHERE insight_id=? AND version=?',
                                (insight_id, version)).fetchone()
        if row is None:
            return None
        return InsightRecord.model_validate(self.repo.payload(row['record_id']))

    def heads(self, status: str | None = None) -> list[InsightRecord]:
        out = []
        for r in self.repo.query('SELECT insight_id FROM insight_heads ORDER BY insight_id'):
            rec = self.head(r['insight_id'])
            if rec and (status is None or rec.status == status):
                out.append(rec)
        return out

    def lineage(self, insight_id: str) -> list[InsightRecord]:
        return [self.version(insight_id, r['version']) for r in self.repo.query(
            'SELECT version FROM insight_versions WHERE insight_id=? ORDER BY version', (insight_id,))]

    # --------------------------------------------------------------- helpers
    def _admission(self, content: InsightContent, importance: int, support_ids: list[str],
                   counter_ids: list[str], has_review: bool) -> tuple[str, list[str], list[str], bool]:
        groups = sorted(self.experiences.groups_of(support_ids))
        domains = sorted(self.experiences.domains_of(support_ids))
        unresolved_counter = bool(counter_ids)
        if importance <= 0:
            return 'retired', groups, domains, unresolved_counter
        if self.immediate_admission:
            return 'active', groups, domains, unresolved_counter
        if not has_review or len(groups) < self.min_support_groups or unresolved_counter:
            return 'provisional', groups, domains, unresolved_counter
        if content.scope.level == 'universal' and len(domains) < self.universal_min_domains:
            return 'provisional', groups, domains, unresolved_counter
        return 'active', groups, domains, unresolved_counter

    def _normalize_operation_evidence(self, op: InsightOperation, allowed_ids: set[str]) -> InsightOperation:
        """Partition cited packet IDs so failed attempts in failure_success packets are placed in counterevidence_ids."""
        pos_ids: list[str] = []
        neg_ids: list[str] = list(op.counterevidence_ids)
        for e in op.support_experience_ids:
            view = self.experiences.get(e) if e in allowed_ids else None
            if view is not None and view.kind == 'agent_attempt' and view.outcome == 'failure':
                if e not in neg_ids:
                    neg_ids.append(e)
            else:
                pos_ids.append(e)
        if not pos_ids and op.operation != 'DOWNVOTE':
            for e in sorted(allowed_ids):
                view = self.experiences.get(e)
                if view is not None and view.eligible and (view.kind != 'agent_attempt' or view.outcome == 'success'):
                    pos_ids.append(e)
        if pos_ids != list(op.support_experience_ids) or neg_ids != list(op.counterevidence_ids):
            new_content = op.proposed_content
            if new_content is not None:
                new_content = new_content.model_copy(update={
                    'support_experience_ids': pos_ids,
                    'counterevidence_ids': neg_ids,
                })
            op = op.model_copy(update={
                'support_experience_ids': pos_ids,
                'counterevidence_ids': neg_ids,
                'proposed_content': new_content,
            })
        return op

    def _validate_evidence(self, op: InsightOperation, allowed_ids: set[str]) -> str | None:
        for e in op.support_experience_ids + op.counterevidence_ids:
            if e not in allowed_ids:
                return f'evidence {e} is not in the comparison packet'
            view = self.experiences.get(e)
            if view is None:
                return f'evidence {e} does not exist'
            if view.kind == 'agent_attempt' and view.outcome == 'error':
                return f'evidence {e} is an error attempt (not cultural evidence)'
        for e in op.support_experience_ids:
            view = self.experiences.get(e)
            if view.kind == 'agent_attempt' and view.outcome != 'success':
                return f'support {e} is a failed attempt and cannot be positive support'
            if not view.eligible:
                return f'support {e} is not an admitted learning record'
        return None

    # ------------------------------------------------------------- apply batch
    def apply_batch(self, batch: InsightUpdateBatchPayload, review: InsightReviewPayload,
                    packet_experience_ids: list[str], run_id: str,
                    proposal_record_id: str | None = None, review_record_id: str | None = None,
                    ignore_duplicate_add: bool = False) -> ApplyResult:
        """Validate the whole batch, then apply it in one transaction."""
        result = ApplyResult()
        reviews = {r.operation_id: r for r in review.reviews}
        allowed = set(packet_experience_ids)
        if proposal_record_id is None:
            proposal_record_id = self.repo.put_object('insight_proposal', batch.model_dump(mode='json'), run_id)
        if review_record_id is None:
            review_record_id = self.repo.put_object('insight_review', review.model_dump(mode='json'), run_id)

        # idempotent replay: the identical batch (same proposal payload) was already committed
        batch_hash = payload_hash(batch.model_dump(mode='json'))
        for r in self.repo.query('SELECT DISTINCT proposal_record_id FROM insight_events WHERE comparison_id=?',
                                 (batch.comparison_id,)):
            prior = self.repo.get_object(r['proposal_record_id'])
            if prior and prior['payload_hash'] == batch_hash:
                result.replayed = True
                return result

        with self.repo.transaction() as c:
            plan: list[tuple[InsightOperation, dict]] = []
            seen_add_fps: set[str] = set()
            seen_lineage_ids: set[str] = set()
            for op in batch.operations:
                op = self._normalize_operation_evidence(op, allowed)
                # If requested, silently ignore if already committed in memory or duplicate in this batch
                if op.operation == 'ADD' and op.proposed_content is not None and ignore_duplicate_add:
                    fp = add_fingerprint(op.proposed_content)
                    if fp in seen_add_fps or c.execute('SELECT 1 FROM insight_events WHERE add_fingerprint=?', (fp,)).fetchone():
                        continue

                rv = reviews.get(op.operation_id)
                reason = None
                if rv is None:
                    reason = 'no A5 review for this operation'
                elif any(ch.status != 'pass' for ch in rv.checks):
                    failed = [ch.check + '=' + ch.status for ch in rv.checks if ch.status != 'pass']
                    reason = 'review not passed: ' + ', '.join(failed)
                if reason is None:
                    reason = self._validate_evidence(op, allowed)
                if reason is None and op.operation != 'ADD':
                    head = self.head(op.target_insight_id)
                    if head is None:
                        reason = f'target insight {op.target_insight_id} does not exist'
                    elif head.version != op.expected_version:
                        raise StaleBatch(f'expected version {op.expected_version} of {op.target_insight_id} '
                                         f'but head is {head.version}')
                    elif (op.target_insight_id in seen_lineage_ids
                          or c.execute('SELECT 1 FROM insight_events WHERE comparison_id=? AND insight_id=?',
                                       (batch.comparison_id, op.target_insight_id)).fetchone()):
                        reason = 'this comparison already committed an operation on this insight lineage'
                    elif head.status == 'retired':
                        reason = 'target insight is retired'
                if reason is None and op.operation == 'ADD':
                    fp = add_fingerprint(op.proposed_content)
                    if c.execute('SELECT 1 FROM insight_events WHERE add_fingerprint=?', (fp,)).fetchone():
                        reason = 'identical lesson/scope already added (fingerprint duplicate)'
                if reason:
                    rid = self.repo.put_object('rejected_proposal', {'comparison_id': batch.comparison_id,
                                                                     'operation': op.model_dump(mode='json'),
                                                                     'reason': reason}, run_id)
                    result.rejected.append({'operation_id': op.operation_id, 'reason': reason, 'record_id': rid})
                    continue
                if op.operation == 'ADD' and op.proposed_content is not None:
                    seen_add_fps.add(add_fingerprint(op.proposed_content))
                elif op.target_insight_id is not None:
                    seen_lineage_ids.add(op.target_insight_id)
                plan.append((op, {}))

            for op, _ in plan:
                if op.operation == 'ADD':
                    content = op.proposed_content
                    fp = add_fingerprint(content)
                    insight_id = deterministic_id('ins', fp)
                    from_version, version = None, 1
                    importance = self.initial_importance
                    support = list(dict.fromkeys(op.support_experience_ids))
                    counter = list(dict.fromkeys(op.counterevidence_ids))
                else:
                    head = self.head(op.target_insight_id)
                    insight_id, from_version, version = head.insight_id, head.version, head.version + 1
                    prev_support = list(head.content.support_experience_ids)
                    prev_counter = list(head.content.counterevidence_ids)
                    fp = None
                    if op.operation == 'UPVOTE':
                        content = head.content
                        importance = head.importance + 1
                        support = list(dict.fromkeys(prev_support + op.support_experience_ids))
                        counter = prev_counter
                    elif op.operation == 'DOWNVOTE':
                        content = head.content
                        importance = max(0, head.importance - 1)
                        support = prev_support
                        counter = list(dict.fromkeys(prev_counter + op.counterevidence_ids))
                    else:  # EDIT: support is rechecked from the evidence cited for the new wording
                        content = op.proposed_content
                        importance = head.importance + 1
                        support = list(dict.fromkeys(op.support_experience_ids))
                        counter = list(dict.fromkeys(op.counterevidence_ids))
                content = content.model_copy(update={'support_experience_ids': support,
                                                     'counterevidence_ids': counter})
                status, groups, domains, unresolved = self._admission(content, importance, support, counter, True)
                record = InsightRecord(insight_id=insight_id, version=version, content=content, importance=importance,
                                       status=status, support_group_ids=groups, support_domains=domains,
                                       unresolved_counterevidence=unresolved, review_ids=[review_record_id],
                                       event_ids=[])
                rid = self.repo.put_object('insight_version', record.model_dump(mode='json'), run_id,
                                           [proposal_record_id, review_record_id])
                c.execute('INSERT INTO insight_versions VALUES(?,?,?,?,?)', (insight_id, version, rid, importance, status))
                c.execute('INSERT INTO insight_heads(insight_id,version) VALUES(?,?) '
                          'ON CONFLICT(insight_id) DO UPDATE SET version=excluded.version', (insight_id, version))
                for e in support:
                    c.execute('INSERT OR IGNORE INTO insight_evidence VALUES(?,?,?,?)', (insight_id, version, e, 'support'))
                for e in counter:
                    c.execute('INSERT OR IGNORE INTO insight_evidence VALUES(?,?,?,?)',
                              (insight_id, version, e, 'counterevidence'))
                event_id = new_id('evt')
                try:
                    c.execute('INSERT INTO insight_events VALUES(?,?,?,?,?,?,?,?,?)',
                              (event_id, batch.comparison_id, insight_id, op.operation, from_version, version,
                               proposal_record_id, review_record_id, fp))
                except sqlite3.IntegrityError as e:
                    raise StaleBatch(f'duplicate event for comparison {batch.comparison_id}: {e}') from e
                result.committed.append({'operation_id': op.operation_id, 'insight_id': insight_id,
                                         'version': version, 'importance': importance, 'status': status,
                                         'event_id': event_id})
        return result
