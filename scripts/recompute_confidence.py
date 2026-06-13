"""Recompute evidence-support scores for existing papers.

This maintenance script does not call PubMed or BioMistral. It reads existing
evidence fields from the SQLite database and applies the current confidence
rubric from confidence.py. Use it after changing the confidence algorithm so old
rows are consistent with newly processed rows.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

from common import add_common_args, configure_db_runtime, configure_logging, maybe_upload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--gene", help="Optional single gene to recompute.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum paper rows to update. 0 means all.")
    parser.add_argument("--dry-run", action="store_true", help="Preview score changes without writing.")
    parser.add_argument("--upload", action="store_true", help="Upload DB to Drive after a successful write.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_file = configure_logging(args.log_dir, "recompute_confidence")
    db = configure_db_runtime(args)

    from confidence import compute_confidence_from_db_row

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", args.db_path)

    where = []
    params = []
    if args.gene:
        where.append("gene = ?")
        params.append(args.gene.upper())
    clause = "WHERE " + " AND ".join(where) if where else ""
    limit_clause = "LIMIT ?" if args.limit and args.limit > 0 else ""
    if limit_clause:
        params.append(args.limit)

    sql = f"""
        SELECT gene, pmid, pmcid, title, functional_study, where_functional,
               in_vitro, in_vivo, knockout, knockdown, shrna, sirna,
               crispr, crispr_screen, confidence, confidence_functional,
               confidence_not_functional, classified_by_llm,
               llm_rules_disagree, rules_functional,
               evidence_perturbation, evidence_in_vitro, evidence_in_vivo,
               evidence_crispr_screen, total_evidence_sents
        FROM papers
        {clause}
        ORDER BY gene, pmid
        {limit_clause}
    """

    updates = []
    preview = []
    with sqlite3.connect(args.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    for row in rows:
        confidence, conf_func, conf_nonfunc = compute_confidence_from_db_row(row)
        old = float(row.get("confidence") or 0)
        updates.append((confidence, conf_func, conf_nonfunc, row["gene"], row["pmid"]))
        if len(preview) < 10 and abs(confidence - old) >= 0.05:
            preview.append((row["gene"], row["pmid"], old, confidence))

    logging.info("Scored %s row(s).", len(updates))
    for gene, pmid, old, new in preview:
        logging.info("Preview %s PMID %s: %.3f -> %.3f", gene, pmid, old, new)

    if args.dry_run:
        logging.info("Dry run complete. No DB rows were updated.")
        return 0

    with db.get_conn(args.db_path) as conn:
        conn.executemany(
            """UPDATE papers
               SET confidence=?,
                   confidence_functional=?,
                   confidence_not_functional=?
               WHERE gene=? AND pmid=?""",
            updates,
        )
    logging.info("Updated %s row(s).", len(updates))
    maybe_upload(args.upload, Path(args.db_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
