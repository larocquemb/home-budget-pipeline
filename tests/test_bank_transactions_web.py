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


def test_refresh_button_requires_identity_action_and_runs_immediate_manual_scan(client,monkeypatch):
    from home_budget_pipeline.transactions import discovery
    calls=[]
    monkeypatch.setattr(discovery,'scan',lambda *args,**kw:calls.append((args,kw)) or {'new_transactions':0,'failed':0,'waiting':0,'unchanged':10})
    url=f'{BASE_PATH}/api/transaction-imports/refresh'
    assert client.post(url).status_code==401
    assert client.post(url,headers={'X-Forwarded-User':'reviewer'}).status_code==403
    result=client.post(url,headers={'X-Forwarded-User':'reviewer','X-Ledger-Action':'transaction-import'})
    assert result.status_code==200
    assert calls[0][0][2]=='reviewer'
    assert calls[0][1]['immediate'] is True


def test_cad_ledger_renders_missing_rates_without_zero_conversion(client,monkeypatch):
    from home_budget_pipeline.transactions import ledger
    row={'transaction_id':8,'bank_date':date(2026,3,12),'account':'Example','account_last4':'1234','description':'<script>unsafe</script>',
         'original_amount':Decimal('10.25'),'currency':'USD','amount_cad':None,'fx_rate':None,'rate_date':None,'conversion_status':'missing_rate'}
    monkeypatch.setattr(ledger,'ledger_rows',lambda *args,**kw:{'rows':[row],'total':1,'missing_rates':1})
    response=client.get(f'{BASE_PATH}/cad-ledger',headers={'X-Forwarded-User':'reviewer'})
    assert response.status_code==200
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in response.text
    assert '$0.00' not in response.text
    assert 'Update CAD rates' in response.text and 'Import / refresh transactions' in response.text
    assert f'href="{BASE_PATH}/transactions/8"' in response.text


def test_receipt_links_render_confirmed_transactions_and_survive_accounting_outage():
    from home_budget_pipeline.web.receipt_bank_links import render
    class Service:
        def receipt_bank_links(self, sha):
            assert sha == 'a'*64
            return [{'transaction_id':8,'description':'<unsafe>','linked_amount':Decimal('4.25'),
                     'bank_amount':Decimal('12.34'),'currency':'CAD'}]
    text=render(Service(), 'a'*64)
    assert f'href="{BASE_PATH}/transactions/8"' in text
    assert '$4.25' in text and '$12.34' in text and '&lt;unsafe&gt;' in text
    class Unavailable:
        def receipt_bank_links(self, sha):
            raise ValueError('private connection details')
    text=render(Unavailable(), 'a'*64)
    assert 'unavailable' in text and 'private connection details' not in text


def test_candidate_selection_and_saved_receipt_source_are_available_in_each_split():
    text=web.allocation_html({'receipt_source_sha256':'b'*64}, [
        {'expense_pk':17,'merchant':'<market>','transaction_datetime':date(2026,3,12),'total':Decimal('12.34')}])
    assert 'data-receipt-choice' in text and 'value="17" data-receipt-source="">&lt;market&gt;' in text
    assert f'href="{BASE_PATH}/receipts/{"b"*64}"' in text


def test_accounts_link_to_exact_account_and_keep_selection_when_paging(client, monkeypatch):
    from home_budget_pipeline.transactions import accounts, ledger
    monkeypatch.setattr(accounts, 'account_rows', lambda *args: [
        {'id':7,'nickname':'House'}, {'id':8,'nickname':'House US'}])
    headers={'X-Forwarded-User':'reviewer'}
    response=client.get(f'{BASE_PATH}/accounts',headers=headers)
    assert f'href="{BASE_PATH}/cad-ledger?account_id=7">View transactions</a>' in response.text
    assert f'href="{BASE_PATH}/cad-ledger?account_id=8">View transactions</a>' in response.text
    calls=[]
    monkeypatch.setattr(ledger,'ledger_rows',lambda *args,**kw:calls.append(kw) or {'rows':[],'total':101,'missing_rates':0})
    response=client.get(f'{BASE_PATH}/cad-ledger?account_id=7&from_date=2026-01-01&direction=asc',headers=headers)
    assert response.status_code==200 and calls[0]['account_id']==7
    assert '<input type="hidden" name="account_id" value="7">' in response.text
    assert 'offset=100' in response.text and 'account_id=7' in response.text
    assert 'All accounts</a>' in response.text


def test_receipt_categories_are_read_from_items_and_linked_category_is_not_editable():
    from home_budget_pipeline.web.receipt_categories import render
    from home_budget_pipeline.transactions.workflows import normalize_allocations
    row={'amount':'12.34','expense_pk':17,'category':'Unrelated category'}
    assert normalize_allocations([row],Decimal('12.34'))[0]['category']=='Receipt line items'
    text=web.allocation_html(row,())
    assert 'value="Receipt line items"' in text and 'maxlength="200" readonly' in text
    class Service:
        def _fetch(self,sql,params):
            assert params==([17],)
            return [{'expense_pk':17,'category':'Groceries','item_amount':Decimal('7.34'),'missing_amounts':0},
                    {'expense_pk':17,'category':'Uncategorized','item_amount':Decimal('4.00'),'missing_amounts':1}]
        def expense_detail(self, pk):
            return {'expense_total':Decimal('12.34'),'items':[
                {'item_name':'<source item>','product_description':'Accepted label','budget_category':'Groceries','line_total':Decimal('7.34')}]}
    text=render(Service(), [{'expense_pk':17}], [{'expense_pk':17}])
    assert text.count('$7.34')==2 and 'Uncategorized' in text
    assert 'Receipt 17</a> — line items' in text and '&lt;source item&gt;' in text and 'Accepted label' in text
    assert 'whole receipt' in text and 'not distributed' in text
    assert f'href="{BASE_PATH}/expenses/17"' in text
