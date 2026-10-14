"""Import new Costco electronic PDFs without rewriting previously imported items."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from . import costco


def import_receipts(root: Path):
    result = {'imported': 0, 'skipped': 0, 'review': [], 'unreadable': []}
    # A session lock also protects manual imports from racing the scheduled job.
    with costco._connect_postgres('') as conn:
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(hashtext('costco-electronic-import'))")
        try:
            known = {row[0] for row in conn.execute('SELECT source_sha256 FROM budget.receipt_evidence')}
            orders = {row[0] for row in conn.execute("SELECT order_id FROM budget.expenses WHERE source='costco'")}
            costco.AI_ENGINE.enabled = False
            for path in sorted(root.glob('*.pdf')):
                if path.name.startswith('.'):
                    continue
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                if sha in known:
                    result['skipped'] += 1
                    continue
                try:
                    text = costco.pdf_text(path)
                except Exception as exc:
                    result['unreadable'].append({'file': path.name, 'error': type(exc).__name__})
                    continue
                order = costco.parse_costco_order_id(text) or path.stem
                if order in orders:
                    result['skipped'] += 1
                    continue
                items = costco.parse_receipt_items(path.stem, text)
                subtotal, tax, total = costco.parse_receipt_amounts(text)
                if total is None:
                    result['unreadable'].append({'file': path.name, 'error': 'missing_total'})
                    continue
                item_sum = round(sum(item['amount_cad'] for item in items), 2)
                complete = bool(items) and subtotal is not None and abs(item_sum-subtotal) <= .01
                row = {'receipt_id': path.stem, 'order_id': order, 'subtotal_cad': subtotal,
                       'tax_cad': tax, 'total_cad': total, 'parsed_item_count': len(items),
                       'parsed_item_sum_cad': item_sum, 'source_path': str(path),
                       'source_reference': f'Costco/{path.name}', 'raw_text': text,
                       'extraction_status': 'complete' if complete else 'partial',
                       'extraction_confidence': 1 if complete else .5}
                costco.write_costco_to_postgres([row], items, '', 'budget')
                known.add(sha)
                orders.add(order)
                result['imported'] += 1
                if not complete:
                    result['review'].append({'file': path.name, 'subtotal': subtotal, 'item_sum': item_sum})
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('costco-electronic-import'))")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.getenv(
        'ELECTRONIC_RECEIPT_SOURCE_ROOT', '/data/receipts/raw/electronic')) / 'Costco')
    args = parser.parse_args()
    if not args.root.is_dir():
        raise RuntimeError(f'Electronic receipt directory does not exist: {args.root}')
    print(json.dumps(import_receipts(args.root)), flush=True)


if __name__ == '__main__':
    main()
