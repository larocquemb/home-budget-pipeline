from pathlib import Path
from types import SimpleNamespace

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
        elif "SELECT source_sha256" in sql and "receipt_processing_status" in sql:
            self._mode = "existing"
        else:
            self._mode = "write"

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


def _candidate(name, sha):
    return backlog_ingest.ReceiptCandidate(Path(name), sha)


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
        discovered=(_candidate("one.pdf", "aaa"),),
        pending=(),
        skipped=(_candidate("one.pdf", "aaa"),),
    )
    monkeypatch.setattr(backlog_ingest, "plan_unprocessed_receipts", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        backlog_ingest,
        "parse_scans_parallel",
        lambda *args, **kwargs: pytest.fail("OCR should not run for skipped receipts"),
    )

    summary = backlog_ingest.process_backlog(conn, Path("/receipts"), Path("/cache"))

    assert summary == {
        "discovered": 1,
        "skipped": 1,
        "succeeded": 0,
        "failed": 0,
        "review_required": 0,
    }


def test_refresh_ocr_cache_reprocesses_completed_receipts(monkeypatch):
    conn = FakeConn(lock=True)
    candidate = _candidate("completed.pdf", "aaa")
    plan = backlog_ingest.DiscoveryPlan(
        discovered=(candidate,),
        pending=(),
        skipped=(candidate,),
    )
    monkeypatch.setattr(backlog_ingest, "plan_unprocessed_receipts", lambda *args, **kwargs: plan)
    refresh_values = []

    def fake_parse(paths, root, workers, cache_dir, refresh):
        assert paths == [candidate.path]
        refresh_values.append(refresh)
        return [SimpleNamespace(extraction_status="complete")]

    monkeypatch.setattr(backlog_ingest, "parse_scans_parallel", fake_parse)
    monkeypatch.setattr(backlog_ingest, "persist_evidence_first", lambda *args, **kwargs: None)

    summary = backlog_ingest.process_backlog(
        conn,
        Path("/receipts"),
        Path("/cache"),
        refresh_ocr_cache=True,
    )

    assert refresh_values == [True]
    assert summary == {
        "discovered": 1,
        "skipped": 0,
        "succeeded": 1,
        "failed": 0,
        "review_required": 0,
    }


def test_process_backlog_rejects_concurrent_run(monkeypatch):
    conn = FakeConn(lock=False)
    monkeypatch.setattr(
        backlog_ingest,
        "plan_unprocessed_receipts",
        lambda *args, **kwargs: pytest.fail("discovery should not run without lock"),
    )

    with pytest.raises(RuntimeError, match="already running"):
        backlog_ingest.process_backlog(conn, Path("/receipts"), Path("/cache"))


def test_process_backlog_isolates_failed_receipt_and_continues(monkeypatch):
    conn = FakeConn(lock=True)
    first = _candidate("bad.pdf", "aaa")
    second = _candidate("good.pdf", "bbb")
    plan = backlog_ingest.DiscoveryPlan((first, second), (first, second), ())
    monkeypatch.setattr(backlog_ingest, "plan_unprocessed_receipts", lambda *args, **kwargs: plan)

    def fake_parse(paths, *args, **kwargs):
        if paths == [first.path]:
            raise ValueError("broken OCR")
        return [SimpleNamespace(extraction_status="complete")]

    persisted = []
    monkeypatch.setattr(backlog_ingest, "parse_scans_parallel", fake_parse)
    monkeypatch.setattr(backlog_ingest, "persist_evidence_first", lambda conn, receipts, schema: persisted.extend(receipts))

    summary = backlog_ingest.process_backlog(conn, Path("/receipts"), Path("/cache"))

    assert summary["failed"] == 1
    assert summary["succeeded"] == 1
    assert len(persisted) == 1
    assert conn.rollbacks == 2
    assert conn.commits >= 3


def test_process_backlog_records_review_required_separately(monkeypatch):
    conn = FakeConn(lock=True)
    candidate = _candidate("review.pdf", "aaa")
    plan = backlog_ingest.DiscoveryPlan((candidate,), (candidate,), ())
    monkeypatch.setattr(backlog_ingest, "plan_unprocessed_receipts", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        backlog_ingest,
        "parse_scans_parallel",
        lambda *args, **kwargs: [SimpleNamespace(extraction_status="review")],
    )
    monkeypatch.setattr(backlog_ingest, "persist_evidence_first", lambda *args, **kwargs: None)

    summary = backlog_ingest.process_backlog(conn, Path("/receipts"), Path("/cache"))

    assert summary["review_required"] == 1
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0


def test_failed_status_preserves_retryable_hash_and_error():
    conn = FakeConn()
    candidate = _candidate("bad.pdf", "abc123")

    backlog_ingest._mark_failed(conn, candidate, Path("."), "ingest", ValueError("bad receipt"))

    identity_sql = conn.cursor_obj.sql[-2]
    status_sql = conn.cursor_obj.sql[-1]
    status_params = conn.cursor_obj.params[-1]
    assert "INSERT INTO ingest.receipts" in identity_sql
    assert "receipt_processing_status" in status_sql
    assert "status = 'failed'" in status_sql
    assert status_params[0] == "abc123"
    assert "ValueError: bad receipt" in status_params[1]


def test_processing_status_increments_attempts_on_retry():
    conn = FakeConn()
    candidate = _candidate("retry.pdf", "abc123")

    backlog_ingest._mark_processing(conn, candidate, Path("."), "ingest")

    sql = conn.cursor_obj.sql[-1]
    assert "attempts = ingest.receipt_processing_status.attempts + 1" in sql
    assert "status = 'processing'" in sql


def test_source_reference_is_stored_with_sha_identity():
    conn = FakeConn()
    root = Path("/receipts")
    candidate = _candidate("/receipts/2026-08-14/receipt.pdf", "abc123")

    backlog_ingest._mark_processing(conn, candidate, root, "ingest")

    identity_sql = conn.cursor_obj.sql[-2]
    identity_params = conn.cursor_obj.params[-2]
    assert "INSERT INTO ingest.receipts" in identity_sql
    assert identity_params == ("abc123", "2026-08-14/receipt.pdf")
