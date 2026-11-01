import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from home_budget_pipeline import cli
from home_budget_pipeline.receipts import cached_reprocess as bulk, ingest, queue_ingest


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    scanned = tmp_path / 'scanned'
    electronic = tmp_path / 'electronic'
    cache = tmp_path / 'cache'
    for root in (scanned, electronic, cache): root.mkdir()
    monkeypatch.setenv('RECEIPT_SOURCE_ROOT', str(scanned))
    monkeypatch.setenv('ELECTRONIC_RECEIPT_SOURCE_ROOT', str(electronic))
    def add(reference, state='valid'):
        source = electronic / reference[11:] if reference.startswith('electronic/') else scanned / reference
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(reference.encode())
        p = cache / (reference + '.json')
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {'metadata': {'source_sha256': ingest.sha256_file(source), 'cache_version':1,
                   'run_uuid':'11111111-1111-4111-8111-111111111111','ocr_passes':[]},
                   'page_text':['EXAMPLE MART\n2027-07-09\nPRODUCT 4.00\nTOTAL 4.00']}
        if state == 'mismatch': payload['metadata']['source_sha256'] = 'f'*64
        if state == 'missing_run': payload['metadata'].pop('run_uuid')
        if state != 'missing': p.write_text('{' if state == 'invalid' else json.dumps(payload))
        if state == 'deleted': source.unlink()
        return p
    def args(*flags):
        return cli.build_parser().parse_args(['receipts','reprocess-cache',
            '--receipt-root',str(scanned),'--ocr-cache',str(cache),
            '--rabbitmq-url','amqp://secret@broker',*flags])
    monkeypatch.setattr(ingest, 'extract_page_text', lambda *a, **kw: pytest.fail('Must not run OCR'))
    return add, args


def test_preview_covers_both_roots_and_never_connects_or_changes_cache(inputs, monkeypatch, capsys):
    add, args = inputs
    paths = [add('2027-07-09/a.pdf'), add('electronic/2027-07-10/b.pdf')]
    add('missing.pdf','missing')
    add('electronic/deleted.pdf','deleted')
    original = [p.read_bytes() for p in paths]
    monkeypatch.setattr(queue_ingest,'connect_broker',lambda *a: pytest.fail('Preview must not connect'))
    assert bulk.run(args()) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out['selected'],out['eligible'],out['published'],out['missing_cache']) == (3,2,0,1)
    assert {r['source_reference'] for r in out['receipts']} == {'2027-07-09/a.pdf','electronic/2027-07-10/b.pdf'}
    assert [p.read_bytes() for p in paths] == original


def test_apply_uses_one_connection_and_stable_per_source_retry_ids(inputs, monkeypatch, capsys):
    add, args = inputs
    add('a.pdf');add('electronic/b.pdf')
    published, connections, closed = [], [], []
    def connect(url):
        connections.append(url)
        return SimpleNamespace(is_open=True,channel=lambda:object(),close=lambda:closed.append(True))
    monkeypatch.setattr(queue_ingest,'connect_broker',connect)
    monkeypatch.setattr(bulk,'declare_result_topology',lambda *a:None)
    monkeypatch.setattr(bulk,'publish_result',lambda channel, exchange, event:published.append(event))
    request = '22222222-2222-4222-8222-222222222222'
    for _ in range(2):
        assert bulk.run(args('--apply','--request-id',request)) == 0
        out = json.loads(capsys.readouterr().out)
        assert out['published'] == 2
        assert out['ocr_executed'] is False
    assert len(connections) == len(closed) == 2
    assert [e.payload['request_id'] for e in published[:2]] == [e.payload['request_id'] for e in published[2:]]
    assert len({e.payload['request_id'] for e in published}) == 2
    assert all(e.payload['persistence_mode'] == 'reprocess' for e in published)


@pytest.mark.parametrize('state',['invalid','mismatch','missing_run'])
def test_invalid_caches_report_failure_without_ocr(inputs, state, capsys):
    add,args = inputs
    add('bad.pdf',state)
    assert bulk.run(args()) == 1
    out=json.loads(capsys.readouterr().out)
    assert out['eligible'] == out['published'] == 0
    assert out['failed'][0]['source_reference'] == 'bad.pdf'


def test_prefix_selects_whole_folders(inputs,capsys):
    add,args=inputs
    add('2027-07-09/a.pdf');add('2027-07-090/b.pdf');add('electronic/c.pdf')
    assert bulk.run(args('--source-prefix','2027-07-09')) == 0
    out=json.loads(capsys.readouterr().out)
    assert [r['source_reference'] for r in out['receipts']] == ['2027-07-09/a.pdf']


@pytest.mark.parametrize('flag,value',[('--source-prefix','../'),('--source-prefix','/absolute'),('--request-id','invalid')])
def test_invalid_selection_is_rejected_before_discovery(inputs,monkeypatch,capsys,flag,value):
    _,args=inputs
    monkeypatch.setattr(bulk,'discover_sources',lambda *a:pytest.fail('Invalid selection must fail first'))
    assert bulk.run(args(flag,value)) == 1
    assert 'error' in json.loads(capsys.readouterr().out)


def test_uncertain_publish_stops_and_returns_retry_batch_id(inputs,monkeypatch,capsys):
    add,args=inputs
    add('a.pdf');add('b.pdf')
    connection=SimpleNamespace(is_open=True,channel=lambda:object(),close=lambda:None)
    monkeypatch.setattr(queue_ingest,'connect_broker',lambda _:connection)
    monkeypatch.setattr(bulk,'declare_result_topology',lambda *a:None)
    def fail(*a): raise RuntimeError('amqp://private:password@broker')
    monkeypatch.setattr(bulk,'publish_result',fail)
    assert bulk.run(args('--apply')) == 1
    out=json.loads(capsys.readouterr().out)
    assert out['published'] == 0 and out['error'] == 'RuntimeError'
    assert out['request_id']
    assert 'password' not in json.dumps(out)
