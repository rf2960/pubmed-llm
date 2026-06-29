"""Suggest selected recompute/reprocess actions from DB audit signals.

This planner is read-only. It helps avoid full database rebuilds by ranking
genes and PMIDs that would benefit most from fast recompute or selected
reprocessing with the current pipeline.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from pathlib import Path

from common import add_common_args, configure_db_runtime, configure_logging


NEGATIVE_PAPER_TYPES = ("review", "clinical_prognostic", "expression_association", "methods_or_dataset")
BAD_VERIFIER_STATUSES = ("needs_review", "weak_support", "not_supported")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--gene", help="Optional gene symbol to plan for.")
    parser.add_argument("--top-genes", type=int, default=12, help="How many genes to show.")
    parser.add_argument("--top-pmids", type=int, default=25, help="How many PMIDs to show.")
    parser.add_argument("--csv-out", help="Optional CSV export of recommended PMID actions.")
    return parser.parse_args()


def _gene_filter(gene: str | None) -> tuple[str, list[str]]:
    if gene:
        return "gene = ?", [gene.upper()]
    return "1=1", []


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


def _classify_action(row: dict) -> tuple[str, str]:
    missing_structured = int(row.get("missing_structured") or 0)
    missing_review = int(row.get("missing_review_reasons") or 0)
    unknown_type = int(row.get("unknown_paper_type") or 0)
    high_conf_weak = int(row.get("high_conf_weak_evidence") or 0)
    functional_no_quote = int(row.get("functional_no_quote") or 0)
    functional_no_perturbation = int(row.get("functional_no_perturbation") or 0)
    functional_no_phenotype = int(row.get("functional_no_phenotype") or 0)
    suspicious_type = int(row.get("suspicious_functional_type") or 0)
    risky_no_llm = int(row.get("risky_without_llm_verifier") or 0)

    if missing_structured or missing_review or unknown_type:
        return "recompute_only", "Backfill deterministic fields first."
    if high_conf_weak or functional_no_perturbation or functional_no_phenotype or suspicious_type:
        return "selected_pmid_reprocess", "High-value suspicious papers need current evidence retrieval/classification."
    if functional_no_quote >= 5 or risky_no_llm >= 20:
        return "selected_gene_reprocess", "Many risky rows for this gene; reprocess in a bounded gene batch."
    if risky_no_llm:
        return "optional_selected_reprocess", "Only reprocess if these papers are biologically important."
    return "no_action", "No immediate maintenance action."


def main() -> int:
    args = parse_args()
    log_file = configure_logging(args.log_dir, "plan_reprocess")
    db = configure_db_runtime(args)

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", args.db_path)

    gene_clause, gene_params = _gene_filter(args.gene)
    negative_types = NEGATIVE_PAPER_TYPES
    bad_statuses = BAD_VERIFIER_STATUSES

    with db.get_conn(args.db_path) as conn:
        gene_rows = _rows(
            conn,
            f"""SELECT gene,
                       COUNT(*) as total_rows,
                       SUM(functional_study=1) as functional_rows,
                       SUM(CASE WHEN structured_evidence_json IS NULL OR TRIM(structured_evidence_json)='' THEN 1 ELSE 0 END) as missing_structured,
                       SUM(CASE WHEN review_reasons IS NULL OR TRIM(review_reasons)='' THEN 1 ELSE 0 END) as missing_review_reasons,
                       SUM(CASE WHEN paper_type IS NULL OR TRIM(paper_type)='' OR paper_type='unknown' THEN 1 ELSE 0 END) as unknown_paper_type,
                       SUM(CASE WHEN confidence >= 0.80 AND (
                            COALESCE(gene_linked_evidence_sents,0)=0
                            OR COALESCE(evidence_quality_score,0)<0.45
                            OR best_evidence_quote IS NULL
                            OR TRIM(best_evidence_quote)=''
                       ) THEN 1 ELSE 0 END) as high_conf_weak_evidence,
                       SUM(CASE WHEN functional_study=1 AND (best_evidence_quote IS NULL OR TRIM(best_evidence_quote)='') THEN 1 ELSE 0 END) as functional_no_quote,
                       SUM(CASE WHEN functional_study=1 AND (evidence_perturbation IS NULL OR TRIM(evidence_perturbation)='') THEN 1 ELSE 0 END) as functional_no_perturbation,
                       SUM(CASE WHEN functional_study=1
                                  AND COALESCE(in_vitro,0)=0
                                  AND COALESCE(in_vivo,0)=0
                                  AND (evidence_in_vitro IS NULL OR TRIM(evidence_in_vitro)='')
                                  AND (evidence_in_vivo IS NULL OR TRIM(evidence_in_vivo)='')
                                THEN 1 ELSE 0 END) as functional_no_phenotype,
                       SUM(CASE WHEN functional_study=1 AND COALESCE(paper_type,'') IN {negative_types} THEN 1 ELSE 0 END) as suspicious_functional_type,
                       SUM(CASE WHEN (
                            functional_study=1
                            OR llm_rules_disagree=1
                            OR COALESCE(verification_status,'') IN {bad_statuses}
                            OR (confidence >= 0.45 AND confidence <= 0.76)
                       ) AND (agentic_verifier_decision IS NULL OR TRIM(agentic_verifier_decision)='')
                       THEN 1 ELSE 0 END) as risky_without_llm_verifier
                FROM papers
                WHERE {gene_clause}
                GROUP BY gene""",
            tuple(gene_params),
        )

        for row in gene_rows:
            action, reason = _classify_action(row)
            row["recommended_action"] = action
            row["reason"] = reason
            row["priority_score"] = (
                int(row.get("missing_structured") or 0)
                + int(row.get("missing_review_reasons") or 0)
                + int(row.get("unknown_paper_type") or 0)
                + 5 * int(row.get("high_conf_weak_evidence") or 0)
                + 4 * int(row.get("functional_no_perturbation") or 0)
                + 4 * int(row.get("functional_no_phenotype") or 0)
                + 3 * int(row.get("suspicious_functional_type") or 0)
                + 2 * int(row.get("functional_no_quote") or 0)
                + int(row.get("risky_without_llm_verifier") or 0)
            )
        gene_rows.sort(key=lambda r: (-int(r["priority_score"]), r["gene"]))

        pmid_rows = _rows(
            conn,
            f"""SELECT gene, pmid, title, year, confidence, functional_study, paper_type,
                       verification_status, gene_match_quality,
                       CASE
                         WHEN confidence >= 0.80 AND (
                            COALESCE(gene_linked_evidence_sents,0)=0
                            OR COALESCE(evidence_quality_score,0)<0.45
                            OR best_evidence_quote IS NULL
                            OR TRIM(best_evidence_quote)=''
                         ) THEN 'high_confidence_weak_evidence'
                         WHEN functional_study=1 AND (evidence_perturbation IS NULL OR TRIM(evidence_perturbation)='') THEN 'functional_without_perturbation'
                         WHEN functional_study=1 AND COALESCE(paper_type,'') IN {negative_types} THEN 'suspicious_paper_type'
                         WHEN functional_study=1 AND (best_evidence_quote IS NULL OR TRIM(best_evidence_quote)='') THEN 'functional_without_best_quote'
                         WHEN (
                            functional_study=1
                            OR llm_rules_disagree=1
                            OR COALESCE(verification_status,'') IN {bad_statuses}
                         ) AND (agentic_verifier_decision IS NULL OR TRIM(agentic_verifier_decision)='')
                         THEN 'missing_llm_verifier'
                         ELSE 'review'
                       END as reason,
                       CASE
                         WHEN confidence >= 0.80 AND (
                            COALESCE(gene_linked_evidence_sents,0)=0
                            OR COALESCE(evidence_quality_score,0)<0.45
                            OR best_evidence_quote IS NULL
                            OR TRIM(best_evidence_quote)=''
                         ) THEN 'selected_pmid_reprocess'
                         WHEN functional_study=1 AND (
                            evidence_perturbation IS NULL
                            OR TRIM(evidence_perturbation)=''
                            OR COALESCE(paper_type,'') IN {negative_types}
                         ) THEN 'selected_pmid_reprocess'
                         ELSE 'review_first'
                       END as recommended_action
                FROM papers
                WHERE {gene_clause}
                  AND (
                    (confidence >= 0.80 AND (
                        COALESCE(gene_linked_evidence_sents,0)=0
                        OR COALESCE(evidence_quality_score,0)<0.45
                        OR best_evidence_quote IS NULL
                        OR TRIM(best_evidence_quote)=''
                    ))
                    OR (functional_study=1 AND (evidence_perturbation IS NULL OR TRIM(evidence_perturbation)=''))
                    OR (functional_study=1 AND COALESCE(paper_type,'') IN {negative_types})
                    OR (functional_study=1 AND (best_evidence_quote IS NULL OR TRIM(best_evidence_quote)=''))
                    OR ((
                        functional_study=1
                        OR llm_rules_disagree=1
                        OR COALESCE(verification_status,'') IN {bad_statuses}
                    ) AND (agentic_verifier_decision IS NULL OR TRIM(agentic_verifier_decision)=''))
                  )
                ORDER BY confidence DESC, gene ASC
                LIMIT ?""",
            tuple(gene_params + [args.top_pmids]),
        )

    logging.info("Top gene-level maintenance recommendations:")
    for row in gene_rows[: args.top_genes]:
        logging.info(
            "  %(gene)s | action=%(recommended_action)s | score=%(priority_score)s | "
            "missing_struct=%(missing_structured)s high_conf_weak=%(high_conf_weak_evidence)s "
            "no_pert=%(functional_no_perturbation)s no_pheno=%(functional_no_phenotype)s "
            "risky_no_llm=%(risky_without_llm_verifier)s | %(reason)s",
            row,
        )

    if pmid_rows:
        logging.info("Top PMID-level candidates:")
        for row in pmid_rows[: args.top_pmids]:
            logging.info(
                "  %s PMID %s | %s | conf=%s | %s",
                row["gene"], row["pmid"], row["recommended_action"], row["confidence"], row["reason"],
            )

    if gene_rows:
        top_action = gene_rows[0]["recommended_action"]
        logging.info("Recommended next step:")
        if top_action == "recompute_only":
            logging.info("  python -u scripts/recompute_confidence.py --db-path %s --upload", args.db_path)
        elif top_action == "selected_pmid_reprocess" and pmid_rows:
            first = pmid_rows[0]
            logging.info(
                "  python -u scripts/reprocess_papers.py --db-path %s --gene %s --pmids %s --ignore-cache --upload",
                args.db_path, first["gene"], first["pmid"],
            )
        elif top_action == "selected_gene_reprocess":
            logging.info(
                "  python -u scripts/reprocess_papers.py --db-path %s --gene %s --max-papers 100 --ignore-cache --upload",
                args.db_path, gene_rows[0]["gene"],
            )
        else:
            logging.info("  Review listed rows first; no broad reprocess recommended.")

    _write_csv(args.csv_out, pmid_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
