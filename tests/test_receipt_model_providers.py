import json
from types import SimpleNamespace

import pytest

from home_budget_pipeline import receipt_model_providers as providers
from home_budget_pipeline.receipt_collaboration import PROPOSAL_SCHEMA


OUTPUT = {'reading': 'Salsa', 'queries': ['Salsa medium'], 'source_ids': ['item:canonical'], 'reason': 'reading'}
IMAGES = [{'id': 'image:1:1', 'data': 'encoded-image'}]


def test_profiles_report_missing_credentials_and_apply_token_budgets(monkeypatch):
    for key in ('RECEIPT_MODEL_PROFILES', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY',
                'ANTHROPIC_RECEIPT_MODEL', 'GEMINI_RECEIPT_MODEL', 'OLLAMA_COLLAB_MODELS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('COLLAB_CONTEXT_TOKENS', '16384')
    monkeypatch.setenv('COLLAB_OUTPUT_TOKENS', '4096')
    monkeypatch.setenv('OLLAMA_COLLAB_MODELS', 'qwen3:30b,qwen3-vl:8b,qwen3:30b')
    enabled, skipped = providers.profiles()
    assert len(enabled) == 2 and enabled[1].vision is True
    assert all(p.output_tokens == 4096 and p.context_tokens == 16384 for p in enabled)
    assert any(p.get('provider') == 'openai' and p['reason'] == 'missing_credentials' for p in skipped)


@pytest.mark.parametrize('rows', [
    [{'name': 'bad', 'provider': 'unknown', 'model': 'test'}],
    [{'name': 'a', 'provider': 'qwen', 'model': 'test', 'context_tokens': 1024, 'output_tokens': 2048}],
    [{'name': 'a', 'provider': 'qwen', 'model': 'test'}] * 2,
])
def test_invalid_profiles_are_rejected_before_requests(monkeypatch, rows):
    monkeypatch.setenv('RECEIPT_MODEL_PROFILES', json.dumps(rows))
    with pytest.raises(ValueError): providers.profiles()


def test_qwen_text_and_vision_requests_use_explicit_budgets_and_release_model(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, 'post', lambda url, body, headers=None: calls.append(body) or {
        'message': {'content': json.dumps(OUTPUT)}, 'done': True, 'done_reason': 'stop',
        'prompt_eval_count': 200, 'eval_count': 40, 'load_duration': 1000000000})
    for vision in (False, True):
        result = providers.request(providers.Profile('qwen', 'qwen', 'qwen', 16384, 4096, vision),
                                   'evidence', PROPOSAL_SCHEMA, IMAGES)
        assert result['input_tokens'] == 200 and result['ollama_load_seconds'] == 1
    assert 'images' not in calls[0]['messages'][0]
    assert calls[1]['messages'][0]['images'] == ['encoded-image']
    assert calls[1]['options'] == {'temperature': 0, 'num_ctx': 16384, 'num_predict': 4096}
    assert calls[1]['keep_alive'] == 0


@pytest.mark.parametrize('provider', ['qwen', 'gemini', 'anthropic'])
def test_truncated_outputs_are_not_accepted(monkeypatch, provider):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'secret')
    monkeypatch.setenv('GEMINI_API_KEY', 'secret')
    data = {'done': True, 'done_reason': 'length', 'stop_reason': 'max_tokens',
            'candidates': [{'finishReason': 'MAX_TOKENS'}]}
    monkeypatch.setattr(providers, 'post', lambda *a, **kw: data)
    with pytest.raises(ValueError, match='Incomplete'):
        providers.request(providers.Profile(provider, provider, 'test'), 'evidence', PROPOSAL_SCHEMA, [])


def test_anthropic_forces_evidence_tool_and_attaches_images(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'secret')
    calls = []
    def post(url, body, headers):
        calls.append((url, body, headers))
        return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': 'receipt_evidence', 'input': OUTPUT}],
                'usage': {'input_tokens': 40, 'output_tokens': 20}}
    monkeypatch.setattr(providers, 'post', post)
    result = providers.request(providers.Profile('claude', 'anthropic', 'test', vision=True), 'evidence', PROPOSAL_SCHEMA, IMAGES)
    assert result['output'] == OUTPUT and result['output_tokens'] == 20
    body = calls[0][1]
    assert body['tools'][0]['input_schema'] == PROPOSAL_SCHEMA
    assert body['tool_choice']['name'] == 'receipt_evidence'
    assert body['messages'][0]['content'][-1]['source']['data'] == 'encoded-image'


def test_gemini_key_stays_in_header_and_images_are_inline(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'secret')
    calls = []
    def post(url, body, headers):
        calls.append((url, body, headers))
        return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(OUTPUT)}]}}],
                'usageMetadata': {'promptTokenCount': 50, 'candidatesTokenCount': 25}}
    monkeypatch.setattr(providers, 'post', post)
    result = providers.request(providers.Profile('gemini', 'gemini', 'test', vision=True), 'evidence', PROPOSAL_SCHEMA, IMAGES)
    assert result['output'] == OUTPUT and result['input_tokens'] == 50
    assert 'secret' not in calls[0][0] and calls[0][2]['x-goog-api-key'] == 'secret'
    assert calls[0][1]['generationConfig']['responseJsonSchema'] == PROPOSAL_SCHEMA
    assert calls[0][1]['contents'][0]['parts'][-1]['inlineData']['data'] == 'encoded-image'


def test_openai_images_schema_and_budget_are_sent_together(monkeypatch):
    import openai
    calls = []
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: SimpleNamespace(responses=SimpleNamespace(
        create=lambda **args: calls.append(args) or SimpleNamespace(status='completed',
            output_text=json.dumps(OUTPUT), usage=SimpleNamespace(input_tokens=50, output_tokens=20)))))
    result = providers.request(providers.Profile('openai', 'openai', 'test', output_tokens=4096, vision=True),
                               'evidence', PROPOSAL_SCHEMA, IMAGES)
    assert result['output'] == OUTPUT
    assert calls[0]['max_output_tokens'] == 4096 and calls[0]['store'] is False
    assert calls[0]['input'][0]['content'][-1]['image_url'].startswith('data:image/png;base64,')
