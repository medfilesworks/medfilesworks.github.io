"""Optional durable Supabase audit log; never stores tokens or file bytes."""
from datetime import datetime, timezone
import requests


def audit_row(email, action, file_id, file_name, status, detail=''):
    return {
        'actor_email': email,
        'action': action,
        'file_id': file_id,
        'file_name': file_name,
        'status': status,
        'detail': str(detail)[:1500],
        'created_at': datetime.now(timezone.utc).isoformat(),
    }


def save_audit(row, supabase_url='', service_key=''):
    """Return (persisted, message); caller may show warning if not persisted."""
    if not supabase_url or not service_key:
        return False, 'سجل الجلسة فقط: لم يتم ربط قاعدة بيانات Supabase بعد'
    try:
        r = requests.post(
            supabase_url.rstrip('/') + '/rest/v1/medfilesworks_audit',
            headers={'apikey':service_key, 'Authorization':'Bearer '+service_key,
                     'Content-Type':'application/json', 'Prefer':'return=minimal'},
            json=row, timeout=15,
        )
        r.raise_for_status()
        return True, ''
    except requests.RequestException as exc:
        return False, f'تعذر حفظ السجل الدائم ({type(exc).__name__})'


def fetch_audit(supabase_url, service_key, limit=250):
    if not supabase_url or not service_key:
        return []
    r = requests.get(
        supabase_url.rstrip('/')+'/rest/v1/medfilesworks_audit',
        params={'select':'created_at,actor_email,action,file_id,file_name,status,detail',
                'order':'created_at.desc', 'limit':min(limit,500)},
        headers={'apikey':service_key,'Authorization':'Bearer '+service_key},timeout=15,
    )
    r.raise_for_status()
    return r.json()
