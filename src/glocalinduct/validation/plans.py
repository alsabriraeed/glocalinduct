"""Deterministic profile and plan validation (guide §4 step 4, §11).

`validate_profile` checks block coverage, quote resolution and metadata provenance.
`validate_plan` checks exact item coverage, unique decisions, valid insight
versions, eligible support membership, exact Arabic support quotations,
essentiality permissions, requirement references and dependency closure. It
cannot prove cultural suitability; A5 checks that later.
"""
from __future__ import annotations

import re

from ..contracts import PlanPayload, QuoteRef, SourceProfilePayload
from ..errors import (PlanConflict, PlanMismatch, ProfileIncomplete, QuoteUnresolved,
                      UnsupportedSubstitution, MemoryReferenceInvalid)
from ..records import RetrievalSnapshot, SourceDocument, SourceProfile
from .spans import TextRegistry, resolve_quote


def source_registry(source: SourceDocument) -> TextRegistry:
    reg = TextRegistry()
    reg.add('source', source.normalized)
    return reg


def validate_profile(payload: SourceProfilePayload, source: SourceDocument,
                     owned_block_ids: list[str] | None = None,
                     supplied_metadata_fields: set[str] | None = None) -> list[str]:
    """Return a list of warnings; raise ProfileIncomplete/QuoteUnresolved on hard failures."""
    reg = source_registry(source)
    owned = owned_block_ids if owned_block_ids is not None else source.normalized.block_ids()
    missing = sorted(set(owned) - set(payload.covered_block_ids))
    if missing:
        raise ProfileIncomplete(f'profile does not cover owned blocks {missing}',
                                details={'missing_blocks': missing})
    extra = sorted(set(payload.covered_block_ids) - set(source.normalized.block_ids()))
    if extra:
        raise ProfileIncomplete(f'profile claims unknown blocks {extra}', details={'unknown_blocks': extra})
    warnings: list[str] = []
    supplied = supplied_metadata_fields if supplied_metadata_fields is not None else {
        k for k, v in source.metadata_provenance.items() if v == 'supplied'}
    for req in payload.requirements:
        for q in req.source_quotes:
            resolve_quote(reg, q, strict=False)
        if req.metadata_field is not None and req.metadata_field not in supplied:
            raise ProfileIncomplete(f'requirement {req.requirement_id} cites metadata field '
                                    f'{req.metadata_field!r} that was not supplied',
                                    details={'requirement_id': req.requirement_id})
    for item in payload.items:
        for q in item.source_quotes:
            resolve_quote(reg, q, strict=False)
            if q.block_id is not None and q.block_id not in owned and q.block_id in source.normalized.block_ids():
                warnings.append(f'item {item.item_id} cites context block {q.block_id} outside owned blocks')
    for tag in payload.domains:
        for q in tag.evidence:
            resolve_quote(reg, q, strict=False)
        if tag.basis == 'supplied' and 'domains' not in supplied:
            warnings.append(f'domain {tag.domain} marked supplied but no domains were supplied')
    if not source.context.objectives:
        obj_reqs = [r for r in payload.requirements if r.metadata_field == 'objectives']
        if obj_reqs:
            raise ProfileIncomplete('profile invents objectives although none were supplied')
    return warnings


def _item_block(item_quotes: list[QuoteRef]) -> set[str]:
    return {q.block_id for q in item_quotes if q.block_id}


