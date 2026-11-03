"""`collect-practice` (guide §9): bounded semantic attempts against a frozen release.

Attempt 1 is source-only. On evaluated failure the findings become bounded
`source_only` feedback; language-only defects keep the frozen plan and let A4
repair wording. A source-based pass is followed by the private reference
comparison on the frozen candidate; `incompatible` yields `reference_informed`
feedback, `unknown` is not success. Success requires both. Every attempt is
stored; only successful practice attempts are eligible examples.

ExpeL mode (`learning.*` in the config): success is decided by the private reference comparison
alone (A5 stays a diagnostic), and each failed attempt also receives an A6 free-text reflection
(`LLM_reflect`) built from the A5 findings and the sanitised comparison, accumulated across attempts.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import trace
from ..agents.runner import UsageMeter
from ..contracts import AttemptExperience, Feedback, Finding
from ..errors import ExpectedPipelineError, GlocalInductError
from ..hashing import new_id, deterministic_id, payload_hash
from ..records import EvaluationReport
from ..services import Services
from ..validation.evaluation import comparison_issue_code
from .common import RunStore
from .infer import AttemptOutcome, run_adaptation
from .ingest import PairRecord, current_split, load_pairs

LANGUAGE_ONLY = {'arabic_quality'}
NON_RECOVERABLE = {'SCHEMA_INVALID', 'PROVIDER_FAILED', 'PROVIDER_REFUSAL', 'BUDGET_EXHAUSTED', 'CONTEXT_OVERFLOW',
                   'MEMORY_REFERENCE_INVALID'}


def _non_recoverable(a, cfg) -> bool:
    """Whether a pipeline error ends the practice task.

    ExpeL/Reflexion: an invalid action is an observation, not the end of the trial. With free-text
    reflection on, a plan the validators reject (make_plan SCHEMA_INVALID / MEMORY_REFERENCE_INVALID) becomes a
    failed attempt that A6 reflects on and the next attempt retries. Every other error still stops the task.
    """
    if not a.error:
        return False
    code = a.error['code']
    if cfg.learning.reflection == 'free_text':
        if code == 'MEMORY_REFERENCE_INVALID':
            return False
        if code == 'SCHEMA_INVALID' and (a.error.get('details') or {}).get('mode') == 'make_plan':
            return False
    return code in NON_RECOVERABLE


@dataclass
class TaskOutcome:
    pair_id: str
    group_id: str
    attempts: list[dict] = field(default_factory=list)
    success: bool = False
    stop_reason: str = ''


def _feedback_from_report(report: EvaluationReport, prior_attempt_id: str, decision) -> tuple[Feedback, bool]:
    findings: list[Finding] = []
    failed = set()
    for c in report.payload.criteria:
        if c.status in ('fail', 'unknown') or any(f.severity in ('substantive', 'critical') for f in c.findings):
            failed.add(c.criterion)
            findings.extend(c.findings)
            if not c.findings:
                findings.append(Finding(issue_code=f'{c.criterion.upper()}_{c.status.upper()}', severity='substantive',
                                        affected_ids=[], evidence=[], explanation=c.explanation,
                                        correction_goal=f'Resolve the {c.criterion} assessment: {c.explanation[:200]}'))
    language_only = bool(failed) and failed <= LANGUAGE_ONLY
    return Feedback(scope='source_only', prior_attempt_id=prior_attempt_id, findings=findings), language_only


def _feedback_from_comparison(comparison, prior_attempt_id: str) -> Feedback:
    findings = []
    for d in comparison.differences:
        if d.classification == 'candidate_defect':
            code = comparison_issue_code(d)
            findings.append(Finding(issue_code=code,
                                    severity='critical' if d.critical else 'substantive', affected_ids=[],
                                    evidence=d.source_quotes + d.candidate_quotes,   # never the reference quote
                                    explanation=d.explanation, correction_goal=d.correction_goal))
    return Feedback(scope='reference_informed', prior_attempt_id=prior_attempt_id, findings=findings)


async def practice_task(pair: PairRecord, services: Services, release, store: RunStore, meter: UsageMeter) -> TaskOutcome:
    with trace.scope(pair_id=pair.pair_id, phase='practice', attempt=None):
        trace.emit_lazy('pair_start', lambda: dict(
            group_id=pair.group_id, source_text=pair.source.normalized.text, release=release.release_id,
            max_attempts=services.config.attempts.practice))
        out = await _practice_task(pair, services, release, store, meter)
        trace.update(attempt=None)
        trace.emit_lazy('pair_end', lambda: dict(success=out.success, stop_reason=out.stop_reason,
                                                 attempts=len(out.attempts)))
        return out


async def _practice_task(pair: PairRecord, services: Services, release, store: RunStore, meter: UsageMeter) -> TaskOutcome:
    cfg = services.config
    out = TaskOutcome(pair.pair_id, pair.group_id)
    prior: AttemptOutcome | None = None
    prior_id: str | None = None
    feedback: list[Feedback] | None = None
    language_only = False
    last_hash: str | None = None
    budget = services.budget(len(pair.source.windows))
    reference = services.private.get(pair.arabic_hash)
    for n in range(1, cfg.attempts.practice + 1):
        attempt_id = f'att_{pair.pair_id}_{n}'
        trace.update(attempt=n)
        trace.emit_lazy('attempt_start', lambda: dict(attempt_id=attempt_id, language_only_repair=language_only,
                                                      prior_attempt_id=prior_id))
        a = await run_adaptation(pair.source, services, release, store, excluded_groups=[pair.group_id],
                                 feedback=feedback, prior=prior, budget=budget, language_only_repair=language_only,
                                 allow_unsupported_substitution=True, reference=reference)
        record = {'attempt': n, 'attempt_id': attempt_id, 'issue_codes': a.issue_codes,
                  'plan_version': a.plan.version if a.plan else None}
        comparison = None
        outcome = 'failure'
        stop = False
        paper_success = cfg.learning.success_signal == 'reference_comparison'
        # The Quality evaluator is reference-only (no criteria): its comparison is the success signal.
        ref_only = a.report is not None and not a.report.payload.criteria
        code = a.error['code'] if a.error else None
        if a.candidate is None or (a.error and (not paper_success or _non_recoverable(a, cfg))):
            outcome = 'error'
            stop = _non_recoverable(a, cfg) if a.error else True
        elif not paper_success and not ref_only and a.decision.status == 'unresolved':
            stop = False
        else:
            # private comparison on the frozen candidate
            # 1) If already evaluated inside unified Quality Evaluator (A5), reuse directly
            cpayload = None
            if a.report and hasattr(a.report, 'payload') and getattr(a.report.payload, 'reference_comparison', None):
                cpayload = a.report.payload.reference_comparison
            if cpayload is not None:
                comparison = EvaluationReport(deterministic_id('eval', a.candidate.candidate_hash, 'cmp',
                                                               payload_hash(cpayload.model_dump(mode='json'))),
                                              'reference_comparison', a.candidate.candidate_id,
                                              a.candidate.candidate_hash, cpayload)
                store.save(f'comparison_{a.candidate.candidate_id}', 'evaluation_report', comparison.to_dict(),
                           parents=[r for r in (a.record_ids.get('report') or a.record_ids.get('candidate'),) if r])
                record['comparison'] = cpayload.result
                trace.emit_lazy('reference_comparison', lambda: dict(
                    report_id=comparison.report_id, result=cpayload.result, payload=cpayload.model_dump(mode='json')))
                outcome = 'success' if cpayload.result == 'compatible' else 'failure'
                stop = cpayload.result in ('compatible', 'unknown')
            else:
                # 2) Fallback to standalone compare_reference if candidate_quality didn't perform it
                try:
                    cpayload, rec = await services.agents.compare_reference(pair.source, a.profile, a.candidate, reference,
                                                                            meter, budget, store.raw)
                    comparison = EvaluationReport(deterministic_id('eval', a.candidate.candidate_hash, 'cmp',
                                                                   payload_hash(cpayload.model_dump(mode='json'))),
                                                  'reference_comparison', a.candidate.candidate_id,
                                                  a.candidate.candidate_hash, cpayload)
                    store.save(f'comparison_{a.candidate.candidate_id}', 'evaluation_report', comparison.to_dict(),
                               parents=[r for r in (a.record_ids.get('report') or a.record_ids.get('candidate'),) if r])
                    record['comparison'] = cpayload.result
                    trace.emit_lazy('reference_comparison', lambda: dict(
                        report_id=comparison.report_id, result=cpayload.result, payload=cpayload.model_dump(mode='json')))
                    outcome = 'success' if cpayload.result == 'compatible' else 'failure'
                    stop = cpayload.result in ('compatible', 'unknown')
                except (ExpectedPipelineError, GlocalInductError) as e:
                    record['comparison_error'] = e.code
                    outcome = 'error'; stop = True
        record['outcome'] = outcome
        # ---- persist the attempt as an experience (variant: agent_attempt)
        supports = sorted({s.experience_id for d in a.plan.payload.decisions for s in d.support}) if a.plan else []
        exp = AttemptExperience(kind='agent_attempt', task_id=pair.pair_id, group_id=pair.group_id, phase='practice',
                                attempt_number=n, previous_attempt_id=prior_id, source_profile_id=a.profile.profile_id if a.profile else None,
                                release_id=release.release_id, retrieval_id=a.retrieval.retrieval_id if a.retrieval else None,
                                plan_id=a.plan.plan_id if a.plan else None, candidate_id=a.candidate.candidate_id if a.candidate else None,
                                quality_report_id=a.report.report_id if a.report else None,
                                comparison_report_id=comparison.report_id if comparison else None,
                                feedback_received=feedback[-1] if feedback else None, outcome=outcome,
                                eligible=(outcome == 'success'), issue_codes=a.issue_codes if outcome != 'success' else [])
        meta = {'source_text_hash': pair.source.text_hash,
                'arabic_text_hash': a.candidate.arabic.text_hash if a.candidate else None,
                'domains': sorted({t.domain for t in a.profile.payload.domains} | set(pair.source.context.domains)) if a.profile else list(pair.source.context.domains),
                'genre': (a.profile.payload.genre if a.profile else None) or pair.source.context.genre,
                'scope': _attempt_scope(pair, a, services), 'summary': a.profile.payload.purpose if a.profile else '',
                'functions': [i.semantic_function for i in a.profile.payload.items] if a.profile else [],
                'source_excerpt': pair.source.normalized.text[:600],
                'transformation': ', '.join(sorted({d.strategy for d in a.plan.payload.decisions})) if a.plan and a.plan.payload.decisions else 'ordinary_translation',
                'preserved': [], 'plan': _plan_summary(a), 'assessment': _assessment_summary(a, comparison),
                'issue_codes': a.issue_codes, 'attempt_number': n, 'previous_attempt_id': prior_id}
        if a.candidate:
            services.texts.put(a.candidate.arabic, 'candidate')
        eid = services.experiences.admit_attempt(exp, meta, store.run_id, parents=[v for v in a.record_ids.values()],
                                                 supports=supports)
        record['experience_id'] = eid
        trace.emit_lazy('experience', lambda: dict(
            experience_id=eid, eligible=exp.eligible, outcome=outcome, record=exp.model_dump(mode='json'),
            retrieval_meta=meta, supports=supports))
        out.attempts.append(record)
        store.note(stage='practice_attempt', pair_id=pair.pair_id, **record)
        trace.emit_lazy('attempt_end', lambda: dict(attempt_id=attempt_id, outcome=outcome, stop=bool(stop),
                                                    issue_codes=a.issue_codes, record=record))
        if outcome == 'success':
            out.success = True; out.stop_reason = 'success'; break
        if stop:
            out.stop_reason = 'unrecoverable_error' if outcome == 'error' else 'reference_unknown'; break
        if a.candidate and a.candidate.candidate_hash == last_hash:
            out.stop_reason = 'identical_candidate'; break
        last_hash = a.candidate.candidate_hash if a.candidate else last_hash
        # ---- build feedback for the next attempt. As in Reflexion / ExpeL, feedback accumulates:
        # the next attempt sees the reflections of ALL earlier failed attempts (bounded by attempts.practice).
        if comparison is not None:
            new_fb = _feedback_from_comparison(comparison.payload, eid)
            language_only = False
        elif a.report is not None:
            new_fb, language_only = _feedback_from_report(a.report, eid, a.decision)
        else:
            new_fb = Feedback(scope='source_only', prior_attempt_id=eid, findings=[Finding(
                issue_code=a.issue_codes[0] if a.issue_codes else 'PLAN_MISMATCH', severity='substantive', affected_ids=[],
                evidence=[], explanation=a.error['message'] if a.error else 'deterministic validation failed',
                correction_goal='produce a plan/candidate that passes deterministic validation')])
            language_only = False
        if cfg.learning.reflection == 'free_text':
            text = await _reflect(pair, a, comparison, outcome, feedback, services, meter, budget, store)
            if text:
                new_fb = new_fb.model_copy(update={'reflection': text})
                language_only = False       # a free-text diagnosis always goes back through the planner
        feedback = [*(feedback or []), new_fb]
        trace.emit_lazy('feedback', lambda: dict(
            for_attempt=n + 1, scope=feedback[-1].scope, language_only_repair=language_only,
            feedback=[f.model_dump(mode='json') for f in feedback]))
        prior, prior_id = a, eid
        out.stop_reason = 'attempt_limit'
    return out


def _failure_signals(a, comparison, outcome: str) -> dict:
    """Failure evidence for A6: A5 findings plus the comparison's candidate defects.

    The comparison's reference quotes and summary are included so the reflection can turn them into
    concrete hints and corrections drawn from the glocalized pair.
    """
    sig: dict = {'outcome': outcome, 'issue_codes': list(a.issue_codes)}
    if a.error:
        sig['pipeline_error'] = {'code': a.error['code'], 'message': str(a.error['message'])[:500]}
    if a.report is not None:
        sig['quality_findings'] = [
            {'criterion': c.criterion, 'status': c.status, 'explanation': c.explanation,
             'findings': [{'issue_code': f.issue_code, 'severity': f.severity, 'explanation': f.explanation,
                           'correction_goal': f.correction_goal} for f in c.findings]}
            for c in a.report.payload.criteria if c.status != 'pass' or c.findings]
    if comparison is not None:
        p = comparison.payload
        sig['comparison'] = {'result': p.result, 'summary': p.explanation, 'differences': [
            {'classification': d.classification, 'critical': d.critical, 'explanation': d.explanation,
             'correction_goal': d.correction_goal, 'source_quotes': [q.quote for q in d.source_quotes],
             'candidate_quotes': [q.quote for q in d.candidate_quotes],
             'glocalized_quotes': [q.quote for q in d.reference_quotes]}
            for d in p.differences if d.classification == 'candidate_defect']}
    return sig


async def _reflect(pair: PairRecord, a, comparison, outcome: str, feedback: list[Feedback] | None, services: Services,
                   meter: UsageMeter, budget, store: RunStore) -> str | None:
    """ExpeL `LLM_reflect`: free-text diagnosis of one failed attempt; None if it could not be produced."""
    if a.profile is None:
        return None
    attempt = {'plan': _plan_summary(a),
               'candidate_blocks': [{'source_block_id': b.source_block_id, 'text_ar': b.text_ar}
                                    for b in a.candidate.payload.blocks] if a.candidate else []}
    earlier = [f.reflection for f in (feedback or []) if f.reflection]
    try:
        reference = services.private.get(pair.arabic_hash)
        payload, _ = await services.agents.reflect_failure(pair.source, a.profile, attempt,
                                                           _failure_signals(a, comparison, outcome), earlier, meter,
                                                           budget, store.raw, glocalized_arabic=reference.text)
    except (ExpectedPipelineError, GlocalInductError, AssertionError) as e:
        trace.emit_lazy('reflection_error', lambda: dict(error=f'{type(e).__name__}: {str(e)[:300]}'))
        return None
    trace.emit_lazy('reflection', lambda: dict(reflection=payload.reflection, earlier_reflections=earlier))
    return payload.reflection


def _attempt_scope(pair, a, services) -> dict:
    """Retrieval scope of an attempt: request context intersected with its supports' constraints."""
    c = pair.source.context
    scope = {'level': 'context', 'domains': list(c.domains), 'genres': [c.genre] if c.genre else [],
             'culture_specific': False, 'locale_scope': c.locale_scope, 'locales': list(c.locales),
             'setting': c.setting, 'time_period': c.time_period, 'cross_domain_transfer': True}
    if a.plan:
        for d in a.plan.payload.decisions:
            if d.strategy == 'substitute':
                scope['culture_specific'] = True
                for s in d.support:
                    v = services.experiences.get(s.experience_id)
                    if v and v.retrieval.get('scope', {}).get('locale_scope') == 'specified':
                        scope['locale_scope'] = 'specified'
                        scope['locales'] = sorted(set(scope['locales']) | set(v.retrieval['scope'].get('locales', [])))
    if scope['locale_scope'] != 'specified':
        scope['locales'] = []
    return scope


