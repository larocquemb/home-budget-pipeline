import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import time

import pytest
import psycopg

from home_budget_pipeline.brave_query_cache import BraveQueryCache, cache_key, request_parameters
from home_budget_pipeline import product_enrichment as core
from test_receipt_recommendation_acceptance_db import receipt, accept

pytestmark = pytest.mark.integration
RESULTS = [{'title': 'Old El Paso Salsa Picante Medium', 'url': 'https://www.sobeys.com/products/salsa', 'snippet': ''}]


@pytest.fixture(autouse=True)
def empty_query_cache():
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        conn.execute('DELETE FROM enrichment.brave_query_cache')


def test_cache_survives_new_jobs_and_rescores_the_same_raw_result_for_each_item(monkeypatch):
    calls = []
    monkeypatch.setattr(core, '_fetch_brave_query', lambda *args, **kwargs: calls.append(args) or RESULTS)
    dsn = os.environ['TEST_DATABASE_URL']
    with BraveQueryCache(dsn) as cache:
        first = core.brave_candidates('secret', 'site:sobeys.com salsa', 'Cep Pic Med', 'sobeys.com')
        other = core.brave_candidates('secret', 'site:sobeys.com salsa', 'Mysterious Widget', 'sobeys.com')
        assert len(calls) == 1 and first[0].score > .9 and other[0].score == 0
        assert cache.requests == 1 and cache.hits == 1
    with BraveQueryCache(dsn) as cache:
        core.brave_candidates('secret', 'site:sobeys.com salsa', 'Cep Pic Med', 'sobeys.com')
        assert len(calls) == 1 and cache.observations[0]['cache_status'] == 'persistent_hit'
        assert cache.requests == 0 and cache.hits == 1
    with BraveQueryCache(dsn, refresh=True) as cache:
        core.brave_candidates('secret', 'site:sobeys.com salsa', 'Cep Pic Med', 'sobeys.com')
        core.brave_candidates('secret', 'site:sobeys.com salsa', 'Cep Pic Med', 'sobeys.com')
        assert len(calls) == 2 and cache.requests == cache.hits == 1


def test_expiration_negative_results_and_failures_are_distinct():
    dsn = os.environ['TEST_DATABASE_URL']
    def fail(): raise RuntimeError('request failed')
    with BraveQueryCache(dsn) as cache:
        with pytest.raises(RuntimeError): cache.get('q', fail)
        assert cache.get('q', lambda: []) == []
        assert cache.requests == 2
    with psycopg.connect(dsn) as conn:
        lifetime = conn.execute("SELECT extract(epoch FROM expires_at-fetched_at) FROM enrichment.brave_query_cache").fetchone()[0]
        assert lifetime == 3600
        conn.execute("UPDATE enrichment.brave_query_cache SET expires_at=NOW()-interval '1 second'")
    with BraveQueryCache(dsn) as cache:
        assert cache.get('q', lambda: RESULTS) == RESULTS
        assert cache.requests == 1
    with psycopg.connect(dsn) as conn:
        assert conn.execute('SELECT extract(epoch FROM expires_at-fetched_at) FROM enrichment.brave_query_cache').fetchone()[0] == 90 * 86400


def test_positive_retention_is_configurable_without_changing_negative_retention(monkeypatch):
    monkeypatch.setenv('BRAVE_CACHE_DAYS', '180')
    with BraveQueryCache(os.environ['TEST_DATABASE_URL']) as cache:
        cache.get('positive', lambda: RESULTS)
        cache.get('negative', lambda: [])
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        lifetimes = [row[0] for row in conn.execute('SELECT extract(epoch FROM expires_at-fetched_at) FROM enrichment.brave_query_cache')]
        assert sorted(lifetimes) == [3600, 180 * 86400]


def test_concurrent_jobs_make_only_one_api_request_for_a_shared_cache_miss():
    dsn = os.environ['TEST_DATABASE_URL']; barrier = Barrier(2); lock = Lock(); calls = []
    with psycopg.connect(dsn) as conn:
        conn.execute('DROP TABLE enrichment.brave_query_cache')
    def fetch():
        with lock: calls.append(1)
        time.sleep(.05)
        return RESULTS
    def job():
        with BraveQueryCache(dsn) as cache:
            barrier.wait(timeout=5)
            return cache.get('shared query', fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: job(), range(2)))
    assert results == [RESULTS, RESULTS] and len(calls) == 1


def test_cache_key_includes_request_semantics_and_contains_no_api_secret():
    request = request_parameters('query')
    for change in [{'country': 'us'}, {'search_lang': 'fr'}, {'count': 20}, {'q': 'other query'}, {'version': 2}]:
        assert cache_key(request) != cache_key({**request, **change})
    assert 'secret' not in str(request)


def test_collaboration_skips_accepted_items_before_limit_and_explicit_refresh_includes_them(receipt, monkeypatch):
    from home_budget_pipeline import receipt_collaboration as collab
    from home_budget_pipeline.receipt_model_providers import Profile
    service,_,_,items,_,_ = receipt
    accept(receipt)
    seen = []
    monkeypatch.setattr(collab, 'collaboration_profiles', lambda: ([Profile('qwen', 'qwen', 'test')], []))
    monkeypatch.setattr(collab.shared, 'load', lambda *args: {})
    monkeypatch.setattr(core, 'accepted_search_priors', lambda *args: [])
    def process(row, *args):
        seen.append(row['id'])
        return {'decision': {'disposition': 'review'}, 'blocking_errors': [], 'item_seconds': .01}
    monkeypatch.setattr(collab, 'collaborate_item', process)
    kwargs = dict(dsn=os.environ['TEST_DATABASE_URL'], api_key='secret', limit=1, threshold=.85, write_db=False, item_ids=tuple(items))
    monkeypatch.setenv('ENRICHMENT_REFRESH', '0')
    assert collab.run(**kwargs)['considered'] == 1 and seen == [items[1]]
    monkeypatch.setenv('ENRICHMENT_REFRESH', '1')
    assert collab.run(**kwargs)['considered'] == 1 and seen == [items[1], items[0]]
    assert service._fetch('SELECT product_description FROM budget.expense_items WHERE id=%s', (items[1],))[0]['product_description'] is None


def test_original_enrichment_skips_accepted_items_and_refresh_bypasses_accepted_cache(receipt, monkeypatch):
    service,_,_,items,_,_ = receipt
    accept(receipt)
    calls = []
    monkeypatch.setattr(core, '_fetch_brave_query', lambda *args, **kwargs: calls.append(args) or RESULTS)
    kwargs = dict(dsn=os.environ['TEST_DATABASE_URL'], api_key='secret', limit=1, threshold=.85,
                  write_db=False, item_ids=tuple(items))
    monkeypatch.setenv('ENRICHMENT_REFRESH', '0')
    assert core.run(**kwargs)['db_hits'] == 1 and not calls
    monkeypatch.setenv('ENRICHMENT_REFRESH', '1')
    assert core.run(**kwargs)['brave_api_requests'] == 1 and len(calls) == 1
    assert service._fetch('SELECT product_description FROM budget.expense_items WHERE id=%s', (items[1],))[0]['product_description'] is None
