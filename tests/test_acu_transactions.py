from decimal import Decimal
import pytest
from home_budget_pipeline.transactions.acu import parse_acu


def export(*rows):
    return ('Unique ID,Date,Description,Amount,Balance\n'+''.join(rows)).encode()


def test_acu_signs_money_and_privacy_redaction():
    data=export('99999999999999999999,"Oct 31, 2026",INTEREST,$0.25,$25.25\n',
                '99999999999999999999,"Oct 30, 2026",WITHDRAWAL,-$5.00,$25.00\n')
    rows=parse_acu(data,'ACU Chequing x1234',account_last4='1234')
    assert [(r.amount,r.kind,r.number) for r in rows]==[(Decimal('-.25'),'deposit',2),(Decimal('5.00'),'withdrawal',3)]
    assert all(r.card_last4 is None and r.raw[0]=='[redacted identifier]' for r in rows)
    assert all('99999999999999999999' not in str(r) for r in rows)


def test_acu_repeated_identifiers_are_not_transaction_keys():
    line='11111111111111111111,"Oct 31, 2026",WITHDRAWAL,-$5.00,$25.00\n'
    rows=parse_acu(export(line,line),'ACU Chequing x1234',account_last4='1234')
    assert len({r.fingerprint for r in rows})==2
    changed=parse_acu(export(line.replace('11111111111111111111','22222222222222222222')),
                      'ACU Chequing x1234',account_last4='1234')
    assert changed[0].fingerprint==rows[0].fingerprint


@pytest.mark.parametrize('line',[
    'id,"Oct 32, 2026",BAD,$1.00,$2.00\n',
    'id,"Oct 31, 2026",BAD,NaN,$2.00\n',
    'id,"Oct 31, 2026",BAD,$1.00,invalid\n',
    'id,"Oct 31, 2026",BAD,$0.00,$2.00\n'])
def test_acu_rejects_malformed_source_rows(line):
    with pytest.raises(ValueError):
        parse_acu(export(line),'ACU Savings x1234',account_type='savings',account_last4='1234')
