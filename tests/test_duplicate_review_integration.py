"""Real PostgreSQL duplicate decisions on explicitly isolated CI/test services."""
import os
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest

from home_budget_pipeline.receipts.duplicate_review import capture_candidates, resolve

pytestmark = pytest.mark.integration


def test_existing_purchase_merge_is_audited_and_excluded_from_spend():
    from home_budget_pipeline.receipts.expense_merge import merge
    from home_budget_pipeline import db_setup
    dsn = os.environ['TEST_DATABASE_URL']
    connect = lambda: psycopg.connect(dsn,row_factory=dict_row)
    expenses, shas = [], [uuid.uuid4().hex*2 for _ in range(2)]
    try:
        with connect() as conn:
            conn.execute(db_setup.ANALYTICS_SCHEMA.read_text())
            for index,sha in enumerate(shas):
                total = 24.60 if index == 0 else 24.68
                pk = conn.execute("INSERT INTO budget.expenses(source,order_id,receipt_item_subtotal,expense_total) VALUES ('scanned',%s,%s,%s) RETURNING id",
                    ('scan:'+sha,24.60,total)).fetchone()['id']
                expenses.append(pk)
                conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name,line_total,budget_category) VALUES (%s,'Synthetic product',%s,'Groceries')",(pk,total))
                conn.execute("INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256,merchant,total,raw_text) VALUES (%s,'scanned',%s,%s,'Synthetic Shop',%s,'Original OCR')",(pk,sha+'.pdf',sha,total))
            original_items = conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=ANY(%s) ORDER BY id',(expenses,)).fetchall()
        args = {'reason':'Verified synthetic duplicate','actor':'Test reviewer','auto_select':True}
        preview = merge(connect,*expenses,**args)
        assert preview['keeper'] == expenses[0]
        result = merge(connect,*expenses,**args,apply=True,expected_version=preview['version'])
        with connect() as conn:
            assert conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=ANY(%s) ORDER BY id',(expenses,)).fetchall() == original_items
            assert conn.execute('SELECT expense_pk FROM budget.analytics_expenses WHERE expense_pk=ANY(%s)',(expenses,)).fetchall() == [{'expense_pk':expenses[0]}]
            assert conn.execute('SELECT count(*) AS n FROM budget.analytics_expense_items WHERE expense_pk=%s',(expenses[1],)).fetchone()['n'] == 0
            evidence = conn.execute('SELECT expense_pk,raw_text FROM budget.receipt_evidence WHERE source_sha256=ANY(%s)',(shas,)).fetchall()
            assert all(e['expense_pk'] == expenses[0] and e['raw_text'] == 'Original OCR' for e in evidence)
            assert conn.execute('SELECT count(*) AS n FROM lineage.receipt_events WHERE id=%s',(result['audit_id'],)).fetchone()['n'] == 1
        assert merge(connect,*reversed(expenses),**args,apply=True,expected_version='old')['unchanged']
    finally:
        with connect() as conn:
            conn.execute('DELETE FROM lineage.receipt_events WHERE source_sha256=ANY(%s)',(shas,))
            conn.execute('DELETE FROM budget.receipt_evidence WHERE source_sha256=ANY(%s)',(shas,))
            conn.execute('DELETE FROM budget.expenses WHERE id=ANY(%s)',(expenses,))


