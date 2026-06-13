"""Process queued gene requests in bounded, resumable batches."""

from __future__ import annotations

import argparse
import gc
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from common import add_common_args, configure_db_runtime, configure_logging, configure_runtime, maybe_upload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process pending Gene Function Lab queue requests.")
    add_common_args(parser)
    parser.add_argument("--max-requests", type=int, default=1, help="Maximum queue requests to attempt.")
    parser.add_argument(
        "--max-papers",
        type=int,
        default=None,
        help="Override per-request max_papers. Use 25/50/100 for backlog triage.",
    )
    parser.add_argument(
        "--reset-processing",
        action="store_true",
        help="Return interrupted processing requests to pending before starting.",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Return error requests to pending before starting.",
    )
    parser.add_argument("--upload", action="store_true", help="Upload DB through drive_sync after each request.")
    parser.add_argument("--upload-at-end", action="store_true", help="Upload DB through drive_sync after the batch.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected requests without processing.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop the batch on first processing error.")
    parser.add_argument(
        "--refresh-stale",
        action="store_true",
        help="After queue work, refresh existing genes whose last_run_at is older than the cutoff.",
    )
    parser.add_argument(
        "--update-interval-days",
        type=int,
        default=30,
        help="Stale-gene interval used by --refresh-stale when --refresh-before is not set.",
    )
    parser.add_argument(
        "--refresh-before",
        default="",
        help="Optional YYYY-MM-DD cutoff. Refresh genes last_run_at before this date.",
    )
    parser.add_argument(
        "--max-refresh-genes",
        type=int,
        default=0,
        help="Maximum stale existing genes to refresh after queue work. 0 means none.",
    )
    parser.add_argument(
        "--refresh-max-papers",
        type=int,
        default=None,
        help="Max papers per stale-gene refresh. Defaults to --max-papers or 300.",
    )
    return parser


def reset_error_requests(db, db_path: str) -> int:
    with db.get_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE request_queue
            SET status='pending',
                started_at=NULL,
                finished_at=NULL,
                error=NULL
            WHERE status='error'
            """
        )
        return cur.rowcount


def clear_gpu_memory() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def refresh_cutoff(args) -> str:
    if args.refresh_before:
        return args.refresh_before.strip()
    cutoff = datetime.utcnow() - timedelta(days=max(args.update_interval_days, 0))
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def gene_last_run(db, gene: str, db_path: str) -> str:
    with db.get_conn(db_path) as conn:
        row = conn.execute("SELECT last_run_at FROM genes WHERE gene=?", (gene.upper(),)).fetchone()
    return row["last_run_at"] if row and row["last_run_at"] else ""


def gene_needs_refresh(db, gene: str, cutoff: str, db_path: str) -> bool:
    last_run_at = gene_last_run(db, gene, db_path)
    return not last_run_at or last_run_at < cutoff


def select_stale_genes(db, db_path: str, cutoff: str, limit: int, exclude: set[str]) -> list[dict]:
    if limit <= 0:
        return []
    exclude = {g.upper() for g in exclude}
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT gene, last_run_at, total_papers, functional_count
            FROM genes
            WHERE last_run_at IS NULL OR last_run_at < ?
            ORDER BY COALESCE(last_run_at, ''), gene COLLATE NOCASE
            """,
            (cutoff,),
        ).fetchall()
    selected = []
    for row in rows:
        if row["gene"].upper() in exclude:
            continue
        selected.append(dict(row))
        if len(selected) >= limit:
            break
    return selected


def analyze_and_store(db, pipeline, gene: str, max_papers: int, db_path: str, label: str) -> int:
    logging.info("%s %s with max_papers=%s", label, gene, max_papers)
    clear_gpu_memory()
    started = time.time()
    rows = pipeline.analyze_gene(gene, max_papers=max_papers)
    if rows:
        db.upsert_papers_bulk(rows, db_path)
    db.update_gene_record(gene, db_path)
    elapsed = time.time() - started
    logging.info("Done %s: %s row(s), %.1f min.", gene, len(rows), elapsed / 60)
    return len(rows)


