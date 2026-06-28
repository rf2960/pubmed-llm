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
    from evidence_agents import adjudicator_agent, review_router_agent, run_pre_scoring_agents, serialize_agent_trace
    from paper_type import classify_paper_type

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
               evidence_crispr_screen, total_evidence_sents,
               verification_status, verification_reasons,
               evidence_quality_score, search_relevance_score,
               evidence_retrieval_score, gene_match_quality,
               publication_types, paper_type, best_evidence_quote,
               gene_linked_evidence_sents, adjudication_status,
               adjudication_reasons,
               review_recommendation, review_reasons, agent_trace
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
        primary = {
            "functional_study": row.get("functional_study"),
            "in_vitro": row.get("in_vitro"),
            "in_vivo": row.get("in_vivo"),
            "knockout": row.get("knockout"),
            "knockdown": row.get("knockdown"),
            "shrna": row.get("shrna"),
            "sirna": row.get("sirna"),
            "crispr": row.get("crispr"),
            "crispr_screen": row.get("crispr_screen"),
        }
        rules_result = {
            **primary,
            "functional_study": row.get("rules_functional")
            if row.get("rules_functional") is not None
            else row.get("functional_study"),
        }
        llm_result = primary if bool(row.get("classified_by_llm")) else None
        ev = {
            "evidence_perturbation": row.get("evidence_perturbation"),
            "evidence_in_vitro": row.get("evidence_in_vitro"),
            "evidence_in_vivo": row.get("evidence_in_vivo"),
            "evidence_crispr_screen": row.get("evidence_crispr_screen"),
            "total_evidence_sents": row.get("total_evidence_sents"),
            "pmcid": row.get("pmcid"),
            "search_relevance_score": row.get("search_relevance_score"),
            "evidence_retrieval_score": row.get("evidence_retrieval_score"),
            "publication_types": row.get("publication_types"),
        }
        evidence_parts = []
        for key in ("evidence_perturbation", "evidence_in_vitro", "evidence_in_vivo", "evidence_crispr_screen"):
            evidence_parts.extend(x.strip() for x in str(row.get(key) or "").split("|") if x.strip())
        gene = row.get("gene", "")
        gene_re = f" {gene.lower()} "
        linked = [s for s in evidence_parts if gene_re in f" {s.lower()} "]
        paper_type = row.get("paper_type") or classify_paper_type(
            title=row.get("title", ""),
            abstract="",
            publication_types=row.get("publication_types", ""),
            evidence=ev,
        )
        best_quote = row.get("best_evidence_quote") or (linked[0] if linked else (evidence_parts[0] if evidence_parts else ""))
        gene_linked_count = int(row.get("gene_linked_evidence_sents") or len(set(linked)))
        ev["paper_type"] = paper_type
        ev["best_evidence_quote"] = best_quote
        ev["gene_linked_evidence_sents"] = gene_linked_count
        row["paper_type"] = paper_type
        row["best_evidence_quote"] = best_quote
        row["gene_linked_evidence_sents"] = gene_linked_count
        agent_result = run_pre_scoring_agents(
            row.get("gene", ""),
            row.get("title", ""),
            "",
            ev,
            primary,
            rules_result,
            llm_result,
        )
        verification = agent_result["verification"]
        row.update(verification)
        confidence, conf_func, conf_nonfunc = compute_confidence_from_db_row(row)
        adjudication = adjudicator_agent(
            confidence,
            primary,
            verification,
            bool(row.get("llm_rules_disagree")),
        )
        route = review_router_agent(
            confidence,
            primary,
            verification,
            bool(row.get("llm_rules_disagree")),
            adjudication,
        )
        trace = agent_result["trace"]
        trace["agents"].append(adjudication["agent"])
        trace["agents"].append(route["agent"])
        old = float(row.get("confidence") or 0)
        updates.append((
            confidence,
            conf_func,
            conf_nonfunc,
            verification["verification_status"],
            verification["verification_reasons"],
            verification["evidence_quality_score"],
            verification["gene_match_quality"],
            paper_type,
            best_quote,
            gene_linked_count,
            adjudication["adjudication"],
            adjudication["adjudication_reasons"],
            route["review_recommendation"],
            route["review_reasons"],
            serialize_agent_trace(trace),
            row["gene"],
            row["pmid"],
        ))
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
                   confidence_not_functional=?,
                   verification_status=?,
                   verification_reasons=?,
                   evidence_quality_score=?,
                   gene_match_quality=?,
                   paper_type=?,
                   best_evidence_quote=?,
                   gene_linked_evidence_sents=?,
                   adjudication_status=?,
                   adjudication_reasons=?,
                   review_recommendation=?,
                   review_reasons=?,
                   agent_trace=?
               WHERE gene=? AND pmid=?""",
            updates,
        )
    logging.info("Updated %s row(s).", len(updates))
    maybe_upload(args.upload, Path(args.db_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