@pytest.mark.parametrize('action', ['confirm', 'reject', 'auto'])
def test_real_duplicate_decision_preserves_purchase_and_originals(action):
    dsn = os.environ['TEST_DATABASE_URL']
    connect = lambda: psycopg.connect(dsn, row_factory=dict_row)
    shas = [uuid.uuid4().hex * 2 for _ in range(2)]
    ids = []
    expense = None
    try:
        with connect() as conn:
            expense = conn.execute("INSERT INTO budget.expenses(source,order_id,expense_total) VALUES ('scanned',%s,12.34) RETURNING id",
                                   (str(uuid.uuid4()),)).fetchone()['id']
            conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name,line_total) VALUES (%s,'Synthetic item',12.34)", (expense,))
            for index, sha in enumerate(shas):
                row = conn.execute('''INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,
                    source_sha256,merchant,transaction_datetime,total,receipt_id,card_last4,raw_text)
                    VALUES (%s,'electronic',%s,%s,'Synthetic shop','2026-01-02 12:30:00',12.34,'INV-123456','1234','Original test text') RETURNING id''',
                    (expense if index == 0 else None, sha+'.pdf', sha)).fetchone()
                ids.append(row['id'])
            before_items = conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s', (expense,)).fetchall()
            before_expense = conn.execute('SELECT * FROM budget.expenses WHERE id=%s', (expense,)).fetchone()
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('budget.receipt-duplicates'))")
            assert capture_candidates(conn, ids[1], allow_auto=action == 'auto')
        if action != 'auto':
            args = dict(expense=expense if action == 'confirm' else None,
                        reject=action == 'reject', reason='Synthetic original verification', actor='Test Reviewer')
            preview = resolve(connect, *ids, **args)
            with pytest.raises(ValueError, match='changed'):
                resolve(connect, *ids, **args, apply=True, expected_version='a'*64)
            result = resolve(connect, *ids, **args, apply=True, expected_version=preview['version'])
            assert result['resolution_status'] == ('confirmed' if action == 'confirm' else 'rejected')
            assert resolve(connect, *ids, **args, apply=True, expected_version=preview['version'])['unchanged']
        with connect() as conn:
            linked = conn.execute('SELECT expense_pk,raw_text FROM budget.receipt_evidence WHERE id=ANY(%s) ORDER BY id', (ids,)).fetchall()
            assert [r['expense_pk'] for r in linked] == [expense, None if action == 'reject' else expense]
            assert all(r['raw_text'] == 'Original test text' for r in linked)
            assert conn.execute('SELECT * FROM budget.expenses WHERE id=%s', (expense,)).fetchone() == before_expense
            assert conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s', (expense,)).fetchall() == before_items
            assert conn.execute('SELECT count(*) AS n FROM lineage.receipt_events WHERE source_sha256=ANY(%s)', (shas,)).fetchone()['n'] == 1
            if action == 'reject':
                assert not capture_candidates(conn, ids[1], allow_auto=True)
    finally:
        with connect() as conn:
            conn.execute('DELETE FROM lineage.receipt_events WHERE source_sha256=ANY(%s)', (shas,))
            conn.execute('DELETE FROM budget.receipt_evidence WHERE source_sha256=ANY(%s)', (shas,))
            if expense is not None:
                conn.execute('DELETE FROM budget.expenses WHERE id=%s', (expense,))


@pytest.mark.parametrize('shared',[False,True])
def test_receipt_delete_restore_preserves_items_and_active_totals(shared):
    from home_budget_pipeline.web.receipt_deletion import change
    connect = lambda: psycopg.connect(os.environ['TEST_DATABASE_URL'],row_factory=dict_row)
    shas = [uuid.uuid4().hex * 2 for _ in range(2 if shared else 1)]
    expense = None
    try:
        with connect() as conn:
            expense = conn.execute("INSERT INTO budget.expenses(source,order_id,expense_total) VALUES ('scanned',%s,12.34) RETURNING id",(str(uuid.uuid4()),)).fetchone()['id']
            conn.execute("INSERT INTO budget.expense_items(expense_pk,item_name,line_total,budget_category) VALUES (%s,'Synthetic product',12.34,'Groceries')",(expense,))
            for sha in shas:
                conn.execute("INSERT INTO budget.receipt_evidence(expense_pk,evidence_type,source_reference,source_sha256,raw_text) VALUES (%s,'scanned',%s,%s,'Original OCR')",(expense,sha+'.pdf',sha))
            before = conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s',(expense,)).fetchall()
        change(connect,shas[0],deleted=True,actor='Test reviewer')
        with connect() as conn:
            active = conn.execute('SELECT expense_total FROM budget.analytics_expenses WHERE expense_pk=%s',(expense,)).fetchall()
            assert bool(active) == shared
            assert conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s',(expense,)).fetchall() == before
        change(connect,shas[0],deleted=False,actor='Test reviewer')
        with connect() as conn:
            assert conn.execute('SELECT expense_total FROM budget.analytics_expenses WHERE expense_pk=%s',(expense,)).fetchone()['expense_total'] == before[0]['line_total']
            assert conn.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s',(expense,)).fetchall() == before
            assert conn.execute('SELECT raw_text FROM budget.receipt_evidence WHERE source_sha256=%s',(shas[0],)).fetchone()['raw_text'] == 'Original OCR'
            assert conn.execute('SELECT count(*) AS n FROM lineage.receipt_events WHERE source_sha256=%s',(shas[0],)).fetchone()['n'] == 2
    finally:
        with connect() as conn:
            conn.execute('DELETE FROM lineage.receipt_events WHERE source_sha256=ANY(%s)',(shas,))
            conn.execute('DELETE FROM budget.receipt_evidence WHERE source_sha256=ANY(%s)',(shas,))
            if expense is not None:
                conn.execute('DELETE FROM budget.expenses WHERE id=%s',(expense,))
