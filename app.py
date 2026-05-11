"""
app.py — HF Spaces web app for Gene Function Lab.
CPU only — no model, no torch.
Password protected via APP_PASSWORD secret.
Uses URL token auth (works inside HF Spaces iframe).
"""

import io
import os
import time
import traceback
from flask import Flask, jsonify, request, send_from_directory, Response, redirect

import db as database
import drive_sync

app = Flask(__name__, template_folder="templates")

#Password config
APP_PASSWORD  = os.environ.get("APP_PASSWORD", "").strip()
COOKIE_NAME   = "gfl_token"
COOKIE_MAXAGE = 60 * 60 * 24 * 7   # 7 days

def _check_auth() -> bool:
    if not APP_PASSWORD:
        return True
    # Check URL token, form token, or cookie
    return (
        request.args.get("token", "")              == APP_PASSWORD or
        request.form.get("token", "")              == APP_PASSWORD or
        request.cookies.get(COOKIE_NAME, "")       == APP_PASSWORD or
        request.headers.get("X-Auth-Token", "")    == APP_PASSWORD
    )

def require_auth():
    if not _check_auth():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")
    return None

# Startup
print("[Startup] Syncing DB from Drive...")
try:
    synced = drive_sync.sync_db_from_drive(force=True)
    print(f"[Startup] Sync result: {synced}")
except Exception as e:
    print(f"[Startup] Sync error: {e}")
    print(traceback.format_exc())

try:
    database.init_db(drive_sync.LOCAL_DB_PATH)
    database.DB_PATH = drive_sync.LOCAL_DB_PATH
except Exception as e:
    print(f"[Startup] DB init error: {e}")

try:
    drive_sync.start_background_sync()
except Exception as e:
    print(f"[Startup] Background sync warning: {e}")

def _local_db():
    return drive_sync.LOCAL_DB_PATH

# Force db module to use the same path
database.DB_PATH = drive_sync.LOCAL_DB_PATH

