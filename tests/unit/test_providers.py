"""Provider adapter checks with mocked clients (no network, no keys, no quality claims)."""
import asyncio
import json
import types

import pytest

from glocalinduct.contracts import SCHEMA_MODELS, PlanPayload
from glocalinduct.errors import ProviderRefusal
from glocalinduct.providers.base import ProviderPermanentError, ProviderTransientError
from glocalinduct.providers.schema_adapter import restrict_schema

BANNED = ('minLength', 'maxLength', 'minimum', 'maximum', 'pattern', 'maxItems', 'exclusiveMinimum')


def _walk(node, path='$'):
    if isinstance(node, dict):
        for k, v in node.items():
            assert k not in BANNED, f'{k} at {path}'
            if k == 'minItems':
                assert v in (0, 1)
            if node.get('type') == 'object' or 'properties' in node:
                assert node.get('additionalProperties') is False
            _walk(v, f'{path}.{k}')
    elif isinstance(node, list):
        for v in node:
            _walk(v)


def test_restricted_schemas_use_supported_subset_only():
    for m in SCHEMA_MODELS:
        _walk(restrict_schema(m.model_json_schema()))


def test_sdk_transform_accepts_contract_schemas():
    anthropic = pytest.importorskip('anthropic')
    if hasattr(anthropic, 'transform_schema'):
        for m in SCHEMA_MODELS:
            anthropic.transform_schema(m.model_json_schema())


class _Resp:
    def __init__(self, text, stop='end_turn'):
        self.content = [types.SimpleNamespace(type='text', text=text)]
        self.stop_reason = stop
        self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)
        self.model = 'claude-test-exact'
        self.id = 'msg_1'


def _anthropic_provider(create):
    anthropic = pytest.importorskip('anthropic')
    from glocalinduct.providers.anthropic_provider import AnthropicProvider
    p = AnthropicProvider(model='claude-test', api_key='k')
    p._client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return p, anthropic


def test_anthropic_adapter_structured_output_and_error_mapping():
    captured = {}

    async def create(**kw):
        captured.update(kw)
        return _Resp('{"a": 1}')
    p, anthropic = _anthropic_provider(create)
    r = asyncio.run(p.generate('sys', '{"mode":"x"}', PlanPayload.model_json_schema(), 100, 'PlanPayload'))
    assert r.text == '{"a": 1}' and r.model == 'claude-test-exact' and r.input_tokens == 10
    assert captured['output_config']['format']['type'] == 'json_schema'
    assert captured['max_tokens'] == 100 and captured['system'] == 'sys'

    async def refuse(**kw):
        return _Resp('', stop='refusal')
    p, _ = _anthropic_provider(refuse)
    with pytest.raises(ProviderRefusal):
        asyncio.run(p.generate('s', '{}', {}, 10))

    async def truncated(**kw):
        return _Resp('{"a"', stop='max_tokens')
    p, _ = _anthropic_provider(truncated)
    with pytest.raises(ProviderTransientError):
        asyncio.run(p.generate('s', '{}', {}, 10))

    import httpx
    def _resp(code):
        return httpx.Response(code, request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))

    async def rate_limited(**kw):
        raise anthropic.RateLimitError('slow down', response=_resp(429), body=None)
    p, _ = _anthropic_provider(rate_limited)
    with pytest.raises(ProviderTransientError):
        asyncio.run(p.generate('s', '{}', {}, 10))

    async def unauthorized(**kw):
        raise anthropic.AuthenticationError('bad key', response=_resp(401), body=None)
    p, _ = _anthropic_provider(unauthorized)
    with pytest.raises(ProviderPermanentError):
        asyncio.run(p.generate('s', '{}', {}, 10))


def test_anthropic_adapter_falls_back_when_structured_output_unsupported():
    calls = []

    async def create(**kw):
        calls.append(kw)
        if 'output_config' in kw:
            raise TypeError('unexpected keyword output_config')
        return _Resp('```json\n{"ok": true}\n```')
    p, _ = _anthropic_provider(create)
    r = asyncio.run(p.generate('sys', '{}', {}, 50))
    assert 'Return ONLY' in calls[-1]['system'] and json.loads(r.text.strip('`json\n')) == {'ok': True}
    assert p._structured_supported is False


def test_openai_compatible_requires_base_url_and_maps_errors(monkeypatch):
    from glocalinduct.providers.openai_compatible import OpenAICompatibleProvider
    with pytest.raises(ProviderPermanentError):
        OpenAICompatibleProvider(model='m', api_key=None, vendor='openai_compatible')
    p = OpenAICompatibleProvider(model='m', api_key='k', vendor='openai')
    import httpx

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            assert headers['Authorization'] == 'Bearer k'
            body = {'choices': [{'message': {'content': '{"x":1}'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 2}, 'model': 'm-exact', 'id': 'c1'}
            return httpx.Response(200, json=body)
    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    r = asyncio.run(p.generate('s', '{}', PlanPayload.model_json_schema(), 10))
    assert r.text == '{"x":1}' and r.model == 'm-exact'


def test_config_runtime_validation_requires_fields_and_key(monkeypatch):
    from glocalinduct.config import load_config
    from glocalinduct.errors import ConfigError
    cfg = load_config(overrides={'runtime': {'provider': 'anthropic', 'generation_model': 'claude-x',
                                             'evaluation_model': 'claude-x', 'context_window_tokens': 100000,
                                             'reserved_output_tokens': 8000}})
    monkeypatch.delenv('GLOCALINDUCT_API_KEY', raising=False)
    with pytest.raises(ConfigError):
        cfg.validate_runtime()
    monkeypatch.setenv('GLOCALINDUCT_API_KEY', 'secret')
    cfg.validate_runtime()
    assert 'secret' not in json.dumps(cfg.redacted())


def test_model_presets_support_qwen3_8_and_gpt_4o(monkeypatch):
    from glocalinduct.config import load_config, resolve_model_preset
    from glocalinduct.providers.base import make_provider

    qwen = resolve_model_preset('qwen3.8')
    assert qwen['provider'] == 'ollama'
    assert qwen['generation_model'] == 'qwen3.8'
    assert qwen['evaluation_model'] == 'qwen3.8'

    cfg_qwen = load_config(overrides={'runtime': qwen})
    cfg_qwen.validate_runtime()
    prov_qwen = make_provider(cfg_qwen)
    assert prov_qwen.name == 'ollama' and prov_qwen.model == 'qwen3.8'
    assert '11434/v1' in prov_qwen.base_url

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    gpt = resolve_model_preset('gpt-4o')
    assert gpt['provider'] == 'openai'
    assert gpt['generation_model'] == 'gpt-4o'
    cfg_gpt = load_config(overrides={'runtime': gpt})
    cfg_gpt.validate_runtime()
    prov_gpt = make_provider(cfg_gpt)
    assert prov_gpt.name == 'openai' and prov_gpt.model == 'gpt-4o'
    assert prov_gpt.base_url == 'https://api.openai.com/v1'
