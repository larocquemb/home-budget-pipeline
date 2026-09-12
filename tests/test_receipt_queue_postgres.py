"""Real, independent PostgreSQL sessions/processes; OCR alone is replaced."""

import hashlib
import multiprocessing
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from home_budget_pipeline.receipts import backlog_ingest as backlog
from home_budget_pipeline.receipts.message import InvalidReceiptMessage, ReceiptMessage

pytestmark = pytest.mark.integration


def receipt_for(path):
    sha = backlog.scan.sha256_file(path)
    return backlog.scan.ScannedReceipt(
        path=str(path), source_reference=path.name, source_sha256=sha,
        merchant=f"Queue test {sha[:12]}", transaction_date="2026-09-01",
        receipt_id=sha, subtotal=5.0, tax=0.0, total=5.0,
        items=[backlog.scan.ScannedItem("Test item", 5.0)],
        text="Test item 5.00\nTOTAL 5.00", extraction_status="complete",
        extraction_confidence=1.0,
    )


def child_process(dsn, filename, entered, release, results):
    import psycopg

    path = Path(filename)
    receipt = receipt_for(path)
    def parse(*args, **kwargs):
        entered.put(os.getpid())
        if not release.wait(15):
            raise TimeoutError("test processing gate timed out")
        return [receipt]
    try:
        with psycopg.connect(dsn) as conn, patch.object(backlog, "parse_scans_parallel", parse):
            status = backlog.process_candidate(
                conn, backlog.ReceiptCandidate(path, receipt.source_sha256),
                path.parent, path.parent / "cache", max_attempts=3, verify_source=True,
            )
        results.put(status)
    except Exception as exc:
        results.put(f"{type(exc).__name__}: {exc}")
        raise


@pytest.fixture
def source(tmp_path):
    # The disposable ingest schema persists for the full test session. Use a
    # unique source reference as well as unique contents so parametrized cases
    # cannot leave multiple hashes under the same reference.
    path = tmp_path / f"receipt-{uuid.uuid4().hex}.pdf"
    path.write_bytes(uuid.uuid4().bytes)
    return path


@pytest.mark.parametrize("same_source", [True, False])
def test_separate_processes_serialize_duplicates_but_allow_different_receipts(source, same_source):
    import psycopg

    ctx = multiprocessing.get_context("spawn")
    entered, results = ctx.Queue(), ctx.Queue()
    release = ctx.Event()
    other = source if same_source else source.with_name("other.pdf")
    if not same_source:
        other.write_bytes(uuid.uuid4().bytes)
    processes = [ctx.Process(
        target=child_process,
        args=(os.environ["TEST_DATABASE_URL"], str(path), entered, release, results),
    ) for path in (source, other)]
    try:
        for process in processes:
            process.start()
        entered.get(timeout=15)
        if not same_source:
            # Both must enter OCR before either is released: no global lock.
            entered.get(timeout=15)
        release.set()
        statuses = sorted(results.get(timeout=20) for _ in processes)
        assert statuses == (["skipped", "succeeded"] if same_source else ["succeeded", "succeeded"])
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        entered.close()
        results.close()
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        for path in set((source, other)):
            sha = backlog.scan.sha256_file(path)
            assert conn.execute(
                "SELECT status, attempts FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (sha,),
            ).fetchone() == ("succeeded", 1)
            assert conn.execute(
                "SELECT count(*) FROM budget.receipt_evidence WHERE source_sha256 = %s", (sha,),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM budget.expense_items i JOIN budget.receipt_evidence e "
                "ON e.expense_pk = i.expense_pk WHERE e.source_sha256 = %s", (sha,),
            ).fetchone() == (1,)


