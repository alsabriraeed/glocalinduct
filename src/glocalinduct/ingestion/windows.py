"""Owner windows with read-only context (guide §4 step 4, §12).

Each block has exactly one owner window. Windows pack consecutive blocks up to
`content_tokens`; each window carries up to `context_tokens_each_side` of
neighbouring read-only blocks. An oversized paragraph is split at a sentence
boundary *before* the text is frozen (structure survives); any other oversized
block raises CONTEXT_OVERFLOW.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import ContextOverflow
from .normalize import Block

_WORD = re.compile(r'\S+')


def count_tokens(text: str) -> int:
    """Pilot tokenizer: whitespace tokens. Replace through the tokenizer adapter
    in providers when a real tokenizer is configured."""
    return len(_WORD.findall(text))


_SENT = re.compile(r'(?<=[.!?؟。])\s+')


def split_oversized_blocks(blocks: list[Block], max_tokens: int) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        if count_tokens(b.text) <= max_tokens:
            out.append(b)
            continue
        if b.kind not in ('paragraph', 'quote'):
            raise ContextOverflow(f'block {b.block_id} ({b.kind}) exceeds {max_tokens} tokens and '
                                  'cannot be split without losing structure',
                                  details={'block_id': b.block_id})
        sentences = _SENT.split(b.text)
        pieces: list[str] = []
        cur: list[str] = []
        cur_n = 0
        for s in sentences:
            n = count_tokens(s)
            if n > max_tokens:
                raise ContextOverflow(f'sentence in block {b.block_id} exceeds {max_tokens} tokens',
                                      details={'block_id': b.block_id})
            if cur and cur_n + n > max_tokens:
                pieces.append(' '.join(cur)); cur, cur_n = [], 0
            cur.append(s); cur_n += n
        if cur:
            pieces.append(' '.join(cur))
        for k, piece in enumerate(pieces, 1):
            out.append(Block(f'{b.block_id}.{k}', b.kind, piece, len(out), b.level, b.cells,
                             dict(b.meta, split_from=b.block_id)))
    # renumber order
    return [Block(b.block_id, b.kind, b.text, i, b.level, b.cells, b.meta) for i, b in enumerate(out)]


@dataclass(frozen=True)
class Window:
    window_id: str
    owned_block_ids: tuple[str, ...]
    context_before_ids: tuple[str, ...] = ()
    context_after_ids: tuple[str, ...] = ()
    content_tokens: int = 0

    def to_dict(self) -> dict:
        return {'window_id': self.window_id, 'owned_block_ids': list(self.owned_block_ids),
                'context_before_ids': list(self.context_before_ids),
                'context_after_ids': list(self.context_after_ids), 'content_tokens': self.content_tokens}


def build_windows(blocks: list[Block], content_tokens: int = 1200,
                  context_tokens_each_side: int = 200) -> list[Window]:
    for b in blocks:
        if count_tokens(b.text) > content_tokens:
            raise ContextOverflow(f'block {b.block_id} exceeds the window budget',
                                  details={'block_id': b.block_id})
    groups: list[list[Block]] = []
    cur: list[Block] = []
    cur_n = 0
    for b in blocks:
        n = count_tokens(b.text)
        if cur and cur_n + n > content_tokens:
            groups.append(cur); cur, cur_n = [], 0
        cur.append(b); cur_n += n
    if cur:
        groups.append(cur)
    windows: list[Window] = []
    for gi, g in enumerate(groups):
        first, last = g[0].order, g[-1].order
        before: list[str] = []
        n = 0
        for b in reversed(blocks[:first]):
            t = count_tokens(b.text)
            if n + t > context_tokens_each_side:
                break
            before.insert(0, b.block_id); n += t
        after: list[str] = []
        n = 0
        for b in blocks[last + 1:]:
            t = count_tokens(b.text)
            if n + t > context_tokens_each_side:
                break
            after.append(b.block_id); n += t
        windows.append(Window(f'w{gi + 1:03d}', tuple(b.block_id for b in g), tuple(before), tuple(after),
                              sum(count_tokens(b.text) for b in g)))
    return windows
