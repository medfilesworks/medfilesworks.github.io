import io
import re
import secrets
import time
import hmac
import hashlib
import base64
from collections import Counter
from urllib.parse import urlencode

import requests
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

st.set_page_config(page_title="medfilesworks", page_icon="📁", layout="wide")
st.markdown("""<style>html,body,[class*=css]{direction:rtl} input,textarea{direction:ltr!important} .stCode{direction:ltr}</style>""",unsafe_allow_html=True)
st.title("medfilesworks | إدارة تحديث ملفات Drive")
st.caption("تحديث محتوى الملفات مع الاحتفاظ بمعرّفاتها وروابطها وصلاحياتها")

SCOPES = ["https://www.googleapis.com/auth/drive", "openid", "https://www.googleapis.com/auth/userinfo.email"]
NATIVE_PREFIX = "application/vnd.google-apps."

def config(key):
    try: return st.secrets[key]
    except Exception: st.error(f"إعداد ناقص: {key}"); st.stop()

CLIENT_ID, CLIENT_SECRET, REDIRECT = config("GOOGLE_CLIENT_ID"),config("GOOGLE_CLIENT_SECRET"),config("REDIRECT_URI")
ALLOWED = {str(x).strip().lower() for x in config("ALLOWED_EMAILS")}
STATE_SECRET = config("STATE_SIGNING_KEY")

def make_state():
    payload = f"{int(time.time())}.{secrets.token_urlsafe(16)}"
    signature = hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{signature}".encode()).decode()

def valid_state(value):
    try:
        decoded = base64.urlsafe_b64decode(value.encode()).decode()
        timestamp, nonce, signature = decoded.split(".")
        payload = f"{timestamp}.{nonce}"
        expected = hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected) and 0 <= time.time() - int(timestamp) < 600
    except (ValueError, UnicodeError, TypeError):
        return False

def google_login():
    state = make_state()
    query = urlencode({"client_id":CLIENT_ID,"redirect_uri":REDIRECT,"response_type":"code","scope":" ".join(SCOPES),"access_type":"offline","prompt":"consent","state":state})
    st.link_button("تسجيل الدخول باستخدام Google", "https://accounts.google.com/o/oauth2/v2/auth?"+query)

