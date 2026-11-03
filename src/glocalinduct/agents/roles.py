"""One Python wrapper per agent role/mode (guide §2, §14).

Each wrapper builds the allowlisted bundle, calls the runner, applies the
deterministic validators, and returns a controller record. Agents return
findings and proposals; the controller owns IDs, hashes and acceptance.
"""
from __future__ import annotations

from typing import Any

from ..contracts import (CandidatePayload, Feedback, InsightReviewPayload, InsightUpdateBatchPayload,
                         PairAnalysisPayload, PlanPayload, QualityEvaluationPayload, ReferenceComparisonPayload,
                         ReflectionPayload, SourceProfilePayload)
from ..errors import SchemaInvalid
from ..hashing import new_id, payload_hash
from ..ingestion.normalize import NormalizedText
from ..ingestion.windows import Window
from ..records import (AdaptationPlan, ArabicCandidate, EvaluationReport, PairAnalysis, RetrievalSnapshot,
                       SourceDocument, SourceProfile)
from ..validation import candidates as cand_val
from ..validation import plans as plan_val
from ..validation.policies import DomainPolicies, ResolvedPolicy
from ..validation.spans import TextRegistry, resolve_quote
from . import bundles
from .runner import AgentRunner, Budget, UsageMeter


class Agents:
    def __init__(self, runner: AgentRunner, policies: DomainPolicies):
        self.runner = runner
        self.policies = policies

    def policy_for(self, source: SourceDocument, profile: SourceProfile | None = None) -> ResolvedPolicy:
        domains = list(source.context.domains)
        if profile is not None:
            domains += [t.domain for t in profile.payload.domains]
        return self.policies.resolve(domains)

    # ------------------------------------------------------------------ A3
    async def analyse_source(self, source: SourceDocument, window: Window | None, meter: UsageMeter,
                             budget: Budget, raw_sink=None) -> tuple[SourceProfilePayload, dict, list[str]]:
        policy = self.policy_for(source)
        bundle = bundles.source_analysis_bundle(source, window, policy)
        payload, rec = await self.runner.call('analyse_source', bundle, SourceProfilePayload, policy.to_dict(),
                                              meter, budget, raw_sink)
        owned = list(window.owned_block_ids) if window else None
        warnings = plan_val.validate_profile(payload, source, owned)
        return payload, rec, warnings

    async def make_plan(self, source: SourceDocument, window: Window | None, profile: SourceProfile,
                        retrieval: RetrievalSnapshot, meter: UsageMeter, budget: Budget,
                        feedback: list[Feedback] | None = None, raw_sink=None,
                        reconciliation: dict | None = None) -> tuple[PlanPayload, dict]:
        policy = self.policy_for(source, profile)
        bundle = bundles.plan_bundle(source, window, profile, retrieval, policy, feedback, reconciliation)
        payload, rec = await self.runner.call('make_plan', bundle, PlanPayload, policy.to_dict(), meter, budget,
                                              raw_sink)
        return payload, rec

    # ------------------------------------------------------------------ A4
    async def generate_arabic(self, source: SourceDocument, window: Window | None, profile: SourceProfile,
                              plan: AdaptationPlan, meter: UsageMeter, budget: Budget,
                              feedback: list[Feedback] | None = None, raw_sink=None) -> tuple[CandidatePayload, dict]:
        assert plan.frozen, 'plan must be frozen before generation'
        policy = self.policy_for(source, profile)
        bundle = bundles.generation_bundle(source, window, profile, plan, policy, feedback)
        payload, rec = await self.runner.call('generate_arabic', bundle, CandidatePayload, policy.to_dict(), meter,
                                              budget, raw_sink)
        owned = list(window.owned_block_ids) if window else source.normalized.block_ids()
        cand_val.validate_candidate(payload, owned, profile.payload, plan.payload)
        return payload, rec

    # ------------------------------------------------------------------ A5
    async def evaluate_candidate(self, source: SourceDocument, window: Window | None, profile: SourceProfile,
                                 plan: AdaptationPlan, candidate_blocks: list[dict], retrieval: RetrievalSnapshot,
                                 meter: UsageMeter, budget: Budget, raw_sink=None,
                                 consistency: ArabicCandidate | None = None,
                                 reference: NormalizedText | None = None) -> tuple[QualityEvaluationPayload, dict]:
        policy = self.policy_for(source, profile)
        if consistency is not None:
            bundle = bundles.consistency_bundle(source, profile, plan, consistency, retrieval, policy, reference=reference)
        else:
            bundle = bundles.candidate_quality_bundle(source, window, profile, plan, candidate_blocks, retrieval, policy,
                                                      reference=reference)
        payload, rec = await self.runner.call('candidate_quality', bundle, QualityEvaluationPayload, policy.to_dict(),
                                              meter, budget, raw_sink)
        if len(payload.criteria) != 3:
            raise SchemaInvalid('candidate_quality (no reference) must return all three criteria',
                                details={'mode': 'candidate_quality'})
        self._check_evidence(payload, bundle)
        return payload, rec

    async def evaluate_against_reference(self, source: SourceDocument, profile: SourceProfile,
                                         candidate: ArabicCandidate, reference: NormalizedText, meter: UsageMeter,
                                         budget: Budget, raw_sink=None) -> tuple[QualityEvaluationPayload, dict]:
        """Reference-only Quality evaluator: the comparison with the reference localization is the whole assessment."""
        cmp_payload, rec = await self.compare_reference(source, profile, candidate, reference, meter, budget, raw_sink)
        return QualityEvaluationPayload(mode='candidate_quality', criteria=[], reference_comparison=cmp_payload), rec

    def _check_evidence(self, payload: QualityEvaluationPayload, bundle: dict) -> None:
        reg = TextRegistry()
        from ..ingestion.normalize import Block, build_normalized_text
        for key, t in bundle['texts'].items():
            reg.add(key, build_normalized_text([Block(b['block_id'], b.get('kind', 'paragraph'), b['text'], i)
                                                 for i, b in enumerate(t['blocks'])]))
        for c in payload.criteria:
            for q in c.evidence:
                resolve_quote(reg, q, strict=False)
            for f in c.findings:
                for q in f.evidence:
                    resolve_quote(reg, q, strict=False)
        if payload.reference_comparison is not None:
            for d in payload.reference_comparison.differences:
                for q in d.source_quotes + d.candidate_quotes + d.reference_quotes:
                    resolve_quote(reg, q, strict=False)

    async def analyse_pair(self, source: SourceDocument, reference: NormalizedText, profile: SourceProfile,
                           meter: UsageMeter, budget: Budget, raw_sink=None) -> tuple[PairAnalysisPayload, dict]:
        policy = self.policy_for(source, profile)
        bundle = bundles.pair_analysis_bundle(source, reference, profile, policy)
        payload, rec = await self.runner.call('analyse_pair', bundle, PairAnalysisPayload, policy.to_dict(), meter,
                                              budget, raw_sink)
        reg = TextRegistry(); reg.add('source', source.normalized); reg.add('reference', reference)
        for a in payload.alignments:
            for q in a.source_quotes + a.target_quotes:
                resolve_quote(reg, q, strict=False)
        for q in payload.scope_evidence + payload.unresolved_regions:
            resolve_quote(reg, q, strict=False)
        for f in payload.newly_noticed_source_items:
            for q in f.source_quotes:
                if q.text_key != 'source':
                    raise ValueError('newly noticed items must cite English source only')
                resolve_quote(reg, q, strict=False)
        return payload, rec

    async def assess_reference(self, source: SourceDocument, reference: NormalizedText, profile: SourceProfile,
                               analysis: PairAnalysis, meter: UsageMeter, budget: Budget,
                               raw_sink=None) -> tuple[QualityEvaluationPayload, dict]:
        policy = self.policy_for(source, profile)
        bundle = bundles.reference_quality_bundle(source, reference, profile, analysis.payload.model_dump(mode='json'),
                                                  policy)
        payload, rec = await self.runner.call('reference_quality', bundle, QualityEvaluationPayload, policy.to_dict(),
                                              meter, budget, raw_sink)
        self._check_evidence(payload, bundle)
        return payload, rec

    async def compare_reference(self, source: SourceDocument, profile: SourceProfile, candidate: ArabicCandidate,
                                reference: NormalizedText, meter: UsageMeter, budget: Budget,
                                raw_sink=None) -> tuple[ReferenceComparisonPayload, dict]:
        frozen_hash = candidate.candidate_hash
        policy = self.policy_for(source, profile)
        bundle = bundles.reference_comparison_bundle(source, profile, candidate, reference, policy)
        payload, rec = await self.runner.call('reference_comparison', bundle, ReferenceComparisonPayload,
                                              policy.to_dict(), meter, budget, raw_sink)
        payload = _reconcile_reference_comparison(payload, candidate, reference)
        assert candidate.candidate_hash == frozen_hash, 'reference comparison must not alter the candidate'
        return payload, rec

    # ------------------------------------------------------------------ A6
    async def reflect_failure(self, source: SourceDocument, profile: SourceProfile, attempt: dict, signals: dict,
                              earlier_reflections: list[str], meter: UsageMeter, budget: Budget, raw_sink=None,
                              forbidden_texts: list[str] | None = None,
                              glocalized_arabic: str | None = None) -> tuple[ReflectionPayload, dict]:
        policy = self.policy_for(source, profile)
        bundle = bundles.reflection_bundle(source, profile, attempt, signals, earlier_reflections, policy,
                                           forbidden_texts, glocalized_arabic)
        payload, rec = await self.runner.call('reflect_failure', bundle, ReflectionPayload, policy.to_dict(), meter,
                                              budget, raw_sink)
        return payload, rec

    # ------------------------------------------------------------------ A2
    async def propose_updates(self, packet: dict, existing: list[dict], domains: list[str], meter: UsageMeter,
                              budget: Budget, raw_sink=None) -> tuple[InsightUpdateBatchPayload, dict]:
        policy = self.policies.resolve(domains)
        bundle = bundles.insight_update_bundle(packet, existing, policy)
        payload, rec = await self.runner.call('propose_updates', bundle, InsightUpdateBatchPayload, policy.to_dict(),
                                              meter, budget, raw_sink)
        if payload.comparison_id != packet['comparison_id']:
            raise ValueError('A2 must echo the controller-assigned comparison_id')
        payload = _normalize_insight_proposal(payload, packet, domains)
        return payload, rec

    async def review_updates(self, proposal: InsightUpdateBatchPayload, packet: dict, existing: list[dict],
                             admission: dict, domains: list[str], meter: UsageMeter, budget: Budget,
                             raw_sink=None) -> tuple[InsightReviewPayload, dict]:
        policy = self.policies.resolve(domains)
        bundle = bundles.insight_review_bundle(proposal.model_dump(mode='json'), packet, existing, admission)
        payload, rec = await self.runner.call('insight_review', bundle, InsightReviewPayload, policy.to_dict(), meter,
                                              budget, raw_sink)
        payload = _reconcile_insight_review(payload, proposal)
        return payload, rec


