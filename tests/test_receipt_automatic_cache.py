import json
from uuid import uuid4
import pytest
from home_budget_pipeline.receipts.message import ReceiptMessage,InvalidReceiptMessage
from home_budget_pipeline.receipts import parallel_ingest as parallel


def test_auto_request_roundtrip_and_legacy_messages_keep_fresh_semantics():
    values=dict(source_sha256='a'*64,source_reference='synthetic.pdf',version=2,request_id=str(uuid4()))
    fresh=ReceiptMessage(**values)
    assert b'ocr_mode' not in fresh.to_bytes()
    assert ReceiptMessage.from_bytes(fresh.to_bytes()).ocr_mode=='fresh'
    auto=ReceiptMessage(**values,ocr_mode='auto')
    assert ReceiptMessage.from_bytes(auto.to_bytes())==auto
    with pytest.raises(InvalidReceiptMessage):ReceiptMessage(**values,ocr_mode='unknown')
    with pytest.raises(InvalidReceiptMessage):ReceiptMessage('a'*64,'synthetic.pdf',ocr_mode='auto')


@pytest.mark.parametrize('kind',['valid','missing','invalid','wrong_source','blank','bad_version','wrong_shape','legacy'])
def test_automatic_cache_validates_before_reuse_and_falls_back_to_ocr(tmp_path,monkeypatch,kind):
    source=tmp_path/'source.pdf';source.write_bytes(b'synthetic')
    cache=tmp_path/'cache';cache.mkdir()
    sha=parallel.scan.sha256_file(source)
    path=cache/('source.pdf.json' if kind!='legacy' else sha+'.json')
    data={'metadata':{'cache_version':3,'source_sha256':sha},'pages':[{'page_number':1,'lines':['Example shop','TOTAL $5.20']} ]}
    if kind=='wrong_source':data['metadata']['source_sha256']='b'*64
    if kind=='blank':data['pages'][0]['lines']=[]
    if kind=='bad_version':data['metadata']['cache_version']='broken'
    if kind=='wrong_shape':data=[]
    if kind!='missing':path.write_text('broken JSON' if kind=='invalid' else json.dumps(data))
    calls=[]
    def ocr(*args,**kwargs):calls.append(True);return ['Example shop\nTOTAL $5.20']
    monkeypatch.setattr(parallel.scan,'extract_page_text',ocr)
    monkeypatch.setattr(parallel.scan,'extract_page_layout',lambda *a:[])
    _,hit=parallel._read_or_create_pages(source,sha,cache,False,source.name)
    assert hit is (kind in ('valid','legacy'))
    assert bool(calls) is (not hit)
    if kind=='legacy':assert path.exists() and (cache/'source.pdf.json').read_bytes()==path.read_bytes()
