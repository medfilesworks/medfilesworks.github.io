"""Offline safety integration tests using an isolated, fake Drive API.

These tests intentionally don't contact Google Drive or Supabase.
"""
from __future__ import annotations
import hashlib
import io
import re
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class MediaIoBaseUpload:
    def __init__(self, stream, mimetype, **kwargs):
        self.content=stream.getvalue()
        self.mimetype=mimetype


class MediaIoBaseDownload:
    def __init__(self, target, req, **kwargs):
        self.target=target
        self.req=req
    def next_chunk(self):
        self.target.write(self.req.content)
        return None, True


http=types.ModuleType('googleapiclient.http')
http.MediaIoBaseUpload=MediaIoBaseUpload
http.MediaIoBaseDownload=MediaIoBaseDownload
sys.modules.setdefault('googleapiclient',types.ModuleType('googleapiclient'))
sys.modules['googleapiclient.http']=http

FOLDER='application/vnd.google-apps.folder'
MAX_TRANSFER_BYTES=100*1024*1024


class Result:
    def __init__(self, data=None, content=None, fail=False):
        self.data=data
        self.content=content
        self.fail=fail
    def execute(self):
        if self.fail:
            raise OSError('simulated connection drop')
        return self.data


class FakeDrive:
    def __init__(self):
        self.items={}
        self.data={}
        self.n=0
        self.fail_after_replace=False
        self.add('root','root',FOLDER,b'',parents=[])
    def files(self):
        return self
    def add(self,name,item_id,mime,payload=b'',parents=None):
        item={'id':item_id,'name':name,'mimeType':mime,'parents':parents or [],
              'version':'1','size':str(len(payload)), 'capabilities':{'canEdit':True,'canDownload':True}}
        if mime!=FOLDER:
            item['md5Checksum']=hashlib.md5(payload).hexdigest()
        self.items[item_id]=item
        self.data[item_id]=payload
        return self.items[item_id]
    def get(self,fileId,**kwargs):
        return Result(dict(self.items[fileId]))
    def get_media(self,fileId,**kwargs):
        return Result(content=self.data[fileId])
    def list(self,q,**kwargs):
        parent_match=re.search(r"'([^']+)' in parents",q)
        parent=parent_match.group(1) if parent_match else None
        name_match=re.search(r"name = '([^']+)'",q)
        name=name_match.group(1) if name_match else None
        mime_match=re.search(r"mimeType = '([^']+)'",q)
        mime=mime_match.group(1) if mime_match else None
        filtered=[]
        for x in self.items.values():
            if parent is not None and parent not in x['parents']:
                continue
            if name is not None and name!=x['name']:
                continue
            if mime is not None and mime!=x['mimeType']:
                continue
            filtered.append(dict(x))
        return Result({'files':filtered,'nextPageToken':None})
    def create(self,body,media_body=None,**kwargs):
        self.n+=1
        file_id='file'+str(self.n).zfill(25)
        mime=body.get('mimeType','application/octet-stream')
        obj=self.add(body.get('name',''),file_id,mime,
                     media_body.content if media_body else b'',
                     body.get('parents',['root']))
        if 'appProperties' in body:
            obj['appProperties']=body['appProperties']
        return Result(dict(obj))
    def update(self,fileId,media_body,**kwargs):
        item=self.items[fileId]
        self.data[fileId]=media_body.content
        item['version']=str(int(item['version'])+1)
        item['size']=str(len(media_body.content))
        item['md5Checksum']=hashlib.md5(media_body.content).hexdigest()
        return Result(dict(item))


def get_metadata(api,file_id):
    return dict(api.items[file_id])


def ensure_folder(item):
    if item['mimeType']!=FOLDER:
        raise ValueError('not folder')


def ensure_binary_target(item):
    if item['mimeType'].startswith('application/vnd.google-apps.'):
        raise ValueError('native unsupported')


def fetch_source(api,item):
    return api.data[item['id']]


def same_revision(a,b):
    return str(a['version'])==str(b['version']) and a['name']==b['name'] and a['mimeType']==b['mimeType']


def replace_content(api,expected,payload):
    now=get_metadata(api,expected['id'])
    if not same_revision(expected,now):
        raise ValueError('revision changed')
    api.update(fileId=expected['id'],media_body=MediaIoBaseUpload(io.BytesIO(payload),now['mimeType']))
    if api.fail_after_replace:
        api.fail_after_replace=False
        raise OSError('simulated lost response after successful update')
    return get_metadata(api,expected['id'])


def is_descendant(api,a,b):
    node=a
    for _ in range(10):
        if node==b: return True
        parents=api.items[node]['parents']
        if not parents:return False
        node=parents[0]
    return False


def scan_tree(api,folder_id,recursive=True):
    result={}
    queue=[(folder_id,'')]
    while queue:
        current,prefix=queue.pop(0)
        for item in api.items.values():
            if current not in item['parents']:continue
            path=prefix+item['name']
            if item['mimeType']==FOLDER:
                if recursive:queue.append((item['id'],path+'/'))
            else:
                result[path]=dict(item)
    return result


