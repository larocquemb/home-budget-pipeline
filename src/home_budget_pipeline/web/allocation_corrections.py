"""Audited corrections to canonical receipt allocations; OCR evidence is immutable."""
import hashlib
import json
import re
import uuid

from ..receipts.allocation import assess, money


class AllocationConflict(ValueError):
    """Reload or retain an item whose review history must not be discarded."""


HEADER_FIELDS = ('id', 'source', 'order_id', 'source_sha256', 'store_name',
                 'receipt_item_subtotal', 'receipt_gst', 'receipt_pst',
                 'receipt_discount_total', 'receipt_tip', 'receipt_service_fee',
                 'receipt_recycling_fee', 'receipt_service_fee_tax',
                 'receipt_total_charged', 'expense_total', 'extraction_status', 'updated_at')


def _json(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(',', ':'))


def _read(cur, sha, *, lock=False):
    cur.execute('''SELECT e.* FROM budget.expenses e
        JOIN budget.receipt_evidence re ON re.expense_pk=e.id
        WHERE re.source_sha256=%s ORDER BY re.is_primary_source DESC,re.id LIMIT 1'''
        + (' FOR UPDATE OF e' if lock else ''), (sha,))
    expense = cur.fetchone()
    if expense is None:
        raise LookupError('Receipt has no accounting record')
    cur.execute('SELECT * FROM budget.expense_items WHERE expense_pk=%s ORDER BY id'
                + (' FOR UPDATE' if lock else ''), (expense['id'],))
    items = cur.fetchall()
    cur.execute('''SELECT id,source_reference,merchant,extraction_status FROM budget.receipt_evidence
        WHERE expense_pk=%s AND source_sha256=%s ORDER BY id''', (expense['id'], sha))
    evidence = cur.fetchall()
    snapshot = {'expense': {k: expense.get(k) for k in HEADER_FIELDS},
                'items': items, 'evidence': evidence}
    charges = sum(money(expense.get(k) or 0) for k in
                  ('receipt_tip', 'receipt_service_fee', 'receipt_recycling_fee', 'receipt_service_fee_tax'))
    assessment = assess(items, expense.get('receipt_item_subtotal'),
                        money(expense.get('receipt_gst') or 0) + money(expense.get('receipt_pst') or 0),
                        expense.get('expense_total'), charges=charges,
                        discount=expense.get('receipt_discount_total') or 0)
    correction = (expense.get('raw_payload') or {}).get('item_allocation_correction')
    return {'version': hashlib.sha256(_json(snapshot).encode()).hexdigest(),
            'snapshot': snapshot, 'assessment': assessment, 'correction': correction}, expense


def read(connect, sha):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            result, _ = _read(cur, sha)
            return result
    finally:
        conn.close()


def normalize(plan):
    if set(plan) - {'merchant', 'subtotal', 'gst', 'pst', 'included_tax', 'reason', 'items'}:
        raise ValueError('Unsupported allocation field')
    result = dict(plan)
    for key, maximum in [('merchant', 300), ('reason', 2000)]:
        value = result.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f'A valid {key} is required')
        result[key] = value.strip()
    for key in ('subtotal', 'gst', 'pst', 'included_tax'):
        result[key] = money(result.get(key, 0))
        if result[key] is None or abs(result[key]) >= 10_000_000_000:
            raise ValueError(f'A valid {key} amount is required')
    items = result.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 500:
        raise ValueError('Supply between 1 and 500 allocation items')
    normalized, ids = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {'id', 'item_name', 'line_total'}:
            raise ValueError('Unsupported allocation item')
        name = item.get('item_name')
        if not isinstance(name, str) or not name.strip() or len(name) > 1000:
            raise ValueError('Each item needs a name of at most 1000 characters')
        identity = item.get('id')
        if identity is not None:
            if type(identity) is not int or identity <= 0 or identity in ids:
                raise ValueError('Item identities must be unique positive integers')
            ids.add(identity)
        amount = money(item.get('line_total'))
        if amount is None or abs(amount) >= 10_000_000_000:
            raise ValueError('Each item needs a valid amount')
        normalized.append({'id': identity, 'item_name': name.strip(), 'line_total': amount})
    result['items'] = normalized
    return result


def _protect_removed(cur, removed, rows):
    if not removed:
        return
    for row in rows:
        if row['id'] in removed and (row.get('product_description') or row.get('product_url')
                or row.get('category_source') == 'manual'):
            raise AllocationConflict('Retain items with reviewed product/category information')
    # Preserve model collaborations, acceptance history, annotations and audits,
    # including references added by later schema versions.
    from psycopg import sql
    cur.execute('''SELECT n.nspname,c.relname,a.attname FROM pg_constraint fk
        JOIN pg_class c ON c.oid=fk.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=fk.conrelid AND a.attnum=ANY(fk.conkey)
        WHERE fk.contype='f' AND fk.confrelid='budget.expense_items'::regclass''')
    for fk in cur.fetchall():
        cur.execute(sql.SQL('SELECT 1 FROM {}.{} WHERE {}=ANY(%s) LIMIT 1').format(
            sql.Identifier(fk['nspname']), sql.Identifier(fk['relname']), sql.Identifier(fk['attname'])),
            (list(removed),))
        if cur.fetchone():
            raise AllocationConflict('Retain items with review/audit history; correct their existing rows instead')


