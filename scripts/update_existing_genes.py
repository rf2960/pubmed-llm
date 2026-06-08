"""Refresh existing database genes in controlled chunks."""

from __future__ import annotations

import argparse
import gc
import logging
import time
from pathlib import Path

from common import add_common_args, configure_db_runtime, configure_logging, configure_runtime, maybe_upload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh existing genes for newly published papers.")
    add_common_args(parser)
    parser.add_argument("--start-at", type=int, default=0, help="Start index in the selected stable gene order.")
    parser.add_argument("--max-genes", type=int, default=5, help="Maximum genes to refresh in this run.")
    parser.add_argument("--max-papers", type=int, default=50, help="Maximum new PMIDs to process per gene.")
    parser.add_argument("--genes", nargs="*", default=None, help="Optional explicit gene list to refresh.")
    parser.add_argument(
        "--order",
        choices=["gene", "last-run"],
        default="gene",
        help="Gene order for --start-at slicing. Default is stable alphabetical order.",
    )
    parser.add_argument("--upload", action="store_true", help="Upload DB through drive_sync after the batch.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected genes without processing.")
    return parser


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
    log_file = configure_logging(args.log_dir, "update_existing_genes")
    db = configure_db_runtime(args)
    db_path = str(Path(args.db_path))

    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", db_path)

    if args.genes:
        genes = [g.upper().strip() for g in args.genes if g.strip()]
    else:
        order_sql = "gene COLLATE NOCASE ASC" if args.order == "gene" else "last_run_at DESC"
        with db.get_conn(db_path) as conn:
            rows = conn.execute(f"SELECT gene FROM genes ORDER BY {order_sql}").fetchall()
        all_genes = [r["gene"] for r in rows]
        genes = all_genes[args.start_at : args.start_at + args.max_genes]

    logging.info("Gene order: %s", args.order)
    logging.info("Selected %s gene(s): %s", len(genes), ", ".join(genes) or "none")
    if not genes:
        return 0
    if args.dry_run:
        logging.info("Dry run complete.")
        return 0

    db, pipeline = configure_runtime(args)
    logging.info("Cache dir: %s", pipeline.CACHE_DIR)
    logging.info("LLM enabled: %s", pipeline.USE_LLM)

    failed = 0
    for idx, gene in enumerate(genes, start=1):
        logging.info("[%s/%s] Refreshing %s max_papers=%s", idx, len(genes), gene, args.max_papers)
        try:
            clear_gpu_memory()
            started = time.time()
            rows = pipeline.analyze_gene(gene, max_papers=args.max_papers)
            if rows:
                db.upsert_papers_bulk(rows, db_path)
            db.update_gene_record(gene, db_path)
            elapsed = time.time() - started
            logging.info("Done %s: %s new row(s), %.1f min.", gene, len(rows), elapsed / 60)
        except Exception as exc:
            failed += 1
            logging.exception("Failed %s: %s", gene, exc)

    if args.upload:
        maybe_upload(True, db_path)

    logging.info("Refresh complete. genes=%s failed=%s", len(genes), failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
