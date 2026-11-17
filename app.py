"""medfilesworks v2 — authorized Google Drive in-place file updater."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import secrets
import time
from urllib.parse import urlencode

import requests
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from audit import audit_row, fetch_audit, save_audit
from drive_ops import (
    FOLDER, MAX_TRANSFER_BYTES, build_plan, ensure_binary_target,
    ensure_folder, extract_id, fetch_source, file_url, get_metadata, is_descendant,
    list_available_folders, list_children, replace_content, scan_tree,
    validate_manual_format,
)

st.set_page_config(page_title='medfilesworks | Drive Workspace', page_icon='📁', layout='wide')
st.markdown('''<style>
.stApp {direction:rtl;} input, textarea, [data-testid="stDataFrame"] {direction:ltr!important;}
.block-container {max-width:1320px;padding-top:1.7rem;}
[data-testid="stMetric"] {border:1px solid rgba(125,125,125,.22);border-radius:12px;padding:12px;}
</style>''', unsafe_allow_html=True)
SCOPES = ['https://www.googleapis.com/auth/drive', 'openid', 'https://www.googleapis.com/auth/userinfo.email']
OWNER = 'marah.h.musallam21@gmail.com'


def setting(name, default=None):
    try:
        return st.secrets[name]
    except (KeyError, FileNotFoundError):
        return default


CLIENT_ID = setting('GOOGLE_CLIENT_ID')
CLIENT_SECRET = setting('GOOGLE_CLIENT_SECRET')
REDIRECT = setting('REDIRECT_URI')
STATE_SECRET = setting('STATE_SIGNING_KEY')
ALLOWED = {str(x).strip().lower() for x in (setting('ALLOWED_EMAILS', []) or [])}
SUPABASE_URL = setting('SUPABASE_URL', '')
SUPABASE_KEY = setting('SUPABASE_SERVICE_ROLE_KEY', '')

st.title('medfilesworks')
st.caption('إدارة تحديث ملفات Google Drive | استبدال المحتوى مع الحفاظ على الرابط ومعرّف الملف')

missing = [k for k, v in {
    'GOOGLE_CLIENT_ID': CLIENT_ID, 'GOOGLE_CLIENT_SECRET': CLIENT_SECRET,
    'REDIRECT_URI': REDIRECT, 'STATE_SIGNING_KEY': STATE_SECRET,
    'ALLOWED_EMAILS': ALLOWED,
}.items() if not v]
if missing:
    st.error('إعدادات Streamlit Secrets ناقصة: ' + ', '.join(missing))
    st.info('افتحي Streamlit Community Cloud → My apps → Settings → Secrets وأضيفي القيم من ملف الإرشادات.')
    st.stop()


def make_state():
    value = f'{int(time.time())}.{secrets.token_urlsafe(24)}'
    signature = hmac.new(str(STATE_SECRET).encode(), value.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{value}.{signature}'.encode()).decode()


def valid_state(value):
    try:
        decoded = base64.urlsafe_b64decode(value.encode()).decode()
        stamp, nonce, signature = decoded.split('.')
        expected = hmac.new(str(STATE_SECRET).encode(), f'{stamp}.{nonce}'.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature) and 0 <= time.time()-int(stamp) < 600
    except (ValueError, TypeError, UnicodeError):
        return False


def login_url():
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': CLIENT_ID, 'redirect_uri': REDIRECT,
        'response_type': 'code', 'scope': ' '.join(SCOPES),
        'access_type': 'offline', 'prompt': 'consent', 'state': make_state(),
    })


def exchange(code):
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'code': code, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'redirect_uri': REDIRECT, 'grant_type': 'authorization_code',
    }, timeout=30)
    r.raise_for_status()
    return r.json()


def credentials():
    tok = st.session_state.get('tokens')
    if not tok:
        return None
    return Credentials(
        token=tok.get('access_token'), refresh_token=tok.get('refresh_token'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET, scopes=SCOPES,
    )


def refresh_token():
    cr = credentials()
    if not cr or not cr.refresh_token:
        raise PermissionError('انتهت صلاحية تسجيل الدخول؛ سجّلي الدخول من جديد')
    cr.refresh(Request())
    st.session_state.tokens['access_token'] = cr.token
    return cr.token


def run_drive(operation):
    try:
        return operation(build('drive', 'v3', credentials=credentials(), cache_discovery=False))
    except Exception as e:
        # One retry only for an expired access token.
        if ('401' in str(e) or 'invalid credentials' in str(e).lower()) and credentials() and credentials().refresh_token:
            refresh_token()
            return operation(build('drive', 'v3', credentials=credentials(), cache_discovery=False))
        raise


def logout():
    st.session_state.clear()
    st.query_params.clear()
    st.rerun()


params = st.query_params
if 'error' in params:
    st.error('رفض Google تسجيل الدخول: ' + str(params.get('error')))
    if st.button('المحاولة مجددًا'):
        st.query_params.clear(); st.rerun()
    st.stop()
if 'code' in params:
    if not valid_state(params.get('state', '')):
        st.error('تعذر التحقق من جلسة Google. أعيدي تسجيل الدخول.')
        if st.button('إعادة المحاولة'):
            st.query_params.clear(); st.rerun()
        st.stop()
    try:
        st.session_state.tokens = exchange(params['code'])
        st.query_params.clear()
        st.rerun()
    except requests.RequestException:
        st.error('فشل الاتصال بخدمة تسجيل الدخول لدى Google. راجعي رابط العودة وبيانات OAuth.')
        st.stop()

if not credentials():
    st.info('الدخول محصور بالحسابات الثلاثة المصرح لها. صلاحيات Drive تحدد الملفات التي يمكن تعديلها.')
    st.link_button('تسجيل الدخول باستخدام Google', login_url(), type='primary')
    st.stop()

try:
    token = st.session_state.tokens['access_token']
    response = requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                            headers={'Authorization': 'Bearer '+token}, timeout=15)
    if response.status_code == 401:
        token = refresh_token()
        response = requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                                headers={'Authorization': 'Bearer '+token}, timeout=15)
    response.raise_for_status()
    email = response.json().get('email', '').strip().lower()
except Exception:
    st.error('تعذر التحقق من هوية الحساب. سجّلي الدخول مجددًا.')
    if st.button('تسجيل الخروج'):
        logout()
    st.stop()
if email not in ALLOWED:
    st.error('هذا الحساب غير مصرح له باستخدام المنصة.')
    if st.button('تسجيل الخروج'):
        logout()
    st.stop()

with st.sidebar:
    st.subheader('الحساب الحالي')
    st.write(email)
    st.caption('المالكة / الإدارة' if email == OWNER else 'محرر')
    st.caption('السجلات الدائمة: مفعّلة' if SUPABASE_URL and SUPABASE_KEY else 'السجلات الدائمة: غير مفعّلة')
    if st.button('تسجيل الخروج', use_container_width=True):
        logout()
    st.divider()
    st.caption('لا يتم حذف الملفات الأصلية أو تغيير روابطها. ملفات Google الأصلية غير مدعومة للاستبدال الثنائي.')

if 'session_audit' not in st.session_state:
    st.session_state.session_audit = []


def record(action, target, status, detail=''):
    row = audit_row(email, action, target['id'], target['name'], status, detail)
    st.session_state.session_audit.insert(0, row)
    saved, message = save_audit(row, SUPABASE_URL, SUPABASE_KEY)
    return saved, message


def table_rows(items):
    return [{'الاسم': f['name'], 'النوع': 'مجلد' if f['mimeType']==FOLDER else f['mimeType'],
             'الحجم (بايت)': f.get('size', ''), 'الرابط': file_url(f)} for f in items]


browse, manual, bulk, history = st.tabs(['مستكشف Drive', 'تحديث ملف', 'تحديث جماعي', 'سجل العمليات'])

with browse:
    st.subheader('استعراض الملفات والمجلدات')
    c1, c2 = st.columns([3, 1])
    with c1:
        folder_url = st.text_input('رابط المجلد (أو معرّفه)', key='browse_url')
    with c2:
        st.write('')
        if st.button('فتح المجلد', use_container_width=True):
            try:
                folder_id = extract_id(folder_url)
                meta = run_drive(lambda api: get_metadata(api, folder_id))
                ensure_folder(meta)
                st.session_state.browse_folder = meta
                st.session_state.browse_items = run_drive(lambda api: list_children(api, folder_id))
            except Exception as exc:
                st.error(f'تعذر فتح المجلد: {exc}')
    if st.button('عرض المجلدات المتاحة'):
        try:
            st.session_state.browse_folder = {'name':'المجلدات المتاحة'}
            st.session_state.browse_items = run_drive(lambda api: list_available_folders(api))
        except Exception as exc:
            st.error(f'تعذر استعراض Drive: {exc}')
    items = st.session_state.get('browse_items', [])
    if 'browse_folder' in st.session_state:
        st.write('**' + st.session_state.browse_folder['name'] + '**')
        st.metric('العناصر المعروضة', len(items))
        st.dataframe(table_rows(items), hide_index=True, use_container_width=True,
                     column_config={'الرابط': st.column_config.LinkColumn('الرابط')})
        folders = [x for x in items if x['mimeType'] == FOLDER]
        if folders:
            names = {f"{f['name']} · {f['id'][:8]}":f for f in folders}
            choice = st.selectbox('انتقلي إلى مجلد فرعي', list(names))
            if st.button('فتح المجلد الفرعي'):
                item = names[choice]
                try:
                    st.session_state.browse_folder = item
                    st.session_state.browse_items = run_drive(lambda api: list_children(api, item['id']))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

with manual:
    st.subheader('استبدال محتوى ملف واحد')
    st.caption('التحديث يتم على الملف نفسه عبر Google Drive API؛ لا يُنشأ ملف بديل ولا يُحذف الأصل.')
    target_url = st.text_input('رابط الملف الأصلي', key='manual_url')
    upload = st.file_uploader('ارفعي النسخة الجديدة من جهازك (حتى 100 MB)', key='manual_file')
    if st.button('فحص الملف الأصلي', key='manual_scan'):
        try:
            item = run_drive(lambda api: get_metadata(api, extract_id(target_url)))
            ensure_binary_target(item)
            st.session_state.manual_preview = item
        except Exception as exc:
            st.session_state.pop('manual_preview', None)
            st.error(str(exc))
    preview = st.session_state.get('manual_preview')
    if preview:
        st.info(f"الاسم: {preview['name']} | المعرّف الثابت: {preview['id']} | النوع: {preview['mimeType']}")
        st.link_button('معاينة الملف الأصلي في Drive', file_url(preview))
        st.caption('المعاينة تعرض بيانات الملف ورابطه؛ افتحيه في Drive لمراجعة محتواه قبل الاستبدال.')
        if upload:
            st.write(f'النسخة الجديدة: **{upload.name}** — {upload.size:,} بايت')
        confirm = st.checkbox('راجعت الملف والنسخة الجديدة، وأوافق على استبدال المحتوى', key='manual_ok')
        if st.button('تنفيذ التحديث', type='primary', disabled=not(upload and confirm), key='manual_execute'):
            try:
                if extract_id(target_url) != preview['id']:
                    raise ValueError('تغير رابط الملف؛ أعيدي الفحص')
                validate_manual_format(preview, upload.name)
                if upload.size > MAX_TRANSFER_BYTES:
                    raise ValueError('الملف يتجاوز 100 MB')
                result = run_drive(lambda api: replace_content(api, preview, upload.getvalue()))
                _, message = record('manual_update', preview, 'success', 'In-place update; file ID retained')
                st.success('نجح التحديث، ومعرّف الملف لم يتغير: '+result['id'])
                if message:
                    st.warning(message)
                st.link_button('فتح الرابط الأصلي', file_url(preview))
                st.session_state.pop('manual_preview', None)
            except Exception as exc:
                _, message = record('manual_update', preview, 'failed', str(exc))
                st.error(f'فشل التحديث: {exc}')
                if message:
                    st.warning(message)

with bulk:
    st.subheader('تحديث ملفات مجلد كامل')
    st.caption('المطابقة حسب المسار النسبي واسم الملف، مع خيار فحص المجلدات الفرعية. لا تُحذف الملفات غير المتطابقة.')
    old_url = st.text_input('رابط مجلد الملفات الأصلية', key='bulk_old')
    new_url = st.text_input('رابط مجلد النسخ الجديدة', key='bulk_new')
    recursive = st.checkbox('تضمين المجلدات الفرعية', value=True, key='bulk_recursive')
    if st.button('فحص المجلدين وبناء خطة التحديث'):
        try:
            old_id, new_id = extract_id(old_url), extract_id(new_url)
            if old_id == new_id:
                raise ValueError('لا يمكن استخدام نفس المجلد كمصدر وهدف')
            def make_plan(api):
                ensure_folder(get_metadata(api, old_id))
                ensure_folder(get_metadata(api, new_id))
                if is_descendant(api, old_id, new_id) or is_descendant(api, new_id, old_id):
                    raise ValueError('المجلدان متداخلان؛ اختاري مجلدين مستقلين')
                originals = scan_tree(api, old_id, recursive=recursive)
                sources = scan_tree(api, new_id, recursive=recursive)
                return build_plan(originals, sources)
            ready, skipped = run_drive(make_plan)
            st.session_state.bulk_plan = {'old':old_id,'new':new_id,'recursive':recursive,
                                          'ready':ready,'skipped':skipped}
            st.session_state.bulk_confirm = False
        except Exception as exc:
            st.session_state.pop('bulk_plan', None)
            st.error(f'فشل الفحص: {exc}')
    plan = st.session_state.get('bulk_plan')
    if plan:
        a, b = st.columns(2)
        a.metric('جاهزة للتحديث', len(plan['ready']))
        b.metric('مستبعدة', len(plan['skipped']))
        if plan['ready']:
            st.dataframe([{'المسار':p['path'], 'الأصل':p['target']['id'],
                           'المصدر':p['source']['id'], 'حجم المصدر':p['source'].get('size','')}
                          for p in plan['ready']], hide_index=True, use_container_width=True)
        if plan['skipped']:
            with st.expander('عرض الملفات المستبعدة وأسباب الاستبعاد'):
                st.dataframe(plan['skipped'], hide_index=True, use_container_width=True)
        agree = st.checkbox('راجعت جميع المطابقات وأوافق على تحديث الملفات الأصلية', key='bulk_confirm')
        if st.button('بدء التحديث الجماعي', type='primary', disabled=not(agree and plan['ready'])):
            if (extract_id(old_url)!=plan['old'] or extract_id(new_url)!=plan['new']
                    or recursive!=plan['recursive']):
                st.error('تغيرت إعدادات المجلدات؛ أعيدي الفحص')
            else:
                results = []
                bar = st.progress(0)
                for i, pair in enumerate(plan['ready']):
                    dst, src = pair['target'], pair['source']
                    try:
                        def update_one(api):
                            # Recheck the source and target before writing.
                            current_source = get_metadata(api, src['id'])
                            if (str(current_source.get('version','')) != str(src.get('version',''))
                                    or current_source['name'] != src['name']):
                                raise ValueError('تغير المصدر بعد المعاينة؛ أعيدي الفحص')
                            if current_source['mimeType'] != dst['mimeType']:
                                raise ValueError('نوع الملف تغير')
                            payload = fetch_source(api, current_source)
                            return replace_content(api, dst, payload)
                        outcome = run_drive(update_one)
                        status, detail = 'success', 'ID retained: '+outcome['id']
                    except Exception as exc:
                        status, detail = 'failed', str(exc)
                    _, warning = record('bulk_update', dst, status, detail)
                    results.append({'المسار':pair['path'], 'الملف':dst['name'],
                                    'النتيجة':'نجاح' if status=='success' else 'فشل',
                                    'التفاصيل':detail, 'معرّف الأصل':dst['id']})
                    bar.progress((i+1)/len(plan['ready']))
                st.session_state.last_bulk_results = results
                st.session_state.pop('bulk_plan', None)
                st.success(f"اكتمل التنفيذ: {sum(x['النتيجة']=='نجاح' for x in results)} ناجح من {len(results)}")
                if warning:
                    st.warning(warning)
    if st.session_state.get('last_bulk_results'):
        rows = st.session_state.last_bulk_results
        st.dataframe(rows, hide_index=True, use_container_width=True)
        buff = io.StringIO()
        writer = csv.DictWriter(buff, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
        st.download_button('تنزيل تقرير CSV', buff.getvalue().encode('utf-8-sig'),
                           'medfilesworks_results.csv', mime='text/csv')

with history:
    st.subheader('سجل عمليات التحديث')
    if SUPABASE_URL and SUPABASE_KEY:
        if email == OWNER:
            try:
                rows = fetch_audit(SUPABASE_URL, SUPABASE_KEY)
                st.caption('السجل الدائم من Supabase — جميع الحسابات')
            except requests.RequestException:
                rows = st.session_state.session_audit
                st.warning('تعذر قراءة السجل الدائم؛ يتم عرض سجل الجلسة الحالية')
        else:
            rows = st.session_state.session_audit
            st.caption('سجل الجلسة الحالية؛ السجل الدائم الكامل متاح للمالكة فقط')
    else:
        rows = st.session_state.session_audit
        st.warning('السجل الحالي مؤقت. لتفعيل سجل دائم، أضيفي Supabase Secrets ونفّذي ملف SQL المرفق.')
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
        buff = io.StringIO()
        writer = csv.DictWriter(buff, fieldnames=list(rows[0].keys()))
        writer.writeheader();writer.writerows(rows)
        st.download_button('تصدير السجل CSV', buff.getvalue().encode('utf-8-sig'),
                           'medfilesworks_audit.csv', mime='text/csv')
    else:
        st.info('لا توجد عمليات مسجلة بعد.')

st.divider()
st.caption('حد الملفات: 100 MB للملف، 2000 ملف للفحص، عمق 12 مجلدًا. لا يُنفذ التحديث إلا بعد المعاينة والموافقة.')
