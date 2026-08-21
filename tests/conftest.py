"""Shared pytest configuration."""

import os


# Keep integration tests isolated from the normal application database while
# making local runs work without an extra environment variable. CI or callers
# can override this explicitly when needed.
os.environ.setdefault(
    "TEST_DATABASE_URL",
    "host=localhost port=5432 dbname=home_budget_test user=paul",
)
