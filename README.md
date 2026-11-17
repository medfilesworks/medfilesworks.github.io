# medfilesworks — Google Drive updater

A Streamlit web application for three authorized Google accounts. Updates existing non-native Drive files **in place** using Drive API `files.update`, retaining the existing file ID and link.

## Accounts
- Owner/admin label: `marah.h.musallam21@gmail.com`
- Editors: `dr.khalil.lectures@gmail.com`, `pmsf.education@gmail.com`

The app does not transfer Drive file ownership. Users must have Editor access to each target folder/file in Google Drive. The app's owner label does not confer extra Drive permissions.

## Deploy from iPad
1. Create a **private** GitHub repository named `medfilesworks` and upload `app.py`, `requirements.txt`, `.gitignore`, and this README. Do **not** upload credentials or secrets.
2. At https://share.streamlit.io deploy a new app from that repository; main file path: `app.py`. Choose a subdomain.
3. In Google Cloud → Google Auth Platform → Clients → `medfilesworks-web`, add the **exact** deployed app URL (including final slash, if used in `REDIRECT_URI`) under **Authorized redirect URIs**. No JavaScript origin is needed for this server-side authorization flow.
4. In Streamlit Cloud → App settings → Secrets, paste the entries from `.streamlit_secrets_example.toml`, replacing placeholder values. `REDIRECT_URI` must exactly match the authorized redirect URI. Generate a unique 32+ character `STATE_SIGNING_KEY` and keep it secret.
5. In Google Auth Platform → Audience → Test users, add the three authorized emails. Ensure Google Drive API is enabled and Google Auth Platform → Data access has the requested Drive scope `https://www.googleapis.com/auth/drive`. Google will display a consent warning for an unverified testing app. Testing-mode OAuth refresh tokens can expire in seven days; users may need to sign in again.
6. Open the Streamlit app URL and log in with an authorized Google account. Share the target folders in Drive with the editors using **Editor** access.
7. Test first on a disposable PDF: copy its existing URL, update it in the manual tab, reopen the original URL and verify the contents changed and file ID stayed identical.

## Features
- Google OAuth sign-in with allowlist of three emails
- List available folders or browse contents by folder link
- Manual update of an existing PDF/image/Office/binary file by Drive URL
- Bulk matching of same-named files from two folder links (direct children only), preview, confirm, CSV results

## Important limitations
- Not a continuously running sync service. Bulk updates are initiated manually.
- Does not recursively traverse subfolders.
- Google Docs/Sheets/Slides native files cannot be replaced with binary content via this workflow.
- The source and target file MIME types must match for bulk updates; manual updates should use the same file format as the target.
- In-memory Streamlit session stores OAuth tokens; sign-in can be lost on session restart. A persistent multi-user production deployment should use hardened server-side session storage and OAuth state/PKCE validation before handling sensitive data.
- Large files may hit memory or platform upload limits; bulk transfers are in-memory.
- App-level activity history is not persisted; download the CSV after a bulk update. Google Drive may retain version/activity information according to its own policies.
- This source has been syntax-checked, **not** integration-tested with live Google Drive credentials.
