"""Discover and process receipt files using disposable ingest state."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import ingest as scan
from .parallel_ingest import parse_scans_parallel, persist_evidence_first

LOCK_NAME = "home-budget-receipt-backlog"


class RetryExhausted(RuntimeError):
    """The persisted attempt budget has been consumed."""


def validate_schema(schema: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("invalid database schema")
    return schema


@contextmanager
def receipt_lock(conn, source_sha256: str):
    """Serialize one source across hosts, including commits made during OCR."""
    key = f"home-budget-receipt:{source_sha256}"
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
        conn.commit()


@dataclass(frozen=True)
class ReceiptCandidate:
    path: Path
    source_sha256: str


@dataclass(frozen=True)
class DiscoveryPlan:
    discovered: tuple[ReceiptCandidate, ...]
    pending: tuple[ReceiptCandidate, ...]
    skipped: tuple[ReceiptCandidate, ...]


def _completed_hashes(conn, hashes: Iterable[str], ingest_schema: str = "ingest") -> set[str]:
    values = tuple(dict.fromkeys(hashes))
    if not values:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT source_sha256
              FROM {ingest_schema}.receipt_processing_status
             WHERE source_sha256 = ANY(%s)
               AND status IN ('succeeded', 'review_required')
            """,
            (list(values),),
        )
        return {row[0] for row in cur.fetchall()}


def plan_unprocessed_receipts(
    conn,
    root: Path,
    *,
    ingest_schema: str = "ingest",
    discover: Callable[[Path], list[Path]] = scan.discover_scans,
    hasher: Callable[[Path], str] = scan.sha256_file,
) -> DiscoveryPlan:
    """Hash files and partition them using rebuildable ingest processing state."""
    validate_schema(ingest_schema)
    candidates = tuple(ReceiptCandidate(path, hasher(path)) for path in discover(root))
    completed = _completed_hashes(conn, (candidate.source_sha256 for candidate in candidates), ingest_schema)
    pending = tuple(candidate for candidate in candidates if candidate.source_sha256 not in completed)
    skipped = tuple(candidate for candidate in candidates if candidate.source_sha256 in completed)
    return DiscoveryPlan(candidates, pending, skipped)


def acquire_backlog_lock(conn) -> bool:
    """Take a session-level advisory lock so only one backlog processor runs at once."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_NAME,))
        return bool(cur.fetchone()[0])


def release_backlog_lock(conn) -> None:
    """Release the session lock from a clean transaction state."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
    conn.commit()


