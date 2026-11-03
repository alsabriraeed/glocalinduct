"""The real pipeline must never silently run on simulated components."""
import pytest

from glocalinduct.config import load_config
from glocalinduct.errors import ConfigError
from glocalinduct.retrieval.embedding import knn_inner_product


def test_entry_points_refuse_simulated_components(monkeypatch):
    monkeypatch.delenv('GLOCALINDUCT_ALLOW_SIMULATION', raising=False)
    monkeypatch.delenv('GLOCALINDUCT_PROVIDER', raising=False)       # env beats overrides; unset -> fake
    cfg = load_config(overrides={'retrieval': {'embedder': 'hashing'}}, root=None)
    assert any('provider' in c for c in cfg.simulated_components())
    assert any('embedder' in c for c in cfg.simulated_components())
    with pytest.raises(ConfigError):
        cfg.assert_real_pipeline()
    monkeypatch.setenv('GLOCALINDUCT_ALLOW_SIMULATION', '1')               # explicit, deliberate demo only
    cfg.assert_real_pipeline()


def test_real_components_pass_the_guard(monkeypatch):
    monkeypatch.delenv('GLOCALINDUCT_ALLOW_SIMULATION', raising=False)
    monkeypatch.setenv('GLOCALINDUCT_PROVIDER', 'openai')
    cfg = load_config(overrides={'runtime': {'provider': 'openai'}}, root=None)
    assert cfg.simulated_components() == []
    cfg.assert_real_pipeline()


def test_faiss_backend_never_falls_back_silently():
    assert knn_inner_product([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]], 'faiss') == pytest.approx([1.0, 0.0])
    with pytest.raises(ValueError):
        knn_inner_product([1.0, 0.0], [[1.0, 0.0, 0.0]], 'faiss')
