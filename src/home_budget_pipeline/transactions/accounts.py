"""Last-four-only account identifiers and explicitly asserted ownership."""
import re


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
    return service._fetch('''SELECT a.id,a.account_reference,a.institution,a.primary_card_last4,
        n.number_kind,n.last4,o.owner_name,o.ownership_role,o.effective_from,o.effective_to,o.asserted_at,o.basis
        FROM accounting.accounts a LEFT JOIN accounting.account_ownerships o ON o.account_id=a.id
        LEFT JOIN accounting.account_numbers n ON n.id=o.account_number_id
        ORDER BY a.id,o.ownership_role,n.last4''')
