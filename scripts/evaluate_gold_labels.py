"""Evaluate stored classifications against a small human-labeled CSV dataset."""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from common import add_common_args, configure_db_runtime, configure_logging


TRUE_VALUES = {"1", "true", "yes", "y", "functional"}
FALSE_VALUES = {"0", "false", "no", "n", "not_functional", "nonfunctional"}


def parse_bool(value: str) -> bool | None:
    v = str(value or "").strip().lower()
    if v in TRUE_VALUES:
        return True
    if v in FALSE_VALUES:
        return False
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument(
        "--labels",
        required=True,
        help="CSV with columns gene, pmid, human_label_functional.",
    )
    parser.add_argument(
        "--write-disagreements",
        default="",
        help="Optional CSV path for rows where DB and human labels disagree.",
    )
    return parser


def load_labels(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = {"gene", "pmid", "human_label_functional"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Label file missing required columns: {', '.join(sorted(missing))}")
        rows = []
        for row in reader:
            label = parse_bool(row.get("human_label_functional", ""))
            if label is None:
                continue
            rows.append({**row, "human_label_functional_bool": label})
    return rows


def fetch_predictions(db_path: str, label_rows: list[dict]) -> dict[tuple[str, str], dict]:
    keys = [(r["gene"].upper().strip(), str(r["pmid"]).strip()) for r in label_rows]
    if not keys:
        return {}
    out = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for gene, pmid in keys:
            row = conn.execute(
                """SELECT gene, pmid, title, functional_study, cancer_type,
                          where_functional, confidence, verification_status,
                          review_recommendation, paper_type, gene_match_quality,
                          adjudication_status, gene_linked_evidence_sents
                   FROM papers
                   WHERE gene=? AND pmid=?""",
                (gene, pmid),
            ).fetchone()
            if row:
                out[(gene, pmid)] = dict(row)
    return out


def main() -> int:
    args = build_parser().parse_args()
    log_file = configure_logging(args.log_dir, "evaluate_gold_labels")
    configure_db_runtime(args)
    logging.info("Log file: %s", log_file)
    logging.info("DB path: %s", args.db_path)
    logging.info("Labels: %s", args.labels)

    labels = load_labels(args.labels)
    preds = fetch_predictions(args.db_path, labels)
    tp = fp = tn = fn = missing = 0
    disagreements = []
    band_counts = defaultdict(lambda: {"n": 0, "correct": 0})
    error_by_type = Counter()

    for label in labels:
        key = (label["gene"].upper().strip(), str(label["pmid"]).strip())
        pred = preds.get(key)
        human = bool(label["human_label_functional_bool"])
        if not pred:
            missing += 1
            disagreements.append({**label, "db_status": "missing"})
            continue
        model = bool(pred.get("functional_study"))
        conf = float(pred.get("confidence") or 0.0)
        band = "strong" if conf >= 0.80 else "moderate" if conf >= 0.60 else "weak"
        band_counts[band]["n"] += 1
        if model == human:
            band_counts[band]["correct"] += 1
        if model and human:
            tp += 1
        elif model and not human:
            fp += 1
            disagreements.append({**label, **pred, "db_status": "false_positive"})
            error_by_type[str(pred.get("paper_type") or "unknown")] += 1
        elif not model and human:
            fn += 1
            disagreements.append({**label, **pred, "db_status": "false_negative"})
            error_by_type[str(pred.get("paper_type") or "unknown")] += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    accuracy = (tp + tn) / (tp + fp + tn + fn) if (tp + fp + tn + fn) else 0.0

    logging.info("Evaluated labels: %s; missing in DB: %s", len(labels), missing)
    logging.info("TP=%s FP=%s TN=%s FN=%s", tp, fp, tn, fn)
    logging.info("Precision=%.3f Recall=%.3f F1=%.3f Accuracy=%.3f", precision, recall, f1, accuracy)
    for band in ("strong", "moderate", "weak"):
        n = band_counts[band]["n"]
        correct = band_counts[band]["correct"]
        if n:
            logging.info("Band %s: n=%s accuracy=%.3f", band, n, correct / n)
    if error_by_type:
        logging.info("Errors by paper_type: %s", dict(error_by_type.most_common()))

    if args.write_disagreements:
        path = Path(args.write_disagreements)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = sorted({k for row in disagreements for k in row.keys()})
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(disagreements)
        logging.info("Wrote disagreements: %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
