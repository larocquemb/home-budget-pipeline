"""ACU headered export: native signed changes, balance and redacted repeated ID."""
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import re
from .bank_rows import BankRow

HEADER=['Unique ID','Date','Description','Amount','Balance']


def money(value):
    value=value.strip()
    if value.count('$')>1:
        raise ValueError('Invalid money')
    normalized=value.replace('$','')
    if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}',normalized):
        raise ValueError('Invalid money')
    amount=Decimal(normalized.replace(',',''))
    if not amount.is_finite() or abs(amount)>=Decimal('1000000000000'):
        raise ValueError('Invalid money')
    return amount


def parse_acu(data,account_reference,*,account_type='chequing',account_last4=None):
    if account_type not in {'chequing','savings'} or not re.fullmatch(r'\d{4}',account_last4 or ''):
        raise ValueError('ACU imports require a deposit account and exactly four identifier digits')
    if not account_reference.strip() or re.search(r'\d{16}',account_reference):
        raise ValueError('Use an account label and last four digits')
    reader=csv.reader(io.StringIO(data.decode('utf-8-sig')),strict=True)
    if next(reader,None)!=HEADER:
        raise ValueError('Expected ACU header: Unique ID, Date, Description, Amount, Balance')
    result=[];occurrences=Counter()
    for number,raw in enumerate(reader,2):
        if len(raw)!=5:
            raise ValueError(f'Row {number}: expected five ACU columns')
        _,day,description,native_amount,balance=(v.strip() for v in raw)
        try:
            parsed_date=datetime.strptime(day,'%b %d, %Y').date()
            amount=-money(native_amount)
            money(balance)
            if not description or not amount:
                raise ValueError()
        except (ValueError,InvalidOperation):
            raise ValueError(f'Row {number}: invalid ACU date, description, amount or balance') from None
        identity=[account_reference.strip().casefold(),parsed_date.isoformat(),
                  ' '.join(description.casefold().split()),str(amount),account_last4]
        key=json.dumps(identity,separators=(',',':'));occurrences[key]+=1
        fingerprint=hashlib.sha256(json.dumps(['acu-deposit-v1',*identity,occurrences[key]],separators=(',',':')).encode()).hexdigest()
        # This field repeats in ACU exports and may be an account/member ID.
        # Do not persist it or treat it as a bank transaction identifier.
        safe_row=['[redacted identifier]',*raw[1:]]
        result.append(BankRow(number,parsed_date,description,amount,None,
                              'withdrawal' if amount>0 else 'deposit',safe_row,fingerprint))
    if not result:
        raise ValueError('The ACU export has no transactions')
    return result
