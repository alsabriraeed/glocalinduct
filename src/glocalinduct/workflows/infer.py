"""Online adaptation pipeline (guide §11–§14).

`run_adaptation` is the shared one-attempt core: profile (per window, merged) →
retrieve → plan (per window, reconciled, frozen) → generate (per window) →
assemble → evaluate (per window + cross-window pass) → controller decision.
`infer` is the deployment entry point: one semantic attempt, no target feedback,
every expected failure becomes `unresolved`.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Criterion, Feedback, PlanPayload, QualityEvaluationPayload, ReferenceComparisonPayload, RunResult
from ..errors import ExpectedPipelineError, GlocalInductError, PlanConflict
from ..hashing import new_id, payload_hash, deterministic_id
from ..memory.releases import Release
from ..records import AdaptationPlan, ArabicCandidate, EvaluationReport, RetrievalSnapshot, SourceDocument, SourceProfile
from ..services import Services
from ..validation import assemble_candidate, decide_result, validate_plan
from ..validation.evaluation import Decision
from ..agents.runner import Budget, UsageMeter
from .common import InferRequest, RunStore, build_source_document, merge_profiles
from .. import trace


def trace_retrieval(r: RetrievalSnapshot) -> dict:
    """Slim retrieval view for the Step Inspector (full texts live in the stored record)."""
    return {'retrieval_id': r.retrieval_id, 'release_id': r.release_id, 'token_budget': r.token_budget,
            'truncation': r.truncation, 'embedder': r.embedder,
            'experiences': [{'experience_id': e.experience_id, 'kind': e.kind, 'group_id': e.group_id,
                             'similarity': e.similarity, 'domains': e.domains,
                             'source_excerpt': e.source_excerpt[:300],
                             'arabic_excerpt': e.arabic_text.text[:300]}
                            for e in r.experiences],
            'insights': [{'insight_id': i.insight_id, 'version': i.version, 'importance': i.importance,
                          'similarity': i.similarity, 'lesson': i.content.get('lesson'),
                          'applies_when': i.content.get('applies_when')} for i in r.insights],
            'filter_decisions': r.filter_decisions[:40]}


def trace_candidate(c: ArabicCandidate) -> dict:
    return {'candidate_id': c.candidate_id, 'candidate_hash': c.candidate_hash, 'plan_id': c.plan_id,
            'arabic_text': c.arabic.text, 'blocks': [b.model_dump(mode='json') for b in c.payload.blocks],
            'realizations': [r.model_dump(mode='json') for r in c.payload.realizations],
            'unresolved_issues': list(c.payload.unresolved_issues), 'block_map': c.block_map,
            'resolved_realizations': c.resolved_realizations}


@dataclass
class AttemptOutcome:
    source: SourceDocument
    profile: SourceProfile | None = None
    retrieval: RetrievalSnapshot | None = None
    plan: AdaptationPlan | None = None
    candidate: ArabicCandidate | None = None
    report: EvaluationReport | None = None
    decision: Decision | None = None
    error: dict | None = None
    record_ids: dict[str, str] = field(default_factory=dict)
    window_reports: list[dict] = field(default_factory=list)
    meter: UsageMeter = field(default_factory=UsageMeter)

    @property
    def issue_codes(self) -> list[str]:
        if self.error:
            return [self.error['code']]
        return list(self.decision.issue_codes) if self.decision else []


def _merge_reports(reports: list[QualityEvaluationPayload]) -> QualityEvaluationPayload:
    """Worst-case merge across windows: a criterion passes only if it passes everywhere."""
    if len(reports) == 1:
        return reports[0]
    merged = []
    for name in ['meaning_and_purpose', 'cultural_fit', 'arabic_quality']:
        cs = [c for r in reports for c in r.criteria if c.criterion == name]
        status = 'pass'
        if any(c.status == 'fail' for c in cs):
            status = 'fail'
        elif any(c.status == 'unknown' for c in cs):
            status = 'unknown'
        scores = [c.score for c in cs if c.score is not None]
        score = min(scores) if scores and status != 'unknown' else None
        findings = [f for c in cs for f in c.findings]
        if status == 'pass' and any(f.severity in ('substantive', 'critical') for f in findings):
            status = 'fail'; score = min(score or 2, 2)
        merged.append(Criterion(criterion=name, status=status, score=score, evidence=[q for c in cs for q in c.evidence],
                                findings=findings, explanation=' | '.join(c.explanation for c in cs)))
    ref_cmps = [r.reference_comparison for r in reports if r.reference_comparison is not None]
    merged_ref_cmp = None
    if ref_cmps:
        res = 'compatible'
        if any(rc.result == 'incompatible' for rc in ref_cmps):
            res = 'incompatible'
        elif any(rc.result == 'unknown' for rc in ref_cmps):
            res = 'unknown'
        diffs = [d for rc in ref_cmps for d in rc.differences]
        expl = ' | '.join(rc.explanation for rc in ref_cmps)
        merged_ref_cmp = ReferenceComparisonPayload(mode='reference_comparison', result=res, differences=diffs, explanation=expl)
    return QualityEvaluationPayload(mode='candidate_quality', criteria=merged, reference_comparison=merged_ref_cmp)


async def compute_profile(source: SourceDocument, services: Services, store: RunStore, meter: UsageMeter,
                          budget: Budget) -> SourceProfile:
    """English-only source profile over owner windows, merged and validated (guide §4 step 4)."""
    windows = source.windows
    n = len(windows)
    parts = []
    if n <= 1:
        for w in windows:
            payload, rec, warnings = await services.agents.analyse_source(source, w if n > 1 else None, meter, budget, store.raw)
            parts.append((w.window_id, payload))
            store.note(stage='analyse_source', window=w.window_id, warnings=warnings, call=rec)
    else:
        sem = asyncio.Semaphore(8)
        async def _analyse_win(w):
            async with sem:
                return w.window_id, *(await services.agents.analyse_source(source, w, meter, budget, store.raw))
        for wid, payload, rec, warnings in await asyncio.gather(*(_analyse_win(w) for w in windows)):
            parts.append((wid, payload))
            store.note(stage='analyse_source', window=wid, warnings=warnings, call=rec)
    merged = merge_profiles(parts, source)
    from ..validation.plans import validate_profile
    validate_profile(merged, source)
    return SourceProfile(profile_id=deterministic_id('prof', source.text_hash, source.context_hash(),
                                                     payload_hash(merged.model_dump(mode='json'))), payload=merged, source_text_id=source.source_text_id,
                         source_hash=source.text_hash, context_hash=source.context_hash(),
                         window_ids=[w.window_id for w in windows])


async def run_adaptation(source: SourceDocument, services: Services, release: Release, store: RunStore,
                         excluded_groups: list[str] | None = None, feedback: list[Feedback] | None = None,
                         prior: AttemptOutcome | None = None, budget: Budget | None = None,
                         language_only_repair: bool = False,
                         allow_unsupported_substitution: bool = False,
                         reference: NormalizedText | None = None, evaluate: bool = True) -> AttemptOutcome:
    out = AttemptOutcome(source=source, meter=UsageMeter())
    agents = services.agents
    windows = source.windows
    budget = budget or services.budget(len(windows))
    n = len(windows)
    try:
        trace.emit_lazy('source', lambda: dict(
            text=source.normalized.text, windows=n, text_hash=source.text_hash,
            context=source.context.model_dump(mode='json') if hasattr(source.context, 'model_dump') else str(source.context),
            release=release.release_id,
            feedback_in=[f.model_dump(mode='json') for f in feedback] if feedback else None,
            language_only_repair=language_only_repair, reused_profile=bool(prior and prior.profile),
            reused_retrieval=bool(prior and prior.retrieval),
            reused_plan=bool(language_only_repair and prior and prior.plan)))
        # ---- 1. English-only profile (reuse a prior frozen profile across attempts)
        store.save('source', 'source_document', source.to_dict(), cache_key=payload_hash(
            {'source': source.text_hash, 'ctx': source.context_hash()}))
        services.texts.put(source.normalized)
        if prior is not None and prior.profile is not None:
            profile = prior.profile
        else:
            profile = await compute_profile(source, services, store, out.meter, budget)
        out.profile = profile
        out.record_ids['profile'] = store.save('profile', 'source_profile', profile.to_dict())
        trace.emit_lazy('profile', lambda: dict(profile_id=profile.profile_id,
                                                profile=profile.payload.model_dump(mode='json')))
        # ---- 2. retrieval (frozen release, English side only)
        if prior is not None and prior.retrieval is not None:
            retrieval = prior.retrieval
        else:
            retrieval = services.search.search(profile, source, release, excluded_groups,
                                               token_budget=max(8000, budget.input_budget // 2))
        out.retrieval = retrieval
        out.record_ids['retrieval'] = store.save('retrieval', 'retrieval_snapshot', retrieval.to_dict())
        trace.emit_lazy('retrieval', lambda: trace_retrieval(retrieval))
        # ---- 3. plan (per window, merged, validated, frozen)
        if language_only_repair and prior is not None and prior.plan is not None:
            plan = prior.plan                      # same frozen plan; A4 repairs wording only
        else:
            payloads: list[PlanPayload] = []
            if n <= 1:
                for w in windows:
                    p, rec = await agents.make_plan(source, w if n > 1 else None, profile, retrieval, out.meter, budget,
                                                    feedback, store.raw)
                    payloads.append(p)
                    store.note(stage='make_plan', window=w.window_id, call=rec)
            else:
                sem = asyncio.Semaphore(8)
                async def _plan_win(w):
                    async with sem:
                        return w.window_id, *(await agents.make_plan(source, w, profile, retrieval, out.meter, budget,
                                                                     feedback, store.raw))
                for wid, p, rec in await asyncio.gather(*(_plan_win(w) for w in windows)):
                    payloads.append(p)
                    store.note(stage='make_plan', window=wid, call=rec)
            version = (prior.plan.version + 1) if prior is not None and prior.plan is not None else 1
            try:
                plan_payload = _merge_plans(payloads)
                plan = AdaptationPlan(plan_id=deterministic_id('plan', profile.profile_id, retrieval.retrieval_id,
                                                               str(version), payload_hash(plan_payload.model_dump(mode='json'))),
                                      payload=plan_payload, profile_id=profile.profile_id,
                                      retrieval_id=retrieval.retrieval_id, source_hash=source.text_hash,
                                      release_id=release.release_id, window_ids=[w.window_id for w in windows],
                                      version=version, feedback_hash=payload_hash([f.model_dump(mode='json') for f in feedback]) if feedback else None)
                validate_plan(plan.payload, profile, source, retrieval, release.release_id,
                              insight_substitution=services.config.learning.insight_substitution,
                              allow_unsupported=allow_unsupported_substitution)
            except PlanConflict as conflict:
                if n == 1:
                    raise
                store.note(stage='reconcile', conflict=conflict.details)
                p, rec = await agents.make_plan(source, None, profile, retrieval, out.meter, budget, feedback, store.raw,
                                                reconciliation={'conflicts': conflict.details,
                                                                'window_decisions': [x.model_dump(mode='json') for x in payloads]})
                plan = AdaptationPlan(plan_id=deterministic_id('plan', profile.profile_id, retrieval.retrieval_id,
                                                               str(version), payload_hash(p.model_dump(mode='json'))),
                                      payload=p, profile_id=profile.profile_id,
                                      retrieval_id=retrieval.retrieval_id, source_hash=source.text_hash,
                                      release_id=release.release_id, window_ids=[w.window_id for w in windows],
                                      version=version, feedback_hash=payload_hash([f.model_dump(mode='json') for f in feedback]) if feedback else None)
                validate_plan(plan.payload, profile, source, retrieval, release.release_id,
                              insight_substitution=services.config.learning.insight_substitution,
                              allow_unsupported=allow_unsupported_substitution)
            plan.freeze()
        out.plan = plan
        out.record_ids['plan'] = store.save(f'plan_v{plan.version}', 'adaptation_plan', plan.to_dict(),
                                            parents=[out.record_ids['profile'], out.record_ids['retrieval']])
        trace.emit_lazy('plan_frozen', lambda: dict(
            plan_id=plan.plan_id, version=plan.version, plan_hash=plan.plan_hash,
            feedback_hash=plan.feedback_hash, payload=plan.payload.model_dump(mode='json')))
        # ---- 4. generate per owner window, assemble, hash
        cand_payloads = []
        if n <= 1:
            for w in windows:
                cp, rec = await agents.generate_arabic(source, w if n > 1 else None, profile, plan, out.meter, budget,
                                                       feedback, store.raw)
                cand_payloads.append(cp)
                store.note(stage='generate_arabic', window=w.window_id, call=rec)
        else:
            sem = asyncio.Semaphore(8)
            async def _gen_win(w):
                async with sem:
                    return w.window_id, *(await agents.generate_arabic(source, w, profile, plan, out.meter, budget,
                                                                       feedback, store.raw))
            for wid, cp, rec in await asyncio.gather(*(_gen_win(w) for w in windows)):
                cand_payloads.append(cp)
                store.note(stage='generate_arabic', window=wid, call=rec)
        candidate = assemble_candidate(source, cand_payloads, plan.plan_id, plan.plan_hash)
        out.candidate = candidate
        out.record_ids['candidate'] = store.save(f'candidate_{candidate.candidate_id}', 'arabic_candidate',
                                                 candidate.to_dict(), parents=[out.record_ids['plan']])
        trace.emit_lazy('candidate', lambda: trace_candidate(candidate))
        # ---- 5. evaluation. In deployment inference (glocalization step), quality evaluation is skipped (`evaluate=False`):
        # the candidate is released directly following the adaptation plan. In practice loops, evaluation runs with reference comparison / criteria.
        if not evaluate:
            fallbacks = [f'{d.item_id}: {d.fallback_reason}' for d in plan.payload.decisions
                         if d.strategy == 'retain' and d.fallback_reason]
            out.decision = Decision('conservative' if fallbacks else 'accepted', [], fallbacks, [])
            trace.emit('decision', status=out.decision.status, issue_codes=[],
                       fallback_reasons=fallbacks, findings=[], evaluated=False,
                       note='quality evaluation skipped during glocalization inference')
            return out
        if reference is not None:
            merged_report, _rec = await agents.evaluate_against_reference(source, profile, candidate, reference,
                                                                          out.meter, budget, store.raw)
            out.window_reports.append({'window_id': 'document', 'report': merged_report.model_dump(mode='json')})
        else:
            reports: list[QualityEvaluationPayload] = []
            for w, cp in zip(windows, cand_payloads):
                blocks = [{'source_block_id': b.source_block_id, 'text_ar': b.text_ar} for b in cp.blocks]
                rp, rec = await agents.evaluate_candidate(source, w if n > 1 else None, profile, plan, blocks, retrieval,
                                                          out.meter, budget, store.raw)
                reports.append(rp)
                out.window_reports.append({'window_id': w.window_id, 'report': rp.model_dump(mode='json')})
            if n > 1:
                rp, rec = await agents.evaluate_candidate(source, None, profile, plan, [], retrieval, out.meter, budget,
                                                          store.raw, consistency=candidate)
                reports.append(rp)
                out.window_reports.append({'window_id': 'cross_window', 'report': rp.model_dump(mode='json')})
            merged_report = _merge_reports(reports)
        rc = merged_report.reference_comparison
        critical = any(f.severity == 'critical' for c in merged_report.criteria for f in c.findings) or bool(
            not merged_report.criteria and rc is not None and any(d.critical for d in rc.differences))
        report = EvaluationReport(report_id=deterministic_id('eval', candidate.candidate_hash,
                                                             payload_hash(merged_report.model_dump(mode='json'))),
                                  mode='candidate_quality', subject_id=candidate.candidate_id,
                                  subject_hash=candidate.candidate_hash, payload=merged_report,
                                  critical=critical,
                                  deterministic_checks=[{'check': 'plan_validation', 'status': 'pass'},
                                                        {'check': 'candidate_validation', 'status': 'pass'}])
        out.report = report
        out.record_ids['report'] = store.save(f'evaluation_{report.report_id}', 'evaluation_report', report.to_dict(),
                                              parents=[out.record_ids['candidate']])
        trace.emit_lazy('evaluation', lambda: dict(
            report_id=report.report_id, critical=report.critical,
            payload=merged_report.model_dump(mode='json'), window_reports=out.window_reports))
        # ---- 6. controller decision
        out.decision = decide_result(merged_report, [plan.payload], services.config.evaluation.min_criterion_score)
        trace.emit_lazy('decision', lambda: dict(
            status=out.decision.status, issue_codes=list(out.decision.issue_codes),
            fallback_reasons=list(out.decision.fallback_reasons), findings=out.decision.findings))
    except ExpectedPipelineError as e:
        out.error = {'code': e.code, 'message': str(e), 'details': e.details}
        store.note(stage='error', **out.error)
        trace.emit('pipeline_error', **out.error)
    except GlocalInductError as e:
        out.error = {'code': e.code, 'message': str(e), 'details': e.details}
        store.note(stage='error', **out.error)
        trace.emit('pipeline_error', **out.error)
    except Exception as e:  # unexpected programming error: visible internally, generic externally
        store.note(stage='internal_error', error=repr(e), trace=traceback.format_exc())
        out.error = {'code': 'PROVIDER_FAILED', 'message': 'internal error (see run log)', 'details': {}}
        trace.emit('pipeline_error', **out.error)
    return out


def _merge_plans(payloads: list[PlanPayload]) -> PlanPayload:
    if len(payloads) == 1:
        return payloads[0]
    decisions, terms, issues, reflections = [], [], [], []
    seen_items = set()
    for i, p in enumerate(payloads):
        for d in p.decisions:
            if d.item_id in seen_items:
                continue
            seen_items.add(d.item_id)
            decisions.append(d.model_copy(update={'decision_id': f'w{i + 1}.{d.decision_id}'}))
        for t in p.terminology:
            terms.append(t.model_copy(update={'term_id': f'w{i + 1}.{t.term_id}'}))
        issues.extend(p.unresolved_issues)
        if p.reflection:
            reflections.append(p.reflection)
    # terminology consistency across windows: bind first occurrence per source concept
    bound: dict[str, str] = {}
    uniq = []
    for t in terms:
        if t.source_concept not in bound:
            bound[t.source_concept] = t.preferred_ar
            uniq.append(t)
    return PlanPayload(decisions=decisions, terminology=uniq, reflection=' | '.join(reflections) or None,
                       unresolved_issues=issues)


@dataclass
class InferOutcome:
    result: RunResult
    attempt: AttemptOutcome
    run_id: str
    release: Release

    def to_dict(self) -> dict:
        a = self.attempt
        return {'run_id': self.run_id, 'result': self.result.model_dump(mode='json'),
                'release': self.release.summary(),
                'profile': a.profile.payload.model_dump(mode='json') if a.profile else None,
                'retrieval': a.retrieval.to_dict() if a.retrieval else None,
                'plan': a.plan.to_dict() if a.plan else None,
                'candidate': {'candidate_id': a.candidate.candidate_id, 'candidate_hash': a.candidate.candidate_hash,
                              'blocks': [{'source_block_id': b.source_block_id, 'text_ar': b.text_ar}
                                         for b in a.candidate.payload.blocks]} if a.candidate else None,
                'evaluation': a.report.payload.model_dump(mode='json') if a.report else None,
                'decision_findings': a.decision.findings if a.decision else [],
                'error': a.error, 'usage': a.meter.to_contract(),
                'source': a.source.to_dict() if a.source is not None else None}


def find_reference_pair(services: Services, source: SourceDocument):
    """Ingested pair whose English side equals `source` (exact hash, else whitespace-insensitive text)."""
    from .ingest import load_pairs
    squash = lambda t: ' '.join(t.split())
    target = squash(source.normalized.text)
    for p in load_pairs(services):
        if p.source.text_hash == source.text_hash or squash(p.source.normalized.text) == target:
            return p
    return None


async def infer(request: InferRequest, services: Services, release_id: str | None = None,
                run_id: str | None = None, lookup_reference: bool = False,
                evaluate: bool = False) -> InferOutcome:
    """Deployment inference (glocalization step): one semantic attempt, no target feedback (guide §14).

    Quality evaluation is skipped during inference (evaluate=False); the Arabic candidate is released
    directly following the frozen adaptation plan. In the practice loop, evaluation is retained."""
    release = services.resolve_release(release_id)
    store = RunStore(services.repo, services.config.path('runs'), run_id)
    store.note(stage='start', request_id=request.request_id, release=release.release_id)
    meter = UsageMeter()
    try:
        source = build_source_document(request.text, request.file_path, request.context,
                                       services.config.windows.content_tokens,
                                       services.config.windows.context_tokens_each_side)
    except GlocalInductError as e:
        result = RunResult(request_id=request.request_id, release_id=release.release_id, status='unresolved',
                           arabic_text=None, plan_ids=[], candidate_ids=[], evaluation_ids=[], fallback_reasons=[],
                           issue_codes=[e.code], usage=meter.to_contract())
        store.save('result', 'run_result', result.model_dump(mode='json'))
        from ..records import SourceDocument
        dummy = AttemptOutcome(source=None)  # type: ignore[arg-type]
        dummy.error = {'code': e.code, 'message': str(e), 'details': e.details}
        return InferOutcome(result, dummy, store.run_id, release)
    budget = services.budget(len(source.windows))
    for k, v in (request.budget_overrides or {}).items():
        if hasattr(budget, k) and v is not None:
            setattr(budget, k, v)
    if evaluate:
        reference = None
        if lookup_reference:
            pair = find_reference_pair(services, source)
            reference = services.private.get(pair.arabic_hash) if pair else None
        attempt = await run_adaptation(source, services, release, store, budget=budget,
                                       reference=reference, evaluate=True)
    else:
        attempt = await run_adaptation(source, services, release, store, budget=budget,
                                       evaluate=False)
    result = finish(request.request_id, release.release_id, attempt)
    store.save('result', 'run_result', result.model_dump(mode='json'))
    # deployment logs are a quarantine audit stream, not memory (guide §6)
    return InferOutcome(result, attempt, store.run_id, release)


def finish(request_id: str, release_id: str, a: AttemptOutcome) -> RunResult:
    usage = a.meter.to_contract()
    if a.error or a.decision is None or a.decision.status == 'unresolved':
        codes = a.issue_codes or ['PROVIDER_FAILED']
        return RunResult(request_id=request_id, release_id=release_id, status='unresolved', arabic_text=None,
                         plan_ids=[a.plan.plan_id] if a.plan else [],
                         candidate_ids=[a.candidate.candidate_id] if a.candidate else [],
                         evaluation_ids=[a.report.report_id] if a.report else [], fallback_reasons=[],
                         issue_codes=codes, usage=usage)
    return RunResult(request_id=request_id, release_id=release_id, status=a.decision.status,
                     arabic_text=a.candidate.arabic.text, plan_ids=[a.plan.plan_id],
                     candidate_ids=[a.candidate.candidate_id], evaluation_ids=[a.report.report_id] if a.report else [],
                     fallback_reasons=a.decision.fallback_reasons, issue_codes=[], usage=usage)
