"""Unified command-line interface for BrownRook Ledger operations."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Sequence

from . import db_setup, receipt_enrichment
from .receipts import backlog_ingest
from .receipts.evidence import record_ocr_feedback
from .receipts import ingest as scan
from .receipts.parallel_ingest import parse_scans_parallel


def _receipt_root_default() -> str:
    return backlog_ingest._default_path(
        "receipts/raw/scanned/inbox",
        "/data/receipts/raw/scanned/inbox",
        "RECEIPT_SOURCE_ROOT",
    )


def _ocr_cache_default() -> str:
    return backlog_ingest._default_path(
        "receipts/derived/ocr-cache",
        "/data/receipts/derived/ocr-cache",
        "HOME_BUDGET_OCR_CACHE",
    )


def _display_database_dsn(dsn: str) -> str:
    """Show the database target without exposing a URL or conninfo password."""
    if not dsn:
        return "not configured"
    redacted = re.sub(r"(://[^:/@\s]+):([^@/\s]+)@", r"\1:***@", dsn)
    return re.sub(r"(?i)(\bpassword=)[^\s&]+", r"\1***", redacted)


def _program_name() -> str:
    invoked = Path(sys.argv[0]).stem
    return invoked if invoked in {"ledger", "brownrook"} else "ledger"


def build_parser(prog: str = "ledger") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="BrownRook Ledger operations.")
    commands = parser.add_subparsers(dest="command", required=True)

    receipts = commands.add_parser("receipts", help="Process receipt evidence.")
    receipt_commands = receipts.add_subparsers(dest="receipt_command", required=True)
    process = receipt_commands.add_parser("process", help="Process the receipt backlog into PostgreSQL.")
    process.add_argument("receipt_root", nargs="?", default=_receipt_root_default())
    process.add_argument("--workers", type=int, default=2)
    process.add_argument("--ocr-cache", default=_ocr_cache_default())
    process.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    process.add_argument("--ingest-schema", default="ingest")
    process.add_argument("--budget-schema", default="budget")
    process.add_argument("--refresh-ocr-cache", action="store_true")
    process.add_argument("--verbose", action="store_true", help="Print per-receipt progress to stderr.")
    process.set_defaults(handler=_process_receipts)

    from .receipts.queue_ingest import add_arguments
    for mode in ("publish", "consume"):
        queued = receipt_commands.add_parser(mode, help=f"{mode.title()} receipt work through RabbitMQ.")
        add_arguments(queued, mode)

    retry = receipt_commands.add_parser(
        "retry", help="Reset a failed or interrupted receipt for the next publish or process command.",
    )
    retry.add_argument("source_reference", help="Exact receipt path relative to the receipt source root.")
    retry.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    retry.add_argument("--ingest-schema", default="ingest")
    retry.set_defaults(handler=_retry_receipt)

    reprocess = receipt_commands.add_parser(
        "reprocess", help="Queue an OCR refresh and extraction replacement for exactly one receipt.",
    )
    reprocess.add_argument("source_reference", help="Exact receipt path relative to the receipt source root.")
    reprocess.add_argument("--receipt-root", default=_receipt_root_default())
    reprocess.add_argument("--rabbitmq-url", default=os.getenv("RABBITMQ_URL", ""))
    reprocess.add_argument("--request-id", help="Reuse a UUID when retrying an uncertain publication.")
    reprocess.add_argument("--verbose", action="store_true", help="Print publication progress to stderr.")
    from .receipts.queue_ingest import publish_reprocess
    reprocess.set_defaults(handler=publish_reprocess)

    enrich = receipt_commands.add_parser(
        "enrich", help="Enrich all line items belonging to one receipt.",
    )
    receipt_enrichment.add_arguments(enrich)
    enrich.set_defaults(handler=receipt_enrichment.run_command)

    feedback = receipt_commands.add_parser("feedback", help="Record verified OCR quality feedback.")
    feedback.add_argument("evidence_id", type=int)
    feedback.add_argument("--outcome", required=True, choices=("confirmed", "corrected", "rejected"))
    feedback.add_argument("--corrected-fields", default="{}", help="JSON object of corrected receipt fields.")
    feedback.add_argument("--notes")
    feedback.add_argument("--verified-by", default=os.getenv("USER"))
    feedback.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    feedback.add_argument("--budget-schema", default="budget")
    feedback.set_defaults(handler=_record_receipt_feedback)

    ocr_cache = commands.add_parser("ocr-cache", help="Manage local OCR cache files.")
    cache_commands = ocr_cache.add_subparsers(dest="cache_command", required=True)
    rebuild = cache_commands.add_parser("rebuild", help="Rebuild OCR cache files without writing to PostgreSQL.")
    rebuild.add_argument("receipt_root", nargs="?", default=_receipt_root_default())
    rebuild.add_argument("--ocr-cache", default=_ocr_cache_default())
    rebuild.add_argument("--workers", type=int, default=2)
    rebuild.set_defaults(handler=_rebuild_ocr_cache)

    database = commands.add_parser("database", help="Manage the PostgreSQL database.")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    setup = database_commands.add_parser("setup", help="Initialize or upgrade the database schema.")
    setup.add_argument(
        "--database-url",
        "--db-dsn",
        dest="db_dsn",
        default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""),
    )
    setup.set_defaults(handler=_setup_database)
    return parser


def _retry_receipt(args: argparse.Namespace) -> int:
    if not args.db_dsn:
        print(
            "Missing database DSN (--db-dsn, DATABASE_URL, or HOME_BUDGET_PG_DSN). "
            "Export .env.dev variables with: set -a; source .env.dev; set +a",
            file=sys.stderr,
        )
        return 1
    conn = scan._db_connect(args.db_dsn)
    try:
        result = backlog_ingest.reset_receipt_for_retry(
            conn, args.source_reference, ingest_schema=args.ingest_schema,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Cannot retry receipt: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    return 0


def _process_receipts(args: argparse.Namespace) -> int:
    root = Path(args.receipt_root).expanduser().resolve()
    cache_dir = Path(args.ocr_cache).expanduser().resolve()
    workers = max(1, args.workers)
    if args.verbose:
        print("Effective receipt processing settings:", file=sys.stderr)
        print(f"  Receipt root: {root}", file=sys.stderr)
        print(f"  OCR cache: {cache_dir}", file=sys.stderr)
        print(f"  Workers: {workers} local process(es)", file=sys.stderr)
        print(f"  Worker host: {socket.gethostname()}", file=sys.stderr)
        print(f"  Database: {_display_database_dsn(args.db_dsn)}", file=sys.stderr)
        print(f"  Ingest schema: {args.ingest_schema}", file=sys.stderr)
        print(f"  Budget schema: {args.budget_schema}", file=sys.stderr)
        print(f"  Refresh OCR cache: {'yes' if args.refresh_ocr_cache else 'no'}", file=sys.stderr)
    conn = scan._db_connect(args.db_dsn)
    try:
        summary = backlog_ingest.process_backlog(
            conn,
            root,
            cache_dir,
            workers=workers,
            ingest_schema=args.ingest_schema,
            budget_schema=args.budget_schema,
            refresh_ocr_cache=args.refresh_ocr_cache,
            verbose=args.verbose,
        )
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


def _rebuild_ocr_cache(args: argparse.Namespace) -> int:
    root = Path(args.receipt_root).expanduser().resolve()
    cache_dir = Path(args.ocr_cache).expanduser().resolve()
    paths = scan.discover_scans(root)
    parse_scans_parallel(paths, root, max(1, args.workers), cache_dir, refresh=True)
    print(json.dumps({
        "discovered": len(paths),
        "cache_rebuilt": len(paths),
        "ocr_cache": str(cache_dir),
    }, indent=2))
    return 0 if paths else 2


def _record_receipt_feedback(args: argparse.Namespace) -> int:
    try:
        corrected_fields = json.loads(args.corrected_fields)
    except json.JSONDecodeError as exc:
        raise ValueError("--corrected-fields must be valid JSON") from exc
    if not isinstance(corrected_fields, dict):
        raise ValueError("--corrected-fields must be a JSON object")
    conn = scan._db_connect(args.db_dsn)
    try:
        record_ocr_feedback(
            conn,
            args.evidence_id,
            args.outcome,
            corrected_fields,
            args.notes,
            args.verified_by,
            args.budget_schema,
        )
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({"evidence_id": args.evidence_id, "outcome": args.outcome}, indent=2))
    return 0


def _setup_database(args: argparse.Namespace) -> int:
    if not args.db_dsn:
        raise RuntimeError("DATABASE_URL, HOME_BUDGET_PG_DSN, or --database-url is required")
    conn = scan._db_connect(args.db_dsn)
    try:
        result = db_setup.ensure_database_schema(conn)
    finally:
        conn.close()
    status = {
        "base_schema": "created" if result["base_created"] else "already_current",
        "product_enrichment_schema": (
            "created" if result["enrichment_schema_created"] else "already_current"
        ),
        "receipt_ocr_lines_schema": (
            "created" if result["receipt_ocr_lines_created"] else "already_current"
        ),
        "receipt_ocr_learning_schema": (
            "created" if result["receipt_ocr_learning_created"] else "already_current"
        ),
        "receipt_ingest_schema": (
            "upgraded" if result["receipt_schema_changed"] else "already_current"
        ),
    }
    print(json.dumps(status, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser(_program_name()).parse_args(argv)
    return args.handler(args)
