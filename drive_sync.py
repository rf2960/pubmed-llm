import os
import io
import json
import time
import threading
import traceback

SYNC_INTERVAL_SECONDS = 3600
DRIVE_FILE_NAME       = "gene_function_lab.db"
LOCAL_DB_PATH         = os.environ.get(
    "GENE_LAB_DB_PATH",
    "/tmp/pubmed_llm/gene_function_lab/gene_function_lab.db",
)
DRIVE_FILE_ID_ENV     = "GOOGLE_DRIVE_DB_FILE_ID"
DRIVE_FOLDER_ID_ENV   = "GOOGLE_DRIVE_FOLDER_ID"

_last_sync     = 0
_sync_lock     = threading.Lock()
_drive_file_id = None


def _get_drive_service(write=False):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError("Run: pip install google-auth google-auth-httplib2 google-api-python-client")

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON secret not set.")

    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    if info.get("type") != "service_account":
        raise RuntimeError(f"Wrong type: {info.get('type')} — expected service_account")

    print(f"[Drive] Service account: {info.get('client_email', 'unknown')}")

    scopes = (
    ["https://www.googleapis.com/auth/drive"]
    if write else
    ["https://www.googleapis.com/auth/drive.readonly"]  # drop drive.file
    )
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_db_file_id(service):
    global _drive_file_id
    if _drive_file_id:
        return _drive_file_id

    configured_file_id = os.environ.get(DRIVE_FILE_ID_ENV, "").strip()
    if configured_file_id:
        _drive_file_id = configured_file_id
        print(f"[Drive] Using {DRIVE_FILE_ID_ENV}: {_drive_file_id}")
        return _drive_file_id

    folder_id = os.environ.get(DRIVE_FOLDER_ID_ENV, "").strip()
    query_parts = [f"name='{DRIVE_FILE_NAME}'", "trashed=false"]
    if folder_id:
        query_parts.append(f"'{folder_id}' in parents")
    query = " and ".join(query_parts)

    print(f"[Drive] Searching for {DRIVE_FILE_NAME}...")
    if folder_id:
        print(f"[Drive] Restricting search to folder id: {folder_id}")
    results = service.files().list(
        q=query,
        fields="files(id, name, modifiedTime, size)",
        orderBy="modifiedTime desc",
        pageSize=10,
    ).execute()

    files = results.get("files", [])
    print(f"[Drive] Found {len(files)} file(s)")

    if not files:
        raise FileNotFoundError(
            f"{DRIVE_FILE_NAME} not found. "
            "Share the gene_function_lab folder with the service account email."
        )
    if len(files) > 1:
        print("[Drive] Multiple DB files found; using most recently modified.")
        for f in files:
            print(
                f"  - id={f.get('id')} modified={f.get('modifiedTime')} "
                f"size={f.get('size')} name={f.get('name')}"
            )

    _drive_file_id = files[0]["id"]
    print(f"[Drive] File id: {_drive_file_id}")
    return _drive_file_id


def sync_db_from_drive(force=False):
    global _last_sync
    now = time.time()
    if not force and (now - _last_sync) < SYNC_INTERVAL_SECONDS:
        return False

    with _sync_lock:
        if not force and (time.time() - _last_sync) < SYNC_INTERVAL_SECONDS:
            return False
        try:
            from googleapiclient.http import MediaIoBaseDownload
            service    = _get_drive_service()
            file_id    = _find_db_file_id(service)
            request    = service.files().get_media(fileId=file_id)
            buf        = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            tmp = LOCAL_DB_PATH + ".tmp"
            os.makedirs(os.path.dirname(LOCAL_DB_PATH), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(buf.getvalue())
            os.replace(tmp, LOCAL_DB_PATH)
            try:
                import db as database
                database.init_db(LOCAL_DB_PATH)
                database.DB_PATH = LOCAL_DB_PATH
            except Exception as migrate_error:
                print(f"[Drive] DB migration warning after sync: {migrate_error}")
                print(traceback.format_exc())
            _last_sync = time.time()
            print(f"[Drive] Synced successfully ({len(buf.getvalue())//1024}KB)")
            return True
        except Exception as e:
            print(f"[Drive] Sync failed: {e}")
            print(traceback.format_exc())
            return False


def upload_db_to_drive(local_path=LOCAL_DB_PATH):
    try:
        from googleapiclient.http import MediaFileUpload
        service = _get_drive_service(write=True)
        file_id = _find_db_file_id(service)
        media   = MediaFileUpload(local_path, mimetype="application/octet-stream")
        service.files().update(fileId=file_id, media_body=media).execute()
        print("[Drive] Uploaded to Drive.")
        return True
    except Exception as e:
        print(f"[Drive] Upload failed: {e}")
        print(traceback.format_exc())
        return False


def write_queue_entry_to_drive(gene, requested_by="", max_papers=300):
    import db as database
    sync_db_from_drive(force=True)
    entry = database.queue_request(
        gene=gene, requested_by=requested_by,
        max_papers=max_papers, db_path=LOCAL_DB_PATH
    )
    upload_db_to_drive(LOCAL_DB_PATH)
    return entry


def start_background_sync():
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    print(f"[Drive] Secret present: {bool(sa)}, length: {len(sa)}")

    def _loop():
        print("[Drive] Running initial sync...")
        try:
            sync_db_from_drive(force=True)
        except Exception as e:
            print(f"[Drive] Initial sync error: {e}")
            print(traceback.format_exc())
        while True:
            time.sleep(SYNC_INTERVAL_SECONDS)
            try:
                sync_db_from_drive()
            except Exception as e:
                print(f"[Drive] Hourly sync error: {e}")
                print(traceback.format_exc())

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print("[Drive] Background sync started.")
