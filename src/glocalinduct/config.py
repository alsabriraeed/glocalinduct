"""Configuration loading and startup validation (guide §16).

Null runtime fields are deliberate deployment requirements: startup rejects them
before any real provider call rather than inventing a model or spending limit.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ConfigError

PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # repository root (contains configs/, prompts/)


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LanguageConfig(_Model):
    source: str = 'en'
    target: str = 'ar'
    variety: str = 'MSA'


class PathsConfig(_Model):
    data_root: str = 'data'
    database: str = 'workspace/staging/memory.sqlite3'
    source_store: str = 'workspace/source_store'
    private_references: str = 'workspace/private_references'
    releases: str = 'workspace/releases'
    runs: str = 'workspace/runs'
    traces: str = 'workspace/traces'
    reports: str = 'workspace/reports'
    prompts: str = 'prompts'
    domain_policies: str = 'configs/domain_policies.yaml'


class SplitConfig(_Model):
    learning: float = 0.8
    development: float = 0.1
    test: float = 0.1
    seed: int = 42
    seed_pairs: list[str] | None = None
    practice_pairs: list[str] | None = None

    @model_validator(mode='after')
    def sums(self):
        if abs(self.learning + self.development + self.test - 1.0) > 1e-9:
            raise ValueError('split ratios must sum to 1')
        return self


class WindowsConfig(_Model):
    content_tokens: int = 1200
    context_tokens_each_side: int = 200


class InsightsConfig(_Model):
    batch_size: int = 8
    min_support_groups: int = 2
    universal_min_domains: int = 2
    initial_importance: int = 2


class RetrievalConfig(_Model):
    insights: int = 0                       # 0 = all active insights (ExpeL concatenates the full rule set)
    experiences: int = 2                    # top-k successful demonstrations (ExpeL k=2)
    candidate_pool: int = 30
    similarity: str = 'cosine'              # inner product on L2-normalised vectors == cosine
    similarity_threshold: float | None = None
    query_mode: Literal['source_text', 'profile'] = 'source_text'  # ExpeL embeds the raw task text
    embedder: Literal['hashing', 'sentence-transformers'] = 'hashing'
    embedder_model: str = 'sentence-transformers/all-mpnet-base-v2'
    embedder_device: str = 'auto'           # auto = CUDA if visible, else CPU; or cpu / cuda / cuda:N
    knn_backend: Literal['python', 'faiss'] = 'python'  # faiss: IndexFlatIP exact kNN (falls back to python)
    insight_scope_filter: bool = True       # keep locale/setting/genre gates on culture-specific insights


class LearningConfig(_Model):
    """How closely the learning loop follows ExpeL (Zhao et al., 2024). Code defaults = legacy behaviour."""
    # False: human references are never retrieved as demonstrations (ExpeL has no human demos). They stay
    # private and only supply the success signal. M0 is empty and every learning pair is a practice task.
    reference_demonstrations: bool = True
    # reference_comparison: success == the private comparison says 'compatible' (ExpeL: environment reward).
    # a5_and_comparison: additionally requires the source-only A5 quality pass.
    success_signal: Literal['a5_and_comparison', 'reference_comparison'] = 'a5_and_comparison'
    # immediate: an ADD becomes active at once, no provisional state (ExpeL). gated: support-group gates.
    insight_admission: Literal['gated', 'immediate'] = 'gated'
    # free_text: an LLM_reflect agent writes a natural-language diagnosis that is fed to the next attempt.
    reflection: Literal['findings', 'free_text'] = 'findings'
    # True: every failed attempt is paired with a success of the same task (ExpeL), not only the last one.
    pair_every_failure: bool = False
    # True: an active insight (cited in insight_refs) may justify a substitution on its own, as ExpeL rules drive
    # the agent. False (guide): a substitution needs a retrieved experience in `support`.
    insight_substitution: bool = False


class AttemptsConfig(_Model):
    practice: int = 3
    deployment: int = 1


class EvaluationConfig(_Model):
    min_criterion_score: int = 3


class ReliabilityConfig(_Model):
    technical_retries: int = 2
    schema_corrections: int = 1
    call_timeout_seconds: int = 120


class BudgetsConfig(_Model):
    short_request_seconds: int = 600
    max_calls_per_window: int = 20
    max_calls_per_document: int | None = None
    max_tokens_per_document: int | None = None
    max_cost_per_document: float | None = None
    long_document_seconds: int | None = 3600


class RuntimeConfig(_Model):
    provider: str | None = None
    generation_model: str | None = None
    evaluation_model: str | None = None
    embedding_model: str | None = None
    tokenizer: str | None = None
    context_window_tokens: int | None = None
    reserved_output_tokens: int | None = None
    temperature: float = 0
    api_key_environment_variable: str = 'GLOCALINDUCT_API_KEY'
    base_url: str | None = None
    require_runtime_fields_before_real_calls: bool = True


class Config(_Model):
    schema_version: str = '1.0'
    language: LanguageConfig = Field(default_factory=LanguageConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    pair_id_pattern: str = r'^(?P<pair_id>[A-Za-z0-9][A-Za-z0-9-]*)(?:__(?P<title>.+))?$'
    include_pairs: list[str] | None = None
    split: SplitConfig = Field(default_factory=SplitConfig)
    seed_fraction_of_learning: float = 0.5
    near_duplicate_jaccard: float = 0.85
    windows: WindowsConfig = Field(default_factory=WindowsConfig)
    insights: InsightsConfig = Field(default_factory=InsightsConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    attempts: AttemptsConfig = Field(default_factory=AttemptsConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    root: Path = Field(default=PACKAGE_ROOT, exclude=True)

    # ------------------------------------------------------------------ paths
    def path(self, name: str) -> Path:
        value = getattr(self.paths, name)
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    # --------------------------------------------------------------- runtime
    @property
    def uses_real_provider(self) -> bool:
        return (self.runtime.provider or 'fake') != 'fake'

    def simulated_components(self) -> list[str]:
        """Configured components that simulate a real process (no semantic claims)."""
        out = []
        if not self.uses_real_provider:
            out.append('runtime.provider=fake (placeholder LLM output)')
        if self.retrieval.embedder == 'hashing':
            out.append('retrieval.embedder=hashing (lexical hashing instead of a semantic model)')
        return out

    def assert_real_pipeline(self) -> None:
        """Entry points (CLI, web server) call this so a missing setting can never silently fake a step.

        Offline tests inject their own services and are unaffected; a deliberate demo needs
        GLOCALINDUCT_ALLOW_SIMULATION=1.
        """
        sim = self.simulated_components()
        if sim and os.environ.get('GLOCALINDUCT_ALLOW_SIMULATION') != '1':
            raise ConfigError('refusing to run with simulated components: ' + '; '.join(sim)
                              + ' (set GLOCALINDUCT_ALLOW_SIMULATION=1 to allow a demo run)')

    def validate_runtime(self) -> None:
        """Reject null deployment fields before a real call (guide §16)."""
        if not self.uses_real_provider:
            return
        if not self.runtime.require_runtime_fields_before_real_calls:
            return
        rt = self.runtime
        missing = [k for k in ('generation_model', 'evaluation_model', 'context_window_tokens',
                               'reserved_output_tokens') if getattr(rt, k) is None]
        if missing:
            raise ConfigError(f'runtime fields required before real calls are null: {missing}')
        api_key = os.environ.get(rt.api_key_environment_variable) or (
            os.environ.get('OPENAI_API_KEY') if rt.provider in ('openai', 'openai_compatible') else None
        )
        if rt.provider != 'ollama' and not api_key:
            raise ConfigError(f'environment variable {rt.api_key_environment_variable} (or OPENAI_API_KEY) is not set')

    def api_key(self) -> str | None:
        return os.environ.get(self.runtime.api_key_environment_variable) or (
            os.environ.get('OPENAI_API_KEY') if self.runtime.provider in ('openai', 'openai_compatible') else None
        )

    def redacted(self) -> dict[str, Any]:
        """Config for logs and manifests: never includes secrets."""
        return self.model_dump(mode='json')


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_dotenv(root: Path | None = None) -> None:
    candidates = [Path.cwd() / '.env']
    if root is not None:
        candidates.insert(0, Path(root) / '.env')
    for candidate in candidates:
        if candidate.is_file():
            for line in candidate.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('\'"')
                    os.environ.setdefault(k, v)
            break


def default_ollama_base_url() -> str:
    if os.environ.get('OLLAMA_BASE_URL'):
        return os.environ['OLLAMA_BASE_URL']
    if Path('/.dockerenv').exists():
        return 'http://ollama:11434/v1'
    return 'http://localhost:11434/v1'


def resolve_model_preset(model_or_provider: str) -> dict[str, Any]:
    """Resolve a model or provider option (e.g. 'qwen3.8', 'gpt-4o', 'ollama', 'openai') into runtime overrides."""
    spec = model_or_provider.strip()
    low = spec.lower()
    if low in ('openai', 'gpt-4o', 'gpt4o'):
        model = 'gpt-4o'
        return {
            'provider': 'openai',
            'generation_model': model,
            'evaluation_model': model,
            'base_url': os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1',
            'context_window_tokens': int(os.environ.get('GLOCALINDUCT_CONTEXT_WINDOW') or 128000),
            'reserved_output_tokens': int(os.environ.get('GLOCALINDUCT_RESERVED_OUTPUT') or 16384),
        }
    if low.startswith('gpt-') or low.startswith('o1') or low.startswith('o3') or low.startswith('o4'):
        return {
            'provider': 'openai',
            'generation_model': spec,
            'evaluation_model': spec,
            'base_url': os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1',
            'context_window_tokens': int(os.environ.get('GLOCALINDUCT_CONTEXT_WINDOW') or 128000),
            'reserved_output_tokens': int(os.environ.get('GLOCALINDUCT_RESERVED_OUTPUT') or 16384),
        }
    if low == 'ollama':
        model = os.environ.get('OLLAMA_MODEL') or 'qwen3.8'
    elif low.startswith('ollama:'):
        model = spec.split(':', 1)[1] or 'qwen3.8'
    elif low in ('qwen3.8', 'qwen3_8'):
        model = 'qwen3.8'
    else:
        model = spec
    return {
        'provider': 'ollama',
        'generation_model': model,
        'evaluation_model': model,
        'base_url': default_ollama_base_url(),
        'context_window_tokens': int(os.environ.get('GLOCALINDUCT_CONTEXT_WINDOW') or 256000),
        'reserved_output_tokens': int(os.environ.get('GLOCALINDUCT_RESERVED_OUTPUT') or 8192),
    }


def load_config(path: str | Path | None = None, overrides: dict | None = None,
                root: str | Path | None = None) -> Config:
    """Load `configs/default.yaml` (or a given file) with optional overrides."""
    if path is None:
        path = PACKAGE_ROOT / 'configs' / 'default.yaml'
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except FileNotFoundError as e:
        raise ConfigError(f'config file not found: {path}') from e
    _load_dotenv(Path(root) if root else (path.parent.parent if path.parent.name == 'configs' else Path.cwd()))
    env_overrides = _runtime_from_env()
    if overrides and overrides.get('runtime', {}).get('provider') and env_overrides.get('provider') and overrides['runtime']['provider'] != env_overrides['provider']:
        env_overrides = {k: v for k, v in env_overrides.items() if k not in ('provider', 'generation_model', 'evaluation_model', 'embedding_model', 'base_url')}
    if env_overrides:
        raw = _deep_merge(raw, {'runtime': env_overrides})
    if overrides:
        raw = _deep_merge(raw, overrides)
    try:
        cfg = Config.model_validate(raw)
    except Exception as e:  # pydantic ValidationError
        raise ConfigError(f'invalid configuration: {e}') from e
    if root is not None:
        cfg = cfg.model_copy(update={'root': Path(root)})
    elif path.parent.name == 'configs':
        cfg = cfg.model_copy(update={'root': path.parent.parent.resolve()})
    return cfg


def _runtime_from_env() -> dict[str, Any]:
    """Allow deployment selection through environment variables (no secrets here)."""
    out: dict[str, Any] = {}
    if os.environ.get('GLOCALINDUCT_MODEL'):
        out.update(resolve_model_preset(os.environ['GLOCALINDUCT_MODEL']))
    mapping = {
        'GLOCALINDUCT_PROVIDER': 'provider',
        'GLOCALINDUCT_GENERATION_MODEL': 'generation_model',
        'GLOCALINDUCT_EVALUATION_MODEL': 'evaluation_model',
        'GLOCALINDUCT_EMBEDDING_MODEL': 'embedding_model',
        'GLOCALINDUCT_BASE_URL': 'base_url',
    }
    for env, key in mapping.items():
        if os.environ.get(env):
            out[key] = os.environ[env]
    provider = out.get('provider')
    if provider == 'ollama':
        if out.get('base_url') == 'https://api.openai.com/v1' or not out.get('base_url'):
            out['base_url'] = default_ollama_base_url()
        if not out.get('generation_model'):
            out['generation_model'] = os.environ.get('OLLAMA_MODEL') or 'qwen3.8'
        if not out.get('evaluation_model'):
            out['evaluation_model'] = out['generation_model']
    else:
        if not out.get('base_url') and os.environ.get('OPENAI_BASE_URL'):
            out['base_url'] = os.environ['OPENAI_BASE_URL']
        if not out.get('generation_model') and os.environ.get('OPENAI_MODEL'):
            out['generation_model'] = os.environ['OPENAI_MODEL']
        if not out.get('evaluation_model') and os.environ.get('OPENAI_MODEL'):
            out['evaluation_model'] = os.environ['OPENAI_MODEL']
    for env, key in (('GLOCALINDUCT_CONTEXT_WINDOW', 'context_window_tokens'),
                     ('GLOCALINDUCT_RESERVED_OUTPUT', 'reserved_output_tokens')):
        if os.environ.get(env):
            out[key] = int(os.environ[env])
    temp = os.environ.get('GLOCALINDUCT_TEMPERATURE') or os.environ.get('MODEL_TEMPERATURE')
    if temp is not None:
        try:
            out['temperature'] = float(temp)
        except ValueError:
            pass
    return out
