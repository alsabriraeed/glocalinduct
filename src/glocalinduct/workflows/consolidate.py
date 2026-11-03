"""`update-insights` (guide §9): post-practice reference admission, comparison
packets (same-task failure→success, cross-task success, new references), insight
extraction and M1 publication. M0 remains unchanged."""
from __future__ import annotations

from .. import trace
from ..agents.runner import UsageMeter
from ..errors import GlocalInductError
from ..services import Services
from .common import RunStore
from .ingest import current_split, load_pairs
from .learning import admit_reference, build_packets, extract_and_apply_insights, publish_release, _packet, AdmissionOutcome


async def update_insights(services: Services, base_release: str = 'M0', release_id: str = 'M1',
                          run_id: str | None = None, overwrite: bool = False,
                          limit: int | None = None, pair_id: str | None = None,
                          overwrite_release: bool | None = None) -> dict:
    """`overwrite` re-admits references that were already admitted; `overwrite_release` (defaults to `overwrite`)
    only controls whether an existing release with the same id is replaced. The online loop uses
    overwrite=False, overwrite_release=True so a rolling release can be republished after every sample."""
    if overwrite_release is None:
        overwrite_release = overwrite
    base = services.resolve_release(base_release)
    store = RunStore(services.repo, services.config.path('runs'), run_id)
    meter = UsageMeter()
    budget = services.budget(1)
    budget.max_calls = None; budget.deadline_seconds = None
    learn = services.config.learning
    # 1. admit practice references through the same path (available only from this release on).
    if pair_id:   # indexed lookups: the online loop calls this once per sample
        attempts = [v for v in services.experiences.for_task(pair_id) if v.phase == 'practice'
                    and v.record.get('release_id') == base.release_id]
    else:
        attempts = [v for v in services.experiences.all() if v.kind == 'agent_attempt' and v.phase == 'practice'
                    and v.record.get('release_id') == base.release_id]
    practiced_pids = sorted({v.record['task_id'] for v in attempts if v.record.get('task_id')})

    if pair_id:
        practice_pairs = load_pairs(services, [pair_id])
        if not practice_pairs:
            raise GlocalInductError(f'pair {pair_id} not found in ingested pairs', code='PAIR_NOT_FOUND')
        manifest_meta = {'targeted_pair': pair_id}
        already = {v.record.get('pair_id'): v for p in practice_pairs
                   for v in services.experiences.references_in_group(p.group_id)}
    else:
        split = current_split(services)
        subsets = {'practice'} if learn.reference_demonstrations else {'practice', 'seed'}
        target_pids = [pid for pid in sorted(split.pair_group)
                       if split.partition_of_pair(pid) == 'learning' and split.subset_of_pair(pid) in subsets]
        if practiced_pids:
            practiced_set = set(practiced_pids)
            target_pids = [pid for pid in target_pids if pid in practiced_set]
        if limit is not None and limit > 0:
            target_pids = target_pids[:limit]
            kept_set = set(target_pids)
            attempts = [v for v in attempts if v.record.get('task_id') in kept_set]
        practice_pairs = load_pairs(services, target_pids)
        manifest_meta = {'split': split.manifest_hash, 'practiced_pairs': len(target_pids)}
        already = {v.record.get('pair_id'): v for v in services.experiences.all() if v.kind == 'reference_example'}
    admissions = []
    for p in (practice_pairs if (pair_id or learn.reference_demonstrations) else []):
        if p.pair_id in already and not overwrite:
            prev = already[p.pair_id]
            admissions.append(AdmissionOutcome(
                pair_id=p.pair_id,
                group_id=p.group_id,
                eligible=prev.eligible,
                experience_id=prev.experience_id,
                reasons=prev.record.get('exclusion_reasons', [])
            ))
        else:
            admissions.append(await admit_reference(p, services, store, meter, budget, phase='practice'))
    new_refs = [a.experience_id for a in admissions if a.eligible]
    # 2. comparison packets
    by_task: dict[str, list] = {}
    for v in attempts:
        by_task.setdefault(v.record['task_id'], []).append(v)
    packets = []
    for task, vs in sorted(by_task.items()):
        vs.sort(key=lambda v: v.record['attempt_number'])
        succ = [v for v in vs if v.outcome == 'success']
        fail = [v for v in vs if v.outcome == 'failure']
        if succ and fail:
            # ExpeL: every failed attempt is paired with a success of the same task (not only the last one)
            for f in (fail if learn.pair_every_failure else fail[-1:]):
                packets.append(_packet(services, 'failure_success', sorted([f.experience_id, succ[0].experience_id])))
    successes = [v.experience_id for v in attempts if v.outcome == 'success' and v.eligible]
    packets += build_packets(services, 'success', successes, services.config.insights.batch_size)
    packets += build_packets(services, 'reference', new_refs, services.config.insights.batch_size)
    trace.emit_lazy('packets_built', lambda: dict(
        count=len(packets), new_references=len(new_refs), successful_attempts=len(successes),
        packets=[{'comparison_id': k['comparison_id'], 'kind': k['kind'], 'records': len(k['records'])} for k in packets]))
    insight_summary = await extract_and_apply_insights(services, packets, store, meter, budget)
    # 3. publish M1
    demos = sorted(set(base.demonstration_ids) | set(new_refs) | set(successes))
    rel = publish_release(services, release_id, demos, store.run_id, base.release_id, manifest_meta,
                          overwrite=overwrite_release, reuse_index=base.index)
    trace.emit_lazy('release_published', lambda: dict(release=rel.summary(), insights=insight_summary,
                                                      base=base.release_id))
    summary = {'release': rel.summary(), 'base': base.release_id,
               'practice_pairs': len(practiced_pids) if practiced_pids else len(practice_pairs),
               'new_references': len(new_refs), 'successful_attempts': len(successes),
               'packets': {'failure_success': sum(p['kind'] == 'failure_success' for p in packets),
                           'success': sum(p['kind'] == 'success' for p in packets),
                           'reference': sum(p['kind'] == 'reference' for p in packets)},
               'insights': insight_summary,
               'active_insights': len(services.insights.heads(status='active')),
               'provisional_insights': len(services.insights.heads(status='provisional')),
               'usage': meter.to_contract(), 'run_id': store.run_id}
    store.save('summary', 'consolidate_summary', summary)
    return summary
