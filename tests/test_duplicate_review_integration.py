"""Real PostgreSQL duplicate decisions on explicitly isolated CI/test services."""
import os
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest

from home_budget_pipeline.receipts.duplicate_review import capture_candidates, resolve

pytestmark = pytest.mark.integration


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
