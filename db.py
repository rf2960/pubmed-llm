"""
db.py — SQLite database layer for Gene Function Lab.

The DB file lives on Google Drive:
  /content/drive/MyDrive/gene_function_lab/gene_function_lab.db

Both Colab (pipeline) and HF Spaces (web app) use this file.
HF Spaces downloads a fresh copy from Drive every hour via the Drive API.
"""

import os
import sqlite3
import pandas as pd
from contextlib import contextmanager
from typing import Optional

# DB path
DRIVE_DB_PATH  = "/content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db"
LOCAL_DB_PATH  = "./gene_function_lab.db"
DB_PATH        = DRIVE_DB_PATH if os.path.exists("/content/drive") else LOCAL_DB_PATH

#  Schema
_CREATE_PAPERS = """
CREATE TABLE IF NOT EXISTS papers (
    gene                      TEXT NOT NULL,
    pmid                      TEXT NOT NULL,
    pmcid                     TEXT,
    pubmed_link               TEXT,
    pmc_link                  TEXT,
    title                     TEXT,
    journal                   TEXT,
    year                      TEXT,
    doi                       TEXT,
    cancer_type               TEXT,
    functional_study          INTEGER,
    where_functional          TEXT,
    in_vitro                  INTEGER,
    in_vivo                   INTEGER,
    knockout                  INTEGER,
    knockdown                 INTEGER,
    shrna                     INTEGER,
    sirna                     INTEGER,
    crispr                    INTEGER,
    crispr_screen             INTEGER,
    impact_in_vitro           TEXT,
    impact_in_vivo            TEXT,
    confidence                REAL,
    confidence_functional     REAL,
    confidence_not_functional REAL,
    classified_by_llm         INTEGER,
    llm_rules_disagree        INTEGER,
    rules_functional          INTEGER,
    llm_reasoning             TEXT,
    evidence_perturbation     TEXT,
    evidence_in_vitro         TEXT,
    evidence_in_vivo          TEXT,
    evidence_crispr_screen    TEXT,
    total_evidence_sents      INTEGER,
    processed_at              TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (gene, pmid)
);
"""

_CREATE_GENES = """
CREATE TABLE IF NOT EXISTS genes (
    gene             TEXT PRIMARY KEY,
    first_run_at     TEXT DEFAULT (datetime('now')),
    last_run_at      TEXT DEFAULT (datetime('now')),
    total_papers     INTEGER DEFAULT 0,
    functional_count INTEGER DEFAULT 0
);
"""

_CREATE_SKIPPED = """
CREATE TABLE IF NOT EXISTS skipped_pmids (
    gene    TEXT NOT NULL,
    pmid    TEXT NOT NULL,
    reason  TEXT,
    PRIMARY KEY (gene, pmid)
);
"""

_CREATE_QUEUE = """
CREATE TABLE IF NOT EXISTS request_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    gene         TEXT NOT NULL,
    status       TEXT DEFAULT 'pending',
    requested_at TEXT DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT,
    requested_by TEXT,
    max_papers   INTEGER DEFAULT 300
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_papers_gene       ON papers(gene);",
    "CREATE INDEX IF NOT EXISTS idx_papers_functional ON papers(functional_study);",
    "CREATE INDEX IF NOT EXISTS idx_papers_cancer     ON papers(cancer_type);",
    "CREATE INDEX IF NOT EXISTS idx_papers_confidence ON papers(confidence);",
    "CREATE INDEX IF NOT EXISTS idx_queue_status      ON request_queue(status);",
    "CREATE INDEX IF NOT EXISTS idx_queue_gene        ON request_queue(gene);",
]


# Connection

@contextmanager
def get_conn(db_path: str = None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = None):
    """Create all tables and indexes if they don't exist."""
    with get_conn(db_path) as conn:
        conn.execute(_CREATE_PAPERS)
        conn.execute(_CREATE_GENES)
        conn.execute(_CREATE_SKIPPED)
        conn.execute(_CREATE_QUEUE)
        for idx in _INDEXES:
            conn.execute(idx)
    print(f"[DB] Initialized at {db_path or DB_PATH}")


# Paper writes

def upsert_paper(row: dict, db_path: str = None):
    cols   = [c for c in row.keys() if c != "processed_at"]
    ph     = ", ".join("?" * len(cols))
    cn     = ", ".join(cols)
    vals   = [int(v) if isinstance(v, bool) else v for v in (row[c] for c in cols)]
    sql    = f"INSERT OR REPLACE INTO papers ({cn}) VALUES ({ph})"
    with get_conn(db_path) as conn:
        conn.execute(sql, vals)


