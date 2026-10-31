from datetime import date

import pytest
from fastapi import HTTPException

from home_budget_pipeline.web.receipt_app import _receipt_filters, _receipt_list, receipts_page


def test_purchase_month_rolls_over_december_and_converts_old_exact_month_search():
    assert _receipt_filters('2026-12', '') == ('', '2026-12', (date(2026, 12, 1), date(2027, 1, 1)))
    # An explicit month can be combined with a source-folder text search.
    assert _receipt_filters('2026-12', '2026-11')[0] == '2026-12'


@pytest.mark.parametrize('month', ['2026-00', '2026-13', '0000-01', '9999-12', "2026-12' OR TRUE", '2026-1'])
def test_invalid_purchase_month_is_rejected_before_querying(month):
    class Service:
        def _fetch(self, *_):
            raise AssertionError('Must not query invalid filters')
    with pytest.raises(HTTPException) as error:
        _receipt_list(Service(), limit=10, offset=0, month=month)
    assert error.value.status_code == 422


def test_purchase_month_is_parameterized_and_preserved_in_page_navigation():
    class Service:
        def __init__(self):
            self.calls = []
        def _fetch(self, sql, params=()):
            self.calls.append((sql, params))
            return ({'total_count': 200},) if 'COUNT(*)' in sql else ()
    service = Service()
    html = receipts_page(limit=100, offset=0, sort='expense_id', direction='asc',
                         q='2026-12', service=service, identity={'user': 'test'})
    sql, params = service.calls[0]
    assert params == (date(2026, 12, 1), date(2027, 1, 1))
    assert 'transaction_datetime, e.transaction_datetime, e.order_date::timestamp) >= %s' in sql
    assert 'ILIKE' not in sql
    assert 'type="month" name="month" value="2026-12"' in html
    assert 'name="q" value=""' in html
    assert 'month=2026-12' in html and 'offset=100' in html
    assert 'Purchase month uses the displayed receipt date' in html
