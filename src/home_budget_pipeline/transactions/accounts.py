"""Last-four-only account identifiers and explicitly asserted ownership."""
import re


def register_account(conn,reference,institution,account_type,last4,nickname,actor,*,currency='CAD',
                     reason='User-confirmed account profile'):
    """Register metadata without importing transactions or assigning owners."""
    if institution not in {'CIBC','ACU','CIBC US'} or account_type not in {'credit_card','chequing','savings'}:
        raise ValueError('Unsupported institution or account type')
    if not re.fullmatch(r'\d{4}',last4) or currency not in {'CAD','USD'}:
        raise ValueError('Use exactly four identifier digits and CAD or USD')
    if not reference.strip() or re.search(r'\d{16}',reference):
        raise ValueError('Use a reference label with last-four identifiers')
    if not isinstance(nickname,str) or not nickname.strip() or len(nickname)>200 or re.search(r'\d{16}',nickname):
        raise ValueError('A nickname of 1–200 characters is required')
    if not actor.strip() or not reason.strip():
        raise ValueError('Actor and reason are required')
    from psycopg.types.json import Jsonb
    import json
    with conn.transaction():
        cur=conn.cursor()
        cur.execute('SELECT * FROM accounting.accounts WHERE account_reference=%s FOR UPDATE',(reference,))
        before=cur.fetchone()
        if before and (before['institution']!=institution or before['account_type']!=account_type or before['currency']!=currency):
            raise ValueError('Account identity or currency conflicts with registered metadata')
        cur.execute('''INSERT INTO accounting.accounts(account_reference,institution,account_type,currency,primary_card_last4,nickname)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_reference) DO UPDATE SET nickname=EXCLUDED.nickname,
                primary_card_last4=COALESCE(accounting.accounts.primary_card_last4,EXCLUDED.primary_card_last4)
            RETURNING *''',(reference,institution,account_type,currency,last4 if account_type=='credit_card' else None,nickname.strip()))
        after=cur.fetchone()
        if after['institution']!=institution or after['account_type']!=account_type or after['currency']!=currency:
            raise ValueError('Account identity or currency conflicts with registered metadata')
        kind='card' if account_type=='credit_card' else 'bank_account'
        cur.execute('SELECT last4 FROM accounting.account_numbers WHERE account_id=%s AND number_kind=%s', (after['id'],kind))
        known={r['last4'] for r in cur.fetchall()}
        if ((kind=='card' and after['primary_card_last4']!=last4)
                       or (kind=='bank_account' and known and last4 not in known)):
            raise ValueError('Last-four identifier conflicts with the registered account')
        cur.execute('''INSERT INTO accounting.account_numbers(account_id,number_kind,last4)
            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''',(after['id'],kind,last4))
        if before!=after:
            cur.execute('''INSERT INTO accounting.account_profile_audit(account_id,actor,reason,before_profile,after_profile)
                VALUES(%s,%s,%s,%s,%s)''',(after['id'],actor,reason,
                    Jsonb(json.loads(json.dumps(before,default=str))),Jsonb(json.loads(json.dumps(after,default=str)))))
        return {'account_id':after['id'],'nickname':after['nickname'],'currency':after['currency']}