def validate_plan(payload: PlanPayload, profile: SourceProfile, source: SourceDocument,
                  retrieval: RetrievalSnapshot, release_id: str,
                  item_ids: list[str] | None = None, insight_substitution: bool = False,
                  allow_unsupported: bool = False) -> list[dict]:
    """Validate a plan payload against the frozen profile and retrieval snapshot.
    Returns the deterministic check log. Raises on any violation."""
    log: list[dict] = []
    prof = profile.payload
    expected_items = item_ids if item_ids is not None else [i.item_id for i in prof.items]
    decided = [d.item_id for d in payload.decisions]
    if sorted(decided) != sorted(expected_items):
        raise PlanMismatch('plan must contain exactly one decision per supplied cultural item',
                           details={'expected': sorted(expected_items), 'got': sorted(decided)})
    log.append({'check': 'item_coverage', 'status': 'pass', 'count': len(expected_items)})
    if retrieval.release_id != release_id:
        raise MemoryReferenceInvalid('retrieval snapshot belongs to a different release',
                                     details={'expected': release_id, 'got': retrieval.release_id})
    items = {i.item_id: i for i in prof.items}
    req_ids = {r.requirement_id for r in prof.requirements}
    insight_versions = {(i.insight_id, i.version) for i in retrieval.insights}
    reg = source_registry(source)
    for d in payload.decisions:
        item = items[d.item_id]
        for rid in d.preserve_requirement_ids:
            if rid not in req_ids:
                raise PlanMismatch(f'decision {d.decision_id} cites unknown requirement {rid}')
        for ref in d.insight_refs:
            if (ref.insight_id, ref.version) not in insight_versions:
                raise MemoryReferenceInvalid(f'decision {d.decision_id} cites insight {ref.insight_id} v{ref.version} '
                                             'not in the retrieval snapshot',
                                             details={'insight_id': ref.insight_id, 'version': ref.version})
        if d.strategy == 'substitute':
            if item.essentiality != 'incidental' and not allow_unsupported:
                raise UnsupportedSubstitution(f'item {item.item_id} is {item.essentiality}; only incidental items '
                                              'may be substituted', details={'item_id': item.item_id})
            if not allow_unsupported and any(getattr(items.get(dep), 'essentiality', 'incidental') == 'essential'
                   for dep in item.depends_on_item_ids):
                raise UnsupportedSubstitution(f'item {item.item_id} depends on an essential item',
                                              details={'item_id': item.item_id})
            if not d.support and not (insight_substitution and d.insight_refs) and not allow_unsupported:
                raise UnsupportedSubstitution(f'substitution for {d.decision_id} needs retrieved experience support',
                                              details={'decision_id': d.decision_id})
            for s in d.support:
                exp = retrieval.experience(s.experience_id)
                if exp is None:
                    raise MemoryReferenceInvalid(f'support experience {s.experience_id} is not in the retrieval snapshot',
                                                 details={'experience_id': s.experience_id})
                if s.arabic_quote.text_key != f'experience:{s.experience_id}':
                    raise MemoryReferenceInvalid(f'support quote must use text_key experience:{s.experience_id}')
                r = TextRegistry()
                r.add(s.arabic_quote.text_key, exp.arabic_text)
                resolve_quote(r, s.arabic_quote, strict=False)
                if not (insight_substitution and d.insight_refs) and not allow_unsupported:
                    words_c = [w.strip('ال') for w in re.findall(r'[\u0621-\u064A]+', d.counterpart_ar) if len(w.strip('ال')) >= 3]
                    quote_text = s.arabic_quote.quote
                    has_match = (
                        d.counterpart_ar in quote_text
                        or quote_text in d.counterpart_ar
                        or d.canonical_concept in quote_text
                        or any(w in quote_text for w in words_c)
                    )
                    if not has_match:
                        raise UnsupportedSubstitution(f'support quote for {d.decision_id} does not contain the counterpart',
                                                      details={'decision_id': d.decision_id})
                    if exp.scope.get('culture_specific') and exp.scope.get('locale_scope') == 'unspecified':
                        raise UnsupportedSubstitution('an unspecified cultural scope cannot support a substitution',
                                                      details={'experience_id': s.experience_id})
        for edit in d.dependent_edits:
            for q in edit.source_quotes:
                resolve_quote(reg, q, strict=False)
            for rel in edit.related_item_ids:
                if rel not in items:
                    raise PlanMismatch(f'dependent edit cites unknown item {rel}')
    # dependency closure: substituted items' dependencies must have a decision, and
    # two decisions in one dependency component cannot bind the same source concept to
    # different counterparts
    decision_of = {d.item_id: d for d in payload.decisions}
    concept_counterparts: dict[str, set[str]] = {}
    for d in payload.decisions:
        if d.strategy == 'substitute':
            concept_counterparts.setdefault(d.canonical_concept, set()).add(d.counterpart_ar)
            for dep in items[d.item_id].depends_on_item_ids:
                if dep not in decision_of:
                    raise PlanConflict(f'dependency {dep} of {d.item_id} has no decision')
    conflicts = {c: sorted(v) for c, v in concept_counterparts.items() if len(v) > 1}
    if conflicts:
        raise PlanConflict('conflicting counterparts for the same concept', details={'conflicts': conflicts})
    for t in payload.terminology:
        for iid in t.item_ids:
            if iid not in items:
                raise PlanMismatch(f'terminology binding cites unknown item {iid}')
    log.append({'check': 'support_resolution', 'status': 'pass'})
    log.append({'check': 'dependency_closure', 'status': 'pass'})
    return log