def correct(connect, sha, expected_version, plan, *, actor_user, actor_email=None):
    from psycopg.types.json import Jsonb
    if not re.fullmatch(r'[0-9a-f]{64}', expected_version or ''):
        raise ValueError('A current allocation version is required')
    plan = normalize(plan)
    if not actor_user or not actor_user.strip():
        raise ValueError('An audit actor is required')
    conn = connect()
    try:
        with conn.cursor() as cur:
            before, expense = _read(cur, sha, lock=True)
            if before['version'] != expected_version:
                raise AllocationConflict('The receipt changed; reload before correcting it')
            charges = sum(money(expense.get(k) or 0) for k in
                          ('receipt_tip', 'receipt_service_fee', 'receipt_recycling_fee', 'receipt_service_fee_tax'))
            assessment = assess(plan['items'], plan['subtotal'], plan['gst'] + plan['pst'],
                                expense.get('expense_total'), charges=charges,
                                discount=expense.get('receipt_discount_total') or 0)
            if not assessment['reconciled']:
                raise ValueError('Allocations must reconcile to the subtotal and unchanged receipt total')
            old = {i['id']: i for i in before['snapshot']['items']}
            retained = {i['id'] for i in plan['items'] if i['id'] is not None}
            if retained - old.keys():
                raise ValueError('An item does not belong to this receipt')
            removed = old.keys() - retained
            _protect_removed(cur, removed, old.values())
            if removed:
                cur.execute('DELETE FROM budget.expense_items WHERE id=ANY(%s)', (list(removed),))
            for item in plan['items']:
                if item['id'] is None:
                    cur.execute('''INSERT INTO budget.expense_items
                        (expense_pk,item_name,line_total,original_line_total,category_source,category_requires_review)
                        VALUES (%s,%s,%s,%s,'unresolved',TRUE) RETURNING id''',
                        (expense['id'], item['item_name'], item['line_total'], item['line_total']))
                    item['id'] = cur.fetchone()['id']
                else:
                    cur.execute('''UPDATE budget.expense_items SET item_name=%s,line_total=%s,
                        original_line_total=COALESCE(original_line_total,line_total),updated_at=NOW()
                        WHERE id=%s AND expense_pk=%s''',
                        (item['item_name'], item['line_total'], item['id'], expense['id']))
            audit_id = str(uuid.uuid4())
            marker = {'audit_id': audit_id, 'actor_user': actor_user,
                      'reason': plan['reason'], 'included_tax': str(plan['included_tax'])}
            cur.execute('''UPDATE budget.expenses SET store_name=%s,receipt_item_subtotal=%s,
                receipt_gst=%s,receipt_pst=%s,
                raw_payload=COALESCE(raw_payload,'{}'::jsonb) || jsonb_build_object('item_allocation_correction',%s::jsonb),
                updated_at=NOW() WHERE id=%s''',
                (plan['merchant'], plan['subtotal'], plan['gst'], plan['pst'], Jsonb(marker), expense['id']))
            # Status remains review when unrelated extraction issues are present.
            remaining = [r for r in (expense.get('raw_payload') or {}).get('review_reasons', [])
                         if r not in {'item_total_mismatch', 'header_total_mismatch', 'invalid_allocation_amount',
                                      'missing_items', 'missing_item_amounts', 'missing_allocation_subtotal',
                                      'unverified_header_total', 'missing_merchant'}]
            status = 'review' if remaining else 'complete'
            cur.execute('UPDATE budget.expenses SET extraction_status=%s WHERE id=%s', (status, expense['id']))
            cur.execute('''UPDATE budget.receipt_evidence SET merchant=%s,extraction_status=%s,updated_at=NOW()
                WHERE expense_pk=%s AND source_sha256=%s''', (plan['merchant'], status, expense['id'], sha))
            after, _ = _read(cur, sha)
            event = {'event': 'receipt_allocation_corrected', 'actor_user': actor_user,
                     'actor_email': actor_email, 'reason': plan['reason'], 'before': before['snapshot'],
                     'after': after['snapshot'], 'plan': plan, 'assessment': assessment,
                     'expected_version': expected_version, 'included_tax_is_additive': False}
            cur.execute('''INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload)
                VALUES (%s,%s,%s,%s)''', (audit_id, sha, before['snapshot']['evidence'][0]['source_reference'],
                                       Jsonb(json.loads(_json(event)))))
        conn.commit()
        return {'audit_id': audit_id, 'version': after['version'], 'assessment': after['assessment'],
                'extraction_status': status, 'item_count': len(plan['items'])}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
