# Maintenance Pipeline Review

For day-to-day operations, use the script-based guide in
[`docs/maintenance.md`](maintenance.md). This file records the pipeline audit
and rationale behind the maintenance changes.

## Does The Current Workflow Support The Goal?

Yes, with the updated maintenance cells and pipeline safeguards:

```text
Lab member requests a gene on Hugging Face
  -> Flask app writes the gene into request_queue
  -> Colab notebook reads pending queue entries
  -> pipeline.py processes PubMed/PMC papers for each gene
  -> rows are saved into SQLite
  -> request is marked done/error
  -> database is uploaded/synced for the website
```

Monthly refresh is also supported:

```text
Existing genes in SQLite
  -> unified Colab maintenance cell
  -> PubMed search for each gene
  -> skip PMIDs already in SQLite/cache
  -> process only new PMIDs
  -> save new rows
  -> upload/sync database
```

## Important Fixes Made

The original notebook and pipeline had two maintenance risks:

1. The queue cells called `pipeline.analyze_gene(...)`, but did not clearly persist returned rows before marking the request done.
2. Monthly refresh skipped old papers using cache files, but not the SQLite database. If the cache folder was missing or empty, the notebook could reprocess many already-stored papers.

The updated code now:

- saves processed rows into SQLite through `db.upsert_papers_bulk(...)`
- updates gene summary records after processing
- skips PMIDs already present in the database
- marks non-cancer skipped PMIDs in SQLite when possible
- processes queue requests in bounded batches instead of an endless Colab loop
- refreshes stale existing genes from the same maintenance entry point
- reports genes still needing refresh with `scripts/check_queue_status.py`

## Why Colab Can Be Slow

One gene can take a long time because the pipeline may do all of this for up to `max_papers` papers:

- PubMed search
- metadata fetch
- PMC full-text lookup and fetch
- sentence extraction
- rule-based feature detection
- BioMistral-7B inference for each paper
- SQLite/cache writes

For 50 queued genes, a naive run with `max_papers=300` can mean up to 15,000 paper-level passes. That is too much for a single free/limited Colab session.

## Recommended Operating Mode

For Colab:

- Process 3-5 requested genes per run.
- Use `MAX_QUEUE_REQUESTS` in the notebook settings cell.
- Use `MAX_PAPERS = 50` or `100` for first-pass triage, or `300` for routine maintenance.
- Keep `REFRESH_EXISTING_GENES = True` and run bounded refreshes with `MAX_REFRESH_GENES`.
- Verify progress with the final status check before starting another batch.
- Avoid leaving the worker polling forever when the queue is empty.

For heavy queue backlogs:

- Do not try to process all 50 genes in one Colab session.
- Start with lower `max_papers`.
- Prioritize genes manually or by request order.
- Consider a paid GPU VM or institutional GPU server if the lab needs routine large batches.

## Better Long-Term Options

If this becomes a regular lab service, Colab is probably not the right production worker. Better options:

- institutional GPU workstation
- cloud GPU VM with scheduled jobs
- RunPod/Lambda Labs-style rented GPU for batch runs
- CPU-first triage mode followed by LLM only for high-signal papers
- smaller biomedical classifier model for first-pass filtering

The Hugging Face Space should remain CPU-only and should not run BioMistral.
