from datetime import date, datetime
from decimal import Decimal

from home_budget_pipeline.data_quality.persistence import PostgresDataQualityStore
from home_budget_pipeline.data_quality.required_fields import (
    DataQualityResult,
    DataQualityStatus,
    DataQualityViolation,
)


class FakeCursor:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.responses.pop(0) if self.responses else None

    def fetchall(self):
        return self.responses.pop(0) if self.responses else []


class FakeConnection:
    def __init__(self, responses=None):
        self.cursor_obj = FakeCursor(responses)
        self.commit_count = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1


def test_loads_canonical_expense_and_items():
    connection = FakeConnection(
        responses=[
            (
                42,
                "scanned",
                "receipt-42",
                date(2026, 8, 20),
                datetime(2026, 8, 20, 12, 30),
                "Sobeys",
                Decimal("12.34"),
            ),
            [(7, 42, "Milk", Decimal("12.34"))],
        ]
    )
    store = PostgresDataQualityStore(connection)

    expense = store.load_expense(42)
    items = store.load_items(42)

    assert expense.expense_pk == 42
    assert expense.source == "scanned"
    assert expense.expense_total == Decimal("12.34")
    assert items[0].item_name == "Milk"
    assert items[0].line_total == Decimal("12.34")


def test_save_result_upserts_summary_replaces_violations_and_commits():
    connection = FakeConnection()
    store = PostgresDataQualityStore(connection)
    result = DataQualityResult(
        expense_pk=42,
        status=DataQualityStatus.FAIL,
        violations=(
            DataQualityViolation("missing_store_name", "store_name"),
            DataQualityViolation("item_missing_line_total", "line_total", item_id=7),
        ),
    )

    store.save_result(result)

    assert connection.commit_count == 1
    statements = connection.cursor_obj.executed
    assert len(statements) == 4
    assert "ON CONFLICT (expense_pk) DO UPDATE" in statements[0][0]
    assert statements[0][1] == (42, "fail", True)
    assert "DELETE FROM budget.expense_data_quality_violations" in statements[1][0]
    assert statements[2][1] == (42, "missing_store_name", "store_name", None, None)
    assert statements[3][1] == (42, "item_missing_line_total", "line_total", 7, None)


def test_save_passing_result_clears_old_violations():
    connection = FakeConnection()
    store = PostgresDataQualityStore(connection)

    store.save_result(DataQualityResult(42, DataQualityStatus.PASS, ()))

    statements = connection.cursor_obj.executed
    assert len(statements) == 2
    assert statements[0][1] == (42, "pass", False)
    assert "DELETE FROM budget.expense_data_quality_violations" in statements[1][0]