def test_failed_completion_rolls_back_evidence_and_can_retry(source, monkeypatch):
    import psycopg

    receipt = receipt_for(source)
    candidate = backlog.ReceiptCandidate(source, receipt.source_sha256)
    monkeypatch.setattr(backlog, "parse_scans_parallel", lambda *a, **kw: [receipt])
    original = backlog._mark_completed
    def fail_completion(*args):
        raise RuntimeError("failed between evidence writes and completion")
    monkeypatch.setattr(backlog, "_mark_completed", fail_completion)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with pytest.raises(RuntimeError):
            backlog.process_candidate(conn, candidate, source.parent, source.parent / "cache")
        assert conn.execute(
            "SELECT count(*) FROM budget.receipt_evidence WHERE source_sha256 = %s", (receipt.source_sha256,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT status, attempts FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (receipt.source_sha256,),
        ).fetchone() == ("failed", 1)
        conn.commit()
        monkeypatch.setattr(backlog, "_mark_completed", original)
        assert backlog.process_candidate(conn, candidate, source.parent, source.parent / "cache") == "succeeded"
        # Simulate successful DB commit followed by death before the broker ACK.
        assert backlog.process_candidate(conn, candidate, source.parent, source.parent / "cache") == "skipped"


def test_retry_budget_survives_fresh_publication_and_source_mutation(source, monkeypatch):
    import psycopg
    from home_budget_pipeline.receipts.queue_ingest import publication_plan

    sha = backlog.scan.sha256_file(source)
    candidate = backlog.ReceiptCandidate(source, sha)
    def parse(*args, **kwargs):
        raise RuntimeError("OCR failed")
    monkeypatch.setattr(backlog, "parse_scans_parallel", parse)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        for _ in range(3):
            with pytest.raises(RuntimeError, match="OCR failed"):
                backlog.process_candidate(conn, candidate, source.parent, source.parent / "cache", max_attempts=3)
        with pytest.raises(backlog.RetryExhausted):
            backlog.process_candidate(conn, candidate, source.parent, source.parent / "cache", max_attempts=3)
        assert publication_plan(conn, source.parent)[0] == []
        # A stale message cannot persist another file under its advertised hash.
        stale = ReceiptMessage(hashlib.sha256(b"different contents").hexdigest(), source.name)
        with pytest.raises(InvalidReceiptMessage, match="changed since publication"):
            backlog.process_candidate(
                conn, backlog.ReceiptCandidate(source, stale.source_sha256),
                source.parent, source.parent / "cache", verify_source=True,
            )


def test_lost_database_session_releases_source_lock(source):
    import psycopg

    sha = backlog.scan.sha256_file(source)
    conn = psycopg.connect(os.environ["TEST_DATABASE_URL"])
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"home-budget-receipt:{sha}",))
    conn.commit()
    conn.close()
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as replacement:
        replacement.execute("SET lock_timeout = '2s'")
        with backlog.receipt_lock(replacement, sha):
            pass


@pytest.mark.parametrize("status", ["processing", "failed", "succeeded", "review_required"])
def test_retry_reset_respects_real_worker_lock_and_completion(source, status):
    import psycopg
    from home_budget_pipeline.receipts.queue_ingest import publication_plan

    sha = backlog.scan.sha256_file(source)
    candidate = backlog.ReceiptCandidate(source, sha)
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        backlog._mark_processing(conn, candidate, source.parent, "ingest")
        conn.execute(
            "UPDATE ingest.receipt_processing_status SET status = %s, attempts = 5 WHERE source_sha256 = %s",
            (status, sha),
        )
        conn.commit()
        with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as worker:
            with backlog.receipt_lock(worker, sha):
                with pytest.raises(RuntimeError, match="currently being processed"):
                    backlog.reset_receipt_for_retry(conn, source.name)
                assert conn.execute(
                    "SELECT status, attempts FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (sha,),
                ).fetchone() == (status, 5)
                conn.commit()
        if status in ("succeeded", "review_required"):
            with pytest.raises(ValueError, match="already completed"):
                backlog.reset_receipt_for_retry(conn, source.name)
            assert conn.execute(
                "SELECT status, attempts FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (sha,),
            ).fetchone() == (status, 5)
        else:
            assert backlog.reset_receipt_for_retry(conn, source.name)["retry_ready"] is True
            assert conn.execute(
                "SELECT status, attempts FROM ingest.receipt_processing_status WHERE source_sha256 = %s", (sha,),
            ).fetchone() == ("failed", 0)
            conn.commit()
            messages, summary = publication_plan(conn, source.parent)
            assert [message.source_sha256 for message in messages] == [sha]
            assert summary["exhausted"] == 0
