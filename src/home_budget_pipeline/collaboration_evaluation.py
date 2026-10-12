"""Read-only evaluation of the production receipt collaboration decision path."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os

from . import receipt_collaboration as collaboration
from . import receipt_shared_evidence as shared
from .receipt_model_providers import Profile
from .brave_query_cache import ACTIVE_CACHE
from .config import runtime_paths


class SharedSearchSnapshot:
    """Freeze raw results per query, without sharing either model's candidate pool."""
    def __init__(self):
        self.results = {}
        self.timestamps = {}
        self.errors = {}
        self.failures = {}
    def get(self, query, fetch):
        if query in self.failures:
            raise self.failures[query]
        if query not in self.results:
            try:
                self.results[query] = fetch()
            except Exception as exc:
                self.failures[query] = exc
                self.errors[query] = {'error_type': type(exc).__name__,
                                      'http_status': getattr(exc, 'code', None)}
                raise
            self.timestamps[query] = datetime.now(timezone.utc).isoformat()
        return deepcopy(self.results[query])


def load_inputs(cases, dsn):
    import psycopg
    from psycopg.rows import dict_row
    if not dsn:
        raise ValueError('missing_database_configuration')
    inputs = {}
    paths = runtime_paths()
    os.environ.setdefault('HOME_BUDGET_RECEIPTS_ROOT', str(paths.scanned_inbox))
    os.environ.setdefault('HOME_BUDGET_OCR_CACHE', str(paths.ocr_cache))
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for case in cases:
            # Explicit filename + printed/OCR aliases selects real evidence. Never
            # manufacture an OCR observation or an arithmetic "pass" from labels.
            rows = conn.execute('''SELECT i.id,i.item_name,i.unit_qty,i.unit_cost,i.line_total,
                e.id AS receipt_id,e.store_name,e.receipt_filename,e.source_reference,
                e.receipt_item_subtotal,e.receipt_gst,e.receipt_pst,e.receipt_discount_total,
                e.expense_total,e.total_recon_diff
                FROM budget.expense_items i JOIN budget.expenses e ON e.id=i.expense_pk
                WHERE (e.receipt_filename=%s OR e.source_reference LIKE %s)
                  AND i.item_name=ANY(%s) AND e.store_name ILIKE %s
                ORDER BY i.id LIMIT 1''', (case['receipt_filename'], '%/' + case['receipt_filename'],
                    case.get('item_names', [case['receipt_item']]), '%' + case['merchant'] + '%')).fetchall()
            if not rows:
                raise ValueError('receipt_item_not_found_in_local_database')
            row = rows[0]
            inputs[case['id']] = {'row': row, 'bundle': shared.load(conn, row)}
        conn.rollback()
    return inputs


def run(cases, models, api_key, threshold, inputs, context_tokens=32768, output_tokens=8192):
    results, evidence = [], []
    for case in cases:
        source = inputs[case['id']]
        snapshot = SharedSearchSnapshot()
        token = ACTIVE_CACHE.set(snapshot)
        try:
            for model in models:
                profile = Profile('evaluation', 'qwen', model, context_tokens, output_tokens, vision=True)
                try:
                    payload = collaboration.collaborate_item(deepcopy(source['row']), deepcopy(source['bundle']),
                        [profile], api_key, threshold, {'case_id': case['id'], 'model': model,
                            'receipt': source['row'].get('receipt_filename') or case.get('receipt_filename') or case['id'],
                            'item_id': source['row'].get('id'), 'receipt_id': source['row'].get('receipt_id')})
                    decision = payload['decision']
                    incomplete = bool(payload.get('blocking_errors') or payload.get('image_errors') or not payload.get('images'))
                    calls = [*payload.get('expansions', []), *payload.get('descriptor_expansions', []),
                             *payload.get('proposals', []), *payload.get('reviews', [])]
                    queries = []
                    for call in calls:
                        queries.extend(call.get('output', {}).get('queries', []))
                    row = {'case_id': case['id'], 'model': model,
                           'status': 'error' if incomplete else 'matched' if decision.get('candidate_url') else 'no_match',
                           'candidate_title': decision.get('candidate_title'),
                           'candidate_url': decision.get('candidate_url'), 'confidence': decision.get('confidence'),
                           'accepted': decision['disposition'] == 'recommended',
                           'seconds': payload['item_seconds'], 'queries': list(dict.fromkeys(queries)),
                           'decision_disposition': decision['disposition'], 'decision_reasons': decision['reasons'],
                           'collaboration': payload}
                    if incomplete:
                        row['error_type'] = 'IncompleteCollaboration'
                except Exception as exc:
                    row = {'case_id': case['id'], 'model': model, 'status': 'error',
                           'error_type': type(exc).__name__, 'confidence': None, 'accepted': False}
                results.append(row)
        finally:
            ACTIVE_CACHE.reset(token)
        case_rows = [row for row in results if row['case_id'] == case['id']]
        image_sets = [row.get('collaboration', {}).get('images') for row in case_rows]
        images_identical = bool(image_sets[0]) and all(images == image_sets[0] for images in image_sets)
        if not images_identical:
            for row in case_rows:
                row.update(status='error', accepted=False)
                row.setdefault('error_type', 'InputImageEvidenceUnavailableOrChanged')
        evidence.append({'case_id': case['id'], 'input_snapshot': source,
                         'input_snapshot_sha256': hashlib.sha256(json.dumps(source, default=str, sort_keys=True).encode()).hexdigest(),
                         'images_identical': images_identical,
                         'query_results': snapshot.results, 'query_fetched_at': snapshot.timestamps,
                         'query_errors': snapshot.errors,
                         'learned_product_priors': 'excluded to avoid leaking previously accepted identities'})
    return results, evidence
