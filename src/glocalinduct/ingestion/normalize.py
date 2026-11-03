"""Canonical text representation (guide §4 step 2).

Authoritative text is NFC-normalised with LF line endings. Arabic letter forms,
diacritics, punctuation and digits are preserved. A separate matching-only
normalisation exists for duplicate detection and never replaces the authoritative text.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from ..hashing import sha256_text

BLOCK_SEPARATOR = '\n\n'
BlockKind = Literal['heading', 'paragraph', 'list_item', 'table_row', 'caption', 'code', 'quote']


def normalize_text(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = unicodedata.normalize('NFC', text)
    return text


@dataclass(frozen=True)
class Block:
    block_id: str
    kind: BlockKind
    text: str                       # NFC, LF, stripped of surrounding whitespace
    order: int
    level: int = 0                  # heading level or list nesting
    cells: tuple[str, ...] = ()     # table rows keep their cells
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'block_id': self.block_id, 'kind': self.kind, 'text': self.text,
                'order': self.order, 'level': self.level, 'cells': list(self.cells)}


@dataclass(frozen=True)
class NormalizedText:
    """Frozen normalised representation: blocks + concatenated text + hash."""
    blocks: tuple[Block, ...]
    text: str
    text_hash: str
    block_offsets: dict[str, int]   # block_id -> start offset in `text` (code points)

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.block_id == block_id:
                return b
        raise KeyError(block_id)

    def block_ids(self) -> list[str]:
        return [b.block_id for b in self.blocks]


def build_normalized_text(blocks: list[Block]) -> NormalizedText:
    offsets: dict[str, int] = {}
    parts: list[str] = []
    pos = 0
    for i, b in enumerate(blocks):
        if i:
            pos += len(BLOCK_SEPARATOR)
        offsets[b.block_id] = pos
        parts.append(b.text)
        pos += len(b.text)
    text = BLOCK_SEPARATOR.join(parts)
    return NormalizedText(blocks=tuple(blocks), text=text, text_hash=sha256_text(text),
                          block_offsets=offsets)


_DIACRITICS = re.compile(r'[ؐ-ًؚ-ٰٟۖ-ۭـ]')
_PUNCT = re.compile(r'[^\w\s]', re.UNICODE)


def matching_text(text: str) -> str:
    """Matching-only normalisation: case-folded, diacritics and punctuation removed,
    whitespace collapsed. Stored separately; never authoritative."""
    t = unicodedata.normalize('NFKC', text).casefold()
    t = _DIACRITICS.sub('', t)
    t = _PUNCT.sub(' ', t)
    return ' '.join(t.split())