def _plan_summary(a) -> dict | None:
    if not a.plan:
        return None
    return {'plan_id': a.plan.plan_id, 'version': a.plan.version, 'reflection': a.plan.payload.reflection,
            'decisions': [{'item_id': d.item_id, 'strategy': d.strategy, 'counterpart_ar': d.counterpart_ar,
                           'fallback_reason': d.fallback_reason} for d in a.plan.payload.decisions]}


def _assessment_summary(a, comparison) -> dict:
    out = {'status': a.decision.status if a.decision else ('error' if a.error else 'not_assessed'),
           'issue_codes': a.issue_codes}
    if a.report:
        out['criteria'] = [{'criterion': c.criterion, 'status': c.status, 'score': c.score} for c in a.report.payload.criteria]
        out['findings'] = [{'issue_code': f.issue_code, 'severity': f.severity, 'correction_goal': f.correction_goal}
                           for c in a.report.payload.criteria for f in c.findings]
    if comparison:
        out['reference_comparison'] = comparison.payload.result
    return out


async def collect_practice(services: Services, release_id: str = 'M0', run_id: str | None = None,
                           limit: int | None = None, pair_id: str | None = None) -> dict:
    release = services.resolve_release(release_id)
    if pair_id:
        pairs = load_pairs(services, [pair_id])
        if not pairs:
            raise GlocalInductError(f'pair {pair_id} not found in ingested pairs', code='PAIR_NOT_FOUND')
    else:
        split = current_split(services)
        # ExpeL mode: references are never demonstrations, so the seed subset is practised as well
        subsets = {'practice'} if services.config.learning.reference_demonstrations else {'practice', 'seed'}
        target_pids = [pid for pid in sorted(split.pair_group)
                       if split.partition_of_pair(pid) == 'learning' and split.subset_of_pair(pid) in subsets]
        if limit is not None and limit > 0:
            target_pids = target_pids[:limit]
        pairs = load_pairs(services, target_pids)
    store = RunStore(services.repo, services.config.path('runs'), run_id)
    meter = UsageMeter()
    tasks = [await practice_task(p, services, release, store, meter) for p in pairs]
    summary = {'release': release.release_id, 'tasks': len(tasks), 'successes': sum(t.success for t in tasks),
               'attempts': sum(len(t.attempts) for t in tasks),
               'detail': [{'pair_id': t.pair_id, 'success': t.success, 'stop_reason': t.stop_reason,
                           'attempts': t.attempts} for t in tasks],
               'usage': meter.to_contract(), 'run_id': store.run_id}
    store.save('summary', 'practice_summary', summary)
    return summary