def upsert_papers_bulk(rows: list, db_path: str = None):
    if not rows:
        return
    cols  = [c for c in rows[0].keys() if c != "processed_at"]
    ph    = ", ".join("?" * len(cols))
    cn    = ", ".join(cols)
    sql   = f"INSERT OR REPLACE INTO papers ({cn}) VALUES ({ph})"
    def clean(r):
        return [int(v) if isinstance(v, bool) else v for v in (r.get(c) for c in cols)]
    with get_conn(db_path) as conn:
        conn.executemany(sql, [clean(r) for r in rows])


def mark_skipped(gene: str, pmid: str, reason: str = "", db_path: str = None):
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO skipped_pmids (gene, pmid, reason) VALUES (?,?,?)",
            (gene, pmid, reason)
        )


def update_gene_record(gene: str, db_path: str = None):
    with get_conn(db_path) as conn:
        conn.execute("""
            INSERT INTO genes (gene, first_run_at, last_run_at, total_papers, functional_count)
            VALUES (?,
                COALESCE((SELECT first_run_at FROM genes WHERE gene=?), datetime('now')),
                datetime('now'),
                (SELECT COUNT(*) FROM papers WHERE gene=?),
                (SELECT COUNT(*) FROM papers WHERE gene=? AND functional_study=1)
            )
            ON CONFLICT(gene) DO UPDATE SET
                last_run_at      = datetime('now'),
                total_papers     = excluded.total_papers,
                functional_count = excluded.functional_count
        """, (gene, gene, gene, gene))


# Paper reads 

def gene_is_processed(gene: str, db_path: str = None) -> bool:
    with get_conn(db_path) as conn:
        r = conn.execute(
            "SELECT COUNT(*) as n FROM papers WHERE gene=?", (gene.upper(),)
        ).fetchone()
        return r["n"] > 0


def get_processed_pmids(gene: str, db_path: str = None) -> set:
    with get_conn(db_path) as conn:
        paper_ids = {r["pmid"] for r in conn.execute(
            "SELECT pmid FROM papers WHERE gene=?", (gene.upper(),)
        ).fetchall()}
        skip_ids = {r["pmid"] for r in conn.execute(
            "SELECT pmid FROM skipped_pmids WHERE gene=?", (gene.upper(),)
        ).fetchall()}
    return paper_ids | skip_ids


def query_papers(
    genes:       list,
    cancer_type: str  = "all",
    functional:  str  = "all",
    min_conf:    float = 0.0,
    page:        int  = 1,
    per_page:    int  = 20,
    db_path:     str  = None,
) -> dict:
    genes_upper  = [g.upper() for g in genes]
    placeholders = ",".join("?" * len(genes_upper))
    where  = [f"gene IN ({placeholders})"]
    params = list(genes_upper)

    if cancer_type != "all":
        where.append("cancer_type = ?"); params.append(cancer_type)
    if functional == "true":
        where.append("functional_study = 1")
    elif functional == "false":
        where.append("functional_study = 0")
    if min_conf > 0:
        where.append("confidence >= ?"); params.append(min_conf)

    clause = " AND ".join(where)
    with get_conn(db_path) as conn:
        total  = conn.execute(
            f"SELECT COUNT(*) as n FROM papers WHERE {clause}", params
        ).fetchone()["n"]
        offset = (page - 1) * per_page
        rows   = conn.execute(
            f"SELECT * FROM papers WHERE {clause} ORDER BY gene, confidence DESC LIMIT ? OFFSET ?",
            params + [per_page, offset]
        ).fetchall()

    return {
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "rows":     [dict(r) for r in rows],
    }


