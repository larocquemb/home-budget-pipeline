"""Bounded model requests for receipt collaboration; no global provider mutations."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    model: str
    context_tokens: int = 16384
    output_tokens: int = 2048
    vision: bool = False


class IncompleteModelOutput(ValueError):
    def __init__(self, usage):
        super().__init__('Incomplete model output')
        self.usage = usage


def incomplete_ollama_output(message):
    """Capture at most 1024 response characters; count reasoning without storing it."""
    message = message if isinstance(message, dict) else {}
    content = message.get('content')
    thinking = message.get('thinking')
    head, tail = None, ''
    if isinstance(content, str):
        head = content if len(content) <= 1024 else content[:768]
        tail = content[-256:] if len(content) > 1024 else ''
    return {
        'sample_limit_chars': 1024,
        'content_chars': len(content) if isinstance(content, str) else None,
        'thinking_chars': len(thinking) if isinstance(thinking, str) else None,
        'content_head': head,
        'content_tail': tail,
        'omitted_content_chars': max(0, len(content) - 1024) if isinstance(content, str) else None,
    }


def profiles() -> tuple[list[Profile], list[dict]]:
    """Explicit profiles override defaults; missing credentials never look like votes."""
    configured = os.getenv('RECEIPT_MODEL_PROFILES', '').strip()
    if configured:
        rows = json.loads(configured)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
            raise ValueError('Configure between one and eight receipt model profiles')
        selected = [Profile(**row) for row in rows]
    else:
        budgets = {'context_tokens': int(os.getenv('COLLAB_CONTEXT_TOKENS', '16384')),
                   'output_tokens': int(os.getenv('COLLAB_OUTPUT_TOKENS', '2048'))}
        selected = [Profile('openai', 'openai', os.getenv('AI_PRODUCT_MODEL', 'gpt-5.6-terra'), **budgets, vision=True)]
        for provider, key in [('anthropic', 'ANTHROPIC_RECEIPT_MODEL'), ('gemini', 'GEMINI_RECEIPT_MODEL')]:
            model = os.getenv(key, '').strip()
            if model:
                selected.append(Profile(provider, provider, model, **budgets, vision=True))
        models = os.getenv('OLLAMA_COLLAB_MODELS', 'qwen3:30b').split(',')
        qwen_budgets = {
            'context_tokens': int(os.getenv('QWEN_CONTEXT_TOKENS', '').strip() or os.getenv('COLLAB_CONTEXT_TOKENS', '32768')),
            'output_tokens': int(os.getenv('QWEN_OUTPUT_TOKENS', '').strip() or os.getenv('COLLAB_OUTPUT_TOKENS', '8192')),
        }
        for index, model in enumerate(dict.fromkeys(m.strip() for m in models if m.strip())):
            selected.append(Profile(f'qwen-{index + 1}', 'qwen', model,
                **qwen_budgets,
                vision='vl' in model.lower()))
    if len(selected) > 8 or len({p.name for p in selected}) != len(selected):
        raise ValueError('Profiles must have unique names and at most eight entries')
    enabled, skipped = [], []
    keys = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'gemini': 'GEMINI_API_KEY'}
    for profile in selected:
        if profile.provider not in {*keys, 'qwen'} or not profile.name or not profile.model:
            raise ValueError('Unknown provider or missing profile name/model')
        if not isinstance(profile.vision, bool):
            raise ValueError('vision must be a JSON boolean')
        if not 1024 <= profile.context_tokens <= 65536 or not 256 <= profile.output_tokens <= 16384:
            raise ValueError('Context must be 1024..65536; output must be 256..16384 tokens')
        if profile.output_tokens >= profile.context_tokens:
            raise ValueError('Output budget must be smaller than context')
        if profile.provider in keys and not os.getenv(keys[profile.provider], '').strip():
            skipped.append({'profile': profile.name, 'provider': profile.provider, 'reason': 'missing_credentials'})
        else:
            enabled.append(profile)
    for provider, key in [('anthropic', 'ANTHROPIC_RECEIPT_MODEL'), ('gemini', 'GEMINI_RECEIPT_MODEL')]:
        if not configured and not os.getenv(key, '').strip():
            skipped.append({'provider': provider, 'reason': 'model_not_configured'})
    if not enabled:
        raise ValueError('No receipt model profiles are available')
    return enabled, skipped


def post(url, body, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(request, timeout=float(os.getenv('COMPARISON_TIMEOUT_SECONDS', '180'))) as response:
        return json.load(response)


def qwen_content(text, schema, images):
    content = text + '\nReturn one concise JSON object matching this schema; do not repeat instructions or evidence:\n' + json.dumps(schema, separators=(',', ':'))
    if images:
        content += '\nImages in order: ' + ', '.join(image['id'] for image in images)
    return content


def request(profile: Profile, text: str, schema: dict, images: list[dict]) -> dict:
    """Return parsed structured output and usage; reject truncated/blocked responses."""
    images = images if profile.vision else []
    if profile.provider == 'openai':
        from openai import OpenAI
        content = [{'type': 'input_text', 'text': text}]
        for image in images:
            content.extend([{'type': 'input_text', 'text': 'Evidence image ' + image['id']},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,' + image['data']}])
        response = OpenAI(timeout=float(os.getenv('COMPARISON_TIMEOUT_SECONDS', '180')), max_retries=0).responses.create(
            model=profile.model, input=[{'role': 'user', 'content': content}],
            reasoning={'effort': 'low'}, max_output_tokens=profile.output_tokens, store=False,
            text={'format': {'type': 'json_schema', 'name': 'receipt_evidence', 'strict': True, 'schema': schema}})
        if response.status != 'completed':
            raise IncompleteModelOutput({'input_tokens': getattr(response.usage, 'input_tokens', None),
                                         'output_tokens': getattr(response.usage, 'output_tokens', None)})
        return {'output': json.loads(response.output_text),
            'input_tokens': getattr(response.usage, 'input_tokens', None),
            'output_tokens': getattr(response.usage, 'output_tokens', None)}
    if profile.provider == 'anthropic':
        content = [{'type': 'text', 'text': text}]
        for image in images:
            content.extend([{'type': 'text', 'text': 'Evidence image ' + image['id']},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': image['data']}}])
        data = post('https://api.anthropic.com/v1/messages', {
            'model': profile.model, 'max_tokens': profile.output_tokens,
            'messages': [{'role': 'user', 'content': content}],
            'tools': [{'name': 'receipt_evidence', 'description': 'Return the requested receipt evidence decision', 'input_schema': schema}],
            'tool_choice': {'type': 'tool', 'name': 'receipt_evidence', 'disable_parallel_tool_use': True}},
            {'x-api-key': os.environ['ANTHROPIC_API_KEY'], 'anthropic-version': '2023-06-01'})
        if data.get('stop_reason') != 'tool_use':
            raise IncompleteModelOutput({key: data.get('usage', {}).get(key) for key in ('input_tokens', 'output_tokens')})
        output = next(block['input'] for block in data['content']
                      if block['type'] == 'tool_use' and block['name'] == 'receipt_evidence')
        return {'output': output, **{key: data.get('usage', {}).get(key) for key in ('input_tokens', 'output_tokens')}}
    if profile.provider == 'gemini':
        parts = [{'text': text}]
        for image in images:
            parts.extend([{'text': 'Evidence image ' + image['id']},
                {'inlineData': {'mimeType': 'image/png', 'data': image['data']}}])
        model = urllib.parse.quote(profile.model.removeprefix('models/'), safe='')
        data = post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent', {
            'contents': [{'role': 'user', 'parts': parts}],
            'generationConfig': {'maxOutputTokens': profile.output_tokens,
                'responseMimeType': 'application/json', 'responseJsonSchema': schema}},
            {'x-goog-api-key': os.environ['GEMINI_API_KEY']})
        candidate = data.get('candidates', [])[0]
        if candidate.get('finishReason') != 'STOP':
            raise IncompleteModelOutput({'input_tokens': data.get('usageMetadata', {}).get('promptTokenCount'),
                                         'output_tokens': data.get('usageMetadata', {}).get('candidatesTokenCount')})
        output = ''.join(p.get('text', '') for p in candidate['content']['parts'] if not p.get('thought'))
        usage = data.get('usageMetadata', {})
        return {'output': json.loads(output), 'input_tokens': usage.get('promptTokenCount'),
                'output_tokens': usage.get('candidatesTokenCount'), 'reasoning_tokens': usage.get('thoughtsTokenCount')}
    if profile.provider != 'qwen':
        raise ValueError('Unknown model provider')
    base = os.getenv('OLLAMA_BASE_URL', 'http://192.168.2.201:11434').rstrip('/')
    message = {'role': 'user', 'content': qwen_content(text, schema, images)}
    if images:
        message['images'] = [image['data'] for image in images]
    data = post(base + '/api/chat', {'model': profile.model, 'messages': [message],
        'stream': False, 'think': False, 'format': schema, 'keep_alive': '5m',
        'options': {'temperature': 0, 'num_ctx': profile.context_tokens, 'num_predict': profile.output_tokens}})
    usage = {'input_tokens': data.get('prompt_eval_count'), 'output_tokens': data.get('eval_count')}
    for field in ('total_duration', 'load_duration', 'prompt_eval_duration', 'eval_duration'):
        value = data.get(field)
        usage['ollama_' + field.replace('duration', 'seconds')] = value / 1e9 if isinstance(value, (int, float)) else None
    if not data.get('done') or data.get('done_reason') != 'stop':
        raise IncompleteModelOutput({**usage, 'finish_reason': data.get('done_reason'),
                                     'incomplete_output': incomplete_ollama_output(data.get('message'))})
    return {'output': json.loads(data['message']['content']), **usage}
