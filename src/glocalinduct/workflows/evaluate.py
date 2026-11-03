"""`evaluate` (guide §9, §18): frozen test/development benchmark.

One semantic attempt per source, no target feedback. The candidate is generated blind and hashed before the
Quality evaluator runs; the evaluator is reference-only (comparison with the private reference localization).
Unresolved cases stay in the denominator; macro averages by domain.
"""
from __future__ import annotations

import json
from collections import defaultdict

from .. import trace
from ..agents.runner import UsageMeter
from ..errors import ExpectedPipelineError, GlocalInductError
from ..hashing import new_id
from ..records import EvaluationReport
from ..services import Services
from .common import RunStore
from .infer import finish, run_adaptation
from .ingest import current_split, load_pairs


async def evaluate(services: Services, partition: str = 'test', release_id: str | None = None,
                   run_id: str | None = None, limit: int | None = None,
                   pair_id: str | None = None) -> dict:
    if partition not in ('test', 'development'):
        raise GlocalInductError('partition must be test or development', code='INPUT_INVALID')
    split = current_split(services)
    release = services.resolve_release(release_id)
    if pair_id:
        pairs = load_pairs(services, [pair_id])
        if not pairs:
            raise GlocalInductError(f'pair {pair_id} not found in ingested pairs', code='PAIR_NOT_FOUND')
    else:
        target_pids = [pid for pid in sorted(split.pair_group) if split.partition_of_pair(pid) == partition]
        if limit:
            target_pids = target_pids[:limit]
        pairs = load_pairs(services, target_pids)
    store = RunStore(services.repo, services.config.path('runs'), run_id)
    meter = UsageMeter()
    rows = []
    for p in pairs:
        trace.update(pair_id=p.pair_id, phase=f'evaluate_{partition}', attempt=None)
        trace.emit_lazy('pair_start', lambda: dict(group_id=p.group_id, source_text=p.source.normalized.text,
                                                   release=release.release_id))
        reference = services.private.get(p.arabic_hash)
        a = await run_adaptation(p.source, services, release, store, excluded_groups=[p.group_id], reference=reference)
        result = finish(p.pair_id, release.release_id, a)
        frozen_hash = a.candidate.candidate_hash if a.candidate else None
        row = {'pair_id': p.pair_id, 'group_id': p.group_id, 'domains': list(p.source.context.domains),
               'status': result.status, 'issue_codes': result.issue_codes,
               'substitutions': sum(d.strategy == 'substitute' for d in a.plan.payload.decisions) if a.plan else 0,
               'fallbacks': len(result.fallback_reasons), 'candidate_hash': frozen_hash,
               'criteria': [{'criterion': c.criterion, 'status': c.status, 'score': c.score}
                            for c in a.report.payload.criteria] if a.report else None,
               'critical': bool(a.report and a.report.critical), 'calls': a.meter.model_calls,
               'windows': len(p.source.windows), 'reference_comparison': None}
        store.save(f'prediction_{p.pair_id}', 'run_result', result.model_dump(mode='json'))
        if a.candidate is not None and a.profile is not None:
            cpayload = a.report.payload.reference_comparison if a.report is not None else None
            if cpayload is not None:   # already produced by the reference-only Quality evaluator
                row['reference_comparison'] = cpayload.result
                trace.emit_lazy('reference_comparison', lambda: dict(
                    result=cpayload.result, payload=cpayload.model_dump(mode='json'),
                    note='decided by the reference-only Quality evaluator'))
            else:
                try:   # evaluation did not complete: fall back to the comparison alone; cannot change the result
                    cpayload, _ = await services.agents.compare_reference(p.source, a.profile, a.candidate, reference,
                                                                          meter, services.budget(1), store.raw)
                    assert a.candidate.candidate_hash == frozen_hash
                    comp = EvaluationReport(new_id('eval'), 'reference_comparison', a.candidate.candidate_id, frozen_hash, cpayload)
                    store.save(f'comparison_{p.pair_id}', 'evaluation_report', comp.to_dict())
                    row['reference_comparison'] = cpayload.result
                    trace.emit_lazy('reference_comparison', lambda: dict(
                        result=cpayload.result, payload=cpayload.model_dump(mode='json'),
                        note='secondary measure after the prediction was frozen'))
                except (ExpectedPipelineError, GlocalInductError) as e:
                    row['reference_comparison'] = f'error:{e.code}'
        meter.model_calls += a.meter.model_calls
        meter.input_tokens += a.meter.input_tokens; meter.output_tokens += a.meter.output_tokens
        rows.append(row)
        trace.emit_lazy('eval_row', lambda: dict(row=row, arabic_text=result.arabic_text))
    trace.update(pair_id=None, phase=None)
    report = summarize(rows)
    report.update({'partition': partition, 'release': release.summary(), 'usage': meter.to_contract(),
                   'run_id': store.run_id, 'rows': rows})
    store.save('report', 'evaluation_summary', report)
    out = services.config.path('reports'); out.mkdir(parents=True, exist_ok=True)
    (out / f'evaluate_{partition}_{release.release_id}_{store.run_id}.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=1), 'utf-8')
    return report


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    counts = {s: sum(r['status'] == s for r in rows) for s in ('accepted', 'conservative', 'unresolved')}
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        for d in (r['domains'] or ['universal']):
            by_domain[d].append(r)
    domain_rates = {d: {'n': len(rs), 'resolved_rate': round(sum(r['status'] != 'unresolved' for r in rs) / len(rs), 3),
                        'accepted_rate': round(sum(r['status'] == 'accepted' for r in rs) / len(rs), 3)}
                    for d, rs in sorted(by_domain.items())}
    macro = round(sum(v['resolved_rate'] for v in domain_rates.values()) / len(domain_rates), 3) if domain_rates else None
    issue_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for c in r['issue_codes']:
            issue_counts[c] += 1
    comp = defaultdict(int)
    for r in rows:
        comp[str(r['reference_comparison'])] += 1
    return {'n': n, 'counts': counts, 'resolved_rate': round((n - counts['unresolved']) / n, 3) if n else None,
            'macro_resolved_rate_by_domain': macro, 'by_domain': domain_rates, 'issue_codes': dict(issue_counts),
            'substitutions_attempted': sum(r['substitutions'] for r in rows),
            'critical_defects': sum(r['critical'] for r in rows), 'reference_comparison': dict(comp),
            'sparse_domains': [d for d, v in domain_rates.items() if v['n'] < 5],
            'note': 'Automatic scores are operational estimates; unresolved cases are in the denominator.'}
