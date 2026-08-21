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
