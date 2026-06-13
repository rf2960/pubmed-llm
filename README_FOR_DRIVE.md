# Gene Function Lab Drive Handoff

Start here when maintaining the Google Drive copy of the PubMed LLM project.

This Drive folder is the day-to-day working environment for lab maintenance.
The Hugging Face website reads the shared SQLite database from Drive, while
Colab uses this folder to run queue processing and monthly refresh jobs.

## What To Run

Use this notebook for routine work:

```text
pubmed_llm_maintenance_runner.ipynb
```

Do not use `pubmed_llm.ipynb` for normal maintenance. It is an older historical
notebook and should live in `archive/notebooks/`.

## Active Files

These files should stay easy to find:

| Path | Purpose |
| --- | --- |
| `pubmed_llm_maintenance_runner.ipynb` | Main Colab runner for non-coding maintainers. |
| `gene_function_lab/gene_function_lab.db` | Active SQLite database used by the website. Do not rename it. |
| `scripts/check_queue_status.py` | Check database and queue status before and after work. |
| `scripts/process_queue.py` | Main worker for new requests, failed requests, and stale monthly refresh. |
| `scripts/update_existing_genes.py` | Advanced fallback for manual refresh chunks. |
| `scripts/check_gene_refresh.py` | Advanced fallback verification for manual refresh chunks. |
| `pipeline.py` | PubMed retrieval, evidence rules, and BioMistral classification. |
| `db.py` | Database schema, migrations, and update helpers. |
| `drive_sync.py` | Upload/download database sync with Google Drive. |
| `docs/` | Maintenance, deployment, and troubleshooting documentation. |
| `functional_study_cache/` | Runtime paper cache. Keep it, but do not edit manually. |
| `logs/` | Maintenance logs. Keep recent logs; archive older logs by month. |

## Normal Maintenance Run

Use this for routine work. The runner processes pending website requests first,
then refreshes existing genes whose `last_run_at` is older than the configured
interval.

1. Open `pubmed_llm_maintenance_runner.ipynb`.
2. Run setup.
3. Run "Check Status".
4. Edit the small settings cell if needed.
5. Run "Main Maintenance Run".
6. Run "Final Check".
7. Confirm queue pending/error counts and stale refresh counts look correct.
8. Confirm the website syncs to the updated DB.

Use bounded batches first:

```text
MAX_QUEUE_REQUESTS = 5
MAX_PAPERS = 300
MAX_REFRESH_GENES = 15
REFRESH_MAX_PAPERS = 300
```

If a previous run was interrupted and queue status shows `processing > 0`, set
`RESET_INTERRUPTED = True` for one run.

## Monthly Refresh

Monthly refresh is now part of the normal maintenance run. Keep
`REFRESH_EXISTING_GENES = True` and `UPDATE_INTERVAL_DAYS = 30`.

For a paused refresh campaign, set a fixed cutoff date in the runner:

```text
REFRESH_BEFORE = '2026-06-08'
MAX_REFRESH_GENES = 15
```

Then rerun the same main maintenance cell until `Check Status` reports
`Genes needing refresh: 0` for that cutoff.

Do not run all genes at once in Colab unless you are prepared for a long run and
possible runtime interruption.

## What Success Looks Like

After queue processing:

- `Queue pending` decreases.
- `Queue done` increases.
- `Queue error` stays at `0` or only includes known failures.
- website paper/gene counts update after sync.

After monthly refresh:

- selected genes show recent `last_run_at`.
- total papers may increase.
- total genes usually stays the same.
- functional paper count may increase.

## What Not To Touch

Avoid manual edits to:

- `gene_function_lab/gene_function_lab.db`
- `functional_study_cache/`
- files inside `__pycache__/`
- Hugging Face or Google service-account secrets

Never paste real passwords, API tokens, or service-account JSON into GitHub,
screenshots, Slack, email, or documentation.

## Recommended Drive Cleanup

Keep active paths stable, but move old material out of the main view:

```text
archive/
  notebooks/
  old_databases/
  old_outputs/
  old_hf_space/
  old_logs/
  generated_files/
```

Archive:

- `pubmed_llm.ipynb`
- old duplicate `gene_function_lab` folders
- old DB backups
- old CSV exports
- stale `hf_space` copies
- old logs

Safe to delete:

- `__pycache__/`
- `.pyc` files

See `docs/drive-cleanup.md` for the full cleanup checklist.
