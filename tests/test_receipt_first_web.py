from home_budget_pipeline.web.receipt_app import (
    BASE_PATH,
    _receipt_detail,
    _receipt_list,
    app,
    receipts_page,
    receipt_url,
)


def test_receipt_url_uses_stable_source_hash():
    source_sha256 = "a" * 64
    assert receipt_url(source_sha256) == f"{BASE_PATH}/receipts/{source_sha256}"


def test_receipt_detail_rejects_non_sha_identity_without_querying_database():
    class Service:
        def _fetch(self, *_args, **_kwargs):
            raise AssertionError("database should not be queried for an invalid receipt key")

    assert _receipt_detail(Service(), "532") is None


def test_receipt_first_routes_are_registered():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert f"{BASE_PATH}/receipts" in paths
    assert f"{BASE_PATH}/receipts/{{source_sha256}}" in paths
    assert f"{BASE_PATH}/api/receipts/{{source_sha256}}" in paths
    assert f"{BASE_PATH}/dashboard" in paths


def test_receipt_expense_sort_is_allowlisted_and_pagination_keeps_direction():
    class Service:
        def __init__(self):
            self.queries = []

        def _fetch(self, sql, params=()):
            self.queries.append((sql, params))
            if 'COUNT(*)' in sql:
                return ({'total_count': 200},)
            return ({'source_sha256': 'a' * 64, 'source_reference': 'receipt.pdf',
                     'expense_pk': 23},)

    service = Service()
    text = receipts_page(limit=100, offset=0, sort='expense_id', direction='asc',
                         service=service, identity={'user': 'test'})
    assert 'ORDER BY re.expense_pk ASC NULLS LAST' in service.queries[-1][0]
    assert 'Expense ID ▲' in text
    assert f'{BASE_PATH}/expenses/23' in text
    assert 'sort=expense_id&amp;direction=desc&amp;limit=100' in text
    assert 'sort=expense_id&direction=asc&limit=100&offset=100' in text
    _receipt_list(service, limit=100, offset=0, sort='expense_id', direction='desc')
    assert 'ORDER BY re.expense_pk DESC NULLS LAST' in service.queries[-1][0]
    _receipt_list(service, limit=100, offset=0, sort='unsafe SQL', direction='unsafe SQL')
    assert 'unsafe SQL' not in service.queries[-1][0]


def test_receipt_search_is_parameterized_and_kept_in_sorting_and_pagination():
    class Service:
        def __init__(self):
            self.queries = []

        def _fetch(self, sql, params=()):
            self.queries.append((sql, params))
            if 'COUNT(*)' in sql:
                return ({'total_count': 150},)
            return ()

    service = Service()
    text = receipts_page(limit=100, offset=100, sort='total', direction='desc',
                         q='Sobeys 2026-02-14', service=service, identity={'user': 'test'})
    count_sql, count_params = service.queries[0]
    row_sql, row_params = service.queries[1]
    assert 'Sobeys' not in row_sql
    assert count_params == ('%Sobeys%', '%Sobeys%', '%2026-02-14%', '%2026-02-14%')
    assert row_params == (*count_params, 100, 100)
    assert 'i.item_name, i.product_description' in count_sql
    assert 'ORDER BY COALESCE(re.total, e.expense_total) DESC NULLS LAST' in row_sql
    assert 'Search receipts' in text
    assert 'value="Sobeys 2026-02-14"' in text
    assert 'sort=merchant&amp;direction=asc&amp;limit=100&amp;q=Sobeys+2026-02-14' in text
    assert 'q=Sobeys+2026-02-14&limit=100&offset=0' in text
    assert 'No receipts match your search.' in text
    assert '150</strong> matching receipts' in text
    _receipt_list(service, limit=100, offset=0, q="100%_ 'quoted'")
    assert service.queries[-1][1][:2] == ('%100\\%\\_%', '%100\\%\\_%')
    assert "'quoted'" not in service.queries[-1][0]
