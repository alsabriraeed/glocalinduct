"""OpenAI-compatible chat-completions adapter (OpenAI, Ollama, vLLM, OpenRouter, ...).

Uses `response_format={"type":"json_schema", ...}` when the vendor supports it and
falls back to `json_object` / prompt-only JSON otherwise. Also exposes an
embeddings endpoint when available (used by retrieval if configured).
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import Provider, ProviderResponse, ProviderPermanentError, ProviderTransientError
from .schema_adapter import restrict_schema
from ..config import default_ollama_base_url
from ..errors import ProviderRefusal

DEFAULT_URLS = {
    'openai': 'https://api.openai.com/v1', 'openrouter': 'https://openrouter.ai/api/v1',
    'groq': 'https://api.groq.com/openai/v1', 'deepseek': 'https://api.deepseek.com/v1',
    'ollama': 'http://localhost:11434/v1', 'vllm': 'http://localhost:8000/v1', 'openai_compatible': None,
}


class OpenAICompatibleProvider(Provider):
    name = 'openai_compatible'

    def __init__(self, model: str, api_key: str | None, temperature: float = 0.0, timeout: float = 120.0,
                 base_url: str | None = None, vendor: str = 'openai', embedding_model: str | None = None):
        super().__init__(model, temperature, timeout)
        self.vendor = vendor
        self.name = vendor
        if vendor == 'ollama' and (not base_url or base_url.rstrip('/') == 'https://api.openai.com/v1'):
            base_url = default_ollama_base_url()
        self.base_url = (base_url or DEFAULT_URLS.get(vendor) or '').rstrip('/')
        if not self.base_url:
            raise ProviderPermanentError('runtime.base_url is required for openai_compatible providers')
        self.api_key = api_key
        self.embedding_model = embedding_model
        self._json_schema_supported = vendor in ('openai', 'openrouter', 'vllm', 'ollama', 'openai_compatible')

    def _headers(self) -> dict[str, str]:
        h = {'Content-Type': 'application/json'}
        if self.api_key:
            h['Authorization'] = f'Bearer {self.api_key}'
        return h

    async def generate(self, system: str, user: str, schema: dict[str, Any], max_output_tokens: int,
                       schema_name: str = 'output') -> ProviderResponse:
        body: dict[str, Any] = {'model': self.model, 'temperature': self.temperature,
                                'max_tokens': max(max_output_tokens, 4096),
                                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        if self.vendor == 'ollama':
            body['options'] = {'num_ctx': max(32768, max_output_tokens * 4)}
            effort = os.environ.get('GLOCALINDUCT_REASONING_EFFORT', 'none').strip().lower()
            if effort and effort not in ('default', 'auto') and 'qwen3' in self.model.lower():
                body['reasoning_effort'] = effort
        if self._json_schema_supported:
            body['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': schema_name, 'strict': True, 'schema': restrict_schema(schema)}}
        else:
            body['response_format'] = {'type': 'json_object'}
            body['messages'][0]['content'] += '\n\nReturn ONLY one JSON object matching the schema.'
        data = await self._post('/chat/completions', body)
        if data is None:  # json_schema unsupported → retry json_object once
            self._json_schema_supported = False
            return await self.generate(system, user, schema, max_output_tokens, schema_name)
        choice = (data.get('choices') or [{}])[0]
        msg = choice.get('message', {})
        if msg.get('refusal'):
            raise ProviderRefusal(str(msg['refusal']))
        content = msg.get('content') or ''
        if choice.get('finish_reason') == 'length':
            try:
                json.loads(content)
            except Exception:
                raise ProviderTransientError('output truncated (finish_reason=length)')
        usage = data.get('usage', {})
        return ProviderResponse(text=msg.get('content') or '', input_tokens=usage.get('prompt_tokens', 0) or 0,
                                output_tokens=usage.get('completion_tokens', 0) or 0, model=data.get('model', self.model),
                                request_id=data.get('id'))

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(self.base_url + path, headers=self._headers(), json=body)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            raise ProviderTransientError(f'{self.vendor} connection error: {e}') from e
        if r.status_code == 400 and 'response_format' in body and body['response_format'].get('type') == 'json_schema' \
                and ('response_format' in r.text or 'json_schema' in r.text):
            return None
        if r.status_code in (401, 403, 404):
            raise ProviderPermanentError(f'{self.vendor} {r.status_code}: {r.text[:300]}')
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderTransientError(f'{self.vendor} {r.status_code}: {r.text[:300]}')
        if r.status_code >= 400:
            raise ProviderPermanentError(f'{self.vendor} {r.status_code}: {r.text[:300]}')
        return r.json()

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.embedding_model:
            return None
        data = await self._post('/embeddings', {'model': self.embedding_model, 'input': texts})
        if not data:
            return None
        return [d['embedding'] for d in sorted(data['data'], key=lambda d: d['index'])]
