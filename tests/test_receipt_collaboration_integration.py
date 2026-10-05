"""Shared evidence selection and append-only collaboration persistence in PostgreSQL."""
import os
import uuid

import pytest

from home_budget_pipeline import receipt_shared_evidence as shared

pytestmark = pytest.mark.integration


def test_latest_ocr_evidence_and_collaboration_tables_preserve_canonical_item():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        receipt = conn.execute('''INSERT INTO budget.expenses
            (source,order_id,store_name,receipt_filename,receipt_item_subtotal,expense_total)
            VALUES ('scanned',%s,'Sobeys','receipt.pdf',3.49,3.49) RETURNING *''', (str(uuid.uuid4()),)).fetchone()
        item = conn.execute('''INSERT INTO budget.expense_items (expense_pk,item_name,unit_qty,unit_cost,line_total)
            VALUES (%s,'Cep Pic Med',1,3.49,3.49) RETURNING *''', (receipt['id'],)).fetchone()
        source_hash = uuid.uuid4().hex * 2
        evidence = conn.execute('''INSERT INTO budget.receipt_evidence
            (expense_pk,evidence_type,source_reference,source_sha256,raw_text)
            VALUES (%s,'scanned','receipt.pdf',%s,'Oep Pic Med 3.49') RETURNING id''',
            (receipt['id'], source_hash)).fetchone()['id']
        for old in (True, False):
            run = uuid.uuid4()
            conn.execute('''INSERT INTO budget.receipt_ocr_runs
                (run_uuid,evidence_id,source_sha256,source_reference,processed_at)
                VALUES (%s,%s,%s,'receipt.pdf',CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour'))''',
                (run, evidence, source_hash, 1 if old else 0))
            conn.execute('''INSERT INTO budget.receipt_ocr_passes
                (run_uuid,pass_id,page_number,engine,engine_type,variant,status,extracted_text)
                VALUES (%s,1,1,'paddle','traditional_ocr','raw','success',%s)''',
                (run, 'Incorrect old reading' if old else 'Oep Pic Med 3.49'))
        row = {**receipt, **item, 'receipt_id': receipt['id']}
        result = shared.load(conn, row)
        assert result['coverage']['available_passes'] == 1
        assert any(s.get('engine') == 'paddle' for s in result['sources'])
        assert all('Incorrect old' not in s['text'] for s in result['sources'])
        run = uuid.uuid4()
        payload = {'evidence_bundle': result, 'decision': {'disposition': 'review', 'canonical_updated': False}}
        conn.execute('''INSERT INTO enrichment.receipt_collaborations
            (run_uuid,expense_item_id,payload) VALUES (%s,%s,%s)''', (run, item['id'], Jsonb(payload)))
        conn.execute('INSERT INTO enrichment.receipt_collaboration_runs (run_uuid,summary) VALUES (%s,%s)',
                     (run, Jsonb({'receipts': [receipt['id']]})))
        stored = conn.execute('SELECT payload FROM enrichment.receipt_collaborations WHERE run_uuid=%s', (run,)).fetchone()
        assert stored['payload']['decision']['canonical_updated'] is False
        assert conn.execute('SELECT item_name FROM budget.expense_items WHERE id=%s', (item['id'],)).fetchone()['item_name'] == 'Cep Pic Med'
        conn.rollback()
