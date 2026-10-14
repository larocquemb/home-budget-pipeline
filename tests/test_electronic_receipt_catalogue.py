import os

import psycopg
import pytest
from psycopg.rows import dict_row

from home_budget_pipeline.web.receipt_app import _receipt_detail, _receipt_list, receipt_page
from home_budget_pipeline.web.queries import LedgerQueryService
from home_budget_pipeline.receipts import electronic


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_import_records():
    # The importer owns committed connections; isolate its test records from
    # other integration tests which seed their own category catalogue.
    tables = ('expenses', 'receipt_evidence', 'expense_categories', 'category_groups')
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True) as conn:
        before = {table: [r[0] for r in conn.execute(f'SELECT id FROM budget.{table}')]
                  for table in tables}
        try:
            yield
        finally:
            for table in tables:
                conn.execute(f'DELETE FROM budget.{table} WHERE NOT (id=ANY(%s::bigint[]))',
                             (before[table],))


def test_imported_evidence_is_listed_and_opened_without_an_ocr_entry():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        class Service:
            def _fetch(self, sql, params=()):
                return conn.execute(sql, params).fetchall()

            def receipt_evidence(self, evidence_id):
                return LedgerQueryService.receipt_evidence(self, evidence_id)

        sha = 'e' * 64
        conn.execute('''INSERT INTO budget.receipt_evidence
            (evidence_type,source_reference,source_sha256,merchant,total,extraction_status,raw_payload)
            VALUES ('electronic','Costco/catalogue-test.pdf',%s,'Costco',12.34,'complete',
                    '{"receipt":{"possible_duplicate_expense_pks":[228]}}')''', (sha,))
        service = Service()
        rows = _receipt_list(service, limit=10, offset=0, q='catalogue-test')
        assert rows.total_count == 1
        assert rows.rows[0]['source_sha256'] == sha
        detail = _receipt_detail(service, sha)
        assert detail['evidence']['evidence_type'] == 'electronic'
        assert detail['processing_status'] == 'imported'
        assert detail['evidence']['source_reference'] == 'Costco/catalogue-test.pdf'
        html = receipt_page(sha, service=service, identity={'user': 'test'})
        assert 'Possible duplicate purchase' in html
        assert '/expenses/228' in html
        from home_budget_pipeline.receipt_graph import build, load
        graph = build(load(conn, sha))
        assert any(node['kind'] == 'Receipt' and node['label'] == 'Costco/catalogue-test.pdf'
                   for node in graph['nodes'])

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


def test_matching_printed_transaction_links_artifact_without_duplicate_spend(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', os.environ['TEST_DATABASE_URL'])
    path = tmp_path / '20260720.pdf'
    path.write_text('unique electronic refund')
    text = '22054912500432607201140\n1981364 WW DRESS 29.99-\nSUBTOTAL 29.99-\nTAX 3.60-\n**** TOTAL 33.59-'
    monkeypatch.setattr(electronic.costco, 'pdf_text', lambda _: text)
    monkeypatch.setattr(electronic.costco, 'try_ai_category_batch', lambda _: {})
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        conn.execute("INSERT INTO budget.category_groups (group_key,group_name) VALUES ('electronic-test','Electronic test') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO budget.expense_categories (category_name,group_id) SELECT 'Clothing',id FROM budget.category_groups WHERE group_key='electronic-test' ON CONFLICT DO NOTHING")
        pk = conn.execute('''INSERT INTO budget.expenses
            (source,order_id,store_name,transaction_datetime,expense_total)
            VALUES ('scanned','electronic-link-test','Costco','2026-07-20 11:40:32',-33.59)
            RETURNING id''').fetchone()[0]
        conn.execute('''INSERT INTO budget.receipt_evidence
            (expense_pk,evidence_type,source_reference,source_sha256,raw_text)
            VALUES (%s,'scanned','refund.pdf',%s,'11:40:34 549 125 43 639')''', (pk, 'd'*64))
        conn.execute("INSERT INTO budget.expense_items (expense_pk,item_name,line_total) VALUES (%s,'Human reviewed dress',-29.99)", (pk,))
        assert electronic.matching_scanned_expense(conn, '22054912600432607201140', -33.59) is None
    result = electronic.import_receipts(tmp_path)
    assert result['imported'] == result['linked_existing'] == 1
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as conn:
        assert conn.execute("SELECT expense_pk FROM budget.receipt_evidence WHERE source_reference='Costco/20260720.pdf'").fetchone()[0] == pk
        assert conn.execute('SELECT item_name FROM budget.expense_items WHERE expense_pk=%s', (pk,)).fetchall() == [('Human reviewed dress',)]
        assert conn.execute("SELECT count(*) FROM budget.expenses WHERE order_id='22054912500432607201140'").fetchone()[0] == 0
