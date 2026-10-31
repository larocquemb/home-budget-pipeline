"""Shared pytest configuration."""

import os
from pathlib import Path

import pytest


@pytest.fixture
def arsene_development_environment():
    """Generate synthetic dev credentials in memory; never embed credential URLs."""
    import importlib.util
    import sys
    path = Path(__file__).resolve().parents[1] / 'scripts/development_backend.py'
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('test_development_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.client_values({key: 'a' * 64 for key in module.SECRET_KEYS})


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


def pytest_collection_finish(session):
    """Never silently create or reset a local database for integration tests."""
    if not os.getenv('TEST_DATABASE_URL') and any(
        item.get_closest_marker('integration') for item in session.items
    ):
        raise pytest.UsageError(
            'Integration tests need an explicit isolated TEST_DATABASE_URL. '
            'On the development Mac, use make test-db against the Arsene test backend.'
        )
