"""Explicit, versioned consolidation of already-booked duplicate purchases."""
import json
import uuid
from decimal import Decimal

from .duplicate_review import version


def source_quality(expense, evidence, items=()):
    """Validation takes precedence over coverage confidence; no merchant/receipt rules."""
    row = next((e for e in evidence if e.get('source_sha256') == expense.get('source_sha256')),
               next((e for e in evidence if e.get('is_primary_source')), evidence[0] if evidence else {}))
    decisions = (row.get('raw_payload') or {}).get('field_decisions') or {}
    conflicts = sum(d.get('status') == 'conflict' for d in decisions.values())
    subtotal, total = expense.get('receipt_item_subtotal'), expense.get('expense_total')
    payload = expense.get('raw_payload') or {}
    printed_tax = payload.get('tax_components') or {}
    canonical_tax = sum((Decimal(str(expense.get(k) or 0)) for k in ('receipt_gst','receipt_pst')), Decimal(0))
    observed_tax = sum((Decimal(str(v)) for v in printed_tax.values()), Decimal(0)) if printed_tax else None
    tax_agrees = observed_tax is None or abs(observed_tax-canonical_tax) <= Decimal('.01')
    money_check = 0
    if subtotal is not None and total is not None:
        calculated = Decimal(str(subtotal))
        for name in ('receipt_tip','receipt_service_fee','receipt_recycling_fee','receipt_service_fee_tax'):
            calculated += Decimal(str(expense.get(name) or 0))
        calculated += observed_tax if observed_tax is not None else canonical_tax
        calculated -= Decimal(str(expense.get('receipt_discount_total') or 0))
        money_check = 1 if abs(calculated - Decimal(str(total))) <= Decimal('.01') and tax_agrees else -1
    item_variance = None
    if items and subtotal is not None:
        item_variance = sum((Decimal(str(i.get('line_total') or 0)) for i in items),Decimal(0)) - Decimal(str(subtotal))
    item_check = 0 if item_variance is None else (1 if abs(item_variance) <= Decimal('.01') else -1)
    reasons = sorted(set(payload.get('review_reasons') or []) | set((row.get('raw_payload') or {}).get('review_reasons') or []))
    coverage = sum(row.get(name) is not None for name in ('merchant','transaction_datetime','total'))
    confidence_value = row.get('extraction_confidence')
    if confidence_value is None:
        confidence_value = expense.get('extraction_confidence')
    confidence = float(confidence_value or 0)
    status = row.get('extraction_status') or expense.get('extraction_status')
    rank = (money_check, item_check, -conflicts, -len(reasons), coverage, int(status == 'complete'), confidence)
    return {'expense_pk':expense['id'], 'rank':rank, 'monetary_reconciliation':money_check,
            'printed_tax_agrees':tax_agrees, 'item_reconciliation':item_check,
            'item_subtotal_variance':str(item_variance) if item_variance is not None else None,
            'review_reasons':reasons, 'conflicting_fields':conflicts, 'core_field_coverage':coverage,
            'extraction_status':status, 'extraction_confidence':confidence,
            'evidence_id':row.get('id')}


def select_source(state):
    compared = [source_quality(e, [r for r in state['evidence'] if r['expense_pk'] == e['id']],
                               [i for i in state['items'] if i['expense_pk'] == e['id']])
                for e in state['expenses']]
    ordered = sorted(compared, key=lambda q: (q['rank'], -q['expense_pk']), reverse=True)
    return {'expense_pk':ordered[0]['expense_pk'], 'comparison':compared,
            'policy':'validated-source-quality-v2',
            'requires_review':any(q['review_reasons'] or q['conflicting_fields'] or q['item_reconciliation'] < 0 or q['monetary_reconciliation'] < 0 for q in ordered[:1]),
            'reason':'Prefer receipt totals reconciled against printed tax, item reconciliation, fewer field conflicts and review reasons, field coverage, complete extraction, then coverage confidence; equal quality retains the oldest ID. Selection does not approve unresolved fields.'}


