"""Validated payload contracts for the proposed GlocalInduct implementation.

This module supplies data contracts, not an LLM workflow. Repository-level
checks must additionally resolve IDs, spans, release membership and provenance.
Requires Pydantic 2. Run export_schemas.py to export the provider-neutral schemas.
"""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmpty = Annotated[str, Field(min_length=1)]
Positive = Annotated[int, Field(ge=1)]
NonNegative = Annotated[int, Field(ge=0)]
Strategy = Literal['retain', 'substitute']
Status = Literal['pass', 'fail', 'unknown', 'not_applicable']
LocaleScope = Literal['general_arab', 'specified', 'unspecified']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True,
                              allow_inf_nan=False)


class QuoteRef(Contract):
    text_key: NonEmpty
    block_id: NonEmpty | None
    quote: NonEmpty
    occurrence: NonNegative


class ResolvedSpan(Contract):
    text_hash: NonEmpty
    start: NonNegative
    end: Positive
    quote: NonEmpty

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start or self.end - self.start != len(self.quote):
            raise ValueError('Span length must equal the Unicode code-point quote length')
        return self


class Context(Contract):
    domains: list[NonEmpty]
    genre: NonEmpty | None
    audience: NonEmpty | None
    locale_scope: LocaleScope
    locales: list[NonEmpty]
    setting: NonEmpty | None
    time_period: NonEmpty | None
    objectives: list[NonEmpty]

    @model_validator(mode='after')
    def locales_match(self):
        if (self.locale_scope == 'specified') != bool(self.locales):
            raise ValueError('Specified locale scope requires locales; other scopes require []')
        return self


class DomainTag(Contract):
    domain: NonEmpty
    basis: Literal['supplied', 'inferred']
    evidence: list[QuoteRef]


class Requirement(Contract):
    requirement_id: NonEmpty
    kind: NonEmpty
    description: NonEmpty
    source_quotes: list[QuoteRef]
    metadata_field: NonEmpty | None
    protected: bool

    @model_validator(mode='after')
    def provenance(self):
        if not self.source_quotes and self.metadata_field is None:
            raise ValueError('A requirement needs source evidence or a supplied metadata field')
        return self


class CulturalItem(Contract):
    item_id: NonEmpty
    category: NonEmpty
    source_quotes: Annotated[list[QuoteRef], Field(min_length=1)]
    semantic_function: NonEmpty
    essentiality: Literal['essential', 'incidental', 'uncertain']
    factual_role: NonEmpty
    requirement_ids: list[NonEmpty]
    depends_on_item_ids: list[NonEmpty]


class SourceProfilePayload(Contract):
    purpose: NonEmpty
    purpose_basis: Literal['supplied', 'inferred', 'uncertain']
    domains: Annotated[list[DomainTag], Field(min_length=1)]
    genre: NonEmpty | None
    audience: NonEmpty | None
    requirements: list[Requirement]
    items: list[CulturalItem]
    covered_block_ids: list[NonEmpty]
    uncertainties: list[NonEmpty]

    @model_validator(mode='after')
    def references(self):
        reqs = [r.requirement_id for r in self.requirements]
        items = [i.item_id for i in self.items]
        if len(reqs) != len(set(reqs)) or len(items) != len(set(items)):
            raise ValueError('Requirement and item handles must be unique')
        for item in self.items:
            if not set(item.requirement_ids) <= set(reqs):
                raise ValueError('Unknown requirement handle')
            if not set(item.depends_on_item_ids) <= set(items):
                raise ValueError('Unknown dependency handle')
            if item.item_id in item.depends_on_item_ids:
                raise ValueError('An item cannot depend on itself')
        if len(self.covered_block_ids) != len(set(self.covered_block_ids)):
            raise ValueError('Duplicate covered blocks')
        return self


class Alignment(Contract):
    alignment_id: NonEmpty
    item_ids: list[NonEmpty]
    source_quotes: list[QuoteRef]
    target_quotes: list[QuoteRef]
    observation: Literal['retain', 'substitute', 'ordinary_translation',
                         'stylistic_change', 'omission', 'addition', 'unresolved']
    preserved_requirement_ids: list[NonEmpty]
    violated_requirement_ids: list[NonEmpty]
    uncertainty: NonEmpty | None
    note: NonEmpty

    @model_validator(mode='after')
    def sides(self):
        if not self.source_quotes and not self.target_quotes:
            raise ValueError('An alignment needs at least one observed side')
        if self.observation == 'omission' and (not self.source_quotes or self.target_quotes):
            raise ValueError('Omission needs source evidence and no target quotation')
        if self.observation == 'addition' and (not self.target_quotes or self.source_quotes):
            raise ValueError('Addition needs target evidence and no source quotation')
        if self.observation in {'retain','substitute','ordinary_translation','stylistic_change'}:
            if not self.source_quotes or not self.target_quotes:
                raise ValueError('Resolved bilateral observation needs both sides')
        return self


