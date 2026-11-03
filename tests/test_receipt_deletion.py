"""Discarding receipt evidence preserves documents, accounting history and undo."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from home_budget_pipeline.web import receipt_deletion as deletion, receipt_app
from home_budget_pipeline.web.app import app, BASE_PATH, query_service


@pytest.mark.parametrize('deleted,other_active', [(True,False),(True,True),(False,False)])
def test_delete_restore_updates_only_receipt_and_purchase_disposition(deleted, other_active):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    before = {'id':31,'expense_pk':11,'source_reference':'example.pdf',
              'raw_payload':{} if deleted else {'receipt_deleted':{'audit_id':'previous'}}}
    expense = {'id':11,'raw_payload':{'preserved':'metadata'}}
    after = deepcopy(before)
    after['raw_payload'] = {'receipt_deleted':{'audit_id':'new'}} if deleted else {}
    active = [{'id':32,'raw_payload':{}}] if other_active or not deleted else []
    cur.fetchone.side_effect = [before,expense,expense,after]
    cur.fetchall.side_effect = [[{'definition':'receipt_deleted'}]*3, active]
    result = deletion.change(lambda:conn, 'a'*64, deleted=deleted, actor='Test reviewer')
    assert result['deleted'] == deleted
    statements = [(a[0],a[1] if len(a)>1 else ()) for a,_ in cur.execute.call_args_list]
    updates = [(s,p) for s,p in statements if s.startswith('UPDATE')]
    assert len(updates) == 2
    evidence_payload = updates[0][1][0].obj
    expense_payload = updates[1][1][0].obj
    assert ('receipt_deleted' in evidence_payload) == deleted
    assert ('receipt_deleted' in expense_payload) == (deleted and not other_active)
    assert expense_payload['preserved'] == 'metadata'
    assert not any('DELETE FROM' in s or 'UPDATE budget.expense_items' in s for s,_ in statements)
    audit, = [p[-1].obj for s,p in statements if 'INSERT INTO lineage.receipt_events' in s]
    assert audit['before'] == before
    assert audit['event'] == ('receipt_deleted' if deleted else 'receipt_restored')
    assert audit['actor_user'] == 'Test reviewer'
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_audit_failure_rolls_back_discard():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    evidence = {'id':31,'source_reference':'example.png','raw_payload':{}}
    cur.fetchone.return_value = evidence
    cur.fetchall.return_value = [{'definition':'receipt_deleted'}]*3
    def execute(sql,*args):
        if 'INSERT INTO lineage.receipt_events' in sql:
            raise RuntimeError('audit unavailable')
    cur.execute.side_effect = execute
    with pytest.raises(RuntimeError,match='audit unavailable'):
        deletion.change(lambda:conn,'a'*64,deleted=True,actor='Test reviewer')
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_repeat_delete_is_noop():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'id':31,'raw_payload':{'receipt_deleted':{'audit_id':'existing'}}}
    assert deletion.change(lambda:conn,'a'*64,deleted=True,actor='Test reviewer')['unchanged']
    conn.commit.assert_not_called()


def test_pending_receipt_can_be_discarded_before_extraction():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    row = {'id':31,'source_reference':'electronic/example.pdf','raw_payload':{}}
    cur.fetchone.side_effect = [None,{'source_reference':row['source_reference']},row,row]
    cur.fetchall.return_value = [{'definition':'receipt_deleted'}]*3
    deletion.change(lambda:conn,'a'*64,deleted=True,actor='Test reviewer')
    insert, = [a for a,_ in cur.execute.call_args_list if 'INSERT INTO budget.receipt_evidence' in a[0]]
    assert insert[1] == ('a'*64,'electronic/example.pdf','electronic')
    conn.commit.assert_called_once()


def test_reprocessing_deleted_receipt_never_recreates_purchase(monkeypatch):
    from home_budget_pipeline.receipts import parallel_ingest as parallel
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (None,True)
    monkeypatch.setattr(parallel,'resolve_merchant_alias',lambda *a:'Example merchant')
    monkeypatch.setattr(parallel,'upsert_evidence',lambda *a,**k:31)
    canonical = MagicMock()
    monkeypatch.setattr(parallel.scan,'upsert_receipt',canonical)
    result = parallel.persist_evidence_first(conn,[SimpleNamespace(merchant='Example merchant')],'budget')
    assert result['matched'] == 1
    canonical.assert_not_called()


def test_delete_and_restore_require_identity_and_action_header(monkeypatch):
    monkeypatch.delenv('LEDGER_PROXY_SECRET',raising=False)
    service = SimpleNamespace(_connect=lambda:None)
    app.dependency_overrides[query_service] = lambda:service
    calls = []
    monkeypatch.setattr(deletion,'change',lambda connect,sha,**kw:calls.append((sha,kw)) or {'deleted':kw['deleted']})
    try:
        with TestClient(app) as client:
            path = BASE_PATH + '/api/receipts/' + 'a'*64
            for method,url,action in [('DELETE',path,'receipt-delete'),('POST',path+'/restore','receipt-restore')]:
                assert client.request(method,url).status_code == 401
                assert client.request(method,url,headers={'X-Forwarded-User':'test'}).status_code == 403
                response = client.request(method,url,headers={'X-Forwarded-User':'test','X-Ledger-Action':action})
                assert response.status_code == 200
        assert [kw['deleted'] for _,kw in calls] == [True,False]
        assert all(kw['actor'] == 'test' for _,kw in calls)
    finally:
        app.dependency_overrides.pop(query_service,None)


def test_deleted_detail_offers_restore_without_review(monkeypatch):
    data = {'deleted':True,'source_reference':'example.pdf','expense_pk':11,
            'processing_status':'review_required','extraction_status':'review',
            'evidence':{'raw_payload':{'receipt_deleted':{'audit_id':'existing'}},
                        'possible_duplicate_expense_pks':[12]},'expense':{}}
    monkeypatch.setattr(receipt_app,'_receipt_detail',lambda *a:data)
    html = receipt_app.receipt_page('a'*64,service=object(),identity={'user':'test'})
    assert 'Restore receipt' in html and 'Undo' in html
    assert '<strong>Status</strong><br>Deleted' in html
    assert 'Possible duplicate purchase' not in html
    assert 'id="refresh-items"' not in html
    assert 'id="allocation-correction"' not in html
    assert 'Retained in history' in html
