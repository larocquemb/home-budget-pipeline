from unittest.mock import MagicMock, patch

from home_budget_pipeline import db_setup


def test_existing_base_schema_applies_constraints_and_ensures_receipt_schema(tmp_path, monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("budget.expenses",)
    constraints = tmp_path / "schema_constraints.sql"
    template = tmp_path / "receipt_processing_template.sql"
    constraints.write_text("DROP INDEX IF EXISTS budget.uq_expense_items_natural;", encoding="utf-8")
    template.write_text("template", encoding="utf-8")
    monkeypatch.setattr(db_setup, "CONSTRAINTS_SCHEMA", constraints)
    monkeypatch.setattr(db_setup, "RECEIPT_TEMPLATE", template)

    with patch.object(db_setup, "ensure_receipt_schema", return_value=False) as ensure:
        result = db_setup.ensure_database_schema(conn)

    assert result == {"base_created": False, "receipt_schema_changed": False}
    conn.execute.assert_any_call("DROP INDEX IF EXISTS budget.uq_expense_items_natural;")
    assert conn.commit.call_count == 1
    ensure.assert_called_once_with(conn, template)


def test_blank_database_loads_base_schema_constraints_before_receipt_schema(tmp_path, monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (None,)

    base = tmp_path / "schema_phase1.sql"
    constraints = tmp_path / "schema_constraints.sql"
    template = tmp_path / "receipt_processing_template.sql"
    base.write_text("CREATE SCHEMA budget;", encoding="utf-8")
    constraints.write_text("DROP INDEX IF EXISTS budget.uq_expense_items_natural;", encoding="utf-8")
    template.write_text("template", encoding="utf-8")
    monkeypatch.setattr(db_setup, "BASE_SCHEMA", base)
    monkeypatch.setattr(db_setup, "CONSTRAINTS_SCHEMA", constraints)
    monkeypatch.setattr(db_setup, "RECEIPT_TEMPLATE", template)

    with patch.object(db_setup, "ensure_receipt_schema", return_value=True) as ensure:
        result = db_setup.ensure_database_schema(conn)

    assert result == {"base_created": True, "receipt_schema_changed": True}
    conn.execute.assert_any_call("CREATE SCHEMA budget;")
    conn.execute.assert_any_call("DROP INDEX IF EXISTS budget.uq_expense_items_natural;")
    assert conn.commit.call_count == 2
    ensure.assert_called_once_with(conn, template)
