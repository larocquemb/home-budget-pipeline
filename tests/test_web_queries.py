from home_budget_pipeline.web.queries import LedgerQueryService


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
