from home_budget_pipeline.categorization.command import resolve_database_dsn


def test_home_budget_pg_dsn_wins_over_database_url():
    env = {
        "HOME_BUDGET_PG_DSN": "host=localhost dbname=home_budget user=paul",
        "DATABASE_URL": "postgresql://fallback.example/home_budget",
    }

    assert resolve_database_dsn(env) == "host=localhost dbname=home_budget user=paul"


def test_database_url_is_supported_as_fallback():
    env = {"DATABASE_URL": "postgresql://fallback.example/home_budget"}

    assert resolve_database_dsn(env) == "postgresql://fallback.example/home_budget"


def test_missing_database_configuration_returns_none():
    assert resolve_database_dsn({}) is None