def load(cur, keeper, duplicate, *, lock=False):
    suffix = ' FOR UPDATE' if lock else ''
    cur.execute('SELECT * FROM budget.expenses WHERE id IN (%s,%s) ORDER BY id' + suffix, (keeper, duplicate))
    expenses = cur.fetchall()
    if len(expenses) != 2:
        raise LookupError('Both existing expenses are required')
    cur.execute('SELECT * FROM budget.expense_items WHERE expense_pk IN (%s,%s) ORDER BY id' + suffix, (keeper, duplicate))
    items = cur.fetchall()
    cur.execute('SELECT * FROM budget.receipt_evidence WHERE expense_pk IN (%s,%s) ORDER BY id' + suffix, (keeper, duplicate))
    evidence = cur.fetchall()
    cur.execute('SELECT * FROM budget.receipt_duplicate_links WHERE canonical_expense_pk IN (%s,%s) '
                'OR (left_evidence_id=ANY(%s) AND right_evidence_id=ANY(%s)) ORDER BY id' + suffix,
                (keeper, duplicate, [e['id'] for e in evidence], [e['id'] for e in evidence]))
    decisions = cur.fetchall()
    cur.execute('SELECT transaction_id,expense_pk FROM budget.transaction_expense_reconciliation WHERE expense_pk IN (%s,%s) ORDER BY transaction_id' + suffix, (keeper,duplicate))
    transactions = cur.fetchall()
    cur.execute('SELECT * FROM budget.expense_transaction_enrichment WHERE expense_pk IN (%s,%s) ORDER BY expense_pk' + suffix, (keeper,duplicate))
    enrichment = cur.fetchall()
    return dict(expenses=expenses, items=items, evidence=evidence, decisions=decisions,
                transactions=transactions, enrichment=enrichment)


def validate(state, keeper, duplicate):
    expenses = {row['id']: row for row in state['expenses']}
    if any((row.get('raw_payload') or {}).get('duplicate_merge') for row in expenses.values()):
        raise ValueError('A purchase is already superseded; review its existing merge separately')
    if not all(any(e['expense_pk'] == pk for e in state['evidence']) for pk in (keeper, duplicate)):
        raise ValueError('Both purchases require retained receipt evidence')
    if any(row['expense_pk'] == duplicate for row in state['transactions'] + state['enrichment']):
        raise ValueError('Duplicate purchase has financial transaction links; reconcile those explicitly first')
    if any(d['resolution_status'] == 'rejected' for d in state['decisions']):
        raise ValueError('A rejected duplicate decision requires separate review')


