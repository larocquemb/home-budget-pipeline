from pathlib import Path


ANALYTICS_DDL = Path("sql/analytics_views.sql")
POSTGRES_MANIFEST = Path("k8s/postgres.yaml")


def sql() -> str:
    return ANALYTICS_DDL.read_text()


def test_defines_all_consumer_analytics_views():
    text = sql()
    for view in (
        "budget.analytics_expenses",
        "budget.analytics_expense_items",
        "budget.analytics_category_spend",
        "budget.analytics_review_queue",
    ):
        assert f"CREATE OR REPLACE VIEW {view}" in text


def test_expense_view_starts_from_one_row_per_canonical_expense():
    text = sql()
    section = text.split("CREATE OR REPLACE VIEW budget.analytics_expenses AS", 1)[1]
    section = section.split("CREATE OR REPLACE VIEW budget.analytics_expense_items AS", 1)[0]
    assert "FROM budget.expenses e" in section
    assert "expense_data_quality_violations" not in section
    assert "expense_items" not in section


def test_quality_and_reconciliation_reasons_remain_separate():
    text = sql()
    assert "reconciliation_requires_review" in text
    assert "data_quality_requires_review" in text
    assert "data_quality_violation_rules" in text
    assert "receipt_total_variance" in text


def test_uncategorized_items_are_explicit_not_dropped():
    text = sql()
    category_section = text.split("CREATE OR REPLACE VIEW budget.analytics_category_spend AS", 1)[1]
    category_section = category_section.split("CREATE OR REPLACE VIEW budget.analytics_review_queue AS", 1)[0]
    assert "COALESCE(ei.budget_category, 'Uncategorized')" in category_section
    assert "LEFT JOIN budget.expense_categories" in category_section


def test_category_spend_aggregates_canonical_items_once():
    text = sql()
    category_section = text.split("CREATE OR REPLACE VIEW budget.analytics_category_spend AS", 1)[1]
    category_section = category_section.split("CREATE OR REPLACE VIEW budget.analytics_review_queue AS", 1)[0]
    assert "FROM budget.expense_items ei" in category_section
    assert "SUM(ei.line_total)" in category_section
    assert "expense_category_splits" not in category_section


def test_analytics_views_do_not_round_monetary_values():
    text = sql()
    assert "ROUND(" not in text


def test_review_queue_filters_only_overall_review_rows():
    text = sql()
    review_section = text.split("CREATE OR REPLACE VIEW budget.analytics_review_queue AS", 1)[1]
    assert "FROM budget.analytics_expenses ae" in review_section
    assert "WHERE ae.requires_review = TRUE" in review_section


def test_fresh_postgres_bootstrap_stages_analytics_ddl_without_migrations():
    manifest = POSTGRES_MANIFEST.read_text()
    bootstrap = Path("scripts/stage_db_bootstrap.sh").read_text()
    assert "scripts/stage_db_bootstrap.sh /schema" in manifest
    assert "sql/analytics_views.sql" in bootstrap
    assert "003-analytics-views.sql" in bootstrap
    assert "migrations/kan_71" not in bootstrap
