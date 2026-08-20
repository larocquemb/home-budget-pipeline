"""Command-line entry point for categorizing one canonical expense."""

from __future__ import annotations

import argparse
import os
from typing import Mapping, Sequence

from .ai_fallback import OpenAICategoryFallback
from .pipeline import CategoryMappingPipeline
from .processing import CanonicalExpenseCategorizer, PostgresExpenseCategorizationStore
from .store import PostgresCategoryMappingStore


def resolve_database_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """Return the configured PostgreSQL DSN.

    HOME_BUDGET_PG_DSN is the canonical application setting. DATABASE_URL is
    accepted as a compatibility fallback for existing deployments and tooling.
    """
    values = os.environ if env is None else env
    return values.get("HOME_BUDGET_PG_DSN") or values.get("DATABASE_URL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Categorize all line items for one canonical Ledger expense."
    )
    parser.add_argument("expense_pk", type=int, help="budget.expenses.id")
    parser.add_argument("--source", help="receipt source, e.g. costco")
    parser.add_argument("--merchant", help="normalized merchant name")
    parser.add_argument(
        "--pg-dsn",
        "--database-url",
        dest="pg_dsn",
        default=resolve_database_dsn(),
        help=(
            "PostgreSQL DSN; defaults to HOME_BUDGET_PG_DSN, then DATABASE_URL "
            "for compatibility"
        ),
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.pg_dsn:
        raise SystemExit(
            "HOME_BUDGET_PG_DSN, DATABASE_URL, or --pg-dsn is required"
        )

    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit(
            "psycopg is required; install the project with the db extra: pip install -e '.[db]'"
        ) from exc

    with psycopg.connect(args.pg_dsn) as connection:
        mapping_store = PostgresCategoryMappingStore(connection)
        pipeline = CategoryMappingPipeline(
            mapping_store=mapping_store,
            ai_fallback=OpenAICategoryFallback(),
        )
        item_store = PostgresExpenseCategorizationStore(connection)
        result = CanonicalExpenseCategorizer(pipeline, item_store).categorize_expense(
            args.expense_pk,
            source=args.source,
            merchant=args.merchant,
        )

    print(f"expense_pk={result.expense_pk}")
    print(f"items={len(result.decisions)}")
    print(f"requires_review={str(result.requires_review).lower()}")
    for split in result.splits:
        print(
            f"split category={split.category_name!r} "
            f"amount={split.category_amount:.2f} "
            f"items={split.item_count} "
            f"requires_review={str(split.requires_review).lower()}"
        )
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
