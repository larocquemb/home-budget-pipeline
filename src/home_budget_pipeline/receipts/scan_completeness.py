"""Flag unverified scan-page joins; compare gaps without filling missing evidence."""
import re
from difflib import SequenceMatcher

from ..receipt_ocr_alternatives import unique_ordered_matches
from .item_evidence import product_name
from .ingest import normalize_line

POLICY = 'scan-page-continuity-v1'


def page_rows(pages):
    result=[]
    for page,text in enumerate(pages,1):
        for line,raw in enumerate(text.splitlines(),1):
            text=normalize_line(raw)
            if re.match(r'^\W*(?:sub\s*total|total|tax\s+summary)\b',text,re.I):
                break
            name=product_name(text)
            if name:
                result.append({'name':name,'page_number':page,'line_number':line})
    return result


def ordered_pairs(left,right):
    def similarity(a,b):
        return SequenceMatcher(None,re.sub('[^a-z0-9]','',a.lower()),
                                    re.sub('[^a-z0-9]','',b.lower())).ratio()
    weights=[[1+score if (score:=similarity(a,b))>=.72 else 0 for b in right] for a in left]
    return unique_ordered_matches(weights)


def boundary_overlap(left,right):
    pairs=ordered_pairs([r['name'] for r in left[-16:]],[r['name'] for r in right[:16]])
    return len(pairs)>=2 and len({right[j]['name'].lower() for j in pairs.values()})>=2


def apply(receipt,pages,observations):
    if len(pages)<2 or receipt.source_reference.startswith('electronic/'):
        return
    from .allocation import assess
    assessment=assess([{'line_total':i.line_total} for i in receipt.items],
                     receipt.subtotal,receipt.tax,receipt.total)
    rows=page_rows(pages)
    joins=[]
    for number in range(1,len(pages)):
        left=[r for r in rows if r['page_number']==number]
        right=[r for r in rows if r['page_number']==number+1]
        if not left or not right:
            continue
        # A completed summary followed by another document is not a scan join.
        if re.search(r'\bSUB\s*TOTAL\b',pages[number-1],re.I):
            continue
        overlap=boundary_overlap(left,right)
        joins.append({'left_page':number,'right_page':number+1,'overlap_detected':overlap,
                      'left_items':left[-4:],'right_items':right[:4]})
    suspect=not assessment['reconciled'] and any(not j['overlap_detected'] for j in joins)
    candidates=[]
    for p in observations:
        if p.get('page_number') in {j[k] for j in joins for k in ('left_page','right_page')}:
            candidates.append({'value':'page_continuity_observation','source':{k:p.get(k) for k in
                ('run_uuid','pass_id','page_number','engine')}})
    receipt.field_decisions['scan_completeness']={
        'policy':POLICY,'status':'conflict' if suspect else 'selected',
        'value':'possible_page_gap' if suspect else 'overlap_observed' if joins and all(
            j['overlap_detected'] for j in joins) else 'financially_reconciled' if assessment['reconciled'] else 'unverified',
        'reason':'A missing overlap plus failed item reconciliation is a scan-gap warning, not proof of absent pixels.',
        'joins':joins,'candidates':candidates}


def compare(reference,current_pages,current_items=()):
    """Locate reference gaps bracketed by observed page-boundary anchors."""
    current=page_rows(current_pages)
    pairs=ordered_pairs([i['item_name'] for i in reference],[r['name'] for r in current])
    reverse={j:i for i,j in pairs.items()}
    gaps=[]
    for page in sorted({r['page_number'] for r in current}):
        left=[j for j in reverse if current[j]['page_number']==page]
        right=[j for j in reverse if current[j]['page_number']==page+1]
        if not left or not right:
            continue
        first,last=reverse[max(left)],reverse[min(right)]
        if last>first+1:
            missing=reference[first+1:last]
            gaps.append({'left_page':page,'right_page':page+1,
                         'missing_reference_items':[dict(item) for item in missing],
                         'left_anchor':current[max(left)]['name'],
                         'right_anchor':current[min(right)]['name']})
    boundaries_available=len(current_pages)>1
    if not boundaries_available:
        # Legacy messages dropped page_text. Identify only gaps between item
        # anchors; do not invent page numbers from the flattened document.
        ordered=sorted(reverse)
        for a,b in zip(ordered,ordered[1:]):
            left,right=reverse[a],reverse[b]
            if right-left-1 > b-a-1:
                gaps.append({'left_page':None,'right_page':None,
                    'missing_reference_items':[dict(item) for item in reference[left+1:right]],
                    'left_anchor':current[a]['name'],'right_anchor':current[b]['name']})
    from decimal import Decimal
    item_pairs=ordered_pairs([i['item_name'] for i in reference],
                             [i['item_name'] for i in current_items])
    differences=[]
    for i,j in item_pairs.items():
        a,b=reference[i].get('line_total'),current_items[j].get('line_total')
        if a is not None and b is not None and Decimal(str(a))!=Decimal(str(b)):
            differences.append({'item_name':reference[i]['item_name'],
                                'reference_value':str(a),'current_value':str(b)})
    return {'matched_rows':len(pairs),'reference_rows':len(reference),
            'page_boundaries_available':boundaries_available,
            'coverage':len(pairs)/len(reference) if reference else 0,'page_gaps':gaps,
            'price_differences':differences,
            'note':'Reference rows are not copied into this receipt. Missing readings can indicate cropping or OCR loss.'}
