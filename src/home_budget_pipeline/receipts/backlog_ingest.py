"""Discover and process only receipt files not already represented as evidence."""

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


def _existing_hashes(conn, hashes: Iterable[str], schema: str = "budget") -> set[str]:
    values = tuple(dict.fromkeys(hashes))
    if not values:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT source_sha256 FROM {schema}.receipt_evidence WHERE source_sha256 = ANY(%s)",
            (list(values),),
        )
        return {row[0] for row in cur.fetchall()}


def plan_unprocessed_receipts(
    conn,
    root: Path,
    *,
    schema: str = "budget",
    discover: Callable[[Path], list[Path]] = scan.discover_scans,
    hasher: Callable[[Path], str] = scan.sha256_file,
) -> DiscoveryPlan:
    """Hash discovered files and partition them into pending and already processed."""
    candidates = tuple(ReceiptCandidate(path, hasher(path)) for path in discover(root))
    existing = _existing_hashes(conn, (candidate.source_sha256 for candidate in candidates), schema)
    pending = tuple(candidate for candidate in candidates if candidate.source_sha256 not in existing)
    skipped = tuple(candidate for candidate in candidates if candidate.source_sha256 in existing)
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
        return str(candidate.path.relative_to(root)) if root.is_dir() else candidate.path.name
    except ValueError:
        return candidate.path.name


def _mark_processing(conn, candidate: ReceiptCandidate, root: Path, schema: str) -> None:
    reference = _source_reference(candidate, root)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.receipt_processing_status (
                source_sha256, source_reference, status, attempts,
                first_attempted_at, last_attempted_at, last_error, completed_at
            )
            VALUES (%s, %s, 'processing', 1, NOW(), NOW(), NULL, NULL)
            ON CONFLICT (source_sha256) DO UPDATE SET
                source_reference = EXCLUDED.source_reference,
                status = 'processing',
                attempts = {schema}.receipt_processing_status.attempts + 1,
                last_attempted_at = NOW(),
                last_error = NULL,
                completed_at = NULL
            """,
            (candidate.source_sha256, reference),
        )


def _mark_completed(conn, candidate: ReceiptCandidate, root: Path, schema: str, status: str) -> None:
    reference = _source_reference(candidate, root)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.receipt_processing_status (
                source_sha256, source_reference, status, attempts,
                first_attempted_at, last_attempted_at, completed_at, last_error
            )
            VALUES (%s, %s, %s, 1, NOW(), NOW(), NOW(), NULL)
            ON CONFLICT (source_sha256) DO UPDATE SET
                source_reference = EXCLUDED.source_reference,
                status = EXCLUDED.status,
                completed_at = NOW(),
                last_error = NULL
            """,
            (candidate.source_sha256, reference, status),
        )


def _mark_failed(conn, candidate: ReceiptCandidate, root: Path, schema: str, exc: Exception) -> None:
    reference = _source_reference(candidate, root)
    message = f"{type(exc).__name__}: {exc}"[:4000]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.receipt_processing_status (
                source_sha256, source_reference, status, attempts,
                first_attempted_at, last_attempted_at, last_error, completed_at
            )
            VALUES (%s, %s, 'failed', 1, NOW(), NOW(), %s, NULL)
            ON CONFLICT (source_sha256) DO UPDATE SET
                source_reference = EXCLUDED.source_reference,
                status = 'failed',
                last_attempted_at = NOW(),
                last_error = EXCLUDED.last_error,
                completed_at = NULL
            """,
            (candidate.source_sha256, reference, message),
        )


def process_backlog(
    conn,
    root: Path,
    cache_dir: Path,
    *,
    workers: int = 2,
    schema: str = "budget",
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
        plan = plan_unprocessed_receipts(conn, root, schema=schema)
        summary["discovered"] = len(plan.discovered)
        summary["skipped"] = len(plan.skipped)

        for candidate in plan.pending:
            try:
                _mark_processing(conn, candidate, root, schema)
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
                persist_evidence_first(conn, [receipt], schema)
                status = "review_required" if receipt.extraction_status != "complete" else "succeeded"
                _mark_completed(conn, candidate, root, schema, status)
                conn.commit()

                if status == "review_required":
                    summary["review_required"] += 1
                else:
                    summary["succeeded"] += 1
            except Exception as exc:
                conn.rollback()
                _mark_failed(conn, candidate, root, schema, exc)
                conn.commit()
                summary["failed"] += 1

        return summary
    finally:
        release_backlog_lock(conn)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process unprocessed receipt files into BrownRook Ledger.")
    parser.add_argument(
        "receipt_root",
        nargs="?",
        default=os.getenv("RECEIPT_SOURCE_ROOT", "/data/receipts/raw/scanned/inbox"),
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--ocr-cache",
        default=os.getenv("HOME_BUDGET_OCR_CACHE", "/data/receipts/derived/ocr-cache"),
    )
    parser.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    parser.add_argument("--db-schema", default="budget")
    parser.add_argument("--refresh-ocr-cache", action="store_true")
    return parser.parse_args()


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
            schema=args.db_schema,
            refresh_ocr_cache=args.refresh_ocr_cache,
        )
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
