"""Optional Firebase Cloud Firestore job index.

Drive manifests remain the source of truth for every update and rollback.
No private credentials or file bytes are written into Firestore.
"""
from __future__ import annotations

from collections.abc import Mapping
try:
    from google.cloud import firestore
except ImportError:  # Local test env may not have the optional SDK.
    firestore = None

from v3_engine import summary

FIRESTORE_SCOPE = 'https://www.googleapis.com/auth/datastore'


def firebase_is_configured(settings: Mapping | None) -> bool:
    if not settings:
        return False
    return all(str(settings.get(key, '')).strip() for key in (
        'project_id', 'client_email', 'private_key',
    ))


def firestore_client(settings: Mapping):
    if not firebase_is_configured(settings):
        raise ValueError('Firebase service account is not fully configured in Streamlit Secrets')
    # Firebase Admin service account private key is server-only. Never print it.
    info = {
        'type': 'service_account',
        'project_id': str(settings['project_id']),
        'client_email': str(settings['client_email']),
        'private_key': str(settings['private_key']).replace('\\n', '\n'),
        'token_uri': 'https://oauth2.googleapis.com/token',
    }
    if settings.get('private_key_id'):
        info['private_key_id'] = str(settings['private_key_id'])
    if firestore is None:
        raise RuntimeError('Install google-cloud-firestore from requirements.txt')
    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=[FIRESTORE_SCOPE],
    )
    return firestore.Client(project=info['project_id'], credentials=credentials)


def sync_job(manifest: dict, settings: Mapping | None, client=None):
    if not firebase_is_configured(settings) and client is None:
        return False, 'Firestore index not connected; recovery manifest saved on Drive'
    counts = summary(manifest)
    row = {
        'job_id': manifest['job_id'],
        'actor_email': manifest['actor'],
        'manifest_file_id': manifest['manifest_id'],
        'source_folder_id': manifest['source_folder'],
        'target_folder_id': manifest.get('target_folder'),
        'state': manifest['status'],
        'total_files': counts['total'],
        'updated_files': counts['updated'],
        'failed_files': counts['failed'],
        'skipped_files': counts['skipped'],
        'created_at': manifest['created_at'],
        'updated_at': manifest['updated_at'],
        'approved_at': manifest.get('approved_at'),
    }
    try:
        database = client if client is not None else firestore_client(settings)
        database.collection('mfw_v3_jobs').document(manifest['job_id']).set(row, merge=True)
        return True, ''
    except Exception as exc:
        # Avoid returning SDK exception text, which may contain configuration details.
        return False, 'Firestore index unavailable: ' + type(exc).__name__


def read_jobs(settings: Mapping | None, actor: str, owner=False, limit=100, client=None):
    if not firebase_is_configured(settings) and client is None:
        return []
    database = client if client is not None else firestore_client(settings)
    collection = database.collection('mfw_v3_jobs')
    safe_limit = max(1, min(int(limit), 500))
    if owner:
        query = collection.order_by('updated_at', direction=firestore.Query.DESCENDING).limit(safe_limit)
    else:
        # No composite index required: filter by actor, then sort in Python.
        query = collection.where('actor_email', '==', actor.lower()).limit(safe_limit)
    rows = [snapshot.to_dict() for snapshot in query.stream()]
    if not owner:
        rows = [row for row in rows if row.get('actor_email') == actor.lower()]
        rows.sort(key=lambda row: row.get('updated_at', ''), reverse=True)
    fields = ('job_id', 'actor_email', 'manifest_file_id', 'state', 'total_files',
              'updated_files', 'failed_files', 'skipped_files', 'created_at', 'updated_at')
    return [{key: row.get(key) for key in fields} for row in rows[:safe_limit]]
