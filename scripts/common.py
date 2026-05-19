"""Shared helpers for maintenance scripts."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_DB = ROOT / "gene_function_lab" / "gene_function_lab.db"
DEFAULT_COLAB_DB = Path("/content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db")
DEFAULT_COLAB_CACHE = Path("/content/drive/MyDrive/pubmed_llm/functional_study_cache")


def add_repo_to_path() -> None:
    root_s = str(ROOT)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)


def default_db_path() -> Path:
    env_path = os.environ.get("GENE_LAB_DB_PATH")
    if env_path:
        return Path(env_path)
    if DEFAULT_COLAB_DB.exists():
        return DEFAULT_COLAB_DB
    return DEFAULT_LOCAL_DB


def default_cache_dir() -> Path:
    env_path = os.environ.get("GDRIVE_CACHE")
    if env_path:
        return Path(env_path)
    if Path("/content/drive").exists():
        return DEFAULT_COLAB_CACHE
    return ROOT / "cache_pubmed"


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db-path",
        default=str(default_db_path()),
        help="SQLite DB path. Defaults to Colab Drive path when available, otherwise local repo DB.",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(default_cache_dir()),
        help="Paper-level cache directory.",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("ENTREZ_EMAIL", ""),
        help="NCBI Entrez email. Can also be set with ENTREZ_EMAIL.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Run rules-only mode. Much faster, but less complete than BioMistral classification.",
    )
    parser.add_argument(
        "--log-dir",
        default=str(ROOT / "logs"),
        help="Directory for timestamped maintenance logs.",
    )


def configure_logging(log_dir: str | Path, prefix: str) -> Path:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = log_path / f"{prefix}_{stamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(file_path, encoding="utf-8"),
        ],
    )
    return file_path


def configure_db_runtime(args):
    add_repo_to_path()

    import db

    db.DB_PATH = str(Path(args.db_path))
    db.init_db(db.DB_PATH)
    return db


def maybe_load_service_account(db_path: str | Path) -> None:
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return
    path = Path(db_path)
    candidates = [
        path.parents[1] / "service-account.json" if len(path.parents) > 1 else None,
        ROOT / "service-account.json",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            try:
                json.loads(text)
            except json.JSONDecodeError:
                logging.warning("Ignoring invalid service account JSON at %s", candidate)
                return
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = text
            logging.info("Loaded Google service account from %s", candidate)
            return


def configure_runtime(args):
    db = configure_db_runtime(args)
    os.environ["GDRIVE_CACHE"] = str(Path(args.cache_dir))
    maybe_load_service_account(args.db_path)

    import pipeline

    pipeline.CACHE_DIR = str(Path(args.cache_dir))
    if args.email:
        pipeline.Entrez.email = args.email
    if args.no_llm:
        pipeline.USE_LLM = False

    return db, pipeline


def maybe_upload(upload: bool, db_path: str | Path) -> bool:
    if not upload:
        return False
    import drive_sync

    drive_sync._drive_file_id = None
    ok = drive_sync.upload_db_to_drive(str(db_path))
    if ok:
        logging.info("Uploaded DB to Drive.")
    else:
        logging.error("DB upload failed.")
    return ok