def gene_summary(gene: str, min_conf: float = 0.0, db_path: str = None) -> dict:
    g = gene.upper()
    with get_conn(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) as n FROM papers WHERE gene=? AND confidence>=?", (g, min_conf)
        ).fetchone()["n"]
        func = conn.execute(
            "SELECT COUNT(*) as n FROM papers WHERE gene=? AND functional_study=1 AND confidence>=?",
            (g, min_conf)
        ).fetchone()["n"]
        cancer_rows = conn.execute(
            "SELECT cancer_type, COUNT(*) as n FROM papers WHERE gene=? AND functional_study=1 AND confidence>=? GROUP BY cancer_type",
            (g, min_conf)
        ).fetchall()
        cancer_counts = {r["cancer_type"]: r["n"] for r in cancer_rows}
        loc = conn.execute(
            """SELECT
                SUM(in_vitro=1 AND in_vivo=0) as vitro_only,
                SUM(in_vitro=0 AND in_vivo=1) as vivo_only,
                SUM(in_vitro=1 AND in_vivo=1) as both
               FROM papers WHERE gene=? AND functional_study=1 AND confidence>=?""",
            (g, min_conf)
        ).fetchone()
        methods = conn.execute(
            """SELECT SUM(knockout) as knockout, SUM(knockdown) as knockdown,
                      SUM(shrna) as shrna, SUM(sirna) as sirna,
                      SUM(crispr) as crispr, SUM(crispr_screen) as crispr_screen
               FROM papers WHERE gene=? AND functional_study=1 AND confidence>=?""",
            (g, min_conf)
        ).fetchone()
        top = conn.execute(
            """SELECT pmid, title, year, journal, pubmed_link, confidence,
                      llm_reasoning, cancer_type, in_vitro, in_vivo, where_functional
               FROM papers WHERE gene=? AND functional_study=1 AND confidence>=?
               ORDER BY confidence DESC LIMIT 5""",
            (g, min_conf)
        ).fetchall()

    return {
        "gene":          g,
        "total":         total,
        "functional":    func,
        "pancreatic":    cancer_counts.get("pancreatic", 0),
        "gi":            cancer_counts.get("gi", 0),
        "other_cancer":  cancer_counts.get("cancer", 0),
        "in_vitro_only": int(loc["vitro_only"] or 0),
        "in_vivo_only":  int(loc["vivo_only"]  or 0),
        "both":          int(loc["both"]        or 0),
        "methods":       {k: int(methods[k] or 0) for k in
                          ["knockout","knockdown","shrna","sirna","crispr","crispr_screen"]},
        "top_papers":    [dict(r) for r in top],
        "already_processed": gene_is_processed(g, db_path),
    }


def db_stats(db_path: str = None) -> dict:
    with get_conn(db_path) as conn:
        total  = conn.execute("SELECT COUNT(*) as n FROM papers").fetchone()["n"]
        genes  = conn.execute("SELECT COUNT(DISTINCT gene) as n FROM papers").fetchone()["n"]
        func   = conn.execute("SELECT COUNT(*) as n FROM papers WHERE functional_study=1").fetchone()["n"]
        top    = conn.execute(
            "SELECT gene, COUNT(*) as n FROM papers WHERE functional_study=1 GROUP BY gene ORDER BY n DESC LIMIT 10"
        ).fetchall()
        cancer = conn.execute(
            "SELECT cancer_type, COUNT(*) as n FROM papers WHERE functional_study=1 GROUP BY cancer_type"
        ).fetchall()
        all_g  = conn.execute("SELECT gene FROM genes ORDER BY last_run_at DESC").fetchall()

    return {
        "total_papers":          total,
        "total_genes":           genes,
        "functional_papers":     func,
        "top_genes":             [{"gene": r["gene"], "count": r["n"]} for r in top],
        "cancer_type_breakdown": {r["cancer_type"]: r["n"] for r in cancer},
        "all_genes":             [r["gene"] for r in all_g],
    }


