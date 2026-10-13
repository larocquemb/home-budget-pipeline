from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import pytest
import yaml
from home_budget_pipeline import model_collaboration_queue as queue
from home_budget_pipeline import receipt_collaboration as collaboration
from home_budget_pipeline import receipt_model_providers as providers

pytestmark = pytest.mark.usefixtures('offline_gpu_sampling')

URL = 'https://sobeys.com/products/old-el-paso-salsa-picante-medium'
TITLE = 'Old El Paso Salsa Picante Medium'
SOURCE = 'ocr:1'
PROFILES = [providers.Profile('qwen', 'qwen', 'qwen3.8:27b', 32768, 8192, True),
            providers.Profile('openai', 'openai', 'test', 16384, 2048, True)]
IMAGE = {'id': 'image:1', 'sha256': 'same', 'data': 'test'}


def bundle():
    return {'sources': [{'id': 'item:canonical', 'kind': 'canonical_item', 'text': 'Cep Pic Med'},
                        {'id': SOURCE, 'kind': 'ocr_pass', 'engine': 'tesseract', 'text': 'Oep Pic Med'}],
            'documents': [], 'validations': {'receipt_arithmetic': 'pass', 'item_arithmetic': 'pass'},
            'coverage': {'available_passes': 1, 'included_passes': 1, 'available_documents': 1, 'included_documents': 1}}


def review(profile, chosen=True):
    identifier = collaboration.candidate_id(URL) if chosen else None
    return {**asdict(profile), 'profile': profile.name, 'status': 'success', 'output': {
        'candidate_id': identifier, 'candidate_title': TITLE if chosen else None,
        'product_source_id': identifier, 'source_ids': [SOURCE], 'reason': 'supported' if chosen else 'uncertain'},
        'exchange_coverage': {'included_sources': 2, 'available_sources': 2},
        'images': [{k: v for k, v in IMAGE.items() if k != 'data'}], 'image_errors': []}


class Dispatch:
    def __init__(self, initial_error=False, abstain=False):
        self.calls, self.initial_error, self.abstain = [], initial_error, abstain
    def round(self, stage, profiles, payload, context):
        self.calls.append((stage, profiles, deepcopy(payload)))
        if stage == 'independent':
            return [{'status': 'error', 'error_type': 'TimeoutError'} if self.initial_error and i == 0 else {
                'search_results': [{'url': URL, 'title': TITLE, 'snippet': '', 'query': 'salsa'}],
                'reviews': [review(p, i != 0)], 'images': review(p)['images'], 'blocking_errors': []}
                for i, p in enumerate(profiles)]
        return [review(p, not (self.abstain and i == 0)) for i, p in enumerate(profiles)]


def test_every_provider_independently_returns_then_reconsiders_shared_findings():
    dispatch = Dispatch()
    payload = queue.collaborate_item(dispatch, {'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
        bundle(), PROFILES, .85, {'run_uuid': str(uuid4()), 'item_id': 1})
    assert [c[0] for c in dispatch.calls] == ['independent', 'exchange']
    assert all(c[1] == PROFILES for c in dispatch.calls)
    findings = dispatch.calls[1][2]['findings']
    assert findings[0]['reviews'][0]['output']['candidate_id'] is None
    assert findings[1]['reviews'][0]['output']['candidate_id'] == collaboration.candidate_id(URL)
    assert payload['decision']['disposition'] == 'recommended'
    assert payload['decision']['provider_families'] == ['openai', 'qwen']
    assert payload['decision']['canonical_updated'] is False
    assert payload['independent_results'][0]['reviews'][0]['output']['candidate_id'] is None


@pytest.mark.parametrize('initial_error,abstain', [(True, False), (False, True)])
def test_failures_or_dissent_are_not_overridden_by_peer_agreement(initial_error, abstain):
    payload = queue.collaborate_item(Dispatch(initial_error, abstain), {'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'},
                                   bundle(), PROFILES, .85, {'run_uuid': str(uuid4()), 'item_id': 1})
    assert payload['decision']['disposition'] == 'review'
    assert bool(payload['blocking_errors']) == initial_error


def test_dispatch_publishes_all_provider_tasks_before_waiting(monkeypatch):
    dispatch = queue.Dispatcher.__new__(queue.Dispatcher)
    events, saved = [], {}
    class Store:
        def add(self, task_id, *args): saved[str(task_id)] = {'status': 'success'}; events.append('save')
        def results(self, cohort): events.append('wait'); return saved
    dispatch.store, dispatch.channel = Store(), object()
    monkeypatch.setattr(queue, 'declare', lambda *a, **kw: None)
    monkeypatch.setattr(queue, 'notification', lambda channel, name, task_id: events.append(name))
    result = dispatch.round('independent', PROFILES, {}, {'run_uuid': str(uuid4()), 'item_id': 1})
    assert events == ['save', 'save', queue.queue_name('qwen'), queue.queue_name('openai'), 'wait']
    assert len(result) == 2


def test_exchange_includes_dissent_images_and_validates_citations(monkeypatch):
    monkeypatch.setattr(queue.shared, 'render_images', lambda _: ([IMAGE], []))
    calls = []
    def request(profile, text, schema, images):
        calls.append((text, images))
        return {'output': review(profile)['output']}
    monkeypatch.setattr(providers, 'request', request)
    candidate = {'id': collaboration.candidate_id(URL), 'title': TITLE, 'ocr_support': [SOURCE], 'url': URL}
    result = queue.peer_review({'profile': asdict(PROFILES[0]), 'bundle': bundle(), 'candidates': [candidate],
                              'findings': [{'reason': 'brand uncertain'}], 'row': {}, 'context': {}})
    assert result['status'] == 'success'
    assert 'brand uncertain' in calls[0][0] and calls[0][1] == [IMAGE]
    def fabricated(*args):
        output = review(PROFILES[0])['output']; output['source_ids'] = ['invented']; return {'output': output}
    monkeypatch.setattr(providers, 'request', fabricated)
    assert queue.peer_review({'profile': asdict(PROFILES[0]), 'bundle': bundle(), 'candidates': [candidate],
                             'findings': [], 'row': {}, 'context': {}})['status'] == 'error'


def test_workers_are_provider_isolated_and_use_gitops_image():
    root = Path(__file__).resolve().parents[1]
    documents = list(yaml.safe_load_all((root / 'k8s/model-collaboration-workers.yaml').read_text()))
    assert len(documents) == 4
    for document in documents:
        provider = document['metadata']['name'].removeprefix('model-worker-')
        container = document['spec']['template']['spec']['containers'][0]
        assert container['args'] == ['--provider', provider]
        assert container['command'] == ['home-budget-model-worker']
        env = {v['name']: v for v in container['env']}
        assert 'RABBITMQ_URL' in env and 'DATABASE_URL' in env
        assert all(k in {'BRAVE_SEARCH_API_KEY', provider.upper() + '_API_KEY'} for k in env if k.endswith('_API_KEY'))
        assert container['volumeMounts'][0]['readOnly'] is True
        assert container['image'] == 'ghcr.io/larocquemb/home-budget-pipeline'
