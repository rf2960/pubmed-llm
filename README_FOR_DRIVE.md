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
| `scripts/process_queue.py` | Process new gene requests from the website queue. |
| `scripts/update_existing_genes.py` | Monthly refresh for genes already in the database. |
| `scripts/check_gene_refresh.py` | Verify that a monthly refresh chunk updated successfully. |
| `pipeline.py` | PubMed retrieval, evidence rules, and BioMistral classification. |
| `db.py` | Database schema, migrations, and update helpers. |
| `drive_sync.py` | Upload/download database sync with Google Drive. |
| `docs/` | Maintenance, deployment, and troubleshooting documentation. |
| `functional_study_cache/` | Runtime paper cache. Keep it, but do not edit manually. |
| `logs/` | Maintenance logs. Keep recent logs; archive older logs by month. |

## Normal Queue Processing

Use this when the website has pending gene requests.

1. Open `pubmed_llm_maintenance_runner.ipynb`.
2. Run setup.
3. Run "Check Status".
4. Run "Process New Queue Requests".
5. Run "Check Status" again.
6. Confirm queue pending/error counts look correct.
7. Confirm the website syncs to the updated DB.

Use small batches first:

```text
MAX_REQUESTS = 3
MAX_PAPERS = 50
```

If a previous run was interrupted and queue status shows `processing > 0`, set
`RESET_PROCESSING = True` for one run, then set it back to `False`.

## Monthly Refresh

Use this once per month to look for newly published papers for genes already in
the database.

Recommended pattern:

```text
START_AT = 0
MAX_GENES = 15
MAX_PAPERS = 500
```

After the chunk finishes:

1. Run the refresh verification cell.
2. Check that the selected genes have a recent `last_run_at`.
3. Change `START_AT` to the next chunk: `15`, then `30`, then `45`, etc.

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

