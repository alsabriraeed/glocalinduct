"""Field-allowlisted input bundles per role/mode (guide §17, storage README).

Every bundle declares the texts an agent may quote under `texts[text_key]`.
Online modes (analyse_source, make_plan, generate_arabic, candidate_quality)
never receive the current Arabic reference, PairAnalysis, target-derived
annotations or private comparison findings. Memory demonstrations from other
eligible groups may include Arabic under `experience:<id>` keys.
"""
from __future__ import annotations

from typing import Any

from ..contracts import Feedback, PlanPayload, SourceProfilePayload
from ..ingestion.normalize import NormalizedText
from ..ingestion.windows import Window
from ..records import AdaptationPlan, ArabicCandidate, RetrievalSnapshot, SourceDocument, SourceProfile
from ..validation.leakage import assert_no_leakage
from ..validation.policies import ResolvedPolicy


def _blocks(nt: NormalizedText, ids: list[str] | None = None) -> list[dict]:
    keep = set(ids) if ids is not None else None
    return [{'block_id': b.block_id, 'kind': b.kind, 'text': b.text, **({'cells': list(b.cells)} if b.cells else {})}
            for b in nt.blocks if keep is None or b.block_id in keep]


def _metadata(source: SourceDocument) -> dict:
    return {'context': source.context.model_dump(mode='json'), 'provenance': source.metadata_provenance}


def _window_texts(source: SourceDocument, window: Window | None) -> dict:
    if window is None:
        return {'texts': {'source': {'blocks': _blocks(source.normalized)}},
                'owned_block_ids': source.normalized.block_ids(), 'context_block_ids': []}
    ctx_ids = list(window.context_before_ids) + list(window.context_after_ids)
    ids = list(window.context_before_ids) + list(window.owned_block_ids) + list(window.context_after_ids)
    return {'texts': {'source': {'blocks': _blocks(source.normalized, ids)}},
            'owned_block_ids': list(window.owned_block_ids), 'context_block_ids': ctx_ids,
            'window_id': window.window_id}


def _profile_for(profile: SourceProfilePayload, owned: list[str] | None, strip: bool = False) -> dict:
    d = profile.model_dump(mode='json')
    if owned is not None:
        own = set(owned)
        d['items'] = [i for i in d['items'] if any(q['block_id'] in own for q in i['source_quotes'])]
    return d


def source_analysis_bundle(source: SourceDocument, window: Window | None, policy: ResolvedPolicy) -> dict:
    b = {**_window_texts(source, window), 'metadata': _metadata(source), 'policy': policy.to_dict()}
    assert_no_leakage(b)
    return b


def _retrieval_view(retrieval: RetrievalSnapshot) -> dict:
    return {'release_id': retrieval.release_id,
            'experiences': [{'experience_id': e.experience_id, 'kind': e.kind, 'scope': e.scope,
                             'domains': e.domains, 'source_function': e.source_function,
                             'transformation': e.transformation, 'preserved': e.preserved,
                             'source_excerpt': e.source_excerpt,
                             'arabic_text': {'text_key': f'experience:{e.experience_id}',
                                             'blocks': _blocks(e.arabic_text)}} for e in retrieval.experiences],
            'insights': [{'insight_id': i.insight_id, 'version': i.version, 'importance': i.importance,
                          'content': {k: v for k, v in i.content.items()
                                      if k not in ('support_experience_ids', 'counterevidence_ids')}}
                         for i in retrieval.insights]}


def plan_bundle(source: SourceDocument, window: Window | None, profile: SourceProfile,
                retrieval: RetrievalSnapshot, policy: ResolvedPolicy, feedback: list[Feedback] | None = None,
                reconciliation: dict | None = None) -> dict:
    owned = list(window.owned_block_ids) if window else None
    texts = _window_texts(source, window)
    for e in retrieval.experiences:
        texts['texts'][f'experience:{e.experience_id}'] = {'blocks': _blocks(e.arabic_text)}
    b = {**texts, 'metadata': _metadata(source), 'policy': policy.to_dict(),
         'profile': _profile_for(profile.payload, owned), 'retrieval': _retrieval_view(retrieval)}
    if feedback:
        b['feedback'] = [f.model_dump(mode='json') for f in feedback]
    if reconciliation:
        b['reconciliation'] = reconciliation
    assert_no_leakage(b)
    return b


