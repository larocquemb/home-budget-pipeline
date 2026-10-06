import pytest
from fastapi.testclient import TestClient

from home_budget_pipeline.web.queries import recommendation_candidate
from home_budget_pipeline.web.receipt_app import (
    BASE_PATH, app, _enrichment_html, _recommendation_html, _receipt_detail, query_service,
)


def payload():
    candidate = {'id': 'product:1', 'title': 'Old El Paso Salsa Picante Medium',
                 'url': 'https://www.sobeys.com/products/salsa', 'query': 'site:sobeys.com picante medium',
                 'confidence': .9167, 'evidence': {'product_page': True,
                 'tokens': [{'token': 'med', 'matched': 'medium', 'weight': .9}]}}
    return {'decision': {'disposition': 'recommended', 'candidate_id': candidate['id'],
                        'candidate_title': candidate['title'], 'candidate_url': candidate['url'],
                        'confidence': .9167, 'canonical_updated': False},
            'search_results': [candidate], 'blocking_errors': [], 'run_uuid': 'run-1',
            'evidence_bundle': {'sources': []}, 'proposals': [], 'reviews': []}


@pytest.mark.parametrize('change', ['review', 'blocked', 'missing', 'recipe', 'url', 'title', 'score'])
def test_acceptance_requires_saved_recommended_product_evidence(change):
    p = payload()
    if change == 'review': p['decision']['disposition'] = 'review'
    if change == 'blocked': p['blocking_errors'] = ['model error']
    if change == 'missing': p['search_results'] = []
    if change == 'recipe': p['search_results'][0]['evidence']['product_page'] = False
    if change == 'url': p['search_results'][0]['url'] = p['decision']['candidate_url'] = 'javascript:alert(1)'
    if change == 'title': p['decision']['candidate_title'] = 'Different product'
    if change == 'score': p['decision']['confidence'] = float('nan')
    with pytest.raises(ValueError): recommendation_candidate(p)


def test_receipt_detail_loads_latest_collaboration_per_item_instead_of_reporting_not_enriched_only():
    class Service:
        def _fetch(self, sql, params=()):
            if 'FROM ingest.receipts r' in sql:
                return [{'source_sha256': 'a'*64, 'evidence_id': 1, 'expense_pk': 2}]
            if 'FROM budget.product_enrichment_results' in sql: return []
            assert 'DISTINCT ON (c.expense_item_id)' in sql
            assert 'c.completed_at DESC,c.id DESC' in sql
            assert params == ('a'*64, [10, 11])
            return [{'id': 3, 'expense_item_id': 10, 'run_uuid': 'run-1', 'completed_at': 'now',
                     'payload': payload(), 'accepted_at': None}]
        def receipt_evidence(self, key): return {'expense_pk': 2}
        def expense_detail(self, key):
            return {'items': [{'expense_item_id': 10}, {'expense_item_id': 11}]}
    items = _receipt_detail(Service(), 'a'*64)['expense']['items']
    assert items[0]['recommendation']['eligible'] is True
    assert items[0]['recommendation']['candidate_title'] == payload()['decision']['candidate_title']
    assert items[1]['recommendation'] is None
    result = _recommendation_html(dict(items[0]), 'a'*64)
    assert 'No accepted match yet' in _enrichment_html(items[0])
    assert 'Saved recommendation' in result and '91.7%' in result
    assert 'Accept recommendation for this item' in result and 'med → medium' in result
    assert 'expected_description' in result and 'collaboration_id' in result


def test_recommendation_ui_escapes_values_and_only_offers_acceptance_when_eligible():
    item = {'expense_item_id': 10, 'recommendation': {'id': 3, 'run_uuid': '<script>',
            'candidate_title': '<img onerror=bad>', 'candidate_url': 'javascript:alert(1)',
            'eligible': False, 'disposition': 'review'}}
    result = _recommendation_html(item, 'a'*64)
    assert '<script>' not in result and '<img' not in result and 'href="javascript:' not in result
    assert 'Accept recommendation for this item' not in result and 'Requires review' in result
    item['recommendation'].update(eligible=True, accepted_at='now')
    assert 'Accepted recommendation' in _recommendation_html(item, 'a'*64)
    assert 'Accept recommendation for this item' not in _recommendation_html(item, 'a'*64)


def test_acceptance_endpoint_authenticates_and_requires_explicit_action_header():
    class Service:
        def accept_product_recommendation(self, *args, **kwargs):
            assert args == ('a'*64, 10, 3)
            assert kwargs == {'expected_description': None, 'expected_url': None,
                             'actor_user': 'paul', 'actor_email': None}
            return {'audit_id': 42}
    app.dependency_overrides[query_service] = lambda: Service()
    try:
        with TestClient(app) as client:
            url = f'{BASE_PATH}/api/receipts/{"a"*64}/accept-recommendation'
            data = {'expense_item_id': 10, 'collaboration_id': 3,
                    'expected_description': None, 'expected_url': None,
                    'candidate_title': 'Forged browser title'}
            assert client.post(url, json=data).status_code == 401
            assert client.post(url, json=data, headers={'X-Forwarded-User': 'paul'}).status_code == 403
            response = client.post(url, json=data, headers={'X-Forwarded-User': 'paul',
                         'X-Ledger-Action': 'accept-product-recommendation'})
            assert response.status_code == 200 and response.json()['audit_id'] == 42
    finally:
        app.dependency_overrides.clear()
