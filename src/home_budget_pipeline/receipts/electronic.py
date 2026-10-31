"""Import new Costco electronic PDFs without rewriting previously imported items."""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import costco


def matching_scanned_expense(conn, order, total):
    """Match the printed warehouse/register/transaction identity and minute."""
    if not re.fullmatch(r'22\d{21}', order):
        return None
    try:
        when = datetime.strptime(order[-10:], '%y%m%d%H%M')
    except ValueError:
        return None
    warehouse, register, transaction = (str(int(order[a:b])) for a, b in ((2, 6), (6, 9), (9, 13)))
    identity = re.compile(r'(?<!\d)' + r'\s+'.join(map(re.escape, (warehouse, register, transaction))) + r'(?!\d)')
    candidates = conn.execute('''SELECT DISTINCT e.id,re.raw_text
        FROM budget.expenses e JOIN budget.receipt_evidence re ON re.expense_pk=e.id
        WHERE e.source='scanned' AND e.store_name ILIKE '%%costco%%'
          AND e.expense_total=%s
          AND e.transaction_datetime >= %s::timestamp
          AND e.transaction_datetime < %s::timestamp + interval '1 minute' ''', (total, when, when)).fetchall()
    matches = {pk for pk, text in candidates if identity.search(text or '')}
    return next(iter(matches)) if len(matches) == 1 else None


def import_receipts(root: Path, *, require_costco=False, source_prefix='Costco'):
    result = {'imported': 0, 'linked_existing': 0, 'skipped': 0, 'review': [], 'unreadable': [], 'unsupported': []}
    # A session lock also protects manual imports from racing the scheduled job.
    with costco._connect_postgres('') as conn:
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(hashtext('costco-electronic-import'))")
        try:
            known = {row[0] for row in conn.execute('SELECT source_sha256 FROM budget.receipt_evidence')}
            orders = {row[0] for row in conn.execute("SELECT order_id FROM budget.expenses WHERE source='costco'")}
            costco.AI_ENGINE.enabled = False
            for path in sorted(root.rglob('*')):
                relative = path.relative_to(root)
                if not path.is_file() or path.suffix.lower() != '.pdf' or any(p.startswith('.') for p in relative.parts):
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
                # The shared electronic inbox also contains other merchants.
                # Never turn their documents into Costco expenses.
                if require_costco and not re.search(r'\bcostco\b', text, re.IGNORECASE):
                    result['unsupported'].append(relative.as_posix())
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
                       'source_reference': f'{source_prefix}/{relative.as_posix()}', 'raw_text': text,
                       'extraction_status': 'complete' if complete else 'partial',
                       'extraction_confidence': 1 if complete else .5}
                matched = matching_scanned_expense(conn, order, total)
                if matched:
                    row['matched_expense_pk'] = matched
                    row['match_reason'] = 'warehouse_register_transaction_minute_total'
                    result['linked_existing'] += 1
                else:
                    # Totals and basket overlap alone do not establish identity.
                    # Surface possible scanned duplicates for review.
                    candidates = conn.execute('''SELECT e.id,re.raw_text
                        FROM budget.expenses e JOIN budget.receipt_evidence re ON re.expense_pk=e.id
                        WHERE e.source='scanned' AND e.store_name ILIKE '%%costco%%'
                          AND e.expense_total=%s''', (total,)).fetchall()
                    skus = {item['sku'] for item in items if item.get('sku')}
                    possible = sorted({pk for pk, raw in candidates if
                        len(skus & set(re.findall(r'\b\d{3,8}\b', raw or ''))) >= 3
                        and costco.parse_receipt_amounts(raw or '')[0] == subtotal})
                    if possible:
                        row['possible_duplicate_expense_pks'] = possible
                        row['extraction_status'] = 'review'
                        row['extraction_confidence'] = .5
                        result['review'].append({'file': path.name,
                            'reason': 'possible_scanned_duplicate', 'expense_pks': possible})
                costco.write_costco_to_postgres([row], items, '', 'budget')
                known.add(sha)
                orders.add(order)
                result['imported'] += 1
                if not complete:
                    result['review'].append({'file': path.name, 'subtotal': subtotal, 'item_sum': item_sum})
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('costco-electronic-import'))")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.getenv(
        'ELECTRONIC_RECEIPT_SOURCE_ROOT', '/data/receipts/raw/electronic')))
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        raise RuntimeError(f'Electronic receipt directory does not exist: {args.root}')
    print(json.dumps(import_receipts(args.root, require_costco=True,
                                   source_prefix='Costco' if args.root.name == 'Costco' else 'electronic')), flush=True)


if __name__ == '__main__':
    main()
