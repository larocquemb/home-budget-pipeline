from contextlib import nullcontext
from decimal import Decimal
from datetime import date
import pytest
from fastapi.testclient import TestClient

from home_budget_pipeline.web.receipt_app import app, BASE_PATH, query_service
from home_budget_pipeline.web import transaction_workflows as web


@pytest.fixture
def client(monkeypatch):
    class Service:
        def _connect(self):
            return nullcontext(object())
    app.dependency_overrides[query_service] = Service
    monkeypatch.setattr(web,"accounting_service",Service)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_save_requires_auth_action_and_records_identity(client, monkeypatch):
    calls=[]
    monkeypatch.setattr(web,'save_allocations',lambda *args,**kw: calls.append((args,kw)) or {'version':1})
    url=f'{BASE_PATH}/api/transactions/8/allocations'
    payload={'expected_version':0,'reason':'Verified','allocations':[{'amount':'1.00','category':'Food'}]}
    assert client.post(url,json=payload).status_code == 401
    assert client.post(url,json=payload,headers={'X-Forwarded-User':'reviewer'}).status_code == 403
    response=client.post(url,json=payload,headers={'X-Forwarded-User':'reviewer','X-Ledger-Action':'transaction-allocation'})
    assert response.status_code == 200
    assert calls[0][0][4] == 'reviewer'
    assert calls[0][1]['apply'] is True
    assert 'receipt_service' in calls[0][1]


@pytest.mark.parametrize('error,status',[(ValueError('Allocations changed; reload'),409),(ValueError('Invalid split'),422),(LookupError('Missing'),404)])
def test_save_reports_rejected_changes(client,monkeypatch,error,status):
    def fail(*args,**kw):raise error
    monkeypatch.setattr(web,'save_allocations',fail)
    response=client.post(f'{BASE_PATH}/api/transactions/8/allocations',json={'expected_version':0,'reason':'Verified','allocations':[{}]},
                         headers={'X-Forwarded-User':'reviewer','X-Ledger-Action':'transaction-allocation'})
    assert response.status_code == status


def test_detail_escapes_bank_and_split_fields_and_payment_has_no_expense_form(monkeypatch):
    tx={'description':'<script>unsafe</script>','transaction_date':date(2026,3,12),'amount':Decimal('12.34'),
        'currency':'CAD','card_last4':'5678','raw_source':{'kind':'purchase'}}
    monkeypatch.setattr(web,'detail',lambda *args:(tx,[],0))
    monkeypatch.setattr(web,'receipt_candidates',lambda *args:())
    class Service:
        def _fetch(self,*args):return ()
    monkeypatch.setattr(web,'accounting_service',Service)
    text=web.transaction_page(8,service=Service(),identity={'user':'reviewer'})
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in text
    assert 'Add split' in text
    tx['raw_source']['kind']='payment'
    text=web.transaction_page(8,service=Service(),identity={'user':'reviewer'})
    assert 'allocation-form' not in text
