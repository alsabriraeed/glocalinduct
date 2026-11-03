"""Source-side memory retrieval (guide §10), aligned with ExpeL.

Demonstrations: the query is the raw source text (`query_mode: source_text`, as ExpeL
embeds the task text; `profile` keeps the legacy profile-derived query). Gates (release
membership, eligibility, excluded groups, locale/setting scope) run before ranking; an
exact inner-product kNN over L2-normalised vectors (FAISS `IndexFlatIP` when available)
ranks the remaining candidates, distinct groups are preferred and the top-k are kept.
Insights: every active insight of the release is supplied to the planner (ExpeL
concatenates the full rule set), ordered by importance; only the culture-specific
locale scope gate may remove one. The prompt budget drops the
lowest-ranked examples first, then the lowest-importance insights. Empty retrieval is valid.
"""
from __future__ import annotations

from typing import Any

from ..hashing import new_id, payload_hash, deterministic_id
from ..ingestion.grouping import jaccard_shingles
from ..ingestion.windows import count_tokens
from ..memory.experience import ExperienceMemory
from ..memory.insights import InsightMemory
from ..memory.releases import Release
from ..memory.store import TextStore
from ..records import RetrievalSnapshot, RetrievedExperience, RetrievedInsight, SourceDocument, SourceProfile
from .embedding import Embedder, knn_inner_product
from .filters import experience_compatible, insight_compatible


def experience_text_for_embedding(view, query_mode: str = 'profile') -> str:
    r = view.retrieval
    if query_mode == 'source_text' and r.get('source_excerpt'):
        return r['source_excerpt']          # same kind of text as the query: the English source
    parts = [r.get('summary', ''), ' '.join(r.get('domains', [])), r.get('genre') or '',
             ' '.join(r.get('functions', [])), r.get('source_excerpt', '')]
    return ' '.join(p for p in parts if p)


def insight_text_for_embedding(content: dict) -> str:
    return ' '.join([content['lesson'], ' '.join(content['applies_when']), ' '.join(content['avoid_when']),
                     ' '.join(content['scope'].get('domains', []))])


def index_identity(embedder: Embedder, query_mode: str) -> dict[str, Any]:
    """What a stored vector depends on: embedder, dimensions, normalisation and the text it embeds."""
    return {**embedder.identity(), 'text': query_mode}


