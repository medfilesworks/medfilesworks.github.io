"""Drive operations for medfilesworks. No file deletion or creation of replacements."""
from __future__ import annotations

import io
import mimetypes
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

FOLDER = 'application/vnd.google-apps.folder'
NATIVE = 'application/vnd.google-apps.'
MAX_DEPTH = 12
MAX_FILES = 2000
MAX_TRANSFER_BYTES = 100 * 1024 * 1024
FIELDS = 'id,name,mimeType,size,webViewLink,modifiedTime,version,md5Checksum,parents,capabilities(canEdit,canDownload)'


def extract_id(value: str) -> str:
    value = (value or '').strip()
    for pattern in (r'/file/d/([\w-]+)', r'/folders/([\w-]+)', r'/d/([\w-]+)', r'[?&]id=([\w-]+)'):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    if re.fullmatch(r'[A-Za-z0-9_-]{20,}', value):
        return value
    raise ValueError('رابط Google Drive أو معرّف الملف غير صالح')


def file_url(item: dict) -> str:
    if item.get('webViewLink'):
        return item['webViewLink']
    if item['mimeType'] == FOLDER:
        return f"https://drive.google.com/drive/folders/{item['id']}"
    return f"https://drive.google.com/file/d/{item['id']}/view"


def get_metadata(api, file_id: str) -> dict:
    return api.files().get(fileId=file_id, fields=FIELDS, supportsAllDrives=True).execute()


def list_children(api, folder_id: str) -> list[dict]:
    items = []
    token = None
    while True:
        result = api.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields=f'nextPageToken,files({FIELDS})',
            pageSize=1000, pageToken=token, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(result.get('files', []))
        token = result.get('nextPageToken')
        if not token:
            break
    return sorted(items, key=lambda f: (f['mimeType'] != FOLDER, f['name'].casefold()))


