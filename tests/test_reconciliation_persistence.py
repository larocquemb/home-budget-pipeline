from decimal import Decimal

from home_budget_pipeline.reconciliation.persistence import PostgresReconciliationStore
from home_budget_pipeline.reconciliation.processing import (
    CanonicalPurchaseReconciler,
    ReconciliationStatus,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.executed = []
        self._row = None
        self._rows = []

    def __enter__(self):
        self.connection.cursors.append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))
        normalized = " ".join(sql.split())
        if "FROM budget.expenses WHERE id = %s" in normalized:
            self._row = (
                42,
                Decimal("21.24"),
                Decimal("26.30"),
                Decimal("1.06"),
                Decimal("2.00"),
                Decimal("3.00"),
                Decimal("1.00"),
                True,
            )
        elif "FROM budget.expense_items" in normalized:
            self._rows = [
                (1, 42, Decimal("6.99"), "Groceries"),
                (2, 42, Decimal("10.00"), "Outdoor Supplies"),
                (3, 42, Decimal("4.25"), "Indoor Supplies"),
            ]
        elif "FROM budget.expense_category_splits" in normalized:
            self._rows = [
                (42, "Groceries", Decimal("6.99")),
                (42, "Indoor Supplies", Decimal("4.25")),
                (42, "Outdoor Supplies", Decimal("10.00")),
            ]

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self):
        self.cursors = []
        self.commit_count = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commit_count += 1


def test_postgres_store_loads_canonical_data_and_persists_result():
    connection = FakeConnection()
    store = PostgresReconciliationStore(connection)

    purchase = store.load_purchase(42)
    items = store.load_items(42)
    splits = store.load_category_splits(42)
    result = CanonicalPurchaseReconciler().reconcile(purchase, items, splits)
    store.save_result(result)

    assert purchase.expense_pk == 42
    assert purchase.receipt_equation_complete is True
    assert purchase.discounts == Decimal("1.00")
    assert result.item_subtotal.status is ReconciliationStatus.PASS
    assert result.receipt_total.status is ReconciliationStatus.PASS
    assert result.category_splits.status is ReconciliationStatus.PASS
    assert connection.commit_count == 1

    save_cursor = connection.cursors[-1]
    sql, params = save_cursor.executed[-1]
    assert "ON CONFLICT (expense_pk) DO UPDATE" in sql
    assert params == (
        42,
        "pass",
        Decimal("0.00"),
        "pass",
        Decimal("0.00"),
        "pass",
        Decimal("0.00"),
        False,
    )


def test_postgres_store_raises_when_canonical_expense_is_missing():
    class MissingCursor(FakeCursor):
        def execute(self, sql, params):
            self.executed.append((sql, params))
            self._row = None

    class MissingConnection(FakeConnection):
        def cursor(self):
            return MissingCursor(self)

    store = PostgresReconciliationStore(MissingConnection())

    try:
        store.load_purchase(999)
    except LookupError as exc:
        assert "canonical expense not found: 999" in str(exc)
    else:
        raise AssertionError("expected LookupError")