def _reconcile_reference_comparison(
    payload: ReferenceComparisonPayload,
    candidate: ArabicCandidate,
    reference: NormalizedText,
) -> ReferenceComparisonPayload:
    """Ensure the human glocalized reference is treated as authoritative ground truth.

    Never allow a candidate to be marked incompatible when its normalized Arabic matches
    the reference, or when a reported 'candidate_defect' penalizes a phrase that actually
    matches the reference's own cultural adaptation.
    """
    from ..ingestion.normalize import matching_text

    cand_norm = matching_text(candidate.arabic.text)
    ref_norm = matching_text(reference.text)
    if cand_norm and cand_norm == ref_norm:
        return payload.model_copy(update={
            'result': 'compatible',
            'differences': [],
            'explanation': 'Candidate matches the authoritative glocalized Arabic reference.',
        })

    if not payload.differences:
        return payload

    kept_diffs = []
    changed = False
    for d in payload.differences:
        if d.classification in ('candidate_defect', 'reference_defect') and d.candidate_quotes:
            c_quotes_norm = [matching_text(q.quote) for q in d.candidate_quotes if matching_text(q.quote)]
            if c_quotes_norm and all(cq in ref_norm for cq in c_quotes_norm):
                changed = True
                continue
        kept_diffs.append(d)

    if changed:
        has_defect = any(d.classification == 'candidate_defect' or d.critical for d in kept_diffs)
        return payload.model_copy(update={
            'result': 'incompatible' if has_defect else 'compatible',
            'differences': kept_diffs,
            'explanation': (
                payload.explanation
                if has_defect
                else 'Candidate aligns with the cultural adaptations of the authoritative glocalized reference.'
            ),
        })
    return payload