def build_index(experiences: ExperienceMemory, insights: InsightMemory, demonstration_ids: list[str],
                insight_versions: list[tuple[str, int]], embedder: Embedder,
                query_mode: str = 'profile', reuse: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a release index. `reuse` is the index of a previous release: its vectors are copied for experiences
    and insight versions it already embeds (both are immutable, so the vector cannot change) when it was built with
    the same embedder identity, so only new items are embedded (keeps per-sample publication cheap)."""
    ident = index_identity(embedder, query_mode)
    old_exp: dict[str, list[float]] = {}
    old_ins: dict[tuple[str, int], list[float]] = {}
    if reuse and reuse.get('embedder') == ident:
        old_exp = {e['experience_id']: e['vector'] for e in reuse.get('experiences', []) if e.get('vector')}
        old_ins = {(i['insight_id'], i['version']): i['vector'] for i in reuse.get('insights', []) if i.get('vector')}
    exp_entries = []
    for eid in sorted(demonstration_ids):
        if eid in old_exp:
            exp_entries.append({'experience_id': eid, 'vector': old_exp[eid]})
            continue
        v = experiences.get(eid)
        text = experience_text_for_embedding(v, query_mode)
        exp_entries.append({'experience_id': eid, 'vector': embedder.embed_one(text)
                            if hasattr(embedder, 'embed_one') else embedder.embed([text])[0]})
    ins_entries = []
    for iid, ver in sorted(insight_versions):
        if (iid, ver) in old_ins:
            ins_entries.append({'insight_id': iid, 'version': ver, 'vector': old_ins[(iid, ver)]})
            continue
        rec = insights.version(iid, ver)
        text = insight_text_for_embedding(rec.content.model_dump(mode='json'))
        ins_entries.append({'insight_id': iid, 'version': ver, 'vector': embedder.embed([text])[0]})
    return {'embedder': ident, 'experiences': exp_entries, 'insights': ins_entries}


class MemorySearch:
    def __init__(self, experiences: ExperienceMemory, insights: InsightMemory, texts: TextStore, embedder: Embedder,
                 max_insights: int = 0, max_experiences: int = 2, candidate_pool: int = 30,
                 similarity_threshold: float | None = None, near_duplicate_jaccard: float = 0.85,
                 query_mode: str = 'source_text', knn_backend: str = 'python', insight_scope_filter: bool = True):
        self.experiences = experiences
        self.insights = insights
        self.texts = texts
        self.embedder = embedder
        self.max_insights = max_insights        # 0/None = all active insights
        self.max_experiences = max_experiences
        self.candidate_pool = candidate_pool
        self.threshold = similarity_threshold
        self.near_dup = near_duplicate_jaccard
        self.query_mode = query_mode
        self.knn_backend = knn_backend
        self.insight_scope_filter = insight_scope_filter
        self._vector_cache: dict[tuple[str, str], list[float]] = {}

    # ----------------------------------------------------------- query text
    def query_text(self, profile: SourceProfile, source: SourceDocument) -> str:
        if self.query_mode == 'source_text':
            return source.normalized.text
        p = profile.payload
        parts = [p.purpose, ' '.join(t.domain for t in p.domains), p.genre or '', p.audience or '',
                 ' '.join(i.semantic_function for i in p.items), ' '.join(i.category for i in p.items),
                 ' '.join(source.context.domains), source.context.setting or '']
        return ' '.join(x for x in parts if x)

    def _demonstration_vectors(self, release: Release, views: dict[str, Any]) -> dict[str, list[float]]:
        """Vectors for the gated candidates.

        Stored release-index vectors are used only when they were built with the current embedder
        identity and text mode; otherwise (e.g. an index published with the hashing embedder) the
        vectors are re-embedded in memory, cached per embedder identity, without touching the release.
        """
        ident = index_identity(self.embedder, self.query_mode)
        key = payload_hash(ident)
        stored = ({e['experience_id']: e['vector'] for e in release.index.get('experiences', [])}
                  if release.index.get('embedder') == ident else {})
        out: dict[str, list[float]] = {}
        missing: list[str] = []
        for eid in views:
            if stored.get(eid):
                out[eid] = stored[eid]
            elif (key, eid) in self._vector_cache:
                out[eid] = self._vector_cache[(key, eid)]
            else:
                missing.append(eid)
        if missing:
            vecs = self.embedder.embed([experience_text_for_embedding(views[e], self.query_mode) for e in missing])
            for eid, vec in zip(missing, vecs):
                self._vector_cache[(key, eid)] = out[eid] = vec
        return out

    def query_context(self, profile: SourceProfile, source: SourceDocument) -> dict:
        c = source.context
        return {'domains': sorted({*c.domains, *[t.domain for t in profile.payload.domains]} - {'general'}),
                'genre': profile.payload.genre or c.genre, 'locale_scope': c.locale_scope, 'locales': list(c.locales),
                'setting': c.setting, 'time_period': c.time_period}

    # ------------------------------------------------------- leakage guard
    def inferred_exclusions(self, source: SourceDocument, release: Release) -> tuple[list[str], list[dict]]:
        """Data leakage guard removed as requested: allow retrieving matching corpus demonstrations."""
        return [], []

    # --------------------------------------------------------------- search
    def search(self, profile: SourceProfile, source: SourceDocument, release: Release,
               excluded_groups: list[str] | None = None, token_budget: int | None = None) -> RetrievalSnapshot:
        excluded = set(excluded_groups or [])
        inferred, decisions = self.inferred_exclusions(source, release)
        excluded |= set(inferred)
        qtext = self.query_text(profile, source)
        qctx = self.query_context(profile, source)
        qvec = self.embedder.embed([qtext])[0]
        filters: list[dict] = list(decisions)
        # ---- experiences: gate first, then exact kNN (inner product on L2-normalised vectors)
        kind_rank = {}
        candidates: list[tuple[bool, str]] = []
        views: dict[str, Any] = {}
        for eid in release.demonstration_ids:
            v = self.experiences.get(eid)
            if v is None or not v.eligible:
                filters.append({'experience_id': eid, 'decision': 'exclude', 'reason': 'not eligible'}); continue
            if v.group_id in excluded:
                filters.append({'experience_id': eid, 'decision': 'exclude', 'reason': f'excluded group {v.group_id}'})
                continue
            ok, why, same = experience_compatible(v.retrieval.get('scope', {}), v.retrieval.get('domains', []), qctx)
            if not ok:
                filters.append({'experience_id': eid, 'decision': 'exclude', 'reason': why}); continue
            candidates.append((same, eid))
            views[eid] = v
        vectors = self._demonstration_vectors(release, views)
        sims = knn_inner_product(qvec, [vectors[eid] for _, eid in candidates], self.knn_backend)
        exp_scores: list[tuple[float, bool, str]] = []
        for (same, eid), sim in zip(candidates, sims):
            if self.threshold is not None and sim < self.threshold:
                filters.append({'experience_id': eid, 'decision': 'exclude', 'reason': f'below threshold {sim:.3f}'})
                continue
            exp_scores.append((sim, same, eid))
            kind_rank[eid] = (0 if views[eid].kind == 'reference_example' else 1, views[eid].group_id)
        # similarity first, then reference observations before attempts, then stable IDs
        exp_scores.sort(key=lambda t: (-round(t[0], 6), kind_rank[t[2]], t[2]))
        pool = exp_scores[: self.candidate_pool]
        chosen: list[tuple[float, bool, str]] = []
        seen_groups: set[str] = set()
        for sim, same, eid in pool:          # prefer distinct groups
            g = self.experiences.get(eid).group_id
            if g in seen_groups:
                continue
            chosen.append((sim, same, eid)); seen_groups.add(g)
            if len(chosen) >= self.max_experiences:
                break
        if len(chosen) < self.max_experiences:
            for t in pool:
                if t not in chosen and len(chosen) < self.max_experiences:
                    chosen.append(t)
        experiences = []
        for sim, same, eid in chosen:
            v = self.experiences.get(eid)
            r = v.retrieval
            experiences.append(RetrievedExperience(
                experience_id=eid, group_id=v.group_id, kind=v.kind, similarity=round(sim, 4), scope=r.get('scope', {}),
                source_excerpt=r.get('source_excerpt', ''), arabic_text=self.texts.get(r['arabic_text_hash']),
                source_function=r.get('summary', ''), transformation=r.get('transformation', ''),
                preserved=r.get('preserved', []), domains=r.get('domains', [])))
        # ---- insights: ExpeL supplies the whole rule set; ranking is by importance, not similarity
        ins_scores: list[tuple[int, str, int]] = []
        for iid, ver in release.insight_versions:
            rec = self.insights.version(iid, ver)
            if rec is None or rec.status != 'active':
                filters.append({'insight_id': iid, 'decision': 'exclude', 'reason': 'not active in release'}); continue
            if self.insight_scope_filter:
                ok, why, _ = insight_compatible(rec.content.model_dump(mode='json')['scope'], qctx)
                if not ok:
                    filters.append({'insight_id': iid, 'decision': 'exclude', 'reason': why}); continue
            ins_scores.append((rec.importance, iid, ver))
        ins_scores.sort(key=lambda t: (-t[0], t[1], t[2]))
        limit = self.max_insights or None          # 0/None = all active insights
        insights = []
        for imp, iid, ver in ins_scores[:limit]:
            rec = self.insights.version(iid, ver)
            insights.append(RetrievedInsight(iid, ver, imp, 0.0, rec.content.model_dump(mode='json')))
        # ---- budget: drop lowest-ranked examples first, then optional insights
        truncation: list[str] = []
        if token_budget is not None:
            def cost():
                return sum(count_tokens(e.arabic_text.text) + count_tokens(e.source_excerpt) for e in experiences) + \
                       sum(count_tokens(i.content['lesson']) + 30 for i in insights)
            while experiences and cost() > token_budget:
                dropped = experiences.pop()
                truncation.append(f'dropped experience {dropped.experience_id} for budget')
            while insights and cost() > token_budget:
                dropped = insights.pop()
                truncation.append(f'dropped insight {dropped.insight_id} for budget')
        query_hash = payload_hash({'q': qtext, 'ctx': qctx})
        selection = [e.experience_id for e in experiences] + [f'{i.insight_id}:{i.version}' for i in insights]
        return RetrievalSnapshot(retrieval_id=deterministic_id('retr', release.release_hash, query_hash, *selection),
                                 release_id=release.release_id,
                                 release_hash=release.release_hash, query_hash=query_hash,
                                 experiences=experiences, insights=insights, excluded_group_ids=sorted(excluded),
                                 filter_decisions=filters, token_budget=token_budget, truncation=truncation,
                                 embedder=self.embedder.name)
