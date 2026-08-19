#!/usr/bin/env python3
"""Parallel front-end for scanned_receipt_ingest with local OCR caching.

Receipt parsing/OCR is parallelized across files using worker processes because
PDFium/pypdfium2 is not thread-safe. Expensive page text extraction is cached by
source SHA-256 so parser-only changes do not rerun PDFium/Tesseract. Database
writes remain sequential on one connection for predictable transactions.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import scanned_receipt_ingest as scan
from receipt_datetime import date_part, extract_transaction_datetime
from receipt_evidence import attach_evidence, find_match, upsert_evidence
from receipt_payment import extract_payment_provenance


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Parallel ingest of scanned paper receipts.")
    p.add_argument("input_path", help="Scan file or directory containing scanned receipts.")
    p.add_argument("--workers", type=int, default=min(12, os.cpu_count() or 1), help="Concurrent receipt worker processes (default: min(12, CPU count)).")
    p.add_argument("--ocr-cache", default=".ocr_cache", help="Local OCR page-text cache directory (default: .ocr_cache).")
    p.add_argument("--refresh-ocr-cache", action="store_true", help="Ignore cached OCR and rebuild it from source scans.")
    p.add_argument("--write-db", action="store_true", help="Persist receipt evidence and reconcile it to canonical expenses.")
    p.add_argument("--db-dsn", default=os.getenv("HOME_BUDGET_PG_DSN", ""))
    p.add_argument("--db-schema", default="budget")
    p.add_argument("--json", dest="json_path", default="", help="Optional JSON report path (keep local; may contain financial data).")
    p.add_argument("--show-review", action="store_true", help="Print receipts requiring review or unreadable.")
    return p.parse_args()


def _read_or_create_pages(path: Path, source_sha256: str, cache_dir: Path, refresh: bool) -> tuple[list[str], bool]:
    cache_path = cache_dir / f"{source_sha256}.json"
    if not refresh:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            pages = data.get("page_text")
            if data.get("source_sha256") == source_sha256 and isinstance(pages, list) and all(isinstance(p, str) for p in pages):
                return pages, True
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    pages = scan.extract_page_text(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_version": 1,
        "source_sha256": source_sha256,
        "page_text": list(pages),
    }
    tmp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(cache_path)
    return list(pages), False


def _parse_scan_cached(path: Path, root: Path, cache_dir: Path, refresh: bool) -> tuple[scan.ScannedReceipt, bool]:
    source_sha256 = scan.sha256_file(path)
    pages, cache_hit = _read_or_create_pages(path, source_sha256, cache_dir, refresh)
    text = scan.merge_page_text(pages)
    subtotal, tax, total = scan.extract_totals(text)
    payment = extract_payment_provenance(text)
    transaction_datetime = extract_transaction_datetime(text)
    transaction_date = date_part(transaction_datetime) or scan.extract_date(text)
    try:
        reference = str(path.relative_to(root)) if root.is_dir() else path.name
    except ValueError:
        reference = path.name

    receipt = scan.ScannedReceipt(
        path=str(path),
        source_reference=reference,
        source_sha256=source_sha256,
        merchant=scan.extract_merchant(text),
        transaction_date=transaction_date,
        receipt_id=scan.extract_receipt_id(text),
        subtotal=subtotal,
        tax=tax,
        total=total,
        payment_method=payment.payment_method,
        card_last4=payment.card_last4,
        items=scan.extract_items(text),
        page_text=list(pages),
        text=text,
    )
    receipt.transaction_datetime = transaction_datetime
    receipt.extraction_confidence = scan.confidence_for(receipt)
    receipt.review_reasons = scan.review_reasons_for(receipt)
    receipt.extraction_status = scan.status_for(receipt)
    return receipt, cache_hit


def _parse_scan_worker(args: tuple[str, str, str, bool]) -> tuple[scan.ScannedReceipt, bool]:
    path_str, root_str, cache_dir_str, refresh = args
    return _parse_scan_cached(Path(path_str), Path(root_str), Path(cache_dir_str), refresh)


def parse_scans_parallel(
    paths: list[Path],
    root: Path,
    workers: int,
    cache_dir: Path | None = None,
    refresh: bool = False,
    *,
    return_cache_hits: bool = False,
):
    """Parse scans while preserving the original helper API.

    Existing callers that omit cache_dir receive only the ordered receipt list,
    matching the pre-cache behavior. The CLI supplies cache_dir and asks for
    cache statistics explicitly.
    """
    workers = max(1, workers)

    if cache_dir is None:
        # Compatibility path used by existing programmatic callers/tests. It
        # deliberately avoids PDFium worker processes when scan.parse_scan is
        # patched or otherwise controlled by the caller.
        receipts = [scan.parse_scan(path, root) for path in paths]
        return (receipts, 0) if return_cache_hits else receipts

    if workers == 1:
        results = [_parse_scan_cached(path, root, cache_dir, refresh) for path in paths]
    else:
        work = [(str(path), str(root), str(cache_dir), refresh) for path in paths]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_parse_scan_worker, work))

    receipts = [receipt for receipt, _ in results]
    cache_hits = sum(1 for _, hit in results if hit)
    return (receipts, cache_hits) if return_cache_hits else receipts


def _receipt_to_dict(receipt: scan.ScannedReceipt) -> dict:
    data = scan.receipt_to_dict(receipt)
    data["transaction_datetime"] = getattr(receipt, "transaction_datetime", None)
    return data


def _persist_transaction_datetime(conn, receipt: scan.ScannedReceipt, schema: str) -> None:
    value = getattr(receipt, "transaction_datetime", None)
    if not value:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {schema}.expenses SET transaction_datetime = %s WHERE source = 'scanned' AND order_id = %s",
            (value, receipt.canonical_order_id),
        )


def persist_evidence_first(conn, receipts: list[scan.ScannedReceipt], schema: str) -> dict[str, int]:
    """Persist evidence, reconciling before creating a new canonical expense."""
    stats = {"matched": 0, "ambiguous": 0, "new": 0}

    for receipt in receipts:
        evidence_id = upsert_evidence(conn, receipt, schema, evidence_type="scanned")
        match = find_match(conn, receipt, schema)

        if match.disposition == "matched" and match.expense_pk is not None:
            attach_evidence(conn, evidence_id, match.expense_pk, schema)
            stats["matched"] += 1
            continue

        if match.disposition == "ambiguous":
            stats["ambiguous"] += 1
            continue

        expense_pk = scan.upsert_receipt(conn, receipt, schema)
        _persist_transaction_datetime(conn, receipt, schema)
        attach_evidence(conn, evidence_id, expense_pk, schema)
        stats["new"] += 1

    return stats


def main() -> int:
    args = parse_args()
    started = time.perf_counter()

    root = Path(args.input_path).expanduser().resolve()
    cache_dir = Path(args.ocr_cache).expanduser().resolve()
    paths = scan.discover_scans(root)
    receipts, cache_hits = parse_scans_parallel(
        paths,
        root,
        args.workers,
        cache_dir,
        args.refresh_ocr_cache,
        return_cache_hits=True,
    )

    db_stats = None
    if args.write_db:
        conn = scan._db_connect(args.db_dsn)
        try:
            db_stats = persist_evidence_first(conn, receipts, args.db_schema)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    elapsed_seconds = round(time.perf_counter() - started, 3)
    receipts_per_second = round(len(paths) / elapsed_seconds, 3) if elapsed_seconds > 0 else None

    report = {
        "discovered": len(paths),
        "complete": sum(r.extraction_status == "complete" for r in receipts),
        "review": sum(r.extraction_status == "review" for r in receipts),
        "unreadable": sum(r.extraction_status == "unreadable" for r in receipts),
        "workers": max(1, args.workers),
        "executor": "process",
        "ocr_cache_hits": cache_hits,
        "ocr_cache_misses": len(paths) - cache_hits,
        "elapsed_seconds": elapsed_seconds,
        "receipts_per_second": receipts_per_second,
        "receipts": [_receipt_to_dict(r) for r in receipts],
    }
    if db_stats is not None:
        report["db_reconciliation"] = db_stats

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "receipts"}, indent=2))
    if args.show_review:
        scan.print_review(receipts)
    return 0 if paths else 2


if __name__ == "__main__":
    raise SystemExit(main())
