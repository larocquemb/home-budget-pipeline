#!/usr/bin/env python3
"""Summarize a local scanned_receipts_report.json without modifying source data.

The report can contain personal financial information and should stay local. This
utility is safe to commit because it reads the report path supplied at runtime.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

PAYMENT_LINE_RE = re.compile(
    r"(?:visa|master\s*card|mastercard|amex|american\s+express|interac|debit|cash|"
    r"card\s+number|card\s+type|acct|account|tender|trans\s+type|entry\s+method|"
    r"reference|author\.?\s*#|auth\s*#|a\s*i\s*d|aid\s*:|approved|[*xXKk#]{2,}\s*\d{2,4})",
    re.I,
)

DATE_LINE_RE = re.compile(
    r"(?:\bdate\b|\btime\b|\b20\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-](?:\d{2}|20\d{2})\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{2,4}\b|"
    r"\b20\d{6}\b)",
    re.I,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze a scanned receipt extraction report.")
    p.add_argument("report", help="Path to scanned_receipts_report.json")
    p.add_argument("--reason", default="", help="Show OCR tails only for receipts containing this review reason.")
    p.add_argument("--tail-lines", type=int, default=20, help="OCR lines to show from the end of each matching receipt.")
    p.add_argument("--limit", type=int, default=8, help="Maximum matching receipt OCR samples to print.")
    p.add_argument(
        "--payment-lines",
        action="store_true",
        help="Show OCR lines likely to contain card/payment provenance for receipts with payment data.",
    )
    p.add_argument(
        "--missing-card-last4",
        action="store_true",
        help="With --payment-lines, limit output to receipts where a payment method was detected but card_last4 was not.",
    )
    p.add_argument(
        "--date-lines",
        action="store_true",
        help="Show OCR lines likely to contain receipt dates/times.",
    )
    p.add_argument(
        "--missing-date",
        action="store_true",
        help="With --date-lines, limit output to receipts where transaction_date was not extracted.",
    )
    return p.parse_args()


def reason_counts(receipts: Iterable[dict]) -> Counter:
    counts: Counter = Counter()
    for receipt in receipts:
        for reason in receipt.get("review_reasons", []) or []:
            counts[str(reason)] += 1
    return counts


def print_summary(data: dict) -> None:
    receipts = data.get("receipts", [])
    print(f"discovered: {data.get('discovered', len(receipts))}")
    print(f"complete:   {data.get('complete', 0)}")
    print(f"review:     {data.get('review', 0)}")
    print(f"unreadable: {data.get('unreadable', 0)}")
    print("\nReview reasons:")
    counts = reason_counts(receipts)
    for reason, count in counts.most_common():
        print(f"  {reason:20s} {count}")


def print_samples(data: dict, reason: str, tail_lines: int, limit: int) -> None:
    if not reason:
        return
    matches = [
        r for r in data.get("receipts", [])
        if reason in (r.get("review_reasons", []) or [])
    ]
    print(f"\nOCR tail samples for reason={reason!r}: {len(matches)} matching")
    for receipt in matches[: max(0, limit)]:
        print("\n" + "=" * 80)
        print(receipt.get("source_reference", "<unknown>"))
        print(
            f"merchant={receipt.get('merchant')!r} date={receipt.get('transaction_date') or '-'} "
            f"total={receipt.get('total') if receipt.get('total') is not None else '-'} "
            f"items={len(receipt.get('items', []))}"
        )
        print("-" * 80)
        lines = str(receipt.get("text", "")).splitlines()
        print("\n".join(lines[-max(1, tail_lines):]))


def print_payment_lines(data: dict, limit: int, missing_card_last4: bool) -> None:
    if limit <= 0:
        return

    matches = []
    for receipt in data.get("receipts", []):
        method = receipt.get("payment_method")
        card_last4 = receipt.get("card_last4")
        if not method and not card_last4:
            continue
        if missing_card_last4 and (not method or card_last4):
            continue
        lines = [
            line.strip()
            for line in str(receipt.get("text", "")).splitlines()
            if PAYMENT_LINE_RE.search(line)
        ]
        if lines:
            matches.append((receipt, lines))

    qualifier = " with detected method but missing card_last4" if missing_card_last4 else ""
    print(f"\nPayment OCR samples{qualifier}: {len(matches)} matching receipts")
    for receipt, lines in matches[:limit]:
        print("\n" + "=" * 80)
        print(receipt.get("source_reference", "<unknown>"))
        print(
            f"payment={receipt.get('payment_method') or '-'} "
            f"card={receipt.get('card_last4') or '-'} "
            f"date={receipt.get('transaction_date') or '-'} "
            f"merchant={receipt.get('merchant')!r}"
        )
        print("-" * 80)
        for line in lines:
            print(line)


def print_date_lines(data: dict, limit: int, missing_date: bool) -> None:
    if limit <= 0:
        return

    matches = []
    for receipt in data.get("receipts", []):
        transaction_date = receipt.get("transaction_date")
        if missing_date and transaction_date:
            continue
        lines = [
            line.strip()
            for line in str(receipt.get("text", "")).splitlines()
            if DATE_LINE_RE.search(line)
        ]
        if lines:
            matches.append((receipt, lines))

    qualifier = " with missing transaction_date" if missing_date else ""
    print(f"\nDate OCR samples{qualifier}: {len(matches)} matching receipts")
    for receipt, lines in matches[:limit]:
        print("\n" + "=" * 80)
        print(receipt.get("source_reference", "<unknown>"))
        print(
            f"date={receipt.get('transaction_date') or '-'} "
            f"merchant={receipt.get('merchant')!r} "
            f"total={receipt.get('total') if receipt.get('total') is not None else '-'}"
        )
        print("-" * 80)
        for line in lines:
            print(line)


def main() -> int:
    args = parse_args()
    path = Path(args.report)
    data = json.loads(path.read_text(encoding="utf-8"))
    print_summary(data)
    print_samples(data, args.reason, args.tail_lines, args.limit)
    if args.payment_lines:
        print_payment_lines(data, args.limit, args.missing_card_last4)
    if args.date_lines:
        print_date_lines(data, args.limit, args.missing_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
