import importlib
from unittest.mock import MagicMock, patch

from home_budget_pipeline import db_setup


def test_sql_directory_can_be_configured_for_installed_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_BUDGET_SQL_DIR", str(tmp_path))

    reloaded = importlib.reload(db_setup)

    assert reloaded.SQL_DIR == tmp_path
    assert reloaded.CONSTRAINTS_SCHEMA == tmp_path / "schema_constraints.sql"
    assert reloaded.ENRICHMENT_SCHEMA == tmp_path / "product_enrichment.sql"
    monkeypatch.delenv("HOME_BUDGET_SQL_DIR")
    importlib.reload(db_setup)


def _write_upgrade_files(tmp_path):
    paths = {
        "constraints": tmp_path / "schema_constraints.sql",
        "audit": tmp_path / "category_mapping_audit.sql",
        "descriptions": tmp_path / "expense_item_descriptions.sql",
        "aliases_migration": tmp_path / "merchant_aliases_migration.sql",
        "ocr_lines": tmp_path / "receipt_ocr_lines.sql",
        "ocr_learning": tmp_path / "receipt_ocr_learning.sql",
        "aliases_data": tmp_path / "merchant_aliases.sql",
        "enrichment": tmp_path / "product_enrichment.sql",
        "template": tmp_path / "receipt_processing_template.sql",
    }
    paths["constraints"].write_text("DROP INDEX IF EXISTS budget.uq_expense_items_natural;", encoding="utf-8")
    paths["audit"].write_text("CREATE TABLE IF NOT EXISTS budget.audit_test(id int);", encoding="utf-8")
    paths["descriptions"].write_text("ALTER TABLE budget.expense_items ADD COLUMN IF NOT EXISTS product_description text;", encoding="utf-8")
    paths["aliases_migration"].write_text("CREATE TABLE IF NOT EXISTS budget.merchant_aliases(id int);", encoding="utf-8")
    paths["ocr_lines"].write_text("CREATE TABLE IF NOT EXISTS budget.receipt_ocr_lines(id bigint);", encoding="utf-8")
    paths["ocr_learning"].write_text("CREATE TABLE IF NOT EXISTS budget.receipt_ocr_runs(run_uuid uuid);", encoding="utf-8")
    paths["aliases_data"].write_text("INSERT INTO budget.merchant_aliases VALUES (1);", encoding="utf-8")
    paths["enrichment"].write_text("CREATE SCHEMA IF NOT EXISTS enrichment; CREATE TABLE IF NOT EXISTS enrichment.product_cache(id int);", encoding="utf-8")
    paths["template"].write_text("template", encoding="utf-8")
    return paths


def _patch_upgrade_files(monkeypatch, paths):
    monkeypatch.setattr(db_setup, "CONSTRAINTS_SCHEMA", paths["constraints"])
    monkeypatch.setattr(db_setup, "CATEGORY_MAPPING_AUDIT_MIGRATION", paths["audit"])
    monkeypatch.setattr(db_setup, "ITEM_DESCRIPTIONS_MIGRATION", paths["descriptions"])
    monkeypatch.setattr(db_setup, "MERCHANT_ALIASES_MIGRATION", paths["aliases_migration"])
    monkeypatch.setattr(db_setup, "RECEIPT_OCR_LINES_MIGRATION", paths["ocr_lines"])
    monkeypatch.setattr(db_setup, "RECEIPT_OCR_LEARNING_MIGRATION", paths["ocr_learning"])
    monkeypatch.setattr(db_setup, "MERCHANT_ALIASES_DATA", paths["aliases_data"])
    monkeypatch.setattr(db_setup, "ENRICHMENT_SCHEMA", paths["enrichment"])
    monkeypatch.setattr(db_setup, "RECEIPT_TEMPLATE", paths["template"])


def test_existing_base_schema_applies_shared_upgrades_and_ensures_receipt_schema(tmp_path, monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [("budget.expenses",), (None,), (None,), (None,)]
    paths = _write_upgrade_files(tmp_path)
    _patch_upgrade_files(monkeypatch, paths)

    with patch.object(db_setup, "ensure_receipt_schema", return_value=False) as ensure:
        result = db_setup.ensure_database_schema(conn)

    assert result == {
        "base_created": False,
        "enrichment_schema_created": True,
        "receipt_ocr_lines_created": True,
        "receipt_ocr_learning_created": True,
        "receipt_schema_changed": False,
    }
    conn.execute.assert_any_call("DROP INDEX IF EXISTS budget.uq_expense_items_natural;")
    conn.execute.assert_any_call("CREATE TABLE IF NOT EXISTS budget.audit_test(id int);")
    conn.execute.assert_any_call("CREATE SCHEMA IF NOT EXISTS enrichment; CREATE TABLE IF NOT EXISTS enrichment.product_cache(id int);")
    conn.execute.assert_any_call("CREATE TABLE IF NOT EXISTS budget.receipt_ocr_lines(id bigint);")
    conn.execute.assert_any_call("CREATE TABLE IF NOT EXISTS budget.receipt_ocr_runs(run_uuid uuid);")
    assert conn.commit.call_count == 8
    ensure.assert_called_once_with(conn, paths["template"])


def test_blank_database_loads_base_and_shared_schemas_before_receipt_schema(tmp_path, monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [(None,), (None,), (None,), (None,)]

    base = tmp_path / "schema_phase1.sql"
    base.write_text("CREATE SCHEMA budget;", encoding="utf-8")
    paths = _write_upgrade_files(tmp_path)
    monkeypatch.setattr(db_setup, "BASE_SCHEMA", base)
    _patch_upgrade_files(monkeypatch, paths)

    with patch.object(db_setup, "ensure_receipt_schema", return_value=True) as ensure:
        result = db_setup.ensure_database_schema(conn)

    assert result == {
        "base_created": True,
        "enrichment_schema_created": True,
        "receipt_ocr_lines_created": True,
        "receipt_ocr_learning_created": True,
        "receipt_schema_changed": True,
    }
    conn.execute.assert_any_call("CREATE SCHEMA budget;")
    conn.execute.assert_any_call("DROP INDEX IF EXISTS budget.uq_expense_items_natural;")
    conn.execute.assert_any_call("CREATE SCHEMA IF NOT EXISTS enrichment; CREATE TABLE IF NOT EXISTS enrichment.product_cache(id int);")
    conn.execute.assert_any_call("CREATE TABLE IF NOT EXISTS budget.receipt_ocr_lines(id bigint);")
    conn.execute.assert_any_call("CREATE TABLE IF NOT EXISTS budget.receipt_ocr_runs(run_uuid uuid);")
    assert conn.commit.call_count == 9
    ensure.assert_called_once_with(conn, paths["template"])