class SourceOmissionFlag(Contract):
    source_quotes: Annotated[list[QuoteRef], Field(min_length=1)]
    description: NonEmpty


class PairAnalysisPayload(Contract):
    alignments: list[Alignment]
    proposed_scope: 'Scope'
    scope_evidence: list[QuoteRef]
    covered_source_block_ids: list[NonEmpty]
    covered_target_block_ids: list[NonEmpty]
    unresolved_regions: list[QuoteRef]
    newly_noticed_source_items: list[SourceOmissionFlag]
    coverage_notes: list[NonEmpty]

    @model_validator(mode='after')
    def example_scope(self):
        if self.proposed_scope.level == 'universal':
            raise ValueError('A single reference example cannot have universal scope')
        return self


class Scope(Contract):
    level: Literal['universal', 'domain', 'context']
    domains: list[NonEmpty]
    genres: list[NonEmpty]
    culture_specific: bool
    locale_scope: LocaleScope
    locales: list[NonEmpty]
    setting: NonEmpty | None
    time_period: NonEmpty | None
    cross_domain_transfer: bool = True

    @model_validator(mode='after')
    def filters(self):
        if (self.locale_scope == 'specified') != bool(self.locales):
            raise ValueError('Locale scope/list mismatch')
        if self.level == 'domain' and not self.domains:
            raise ValueError('Domain-level scope requires named domains')
        if self.level == 'universal' and (self.domains or self.genres or self.setting
                                          or self.time_period or self.locales):
            raise ValueError('Universal scope cannot contain restrictive filters')
        return self


class InsightContent(Contract):
    lesson: NonEmpty
    applies_when: Annotated[list[NonEmpty], Field(min_length=1)]
    avoid_when: list[NonEmpty]
    recommended_strategy: Strategy | None
    preserve: Annotated[list[NonEmpty], Field(min_length=1)]
    scope: Scope
    support_experience_ids: list[NonEmpty]
    counterevidence_ids: list[NonEmpty]
    origin: Literal['reference', 'attempt', 'mixed']


class InsightOperation(Contract):
    operation_id: NonEmpty
    operation: Literal['ADD', 'EDIT', 'UPVOTE', 'DOWNVOTE']
    target_insight_id: NonEmpty | None
    expected_version: Positive | None
    proposed_content: InsightContent | None
    support_experience_ids: list[NonEmpty]
    counterevidence_ids: list[NonEmpty]
    justification: NonEmpty

    @model_validator(mode='after')
    def operation_shape(self):
        if self.operation == 'ADD':
            if self.target_insight_id is not None or self.expected_version is not None:
                raise ValueError('ADD cannot target an existing insight/version')
        elif self.target_insight_id is None or self.expected_version is None:
            raise ValueError('Non-ADD requires an existing insight and expected version')
        if (self.operation in {'ADD','EDIT'}) != (self.proposed_content is not None):
            raise ValueError('Only ADD/EDIT carry proposed content')
        if not self.support_experience_ids and not self.counterevidence_ids:
            raise ValueError('Every operation needs cited comparison evidence')
        return self


class InsightUpdateBatchPayload(Contract):
    comparison_id: NonEmpty
    operations: list[InsightOperation]

    @model_validator(mode='after')
    def unique_targets(self):
        ids = [o.operation_id for o in self.operations]
        targets = [o.target_insight_id for o in self.operations if o.target_insight_id]
        if len(ids) != len(set(ids)) or len(targets) != len(set(targets)):
            raise ValueError('Unique operation IDs and one operation per existing target required')
        return self


class InsightRef(Contract):
    insight_id: NonEmpty
    version: Positive


class SupportRef(Contract):
    experience_id: NonEmpty
    arabic_quote: QuoteRef
    compatible_function: NonEmpty


class DependentEdit(Contract):
    source_quotes: Annotated[list[QuoteRef], Field(min_length=1)]
    related_item_ids: list[NonEmpty]
    preserve_requirement_ids: list[NonEmpty]
    adjustment: NonEmpty


