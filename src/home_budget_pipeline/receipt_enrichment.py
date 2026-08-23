"""Run product enrichment for every line item belonging to one receipt."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .product_enrichment import run


def receipt_patterns(receipt: str) -> tuple[str, ...]:
    """Return source-reference patterns for a non-numeric receipt selector.

    Numeric selectors are authoritative database receipt/expense ids and are
    intentionally not translated to scanner filenames. Filename/path selectors
    are matched by basename.
    """
    value = receipt.strip()
    if not value:
        raise ValueError("receipt selector is required")
    if value.isdigit():
        return ()
    name = Path(value).name
    return (f"%{name}%",)


def _merchant_clause(merchant: str) -> tuple[str, tuple[object, ...]]:
    merchant_filter = merchant.strip()
    if not merchant_filter:
        return "", ()
    return " AND e.store_name ILIKE %s", (f"%{merchant_filter}%",)


def resolve_item_ids(dsn: str, receipt: str, merchant: str = "") -> tuple[int, ...]:
    import psycopg

    value = receipt.strip()
    if not value:
        raise ValueError("receipt selector is required")

    with psycopg.connect(dsn) as conn:
        # Numeric selectors are the database receipt/expense id. They are
        # already unique, so merchant is deliberately ignored.
        if value.isdigit():
            rows = conn.execute(
                """
                SELECT i.id
                  FROM budget.expense_items i
                  JOIN budget.expenses e ON e.id=i.expense_pk
                 WHERE e.id=%s
                 ORDER BY i.id
                """,
                (int(value),),
            ).fetchall()
            return tuple(int(row[0]) for row in rows)

        patterns = receipt_patterns(value)
        merchant_sql, merchant_params = _merchant_clause(merchant)
        clauses: list[str] = []
        params: list[object] = []
        for pattern in patterns:
            clauses.extend(
                [
                    "e.receipt_filename ILIKE %s",
                    "e.source_reference ILIKE %s",
                    "EXISTS (SELECT 1 FROM budget.receipt_evidence re "
                    "WHERE re.expense_pk=e.id AND re.source_reference ILIKE %s)",
                ]
            )
            params.extend((pattern, pattern, pattern))
        where = " OR ".join(f"({clause})" for clause in clauses)
        if merchant_sql:
            where = f"({where}){merchant_sql}"
            params.extend(merchant_params)
        rows = conn.execute(
            f"""
            SELECT i.id
              FROM budget.expense_items i
              JOIN budget.expenses e ON e.id=i.expense_pk
             WHERE {where}
             ORDER BY i.id
            """,
            tuple(params),
        ).fetchall()
    return tuple(int(row[0]) for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--merchant", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--write-db", action="store_true")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not dsn or not api_key:
        raise RuntimeError("DATABASE_URL and BRAVE_SEARCH_API_KEY are required")

    item_ids = resolve_item_ids(dsn, args.receipt, args.merchant)
    if not item_ids:
        merchant_text = (
            f" for merchant {args.merchant!r}"
            if args.merchant and not args.receipt.strip().isdigit()
            else ""
        )
        raise RuntimeError(f"No line items found for receipt {args.receipt!r}{merchant_text}")

    result = run(
        dsn=dsn,
        api_key=api_key,
        limit=max(args.limit, len(item_ids)),
        threshold=args.threshold,
        write_db=args.write_db,
        item_ids=item_ids,
    )
    payload = {
        "receipt": args.receipt,
        "merchant": None if args.receipt.strip().isdigit() else (args.merchant or None),
        "item_ids": list(item_ids),
        **result,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
