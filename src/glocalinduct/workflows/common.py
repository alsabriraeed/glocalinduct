"""Shared workflow helpers: request normalisation, run persistence, profile merging."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import Context, SourceProfilePayload
from ..errors import ExtractionError, GlocalInductError
from ..hashing import new_id, payload_hash, utc_now
from ..ingestion import build_normalized_text, build_windows, extract_file, extract_text
from ..ingestion.normalize import NormalizedText
from ..ingestion.windows import split_oversized_blocks
from ..memory.store import Repository
from ..records import SourceDocument, SourceProfile

CONTEXT_DEFAULTS = {'domains': [], 'genre': None, 'audience': None, 'locale_scope': 'unspecified', 'locales': [],
                    'setting': None, 'time_period': None, 'objectives': []}


@dataclass
class InferRequest:
    text: str | None = None
    file_path: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: new_id('req'))
    budget_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'request_id': self.request_id, 'text': self.text, 'file_path': self.file_path,
                'context': self.context, 'budget_overrides': self.budget_overrides}


def context_from_metadata(meta: dict[str, Any]) -> tuple[Context, dict[str, str]]:
    """Build a Context, recording which fields were supplied vs defaulted (guide §4 step 1)."""
    clean_meta = dict(meta)
    if str(clean_meta.get('genre', '')).strip().lower() in ('sentence', 'statement'):
        clean_meta.pop('genre', None)
    if str(clean_meta.get('audience', '')).strip().lower() in ('general adult readers', 'general'):
        clean_meta.pop('audience', None)
    if 'domains' in clean_meta:
        doms = [clean_meta['domains']] if isinstance(clean_meta['domains'], str) else list(clean_meta['domains'])
        cleaned_doms = [str(d).strip().lower().replace(' ', '_') for d in doms
                        if str(d).strip() and str(d).strip().lower() != 'general']
        if cleaned_doms:
            clean_meta['domains'] = cleaned_doms
        else:
            clean_meta.pop('domains', None)

    values = dict(CONTEXT_DEFAULTS)
    prov: dict[str, str] = {}
    for k in CONTEXT_DEFAULTS:
        if k in clean_meta and clean_meta[k] not in (None, '', []):
            values[k] = clean_meta[k]
            prov[k] = 'supplied'
        else:
            prov[k] = 'default'
    if isinstance(values['domains'], str):
        values['domains'] = [values['domains']]
    values['domains'] = [str(d).strip().lower().replace(' ', '_') for d in values['domains']
                         if str(d).strip() and str(d).strip().lower() != 'general']
    if not values['domains'] and 'domains' in prov:
        prov['domains'] = 'default'
    if isinstance(values['locales'], str):
        values['locales'] = [values['locales']]
    if isinstance(values['objectives'], str):
        values['objectives'] = [values['objectives']]
    if values['locales'] and values['locale_scope'] != 'specified':
        values['locale_scope'] = 'specified'
    if values['locale_scope'] == 'specified' and not values['locales']:
        raise GlocalInductError('locale_scope "specified" requires at least one locale', code='INPUT_INVALID')
    if values['locale_scope'] not in ('general_arab', 'specified', 'unspecified'):
        raise GlocalInductError(f'unknown locale_scope {values["locale_scope"]!r}', code='INPUT_INVALID')
    return Context.model_validate(values), prov


def build_source_document(text: str | None = None, file_path: str | None = None, context: dict | None = None,
                          content_tokens: int = 1200, context_tokens: int = 200, pair_id: str | None = None,
                          origin: str = 'request') -> SourceDocument:
    ctx, prov = context_from_metadata(context or {})
    raw_hash = None
    warnings: list[str] = []
    if file_path:
        res = extract_file(file_path)
        if res.issues:
            raise ExtractionError(f'extraction issues: {res.issues}', code=res.issues[0], details={'warnings': res.warnings})
        blocks = list(res.normalized.blocks)
        raw_hash, warnings = res.raw_hash, res.warnings
    elif text is not None and text.strip():
        blocks = extract_text(text)
    else:
        raise GlocalInductError('empty request: provide text or a file', code='INPUT_INVALID')
    if not blocks:
        raise ExtractionError('no content blocks extracted')
    blocks = split_oversized_blocks(blocks, content_tokens)
    nt = build_normalized_text(blocks)
    windows = build_windows(blocks, content_tokens, context_tokens)
    return SourceDocument(source_text_id=f'src_{nt.text_hash[:16]}', normalized=nt, windows=windows, context=ctx,
                          metadata_provenance=prov, raw_hash=raw_hash, origin=origin, pair_id=pair_id,
                          extraction_warnings=warnings)


def merge_profiles(parts: list[tuple[str, SourceProfilePayload]], source: SourceDocument) -> SourceProfilePayload:
    """Deterministic merge of window profiles: handles are prefixed by window, duplicate
    item mentions (exact source span) are removed, conflicts become uncertainties."""
    if len(parts) == 1:
        return parts[0][1]
    owned_by_win = {w.window_id: set(w.owned_block_ids) for w in source.windows}
    reqs, items, domains, covered, uncertainties = [], [], {}, [], []
    purposes = []
    seen_spans: dict[tuple, str] = {}
    id_map: dict[tuple[str, str], str] = {}
    for wid, p in parts:
        own = owned_by_win.get(wid)
        purposes.append(p.purpose)
        for t in p.domains:
            domains.setdefault(t.domain, t)
        for r in p.requirements:
            nid = f'{wid}.{r.requirement_id}'
            id_map[(wid, r.requirement_id)] = nid
            reqs.append(r.model_copy(update={'requirement_id': nid}))
        for it in p.items:
            if own is not None:
                owned_quotes = [q for q in it.source_quotes if q.block_id in own]
                if not owned_quotes:
                    continue
                it = it.model_copy(update={'source_quotes': owned_quotes})
            key = tuple((q.text_key, q.block_id, q.quote, q.occurrence) for q in it.source_quotes)
            if key in seen_spans:
                id_map[(wid, it.item_id)] = seen_spans[key]
                continue
            nid = f'{wid}.{it.item_id}'
            seen_spans[key] = nid
            id_map[(wid, it.item_id)] = nid
            items.append((wid, it, nid))
        covered.extend(p.covered_block_ids)
        uncertainties.extend(p.uncertainties)
    final_items = []
    for wid, it, nid in items:
        final_items.append(it.model_copy(update={
            'item_id': nid,
            'requirement_ids': [id_map[(wid, r)] for r in it.requirement_ids if (wid, r) in id_map],
            'depends_on_item_ids': [id_map[(wid, d)] for d in it.depends_on_item_ids if (wid, d) in id_map and id_map[(wid, d)] != nid]}))
    purpose = purposes[0]
    if len(set(purposes)) > 1:
        uncertainties.append('Window purposes differ: ' + ' | '.join(sorted(set(purposes))))
    return SourceProfilePayload(purpose=purpose, purpose_basis=parts[0][1].purpose_basis,
                                domains=list(domains.values()), genre=parts[0][1].genre, audience=parts[0][1].audience,
                                requirements=reqs, items=final_items, covered_block_ids=list(dict.fromkeys(covered)),
                                uncertainties=list(dict.fromkeys(uncertainties)))


class RunStore:
    """Persists every stage (including raw provider responses before interpretation)
    to the repository and to a per-run folder for inspection."""

    def __init__(self, repo: Repository, runs_dir: Path, run_id: str | None = None):
        self.repo = repo
        self.run_id = run_id or new_id('run')
        self.dir = Path(runs_dir) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, Any] = {}

    def raw(self, item: dict) -> None:
        with (self.dir / 'raw_responses.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({**item, 'at': utc_now()}, ensure_ascii=False) + '\n')

    def save(self, stage_key: str, record_type: str, payload: dict, parents: list[str] | None = None,
             cache_key: str | None = None, record_id: str | None = None) -> str:
        rid = self.repo.put_object(record_type, payload, self.run_id, parents, record_id)
        self.repo.checkpoint(self.run_id, stage_key, cache_key or payload_hash({'stage': stage_key, 'rid': rid}), rid)
        (self.dir / f'{stage_key}.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1), 'utf-8')
        self.records[stage_key] = payload
        return rid

    def resume(self, stage_key: str, cache_key: str) -> dict | None:
        rid = self.repo.find_checkpoint(self.run_id, stage_key) or self.repo.find_by_cache_key(cache_key)
        return self.repo.payload(rid) if rid else None

    def note(self, **kw) -> None:
        with (self.dir / 'log.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({**kw, 'at': utc_now()}, ensure_ascii=False) + '\n')
