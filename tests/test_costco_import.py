from home_budget_pipeline.receipts import costco


def test_costco_uses_secret_backed_database_url_when_dsn_is_omitted(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@database/home_budget")
    captured = {}

    class Psycopg:
        @staticmethod
        def connect(dsn):
            captured["dsn"] = dsn
            return object()

    monkeypatch.setitem(__import__("sys").modules, "psycopg", Psycopg)

    costco._connect_postgres("")

    assert captured["dsn"] == "postgresql://user:secret@database/home_budget"


def test_costco_category_sources_use_canonical_vocabulary():
    assert costco.canonical_category_source("ai") == "ai"
    assert costco.canonical_category_source("default") == "unresolved"
    assert costco.canonical_category_source("verified_cache") == "rule"
    assert costco.canonical_category_source("override") == "rule"
    assert costco.canonical_category_source("exact") == "rule"
    assert costco.canonical_category_source("fuzzy") == "rule"


def test_costco_general_towels_are_indoor_supplies_without_overriding_beach_towels():
    assert costco.classify_category_with_source("TOWEL", allow_ai=False) == (
        "Indoor Supplies",
        "verified_cache",
    )
    assert costco.classify_category_with_source("BATH TOWEL", allow_ai=False) == (
        "Indoor Supplies",
        "verified_cache",
    )
    assert costco.classify_category_with_source("BEACH TOWEL", allow_ai=False) != (
        "Indoor Supplies",
        "verified_cache",
    )
