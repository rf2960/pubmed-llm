# Deployment Guide

This guide explains how to maintain the Hugging Face Space deployment safely.

## Deployment Model

The Hugging Face Space is a lightweight Flask website. It should not run BioMistral or process PubMed batches. It only serves the search/review UI and syncs a SQLite DB file from Google Drive.

```text
Google Drive DB file
  -> Hugging Face Space startup sync
  -> Flask website reads local DB copy
  -> optional background sync
  -> optional review/upload writes back to Drive
```

## Required Space Files

The Space must contain:

```text
app.py
db.py
drive_sync.py
Dockerfile
requirements.txt
templates/index.html
```

Do not replace the app with a root-level `index.html`. The Flask route serves `templates/index.html`.

## Required Secrets

Configure these in the Hugging Face Space settings, not in Git:

| Secret | Required | Purpose |
| --- | --- | --- |
| `APP_PASSWORD` | Recommended | Password for the website login. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes for Drive sync | Full Google service-account JSON content. |
| `GOOGLE_DRIVE_DB_FILE_ID` | Strongly recommended | Exact file id for the shared `gene_function_lab.db`. |
| `GOOGLE_DRIVE_FOLDER_ID` | Optional | Limits Drive lookup to one folder when file id is not set. |
| `GENE_LAB_DB_PATH` | Optional | Local DB path inside the Space container. Defaults to `/tmp/...`. |

Security notes:

- Never commit service-account JSON, Hugging Face tokens, or passwords.
- Treat authenticated URLs with `token=...` as private because they may appear in logs or browser history.
- If a secret was pasted into chat, email, or Git by mistake, rotate it.

## Google Drive Permissions

The service-account email inside `GOOGLE_SERVICE_ACCOUNT_JSON` must have access to the Drive DB file.

Recommended setup:

1. Put the real SQLite file in one stable Drive location.
2. Open the file's Share dialog.
3. Share it with the service-account `client_email`.
4. Copy the file id from the Drive URL.
5. Set `GOOGLE_DRIVE_DB_FILE_ID` to that id in the Space.

Folder links are not DB file ids. Use the file URL for `gene_function_lab.db`, not the folder URL.

## Updating The Website Code

For a duplicated Space, update these files when GitHub has a code change:

- `app.py`
- `db.py`
- `drive_sync.py`
- `templates/index.html`
- `requirements.txt` if dependencies changed

Then restart or rebuild the Space.

If you use the Hugging Face web UI:

1. Open the Space.
2. Go to **Files**.
3. Upload the changed file(s) into the same paths.
4. Wait for rebuild/restart.
5. Open **Logs** and confirm startup sync succeeded.

Expected healthy startup log:

```text
[Drive] Service account: ...
[Drive] Using GOOGLE_DRIVE_DB_FILE_ID: ...
[Drive] Synced successfully (...)
[Startup] Sync result: True
[DB] Initialized at /tmp/pubmed_llm/gene_function_lab/gene_function_lab.db
```

## Updating The Database

The worker usually writes to the DB mounted in Google Drive:

```text
/content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db
```

If `GOOGLE_SERVICE_ACCOUNT_JSON` is not set in Colab, the API upload step may fail, but the mounted Drive DB can still be updated. In that case, the website must sync from the exact same Drive DB file.

After a successful worker run:

1. Confirm the DB counts changed with `scripts/check_queue_status.py`.
2. Trigger website sync from the authenticated website or restart the Space.
3. Confirm website counts changed.

## Common Deployment Failures

| Error | Meaning | Fix |
| --- | --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON` | Secret value is not raw JSON | Paste the full JSON object, not a filename or escaped wrapper. |
| `File not found` for Drive file id | Service account cannot access that file or id is wrong | Share the DB file with the service account and verify the id is the DB file id. |
| `no such column: review_status` | Space loaded an older DB schema | Use current `db.py`; restart the Space so migrations run after sync. |
| `0 papers - 0 genes - synced error` | Startup sync failed | Check logs, secrets, file id, and Drive permissions. |
| Website counts stay old | Space syncs a different DB copy | Set `GOOGLE_DRIVE_DB_FILE_ID` everywhere. |

## Backup Recommendation

Before large monthly refresh runs, make a copy of `gene_function_lab.db` in Drive with the date in the filename. Keep at least the last two monthly copies.