def reset_receipt_for_retry(conn, source_reference: str, *, ingest_schema: str = "ingest") -> dict:
    """Reset one unfinished receipt while excluding active receipt processors.

    The transaction advisory lock uses the same key as receipt_lock. Read the
    status after taking it so a just-completed receipt cannot be reset by a race.
    """
    validate_schema(ingest_schema)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT source_sha256 FROM {ingest_schema}.receipts WHERE source_reference = %s",
                (source_reference,),
            )
            matches = cur.fetchall()
            if not matches:
                raise ValueError("no receipt found for that source reference")
            if len(matches) != 1:
                raise ValueError("source reference matches multiple receipt hashes; cannot safely select one")
            source_sha256 = matches[0][0]
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"home-budget-receipt:{source_sha256}",),
            )
            if not cur.fetchone()[0]:
                raise RuntimeError("receipt is currently being processed; retry after that processor finishes")
            cur.execute(
                f"SELECT status, attempts FROM {ingest_schema}.receipt_processing_status "
                "WHERE source_sha256 = %s FOR UPDATE",
                (source_sha256,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("receipt has no processing attempts to reset; run receipts publish")
            status, attempts = row
            if status not in ("failed", "processing"):
                raise ValueError(f"receipt is already completed ({status}); retry would discard its completion state")
            cur.execute(
                f"UPDATE {ingest_schema}.receipt_processing_status "
                "SET status = 'failed', attempts = 0, first_attempted_at = NULL, "
                "last_attempted_at = NULL, last_error = NULL, completed_at = NULL "
                "WHERE source_sha256 = %s",
                (source_sha256,),
            )
        conn.commit()
        return {
            "source_reference": source_reference,
            "source_sha256": source_sha256,
            "previous_status": status,
            "previous_attempts": attempts,
            "attempts": 0,
            "retry_ready": True,
        }
    except Exception:
        conn.rollback()
        raise


def _source_reference(candidate: ReceiptCandidate, root: Path) -> str:
    try:
        return candidate.path.relative_to(root).as_posix()
    except ValueError:
        return candidate.path.name


def _upsert_source_identity(conn, candidate: ReceiptCandidate, root: Path, ingest_schema: str) -> str:
    reference = _source_reference(candidate, root)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {ingest_schema}.receipts (source_sha256, source_reference)
            VALUES (%s, %s)
            ON CONFLICT (source_sha256) DO UPDATE SET
                source_reference = EXCLUDED.source_reference
            """,
            (candidate.source_sha256, reference),
        )
    return reference


def _mark_processing(conn, candidate: ReceiptCandidate, root: Path, ingest_schema: str) -> None:
    _upsert_source_identity(conn, candidate, root, ingest_schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {ingest_schema}.receipt_processing_status (
                source_sha256, status, attempts,
                first_attempted_at, last_attempted_at, last_error, completed_at
            )
            VALUES (%s, 'processing', 1, NOW(), NOW(), NULL, NULL)
            ON CONFLICT (source_sha256) DO UPDATE SET
                status = 'processing',
                attempts = {ingest_schema}.receipt_processing_status.attempts + 1,
                last_attempted_at = NOW(),
                last_error = NULL,
                completed_at = NULL
            """,
            (candidate.source_sha256,),
        )


def _mark_completed(conn, candidate: ReceiptCandidate, root: Path, ingest_schema: str, status: str) -> None:
    _upsert_source_identity(conn, candidate, root, ingest_schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {ingest_schema}.receipt_processing_status (
                source_sha256, status, attempts,
                first_attempted_at, last_attempted_at, completed_at, last_error
            )
            VALUES (%s, %s, 1, NOW(), NOW(), NOW(), NULL)
            ON CONFLICT (source_sha256) DO UPDATE SET
                status = EXCLUDED.status,
                completed_at = NOW(),
                last_error = NULL
            """,
            (candidate.source_sha256, status),
        )


def _mark_failed(conn, candidate: ReceiptCandidate, root: Path, ingest_schema: str, exc: Exception) -> None:
    _upsert_source_identity(conn, candidate, root, ingest_schema)
    message = f"{type(exc).__name__}: {exc}"[:4000]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {ingest_schema}.receipt_processing_status (
                source_sha256, status, attempts,
                first_attempted_at, last_attempted_at, last_error, completed_at
            )
            VALUES (%s, 'failed', 1, NOW(), NOW(), %s, NULL)
            ON CONFLICT (source_sha256) DO UPDATE SET
                status = 'failed',
                last_attempted_at = NOW(),
                last_error = EXCLUDED.last_error,
                completed_at = NULL
            """,
            (candidate.source_sha256, message),
        )


def process_candidate(
    conn,
    candidate: ReceiptCandidate,
    root: Path,
    cache_dir: Path,
    *,
    workers: int = 1,
    ingest_schema: str = "ingest",
    budget_schema: str = "budget",
    refresh_ocr_cache: bool = False,
    max_attempts: int | None = None,
    verify_source: bool = False,
    reprocess_request_id: str | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> str:
    """Commit one receipt atomically; recheck completion after taking its lock."""
    validate_schema(ingest_schema)
    validate_schema(budget_schema)
    reference = _source_reference(candidate, root)
    with receipt_lock(conn, candidate.source_sha256):
        if reprocess_request_id:
            if not refresh_ocr_cache:
                raise ValueError("a reprocess request must refresh OCR")
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {budget_schema}.receipt_reprocess_requests (source_sha256, request_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (candidate.source_sha256, reprocess_request_id),
                )
                cur.execute(
                    f"SELECT attempts, completed_at FROM {budget_schema}.receipt_reprocess_requests "
                    "WHERE source_sha256 = %s AND request_id = %s FOR UPDATE",
                    (candidate.source_sha256, reprocess_request_id),
                )
                request_attempts, completed_at = cur.fetchone()
                if completed_at is not None:
                    conn.commit()
                    return "skipped"
                if max_attempts is not None and request_attempts >= max_attempts:
                    raise RetryExhausted("reprocess request attempt limit reached")
                cur.execute(
                    f"UPDATE {budget_schema}.receipt_reprocess_requests SET attempts = attempts + 1 "
                    "WHERE source_sha256 = %s AND request_id = %s",
                    (candidate.source_sha256, reprocess_request_id),
                )
        if not refresh_ocr_cache and candidate.source_sha256 in _completed_hashes(
            conn, [candidate.source_sha256], ingest_schema
        ):
            conn.commit()
            return "skipped"
        if max_attempts is not None and not reprocess_request_id:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT attempts FROM {ingest_schema}.receipt_processing_status WHERE source_sha256 = %s",
                    (candidate.source_sha256,),
                )
                row = cur.fetchone()
            if row and row[0] >= max_attempts:
                error = RetryExhausted("receipt attempt limit reached")
                _mark_failed(conn, candidate, root, ingest_schema, error)
                conn.commit()
                raise error
        try:
            _mark_processing(conn, candidate, root, ingest_schema)
            conn.commit()
            if verify_source and scan.sha256_file(candidate.path) != candidate.source_sha256:
                from .message import InvalidReceiptMessage
                raise InvalidReceiptMessage("source contents changed since publication")
            progress(f"Running OCR and parsing {reference}")
            receipts = parse_scans_parallel(
                [candidate.path], root, max(1, workers), cache_dir, refresh_ocr_cache,
            )
            if len(receipts) != 1:
                raise RuntimeError(f"expected one parsed receipt, got {len(receipts)}")
            receipt = receipts[0]
            if verify_source and (
                receipt.source_sha256 != candidate.source_sha256
                or scan.sha256_file(candidate.path) != candidate.source_sha256
            ):
                from .message import InvalidReceiptMessage
                raise InvalidReceiptMessage("source contents changed during processing")
            progress(
                f"OCR complete: worker={getattr(receipt, 'worker_host', 'unknown')}:"
                f"{getattr(receipt, 'worker_pid', 'unknown')}, "
                f"status={receipt.extraction_status}, merchant={getattr(receipt, 'merchant', None)!r}, "
                f"total={getattr(receipt, 'total', None)}"
            )
            progress(f"Persisting {reference}")
            persist_evidence_first(conn, [receipt], budget_schema, replace_existing=refresh_ocr_cache)
            status = "review_required" if receipt.extraction_status != "complete" else "succeeded"
            _mark_completed(conn, candidate, root, ingest_schema, status)
            if reprocess_request_id:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {budget_schema}.receipt_reprocess_requests SET completed_at = NOW() "
                        "WHERE source_sha256 = %s AND request_id = %s",
                        (candidate.source_sha256, reprocess_request_id),
                    )
            conn.commit()
            return status
        except Exception as exc:
            conn.rollback()
            _mark_failed(conn, candidate, root, ingest_schema, exc)
            conn.commit()
            raise


