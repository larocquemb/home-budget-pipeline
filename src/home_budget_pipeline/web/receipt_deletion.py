"""Reversible receipt deletion, with original files and audit history retained."""
import json
import re
import uuid
from datetime import datetime, timezone

from ..receipts.catalogue import RECEIPT_CATALOGUE


def change(connect, sha, *, deleted, actor):
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from ..db_setup import ANALYTICS_SCHEMA
    if not re.fullmatch('[0-9a-f]{64}', sha) or not actor.strip():
        raise ValueError('Receipt identity and authenticated user are required')
    conn = connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('budget.receipt-duplicates'))")
            cur.execute('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s FOR UPDATE', (sha,))
            before = cur.fetchone()
            if before is None:
                cur.execute(f'SELECT source_reference FROM {RECEIPT_CATALOGUE} WHERE source_sha256=%s', (sha,))
                source = cur.fetchone()
                if source is None:
                    raise LookupError('Receipt not found')
                if not deleted:
                    return {'deleted':False,'unchanged':True,'source_sha256':sha}
                cur.execute('''INSERT INTO budget.receipt_evidence
                    (source_sha256,source_reference,evidence_type,raw_payload)
                    VALUES (%s,%s,%s,'{}'::jsonb) RETURNING *''',
                    (sha,source['source_reference'],'electronic' if source['source_reference'].startswith('electronic/') else 'scanned'))
                current = cur.fetchone()
            else:
                current = before
            payload = dict(current.get('raw_payload') or {})
            if bool(payload.get('receipt_deleted')) == deleted:
                return {'deleted':deleted,'unchanged':True,'source_sha256':sha}
            # Use the same idempotent view migration as database setup. Installing
            # older dev schemas is part of this transaction, not a user CLI step.
            cur.execute("SELECT definition FROM pg_views WHERE schemaname='budget' AND viewname=ANY(%s)",
                        (['analytics_expenses','analytics_expense_items','analytics_category_spend'],))
            views = cur.fetchall()
            if len(views) != 3 or any('receipt_deleted' not in v['definition'] for v in views):
                cur.execute(ANALYTICS_SCHEMA.read_text())
            audit_id = str(uuid.uuid4())
            marker = {'audit_id':audit_id,'actor_user':actor,'deleted_at':datetime.now(timezone.utc).isoformat()}
            if deleted:
                payload['receipt_deleted'] = marker
            else:
                payload.pop('receipt_deleted', None)
            cur.execute('UPDATE budget.receipt_evidence SET raw_payload=%s,updated_at=NOW() WHERE id=%s',
                        (Jsonb(payload),current['id']))
            expense_before = expense_after = None
            expense_pk = current.get('expense_pk')
            if expense_pk is not None:
                cur.execute('SELECT * FROM budget.expenses WHERE id=%s FOR UPDATE',(expense_pk,))
                expense_before = cur.fetchone()
                cur.execute('''SELECT id,raw_payload FROM budget.receipt_evidence
                    WHERE expense_pk=%s ORDER BY id FOR UPDATE''',(expense_pk,))
                active = [r for r in cur.fetchall() if not (r.get('raw_payload') or {}).get('receipt_deleted')]
                expense_payload = dict(expense_before.get('raw_payload') or {})
                if not active:
                    expense_payload['receipt_deleted'] = marker
                else:
                    expense_payload.pop('receipt_deleted',None)
                cur.execute('UPDATE budget.expenses SET raw_payload=%s WHERE id=%s RETURNING *',
                            (Jsonb(expense_payload),expense_pk))
                expense_after = cur.fetchone()
            cur.execute('SELECT * FROM budget.receipt_evidence WHERE id=%s',(current['id'],))
            after = cur.fetchone()
            event = {'event':'receipt_deleted' if deleted else 'receipt_restored','actor_user':actor,
                     'before':before,'after':after,'expense_before':expense_before,'expense_after':expense_after}
            cur.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)',
                        (audit_id,sha,current['source_reference'],Jsonb(json.loads(json.dumps(event,default=str)))))
        conn.commit()
        return {'deleted':deleted,'source_sha256':sha,'audit_id':audit_id,'expense_pk':expense_pk}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
