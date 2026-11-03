"""Controller-owned persistent records (guide §5).

Agent payloads (contracts.py) carry content and local handles. The controller
wraps them in records that add IDs, hashes, lineage and frozen bindings. All
records serialise to canonical JSON through `to_dict` and are stored immutably.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from .contracts import (CandidatePayload, Context, PlanPayload, QualityEvaluationPayload,
                        ReferenceComparisonPayload, SourceProfilePayload, PairAnalysisPayload)
from .hashing import payload_hash, new_id
from .ingestion.normalize import NormalizedText, Block, build_normalized_text
from .ingestion.windows import Window


def _nt_to_dict(nt: NormalizedText) -> dict:
    return {'blocks': [b.to_dict() for b in nt.blocks], 'text_hash': nt.text_hash}


def _nt_from_dict(d: dict) -> NormalizedText:
    blocks = [Block(b['block_id'], b['kind'], b['text'], b['order'], b.get('level', 0),
                    tuple(b.get('cells', []))) for b in d['blocks']]
    return build_normalized_text(blocks)


@dataclass
class SourceDocument:
    """A frozen English source with its windows and declared context. Never carries
    the current Arabic reference (guide §4, §17)."""
    source_text_id: str
    normalized: NormalizedText
    windows: list[Window]
    context: Context
    metadata_provenance: dict[str, str]     # field -> supplied|inferred|default
    raw_hash: str | None = None
    origin: str = 'request'                 # request|pair
    pair_id: str | None = None
    extraction_warnings: list[str] = field(default_factory=list)

    @property
    def text_hash(self) -> str:
        return self.normalized.text_hash

    def context_hash(self) -> str:
        return payload_hash(self.context.model_dump(mode='json'))

    def to_dict(self) -> dict:
        return {'source_text_id': self.source_text_id, 'normalized': _nt_to_dict(self.normalized),
                'windows': [w.to_dict() for w in self.windows],
                'context': self.context.model_dump(mode='json'),
                'metadata_provenance': self.metadata_provenance, 'raw_hash': self.raw_hash,
                'origin': self.origin, 'pair_id': self.pair_id,
                'extraction_warnings': self.extraction_warnings}

    @classmethod
    def from_dict(cls, d: dict) -> 'SourceDocument':
        return cls(source_text_id=d['source_text_id'], normalized=_nt_from_dict(d['normalized']),
                   windows=[Window(w['window_id'], tuple(w['owned_block_ids']), tuple(w['context_before_ids']),
                                   tuple(w['context_after_ids']), w.get('content_tokens', 0)) for w in d['windows']],
                   context=Context.model_validate(d['context']), metadata_provenance=d['metadata_provenance'],
                   raw_hash=d.get('raw_hash'), origin=d.get('origin', 'request'), pair_id=d.get('pair_id'),
                   extraction_warnings=d.get('extraction_warnings', []))


@dataclass
class SourceProfile:
    profile_id: str
    payload: SourceProfilePayload
    source_text_id: str
    source_hash: str
    context_hash: str
    window_ids: list[str]
    version: int = 1
    merged_from: list[str] = field(default_factory=list)

    @property
    def profile_hash(self) -> str:
        return payload_hash(self.payload.model_dump(mode='json'))

    def to_dict(self) -> dict:
        return {'profile_id': self.profile_id, 'payload': self.payload.model_dump(mode='json'),
                'source_text_id': self.source_text_id, 'source_hash': self.source_hash,
                'context_hash': self.context_hash, 'window_ids': self.window_ids, 'version': self.version,
                'merged_from': self.merged_from, 'profile_hash': self.profile_hash}

    @classmethod
    def from_dict(cls, d: dict) -> 'SourceProfile':
        return cls(d['profile_id'], SourceProfilePayload.model_validate(d['payload']), d['source_text_id'],
                   d['source_hash'], d['context_hash'], d['window_ids'], d.get('version', 1), d.get('merged_from', []))


@dataclass
class RetrievedExperience:
    experience_id: str
    group_id: str
    kind: str                       # reference_example | agent_attempt
    similarity: float
    scope: dict
    source_excerpt: str             # English source (accepted example)
    arabic_text: NormalizedText     # Arabic example text (memory demonstration)
    source_function: str
    transformation: str
    preserved: list[str]
    domains: list[str]

    def to_dict(self) -> dict:
        return {'experience_id': self.experience_id, 'group_id': self.group_id, 'kind': self.kind,
                'similarity': self.similarity, 'scope': self.scope, 'source_excerpt': self.source_excerpt,
                'arabic_text': _nt_to_dict(self.arabic_text), 'source_function': self.source_function,
                'transformation': self.transformation, 'preserved': self.preserved, 'domains': self.domains}


@dataclass
class RetrievedInsight:
    insight_id: str
    version: int
    importance: int
    similarity: float
    content: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalSnapshot:
    retrieval_id: str
    release_id: str
    release_hash: str
    query_hash: str
    experiences: list[RetrievedExperience]
    insights: list[RetrievedInsight]
    excluded_group_ids: list[str]
    filter_decisions: list[dict]
    token_budget: int | None
    truncation: list[str] = field(default_factory=list)
    embedder: str = ''

    def experience(self, experience_id: str) -> RetrievedExperience | None:
        for e in self.experiences:
            if e.experience_id == experience_id:
                return e
        return None

    def to_dict(self) -> dict:
        return {'retrieval_id': self.retrieval_id, 'release_id': self.release_id, 'release_hash': self.release_hash,
                'query_hash': self.query_hash, 'experiences': [e.to_dict() for e in self.experiences],
                'insights': [i.to_dict() for i in self.insights], 'excluded_group_ids': self.excluded_group_ids,
                'filter_decisions': self.filter_decisions, 'token_budget': self.token_budget,
                'truncation': self.truncation, 'embedder': self.embedder}


@dataclass
class AdaptationPlan:
    plan_id: str
    payload: PlanPayload
    profile_id: str
    retrieval_id: str
    source_hash: str
    release_id: str
    window_ids: list[str]
    version: int = 1
    plan_hash: str = ''            # assigned only after validation (frozen)
    feedback_hash: str | None = None

    def freeze(self) -> None:
        self.plan_hash = payload_hash({'payload': self.payload.model_dump(mode='json'), 'profile_id': self.profile_id,
                                       'retrieval_id': self.retrieval_id, 'source_hash': self.source_hash,
                                       'release_id': self.release_id, 'version': self.version})

    @property
    def frozen(self) -> bool:
        return bool(self.plan_hash)

    def to_dict(self) -> dict:
        return {'plan_id': self.plan_id, 'payload': self.payload.model_dump(mode='json'), 'profile_id': self.profile_id,
                'retrieval_id': self.retrieval_id, 'source_hash': self.source_hash, 'release_id': self.release_id,
                'window_ids': self.window_ids, 'version': self.version, 'plan_hash': self.plan_hash,
                'feedback_hash': self.feedback_hash}


@dataclass
class ArabicCandidate:
    candidate_id: str
    plan_id: str
    plan_hash: str
    source_hash: str
    payload: CandidatePayload
    arabic: NormalizedText              # assembled in source order
    block_map: dict[str, str]           # source_block_id -> output block id
    resolved_realizations: dict[str, list[dict]]   # item_id -> resolved spans
    candidate_hash: str = ''

    def to_dict(self) -> dict:
        return {'candidate_id': self.candidate_id, 'plan_id': self.plan_id, 'plan_hash': self.plan_hash,
                'source_hash': self.source_hash, 'payload': self.payload.model_dump(mode='json'),
                'arabic': _nt_to_dict(self.arabic), 'arabic_text': self.arabic.text, 'block_map': self.block_map,
                'resolved_realizations': self.resolved_realizations, 'candidate_hash': self.candidate_hash}


@dataclass
class EvaluationReport:
    report_id: str
    mode: str
    subject_id: str
    subject_hash: str
    payload: QualityEvaluationPayload | ReferenceComparisonPayload | Any
    critical: bool = False
    deterministic_checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        p = self.payload.model_dump(mode='json') if hasattr(self.payload, 'model_dump') else self.payload
        return {'report_id': self.report_id, 'mode': self.mode, 'subject_id': self.subject_id,
                'subject_hash': self.subject_hash, 'payload': p, 'critical': self.critical,
                'deterministic_checks': self.deterministic_checks}


@dataclass
class PairAnalysis:
    analysis_id: str
    payload: PairAnalysisPayload
    pair_id: str
    profile_id: str
    source_hash: str
    arabic_hash: str

    def to_dict(self) -> dict:
        return {'analysis_id': self.analysis_id, 'payload': self.payload.model_dump(mode='json'),
                'pair_id': self.pair_id, 'profile_id': self.profile_id, 'source_hash': self.source_hash,
                'arabic_hash': self.arabic_hash}


def make_id(prefix: str) -> str:
    return new_id(prefix)
