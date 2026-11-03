"""`ingest_pairs` and `split_groups` workflows (guide §4, §14).

Pair records hold the English source document, the private Arabic reference
descriptor (never included in online bundles) and metadata provenance. Grouping
and the frozen split manifest are stored as immutable objects.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ExtractionError, GlocalInductError
from ..hashing import payload_hash, sha256_bytes
from ..ingestion import (
    discover_pairs, extract_file, extract_text, load_metadata, load_xlsx_pairs,
    group_documents, split_groups as _split,
)
from ..ingestion.grouping import DocumentForGrouping
from ..ingestion.splits import SplitManifest
from ..ingestion.windows import split_oversized_blocks
from ..ingestion import build_normalized_text, build_windows
from ..records import SourceDocument
from ..services import Services
from .common import context_from_metadata


@dataclass
class PairRecord:
    pair_id: str
    source: SourceDocument
    arabic_hash: str                 # private reference, resolved only through PrivateReferenceStore
    english_raw_hash: str
    arabic_raw_hash: str
    metadata: dict[str, Any]
    family: str | None
    warnings: list[str] = field(default_factory=list)
    group_id: str | None = None

    def to_dict(self) -> dict:
        return {'pair_id': self.pair_id, 'source': self.source.to_dict(), 'arabic_hash': self.arabic_hash,
                'english_raw_hash': self.english_raw_hash, 'arabic_raw_hash': self.arabic_raw_hash,
                'metadata': self.metadata, 'family': self.family, 'warnings': self.warnings, 'group_id': self.group_id}

    @classmethod
    def from_dict(cls, d: dict) -> 'PairRecord':
        return cls(d['pair_id'], SourceDocument.from_dict(d['source']), d['arabic_hash'], d['english_raw_hash'],
                   d['arabic_raw_hash'], d['metadata'], d.get('family'), d.get('warnings', []), d.get('group_id'))


@dataclass
class IngestReport:
    pairs: list[PairRecord]
    exclusions: list[dict]
    manifest_hash: str = ''

    def summary(self) -> dict:
        return {'pairs': len(self.pairs), 'exclusions': len(self.exclusions), 'manifest_hash': self.manifest_hash,
                'exclusion_codes': sorted({e['code'] for e in self.exclusions})}


def ingest_pairs(services: Services, data_root: str | Path | None = None, run_id: str = 'ingest') -> IngestReport:
    cfg = services.config
    root = Path(data_root) if data_root else cfg.path('data_root')
    pairs: list[PairRecord] = []
    ar_texts: dict[str, str] = {}

    if root.is_file() and root.suffix.lower() == '.xlsx':
        raw_rows, exclusions = load_xlsx_pairs(root)
        if cfg.include_pairs and (data_root is None or root.resolve() == cfg.path('data_root').resolve()):
            allowed = set(cfg.include_pairs)
            raw_rows = [row for row in raw_rows if row[0] in allowed]
            exclusions = [e for e in exclusions if e.get('pair_id') in allowed]
        for pid, en_text, ar_text in raw_rows:
            en_blocks = extract_text(en_text)
            ar_blocks = extract_text(ar_text)
            if not en_blocks or not ar_blocks:
                exclusions.append({'pair_id': pid, 'code': 'EXTRACTION_FAILED', 'reason': 'no content blocks extracted'})
                continue
            try:
                ctx, prov = context_from_metadata({})
                blocks = split_oversized_blocks(list(en_blocks), cfg.windows.content_tokens)
                nt = build_normalized_text(blocks)
                ar_nt = build_normalized_text(ar_blocks)
                windows = build_windows(blocks, cfg.windows.content_tokens, cfg.windows.context_tokens_each_side)
            except GlocalInductError as e:
                exclusions.append({'pair_id': pid, 'code': e.code, 'reason': str(e)}); continue
            en_raw_hash = sha256_bytes(en_text.encode('utf-8'))
            ar_raw_hash = sha256_bytes(ar_text.encode('utf-8'))
            source = SourceDocument(source_text_id=f'src_{nt.text_hash[:16]}', normalized=nt, windows=windows, context=ctx,
                                    metadata_provenance=prov, raw_hash=en_raw_hash, origin='pair', pair_id=pid,
                                    extraction_warnings=[])
            services.texts.put(nt, 'source')
            services.private.put(ar_nt, 'reference')
            ar_texts[pid] = ar_nt.text
            rec = PairRecord(pid, source, ar_nt.text_hash, en_raw_hash, ar_raw_hash, {}, None, [])
            pairs.append(rec)
    else:
        disc = discover_pairs(root, cfg.pair_id_pattern)
        if cfg.include_pairs and (data_root is None or root.resolve() == cfg.path('data_root').resolve()):
            allowed = set(cfg.include_pairs)
            disc_pairs = [pp for pp in disc.pairs if pp.pair_id in allowed]
            exclusions = [e for e in disc.exclusions if e.get('pair_id') in allowed]
        else:
            disc_pairs = disc.pairs
            exclusions = list(disc.exclusions)
        metadata = load_metadata(root)
        for pp in disc_pairs:
            meta = dict(metadata.get(pp.pair_id, {}))
            try:
                en = extract_file(pp.english)
                ar = extract_file(pp.arabic)
            except ExtractionError as e:
                exclusions.append({'pair_id': pp.pair_id, 'code': e.code, 'reason': str(e)}); continue
            issues = en.issues + ar.issues
            if issues:
                exclusions.append({'pair_id': pp.pair_id, 'code': issues[0], 'reason': 'extraction issues',
                                   'warnings': en.warnings + ar.warnings}); continue
            try:
                ctx, prov = context_from_metadata(meta)
                blocks = split_oversized_blocks(list(en.normalized.blocks), cfg.windows.content_tokens)
                nt = build_normalized_text(blocks)
                windows = build_windows(blocks, cfg.windows.content_tokens, cfg.windows.context_tokens_each_side)
            except GlocalInductError as e:
                exclusions.append({'pair_id': pp.pair_id, 'code': e.code, 'reason': str(e)}); continue
            source = SourceDocument(source_text_id=f'src_{nt.text_hash[:16]}', normalized=nt, windows=windows, context=ctx,
                                    metadata_provenance=prov, raw_hash=en.raw_hash, origin='pair', pair_id=pp.pair_id,
                                    extraction_warnings=en.warnings)
            services.texts.put(nt, 'source')
            services.private.put(ar.normalized, 'reference')
            ar_texts[pp.pair_id] = ar.normalized.text
            rec = PairRecord(pp.pair_id, source, ar.normalized.text_hash, en.raw_hash, ar.raw_hash, meta,
                             meta.get('document_family'), en.warnings + ar.warnings)
            pairs.append(rec)

    # grouping uses both languages offline
    docs = [DocumentForGrouping(p.pair_id, p.family, p.source.text_hash, p.arabic_hash, p.source.normalized.text,
                                ar_texts.get(p.pair_id) or services.private.get(p.arabic_hash).text) for p in pairs]
    grouping = group_documents(docs, cfg.near_duplicate_jaccard)
    for p in pairs:
        p.group_id = grouping.group_of[p.pair_id]
    manifest = {'pairs': [p.pair_id for p in pairs], 'group_of': grouping.group_of, 'links': grouping.links,
                'exclusions': exclusions, 'grouping_version': grouping.version}
    mhash = payload_hash(manifest)
    with services.repo.transaction():
        for p in pairs:
            payload = p.to_dict()
            existing = services.repo.get_object(f'pair_{p.pair_id}')
            if existing is not None:
                if existing['payload_hash'] != payload_hash(payload):
                    raise GlocalInductError(f'pair {p.pair_id} was already ingested with different content; '
                                            'source files are immutable once ingested', code='DUPLICATE_ID')
                continue   # idempotent re-ingest
            services.repo.put_object('pair_record', payload, run_id, record_id=f'pair_{p.pair_id}')
        services.repo.put_object('ingest_manifest', {**manifest, 'manifest_hash': mhash}, run_id)
    out_dir = cfg.path('database').parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'ingest_manifest.json').write_text(json.dumps({**manifest, 'manifest_hash': mhash}, ensure_ascii=False, indent=1), 'utf-8')
    return IngestReport(pairs, exclusions, mhash)


def load_pairs(services: Services, pair_ids: list[str] | None = None) -> list[PairRecord]:
    if pair_ids is not None:
        out: list[PairRecord] = []
        for pid in pair_ids:
            obj = services.repo.get_object(f'pair_{pid}')
            if obj is not None:
                out.append(PairRecord.from_dict(obj['payload']))
        return out
    return [PairRecord.from_dict(o['payload']) for o in services.repo.objects_of_type('pair_record')]


def split_groups(services: Services, run_id: str = 'split', overwrite: bool = False) -> SplitManifest:
    cfg = services.config
    pairs = load_pairs(services)
    if not pairs:
        raise GlocalInductError('no ingested pairs; run ingest first', code='INPUT_INVALID')
    existing = services.repo.objects_of_type('split_manifest')
    if existing and not overwrite:
        prev = SplitManifest.from_dict(existing[-1]['payload'])
        if all(p.pair_id in prev.pair_group for p in pairs):
            return prev
    pair_group = {p.pair_id: p.group_id for p in pairs}
    has_mixed_series = (any(p.pair_id.startswith('0000') for p in pairs)
                        and any(not p.pair_id.startswith('0000') for p in pairs))
    pair_domains = {
        p.pair_id: list(p.source.context.domains) or (
            [f"general_{'0000' if p.pair_id.startswith('0000') else '2000'}"] if has_mixed_series else ['general']
        )
        for p in pairs
    }
    m = _split(pair_group, pair_domains, {'learning': cfg.split.learning, 'development': cfg.split.development,
                                          'test': cfg.split.test}, cfg.split.seed, cfg.seed_fraction_of_learning)
    if cfg.split.seed_pairs is not None or cfg.split.practice_pairs is not None:
        seed_set = set(cfg.split.seed_pairs or [])
        practice_set = set(cfg.split.practice_pairs or [])
        for pid, gid in pair_group.items():
            if pid in seed_set:
                m.partition_of_group[gid] = 'learning'
                m.subset_of_group[gid] = 'seed'
            elif pid in practice_set:
                m.partition_of_group[gid] = 'learning'
                m.subset_of_group[gid] = 'practice'
        groups = sorted(set(pair_group.values()))
        m.counts = {k: sum(1 for g in groups if m.partition_of_group.get(g) == k)
                    for k in ('learning', 'development', 'test')}
        m.counts['seed'] = sum(1 for g in groups if m.partition_of_group.get(g) == 'learning' and m.subset_of_group.get(g) == 'seed')
        m.counts['practice'] = sum(1 for g in groups if m.partition_of_group.get(g) == 'learning' and m.subset_of_group.get(g) == 'practice')
        m.manifest_hash = payload_hash({k: v for k, v in m.to_dict().items() if k != 'manifest_hash'})
    services.repo.put_object('split_manifest', m.to_dict(), run_id)
    (cfg.path('database').parent / 'split_manifest.json').write_text(json.dumps(m.to_dict(), indent=1), 'utf-8')
    return m


def current_split(services: Services) -> SplitManifest:
    objs = services.repo.objects_of_type('split_manifest')
    if objs:
        return SplitManifest.from_dict(objs[-1]['payload'])
    split_file = services.config.path('database').parent / 'split_manifest.json'
    if split_file.is_file():
        data = json.loads(split_file.read_text('utf-8'))
        services.repo.put_object('split_manifest', data, 'split_restore')
        return SplitManifest.from_dict(data)
    if services.repo.objects_of_type('pair_record'):
        return split_groups(services)
    raise GlocalInductError('no split manifest; run split first', code='INPUT_INVALID')