def list_available_folders(api, limit=200) -> list[dict]:
    result = []
    token = None
    while len(result) < limit:
        page = api.files().list(
            q=f"mimeType = '{FOLDER}' and trashed = false",
            fields=f'nextPageToken,files({FIELDS})',
            pageSize=min(1000, limit-len(result)), pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        result.extend(page.get('files', []))
        token = page.get('nextPageToken')
        if not token:
            break
    return result


def ensure_folder(item: dict):
    if item['mimeType'] != FOLDER:
        raise ValueError('الرابط لا يشير إلى مجلد')


def ensure_binary_target(item: dict):
    if item['mimeType'].startswith(NATIVE):
        raise ValueError('ملفات Google Docs/Sheets/Slides الأصلية لا تدعم الاستبدال الثنائي')
    if item.get('capabilities', {}).get('canEdit') is False:
        raise PermissionError('لا توجد صلاحية تحرير هذا الملف')


def ensure_binary_source(item: dict):
    if item['mimeType'].startswith(NATIVE):
        raise ValueError('المصدر ملف Google أصلي غير مدعوم')
    if item.get('capabilities', {}).get('canDownload') is False:
        raise PermissionError('لا توجد صلاحية تنزيل المصدر')
    if int(item.get('size', 0)) > MAX_TRANSFER_BYTES:
        raise ValueError('المصدر أكبر من الحد المسموح (100 MB)')


def is_descendant(api, possible_child_id: str, possible_ancestor_id: str, max_steps=100) -> bool:
    """True if a Drive folder is contained inside the other folder."""
    current = possible_child_id
    seen = set()
    for _ in range(max_steps):
        if current == possible_ancestor_id:
            return True
        if current in seen:
            raise ValueError('تعذر التحقق من تسلسل المجلدات')
        seen.add(current)
        item = get_metadata(api, current)
        parents = item.get('parents', [])
        if not parents:
            return False
        current = parents[0]
    raise ValueError('تعذر التحقق من تسلسل المجلدات الطويل')


def scan_tree(api, folder_id: str, recursive=True, max_depth=MAX_DEPTH, max_files=MAX_FILES) -> dict[str, dict]:
    """Relative POSIX path -> file metadata. Detect duplicate names/paths and enforce bounds."""
    result = {}
    queue = [(folder_id, '', 0)]
    visited = set()
    while queue:
        current, prefix, depth = queue.pop(0)
        if current in visited:
            raise ValueError('تم اكتشاف مجلد مكرر في المسار')
        visited.add(current)
        children = list_children(api, current)
        names = Counter(f['name'].casefold() for f in children)
        for item in children:
            if names[item['name'].casefold()] > 1:
                raise ValueError(f"اسم مكرر داخل المجلد: {prefix}{item['name']}")
            path = f"{prefix}{item['name']}"
            if item['mimeType'] == FOLDER:
                if recursive:
                    if depth >= max_depth:
                        raise ValueError(f'تم تجاوز الحد الأقصى لعمق المجلدات ({max_depth})')
                    queue.append((item['id'], path+'/', depth+1))
            else:
                if path in result:
                    raise ValueError(f'مسار ملف مكرر: {path}')
                result[path] = item
                if len(result) > max_files:
                    raise ValueError(f'تجاوز عدد الملفات المسموح فحصها ({max_files})')
    return result


def build_plan(target_files: dict[str, dict], source_files: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    ready, skipped = [], []
    for path, src in sorted(source_files.items()):
        dst = target_files.get(path)
        reason = None
        if dst is None:
            reason = 'لا يوجد ملف أصلي مطابق لنفس المسار'
        elif dst['id'] == src['id']:
            reason = 'المصدر والهدف نفس الملف'
        elif dst['mimeType'].startswith(NATIVE) or src['mimeType'].startswith(NATIVE):
            reason = 'ملف Google أصلي غير مدعوم'
        elif dst['mimeType'] != src['mimeType']:
            reason = 'نوع الملف مختلف'
        elif dst.get('capabilities', {}).get('canEdit') is False:
            reason = 'لا توجد صلاحية تعديل'
        elif src.get('capabilities', {}).get('canDownload') is False:
            reason = 'لا توجد صلاحية تنزيل المصدر'
        elif int(src.get('size', 0)) > MAX_TRANSFER_BYTES:
            reason = 'حجم المصدر يتجاوز 100 MB'
        if reason:
            skipped.append({'path': path, 'reason': reason})
        else:
            ready.append({'path': path, 'target': dst, 'source': src})
    return ready, skipped


def same_revision(previous: dict, current: dict) -> bool:
    return (str(previous.get('version', '')) == str(current.get('version', ''))
            and previous.get('name') == current.get('name')
            and previous.get('mimeType') == current.get('mimeType'))


def validate_manual_format(target: dict, filename: str):
    target_suffix = PurePosixPath(target['name']).suffix.casefold()
    uploaded_suffix = PurePosixPath(filename).suffix.casefold()
    if target_suffix and target_suffix != uploaded_suffix:
        raise ValueError(f'امتداد النسخة الجديدة {uploaded_suffix or "(بدون)"} مختلف عن الأصل {target_suffix}')
    guess, _ = mimetypes.guess_type(filename)
    if guess and target['mimeType'] != 'application/octet-stream' and guess != target['mimeType']:
        # Some Office formats are represented by generic MIME; extension check above is authoritative.
        if target_suffix not in {'.docx','.xlsx','.pptx','.zip','.csv','.md','.txt'}:
            raise ValueError('نوع الملف المرفوع لا يطابق نوع الملف الأصلي')


def fetch_source(api, source: dict) -> bytes:
    ensure_binary_source(source)
    req = api.files().get_media(fileId=source['id'], supportsAllDrives=True)
    output = io.BytesIO()
    downloader = MediaIoBaseDownload(output, req, chunksize=4 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
        if output.tell() > MAX_TRANSFER_BYTES:
            raise ValueError('تجاوز الملف الحد الأقصى أثناء التنزيل')
    return output.getvalue()


def replace_content(api, expected: dict, payload: bytes) -> dict:
    if not payload:
        raise ValueError('لا يمكن استبدال الملف بمحتوى فارغ')
    if len(payload) > MAX_TRANSFER_BYTES:
        raise ValueError('حجم الملف يتجاوز 100 MB')
    latest = get_metadata(api, expected['id'])
    ensure_binary_target(latest)
    if not same_revision(expected, latest):
        raise ValueError('تغير الملف الأصلي منذ المعاينة؛ أعيدي الفحص')
    upload = MediaIoBaseUpload(io.BytesIO(payload), mimetype=latest['mimeType'], resumable=True, chunksize=4*1024*1024)
    result = api.files().update(
        fileId=latest['id'], media_body=upload,
        fields='id,name,mimeType,webViewLink,version', supportsAllDrives=True,
    ).execute()
    if result['id'] != latest['id']:
        raise RuntimeError('لم يتم الحفاظ على معرّف الملف الأصلي')
    return result
