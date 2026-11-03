"""Corroborate a printed brand and address-anchored branch without a retailer list."""
import re
from collections import defaultdict
from difflib import SequenceMatcher


def resolve(observations):
    from .ingest import _store_header_lines, _store_street_line, _line_key, _looks_like_tesseract_tsv
    logos,branches=[],[]
    for p in observations or ():
        if (p.get('page_number')!=1 or p.get('status') not in (None,'success')
                or not p.get('run_uuid') or not p.get('engine')):
            continue
        text=p.get('text') or p.get('extracted_text') or ''
        header=_store_header_lines(text)
        if not header or _looks_like_tesseract_tsv('\n'.join(header)):
            continue
        source={k:p.get(k) for k in ('run_uuid','pass_id','page_number','engine')}
        address_index=next((i for i,line in enumerate(header) if _store_street_line(line)),len(header))
        for n,logo in enumerate(header[:min(address_index,5)]):
            logo=logo.strip(' .:;|\"\'')
            if (re.fullmatch(r"[A-Za-z]+(?:[ '&’-]+[A-Za-z]+){0,3}",logo)
                    and len(_line_key(logo))>=4):
                logos.append({'value':logo,'words':re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?",logo),
                    'source':{**source,'line_number':n+1}})
        for index,line in enumerate(header):
            if index and _store_street_line(line):
                label=header[index-1].strip(' .:;|\"\'')
                words=re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?",label)
                if (2<=len(words)<=8 and not re.search(r'\d',label)
                        and not re.search(r'\b(?:phone|fax|tel|customer|member|bill|ship)\b',label,re.I)):
                    branches.append({'value':label,'words':words,
                        'source':{**source,'line_number':index}})
    supported=defaultdict(list)
    for logo in logos:
        for branch in branches:
            if logo['source']['run_uuid']!=branch['source']['run_uuid']:
                continue
            words=branch['words']
            if (len(logo['words'])>=len(words) or
                    [w.lower() for w in logo['words']]!=[w.lower() for w in words[:len(logo['words'])]]):
                continue
            # The spelling must occur in two distinct printed roles. Another
            # engine must corroborate the complete branch header on this run.
            key=_line_key(branch['value'])
            independent=[]
            for other in branches:
                a,b=re.sub('[^a-z]','',key),re.sub('[^a-z]','',_line_key(other['value']))
                changes=sum(max(i2-i1,j2-j1) for tag,i1,i2,j1,j2 in SequenceMatcher(None,a,b).get_opcodes() if tag!='equal')
                if (changes<=1 and other['source']['engine']!=branch['source']['engine']
                        and other['source']['run_uuid']==branch['source']['run_uuid']):
                    independent.append(other)
            if independent:
                supported[(_line_key(logo['value']),key)].append((logo,branch,independent))
    if len(supported)!=1:
        return {}
    logo,branch,independent=next(iter(supported.values()))[0]
    candidates=[{k:r[k] for k in ('value','source')} for r in [logo,branch,*independent]]
    return {field:{'policy':'repeated-brand-and-branch-v1','status':'selected','value':value,
                   'reason':'printed_logo_and_address_anchored_branch_with_independent_engine',
                   'candidates':candidates}
            for field,value in [('merchant',logo['value']),('store',branch['value'])]}
