from decimal import Decimal
import pytest
from home_budget_pipeline.transactions.cibcus import parse_cibcus


def test_us_csv_signs_descriptions_and_masked_account_references():
    text=('Transaction Date, Short Description, Long Description, Amount\n'
          '2026-05-11,TRANSFER,TRANSFER TO 123456789012,-25.00\n'
          '2026-05-12,CREDIT,TRANSFER FROM 987654321098,40.00\n')
    rows=parse_cibcus(text.encode(),'CIBC US Smart x1234',account_last4='1234')
    assert [(r.amount,r.kind) for r in rows]==[(Decimal('25.00'),'withdrawal'),(Decimal('-40.00'),'deposit')]
    assert rows[0].description=='TRANSFER TO ****9012'
    assert '123456789012' not in str(rows)
    assert rows[0].number==2


@pytest.mark.parametrize('line',['2026-05-32,TEXT,TEXT,1.00\n','2026-05-11,TEXT,TEXT,NaN\n','2026-05-11,,,1.00\n'])
def test_us_csv_rejects_invalid_rows(line):
    with pytest.raises(ValueError):
        parse_cibcus(('Transaction Date,Short Description,Long Description,Amount\n'+line).encode(),
                     'CIBC US Smart x1234',account_last4='1234')
