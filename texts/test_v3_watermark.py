import io
import sys
from pathlib import Path

import fitz
import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from v3_watermark import watermark_bytes,preview_png,validate_config

CFG={'mode':'text','text':'medfilesworks','opacity':0.23,
     'rotation':25,'scale':1,'placement':'center','color':'#303B50'}


def source_image(kind='PNG'):
    result=io.BytesIO()
    Image.new('RGB',(650,820),(245,245,245)).save(result,format=kind)
    return result.getvalue()


def test_pdf_preserves_pages_and_selectable_text():
    pdf=fitz.open()
    for idx in range(3):
        pdf.new_page().insert_text((64,80),f'Original page {idx}')
    original=pdf.tobytes()
    pdf.close()
    modified=watermark_bytes(original,'application/pdf',CFG)
    doc=fitz.open(stream=modified,filetype='pdf')
    assert doc.page_count==3
    assert all(f'Original page {idx}' in doc[idx].get_text() for idx in range(3))
    assert len(preview_png(modified,'application/pdf'))>100
    doc.close()


@pytest.mark.parametrize('mime,format',[
    ('image/png','PNG'),('image/jpeg','JPEG'),('image/webp','WEBP')
])
def test_image_formats(mime,format):
    original=source_image(format)
    watermarked=watermark_bytes(original,mime,CFG)
    image=Image.open(io.BytesIO(watermarked))
    assert image.size==(650,820)
    assert image.format==format


def test_text_and_logo_and_repeating():
    logo=io.BytesIO()
    Image.new('RGBA',(120,70),(0,90,150,220)).save(logo,format='PNG')
    cfg={**CFG,'mode':'both','placement':'repeated'}
    rendered=watermark_bytes(source_image(),'image/png',cfg,logo.getvalue())
    assert len(rendered)>1000


def test_rejects_unsupported_and_empty_text():
    with pytest.raises(ValueError):
        watermark_bytes(b'bytes','application/vnd.google-apps.document',CFG)
    with pytest.raises(ValueError):
        validate_config({**CFG,'text':''})
