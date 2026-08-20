from pathlib import Path

import pytest

from home_budget_pipeline.receipts import backlog_ingest


class FakeCursor:
    def __init__(self, *, existing=(), lock=True):
        self.existing = tuple(existing)
        self.lock = lock
        self.sql = []
        self.params = []
        self._mode = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(params)
        if "pg_try_advisory_lock" in sql:
            self._mode = "lock"
        elif "pg_advisory_unlock" in sql:
            self._mode = "unlock"
        elif "receipt_evidence" in sql:
            self._mode = "existing"

    def fetchone(self):
        if self._mode == "lock":
            return (self.lock,)
        if self._mode == "unlock":
            return (True,)
        raise AssertionError("unexpected fetchone")

    def fetchall(self):
        if self._mode == "existing":
            return [(value,) for value in self.existing]
        raise AssertionError("unexpected fetchall")


class FakeConn:
    def __init__(self, *, existing=(), lock=True):
        self.cursor_obj = FakeCursor(existing=existing, lock=lock)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_plan_unprocessed_receipts_skips_existing_hashes():
    paths = [Path("one.pdf"), Path("two.jpg"), Path("three.png")]
    hashes = {paths[0]: "aaa", paths[1]: "bbb", paths[2]: "ccc"}
    conn = FakeConn(existing=("bbb",))

    plan = backlog_ingest.plan_unprocessed_receipts(
        conn,
        Path("/receipts"),
        discover=lambda root: paths,
        hasher=hashes.__getitem__,
    )

    assert [candidate.path for candidate in plan.pending] == [paths[0], paths[2]]
    assert [candidate.path for candidate in plan.skipped] == [paths[1]]
    assert len(plan.discovered) == 3


def test_plan_empty_discovery_does_not_require_hash_rows():
    conn = FakeConn()
    plan = backlog_ingest.plan_unprocessed_receipts(
        conn,
        Path("/receipts"),
        discover=lambda root: [],
        hasher=lambda path: pytest.fail("hasher should not be called"),
    )
    assert plan.discovered == ()
    assert plan.pending == ()
    assert plan.skipped == ()


def test_acquire_backlog_lock_uses_named_postgres_advisory_lock():
    conn = FakeConn(lock=True)
    assert backlog_ingest.acquire_backlog_lock(conn) is True
    assert "pg_try_advisory_lock" in conn.cursor_obj.sql[-1]
    assert conn.cursor_obj.params[-1] == (backlog_ingest.LOCK_NAME,)


def test_acquire_backlog_lock_reports_busy_processor():
    conn = FakeConn(lock=False)
    assert backlog_ingest.acquire_backlog_lock(conn) is False


def test_process_backlog_returns_without_ocr_when_everything_is_skipped(monkeypatch):
    conn = FakeConn(lock=True)
    plan = backlog_ingest.DiscoveryPlan(
        discovered=(backlog_ingest.ReceiptCandidate(Path("one.pdf"), "aaa"),),
        pending=(),
        skipped=(backlog_ingest.ReceiptCandidate(Path("one.pdf"), "aaa"),),
    )
    monkeypatch.setattr(backlog_ingest, "plan_unprocessed_receipts", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        backlog_ingest,
        "parse_scans_parallel",
        lambda *args, **kwargs: pytest.fail("OCR should not run for skipped receipts"),
    )

    summary = backlog_ingest.process_backlog(conn, Path("/receipts"), Path("/cache"))

    assert summary == {"discovered": 1, "skipped": 1, "processed": 0, "review_required": 0}


def test_process_backlog_rejects_concurrent_run(monkeypatch):
    conn = FakeConn(lock=False)
    monkeypatch.setattr(
        backlog_ingest,
        "plan_unprocessed_receipts",
        lambda *args, **kwargs: pytest.fail("discovery should not run without lock"),
    )

    with pytest.raises(RuntimeError, match="already running"):
        backlog_ingest.process_backlog(conn, Path("/receipts"), Path("/cache"))
