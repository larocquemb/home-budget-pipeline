#!/usr/bin/env python3
"""Summarize a local scanned_receipts_report.json without modifying source data.

The report can contain personal financial information and should stay local. This
utility is safe to commit because it reads the report path supplied at runtime.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Iterable


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze a scanned receipt extraction report.")
    p.add_argument("report", help="Path to scanned_receipts_report.json")
    p.add_argument("--reason", default="", help="Show OCR tails only for receipts containing this review reason.")
    p.add_argument("--tail-lines", type=int, default=20, help="OCR lines to show from the end of each matching receipt.")
    p.add_argument("--limit", type=int, default=8, help="Maximum matching receipt OCR samples to print.")
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


def main() -> int:
    args = parse_args()
    path = Path(args.report)
    data = json.loads(path.read_text(encoding="utf-8"))
    print_summary(data)
    print_samples(data, args.reason, args.tail_lines, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
