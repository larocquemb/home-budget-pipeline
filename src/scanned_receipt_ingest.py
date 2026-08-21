"""Compatibility shim for the packaged receipt ingestion module."""

from home_budget_pipeline.receipts.ingest import *  # noqa: F401,F403
from home_budget_pipeline.receipts.ingest import _db_connect  # noqa: F401
