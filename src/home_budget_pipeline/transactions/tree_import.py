"""Preview/apply archived CIBC exports by product and last-four account folder."""
from pathlib import Path
import re
from .cibc import parse_cibc
from .workflows import import_cibc

PRODUCTS={'mastercard':('credit_card','MasterCard'),'visa':('credit_card','Visa'),
          'smart':('chequing','Smart'),'eadvantage':('savings','eAdvantage')}


def plans(conn, root, currencies=None):
    root=Path(root).resolve()
    if not root.is_dir():
        raise ValueError('CIBC archive root is not a directory')
    result=[]
    currencies={k.casefold():v for k,v in (currencies or {}).items()}
    used=set()
    for file in sorted(root.rglob('*.csv')):
        parts=file.relative_to(root).parts
        if len(parts) not in (3,4) or parts[0].casefold() not in PRODUCTS or not re.fullmatch(r'x\d{4}',parts[1]):
            raise ValueError('Use product/xLAST4/[year/]download.csv below the CIBC root')
        if len(parts)==4 and not re.fullmatch(r'\d{4}',parts[2]):
            raise ValueError('Optional year folders must have four digits')
        account_type,label=PRODUCTS[parts[0].casefold()]
        last4=parts[1][1:]
        matches=conn.execute('''SELECT DISTINCT a.account_reference,a.currency FROM accounting.accounts a
            LEFT JOIN accounting.account_numbers n ON n.account_id=a.id
            WHERE a.institution='CIBC' AND a.account_type=%s AND
            (CASE WHEN a.account_type='credit_card' THEN a.primary_card_last4=%s
                  ELSE n.number_kind='bank_account' AND n.last4=%s END)''',
            (account_type,last4,last4)).fetchall()
        if len(matches)>1:
            raise ValueError('More than one account has this last-four identifier; review the account registry')
        reference=matches[0]['account_reference'] if matches else f'CIBC {label} x{last4}'
        key='/'.join(parts[:2]).casefold()
        if key in currencies:
            used.add(key)
        currency=currencies.get(key,matches[0]['currency'] if matches else 'CAD')
        if currency not in {'CAD','USD'}:
            raise ValueError('Supported export currencies are CAD and USD')
        if matches and matches[0]['currency']!=currency:
            raise ValueError('Currency override conflicts with the registered account')
        parse_cibc(file.read_bytes(),reference,account_type=account_type,account_last4=last4)
        result.append((file,reference,account_type,last4,currency))
    if not result:
        raise ValueError('No CSV exports found below the CIBC archive root')
    if set(currencies)-used:
        raise ValueError('Currency override did not match an archived account folder')
    return result


def import_tree(conn,root,actor,*,apply=False,currencies=None):
    with conn.transaction():
        planned=plans(conn,root,currencies)  # Validate all files before any import.
        known=set()
        reports=[]
        for file,reference,account_type,last4,currency in planned:
            report=import_cibc(conn,file,reference,actor,apply=apply,account_type=account_type,
                               account_last4=last4,extra_known=known,currency=currency)
            reports.append({'file':str(file),'account_reference':reference,'account_type':account_type,
                            'account_last4':last4,**report})
        return {'applied':apply,'files':reports,'rows':sum(r['rows'] for r in reports),
                'new':sum(r['new'] for r in reports),
                'already_imported':sum(r['already_imported'] for r in reports)}
