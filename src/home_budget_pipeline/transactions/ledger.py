"""Read-only CAD activity ledger and explicit public Bank of Canada rate refresh."""
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation
import hashlib
import json
from urllib.parse import urlencode
from urllib.request import Request,urlopen

PROVIDER='bank_of_canada'
SERIES='FXUSDCAD'  # Bank of Canada: Canadian dollars per one US dollar.


def parse_rates(payload,start,end):
    observations=payload.get('observations')
    if not isinstance(observations,list):
        raise ValueError('Bank of Canada response has no observation list')
    rates={}
    for row in observations:
        day=date.fromisoformat(row['d'])
        value=(row.get(SERIES) or {}).get('v')
        if value in (None,''):
            continue
        try:
            rate=Decimal(str(value))
            if not rate.is_finite() or not 0<rate<1000:
                raise ValueError()
        except (ValueError,InvalidOperation):
            raise ValueError('Invalid USD/CAD observation from Bank of Canada') from None
        if not start<=day<=end:
            raise ValueError('Bank of Canada returned an observation outside the requested range')
        if day in rates and rates[day]!=rate:
            raise ValueError('Conflicting observations in Bank of Canada response')
        rates[day]=rate
    return rates


def update_rates(conn,actor,*,fetch=None):
    from psycopg.types.json import Jsonb
    bounds=conn.execute("SELECT min(transaction_date) AS first,max(transaction_date) AS last FROM accounting.financial_transactions WHERE currency='USD'").fetchone()
    if bounds['first'] is None:
        return {'observations':0,'message':'No USD transactions need conversion'}
    start,end=bounds['first']-timedelta(days=7),bounds['last']
    url='https://www.bankofcanada.ca/valet/observations/'+SERIES+'/json?'+urlencode({'start_date':start.isoformat(),'end_date':end.isoformat()})
    if fetch is None:
        def fetch(url):
            with urlopen(Request(url,headers={'User-Agent':'BrownRookLedger/1.0'}),timeout=25) as response:
                body=response.read(2_000_001)
            if len(body)>2_000_000:
                raise ValueError('Bank of Canada response exceeded the expected size')
            return body
    raw=fetch(url)
    payload=json.loads(raw)
    rates=parse_rates(payload,start,end)
    if not rates:
        raise ValueError('No USD/CAD rates returned; existing rates are unchanged')
    with conn.transaction():
        record=conn.execute('''INSERT INTO accounting.exchange_rate_downloads
            (provider,source_url,source_sha256,payload,actor) VALUES(%s,%s,%s,%s,%s) RETURNING id''',
            (PROVIDER,url,hashlib.sha256(raw).hexdigest(),Jsonb(payload),actor)).fetchone()['id']
        for day,rate in rates.items():
            conn.execute('INSERT INTO accounting.exchange_rate_observations VALUES(%s,%s,%s)',(record,day,rate))
            conn.execute('''INSERT INTO accounting.exchange_rates(provider,base_currency,quote_currency,rate_date,rate,download_id)
                VALUES(%s,'USD','CAD',%s,%s,%s) ON CONFLICT(provider,base_currency,quote_currency,rate_date)
                DO UPDATE SET rate=EXCLUDED.rate,download_id=EXCLUDED.download_id''',(PROVIDER,day,rate,record))
    return {'provider':PROVIDER,'observations':len(rates),'from':start,'to':end,'download_id':record,
            'purpose':'CAD reporting equivalents; original bank amounts and settlement costs remain unchanged'}


def ledger_rows(service,*,limit=100,offset=0,direction='desc',account='',start=None,end=None):
    if not 1<=limit<=500 or offset<0 or direction not in {'asc','desc'}:
        raise ValueError('Invalid ledger pagination or sort direction')
    where=[];params=[]
    if account:
        where.append("(account ILIKE %s OR account_last4=%s)");params.extend(('%'+account+'%',account))
    if start is not None:
        where.append('bank_date>=%s');params.append(start)
    if end is not None:
        where.append('bank_date<=%s');params.append(end)
    if start and end and start>end:
        raise ValueError('Start date must not be after end date')
    condition=' WHERE '+' AND '.join(where) if where else ''
    count=service._fetch('''SELECT count(*) AS total,
        count(*) FILTER(WHERE amount_cad IS NULL) AS missing_rates
        FROM accounting.transaction_ledger_cad'''+condition,tuple(params))[0]
    rows=service._fetch('SELECT * FROM accounting.transaction_ledger_cad'+condition+
        ' ORDER BY bank_date '+direction.upper()+',transaction_id '+direction.upper()+' LIMIT %s OFFSET %s',tuple(params)+(limit,offset))
    return {'rows':rows,'total':count['total'],'missing_rates':count['missing_rates'],'limit':limit,'offset':offset}


def text_table(rows):
    fields=[('bank_date','Date',10),('account','Account',20),('account_last4','End',4),
            ('description','Description',44),('original_amount','Original',12),('currency','CCY',3),
            ('amount_cad','CAD amount',13),('conversion_status','Conversion',24)]
    def clean(value):
        return ''.join(ch for ch in str(value) if ch.isprintable())
    lines=[' | '.join(label.ljust(width) for key,label,width in fields)]
    for row in rows:
        values=[]
        for key,label,width in fields:
            value=clean('—' if row.get(key) is None else row[key])
            if key not in {'original_amount','amount_cad'} and len(value)>width:
                value=value[:width-1]+'…'
            values.append(value.ljust(width))
        lines.append(' | '.join(values))
    return '\n'.join(lines)
