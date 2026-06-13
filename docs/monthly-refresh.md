# Monthly Refresh Guide

Use this guide once per month to check existing genes for newly published papers.

## Goal

The database already contains many genes. Monthly refresh should:

- keep the same genes
- search PubMed again
- skip PMIDs already stored in SQLite
- add newly found papers
- update per-gene counts
- preserve human review fields for existing rows
- sync the updated DB to the website

Do not try to refresh all genes in one Colab session unless you have stable compute and time.
The routine path is now the unified maintenance runner, which processes new
queue requests first and then refreshes existing stale genes in a bounded batch.

## Before You Start

1. Open `pubmed_llm_maintenance_runner.ipynb` in Colab.
2. Mount Drive and run setup.
3. Confirm the DB path points to the shared Drive DB.
4. Set `REFRESH_EXISTING_GENES = True`.
5. Set `REFRESH_MAX_PAPERS` based on how deep you want to refresh.
6. Make a dated backup copy of the DB for large runs.

Recommended starting settings:

```python
MAX_QUEUE_REQUESTS = 5
MAX_PAPERS = 300
REFRESH_EXISTING_GENES = True
UPDATE_INTERVAL_DAYS = 30
MAX_REFRESH_GENES = 15
REFRESH_MAX_PAPERS = 300
```

`REFRESH_MAX_PAPERS=300` means up to 300 PubMed hits per existing gene can be
considered. The pipeline skips PMIDs already in the database, so it does not
reprocess every old paper.

## Routine Monthly Run

Use `scripts/process_queue.py` through the maintenance runner. It selects genes
whose `genes.last_run_at` is older than the configured interval.

For a normal rolling monthly refresh:

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

For a paused campaign, use a fixed cutoff date. Example: to continue genes that
were not refreshed on or after June 8, 2026:

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

## Verify Progress

Before and after a run, check the same refresh cutoff:

```bash
python -u scripts/check_queue_status.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --refresh-before 2026-06-08
```

Healthy output should show:

- queue pending/error counts at `0`
- `Genes needing refresh` decreasing after each run
- recent `last_run_at` values for refreshed genes
- paper counts increasing when new PubMed papers are found

## Advanced Manual Chunking

Use `scripts/update_existing_genes.py` only if you intentionally want stable
alphabetical chunk control. This is useful for debugging one batch, but it is no
longer the recommended routine workflow.

```bash
python -u scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 0 \
  --max-genes 15 \
  --max-papers 300 \
  --upload
```

Verify a manual chunk with:

```bash
python -u scripts/check_gene_refresh.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --start-at 15 \
  --max-genes 15
```

If a gene shows `missing_from_genes_table`, it has not been processed or its summary record is missing.

## Sync The Website

After the Drive DB is updated:

1. Open the Hugging Face Space.
2. Log in.
3. Trigger sync or restart the Space.
4. Confirm the top-right `synced` timestamp changed.
5. Confirm paper/functional counts changed when expected.

If the timestamp changes but counts do not, the Space is probably reading a different DB file. Check `GOOGLE_DRIVE_DB_FILE_ID`.

## Reading The Logs

The scripts create timestamped logs in:

```text
logs/
```

Useful lines:

```text
Stale refresh cutoff: ...
Selected refresh GENE last_run_at=...
[stale refresh] GENE with max_papers=...
Done GENE: 23 new row(s), 5.2 min.
Batch complete. queue_attempted=... queue_succeeded=... queue_failed=... refresh_succeeded=... refresh_failed=0
```

If the final line says `queue_failed=0` and `refresh_failed=0`, the script did
not hit any gene-level exceptions.

## When To Stop

It is fine to stop after any bounded run. Next time, use the same
`REFRESH_BEFORE` date for a paused campaign or the same `UPDATE_INTERVAL_DAYS`
for routine monthly maintenance. The unified runner will skip recently refreshed
genes automatically.

## If Colab Disconnects

For the unified runner, set `RESET_INTERRUPTED = True` once if queue status
shows `processing > 0`, then rerun the same maintenance cell. Existing PMIDs are
skipped, and new rows are upserted into SQLite.

If the same gene repeatedly fails:

1. Copy the error from the log.
2. Retry only that gene:

```bash
python -u scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --genes GENE_SYMBOL \
  --max-papers 500 \
  --upload
```

## Practical Compute Guidance

- `MAX_REFRESH_GENES=15` is a reasonable Colab refresh chunk size.
- `REFRESH_MAX_PAPERS=300` is the current routine setting; `500` can be slow for very common genes.
- If one run takes too long, lower `MAX_REFRESH_GENES` first.
- If a specific gene has too many hits, rerun with lower `REFRESH_MAX_PAPERS`.
- Do not run BioMistral in the Hugging Face CPU Space.
