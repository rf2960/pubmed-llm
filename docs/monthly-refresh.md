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

## Before You Start

1. Open `pubmed_llm_maintenance_runner.ipynb` in Colab.
2. Mount Drive and run setup.
3. Confirm the DB path points to the shared Drive DB.
4. Set `MAX_PAPERS` based on how deep you want to refresh.
5. Make a dated backup copy of the DB for large runs.

Recommended starting settings:

```python
START_AT = 0
MAX_GENES = 15
MAX_PAPERS = 500
```

`MAX_PAPERS=500` means up to 500 PubMed hits per gene can be considered. The script still skips PMIDs already in the database, so it does not reprocess every old paper.

## Stable Chunking

`scripts/update_existing_genes.py` uses stable alphabetical gene order by default.

That means these chunks are safe:

```text
0-14      START_AT = 0
15-29     START_AT = 15
30-44     START_AT = 30
45-59     START_AT = 45
...
```

Earlier runs update `last_run_at`, but the default gene order does not change because it is alphabetical.

## Run A Chunk

```bash
python -u scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 0 \
  --max-genes 15 \
  --max-papers 500 \
  --upload
```

Then continue:

```bash
python -u scripts/update_existing_genes.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --cache-dir /content/drive/MyDrive/pubmed_llm/functional_study_cache \
  --start-at 15 \
  --max-genes 15 \
  --max-papers 500 \
  --upload
```

## Verify A Chunk

After a chunk finishes, run:

```bash
python -u scripts/check_gene_refresh.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --start-at 0 \
  --max-genes 15
```

For the second chunk:

```bash
python -u scripts/check_gene_refresh.py \
  --db-path /content/drive/MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db \
  --start-at 15 \
  --max-genes 15
```

Healthy output should show:

- the same selected genes as the update command
- recent `last_run_at` values
- nonzero paper counts for processed genes
- newest publication year if recent papers were found

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
Gene order: gene
Selected 15 gene(s): ...
[1/15] Refreshing ...
Done GENE: 23 new row(s), 5.2 min.
Refresh complete. genes=15 failed=0
```

If the final line says `failed=0`, the script did not hit any gene-level exceptions.

## When To Stop

It is fine to stop after a chunk. Record the next `START_AT` value and continue later.

For 255 genes with `MAX_GENES=15`, the chunk starts are:

```text
0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180, 195, 210, 225, 240
```

The last chunk will contain fewer than 15 genes.

## If Colab Disconnects

Monthly refresh does not use queue status, so there is no `processing` queue row to reset. Just rerun the same chunk. Existing PMIDs are skipped, and new rows are upserted into SQLite.

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

- `MAX_GENES=15` is a reasonable Colab chunk size.
- `MAX_PAPERS=500` can be slow for very common genes.
- If one chunk takes too long, lower `MAX_GENES` first.
- If a specific gene has too many hits, rerun that gene with lower `MAX_PAPERS`.
- Do not run BioMistral in the Hugging Face CPU Space.
