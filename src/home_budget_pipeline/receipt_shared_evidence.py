"""Read immutable OCR evidence and bounded images for one canonical receipt item."""
from __future__ import annotations

import base64
from difflib import SequenceMatcher
from decimal import Decimal
import hashlib
import io
import os
from pathlib import Path

from . import product_enrichment as core


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
                              'provenance': p.get('provenance') or {}})
        for line in lines:
            sources.append({'id': f"{pass_id}:line:{line['line_number']}", 'kind': 'ocr_pass',
                'engine': p['engine'], 'engine_type': p['engine_type'], 'ocr_run_uuid': str(p['run_uuid']),
                'pass_id': p['pass_id'], 'evidence_id': p['evidence_id'], 'page_number': p['page_number'],
                'variant': p['variant'], **line})
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
        'ocr_passes': pass_summaries, 'geometry': matched_geometry[:12], 'validations': validations,
        'coverage': {'available_documents': len(documents), 'included_documents': min(4, len(documents)),
                     'available_passes': int(passes[0]['available_passes']) if passes else 0,
                     'included_passes': len(passes), 'retrieved_item_sources': len(sources)},
        'receipt_totals': {key: str(row[key]) if row.get(key) is not None else None for key in
            ('receipt_item_subtotal', 'receipt_gst', 'receipt_pst', 'receipt_discount_total', 'expense_total', 'total_recon_diff')}}


def render_images(bundle: dict) -> tuple[list[dict], list[dict]]:
    """Read only hash-verified documents beneath the configured receipt root."""
    from PIL import Image
    images, errors = [], []
    root = Path(os.getenv('HOME_BUDGET_RECEIPTS_ROOT', '/data/receipts/raw/scanned/inbox')).resolve()
    for document in bundle['documents']:
        if len(images) >= 2:
            break
        try:
            path = (root / document['source_reference']).resolve()
            if not path.is_relative_to(root) or path.stat().st_size > 20 * 1024 ** 2:
                raise ValueError('Source outside receipt root or too large')
            with path.open('rb') as source:
                source_bytes = source.read(20 * 1024 ** 2 + 1)
            if len(source_bytes) > 20 * 1024 ** 2 or hashlib.sha256(source_bytes).hexdigest() != document['source_sha256']:
                raise ValueError('Source hash mismatch')
            page_numbers = sorted({g['page_number'] for g in bundle['geometry'] if g['evidence_id'] == document['id']}) or [1]
            if path.suffix.lower() == '.pdf':
                import pypdfium2 as pdfium
                pdf = pdfium.PdfDocument(source_bytes)
                try:
                    for number in page_numbers[:2 - len(images)]:
                        page = pdf[number - 1]
                        scale = min(2, 2048 / max(page.get_size()))
                        bitmap = page.render(scale=scale)
                        try:
                            image = bitmap.to_pil().copy()
                        finally:
                            bitmap.close()
                            page.close()
                        images.append(encode_image(image, document['id'], number))
                finally:
                    pdf.close()
            elif path.suffix.lower() in {'.png', '.jpg', '.jpeg'}:
                with Image.open(io.BytesIO(source_bytes)) as image:
                    images.append(encode_image(image, document['id'], 1))
            else:
                raise ValueError('Unsupported receipt image format')
        except Exception as exc:
            errors.append({'evidence_id': document['id'], 'error_type': type(exc).__name__})
    return images, errors


def encode_image(image, evidence_id: int, page: int) -> dict:
    image.thumbnail((2048, 2048))
    image = image.convert('RGB')
    output = io.BytesIO()
    image.save(output, format='PNG')
    data = output.getvalue()
    return {'id': f'image:{evidence_id}:{page}', 'data': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest(), 'page_number': page, 'evidence_id': evidence_id}
