"""ExpeL-aligned retrieval: exact kNN scoring, raw-source query, all active insights, embedder mismatch."""
import asyncio
from pathlib import Path

import pytest

from glocalinduct.retrieval.embedding import (Embedder, HashingEmbedder, SentenceTransformerEmbedder, cosine,
                                              knn_inner_product, make_embedder)
from glocalinduct.retrieval.search import MemorySearch, index_identity
from glocalinduct.workflows import InferRequest, infer
from glocalinduct.workflows.consolidate import update_insights
from glocalinduct.workflows.ingest import ingest_pairs, split_groups, load_pairs
from glocalinduct.workflows.practice import collect_practice
from glocalinduct.workflows.seed import build_seed_memory

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures' / 'corpus'


def test_knn_inner_product_matches_cosine_and_faiss():
    emb = HashingEmbedder(dimensions=64)
    texts = ['family dinner on thanksgiving', 'water boils at 100 degrees', 'pumpkin pie for the family dinner']
    vecs = emb.embed(texts)
    q = emb.embed(['thanksgiving family dinner'])[0]
    py = knn_inner_product(q, vecs, 'python')
    assert py == pytest.approx([cosine(q, v) for v in vecs])
    assert knn_inner_product(q, [], 'faiss') == []
    pytest.importorskip('faiss'); pytest.importorskip('numpy')
    fa = knn_inner_product(q, vecs, 'faiss')
    assert fa == pytest.approx(py, abs=1e-5)
    assert max(range(3), key=py.__getitem__) == max(range(3), key=fa.__getitem__)


def test_embedder_factory_and_model_name_validation():
    class Cfg:
        embedder = 'hashing'; embedder_model = 'x'
    assert isinstance(make_embedder(Cfg), HashingEmbedder)
    Cfg.embedder = 'sentence-transformers'; Cfg.embedder_model = 'sentence-transformers/all-mpnet-base-v2'
    e = make_embedder(Cfg)                       # lazy: no torch import, no download here
    assert isinstance(e, SentenceTransformerEmbedder) and e.name == 'all-mpnet-base-v2' and e.dimensions == 768
    with pytest.raises(ValueError):
        SentenceTransformerEmbedder('../../etc/passwd')


class _Stub(Embedder):
    name = 'stub-64'
    dimensions = 64

    def __init__(self):
        self.inner = HashingEmbedder(dimensions=64)
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return self.inner.embed(texts)


def _learned(make_services):
    s = make_services()
    ingest_pairs(s, CORPUS); split_groups(s)
    asyncio.run(build_seed_memory(s, 'M0'))
    asyncio.run(collect_practice(s, 'M0'))
    asyncio.run(update_insights(s, 'M0', 'M1'))
    return s


def test_all_active_insights_and_embedder_mismatch_recompute(make_services):
    s = _learned(make_services)
    rel = s.releases.get('M1')
    # release index records the embedder AND the text it embeds
    assert rel.index['embedder'] == index_identity(s.embedder, s.config.retrieval.query_mode)
    out = asyncio.run(infer(InferRequest(text='Lina buys pancakes near Main Street every Saturday for 3 dollars.',
                                         context={'domains': ['education'], 'locale_scope': 'general_arab'}), s, 'M1'))
    profile, source = out.attempt.profile, load_pairs(s)[0].source
    # every active insight of the release is supplied (no similarity cut-off, no top-k)
    active = [(i, v) for i, v in rel.insight_versions if s.insights.version(i, v).status == 'active']
    ms = lambda emb, **kw: MemorySearch(s.experiences, s.insights, s.texts, emb, **kw)
    snap = ms(s.embedder, insight_scope_filter=False).search(profile, source, rel)
    assert len(snap.insights) == len(active) >= 1
    imps = [i.importance for i in snap.insights]
    assert imps == sorted(imps, reverse=True)
    # a different embedder identity than the stored index: vectors are re-embedded in memory, then cached
    stub = _Stub()
    search = ms(stub)
    snap1 = search.search(profile, source, rel)
    first = stub.calls
    assert snap1.embedder == 'stub-64' and first >= 2          # query + batch of demonstrations
    search.search(profile, source, rel)
    assert stub.calls == first + 1                             # only the query is embedded again
    assert len(snap1.experiences) <= 2
