"""Last-four-only account identifiers and explicitly asserted ownership."""
import re


def record_ownership(conn, reference, primary_last4, primary_owner, cardholders, actor, archive_root=None):
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
        if not account:
            raise ValueError('Import or migrate the account before recording ownership')
        if account['primary_card_last4'] not in (None,primary_last4):
            raise ValueError('Account primary identifier differs; record an explicit account correction')
        cur.execute('''UPDATE accounting.accounts SET primary_card_last4=%s,
            archive_root=COALESCE(%s,archive_root) WHERE id=%s''',(primary_last4,archive_root,account['id']))
        assertions=[(None,primary_owner,'primary')]
        for ending,owner in cardholders.items():
            cur.execute('''INSERT INTO accounting.account_numbers(account_id,number_kind,last4)
                VALUES (%s,'card',%s) ON CONFLICT(account_id,number_kind,last4) DO UPDATE SET last4=EXCLUDED.last4 RETURNING id''',
                        (account['id'],ending))
            assertions.append((cur.fetchone()['id'],owner,'cardholder'))
        for number_id,owner,role in assertions:
            cur.execute('''SELECT owner_name FROM accounting.account_ownerships
                WHERE account_id=%s AND account_number_id IS NOT DISTINCT FROM %s AND ownership_role=%s
                  AND effective_to IS NULL''',(account['id'],number_id,role))
            if any(r['owner_name']!=owner for r in cur.fetchall()):
                raise ValueError('Ownership differs; preserve history and record an explicit correction')
            cur.execute('''INSERT INTO accounting.account_ownerships
                (account_id,account_number_id,owner_name,ownership_role,actor,basis)
                VALUES (%s,%s,%s,%s,%s,'user-confirmed current ownership; historical effective dates unknown')
                ON CONFLICT DO NOTHING''',(account['id'],number_id,owner,role,actor))
        return {'account_id':account['id'],'primary_last4':primary_last4,'ownership_assertions':len(assertions),
                'historical_payer_inference':False}


def account_rows(service):
    return service._fetch('''SELECT a.id,a.account_reference,a.institution,a.primary_card_last4,
        n.number_kind,n.last4,o.owner_name,o.ownership_role,o.effective_from,o.effective_to,o.asserted_at,o.basis
        FROM accounting.accounts a LEFT JOIN accounting.account_ownerships o ON o.account_id=a.id
        LEFT JOIN accounting.account_numbers n ON n.id=o.account_number_id
        ORDER BY a.id,o.ownership_role,n.last4''')
