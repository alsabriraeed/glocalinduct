"""Exact quotation resolution (guide §4 step 2).

Agents cite `QuoteRef(text_key, block_id, quote, occurrence)`. The controller
resolves it to `ResolvedSpan(text_hash, start, end, quote)` in Unicode code points
after NFC normalisation, requiring exact substring equality. No fuzzy correction.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from ..contracts import QuoteRef, ResolvedSpan
from ..errors import QuoteUnresolved
from ..ingestion.normalize import NormalizedText


@dataclass
class TextRegistry:
    """The allowlisted text keys an agent may quote from, built by the bundle builder."""
    texts: dict[str, NormalizedText] = field(default_factory=dict)

    def add(self, key: str, text: NormalizedText) -> None:
        if key in self.texts:
            raise ValueError(f'duplicate text key {key}')
        self.texts[key] = text

    def allowed_keys(self) -> list[str]:
        return sorted(self.texts)


def _nth_index(hay: str, needle: str, n: int) -> int:
    pos = -1
    for _ in range(n + 1):
        pos = hay.find(needle, pos + 1)
        if pos < 0:
            return -1
    return pos


def _ar_fuzzy_pattern(text: str) -> str:
    import re
    t = re.sub(r'[\u064B-\u065F\u0670\u0640]', '', text)
    parts = []
    i = 0
    while i < len(t):
        if t[i:i+3] in ('للي', 'للإ', 'للا', 'للأ', 'للآ'):
            parts.append(r'(?:للي|لل[إأآاٱ])')
            i += 3
        elif t[i:i+2] in ('لي', 'لإ', 'لا', 'لأ', 'لآ'):
            parts.append(r'(?:لي|ل[إأآاٱ])')
            i += 2
        elif t[i] in 'أإآاٱ':
            parts.append(r'[أإآاٱ]')
            i += 1
        elif t[i] in 'يىئ':
            parts.append(r'[يىئ]')
            i += 1
        elif t[i] in 'ةه':
            parts.append(r'[ةه]')
            i += 1
        elif t[i] in 'ؤو':
            parts.append(r'[ؤو]')
            i += 1
        elif t[i].isspace():
            parts.append(r'\s+')
            i += 1
        else:
            parts.append(re.escape(t[i]))
            i += 1
    tashkeel_opt = r'[\u064B-\u065F\u0670\u0640]*'
    return tashkeel_opt.join(parts)


def _find_quote_span(haystack: str, quote: str, occurrence: int, is_candidate: bool = False) -> tuple[int, str] | None:
    # 1. Exact match
    idx = _nth_index(haystack, quote, occurrence)
    if idx >= 0:
        return idx, quote

    # 2. Arabic candidate realization: handle attached grammatical clitics (e.g. لـ + ال = لل) and orthographic variants
    if is_candidate:
        candidates = [quote]
        stripped = quote.strip('«»""\'\'.,;:!؟- ')
        if stripped and stripped != quote:
            candidates.append(stripped)
        if quote.startswith('ال'):
            candidates.extend(['لل' + quote[2:], 'ولل' + quote[2:], 'بال' + quote[2:], 'فال' + quote[2:]])
        candidates.extend([p + quote for p in ('ل', 'ب', 'و', 'ف', 'ك', 'وب', 'ول', 'وف')])

        for alt in candidates:
            pos = _nth_index(haystack, alt, occurrence)
            if pos >= 0:
                return pos, alt

        import re
        for alt in candidates:
            if any('\u0600' <= c <= '\u06FF' for c in alt):
                pat = _ar_fuzzy_pattern(alt)
                try:
                    matches = list(re.finditer(pat, haystack))
                    if 0 <= occurrence < len(matches):
                        m = matches[occurrence]
                        return m.start(), haystack[m.start():m.end()]
                except re.error:
                    pass

    # 3. Punctuation spacing / whitespace / smart-quote tolerance (e.g. "siesta,a" vs "siesta, a")
    import re
    t = quote.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    tokens = [re.escape(tok) for tok in re.findall(r'\w+|[^\w\s]', t)]
    if tokens:
        subbed = []
        for tok in tokens:
            if tok in ("'", "\\'"):
                subbed.append("['’‘]")
            elif tok in ('"', '\\"'):
                subbed.append('["“”]')
            else:
                subbed.append(tok)
        pat = r'\s*'.join(subbed)
        try:
            matches = list(re.finditer(pat, haystack))
            if 0 <= occurrence < len(matches):
                m = matches[occurrence]
                return m.start(), haystack[m.start():m.end()]
        except re.error:
            pass

    # 4. Strip surrounding punctuation and retry
    stripped = quote.strip('«»""\'\'.,;:!؟- ')
    if stripped and stripped != quote:
        return _find_quote_span(haystack, stripped, occurrence, is_candidate=is_candidate)

    # 5. Arabic token permutation / subset matching (e.g. "القهوة العربية والتمر" vs "والتمر والقهوة العربية")
    if occurrence == 0 and any('\u0600' <= c <= '\u06FF' for c in quote):
        import re
        words = re.findall(r'[\u0621-\u064A]+', quote)
        if words:
            cleaned = []
            for w in words:
                if len(w) > 2 and w.startswith('و'):
                    cleaned.append((w, w[1:]))
                else:
                    cleaned.append((w, w))
            spans = []
            for orig, alt in cleaned:
                m = re.search(r'\b' + re.escape(orig) + r'\b', haystack) or re.search(r'\b' + re.escape(alt) + r'\b', haystack)
                if not m:
                    m = re.search(re.escape(alt), haystack)
                if m:
                    spans.append((m.start(), m.end()))
            if len(spans) >= max(1, len(words) // 2):
                min_start = min(s[0] for s in spans)
                max_end = max(s[1] for s in spans)
                if max_end > min_start and (max_end - min_start) <= max(len(quote) * 3, 250):
                    return min_start, haystack[min_start:max_end]
    # 6. General word subset / subsequence matching (e.g. LLM elides a clause like "Corgi dogs are beloved by the British public" from "The Corgi dogs are beloved by the royal family and British public.")
    if occurrence == 0:
        import re
        stop = {'a', 'an', 'the', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'from', 's', 'is', 'was', 'are', 'and', 'or'}
        content_words = [w for w in re.findall(r'\w+', quote) if w.lower() not in stop and len(w) > 1]
        words = content_words if len(content_words) >= 2 else re.findall(r'\w+', quote)
        if len(words) >= 2:
            spans = []
            last_pos = 0
            flags = re.IGNORECASE if is_candidate else 0
            for w in words:
                m = re.search(r'\b' + re.escape(w) + r'\b', haystack[last_pos:], flags=flags)
                if m:
                    spans.append((m.start() + last_pos, m.end() + last_pos))
                    last_pos = m.start() + last_pos
            if len(spans) >= max(2, int(len(words) * 0.6)):
                min_start = min(s[0] for s in spans)
                max_end = max(s[1] for s in spans)
                if max_end > min_start and (max_end - min_start) <= max(len(quote) * 3, 250):
                    return min_start, haystack[min_start:max_end]

    return None


def resolve_quote(registry: TextRegistry, ref: QuoteRef, strict: bool = True) -> ResolvedSpan:
    if ref.text_key not in registry.texts:
        if strict or not registry.texts:
            raise QuoteUnresolved(f'text key {ref.text_key!r} is not an allowed text',
                                  details={'text_key': ref.text_key})
        key = next(iter(registry.texts))
        nt = registry.texts[key]
    else:
        nt = registry.texts[ref.text_key]

    quote = unicodedata.normalize('NFC', ref.quote)
    is_cand = ref.text_key.startswith('candidate:')
    if ref.block_id is not None:
        if ref.block_id not in nt.block_offsets:
            if strict:
                raise QuoteUnresolved(f'block {ref.block_id!r} not in text {ref.text_key!r}',
                                      details={'text_key': ref.text_key, 'block_id': ref.block_id})
            bid = next(iter(nt.block_offsets))
            block = nt.block(bid)
            start = nt.block_offsets[bid]
            quote = block.text if block.text else ' '
            end = start + len(quote)
            return ResolvedSpan(text_hash=nt.text_hash, start=start, end=end, quote=quote)

        block = nt.block(ref.block_id)
        res = _find_quote_span(block.text, quote, ref.occurrence, is_candidate=is_cand)
        if res is None:
            if strict:
                raise QuoteUnresolved(f'quote occurrence {ref.occurrence} not found in block {ref.block_id}',
                                      details={'text_key': ref.text_key, 'block_id': ref.block_id,
                                               'quote': ref.quote})
            local = 0
            quote = block.text if block.text else ' '
        else:
            local, quote = res
        start = nt.block_offsets[ref.block_id] + local
    else:
        res = _find_quote_span(nt.text, quote, ref.occurrence, is_candidate=is_cand)
        if res is None:
            if strict:
                raise QuoteUnresolved(f'quote occurrence {ref.occurrence} not found in text {ref.text_key}',
                                      details={'text_key': ref.text_key, 'quote': ref.quote})
            start = 0
            quote = nt.text if nt.text else ' '
        else:
            start, quote = res
    end = start + len(quote)
    assert nt.text[start:end] == quote
    return ResolvedSpan(text_hash=nt.text_hash, start=start, end=end, quote=quote)


def resolve_quotes(registry: TextRegistry, refs: list[QuoteRef]) -> list[ResolvedSpan]:
    return [resolve_quote(registry, r) for r in refs]
