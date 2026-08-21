import importlib
from unittest.mock import MagicMock, patch

from home_budget_pipeline import db_setup


def test_sql_directory_can_be_configured_for_installed_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_BUDGET_SQL_DIR", str(tmp_path))

    reloaded = importlib.reload(db_setup)

    assert reloaded.SQL_DIR == tmp_path
    assert reloaded.CONSTRAINTS_SCHEMA == tmp_path / "schema_constraints.sql"
    monkeypatch.delenv("HOME_BUDGET_SQL_DIR")
    importlib.reload(db_setup)


def test_existing_base_schema_applies_constraints_and_ensures_receipt_schema(tmp_path, monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("budget.expenses",)
    constraints = tmp_path / "schema_constraints.sql"
    audit_migration = tmp_path / "category_mapping_audit.sql"
    descriptions_migration = tmp_path / "expense_item_descriptions.sql"
    template = tmp_path / "receipt_processing_template.sql"
    constraints.write_text("DROP INDEX IF EXISTS budget.uq_expense_items_natural;", encoding="utf-8")
    audit_migration.write_text("CREATE TABLE IF NOT EXISTS budget.audit_test(id int);", encoding="utf-8")
    descriptions_migration.write_text("ALTER TABLE budget.expense_items ADD COLUMN IF NOT EXISTS product_description text;", encoding="utf-8")
    template.write_text("template", encoding="utf-8")
    monkeypatch.setattr(db_setup, "CONSTRAINTS_SCHEMA", constraints)
    monkeypatch.setattr(db_setup, "CATEGORY_MAPPING_AUDIT_MIGRATION", audit_migration)
    monkeypatch.setattr(db_setup, "ITEM_DESCRIPTIONS_MIGRATION", descriptions_migration)
    monkeypatch.setattr(db_setup, "RECEIPT_TEMPLATE", template)

    with patch.object(db_setup, "ensure_receipt_schema", return_value=False) as ensure:
        result = db_setup.ensure_database_schema(conn)

    assert result == {"base_created": False, "receipt_schema_changed": False}
    conn.execute.assert_any_call("DROP INDEX IF EXISTS budget.uq_expense_items_natural;")
    conn.execute.assert_any_call("CREATE TABLE IF NOT EXISTS budget.audit_test(id int);")
    assert conn.commit.call_count == 3
    ensure.assert_called_once_with(conn, template)


def test_blank_database_loads_base_schema_constraints_before_receipt_schema(tmp_path, monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (None,)

    base = tmp_path / "schema_phase1.sql"
    constraints = tmp_path / "schema_constraints.sql"
    audit_migration = tmp_path / "category_mapping_audit.sql"
    descriptions_migration = tmp_path / "expense_item_descriptions.sql"
    template = tmp_path / "receipt_processing_template.sql"
    base.write_text("CREATE SCHEMA budget;", encoding="utf-8")
    constraints.write_text("DROP INDEX IF EXISTS budget.uq_expense_items_natural;", encoding="utf-8")
    audit_migration.write_text("CREATE TABLE IF NOT EXISTS budget.audit_test(id int);", encoding="utf-8")
    descriptions_migration.write_text("ALTER TABLE budget.expense_items ADD COLUMN IF NOT EXISTS product_description text;", encoding="utf-8")
    template.write_text("template", encoding="utf-8")
    monkeypatch.setattr(db_setup, "BASE_SCHEMA", base)
    monkeypatch.setattr(db_setup, "CONSTRAINTS_SCHEMA", constraints)
    monkeypatch.setattr(db_setup, "CATEGORY_MAPPING_AUDIT_MIGRATION", audit_migration)
    monkeypatch.setattr(db_setup, "ITEM_DESCRIPTIONS_MIGRATION", descriptions_migration)
    monkeypatch.setattr(db_setup, "RECEIPT_TEMPLATE", template)

    with patch.object(db_setup, "ensure_receipt_schema", return_value=True) as ensure:
        result = db_setup.ensure_database_schema(conn)

    assert result == {"base_created": True, "receipt_schema_changed": True}
    conn.execute.assert_any_call("CREATE SCHEMA budget;")
    conn.execute.assert_any_call("DROP INDEX IF EXISTS budget.uq_expense_items_natural;")
    assert conn.commit.call_count == 4
    ensure.assert_called_once_with(conn, template)
