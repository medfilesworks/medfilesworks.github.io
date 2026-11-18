"""Additive Streamlit page: the original medfilesworks V2 app.py is untouched."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from drive_ops import (
    MAX_TRANSFER_BYTES, ensure_folder, extract_id, fetch_source,
    file_url, get_metadata, scan_tree,
)
from v3_engine import (
    approve_job, begin_rollback, create_job, ensure_actor, list_recent_jobs,
    load_job, prepare_targets, recovery_root, report_rows, rollback_batch,
    stage_batch, stage_retries, summary, update_batch,
)
from v3_store import firebase_is_configured, read_jobs, sync_job
from v3_watermark import (SUPPORTED_MIMES, drag_stamp_png, preview_png,
                          preview_first_page_pair, validate_config)

st.set_page_config(page_title='V3 Studio | medfilesworks', page_icon='🛡️', layout='wide')
st.markdown('''<style>
.stApp {direction:rtl;} input,textarea,[data-testid="stDataFrame"] {direction:ltr!important;}
.block-container {max-width:1380px;padding-top:1.4rem;}
[data-testid="stMetric"] {border:1px solid rgba(125,125,125,.2);border-radius:12px;padding:12px;}
</style>''', unsafe_allow_html=True)
OWNER='marah.h.musallam21@gmail.com'
try:
position_editor = components.declare_component(
'medfiles_position_editor',
path=str(Path(file).resolve().parents[1] / 'v3_drag_editor'),
)
except Exception as exc:
st.error(f'تعذر تحميل محرر السحب: {exc}')
st.stop()
POSITION_OPTIONS = {
    'أعلى اليسار': 'top_left', 'أعلى الوسط': 'top_center', 'أعلى اليمين': 'top_right',
    'وسط اليسار': 'center_left', 'الوسط': 'center', 'وسط اليمين': 'center_right',
    'أسفل اليسار': 'bottom_left', 'أسفل الوسط': 'bottom_center', 'أسفل اليمين': 'bottom_right',
    'متكرر على الصفحة': 'repeated', 'توزيع حر بالسحب': 'custom',
}

STEPS = ['1. العلامة المائية', '2. المطابقة والتحديث',
         '3. المراجعة والاعتماد/التراجع', '4. السجل والتقارير']
SCOPES=['https://www.googleapis.com/auth/drive','openid',
        'https://www.googleapis.com/auth/userinfo.email']


def secret(key, fallback=''):
    try:
        return st.secrets.get(key, fallback)
    except (FileNotFoundError, KeyError):
        return fallback


def credentials():
    tok = st.session_state.get('tokens')
    if not tok:
        return None
    return Credentials(token=tok.get('access_token'),
                       refresh_token=tok.get('refresh_token'),
                       token_uri='https://oauth2.googleapis.com/token',
                       client_id=secret('GOOGLE_CLIENT_ID'),
                       client_secret=secret('GOOGLE_CLIENT_SECRET'), scopes=SCOPES)


def refresh_credentials():
    cr=credentials()
    if not cr or not cr.refresh_token:
        raise PermissionError('انتهت جلسة Google؛ سجّلي الدخول من الصفحة الرئيسية')
    cr.refresh(Request())
    st.session_state.tokens['access_token']=cr.token
    return cr.token


def drive_call(action):
    cr=credentials()
    if not cr:
        raise PermissionError('يجب تسجيل الدخول من الصفحة الرئيسية')
    try:
        return action(build('drive','v3',credentials=cr,cache_discovery=False))
    except Exception as exc:
        if ('401' in str(exc) or 'invalid credentials' in str(exc).lower()) and cr.refresh_token:
            refresh_credentials()
            return action(build('drive','v3',credentials=credentials(),cache_discovery=False))
        raise


def logged_in_email():
    tok=st.session_state.get('tokens')
    if not tok:
        return ''
    token=tok.get('access_token','')
    response=requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                          headers={'Authorization':'Bearer '+token},timeout=15)
    if response.status_code==401:
        token=refresh_credentials()
        response=requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                              headers={'Authorization':'Bearer '+token},timeout=15)
    response.raise_for_status()
    return response.json().get('email','').strip().lower()


st.title('Watermark & Safe Update Studio')
st.caption('medfilesworks V3 | إضافة العلامة المائية ← المطابقة ← التحديث ← المراجعة ← اعتماد أو تراجع')
if not credentials():
    st.warning('يجب تسجيل الدخول باستخدام Google من صفحة medfilesworks الرئيسية أولًا.')
    st.page_link('app.py',label='العودة لصفحة تسجيل الدخول')
    st.stop()
try:
    email=logged_in_email()
except Exception:
    st.error('تعذر التحقق من هوية Google؛ أعيدي الدخول من الصفحة الرئيسية')
    st.stop()
allowed={str(x).strip().lower() for x in (secret('ALLOWED_EMAILS',[]) or [])}
if email not in allowed:
    st.error('هذا الحساب غير مصرح له باستخدام المنصة')
    st.stop()
is_owner=email==OWNER
root_secret=secret('BACKUP_ROOT_FOLDER_ID','')
firebase_settings=secret('FIREBASE',{})
firebase_enabled=firebase_is_configured(firebase_settings)


def sync(manifest):
    ok,msg=sync_job(manifest,firebase_settings)
    if not ok:
        st.info(msg)


def get_job():
    manifest_id=st.session_state.get('v3_manifest_id')
    if not manifest_id:
        return None
    manifest=drive_call(lambda api:load_job(api,manifest_id))
    ensure_actor(manifest,email,is_owner)
    return manifest


def save_job_from_result(manifest, rerun=False):
    st.session_state.v3_manifest_id=manifest['manifest_id']
    sync(manifest)
    if manifest['status']=='stage_ready':
        st.session_state.v3_nav_next=STEPS[1]
    elif manifest['status'] in ('pending_review','rolling_back','rollback_partial','rolled_back'):
        st.session_state.v3_nav_next=STEPS[2]
    if rerun:
        st.rerun()


def _run_safely_until_budget(api, current, mode, progress=None, max_seconds=45):
    """One-click processing, with a durable checkpoint at every file.

    If Streamlit's runtime limit is reached, the next button press resumes
    from the Drive manifest. No background worker is implied.
    """
    start=time.monotonic()
    states={'stage':('new',),'update':('ready','backed_up'),
            'rollback':('updated','restore_failed','restore_conflict')}
    workers={'stage':stage_batch,'update':update_batch,'rollback':rollback_batch}
    states_to_work=states[mode]
    initial=sum(e['state'] in states_to_work for e in current['entries'])
    while time.monotonic()-start < max_seconds:
        remaining=sum(e['state'] in states_to_work for e in current['entries'])
        if remaining==0:
            break
        before=[e['state'] for e in current['entries']]
        current=workers[mode](api,current,limit=min(10,remaining))
        after=[e['state'] for e in current['entries']]
        if progress:
            finished=max(0,initial-sum(e['state'] in states_to_work for e in current['entries']))
            progress.progress(finished/max(initial,1))
        if before==after or current['status'] in ('stage_ready','pending_review','rolled_back'):
            break
    return current


with st.sidebar:
    st.subheader('V3 Workspace')
    st.caption(email)
    st.caption('المالكة' if is_owner else 'محرر')
    if st.button('فتح عملية جديدة',use_container_width=True):
        for name in ['v3_manifest_id','v3_source_files','v3_source_id',
                     'v3_preview_key','v3_preview_original','v3_preview_new']:
            st.session_state.pop(name,None)
        st.rerun()
    st.divider()
    st.caption('استعادة ملفات النسخ الاحتياطية ممكنة بعد إعادة التشغيل؛ لا تُحذف تلقائيًا.')
    if not firebase_enabled:
        st.info('Firebase indexing needs the private key in Streamlit Secrets. Drive backups and rollback remain available.')

try:
    manifest=get_job()
except Exception as exc:
    manifest=None
    st.error('تعذر تحميل العملية الحالية: '+str(exc))

if manifest:
    s=summary(manifest)
    cols=st.columns(4)
    cols[0].metric('الملفات',s['total'])
    cols[1].metric('محدثة',s['updated'])
    cols[2].metric('مستعادة',s['restored'])
    cols[3].metric('أخطاء',s['failed'])
    st.caption('Job ID: '+manifest['job_id']+' | Status: '+manifest['status']+
               ' | Manifest: '+manifest['manifest_id'])
else:
    st.info('ابدئي من إعداد العلامة المائية، أو افتحي عملية سابقة من تبويب السجل.')

if 'v3_nav_next' in st.session_state:
    st.session_state['v3_nav']=st.session_state.pop('v3_nav_next')
current_step=st.radio('Workflow',STEPS, horizontal=True, key='v3_nav',label_visibility='collapsed')
st.divider()

if current_step==STEPS[0]:
    st.subheader('استوديو العلامة المائية')
    if manifest:
        st.info('العملية الحالية: '+manifest['status']+' — لتغيير إعدادات العلامة أنشئي عملية جديدة.')
    else:
        url=st.text_input('رابط المجلد الجديد على Google Drive',key='v3_new_folder')
        recurse=st.checkbox('تضمين المجلدات الفرعية',value=True,key='v3_recurse')
        if st.button('فحص المجلد الجديد',key='v3_scan_source'):
            try:
                folder_id=extract_id(url)
                def scan(api):
                    ensure_folder(get_metadata(api,folder_id))
                    return scan_tree(api,folder_id,recursive=recurse)
                found=drive_call(scan)
                st.session_state.v3_source_files=found
                st.session_state.v3_source_id=folder_id
                st.session_state.v3_preview_key=None
                st.success(f'تم فحص {len(found)} ملفًا')
            except Exception as exc:
                st.session_state.pop('v3_source_files',None)
                st.error('فشل الفحص: '+str(exc))
        found=st.session_state.get('v3_source_files')
        if found is not None:
            supported={path:item for path,item in found.items()
                       if item['mimeType'] in SUPPORTED_MIMES
                       and int(item.get('size',0))<=MAX_TRANSFER_BYTES}
            st.caption(f'ملفات المجلد: {len(found)} | ملفات تدعم العلامة المائية: {len(supported)}')
            if len(supported)!=len(found):
                st.warning('الملفات غير المدعومة ستُستبعد من المعالجة دون حذف أو تعديل.')
            if supported:
                st.dataframe([{'المسار':path,'النوع':item['mimeType'],
                               'الحجم (MB)':round(int(item.get('size',0))/1048576,2)}
                              for path,item in list(supported.items())[:100]],
                             hide_index=True,use_container_width=True)
                mode=st.radio('نوع العلامة',['نص','شعار','نص + شعار'],horizontal=True)
                mode_value={'نص':'text','شعار':'logo','نص + شعار':'both'}[mode]
                txt=''
                if mode_value in ('text','both'):
                    txt=st.text_input('النص',value='medfilesworks',max_chars=180)
                logo_file=None
                if mode_value in ('logo','both'):
                    logo_file=st.file_uploader('ارفعي شعارًا PNG أو JPG (حتى 5 MB)',
                                               type=['png','jpg','jpeg'],key='v3_logo')
                c1,c2,c3=st.columns(3)
                with c1:
                    opacity=st.slider('الشفافية',5,75,22,5)/100
                    color=st.color_picker('لون النص','#45546B')
                with c2:
                    rotation=st.slider('الدوران',-75,75,30,5)
                    scale=st.slider('حجم العلامة',0.5,2.5,1.0,0.1)
                with c3:
                    placement_label=st.selectbox('التوزيع',list(POSITION_OPTIONS),
                                                 index=list(POSITION_OPTIONS).index('الوسط'))
                    margin=st.slider('البعد عن الحافة (%)',0.0,12.0,0.0,0.5,
                                     disabled=POSITION_OPTIONS[placement_label] in ('custom','repeated'))
                logo_bytes=logo_file.getvalue() if logo_file else None
                cfg={'mode':mode_value,'text':txt,'opacity':opacity,
                     'rotation':rotation,'scale':scale,'color':color,
                     'placement':POSITION_OPTIONS[placement_label],
                     'margin_percent':margin}
                sample_mode=st.radio('مصدر صفحة المعاينة',
                                     ['ملف من المجلد','رفع ملف تجريبي'],
                                     horizontal=True,key='v3_preview_source')
                sample_file=None
                if sample_mode.startswith('رفع'):
                    sample_file=st.file_uploader(
                        'ارفعي ملف PDF أو صورة لمعاينة الصفحة الأولى فقط',
                        type=['pdf','png','jpg','jpeg','webp'],key='v3_sample_upload')
                    chosen='sample_upload'
                    sample_hash=hashlib.sha256(sample_file.getvalue()).hexdigest() if sample_file else 'empty'
                else:
                    chosen=st.selectbox('اختاري ملفًا لمعاينة العلامة',list(supported),
                                        key='v3_sample_file')
                    sample_hash=str(supported[chosen].get('version',''))

                if cfg['placement']=='custom':
                    st.caption('التوزيع الحر: اسحبي العلامة مباشرة فوق صورة الصفحة الأولى. '
                               'الموضع يُحفظ كنسبة من مساحة الصفحة ويُعمّم على بقية الصفحات، '
                               'حتى إن اختلف حجم PDF.')
                    source_key=(st.session_state.v3_source_id,chosen,sample_hash)
                    if st.button('فتح الصفحة للتحريك الحر',key='v3_open_drag'):
                        try:
                            validate_config(cfg,logo_bytes)
                            if sample_mode.startswith('رفع'):
                                if not sample_file:
                                    raise ValueError('ارفعي ملف المعاينة أولًا')
                                if sample_file.size>MAX_TRANSFER_BYTES:
                                    raise ValueError('ملف المعاينة أكبر من 100 MB')
                                mime_map={'.pdf':'application/pdf','.png':'image/png',
                                          '.jpg':'image/jpeg','.jpeg':'image/jpeg',
                                          '.webp':'image/webp'}
                                mime_type=mime_map.get('.'+sample_file.name.rsplit('.',1)[-1].lower())
                                if not mime_type:
                                    raise ValueError('صيغة ملف المعاينة غير مدعومة')
                                source_bytes=sample_file.getvalue()
                            else:
                                src=supported[chosen]
                                mime_type=src['mimeType']
                                source_bytes=drive_call(lambda api:fetch_source(api,src))
                            st.session_state.v3_drag_page=preview_png(source_bytes,mime_type,width=900)
                            st.session_state.v3_drag_source=source_key
                        except Exception as exc:
                            st.error('تعذر فتح محرر التوزيع الحر: '+str(exc))
                    if st.session_state.get('v3_drag_source')==source_key:
                        base=st.session_state.v3_drag_page
                        with Image.open(io.BytesIO(base)) as bg:
                            page_w,page_h=bg.size
                        mark=drag_stamp_png(page_w,page_h,cfg,logo_bytes)
                        with Image.open(io.BytesIO(mark)) as mark_image:
                            mark_w=mark_image.width
                        previous=st.session_state.get('v3_custom_coords') or {'x':0.5,'y':0.5}
                        value=position_editor(
                            page_png_b64=base64.b64encode(base).decode('ascii'),
                            mark_png_b64=base64.b64encode(mark).decode('ascii'),
                            page_width=page_w, stamp_width=mark_w,
                            x=previous['x'],y=previous['y'],
                            key='v3_position_editor')
                        if isinstance(value,dict) and 'x' in value and 'y' in value:
                            st.session_state.v3_custom_coords={
                                'x':max(0,min(1,float(value['x']))),
                                'y':max(0,min(1,float(value['y'])))}
                    else:
                        st.info('اضغطي «فتح الصفحة للتحريك الحر» لعرض الورقة وسحب العلامة عليها.')
                    xvalue=st.session_state.get('v3_custom_coords') or {'x':0.5,'y':0.5}
                    cfg['custom_x']=xvalue['x']
                    cfg['custom_y']=xvalue['y']
                    st.caption(f"الموضع المعتمد: أفقي {xvalue['x']*100:.1f}% | عمودي {xvalue['y']*100:.1f}%")
                key=hashlib.sha256((json.dumps(cfg,sort_keys=True)+
                                    hashlib.sha256(logo_bytes or b'').hexdigest()+
                                    st.session_state.v3_source_id).encode()).hexdigest()
                preview_key=key+chosen+sample_hash
                st.caption('\u0645\u0639\u0627\u064a\u0646\u0629 \u0648\u0627\u062d\u062f\u0629\u061b \u0633\u062a\u064f\u0637\u0628\u0651\u064e\u0642 \u0627\u0644\u0639\u0644\u0627\u0645\u0629 \u0628\u0646\u0641\u0633 \u0627\u0644\u0625\u0639\u062f\u0627\u062f\u0627\u062a \u0639\u0644\u0649 \u062c\u0645\u064a\u0639 \u0635\u0641\u062d\u0627\u062a \u0643\u0644 \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u062f\u0639\u0648\u0645\u0629.')
                if st.button('\u0645\u0639\u0627\u064a\u0646\u0629 \u0627\u0644\u0635\u0641\u062d\u0629 \u0627\u0644\u0623\u0648\u0644\u0649 \u0642\u0628\u0644 \u062a\u0639\u0645\u064a\u0645 \u0627\u0644\u0639\u0644\u0627\u0645\u0629',key='v3_render_preview'):
                    try:
                        validate_config(cfg,logo_bytes)
                        if sample_mode.startswith('\u0631\u0641\u0639'):
                            if not sample_file:
                                raise ValueError('Upload a sample PDF or image for preview')
                            if sample_file.size>MAX_TRANSFER_BYTES:
                                raise ValueError('Sample exceeds 100 MB')
                            mime_map={'.pdf':'application/pdf','.png':'image/png',
                                      '.jpg':'image/jpeg','.jpeg':'image/jpeg',
                                      '.webp':'image/webp'}
                            suffix='.'+sample_file.name.rsplit('.',1)[-1].lower()
                            mime_type=mime_map.get(suffix)
                            if not mime_type:
                                raise ValueError('Unsupported sample format')
                            original=sample_file.getvalue()
                        else:
                            src=supported[chosen]
                            mime_type=src['mimeType']
                            original=drive_call(lambda api: fetch_source(api,src))
                        before,after=preview_first_page_pair(
                            original,mime_type,cfg,logo_bytes)
                        st.session_state.v3_preview_original=before
                        st.session_state.v3_preview_new=after
                        st.session_state.v3_preview_key=preview_key
                        st.success('تم توليد المعاينة دون تغيير الملفات في Google Drive')
                    except Exception as exc:
                        st.session_state.pop('v3_preview_key',None)
                        st.error('فشلت المعاينة: '+str(exc))
                if st.session_state.get('v3_preview_key')==preview_key:
                    left,right=st.columns(2)
                    left.caption('قبل العلامة المائية')
                    left.image(st.session_state.v3_preview_original,use_container_width=True)
                    right.caption('بعد العلامة المائية')
                    right.image(st.session_state.v3_preview_new,use_container_width=True)
                    confirm=st.checkbox('راجعت العلامة المائية وأوافق على تجهيز الملفات',key='v3_mark_confirm')
                    if st.button('\u0627\u0639\u062a\u0645\u0627\u062f \u0627\u0644\u0639\u0644\u0627\u0645\u0629 \u0648\u062a\u0637\u0628\u064a\u0642\u0647\u0627 \u062a\u0644\u0642\u0627\u0626\u064a\u064b\u0627 \u0639\u0644\u0649 \u062c\u0645\u064a\u0639 \u0627\u0644\u0635\u0641\u062d\u0627\u062a \u0648\u0627\u0644\u0645\u0644\u0641\u0627\u062a',type='primary',
                                 disabled=not confirm,key='v3_start_stage'):
                        try:
                            if extract_id(url)!=st.session_state.v3_source_id:
                                raise ValueError('تغير رابط المجلد بعد الفحص')
                            created=drive_call(lambda api:create_job(
                                api,st.session_state.v3_source_id,email,cfg,logo_bytes,
                                recursive=recurse,configured_root=root_secret or None))
                            # Persist job first. Any partial staging can be resumed safely.
                            st.session_state.v3_manifest_id=created['manifest_id']
                            sync(created)
                            bar=st.progress(0)
                            changed=drive_call(lambda api:_run_safely_until_budget(
                                api,created,'stage',progress=bar,max_seconds=40))
                            save_job_from_result(changed,rerun=True)
                        except Exception as exc:
                            st.error('تعذر إنشاء العملية أو تجهيزها؛ يمكن استكمالها من السجل: '+str(exc))
                else:
                    st.caption('غيّري الإعدادات كما تريدين ثم اضغطي «معاينة قبل التطبيق».')
    if manifest and manifest['status'] in ('staging','stage_ready'):
        st.divider()
        st.subheader('تجهيز نسخ الملفات مع العلامة المائية')
        processed=sum(e['state'] in ('staged','stage_skipped') for e in manifest['entries'])
        total=len(manifest['entries'])
        st.progress(processed/max(total,1))
        st.write(f'{processed} / {total} ملف')
        failed=[e for e in manifest['entries'] if e['state']=='stage_failed']
        if failed:
            st.error(f'فشل تجهيز {len(failed)} ملفًا. راجعي التقرير وأعيدي المحاولة.')
            if st.button('إعادة محاولة تجهيز الملفات الفاشلة'):
                try:
                    changed=drive_call(lambda api:stage_retries(api,manifest))
                    save_job_from_result(changed,rerun=True)
                except Exception as exc:
                    st.error(str(exc))
        if manifest['status']=='staging':
            batch=st.select_slider('عدد الملفات لكل دفعة',options=[1,2,5,10,20],value=5)
            if st.button('تجهيز الدفعة التالية',type='primary'):
                bar=st.progress(0)
                try:
                    def do_stage(api):
                        return stage_batch(api,manifest,limit=batch,
                            progress=lambda n,t:bar.progress(n/max(t,1)))
                    changed=drive_call(do_stage)
                    save_job_from_result(changed,rerun=True)
                except Exception as exc:
                    st.error('تعذر تجهيز الملفات: '+str(exc))
            if st.button('تجهيز جميع الملفات تلقائيًا (قابل للاستئناف)',key='v3_stage_all'):
                bar=st.progress(0)
                try:
                    changed=drive_call(lambda api:_run_safely_until_budget(
                        api,manifest,'stage',progress=bar))
                    save_job_from_result(changed,rerun=True)
                except Exception as exc:
                    st.error('توقف تجهيز الملفات؛ يمكن استئناف العملية: '+str(exc))
        else:
            st.success('\u062a\u0645 \u062a\u062c\u0647\u064a\u0632 \u0627\u0644\u0639\u0644\u0627\u0645\u0629\u061b \u0627\u0644\u0627\u0646\u062a\u0642\u0627\u0644 \u0625\u0644\u0649 \u0627\u0644\u0645\u0637\u0627\u0628\u0642\u0629 \u0648\u0627\u0644\u062a\u062d\u062f\u064a\u062b.')

if current_step==STEPS[1]:
    st.subheader('Smart Matching & Safe Update')
    if not manifest:
        st.info('ابدئي بتجهيز العلامة المائية من التبويب الأول.')
    elif manifest['status']=='stage_ready':
        st.success('تم اعتماد العلامة المائية وتجهيز الملفات. اختاري الآن المجلد القديم.')
        old_url=st.text_input('رابط المجلد القديم الذي تريدين الحفاظ على روابط ملفاته',
                              key='v3_old_folder')
        if st.button('فحص الملفات ومطابقتها',type='primary'):
            try:
                changed=drive_call(lambda api:prepare_targets(api,manifest,extract_id(old_url)))
                save_job_from_result(changed,rerun=True)
            except Exception as exc:
                st.error('تعذرت المطابقة: '+str(exc))
    elif manifest['status'] in ('ready_to_update','updating','pending_review'):
        matching=[e for e in manifest['entries'] if e.get('target_id')]
        st.caption('المجلد القديم: '+str(manifest.get('target_folder')))
        st.dataframe([{'الملف':e['path'],'المعرّف الأصلي':e['target_id'],
                       'الحالة':e['state'],'الملاحظة':e.get('error','')}
                      for e in matching],hide_index=True,use_container_width=True)
        if manifest['status'] in ('ready_to_update','updating'):
            st.warning('عند تنفيذ التحديث، ستظهر النسخة الجديدة فورًا لمن يملك الرابط. زر «اعتماد» لا يؤخر النشر؛ بل يغلق مرحلة المراجعة.')
            agree=st.checkbox('أوافق على حفظ نسخة احتياطية قبل استبدال كل ملف، مع إمكانية التراجع',
                              key='v3_update_agree')
            batch=st.select_slider('حجم دفعة التحديث',options=[1,2,5,10,20],value=5,
                                   key='v3_update_batch_size')
            if st.button('تنفيذ / استكمال التحديث الآمن',type='primary',
                         disabled=not agree):
                bar=st.progress(0)
                try:
                    def do_update(api):
                        return update_batch(api,manifest,limit=batch,
                            progress=lambda n,t:bar.progress(n/max(t,1)))
                    changed=drive_call(do_update)
                    save_job_from_result(changed,rerun=True)
                except Exception as exc:
                    st.error('تعذر تنفيذ الدفعة: '+str(exc))
            if st.button('تحديث كل الملفات بأمان (قابل للاستئناف)',type='primary',
                         disabled=not agree,key='v3_update_all'):
                bar=st.progress(0)
                try:
                    changed=drive_call(lambda api:_run_safely_until_budget(
                        api,manifest,'update',progress=bar))
                    save_job_from_result(changed,rerun=True)
                except Exception as exc:
                    st.error('توقف التحديث؛ النسخ الاحتياطية محفوظة: '+str(exc))
        if manifest['status']=='pending_review':
            st.success('اكتمل التنفيذ أو توقفت الملفات غير الناجحة. انتقلي إلى المراجعة.')
            if any(e['state']=='update_failed' for e in manifest['entries']):
                st.warning('توجد ملفات فاشلة. يمكن إعادة المحاولة بعد التحقق من سبب الفشل.')
                if st.button('إعادة محاولة الملفات الفاشلة'):
                    try:
                        changed=drive_call(lambda api:update_batch(api,manifest,limit=5,
                                                                     retry_failed=True))
                        save_job_from_result(changed,rerun=True)
                    except Exception as exc:
                        st.error(str(exc))
    else:
        st.info('حالة العملية الحالية: '+manifest['status'])

if current_step==STEPS[2]:
    st.subheader('مراجعة التحديث، ثم اعتماد أو تراجع')
    if not manifest:
        st.info('لا توجد عملية محددة')
    else:
        counts=summary(manifest)
        st.write(f"محدثة: **{counts['updated']}** | مستعادة: **{counts['restored']}** | أخطاء: **{counts['failed']}**")
        if manifest['status'] in ('pending_review','updating','approved','rolling_back','rollback_partial','rolled_back'):
            updated=[e for e in manifest['entries'] if e.get('backup_id')]
            if updated:
                options={f"{e['path']} ({e['state']})":e for e in updated}
                chosen=st.selectbox('ملف للمراجعة: نسخة قديمة مقابل ملف Drive الحالي',
                                    list(options),key='v3_review_choice')
                if st.button('عرض المقارنة الفعلية',key='v3_compare_file'):
                    entry=options[chosen]
                    try:
                        def make_compare(api):
                            before=get_metadata(api,entry['backup_id'])
                            current=get_metadata(api,entry['target_id'])
                            original=fetch_source(api,before)
                            present=fetch_source(api,current)
                            return preview_png(original,entry['mime_type']),preview_png(present,entry['mime_type'])
                        old_png,new_png=drive_call(make_compare)
                        st.session_state.v3_review_images=(chosen,old_png,new_png)
                    except Exception as exc:
                        st.error('تعذر فتح المعاينة: '+str(exc))
                review_images=st.session_state.get('v3_review_images')
                if review_images and review_images[0]==chosen:
                    a,b=st.columns(2)
                    a.caption('الأصل المحفوظ قبل التحديث')
                    a.image(review_images[1],use_container_width=True)
                    b.caption('الملف الموجود حاليًا في Drive')
                    b.image(review_images[2],use_container_width=True)
                st.link_button('فتح الملف الأصلي بنفس رابطه',
                               file_url({'id':options[chosen]['target_id'],
                                         'mimeType':options[chosen]['mime_type']}))
            if manifest['status']=='pending_review':
                st.divider()
                st.warning('مهم: الملفات الآن محدثة بالفعل على Google Drive، لكنها غير معتمدة داخل سجل V3.')
                col1,col2=st.columns(2)
                with col1:
                    approve=st.checkbox('راجعت النتائج وأوافق على اعتماد التحديث',
                                        key='v3_approve_confirm')
                    partial=any(e['state']=='update_failed' for e in manifest['entries'])
                    approve_partial=st.checkbox('أوافق على اعتماد الملفات الناجحة رغم فشل ملفات أخرى',
                                                key='v3_approve_partial') if partial else False
                    if st.button('اعتماد العمل',type='primary',
                                 disabled=not(approve and (not partial or approve_partial))):
                        try:
                            changed=drive_call(lambda api:approve_job(api,manifest,
                                                            allow_partial=approve_partial))
                            save_job_from_result(changed,rerun=True)
                        except Exception as exc:
                            st.error(str(exc))
                with col2:
                    eligible=[e['path'] for e in manifest['entries']
                              if e.get('backup_id') and e['state'] in ('updated','backed_up','update_failed')]
                    selected_paths=st.multiselect(
                        '\u0627\u062e\u062a\u0627\u0631\u064a \u0627\u0644\u0645\u0644\u0641\u0627\u062a \u0627\u0644\u0645\u0637\u0644\u0648\u0628 \u0627\u0633\u062a\u0639\u0627\u062f\u062a\u0647\u0627 (\u0627\u0644\u0643\u0644 \u0645\u062d\u062f\u062f \u0627\u0641\u062a\u0631\u0627\u0636\u064a\u064b\u0627)',
                        eligible,default=eligible,key='v3_rollback_paths')
                    rollback=st.checkbox('أؤكد رغبتي في استعادة النسخ القديمة وإلغاء التحديث',
                                         key='v3_rollback_confirm')
                    if st.button('تراجع: استعادة الملفات القديمة',
                                 disabled=not rollback or not selected_paths):
                        try:
                            changed=drive_call(lambda api:begin_rollback(
                                api,manifest,paths=selected_paths))
                            bar=st.progress(0)
                            changed=drive_call(lambda api:_run_safely_until_budget(
                                api,changed,'rollback',progress=bar,max_seconds=40))
                            save_job_from_result(changed,rerun=True)
                        except Exception as exc:
                            st.error(str(exc))
            if manifest['status'] in ('rolling_back','rollback_partial'):
                st.error('الاستعادة قيد التنفيذ أو جزئية؛ افتحي التقرير لمراجعة التعارضات.')
                if st.button('استكمال استعادة الدفعة التالية',type='primary'):
                    bar=st.progress(0)
                    try:
                        changed=drive_call(lambda api:rollback_batch(api,manifest,limit=5,
                                                progress=lambda n,t:bar.progress(n/max(t,1))))
                        save_job_from_result(changed,rerun=True)
                    except Exception as exc:
                        st.error('فشلت الاستعادة: '+str(exc))
                if st.button('استعادة جميع الملفات (قابل للاستئناف)',key='v3_rollback_all'):
                    bar=st.progress(0)
                    try:
                        changed=drive_call(lambda api:_run_safely_until_budget(
                            api,manifest,'rollback',progress=bar))
                        save_job_from_result(changed,rerun=True)
                    except Exception as exc:
                        st.error('توقفت الاستعادة، لا يزال سجلها محفوظًا: '+str(exc))
            if manifest['status']=='approved':
                st.success('تم اعتماد العملية. النسخ الاحتياطية محفوظة ولم تُحذف.')
                if is_owner:
                    admin_back=st.checkbox('بصفتي المالكة، أؤكد بدء استعادة عملية معتمدة',
                                          key='v3_admin_rollback')
                    if st.button('استعادة من عملية معتمدة',disabled=not admin_back):
                        try:
                            changed=drive_call(lambda api:begin_rollback(api,manifest))
                            save_job_from_result(changed,rerun=True)
                        except Exception as exc:
                            st.error(str(exc))
            if manifest['status']=='rolled_back':
                st.success('اكتملت الاستعادة. روابط الملفات ومعرفاتها الأصلية بقيت دون تغيير.')
        else:
            st.info('لم يبدأ التحديث بعد؛ انتقلي إلى التبويب الثاني.')

if current_step==STEPS[3]:
    st.subheader('التقارير وسجل عمليات V3')
    if manifest:
        rows=report_rows(manifest)
        st.dataframe(rows,hide_index=True,use_container_width=True)
        st.download_button('تصدير JSON',json.dumps({
            'job_id':manifest['job_id'],'state':manifest['status'],
            'created_at':manifest['created_at'],'files':rows,
        },ensure_ascii=False,indent=2).encode('utf-8'),
            file_name=f"mfw_v3_{manifest['job_id']}.json",mime='application/json')
        if rows:
            buffer=io.StringIO()
            writer=csv.DictWriter(buffer,fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            st.download_button('تصدير CSV',buffer.getvalue().encode('utf-8-sig'),
                               file_name=f"mfw_v3_{manifest['job_id']}.csv",mime='text/csv')
        with st.expander('آخر أحداث العملية'):
            st.dataframe(manifest.get('events',[])[::-1],hide_index=True,
                         use_container_width=True)
    st.divider()
    st.subheader('فتح عملية سابقة للتراجع أو المراجعة')
    if st.button('تحديث قائمة العمليات المحفوظة في Drive'):
        try:
            st.session_state.v3_recent_jobs=drive_call(lambda api:list_recent_jobs(
                api,configured_root=root_secret or None,limit=25))
        except Exception as exc:
            st.error('تعذر قراءة العمليات: '+str(exc))
    jobs=st.session_state.get('v3_recent_jobs',[])
    if jobs:
        labels={f"{j['job_id']} · {j.get('created_at') or ''}":j['manifest_id'] for j in jobs}
        selected=st.selectbox('العمليات الموجودة في مجلد الاستعادة',list(labels))
        if st.button('فتح هذه العملية'):
            try:
                selected_manifest=drive_call(lambda api:load_job(api,labels[selected]))
                ensure_actor(selected_manifest,email,is_owner)
                st.session_state.v3_manifest_id=selected_manifest['manifest_id']
                st.rerun()
            except Exception as exc:
                st.error('لا يمكن فتح هذه العملية: '+str(exc))
    manual_id=st.text_input('أو أدخلي معرّف ملف Manifest الموجود على Drive',
                            key='v3_manual_manifest_id')
    if st.button('فتح Manifest بالمعرّف',disabled=not manual_id.strip()):
        try:
            opened=drive_call(lambda api:load_job(api,extract_id(manual_id)))
            ensure_actor(opened,email,is_owner)
            st.session_state.v3_manifest_id=opened['manifest_id']
            st.rerun()
        except Exception as exc:
            st.error('تعذر الفتح: '+str(exc))
    if firebase_enabled:
        with st.expander('Firebase Firestore index'):
            if st.button('قراءة السجل الدائم'):
                try:
                    st.session_state.v3_db_rows=read_jobs(firebase_settings,email,is_owner)
                except Exception as exc:
                    st.error('Firebase Firestore read failed: '+type(exc).__name__)
            if st.session_state.get('v3_db_rows'):
                st.dataframe(st.session_state.v3_db_rows,hide_index=True,
                             use_container_width=True)

st.divider()
st.caption('مدعوم: PDF والصور الثابتة | الحد 100 MB للملف | لا يتم حذف الأصل | التراجع يستعيد المحتوى إذا لم يطرأ عليه تعديل خارجي')
