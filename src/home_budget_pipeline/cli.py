"""Unified command-line interface for BrownRook Ledger operations."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Sequence

from . import db_setup, receipt_enrichment, telemetry
from .receipts import backlog_ingest
from .receipts.evidence import record_ocr_feedback
from .receipts import ingest as scan


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
    parser.add_argument("--env-file", help="Load explicit settings before command defaults, e.g. .env.dev; place before the command.")
    commands = parser.add_subparsers(dest="command", required=True)

    development = commands.add_parser("development", help="Configure and validate the isolated Arsene development backends.")
    development.add_argument("action", choices=("credentials", "configure", "check", "bootstrap", "replay"))
    development.add_argument("--apply", action="store_true", help="Apply connection changes for configure; default is a preview.")
    development.add_argument("--test", action="store_true", help="Bootstrap the isolated test database.")
    development.add_argument("--secrets-file", help="Private backend credential file; default .dev-backend/secrets.yml.")
    development.add_argument("--evidence", help="Private saved graph evidence for replay; no model/search calls.")
    development.add_argument("--run", default="", help="Historical collaboration run to select during replay.")
    development.add_argument("--output", help="Private replay report path.")
    development.set_defaults(handler=_development_backend)

    web = commands.add_parser("web", help="Control the native development web application.")
    web_commands = web.add_subparsers(dest="web_command", required=True)
    web_serve = web_commands.add_parser("serve", help="Run the native development web server in this terminal with code reload.")
    web_serve.set_defaults(handler=_serve_web)
    web_services = web_commands.add_parser("services", help="Manage the Mac web LaunchAgent.")
    web_services.add_argument("action", choices=("start", "stop", "restart", "status", "logs"))
    web_services.set_defaults(handler=_web_services)

    receipts = commands.add_parser("receipts", help="Process receipt evidence.")
    receipt_commands = receipts.add_subparsers(dest="receipt_command", required=True)
    services = receipt_commands.add_parser("services", help="Control native Mac receipt workers and collector.")
    services.add_argument("action", choices=("start", "stop", "restart", "status", "logs"))
    services.add_argument("--workers", type=int)
    services.add_argument("--ocr-threads", type=int)
    services.add_argument("--only", choices=("collector",), help="Control only the collector, leaving OCR workers running.")
    services.set_defaults(handler=_receipt_services)
    process = receipt_commands.add_parser("process", help="Queue the receipt backlog through RabbitMQ.")
    process.add_argument("receipt_root", nargs="?", default=_receipt_root_default())
    process.add_argument("--workers", type=int, help="Deprecated; concurrency is controlled by RabbitMQ consumers.")
    process.add_argument("--rabbitmq-url", default=os.getenv("RABBITMQ_URL", ""))
    process.add_argument("--request-id", help="Reuse a batch UUID when retrying an uncertain refresh publication.")
    process.add_argument("--ocr-cache", default=_ocr_cache_default())
    process.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    process.add_argument("--ingest-schema", default="ingest")
    process.add_argument("--budget-schema", default="budget")
    process.add_argument("--refresh-ocr-cache", action="store_true")
    process.add_argument("--verbose", action="store_true", help="Print per-receipt progress to stderr.")
    process.set_defaults(handler=_process_receipts, queue_mode="publish")

    from .receipts.queue_ingest import add_arguments
    for mode in ("publish", "consume"):
        queued = receipt_commands.add_parser(mode, help=f"{mode.title()} receipt work through RabbitMQ.")
        add_arguments(queued, mode)

    collect = receipt_commands.add_parser(
        "collect", help="Validate and durably persist OCR result events from RabbitMQ.",
    )
    collect.add_argument("--db-dsn", default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""))
    collect.add_argument("--rabbitmq-url", default=os.getenv("RABBITMQ_URL", ""))
    collect.add_argument("--ingest-schema", default="ingest")
    collect.add_argument("--budget-schema", default="budget")
    from .receipts.ocr_collector import run_collector
    collect.set_defaults(handler=run_collector)

    recover = receipt_commands.add_parser('recover-results', help='Recover the known missing-evidence cache-only result failures on development.')
    recover.add_argument('--apply', action='store_true', help='Persist eligible results and acknowledge only after commit; otherwise preview.')
    recover.add_argument('--limit', type=int, default=1000)
    recover.add_argument('--evidence-dir', required=True, help='Private directory retaining original rejected messages and headers.')
    from .receipts.recover_results import run_recovery
    recover.set_defaults(handler=run_recovery)

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

    ocr_cache = commands.add_parser("ocr-cache", help="Manage OCR cache files through RabbitMQ workers.")
    cache_commands = ocr_cache.add_subparsers(dest="cache_command", required=True)
    clean = cache_commands.add_parser("clean", help="Empty the local cache, retaining prior outputs privately; stops receipt services.")
    clean.add_argument("--local", action="store_true", required=True, help="Operate only on the repository's native Mac cache.")
    clean.set_defaults(handler=_clean_local_cache)
    rebuild = cache_commands.add_parser("rebuild", help="Queue cache rebuilds without replacing saved extraction results.")
    rebuild.add_argument("receipt_root", nargs="?", default=_receipt_root_default())
    rebuild.add_argument(
        "--ocr-cache",
        default=_ocr_cache_default(),
        help="Cache directory inspected by --missing-only; each consumer uses its own configured path.",
    )
    rebuild.add_argument("--rabbitmq-url", default=os.getenv("RABBITMQ_URL", ""))
    rebuild.add_argument("--request-id", help="Reuse a batch UUID when retrying an uncertain publication.")
    rebuild.add_argument(
        "--source-reference",
        help="Queue exactly one receipt path relative to the receipt root.",
    )
    rebuild.add_argument(
        "--missing-only",
        action="store_true",
        help="Queue only receipts whose current or legacy OCR cache is absent.",
    )
    rebuild.add_argument("--workers", type=int, help="Deprecated; concurrency is controlled by RabbitMQ consumers.")
    rebuild.add_argument("--verbose", action="store_true", help="Print confirmed publication progress to stderr; OCR progress is in worker logs.")
    rebuild.set_defaults(handler=_rebuild_ocr_cache)

    database = commands.add_parser("database", help="Manage the PostgreSQL database.")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    rebuild_db = database_commands.add_parser("rebuild", help="Delete and recreate application schemas on an explicit development target.")
    rebuild_db.add_argument("--target", choices=("arsene-dev", "arsene-test"), required=True)
    rebuild_db.add_argument("--confirm", default="", help="Arsene requires the exact development database name.")
    rebuild_db.set_defaults(handler=_rebuild_database)
    setup = database_commands.add_parser("setup", help="Initialize or upgrade the database schema.")
    setup.add_argument(
        "--database-url",
        "--db-dsn",
        dest="db_dsn",
        default=os.getenv("DATABASE_URL") or os.getenv("HOME_BUDGET_PG_DSN", ""),
    )
    setup.set_defaults(handler=_setup_database)
    return parser


def _receipt_services(args):
    from .local_operations import run_receipt_services
    return run_receipt_services(args)


def _clean_local_cache(args):
    from .local_operations import clean_local_cache
    return clean_local_cache(args)


def _rebuild_database(args):
    from .local_operations import rebuild_database
    return rebuild_database(args)


def _development_backend(args):
    from .local_operations import development_backend
    return development_backend(args)


def _web_services(args):
    from .local_operations import run_web_services
    return run_web_services(args)


def _serve_web(args):
    from .local_operations import serve_web
    return serve_web(args)


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


def _queue_concurrency_notice(args: argparse.Namespace) -> None:
    if args.workers is not None:
        print("--workers no longer starts local processes; concurrency is controlled by RabbitMQ consumers.", file=sys.stderr)


def _process_receipts(args: argparse.Namespace) -> int:
    from .receipts import queue_ingest

    _queue_concurrency_notice(args)
    if args.refresh_ocr_cache:
        return queue_ingest.publish_batch(args)
    if args.request_id:
        raise ValueError("--request-id requires --refresh-ocr-cache")
    return queue_ingest.run(args)


def _rebuild_ocr_cache(args: argparse.Namespace) -> int:
    from .receipts.queue_ingest import publish_batch

    _queue_concurrency_notice(args)
    return publish_batch(args, cache_only=True)


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
    from contextlib import ExitStack
    from .cli_environment import environment_file
    # Load settings before building parsers: argument defaults read the environment.
    selector = argparse.ArgumentParser(add_help=False)
    selector.add_argument("--env-file")
    selected, _ = selector.parse_known_args(argv)
    with ExitStack() as stack:
        try:
            stack.enter_context(environment_file(selected.env_file))
        except (ValueError, OSError) as exc:
            print(str(exc) if isinstance(exc, ValueError) else
                  f"Cannot load Ledger settings ({type(exc).__name__})", file=sys.stderr)
            return 2
        return _main(argv)


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser(_program_name()).parse_args(argv)
    receipt_command = getattr(args, "receipt_command", "")
    if receipt_command in {"consume", "collect", "services"}:
        return args.handler(args)  # LaunchAgents already capture their output.
    service = "enrichment" if receipt_command == "enrich" else (
        "publisher" if receipt_command in {"process", "publish", "reprocess"} or args.command == "ocr-cache" else "cli"
    )
    operation = f"{args.command} {receipt_command}".strip()
    with telemetry.local_command_logging(service):
        telemetry.LOG.info("command_started operation=%s", operation)
        try:
            result = args.handler(args)
        except Exception:
            telemetry.LOG.exception("command_failed operation=%s", operation)
            raise
        telemetry.LOG.info("command_completed operation=%s exit_code=%s", operation, result)
        return result
