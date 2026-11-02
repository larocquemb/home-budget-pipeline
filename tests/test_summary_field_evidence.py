"""General payment/date layouts and competing original readings, without OCR."""
import pytest

from home_budget_pipeline.receipts import ingest, parallel_ingest, summary_evidence
from home_budget_pipeline.receipts.datetime import extract_transaction_datetime
from home_budget_pipeline.receipts.payment import extract_payment_provenance


def observation(text, pass_id=1, **extra):
    return dict(run_uuid='11111111-1111-1111-1111-111111111111', pass_id=pass_id,
                engine='tesseract', page_number=1, status='success',
                provenance={'lines': [{'text': line} for line in text.splitlines()]}, **extra)


@pytest.mark.parametrize('middle', ['*x\n24.60', 'RF\n$24.60', 'CHIP\nRCPT 123456'])
def test_numbered_card_mask_uses_bounded_terminal_block(middle):
    text = 'Mastercard TENDER 24.60\nCARD MC\n' + middle + '\nNO. ********7654'
    assert extract_payment_provenance(text).card_last4 == '7654'


@pytest.mark.parametrize('middle', ['MEMBER NUMBER', 'PRODUCT 24.60', 'Customer address',
                                  'RF\nRF\nRF\nRF\nRF\nRF'])
def test_card_context_never_crosses_unrelated_rows_or_bound(middle):
    assert extract_payment_provenance('CARD MC\n' + middle + '\nNO. ********7654').card_last4 is None


def test_split_terminal_date_and_time_have_equal_quality_to_inline():
    split = 'DATE 07/12/2031\nRESP 001\nAUTH 12345\nTIME 09:42:13'
    assert extract_transaction_datetime(split) == '2031-07-12T09:42:13'
    assert ingest._has_literal_valid_timestamp(split)
    assert ingest._has_literal_valid_timestamp('DATE 07/12/2031 TIME 09:42:13')
    assert extract_transaction_datetime('DATE 07/12/2031\nITEM 12.00\nTIME 09:42:13') == '2031-07-12'


@pytest.mark.parametrize('reverse', [False, True])
def test_competing_years_review_without_majority_or_engine_guess(reverse):
    readings = [observation('DATE 07/12/2091 TIME 09:42:13', i) for i in range(1, 7)]
    readings += [observation('DATE 07/12/2031\nRESP 001\nAUTH 12345\nTIME 09:42:13', 7)]
    if reverse:
        readings.reverse()
    result = summary_evidence.resolve('', readings)['transaction_datetime']
    assert result['value'] is None and result['status'] == 'conflict'
    assert {c['value'][:10] for c in result['candidates']} == {'2091-07-12', '2031-07-12'}
    assert {c['source']['pass_id'] for c in result['candidates']} == set(range(1, 8))


@pytest.mark.parametrize('reverse', [False, True])
def test_repeated_printed_date_resolves_single_glyph_year_error(reverse):
    readings = [observation('DATE 07/12/2091 TIME 09:42:13\nApproved\nStore Oper 07/12/31', i)
                for i in range(1, 7)]
    readings += [observation('DATE 07/12/2031\nRESP 001\nAUTH 12345\nTIME 09:42:13\nApproved\n07/12/31', 7)]
    if reverse:
        readings.reverse()
    result = summary_evidence.resolve('', readings)['transaction_datetime']
    assert result['value'] == '2031-07-12T09:42:13'
    assert result['status'] == 'selected'
    assert result['reason'] == 'full_year_supported_by_separately_printed_short_date'
    assert any(c['value'].startswith('2091') for c in result['candidates'])


@pytest.mark.parametrize('other', [
    'DATE 08/12/2091 TIME 09:42:13',
    'DATE 07/12/2099 TIME 09:42:13',
    'DATE 07/12/2091 TIME 09:44:13',
    'DATE 07/12/2091 TIME 09:42:13\nApproved\n07/12/91',
])
def test_repeated_date_does_not_resolve_different_day_multiple_glyphs_time_or_short_year_conflict(other):
    readings = [observation(other), observation('DATE 07/12/2031 TIME 09:42:13\nApproved\n07/12/31', 2)]
    assert summary_evidence.resolve('', readings)['transaction_datetime']['status'] == 'conflict'


def test_short_year_in_another_pass_is_not_independent_printed_corroboration():
    readings = [observation('DATE 07/12/2091'), observation('DATE 07/12/2031', 2),
                observation('07/12/31', 3)]
    assert summary_evidence.resolve('', readings)['transaction_datetime']['status'] == 'conflict'


@pytest.mark.parametrize('value,decision,updates', [(None, {}, False),
    (None, {'transaction_datetime': {'value': None, 'status': 'conflict'}}, True),
    ('2031-07-12T09:42:13', {'transaction_datetime': {'value': '2031-07-12T09:42:13', 'status': 'selected'}}, True)])
def test_canonical_datetime_replaces_stale_ocr_value_but_preserves_audited_correction(value, decision, updates):
    receipt = ingest.ScannedReceipt(path='', source_reference='', source_sha256='a' * 64,
        merchant='Example Shop', transaction_date=None, receipt_id=None, subtotal=None, tax=None, total=None,
        field_decisions=decision)
    receipt.transaction_datetime = value
    calls = []
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params): calls.append((sql, params))
    parallel_ingest._persist_transaction_datetime(Connection(), receipt, 'budget')
    assert bool(calls) is updates
    if updates:
        assert calls[0][1][0] == value
        assert "NOT (COALESCE(raw_payload,'{}'::jsonb) ? 'receipt_date_correction')" in calls[0][0]


