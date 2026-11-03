"""Provider adapters: an async structured-generation interface (guide §3, §14).

All provider-specific code stays behind this interface. The runner owns retries,
schema correction and budgets; adapters only translate one request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..errors import ProviderFailed, ProviderRefusal


class ProviderTransientError(ProviderFailed):
    """Timeout, rate limit or server error: retryable with backoff."""


class ProviderPermanentError(ProviderFailed):
    """Authentication or invalid request: never retried."""


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ''
    request_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    refusal: bool = False


class Provider:
    name = 'base'

    def __init__(self, model: str, temperature: float = 0.0, timeout: float = 120.0):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    async def generate(self, system: str, user: str, schema: dict[str, Any], max_output_tokens: int,
                       schema_name: str = 'output') -> ProviderResponse:
        raise NotImplementedError

    def model_identity(self) -> dict[str, Any]:
        """Model alias and whether the provider reports an exact revision."""
        return {'provider': self.name, 'model': self.model, 'exact_revision': False}

    def count_tokens(self, text: str) -> int:
        from ..ingestion.windows import count_tokens
        return count_tokens(text)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Optional embedding support; None means 'use the local embedder'."""
        return None


def make_provider(cfg: Config, role: str = 'generation') -> Provider:
    """Build the configured provider for a role (generation | evaluation)."""
    name = cfg.runtime.provider or 'fake'
    if name == 'fake':
        from .fake import FakeProvider
        return FakeProvider()
    cfg.validate_runtime()
    model = cfg.runtime.evaluation_model if role == 'evaluation' else cfg.runtime.generation_model
    timeout = float(cfg.reliability.call_timeout_seconds)
    if name == 'anthropic':
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(model=model, api_key=cfg.api_key(), temperature=cfg.runtime.temperature,
                                 timeout=timeout, base_url=cfg.runtime.base_url)
    if name in ('openai', 'openai_compatible', 'ollama', 'vllm', 'openrouter', 'groq', 'deepseek'):
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(model=model, api_key=cfg.api_key(), temperature=cfg.runtime.temperature,
                                        timeout=timeout, base_url=cfg.runtime.base_url, vendor=name)
    raise ProviderPermanentError(f'unknown provider {name!r}')
