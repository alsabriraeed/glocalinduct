"""Candidate assembly and deterministic checks (guide §12).

The controller assembles Arabic blocks in source order, assigns persistent output
block IDs, resolves every item realization against `candidate:<source_block_id>`,
and computes the candidate hash. Coverage, extra blocks, unresolved quotes and
fixed-surface identity are deterministic failures.
"""
from __future__ import annotations

import unicodedata

from ..contracts import CandidatePayload, PlanPayload, SourceProfilePayload
from ..errors import CoverageIncomplete, PlanMismatch
from ..hashing import payload_hash, new_id, deterministic_id
from ..ingestion.normalize import Block, build_normalized_text, normalize_text, NormalizedText
from ..records import ArabicCandidate, SourceDocument
from .spans import TextRegistry, resolve_quote


def candidate_registry(payload: CandidatePayload) -> TextRegistry:
    reg = TextRegistry()
    for b in payload.blocks:
        nt = build_normalized_text([Block(b.source_block_id, 'paragraph', normalize_text(b.text_ar).strip(), 0)])
        reg.add(f'candidate:{b.source_block_id}', nt)
    return reg


def items_in_blocks(profile: SourceProfilePayload, block_ids: list[str]) -> list[str]:
    owned = set(block_ids)
    return [i.item_id for i in profile.items
            if any((q.block_id in owned) for q in i.source_quotes)]


def validate_candidate(payload: CandidatePayload, owned_block_ids: list[str],
                       profile: SourceProfilePayload, plan: PlanPayload) -> list[dict]:
    log: list[dict] = []
    got = [b.source_block_id for b in payload.blocks]
    missing = sorted(set(owned_block_ids) - set(got))
    extra = sorted(set(got) - set(owned_block_ids))
    if missing:
        raise CoverageIncomplete(f'candidate is missing owned blocks {missing}', details={'missing': missing})
    if extra:
        raise CoverageIncomplete(f'candidate emits blocks it does not own {extra}', details={'extra': extra})
    log.append({'check': 'block_coverage', 'status': 'pass'})
    expected_items = set(items_in_blocks(profile, owned_block_ids))
    realized = {r.item_id for r in payload.realizations}
    unrealized = sorted(expected_items - realized)
    if unrealized:
        raise CoverageIncomplete(f'items without a realization: {unrealized}', details={'items': unrealized})
    unknown = sorted(realized - {i.item_id for i in profile.items})
    if unknown:
        raise PlanMismatch(f'realizations for unknown items {unknown}')
    reg = candidate_registry(payload)
    decisions = {d.item_id: d for d in plan.decisions}
    for r in payload.realizations:
        quotes = []
        for q in r.output_quotes:
            if not q.text_key.startswith('candidate:') or q.block_id != q.text_key[len('candidate:'):]:
                raise PlanMismatch(f'realization of {r.item_id} must quote candidate:<block_id> with matching block_id')
            resolve_quote(reg, q, strict=(len(owned_block_ids) <= 1))
            quotes.append(unicodedata.normalize('NFC', q.quote))
        d = decisions.get(r.item_id)
        if d and d.strategy == 'substitute' and d.term_policy == 'fixed':
            import re
            surface = unicodedata.normalize('NFC', d.counterpart_ar)
            words_c = [w.strip('ال') for w in re.findall(r'[\u0621-\u064A]+', surface) if len(w.strip('ال')) >= 3]
            if not (any(surface in q for q in quotes) or (words_c and any(w in q for q in quotes for w in words_c))):
                raise PlanMismatch(f'fixed counterpart {d.counterpart_ar!r} absent from realization of {r.item_id}',
                                   details={'item_id': r.item_id})
    log.append({'check': 'realization_resolution', 'status': 'pass'})
    for b in payload.blocks:
        if not normalize_text(b.text_ar).strip():
            raise CoverageIncomplete(f'empty Arabic block {b.source_block_id}')
    return log


def assemble_candidate(source: SourceDocument, payloads: list[CandidatePayload], plan_id: str,
                       plan_hash: str) -> ArabicCandidate:
    """Merge window payloads into one document in source order."""
    by_block: dict[str, str] = {}
    realizations: dict[str, list[dict]] = {}
    for p in payloads:
        reg = candidate_registry(p)
        for b in p.blocks:
            if b.source_block_id in by_block:
                raise CoverageIncomplete(f'block {b.source_block_id} emitted twice')
            by_block[b.source_block_id] = normalize_text(b.text_ar).strip()
        for r in p.realizations:
            spans = []
            for q in r.output_quotes:
                s = resolve_quote(reg, q, strict=(len(source.windows) <= 1))
                spans.append({'block_id': q.block_id, 'quote': s.quote, 'start': s.start, 'end': s.end})
            realizations.setdefault(r.item_id, []).extend(spans)
    missing = [b for b in source.normalized.block_ids() if b not in by_block]
    if missing:
        raise CoverageIncomplete(f'assembled candidate lacks blocks {missing}', details={'missing': missing})
    blocks: list[Block] = []
    block_map: dict[str, str] = {}
    for src in source.normalized.blocks:
        out_id = f'ar_{src.block_id}'
        block_map[src.block_id] = out_id
        blocks.append(Block(out_id, src.kind, by_block[src.block_id], len(blocks), src.level, src.cells))
    arabic = build_normalized_text(blocks)
    merged = CandidatePayload(blocks=[{'source_block_id': k, 'text_ar': v} for k, v in
                                      ((s.block_id, by_block[s.block_id]) for s in source.normalized.blocks)],
                              realizations=[{'item_id': k, 'output_quotes': [
                                  {'text_key': f'candidate:{s["block_id"]}', 'block_id': s['block_id'],
                                   'quote': s['quote'], 'occurrence': 0} for s in v]} for k, v in realizations.items()],
                              unresolved_issues=[i for p in payloads for i in p.unresolved_issues])
    candidate_hash = payload_hash({'plan_hash': plan_hash, 'source_hash': source.text_hash,
                                   'arabic_hash': arabic.text_hash, 'blocks': [b.to_dict() for b in blocks]})
    cand = ArabicCandidate(candidate_id=deterministic_id('cand', candidate_hash), plan_id=plan_id, plan_hash=plan_hash,
                           source_hash=source.text_hash, payload=merged, arabic=arabic, block_map=block_map,
                           resolved_realizations=realizations, candidate_hash=candidate_hash)
    return cand
