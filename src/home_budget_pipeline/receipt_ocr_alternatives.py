"""Conservative, ordered associations between canonical rows and OCR readings."""
from __future__ import annotations

from difflib import SequenceMatcher
import re

from .receipts.ingest import MONEY_RE, _line_key, _ocr_lines_match


def description(text):
    return re.sub(r"\s+(?:BC|NC|TP|C)\s*$", "", MONEY_RE.sub("", text)).strip()


def align(left, right):
    """Return only unique matches across optimal monotonic alignments.

    Missing duplicate purchases are ambiguous, so neither row receives that
    reading. Line numbers alone never establish identity across OCR passes.
    """
    n, m = len(left), len(right)
    weights = [[0.0] * m for _ in range(n)]
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            if _ocr_lines_match(a, b):
                weights[i][j] = 1 + SequenceMatcher(
                    None, _line_key(description(a)), _line_key(description(b))).ratio()
    forward = [[0.0] * (m + 1) for _ in range(n + 1)]
    backward = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            forward[i + 1][j + 1] = max(forward[i][j + 1], forward[i + 1][j],
                                        forward[i][j] + weights[i][j])
    for i in reversed(range(n)):
        for j in reversed(range(m)):
            backward[i][j] = max(backward[i + 1][j], backward[i][j + 1],
                                  backward[i + 1][j + 1] + weights[i][j])
    possible = [(i, j) for i in range(n) for j in range(m) if weights[i][j] and
                abs(forward[i][j] + weights[i][j] + backward[i + 1][j + 1]
                    - forward[n][m]) < 1e-8]
    return {i: j for i, j in possible
            if sum(a == i for a, _ in possible) == 1 and
            sum(b == j for _, b in possible) == 1}


def alternatives(items, documents, passes):
    """Keep all distinct aligned spellings and every supporting observation."""
    result = {item['id']: [{'text': item['item_name'], 'sources': []}] for item in items}
    item_lines = [f"{item['item_name']} ${item['line_total']}" if item.get('line_total') is not None
                  else item['item_name'] for item in items]
    for document in documents:
        consensus = (document['raw_text'] or '').splitlines()
        item_rows = align(item_lines, consensus)
        for p in passes:
            if p['evidence_id'] != document['id'] or p['status'] != 'success' or not p.get('extracted_text'):
                continue
            # Combined text may span pages; align against the entire ordered
            # document. Ambiguous cross-page matches are excluded by align().
            lines = p['extracted_text'].splitlines()
            pass_rows = align(consensus, lines)
            for item_index, consensus_index in item_rows.items():
                if consensus_index not in pass_rows:
                    continue
                line_index = pass_rows[consensus_index]
                text = description(lines[line_index])
                if not text:
                    continue
                readings = result[items[item_index]['id']]
                reading = next((entry for entry in readings if entry['text'] == text), None)
                if reading is None:
                    reading = {'text': text, 'sources': []}
                    readings.append(reading)
                observation = next((entry for entry in (p.get('provenance') or {}).get('lines', [])
                                    if entry.get('line_number') == line_index + 1
                                    and entry.get('text') == lines[line_index]), {})
                reading['sources'].append({
                    'run_uuid': str(p['run_uuid']), 'pass_id': p['pass_id'],
                    'evidence_id': document['id'], 'page_number': p['page_number'],
                    'line_number': line_index + 1, 'text': lines[line_index],
                    'engine': p['engine'], 'variant': p['variant'],
                    'confidence': observation.get('confidence'),
                    'geometry': {key: observation.get(key) for key in ('x', 'y', 'width', 'height')},
                    'association_method': 'unique_ordered_text_and_price_alignment',
                })
    return result


def load(conn, receipt_id):
    from .receipt_shared_evidence import recover_pass_text
    items = conn.execute('''SELECT id,item_name,line_total FROM budget.expense_items
        WHERE expense_pk=%s ORDER BY id''', (receipt_id,)).fetchall()
    documents = conn.execute('''SELECT id,source_reference,source_sha256,raw_text
        FROM budget.receipt_evidence WHERE expense_pk=%s ORDER BY id''', (receipt_id,)).fetchall()
    passes = conn.execute('''SELECT p.*,r.evidence_id FROM budget.receipt_ocr_passes p
        JOIN budget.receipt_ocr_runs r USING (run_uuid)
        JOIN budget.receipt_evidence e ON e.id=r.evidence_id
        WHERE e.expense_pk=%s AND r.source_sha256=e.source_sha256
          AND r.run_uuid=(SELECT latest.run_uuid FROM budget.receipt_ocr_runs latest
            WHERE latest.evidence_id=e.id ORDER BY latest.processed_at DESC,latest.run_uuid DESC LIMIT 1)
        ORDER BY p.page_number,p.pass_id''', (receipt_id,)).fetchall()
    errors = recover_pass_text(conn, documents, passes)
    return alternatives(items, documents, passes), errors
