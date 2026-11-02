"""Audited identity extraction from retained receipt text, without financial writes."""
import hashlib
import json
import uuid
import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .store_contact import extract_store_contact
from .ingest import extract_merchant_store, extract_store_address, extract_receipt_id, pdf_merchant_hint, sha256_file
from .payment import extract_payment_provenance


def identity_plan(rows):
    changes = []
    inputs = []
    for row in rows:
        text = row['raw_text'] or ''
        old = {key: row.get(key) for key in ('merchant', 'receipt_id', 'payment_method', 'card_last4')}
        old['store'] = (row.get('raw_payload') or {}).get('store')
        old['store_address'] = (row.get('raw_payload') or {}).get('store_address')
        for key in ('store_phone', 'tax_registration_number'):
            old[key] = (row.get('raw_payload') or {}).get(key)
        merchant, store = extract_merchant_store(text, row.get('merchant_hint'), observations=row.get('ocr_observations', []))
        payment = extract_payment_provenance(text)
        new = {'merchant': merchant or (old['merchant'] if old['merchant'] != store else None), 'store': store or old['store'],
               'store_address': extract_store_address(text, observations=row.get('ocr_observations', [])) or old['store_address'],
               'receipt_id': extract_receipt_id(text) or old['receipt_id'],
               'payment_method': payment.payment_method or old['payment_method'],
               'card_last4': payment.card_last4 or old['card_last4']}
        contact = extract_store_contact(text, observations=row.get('ocr_observations', []))
        new.update({key: value or old[key] for key, value in contact.items()})
        for correction in row.get('feedback_fields') or []:
            for key in new.keys() & correction.keys():
                new[key] = old[key]
        if not text.strip():
            continue
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        observations_hash = hashlib.sha256(json.dumps(row.get('ocr_observations') or [],sort_keys=True,default=str).encode()).hexdigest()
        inputs.append({'id': row['id'], 'source_sha256': row['source_sha256'],
                       'text_sha256': text_hash, 'ocr_observations_sha256': observations_hash, 'before': old, 'after': new})
        if old != new:
            changes.append({'id': row['id'], 'source_sha256': row['source_sha256'],
                            'source_reference': row['source_reference'], 'text_sha256': text_hash,
                            'ocr_observations_sha256': observations_hash,
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
        # Retained text may omit a browser-export issuer title. Consult only a
        # matching original PDF, never a guessed merchant from its card label.
        from .sources import resolve_reference
        root = os.getenv('RECEIPT_SOURCE_ROOT')
        if root:
            for row in rows:
                if row.get('merchant_hint'):
                    continue
                try:
                    path = resolve_reference(row['source_reference'],Path(root))
                    if path.is_file() and sha256_file(path)==row['source_sha256']:
                        row['merchant_hint'] = pdf_merchant_hint(path)
                except (OSError,ValueError):
                    continue
        from ..receipt_shared_evidence import recover_pass_text
        observation_errors = []
        for row in rows:
            passes = conn.execute('''
                SELECT p.*,r.evidence_id FROM budget.receipt_ocr_passes p
                  JOIN budget.receipt_ocr_runs r USING(run_uuid)
                 WHERE r.evidence_id=%s AND r.source_sha256=%s AND p.page_number=1
                   AND p.status='success' AND p.variant<>'geometry'
                   AND r.run_uuid=(SELECT latest.run_uuid FROM budget.receipt_ocr_runs latest
                       WHERE latest.evidence_id=%s ORDER BY latest.processed_at DESC,latest.run_uuid DESC LIMIT 1)
                 ORDER BY p.pass_id
            ''', (row['id'], row['source_sha256'], row['id'])).fetchall()
            observation_errors.extend(recover_pass_text(conn, [row], passes,
                cache_root=getattr(args,'ocr_cache',None), uri_prefix=getattr(args,'artifact_uri_prefix',None)))
            row['ocr_observations'] = [
                {'text':p['extracted_text'],'page_number':p['page_number'],'status':p['status'],
                 'run_uuid':str(p['run_uuid']),'pass_id':p['pass_id']}
                for p in passes if p.get('extracted_text')
            ]
        plan = identity_plan(rows)
        plan['observation_errors'] = observation_errors
        if args.apply:
            if args.expected_version != plan['version']:
                raise ValueError('Identity inputs changed or --expected-version is missing; preview again')
            for change in plan['changes']:
                new = change['after']
                conn.execute('''UPDATE budget.receipt_evidence
                    SET merchant=%s,receipt_id=%s,payment_method=%s,card_last4=%s,
                        raw_payload=COALESCE(raw_payload,'{}'::jsonb)||jsonb_build_object('store',%s::text,'store_address',%s::text,'store_phone',%s::text,'tax_registration_number',%s::text),
                        updated_at=NOW() WHERE id=%s''',
                    (new['merchant'],new['receipt_id'],new['payment_method'],new['card_last4'],new['store'],new['store_address'],new['store_phone'],new['tax_registration_number'],change['id']))
                conn.execute('''INSERT INTO lineage.receipt_events
                    (id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)''',
                    (uuid.uuid4(),change['source_sha256'],change['source_reference'],Jsonb({
                        'status':'identity_refreshed','operation':'receipt.identity-extraction.v1',
                        'basis':'retained_source_text_and_ocr_passes','text_sha256':change['text_sha256'],
                        'ocr_observations_sha256':change['ocr_observations_sha256'],
                        'before':change['before'],'after':new,'plan_version':plan['version']})))
            plan['applied'] = True
        print(json.dumps(plan, default=str))
    return 0
