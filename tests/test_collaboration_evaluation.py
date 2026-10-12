from copy import deepcopy
import pytest
from home_budget_pipeline import collaboration_evaluation as evaluation
from home_budget_pipeline import receipt_collaboration as collaboration
from home_budget_pipeline import receipt_model_providers as providers
from home_budget_pipeline import receipt_shared_evidence as shared
from home_budget_pipeline import product_enrichment as core
from home_budget_pipeline.brave_query_cache import ACTIVE_CACHE

pytestmark = pytest.mark.usefixtures('offline_gpu_sampling')
URL = 'https://www.sobeys.com/products/old-el-paso-salsa-picante-style-restaurant-medium-650-ml'
TITLE = 'Buy Old El Paso Salsa Picante Style Restaurant Medium 650 ml | Sobeys Inc.'
SOURCE = 'pass:alternative:1'


def test_actual_production_prefix_and_review_path_runs_for_each_model(monkeypatch):
    bundle = {'sources': [{'id': 'canonical', 'kind': 'canonical_item', 'text': 'Cep Pic Med'},
        {'id': SOURCE, 'kind': 'ocr_pass', 'engine': 'tesseract', 'text': 'Oep Pic Med'}],
        'documents': [], 'receipt_totals': {}, 'coverage': {'available_passes': 1, 'included_passes': 1,
        'available_documents': 0, 'included_documents': 0},
        'validations': {'receipt_arithmetic': 'pass', 'item_arithmetic': 'pass'}}
    original = deepcopy(bundle)
    calls, searches = [], []
    def request(profile, text, schema, images):
        if set(schema['properties']) == {'queries'}:
            calls.append((profile.model, 'prefix', text))
            return {'output': {'queries': ['picante medium', 'pictorial medium', 'piccolo medium']}}
        calls.append((profile.model, 'review', text))
        assert images and SOURCE in schema['properties']['source_ids']['items']['enum']
        identifier = collaboration.candidate_id(URL)
        return {'output': {'candidate_id': identifier, 'candidate_title': TITLE,
            'product_source_id': identifier, 'source_ids': [SOURCE], 'reason': 'Evidence agrees'}}
    def fetch(key, query, **kwargs):
        searches.append(query)
        return [{'title': TITLE, 'url': URL, 'snippet': ''}] if query == 'site:sobeys.com picante medium' else []
    monkeypatch.setattr(providers, 'request', request)
    monkeypatch.setattr(core, '_fetch_brave_query', fetch)
    monkeypatch.setattr(shared, 'render_images', lambda bundle: ([{'id': 'image:1', 'data': 'same-image'}], []))
    case = {'id': 'salsa'}
    inputs = {'salsa': {'row': {'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}, 'bundle': bundle}}
    rows, evidence = evaluation.run([case], ['baseline', 'candidate'], 'key', .85, inputs)
    assert [(model, stage) for model, stage, _ in calls] == [
        ('baseline', 'prefix'), ('baseline', 'review'), ('candidate', 'prefix'), ('candidate', 'review')]
    assert calls[0][2] == calls[2][2]
    assert all(row['candidate_url'] == URL and row['accepted'] for row in rows)
    assert all(row['collaboration']['decision']['canonical_updated'] is False for row in rows)
    assert searches.count('site:sobeys.com picante medium') == 1
    assert bundle == original
    assert ACTIVE_CACHE.get() is None


def test_search_snapshot_is_copied_not_shared_mutable_data():
    snapshot = evaluation.SharedSearchSnapshot()
    first = snapshot.get('query', lambda: [{'title': 'same evidence'}])
    first[0]['title'] = 'changed by caller'
    assert snapshot.get('query', lambda: pytest.fail('must reuse captured search')) == [{'title': 'same evidence'}]


def test_database_transaction_is_read_only_and_uses_real_shared_loader(monkeypatch):
    import psycopg
    statements = []
    row = {'id': 7, 'item_name': 'Cep Pic Med', 'store_name': 'Sobeys'}
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params=None): statements.append(sql); return self
        def fetchall(self): return [row]
        def rollback(self): statements.append('ROLLBACK')
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: Connection())
    calls = []
    monkeypatch.setattr(shared, 'load', lambda conn, supplied: calls.append(supplied) or {'sources': []})
    case = {'id': 'salsa', 'receipt_filename': 'receipt.pdf', 'receipt_item': 'Cep Pic Med', 'merchant': 'Sobeys'}
    inputs = evaluation.load_inputs([case], 'local-test')
    assert 'READ ONLY' in statements[0]
    assert statements[-1] == 'ROLLBACK'
    assert all(sql.startswith(('SET ', 'SELECT ', 'ROLLBACK')) for sql in statements)
    assert calls == [row] and inputs['salsa']['row'] == row


def test_collaboration_failure_is_incomplete_and_restores_cache_context(monkeypatch):
    monkeypatch.setattr(collaboration, 'collaborate_item', lambda *args: (_ for _ in ()).throw(TimeoutError('secret')))
    rows, _ = evaluation.run([{'id': 'case'}], ['base', 'new'], 'key', .85,
                            {'case': {'row': {}, 'bundle': {}}})
    assert all(row['status'] == 'error' and row['error_type'] == 'TimeoutError' for row in rows)
    assert 'secret' not in str(rows)
    assert ACTIVE_CACHE.get() is None


def test_failed_search_is_frozen_for_both_models_without_secret_error_text():
    import urllib.error
    snapshot = evaluation.SharedSearchSnapshot()
    calls = []
    def fetch():
        calls.append(1)
        raise urllib.error.HTTPError('https://example.invalid', 402, 'SECRET', {}, None)
    for _ in range(2):
        with pytest.raises(urllib.error.HTTPError):
            snapshot.get('same-query', fetch)
    assert len(calls) == 1
    assert snapshot.errors['same-query'] == {'error_type': 'HTTPError', 'http_status': 402}
    assert 'SECRET' not in str(snapshot.errors)
