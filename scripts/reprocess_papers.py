"""Force-reprocess existing papers with the current search/classification pipeline.

Use this when algorithm changes should affect old database rows, not only new
queue requests. The script can rebuild one gene from search results, rebuild all
currently stored PMIDs for a gene, or rebuild an explicit PMID list.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from common import add_common_args, configure_logging, configure_runtime, maybe_upload


COMPARE_FIELDS = (
    "functional_study",
    "where_functional",
    "cancer_type",
    "confidence",
    "verification_status",
    "review_recommendation",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--gene", required=True, help="Gene symbol to reprocess.")
    parser.add_argument("--pmids", nargs="*", default=None, help="Optional explicit PMID list.")
    parser.add_argument(
        "--all-papers-for-gene",
        action="store_true",
        help="Reprocess every PMID currently stored for --gene.",
    )
    parser.add_argument(
        "--max-papers",
        type=int,
        default=50,
        help="Maximum search-ranked papers to rebuild when --pmids is not provided.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be rebuilt. Does not run PubMed/LLM or write the database.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip DB backup before writing. Not recommended.",
    )
    parser.add_argument(
        "--ignore-cache",
        action="store_true",
        help="Ignore cached paper classifications and recompute from current code.",
    )
    parser.add_argument("--upload", action="store_true", help="Upload DB to Drive after successful write.")
    return parser


def backup_db(db_path: str) -> Path:
    path = Path(db_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.backup_{stamp}{path.suffix}")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy2(path, backup)
    logging.info("Backup written: %s", backup)
    return backup


def existing_pmids_for_gene(db_path: str, gene: str) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT pmid FROM papers WHERE gene=? ORDER BY CAST(year AS INTEGER) DESC, pmid DESC",
            (gene.upper(),),
        ).fetchall()
    return [str(r["pmid"]) for r in rows]


def old_rows(db_path: str, gene: str, pmids: list[str] | None) -> dict[str, dict]:
    params: list[str] = [gene.upper()]
    clause = "gene=?"
    if pmids:
        placeholders = ",".join("?" for _ in pmids)
        clause += f" AND pmid IN ({placeholders})"
        params.extend(str(p) for p in pmids)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM papers WHERE {clause}", params).fetchall()
    return {str(r["pmid"]): dict(r) for r in rows}


def summarize_changes(before: dict[str, dict], after_rows: list[dict]) -> None:
    changed = 0
    for row in after_rows:
        pmid = str(row.get("pmid"))
        old = before.get(pmid)
        if not old:
            logging.info("New row %s PMID %s", row.get("gene"), pmid)
            continue
        diffs = []
        for field in COMPARE_FIELDS:
            old_v = old.get(field)
            new_v = row.get(field)
            if field == "confidence":
                old_v = round(float(old_v or 0), 3)
                new_v = round(float(new_v or 0), 3)
            if old_v != new_v:
                diffs.append(f"{field}: {old_v} -> {new_v}")
        if diffs:
            changed += 1
            logging.info("Changed PMID %s | %s", pmid, "; ".join(diffs))
    logging.info("Changed rows: %s / %s", changed, len(after_rows))


def main() -> int:
    args = build_parser().parse_args()
    log_file = configure_logging(args.log_dir, "reprocess_papers")
    gene = args.gene.upper().strip()
    db_path = str(Path(args.db_path))

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", db_path)

    explicit_pmids = [str(p).strip() for p in (args.pmids or []) if str(p).strip()]
    if args.all_papers_for_gene:
        explicit_pmids = existing_pmids_for_gene(db_path, gene)
    if args.dry_run:
        if explicit_pmids:
            logging.info("Dry run: would reprocess %s PMID(s) for %s.", len(explicit_pmids), gene)
            logging.info("PMIDs: %s", ", ".join(explicit_pmids[:50]) + (" ..." if len(explicit_pmids) > 50 else ""))
        else:
            logging.info(
                "Dry run: would re-run PubMed search and rebuild top %s ranked paper(s) for %s.",
                args.max_papers,
                gene,
            )
        return 0

    if not args.no_backup:
        backup_db(db_path)

    db, pipeline = configure_runtime(args)
    before = old_rows(db_path, gene, explicit_pmids or None)
    rows = pipeline.analyze_gene(
        gene,
        max_papers=args.max_papers if not explicit_pmids else len(explicit_pmids),
        force_pmids=explicit_pmids or None,
        include_processed=True,
        use_cache=not args.ignore_cache,
    )

    summarize_changes(before, rows)
    if rows:
        db.upsert_papers_bulk(rows, db_path)
    db.update_gene_record(gene, db_path)
    logging.info("Reprocess complete for %s: wrote %s row(s).", gene, len(rows))
    maybe_upload(args.upload, Path(db_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
