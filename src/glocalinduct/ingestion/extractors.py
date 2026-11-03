"""Format adapters: UTF-8 TXT / Markdown, DOCX, text-bearing PDF → typed blocks.

Markdown is content: embedded instructions never execute. Table rows keep their
cells. Image-only PDFs return OCR_REQUIRED. Nothing is silently dropped: anything
the adapter cannot represent is reported as a warning (guide §4 step 2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ExtractionError
from ..hashing import sha256_bytes
from .normalize import Block, NormalizedText, build_normalized_text, normalize_text

SUPPORTED_EXTENSIONS = {'.txt', '.md', '.markdown', '.docx', '.pdf'}


@dataclass
class ExtractionResult:
    normalized: NormalizedText
    raw_hash: str
    format: str
    warnings: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)   # issue codes requiring exclusion


# ----------------------------------------------------------------- Markdown/TXT
_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_LIST = re.compile(r'^(\s*)([-*+]|\d+[.)])\s+(.*)$')
_TABLE_SEP = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')
_FENCE = re.compile(r'^\s*(```|~~~)')


def extract_text(text: str, prefix: str = 'b') -> list[Block]:
    """Parse plain text or Markdown into typed blocks. Deterministic block IDs."""
    text = normalize_text(text)
    lines = text.split('\n')
    blocks: list[Block] = []
    para: list[str] = []
    i = 0
    n = 0

    def flush_para():
        nonlocal para, n
        if para:
            body = ' '.join(l.strip() for l in para).strip()
            if body:
                n += 1
                blocks.append(Block(f'{prefix}{n:04d}', 'paragraph', body, len(blocks)))
            para = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para(); i += 1; continue
        if _FENCE.match(line):
            flush_para()
            fence = _FENCE.match(line).group(1)
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i]); i += 1
            i += 1  # closing fence
            n += 1
            blocks.append(Block(f'{prefix}{n:04d}', 'code', '\n'.join(code).strip('\n'), len(blocks)))
            continue
        m = _HEADING.match(stripped)
        if m:
            flush_para(); n += 1
            blocks.append(Block(f'{prefix}{n:04d}', 'heading', m.group(2).strip(), len(blocks),
                                level=len(m.group(1))))
            i += 1; continue
        m = _LIST.match(line)
        if m:
            flush_para(); n += 1
            level = len(m.group(1).replace('\t', '    ')) // 2
            blocks.append(Block(f'{prefix}{n:04d}', 'list_item', m.group(3).strip(), len(blocks),
                                level=level, meta={'marker': m.group(2)}))
            i += 1; continue
        if stripped.startswith('|') and stripped.endswith('|') and i + 1 < len(lines) \
                and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            header = [c.strip() for c in stripped.strip('|').split('|')]
            n += 1
            blocks.append(Block(f'{prefix}{n:04d}', 'table_row', ' | '.join(header), len(blocks),
                                cells=tuple(header), meta={'header': True}))
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                n += 1
                blocks.append(Block(f'{prefix}{n:04d}', 'table_row', ' | '.join(cells), len(blocks),
                                    cells=tuple(cells)))
                i += 1
            continue
        if stripped.startswith('>'):
            flush_para(); n += 1
            blocks.append(Block(f'{prefix}{n:04d}', 'quote', stripped.lstrip('> ').strip(), len(blocks)))
            i += 1; continue
        para.append(line)
        i += 1
    flush_para()
    return blocks


# --------------------------------------------------------------------- DOCX
def _extract_docx(path: Path) -> tuple[list[Block], list[str]]:
    try:
        import docx  # python-docx
    except ImportError as e:  # pragma: no cover
        raise ExtractionError('python-docx is not installed') from e
    doc = docx.Document(str(path))
    blocks: list[Block] = []
    warnings: list[str] = []
    n = 0
    body = doc.element.body
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            p = Paragraph(child, doc)
            text = normalize_text(p.text).strip()
            if not text:
                continue
            style = (p.style.name or '').lower() if p.style is not None else ''
            n += 1
            if style.startswith('heading'):
                lvl = int(re.sub(r'\D', '', style) or 1)
                blocks.append(Block(f'b{n:04d}', 'heading', text, len(blocks), level=lvl))
            elif 'list' in style:
                blocks.append(Block(f'b{n:04d}', 'list_item', text, len(blocks)))
            elif 'caption' in style:
                blocks.append(Block(f'b{n:04d}', 'caption', text, len(blocks)))
            else:
                blocks.append(Block(f'b{n:04d}', 'paragraph', text, len(blocks)))
        elif tag == 'tbl':
            t = Table(child, doc)
            for r_i, row in enumerate(t.rows):
                cells = tuple(normalize_text(c.text).strip() for c in row.cells)
                if not any(cells):
                    continue
                n += 1
                blocks.append(Block(f'b{n:04d}', 'table_row', ' | '.join(cells), len(blocks),
                                    cells=cells, meta={'header': r_i == 0}))
        else:
            warnings.append(f'unsupported docx element skipped: {tag}')
    if doc.inline_shapes:
        warnings.append(f'{len(doc.inline_shapes)} inline image(s) not represented')
    return blocks, warnings


# ---------------------------------------------------------------------- PDF
def _extract_pdf(path: Path) -> tuple[list[Block], list[str], list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise ExtractionError('pypdf is not installed') from e
    reader = PdfReader(str(path))
    texts = []
    for page in reader.pages:
        texts.append(page.extract_text() or '')
    joined = '\n\n'.join(texts)
    if not joined.strip():
        return [], ['no extractable text layer'], ['OCR_REQUIRED']
    warnings = ['PDF reading order and table structure are heuristic; verify equations/tables']
    return extract_text(joined), warnings, []


# --------------------------------------------------------------------- entry
def extract_file(path: str | Path) -> ExtractionResult:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f'unsupported extension {ext}', details={'path': str(path)})
    raw = path.read_bytes()
    raw_hash = sha256_bytes(raw)
    warnings: list[str] = []
    issues: list[str] = []
    if ext in {'.txt', '.md', '.markdown'}:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as e:
            raise ExtractionError('file is not valid UTF-8', details={'path': str(path)}) from e
        blocks = extract_text(text)
        fmt = 'markdown' if ext != '.txt' else 'text'
    elif ext == '.docx':
        blocks, warnings = _extract_docx(path)
        fmt = 'docx'
    else:
        blocks, warnings, issues = _extract_pdf(path)
        fmt = 'pdf'
    if not blocks and not issues:
        issues.append('EXTRACTION_FAILED')
        warnings.append('no content blocks extracted')
    return ExtractionResult(normalized=build_normalized_text(blocks), raw_hash=raw_hash,
                            format=fmt, warnings=warnings, issues=issues)
