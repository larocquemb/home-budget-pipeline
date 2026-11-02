"""Explicit unknown extraction dates must not resurrect legacy values."""
import json
from datetime import datetime

import pytest

from home_budget_pipeline.web.app import _post_ocr_text
from home_budget_pipeline.web import receipt_app
from home_budget_pipeline.web.receipt_fields import receipt_datetime, RECEIPT_DATETIME_SQL


@pytest.mark.parametrize('decision', [{'value': None, 'status': 'conflict'},
                                    {'value': None, 'status': 'selected'}])
def test_explicit_unknown_date_blocks_accounting_and_filename_fallback(decision):
    evidence = {'transaction_datetime': None, 'source_reference': '20310712_example_24_60.pdf',
                'raw_payload': {'field_decisions': {'transaction_datetime': decision}}}
    expense = {'transaction_datetime': '2091-07-12T09:42:13', 'order_date': '2091-07-12'}
    assert receipt_datetime(evidence, expense, '2031-07-12') is None
    text = _post_ocr_text(evidence, expense)
    assert 'Date/time: \n' in text
    assert '2091' not in text and '2031' not in text


def test_stored_evidence_date_and_legacy_missing_evidence_keep_existing_behavior():
    actual = datetime(2031, 7, 12, 9, 42, 13)
    evidence = {'transaction_datetime': actual, 'raw_payload': {'field_decisions': {
        'transaction_datetime': {'status': 'conflict', 'value': None}}}}
    assert receipt_datetime(evidence, {'transaction_datetime': 'older'}) == actual
    assert receipt_datetime({}, {'order_date': '2031-07-12'}) == '2031-07-12'
    assert receipt_datetime({}, {}, '2031-07-12') == '2031-07-12'


def test_receipt_header_and_api_honor_date_conflict(monkeypatch):
    evidence = {'transaction_datetime': None, 'merchant': 'Example Shop', 'total': 24.60,
        'raw_payload': {'field_decisions': {'transaction_datetime': {'value': None, 'status': 'conflict'}}}}
    expense = {'transaction_datetime': '2091-07-12T09:42:13', 'order_date': '2091-07-12'}
    class Service:
        def _fetch(self, *a): return ({'evidence_id': 1, 'expense_pk': 2, 'source_reference': 'sample.png'},)
        def receipt_evidence(self, *a): return evidence
        def expense_detail(self, *a): return expense
    service = Service()
    result = receipt_app._receipt_detail(service, 'a' * 64)
    assert result['transaction_datetime'] is None
    html = receipt_app.receipt_page('a' * 64, service=service, identity={'user': 'test'})
    assert '2091' not in html
    assert 'Conflicting OCR date/time readings require review.' in html


def test_date_list_filter_search_selection_and_sort_share_explicit_unknown_rule():
    calls = []
    class Service:
        def _fetch(self, sql, params=()):
            calls.append((sql, params))
            return ({'total_count': 0},) if 'COUNT(*)' in sql else ()
    receipt_app._receipt_list(Service(), limit=10, offset=0, q='date', month='2031-07', sort='date')
    assert RECEIPT_DATETIME_SQL in calls[0][0] and RECEIPT_DATETIME_SQL in calls[1][0]
    assert "(re.raw_payload->'field_decisions') ? 'transaction_datetime' THEN NULL" in RECEIPT_DATETIME_SQL
    assert 'ORDER BY ' + RECEIPT_DATETIME_SQL in calls[1][0]
