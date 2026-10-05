"""Read immutable OCR evidence and bounded images for one canonical receipt item."""
from __future__ import annotations

import base64
from difflib import SequenceMatcher
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import product_enrichment as core

MAX_CACHE_BYTES = 20 * 1024 ** 2


class CacheEvidenceError(ValueError):
    pass


def verified_pass_text(artifact, document, passes):
    """Read only the recorded current-run artifact beneath the cache root."""
    prefix = os.getenv('OCR_ARTIFACT_URI_PREFIX', 'pvc://receipt-ocr').rstrip('/')
    uri = artifact['uri']
    if urlsplit(prefix).scheme != 'pvc' or not uri.startswith(prefix + '/'):
        raise CacheEvidenceError('unsupported_artifact_uri')
    suffix = unquote(uri[len(prefix) + 1:])
    root = Path(os.getenv('HOME_BUDGET_OCR_CACHE', '/data/receipts/derived/ocr-cache')).resolve()
    path = (root / suffix).resolve()
    if not path.is_relative_to(root) or urlsplit(uri).query or urlsplit(uri).fragment:
        raise CacheEvidenceError('outside_cache_root')
    size = artifact['size_bytes']
    if not isinstance(size, int) or not 0 < size <= MAX_CACHE_BYTES:
        raise CacheEvidenceError('cache_size_invalid')
    with path.open('rb') as source:
        if os.fstat(source.fileno()).st_size != size:
            raise CacheEvidenceError('cache_size_mismatch')
        data = source.read(MAX_CACHE_BYTES + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != artifact['sha256']:
        raise CacheEvidenceError('cache_hash_mismatch')
    metadata = json.loads(data)['metadata']
    if str(metadata.get('run_uuid')) != str(artifact['run_uuid']):
        raise CacheEvidenceError('cache_run_mismatch')
    if metadata.get('source_sha256') != document['source_sha256'] or metadata.get('source_reference') != document['source_reference']:
        raise CacheEvidenceError('cache_source_mismatch')
    cached = metadata['ocr_passes']
    by_id = {entry['pass_id']: entry for entry in cached}
    if len(by_id) != len(cached):
        raise CacheEvidenceError('duplicate_cache_pass')
    recovered = {}
    for p in passes:
        if str(p['run_uuid']) != str(artifact['run_uuid']) or p['status'] != 'success' or p.get('extracted_text') is not None:
            continue
        entry = by_id.get(p['pass_id'])
        if not entry or str(entry.get('run_uuid')) != str(p['run_uuid']) or any(entry.get(key) != p.get(key) for key in ('engine', 'status', 'page_number', 'variant')):
            raise CacheEvidenceError('cache_pass_mismatch')
        if not isinstance(entry.get('text'), str):
            raise CacheEvidenceError('cache_pass_text_missing')
        recovered[p['pass_id']] = entry['text']
    return recovered


def recover_pass_text(conn, documents, passes):
    """Hydrate missing pass text without mutating PostgreSQL or using stale runs."""
    run_ids = sorted({str(p['run_uuid']) for p in passes
                      if p['status'] == 'success' and p.get('extracted_text') is None and p.get('variant') != 'geometry'})
    if not run_ids:
        return []
    artifacts = conn.execute('''SELECT run_uuid,uri,sha256,size_bytes FROM budget.receipt_ocr_artifacts
        WHERE kind='ocr-cache' AND run_uuid=ANY(%s::uuid[]) ORDER BY created_at DESC,id DESC''', (run_ids,)).fetchall()
    errors, found = [], set()
    by_document = {d['id']: d for d in documents}
    for artifact in artifacts:
        run = str(artifact['run_uuid'])
        if run in found:
            continue
        found.add(run)
        selected = [p for p in passes if str(p['run_uuid']) == run and p.get('variant') != 'geometry']
        try:
            text = verified_pass_text(artifact, by_document[selected[0]['evidence_id']], selected)
            for p in selected:
                if p['pass_id'] in text:
                    p['extracted_text'] = text[p['pass_id']]
                    p['text_artifact'] = {key: artifact[key] for key in ('uri', 'sha256')}
        except Exception as exc:
            errors.append({'run_uuid': run, 'reason': str(exc) if isinstance(exc, CacheEvidenceError) else 'cache_read_failed',
                           'error_type': type(exc).__name__})
    errors.extend({'run_uuid': run, 'reason': 'cache_artifact_missing'} for run in run_ids if run not in found)
    return errors


def relevant_lines(text: str, item: str, domain: str, limit=3) -> list[dict]:
    """Retrieve probable item lines, retaining ambiguity instead of claiming identity."""
    ranked = []
    normalized = core.normalized_cache_item_name(item)
    for index, line in enumerate((text or '').splitlines()):
        score = max(SequenceMatcher(None, normalized, core.normalized_cache_item_name(line)).ratio(),
            core.candidate_score(item, domain, line, f'https://{domain}/products/evidence') if domain == 'sobeys.com' else 0)
        if score >= .5:
            ranked.append({'line_number': index + 1, 'text': line[:500], 'retrieval_score': round(score, 4)})
    return sorted(ranked, key=lambda line: (-line['retrieval_score'], line['line_number']))[:limit]


def load(conn, row: dict) -> dict:
    """Use latest OCR run per source; never combine stale passes with current ones."""
    domain = core.retailer_domain(row['store_name'])
    documents = conn.execute('''SELECT id, source_reference, source_sha256, raw_text
        FROM budget.receipt_evidence WHERE expense_pk=%s ORDER BY is_primary_source DESC, id DESC''',
        (row['receipt_id'],)).fetchall()
    passes = conn.execute('''SELECT p.*, r.evidence_id, count(*) OVER () AS available_passes
        FROM budget.receipt_ocr_passes p JOIN budget.receipt_ocr_runs r USING (run_uuid)
        JOIN budget.receipt_evidence e ON e.id=r.evidence_id
        WHERE e.expense_pk=%s AND r.source_sha256=e.source_sha256 AND r.run_uuid=(
            SELECT latest.run_uuid FROM budget.receipt_ocr_runs latest
            WHERE latest.evidence_id=e.id ORDER BY latest.processed_at DESC, latest.run_uuid DESC LIMIT 1)
        ORDER BY p.selected_base DESC, p.page_number, p.engine, p.pass_id LIMIT 80''',
        (row['receipt_id'],)).fetchall()
    geometry = conn.execute('''SELECT l.* FROM budget.receipt_ocr_lines l
        JOIN budget.receipt_evidence e ON e.id=l.evidence_id
        WHERE e.expense_pk=%s ORDER BY l.evidence_id DESC,l.page_number,l.line_number LIMIT 1000''',
        (row['receipt_id'],)).fetchall()
    artifact_errors = recover_pass_text(conn, documents, passes)
    sources = [{'id': 'item:canonical', 'kind': 'canonical_item', 'text': row['item_name'],
        'unit_qty': str(row.get('unit_qty')) if row.get('unit_qty') is not None else None,
        'unit_cost': str(row.get('unit_cost')) if row.get('unit_cost') is not None else None,
        'line_total': str(row.get('line_total')) if row.get('line_total') is not None else None}]
    for document in documents[:4]:
        for line in relevant_lines(document['raw_text'], row['item_name'], domain):
            sources.append({'id': f"consensus:{document['id']}:{line['line_number']}",
                'kind': 'ocr_consensus', 'evidence_id': document['id'], **line})
    pass_summaries = []
    for p in passes:
        pass_id = f"pass:{p['run_uuid']}:{p['pass_id']}"
        lines = relevant_lines(p['extracted_text'], row['item_name'], domain) if p['status'] == 'success' else []
        pass_summaries.append({'id': pass_id, 'engine': p['engine'], 'status': p['status'],
                              'seconds': str(p['seconds']) if p['seconds'] is not None else None,
                              'matching_lines': len(lines), 'quality': p.get('quality') or {},
                              'engine_options': p.get('engine_options') or {}, 'usage': p.get('usage') or {},
                              'provenance': p.get('provenance') or {},
                              'text_available': p.get('extracted_text') is not None,
                              'text_artifact': p.get('text_artifact')})
        for line in lines:
            sources.append({'id': f"{pass_id}:line:{line['line_number']}", 'kind': 'ocr_pass',
                'engine': p['engine'], 'engine_type': p['engine_type'], 'ocr_run_uuid': str(p['run_uuid']),
                'pass_id': p['pass_id'], 'evidence_id': p['evidence_id'], 'page_number': p['page_number'],
                'variant': p['variant'], 'text_artifact': p.get('text_artifact'), **line})
    matched_geometry = []
    for line in geometry:
        if relevant_lines(line['text'], row['item_name'], domain, limit=1):
            source = {'id': f"layout:{line['evidence_id']}:{line['page_number']}:{line['line_number']}",
                'kind': 'ocr_layout', **{key: line[key] for key in (
                    'text', 'evidence_id', 'page_number', 'line_number', 'x', 'y', 'width', 'height')}}
            matched_geometry.append(source)
    sources.extend(matched_geometry[:12])
    validations = {'receipt_arithmetic': 'not_checkable', 'item_arithmetic': 'not_checkable'}
    if row.get('expense_total') is not None and row.get('receipt_item_subtotal') is not None and row.get('total_recon_diff') is not None:
        validations['receipt_arithmetic'] = 'pass' if abs(row['total_recon_diff']) <= Decimal('0.01') else 'fail'
    if all(row.get(key) is not None for key in ('unit_qty', 'unit_cost', 'line_total')):
        validations['item_arithmetic'] = 'pass' if abs(row['unit_qty'] * row['unit_cost'] - row['line_total']) <= Decimal('0.01') else 'fail'
    return {'sources': sources, 'documents': [{k: d[k] for k in ('id', 'source_reference', 'source_sha256')} for d in documents[:4]],
        'ocr_passes': pass_summaries, 'artifact_errors': artifact_errors, 'geometry': matched_geometry[:12], 'validations': validations,
        'coverage': {'available_documents': len(documents), 'included_documents': min(4, len(documents)),
                     'available_passes': int(passes[0]['available_passes']) if passes else 0,
                     'included_passes': len(passes), 'retrieved_item_sources': len(sources)},
        'receipt_totals': {key: str(row[key]) if row.get(key) is not None else None for key in
            ('receipt_item_subtotal', 'receipt_gst', 'receipt_pst', 'receipt_discount_total', 'expense_total', 'total_recon_diff')}}


MAX_SOURCE_BYTES = 128 * 1024 ** 2


class ReceiptImageError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def render_images(bundle: dict) -> tuple[list[dict], list[dict]]:
    """Stream hash-verified sources; send at most two bounded page images."""
    from PIL import Image
    images, errors = [], []
    root = Path(os.getenv('HOME_BUDGET_RECEIPTS_ROOT', '/data/receipts/raw/scanned/inbox')).resolve()
    for document in bundle['documents']:
        if len(images) >= 2:
            break
        try:
            path = (root / document['source_reference']).resolve()
            if not path.is_relative_to(root):
                raise ReceiptImageError('outside_receipt_root')
            with path.open('rb') as source:
                if os.fstat(source.fileno()).st_size > MAX_SOURCE_BYTES:
                    raise ReceiptImageError('source_too_large')
                digest = hashlib.sha256()
                size = 0
                while chunk := source.read(1024 ** 2):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise ReceiptImageError('source_too_large')
                    digest.update(chunk)
                if digest.hexdigest() != document['source_sha256']:
                    raise ReceiptImageError('source_hash_mismatch')
                source.seek(0)
                page_numbers = sorted({g['page_number'] for g in bundle['geometry'] if g['evidence_id'] == document['id']}) or [1]
                if path.suffix.lower() == '.pdf':
                    import pypdfium2 as pdfium
                    pdf = pdfium.PdfDocument(source)
                    try:
                        for number in page_numbers[:2 - len(images)]:
                            page = pdf[number - 1]
                            try:
                                scale = min(2, 2048 / max(page.get_size()))
                                bitmap = page.render(scale=scale)
                                try:
                                    image = bitmap.to_pil().copy()
                                finally:
                                    bitmap.close()
                            finally:
                                page.close()
                            images.append(encode_image(image, document['id'], number))
                    finally:
                        pdf.close()
                elif path.suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                    with Image.open(source) as image:
                        images.append(encode_image(image, document['id'], 1))
                else:
                    raise ReceiptImageError('unsupported_image_format')
        except Exception as exc:
            errors.append({'evidence_id': document['id'], 'error_type': type(exc).__name__,
                           'reason': getattr(exc, 'reason', 'image_render_failed')})
    return images, errors


def encode_image(image, evidence_id: int, page: int) -> dict:
    image.thumbnail((2048, 2048))
    image = image.convert('RGB')
    output = io.BytesIO()
    image.save(output, format='PNG')
    data = output.getvalue()
    return {'id': f'image:{evidence_id}:{page}', 'data': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest(), 'page_number': page, 'evidence_id': evidence_id}