def test_purchase_purpose_outranks_print_and_payment_dates_across_passes():
    readings = [observation('Printed 08/13/2031\nPayment date 07/13/2031', 1),
                observation('Purchase date 07/12/2031', 2)]
    decision = summary_evidence.resolve('', readings)['transaction_datetime']
    assert decision['value'] == '2031-07-12'
    assert decision['status'] == 'selected'


def test_single_day_with_conflicting_times_keeps_date_and_reviews_time():
    decisions = summary_evidence.resolve('', [observation('07/12/2031 09:42:13'),
        observation('07/12/2031 09:44:13', 2)])
    assert decisions['transaction_datetime']['value'] == '2031-07-12'
    assert decisions['transaction_datetime']['status'] == 'conflict'


def test_failed_pass_and_metadata_do_not_supply_date_or_card():
    readings = [observation('DATE 07/12/2031\nCARD MC\n*x\n24.60\nNO. ****7654')]
    bad = observation('DATE 07/12/2091\nVISA ****2222', 2)
    bad['status'] = 'failed'
    readings += [bad, observation('Printed 08/13/2031', 3)]
    decisions = summary_evidence.resolve('', readings)
    assert decisions['transaction_datetime']['value'] == '2031-07-12'
    assert decisions['card_last4']['value'] == '7654'
    assert decisions['card_last4']['candidates'][0]['line_numbers'] == [2, 3, 4, 5]


def test_flattened_engine_metadata_is_not_transaction_evidence():
    metadata = 'level page_num block_num par_num line_num word_num left top width height conf text\nDATE 07/12/2091\nVisa ****2222'
    decisions = summary_evidence.resolve('', [observation(metadata),
        observation('DATE 07/12/2031\nVisa ****7654', 2)])
    assert decisions['transaction_datetime']['value'] == '2031-07-12'
    assert decisions['card_last4']['value'] == '7654'


def test_conflicting_cards_never_choose_first():
    assert extract_payment_provenance('Visa ****7654\nVisa ****2222').card_last4 is None
    decision = summary_evidence.resolve('', [observation('Visa ****7654'),
                                             observation('Visa ****2222', 2)])['card_last4']
    assert decision['status'] == 'conflict' and decision['value'] is None


def test_cached_parser_uses_original_passes_and_keeps_source_text(tmp_path, monkeypatch):
    source = tmp_path / 'receipt.png'
    source.write_bytes(b'unchanged source')
    text = 'EXAMPLE SHOP\nDATE 07/12/2091\nPRODUCT 24.60\nTOTAL 24.60\nMastercard'
    readings = [observation('DATE 07/12/2091'), observation(
        'DATE 07/12/2031\nCARD MC\n*x\n24.60\nNO. ****7654', 2)]
    monkeypatch.setattr(parallel_ingest, '_read_or_create_pages', lambda *a, **kw: ([text], True))
    monkeypatch.setattr(parallel_ingest, '_ocr_run_metadata', lambda *a: {'ocr_passes': readings})
    monkeypatch.setattr(parallel_ingest, '_structured_layout', lambda *a: [])
    receipt, hit = parallel_ingest._parse_scan_cached(source, tmp_path, tmp_path / 'cache', False)
    assert hit and receipt.card_last4 == '7654'
    assert receipt.transaction_date is None and receipt.transaction_datetime is None
    assert 'conflicting_ocr_transaction_datetime' in receipt.review_reasons
    assert receipt.extraction_status == 'review'
    assert receipt.text == text and source.read_bytes() == b'unchanged source'
    assert receipt.field_decisions['transaction_datetime']['candidates'][1]['source']['pass_id'] == 2


def test_direct_parser_uses_same_field_evidence_as_cached_parser(tmp_path, monkeypatch):
    source = tmp_path / 'receipt.png'
    source.write_bytes(b'unchanged source')
    text = 'EXAMPLE SHOP\nDATE 07/12/2091\nPRODUCT 24.60\nTOTAL 24.60\nMastercard'
    readings = [observation('DATE 07/12/2031\nCARD MC\n*x\n24.60\nNO. ****7654')]
    def extract(path, metrics):
        metrics.extend(readings)
        return [text]
    monkeypatch.setattr(ingest, 'extract_page_text', extract)
    direct = ingest.parse_scan(source, tmp_path)
    monkeypatch.setattr(parallel_ingest, '_read_or_create_pages', lambda *a, **kw: ([text], True))
    monkeypatch.setattr(parallel_ingest, '_ocr_run_metadata', lambda *a: {'ocr_passes': readings})
    monkeypatch.setattr(parallel_ingest, '_structured_layout', lambda *a: [])
    cached, _ = parallel_ingest._parse_scan_cached(source, tmp_path, tmp_path / 'cache', False)
    assert direct.card_last4 == cached.card_last4 == '7654'
    assert direct.transaction_date == cached.transaction_date == '2031-07-12'
    assert direct.field_decisions == cached.field_decisions


def test_financial_persistence_keeps_cross_pass_card_instead_of_reparsing_consensus(monkeypatch):
    from home_budget_pipeline.receipts.payment import PaymentProvenance
    receipt = ingest.ScannedReceipt(path='', source_reference='receipt.png', source_sha256='a' * 64,
        merchant='EXAMPLE SHOP', transaction_date=None, receipt_id=None, subtotal=24.60, tax=0., total=24.60,
        text='EXAMPLE SHOP\nMastercard\nTOTAL 24.60', card_last4='7654',
        field_decisions={'card_last4': {'value': '7654', 'status': 'selected'}})
    seen = []
    monkeypatch.setattr(ingest, 'resolve_owner_from_db', lambda conn, payment, *a: seen.append(payment))
    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, *a): pass
        def fetchone(self): return (123,)
    assert ingest.upsert_receipt(Connection(), receipt) == 123
    assert seen == [PaymentProvenance('Mastercard', '7654')]
    assert receipt.card_last4 == '7654'
