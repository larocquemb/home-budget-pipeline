import json
from pathlib import Path
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
    monkeypatch.setenv('QWEN_CONTEXT_TOKENS', '')
    monkeypatch.setenv('QWEN_OUTPUT_TOKENS', ' ')
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
    assert json.dumps(PROPOSAL_SCHEMA, separators=(',', ':')) in calls[0]['messages'][0]['content']
    assert calls[0]['format'] == PROPOSAL_SCHEMA


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


@pytest.mark.parametrize('content', ['', '{"reading": "unfinished', 'START' + '繰り返し' * 1000 + 'END'])
def test_qwen_truncation_diagnostics_are_bounded_and_keep_usage(monkeypatch, content):
    thinking = 'private reasoning' * 500
    monkeypatch.setattr(providers, 'post', lambda *args: {
        'done': True, 'done_reason': 'length', 'message': {'content': content, 'thinking': thinking},
        'prompt_eval_count': 623, 'eval_count': 2048, 'eval_duration': 2_000_000_000,
        'total_duration': 3_000_000_000, 'unexpected': 'must not be retained'})
    with pytest.raises(providers.IncompleteModelOutput) as error:
        providers.request(providers.Profile('qwen', 'qwen', 'test'), 'request must not be retained', PROPOSAL_SCHEMA, IMAGES)
    usage = error.value.usage
    sample = usage['incomplete_output']
    assert sample['content_chars'] == len(content) and sample['thinking_chars'] == len(thinking)
    assert len(sample['content_head']) + len(sample['content_tail']) <= 1024
    expected_head = content[:768] if len(content) > 1024 else content
    assert sample['content_head'] == expected_head
    assert sample['omitted_content_chars'] == max(0, len(content) - 1024)
    if len(content) > 1024: assert sample['content_tail'].endswith('END')
    assert usage['input_tokens'] == 623 and usage['output_tokens'] == 2048
    assert usage['ollama_eval_seconds'] == 2 and usage['ollama_total_seconds'] == 3
    assert 'private reasoning' not in json.dumps(usage)
    assert 'must not be retained' not in json.dumps(usage)
    assert 'encoded-image' not in json.dumps(usage)


@pytest.mark.parametrize('message', [None, {}, {'content': None}, {'content': [], 'thinking': {}}])
def test_missing_or_invalid_response_fields_do_not_mask_truncation(message):
    sample = providers.incomplete_ollama_output(message)
    assert sample['content_chars'] is None and sample['thinking_chars'] is None
    assert sample['content_head'] is None and sample['omitted_content_chars'] is None


def test_qwen_defaults_leave_room_for_reasoning_without_increasing_paid_budget(monkeypatch):
    for key in ('RECEIPT_MODEL_PROFILES', 'COLLAB_CONTEXT_TOKENS', 'COLLAB_OUTPUT_TOKENS',
                'QWEN_CONTEXT_TOKENS', 'QWEN_OUTPUT_TOKENS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    enabled, _ = providers.profiles()
    qwen = next(p for p in enabled if p.provider == 'qwen')
    openai = next(p for p in enabled if p.provider == 'openai')
    assert (qwen.context_tokens, qwen.output_tokens) == (32768, 8192)
    assert (openai.context_tokens, openai.output_tokens) == (16384, 2048)
    monkeypatch.setenv('QWEN_OUTPUT_TOKENS', '16384')
    enabled, _ = providers.profiles()
    assert next(p for p in enabled if p.provider == 'qwen').output_tokens == 16384
    assert next(p for p in enabled if p.provider == 'openai').output_tokens == 2048


def test_production_configmap_preserves_qwen_budget_over_shared_defaults(monkeypatch):
    import yaml
    for key in ('RECEIPT_MODEL_PROFILES', 'COLLAB_CONTEXT_TOKENS', 'COLLAB_OUTPUT_TOKENS',
                'QWEN_CONTEXT_TOKENS', 'QWEN_OUTPUT_TOKENS'):
        monkeypatch.delenv(key, raising=False)
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'k8s/receipt-model-profiles.yaml').read_text())
    for key, value in config['data'].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    enabled, _ = providers.profiles()
    qwen = next(p for p in enabled if p.provider == 'qwen')
    openai = next(p for p in enabled if p.provider == 'openai')
    assert (qwen.context_tokens, qwen.output_tokens) == (32768, 8192)
    assert (openai.context_tokens, openai.output_tokens) == (16384, 2048)
    assert qwen.model == 'qwen3-vl:30b-a3b-instruct-q4_K_M'
