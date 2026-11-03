"""Embedding adapters (guide §10).

`HashingEmbedder` is a deterministic, dependency-free local embedder (feature
hashing of word uni/bigrams, L2-normalised). It needs no network and is the
pilot default. A provider embedding model can replace it behind the same
interface; the release records embedder identity, dimensions and normalisation.
`SentenceTransformerEmbedder` is the ExpeL embedder (`all-mpnet-base-v2`); kNN
scoring can run through FAISS `IndexFlatIP` (see `knn_inner_product`).
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading

from ..ingestion.normalize import matching_text

_TOKEN = re.compile(r'\w+', re.UNICODE)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)); db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


class Embedder:
    name = 'base'
    dimensions = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def identity(self) -> dict:
        return {'embedder': self.name, 'dimensions': self.dimensions, 'normalization': 'l2'}


class HashingEmbedder(Embedder):
    name = 'hashing-unibigram-v1'

    def __init__(self, dimensions: int = 512):
        self.dimensions = dimensions

    def _features(self, text: str) -> list[str]:
        toks = _TOKEN.findall(matching_text(text))
        feats = list(toks)
        feats += [f'{a}_{b}' for a, b in zip(toks, toks[1:])]
        return feats

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for f in self._features(text):
            h = hashlib.blake2b(f.encode('utf-8'), digest_size=8).digest()
            idx = int.from_bytes(h[:4], 'big') % self.dimensions
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


_MODEL_NAME = re.compile(r'^[A-Za-z0-9][\w.\-]*(/[\w.\-]+)?$')
_DEVICE = re.compile(r'^(cpu|cuda(:\d+)?)$')
_KNOWN_DIMENSIONS = {'all-mpnet-base-v2': 768, 'all-MiniLM-L6-v2': 384}
_log = logging.getLogger(__name__)


class SentenceTransformerEmbedder(Embedder):
    """Semantic embedder used by ExpeL (`all-mpnet-base-v2`), L2-normalised.

    The model is loaded lazily on first use so that importing the package, building
    services and running offline tests never needs torch or the network.
    `device='auto'` uses CUDA when torch can see a GPU, otherwise CPU (with a warning).
    `trust_remote_code` is never enabled.
    """

    def __init__(self, model: str = 'sentence-transformers/all-mpnet-base-v2', device: str = 'auto'):
        if not _MODEL_NAME.match(model):
            raise ValueError('invalid embedding model name')
        if device != 'auto' and not _DEVICE.match(device):
            raise ValueError('invalid embedding device')
        self.model_name = model
        self.name = model.rsplit('/', 1)[-1]
        self.requested_device = device
        self.device: str | None = None          # resolved on first load
        self._model = None
        self._lock = threading.Lock()
        self._dimensions = _KNOWN_DIMENSIONS.get(self.name, 0)

    @property
    def dimensions(self) -> int:       # type: ignore[override]
        if not self._dimensions:
            self._dimensions = int(self._load().get_sentence_embedding_dimension())
        return self._dimensions

    def _resolve_device(self) -> str:
        import torch
        want = self.requested_device
        if want == 'auto':
            if torch.cuda.is_available():
                return 'cuda'
            _log.warning('retrieval.embedder_device=auto but no CUDA device is visible; embedding on CPU')
            return 'cpu'
        if want.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError(f'embedder_device={want} requested but no CUDA device is visible to torch')
        return want

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as e:   # pragma: no cover - depends on the environment
                    raise RuntimeError(
                        'sentence-transformers is not installed; install the "semantic" extra or set '
                        'retrieval.embedder: hashing') from e
                self.device = self._resolve_device()
                self._model = SentenceTransformer(self.model_name, device=self.device)
                _log.info('embedder %s loaded on %s', self.name, self.device)
            return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._load().encode(list(texts), normalize_embeddings=True, convert_to_numpy=True,
                                   show_progress_bar=False)
        return [[float(x) for x in v] for v in vecs]


def make_embedder(retrieval) -> Embedder:
    """Build the embedder selected by `RetrievalConfig.embedder`."""
    if retrieval.embedder == 'sentence-transformers':
        return SentenceTransformerEmbedder(retrieval.embedder_model, getattr(retrieval, 'embedder_device', 'auto'))
    return HashingEmbedder()


def knn_inner_product(query: list[float], vectors: list[list[float]], backend: str = 'python') -> list[float]:
    """Exact inner-product scores of `query` against every vector (cosine on L2-normalised input).

    `backend='faiss'` uses `faiss.IndexFlatIP` as in ExpeL (exact kNN with k = all candidates, so the caller
    can apply its own deterministic tie-breaking). It never degrades silently: missing faiss/numpy or
    mismatched dimensions raise instead of falling back to the pure-python path.
    """
    if not vectors:
        return []
    if backend == 'faiss':
        if any(len(v) != len(query) for v in vectors):
            raise ValueError('faiss kNN: query and stored vectors have different dimensions')
        try:
            import faiss
            import numpy as np
        except ImportError as e:
            raise RuntimeError('retrieval.knn_backend=faiss but faiss/numpy are not installed') from e
        index = faiss.IndexFlatIP(len(query))
        index.add(np.asarray(vectors, dtype='float32'))
        scores, ids = index.search(np.asarray([query], dtype='float32'), len(vectors))
        out = [0.0] * len(vectors)
        for s, i in zip(scores[0], ids[0]):
            if i >= 0:
                out[int(i)] = float(s)
        return out
    return [cosine(query, v) for v in vectors]
