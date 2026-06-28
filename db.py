"""
db.py — SQLite database layer for Gene Function Lab.

The DB file lives on Google Drive:
  /content/drive/MyDrive/gene_function_lab/gene_function_lab.db

Both Colab (pipeline) and HF Spaces (web app) use this file.
HF Spaces downloads a fresh copy from Drive every hour via the Drive API.
"""

import json
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
    publication_types         TEXT,
    paper_type                TEXT,
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
    verification_status       TEXT,
    verification_reasons      TEXT,
    evidence_quality_score    REAL,
    search_relevance_score    REAL,
    evidence_retrieval_score  REAL,
    gene_match_quality        TEXT,
    review_recommendation     TEXT,
    review_reasons            TEXT,
    adjudication_status       TEXT,
    adjudication_reasons      TEXT,
    agentic_verifier_decision TEXT,
    agentic_verifier_reason   TEXT,
    agentic_verifier_quote    TEXT,
    agentic_verifier_needs_review INTEGER,
    structured_evidence_json  TEXT,
    agent_trace               TEXT,
    best_evidence_quote       TEXT,
    evidence_perturbation     TEXT,
    evidence_in_vitro         TEXT,
    evidence_in_vivo          TEXT,
    evidence_crispr_screen    TEXT,
    total_evidence_sents      INTEGER,
    gene_linked_evidence_sents INTEGER,
    review_status             TEXT DEFAULT 'unreviewed',
    review_label              TEXT,
    review_notes              TEXT,
    reviewed_by               TEXT,
    reviewed_at               TEXT,
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
    "CREATE INDEX IF NOT EXISTS idx_papers_paper_type ON papers(paper_type);",
    "CREATE INDEX IF NOT EXISTS idx_papers_confidence ON papers(confidence);",
    "CREATE INDEX IF NOT EXISTS idx_papers_review     ON papers(review_status);",
    "CREATE INDEX IF NOT EXISTS idx_queue_status      ON request_queue(status);",
    "CREATE INDEX IF NOT EXISTS idx_queue_gene        ON request_queue(gene);",
]

_PAPER_MIGRATIONS = {
    "review_status": "TEXT DEFAULT 'unreviewed'",
    "review_label": "TEXT",
    "review_notes": "TEXT",
    "reviewed_by": "TEXT",
    "reviewed_at": "TEXT",
    "verification_status": "TEXT",
    "verification_reasons": "TEXT",
    "evidence_quality_score": "REAL",
    "search_relevance_score": "REAL",
    "evidence_retrieval_score": "REAL",
    "publication_types": "TEXT",
    "paper_type": "TEXT",
    "best_evidence_quote": "TEXT",
    "gene_linked_evidence_sents": "INTEGER",
    "gene_match_quality": "TEXT",
    "adjudication_status": "TEXT",
    "adjudication_reasons": "TEXT",
    "agentic_verifier_decision": "TEXT",
    "agentic_verifier_reason": "TEXT",
    "agentic_verifier_quote": "TEXT",
    "agentic_verifier_needs_review": "INTEGER",
    "structured_evidence_json": "TEXT",
    "review_recommendation": "TEXT",
    "review_reasons": "TEXT",
    "agent_trace": "TEXT",
}


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
        paper_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(papers)").fetchall()
        }
        for col, decl in _PAPER_MIGRATIONS.items():
            if col not in paper_cols:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {decl}")
        for idx in _INDEXES:
            conn.execute(idx)
    print(f"[DB] Initialized at {db_path or DB_PATH}")


# Paper writes

def upsert_paper(row: dict, db_path: str = None):
    cols   = [c for c in row.keys() if c != "processed_at"]
    ph     = ", ".join("?" * len(cols))
    cn     = ", ".join(cols)
    vals   = [int(v) if isinstance(v, bool) else v for v in (row[c] for c in cols)]
    update_cols = [c for c in cols if c not in ("gene", "pmid")]
    update_sql = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    sql    = f"""
        INSERT INTO papers ({cn}) VALUES ({ph})
        ON CONFLICT(gene, pmid) DO UPDATE SET {update_sql}
    """
    with get_conn(db_path) as conn:
        conn.execute(sql, vals)


