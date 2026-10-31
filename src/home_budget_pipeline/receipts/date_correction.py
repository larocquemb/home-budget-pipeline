"""Versioned, atomic corrections of receipt-local dates without replacing OCR."""
import hashlib
import json
import re
import uuid
from datetime import datetime


def normalize_datetime(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?', value):
        raise ValueError('Use an ISO local date or datetime without a timezone')
    return datetime.fromisoformat(value)


def _read(cur, sha, lock=False):
    if not re.fullmatch(r'[0-9a-f]{64}', sha or ''):
        raise ValueError('A receipt SHA-256 is required')
    cur.execute('SELECT * FROM budget.receipt_evidence WHERE source_sha256=%s'
                + (' FOR UPDATE' if lock else ''), (sha,))
    evidence = cur.fetchone()
    if evidence is None:
        raise LookupError('Receipt evidence not found')
    expense = None
    if evidence['expense_pk'] is not None:
        cur.execute('SELECT * FROM budget.expenses WHERE id=%s'
                    + (' FOR UPDATE' if lock else ''), (evidence['expense_pk'],))
        expense = cur.fetchone()
    cur.execute('SELECT * FROM budget.receipt_ocr_feedback WHERE evidence_id=%s'
                + (' FOR UPDATE' if lock else ''), (evidence['id'],))
    snapshot = {'evidence': evidence, 'expense': expense, 'feedback': cur.fetchone()}
    version = hashlib.sha256(json.dumps(snapshot, default=str, sort_keys=True).encode()).hexdigest()
    return {'version': version, 'snapshot': snapshot}


def correct(connect, sha, value, reason, *, actor_user, expected_version=None, apply=False):
    from psycopg.types.json import Jsonb
    from .evidence import record_ocr_feedback
    value = normalize_datetime(value)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('A correction reason is required (at most 2000 characters)')
    if not actor_user or not actor_user.strip():
        raise ValueError('An audit actor is required')
    if apply and not re.fullmatch(r'[0-9a-f]{64}', expected_version or ''):
        raise ValueError('Apply requires the current preview version')
    conn = connect()
    try:
        with conn.cursor() as cur:
            if not apply:
                cur.execute('SET TRANSACTION READ ONLY')
            before = _read(cur, sha, lock=apply)
            snapshot = before['snapshot']
            if not apply:
                return {'apply': False, 'version': before['version'],
                        'old_datetime': snapshot['evidence']['transaction_datetime'],
                        'new_datetime': value, 'expense_pk': snapshot['evidence']['expense_pk']}
            if before['version'] != expected_version:
                raise ValueError('Receipt changed; reload and preview before applying')
            audit_id = str(uuid.uuid4())
            cur.execute('UPDATE budget.receipt_evidence SET transaction_datetime=%s,updated_at=NOW() WHERE id=%s',
                        (value, snapshot['evidence']['id']))
            if snapshot['expense']:
                marker = {'audit_id': audit_id, 'reason': reason.strip(), 'actor_user': actor_user}
                cur.execute("""UPDATE budget.expenses SET order_date=%s,transaction_datetime=%s,
                    raw_payload=COALESCE(raw_payload,'{}'::jsonb) || jsonb_build_object('receipt_date_correction',%s::jsonb),
                    updated_at=NOW() WHERE id=%s""",
                    (value.date(), value, Jsonb(marker), snapshot['expense']['id']))
            fields = dict((snapshot['feedback'] or {}).get('corrected_fields') or {})
            fields['transaction_datetime'] = value.isoformat(timespec='seconds')
            record_ocr_feedback(conn, snapshot['evidence']['id'], 'corrected', fields,
                                notes=reason.strip(), verified_by=actor_user)
            after = _read(cur, sha)
            cur.execute('''INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload)
                VALUES (%s,%s,%s,%s)''', (audit_id, sha, snapshot['evidence']['source_reference'],
                Jsonb(json.loads(json.dumps({'event': 'receipt_date_corrected', 'actor_user': actor_user,
                    'reason': reason.strip(), 'before': snapshot, 'after': after['snapshot']}, default=str)))))
        conn.commit()
        return {'apply': True, 'audit_id': audit_id, 'version': after['version'], 'new_datetime': value}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