def record_ownership(conn, reference, primary_last4, primary_owner, cardholders, actor, archive_root=None,
                     reason='User-confirmed account and linked card ownership'):
    if re.search(r'\d{16}',reference):
        raise ValueError('Use a label and last four digits, never a full account number')
    if not re.fullmatch(r'\d{4}',primary_last4):
        raise ValueError('Use only the primary card last four digits')
    if not primary_owner.strip() or not actor.strip() or not isinstance(cardholders,dict):
        raise ValueError('Primary owner, actor and cardholder mapping are required')
    for ending,owner in cardholders.items():
        if not re.fullmatch(r'\d{4}',ending) or not isinstance(owner,str) or not owner.strip():
            raise ValueError('Cardholders require exactly four digits and a name')
    if cardholders.get(primary_last4)!=primary_owner:
        raise ValueError('The primary cardholder must match the primary account owner')
    with conn.transaction():
        cur=conn.cursor()
        cur.execute('SELECT id,primary_card_last4 FROM accounting.accounts WHERE account_reference=%s FOR UPDATE',(reference,))
        account=cur.fetchone()
        if not reason.strip():
            raise ValueError('An ownership correction reason is required')
        if not account:
            raise ValueError('Import or migrate the account before recording ownership')
        if account['primary_card_last4'] not in (None,primary_last4):
            raise ValueError('Account primary identifier differs; record an explicit account correction')
        cur.execute('''UPDATE accounting.accounts SET primary_card_last4=%s,
            archive_root=COALESCE(%s,archive_root) WHERE id=%s''',(primary_last4,archive_root,account['id']))
        cur.execute('SELECT * FROM accounting.account_ownerships WHERE account_id=%s ORDER BY id',(account['id'],))
        before=cur.fetchall()
        assertions=[]
        for ending,owner in cardholders.items():
            cur.execute('''INSERT INTO accounting.account_numbers(account_id,number_kind,last4)
                VALUES (%s,'card',%s) ON CONFLICT(account_id,number_kind,last4) DO UPDATE SET last4=EXCLUDED.last4 RETURNING id''',
                        (account['id'],ending))
            assertions.append((cur.fetchone()['id'],owner,'primary' if ending==primary_last4 else 'secondary'))
        old={(r['account_number_id'],r['owner_name'],r['ownership_role']) for r in before}
        if old==set(assertions) and len(before)==len(assertions):
            return {'account_id':account['id'],'primary_last4':primary_last4,'ownership_assertions':len(assertions),
                    'changed':False,'historical_payer_inference':False}
        cur.execute('DELETE FROM accounting.account_ownerships WHERE account_id=%s',(account['id'],))
        for number_id,owner,role in assertions:
            cur.execute('''INSERT INTO accounting.account_ownerships
                (account_id,account_number_id,owner_name,ownership_role,actor,basis)
                VALUES (%s,%s,%s,%s,%s,'user-confirmed current ownership; historical effective dates unknown')
                ON CONFLICT DO NOTHING''',(account['id'],number_id,owner,role,actor))
        cur.execute('SELECT * FROM accounting.account_ownerships WHERE account_id=%s ORDER BY id',(account['id'],))
        after=cur.fetchall()
        import json
        from psycopg.types.json import Jsonb
        cur.execute('''INSERT INTO accounting.account_ownership_audit
            (account_id,actor,reason,before_ownerships,after_ownerships) VALUES(%s,%s,%s,%s,%s)''',
            (account['id'],actor,reason,Jsonb(json.loads(json.dumps(before,default=str))),
             Jsonb(json.loads(json.dumps(after,default=str)))))
        cur.execute('ALTER TABLE accounting.account_ownerships VALIDATE CONSTRAINT account_ownerships_roles_v2')
        cur.execute('ALTER TABLE accounting.account_ownerships VALIDATE CONSTRAINT account_ownerships_card_reference_v2')
        cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_account_ownership_card
            ON accounting.account_ownerships(account_number_id)''')
        return {'account_id':account['id'],'primary_last4':primary_last4,'ownership_assertions':len(assertions),
                'changed':True,'historical_payer_inference':False}


def account_rows(service):
    return service._fetch('''SELECT a.id,a.account_reference,a.nickname,a.institution,a.primary_card_last4,a.currency,
        a.account_type,n.number_kind,n.last4,o.owner_name,o.ownership_role,o.effective_from,o.effective_to,o.asserted_at,o.basis
        FROM accounting.accounts a LEFT JOIN accounting.account_numbers n ON n.account_id=a.id
        LEFT JOIN accounting.account_ownerships o ON o.account_number_id=n.id
        ORDER BY a.id,o.ownership_role,n.last4''')