def generation_bundle(source: SourceDocument, window: Window | None, profile: SourceProfile,
                      plan: AdaptationPlan, policy: ResolvedPolicy, feedback: list[Feedback] | None = None) -> dict:
    owned = list(window.owned_block_ids) if window else None
    prof = _profile_for(profile.payload, owned)
    item_ids = {i['item_id'] for i in prof['items']}
    plan_d = plan.payload.model_dump(mode='json')
    plan_d['decisions'] = [{k: v for k, v in d.items() if k != 'rationale'} for d in plan_d['decisions']
                           if d['item_id'] in item_ids]
    b = {**_window_texts(source, window), 'metadata': _metadata(source), 'policy': policy.to_dict(),
         'profile': {'purpose': prof['purpose'], 'domains': prof['domains'], 'genre': prof['genre'],
                     'audience': prof['audience'], 'requirements': prof['requirements'], 'items': prof['items']},
         'plan': plan_d, 'plan_id': plan.plan_id}
    if feedback:
        b['feedback'] = [f.model_dump(mode='json') for f in feedback]
    assert_no_leakage(b)
    return b


def candidate_quality_bundle(source: SourceDocument, window: Window | None, profile: SourceProfile,
                             plan: AdaptationPlan, candidate_blocks: list[dict], retrieval: RetrievalSnapshot,
                             policy: ResolvedPolicy, reference: NormalizedText | None = None) -> dict:
    owned = list(window.owned_block_ids) if window else None
    prof = _profile_for(profile.payload, owned)
    item_ids = {i['item_id'] for i in prof['items']}
    decisions = []
    for d in plan.payload.decisions:
        if d.item_id not in item_ids:
            continue
        dd = d.model_dump(mode='json')
        dd.pop('rationale', None)            # no persuasive rationale (guide §13)
        decisions.append(dd)
    cited = {s.experience_id for d in plan.payload.decisions for s in d.support}
    support = [{'experience_id': e.experience_id, 'scope': e.scope, 'domains': e.domains,
                'source_function': e.source_function, 'source_excerpt': e.source_excerpt,
                'arabic_text': {'text_key': f'experience:{e.experience_id}', 'blocks': _blocks(e.arabic_text)}}
               for e in retrieval.experiences if e.experience_id in cited]
    texts = _window_texts(source, window)
    for b_ in candidate_blocks:
        texts['texts'][f'candidate:{b_["source_block_id"]}'] = {'blocks': [
            {'block_id': b_['source_block_id'], 'kind': 'paragraph', 'text': b_['text_ar']}]}
    if reference is not None:
        texts['texts']['reference'] = {'blocks': _blocks(reference)}
    for e in retrieval.experiences:
        if e.experience_id in cited:
            texts['texts'][f'experience:{e.experience_id}'] = {'blocks': _blocks(e.arabic_text)}
    b = {**texts, 'metadata': _metadata(source), 'policy': policy.to_dict(),
         'profile': {'purpose': prof['purpose'], 'domains': prof['domains'], 'requirements': prof['requirements'],
                     'items': prof['items']},
         'plan': {'decisions': decisions, 'terminology': plan.payload.model_dump(mode='json')['terminology']},
         'candidate': {'blocks': candidate_blocks}, 'support': support}
    if reference is not None:
        b['reference'] = {'blocks': _blocks(reference)}
    else:
        assert_no_leakage(b)
    return b


