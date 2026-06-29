"""Audit algorithm/backfill fields in the SQLite database.

Use this after code updates, confidence recompute, or selected reprocessing to
see whether rows have the fields needed by the current website and review
workflow. This script is read-only and does not call PubMed or BioMistral.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
from collections import Counter
from pathlib import Path

from common import add_common_args, configure_db_runtime, configure_logging


BAD_VERIFIER_STATUSES = ("needs_review", "weak_support", "not_supported")
NEGATIVE_PAPER_TYPES = ("review", "clinical_prognostic", "expression_association", "methods_or_dataset")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--gene", help="Optional gene symbol to audit.")
    parser.add_argument("--examples", type=int, default=8, help="Example row count per warning section.")
    parser.add_argument("--csv-out", help="Optional CSV of suspicious paper rows.")
    parser.add_argument("--gene-csv-out", help="Optional CSV of per-gene audit summary.")
    return parser.parse_args()


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _gene_filter(gene: str | None) -> tuple[str, list[str]]:
    if gene:
        return "gene = ?", [gene.upper()]
    return "1=1", []


def _write_csv(path: str | None, rows: list[dict]) -> None:
    if not path or not rows:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logging.info("Wrote CSV: %s", out)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _print_examples(title: str, rows: list[dict]) -> None:
    if not rows:
        return
    logging.info("%s examples:", title)
    for row in rows:
        logging.info(
            "  %s PMID %s | conf=%s | %s",
            row.get("gene"),
            row.get("pmid"),
            row.get("confidence"),
            (row.get("title") or "")[:90],
        )


def main() -> int:
    args = parse_args()
    log_file = configure_logging(args.log_dir, "check_algorithm_fields")
    db = configure_db_runtime(args)

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", args.db_path)

    gene_clause, gene_params = _gene_filter(args.gene)

    with db.get_conn(args.db_path) as conn:
        total = _scalar(conn, f"SELECT COUNT(*) FROM papers WHERE {gene_clause}", tuple(gene_params))
        functional = _scalar(conn, f"SELECT COUNT(*) FROM papers WHERE {gene_clause} AND functional_study=1", tuple(gene_params))

        checks = {
            "missing_structured_evidence": """
                (structured_evidence_json IS NULL OR TRIM(structured_evidence_json)='')
            """,
            "missing_review_reasons": """
                (review_reasons IS NULL OR TRIM(review_reasons)='')
            """,
            "missing_or_unknown_paper_type": """
                (paper_type IS NULL OR TRIM(paper_type)='' OR paper_type='unknown')
            """,
            "functional_without_best_quote": """
                functional_study=1
                AND (best_evidence_quote IS NULL OR TRIM(best_evidence_quote)='')
            """,
            "functional_weak_gene_match": """
                functional_study=1
                AND COALESCE(gene_match_quality, '')='weak'
            """,
            "functional_without_perturbation_evidence": """
                functional_study=1
                AND (evidence_perturbation IS NULL OR TRIM(evidence_perturbation)='')
            """,
            "functional_without_phenotype_evidence": """
                functional_study=1
                AND COALESCE(in_vitro,0)=0
                AND COALESCE(in_vivo,0)=0
                AND (evidence_in_vitro IS NULL OR TRIM(evidence_in_vitro)='')
                AND (evidence_in_vivo IS NULL OR TRIM(evidence_in_vivo)='')
            """,
            "high_confidence_weak_evidence": """
                confidence >= 0.80
                AND (
                    COALESCE(gene_linked_evidence_sents, 0)=0
                    OR COALESCE(evidence_quality_score, 0) < 0.45
                    OR best_evidence_quote IS NULL
                    OR TRIM(best_evidence_quote)=''
                )
            """,
            "functional_suspicious_paper_type": f"""
                functional_study=1
                AND COALESCE(paper_type, '') IN {NEGATIVE_PAPER_TYPES}
            """,
            "risky_without_llm_verifier": f"""
                (
                    functional_study=1
                    OR llm_rules_disagree=1
                    OR COALESCE(verification_status, '') IN {BAD_VERIFIER_STATUSES}
                    OR (confidence >= 0.45 AND confidence <= 0.76)
                )
                AND (agentic_verifier_decision IS NULL OR TRIM(agentic_verifier_decision)='')
            """,
        }

        counts = {
            name: _scalar(conn, f"SELECT COUNT(*) FROM papers WHERE {gene_clause} AND ({predicate})", tuple(gene_params))
            for name, predicate in checks.items()
        }

        malformed_structured = 0
        structured_status = Counter()
        for row in conn.execute(
            f"""SELECT structured_evidence_json FROM papers
                WHERE {gene_clause}
                  AND structured_evidence_json IS NOT NULL
                  AND TRIM(structured_evidence_json)!=''""",
            tuple(gene_params),
        ).fetchall():
            try:
                payload = json.loads(row["structured_evidence_json"] or "{}")
                structured_status[str(payload.get("status") or "unknown")] += 1
            except Exception:
                malformed_structured += 1

        repeated_conf = _rows(
            conn,
            f"""SELECT ROUND(confidence, 3) as confidence_value, COUNT(*) as n
                FROM papers
                WHERE {gene_clause}
                GROUP BY ROUND(confidence, 3)
                HAVING COUNT(*) >= 50
                ORDER BY n DESC, confidence_value DESC
                LIMIT 10""",
            tuple(gene_params),
        )

        gene_summary = _rows(
            conn,
            f"""SELECT gene,
                       COUNT(*) as total_rows,
                       SUM(functional_study=1) as functional_rows,
                       SUM(CASE WHEN {checks['missing_structured_evidence']} THEN 1 ELSE 0 END) as missing_structured,
                       SUM(CASE WHEN {checks['functional_without_best_quote']} THEN 1 ELSE 0 END) as functional_no_quote,
                       SUM(CASE WHEN {checks['functional_without_perturbation_evidence']} THEN 1 ELSE 0 END) as functional_no_perturbation,
                       SUM(CASE WHEN {checks['functional_without_phenotype_evidence']} THEN 1 ELSE 0 END) as functional_no_phenotype,
                       SUM(CASE WHEN {checks['high_confidence_weak_evidence']} THEN 1 ELSE 0 END) as high_conf_weak_evidence,
                       SUM(CASE WHEN {checks['functional_suspicious_paper_type']} THEN 1 ELSE 0 END) as suspicious_functional_type,
                       SUM(CASE WHEN {checks['risky_without_llm_verifier']} THEN 1 ELSE 0 END) as risky_without_llm_verifier,
                       SUM(CASE WHEN COALESCE(review_recommendation, '') IN ('high_priority_review', 'medium_priority_review') THEN 1 ELSE 0 END) as review_needed
                FROM papers
                WHERE {gene_clause}
                GROUP BY gene
                ORDER BY (
                    missing_structured
                    + functional_no_quote * 3
                    + functional_no_perturbation * 3
                    + functional_no_phenotype * 3
                    + high_conf_weak_evidence * 4
                    + suspicious_functional_type * 3
                    + risky_without_llm_verifier
                    + review_needed
                ) DESC, gene ASC
                LIMIT 25""",
            tuple(gene_params),
        )

        suspicious_rows = _rows(
            conn,
            f"""SELECT gene, pmid, title, year, confidence, functional_study, paper_type,
                       verification_status, gene_match_quality, review_recommendation,
                       CASE
                         WHEN {checks['high_confidence_weak_evidence']} THEN 'high_confidence_weak_evidence'
                         WHEN {checks['functional_without_perturbation_evidence']} THEN 'functional_without_perturbation_evidence'
                         WHEN {checks['functional_without_phenotype_evidence']} THEN 'functional_without_phenotype_evidence'
                         WHEN {checks['functional_suspicious_paper_type']} THEN 'functional_suspicious_paper_type'
                         WHEN {checks['functional_without_best_quote']} THEN 'functional_without_best_quote'
                         WHEN {checks['risky_without_llm_verifier']} THEN 'risky_without_llm_verifier'
                         ELSE 'other'
                       END as issue
                FROM papers
                WHERE {gene_clause}
                  AND (
                    {checks['high_confidence_weak_evidence']}
                    OR {checks['functional_without_perturbation_evidence']}
                    OR {checks['functional_without_phenotype_evidence']}
                    OR {checks['functional_suspicious_paper_type']}
                    OR {checks['functional_without_best_quote']}
                    OR {checks['risky_without_llm_verifier']}
                  )
                ORDER BY
                  CASE issue
                    WHEN 'high_confidence_weak_evidence' THEN 1
                    WHEN 'functional_without_perturbation_evidence' THEN 2
                    WHEN 'functional_without_phenotype_evidence' THEN 3
                    ELSE 9
                  END,
                  confidence DESC
                LIMIT 200""",
            tuple(gene_params),
        )

        logging.info("Rows audited: %s", total)
        logging.info("Functional rows: %s", functional)
        logging.info("Structured evidence status: %s", dict(structured_status))
        logging.info("Malformed structured_evidence_json: %s", malformed_structured)
        for name, value in counts.items():
            logging.info("%s: %s", name, value)

        if repeated_conf:
            logging.info("Repeated confidence buckets (>=50 rows): %s", repeated_conf)

        _print_examples("Top suspicious rows", suspicious_rows[: args.examples])
        if gene_summary:
            logging.info("Top genes needing maintenance:")
            for row in gene_summary[: args.examples]:
                logging.info(
                    "  %(gene)s | total=%(total_rows)s functional=%(functional_rows)s review_needed=%(review_needed)s "
                    "missing_struct=%(missing_structured)s high_conf_weak=%(high_conf_weak_evidence)s "
                    "func_no_quote=%(functional_no_quote)s risky_no_llm=%(risky_without_llm_verifier)s",
                    row,
                )

        _write_csv(args.csv_out, suspicious_rows)
        _write_csv(args.gene_csv_out, gene_summary)

    logging.info("Recommended next command:")
    if counts["missing_structured_evidence"] or counts["missing_review_reasons"] or counts["missing_or_unknown_paper_type"]:
        logging.info("  python -u scripts/recompute_confidence.py --db-path %s --upload", args.db_path)
    elif any(counts[k] for k in (
        "high_confidence_weak_evidence",
        "functional_without_perturbation_evidence",
        "functional_without_phenotype_evidence",
        "functional_suspicious_paper_type",
    )):
        logging.info("  python -u scripts/plan_reprocess.py --db-path %s", args.db_path)
    elif counts["risky_without_llm_verifier"]:
        logging.info("  Optional: selected reprocess for high-value risky rows only.")
    else:
        logging.info("  No immediate algorithm maintenance needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
