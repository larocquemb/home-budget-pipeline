from pathlib import Path

import pytest

from home_budget_pipeline.receipts import backlog_ingest


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        if self.conn.aborted:
            raise RuntimeError("transaction aborted")
        self.conn.statements.append(sql)

    def fetchone(self):
        return (True,)


class _Connection:
    def __init__(self):
        self.aborted = False
        self.rollback_calls = 0
        self.commit_calls = 0
        self.statements = []

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rollback_calls += 1
        self.aborted = False

    def commit(self):
        self.commit_calls += 1


def test_process_backlog_preserves_root_error_when_transaction_is_aborted(monkeypatch):
    conn = _Connection()

    def fail_discovery(*args, **kwargs):
        conn.aborted = True
        raise ValueError("root database failure")

    monkeypatch.setattr(backlog_ingest, "plan_unprocessed_receipts", fail_discovery)

    with pytest.raises(ValueError, match="root database failure"):
        backlog_ingest.process_backlog(
            conn,
            Path("/tmp/receipts"),
            Path("/tmp/ocr-cache"),
        )

    assert conn.rollback_calls == 1
    assert conn.commit_calls == 1
    assert any("pg_advisory_unlock" in sql for sql in conn.statements)
