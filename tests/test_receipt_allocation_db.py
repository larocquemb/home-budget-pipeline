import os
import uuid

import psycopg
import pytest

from test_receipt_recommendation_acceptance_db import receipt
from home_budget_pipeline.web.allocation_corrections import AllocationConflict
from home_budget_pipeline.receipts import ingest as scan

pytestmark = pytest.mark.integration


def plan(receipt):
    service, sha, _, items, *_ = receipt
    state = service.receipt_allocation(sha)
    return state['version'], {'merchant': 'Reviewed Shop', 'subtotal': '12.98', 'gst': '0', 'pst': '0',
        'included_tax': '.62', 'reason': 'Verified against the synthetic original',
        'items': [{'id': i, 'item_name': 'Reviewed product', 'line_total': '6.49'} for i in items]}


def save(receipt, version, values):
    return receipt[0].correct_receipt_allocation(receipt[1], version, values, actor_user='reviewer')


def test_correction_keeps_total_identity_evidence_and_review_history(receipt):
    service, sha, pk, items, collaboration, *_ = receipt
    before = service._fetch('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s', (sha,))[0]
    version, values = plan(receipt)
    result = save(receipt, version, values)
    assert result['assessment']['reconciled'] and result['item_count'] == 2
    e = service._fetch('SELECT * FROM budget.expenses WHERE id=%s', (pk,))[0]
    assert str(e['expense_total']) == '12.98' and e['order_id'] == sha
    assert str(e['total_recon_diff']) == '0.00'
    after = service._fetch('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s', (sha,))[0]
    assert all(after[k] == before[k] for k in ('raw_text', 'raw_payload', 'source_sha256', 'total'))
    assert service._fetch('SELECT 1 FROM enrichment.receipt_collaborations WHERE id=%s', (collaboration,))
    audit = service._fetch("SELECT payload FROM lineage.receipt_events WHERE id=%s", (result['audit_id'],))[0]['payload']
    assert audit['before']['expense']['store_name'] == 'Sobeys'
    assert audit['after']['expense']['store_name'] == 'Reviewed Shop'
    with pytest.raises(AllocationConflict):
        save(receipt, version, values)


def test_unbalanced_correction_and_cross_receipt_item_rejected(receipt):
    version, values = plan(receipt)
    values['items'][0]['line_total'] = '6.00'
    with pytest.raises(ValueError, match='reconcile'):
        save(receipt, version, values)
    values['items'][0]['line_total'] = '6.49'
    values['items'][0]['id'] = 999999999
    with pytest.raises(ValueError, match='belong'):
        save(receipt, version, values)


def test_cannot_remove_item_with_model_history(receipt):
    version, values = plan(receipt)
    values['items'] = [{'id': None, 'item_name': 'Combined', 'line_total': '12.98'}]
    with pytest.raises(AllocationConflict, match='history'):
        save(receipt, version, values)
    assert len(receipt[0].receipt_allocation(receipt[1])['snapshot']['items']) == 2


def test_failed_audit_rolls_back_correction(receipt):
    service, sha, pk, *_ = receipt
    with service._connect() as conn:
        conn.execute("CREATE FUNCTION lineage.reject_allocation_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.payload->>'event'='receipt_allocation_corrected' THEN RAISE EXCEPTION 'audit failure'; END IF; RETURN NEW; END $$")
        conn.execute('CREATE TRIGGER reject_allocation_test BEFORE INSERT ON lineage.receipt_events FOR EACH ROW EXECUTE FUNCTION lineage.reject_allocation_test()')
    version, values = plan(receipt)
    try:
        with pytest.raises(Exception, match='audit failure'):
            save(receipt, version, values)
        assert service.receipt_allocation(sha)['version'] == version
    finally:
        with service._connect() as conn:
            conn.execute('DROP TRIGGER reject_allocation_test ON lineage.receipt_events')
            conn.execute('DROP FUNCTION lineage.reject_allocation_test()')


def test_verified_canonical_allocation_survives_later_scanned_import(receipt):
    service, sha, pk, *_ = receipt
    with service._connect() as conn:
        conn.execute("UPDATE budget.expenses SET source='scanned',order_id=%s WHERE id=%s", ('scan:'+sha, pk))
    version, values = plan(receipt)
    save(receipt, version, values)
    before = service.receipt_allocation(sha)
    r = scan.ScannedReceipt('synthetic.pdf', 'acceptance.pdf', sha, 'Wrong OCR', '2026-01-01', 'test',
                            999, 0, 999, items=[scan.ScannedItem('Wrong OCR', 999)])
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        assert scan.upsert_receipt(conn, r) == pk
    assert service.receipt_allocation(sha)['snapshot']['items'] == before['snapshot']['items']
    assert service._fetch('SELECT store_name FROM budget.expenses WHERE id=%s', (pk,))[0]['store_name'] == 'Reviewed Shop'
