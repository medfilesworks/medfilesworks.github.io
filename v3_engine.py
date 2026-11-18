"""Drive-backed, restart-safe watermark -> update -> review -> rollback workflow.

The original Drive file IDs are never replaced or deleted. Staged and backup
objects live in a separate recovery folder. Every state transition is saved
in a Drive JSON manifest for restart recovery without relying on Streamlit.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timezone

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from drive_ops import (
    FOLDER, MAX_TRANSFER_BYTES, build_plan, ensure_binary_target, ensure_folder,
    fetch_source, file_url, get_metadata, is_descendant, replace_content,
    same_revision, scan_tree,
)
from v3_watermark import SUPPORTED_MIMES, normalize_logo_bytes, validate_config, watermark_bytes

ROOT_NAME = 'medfilesworks_V3_recovery'
MANIFEST_NAME = 'v3_manifest.json'
RECOVERY_STATES = {'updated', 'restore_failed', 'restore_conflict'}
END_STATES = {'staged', 'stage_skipped', 'stage_failed'}
MAX_EVENTS = 100


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def checksum(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def _escape(value: str) -> str:
    return value.replace('\\', '\\\\').replace("'", "\\'")


def _list(api, query: str, fields='id,name,mimeType,createdTime,parents,webViewLink,appProperties', limit=100):
    result, token = [], None
    while len(result) < limit:
        response = api.files().list(q=query, fields=f'nextPageToken,files({fields})',
                                    pageSize=min(limit-len(result), 1000),pageToken=token,
                                    supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
        result.extend(response.get('files', []))
        token = response.get('nextPageToken')
        if not token:
            break
    return result


def _folder(api, name: str, parent: str | None = None, properties: dict | None = None):
    body = {'name':name, 'mimeType':FOLDER}
    if parent:
        body['parents'] = [parent]
    if properties:
        body['appProperties'] = properties
    return api.files().create(body=body, fields='id,name,mimeType,webViewLink',
                              supportsAllDrives=True).execute()


def recovery_root(api, configured_root: str | None = None):
    if configured_root:
        item = get_metadata(api, configured_root)
        ensure_folder(item)
        if item.get('capabilities', {}).get('canEdit') is False:
            raise PermissionError('لا توجد صلاحية تحرير مجلد النسخ الاحتياطية')
        return item['id']
    q = f"name = '{ROOT_NAME}' and mimeType = '{FOLDER}' and trashed = false and 'root' in parents"
    found = _list(api, q, limit=20)
    if len(found) > 1:
        raise ValueError('يوجد أكثر من مجلد استعادة بالاسم نفسه؛ عيّني BACKUP_ROOT_FOLDER_ID')
    if found:
        return found[0]['id']
    return _folder(api, ROOT_NAME, properties={'mfw_kind':'recovery_root'})['id']


def _upload(api, parent_id: str, name: str, payload: bytes,
            mime_type: str, properties: dict | None = None) -> dict:
    if not payload:
        raise ValueError('لا يمكن رفع ملف فارغ')
    if len(payload) > MAX_TRANSFER_BYTES:
        raise ValueError('يتجاوز الملف الحد الأقصى 100 MB')
    body = {'name':name, 'mimeType':mime_type, 'parents':[parent_id]}
    if properties:
        body['appProperties'] = properties
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype=mime_type,
                             resumable=True, chunksize=4*1024*1024)
    item = api.files().create(body=body,media_body=media,
                              fields='id,name,mimeType,size,md5Checksum,version,webViewLink',
                              supportsAllDrives=True).execute()
    remote_hash = item.get('md5Checksum')
    if remote_hash and remote_hash != checksum(payload):
        raise RuntimeError('فشل التحقق من سلامة النسخة المرفوعة')
    return item


def _read(api, file_id: str, max_size=MAX_TRANSFER_BYTES) -> bytes:
    req = api.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req, chunksize=4*1024*1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
        if buf.tell() > max_size:
            raise ValueError('ملف يتجاوز الحد المسموح أثناء التنزيل')
    return buf.getvalue()


def _save(api, manifest: dict) -> dict:
    """Save manifest after EVERY completed file, including backup-before-update."""
    manifest['updated_at'] = utc_now()
    manifest['revision'] = int(manifest.get('revision', 0)) + 1
    data = json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('سجل العملية أكبر من الحد الآمن')
    if manifest.get('manifest_id'):
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype='application/json', resumable=False)
        api.files().update(fileId=manifest['manifest_id'], media_body=media,
                           fields='id,version',supportsAllDrives=True).execute()
    else:
        created = _upload(api, manifest['folder_id'], MANIFEST_NAME, data,
                          'application/json', {'mfw_kind':'v3_manifest', 'job_id':manifest['job_id']})
        manifest['manifest_id'] = created['id']
        # Commit manifest_id in the next write so the manifest is self-contained.
        return _save(api, manifest)
    return manifest


def load_job(api, manifest_id: str) -> dict:
    raw = _read(api, manifest_id, max_size=8*1024*1024)
    manifest = json.loads(raw.decode('utf-8'))
    if manifest.get('format') != 'medfilesworks_v3' or manifest.get('manifest_id') != manifest_id:
        raise ValueError('ملف الجلسة غير صالح')
    return manifest


def ensure_actor(manifest: dict, email: str, is_owner: bool = False):
    # Owner may review but must still have Drive read/edit permissions.
    if manifest.get('actor', '').lower() != email.lower() and not is_owner:
        raise PermissionError('هذه العملية تخص حساب محرر آخر')


def event(manifest: dict, kind: str, path: str = '', detail: str = ''):
    events = manifest.setdefault('events', [])
    events.append({'at':utc_now(),'kind':kind,'path':path,'detail':str(detail)[:250]})
    if len(events) > MAX_EVENTS:
        del events[:-MAX_EVENTS]


def create_job(api, source_folder: str, actor: str, config: dict,
               logo_bytes: bytes | None = None, recursive: bool = True,
               configured_root: str | None = None) -> dict:
    item = get_metadata(api, source_folder)
    ensure_folder(item)
    cfg = validate_config(config, logo_bytes)
    source_files = scan_tree(api, source_folder, recursive=recursive)
    if not source_files:
        raise ValueError('المجلد الجديد لا يحتوي ملفات')
    job_id = uuid.uuid4().hex[:16]
    root = recovery_root(api, configured_root)
    folder = _folder(api, 'MFW_V3_' + job_id, parent=root,
                     properties={'mfw_kind':'v3_job','job_id':job_id})
    if logo_bytes:
        logo_bytes = normalize_logo_bytes(logo_bytes)
        logo = _upload(api, folder['id'], 'watermark_logo.png', logo_bytes,
                       'image/png', {'mfw_kind':'watermark_logo'})
        cfg['logo_file_id'] = logo['id']
    entries = []
    for path, src in sorted(source_files.items()):
        reason = ''
        if src['mimeType'] not in SUPPORTED_MIMES:
            reason = 'نوع غير مدعوم للعلامة المائية'
        elif src.get('capabilities', {}).get('canDownload') is False:
            reason = 'لا توجد صلاحية تنزيل'
        elif int(src.get('size', 0)) > MAX_TRANSFER_BYTES:
            reason = 'أكبر من 100 MB'
        entries.append({
            'path':path, 'source_id':src['id'], 'source_name':src['name'],
            'mime_type':src['mimeType'], 'source_version':src.get('version'),
            'state':'stage_skipped' if reason else 'new',
            'error':reason,
        })
    manifest = {
        'format':'medfilesworks_v3', 'job_id':job_id,
        'actor':actor.lower(), 'created_at':utc_now(), 'updated_at':utc_now(),
        'revision':0, 'folder_id':folder['id'], 'root_id':root,
        'manifest_id':None, 'source_folder':source_folder,
        'target_folder':None, 'recursive':recursive,
        'status':'staging', 'config':cfg, 'entries':entries,
        'events':[], 'approved_at':None,
    }
    event(manifest, 'job_created', detail=f'{len(entries)} file(s)')
    return _save(api,manifest)


def _load_logo(api, manifest):
    logo_id = manifest['config'].get('logo_file_id')
    return _read(api, logo_id, max_size=5*1024*1024) if logo_id else None


def stage_batch(api, manifest: dict, limit=5, progress=None) -> dict:
    if manifest['status'] not in ('staging', 'stage_ready'):
        raise ValueError('مرحلة العلامة المائية مغلقة')
    logo_bytes = _load_logo(api, manifest)
    pending = [e for e in manifest['entries'] if e['state'] == 'new']
    if pending:
        manifest['status'] = 'staging'
        _save(api, manifest)
    for n, entry in enumerate(pending[:limit], start=1):
        try:
            latest = get_metadata(api, entry['source_id'])
            if (str(latest.get('version', '')) != str(entry.get('source_version', ''))
                    or latest['name'] != entry['source_name']
                    or latest['mimeType'] != entry['mime_type']):
                raise ValueError('تغير المصدر منذ بدء العملية')
            payload = fetch_source(api, latest)
            prepared = watermark_bytes(payload, latest['mimeType'], manifest['config'], logo_bytes)
            suffix = entry['source_name'].rsplit('.', 1)[-1].lower()
            filename = f"stage_{hashlib.sha256(entry['path'].encode()).hexdigest()[:16]}.{suffix}"
            staged = _upload(api, manifest['folder_id'], filename, prepared,
                             latest['mimeType'], {'mfw_kind':'staged','job_id':manifest['job_id']})
            entry.update(state='staged', stage_id=staged['id'], stage_md5=checksum(prepared),
                         stage_size=len(prepared), error='')
            event(manifest,'stage_ok',entry['path'])
        except Exception as exc:
            entry.update(state='stage_failed',error=str(exc)[:350])
            event(manifest,'stage_failed',entry['path'],str(exc))
        _save(api, manifest)
        if progress:
            progress(n, min(limit, len(pending)))
    if all(e['state'] in END_STATES and e['state'] != 'stage_failed' for e in manifest['entries']):
        manifest['status'] = 'stage_ready'
        _save(api, manifest)
    return manifest


def stage_retries(api, manifest: dict, paths: list[str] | None = None):
    for entry in manifest['entries']:
        if entry['state'] == 'stage_failed' and (paths is None or entry['path'] in paths):
            entry['state'] = 'new'
            entry['error'] = ''
    _save(api, manifest)
    return manifest


def prepare_targets(api, manifest: dict, target_folder: str) -> dict:
    if manifest['status'] != 'stage_ready':
        raise ValueError('يجب إكمال وتجربة العلامات المائية أولًا')
    if target_folder == manifest['source_folder']:
        raise ValueError('المجلد القديم والجديد لا يمكن أن يكونا نفس المجلد')
    ensure_folder(get_metadata(api,target_folder))
    if (is_descendant(api, manifest['source_folder'], target_folder)
            or is_descendant(api, target_folder, manifest['source_folder'])):
        raise ValueError('المجلدان متداخلان؛ اختاري مجلدين مستقلين')
    targets = scan_tree(api,target_folder,recursive=manifest['recursive'])
    staged = {}
    for entry in manifest['entries']:
        if entry['state'] == 'staged':
            staged[entry['path']] = get_metadata(api,entry['stage_id'])
    ready, skipped = build_plan(targets, staged)
    ready_map = {x['path']:x for x in ready}
    skipped_map = {x['path']:x['reason'] for x in skipped}
    for entry in manifest['entries']:
        path=entry['path']
        if path in ready_map:
            target = ready_map[path]['target']
            entry.update(state='ready',target_id=target['id'],
                         target_name=target['name'],target_version=target.get('version'),
                         target_md5=target.get('md5Checksum'),error='')
        elif entry['state'] == 'staged':
            entry.update(state='match_skipped',error=skipped_map.get(path,'غير مطابق'))
    manifest['target_folder']=target_folder
    manifest['status']='ready_to_update'
    event(manifest,'match_complete',detail=f'{len(ready)} matched; {len(skipped)} skipped')
    return _save(api,manifest)


def _expected_target(entry: dict) -> dict:
    return {'id':entry['target_id'], 'name':entry['target_name'],
            'mimeType':entry['mime_type'], 'version':entry['target_version']}


def _check_staged(api,entry: dict) -> bytes:
    stage = get_metadata(api, entry['stage_id'])
    if stage['mimeType'] != entry['mime_type'] or stage.get('md5Checksum') != entry['stage_md5']:
        raise ValueError('الملف المجهز تغير أو فشل التحقق من سلامته')
    data = fetch_source(api,stage)
    if checksum(data) != entry['stage_md5']:
        raise ValueError('الملف المجهز لا يطابق النسخة المعتمدة')
    return data


def update_batch(api, manifest: dict, limit=5, retry_failed=False, progress=None) -> dict:
    if manifest['status'] not in ('ready_to_update','updating','pending_review'):
        raise ValueError('عملية التحديث غير جاهزة')
    allowed_states = ('ready','backed_up','update_failed') if retry_failed else ('ready','backed_up')
    pending = [e for e in manifest['entries'] if e['state'] in allowed_states]
    manifest['status']='updating'
    _save(api,manifest)
    for n, entry in enumerate(pending[:limit],start=1):
        try:
            now = get_metadata(api,entry['target_id'])
            ensure_binary_target(now)
            if now['mimeType'] != entry['mime_type']:
                raise ValueError('نوع الملف الأصلي تغير')
            # Recovery after a power loss between files.update and manifest save.
            if entry.get('backup_id') and now.get('md5Checksum') == entry['stage_md5']:
                entry.update(state='updated',updated_md5=now.get('md5Checksum'),
                             updated_version=now.get('version'),error='')
            else:
                if entry['state'] == 'ready' or (entry['state']=='update_failed' and not entry.get('backup_id')):
                    if str(now.get('version','')) != str(entry['target_version']):
                        raise ValueError('الملف الأصلي تغير بعد المطابقة؛ أوقفي العملية وراجعي الملف')
                    if now.get('md5Checksum') == entry['stage_md5']:
                        entry.update(state='unchanged',error='')
                        _save(api, manifest)
                        continue
                    original = fetch_source(api,now)
                    original_md5 = checksum(original)
                    backup_name = f"backup_{entry['target_id']}_{entry['target_name']}"
                    backup = _upload(api,manifest['folder_id'],backup_name,original,
                                     now['mimeType'],{'mfw_kind':'backup',
                                                      'target_id':entry['target_id'],
                                                      'job_id':manifest['job_id']})
                    backup_id = backup['id']
                    backup_meta = get_metadata(api, backup_id)
                    if backup_meta.get('md5Checksum') != original_md5:
                        raise RuntimeError('فشل التحقق من النسخة الاحتياطية؛ لم يُستبدل الأصل')
                    entry.update(state='backed_up',backup_id=backup_id,
                                 backup_md5=original_md5,error='')
                    event(manifest,'backup_ok',entry['path'])
                    _save(api,manifest)
                # The backup has been durably recorded before any update occurs.
                now = get_metadata(api,entry['target_id'])
                if str(now.get('version','')) != str(entry['target_version']):
                    raise ValueError('الملف الأصلي تغير قبل التنفيذ؛ لا يمكن الاستبدال')
                prepared = _check_staged(api,entry)
                replace_content(api,_expected_target(entry),prepared)
                newest = get_metadata(api,entry['target_id'])
                if newest.get('md5Checksum') != entry['stage_md5']:
                    raise RuntimeError('لم تنجح مطابقة محتوى الملف بعد التحديث؛ تحتاج العملية مراجعة')
                entry.update(state='updated',updated_md5=newest.get('md5Checksum'),
                             updated_version=newest.get('version'),error='')
            event(manifest,'update_ok',entry['path'])
        except Exception as exc:
            # Do not discard backup data for a failed file.
            entry.update(state='update_failed',error=str(exc)[:400])
            event(manifest,'update_failed',entry['path'],str(exc))
        _save(api,manifest)
        if progress:
            progress(n,min(limit,len(pending)))
    if not any(e['state'] in ('ready','backed_up') for e in manifest['entries']):
        manifest['status']='pending_review'
        event(manifest,'review_ready')
        _save(api,manifest)
    return manifest


def approve_job(api,manifest:dict, allow_partial=False) -> dict:
    if manifest['status'] != 'pending_review':
        raise ValueError('يجب إنهاء التحديث ومراجعته قبل الاعتماد')
    failures = [x for x in manifest['entries'] if x['state']=='update_failed']
    if failures and not allow_partial:
        raise ValueError('هناك ملفات فشلت؛ راجعي النتائج أو اعتمدي الجزئي صراحة')
    manifest['status']='approved'
    manifest['approved_at']=utc_now()
    event(manifest,'approved',detail='partial' if failures else 'complete')
    return _save(api,manifest)


def begin_rollback(api, manifest: dict, paths: list[str] | None = None) -> dict:
    if manifest['status'] not in ('pending_review', 'updating', 'approved',
                                  'rollback_partial', 'rolling_back'):
        raise ValueError('Rollback is unavailable in the current job state')
    if manifest['status'] in ('rollback_partial', 'rolling_back'):
        # Resume the previously selected scope; never silently widen it.
        return manifest
    eligible = [e for e in manifest['entries']
                if e.get('backup_id') and e['state'] in
                ('updated', 'backed_up', 'update_failed', 'restore_failed', 'restore_conflict')]
    known_paths = {e['path'] for e in eligible}
    selected = set(paths) if paths is not None else known_paths
    if not selected or not selected.issubset(known_paths):
        raise ValueError('Choose valid updated files for rollback')
    for entry in manifest['entries']:
        entry['rollback_requested'] = entry['path'] in selected
    # A lost upload response may hide an update that succeeded remotely.
    for entry in eligible:
        if not entry['rollback_requested']:
            continue
        if entry['state'] in ('backed_up', 'update_failed') and entry.get('backup_id'):
            current = get_metadata(api, entry['target_id'])
            current_hash = current.get('md5Checksum')
            if current_hash == entry['stage_md5']:
                entry['state'] = 'updated'
            elif current_hash == entry['backup_md5']:
                entry['state'] = 'not_updated'
            else:
                entry['state'] = 'restore_conflict'
                entry['error'] = 'Unexpected content detected; manual review required'
    manifest['status'] = 'rolling_back'
    event(manifest, 'rollback_started', detail=f'{len(selected)} selected')
    return _save(api, manifest)


def rollback_batch(api,manifest:dict,limit=5,progress=None) -> dict:
    if manifest['status'] not in ('rolling_back','rollback_partial'):
        raise ValueError('يجب بدء عملية الاستعادة أولًا')
    candidates = [e for e in manifest['entries']
                  if e['state'] in RECOVERY_STATES and e.get('rollback_requested', True)]
    for n,entry in enumerate(candidates[:limit],start=1):
        try:
            if not entry.get('backup_id'):
                raise ValueError('لا توجد نسخة احتياطية موثقة')
            current = get_metadata(api,entry['target_id'])
            ensure_binary_target(current)
            if current.get('md5Checksum') == entry['backup_md5']:
                entry.update(state='rolled_back',error='')
            elif current.get('md5Checksum') != entry['stage_md5']:
                raise ValueError('تغير الملف بعد التحديث؛ الاستعادة التلقائية ممنوعة لحماية التعديلات الجديدة')
            else:
                backup = get_metadata(api,entry['backup_id'])
                original = fetch_source(api,backup)
                if checksum(original) != entry['backup_md5']:
                    raise ValueError('النسخة الاحتياطية فشلت في اختبار السلامة')
                replace_content(api,current,original)
                after = get_metadata(api,entry['target_id'])
                if after.get('md5Checksum') != entry['backup_md5']:
                    raise RuntimeError('الاستعادة لم تحقق تطابق المحتوى')
                entry.update(state='rolled_back',error='')
            event(manifest,'rollback_ok',entry['path'])
        except Exception as exc:
            entry.update(state='restore_conflict' if 'تغير الملف' in str(exc) else 'restore_failed',
                         error=str(exc)[:400])
            event(manifest,'rollback_failed',entry['path'],str(exc))
        _save(api,manifest)
        if progress:
            progress(n,min(limit,len(candidates)))
    remaining = [e for e in manifest['entries']
                 if e['state'] in RECOVERY_STATES and e.get('rollback_requested', True)]
    if remaining:
        manifest['status'] = 'rollback_partial'
    elif any(e['state'] == 'updated' for e in manifest['entries']):
        # A selected subset was restored; other updates await review.
        manifest['status'] = 'pending_review'
        event(manifest, 'partial_rollback_finished')
    else:
        manifest['status'] = 'rolled_back'
        event(manifest, 'rollback_finished')
    return _save(api,manifest)


def list_recent_jobs(api,configured_root=None,limit=25):
    root=recovery_root(api,configured_root)
    folders = _list(api, f"'{root}' in parents and mimeType = '{FOLDER}' and trashed = false",
                    limit=limit*2)
    folders = sorted((f for f in folders if f['name'].startswith('MFW_V3_')),
                     key=lambda x:x.get('createdTime',''),reverse=True)[:limit]
    found=[]
    for folder in folders:
        manifests=_list(api,f"'{folder['id']}' in parents and name = '{MANIFEST_NAME}' and trashed = false",
                        limit=2)
        if manifests:
            found.append({'folder_id':folder['id'],'manifest_id':manifests[0]['id'],
                          'job_id':folder['name'][7:], 'created_at':folder.get('createdTime')})
    return found


def summary(manifest):
    from collections import Counter
    counts=Counter(e['state'] for e in manifest['entries'])
    return {'total':len(manifest['entries']), 'updated':counts['updated'],
            'restored':counts['rolled_back'],'failed':sum(counts[x] for x in
                      ['stage_failed','update_failed','restore_failed','restore_conflict']),
            'skipped':counts['stage_skipped']+counts['match_skipped'],
            'pending':counts['new']+counts['staged']+counts['ready']+counts['backed_up'],
            'counts':dict(counts)}


def report_rows(manifest):
    return [{'path':e['path'],'state':e['state'],'original_id':e.get('target_id',''),
             'original_link':file_url({'id':e['target_id'],'mimeType':e['mime_type']})
             if e.get('target_id') else '',
             'source_id':e['source_id'],'backup_id':e.get('backup_id',''),
             'error':e.get('error','')} for e in manifest['entries']]
