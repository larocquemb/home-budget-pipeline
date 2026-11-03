"""Corroborate coherent printed financial headers across original OCR engines."""
import json
from collections import defaultdict
from decimal import Decimal

from . import ingest

POLICY = 'coherent-printed-header-v1'


def apply(receipt, observations):
    candidates=[]
    support=defaultdict(set)
    for p in observations:
        text=p.get('text') or p.get('extracted_text') or ''
        if (p.get('status') not in (None,'success') or ingest._looks_like_tesseract_tsv(text)
                or not p.get('run_uuid') or p.get('page_number') is None):
            continue
        subtotal,tax,total=ingest.extract_totals(text,infer_tax=False)
        if any(v is None for v in (subtotal,tax,total)):
            continue
        values=tuple(Decimal(str(v)) for v in (subtotal,tax,total))
        coherent=abs(values[0]+values[1]-values[2])<=Decimal('.01')
        candidate={'value':json.dumps(dict(zip(('subtotal','tax','total'),map(str,values)))),
                   'coherent':coherent,'source':{k:p.get(k) for k in
                    ('run_uuid','pass_id','page_number','engine','variant')}}
        candidates.append(candidate)
        if coherent and p.get('engine'):
            support[(p['run_uuid'],p['page_number'],values)].add(p['engine'])
    supported={values for (_,_,values),engines in support.items() if len(engines)>=2}
    if len(supported)!=1:
        return
    values=next(iter(supported))
    receipt.subtotal,receipt.tax,receipt.total=map(float,values)
    receipt.field_decisions['financial_header']={
        'policy':POLICY,'status':'selected','value':json.dumps(dict(zip(
            ('subtotal','tax','total'),map(str,values)))),
        'reason':'coherent_printed_header_agrees_across_engines','candidates':candidates}
