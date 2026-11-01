"""Audited identity extraction from retained receipt text, without financial writes."""
import hashlib
import json
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .ingest import extract_merchant_store, extract_receipt_id
from .payment import extract_payment_provenance


def identity_plan(rows):
    changes = []
    inputs = []
    for row in rows:
        text = row['raw_text'] or ''
        old = {key: row.get(key) for key in ('merchant', 'receipt_id', 'payment_method', 'card_last4')}
        old['store'] = (row.get('raw_payload') or {}).get('store')
        merchant, store = extract_merchant_store(text, row.get('merchant_hint'))
        payment = extract_payment_provenance(text)
        new = {'merchant': merchant or (old['merchant'] if old['merchant'] != store else None), 'store': store or old['store'],
               'receipt_id': extract_receipt_id(text) or old['receipt_id'],
               'payment_method': payment.payment_method or old['payment_method'],
               'card_last4': payment.card_last4 or old['card_last4']}
        for correction in row.get('feedback_fields') or []:
            for key in new.keys() & correction.keys():
                new[key] = old[key]
        if not text.strip():
            continue
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        inputs.append({'id': row['id'], 'source_sha256': row['source_sha256'],
                       'text_sha256': text_hash, 'before': old, 'after': new})
        if old != new:
            changes.append({'id': row['id'], 'source_sha256': row['source_sha256'],
                            'source_reference': row['source_reference'], 'text_sha256': text_hash,
                            'before': old, 'after': new})
    version = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return {'version': version, 'scanned': len(rows), 'changes': changes, 'applied': False}


def run(args):
    import psycopg
    with psycopg.connect(args.db_dsn, row_factory=dict_row) as conn:
        # Serialize with receipt reconciliation; lock selected evidence so a
        # preview cannot be applied over a newly arrived extraction.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('budget.receipt-duplicates'))")
        rows = conn.execute('''
            SELECT re.id,re.source_sha256,re.source_reference,re.raw_text,re.raw_payload,
                   re.merchant,re.receipt_id,re.payment_method,re.card_last4,
                   e.store_name AS merchant_hint,
                   (SELECT jsonb_agg(f.corrected_fields) FROM budget.receipt_ocr_feedback f
                     WHERE f.evidence_id=re.id) AS feedback_fields
              FROM budget.receipt_evidence re LEFT JOIN budget.expenses e ON e.id=re.expense_pk
             WHERE COALESCE(re.raw_payload->>'source_archived','false')<>'true'
               AND (%s::text IS NULL OR re.source_reference=%s)
             ORDER BY re.id FOR UPDATE OF re
        ''', (args.source_reference, args.source_reference)).fetchall()
        plan = identity_plan(rows)
        if args.apply:
            if args.expected_version != plan['version']:
                raise ValueError('Identity inputs changed or --expected-version is missing; preview again')
            for change in plan['changes']:
                new = change['after']
                conn.execute('''UPDATE budget.receipt_evidence
                    SET merchant=%s,receipt_id=%s,payment_method=%s,card_last4=%s,
                        raw_payload=COALESCE(raw_payload,'{}'::jsonb)||jsonb_build_object('store',%s::text),
                        updated_at=NOW() WHERE id=%s''',
                    (new['merchant'],new['receipt_id'],new['payment_method'],new['card_last4'],new['store'],change['id']))
                conn.execute('''INSERT INTO lineage.receipt_events
                    (id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)''',
                    (uuid.uuid4(),change['source_sha256'],change['source_reference'],Jsonb({
                        'status':'identity_refreshed','operation':'receipt.identity-extraction.v1',
                        'basis':'retained_source_text','text_sha256':change['text_sha256'],
                        'before':change['before'],'after':new,'plan_version':plan['version']})))
            plan['applied'] = True
        print(json.dumps(plan, default=str))
    return 0
