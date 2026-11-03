"""Exact string-ID pairing of English/Arabic files (guide §4 step 1).

Titles may differ; the ID before `__` is authoritative. Case and leading zeros are
preserved. Duplicate IDs, missing partners, unknown extensions and paths outside
the data root are explicit exclusions. No fuzzy or positional matching.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import GlocalInductError
from .extractors import SUPPORTED_EXTENSIONS

DEFAULT_PATTERN = r'^(?P<pair_id>[A-Za-z0-9][A-Za-z0-9-]*)(?:__(?P<title>.+))?$'
_CSI_OPEN = re.compile(r'<CSI>\s*')
_CSI_CLOSE = re.compile(r'\s*</CSI>')


def strip_csi_tags(text: str) -> str:
    """Remove inline <CSI> ... </CSI> tags if present in dataset cells."""
    if '<CSI>' not in text and '</CSI>' not in text:
        return text
    return _CSI_OPEN.sub('', _CSI_CLOSE.sub('', text))


def load_xlsx_pairs(
    xlsx_path: str | Path,
    english_col: str = 'original',
    arabic_col: str = 'glocalized',
) -> tuple[list[tuple[str, str, str]], list[dict]]:
    """Load (pair_id, english_text, arabic_text) rows from an Excel (.xlsx) workbook.

    Columns are matched case-insensitively by header name (`original` for English and
    `glocalized` for Arabic by default), falling back to columns A and B.
    """
    path = Path(xlsx_path)
    if not path.is_file():
        raise GlocalInductError(f'xlsx dataset file not found: {path}', code='INPUT_INVALID')

    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    try:
        with zipfile.ZipFile(path) as z:
            strings: list[str] = []
            if 'xl/sharedStrings.xml' in z.namelist():
                ss_root = ET.fromstring(z.read('xl/sharedStrings.xml'))
                strings = [
                    ''.join(t.text or '' for t in si.findall('.//s:t', ns))
                    for si in ss_root.findall('s:si', ns)
                ]
            sheet_names = sorted(n for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))
            if not sheet_names:
                raise GlocalInductError(f'no worksheet found in {path}', code='EXTRACTION_FAILED')
            sheet = ET.fromstring(z.read(sheet_names[0]))
    except zipfile.BadZipFile as e:
        raise GlocalInductError(f'invalid xlsx archive {path}: {e}', code='EXTRACTION_FAILED') from e

    rows = sheet.findall('.//s:row', ns)
    if not rows:
        return [], []

    def _row_cells(row_el) -> dict[str, str]:
        vals: dict[str, str] = {}
        for c in row_el.findall('s:c', ns):
            ref = c.attrib.get('r', '')
            col = ''.join(ch for ch in ref if ch.isalpha())
            t = c.attrib.get('t')
            is_el = c.find('s:is', ns)
            v = c.find('s:v', ns)
            if t == 's' and v is not None and v.text is not None:
                idx = int(v.text)
                vals[col] = strings[idx] if 0 <= idx < len(strings) else ''
            elif is_el is not None:
                vals[col] = ''.join(x.text or '' for x in is_el.findall('.//s:t', ns))
            elif v is not None and v.text is not None:
                vals[col] = v.text
            else:
                vals[col] = ''
        return vals

    header = _row_cells(rows[0])
    en_col_letter = 'A'
    ar_col_letter = 'B'
    id_col_letter: str | None = None
    for col, name in header.items():
        norm = name.strip().lower()
        if norm in (english_col.lower(), 'english', 'source', 'en'):
            en_col_letter = col
        elif norm in (arabic_col.lower(), 'arabic', 'target', 'ar'):
            ar_col_letter = col
        elif norm in ('pair_id', 'id'):
            id_col_letter = col

    pairs: list[tuple[str, str, str]] = []
    exclusions: list[dict] = []
    seen_ids: set[str] = set()

    for idx, r in enumerate(rows[1:], 1):
        vals = _row_cells(r)
        raw_id = vals.get(id_col_letter, '').strip() if id_col_letter else ''
        pid = raw_id if raw_id else f'{idx:06d}'
        if pid in seen_ids:
            exclusions.append({'pair_id': pid, 'code': 'DUPLICATE_ID', 'reason': f'duplicate row ID {pid}'})
            continue
        seen_ids.add(pid)
        en_text = strip_csi_tags(vals.get(en_col_letter, '')).strip()
        ar_text = strip_csi_tags(vals.get(ar_col_letter, '')).strip()
        if not en_text or not ar_text:
            exclusions.append({
                'pair_id': pid,
                'code': 'EXTRACTION_FAILED',
                'reason': 'empty original or glocalized text in xlsx row',
            })
            continue
        pairs.append((pid, en_text, ar_text))

    return pairs, exclusions


@dataclass(frozen=True)
class PairPaths:
    pair_id: str
    english: Path
    arabic: Path
    english_title: str | None = None
    arabic_title: str | None = None


@dataclass
class DiscoveryReport:
    pairs: list[PairPaths]
    exclusions: list[dict] = field(default_factory=list)


def parse_pair_filename(path: Path, pattern: str = DEFAULT_PATTERN) -> tuple[str, str | None] | None:
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    m = re.match(pattern, stem)
    if not m:
        return None
    return m.group('pair_id'), (m.groupdict().get('title') or None)


def _scan(folder: Path, data_root: Path, pattern: str, side: str, exclusions: list[dict]):
    found: dict[str, tuple[Path, str | None]] = {}
    if not folder.is_dir():
        raise GlocalInductError(f'{side} folder missing: {folder}', code='INPUT_INVALID')
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        try:
            p.resolve().relative_to(data_root.resolve())
        except ValueError:
            exclusions.append({'path': str(p), 'side': side, 'code': 'PAIR_ID_INVALID',
                               'reason': 'path outside data root'})
            continue
        if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            exclusions.append({'path': str(p), 'side': side, 'code': 'EXTRACTION_FAILED',
                               'reason': f'unknown extension {p.suffix}'})
            continue
        parsed = parse_pair_filename(p, pattern)
        if parsed is None:
            exclusions.append({'path': str(p), 'side': side, 'code': 'PAIR_ID_INVALID',
                               'reason': 'filename does not match pair_id pattern'})
            continue
        pid, title = parsed
        if pid in found:
            exclusions.append({'path': str(p), 'side': side, 'code': 'DUPLICATE_ID', 'pair_id': pid,
                               'reason': f'duplicate of {found[pid][0].name}'})
            found[pid] = (found[pid][0], found[pid][1])
            found.setdefault('__dup__' + pid, (p, title))
            continue
        found[pid] = (p, title)
    # drop ids that had duplicates entirely (ambiguous)
    dups = {k[len('__dup__'):] for k in found if k.startswith('__dup__')}
    for d in dups:
        first = found.pop(d)
        exclusions.append({'path': str(first[0]), 'side': side, 'code': 'DUPLICATE_ID', 'pair_id': d,
                           'reason': 'ambiguous duplicate ID'})
    return {k: v for k, v in found.items() if not k.startswith('__dup__')}


def discover_pairs(data_root: str | Path, pattern: str = DEFAULT_PATTERN) -> DiscoveryReport:
    data_root = Path(data_root)
    exclusions: list[dict] = []
    en = _scan(data_root / 'english', data_root, pattern, 'english', exclusions)
    ar = _scan(data_root / 'arabic', data_root, pattern, 'arabic', exclusions)
    pairs: list[PairPaths] = []
    for pid in sorted(set(en) | set(ar)):
        if pid in en and pid in ar:
            pairs.append(PairPaths(pid, en[pid][0], ar[pid][0], en[pid][1], ar[pid][1]))
        else:
            side = 'english' if pid in en else 'arabic'
            path = (en.get(pid) or ar.get(pid))[0]
            exclusions.append({'path': str(path), 'side': side, 'code': 'MISSING_PARTNER',
                               'pair_id': pid, 'reason': 'no partner file with the same ID'})
    pairs.sort(key=lambda p: p.pair_id)
    return DiscoveryReport(pairs=pairs, exclusions=exclusions)


def load_metadata(data_root: str | Path) -> dict[str, dict]:
    """One JSON object per pair_id; duplicate rows are errors. Missing file → {}."""
    path = Path(data_root) / 'metadata.jsonl'
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for ln, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise GlocalInductError(f'metadata.jsonl line {ln} invalid JSON: {e}', code='INPUT_INVALID')
        pid = row.get('pair_id')
        if not isinstance(pid, str) or not pid:
            raise GlocalInductError(f'metadata.jsonl line {ln}: missing pair_id', code='PAIR_ID_INVALID')
        if pid in out:
            raise GlocalInductError(f'metadata.jsonl duplicate pair_id {pid}', code='DUPLICATE_ID')
        out[pid] = row
    return out