def _normalize_insight_proposal(
    payload: InsightUpdateBatchPayload,
    packet: dict,
    domains: list[str],
) -> InsightUpdateBatchPayload:
    """Normalize proposed insight scopes so single-domain evidence is not rejected for 'universal' overclaim."""
    from ..contracts import Scope

    records_by_id = {r['experience_id']: r for r in packet.get('records', [])}
    normalized_ops = []
    changed = False
    for op in payload.operations:
        content = op.proposed_content
        if content is not None:
            sc = content.scope
            support_domains = sorted({
                d
                for eid in (op.support_experience_ids or content.support_experience_ids)
                for d in records_by_id.get(eid, {}).get('domains', [])
                if d
            }) or sorted(set(domains)) or ['general']
            locale_scope = sc.locale_scope
            if sc.culture_specific and locale_scope == 'unspecified' and not sc.locales:
                locale_scope = 'general_arab'
            if sc.level == 'universal' and len(support_domains) < 2:
                new_scope = Scope(
                    level='domain',
                    domains=support_domains,
                    genres=list(sc.genres),
                    culture_specific=sc.culture_specific,
                    locale_scope=locale_scope,
                    locales=list(sc.locales),
                    setting=sc.setting,
                    time_period=sc.time_period,
                    cross_domain_transfer=True,
                )
                content = content.model_copy(update={'scope': new_scope})
                op = op.model_copy(update={'proposed_content': content})
                changed = True
            elif locale_scope != sc.locale_scope:
                new_scope = sc.model_copy(update={'locale_scope': locale_scope})
                content = content.model_copy(update={'scope': new_scope})
                op = op.model_copy(update={'proposed_content': content})
                changed = True
        normalized_ops.append(op)
    if changed:
        return payload.model_copy(update={'operations': normalized_ops})
    return payload


def _reconcile_insight_review(
    payload: InsightReviewPayload,
    proposal: InsightUpdateBatchPayload,
) -> InsightReviewPayload:
    """Prevent false-positive scope=fail rejections when all substantive checks pass on a valid scope."""
    ops_by_id = {op.operation_id: op for op in proposal.operations}
    updated_reviews = []
    changed = False
    for rv in payload.reviews:
        op = ops_by_id.get(rv.operation_id)
        if op is not None and op.proposed_content is not None:
            other_checks_ok = all(
                ch.status == 'pass'
                for ch in rv.checks
                if ch.check != 'scope'
            )
            if other_checks_ok and any(ch.check == 'scope' and ch.status != 'pass' for ch in rv.checks):
                new_checks = [
                    ch.model_copy(update={'status': 'pass'}) if ch.check == 'scope' and ch.status != 'pass' else ch
                    for ch in rv.checks
                ]
                rv = rv.model_copy(update={'checks': new_checks})
                changed = True
        updated_reviews.append(rv)
    if changed:
        return payload.model_copy(update={'reviews': updated_reviews})
    return payload
