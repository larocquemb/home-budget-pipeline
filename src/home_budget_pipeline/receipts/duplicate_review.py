"""Candidate capture and atomic, versioned human duplicate decisions (KAN-151)."""
import hashlib
import json
import re
import uuid
from dataclasses import asdict, replace

from .deduplication import (
    DuplicateDisposition, ReceiptDuplicateScorer, ReceiptEvidenceFingerprint,
)
from .deduplication_store import PostgresReceiptDuplicateStore


def fingerprint(row):
    return ReceiptEvidenceFingerprint(
        evidence_id=row['id'], source_type=row['evidence_type'],
        **{k: row.get(k) for k in ('merchant', 'transaction_datetime', 'total', 'receipt_id', 'card_last4')})


def version(state):
    return hashlib.sha256(json.dumps(state, default=str, sort_keys=True).encode()).hexdigest()


def capture_candidates(conn, evidence_id, schema='budget', *, allow_auto=False):
    """Called inside the import transaction; never commits or invents acceptance.

    Low-score same-time/total documents remain review candidates even when OCR
    merchant strings differ. A score alone never authorizes linking purchases.
    """
    from psycopg.rows import dict_row
    if schema != 'budget':
        # The duplicate schema belongs to budget; custom import schemas do not
        # have this feature installed and must not write into another schema.
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute('SELECT * FROM budget.receipt_evidence WHERE id=%s', (evidence_id,))
        source = cur.fetchone()
        if not source or any((source.get('raw_payload') or {}).get(k) for k in ('source_archived','receipt_deleted')):
            return []
        cur.execute('''SELECT * FROM budget.receipt_evidence
            WHERE id<>%s AND COALESCE(raw_payload->>'source_archived','false') <> 'true'
                AND NOT (COALESCE(raw_payload,'{}'::jsonb) ? 'receipt_deleted') AND ((receipt_id IS NOT NULL AND receipt_id<>'' AND receipt_id=%s)
                OR (total=%s AND transaction_datetime::date=%s::date))
            ORDER BY id''', (evidence_id, source['receipt_id'], source['total'], source['transaction_datetime']))
        others = cur.fetchall()
        results = []
        store = PostgresReceiptDuplicateStore(conn, commit_on_write=False)
        for other in others:
            if source['expense_pk'] is not None and source['expense_pk'] == other['expense_pk']:
                continue
            result = ReceiptDuplicateScorer().score(fingerprint(source), fingerprint(other))
            if result.disposition is DuplicateDisposition.DISTINCT:
                if not {'time_within_5m', 'total_exact'} <= set(result.reasons):
                    continue
                result = replace(result, disposition=DuplicateDisposition.REVIEW,
                    reasons=(*result.reasons, 'low_score_same_time_total_requires_human_review'))
            # Rescans must never reopen or overwrite a human decision.
            left, right = sorted((evidence_id, other['id']))
            cur.execute('SELECT resolution_status FROM budget.receipt_duplicate_links '
                        'WHERE left_evidence_id=%s AND right_evidence_id=%s', (left, right))
            prior = cur.fetchone()
            if prior and prior['resolution_status'] != 'pending':
                continue
            store.save_score(result)
            if other['expense_pk'] != source['expense_pk'] or source['expense_pk'] is None:
                results.append(result)
        targets = {other['expense_pk'] for other in others if other['expense_pk'] is not None
                   and any(other['id'] in (r.left_evidence_id, r.right_evidence_id) for r in results)}
        precise_time = source['transaction_datetime'] and source['transaction_datetime'].time().isoformat() != '00:00:00'
        strong_identifier = len(re.sub(r'\D', '', source['receipt_id'] or '')) >= 6
        exact = [r for r in results if r.disposition is DuplicateDisposition.EXACT
                 and precise_time and strong_identifier and 'identifier_match' in r.reasons
                 and any(other['expense_pk'] is not None and other['id'] in
                         (r.left_evidence_id, r.right_evidence_id) for other in others)]
        if allow_auto and source['expense_pk'] is None and len(targets) == 1 and len(exact) == 1:
            # Only an unambiguous exact match can be automatic. Never merge two
            # existing purchases or silently upgrade a mere metadata match.
            from psycopg.types.json import Jsonb
            result = exact[0]
            before = read_state(cur, result.left_evidence_id, result.right_evidence_id, lock=True)
            store.auto_link_exact(result, next(iter(targets)))
            after = read_state(cur, result.left_evidence_id, result.right_evidence_id)
            cur.execute('''INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload)
                VALUES (%s,%s,%s,%s)''', (str(uuid.uuid4()), source['source_sha256'], source['source_reference'],
                Jsonb(json.loads(json.dumps({'event': 'receipt_duplicate_auto_linked',
                    'actor_user': 'receipt_import', 'reason': 'Unambiguous exact duplicate score',
                    'before': before, 'after': after}, default=str)))))
        return results


