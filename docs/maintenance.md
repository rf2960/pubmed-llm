# Maintenance Guide

This is the operational guide for keeping the Hugging Face website database current.

Example clean baseline after the May 2026 backlog cleanup:

- 23,973 papers
- 255 genes
- 3,404 functional papers
- 0 pending queue requests
- 0 processing requests
- 0 error requests

Do not treat those numbers as fixed. The current website counts should increase
after monthly refresh runs.

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
| Evidence-support scoring | `confidence.py` | Shared confidence/evidence-support rubric for new processing and score recomputation. |
| Human review API | `app.py` `/api/review` | Saves reviewer status, label, notes, and reviewer metadata for a paper. |
| Main worker | `scripts/process_queue.py` | Processes pending queue requests, retries failed requests, and refreshes stale existing genes in controlled batches. |
| Confidence recompute | `scripts/recompute_confidence.py` | Re-scores existing rows after the scoring algorithm changes, without rerunning PubMed or BioMistral. |
| Manual refresh fallback | `scripts/update_existing_genes.py` | Advanced manual chunk refresh for existing genes. |
| Status check | `scripts/check_queue_status.py` | Prints DB and queue status. |
| Refresh verification | `scripts/check_gene_refresh.py` | Confirms the selected refresh chunk and last run times. |

## Human Review And Confidence

The confidence score is an evidence-support score, not a calibrated probability.
The current rubric lives in `confidence.py`. It scores direct perturbation
evidence, phenotype/model strength, evidence depth, perturbation method strength,
and rule/LLM agreement, with penalties for expression-only, correlation-only,
review-only, or missing-evidence patterns. The website displays a
weak/moderate/strong label and adds review-priority signals when the row looks
risky, such as LLM/rule disagreement, weak extracted evidence, or a functional
label without perturbation evidence.

If `confidence.py` changes, old rows keep old scores until refreshed or
recomputed. To recompute existing rows without rerunning BioMistral:

```bash
python -u scripts/recompute_confidence.py --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db --upload
```

Use the expanded paper row in the website to set:

- `review_status`: `unreviewed`, `needs_review`, or `reviewed`
- `review_label`: `functional`, `not_functional`, `unclear`, or blank
- `review_notes`: short reviewer notes

Reviewer fields are stored in the same SQLite DB. Future worker reruns preserve
existing review fields when updating paper evidence. If the website cannot
upload the DB back to Drive, the review is saved only in the running Space
container; replace/upload the Drive DB before restarting the Space.

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

It contains setup, one small editable settings cell, a status check, one main
maintenance run cell, and a final check. Routine queue processing, retry, and
monthly refresh all go through the same main run cell.

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
python -u scripts/check_queue_status.py \
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

## Main Maintenance Run

Use `scripts/process_queue.py` as the normal maintenance entry point. It handles
new queue requests first, then refreshes existing genes whose `last_run_at` is
older than the configured cutoff.

Recommended Colab-sized batch:

```bash
python -u scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 5 \
  --max-papers 300 \
  --retry-errors \
  --reset-processing \
  --refresh-stale \
  --update-interval-days 30 \
  --max-refresh-genes 15 \
  --refresh-max-papers 300 \
  --upload-at-end
```

To finish a paused monthly campaign, set a fixed cutoff date. This selects genes
whose `last_run_at` is still before the campaign start:

```bash
python -u scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 5 \
  --max-papers 300 \
  --retry-errors \
  --reset-processing \
  --refresh-stale \
  --refresh-before 2026-06-08 \
  --max-refresh-genes 15 \
  --refresh-max-papers 300 \
  --upload-at-end
```

Before and after the run, check status with the same cutoff:

```bash
python -u scripts/check_queue_status.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --refresh-before 2026-06-08
```

### Fast Rules-Only Triage

If Colab GPU is unavailable, you can run a faster lower-quality triage:

```bash
python -u scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --max-requests 5 \
  --max-papers 100 \
  --no-llm
```

Use this only when you need a rough pass. LLM-assisted results are preferred for final review.

## Advanced Manual Monthly Refresh

The main runner now covers routine monthly refresh. Use
`scripts/update_existing_genes.py` only when you intentionally want manual
alphabetical chunk control.

```bash
python -u scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 0 \
  --max-genes 5 \
  --max-papers 50 \
  --upload
```

Next chunk:

```bash
python -u scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 5 \
  --max-genes 5 \
  --max-papers 50 \
  --upload
```

The pipeline skips PMIDs already present in SQLite, so monthly refresh should focus on newly found papers.

Verify the same chunk after it finishes:

```bash
python -u scripts/check_gene_refresh.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --start-at 0 \
  --max-genes 5
```

For a full monthly operating guide, see [`docs/monthly-refresh.md`](monthly-refresh.md).

## Failure Recovery

If Colab disconnects, some requests may remain `processing`. Reset them:

```bash
python -u scripts/process_queue.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --reset-processing \
  --max-requests 0
```

If requests are marked `error` after a code or credential fix, retry them:

```bash
python -u scripts/process_queue.py \
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
