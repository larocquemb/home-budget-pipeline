"""Initialize or upgrade the BrownRook Ledger PostgreSQL schema."""

from __future__ import annotations

import os
from pathlib import Path

from home_budget_pipeline.receipts.schema_blue_green import ensure_receipt_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = Path(os.environ.get("HOME_BUDGET_SQL_DIR", REPO_ROOT / "sql"))
BASE_SCHEMA = SQL_DIR / "schema_phase1.sql"
CONSTRAINTS_SCHEMA = SQL_DIR / "schema_constraints.sql"
RECEIPT_TEMPLATE = SQL_DIR / "receipt_processing_template.sql"
ENRICHMENT_SCHEMA = SQL_DIR / "product_enrichment.sql"
CATEGORY_MAPPING_AUDIT_MIGRATION = SQL_DIR / "migrations" / "category_mapping_audit.sql"
ITEM_DESCRIPTIONS_MIGRATION = SQL_DIR / "migrations" / "expense_item_descriptions.sql"
MERCHANT_ALIASES_MIGRATION = SQL_DIR / "migrations" / "merchant_aliases.sql"
MERCHANT_ALIASES_DATA = SQL_DIR / "merchant_aliases.sql"


def _relation_exists(conn, qualified_name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (qualified_name,)).fetchone()[0] is not None


def _execute_sql_file(conn, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def ensure_database_schema(conn) -> dict[str, bool]:
    """Ensure canonical schemas plus current receipt-ingest blue/green state."""
    base_created = False
    if not _relation_exists(conn, "budget.expenses"):
        _execute_sql_file(conn, BASE_SCHEMA)
        base_created = True

    # Constraint policy and additive shared schemas must be applied to existing
    # databases too. Receipt ingest remains independently managed blue/green.
    _execute_sql_file(conn, CONSTRAINTS_SCHEMA)
    _execute_sql_file(conn, CATEGORY_MAPPING_AUDIT_MIGRATION)
    _execute_sql_file(conn, ITEM_DESCRIPTIONS_MIGRATION)
    _execute_sql_file(conn, MERCHANT_ALIASES_MIGRATION)
    _execute_sql_file(conn, MERCHANT_ALIASES_DATA)

    enrichment_created = not _relation_exists(conn, "enrichment.product_cache")
    _execute_sql_file(conn, ENRICHMENT_SCHEMA)

    receipt_changed = ensure_receipt_schema(conn, RECEIPT_TEMPLATE)
    return {
        "base_created": base_created,
        "enrichment_schema_created": enrichment_created,
        "receipt_schema_changed": receipt_changed,
    }


def main() -> int:
    import psycopg

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("HOME_BUDGET_PG_DSN")
    if not dsn:
        raise RuntimeError("DATABASE_URL or HOME_BUDGET_PG_DSN is required")

    with psycopg.connect(dsn) as conn:
        result = ensure_database_schema(conn)

    if result["base_created"]:
        print("base budget schema initialized")
    else:
        print("base budget schema already present")

    if result["enrichment_schema_created"]:
        print("product enrichment schema initialized")
    else:
        print("product enrichment schema already current")

    if result["receipt_schema_changed"]:
        print("receipt ingest schema upgraded")
    else:
        print("receipt ingest schema already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
