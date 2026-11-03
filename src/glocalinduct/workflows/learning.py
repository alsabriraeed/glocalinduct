"""Reference admission and insight extraction shared by seed and consolidation
(guide §8, §9, §7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import trace
from ..agents.runner import Budget, UsageMeter
from ..contracts import ReferenceExperience, InsightRecord
from ..errors import ExpectedPipelineError, GlocalInductError
from ..hashing import new_id, payload_hash, deterministic_id
from ..memory.insights import StaleBatch, comparison_id
from ..records import EvaluationReport, PairAnalysis, SourceProfile
from ..retrieval.search import build_index
from ..services import Services
from .common import RunStore
from .infer import compute_profile
from .ingest import PairRecord


@dataclass
class AdmissionOutcome:
    pair_id: str
    group_id: str
    eligible: bool
    experience_id: str | None
    reasons: list[str] = field(default_factory=list)
    error: str | None = None


def _admission_decision(analysis, report) -> list[str]:
    """Controller-owned admission from A1 observations and A5 reference_quality."""
    reasons: list[str] = []
    for a in analysis.alignments:
        if a.violated_requirement_ids:
            reasons.append(f'ALIGNMENT_UNRESOLVED: alignment {a.alignment_id} violates {a.violated_requirement_ids}')
        if a.observation == 'unresolved' and a.item_ids:
            reasons.append(f'ALIGNMENT_UNRESOLVED: unresolved alignment on items {a.item_ids}')
        if a.observation == 'omission' and a.preserved_requirement_ids == [] and a.item_ids:
            reasons.append(f'ALIGNMENT_UNRESOLVED: omission of cultural item {a.item_ids}')
    for c in report.criteria:
        if c.status != 'pass':
            reasons.append(f'{c.criterion}: {c.status}')
        if any(f.severity == 'critical' for f in c.findings):
            reasons.append(f'{c.criterion}: critical finding')
    return reasons


async def admit_reference(pair: PairRecord, services: Services, store: RunStore, meter: UsageMeter, budget: Budget,
                          phase: str = 'seed', profile: SourceProfile | None = None) -> AdmissionOutcome:
    with trace.scope(pair_id=pair.pair_id, phase=phase, attempt=None):
        trace.emit_lazy('pair_start', lambda: dict(group_id=pair.group_id, source_text=pair.source.normalized.text))
        return await _admit_reference(pair, services, store, meter, budget, phase, profile)


async def _admit_reference(pair: PairRecord, services: Services, store: RunStore, meter: UsageMeter, budget: Budget,
                           phase: str = 'seed', profile: SourceProfile | None = None) -> AdmissionOutcome:
    """Profile (English-only, frozen) → A1 pair analysis → A5 reference quality → admission."""
    try:
        source = pair.source
        if profile is None and services.provider.name != 'fake':
            ctx_h = source.context_hash()
            for obj in reversed(services.repo.objects_of_type('source_profile')):
                pl = obj['payload']
                if pl.get('source_hash') == source.text_hash and pl.get('context_hash') == ctx_h:
                    profile = SourceProfile.from_dict(pl)
                    break
        if profile is None:
            profile = await compute_profile(source, services, store, meter, budget)
        pid = store.save(f'profile_{pair.pair_id}', 'source_profile', profile.to_dict())
        reference = services.private.get(pair.arabic_hash)      # private access path (offline learning only)
        payload, rec = await services.agents.analyse_pair(source, reference, profile, meter, budget, store.raw)
        analysis = PairAnalysis(deterministic_id('pana', pair.pair_id, payload_hash(payload.model_dump(mode='json'))), payload, pair.pair_id, profile.profile_id, source.text_hash, pair.arabic_hash)
        aid = store.save(f'pair_analysis_{pair.pair_id}', 'pair_analysis', analysis.to_dict(), parents=[pid])
        qpayload, rec = await services.agents.assess_reference(source, reference, profile, analysis, meter, budget, store.raw)
        report = EvaluationReport(deterministic_id('eval', pair.arabic_hash, payload_hash(qpayload.model_dump(mode='json'))),
                                  'reference_quality', pair.pair_id, pair.arabic_hash, qpayload,
                                  critical=any(f.severity == 'critical' for c in qpayload.criteria for f in c.findings))
        rid = store.save(f'reference_quality_{pair.pair_id}', 'evaluation_report', report.to_dict(), parents=[aid])
        reasons = _admission_decision(payload, qpayload)
        eligible = not reasons
        record = ReferenceExperience(kind='reference_example', pair_id=pair.pair_id, group_id=pair.group_id,
                                     source_text_id=source.source_text_id, source_profile_id=profile.profile_id,
                                     arabic_text_id=pair.arabic_hash, pair_analysis_id=analysis.analysis_id,
                                     quality_report_id=report.report_id, scope=payload.proposed_scope,
                                     eligible=eligible, exclusion_reasons=reasons)
        items = profile.payload.items
        meta = {'source_text_hash': source.text_hash, 'arabic_text_hash': pair.arabic_hash,
                'domains': sorted({t.domain for t in profile.payload.domains} | set(source.context.domains)),
                'genre': profile.payload.genre or source.context.genre,
                'scope': {**payload.proposed_scope.model_dump(mode='json'), 'cross_domain_transfer': True},
                'summary': profile.payload.purpose,
                'functions': [i.semantic_function for i in items],
                'source_excerpt': source.normalized.text[:600],
                'transformation': ', '.join(sorted({a.observation for a in payload.alignments})) or 'ordinary_translation',
                'preserved': sorted({r for a in payload.alignments for r in a.preserved_requirement_ids})[:20],
                'observations': [{'alignment_id': a.alignment_id, 'observation': a.observation, 'item_ids': a.item_ids,
                                  'source': [q.quote for q in a.source_quotes], 'target': [q.quote for q in a.target_quotes]}
                                 for a in payload.alignments]}
        if eligible:
            # an admitted example's Arabic becomes a memory demonstration resolvable by online
            # bundle builders (other groups only; the current group is always excluded at retrieval)
            services.texts.put(reference, 'demonstration')
        eid = services.experiences.admit_reference(record, meta, store.run_id, parents=[pid, aid, rid], phase=phase)
        store.note(stage='admit_reference', pair_id=pair.pair_id, eligible=eligible, reasons=reasons, experience_id=eid)
        trace.emit_lazy('admission', lambda: dict(
            experience_id=eid, eligible=eligible, reasons=reasons, record=record.model_dump(mode='json'),
            retrieval_meta=meta, profile=profile.payload.model_dump(mode='json'),
            pair_analysis=payload.model_dump(mode='json'), reference_quality=qpayload.model_dump(mode='json')))
        return AdmissionOutcome(pair.pair_id, pair.group_id, eligible, eid, reasons)
    except (ExpectedPipelineError, GlocalInductError) as e:
        store.note(stage='admit_reference_error', pair_id=pair.pair_id, code=e.code, message=str(e))
        trace.emit_lazy('admission_error', lambda: dict(code=e.code, message=str(e)))
        return AdmissionOutcome(pair.pair_id, pair.group_id, False, None, [f'{e.code}: {e}'], error=e.code)


# ----------------------------------------------------------------- insights
def _record_view(services: Services, eid: str, include_arabic: bool = True) -> dict:
    v = services.experiences.get(eid)
    r = v.retrieval
    d = {'experience_id': eid, 'kind': v.kind, 'group_id': v.group_id, 'phase': v.phase, 'outcome': v.outcome,
         'domains': r.get('domains', []), 'genre': r.get('genre'), 'scope': r.get('scope', {}),
         'summary': r.get('summary', ''), 'source_excerpt': r.get('source_excerpt', ''),
         'transformation': r.get('transformation', ''), 'observations': r.get('observations', [])[:12],
         'plan': r.get('plan'), 'assessment': r.get('assessment')}
    fb = v.record.get('feedback_received') if v.kind == 'agent_attempt' else None
    if fb and fb.get('reflection'):
        d['reflection_received'] = fb['reflection']
    if include_arabic and r.get('arabic_text_hash') and services.texts.has(r['arabic_text_hash']):
        d['arabic_excerpt'] = services.texts.get(r['arabic_text_hash']).text[:600]
    elif include_arabic and r.get('arabic_text_hash') and services.private.has(r['arabic_text_hash']):
        d['arabic_excerpt'] = services.private.get(r['arabic_text_hash']).text[:600]
    return d


def build_packets(services: Services, kind: str, experience_ids: list[str], batch_size: int = 8,
                  distinct_groups: bool = True) -> list[dict]:
    """Comparison packets with stable sorted IDs and deterministic comparison IDs."""
    ids = sorted(experience_ids)
    packets: list[dict] = []
    batch: list[str] = []
    groups: set[str] = set()
    for eid in ids:
        v = services.experiences.get(eid)
        if distinct_groups and v.group_id in groups:
            continue
        batch.append(eid); groups.add(v.group_id)
        if len(batch) >= batch_size:
            packets.append(_packet(services, kind, batch)); batch, groups = [], set()
    if batch:
        packets.append(_packet(services, kind, batch))
    return packets


def _packet(services: Services, kind: str, ids: list[str]) -> dict:
    hashes = [services.repo.get_object(e)['payload_hash'] for e in ids]
    return {'comparison_id': comparison_id(kind, ids, hashes), 'kind': kind,
            'records': [_record_view(services, e) for e in ids]}


async def extract_and_apply_insights(services: Services, packets: list[dict], store: RunStore, meter: UsageMeter,
                                     budget: Budget) -> dict:
    with trace.scope(phase='insights', pair_id=None, attempt=None):
        return await _extract_and_apply_insights(services, packets, store, meter, budget)


async def _extract_and_apply_insights(services: Services, packets: list[dict], store: RunStore, meter: UsageMeter,
                                      budget: Budget) -> dict:
    """A2 proposal → A5 review → deterministic transaction, per packet (guide §7)."""
    summary = {'packets': len(packets), 'proposed': 0, 'committed': 0, 'rejected': 0, 'stale': 0, 'errors': 0}
    for packet in packets:
        trace.update(comparison_id=packet['comparison_id'])
        domains = sorted({d for r in packet['records'] for d in r['domains']})
        existing = [i.model_dump(mode='json') for i in services.insights.heads() if i.status != 'retired']
        trace.emit_lazy('packet', lambda: dict(packet_kind=packet['kind'], records=packet['records'], existing_insights=existing))
        try:
            proposal, rec = await services.agents.propose_updates(packet, existing, domains, meter, budget, store.raw)
            summary['proposed'] += len(proposal.operations)
            if not proposal.operations:
                trace.emit_lazy('insight_batch', lambda: dict(note='A2 proposed no operations', committed=[], rejected=[]))
                continue
            prop_id = store.save(f'proposal_{packet["comparison_id"]}', 'insight_proposal', proposal.model_dump(mode='json'))
            min_groups = 1 if services.config.learning.insight_admission == 'immediate' else services.insights.min_support_groups
            admission = {'min_support_groups': min_groups,
                         'universal_min_domains': services.insights.universal_min_domains,
                         'admission_mode': services.config.learning.insight_admission}
            review, rec = await services.agents.review_updates(proposal, packet, existing, admission, domains, meter,
                                                               budget, store.raw)
            rev_id = store.save(f'review_{packet["comparison_id"]}', 'insight_review', review.model_dump(mode='json'))
            res = services.insights.apply_batch(proposal, review, [r['experience_id'] for r in packet['records']],
                                                store.run_id, prop_id, rev_id, ignore_duplicate_add=True)
            summary['committed'] += len(res.committed); summary['rejected'] += len(res.rejected)
            store.note(stage='insight_batch', comparison_id=packet['comparison_id'], committed=res.committed,
                       rejected=res.rejected, replayed=res.replayed)
            trace.emit_lazy('insight_batch', lambda: dict(
                proposal=proposal.model_dump(mode='json'), review=review.model_dump(mode='json'),
                committed=res.committed, rejected=res.rejected, replayed=res.replayed))
        except StaleBatch as e:
            summary['stale'] += 1
            store.note(stage='insight_batch_stale', comparison_id=packet['comparison_id'], error=str(e))
            trace.emit_lazy('insight_batch_error', lambda: dict(code='STALE_BATCH', message=str(e)))
        except (ExpectedPipelineError, GlocalInductError) as e:
            summary['errors'] += 1
            store.note(stage='insight_batch_error', comparison_id=packet['comparison_id'], code=e.code, message=str(e))
            trace.emit_lazy('insight_batch_error', lambda: dict(code=e.code, message=str(e)))
    return summary


def publish_release(services: Services, release_id: str, demonstration_ids: list[str], run_id: str,
                    base_release: str | None = None, extra_hashes: dict | None = None,
                    overwrite: bool = False, reuse_index: dict | None = None):
    active = services.insights.heads(status='active')
    index = build_index(services.experiences, services.insights, demonstration_ids,
                        [(i.insight_id, i.version) for i in active], services.embedder,
                        services.config.retrieval.query_mode, reuse=reuse_index)
    hashes = {**services.version_hashes(), **(extra_hashes or {})}
    return services.releases.publish(release_id, demonstration_ids, active, index, hashes, run_id, base_release,
                                     overwrite=overwrite)
