import pytest

from home_budget_pipeline.web.queries import LedgerQueryService, RECEIPT_PROCESSING_SORT_COLUMNS


@pytest.mark.parametrize("sort", RECEIPT_PROCESSING_SORT_COLUMNS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_processing_sorts_before_pagination_with_stable_ties(sort, direction):
    service, _, cursor = service_with()
    service.receipt_processing_status(limit=10, offset=20, sort=sort, direction=direction)
    sql, params = cursor.executed[3]
    assert f"ORDER BY {sort} {direction.upper()} NULLS LAST, source_reference ASC, source_sha256 ASC LIMIT %s OFFSET %s" in sql
    assert params == (10, 20)


def test_processing_sort_rejects_sql_in_sort_and_direction():
    service, _, cursor = service_with()
    service.receipt_processing_status(sort="attempts; DROP TABLE budget.expenses", direction="asc; SELECT 1")
    sql, _ = cursor.executed[3]
    assert "ORDER BY last_attempted_at DESC NULLS LAST" in sql
    assert "DROP" not in sql
    assert "asc; SELECT 1" not in sql


@pytest.mark.parametrize("month", ["2026-10", "2026-12", "2028-02"])
def test_processing_month_uses_only_source_folder_and_parameterized_month(month):
    service, _, cursor = service_with()
    service.receipt_processing_status(limit=10, offset=20, month=month)
    count_sql, count_params = cursor.executed[1]
    sql, params = cursor.executed[3]
    assert count_params == (month,)
    assert params == (*count_params, 10, 20)
    for query in (count_sql, sql):
        assert "WHERE source_month = %s" in query
        assert "substring(r.source_reference" in query
        assert "transaction_datetime" not in query
        assert "FROM budget.receipt_evidence" in query


def test_processing_unassigned_and_all_months():
    service, _, cursor = service_with()
    service.receipt_processing_status(month="unassigned")
    assert "WHERE source_month IS NULL" in cursor.executed[3][0]
    service, _, cursor = service_with()
    service.receipt_processing_status(month="all")
    assert "WHERE source_month" not in cursor.executed[3][0]


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "0000-10", "2026-10' OR true--"])
def test_processing_rejects_invalid_months_before_querying(month):
    service, _, cursor = service_with()
    with pytest.raises(ValueError, match="YYYY-MM"):
        service.receipt_processing_status(month=month)
    assert not cursor.executed


class FakeCursor:
    def __init__(self, rows=(), columns=(), responses=()):
        self.rows = list(rows)
        self.description = [(column,) for column in columns]
        self.responses = list(responses)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        if self.responses and not sql.lstrip().upper().startswith("SET "):
            rows, columns = self.responses.pop(0)
            self.rows = list(rows)
            self.description = [(column,) for column in columns]

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def test_active_categories_are_loaded_in_display_order():
    service, _, cursor = service_with(
        responses=(([('Groceries',), ('Indoor Supplies',)], ('category_name',)),)
    )

    assert service.active_categories() == ('Groceries', 'Indoor Supplies')
    assert "WHERE is_active = TRUE" in cursor.executed[1][0]


def test_category_rules_search_is_parameterized():
    service, _, cursor = service_with()

    service.category_rules(search="towel")

    sql, params = cursor.executed[1]
    assert "match_text ILIKE %s" in sql
    assert params == ("%towel%",) * 4


def service_with(rows=(), columns=(), responses=()):
    cursor = FakeCursor(rows, columns, responses)
    connection = FakeConnection(cursor)
    service = LedgerQueryService(connect=lambda: connection)
    return service, connection, cursor


def test_query_service_sets_transaction_read_only_before_select():
    service, connection, cursor = service_with(
        responses=(
            ([(1,)], ("total_count",)),
            ([(1, "costco")], ("expense_pk", "source")),
        ),
    )

    page = service.analytics_expenses(limit=10)

    assert cursor.executed[0][0] == "SET TRANSACTION READ ONLY"
    assert "COUNT(*) AS total_count" in cursor.executed[1][0]
    assert "FROM budget.analytics_expenses" in cursor.executed[3][0]
    assert page.rows[0] == {"expense_pk": 1, "source": "costco"}
    assert connection.closed is True


