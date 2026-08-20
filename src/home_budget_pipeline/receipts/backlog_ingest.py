"""Discover and process only receipt files not already represented as evidence."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import scanned_receipt_ingest as scan

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
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))


def process_backlog(
    conn,
    root: Path,
    cache_dir: Path,
    *,
    workers: int = 2,
    schema: str = "budget",
    refresh_ocr_cache: bool = False,
) -> dict[str, int]:
    """Process pending receipts through the existing parser/evidence persistence path."""
    if not acquire_backlog_lock(conn):
        raise RuntimeError("another receipt backlog processor is already running")

    try:
        plan = plan_unprocessed_receipts(conn, root, schema=schema)
        pending_paths = [candidate.path for candidate in plan.pending]
        if not pending_paths:
            return {
                "discovered": len(plan.discovered),
                "skipped": len(plan.skipped),
                "processed": 0,
                "review_required": 0,
            }

        receipts = parse_scans_parallel(
            pending_paths,
            root,
            workers,
            cache_dir,
            refresh_ocr_cache,
        )
        persist_evidence_first(conn, receipts, schema)
        conn.commit()
        return {
            "discovered": len(plan.discovered),
            "skipped": len(plan.skipped),
            "processed": len(receipts),
            "review_required": sum(r.extraction_status != "complete" for r in receipts),
        }
    except Exception:
        conn.rollback()
        raise
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
