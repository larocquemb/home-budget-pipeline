import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from home_budget_pipeline.web.receipt_app import _receipt_list

pytestmark = pytest.mark.integration


def test_month_uses_purchase_date_not_source_folder_for_scanned_and_electronic_receipts():
    with psycopg.connect(os.environ['TEST_DATABASE_URL'], row_factory=dict_row) as conn:
        class Service:
            def _fetch(self, sql, params=()):
                return conn.execute(sql, params).fetchall()
        prefix = 'month-test-' + uuid.uuid4().hex
        rows = [
            ('2026-12', '2026-11-30T23:59:59', 'scanned', False),
            ('2026-11', '2026-12-01T00:00:00', 'scanned', True),
            ('2026-12', '2026-12-31T23:59:59', 'electronic', True),
            ('2026-12', '2027-01-01T00:00:00', 'electronic', False),
            ('2026-12', None, 'scanned', False),
        ]
        expected = set()
        for index, (folder, timestamp, kind, included) in enumerate(rows):
            sha = uuid.uuid4().hex * 2
            if included:
                expected.add(sha)
            conn.execute('''INSERT INTO budget.receipt_evidence
                (source_sha256,source_reference,evidence_type,transaction_datetime)
                VALUES (%s,%s,%s,%s)''', (sha, f'{folder}/{prefix}-{index}.pdf', kind, timestamp))
        # A canonical date is used when the evidence timestamp is missing.
        pk = conn.execute("INSERT INTO budget.expenses(source,order_id,order_date) VALUES ('scanned',%s,'2026-12-15') RETURNING id", (prefix,)).fetchone()['id']
        fallback = uuid.uuid4().hex * 2
        conn.execute('''INSERT INTO budget.receipt_evidence
            (source_sha256,source_reference,evidence_type,expense_pk)
            VALUES (%s,%s,'scanned',%s)''', (fallback, f'2026-11/{prefix}-fallback.pdf', pk))
        expected.add(fallback)
        result = _receipt_list(Service(), limit=100, offset=0, q=prefix, month='2026-12')
        assert result.total_count == 3
        assert {r['source_sha256'] for r in result.rows} == expected
        conn.rollback()