def test_expense_filters_are_server_side_and_parameterized():
    service, _, cursor = service_with(
        responses=(([(0,)], ("total_count",)), ([], ()))
    )

    service.analytics_expenses(
        limit=25,
        offset=50,
        source="costco",
        merchant="Regent",
        requires_review=True,
    )

    count_sql, count_params = cursor.executed[1]
    sql, params = cursor.executed[3]
    assert "source = %s" in count_sql
    assert count_params == ("costco", "%Regent%", True)
    assert "source = %s" in sql
    assert "store_name ILIKE %s" in sql
    assert "requires_review = %s" in sql
    assert params == ("costco", "%Regent%", True, 25, 50)


def test_expense_sorting_uses_allowlisted_server_side_column():
    service, _, cursor = service_with(
        responses=(([(0,)], ("total_count",)), ([], ()))
    )

    service.analytics_expenses(sort="expense_total", direction="asc")

    sql, _ = cursor.executed[3]
    assert "ORDER BY expense_total ASC NULLS LAST" in sql


def test_unknown_expense_sort_falls_back_to_date_descending():
    service, _, cursor = service_with(
        responses=(([(0,)], ("total_count",)), ([], ()))
    )

    service.analytics_expenses(sort="expense_total; DROP TABLE budget.expenses", direction="sideways")

    sql, _ = cursor.executed[3]
    assert "ORDER BY COALESCE(order_date, transaction_datetime::date) DESC NULLS LAST" in sql
    assert "DROP TABLE" not in sql


def test_expenses_include_filtered_total_for_pagination():
    service, _, cursor = service_with(
        responses=(
            ([(73,)], ("total_count",)),
            ([(1, "costco")], ("expense_pk", "source")),
        ),
    )

    page = service.analytics_expenses(limit=10)

    assert "COUNT(*) AS total_count" in cursor.executed[1][0]
    assert page.total_count == 73
    assert page.rows == ({"expense_pk": 1, "source": "costco"},)


def test_expenses_include_zero_total_when_page_is_empty():
    service, _, _ = service_with(
        responses=(([(0,)], ("total_count",)), ([], ()))
    )

    page = service.analytics_expenses(limit=10, merchant="missing")

    assert page.total_count == 0
    assert page.rows == ()


def test_category_spend_reads_kan71_view_instead_of_recalculating():
    service, _, cursor = service_with()

    service.category_spend(limit=20, offset=0)

    sql, _ = cursor.executed[1]
    assert "FROM budget.analytics_category_spend" in sql
    assert "SUM(" not in sql
    assert "GROUP BY" not in sql


def test_review_queue_reads_kan71_review_view():
    service, _, cursor = service_with()

    service.review_queue(limit=20)

    assert "FROM budget.analytics_review_queue" in cursor.executed[1][0]


def test_extraction_audit_counts_items_and_applies_threshold():
    service, _, cursor = service_with()

    service.extraction_audit(limit=25, offset=5, status="complete", max_items=4)

    sql, params = cursor.executed[1]
    assert "LEFT JOIN budget.expense_items" in sql
    assert "COUNT(i.id) AS item_count" in sql
    assert "HAVING COUNT(i.id) <= %s" in sql
    assert "e.extraction_status = %s" in sql
    assert params == ("complete", 4, 25, 5)


def test_duplicate_queue_is_pending_only():
    service, _, cursor = service_with()

    service.pending_duplicates(limit=20)

    sql, _ = cursor.executed[1]
    assert "FROM budget.receipt_duplicate_links" in sql
    assert "resolution_status = 'pending'" in sql


def test_transactions_join_persisted_reconciliation_results():
    service, _, cursor = service_with()

    service.transaction_reconciliation(limit=20)

    sql, _ = cursor.executed[1]
    assert "FROM budget.financial_transactions" in sql
    assert "budget.transaction_expense_reconciliation" in sql
