"""Colab-safe runner for recomputing PubMed-LLM evidence-support fields.

Use this file when you want to update the existing SQLite database after a
confidence / paper-type / review-routing algorithm change, without rerunning
PubMed search or BioMistral.

In Colab, run one normal Python cell:

    %run /content/drive/MyDrive/pubmed_llm/colab_recompute_confidence_update.py

or:

    exec(open("/content/drive/MyDrive/pubmed_llm/colab_recompute_confidence_update.py").read())

Do not paste `python -u ...` into a Python cell unless it starts with `!`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# ---- Lab-member settings -------------------------------------------------

# Main Drive project folder.
PROJECT_DIR = Path("/content/drive/MyDrive/pubmed_llm")

# SQLite DB to update.
DB_PATH = PROJECT_DIR / "gene_function_lab" / "gene_function_lab.db"

# Optional: recompute only one gene. Leave empty for all genes/papers.
GENE = ""

# Optional: limit rows for a test run. Use 0 for all rows.
LIMIT = 0

# Start with True if you want a preview only. Set False for the real update.
DRY_RUN = False

# Leave False if Colab only has HF_TOKEN. The DB is still updated in mounted
# Drive. Set True only if GOOGLE_SERVICE_ACCOUNT_JSON is available and you want
# the script to call the Drive API upload at the end.
UPLOAD_WITH_DRIVE_API = False

# Set True only if dependencies are missing. Recompute usually does not need the
# heavy BioMistral dependencies.
INSTALL_REQUIREMENTS = False


# ---- Runner code ---------------------------------------------------------

def mount_drive_if_needed() -> None:
    """Mount Google Drive when running inside Colab."""
    try:
        from google.colab import drive  # type: ignore
    except Exception:
        return

    if not Path("/content/drive").exists():
        drive.mount("/content/drive")
    elif not PROJECT_DIR.exists():
        drive.mount("/content/drive")


def load_colab_secrets() -> None:
    """Load optional Colab secrets into environment variables."""
    try:
        from google.colab import userdata  # type: ignore
    except Exception:
        return

    for name in (
        "HF_TOKEN",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_DRIVE_DB_FILE_ID",
        "ENTREZ_EMAIL",
    ):
        if os.environ.get(name):
            continue
        try:
            value = userdata.get(name)
        except Exception:
            value = None
        if value:
            os.environ[name] = value
            print(f"Loaded Colab secret: {name}", flush=True)


def run_streamed(command: list[str], cwd: Path) -> int:
    """Run a subprocess and stream output live into the notebook."""
    print("\n$ " + " ".join(command), flush=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
    return process.wait()


def main() -> int:
    mount_drive_if_needed()
    load_colab_secrets()

    if not PROJECT_DIR.exists():
        raise FileNotFoundError(f"Project folder not found: {PROJECT_DIR}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    os.chdir(PROJECT_DIR)
    print(f"Project: {PROJECT_DIR}", flush=True)
    print(f"DB: {DB_PATH}", flush=True)
    print(f"Dry run: {DRY_RUN}", flush=True)
    print(f"Drive API upload: {UPLOAD_WITH_DRIVE_API}", flush=True)

    if INSTALL_REQUIREMENTS:
        rc = run_streamed(
            [sys.executable, "-m", "pip", "install", "-r", "requirements-worker.txt"],
            PROJECT_DIR,
        )
        if rc != 0:
            return rc

    command = [
        sys.executable,
        "-u",
        "scripts/recompute_confidence.py",
        "--db-path",
        str(DB_PATH),
    ]
    if GENE.strip():
        command += ["--gene", GENE.strip().upper()]
    if LIMIT and LIMIT > 0:
        command += ["--limit", str(int(LIMIT))]
    if DRY_RUN:
        command.append("--dry-run")
    if UPLOAD_WITH_DRIVE_API:
        command.append("--upload")

    rc = run_streamed(command, PROJECT_DIR)
    if rc == 0:
        print("\nDone.", flush=True)
        if not DRY_RUN:
            print(
                "Next: open the Hugging Face sync URL so the website reloads the Drive DB.",
                flush=True,
            )
    else:
        print(f"\nFailed with exit code {rc}.", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