# Login page 
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gene Function Lab — Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{min-height:100vh;background:#f4f5f7;display:flex;align-items:center;justify-content:center;font-family:'Inter',sans-serif;}
.card{background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;padding:40px;width:100%;max-width:380px;box-shadow:0 2px 16px rgba(0,0,0,.06);}
.logo{font-size:18px;font-weight:700;color:#111827;letter-spacing:-.01em;margin-bottom:4px;}
.logo em{color:#2563eb;font-style:normal;}
.sub{font-size:13px;color:#6b7280;margin-bottom:28px;}
label{font-size:13px;font-weight:500;color:#111827;display:block;margin-bottom:6px;}
input[type=password]{width:100%;background:#f4f5f7;border:1px solid #e5e7eb;color:#111827;font-family:'Inter',sans-serif;font-size:14px;padding:10px 13px;border-radius:8px;outline:none;transition:border-color .15s,box-shadow .15s;}
input[type=password]:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.1);background:#fff;}
input[type=password]::placeholder{color:#9ca3af;}
button{width:100%;margin-top:14px;padding:11px;background:#2563eb;color:#fff;border:none;border-radius:8px;font-family:'Inter',sans-serif;font-size:14px;font-weight:500;cursor:pointer;transition:background .15s;}
button:hover{background:#1d4ed8;}
.err{margin-top:12px;padding:11px 14px;background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;font-size:13px;color:#dc2626;display:none;}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Gene Function <em>Lab</em></div>
  <div class="sub">Lab access only — enter the password to continue</div>
  <form id="loginForm">
    <label>Password</label>
    <input type="password" id="pwd" autofocus placeholder="Enter lab password">
    <button type="submit">Enter Lab</button>
    <div class="err" id="err"></div>
  </form>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit', async function(e) {
  e.preventDefault();
  const pwd = document.getElementById('pwd').value.trim();
  const err = document.getElementById('err');
  err.style.display = 'none';
  try {
    const r = await fetch('/api/auth', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pwd})
    });
    const d = await r.json();
    if (d.ok) {
      // Store token in sessionStorage and redirect
      sessionStorage.setItem('gfl_token', pwd);
      window.location.href = '/?token=' + encodeURIComponent(pwd);
    } else {
      err.textContent = 'Incorrect password. Try again.';
      err.style.display = 'block';
    }
  } catch(ex) {
    err.textContent = 'Error connecting. Try again.';
    err.style.display = 'block';
  }
});
</script>
</body>
</html>"""

@app.route("/login")
def login():
    return LOGIN_HTML

@app.route("/api/auth", methods=["POST"])
def api_auth():
    """Check password — called by login page JS."""
    body = request.get_json(force=True) or {}
    pwd  = body.get("password", "").strip()
    if not APP_PASSWORD:
        return jsonify({"ok": True})
    if pwd == APP_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401

# Routes
@app.route("/")
def index():
    auth = require_auth()
    if auth: return auth
    return send_from_directory("templates", "index.html")

@app.route("/api/stats")
def api_stats():
    auth = require_auth()
    if auth: return auth
    try:
        stats = database.db_stats(_local_db())
        stats["last_sync"] = time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(drive_sync._last_sync)
        ) if drive_sync._last_sync else "never"
        return jsonify(stats)
    except Exception as e:
        return jsonify({
            "total_papers": 0, "total_genes": 0, "functional_papers": 0,
            "top_genes": [], "all_genes": [], "last_sync": "error", "error": str(e)
        })

@app.route("/api/query")
def api_query():
    auth = require_auth()
    if auth: return auth
    genes_raw   = request.args.get("genes", "").strip()
    min_conf    = float(request.args.get("min_conf", 0.0))
    cancer_type = request.args.get("cancer_type", "all")
    functional  = request.args.get("functional",  "all")
    page        = max(1,   int(request.args.get("page",     1)))
    per_page    = min(100, int(request.args.get("per_page", 20)))
    if not genes_raw:
        return jsonify({"error": "genes parameter is required"}), 400
    genes = [g.strip().upper() for g in genes_raw.split(",") if g.strip()]
    try:
        result = database.query_papers(
            genes=genes, cancer_type=cancer_type, functional=functional,
            min_conf=min_conf, page=page, per_page=per_page, db_path=_local_db(),
        )
        result["summaries"] = {
            g: database.gene_summary(g, min_conf=min_conf, db_path=_local_db())
            for g in genes
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "rows": [], "total": 0, "summaries": {}}), 200

@app.route("/api/check")
def api_check():
    auth = require_auth()
    if auth: return auth
    genes_raw = request.args.get("genes", "").strip()
    if not genes_raw:
        return jsonify({"error": "genes parameter required"}), 400
    genes = [g.strip().upper() for g in genes_raw.split(",") if g.strip()]
    result = {}
    for gene in genes:
        in_db    = database.gene_is_processed(gene, _local_db())
        q_status = database.get_queue_status(gene, _local_db())
        result[gene] = {
            "in_db":        in_db,
            "queue_status": q_status,
            "summary":      database.gene_summary(gene, db_path=_local_db()) if in_db else None,
        }
    return jsonify(result)

@app.route("/api/request", methods=["POST"])
def api_request():
    auth = require_auth()
    if auth: return auth
    body       = request.get_json(force=True) or {}
    gene       = (body.get("gene") or "").strip().upper()
    max_papers = int(body.get("max_papers", 300))
    if not gene:
        return jsonify({"error": "gene is required"}), 400
    if not gene.replace("-","").replace("_","").isalnum() or len(gene) > 20:
        return jsonify({"error": "Invalid gene name"}), 400
    if database.gene_is_processed(gene, _local_db()):
        return jsonify({
            "status": "already_processed", "gene": gene,
            "summary": database.gene_summary(gene, db_path=_local_db()),
            "message": f"{gene} is already in the database. Search it to see results."
        })
    existing = database.get_queue_status(gene, _local_db())
    if existing and existing["status"] in ("pending", "processing"):
        return jsonify({
            "status": existing["status"], "gene": gene, "queue": existing,
            "message": f"{gene} is already {existing['status']}. Check back soon."
        })
    try:
        entry = drive_sync.write_queue_entry_to_drive(
            gene=gene, requested_by=request.remote_addr or "", max_papers=max_papers
        )
        return jsonify({
            "status": "queued", "gene": gene, "queue": entry,
            "message": f"{gene} has been queued. Results will appear once the pipeline runs."
        })
    except Exception as e:
        return jsonify({"error": f"Could not queue request: {e}"}), 500

@app.route("/api/queue")
def api_queue():
    auth = require_auth()
    if auth: return auth
    try:
        queue   = database.get_all_queue(_local_db())
        pending = [q for q in queue if q["status"] == "pending"]
        return jsonify({"queue": queue, "pending_count": len(pending)})
    except Exception as e:
        return jsonify({"queue": [], "pending_count": 0, "error": str(e)})

@app.route("/api/export")
def api_export():
    auth = require_auth()
    if auth: return auth
    genes_raw   = request.args.get("genes", "").strip()
    min_conf    = float(request.args.get("min_conf", 0.0))
    cancer_type = request.args.get("cancer_type", "all")
    functional  = request.args.get("functional",  "all")
    genes = [g.strip().upper() for g in genes_raw.split(",") if g.strip()] if genes_raw else None
    try:
        df = database.export_to_df(
            genes=genes, cancer_type=cancer_type,
            functional=functional, min_conf=min_conf, db_path=_local_db(),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    fname = f"functional_papers_{'_'.join(genes[:3]) if genes else 'all'}.csv"
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )

@app.route("/api/sync", methods=["GET", "POST"])
def api_sync():
    auth = require_auth()
    if auth: return auth
    try:
        drive_sync._drive_file_id = None
        synced = drive_sync.sync_db_from_drive(force=True)
        return jsonify({"synced": synced, "message": "DB refreshed from Drive."})
    except Exception as e:
        return jsonify({"synced": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)