def export_to_df(
    genes:       list  = None,
    cancer_type: str   = "all",
    functional:  str   = "all",
    min_conf:    float = 0.0,
    db_path:     str   = None,
) -> pd.DataFrame:
    where, params = [], []
    if genes:
        ph = ",".join("?" * len(genes))
        where.append(f"gene IN ({ph})")
        params += [g.upper() for g in genes]
    if cancer_type != "all":
        where.append("cancer_type = ?"); params.append(cancer_type)
    if functional == "true":
        where.append("functional_study = 1")
    elif functional == "false":
        where.append("functional_study = 0")
    if min_conf > 0:
        where.append("confidence >= ?"); params.append(min_conf)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_conn(db_path) as conn:
        df = pd.read_sql_query(
            f"SELECT * FROM papers {clause} ORDER BY gene, confidence DESC",
            conn, params=params
        )

    if df.empty:
        return df

    # Convert boolean columns to YES/NO
    bool_cols = ["functional_study","in_vitro","in_vivo","knockout","knockdown",
                 "shrna","sirna","crispr","crispr_screen"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: "YES" if x else "NO")

    # Convert confidence float to percentage string
    if "confidence" in df.columns:
        df["confidence"] = df["confidence"].apply(
            lambda x: f"{round(float(x)*100)}%" if pd.notna(x) else "0%"
        )

    # Rename columns for clean export
    df = df.rename(columns={
        "evidence_perturbation": "evidence_functional_study",
        "llm_reasoning":         "overall_decision",
    })

    # Select and order export columns
    export_cols = [
        "gene", "pmid", "pubmed_link", "title", "journal", "year",
        "cancer_type", "functional_study", "in_vitro", "in_vivo",
        "knockout", "knockdown", "shrna", "sirna", "crispr", "crispr_screen",
        "confidence", "evidence_functional_study", "evidence_in_vitro",
        "evidence_in_vivo", "evidence_crispr_screen", "overall_decision",
    ]
    return df[[c for c in export_cols if c in df.columns]]


# Request queue 

def queue_request(gene: str, requested_by: str = "", max_papers: int = 300,
                  db_path: str = None) -> dict:
    """
    Add a gene to the processing queue.
    Returns the queue entry dict.
    If gene is already pending/processing, returns that existing entry instead.
    """
    gene = gene.upper().strip()
    with get_conn(db_path) as conn:
        # Check for duplicate active request
        existing = conn.execute(
            "SELECT * FROM request_queue WHERE gene=? AND status IN ('pending','processing') ORDER BY id DESC LIMIT 1",
            (gene,)
        ).fetchone()
        if existing:
            return dict(existing)

        conn.execute(
            "INSERT INTO request_queue (gene, requested_by, max_papers) VALUES (?,?,?)",
            (gene, requested_by, max_papers)
        )
        row = conn.execute(
            "SELECT * FROM request_queue WHERE gene=? ORDER BY id DESC LIMIT 1", (gene,)
        ).fetchone()
        return dict(row)


def get_queue_status(gene: str, db_path: str = None) -> Optional[dict]:
    """Get the most recent queue entry for a gene."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM request_queue WHERE gene=? ORDER BY id DESC LIMIT 1",
            (gene.upper(),)
        ).fetchone()
        return dict(row) if row else None


def get_pending_requests(db_path: str = None) -> list:
    """Return all pending requests ordered by submission time."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM request_queue WHERE status='pending' ORDER BY requested_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_queue(db_path: str = None) -> list:
    """Return full queue history, most recent first."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM request_queue ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_queue_processing(queue_id: int, db_path: str = None):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE request_queue SET status='processing', started_at=datetime('now') WHERE id=?",
            (queue_id,)
        )


def mark_queue_done(queue_id: int, db_path: str = None):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE request_queue SET status='done', finished_at=datetime('now') WHERE id=?",
            (queue_id,)
        )


def mark_queue_error(queue_id: int, error: str, db_path: str = None):
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE request_queue SET status='error', finished_at=datetime('now'), error=? WHERE id=?",
            (error, queue_id)
        )

def upsert_papers_bulk(rows: list, db_path: str = None):
    if not rows:
        return

    BOOL_COLS = {'functional_study','in_vitro','in_vivo','knockout',
                 'knockdown','shrna','sirna','crispr','crispr_screen',
                 'classified_by_llm','llm_rules_disagree','rules_functional'}

    def clean(r):
        out = {}
        for k, v in r.items():
            if k in BOOL_COLS:
                if isinstance(v, str):
                    out[k] = 1 if v.upper() == 'YES' else 0
                else:
                    out[k] = int(bool(v))
            elif k == 'confidence' and isinstance(v, str) and '%' in v:
                out[k] = round(float(v.replace('%','').strip()) / 100, 4)
            elif isinstance(v, bool):
                out[k] = int(v)
            else:
                out[k] = v
        return out

    cleaned = [clean(r) for r in rows]
    cols  = [c for c in cleaned[0].keys() if c != "processed_at"]
    ph    = ", ".join("?" * len(cols))
    cn    = ", ".join(cols)
    sql   = f"INSERT OR REPLACE INTO papers ({cn}) VALUES ({ph})"
    with get_conn(db_path) as conn:
        conn.executemany(sql, [[r.get(c) for c in cols] for r in cleaned])