def main() -> int:
    args = build_parser().parse_args()
    log_file = configure_logging(args.log_dir, "process_queue")
    db = configure_db_runtime(args)
    db_path = str(Path(args.db_path))

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", db_path)

    if args.dry_run and (args.reset_processing or args.retry_errors):
        logging.info("Dry run: reset-processing/retry-errors flags will not change queue state.")
    if args.reset_processing and not args.dry_run:
        reset_n = db.reset_processing_requests(db_path)
        logging.info("Reset %s interrupted processing request(s) to pending.", reset_n)
    if args.retry_errors and not args.dry_run:
        reset_n = reset_error_requests(db, db_path)
        logging.info("Reset %s error request(s) to pending.", reset_n)

    pending = db.get_pending_requests(db_path)
    logging.info("Pending requests: %s", len(pending))
    selected = pending[: max(args.max_requests, 0)]
    cutoff = refresh_cutoff(args)
    stale = select_stale_genes(
        db,
        db_path,
        cutoff,
        args.max_refresh_genes if args.refresh_stale else 0,
        {req["gene"] for req in selected},
    )
    if not selected and not stale:
        logging.info("No pending requests or stale genes selected.")
        return 0

    for req in selected:
        max_papers = args.max_papers or req.get("max_papers") or 300
        logging.info(
            "Selected #%s %s requested=%s max_papers=%s",
            req["id"],
            req["gene"],
            req.get("requested_at"),
            max_papers,
        )
    if args.refresh_stale:
        logging.info(
            "Stale refresh cutoff: %s; selected %s existing gene(s).",
            cutoff,
            len(stale),
        )
        for row in stale:
            logging.info(
                "Selected refresh %s last_run_at=%s total_papers=%s",
                row["gene"],
                row.get("last_run_at") or "never",
                row.get("total_papers"),
            )

    if args.dry_run:
        logging.info("Dry run complete.")
        return 0

    db, pipeline = configure_runtime(args)
    logging.info("Cache dir: %s", pipeline.CACHE_DIR)
    logging.info("LLM enabled: %s", pipeline.USE_LLM)

    attempted = 0
    succeeded = 0
    failed = 0

    for req in selected:
        qid = req["id"]
        gene = req["gene"]
        max_papers = args.max_papers or req.get("max_papers") or 300
        attempted += 1
        logging.info("Processing #%s %s with max_papers=%s", qid, gene, max_papers)
        db.mark_queue_processing(qid, db_path)

        try:
            if db.gene_is_processed(gene, db_path):
                if args.refresh_stale and gene_needs_refresh(db, gene, cutoff, db_path):
                    analyze_and_store(db, pipeline, gene, max_papers, db_path, "[queue refresh]")
                else:
                    logging.info("%s already has papers in DB and is not stale; marking request done.", gene)
                db.mark_queue_done(qid, db_path)
                succeeded += 1
                continue

            analyze_and_store(db, pipeline, gene, max_papers, db_path, "[queue new]")
            db.mark_queue_done(qid, db_path)
            succeeded += 1
            if args.upload:
                maybe_upload(True, db_path)
        except Exception as exc:
            failed += 1
            logging.exception("Failed %s: %s", gene, exc)
            db.mark_queue_error(qid, str(exc), db_path)
            if args.stop_on_error:
                break

    refresh_failed = 0
    refresh_succeeded = 0
    refresh_max_papers = args.refresh_max_papers or args.max_papers or 300
    for row in stale:
        gene = row["gene"]
        try:
            analyze_and_store(db, pipeline, gene, refresh_max_papers, db_path, "[stale refresh]")
            refresh_succeeded += 1
        except Exception as exc:
            refresh_failed += 1
            logging.exception("Failed stale refresh %s: %s", gene, exc)
            if args.stop_on_error:
                break

    if args.upload_at_end:
        maybe_upload(True, db_path)

    logging.info(
        "Batch complete. queue_attempted=%s queue_succeeded=%s queue_failed=%s "
        "refresh_succeeded=%s refresh_failed=%s",
        attempted,
        succeeded,
        failed,
        refresh_succeeded,
        refresh_failed,
    )
    return 1 if (failed or refresh_failed) and args.stop_on_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
