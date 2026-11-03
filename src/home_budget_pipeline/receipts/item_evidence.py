"""Select item amounts from ordered original OCR evidence, without balancing guesses."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
import re

from ..receipt_ocr_alternatives import align, unique_ordered_matches
from . import ingest as scan
from .departments import HEADING

POLICY = 'ordered-item-price-evidence-v1'
MIN_PRICE_CONFIDENCE = 90
PRICE_CONFIDENCE_MARGIN = 15
PRICE_ONLY = re.compile(r'-?\$?\s*\d[\d ,.]*-?\s*[A-Z]{0,2}', re.I)
ADJUSTMENT = re.compile(r'^(?:[I1lT]NSTANT\s+SAVINGS|\+?EHC)\b', re.I)


def product_name(line):
    if (re.fullmatch(r'[A-Za-z]{1,2}',line) or scan.item_metadata_line(line) or not re.search(r'[A-Za-z]{2}', line) or HEADING.fullmatch(line)
            or scan.TOTAL_WORDS.search(line) or scan.NON_ITEM_WORDS.search(line)
            or ADJUSTMENT.match(line)
            or re.match(r'^\d+(?:\.\d+)?\s*(?:kg|lb)\b', line, re.I)
            or re.search(r'\b(?:date|phone|tel|auth|reference|address)\b',line,re.I)):
        return None
    matches = list(scan.MONEY_RE.finditer(line))
    name = line[:matches[-1].start()] if matches else line
    name = scan.clean_extracted_item_name(name.strip(' :-'))
    return name if len(re.sub('[^A-Za-z]','',name)) >= 2 else None


def rows(text, *, placeholders=False, provenance=None, include_quantity=False):
    """Join only unambiguous monotonic name/amount associations.

    Adjacent columns may be emitted price-first or name-first. Maximum ordered
    matching preserves repeated products and refuses ambiguous missing prices.
    Original line numbers are retained even when the derived reading is joined.
    """
    lines = [scan.normalize_line(l) for l in text.splitlines()]
    names = [i for i,l in enumerate(lines) if (product_name(l) or ADJUSTMENT.match(l)) and scan.parse_money(l) is None]
    prices = [i for i,l in enumerate(lines) if PRICE_ONLY.fullmatch(l) and scan.parse_money(l) is not None
              and not (i and scan.parse_money(lines[i-1]) is None and (scan.TOTAL_WORDS.search(lines[i-1])
                              or re.search(r'\bsaved\b',lines[i-1],re.I)))]
    # Within a complete uninterrupted column block, monotonic order pairs
    # equal numbers of names and amounts, even if the engine emits two prices
    # before two names. Incomplete blocks keep the conservative local matching.
    block_pairs={}
    block=[]
    def close_block():
        ns=[k for k in block if k in names]
        ps=[k for k in block if k in prices]
        if ns and len(ns)==len(ps) and not (
                ps[0]<ns[0] and ps[0]>0 and HEADING.fullmatch(lines[ps[0]-1])
                and (len(ns)==1 or not all(p<n for n,p in zip(ns,ps)))):
            block_pairs.update(zip(ns,ps))
        block.clear()
    for k,line in enumerate(lines):
        if k in names or k in prices or re.fullmatch(r'[A-Z]{1,2}',line):
            block.append(k)
        else:
            close_block()
    close_block()
    directions={}
    boundaries=[-1]+[i for i,l in enumerate(lines) if HEADING.fullmatch(l)]+[len(lines)]
    for start,end in zip(boundaries,boundaries[1:]):
        signs={1 if j>i else -1 for i,j in block_pairs.items() if start<i<end}
        if len(signs)==1 and sum(start<i<end for i in block_pairs)>=2:
            for k in range(start+1,end):directions[k]=next(iter(signs))
    geometry={line.get('line_number'):line for line in (provenance or {}).get('lines',[])}
    def row_weight(i,j):
        name,price=geometry.get(i+1,{}),geometry.get(j+1,{})
        if all(isinstance(row.get(k),(float,int)) for row in (name,price)
               for k in ('x','y','width','height')):
            overlap=min(name['y']+name['height'],price['y']+price['height'])-max(name['y'],price['y'])
            if (min(name['height'],price['height'])>0 and
                    overlap/min(name['height'],price['height'])>=.6
                    and price['x']>=name['x']+name['width']-5):
                return 2+overlap/min(name['height'],price['height'])
            return 0
        if i in block_pairs:
            return int(block_pairs[i]==j)
        if j==i-1 and j>0 and HEADING.fullmatch(lines[j-1]):
            return 0  # A price after a section boundary may belong to the prior section.
        # Price columns may put tax markers between names and prices, or
        # separate the price and its tax marker.
        # Include adjustment labels in the same matching so their amounts cannot
        # be stolen by an adjacent product. Ambiguous optimal matches abstain.
        if directions.get(i) and (j-i)*directions[i]<0:
            return 0
        between=lines[min(i,j)+1:max(i,j)]
        return int(abs(i-j)<=3 and all(
            not l or re.fullmatch(r'[A-Z]{1,2}',l)
            for k,l in enumerate(between,min(i,j)+1)))
    pairs = unique_ordered_matches([[row_weight(i,j) for j in prices] for i in names])
    money_lines = {}
    for ni,pi in pairs.items():
        i,j=names[ni],prices[pi]
        lines[i] += ' ' + lines[j]
        lines[j] = ''
        if j+1<len(lines) and re.fullmatch(r'[A-Z]{1,2}',lines[j+1]):
            # A separate price-column tax marker is part of that priced row,
            # not a new unreadable product that breaks its discount link.
            lines[j+1] = ''
        money_lines[i] = j
    paired_prices={prices[pi] for pi in pairs.values()}
    for j in prices:
        if j not in paired_prices and any(abs(i-j)==1 for i in names):
            lines[j]=''  # Stop the ordinary wrapped parser borrowing an ambiguous price.
    items = scan.extract_items('\n'.join(lines))
    result, start = [], 0
    for item in items:
        key = scan._line_key(item.item_name)
        found = next((i for i in range(start,len(lines)) if
                      scan.parse_money(lines[i]) is not None
                      and scan._line_key(product_name(lines[i]) or '') == key),None)
        if found is None:
            continue
        result.append(dict(name=item.item_name,amount=Decimal(str(item.line_total)),
                           line_number=found+1,money_line_number=money_lines.get(found,found)+1,
                           currency='$' in lines[found],text=lines[found],gross=scan.parse_money(lines[found])))
        start=found+1
    weight_detail=re.compile(r'^(\d+(?:\.\d+)?)\s*(kg|lb)\s*@\s*\$?\s*(\d+\.\d{2})\s*/\s*\2\b',re.I)
    for row in result:
        following=row['line_number']
        match=weight_detail.match(lines[following]) if following<len(lines) else None
        if match:
            row['weight_detail']=(str(Decimal(match[1])),match[2].lower(),str(Decimal(match[3])))
    if include_quantity:
        weighted = re.compile(r'^(?P<quantity>\d+(?:\.\d+)?)\s*(?P<unit>kg|lb)\s*@\s*\$?\s*(?P<price>\d+\.\d{2})\s*/\s*(?P=unit)\b',re.I)
        for i,line in enumerate(lines[:-1]):
            name=product_name(line)
            match=weighted.match(lines[i+1])
            if not name or not match or Decimal(match['quantity'])<=0:
                continue
            existing=next((r for r in result if r['line_number']==i+1),None)
            if existing is not None:
                # A printed item discount changes the net amount, not the
                # weight-times-unit-price gross calculation.
                if existing['amount']!=existing['gross']:
                    continue
                continue  # Preserve one association per original item row.
            calculated=(Decimal(match['quantity'])*Decimal(match['price'])).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            result.append(dict(name=name,amount=calculated,line_number=i+1,money_line_number=i+2,
                currency='$' in lines[i+1],text=line+' | '+lines[i+1],gross=calculated,
                kind='quantity_check',quantity=match['quantity'],unit_price=match['price']))
        result.sort(key=lambda r:r['line_number'])
    if placeholders and result:
        occupied={r['line_number'] for r in result}
        for i in range(result[0]['line_number']-1,result[-1]['line_number']):
            name=product_name(lines[i])
            if name and i+1 not in occupied and scan.parse_money(lines[i]) is None:
                result.append(dict(name=name,amount=None,line_number=i+1,
                                   money_line_number=None,currency=False,text=lines[i],gross=None))
        result.sort(key=lambda r:r['line_number'])
    return result


def number_signature(name):
    # Unit G can be read as a trailing 6, including decimal package sizes.
    return re.findall(r'\d+',re.sub(r'(?<=\d)[gG]\b','6',name))


def contextual_matches(base, readings):
    matches=align([r['name'] for r in base],[r['name'] for r in readings])
    anchors=sorted(matches.items())
    for (left,other_left),(right,other_right) in zip(anchors,anchors[1:]):
        missing=[i for i in range(left+1,right) if base[i]['amount'] is not None]
        other_missing=list(range(other_left+1,other_right))
        if len(missing)==len(other_missing)==1:
            i,j=missing[0],other_missing[0]
            a=re.sub('[^a-z]','',base[i]['name'].lower())
            b=re.sub('[^a-z]','',readings[j]['name'].lower())
            if SequenceMatcher(None,a,b).ratio()>=.6:
                matches[i]=j
    return matches


def resolve(pages, observations, *, layout_pages=()):
    sources = defaultdict(list)
    for p in observations:
        text = p.get('text') or p.get('extracted_text') or ''
        if (p.get('status') not in (None,'success') or not p.get('page_number')
                or not text or scan._looks_like_tesseract_tsv(text)):
            continue
        provenance=p.get('provenance') or {}
        if isinstance(provenance,str):
            import json
            provenance=json.loads(provenance)
        sources[p['page_number']].append((p,rows(text,provenance=provenance,include_quantity=True)))
    if not any(readings for observations in sources.values() for _,readings in observations):
        return scan.extract_items(scan.merge_page_text(pages)), []
    selected, decisions = [], []
    for page_number,page in enumerate(pages,1):
        base=rows(page,placeholders=True)
        base_lines={}
        def retain(lines):
            for line in lines:
                if isinstance(line,dict):
                    base_lines[line.get('line_number')]=line
                    retain(line.get('children',[]))
        for layout_page in layout_pages or ():
            if layout_page.get('page_number')==page_number:
                retain(layout_page.get('lines',[]))
        page_lines=page.splitlines()
        for row in base:
            original=base_lines.get(row['money_line_number'],{})
            number=row['money_line_number']
            if number and number<=len(page_lines) and original.get('text')==page_lines[number-1]:
                row['confidence']=original.get('confidence')
        candidates=defaultdict(list)
        for p,readings in sources[page_number]:
            matches=contextual_matches(base,readings)
            for bi,ri in matches.items():
                a,b=base[bi],readings[ri]
                # Different product sizes/codes are not interchangeable prices.
                if number_signature(a['name']) != number_signature(b['name']):
                    # A digit introduced into a damaged produce description
                    # cannot defeat a unique ordered match with the exact same
                    # printed weight/unit-price detail. Explicit sizes/codes stay
                    # distinct, and a weight calculation never invents a price.
                    sized=any(re.search(r'\d+\s*(?:kg|lb|g|ml|l|pk|pack)\b|^\d+\b',name,re.I)
                              for name in (a['name'],b['name']))
                    if sized or not a.get('weight_detail') or a.get('weight_detail')!=b.get('weight_detail'):
                        continue
                provenance=p.get('provenance') or {}
                if isinstance(provenance,str):
                    import json
                    provenance=json.loads(provenance)
                confidence=next((line.get('confidence') for line in provenance.get('lines',[])
                    if line.get('line_number')==b['money_line_number']),None)
                candidates[bi].append(dict(value=f"{b['amount']:.2f}",confidence=confidence,
                    kind=b.get('kind','literal'),quantity=b.get('quantity'),unit_price=b.get('unit_price'),source={
                    **{k:p.get(k) for k in ('run_uuid','pass_id','engine','dpi','psm','variant')},
                    'page_number':page_number,'line_number':b['line_number'],
                    'money_line_number':b['money_line_number'],
                    'geometry':next(({k:line.get(k) for k in ('x','y','width','height')}
                        for line in provenance.get('lines',[]) if line.get('line_number')==b['money_line_number']),{})},
                    currency=b['currency'],text=b['text'],gross_value=str(b['gross']) if b['gross'] is not None else None))
        for index,row in enumerate(base):
            options=candidates[index]
            literal_values={Decimal(c['value']) for c in options if c.get('kind')!='quantity_check'}
            if row['amount'] is not None:
                literal_values.add(row['amount'])
            for c in options:
                c['eligible']=c.get('kind')!='quantity_check' or Decimal(c['value']) in literal_values
            eligible=[c for c in options if c['eligible']]
            engines=defaultdict(set)
            configurations=defaultdict(set)
            for c in eligible:
                source=c['source']
                if source.get('engine'):
                    engines[(source.get('run_uuid'),c['value'])].add(source['engine'])
                if c['currency']:
                    configurations[c['value']].add((source.get('dpi'),source.get('psm'),source.get('variant')))
            supported=sorted({v for (run,v),e in engines.items() if run is not None and len(e)>=2})
            reason=('observed_price_corroborated_by_weight_quantity'
                    if any(c.get('kind')=='quantity_check' and c['eligible'] and c['value'] in supported for c in options)
                    else 'independent_engines_agree')
            if (not supported and row['amount'] is not None and not row['currency']
                    and len({c['source'].get('run_uuid') for c in options})==1
                    and all(c['source'].get('run_uuid') is not None for c in options)):
                # A dollar sign read as a leading digit is recoverable only
                # from matching explicit-currency readings in diverse renders.
                original=f"{row['amount']:.2f}"
                supported=[v for v,c in configurations.items() if len(c)>=2
                           and original[1:]==f'{Decimal(v):.2f}']
                reason='currency_amount_corroborated_by_distinct_renders'
            if (not supported and options
                    and len({c['source'].get('run_uuid') for c in options})==1
                    and all(c['source'].get('run_uuid') is not None for c in options)):
                confident=defaultdict(list)
                for c in eligible:
                    confident[c['value']].append(c)
                clear=[]
                for v,readings in confident.items():
                    quality=max((c.get('confidence') or 0 for c in readings if c['currency']),default=0)
                    opponents=[c.get('confidence') for c in eligible if c['value']!=v]
                    if (row['amount'] is not None and Decimal(v)!=row['amount']
                            and (row.get('confidence') or 0)>0):
                        opponents.append(row['confidence'])
                    # Confidence is evidence quality, not a correctness probability.
                    if (quality>=MIN_PRICE_CONFIDENCE and opponents and all(q is not None for q in opponents)
                            and quality-max(opponents)>=PRICE_CONFIDENCE_MARGIN):
                        clear.append(v)
                if len(clear)==1:
                    supported=clear
                    reason='clear_currency_reading_with_confidence_margin'
            value=Decimal(supported[0]) if len(supported)==1 else row['amount']
            observed={Decimal(c['value']) for c in eligible}
            if row['amount'] is not None:
                observed.add(row['amount'])
            conflict=len(supported)>1 or (not supported and len(observed)>1)
            status=('conflict' if conflict else 'selected' if supported else 'observed'
                    if any(Decimal(c['value'])==row['amount'] for c in eligible) else 'unverified')
            winning=[c for c in eligible if Decimal(c['value'])==value]
            gross=(max(winning,key=lambda c:c.get('confidence') or 0)['gross_value']
                   if winning else str(row['gross']) if row['gross'] is not None else None)
            decisions.append(dict(page_number=page_number,line_number=row['line_number'],
                gross_value=gross, base_confidence=row.get('confidence'),
                included=value is not None,item_name=row['name'],original_value=str(row['amount']) if row['amount'] is not None else None,
                value=str(value) if value is not None else None,status=status,
                reason=reason if len(supported)==1 else 'insufficient_or_conflicting_price_evidence',
                candidates=options))
            if value is not None:
                selected.append(scan.ScannedItem(row['name'],float(value)))
    return collapse_page_overlap(decisions),decisions


def collapse_page_overlap(decisions):
    """Collapse matching boundary products only when their observed gross prices agree.

    Require two successive products, including a strong description anchor.
    A conflicting earlier price may defer to a corroborated later price only
    when that exact later gross was also observed on the earlier row.
    The later page retains its complete adjustment rows; originals remain in
    decisions and its overlap_sources. Never merge repeated rows within a page.
    """
    pages=defaultdict(list)
    for d in decisions:
        if d['included']:
            pages[d['page_number']].append(d)
    retained=[]
    for number,current in sorted(pages.items()):
        previous=[d for d in retained if d['page_number']==number-1]
        overlap=0
        for count in range(2,min(len(previous),len(current),16)+1):
            left,right=previous[-count:],current[:count]
            ratios=[SequenceMatcher(None,re.sub('[^a-z0-9]','',a['item_name'].lower()),
                                        re.sub('[^a-z0-9]','',b['item_name'].lower())).ratio()
                    for a,b in zip(left,right)]
            if (min(ratios)>=.6 and max(ratios)>=.9 and all(
                    a['gross_value'] is not None and b['gross_value'] is not None
                    and (Decimal(a['gross_value'])==Decimal(b['gross_value']) or (
                        a['status']=='conflict' and b['status']=='selected'
                        and any(c.get('gross_value') is not None and
                                Decimal(c['gross_value'])==Decimal(b['gross_value'])
                                for c in a['candidates'])))
                    for a,b in zip(left,right))):
                overlap=count
        if overlap:
            for a,b in zip(previous[-overlap:],current[:overlap]):
                a['included']=False
                a['overlap_with']={'page_number':number,'line_number':b['line_number']}
                b.setdefault('overlap_sources',[]).append({k:a[k] for k in
                    ('page_number','line_number','item_name','value','gross_value','candidates')})
            retained=retained[:-overlap]
        retained.extend(current)
    return [scan.ScannedItem(d['item_name'],float(Decimal(d['value']))) for d in retained]


def apply(receipt, pages, observations):
    from .departments import assign_items
    items,decisions=resolve(pages,observations,layout_pages=receipt.ocr_layout)
    if not decisions:
        from .scan_completeness import apply as apply_scan_completeness
        apply_scan_completeness(receipt,pages,observations)
        return
    prior_sections=defaultdict(set)
    for old in receipt.items:
        prior_sections[(old.item_name,Decimal(str(old.line_total)))].add(old.department)
    receipt.items=scan.normalize_item_signs(items,receipt.total)
    assign_items(receipt.items,receipt.ocr_layout)
    included=[d for d in decisions if d['included']]
    for item,decision in zip(receipt.items,included):
        original=decision['original_value']
        if item.department is None and original is not None:
            sections=prior_sections[(item.item_name,Decimal(original))]
            if len(sections)==1:
                item.department=next(iter(sections))

    verified=(all(d['status'] in {'selected','observed'} for d in decisions if d['included'])
              and not any(d['value'] is None and d['candidates'] for d in decisions))
    receipt.field_decisions['items']={
        'policy':POLICY,'status':'selected' if verified else 'conflict',
        'value':str(len(items)),'reason':'observed_base_or_corroborated_price_evidence','rows':decisions,
        'candidates':[{**candidate,'item_name':d['item_name'],
                       'item_page':d['page_number'],'item_line':d['line_number']}
                      for d in decisions for candidate in d['candidates']]}
    from .scan_completeness import apply as apply_scan_completeness
    apply_scan_completeness(receipt,pages,observations)


def corroborated_missing_adjustments(receipt, issue_count):
    """Resolve missing discount text only from the same row's two-engine net evidence."""
    recovered=[]
    decisions=receipt.field_decisions.get('items',{}).get('rows',[])
    for page_number,page in enumerate(receipt.page_text,1):
        lines=page.splitlines()
        for index,line in enumerate(lines):
            if not ADJUSTMENT.match(scan.normalize_line(line)) or 'savings' not in line.lower():
                continue
            if scan.parse_money(line) is not None:
                continue
            if index+1<len(lines) and PRICE_ONLY.fullmatch(lines[index+1].strip()):
                continue
            prior=[d for d in decisions if d['page_number']==page_number and d['line_number']<index+1]
            if not prior:
                return False
            row=prior[-1]
            if row['status']!='selected' or row['value'] is None or row['gross_value'] is None:
                return False
            net,gross=Decimal(row['value']),Decimal(row['gross_value'])
            if not 0<=net<gross:
                return False
            support=defaultdict(set)
            for c in row['candidates']:
                if Decimal(c['value'])==net and c['gross_value'] is not None and Decimal(c['gross_value'])==gross:
                    support[c['source'].get('run_uuid')].add(c['source'].get('engine'))
            if not any(run and len(engines-{None})>=2 for run,engines in support.items()):
                return False
            recovered.append({'page_number':page_number,'line_number':index+1,
                              'item_line_number':row['line_number'],'discount':str(gross-net)})
    if len(recovered)!=issue_count:
        return False
    receipt.field_decisions['items']['recovered_adjustments']=recovered
    return bool(recovered)
