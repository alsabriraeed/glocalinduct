"""Group related documents before splitting (guide §4 step 3).

Union-find over shared document_family, equal normalised hashes and conservative
near-duplicate links (5-token shingles, Jaccard >= threshold on case-folded tokens;
exact match for documents shorter than five tokens).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..hashing import deterministic_id
from .normalize import matching_text


def _tokens(text: str) -> list[str]:
    return matching_text(text).split()


def shingles(text: str, k: int = 5) -> set[str]:
    toks = _tokens(text)
    if len(toks) < k:
        return {' '.join(toks)} if toks else set()
    return {' '.join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def jaccard_shingles(a: str, b: str, k: int = 5) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < k or len(tb) < k:
        return 1.0 if ' '.join(ta) == ' '.join(tb) and ta else 0.0
    sa, sb = shingles(a, k), shingles(b, k)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # deterministic root: smallest id
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


@dataclass
class DocumentForGrouping:
    pair_id: str
    family: str | None
    english_hash: str
    arabic_hash: str | None
    english_text: str
    arabic_text: str | None = None


@dataclass
class GroupingResult:
    group_of: dict[str, str]
    links: list[dict] = field(default_factory=list)
    version: str = 'grouping-1.0'


def group_documents(docs: list[DocumentForGrouping], jaccard_threshold: float = 0.85) -> GroupingResult:
    uf = UnionFind()
    links: list[dict] = []
    docs = sorted(docs, key=lambda d: d.pair_id)
    for d in docs:
        uf.find(d.pair_id)
    by_family: dict[str, list[str]] = {}
    by_en: dict[str, list[str]] = {}
    by_ar: dict[str, list[str]] = {}
    for d in docs:
        if d.family:
            by_family.setdefault(d.family, []).append(d.pair_id)
        by_en.setdefault(d.english_hash, []).append(d.pair_id)
        if d.arabic_hash:
            by_ar.setdefault(d.arabic_hash, []).append(d.pair_id)
    for kind, table in (('family', by_family), ('english_hash', by_en), ('arabic_hash', by_ar)):
        for key, ids in table.items():
            for other in ids[1:]:
                uf.union(ids[0], other)
                links.append({'kind': kind, 'a': ids[0], 'b': other, 'key': key})
    # near duplicates (both languages when available) using an exact inverted shingle index
    from collections import defaultdict
    en_info: list[tuple[bool, set[str], int]] = []
    ar_info: list[tuple[bool, set[str], int]] = []
    inv: dict[tuple[str, bool, str], list[int]] = defaultdict(list)

    for idx, d in enumerate(docs):
        ta = _tokens(d.english_text)
        short_a = len(ta) < 5
        sa = ({' '.join(ta)} if ta else set()) if short_a else {' '.join(ta[m:m + 5]) for m in range(len(ta) - 4)}
        en_info.append((short_a, sa, len(sa)))
        for sh in sa:
            inv[('en', short_a, sh)].append(idx)

        if d.arabic_text:
            tb = _tokens(d.arabic_text)
            short_b = len(tb) < 5
            sb = ({' '.join(tb)} if tb else set()) if short_b else {' '.join(tb[m:m + 5]) for m in range(len(tb) - 4)}
            ar_info.append((short_b, sb, len(sb)))
            for sh in sb:
                inv[('ar', short_b, sh)].append(idx)
        else:
            ar_info.append((True, set(), 0))

    cand_pairs: set[tuple[int, int]] = set()
    for (lang, is_short, _), idxs in inv.items():
        if len(idxs) < 2:
            continue
        info = en_info if lang == 'en' else ar_info
        for a_i in range(len(idxs)):
            i = idxs[a_i]
            li = info[i][2]
            for b_i in range(a_i + 1, len(idxs)):
                j = idxs[b_i]
                lj = info[j][2]
                if is_short or (li >= jaccard_threshold * lj and lj >= jaccard_threshold * li):
                    cand_pairs.add((i, j))

    for i, j in sorted(cand_pairs):
        a, b = docs[i], docs[j]
        if uf.find(a.pair_id) == uf.find(b.pair_id):
            continue
        short_ea, sa_en, len_ea = en_info[i]
        short_eb, sb_en, len_eb = en_info[j]
        if short_ea or short_eb:
            sim_en = 1.0 if (sa_en == sb_en and sa_en) else 0.0
        elif sa_en and sb_en:
            inter = len(sa_en & sb_en)
            sim_en = inter / (len_ea + len_eb - inter)
        else:
            sim_en = 0.0

        short_aa, sa_ar, len_aa = ar_info[i]
        short_ab, sb_ar, len_ab = ar_info[j]
        if not a.arabic_text or not b.arabic_text:
            sim_ar = 0.0
        elif short_aa or short_ab:
            sim_ar = 1.0 if (sa_ar == sb_ar and sa_ar) else 0.0
        elif sa_ar and sb_ar:
            inter_ar = len(sa_ar & sb_ar)
            sim_ar = inter_ar / (len_aa + len_ab - inter_ar)
        else:
            sim_ar = 0.0

        sim = max(sim_en, sim_ar)
        if sim >= jaccard_threshold:
            uf.union(a.pair_id, b.pair_id)
            links.append({'kind': 'near_duplicate', 'a': a.pair_id, 'b': b.pair_id,
                          'similarity': round(sim, 4)})
    group_of = {}
    for d in docs:
        root = uf.find(d.pair_id)
        group_of[d.pair_id] = deterministic_id('grp', root)
    return GroupingResult(group_of=group_of, links=links)
