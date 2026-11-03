"""Optional printed store contact and tax registration evidence."""
import re
from collections import defaultdict

PHONE = re.compile(r'(?<!\d)(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-]+([2-9]\d{2})[ .-]+(\d{4})(?!\d)')
RT = re.compile(r'(?<!\d)(\d{3}[ -]?\d{3}[ -]?\d{3})\s*[- ]?RT\s*(\d{4})(?!\d)', re.I)


def phone_evidence(text, observations=()):
    from .ingest import _store_header_lines, _looks_like_tesseract_tsv
    candidates=[]
    readings=[(text,{'kind':'selected_text'})]
    for p in observations or ():
        if p.get('page_number')!=1 or p.get('status') not in (None,'success'):
            continue
        reading=p.get('text') or p.get('extracted_text') or ''
        if reading and not _looks_like_tesseract_tsv(reading):
            readings.append((reading,{k:p.get(k) for k in ('run_uuid','pass_id','page_number','engine')}))
    support=defaultdict(set)
    for reading,source in readings:
        for number,line in enumerate(_store_header_lines(reading),1):
            if re.search(r'\b(?:fax|customer|member|loyalty|order|invoice)\b',line,re.I):
                continue
            for match in PHONE.finditer(line):
                if not (PHONE.fullmatch(line.strip()) or re.match(r'^(?:phone|telephone|tel)\b',line,re.I)):
                    continue
                value='-'.join(match.groups())
                candidates.append({'value':value,'source':{**source,'line_number':number}})
                if source.get('run_uuid') and source.get('engine'):
                    support[(source['run_uuid'],source['page_number'],value)].add(source['engine'])
    values={c['value'] for c in candidates}
    corroborated={value for (_,_,value),engines in support.items() if len(engines)>=2}
    value=next(iter(values)) if len(values)==1 else next(iter(corroborated)) if len(corroborated)==1 else None
    return {'policy':'printed-header-phone-v1','value':value,
            'status':'selected' if value is not None or not values else 'conflict',
            'reason':'independent_engines_agree' if len(values)>1 and value else 'unambiguous_printed_phone' if value else 'missing_or_conflicting_phone',
            'candidates':candidates}


def extract_store_contact(text, *, observations=()):
    from .ingest import _store_header_readings, _store_header_lines
    registrations = set()
    for reading in _store_header_readings(text, observations):
        for match in RT.finditer(reading):
            registrations.add(re.sub(r'\D', '', match[1]) + 'RT' + match[2])
    return {'store_phone': phone_evidence(text,observations)['value'],
            'tax_registration_number': next(iter(registrations)) if len(registrations) == 1 else None}
