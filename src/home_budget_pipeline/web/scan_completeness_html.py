"""Describe incomplete scan evidence without inventing missing purchase rows."""
from .render import esc


def render(receipt,base):
    evidence=receipt.get('evidence') or {}
    decision=((evidence.get('raw_payload') or {}).get('field_decisions') or {}).get('scan_completeness') or {}
    comparison=receipt.get('source_comparison') or {}
    gaps=comparison.get('page_gaps') or []
    body=''
    if decision.get('value')=='possible_page_gap' or gaps:
        body+='<div class="card"><strong>Possible missing scan content</strong><p>The page join could not be verified. Items may be outside the scan or unreadable. Re-scan with overlapping pages, or inspect a reconciled copy.</p></div>'
    if comparison:
        pk=comparison['reference_expense_pk']
        link=f'{base}/receipts/{esc(comparison["reference_sha256"])}'
        body+=f'<div class="card"><strong>Source comparison</strong><p><a href="{link}">Reconciled copy · Expense {esc(pk)}</a> · {esc(comparison["matched_rows"])} of {esc(comparison["reference_rows"])} reference rows matched.</p>'
        for gap in gaps:
            names=', '.join(item['item_name'] for item in gap['missing_reference_items'])
            location=(f'Between pages {esc(gap["left_page"])} and {esc(gap["right_page"])}'
                      if gap['left_page'] is not None else
                      f'Between {esc(gap.get("left_anchor"))} and {esc(gap.get("right_anchor"))}')
            body+=f'<p>{location}, the reference has additional rows: {esc(names)}.</p>'
        if comparison.get('page_boundaries_available') is False:
            body+='<p>Stored page boundaries are unavailable for this older extraction. Refresh its cached extraction to retain them.</p>'
        if not gaps:
            body+='<p>No missing block was identified between matched page-boundary anchors. Check OCR prices and repeated page content.</p>'
        differences=comparison.get('price_differences') or []
        if differences:
            body+='<details><summary>Different extracted prices — inspect the images</summary><table><thead><tr><th>Item</th><th>This copy</th><th>Reconciled copy</th></tr></thead><tbody>'
            for row in differences:
                body+=f'<tr><td>{esc(row["item_name"])}</td><td>{esc(row["current_value"])}</td><td>{esc(row["reference_value"])}</td></tr>'
            body+='</tbody></table></details>'
        body+='<p>Reference items are shown for comparison and are not added to this receipt.</p></div>'
    return body