def upsert_papers_bulk(rows: list, db_path: str = None):
    if not rows:
        return
    cols_seen = []
    for row in rows:
        for key in row.keys():
            if key != "processed_at" and key not in cols_seen:
                cols_seen.append(key)
    cols  = cols_seen
    ph    = ", ".join("?" * len(cols))
    cn    = ", ".join(cols)
    update_cols = [c for c in cols if c not in ("gene", "pmid")]
    update_sql = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    sql   = f"""
        INSERT INTO papers ({cn}) VALUES ({ph})
        ON CONFLICT(gene, pmid) DO UPDATE SET {update_sql}
    """
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


def _text_present(value) -> bool:
    return bool(str(value or "").strip())


def _confidence_label(row: dict) -> str:
    conf = float(row.get("confidence") or 0)
    if conf >= 0.80:
        return "strong"
    if conf >= 0.60:
        return "moderate"
    return "weak"


def _review_signals(row: dict) -> list:
    signals = []
    conf = float(row.get("confidence") or 0)
    functional = bool(row.get("functional_study"))
    has_perturbation = _text_present(row.get("evidence_perturbation"))
    total_evidence = int(row.get("total_evidence_sents") or 0)

    if row.get("llm_rules_disagree"):
        signals.append("llm_rules_disagree")
    verifier_status = str(row.get("verification_status") or "").lower()
    if verifier_status in {"needs_review", "weak_support", "not_supported"}:
        signals.append(f"verifier_{verifier_status}")
    recommendation = str(row.get("review_recommendation") or "").lower()
    if recommendation in {"high_priority_review", "medium_priority_review"}:
        signals.append(recommendation)
    if row.get("gene_match_quality") == "weak":
        signals.append("weak_gene_match")
    if str(row.get("adjudication_status") or "").lower() == "challenge":
        signals.append("adjudicator_challenge")
    agentic_decision = str(row.get("agentic_verifier_decision") or "").lower()
    if agentic_decision == "challenge":
        signals.append("llm_verifier_challenge")
    elif agentic_decision == "unclear":
        signals.append("llm_verifier_unclear")
    if row.get("agentic_verifier_needs_review"):
        signals.append("llm_verifier_needs_review")
    if row.get("structured_evidence_json"):
        try:
            structured = json.loads(row.get("structured_evidence_json") or "{}")
            if bool(row.get("functional_study")) and structured.get("status") in {"partial", "missing"}:
                signals.append(f"structured_evidence_{structured.get('status')}")
        except Exception:
            signals.append("structured_evidence_unreadable")
    if str(row.get("paper_type") or "").lower() in {
        "review",
        "clinical_prognostic",
        "expression_association",
        "methods_or_dataset",
    }:
        signals.append("negative_paper_type")
    if int(row.get("gene_linked_evidence_sents") or 0) == 0:
        signals.append("no_gene_linked_evidence")
    if 0.45 <= conf <= 0.70:
        signals.append("borderline_confidence")
    if functional and not has_perturbation:
        signals.append("functional_without_perturbation_evidence")
    if total_evidence == 0:
        signals.append("no_extracted_evidence")
    if not row.get("classified_by_llm"):
        signals.append("rules_only")
    return signals


def _review_priority(row: dict) -> str:
    status = row.get("review_status") or "unreviewed"
    if status == "reviewed":
        return "reviewed"
    signals = _review_signals(row)
    if (
        "llm_rules_disagree" in signals
        or "functional_without_perturbation_evidence" in signals
        or "verifier_not_supported" in signals
        or "verifier_weak_support" in signals
        or "adjudicator_challenge" in signals
        or "llm_verifier_challenge" in signals
        or "high_priority_review" in signals
    ):
        return "high"
    if signals:
        return "medium"
    return "low"