class Decision(Contract):
    decision_id: NonEmpty
    item_id: NonEmpty
    strategy: Strategy
    counterpart_ar: NonEmpty | None
    canonical_concept: NonEmpty | None
    term_policy: Literal['fixed', 'inflectable']
    support: list[SupportRef]
    insight_refs: list[InsightRef]
    preserve_requirement_ids: list[NonEmpty]
    dependent_edits: list[DependentEdit]
    fallback_reason: NonEmpty | None
    rationale: NonEmpty

    @model_validator(mode='before')
    @classmethod
    def sanitize_strategy_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get('strategy') == 'substitute':
                if data.get('fallback_reason') and not data.get('support') and not data.get('insight_refs'):
                    data['strategy'] = 'retain'
                    data['counterpart_ar'] = None
                    data['canonical_concept'] = None
                else:
                    data['fallback_reason'] = None
            elif data.get('strategy') == 'retain':
                data['counterpart_ar'] = None
                data['canonical_concept'] = None
        return data

    @model_validator(mode='after')
    def strategy_shape(self):
        if self.strategy == 'substitute':
            if self.counterpart_ar is None:
                raise ValueError("Substitution needs counterpart_ar")
            if self.canonical_concept is None:
                raise ValueError("Substitution needs canonical_concept")
        elif self.counterpart_ar is not None or self.canonical_concept is not None:
            raise ValueError('Retention cannot carry a replacement counterpart')
        return self


class TermBinding(Contract):
    source_concept: NonEmpty
    preferred_ar: NonEmpty
    item_ids: list[NonEmpty]
    policy: Literal['fixed', 'inflectable']


class PlanPayload(Contract):
    decisions: list[Decision]
    terminology: list[TermBinding]
    reflection: NonEmpty | None
    unresolved_issues: list[NonEmpty]

    @model_validator(mode='after')
    def one_decision(self):
        items = [d.item_id for d in self.decisions]
        ids = [d.decision_id for d in self.decisions]
        if len(items) != len(set(items)) or len(ids) != len(set(ids)):
            raise ValueError('One unique decision per supplied item')
        return self


class ArabicBlock(Contract):
    source_block_id: NonEmpty
    text_ar: NonEmpty


class ItemRealization(Contract):
    item_id: NonEmpty
    output_quotes: Annotated[list[QuoteRef], Field(min_length=1)]