def process_backlog(
    conn,
    root: Path,
    cache_dir: Path,
    *,
    workers: int = 2,
    ingest_schema: str = "ingest",
    budget_schema: str = "budget",
    refresh_ocr_cache: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Process pending receipts independently so one failure does not stop the backlog."""
    if not acquire_backlog_lock(conn):
        raise RuntimeError("another receipt backlog processor is already running")

    summary = {
        "discovered": 0,
        "skipped": 0,
        "succeeded": 0,
        "failed": 0,
        "review_required": 0,
    }

    def progress(message: str) -> None:
        if verbose:
            print(message, file=sys.stderr, flush=True)

    try:
        progress(f"Discovering receipts under {root}")
        plan = plan_unprocessed_receipts(conn, root, ingest_schema=ingest_schema)
        pending = plan.discovered if refresh_ocr_cache else plan.pending
        summary["discovered"] = len(plan.discovered)
        summary["skipped"] = 0 if refresh_ocr_cache else len(plan.skipped)
        progress(
            f"Discovered {len(plan.discovered)} receipt(s); "
            f"processing {len(pending)}, skipping {summary['skipped']}"
        )

        for index, candidate in enumerate(pending, start=1):
            reference = _source_reference(candidate, root)
            try:
                progress(f"[{index}/{len(pending)}] Starting {reference}")
                status = process_candidate(
                    conn, candidate, root, cache_dir,
                    workers=workers,
                    ingest_schema=ingest_schema,
                    budget_schema=budget_schema,
                    refresh_ocr_cache=refresh_ocr_cache,
                    progress=lambda message: progress(f"[{index}/{len(pending)}] {message}"),
                )
                summary[status] += 1
                progress(f"[{index}/{len(pending)}] Finished {reference}: {status}")
            except Exception as exc:
                summary["failed"] += 1
                progress(f"[{index}/{len(pending)}] Failed {reference}: {type(exc).__name__}: {exc}")

        progress("Receipt processing complete")
        return summary
    finally:
        release_backlog_lock(conn)


def _default_path(relative_path: str, legacy_default: str, explicit_env: str) -> str:
    explicit = os.getenv(explicit_env)
    if explicit:
        return explicit
    data_root = os.getenv("HOME_BUDGET_DATA_ROOT")
    if data_root:
        return str(Path(data_root).expanduser() / relative_path)
    return legacy_default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from ..cli import build_parser

    return build_parser().parse_args(["receipts", "process", *(sys.argv[1:] if argv is None else argv)])


def main() -> int:
    # Preserve the legacy entry point while enforcing the same queue dispatch.
    from ..cli import main as ledger_main

    return ledger_main(["receipts", "process", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