def _review_summary(row: dict) -> str:
    """Return a short lab-facing explanation of why a row needs attention."""
    signals = set(_review_signals(row))
    bits: list[str] = []
    if "llm_verifier_challenge" in signals:
        bits.append("LLM verifier challenged the classification")
    elif "llm_verifier_unclear" in signals:
        bits.append("LLM verifier marked the evidence unclear")
    if "adjudicator_challenge" in signals:
        bits.append("automated adjudicator found an inconsistency")
    if "llm_rules_disagree" in signals:
        bits.append("rules and BioMistral disagree")
    if "verifier_not_supported" in signals:
        bits.append("deterministic verifier did not find enough support")
    elif "verifier_weak_support" in signals:
        bits.append("deterministic verifier found weak support")
    if "structured_evidence_missing" in signals:
        bits.append("structured extractor found missing core evidence")
    elif "structured_evidence_partial" in signals:
        bits.append("structured extractor found incomplete evidence")
    if "functional_without_perturbation_evidence" in signals:
        bits.append("functional label lacks perturbation evidence")
    if "no_gene_linked_evidence" in signals:
        bits.append("no direct gene-linked evidence sentence")
    if "weak_gene_match" in signals:
        bits.append("target gene match is weak")
    if "negative_paper_type" in signals:
        bits.append("paper type looks review/prognosis/expression/methods-like")
    if "borderline_confidence" in signals:
        bits.append("evidence-support score is borderline")
    if "rules_only" in signals:
        bits.append("classified without BioMistral")
    if not bits:
        return "No major automated review flags."
    return "; ".join(dict.fromkeys(bits[:5]))


def annotate_paper_row(row: dict) -> dict:
    out = dict(row)
    out["review_status"] = out.get("review_status") or "unreviewed"
    out["confidence_label"] = _confidence_label(out)
    out["review_priority"] = _review_priority(out)
    out["review_signals"] = _review_signals(out)
    out["review_summary"] = _review_summary(out)
    try:
        from confidence import explain_confidence_from_db_row

        explanation = explain_confidence_from_db_row(out)
        out["support_components"] = explanation["components"]
        out["support_reasons"] = explanation["reasons"]
    except Exception:
        out["support_components"] = {}
        out["support_reasons"] = []
    return out


