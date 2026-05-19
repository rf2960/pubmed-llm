"""Process queued gene requests in bounded, resumable batches."""

from __future__ import annotations

import argparse
import gc
import logging
import time
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


def main() -> int:
    args = build_parser().parse_args()
    log_file = configure_logging(args.log_dir, "process_queue")
    db = configure_db_runtime(args)
    db_path = str(Path(args.db_path))

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", db_path)

    if args.reset_processing:
        reset_n = db.reset_processing_requests(db_path)
        logging.info("Reset %s interrupted processing request(s) to pending.", reset_n)
    if args.retry_errors:
        reset_n = reset_error_requests(db, db_path)
        logging.info("Reset %s error request(s) to pending.", reset_n)

    pending = db.get_pending_requests(db_path)
    logging.info("Pending requests: %s", len(pending))
    selected = pending[: max(args.max_requests, 0)]
    if not selected:
        logging.info("No pending requests selected.")
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
                logging.info("%s already has papers in DB; marking request done.", gene)
                db.update_gene_record(gene, db_path)
                db.mark_queue_done(qid, db_path)
                succeeded += 1
                continue

            clear_gpu_memory()
            started = time.time()
            rows = pipeline.analyze_gene(gene, max_papers=max_papers)
            if rows:
                db.upsert_papers_bulk(rows, db_path)
            db.update_gene_record(gene, db_path)
            db.mark_queue_done(qid, db_path)
            elapsed = time.time() - started
            succeeded += 1
            logging.info("Done %s: %s row(s), %.1f min.", gene, len(rows), elapsed / 60)
            if args.upload:
                maybe_upload(True, db_path)
        except Exception as exc:
            failed += 1
            logging.exception("Failed %s: %s", gene, exc)
            db.mark_queue_error(qid, str(exc), db_path)
            if args.stop_on_error:
                break

    if args.upload_at_end:
        maybe_upload(True, db_path)

    logging.info("Batch complete. attempted=%s succeeded=%s failed=%s", attempted, succeeded, failed)
    return 1 if failed and args.stop_on_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
