#!/usr/bin/env python3
"""Parallel front-end for scanned_receipt_ingest with local OCR caching.

Receipt parsing/OCR is parallelized across files using worker processes because
PDFium/pypdfium2 is not thread-safe. Expensive page text extraction is cached by
source SHA-256 so parser-only changes do not rerun PDFium/Tesseract. Database
writes remain sequential on one connection for predictable transactions.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ingest as scan
from receipt_datetime import date_part, extract_transaction_datetime
from receipt_evidence import attach_evidence, find_match, resolve_merchant_alias, upsert_evidence
from receipt_payment import extract_payment_provenance
from receipt_total_reconcile import reconcile_total_from_text


def _page_text_lines(page: str) -> list[str]:
    """Split one OCR page into clean, individually inspectable lines."""
    return [
        line
        for line in page.splitlines()
        if not scan._looks_like_tesseract_tsv(line)
    ]


def _line_texts(lines: list) -> list[str]:
    """Flatten hierarchical cache lines back into receipt reading order."""
    result: list[str] = []
    for line in lines:
        if isinstance(line, str):
            result.append(line)
            continue
        if isinstance(line, dict) and isinstance(line.get("text"), str):
            result.append(line["text"])
            result.extend(_line_texts(line.get("children", [])))
    return result


def _cache_metadata(data: dict) -> dict:
    """Return current nested metadata or the legacy top-level metadata fields."""
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else data


def _cached_pages(data: dict) -> list[str] | None:
    """Read all supported cache layouts as one text string per source page."""
    version = _cache_metadata(data).get("cache_version", 1)
    if version >= 3:
        cached = data.get("pages")
        if not isinstance(cached, list):
            return None
        pages: list[str] = []
        for index, page in enumerate(cached, start=1):
            if not isinstance(page, dict) or page.get("page_number") != index:
                return None
            lines = page.get("lines")
            if not isinstance(lines, list):
                return None
            if version >= 4:
                if not all(isinstance(line, dict) and isinstance(line.get("text"), str) for line in lines):
                    return None
                text_lines = _line_texts(lines)
            else:
                if not all(isinstance(line, str) for line in lines):
                    return None
                text_lines = lines
            pages.append("\n".join(_page_text_lines("\n".join(text_lines))))
        return pages

    page_text = data.get("page_text")
    if not isinstance(page_text, list) or not all(isinstance(text, str) for text in page_text):
        return None
    if version == 2:
        return ["\n".join(_page_text_lines("\n".join(page_text)))]
    return ["\n".join(_page_text_lines(page)) for page in page_text]


def _structured_layout(cache_path: Path) -> list[dict]:
    """Return structured cache pages, excluding legacy string-only formats."""
    try:
        pages = json.loads(cache_path.read_text(encoding="utf-8")).get("pages", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(pages, list):
        return []
    for page in pages:
        if not isinstance(page, dict) or not all(isinstance(line, dict) for line in page.get("lines", [])):
            return []
    return pages


def _ocr_run_metadata(cache_path: Path) -> dict:
    """Return persisted learning telemetry from a current structured cache."""
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    metadata = _cache_metadata(payload)
    if not isinstance(metadata.get("run_uuid"), str) or not isinstance(metadata.get("ocr_passes"), list):
        return {}
    return {
        "run_uuid": metadata["run_uuid"],
        "cache_version": metadata.get("cache_version"),
        "processed_at": metadata.get("processed_at"),
        "processing_seconds": metadata.get("processing_seconds"),
        "timings": metadata.get("timings") or {},
        "ocr_passes": metadata["ocr_passes"],
    }


def _cache_path(cache_dir: Path, source_reference: str | None, source_sha256: str) -> Path:
    """Mirror the source path and preserve its filename before adding .json."""
    if not source_reference:
        return cache_dir / f"{source_sha256}.json"
    reference = Path(source_reference)
    if reference.is_absolute() or ".." in reference.parts:
        reference = Path(reference.name)
    return cache_dir / reference.parent / f"{reference.name}.json"


def has_ocr_cache(cache_dir: Path, source_reference: str, source_sha256: str) -> bool:
    """Recognize both current and legacy cache locations without running OCR."""
    return any(path.is_file() for path in (
        _cache_path(cache_dir, source_reference, source_sha256),
        cache_dir / f"{source_sha256}.json",
    ))


DEPARTMENT_RE = re.compile(
    r"^(?:BAKERY|DELI|DAIRY|FROZEN(?: FOODS?)?|GROCERY|MEAT|PRODUCE|SEAFOOD|PHARMACY|HOUSEHOLD)$",
    re.I,
)


def _page_indent_layout(lines: list[scan.OCRLine]) -> list[dict | None]:
    """Describe each line's horizontal offset relative to its page content."""
    positioned = [line for line in lines if line.x is not None and line.width is not None]
    if not positioned:
        return [None] * len(lines)
    page_left = min(line.x for line in positioned)
    page_right = max(line.x + line.width for line in positioned)
    page_width = max(1, page_right - page_left)
    heights = sorted(line.height for line in positioned if line.height is not None)
    tolerance = max(12, round((heights[len(heights) // 2] if heights else 0) * 0.75))
    levels: list[int] = []
    for x in sorted({line.x for line in positioned}):
        if not levels or x - levels[-1] > tolerance:
            levels.append(x)

    result: list[dict | None] = []
    for line in lines:
        if line.x is None:
            result.append(None)
            continue
        indent = line.x - page_left
        level = min(range(len(levels)), key=lambda index: abs(levels[index] - line.x))
        result.append({
            "page": {"left": page_left, "width": page_width},
            "indent": {
                "pixels": indent,
                "ratio": round(indent / page_width, 4),
                "level": level,
            },
        })
    return result


def _cache_layout_pages(layout_pages: list[list[scan.OCRLine]]) -> list[dict]:
    department: str | None = None
    preceding_item: dict | None = None
    result: list[dict] = []
    for page_number, lines in enumerate(layout_pages, start=1):
        indent_layout = _page_indent_layout(lines)
        cached_lines: list[dict] = []
        for line_number, line in enumerate(lines, start=1):
            text = line.text
            is_heading = bool(DEPARTMENT_RE.fullmatch(text.strip()))
            is_discount = bool(re.search(r"\b(?:INSTANT SAVINGS?|YOU SAVED|DISCOUNTS?|COUPONS?)\b", text, re.I))
            is_points = bool(re.search(r"\b(?:POINTS? EARNED|EARNED POINTS?|\d+\s+PTS)\b", text, re.I))
            is_price_detail = bool(re.search(
                r"^\s*(?:\d+(?:\.\d+)?\s*(?:kg|lb)\s*@|\d+\s*@\s*\d+\s*/)",
                text,
                re.I,
            ))
            is_fee = bool(re.search(r"(?:^|\s)\+?EHC\b|\b(?:BOTTLE|CONTAINER) DEPOSIT\b", text, re.I))
            is_summary = bool(re.search(r"\b(?:SUB\s*TOTAL|TOTAL|TENDER|PAYMENT)\b", text, re.I))
            is_item = bool(
                scan.parse_money(text) is not None
                and not any((is_discount, is_points, is_price_detail, is_fee, is_summary))
            )
            if is_heading:
                department = text.strip().upper()
                preceding_item = None
            elif is_summary:
                department = None
                preceding_item = None
            line_type = (
                "department_heading" if is_heading
                else "discount" if is_discount
                else "points" if is_points
                else "price_detail" if is_price_detail
                else "fee" if is_fee
                else "summary" if is_summary
                else "item" if is_item
                else "text"
            )
            applies_to_item = is_discount or is_points or is_price_detail or is_fee
            bounds = (
                {"x": line.x, "y": line.y, "width": line.width, "height": line.height}
                if line.x is not None
                else None
            )
            line_layout = indent_layout[line_number - 1]
            if line_layout is not None:
                line_layout = {"bounds": bounds, **line_layout}
            cached_line = {
                "line_number": line_number,
                "text": text,
                "confidence": round(line.confidence, 3),
                "layout": line_layout,
                "line_type": line_type,
                "department": department if not is_heading else None,
                "applies_to": preceding_item if applies_to_item else None,
            }
            cached_lines.append(cached_line)
            if is_item:
                preceding_item = {"page_number": page_number, "line_number": line_number}
        result.append({"page_number": page_number, "lines": _nest_cache_lines(cached_lines)})
    return result


def _nest_cache_lines(lines: list[dict]) -> list[dict]:
    """Nest departments and item-supporting lines while retaining source order."""
    roots: list[dict] = []
    department: dict | None = None
    item: dict | None = None
    supporting_types = {"discount", "points", "price_detail", "fee"}
    for line in lines:
        line.pop("applies_to", None)
        line_type = line.get("line_type")
        if line_type == "department_heading":
            line["children"] = []
            roots.append(line)
            department = line
            item = None
        elif line_type == "item":
            line["children"] = []
            (department["children"] if department else roots).append(line)
            item = line
        elif line_type in supporting_types and item is not None:
            item["children"].append(line)
        elif line_type == "text" and department is not None:
            department["children"].append(line)
        else:
            roots.append(line)
            if line_type not in supporting_types:
                department = None
                item = None
    return roots


def _format_cache_json(payload: dict) -> str:
    """Pretty-print cache JSON while keeping small geometry objects together."""
    formatted = json.dumps(payload, ensure_ascii=False, indent=2)
    formatted = re.sub(
        r'"bounds": \{\n\s+"x": ([^,\n]+),\n\s+"y": ([^,\n]+),'
        r'\n\s+"width": ([^,\n]+),\n\s+"height": ([^\n]+)\n\s+\}',
        r'"bounds": {"x": \1, "y": \2, "width": \3, "height": \4}',
        formatted,
    )
    formatted = re.sub(
        r'"page": \{\n\s+"left": ([^,\n]+),\n\s+"width": ([^\n]+)\n\s+\}',
        r'"page": {"left": \1, "width": \2}',
        formatted,
    )
    formatted = re.sub(
        r'"indent": \{\n\s+"pixels": ([^,\n]+),\n\s+"ratio": ([^,\n]+),'
        r'\n\s+"level": ([^\n]+)\n\s+\}',
        r'"indent": {"pixels": \1, "ratio": \2, "level": \3}',
        formatted,
    )
    return formatted + "\n"


def _order_ocr_passes(passes: list[dict]) -> None:
    """Put each page's selected base first, then rank the alternatives."""
    passes.sort(key=lambda item: (
        item.get("page_number") or 0,
        not bool(item.get("selected_base")),
        -(item.get("consensus_coverage_ratio") or 0),
        -(item.get("structural_score") or 0),
        str(item.get("engine") or ""),
        int(item.get("dpi") or 0),
        str(item.get("psm") or ""),
        str(item.get("variant") or ""),
    ))


def _assign_ocr_pass_keys(passes: list[dict]) -> str:
    """Assign the composite (run_uuid, pass_id) identity for one OCR run."""
    run_uuid = str(uuid.uuid4())
    for pass_id, item in enumerate(passes, start=1):
        item["run_uuid"] = run_uuid
        item["pass_id"] = pass_id
    return run_uuid


def _read_or_create_pages(
    path: Path,
    source_sha256: str,
    cache_dir: Path,
    refresh: bool,
    source_reference: str | None = None,
) -> tuple[list[str], bool]:
    cache_path = _cache_path(cache_dir, source_reference, source_sha256)
    legacy_cache_path = cache_dir / f"{source_sha256}.json"
    if not refresh:
        for candidate in dict.fromkeys((cache_path, legacy_cache_path)):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                pages = _cached_pages(data)
                if _cache_metadata(data).get("source_sha256") == source_sha256 and pages is not None:
                    return pages, True
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue

    started = time.perf_counter()
    ocr_passes: list[dict] = []
    pages = scan.extract_page_text(path, ocr_passes)
    _order_ocr_passes(ocr_passes)
    text_extracted = time.perf_counter()
    layout_pages = scan.extract_page_layout(path, pages)
    layout_extracted = time.perf_counter()
    geometry_seconds = round(layout_extracted - text_extracted, 3)
    geometry_line_count = sum(len(page) for page in layout_pages)
    ocr_passes.append({
        "schema_version": 1,
        "page_number": None,
        "engine": "tesseract",
        "engine_type": "layout_ocr",
        "dpi": 300,
        "psm": "6",
        "variant": "geometry",
        "seconds": geometry_seconds,
        "status": "success" if geometry_line_count else "no_text",
        "error_type": None,
        "line_count": geometry_line_count,
        "character_count": None,
        "structural_score": None,
        "summary_score": None,
        "valid_timestamp": None,
        "selected_base": False,
        "consensus_line_coverage": None,
        "consensus_coverage_ratio": None,
        "quality": {
            "line_count": geometry_line_count,
            "character_count": None,
            "structural_score": None,
            "summary_score": None,
            "valid_timestamp": None,
            "consensus_line_coverage": None,
            "consensus_coverage_ratio": None,
        },
        "engine_options": {"psm": "6"},
        "usage": {},
        "provenance": {},
    })
    run_uuid = _assign_ocr_pass_keys(ocr_passes)
    cache_pages = _cache_layout_pages(layout_pages)
    plain_text = scan.merge_page_text([
        "\n".join(_line_texts(page["lines"]))
        for page in cache_pages
    ])
    structured = time.perf_counter()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "schema_version": 1,
            "cache_version": 13,
            "run_uuid": run_uuid,
            "source_reference": source_reference or path.name,
            "source_sha256": source_sha256,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "processing_seconds": round(structured - started, 3),
            "timings": {
                "text_extraction_seconds": round(text_extracted - started, 3),
                "layout_extraction_seconds": round(layout_extracted - text_extracted, 3),
                "structuring_seconds": round(structured - layout_extracted, 3),
            },
            "ocr_passes": ocr_passes,
        },
        "plain_text": plain_text,
        "pages": cache_pages,
    }
    tmp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
    tmp_path.write_text(_format_cache_json(payload), encoding="utf-8")
    tmp_path.replace(cache_path)
    return ["\n".join(_line_texts(page["lines"])) for page in cache_pages], False


def _parse_scan_cached(path: Path, root: Path, cache_dir: Path, refresh: bool) -> tuple[scan.ScannedReceipt, bool]:
    source_sha256 = scan.sha256_file(path)
    try:
        source_reference = path.relative_to(root).as_posix() if root.is_dir() else path.name
    except ValueError:
        source_reference = path.name
    pages, cache_hit = _read_or_create_pages(
        path,
        source_sha256,
        cache_dir,
        refresh,
        source_reference,
    )
    text = scan.merge_page_text(pages)
    subtotal, tax, total = scan.extract_totals(text)
    total = reconcile_total_from_text(text, total)
    filename_date, filename_merchant, filename_total = scan.receipt_info_from_filename(path)
    if total is None:
        total = filename_total
    payment = extract_payment_provenance(text)
    transaction_datetime = extract_transaction_datetime(text)
    transaction_date = date_part(transaction_datetime) or scan.extract_date(text) or filename_date
    reference = source_reference

    receipt = scan.ScannedReceipt(
        path=str(path),
        source_reference=reference,
        source_sha256=source_sha256,
        merchant=scan.prefer_filename_merchant(scan.extract_merchant(text), filename_merchant),
        transaction_date=transaction_date,
        receipt_id=scan.extract_receipt_id(text),
        subtotal=subtotal,
        tax=tax,
        total=total,
        payment_method=payment.payment_method,
        card_last4=payment.card_last4,
        items=scan.normalize_item_signs(scan.extract_items(text), total),
        page_text=list(pages),
        ocr_layout=_structured_layout(_cache_path(cache_dir, source_reference, source_sha256)),
        text=text,
    )
    receipt.ocr_run = _ocr_run_metadata(_cache_path(cache_dir, source_reference, source_sha256))
    receipt.transaction_datetime = transaction_datetime
    receipt.worker_pid = os.getpid()
    receipt.worker_host = socket.gethostname()
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
    progress: Callable[[str], None] | None = None,
):
    """Parse scans while preserving the original helper API.

    Existing callers that omit cache_dir receive only the ordered receipt list,
    matching the pre-cache behavior. The CLI supplies cache_dir and asks for
    cache statistics explicitly. Cache progress is reported from the parent
    process; parallel completions are reported as they finish, while returned
    receipts retain input order.
    """
    workers = max(1, workers)

    if cache_dir is None:
        receipts = [scan.parse_scan(path, root) for path in paths]
        return (receipts, 0) if return_cache_hits else receipts

    def report(path: Path, action: str, completed: int) -> None:
        if progress is not None:
            try:
                reference = path.relative_to(root).as_posix() if root.is_dir() else path.name
            except ValueError:
                reference = path.name
            progress(f"[{completed}/{len(paths)}] {action} {reference}")

    if workers == 1:
        results = []
        for path in paths:
            report(path, "Starting", len(results))
            try:
                result = _parse_scan_cached(path, root, cache_dir, refresh)
            except Exception:
                report(path, "Failed", len(results))
                raise
            results.append(result)
            report(path, "Completed", len(results))
    else:
        work = [(str(path), str(root), str(cache_dir), refresh) for path in paths]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            if progress is None:
                results = list(executor.map(_parse_scan_worker, work))
            else:
                futures = {}
                for index, item in enumerate(work):
                    futures[executor.submit(_parse_scan_worker, item)] = index
                    report(paths[index], "Queued", 0)
                results = [None] * len(paths)
                completed = 0
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except Exception:
                        report(paths[index], "Failed", completed)
                        for pending in futures:
                            pending.cancel()
                        raise
                    completed += 1
                    report(paths[index], "Completed", completed)

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


def persist_evidence_first(
    conn,
    receipts: list[scan.ScannedReceipt],
    schema: str,
    *,
    replace_existing: bool = False,
) -> dict[str, int]:
    """Persist evidence, reconciling before creating a new canonical expense."""
    stats = {"matched": 0, "ambiguous": 0, "new": 0}

    for receipt in receipts:
        receipt.merchant = resolve_merchant_alias(conn, receipt.merchant, schema)
        evidence_id = upsert_evidence(conn, receipt, schema, evidence_type="scanned")
        if replace_existing:
            with conn.cursor() as cur:
                cur.execute(f"SELECT expense_pk FROM {schema}.receipt_evidence WHERE id = %s", (evidence_id,))
                existing_expense_pk = cur.fetchone()[0]
            if existing_expense_pk is not None:
                expense_pk = scan.upsert_receipt(conn, receipt, schema)
                _persist_transaction_datetime(conn, receipt, schema)
                attach_evidence(conn, evidence_id, expense_pk, schema)
                stats["matched"] += 1
                continue
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
    """Legacy command alias; receipt work is always submitted through RabbitMQ."""
    import sys
    from ..cli import main as ledger_main

    return ledger_main(["receipts", "process", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