def query_papers(
    genes:       list,
    cancer_type: str  = "all",
    functional:  str  = "all",
    review_status: str = "all",
    min_conf:    float = 0.0,
    page:        int  = 1,
    per_page:    int  = 20,
    paper_sort:  str  = "support_desc",
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
    elif functional == "in_vitro":
        where.append("functional_study = 1 AND in_vitro = 1 AND in_vivo = 0")
    elif functional == "in_vivo":
        where.append("functional_study = 1 AND in_vitro = 0 AND in_vivo = 1")
    elif functional == "both":
        where.append("functional_study = 1 AND in_vitro = 1 AND in_vivo = 1")
    if review_status != "all":
        where.append("COALESCE(review_status, 'unreviewed') = ?"); params.append(review_status)
    if min_conf > 0:
        where.append("confidence >= ?"); params.append(min_conf)

    clause = " AND ".join(where)
    order_sql, order_params = _paper_order_clause(genes_upper, paper_sort)
    with get_conn(db_path) as conn:
        total  = conn.execute(
            f"SELECT COUNT(*) as n FROM papers WHERE {clause}", params
        ).fetchone()["n"]
        offset = (page - 1) * per_page
        rows   = conn.execute(
            f"SELECT * FROM papers WHERE {clause} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            params + order_params + [per_page, offset]
        ).fetchall()

    return {
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "rows":     [annotate_paper_row(dict(r)) for r in rows],
    }


def _paper_order_clause(genes_upper: list, paper_sort: str = "support_desc") -> tuple:
    """Build a paper ORDER BY clause that keeps selected genes grouped.

    The first sort key preserves the gene card order supplied by the API.
    The second sort key controls paper order inside each gene group.
    """
    case_parts = [f"WHEN ? THEN {i}" for i, _ in enumerate(genes_upper)]
    gene_order = f"CASE gene {' '.join(case_parts)} ELSE {len(genes_upper)} END"
    sort_map = {
        "support_desc": "confidence DESC, year DESC, title COLLATE NOCASE ASC",
        "support_asc": "confidence ASC, year DESC, title COLLATE NOCASE ASC",
        "year_desc": "CAST(year AS INTEGER) DESC, confidence DESC, title COLLATE NOCASE ASC",
        "year_asc": "CAST(year AS INTEGER) ASC, confidence DESC, title COLLATE NOCASE ASC",
        "title_asc": "title COLLATE NOCASE ASC, year DESC, confidence DESC",
        "title_desc": "title COLLATE NOCASE DESC, year DESC, confidence DESC",
        "functional_desc": "functional_study DESC, confidence DESC, year DESC",
        "paper_type": "COALESCE(paper_type, 'unknown') COLLATE NOCASE ASC, confidence DESC, year DESC",
    }
    paper_order = sort_map.get(paper_sort, sort_map["support_desc"])
    return f"{gene_order}, {paper_order}", list(genes_upper)


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
        cancer_detail_rows = conn.execute(
            """SELECT COALESCE(cancer_type, 'unknown') as cancer_type,
                      COUNT(*) as total,
                      SUM(CASE WHEN functional_study=1 THEN 1 ELSE 0 END) as functional,
                      SUM(CASE WHEN functional_study=1 AND in_vitro=1 AND in_vivo=0 THEN 1 ELSE 0 END) as in_vitro_only,
                      SUM(CASE WHEN functional_study=1 AND in_vitro=0 AND in_vivo=1 THEN 1 ELSE 0 END) as in_vivo_only,
                      SUM(CASE WHEN functional_study=1 AND in_vitro=1 AND in_vivo=1 THEN 1 ELSE 0 END) as both,
                      SUM(CASE WHEN functional_study=1 AND COALESCE(in_vitro,0)=0 AND COALESCE(in_vivo,0)=0 THEN 1 ELSE 0 END) as unspecified,
                      SUM(CASE WHEN functional_study=1 AND knockout=1 THEN 1 ELSE 0 END) as knockout,
                      SUM(CASE WHEN functional_study=1 AND knockdown=1 THEN 1 ELSE 0 END) as knockdown,
                      SUM(CASE WHEN functional_study=1 AND shrna=1 THEN 1 ELSE 0 END) as shrna,
                      SUM(CASE WHEN functional_study=1 AND sirna=1 THEN 1 ELSE 0 END) as sirna,
                      SUM(CASE WHEN functional_study=1 AND crispr=1 THEN 1 ELSE 0 END) as crispr,
                      SUM(CASE WHEN functional_study=1 AND crispr_screen=1 THEN 1 ELSE 0 END) as crispr_screen
               FROM papers
               WHERE gene=? AND confidence>=?
               GROUP BY COALESCE(cancer_type, 'unknown')""",
            (g, min_conf)
        ).fetchall()
        def empty_cancer_detail():
            return {
                "total": 0,
                "functional": 0,
                "nonfunctional": 0,
                "functional_pct": 0.0,
                "in_vitro_only": 0,
                "in_vivo_only": 0,
                "both": 0,
                "unspecified": 0,
                "methods": {
                    "knockout": 0,
                    "knockdown": 0,
                    "shrna": 0,
                    "sirna": 0,
                    "crispr": 0,
                    "crispr_screen": 0,
                },
            }
        cancer_breakdown = {
            "pancreatic": empty_cancer_detail(),
            "gi": empty_cancer_detail(),
            "cancer": empty_cancer_detail(),
            "unknown": empty_cancer_detail(),
        }
        for row in cancer_detail_rows:
            key = row["cancer_type"] or "unknown"
            total_for_type = int(row["total"] or 0)
            functional_for_type = int(row["functional"] or 0)
            cancer_breakdown[key] = {
                "total": total_for_type,
                "functional": functional_for_type,
                "nonfunctional": max(total_for_type - functional_for_type, 0),
                "functional_pct": round(functional_for_type / total_for_type, 3) if total_for_type else 0.0,
                "in_vitro_only": int(row["in_vitro_only"] or 0),
                "in_vivo_only": int(row["in_vivo_only"] or 0),
                "both": int(row["both"] or 0),
                "unspecified": int(row["unspecified"] or 0),
                "methods": {
                    "knockout": int(row["knockout"] or 0),
                    "knockdown": int(row["knockdown"] or 0),
                    "shrna": int(row["shrna"] or 0),
                    "sirna": int(row["sirna"] or 0),
                    "crispr": int(row["crispr"] or 0),
                    "crispr_screen": int(row["crispr_screen"] or 0),
                },
            }
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
        support = conn.execute(
            """SELECT AVG(confidence) as avg_confidence,
                      SUM(confidence >= 0.80) as strong,
                      SUM(confidence >= 0.60 AND confidence < 0.80) as moderate,
                      SUM(confidence < 0.60) as weak,
                      SUM(COALESCE(llm_rules_disagree, 0)=1) as disagree,
                      SUM(COALESCE(review_status, 'unreviewed') != 'reviewed') as unreviewed
               FROM papers
               WHERE gene=? AND functional_study=1 AND confidence>=?""",
            (g, min_conf)
        ).fetchone()
        top = conn.execute(
            """SELECT pmid, title, year, journal, pubmed_link, confidence,
                      llm_reasoning, cancer_type, in_vitro, in_vivo, where_functional,
                      review_status, review_label
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
        "unknown_cancer": cancer_counts.get("unknown", 0),
        "cancer_breakdown": cancer_breakdown,
        "in_vitro_only": int(loc["vitro_only"] or 0),
        "in_vivo_only":  int(loc["vivo_only"]  or 0),
        "both":          int(loc["both"]        or 0),
        "methods":       {k: int(methods[k] or 0) for k in
                          ["knockout","knockdown","shrna","sirna","crispr","crispr_screen"]},
        "support_avg":   float(support["avg_confidence"] or 0.0),
        "support_strong": int(support["strong"] or 0),
        "support_moderate": int(support["moderate"] or 0),
        "support_weak":  int(support["weak"] or 0),
        "support_disagree": int(support["disagree"] or 0),
        "support_unreviewed": int(support["unreviewed"] or 0),
        "top_papers":    [annotate_paper_row(dict(r)) for r in top],
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
        review = conn.execute(
            "SELECT COALESCE(review_status, 'unreviewed') as status, COUNT(*) as n FROM papers GROUP BY COALESCE(review_status, 'unreviewed')"
        ).fetchall()

    return {
        "total_papers":          total,
        "total_genes":           genes,
        "functional_papers":     func,
        "top_genes":             [{"gene": r["gene"], "count": r["n"]} for r in top],
        "cancer_type_breakdown": {r["cancer_type"]: r["n"] for r in cancer},
        "review_status_breakdown": {r["status"]: r["n"] for r in review},
        "all_genes":             [r["gene"] for r in all_g],
    }


def export_to_df(
    genes:       list  = None,
    cancer_type: str   = "all",
    functional:  str   = "all",
    review_status: str = "all",
    min_conf:    float = 0.0,
    paper_sort:  str   = "support_desc",
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
    elif functional == "in_vitro":
        where.append("functional_study = 1 AND in_vitro = 1 AND in_vivo = 0")
    elif functional == "in_vivo":
        where.append("functional_study = 1 AND in_vitro = 0 AND in_vivo = 1")
    elif functional == "both":
        where.append("functional_study = 1 AND in_vitro = 1 AND in_vivo = 1")
    if review_status != "all":
        where.append("COALESCE(review_status, 'unreviewed') = ?"); params.append(review_status)
    if min_conf > 0:
        where.append("confidence >= ?"); params.append(min_conf)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = "gene, confidence DESC"
    order_params = []
    if genes:
        order_sql, order_params = _paper_order_clause([g.upper() for g in genes], paper_sort)
    with get_conn(db_path) as conn:
        df = pd.read_sql_query(
            f"SELECT * FROM papers {clause} ORDER BY {order_sql}",
            conn, params=params + order_params
        )

    if df.empty:
        return df

    # Convert boolean columns to YES/NO
    bool_cols = ["functional_study","in_vitro","in_vivo","knockout","knockdown",
                 "shrna","sirna","crispr","crispr_screen"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: "YES" if x else "NO")

    # Keep evidence support as a 0-1 decimal. It is a heuristic support score,
    # not a calibrated probability or percentage.
    if "confidence" in df.columns:
        df["confidence"] = df["confidence"].apply(
            lambda x: round(float(x), 3) if pd.notna(x) else 0.0
        )

    # Rename columns for clean export
    df = df.rename(columns={
        "evidence_perturbation": "evidence_functional_study",
        "llm_reasoning":         "overall_decision",
    })

    # Select and order export columns
    export_cols = [
        "gene", "pmid", "pubmed_link", "title", "journal", "year",
        "cancer_type", "publication_types", "paper_type", "functional_study", "in_vitro", "in_vivo",
        "knockout", "knockdown", "shrna", "sirna", "crispr", "crispr_screen",
        "confidence", "best_evidence_quote", "evidence_functional_study", "evidence_in_vitro",
        "evidence_in_vivo", "evidence_crispr_screen", "overall_decision",
        "verification_status", "verification_reasons", "evidence_quality_score",
        "search_relevance_score", "evidence_retrieval_score", "gene_linked_evidence_sents",
        "gene_match_quality", "adjudication_status", "adjudication_reasons",
        "review_recommendation", "review_reasons", "structured_evidence_json",
        "review_status", "review_label", "review_notes", "reviewed_by", "reviewed_at",
    ]
    return df[[c for c in export_cols if c in df.columns]]


def export_gene_summary_to_df(
    genes: list = None,
    min_conf: float = 0.0,
    db_path: str = None,
) -> pd.DataFrame:
    """Return one row per gene with review-friendly aggregate evidence counts.

    This is intentionally gene-level, not paper-level. It is used by the
    website's "Export Gene Summary CSV" action so lab members can compare
    selected genes without manually aggregating the paper table.
    """
    with get_conn(db_path) as conn:
        if genes:
            gene_list = [g.upper().strip() for g in genes if g and g.strip()]
        else:
            rows = conn.execute("SELECT gene FROM genes ORDER BY gene").fetchall()
            gene_list = [r["gene"] for r in rows]
            if not gene_list:
                rows = conn.execute("SELECT DISTINCT gene FROM papers ORDER BY gene").fetchall()
                gene_list = [r["gene"] for r in rows]

    summary_rows = []
    cancer_labels = [
        ("pancreatic", "pancreatic"),
        ("gi", "gi"),
        ("cancer", "other_cancer"),
        ("unknown", "unknown"),
    ]
    for gene in gene_list:
        summary = gene_summary(gene, min_conf=min_conf, db_path=db_path)
        methods = summary.get("methods", {})
        row = {
            "gene": summary["gene"],
            "total_papers": summary["total"],
            "functional_papers": summary["functional"],
            "functional_pct": round(summary["functional"] / summary["total"], 3) if summary["total"] else 0.0,
            "in_vitro_only": summary["in_vitro_only"],
            "in_vivo_only": summary["in_vivo_only"],
            "both_in_vitro_and_in_vivo": summary["both"],
            "unspecified_evidence": max(
                summary["functional"]
                - summary["in_vitro_only"]
                - summary["in_vivo_only"]
                - summary["both"],
                0,
            ),
            "knockout": methods.get("knockout", 0),
            "knockdown": methods.get("knockdown", 0),
            "shrna": methods.get("shrna", 0),
            "sirna": methods.get("sirna", 0),
            "crispr": methods.get("crispr", 0),
            "crispr_screen": methods.get("crispr_screen", 0),
            "avg_evidence_support": round(summary["support_avg"], 3),
            "strong_support_papers": summary["support_strong"],
            "moderate_support_papers": summary["support_moderate"],
            "weak_support_papers": summary["support_weak"],
            "rule_llm_disagreements": summary["support_disagree"],
            "unreviewed_functional_papers": summary["support_unreviewed"],
            "already_processed": summary["already_processed"],
        }
        for source_key, export_key in cancer_labels:
            detail = summary.get("cancer_breakdown", {}).get(source_key, {})
            row[f"{export_key}_total_papers"] = int(detail.get("total", 0))
            row[f"{export_key}_functional_papers"] = int(detail.get("functional", 0))
            row[f"{export_key}_functional_pct"] = round(float(detail.get("functional_pct", 0.0)), 3)
            row[f"{export_key}_in_vitro_only"] = int(detail.get("in_vitro_only", 0))
            row[f"{export_key}_in_vivo_only"] = int(detail.get("in_vivo_only", 0))
            row[f"{export_key}_both_in_vitro_and_in_vivo"] = int(detail.get("both", 0))
            row[f"{export_key}_unspecified_evidence"] = int(detail.get("unspecified", 0))
            detail_methods = detail.get("methods", {})
            row[f"{export_key}_knockout"] = int(detail_methods.get("knockout", 0))
            row[f"{export_key}_knockdown"] = int(detail_methods.get("knockdown", 0))
            row[f"{export_key}_shrna"] = int(detail_methods.get("shrna", 0))
            row[f"{export_key}_sirna"] = int(detail_methods.get("sirna", 0))
            row[f"{export_key}_crispr"] = int(detail_methods.get("crispr", 0))
            row[f"{export_key}_crispr_screen"] = int(detail_methods.get("crispr_screen", 0))
        summary_rows.append(row)

    return pd.DataFrame(summary_rows)


def update_paper_review(
    gene: str,
    pmid: str,
    review_status: str = "unreviewed",
    review_label: str = "",
    review_notes: str = "",
    reviewed_by: str = "",
    db_path: str = None,
) -> Optional[dict]:
    allowed_status = {"unreviewed", "needs_review", "reviewed"}
    allowed_label = {"", "functional", "not_functional", "unclear"}
    status = (review_status or "unreviewed").strip().lower()
    label = (review_label or "").strip().lower()
    if status not in allowed_status:
        raise ValueError(f"Invalid review_status: {review_status}")
    if label not in allowed_label:
        raise ValueError(f"Invalid review_label: {review_label}")

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT gene, pmid FROM papers WHERE gene=? AND pmid=?",
            (gene.upper().strip(), str(pmid).strip()),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE papers
            SET review_status=?,
                review_label=?,
                review_notes=?,
                reviewed_by=?,
                reviewed_at=datetime('now')
            WHERE gene=? AND pmid=?
            """,
            (
                status,
                label or None,
                review_notes.strip(),
                reviewed_by.strip(),
                gene.upper().strip(),
                str(pmid).strip(),
            ),
        )
        updated = conn.execute(
            "SELECT * FROM papers WHERE gene=? AND pmid=?",
            (gene.upper().strip(), str(pmid).strip()),
        ).fetchone()
        return annotate_paper_row(dict(updated)) if updated else None


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


def reset_processing_requests(db_path: str = None) -> int:
    """
    Return abandoned processing requests to pending.

    Use this after a Colab/runtime interruption. A request can be left in
    processing if the notebook disconnects before marking it done or error.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute("""
            UPDATE request_queue
            SET status='pending',
                started_at=NULL,
                error=COALESCE(error, 'Reset after interrupted worker')
            WHERE status='processing'
        """)
        return cur.rowcount


def upsert_papers_bulk(rows: list, db_path: str = None):
    if not rows:
        return

    BOOL_COLS = {'functional_study','in_vitro','in_vivo','knockout',
                 'knockdown','shrna','sirna','crispr','crispr_screen',
                 'classified_by_llm','llm_rules_disagree','rules_functional'}
    REMAP_COLS = {
        'evidence_functional_study': 'evidence_perturbation',
        'overall_decision': 'llm_reasoning',
    }

    def clean(r):
        out = {}
        for k, v in r.items():
            k = REMAP_COLS.get(k, k)
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
    with get_conn(db_path) as conn:
        table_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(papers)").fetchall()
        }
        cols  = [c for c in cleaned[0].keys() if c != "processed_at" and c in table_cols]
        ph    = ", ".join("?" * len(cols))
        cn    = ", ".join(cols)
        update_cols = [c for c in cols if c not in ("gene", "pmid")]
        update_sql = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        sql   = f"""
            INSERT INTO papers ({cn}) VALUES ({ph})
            ON CONFLICT(gene, pmid) DO UPDATE SET {update_sql}
        """
        conn.executemany(sql, [[r.get(c) for c in cols] for r in cleaned])
