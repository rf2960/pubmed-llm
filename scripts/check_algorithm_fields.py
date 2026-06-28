"""Audit algorithm/backfill fields in the SQLite database.

Use this after code updates, confidence recompute, or selected reprocessing to
see whether rows have the fields needed by the current website and review
workflow. This script does not modify the database and does not call PubMed or
BioMistral.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections import Counter

from common import add_common_args, configure_db_runtime, configure_logging


BAD_VERIFIER_STATUSES = ("needs_review", "weak_support", "not_supported")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--gene", help="Optional gene symbol to audit.")
    parser.add_argument("--examples", type=int, default=8, help="Example row count per warning section.")
    return parser.parse_args()


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _print_examples(conn: sqlite3.Connection, title: str, sql: str, params: tuple, n: int) -> None:
    rows = conn.execute(sql, (*params, n)).fetchall()
    if not rows:
        return
    logging.info("%s examples:", title)
    for row in rows:
        logging.info("  %s PMID %s | %s", row["gene"], row["pmid"], (row["title"] or "")[:95])


def main() -> int:
    args = parse_args()
    log_file = configure_logging(args.log_dir, "check_algorithm_fields")
    db = configure_db_runtime(args)

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", args.db_path)

    where = []
    params: list[str] = []
    if args.gene:
        where.append("gene = ?")
        params.append(args.gene.upper())
    clause = "WHERE " + " AND ".join(where) if where else ""
    and_clause = (" AND " + " AND ".join(where)) if where else ""

    with db.get_conn(args.db_path) as conn:
        total = _scalar(conn, f"SELECT COUNT(*) FROM papers {clause}", tuple(params))
        functional = _scalar(conn, f"SELECT COUNT(*) FROM papers WHERE functional_study=1{and_clause}", tuple(params))
        missing_structured = _scalar(
            conn,
            f"""SELECT COUNT(*) FROM papers
                WHERE (structured_evidence_json IS NULL OR TRIM(structured_evidence_json)='')
                {and_clause}""",
            tuple(params),
        )
        missing_review_reasons = _scalar(
            conn,
            f"""SELECT COUNT(*) FROM papers
                WHERE (review_reasons IS NULL OR TRIM(review_reasons)='')
                {and_clause}""",
            tuple(params),
        )
        missing_paper_type = _scalar(
            conn,
            f"""SELECT COUNT(*) FROM papers
                WHERE (paper_type IS NULL OR TRIM(paper_type)='' OR paper_type='unknown')
                {and_clause}""",
            tuple(params),
        )
        functional_no_quote = _scalar(
            conn,
            f"""SELECT COUNT(*) FROM papers
                WHERE functional_study=1
                  AND (best_evidence_quote IS NULL OR TRIM(best_evidence_quote)='')
                {and_clause}""",
            tuple(params),
        )
        functional_weak_gene = _scalar(
            conn,
            f"""SELECT COUNT(*) FROM papers
                WHERE functional_study=1
                  AND COALESCE(gene_match_quality, '')='weak'
                {and_clause}""",
            tuple(params),
        )
        risky_missing_llm_verifier = _scalar(
            conn,
            f"""SELECT COUNT(*) FROM papers
                WHERE (
                    functional_study=1
                    OR llm_rules_disagree=1
                    OR COALESCE(verification_status, '') IN {BAD_VERIFIER_STATUSES}
                    OR (confidence >= 0.45 AND confidence <= 0.76)
                )
                  AND (agentic_verifier_decision IS NULL OR TRIM(agentic_verifier_decision)='')
                {and_clause}""",
            tuple(params),
        )

        malformed_structured = 0
        structured_status = Counter()
        rows = conn.execute(
            f"""SELECT structured_evidence_json FROM papers
                WHERE structured_evidence_json IS NOT NULL
                  AND TRIM(structured_evidence_json)!=''
                {and_clause}""",
            tuple(params),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["structured_evidence_json"] or "{}")
                structured_status[str(payload.get("status") or "unknown")] += 1
            except Exception:
                malformed_structured += 1

        logging.info("Rows audited: %s", total)
        logging.info("Functional rows: %s", functional)
        logging.info("Structured evidence status: %s", dict(structured_status))
        logging.info("Missing structured_evidence_json: %s", missing_structured)
        logging.info("Malformed structured_evidence_json: %s", malformed_structured)
        logging.info("Missing review_reasons: %s", missing_review_reasons)
        logging.info("Missing/unknown paper_type: %s", missing_paper_type)
        logging.info("Functional rows without best_evidence_quote: %s", functional_no_quote)
        logging.info("Functional rows with weak gene match: %s", functional_weak_gene)
        logging.info("Risky rows without LLM verifier fields: %s", risky_missing_llm_verifier)

        _print_examples(
            conn,
            "Missing structured evidence",
            f"""SELECT gene, pmid, title FROM papers
                WHERE (structured_evidence_json IS NULL OR TRIM(structured_evidence_json)='')
                {and_clause}
                ORDER BY gene, pmid LIMIT ?""",
            tuple(params),
            args.examples,
        )
        _print_examples(
            conn,
            "Functional rows without best quote",
            f"""SELECT gene, pmid, title FROM papers
                WHERE functional_study=1
                  AND (best_evidence_quote IS NULL OR TRIM(best_evidence_quote)='')
                {and_clause}
                ORDER BY confidence DESC LIMIT ?""",
            tuple(params),
            args.examples,
        )

    logging.info("Recommended interpretation:")
    if missing_structured:
        logging.info("  Run scripts/recompute_confidence.py to backfill structured evidence from stored snippets.")
    if risky_missing_llm_verifier:
        logging.info("  LLM verifier fields require selected full reprocessing; recompute alone cannot create them.")
    if functional_no_quote or functional_weak_gene:
        logging.info("  Review examples before broad reprocessing; these are likely useful ADAM10-style test cases.")
    if not any([missing_structured, malformed_structured, missing_review_reasons, functional_no_quote, functional_weak_gene]):
        logging.info("  Core deterministic backfill fields look complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
