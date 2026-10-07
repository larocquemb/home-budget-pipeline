"""Shared pytest configuration."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def offline_gpu_sampling(monkeypatch):
    """Model unit tests exercise summaries without polling production telemetry.

    Dedicated GPU sampler tests cover polling, timing and errors with fake HTTP
    responses. Mocking model requests alone does not isolate their sampler thread.
    """
    from home_budget_pipeline.gpu_sampling import GpuSampler

    monkeypatch.setattr(GpuSampler, 'poll', lambda self: None)


@pytest.fixture
def mock_brave_query_cache(monkeypatch):
    """Runner orchestration mocks use no real cache database; cache tests use PostgreSQL."""
    from home_budget_pipeline import brave_query_cache
    class Cache:
        def __init__(self, *args, **kwargs):
            self.observations, self.context = [], {}
            self.hits = self.requests = 0
        def __enter__(self): return self
        def __exit__(self, *exc): pass
    monkeypatch.setattr(brave_query_cache, 'BraveQueryCache', Cache)


_LOCAL_TEST_DSN = "host=localhost port=5432 dbname=home_budget_test user=paul"
_USING_LOCAL_DEFAULT = "TEST_DATABASE_URL" not in os.environ

# Keep integration tests isolated from the normal application database while
# making local runs work without an extra environment variable. CI or callers
# can override this explicitly when needed.
os.environ.setdefault("TEST_DATABASE_URL", _LOCAL_TEST_DSN)


def _run_psql(dsn: str, *args: str) -> None:
    subprocess.run(
        ["psql", "--dbname", dsn, "-v", "ON_ERROR_STOP=1", *args],
        check=True,
    )


def pytest_sessionstart(session) -> None:
    """Rebuild the disposable local integration schema before a test session.

    CI already provisions and bootstraps its own PostgreSQL service, so this is
    only applied when pytest is using the local default test DSN. Unit-only
    runs (``-m 'not integration'``) do not touch PostgreSQL.
    """
    if not _USING_LOCAL_DEFAULT:
        return

    markexpr = (session.config.option.markexpr or "").strip().lower()
    if markexpr and "not integration" in markexpr:
        return

    dsn = os.environ["TEST_DATABASE_URL"]
    root = Path(__file__).resolve().parents[1]

    # The database is dedicated to tests, so clearing these schemas makes every
    # local integration run repeatable and removes rows left by previous runs.
    _run_psql(
        dsn,
        "-c",
        "DROP SCHEMA IF EXISTS ingest_blue CASCADE; "
        "DROP SCHEMA IF EXISTS ingest_green CASCADE; "
        "DROP SCHEMA IF EXISTS ingest CASCADE; "
        "DROP SCHEMA IF EXISTS ops CASCADE; "
        "DROP SCHEMA IF EXISTS enrichment CASCADE; "
        "DROP SCHEMA IF EXISTS lineage CASCADE; "
        "DROP SCHEMA IF EXISTS budget CASCADE;",
    )

    for relative_path in (
        "sql/schema_phase1.sql",
        "sql/receipt_processing.sql",
        "sql/schema_constraints.sql",
        "sql/product_enrichment.sql",
        "sql/receipt_reprocess_requests.sql",
        "sql/ocr_result_collector.sql",
    ):
        _run_psql(dsn, "-f", str(root / relative_path))