def consistency_bundle(source: SourceDocument, profile: SourceProfile, plan: AdaptationPlan,
                       candidate: ArabicCandidate, retrieval: RetrievalSnapshot, policy: ResolvedPolicy,
                       reference: NormalizedText | None = None) -> dict:
    """Cross-window pass over the complete document (guide §12)."""
    blocks = [{'source_block_id': b.source_block_id, 'text_ar': b.text_ar} for b in candidate.payload.blocks]
    b = candidate_quality_bundle(source, None, profile, plan, blocks, retrieval, policy, reference=reference)
    b['pass'] = 'cross_window_consistency'
    return b


# ----------------------------------------------------------- offline (reference) modes
def pair_analysis_bundle(source: SourceDocument, reference: NormalizedText, profile: SourceProfile,
                         policy: ResolvedPolicy, candidates: list[dict] | None = None) -> dict:
    return {'texts': {'source': {'blocks': _blocks(source.normalized)}, 'reference': {'blocks': _blocks(reference)}},
            'metadata': _metadata(source), 'policy': policy.to_dict(), 'profile': profile.payload.model_dump(mode='json'),
            'alignment_candidates': candidates or []}


def reference_quality_bundle(source: SourceDocument, reference: NormalizedText, profile: SourceProfile,
                             analysis: dict, policy: ResolvedPolicy) -> dict:
    return {'texts': {'source': {'blocks': _blocks(source.normalized)}, 'reference': {'blocks': _blocks(reference)}},
            'metadata': _metadata(source), 'policy': policy.to_dict(), 'profile': profile.payload.model_dump(mode='json'),
            'pair_analysis': analysis}


def reference_comparison_bundle(source: SourceDocument, profile: SourceProfile, candidate: ArabicCandidate,
                                reference: NormalizedText, policy: ResolvedPolicy) -> dict:
    cand_blocks = {f'candidate:{b.source_block_id}': {'blocks': [{'block_id': b.source_block_id, 'kind': 'paragraph',
                                                                   'text': b.text_ar}]} for b in candidate.payload.blocks}
    return {'texts': {'source': {'blocks': _blocks(source.normalized)}, 'reference': {'blocks': _blocks(reference)},
                      **cand_blocks},
            'metadata': _metadata(source), 'policy': policy.to_dict(),
            'profile': {'requirements': profile.payload.model_dump(mode='json')['requirements']},
            'candidate': {'candidate_id': candidate.candidate_id, 'candidate_hash': candidate.candidate_hash,
                          'blocks': [{'source_block_id': b.source_block_id, 'text_ar': b.text_ar}
                                     for b in candidate.payload.blocks]}}


def insight_update_bundle(packet: dict, existing_insights: list[dict], policy: ResolvedPolicy) -> dict:
    return {'packet': packet, 'existing_insights': existing_insights, 'policy': policy.to_dict()}


def insight_review_bundle(proposal: dict, packet: dict, existing_insights: list[dict], admission: dict) -> dict:
    return {'proposal': proposal, 'packet': packet, 'existing_insights': existing_insights, 'admission_policy': admission}


def reflection_bundle(source: SourceDocument, profile: SourceProfile, attempt: dict, signals: dict,
                      earlier_reflections: list[str], policy: ResolvedPolicy,
                      forbidden_texts: list[str] | None = None, glocalized_arabic: str | None = None) -> dict:
    """ExpeL `LLM_reflect` input: the failed attempt plus the failure signals.

    `signals` is built by the controller from A5 findings and the comparison. During practice the
    reflector also receives the glocalized (reference) Arabic of this very pair, so its reflection can
    carry concrete hints and corrections for the next attempt.
    """
    prof = profile.payload.model_dump(mode='json')
    b = {'texts': {'source': {'blocks': _blocks(source.normalized)}}, 'metadata': _metadata(source),
         'policy': policy.to_dict(),
         'profile': {'purpose': prof['purpose'], 'requirements': prof['requirements'], 'items': prof['items']},
         'attempt': attempt, 'signals': signals, 'earlier_reflections': list(earlier_reflections)}
    if glocalized_arabic:
        b['glocalized_pair'] = {'source_text': source.normalized.text, 'glocalized_arabic': glocalized_arabic}
    assert_no_leakage(b, forbidden_texts)
    return b
