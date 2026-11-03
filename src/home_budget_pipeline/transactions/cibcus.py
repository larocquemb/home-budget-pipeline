"""CIBC US signed-change exports with short/long descriptions and masked IDs."""
from collections import Counter
from datetime import date
from decimal import Decimal,InvalidOperation
import csv
import hashlib
import io
import json
import re
from .bank_rows import BankRow,redact_identifiers

HEADER=['Transaction Date','Short Description','Long Description','Amount']


def parse_cibcus(data,account_reference,*,account_type='chequing',account_last4=None):
    if account_type not in {'chequing','savings'} or not re.fullmatch(r'\d{4}',account_last4 or ''):
        raise ValueError('CIBC US requires a deposit account with a last-four identifier')
    if not account_reference.strip() or re.search(r'\d{16}',account_reference):
        raise ValueError('Use an account label and last four digits')
    reader=csv.reader(io.StringIO(data.decode('utf-8-sig')),strict=True)
    header=next(reader,None)
    if header is None or [v.strip() for v in header]!=HEADER:
        raise ValueError('Expected CIBC US date, short description, long description, amount headers')
    result=[];occurrences=Counter()
    for number,raw in enumerate(reader,2):
        if len(raw)!=4:
            raise ValueError(f'Row {number}: expected four CIBC US columns')
        day,short,long,native_amount=(v.strip() for v in raw)
        try:
            parsed_date=date.fromisoformat(day)
            if not re.fullmatch(r'-?\d+\.\d{2}',native_amount):
                raise ValueError()
            amount=-Decimal(native_amount)
            if not amount.is_finite() or not amount or abs(amount)>=Decimal('1000000000000') or not (short or long):
                raise ValueError()
        except (ValueError,InvalidOperation):
            raise ValueError(f'Row {number}: invalid CIBC US date, description or amount') from None
        safe_short,safe_long=redact_identifiers(short),redact_identifiers(long)
        description=safe_long or safe_short
        # Hash original text for stable identity without persisting its identifiers.
        identity=[account_reference.strip().casefold(),parsed_date.isoformat(),
                  ' '.join(short.casefold().split()),' '.join(long.casefold().split()),str(amount),account_last4]
        key=json.dumps(identity,separators=(',',':'));occurrences[key]+=1
        fingerprint=hashlib.sha256(json.dumps(['cibcus-deposit-v1',*identity,occurrences[key]],separators=(',',':')).encode()).hexdigest()
        result.append(BankRow(number,parsed_date,description,amount,None,'withdrawal' if amount>0 else 'deposit',
                              [raw[0],redact_identifiers(raw[1]),redact_identifiers(raw[2]),raw[3]],fingerprint))
    if not result:
        raise ValueError('The CIBC US export has no transactions')
    return result
