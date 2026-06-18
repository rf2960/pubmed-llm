# PubMed-LLM Website Update Quickstart

This guide is only for routine website/database updates. Code changes and deeper
pipeline edits should be handled by the current maintainer.

## What To Open

In Google Drive, open:

```text
pubmed_llm/pubmed_llm_maintenance_runner.ipynb
```

Open it with **Google Colab**.

## Required Access / Secrets

Do not paste secrets directly into notebook code. Add them in Colab:

```text
Left sidebar -> key icon "Secrets" -> Add new secret
```

Common secrets:

| Secret name | What it is used for | Where to get it |
| --- | --- | --- |
| `HF_TOKEN` | Downloads the BioMistral model from Hugging Face with better reliability. | Hugging Face account -> Settings -> Access Tokens. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Allows automatic DB upload/sync through Google Drive API. | Ask the current maintainer or lab owner for the service-account JSON. |
| `GOOGLE_DRIVE_DB_FILE_ID` | Identifies the Drive DB file used by the website. | From the Google Drive DB file URL, or ask the current maintainer. |
| `ENTREZ_EMAIL` | NCBI PubMed API contact email. | Use a lab or maintainer email. |

If `GOOGLE_SERVICE_ACCOUNT_JSON` is missing, the notebook may still update the
DB file in Drive, but automatic upload may fail. Ask the maintainer before
changing Drive file locations.

## Routine Update Cycle

1. Open `pubmed_llm_maintenance_runner.ipynb` in Colab.
2. Run **1. Setup**.
3. Review **2. Settings**.
   - `MAX_QUEUE_REQUESTS`: how many new queued genes to process.
   - `MAX_PAPERS`: papers per new gene.
   - `REFRESH_EXISTING_GENES`: keep `True` for monthly maintenance.
   - `MAX_REFRESH_GENES`: how many existing genes to refresh in this run.
   - `REFRESH_MAX_PAPERS`: papers checked per refreshed gene.
4. Run **3. Check Status**.
5. Run **4. Main Maintenance Run**.
6. Run **5. Final Check**.
7. Open the Hugging Face website and trigger/sanity-check sync.
8. Confirm the website top-right stats and synced time updated.

## Hugging Face Website Sync

Open the Space website. If needed, trigger the sync endpoint:

```text
https://<space-domain>/api/sync?token=<APP_PASSWORD>
```

Do not save the real app password in GitHub or public docs.

After syncing, verify:

- paper count looks reasonable
- gene count looks reasonable
- `synced ... UTC` timestamp updated
- queue page has no unexpected pending/error rows

## If Something Fails

| Symptom | What to do |
| --- | --- |
| Colab disconnects | Reopen the notebook, run Setup, then Check Status. If rows are stuck as `processing`, set `RESET_INTERRUPTED = True` and rerun Main Maintenance Run. |
| `GOOGLE_SERVICE_ACCOUNT_JSON secret not set` | DB processing may still have succeeded, but automatic upload failed. Ask the maintainer for the service-account JSON or manually verify the Drive DB used by the website. |
| Hugging Face website is asleep | Open the website and wait for it to wake up. Free Spaces may sleep when unused. |
| Website stats did not update | Trigger `/api/sync`, then refresh the page. If still unchanged, check that the website is reading the same Drive DB file that Colab updated. |
| Many queue errors | Stop and send the latest log file from `pubmed_llm/logs/` to the maintainer. |

## Files To Avoid Editing

Do not manually edit:

```text
gene_function_lab/gene_function_lab.db
functional_study_cache/
scripts/
pipeline.py
db.py
confidence.py
```

Routine users should only run the maintenance notebook.
