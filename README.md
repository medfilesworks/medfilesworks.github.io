# medfilesworks v2 — Google Drive in-place updater

## What this version implements

- Google OAuth sign-in and an explicit allowlist for three accounts.
- Drive folder browser, including entering subfolders and opening Drive links.
- Manual binary-file replacement **in place** with `files.update` (retains existing Drive file ID/link).
- Recursive bulk update: match files by **relative path + filename** across two folder trees; show a dry-run preview and excluded items; require confirmation before writing.
- Safeguards: rejects Google Docs/Sheets/Slides native formats, duplicate names in a folder, same/overlapping roots, wrong MIME, changed target/source versions, files over 100 MB, empty payloads, and files without editing rights.
- Session audit log and CSV export. **Optional** persistent Supabase audit (owner sees full log).
- No delete API calls; no automatic changes without an explicit confirmation.

## Existing production app: setup and OAuth

1. Open **https://share.streamlit.io** > **My apps** > `medfilesworks` > **Settings** > **Secrets**.
2. Paste the values from `.streamlit_secrets_example.toml`, substituting your real Google OAuth Client ID/Secret. Keep the secret only in Streamlit Secrets, **never in GitHub**.
3. `REDIRECT_URI` must exactly match the **actual deployed URL** (e.g. `https://medfilesworks.streamlit.app`) with the same trailing slash convention on both sides.
4. In **Google Cloud Console** > **Google Auth Platform** > **Clients** > the Web application OAuth client > **Authorized redirect URIs**, add the identical URL. Click Save.
5. In **Google Auth Platform** > **Audience**, add the three users under **Test users** (if the app is in Testing). In **Data access**, configure the requested Google Drive scope. Google may warn about an unverified app; Drive broad scope is restricted and may require verification for wider distribution.
6. Google Drive API must be enabled for the Google Cloud project. The Drive owner must grant Editor permission to the folders/files that editors should update.
7. Save Streamlit Secrets and restart the app if needed. Open the site and sign in with one of the allowed Google accounts.
8. Test on a disposable PDF first: copy its URL, update the content in **تحديث ملف**, then reopen the *same* URL to verify the content changed while the ID did not.

### Secrets template

```toml
GOOGLE_CLIENT_ID = "YOUR_GOOGLE_CLIENT_ID"
GOOGLE_CLIENT_SECRET = "YOUR_GOOGLE_CLIENT_SECRET"
REDIRECT_URI = "https://medfilesworks.streamlit.app"
STATE_SIGNING_KEY = "YOUR_64_CHARACTER_RANDOM_HEX_KEY"
ALLOWED_EMAILS = ["marah.h.musallam21@gmail.com", "dr.khalil.lectures@gmail.com", "pmsf.education@gmail.com"]
SUPABASE_URL = ""
SUPABASE_SERVICE_ROLE_KEY = ""
```

To generate a random signing key locally: `python -c "import secrets; print(secrets.token_hex(32))"`.

## Updating the existing GitHub repository to v2

Upload these files **to the repository root** (replacing the old files with the same names):

- `app.py`
- `drive_ops.py`
- `audit.py`
- `requirements.txt`
- `README.md`
- `.gitignore` (optional but recommended)
- `supabase_schema.sql` (optional, used only for permanent logs)

**Do not upload** `.streamlit_secrets_example.toml` with real values; this example has placeholders only. Do not upload `secrets.toml` or your Google OAuth JSON. The existing Streamlit deployment normally redeploys on commits to the configured branch.

## Optional: durable audit history with Supabase

1. Create/select a Supabase project.
2. Open **SQL Editor** and run `supabase_schema.sql` once.
3. From Supabase project settings/API, copy the Project URL and the **service_role key** (secret; server-only).
4. In **Streamlit Secrets** set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.
5. Restart the app. The owner account can see audit records from all users. Editors see their current-session records. This log records metadata, not file content or OAuth tokens.

Without Supabase, the audit tab is **session-only** and not persistent. This is explicitly indicated in the UI.

## Limitations and safety

- **No production OAuth session hardening guarantee.** The state parameter is signed and expires after 10 minutes, but this implementation does not persist a per-browser OAuth nonce/PKCE verifier across browser redirects. Use a dedicated session store + PKCE before using this with sensitive/high-stakes data at scale.
- Google OAuth in **Testing** can issue refresh tokens that expire after 7 days. Users may need to sign in again.
- Replaces content of **binary files** (PDF, images, Office uploads, ZIPs, etc.), not native Google Docs/Sheets/Slides.
- A successful update preserves Drive file IDs/URLs but does not guarantee retention of *all* historical metadata, Drive revisions, or third-party caches. Test on a disposable file.
- Max 100 MB per source/update, 2000 files per scan, max depth 12. No unattended background sync.
- A single bulk run is **not atomic**: successful files remain updated if later files fail. Download the CSV report.
- This code was syntax checked and pure-function unit tested. **Live Google OAuth/Drive integration and Streamlit deployment were not tested in this environment** (credentials/network not available).

## Development

`pip install -r requirements.txt`

`streamlit run app.py`

Run tests (requires pytest): `pytest -q tests/`
