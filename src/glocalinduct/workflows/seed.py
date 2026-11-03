"""`build-seed-memory` (guide §8): admit seed references, extract initial insights, publish M0."""
from __future__ import annotations

from .. import trace
from ..agents.runner import UsageMeter
from ..services import Services
from .common import RunStore
from .ingest import current_split, load_pairs
from .learning import admit_reference, build_packets, extract_and_apply_insights, publish_release, AdmissionOutcome


async def build_seed_memory(services: Services, release_id: str = 'M0', run_id: str | None = None,
                            limit: int | None = None, overwrite: bool = False,
                            pair_id: str | None = None) -> dict:
    if pair_id:
        pairs = load_pairs(services, [pair_id])
        if not pairs:
            raise GlocalInductError(f'pair {pair_id} not found in ingested pairs', code='PAIR_NOT_FOUND')
        manifest_meta = {'targeted_pair': pair_id}
        paper = False
    else:
        split = current_split(services)
        paper = not services.config.learning.reference_demonstrations
        if paper:
            pairs = []
        else:
            target_pids = [pid for pid in sorted(split.pair_group)
                           if split.partition_of_pair(pid) == 'learning' and split.subset_of_pair(pid) == 'seed']
            if limit is not None and limit > 0:
                target_pids = target_pids[:limit]
            pairs = load_pairs(services, target_pids)
        manifest_meta = {'split': split.manifest_hash}
    store = RunStore(services.repo, services.config.path('runs'), run_id)
    meter = UsageMeter()
    budget = services.budget(1)
    budget.max_calls = None            # offline batch: per-document budgets are enforced per call below
    budget.deadline_seconds = None
    already = {v.record.get('pair_id'): v for v in services.experiences.all() if v.kind == 'reference_example'}
    admissions = []
    for p in pairs:
        if p.pair_id in already and not overwrite:
            prev = already[p.pair_id]
            admissions.append(AdmissionOutcome(
                pair_id=p.pair_id,
                group_id=p.group_id,
                eligible=prev.eligible,
                experience_id=prev.experience_id,
                reasons=prev.record.get('exclusion_reasons', [])
            ))
            trace.emit_lazy('pair_skipped', lambda: dict(
                pair_id=p.pair_id, experience_id=prev.experience_id, eligible=prev.eligible,
                note='already admitted by a previous run; reused without model calls'))
        else:
            admissions.append(await admit_reference(p, services, store, meter, budget, phase='seed'))
    eligible = [a.experience_id for a in admissions if a.eligible]
    packets = build_packets(services, 'reference', eligible, services.config.insights.batch_size)
    trace.emit_lazy('packets_built', lambda: dict(
        count=len(packets), eligible=len(eligible),
        packets=[{'comparison_id': k['comparison_id'], 'kind': k['kind'], 'records': len(k['records'])} for k in packets]))
    insight_summary = await extract_and_apply_insights(services, packets, store, meter, budget)
    rel = publish_release(services, release_id, eligible, store.run_id, None, manifest_meta,
                          overwrite=overwrite)
    trace.emit_lazy('release_published', lambda: dict(release=rel.summary(), insights=insight_summary))
    summary = {'release': rel.summary(), 'seed_pairs': len(pairs), 'admitted': len(eligible),
               'excluded': [{'pair_id': a.pair_id, 'reasons': a.reasons} for a in admissions if not a.eligible],
               'insights': insight_summary, 'usage': meter.to_contract(), 'run_id': store.run_id}
    if not pair_id and paper:
        summary['note'] = 'ExpeL mode: empty M0 (learning.reference_demonstrations is false)'
    store.save('summary', 'seed_summary', summary)
    return summary
