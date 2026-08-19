#!/usr/bin/env python3
"""Parallel front-end for scanned_receipt_ingest.

Receipt parsing/OCR is parallelized across files. Each receipt remains internally
sequential so page ordering and overlap handling are unchanged. Database writes
remain sequential on one connection for predictable transactions.
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import scanned_receipt_ingest as scan


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parallel ingest of scanned paper receipts.")
    p.add_argument("input_path", help="Scan file or directory containing scanned receipts.")
    p.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1), help="Concurrent receipt workers (default: min(6, CPU count)).")
    p.add_argument("--write-db", action="store_true", help="Upsert budget.expenses and budget.expense_items after OCR completes.")
    p.add_argument("--db-dsn", default=os.getenv("HOME_BUDGET_PG_DSN", ""))
    p.add_argument("--db-schema", default="budget")
    p.add_argument("--json", dest="json_path", default="", help="Optional JSON report path (keep local; may contain financial data).")
    p.add_argument("--show-review", action="store_true", help="Print receipts requiring review or unreadable.")
    return p.parse_args()


def parse_scans_parallel(paths: list[Path], root: Path, workers: int) -> list[scan.ScannedReceipt]:
    workers = max(1, workers)
    if workers == 1:
        return [scan.parse_scan(path, root) for path in paths]

    # executor.map preserves input ordering while OCR/rendering happens concurrently.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="receipt") as executor:
        return list(executor.map(lambda path: scan.parse_scan(path, root), paths))


def main() -> int:
    args = parse_args()
    root = Path(args.input_path).expanduser().resolve()
    paths = scan.discover_scans(root)
    receipts = parse_scans_parallel(paths, root, args.workers)

    if args.write_db:
        conn = scan._db_connect(args.db_dsn)
        try:
            for receipt in receipts:
                scan.upsert_receipt(conn, receipt, args.db_schema)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    report = {
        "discovered": len(paths),
        "complete": sum(r.extraction_status == "complete" for r in receipts),
        "review": sum(r.extraction_status == "review" for r in receipts),
        "unreadable": sum(r.extraction_status == "unreadable" for r in receipts),
        "workers": max(1, args.workers),
        "receipts": [scan.receipt_to_dict(r) for r in receipts],
    }
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "receipts"}, indent=2))
    if args.show_review:
        scan.print_review(receipts)
    return 0 if paths else 2


if __name__ == "__main__":
    raise SystemExit(main())
