# Maintenance Guide

This is the operational guide for keeping the Hugging Face website database current.

Current clean baseline as of May 22, 2026:

- 23,973 papers
- 255 genes
- 3,404 functional papers
- 0 pending queue requests
- 0 processing requests
- 0 error requests

## Pipeline Summary

```text
Hugging Face website
  -> /api/request
  -> SQLite request_queue
  -> worker script processes pending genes
  -> pipeline.py writes paper evidence into SQLite
  -> Drive sync makes the updated DB visible to the website
```

The website should stay CPU-only. The worker should run in Colab, a lab GPU machine, or a rented GPU VM.

## Where Things Live

| Component | File / Table | Purpose |
| --- | --- | --- |
| Website request endpoint | `app.py` `/api/request` | Adds a requested gene to the queue. |
| Queue table | SQLite `request_queue` | Stores `pending`, `processing`, `done`, and `error` gene requests. |
| Paper database | SQLite `papers` | Stores extracted paper-level evidence. |
| Gene summaries | SQLite `genes` | Stores per-gene paper/function counts and last run time. |
| Worker pipeline | `pipeline.py` | PubMed search, full-text fetch, evidence extraction, rules, LLM classification. |
| Queue worker | `scripts/process_queue.py` | Processes pending queue requests in controlled batches. |
| Monthly refresh | `scripts/update_existing_genes.py` | Refreshes existing genes for new PubMed papers. |
| Status check | `scripts/check_queue_status.py` | Prints DB and queue status. |

## Configure Secrets Safely

Do not hard-code passwords or tokens.

Use Colab Secrets, Hugging Face Space secrets, or private environment variables:

- `HF_TOKEN`: Hugging Face token for downloading BioMistral.
- `ENTREZ_EMAIL`: email for NCBI Entrez requests.
- `GOOGLE_SERVICE_ACCOUNT_JSON`: service-account JSON content for Drive sync.
- `GOOGLE_DRIVE_DB_FILE_ID`: exact Drive file id for `gene_function_lab.db`. Recommended when more than one DB copy exists.
- `GOOGLE_DRIVE_FOLDER_ID`: optional Drive folder id to restrict DB file lookup.
- `APP_PASSWORD`: private website password in the Hugging Face Space.
- `GENE_LAB_DB_PATH`: optional override for the SQLite DB path.
- `GDRIVE_CACHE`: optional override for the paper cache directory.

See `.env.example` for names only. Do not commit real values.

## Recommended Colab Setup

For routine lab use, prefer:

```text
pubmed_llm_maintenance_runner.ipynb
```

It contains a setup cell, one small editable settings cell, and separate task
cells for checking status, processing the queue, retrying errors, monthly
refresh, code updates, and upload.

If running commands manually, run this once at the top of a Colab runtime after
mounting Drive:

```python
%cd /content/drive/MyDrive/pubmed_llm
!pip install -r requirements-worker.txt
```

Then authenticate to Hugging Face using Colab Secrets:

```python
from google.colab import userdata
from huggingface_hub import login
login(userdata.get("HF_TOKEN"))
```

If Drive upload is needed, place a private service-account file at:

```text
MyDrive/pubmed_llm/service-account.json
```

or set `GOOGLE_SERVICE_ACCOUNT_JSON` in the runtime environment.

If the website sync timestamp updates but the counts stay old, Hugging Face is
probably reading a different `gene_function_lab.db` file than the worker is
updating. Fix this by setting `GOOGLE_DRIVE_DB_FILE_ID` in both Hugging Face
Space secrets and the worker runtime, using the file id from the Drive URL for
the database file that should be the single source of truth.

Example Colab runtime setup:

```python
from google.colab import userdata
import os

os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = userdata.get("GOOGLE_SERVICE_ACCOUNT_JSON")
os.environ["GOOGLE_DRIVE_DB_FILE_ID"] = userdata.get("GOOGLE_DRIVE_DB_FILE_ID")
```

## Check Queue Status

Run this before processing:

```bash
python scripts/check_queue_status.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db
```

This prints:

- total papers
- total genes
- pending requests
- processing requests
- done requests
- error requests
- queue preview

## Process The Current Backlog

For the current backlog, do not process all queued genes in one run.

Start with a small smoke test:

```bash
python scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 1 \
  --max-papers 25 \
  --reset-processing \
  --upload-at-end
```

If that succeeds, increase gradually:

```bash
python scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 3 \
  --max-papers 50 \
  --upload-at-end
```

For a larger batch on a stable GPU machine:

```bash
python scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 5 \
  --max-papers 100 \
  --upload-at-end
```

### Fast Rules-Only Triage

If Colab GPU is unavailable, you can run a faster lower-quality triage:

```bash
python scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 5 \
  --max-papers 100 \
  --no-llm
```

Use this only when you need a rough pass. LLM-assisted results are preferred for final review.

## Monthly Refresh Existing Genes

Refresh existing genes in chunks:

```bash
python scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 0 \
  --max-genes 5 \
  --max-papers 50 \
  --upload
```

Next chunk:

```bash
python scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 5 \
  --max-genes 5 \
  --max-papers 50 \
  --upload
```

The pipeline skips PMIDs already present in SQLite, so monthly refresh should focus on newly found papers.

## Failure Recovery

If Colab disconnects, some requests may remain `processing`. Reset them:

```bash
python scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --reset-processing \
  --max-requests 0
```

If requests are marked `error` after a code or credential fix, retry them:

```bash
python scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --retry-errors \
  --max-requests 1 \
  --max-papers 25
```

## Why Processing Is Slow

One gene can be slow because each paper may require:

- PubMed search
- PubMed metadata fetch
- PMC full-text lookup and XML fetch
- evidence sentence extraction
- BioMistral-7B inference
- SQLite/cache writes

For 87 queued genes, `300` papers per gene could mean up to `26,100` paper-level passes. This is too much for a single Colab session.

## Practical Backlog Plan

1. Run `check_queue_status.py`.
2. Run one smoke-test request with `--max-requests 1 --max-papers 25`.
3. If successful, run batches of `3` genes with `50` papers each.
4. Increase to `5` genes with `100` papers only if the runtime is stable.
5. Upload after each batch.
6. Check the website.
7. Repeat until pending queue is low.

## Remaining Improvement Ideas

- Add a CPU-first prefilter so the LLM only runs on high-signal papers.
- Store PubMed metadata/full-text fetches separately from final classifications.
- Add a reviewed gold-label set and evaluation report.
- Move the worker from Colab to a scheduled GPU machine if this becomes a regular lab service.