class CandidatePayload(Contract):
    blocks: Annotated[list[ArabicBlock], Field(min_length=1)]
    realizations: list[ItemRealization]
    unresolved_issues: list[NonEmpty]

    @model_validator(mode='before')
    @classmethod
    def normalize_quotes(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for r in data.get('realizations', []):
                if isinstance(r, dict):
                    for q in r.get('output_quotes', []):
                        if isinstance(q, dict) and 'block_id' in q:
                            bid = q['block_id']
                            tk = q.get('text_key', '')
                            if not tk or not tk.startswith('candidate:'):
                                q['text_key'] = f'candidate:{bid}'
        return data

    @model_validator(mode='after')
    def unique_owners(self):
        blocks = [b.source_block_id for b in self.blocks]
        items = [r.item_id for r in self.realizations]
        if len(blocks) != len(set(blocks)) or len(items) != len(set(items)):
            raise ValueError('Duplicate block owner or item realization')
        return self


class Finding(Contract):
    issue_code: NonEmpty
    severity: Literal['minor', 'substantive', 'critical']
    affected_ids: list[NonEmpty]
    evidence: list[QuoteRef]
    explanation: NonEmpty
    correction_goal: NonEmpty | None


class Criterion(Contract):
    criterion: Literal['meaning_and_purpose', 'cultural_fit', 'arabic_quality']
    status: Status
    score: Annotated[int, Field(ge=0,le=4)] | None
    evidence: list[QuoteRef]
    findings: list[Finding]
    explanation: NonEmpty

    @model_validator(mode='after')
    def score_status(self):
        if self.status in {'unknown','not_applicable'}:
            if self.score is not None:
                raise ValueError('Unknown/not applicable requires null score')
        elif self.score is None:
            raise ValueError('Pass/fail requires a score')
        elif self.status == 'pass' and self.score < 3:
            raise ValueError('Pass needs score >= 3')
        elif self.status == 'fail' and self.score >= 3:
            raise ValueError('Fail needs score <= 2')
        if self.status == 'pass' and any(f.severity in {'substantive','critical'} for f in self.findings):
            raise ValueError('Substantive/critical findings cannot pass')
        return self


class ComparisonDifference(Contract):
    classification: Literal['legitimate_alternative','candidate_defect','reference_defect','uncertain']
    source_quotes: list[QuoteRef] = Field(default_factory=list)
    candidate_quotes: list[QuoteRef] = Field(default_factory=list)
    reference_quotes: list[QuoteRef] = Field(default_factory=list)
    explanation: NonEmpty
    correction_goal: NonEmpty | None = None
    critical: bool = False


class ReferenceComparisonPayload(Contract):
    mode: Literal['reference_comparison'] = 'reference_comparison'
    result: Literal['compatible','incompatible','unknown']
    differences: list[ComparisonDifference] = Field(default_factory=list)
    explanation: NonEmpty

    @model_validator(mode='after')
    def comparison_consistency(self):
        defects = [d for d in self.differences if d.classification in {'candidate_defect', 'reference_defect'}]
        if self.result == 'incompatible' and not defects:
            raise ValueError('Incompatible requires a substantive candidate or reference defect')
        if self.result == 'compatible' and (defects or any(d.critical for d in self.differences)):
            raise ValueError('Compatible cannot retain a candidate defect, reference defect, or critical difference')
        return self


class QualityEvaluationPayload(Contract):
    mode: Literal['reference_quality', 'candidate_quality']
    criteria: Annotated[list[Criterion], Field(max_length=3)]
    reference_comparison: ReferenceComparisonPayload | None = None

    @model_validator(mode='after')
    def mode_applicability(self):
        if not self.criteria:
            # Reference-only evaluation: the comparison with the reference localization is the whole assessment.
            if self.mode != 'candidate_quality' or self.reference_comparison is None:
                raise ValueError('Exactly one result for each of the three criteria is required')
            return self
        names = {c.criterion for c in self.criteria}
        expected = {'meaning_and_purpose','cultural_fit','arabic_quality'}
        if len(self.criteria) != 3 or names != expected:
            raise ValueError('Exactly one result for each of the three criteria is required')
        for c in self.criteria:
            if c.status == 'not_applicable':
                raise ValueError(f'Criterion {c.criterion} cannot be not_applicable')
        return self



class ReviewCheck(Contract):
    check: Literal['evidence_support','applicability','scope','preservation','contradiction_handling']
    status: Literal['pass','fail','unknown']
    experience_ids: list[NonEmpty]
    explanation: NonEmpty


class OperationReview(Contract):
    operation_id: NonEmpty
    checks: Annotated[list[ReviewCheck], Field(min_length=5,max_length=5)]

    @model_validator(mode='after')
    def complete(self):
        names = {c.check for c in self.checks}
        if names != {'evidence_support','applicability','scope','preservation','contradiction_handling'}:
            raise ValueError('Exactly five distinct review checks required')
        return self


class InsightReviewPayload(Contract):
    mode: Literal['insight_review']
    reviews: list[OperationReview]

    @model_validator(mode='after')
    def distinct(self):
        ids = [r.operation_id for r in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate operation review')
        return self


class Feedback(Contract):
    scope: Literal['source_only','reference_informed']
    prior_attempt_id: NonEmpty
    findings: list[Finding]
    # ExpeL `LLM_reflect`: a free-text diagnosis of the failed attempt (None for structured-findings feedback)
    reflection: NonEmpty | None = None


class ReflectionPayload(Contract):
    """Output of the failure-reflection agent: a short natural-language diagnosis for the next attempt."""
    mode: Literal['reflect_failure']
    reflection: NonEmpty


class ReferenceExperience(Contract):
    kind: Literal['reference_example']
    pair_id: NonEmpty
    group_id: NonEmpty
    source_text_id: NonEmpty
    source_profile_id: NonEmpty
    arabic_text_id: NonEmpty
    pair_analysis_id: NonEmpty
    quality_report_id: NonEmpty
    scope: Scope
    eligible: bool
    exclusion_reasons: list[NonEmpty]

    @model_validator(mode='after')
    def eligible_shape(self):
        if self.eligible and self.exclusion_reasons:
            raise ValueError('Eligible reference cannot have exclusion reasons')
        return self


class AttemptExperience(Contract):
    kind: Literal['agent_attempt']
    task_id: NonEmpty
    group_id: NonEmpty
    phase: Literal['practice','deployment','development','test']
    attempt_number: Positive
    previous_attempt_id: NonEmpty | None
    source_profile_id: NonEmpty | None
    release_id: NonEmpty
    retrieval_id: NonEmpty | None
    plan_id: NonEmpty | None
    candidate_id: NonEmpty | None
    quality_report_id: NonEmpty | None
    comparison_report_id: NonEmpty | None
    feedback_received: Feedback | None
    outcome: Literal['success','failure','error']
    eligible: bool
    issue_codes: list[NonEmpty]

    @model_validator(mode='after')
    def attempt_shape(self):
        if (self.attempt_number == 1) != (self.previous_attempt_id is None):
            raise ValueError('Only initial attempts have no previous attempt ID')
        if self.outcome == 'success':
            required = [self.source_profile_id, self.retrieval_id, self.plan_id, self.candidate_id]
            if self.phase != 'practice':
                required.append(self.quality_report_id)   # practice success is decided by the reference comparison
            if not all(required):
                raise ValueError('Success requires complete generation and assessment records')
            if self.phase == 'practice' and self.comparison_report_id is None:
                raise ValueError('Practice success requires reference comparison')
        if self.eligible and (self.outcome != 'success' or self.phase != 'practice'):
            raise ValueError('Only successful practice attempts may be admitted examples')
        if self.outcome == 'error' and not self.issue_codes:
            raise ValueError('Error attempt needs an issue code')
        if self.feedback_received and self.phase != 'practice':
            if self.feedback_received.scope == 'reference_informed':
                raise ValueError('Reference-informed feedback is allowed only in practice')
        return self


class InsightRecord(Contract):
    insight_id: NonEmpty
    version: Positive
    content: InsightContent
    importance: NonNegative
    status: Literal['provisional','active','retired']
    support_group_ids: list[NonEmpty]
    support_domains: list[NonEmpty]
    unresolved_counterevidence: bool
    review_ids: list[NonEmpty]
    event_ids: list[NonEmpty]

    @model_validator(mode='after')
    def activation_shape(self):
        if self.importance == 0 and self.status != 'retired':
            raise ValueError('Zero importance must retire an insight')
        # The support-group, counterevidence and universal-domain gates are admission policy (they depend on
        # `learning.insight_admission`) and are enforced by InsightMemory._admission, not by this data contract.
        if self.status == 'active' and not self.review_ids:
            raise ValueError('Active insight requires a review record')
        return self


class Usage(Contract):
    model_calls: NonNegative
    input_tokens: NonNegative
    output_tokens: NonNegative
    elapsed_seconds: Annotated[float, Field(ge=0)]
    cost: Annotated[float, Field(ge=0)] | None
    currency: NonEmpty | None


class RunResult(Contract):
    request_id: NonEmpty
    release_id: NonEmpty
    status: Literal['accepted','conservative','unresolved']
    arabic_text: NonEmpty | None
    plan_ids: list[NonEmpty]
    candidate_ids: list[NonEmpty]
    evaluation_ids: list[NonEmpty]
    fallback_reasons: list[NonEmpty]
    issue_codes: list[NonEmpty]
    usage: Usage

    @model_validator(mode='after')
    def final_shape(self):
        if self.status == 'unresolved':
            if self.arabic_text is not None or not self.issue_codes:
                raise ValueError('Unresolved exposes no Arabic text and requires issue codes')
        else:
            # evaluation_ids may be empty: text with no matching reference localization is released unevaluated
            if self.arabic_text is None or not self.plan_ids or not self.candidate_ids:
                raise ValueError('Accepted output requires text and complete record references')
            if self.issue_codes:
                raise ValueError('Accepted/conservative results cannot carry blocking issue codes')
        if self.status == 'conservative' and not self.fallback_reasons:
            raise ValueError('Conservative needs a disclosed safe-retention reason')
        if self.status == 'accepted' and self.fallback_reasons:
            raise ValueError('Fallback output is conservative, not accepted')
        return self


SCHEMA_MODELS = [Context, ResolvedSpan, SourceProfilePayload, PairAnalysisPayload,
                 InsightUpdateBatchPayload, PlanPayload, CandidatePayload,
                 QualityEvaluationPayload, ReferenceComparisonPayload,
                 InsightReviewPayload, ReferenceExperience, AttemptExperience,
                 InsightRecord, RunResult]
