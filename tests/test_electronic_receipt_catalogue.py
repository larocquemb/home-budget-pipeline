import os

import psycopg
import pytest
from psycopg.rows import dict_row

from home_budget_pipeline.web.receipt_app import _receipt_detail, _receipt_list
from home_budget_pipeline.receipts import electronic


pytestmark = pytest.mark.integration


def test_imported_evidence_is_listed_and_opened_without_an_ocr_entry():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        class Service:
            def _fetch(self, sql, params=()):
                return conn.execute(sql, params).fetchall()

            def receipt_evidence(self, evidence_id):
                return conn.execute('SELECT * FROM budget.receipt_evidence WHERE id=%s',
                                    (evidence_id,)).fetchone()

        sha = 'e' * 64
        conn.execute('''INSERT INTO budget.receipt_evidence
            (evidence_type,source_reference,source_sha256,merchant,total,extraction_status)
            VALUES ('electronic','Costco/catalogue-test.pdf',%s,'Costco',12.34,'complete')''', (sha,))
        service = Service()
        rows = _receipt_list(service, limit=10, offset=0, q='catalogue-test')
        assert rows.total_count == 1
        assert rows.rows[0]['source_sha256'] == sha
        detail = _receipt_detail(service, sha)
        assert detail['evidence']['evidence_type'] == 'electronic'
        assert detail['processing_status'] == 'imported'
        assert detail['evidence']['source_reference'] == 'Costco/catalogue-test.pdf'

        # Registering the same artifact for OCR must not duplicate the listing.
        conn.execute('''INSERT INTO ingest.receipts (source_sha256,source_reference)
                        VALUES (%s,'Costco/catalogue-test.pdf')''', (sha,))
        assert _receipt_list(service, limit=10, offset=0, q='catalogue-test').total_count == 1
        # A newly discovered scan is still visible before extraction.
        pending = 'f' * 64
        conn.execute('''INSERT INTO ingest.receipts (source_sha256,source_reference)
                        VALUES (%s,'pending-catalogue-test.pdf')''', (pending,))
        assert _receipt_list(service, limit=10, offset=0, q='catalogue-test').total_count == 2
        conn.rollback()


def test_electronic_import_preserves_edits_and_reports_partial_and_empty_files(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', os.environ['TEST_DATABASE_URL'])
    texts = {
        '20260101.pdf': '123456 MILK 5.00 N\nSUBTOTAL 5.00\n**** TOTAL 5.00',
        '20260102.pdf': '123456 MILK 5.00 N\nSUBTOTAL 10.00\n**** TOTAL 10.00',
        '20260103.pdf': '',
    }
    for name, text in texts.items():
        (tmp_path / name).write_text(text)
    def read_pdf(path):
        if not texts[path.name]:
            raise ValueError('empty file')
        return texts[path.name]
    monkeypatch.setattr(electronic.costco, 'pdf_text', read_pdf)
    monkeypatch.setattr(electronic.costco, 'try_ai_category_batch', lambda _: {})
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        group = conn.execute("INSERT INTO budget.category_groups (group_key,group_name) VALUES ('electronic-test','Electronic test') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO budget.expense_categories (category_name,group_id) VALUES ('Groceries',%s) ON CONFLICT DO NOTHING", (group,))
    first = electronic.import_receipts(tmp_path)
    assert first['imported'] == 2
    assert first['review'] == [{'file': '20260102.pdf', 'subtotal': 10.0, 'item_sum': 5.0}]
    assert first['unreadable'] == [{'file': '20260103.pdf', 'error': 'ValueError'}]
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        conn.execute("UPDATE budget.expense_items SET item_name='Human corrected milk' WHERE expense_pk=(SELECT id FROM budget.expenses WHERE source='costco' AND order_id='20260101')")
    second = electronic.import_receipts(tmp_path)
    assert second['imported'] == 0
    assert second['skipped'] == 2
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        assert conn.execute("SELECT item_name FROM budget.expense_items WHERE expense_pk=(SELECT id FROM budget.expenses WHERE source='costco' AND order_id='20260101')").fetchone()[0] == 'Human corrected milk'
        assert conn.execute("SELECT extraction_status FROM budget.receipt_evidence WHERE receipt_id='20260102'").fetchone()[0] == 'partial'
