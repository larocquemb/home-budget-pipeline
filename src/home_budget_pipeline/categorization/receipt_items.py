"""Refresh OCR items without discarding matched human decisions or item identities."""
from collections import Counter, defaultdict, deque
from decimal import Decimal

from .item_rules import apply_category

FIELDS = ('id', 'item_name', 'line_total', 'budget_category', 'category_source',
          'category_rationale', 'product_description', 'product_url')


def item_key(name, amount):
    return name, Decimal(str(amount)) if amount is not None else None


def load_existing(cur, order_id, schema):
    cur.execute(f"SELECT id FROM {schema}.expenses WHERE source='scanned' AND order_id=%s AND NOT (COALESCE(raw_payload, '{{}}'::jsonb) ? 'duplicate_merge') FOR UPDATE", (order_id,))
    row = cur.fetchone()
    if not row:
        return None, []
    expense_pk = row['id'] if isinstance(row, dict) else row[0]
    cur.execute(f"SELECT {','.join(FIELDS)} FROM {schema}.expense_items WHERE expense_pk=%s ORDER BY id FOR UPDATE", (expense_pk,))
    return expense_pk, [dict(row) if isinstance(row, dict) else dict(zip(FIELDS, row)) for row in cur.fetchall()]


def unmatched_reviewed(items, existing):
    available = Counter(item_key(i.item_name, i.line_total) for i in items)
    # Preserve reviewed rows first if duplicate item counts have changed.
    for row in sorted(existing, key=lambda r: not reviewed(r)):
        key = item_key(row['item_name'], row['line_total'])
        if available[key]:
            available[key] -= 1
        elif reviewed(row):
            return True
    return False


def reviewed(row):
    return (row.get('category_source') == 'manual'
            or row.get('category_rationale') == 'human-approved category override'
            or bool(row.get('product_description') or row.get('product_url')))


def persist_items(cur, receipt, expense_pk, existing, schema):
    available = defaultdict(deque)
    for row in sorted(existing, key=lambda r: not reviewed(r)):
        available[item_key(row['item_name'], row['line_total'])].append(row)
    retained = set()
    for item in receipt.items:
        matches = available[item_key(item.item_name, item.line_total)]
        if matches:
            row = matches.popleft()
        else:
            cur.execute(f'''INSERT INTO {schema}.expense_items (
                expense_pk,item_name,unit_qty,unit_cost,line_total,original_line_total,category_source
                ) VALUES (%s,%s,1,%s,%s,%s,NULL) RETURNING id''',
                (expense_pk,item.item_name,item.line_total,item.line_total,item.line_total))
            saved = cur.fetchone()
            row = {'id': saved['id'] if isinstance(saved, dict) else saved[0], 'item_name': item.item_name}
        retained.add(row['id'])
        apply_category(cur, {**row, 'department': item.department, 'source': 'scanned',
                             'store_name': receipt.merchant}, row.get('product_description') or '', schema=schema)
    stale = [row['id'] for row in existing if row['id'] not in retained]
    if stale:
        cur.execute(f'DELETE FROM {schema}.expense_items WHERE expense_pk=%s AND id=ANY(%s)', (expense_pk, stale))