def merge(connect, keeper, duplicate, *, reason, actor, apply=False, expected_version=None, auto_select=False):
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    if not keeper or not duplicate or min(keeper, duplicate) <= 0 or keeper == duplicate:
        raise ValueError('Choose two different positive expense IDs')
    if not actor.strip() or not reason.strip() or len(reason) > 2000:
        raise ValueError('Reviewer and reason (at most 2000 characters) are required')
    conn = connect()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            if apply:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('budget.receipt-duplicates'))")
            else:
                cur.execute('SET TRANSACTION READ ONLY')
            before = load(cur, keeper, duplicate, lock=apply)
            selection = None
            if auto_select:
                merged = [e for e in before['expenses'] if (e.get('raw_payload') or {}).get('duplicate_merge')]
                if len(merged) == 1:
                    duplicate = merged[0]['id']
                    keeper = merged[0]['raw_payload']['duplicate_merge']['canonical_expense_pk']
                    if keeper not in {e['id'] for e in before['expenses']}:
                        raise ValueError('Existing merge points outside this pair; separate review required')
            if auto_select and not any((e.get('raw_payload') or {}).get('duplicate_merge') for e in before['expenses']):
                selection = select_source(before)
                if selection['expense_pk'] != keeper:
                    keeper, duplicate = duplicate, keeper
            old = next(e for e in before['expenses'] if e['id'] == duplicate)
            marker = (old.get('raw_payload') or {}).get('duplicate_merge') or {}
            if marker.get('canonical_expense_pk') == keeper:
                target = next(e for e in before['expenses'] if e['id'] == keeper)
                if (target.get('raw_payload') or {}).get('duplicate_merge') or any(e['expense_pk'] != keeper for e in before['evidence']):
                    raise ValueError('Saved merge links changed; separate review required')
                return {'apply': apply, 'unchanged': True, 'expense_pk': keeper,
                        'superseded_expense_pk': duplicate, 'audit_id': marker.get('audit_id')}
            validate(before, keeper, duplicate)
            token_input = {'keeper':keeper, 'duplicate':duplicate, 'state':before}
            if selection is not None:
                token_input['selection'] = selection
            token = version(token_input)
            if not apply:
                return {'apply': False, 'version': token, 'keeper': keeper, 'duplicate': duplicate,
                        'effect': 'Keep chosen purchase/items; retain superseded purchase/items as history, relink its evidence, exclude it from active spending.',
                        'selection':selection, 'before': before}
            if token != expected_version:
                raise ValueError('Purchase or evidence changed; preview again before applying')
            cur.execute("SELECT viewname,definition FROM pg_views WHERE schemaname='budget' AND viewname=ANY(%s)",
                        (['analytics_expenses', 'analytics_expense_items', 'analytics_category_spend'],))
            views = cur.fetchall()
            if len(views) != 3 or any('duplicate_merge' not in row['definition'] for row in views):
                raise ValueError('Run ledger --env-file .env.dev database setup to install duplicate-aware reporting views first')
            audit_id = str(uuid.uuid4())
            marker = {'canonical_expense_pk': keeper, 'superseded_expense_pk': duplicate,
                      'audit_id': audit_id, 'reason': reason.strip(), 'actor_user': actor}
            cur.execute("UPDATE budget.expenses SET raw_payload=COALESCE(raw_payload,'{}'::jsonb)||jsonb_build_object('duplicate_merge',%s::jsonb) WHERE id=%s", (Jsonb(marker), duplicate))
            cur.execute("UPDATE budget.receipt_evidence SET expense_pk=%s,is_primary_source=FALSE,raw_payload=COALESCE(raw_payload,'{}'::jsonb)||jsonb_build_object('duplicate_merge',%s::jsonb),updated_at=NOW() WHERE expense_pk=%s", (keeper, Jsonb(marker), duplicate))
            cur.execute('UPDATE budget.receipt_duplicate_links SET canonical_expense_pk=%s,updated_at=NOW() WHERE canonical_expense_pk=%s', (keeper, duplicate))
            from dataclasses import replace
            from .duplicate_review import fingerprint
            from .deduplication import ReceiptDuplicateScorer, DuplicateDisposition
            from .deduplication_store import PostgresReceiptDuplicateStore
            store = PostgresReceiptDuplicateStore(conn, commit_on_write=False)
            for left in (e for e in before['evidence'] if e['expense_pk'] == keeper):
                for right in (e for e in before['evidence'] if e['expense_pk'] == duplicate):
                    score = ReceiptDuplicateScorer().score(fingerprint(left), fingerprint(right))
                    if score.disposition is DuplicateDisposition.DISTINCT:
                        score = replace(score, disposition=DuplicateDisposition.REVIEW,
                                        reasons=(*score.reasons,'human_confirmed_duplicate_merge'))
                    store.confirm_link(score, keeper)

            after = load(cur, keeper, duplicate)
            source = next(e for e in before['evidence'] if e['expense_pk'] == duplicate)
            payload = {'event':'receipt_expenses_merged', 'actor_user':actor, 'reason':reason.strip(),
                       'keeper':keeper, 'duplicate':duplicate, 'selection':selection, 'before':before, 'after':after}
            cur.execute('INSERT INTO lineage.receipt_events(id,source_sha256,source_reference,payload) VALUES (%s,%s,%s,%s)',
                        (audit_id,source['source_sha256'],source['source_reference'],Jsonb(json.loads(json.dumps(payload,default=str)))))
        conn.commit()
        return {'apply':True, 'expense_pk':keeper, 'superseded_expense_pk':duplicate, 'audit_id':audit_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
