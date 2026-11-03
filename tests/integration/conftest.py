import pytest

from glocalinduct.config import load_config
from glocalinduct.providers.fake import FakeProvider
from glocalinduct.services import build_services

LEGACY_LEARNING = {'reference_demonstrations': True, 'success_signal': 'a5_and_comparison',
                   'insight_admission': 'gated', 'reflection': 'findings', 'pair_every_failure': False,
                   'insight_substitution': False}
EXPEL_LEARNING = {'reference_demonstrations': False, 'success_signal': 'reference_comparison',
                  'insight_admission': 'immediate', 'reflection': 'free_text', 'pair_every_failure': True,
                  'insight_substitution': True}

TEXT = ("# Family dinner\n\nEvery Thanksgiving, the Carter family gathers around the table for pumpkin pie. "
        "The meal costs about 40 dollars per person.\n\nWater boils at 212 degrees Fahrenheit at sea level.")


@pytest.fixture
def make_services(tmp_path):
    created = []

    def _make(provider=None, overrides=None, config_overrides=None):
        from glocalinduct.config import _deep_merge
        offline = {'retrieval': {'embedder': 'hashing', 'knn_backend': 'python'},   # no model download in tests
                   'learning': LEGACY_LEARNING}
        cfg = load_config(overrides=_deep_merge(offline, config_overrides or {}), root=None)
        base = tmp_path / f'ws{len(created)}'
        cfg = cfg.model_copy(update={'paths': cfg.paths.model_copy(update={
            'database': str(base / 'db.sqlite3'), 'source_store': str(base / 'texts'),
            'private_references': str(base / 'private'), 'releases': str(base / 'releases'),
            'runs': str(base / 'runs'), 'reports': str(base / 'reports')})})
        prov = provider or FakeProvider(overrides=overrides)
        s = build_services(cfg, provider=prov)
        created.append(s)
        return s
    yield _make
    for s in created:
        s.close()
