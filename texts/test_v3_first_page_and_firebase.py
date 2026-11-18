import io
import sys
from pathlib import Path

import fitz
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import v3_watermark
from v3_store import firebase_is_configured, sync_job, read_jobs

CFG = {'mode':'text','text':'medfilesworks','opacity':0.22,
       'rotation':30,'scale':1.0,'placement':'center','color':'#45546B'}


def test_first_page_preview_only_one_page(monkeypatch):
    doc = fitz.open()
    for page_index in range(8):
        doc.new_page().insert_text((44,80),f'PAGE {page_index+1}')
    content = doc.tobytes()
    doc.close()
    pages_seen=[]
    original = v3_watermark.watermark_bytes

    def spy(data,mime,config,logo=None):
        sub = fitz.open(stream=data,filetype='pdf')
        pages_seen.append(sub.page_count)
        sub.close()
        return original(data,mime,config,logo)

    monkeypatch.setattr(v3_watermark,'watermark_bytes',spy)
    before,after = v3_watermark.preview_first_page_pair(content,'application/pdf',CFG)
    assert before.startswith(b'\x89PNG')
    assert after.startswith(b'\x89PNG')
    assert pages_seen == [1]


def test_normalize_uploaded_jpeg_watermark():
    source = io.BytesIO()
    Image.new('RGB',(185,79),(40,100,220)).save(source,format='JPEG')
    normalized=v3_watermark.normalize_logo_bytes(source.getvalue())
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format=='PNG'
        assert image.size==(185,79)
        assert image.mode=='RGBA'
    before,after=v3_watermark.preview_first_page_pair(
        source.getvalue(),'image/jpeg',{'mode':'logo',**{k:v for k,v in CFG.items() if k!='mode'}},normalized)
    assert before.startswith(b'\x89PNG') and after.startswith(b'\x89PNG')


def test_firebase_requires_private_key():
    assert not firebase_is_configured({'project_id':'p','client_email':'x@example.com'})
    assert firebase_is_configured({'project_id':'p','client_email':'x@example.com',
                                   'private_key':'some-value'})


class FakeCollection:
    def __init__(self):
        self.row={}
    def document(self,key):
        self.id=key
        return self
    def set(self,row,merge):
        assert merge is True
        self.row=row
    def order_by(self,*args,**kwargs):
        return self
    def where(self,*args,**kwargs):
        self.search_field=args
        return self
    def limit(self,*args):
        return self
    def stream(self):
        return [FakeSnapshot(self.row)] if self.row else []


class FakeSnapshot:
    def __init__(self,row):self.row=row
    def to_dict(self):return self.row


class FakeDatabase:
    def __init__(self):self.collections={}
    def collection(self,name):
        self.collections.setdefault(name,FakeCollection())
        return self.collections[name]


def test_firestore_job_index_from_manifest_without_file_bytes():
    manifest = {
        'job_id':'abc123','actor':'editor@example.com','manifest_id':'FILE123',
        'source_folder':'SRC','target_folder':'DST',
        'status':'pending_review','created_at':'2026-11-18T09:00:00Z',
        'updated_at':'2026-11-18T10:00:00Z','approved_at':None,
        'entries':[{'state':'updated','path':'a.pdf'},{'state':'stage_skipped','path':'b.docx'}],
    }
    db=FakeDatabase()
    ok,message=sync_job(manifest,{},client=db)
    assert ok and message==''
    assert db.collections['mfw_v3_jobs'].row['updated_files']==1
    assert db.collections['mfw_v3_jobs'].row['skipped_files']==1
    rows=read_jobs({},actor='editor@example.com',client=db)
    assert len(rows)==1
    assert rows[0]['manifest_file_id']=='FILE123'
    assert rows[0]['actor_email']=='editor@example.com'
