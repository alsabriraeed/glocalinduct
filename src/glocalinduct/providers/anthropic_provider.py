"""Anthropic Messages API adapter using structured JSON output
(`output_config.format` = json_schema; docs: platform.claude.com/docs/en/build-with-claude/structured-outputs).

The SDK's own retries are disabled; the runner owns technical retries and
schema corrections. The model identity is whatever the operator configured; a
mutable alias is recorded as such (guide §15).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .base import Provider, ProviderResponse, ProviderPermanentError, ProviderTransientError
from .schema_adapter import restrict_schema
from ..errors import ProviderRefusal


class AnthropicProvider(Provider):
    name = 'anthropic'

    def __init__(self, model: str, api_key: str | None, temperature: float = 0.0, timeout: float = 120.0,
                 base_url: str | None = None):
        super().__init__(model, temperature, timeout)
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise ProviderPermanentError('the anthropic package is not installed (pip install anthropic)') from e
        self._anthropic = anthropic
        kwargs: dict[str, Any] = {'api_key': api_key, 'max_retries': 0, 'timeout': timeout}
        if base_url:
            kwargs['base_url'] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)
        self._structured_supported = True

    def _schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            from anthropic import transform_schema  # type: ignore
            return transform_schema(schema)
        except Exception:
            return restrict_schema(schema)

    async def generate(self, system: str, user: str, schema: dict[str, Any], max_output_tokens: int,
                       schema_name: str = 'output') -> ProviderResponse:
        a = self._anthropic
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=max_output_tokens, temperature=self.temperature,
                                      system=system, messages=[{'role': 'user', 'content': user}])
        if self._structured_supported:
            kwargs['output_config'] = {'format': {'type': 'json_schema', 'schema': self._schema(schema)}}
        try:
            resp = await self._client.messages.create(**kwargs)
        except TypeError:
            # Older SDK without output_config: fall back to prompt-only JSON.
            self._structured_supported = False
            kwargs.pop('output_config', None)
            resp = await self._call_plain(kwargs)
        except a.BadRequestError as e:
            if 'output_config' in str(e) or 'json_schema' in str(e):
                self._structured_supported = False
                kwargs.pop('output_config', None)
                resp = await self._call_plain(kwargs)
            else:
                raise ProviderPermanentError(f'anthropic bad request: {e}') from e
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as e:
            raise ProviderPermanentError(f'anthropic permanent error: {e}') from e
        except (a.RateLimitError, a.APITimeoutError, a.APIConnectionError, a.InternalServerError) as e:
            raise ProviderTransientError(f'anthropic transient error: {e}') from e
        except a.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409, 429, 529):
                raise ProviderTransientError(f'anthropic status {e.status_code}: {e}') from e
            raise ProviderPermanentError(f'anthropic status {e.status_code}: {e}') from e
        text = ''.join(getattr(b, 'text', '') for b in resp.content if getattr(b, 'type', '') == 'text')
        stop = getattr(resp, 'stop_reason', None)
        if stop == 'refusal':
            raise ProviderRefusal('provider refused the request')
        if stop == 'max_tokens':
            raise ProviderTransientError('output truncated at max_tokens; increase reserved_output_tokens')
        usage = getattr(resp, 'usage', None)
        return ProviderResponse(text=text, input_tokens=getattr(usage, 'input_tokens', 0) or 0,
                                output_tokens=getattr(usage, 'output_tokens', 0) or 0,
                                model=getattr(resp, 'model', self.model), request_id=getattr(resp, 'id', None),
                                raw={'stop_reason': stop})

    async def _call_plain(self, kwargs: dict[str, Any]):
        kwargs['system'] = kwargs['system'] + '\n\nReturn ONLY a single JSON object. No prose, no code fences.'
        return await self._client.messages.create(**kwargs)

    def model_identity(self) -> dict[str, Any]:
        return {'provider': 'anthropic', 'model': self.model, 'exact_revision': False,
                'note': 'model string may be a mutable alias; response.model is recorded per call'}
