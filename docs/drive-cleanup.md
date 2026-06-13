# Google Drive Cleanup Checklist

This checklist keeps the Drive workspace understandable for future lab members
without changing the current architecture.

The cleanup strategy is conservative:

- keep active runtime paths stable
- archive old or confusing material instead of deleting it immediately
- delete only generated files that can be recreated
- never move or rename the active database unless code and Space settings are
  updated and tested

## Current Active Drive Root

Expected active folder:

```text
MyDrive/pubmed_llm/
```

Expected active database:

```text
MyDrive/pubmed_llm/gene_function_lab/gene_function_lab.db
```

Expected active cache:

```text
MyDrive/pubmed_llm/functional_study_cache/
```

Do not change those paths during routine cleanup. The Colab runner and scripts
expect them.

## Recommended Clean Structure

```text
pubmed_llm/
  README_FOR_DRIVE.md
  pubmed_llm_maintenance_runner.ipynb
  pipeline.py
  db.py
  drive_sync.py
  requirements-worker.txt
  scripts/
  docs/
  gene_function_lab/
    README_DATABASE.md
    gene_function_lab.db
  functional_study_cache/
  logs/
  archive/
    notebooks/
    old_databases/
    old_outputs/
    old_hf_space/
    old_logs/
    generated_files/
```

## KEEP

Keep these in the active workspace.

| Item | Reason |
| --- | --- |
| `README_FOR_DRIVE.md` | First file future maintainers should read. |
| `pubmed_llm_maintenance_runner.ipynb` | Main Colab runner. |
| `pipeline.py` | PubMed retrieval, rules, evidence extraction, LLM classification. |
| `db.py` | SQLite schema, migrations, queue, review, and export helpers. |
| `drive_sync.py` | Drive upload/download sync for worker and website. |
| `requirements-worker.txt` | Colab worker dependencies. |
| `scripts/common.py` | Shared runtime config and logging helpers. |
| `scripts/check_queue_status.py` | Queue and DB status check. |
| `scripts/process_queue.py` | Main worker for website queue and stale gene refresh. |
| `scripts/update_existing_genes.py` | Advanced fallback for manual refresh chunks. |
| `scripts/check_gene_refresh.py` | Advanced fallback verifier for manual refresh chunks. |
| `docs/` | Maintenance documentation. |
| `gene_function_lab/gene_function_lab.db` | Active source-of-truth database. |
| `functional_study_cache/` | Runtime cache. Avoid manual edits. |
| `logs/` | Recent troubleshooting logs. |
| `secrets/README.md` | Secret setup instructions only. |

## ARCHIVE

Move these into `archive/` so they do not confuse maintainers.

| Item | Destination | Reason |
| --- | --- | --- |
| `pubmed_llm.ipynb` | `archive/notebooks/` | Legacy notebook with old workflow and historical output. |
| old duplicate `gene_function_lab/` folders | `archive/old_databases/` | Avoid updating or syncing the wrong DB. |
| `gene_function_lab_old.db` | `archive/old_databases/` | Historical backup, not active. |
| old CSV exports | `archive/old_outputs/` | Useful history, not part of operations. |
| stale `hf_space/` copies | `archive/old_hf_space/` | Current Space files should come from GitHub or the live Space, not stale Drive copies. |
| old `process_queue_*.log` files | `archive/old_logs/YYYY-MM/` | Keep current logs easy to scan. |
| old `update_existing_genes_*.log` files | `archive/old_logs/YYYY-MM/` | Keep current logs easy to scan. |

## DELETE Safe

These are generated files and can be removed from Drive.

| Item | Reason |
| --- | --- |
| `__pycache__/` | Python bytecode cache, regenerated automatically. |
| `*.pyc` | Python bytecode cache, regenerated automatically. |
| empty temporary `.tmp` files | Failed sync leftovers, safe after confirming no run is active. |

## REVIEW LATER

Do not delete these until someone confirms they are no longer needed.

| Item | Why |
| --- | --- |
| older `functional_study_cache/` folders outside active root | They may speed up reruns, but are not required if inactive. |
| old Hugging Face deployment folders | Confirm whether they match the live Space before archiving permanently. |
| unknown files created by the original author | Archive first, delete only after a full handoff review. |

## Minimum Cleanup Procedure

1. Create `archive/` in `MyDrive/pubmed_llm/`.
2. Create subfolders:
   - `archive/notebooks/`
   - `archive/old_databases/`
   - `archive/old_outputs/`
   - `archive/old_hf_space/`
   - `archive/old_logs/`
   - `archive/generated_files/`
3. Move `pubmed_llm.ipynb` to `archive/notebooks/`.
4. Move old DB backups and old CSV exports to archive.
5. Move stale `hf_space/` folders to archive after confirming the live Space is
   managed through GitHub.
6. Delete `__pycache__/` and `.pyc` files.
7. Leave `gene_function_lab/gene_function_lab.db` in place.
8. Leave `functional_study_cache/` in place.
9. Run `pubmed_llm_maintenance_runner.ipynb` section "Check Status".
10. Open the website and confirm paper/gene counts still load.

## After Cleanup

The active folder should show only files that a maintainer might reasonably use.

Expected active top-level items:

```text
README_FOR_DRIVE.md
pubmed_llm_maintenance_runner.ipynb
pipeline.py
db.py
drive_sync.py
requirements-worker.txt
scripts/
docs/
gene_function_lab/
functional_study_cache/
logs/
archive/
secrets/
```

The old notebook should not be visible in the active root.
