from pathlib import Path

from home_budget_pipeline.categorization.catalog import (
    CATEGORY_ORDER,
    VALID_CATEGORIES,
    canonicalize_category,
)
from home_budget_pipeline.categorization.catalog_config import load_catalog, render_catalog_sql


def test_category_catalog_is_valid_and_grouped():
    catalog = load_catalog(Path("config/categories.yaml"))

    assert [group["key"] for group in catalog["groups"]] == [
        "mandatory",
        "sinking",
        "discretionary",
        "investments",
    ]

    categories = {category["name"]: category for category in catalog["categories"]}
    assert categories["Subscriptions"]["group"] == "mandatory"
    assert "Online Svcs" in categories["Subscriptions"]["aliases"]
    assert categories["Gifts"]["group"] == "discretionary"
    assert "Birthday / Celebrations" in categories["Gifts"]["aliases"]
    assert categories["Misc Household"]["group"] == "discretionary"
    assert categories["Lake"]["description"] is None


def test_runtime_catalog_is_authoritative_and_resolves_legacy_aliases():
    assert "Subscriptions" in VALID_CATEGORIES
    assert "Gifts" in VALID_CATEGORIES
    assert "Online Svcs" not in VALID_CATEGORIES
    assert "Birthday / Celebrations" not in VALID_CATEGORIES
    assert "Rental Expenses" not in VALID_CATEGORIES
    assert canonicalize_category("Online Svcs") == "Subscriptions"
    assert canonicalize_category("Birthday / Celebrations") == "Gifts"
    assert canonicalize_category("Misc Paul & Rox") == "Misc Household"
    assert len(CATEGORY_ORDER) == len(VALID_CATEGORIES)


def test_catalog_renders_bootstrap_upserts():
    sql = render_catalog_sql(load_catalog(Path("config/categories.yaml")))

    assert "INSERT INTO budget.category_groups" in sql
    assert "INSERT INTO budget.expense_categories" in sql
    assert "INSERT INTO budget.expense_category_aliases" in sql
    assert "'Subscriptions'" in sql
    assert "'Online Svcs'" in sql
    assert sql.startswith("BEGIN;")
    assert sql.endswith("COMMIT;\n")
