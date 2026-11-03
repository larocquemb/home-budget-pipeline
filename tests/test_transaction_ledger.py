from datetime import date
from decimal import Decimal
import pytest
from home_budget_pipeline.transactions.ledger import parse_rates,text_table


def test_bank_of_canada_observations_are_cad_per_usd_and_missing_values_stay_missing():
    payload={'observations':[{'d':'2026-03-12','FXUSDCAD':{'v':'1.3654'}},
                             {'d':'2026-03-13','FXUSDCAD':{'v':''}}]}
    assert parse_rates(payload,date(2026,3,1),date(2026,3,31))=={date(2026,3,12):Decimal('1.3654')}


@pytest.mark.parametrize('value',['NaN','-1','0','1000','bad'])
def test_invalid_rates_cannot_become_cad_amounts(value):
    with pytest.raises(ValueError):
        parse_rates({'observations':[{'d':'2026-03-12','FXUSDCAD':{'v':value}}]},date(2026,3,1),date(2026,3,31))


def test_cli_ledger_does_not_truncate_monetary_values():
    text=text_table([{'bank_date':date(2026,3,12),'account':'Example','account_last4':'1234','description':'Example',
                     'original_amount':Decimal('123456789012.34'),'currency':'USD',
                     'amount_cad':Decimal('987654321098765.43'),'conversion_status':'boc_daily'}])
    assert '123456789012.34' in text and '987654321098765.43' in text
