"""Print queue and database maintenance status."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from common import add_common_args, configure_db_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Gene Function Lab queue/database status.")
    add_common_args(parser)
    parser.add_argument("--limit", type=int, default=30, help="Number of queue rows to print.")
    parser.add_argument("--show-done", action="store_true", help="Include done rows in queue preview.")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=30,
        help="Report genes with last_run_at older than this many days.",
    )
    parser.add_argument(
        "--refresh-before",
        default="",
        help="Optional YYYY-MM-DD cutoff for monthly-campaign checks.",
    )
    return parser


def count_rows(conn: sqlite3.Connection, status: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM request_queue WHERE status=?", (status,)
    ).fetchone()[0]


def main() -> int:
    args = build_parser().parse_args()
    db = configure_db_runtime(args)
    db_path = Path(args.db_path)

    stats = db.db_stats(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        pending = count_rows(conn, "pending")
        processing = count_rows(conn, "processing")
        done = count_rows(conn, "done")
        error = count_rows(conn, "error")
        where = "" if args.show_done else "WHERE status!='done'"
        rows = conn.execute(
            f"""
            SELECT id, gene, status, requested_at, started_at, finished_at, error, max_papers
            FROM request_queue
            {where}
            ORDER BY
              CASE status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 WHEN 'error' THEN 2 ELSE 3 END,
              requested_at ASC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        cutoff = (
            args.refresh_before.strip()
            if args.refresh_before
            else (datetime.utcnow() - timedelta(days=max(args.stale_days, 0))).strftime("%Y-%m-%d %H:%M:%S")
        )
        stale = conn.execute(
            """
            SELECT gene, last_run_at, total_papers
            FROM genes
            WHERE last_run_at IS NULL OR last_run_at < ?
            ORDER BY COALESCE(last_run_at, ''), gene COLLATE NOCASE
            LIMIT ?
            """,
            (cutoff, args.limit),
        ).fetchall()
        stale_count = conn.execute(
            "SELECT COUNT(*) FROM genes WHERE last_run_at IS NULL OR last_run_at < ?",
            (cutoff,),
        ).fetchone()[0]

    print(f"DB: {db_path}")
    print(f"Papers: {stats['total_papers']:,}")
    print(f"Genes: {stats['total_genes']:,}")
    print(f"Functional papers: {stats['functional_papers']:,}")
    print()
    print(f"Queue pending: {pending}")
    print(f"Queue processing: {processing}")
    print(f"Queue done: {done}")
    print(f"Queue error: {error}")
    print()
    print(f"Refresh cutoff: {cutoff}")
    print(f"Genes needing refresh: {stale_count}")
    if stale:
        print("Refresh preview:")
        for r in stale:
            print(
                f"  {r['gene']:<12} last_run={r['last_run_at'] or 'never':<19} "
                f"papers={r['total_papers'] or 0}"
            )
        print()
    if not rows:
        print("No queue rows to show.")
        return 0

    print("Queue preview:")
    for r in rows:
        err = f" | error={r['error'][:90]}" if r["error"] else ""
        print(
            f"  #{r['id']:<4} {r['gene']:<12} {r['status']:<10} "
            f"max={r['max_papers'] or ''} requested={r['requested_at']}{err}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
