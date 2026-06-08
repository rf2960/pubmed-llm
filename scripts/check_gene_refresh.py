"""Verify monthly refresh status for a stable gene chunk."""

from __future__ import annotations

import argparse
from pathlib import Path

from common import add_repo_to_path, default_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show refresh status for genes selected by stable chunk or explicit names."
    )
    parser.add_argument(
        "--db-path",
        default=str(default_db_path()),
        help="SQLite DB path. Defaults to Colab Drive path when available, otherwise local repo DB.",
    )
    parser.add_argument("--start-at", type=int, default=0, help="Start index in stable gene order.")
    parser.add_argument("--max-genes", type=int, default=15, help="Number of genes to inspect.")
    parser.add_argument("--genes", nargs="*", default=None, help="Optional explicit gene list.")
    parser.add_argument(
        "--order",
        choices=["gene", "last-run"],
        default="gene",
        help="Gene order for --start-at slicing. Default is stable alphabetical order.",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Optional timestamp/date prefix, for example 2026-06-08. Adds a refreshed_since check.",
    )
    return parser


def selected_genes(conn, args) -> list[str]:
    if args.genes:
        return [g.upper().strip() for g in args.genes if g.strip()]

    order_sql = "gene COLLATE NOCASE ASC" if args.order == "gene" else "last_run_at DESC"
    rows = conn.execute(f"SELECT gene FROM genes ORDER BY {order_sql}").fetchall()
    all_genes = [r["gene"] for r in rows]
    return all_genes[args.start_at : args.start_at + args.max_genes]


def gene_refresh_row(conn, gene: str, since: str | None) -> dict:
    summary = conn.execute(
        """
        SELECT gene, last_run_at, total_papers, functional_count
        FROM genes
        WHERE gene=?
        """,
        (gene,),
    ).fetchone()
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS paper_rows,
            SUM(CASE WHEN functional_study=1 THEN 1 ELSE 0 END) AS functional_rows,
            MAX(CAST(NULLIF(year, '') AS INTEGER)) AS newest_year,
            MAX(processed_at) AS newest_processed_at
        FROM papers
        WHERE gene=?
        """,
        (gene,),
    ).fetchone()

    last_run_at = summary["last_run_at"] if summary else ""
    refreshed_since = ""
    if since:
        refreshed_since = "yes" if last_run_at and last_run_at >= since else "no"

    return {
        "gene": gene,
        "last_run_at": last_run_at or "missing_from_genes_table",
        "summary_papers": summary["total_papers"] if summary else 0,
        "paper_rows": counts["paper_rows"] or 0,
        "functional": counts["functional_rows"] or 0,
        "newest_year": counts["newest_year"] or "",
        "newest_processed_at": counts["newest_processed_at"] or "",
        "refreshed_since": refreshed_since,
    }


def print_table(rows: list[dict], include_since: bool) -> None:
    columns = [
        ("gene", 12),
        ("last_run_at", 20),
        ("summary_papers", 14),
        ("paper_rows", 11),
        ("functional", 10),
        ("newest_year", 11),
        ("newest_processed_at", 20),
    ]
    if include_since:
        columns.append(("refreshed_since", 15))

    header = "  ".join(name.ljust(width) for name, width in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row[name]).ljust(width)[:width] for name, width in columns))


def main() -> int:
    args = build_parser().parse_args()
    add_repo_to_path()

    import db

    db_path = str(Path(args.db_path))
    db.DB_PATH = db_path
    db.init_db(db_path)

    with db.get_conn(db_path) as conn:
        genes = selected_genes(conn, args)
        print(f"DB: {db_path}")
        print(f"Gene order: {args.order}")
        print(f"Selected {len(genes)} gene(s): {', '.join(genes) or 'none'}")
        if not genes:
            return 0
        rows = [gene_refresh_row(conn, gene, args.since) for gene in genes]

    print_table(rows, include_since=bool(args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
