import pytest
from fastapi.testclient import TestClient

from home_budget_pipeline.web.allocation_corrections import AllocationConflict
from home_budget_pipeline.web.allocation_html import allocation_html
from home_budget_pipeline.web.receipt_app import BASE_PATH, app, query_service


@pytest.fixture
def client():
    class Service:
        failure = None

        def __init__(self):
            self.calls = []

        def receipt_allocation(self, sha):
            return {'version': 'b' * 64}

        def correct_receipt_allocation(self, sha, version, plan, **actor):
            if self.failure:
                raise self.failure
            self.calls.append((sha, version, plan, actor))
            return {'audit_id': 'synthetic-audit'}

    service = Service()
    app.dependency_overrides[query_service] = lambda: service
    try:
        with TestClient(app) as test_client:
            yield test_client, service
    finally:
        app.dependency_overrides.clear()


def request_data():
    return {'expected_version': 'b' * 64, 'merchant': 'Synthetic Shop',
            'subtotal': '10.00', 'reason': 'Checked original',
            'items': [{'id': 1, 'item_name': 'Product', 'line_total': '10.00'}]}


URL = f'{BASE_PATH}/api/receipts/{"a" * 64}/allocation'
HEADERS = {'X-Forwarded-User': 'reviewer',
           'X-Ledger-Action': 'correct-receipt-allocation'}


def test_allocation_endpoint_requires_identity_and_explicit_action(client):
    http, service = client
    assert http.get(URL).status_code == 401
    assert http.post(URL, json=request_data()).status_code == 401
    assert http.post(URL, json=request_data(), headers={'X-Forwarded-User': 'reviewer'}).status_code == 403
    assert service.calls == []
    assert http.get(URL, headers=HEADERS).json()['version'] == 'b' * 64
    assert http.post(URL, json=request_data(), headers=HEADERS).status_code == 200
    sha, version, plan, actor = service.calls[0]
    assert (sha, version) == ('a' * 64, 'b' * 64)
    assert 'expected_version' not in plan
    assert actor == {'actor_user': 'reviewer', 'actor_email': None}


@pytest.mark.parametrize('failure,status', [(AllocationConflict('Reload'), 409),
    (LookupError('Missing'), 404), (ValueError('Invalid amount'), 422)])
def test_allocation_endpoint_reports_conflict_or_invalid_plan(client, failure, status):
    http, service = client
    service.failure = failure
    assert http.post(URL, json=request_data(), headers=HEADERS).status_code == status


def test_allocation_endpoint_rejects_total_changes_and_invalid_identity(client):
    http, service = client
    data = request_data()
    data['expense_total'] = '11.00'
    assert http.post(URL, json=data, headers=HEADERS).status_code == 422
    assert http.post(URL.replace('a' * 64, 'invalid'), json=request_data(), headers=HEADERS).status_code == 422
    assert service.calls == []


def test_allocation_form_escapes_names_and_shows_unreconciled_items():
    state = {'version': 'b' * 64, 'snapshot': {
        'expense': {'store_name': '<script>bad</script>', 'expense_total': '10.00'},
        'items': [{'id': 1, 'item_name': '\"><img src=x onerror=bad>', 'line_total': '9.00'}]},
        'assessment': {'reconciled': False, 'item_total': '9.00', 'subtotal': '10.00',
                       'item_difference': '-1.00', 'header_difference': '0.00',
                       'reasons': ['item_total_mismatch']}}
    html = allocation_html(state, 'a' * 64)
    assert '<script>' not in html and '<img' not in html
    assert 'Needs allocation review' in html
    assert 'name="expense_total"' not in html
    assert 'name="reason"' in html and 'data-version=' in html
