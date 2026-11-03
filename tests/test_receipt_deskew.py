"""Synthetic scan geometry, independent of merchant, amount and source identity."""
import pytest
from PIL import Image, ImageDraw, ImageFont

from home_budget_pipeline.receipts.deskew import apply, estimate
from home_budget_pipeline.receipts import ingest


def printed_page():
    image=Image.new('RGB',(650,1000),'white')
    draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=18)
    for index in range(35):
        y=40+index*26
        draw.text((45,y),f'Product number {index} Sample text',fill='black',font=font)
        draw.text((490,y),f'${index+1}.25',fill='black',font=font)
    return image


@pytest.mark.parametrize('angle',[-8,-3.5,2.7,7])
def test_tilted_text_is_leveled_and_content_keeps_an_expanded_canvas(angle):
    page=printed_page().rotate(angle,expand=True,fillcolor='white')
    corrected,metadata=apply(page)
    assert metadata['status']=='rotated'
    assert abs(metadata['angle_degrees']+angle)<=.3
    assert corrected.width>=page.width and corrected.height>=page.height
    assert abs(estimate(corrected)['angle_degrees'])<=.2
    assert metadata['input_size']==list(page.size)
    assert len(metadata['source_to_deskew_canvas'])==6


def test_level_blank_and_isolated_rule_are_left_unchanged():
    rule=Image.new('RGB',(600,900),'white')
    ImageDraw.Draw(rule).line((10,200,570,230),fill='black',width=3)
    for page in (printed_page(),Image.new('RGB',(600,900),'white'),rule):
        corrected,metadata=apply(page)
        assert metadata['angle_degrees']==0
        assert corrected.tobytes()==page.tobytes()


def test_all_recognition_engines_variants_and_dpis_receive_deskewed_pages(tmp_path,monkeypatch):
    pages=[printed_page().rotate(angle,expand=True,fillcolor='white') for angle in (-4,3)]
    seen=[]
    def candidate(image,dpi,psm,engine='tesseract'):
        seen.append((engine,estimate(image)['angle_degrees']))
        return ingest.OCRCandidate('Product $2.25', (ingest.OCRLine('Product $2.25',95),),dpi,psm,engine)
    def tesseract(path,*,dpi,psm):
        with Image.open(path) as image:return candidate(image,dpi,psm)
    monkeypatch.setattr(ingest,'_run_tesseract_candidate',tesseract)
    monkeypatch.setattr(ingest,'_run_paddle_candidate',lambda image,*,dpi:candidate(image,dpi,'paddle','paddle'))
    monkeypatch.setattr(ingest,'_paddle_ocr_enabled',lambda:True)
    metrics=[]
    ingest._ocr_rendered_pages(lambda index,dpi:pages[index],2,metrics,run_uuid='synthetic')
    assert {'paddle','tesseract'}=={engine for engine,_ in seen}
    assert all(abs(angle)<=.2 for _,angle in seen)
    assert all(abs(m['provenance']['preprocessing']['deskew']['angle_degrees']-
                   {1:4,2:-3}[m['page_number']])<=.3 for m in metrics)
    assert {m['variant'] for m in metrics}=={'raw','enhanced','high_detail'}
    assert all('enhancement' in m['provenance']['preprocessing'] for m in metrics if m['variant']=='enhanced')


def test_layout_ocr_levels_each_tiff_frame_and_retains_its_transform(tmp_path,monkeypatch):
    source=tmp_path/'multipage.tif'
    images=[printed_page().rotate(angle,expand=True,fillcolor='white') for angle in (-4,3)]
    images[0].save(source,save_all=True,append_images=images[1:])
    checked=[]
    def candidate(path,**kwargs):
        with Image.open(path) as image:checked.append(estimate(image)['angle_degrees'])
        return ingest.OCRCandidate('Product $2.25',(ingest.OCRLine('Product $2.25',95),),300,'6')
    monkeypatch.setattr(ingest,'_run_tesseract_candidate',candidate)
    result=ingest.extract_page_layout(source,['Product $2.25']*2)
    assert len(result)==len(checked)==2
    assert all(abs(angle)<=.2 for angle in checked)
    assert [p['page_number'] for p in result.preprocessing]==[1,2]
    assert abs(result.preprocessing[0]['deskew']['angle_degrees']-4)<=.3
    assert abs(result.preprocessing[1]['deskew']['angle_degrees']+3)<=.3