def build_plan(targets,sources):
    ready,skipped=[],[]
    for path,item in sources.items():
        target=targets.get(path)
        if not target or target['mimeType']!=item['mimeType']:
            skipped.append({'path':path,'reason':'unmatched'})
        else:
            ready.append({'path':path,'target':target,'source':item})
    return ready,skipped


def file_url(item):
    return f"https://drive.google.com/file/d/{item['id']}/view"


drive_ops=types.ModuleType('drive_ops')
for key in ['FOLDER','MAX_TRANSFER_BYTES','build_plan','ensure_binary_target',
            'ensure_folder','fetch_source','file_url','get_metadata',
            'is_descendant','replace_content','same_revision','scan_tree']:
    setattr(drive_ops,key,globals()[key])
sys.modules['drive_ops']=drive_ops
import v3_engine as engine


def image_bytes():
    result=io.BytesIO()
    Image.new('RGB',(200,240),'white').save(result,format='PNG')
    return result.getvalue()


def sample():
    api=FakeDrive()
    old=api.add('old','old_folder',FOLDER,parents=['root'])
    new=api.add('new','new_folder',FOLDER,parents=['root'])
    target=api.add('intro.png','orig_file', 'image/png',image_bytes(),parents=[old['id']])
    source=api.add('intro.png','source_file','image/png',image_bytes(),parents=[new['id']])
    cfg={'mode':'text','text':'MEDFILESWORKS','opacity':0.25,
         'rotation':30,'scale':1.0,'placement':'center','color':'#45546B'}
    return api,old,new,target,source,cfg


def prepared(api,old,new,cfg):
    job=engine.create_job(api,new['id'],'marah.h.musallam21@gmail.com',cfg)
    manifest_id=job['manifest_id']
    assert engine.load_job(api,manifest_id)['job_id']==job['job_id']
    job=engine.stage_batch(api,job)
    assert job['status']=='stage_ready'
    job=engine.prepare_targets(api,job,old['id'])
    assert job['status']=='ready_to_update'
    if job['entries'][0]['state']=='ready':
        assert job['entries'][0]['target_id']=='orig_file'
    return job


def test_update_review_approve_rollback_preserves_file_id_and_content():
    api,old,new,target,source,cfg=sample()
    before=api.data['orig_file']
    job=prepared(api,old,new,cfg)
    job=engine.update_batch(api,job)
    assert job['status']=='pending_review'
    assert job['entries'][0]['state']=='updated'
    assert api.data['orig_file']!=before
    assert job['entries'][0]['backup_id']
    assert engine.load_job(api,job['manifest_id'])['status']=='pending_review'
    job=engine.approve_job(api,job)
    assert job['status']=='approved'
    job=engine.begin_rollback(api,job)
    job=engine.rollback_batch(api,job)
    assert job['status']=='rolled_back'
    assert api.data['orig_file']==before
    assert api.items['orig_file']['id']=='orig_file'


def test_rollback_rejects_unexpected_third_party_change():
    api,old,new,target,source,cfg=sample()
    job=engine.update_batch(api,prepared(api,old,new,cfg))
    api.update(fileId='orig_file',media_body=MediaIoBaseUpload(io.BytesIO(b'by another editor'),'image/png'))
    job=engine.begin_rollback(api,job)
    job=engine.rollback_batch(api,job)
    assert job['status']=='rollback_partial'
    assert job['entries'][0]['state']=='restore_conflict'
    assert api.data['orig_file']==b'by another editor'


def test_lost_update_response_can_still_restore():
    api,old,new,target,source,cfg=sample()
    original=api.data['orig_file']
    job=prepared(api,old,new,cfg)
    api.fail_after_replace=True
    job=engine.update_batch(api,job)
    assert job['entries'][0]['state']=='update_failed'
    assert api.data['orig_file']!=original
    job=engine.begin_rollback(api,job)
    assert job['entries'][0]['state']=='updated'
    job=engine.rollback_batch(api,job)
    assert api.data['orig_file']==original


def test_does_not_replace_unmatched_file():
    api,old,new,target,source,cfg=sample()
    api.items['source_file']['name']='new-only.png'
    before=api.data['orig_file']
    job=prepared(api,old,new,cfg)
    assert not any(e['state']=='ready' for e in job['entries'])
    job=engine.update_batch(api,job)
    assert api.data['orig_file']==before


def test_selected_rollback_restores_only_one_file():
    api,old,new,target,source,cfg=sample()
    api.add('second.png','old_two','image/png',image_bytes(),parents=[old['id']])
    api.add('second.png','new_two','image/png',image_bytes(),parents=[new['id']])
    first_before=api.data['orig_file']
    second_before=api.data['old_two']
    job=engine.create_job(api,new['id'],'marah.h.musallam21@gmail.com',cfg)
    job=engine.stage_batch(api,job)
    job=engine.prepare_targets(api,job,old['id'])
    job=engine.update_batch(api,job)
    assert job['status']=='pending_review'
    assert api.data['orig_file']!=first_before and api.data['old_two']!=second_before
    job=engine.begin_rollback(api,job,paths=['intro.png'])
    job=engine.rollback_batch(api,job)
    assert job['status']=='pending_review'
    assert api.data['orig_file']==first_before
    assert api.data['old_two']!=second_before
    assert {e['path']:e['state'] for e in job['entries']} == {
        'intro.png':'rolled_back','second.png':'updated'}