def read_state(cur, left, right, lock=False):
    left, right = sorted((left, right))
    if left <= 0 or left == right:
        raise ValueError('Choose two different positive evidence IDs')
    cur.execute('SELECT * FROM budget.receipt_evidence WHERE id IN (%s,%s) ORDER BY id'
                + (' FOR UPDATE' if lock else ''), (left, right))
    evidence = cur.fetchall()
    if len(evidence) != 2:
        raise LookupError('Receipt evidence not found')
    cur.execute('SELECT * FROM budget.receipt_duplicate_links WHERE left_evidence_id=%s AND right_evidence_id=%s'
                + (' FOR UPDATE' if lock else ''), (left, right))
    return {'evidence': evidence, 'decision': cur.fetchone()}


def resolve(connect, left, right, *, expense=None, reject=False, reason='', actor='',
            expected_version=None, apply=False):
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    if not reason.strip() or len(reason) > 2000 or not actor.strip():
        raise ValueError('A reviewer and reason (at most 2000 characters) are required')
    if not reject and (not expense or expense <= 0):
        raise ValueError('Confirmation requires an existing canonical expense ID')
    if apply and not re.fullmatch('[0-9a-f]{64}', expected_version or ''):
        raise ValueError('Apply requires the current preview version')
    conn = connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            if not apply:
                cur.execute('SET TRANSACTION READ ONLY')
            else:
                # Same lock as imports; two decisions cannot race candidate capture.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('budget.receipt-duplicates'))")
            before = read_state(cur, left, right, lock=apply)
            current_version = version(before)
            decision = before['decision'] or {}
            if decision.get('resolution_status') in ('confirmed', 'auto_linked', 'rejected'):
                same = (reject and decision['resolution_status'] == 'rejected') or (
                    not reject and decision.get('canonical_expense_pk') == expense
                    and decision['resolution_status'] in ('confirmed', 'auto_linked'))
                if same:
                    if not reject and any(e['expense_pk'] != expense for e in before['evidence']):
                        raise ValueError('Evidence links no longer match the saved decision; separate review required')
                    return {'apply': apply, 'unchanged': True, 'version': current_version,
                            'expense_pk': decision.get('canonical_expense_pk')}
                raise ValueError('This pair is already resolved; conflicting decisions require separate review')
            if not reject:
                links = [e['expense_pk'] for e in before['evidence']]
                if expense not in links or any(e not in (None, expense) for e in links):
                    raise ValueError('One document must belong to the chosen expense; merging or reassigning purchases is not supported')
                cur.execute('SELECT id FROM budget.expenses WHERE id=%s', (expense,))
                if cur.fetchone() is None:
                    raise LookupError('Canonical expense not found')
            elif (before['evidence'][0]['expense_pk'] is not None
                  and before['evidence'][0]['expense_pk'] == before['evidence'][1]['expense_pk']):
                raise ValueError('Documents already share an expense; rejection cannot split an existing purchase')
            if not apply:
                return {'apply': False, 'version': current_version, 'expense_pk': expense,
                        'action': 'reject' if reject else 'confirm', 'evidence': before['evidence']}
            if current_version != expected_version:
                raise ValueError('Receipt or decision changed; preview again before applying')
            score = ReceiptDuplicateScorer().score(*(fingerprint(e) for e in before['evidence']))
            # Human confirmation is distinct from the automated score. Retain the
            # real score/reasons and explicitly record any low-score override.
            if score.disposition is DuplicateDisposition.DISTINCT:
                score = replace(score, disposition=DuplicateDisposition.REVIEW,
                    reasons=(*score.reasons, 'human_review_of_low_score_pair'))
            store = PostgresReceiptDuplicateStore(conn, commit_on_write=False)
            if reject:
                store.save_score(score)
                result = store.reject(score)
            else:
                result = store.confirm_link(score, expense)
            after = read_state(cur, left, right)
            audit_id = str(uuid.uuid4())
            source = before['evidence'][-1]
            payload = {'event': 'receipt_duplicate_rejected' if reject else 'receipt_duplicate_confirmed',
                       'actor_user': actor, 'reason': reason.strip(), 'before': before, 'after': after}
            cur.execute('''INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload)
                VALUES (%s,%s,%s,%s)''', (audit_id, source['source_sha256'], source['source_reference'],
                Jsonb(json.loads(json.dumps(payload, default=str)))))
        conn.commit()
        return {'apply': True, 'audit_id': audit_id, **asdict(result)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
