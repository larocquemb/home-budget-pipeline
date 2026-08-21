"""Discover and process receipt files using disposable ingest state."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import ingest as scan
from .parallel_ingest import parse_scans_parallel, persist_evidence_first

LOCK_NAME = "home-budget-receipt-backlog"


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


def _source_reference(candidate: ReceiptCandidate, root: Path) -> str:
    try:
        return str(candidate.path.relative_to(root))
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


def process_backlog(
    conn,
    root: Path,
    cache_dir: Path,
    *,
    workers: int = 2,
    ingest_schema: str = "ingest",
    budget_schema: str = "budget",
    refresh_ocr_cache: bool = False,
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

    try:
        plan = plan_unprocessed_receipts(conn, root, ingest_schema=ingest_schema)
        summary["discovered"] = len(plan.discovered)
        summary["skipped"] = len(plan.skipped)

        for candidate in plan.pending:
            try:
                _mark_processing(conn, candidate, root, ingest_schema)
                conn.commit()

                receipts = parse_scans_parallel(
                    [candidate.path],
                    root,
                    max(1, workers),
                    cache_dir,
                    refresh_ocr_cache,
                )
                if len(receipts) != 1:
                    raise RuntimeError(f"expected one parsed receipt, got {len(receipts)}")

                receipt = receipts[0]
                persist_evidence_first(conn, [receipt], budget_schema)
                status = "review_required" if receipt.extraction_status != "complete" else "succeeded"
                _mark_completed(conn, candidate, root, ingest_schema, status)
                conn.commit()

                if status == "review_required":
                    summary["review_required"] += 1
                else:
                    summary["succeeded"] += 1
            except Exception as exc:
                conn.rollback()
                _mark_failed(conn, candidate, root, ingest_schema, exc)
                conn.commit()
                summary["failed"] += 1

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
    parser = argparse.ArgumentParser(description="Process unprocessed receipt files into BrownRook Ledger.")
    parser.add_argument(
        "receipt_root",
        nargs="?",
        default=_default_path(
            "receipts/raw/scanned/inbox",
            "/data/receipts/raw/scanned/inbox",
            "RECEIPT_SOURCE_ROOT",
        ),
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--ocr-cache",
        default=_default_path(
            "receipts/derived/ocr-cache",
            "/data/receipts/derived/ocr-cache",
            "HOME_BUDGET_OCR_CACHE",
        ),
    )
    parser.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    parser.add_argument("--ingest-schema", default="ingest")
    parser.add_argument("--budget-schema", default="budget")
    parser.add_argument("--refresh-ocr-cache", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    root = Path(args.receipt_root).expanduser().resolve()
    cache_dir = Path(args.ocr_cache).expanduser().resolve()
    conn = scan._db_connect(args.db_dsn)
    try:
        summary = process_backlog(
            conn,
            root,
            cache_dir,
            workers=max(1, args.workers),
            ingest_schema=args.ingest_schema,
            budget_schema=args.budget_schema,
            refresh_ocr_cache=args.refresh_ocr_cache,
        )
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