def exchange(code):
    r=requests.post("https://oauth2.googleapis.com/token",data={"code":code,"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"redirect_uri":REDIRECT,"grant_type":"authorization_code"},timeout=30)
    r.raise_for_status()
    return r.json()

def credentials():
    tok=st.session_state.get("tokens")
    if not tok: return None
    creds=Credentials(token=tok.get("access_token"),refresh_token=tok.get("refresh_token"),token_uri="https://oauth2.googleapis.com/token",client_id=CLIENT_ID,client_secret=CLIENT_SECRET,scopes=SCOPES)
    # Refresh only when a Drive call signals token expiration; token response has no datetime here.
    return creds

def drive():
    return build("drive","v3",credentials=credentials(),cache_discovery=False)

def run_drive(fn):
    try: return fn(drive())
    except Exception as exc:
        if "401" in str(exc) and st.session_state.get("tokens",{}).get("refresh_token"):
            creds=credentials();creds.refresh(Request());st.session_state.tokens["access_token"]=creds.token
            return fn(drive())
        raise

def extract_id(value):
    value=value.strip()
    for pat in [r"/file/d/([\w-]+)",r"/folders/([\w-]+)",r"[?&]id=([\w-]+)",r"/d/([\w-]+)"]:
        match=re.search(pat,value)
        if match:return match.group(1)
    if re.fullmatch(r"[\w-]{20,}",value):return value
    raise ValueError("رابط أو معرّف غير صالح")

def metadata(api,file_id):
    return api.files().get(fileId=file_id,fields="id,name,mimeType,webViewLink,size,parents,capabilities(canEdit)",supportsAllDrives=True).execute()

def list_folder(api,folder_id):
    files=[];page=None
    while True:
        result=api.files().list(q=f"'{folder_id}' in parents and trashed = false",fields="nextPageToken,files(id,name,mimeType,size,capabilities(canEdit))",pageSize=1000,pageToken=page,supportsAllDrives=True,includeItemsFromAllDrives=True).execute()
        files.extend(result.get("files",[]));page=result.get("nextPageToken")
        if not page:break
    return files

def check_target(f):
    if f.get("mimeType","").startswith(NATIVE_PREFIX):raise ValueError("ملف Google أصلي؛ غير مدعوم في الاستبدال الثنائي")
    if f.get("capabilities",{}).get("canEdit") is False:raise PermissionError("لا توجد صلاحية تعديل")

def update_from_bytes(api,target,content,mime):
    check_target(target)
    media=MediaIoBaseUpload(io.BytesIO(content),mimetype=mime or target["mimeType"],resumable=True)
    return api.files().update(fileId=target["id"],media_body=media,fields="id,name,webViewLink",supportsAllDrives=True).execute()

def download_bytes(api,source):
    if source["mimeType"].startswith(NATIVE_PREFIX):raise ValueError("ملف مصدر من نوع Google أصلي؛ غير مدعوم")
    return api.files().get_media(fileId=source["id"],supportsAllDrives=True).execute()

# OAuth state is signed and time-limited; browser session state may reset on redirect.
params=st.query_params
if "code" in params:
    if not valid_state(params.get("state", "")):
        st.error("انتهت جلسة تسجيل الدخول أو فشل التحقق الأمني. أعيدي المحاولة.")
        if st.button("إعادة تسجيل الدخول"):st.query_params.clear();st.rerun()
        st.stop()
    try:
        st.session_state.tokens=exchange(params["code"])
        st.query_params.clear()
        st.rerun()
    except Exception as e:st.error(f"تعذر تسجيل الدخول: {e}");st.stop()

if not credentials():
    st.info("سجّلي الدخول بحساب Google لديه صلاحية تعديل الملفات المطلوبة.")
    google_login();st.stop()

try:
    profile=requests.get("https://www.googleapis.com/oauth2/v2/userinfo",headers={"Authorization":"Bearer "+st.session_state.tokens["access_token"]},timeout=15)
    if profile.status_code==401 and st.session_state.tokens.get("refresh_token"):
        cr=credentials();cr.refresh(Request());st.session_state.tokens["access_token"]=cr.token
        profile=requests.get("https://www.googleapis.com/oauth2/v2/userinfo",headers={"Authorization":"Bearer "+cr.token},timeout=15)
    profile.raise_for_status();email=profile.json().get("email","").lower()
except Exception as e:
    st.error(f"تعذر التحقق من الحساب: {e}");st.stop()

if email not in ALLOWED:
    st.error("هذا الحساب غير مدرج في قائمة المستخدمين المسموح لهم.")
    if st.button("تسجيل الخروج"):st.session_state.clear();st.rerun()
    st.stop()

st.success(f"متصل بالحساب: {email} — {'المالكة' if email == 'marah.h.musallam21@gmail.com' else 'محرر'}")
if st.button("تسجيل الخروج"):
    st.session_state.clear();st.rerun()

browser,manual,bulk=st.tabs(["استعراض ملفات Drive","تحديث يدوي","تحديث جماعي بين مجلدين"])
with browser:
    st.subheader("استعراض المجلدات المشتركة")
    st.caption("تظهر العناصر التي يستطيع الحساب الحالي الوصول إليها. للمجلدات المشتركة معك، قد تحتاج إلى فتحها من رابطها أولًا.")
    folder_link=st.text_input("رابط مجلد لعرض محتوياته (اختياري)",key="browse_folder")
    if st.button("عرض المجلد",key="browse_button"):
        try:
            fid=extract_id(folder_link)
            folder=run_drive(lambda a:metadata(a,fid))
            if folder['mimeType']!='application/vnd.google-apps.folder':raise ValueError('الرابط ليس مجلدًا')
            entries=run_drive(lambda a:list_folder(a,fid))
            st.session_state.browse_items=entries
            st.session_state.browse_title=folder['name']
        except Exception as e:st.error(str(e))
    if st.button("عرض المجلدات المتاحة",key="browse_root"):
        try:
            def get_folders(api):
                items=[];token=None
                while True:
                    res=api.files().list(q="mimeType = 'application/vnd.google-apps.folder' and trashed = false",fields="nextPageToken,files(id,name,mimeType,webViewLink)",pageSize=1000,pageToken=token).execute()
                    items+=res.get('files',[]);token=res.get('nextPageToken')
                    if not token:break
                return items
            st.session_state.browse_items=run_drive(get_folders)
            st.session_state.browse_title='المجلدات المتاحة'
        except Exception as e:st.error(str(e))
    if 'browse_items' in st.session_state:
        st.write(st.session_state.get('browse_title','الملفات'))
        items=st.session_state.browse_items
        st.dataframe([{'الاسم':f['name'],'النوع':'مجلد' if f['mimeType']=='application/vnd.google-apps.folder' else 'ملف','الرابط':'https://drive.google.com/drive/folders/'+f['id'] if f['mimeType']=='application/vnd.google-apps.folder' else 'https://drive.google.com/file/d/'+f['id']+'/view'} for f in items],hide_index=True,use_container_width=True)
        st.caption('للتحديث اليدوي انسخي رابط الملف من الجدول إلى تبويب التحديث اليدوي.')

with manual:
    st.subheader("استبدال ملف واحد")
    url=st.text_input("رابط الملف الأصلي أو معرّفه")
    upload=st.file_uploader("النسخة الجديدة من جهازك",key="manual_upload")
    if st.button("فحص الملف",key="manual_check"):
        try:
            f=run_drive(lambda a:metadata(a,extract_id(url)));check_target(f)
            st.session_state.manual_target=f
            st.success(f"الملف المستهدف: {f['name']} — {f['id']}")
        except Exception as e:st.session_state.pop("manual_target",None);st.error(str(e))
    target=st.session_state.get("manual_target")
    if target:
        st.warning(f"سيُستبدل محتوى {target['name']} ولن يتغير معرّفه.")
        confirm=st.checkbox("أؤكد أنني راجعت الملف المستهدف والنسخة الجديدة",key="manual_confirm")
        if st.button("تنفيذ التحديث",disabled=not(confirm and upload),key="manual_update"):
            try:
                # Ensure source URL has not changed since preview.
                if extract_id(url)!=target["id"]:raise ValueError("تغير الرابط؛ أعيدي الفحص")
                latest=run_drive(lambda a:metadata(a,target["id"]))
                result=run_drive(lambda a:update_from_bytes(a,latest,upload.getvalue(),latest["mimeType"]))
                st.success("تم التحديث بنجاح. معرّف الملف ثابت: "+result["id"])
                st.session_state.pop("manual_target",None)
            except Exception as e:st.error(f"فشل التحديث: {e}")

with bulk:
    st.subheader("مطابقة الملفات بالاسم بين مجلدين")
    st.info("المطابقة تشمل الملفات الموجودة مباشرة داخل المجلدين، وليس المجلدات الفرعية. لا تُحذف الملفات غير المتطابقة.")
    old=st.text_input("رابط مجلد الملفات الأصلية",key="old_folder")
    new=st.text_input("رابط مجلد النسخ الجديدة",key="new_folder")
    if st.button("فحص المجلدين"):
        try:
            old_id,new_id=extract_id(old),extract_id(new)
            if old_id==new_id:raise ValueError("يجب اختيار مجلدين مختلفين")
            originals=run_drive(lambda a:list_folder(a,old_id))
            updates=run_drive(lambda a:list_folder(a,new_id))
            oc=Counter(f["name"] for f in originals);nc=Counter(f["name"] for f in updates)
            oldmap={f["name"]:f for f in originals if oc[f["name"]]==1}
            newmap={f["name"]:f for f in updates if nc[f["name"]]==1}
            pairs=[];skipped=[]
            for name,source in newmap.items():
                if name not in oldmap:skipped.append((name,"لا يوجد ملف أصلي مطابق"));continue
                target=oldmap[name]
                if source["mimeType"].startswith(NATIVE_PREFIX) or target["mimeType"].startswith(NATIVE_PREFIX):skipped.append((name,"نوع Google أصلي غير مدعوم"));continue
                if target.get("capabilities",{}).get("canEdit") is False:skipped.append((name,"لا توجد صلاحية تعديل"));continue
                if source["mimeType"]!=target["mimeType"]:skipped.append((name,"نوع الملف مختلف"));continue
                pairs.append((target,source))
            for name in sorted(set(n for n,c in oc.items() if c>1)|set(n for n,c in nc.items() if c>1)):skipped.append((name,"اسم مكرر؛ يلزم معالجة يدوية"))
            st.session_state.bulk_plan={"old":old_id,"new":new_id,"pairs":pairs,"skipped":skipped}
        except Exception as e:st.session_state.pop("bulk_plan",None);st.error(str(e))
    plan=st.session_state.get("bulk_plan")
    if plan:
        st.metric("ملفات جاهزة للتحديث",len(plan["pairs"]))
        if plan["pairs"]:st.dataframe([{"الاسم":t["name"],"المعرّف الأصلي":t["id"],"معرّف النسخة الجديدة":s["id"]} for t,s in plan["pairs"]],hide_index=True,use_container_width=True)
        if plan["skipped"]:st.warning(f"ملفات مستبعدة: {len(plan['skipped'])}");st.dataframe([{"الاسم":n,"السبب":why} for n,why in plan["skipped"]],hide_index=True,use_container_width=True)
        agree=st.checkbox("راجعت المطابقات وأوافق على استبدال محتوى الملفات الأصلية",key="bulk_agree")
        if st.button("بدء التحديث الجماعي",disabled=not agree or not plan["pairs"]):
            if extract_id(old)!=plan["old"] or extract_id(new)!=plan["new"]:st.error("تغير أحد المجلدين؛ أعيدي الفحص")
            else:
                results=[];progress=st.progress(0)
                for i,(target,source) in enumerate(plan["pairs"]):
                    try:
                        def process(api):
                            current=metadata(api,target["id"])
                            if current["name"]!=target["name"]:raise ValueError("تغير اسم الملف الأصلي بعد الفحص")
                            src=metadata(api,source["id"])
                            if src["name"]!=source["name"]:raise ValueError("تغير اسم الملف الجديد بعد الفحص")
                            payload=download_bytes(api,src)
                            return update_from_bytes(api,current,payload,src["mimeType"])
                        result=run_drive(process)
                        results.append({"الملف":target["name"],"النتيجة":"نجاح","المعرّف":result["id"]})
                    except Exception as e:results.append({"الملف":target["name"],"النتيجة":"فشل: "+str(e),"المعرّف":target["id"]})
                    progress.progress((i+1)/len(plan["pairs"]))
                st.dataframe(results,hide_index=True,use_container_width=True)
                import csv
                buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=["الملف","النتيجة","المعرّف"]);writer.writeheader();writer.writerows(results)
                st.download_button("تنزيل تقرير النتائج CSV",buffer.getvalue().encode("utf-8-sig"),"update_report.csv","text/csv")
                st.session_state.pop("bulk_plan",None)

st.divider()
st.caption("تُحفظ رموز Google في جلسة التطبيق فقط، ولا تُخزن في المستودع. استخدمي HTTPS ولا تضعي أسرار OAuth في ملفات GitHub.")
