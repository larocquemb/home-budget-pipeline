from datetime import datetime
import pytest

from test_receipt_recommendation_acceptance_db import receipt as base_receipt
from home_budget_pipeline.receipts.date_correction import correct
from home_budget_pipeline.receipts import ingest as scan
from home_budget_pipeline.receipts.evidence import upsert_evidence
from home_budget_pipeline.receipts.parallel_ingest import _persist_transaction_datetime

pytestmark = pytest.mark.integration


@pytest.fixture
def receipt(base_receipt):
    try:
        yield base_receipt
    finally:
        service, sha, pk, *_ = base_receipt
        # Restore the relation so the shared fixture also removes unlinked evidence.
        with service._connect() as conn:
            conn.execute('UPDATE budget.receipt_evidence SET expense_pk=%s WHERE source_sha256=%s', (pk, sha))


def repair(receipt, **options):
    service, sha, *_ = receipt
    return correct(service._connect, sha, '2026-07-09T11:25', 'Verified synthetic source',
                   actor_user='test-operator', **options)


@pytest.mark.parametrize('linked', [True, False])
def test_date_repair_is_audited_and_preserves_ocr_amounts_and_categories(receipt, linked):
    service, sha, pk, *_ = receipt
    if not linked:
        with service._connect() as conn:
            conn.execute('UPDATE budget.receipt_evidence SET expense_pk=NULL WHERE source_sha256=%s', (sha,))
    before = service._fetch('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s', (sha,))[0]
    items = service._fetch('SELECT * FROM budget.expense_items WHERE expense_pk=%s ORDER BY id', (pk,))
    preview = repair(receipt)
    assert preview['apply'] is False
    saved = repair(receipt, expected_version=preview['version'], apply=True)
    after = service._fetch('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s', (sha,))[0]
    assert after['transaction_datetime'] == datetime(2026, 7, 9, 11, 25)
    for key in ('raw_text', 'raw_payload', 'page_text', 'total', 'source_sha256', 'expense_pk'):
        assert after[key] == before[key]
    assert service._fetch('SELECT * FROM budget.expense_items WHERE expense_pk=%s ORDER BY id', (pk,)) == items
    expense = service._fetch('SELECT * FROM budget.expenses WHERE id=%s', (pk,))[0]
    assert str(expense['expense_total']) == '12.98'
    assert (expense['transaction_datetime'] is not None) == linked
    audit = service._fetch('SELECT payload FROM lineage.receipt_events WHERE id=%s', (saved['audit_id'],))[0]['payload']
    assert audit['event'] == 'receipt_date_corrected' and audit['before']['evidence']['transaction_datetime'] is None
    with pytest.raises(ValueError, match='changed'):
        repair(receipt, expected_version=preview['version'], apply=True)


def test_corrected_dates_survive_later_scanned_import(receipt):
    service, sha, pk, *_ = receipt
    with service._connect() as conn:
        conn.execute("UPDATE budget.expenses SET source='scanned',order_id=%s WHERE id=%s", ('scan:'+sha, pk))
    preview = repair(receipt)
    repair(receipt, expected_version=preview['version'], apply=True)
    r = scan.ScannedReceipt('synthetic.pdf', 'acceptance.pdf', sha, 'Shop', '2036-01-01', 'test',
                            12.98, 0, 12.98, items=[scan.ScannedItem('Product', 12.98)])
    r.transaction_datetime = '2036-01-01T00:00:00'
    import psycopg, os
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        scan.upsert_receipt(conn, r)
        _persist_transaction_datetime(conn, r, 'budget')
        upsert_evidence(conn, r, expense_pk=pk)
    expense = service._fetch('SELECT order_date,transaction_datetime,raw_payload FROM budget.expenses WHERE id=%s', (pk,))[0]
    assert str(expense['order_date']) == '2026-07-09'
    assert expense['transaction_datetime'] == datetime(2026, 7, 9, 11, 25)
    assert expense['raw_payload']['receipt_date_correction']
    assert service._fetch('SELECT transaction_datetime FROM budget.receipt_evidence WHERE source_sha256=%s', (sha,))[0]['transaction_datetime'] == datetime(2026, 7, 9, 11, 25)


def test_date_audit_failure_rolls_back_evidence_expense_and_feedback(receipt):
    service, sha, *_ = receipt
    preview = repair(receipt)
    with service._connect() as conn:
        conn.execute("CREATE FUNCTION lineage.reject_date_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.payload->>'event'='receipt_date_corrected' THEN RAISE EXCEPTION 'date audit failure'; END IF; RETURN NEW; END $$")
        conn.execute('CREATE TRIGGER reject_date_test BEFORE INSERT ON lineage.receipt_events FOR EACH ROW EXECUTE FUNCTION lineage.reject_date_test()')
    try:
        with pytest.raises(Exception, match='date audit failure'):
            repair(receipt, expected_version=preview['version'], apply=True)
        assert repair(receipt)['version'] == preview['version']
    finally:
        with service._connect() as conn:
            conn.execute('DROP TRIGGER reject_date_test ON lineage.receipt_events')
            conn.execute('DROP FUNCTION lineage.reject_date_test()')
