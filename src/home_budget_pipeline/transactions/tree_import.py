"""Preview/apply institution/product/last-four archives with optional year folders."""
from pathlib import Path
import re
from .cibc import parse_cibc
from .acu import parse_acu
from .cibcus import parse_cibcus
from .workflows import import_bank_csv

PRODUCTS={'mastercard':('credit_card','MasterCard'),'visa':('credit_card','Visa'),
          'smart':('chequing','Smart'),'eadvantage':('savings','eAdvantage'),'us':('chequing','US')}
PROFILES={'cibc':('CIBC',PRODUCTS,parse_cibc),
          'acu':('ACU',{'chequing':('chequing','Chequing'),'savings':('savings','Savings')},parse_acu),
          'cibcus':('CIBC US',{'smart':('chequing','Smart'),'savings':('savings','Savings')},parse_cibcus)}


def plans(conn, root, currencies=None):
    root=Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Transaction archive root is not a directory')
    result=[]
    currencies={k.casefold():v for k,v in (currencies or {}).items()}
    used=set()
    for file in sorted(root.rglob('*.csv')):
        parts=file.relative_to(root).parts
        if root.name.casefold() in PROFILES:
            bank=root.name.casefold();prefix=''
        elif parts[0].casefold() in PROFILES:
            bank=parts[0].casefold();prefix=bank+'/';parts=parts[1:]
        else:
            raise ValueError('Use institution/product/xLAST4/[year/]download.csv (CIBC, CIBC US or ACU)')
        institution,products,parser=PROFILES[bank]
        if len(parts) not in (3,4) or parts[0].casefold() not in products or not re.fullmatch(r'x\d{4}',parts[1]):
            raise ValueError('Use a supported product/xLAST4/[year/]download.csv below the institution root')
        if len(parts)==4 and not re.fullmatch(r'\d{4}',parts[2]):
            raise ValueError('Optional year folders must have four digits')
        account_type,label=products[parts[0].casefold()]
        last4=parts[1][1:]
        matches=conn.execute('''SELECT DISTINCT a.account_reference,a.currency FROM accounting.accounts a
            LEFT JOIN accounting.account_numbers n ON n.account_id=a.id
            WHERE a.institution=%s AND a.account_type=%s AND
            (CASE WHEN a.account_type='credit_card' THEN a.primary_card_last4=%s
                  ELSE n.number_kind='bank_account' AND n.last4=%s END)''',
            (institution,account_type,last4,last4)).fetchall()
        if len(matches)>1:
            raise ValueError('More than one account has this last-four identifier; review the account registry')
        reference=matches[0]['account_reference'] if matches else f'{institution} {label} x{last4}'
        key=(prefix+'/'.join(parts[:2])).casefold()
        if key in currencies:
            used.add(key)
        default_currency='USD' if bank=='cibcus' or (bank=='cibc' and parts[0].casefold()=='us') else 'CAD'
        currency=currencies.get(key,matches[0]['currency'] if matches else default_currency)
        if currency not in {'CAD','USD'}:
            raise ValueError('Supported export currencies are CAD and USD')
        if matches and matches[0]['currency']!=currency:
            raise ValueError('Currency override conflicts with the registered account')
        parser(file.read_bytes(),reference,account_type=account_type,account_last4=last4)
        result.append((file,reference,account_type,last4,currency,institution))
    if not result:
        raise ValueError('No CSV exports found below the archive root')
    if set(currencies)-used:
        raise ValueError('Currency override did not match an archived account folder')
    return result


def import_tree(conn,root,actor,*,apply=False,currencies=None):
    with conn.transaction():
        planned=plans(conn,root,currencies)  # Validate all files before any import.
        known=set()
        reports=[]
        for file,reference,account_type,last4,currency,institution in planned:
            report=import_bank_csv(conn,file,reference,actor,apply=apply,account_type=account_type,
                               account_last4=last4,extra_known=known,currency=currency,institution=institution)
            reports.append({'file':str(file),'account_reference':reference,'account_type':account_type,
                            'account_last4':last4,'institution':institution,**report})
        return {'applied':apply,'files':reports,'rows':sum(r['rows'] for r in reports),
                'new':sum(r['new'] for r in reports),
                'already_imported':sum(r['already_imported'] for r in reports)}
