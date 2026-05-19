# Google Drive Upload Bundle

This repository can generate a local Drive upload bundle at:

```text
drive_upload/pubmed_llm/
```

Upload the inner `pubmed_llm` folder to Google Drive so the final Drive path is:

```text
MyDrive/pubmed_llm/
```

That path matches the Colab notebook constants:

```python
DRIVE_FOLDER = "/content/drive/MyDrive/pubmed_llm"
DB_PATH = f"{DRIVE_FOLDER}/gene_function_lab/gene_function_lab.db"
CACHE_DIR = "/content/drive/MyDrive/pubmed_llm/functional_study_cache"
SA_KEY_PATH = f"{DRIVE_FOLDER}/service-account.json"
```

## Bundle Layout

```text
pubmed_llm/
  README_FOR_DRIVE.md
  pubmed_llm.ipynb
  pipeline.py
  db.py
  drive_sync.py
  requirements-worker.txt
  scripts/
    check_queue_status.py
    process_queue.py
    update_existing_genes.py
    common.py
  docs/
    maintenance.md
  gene_function_lab/
    gene_function_lab.db
  functional_study_cache/
    .gitkeep
  secrets/
    README.md
  hf_space/
    README_HF_SPACE.md
    app.py
    db.py
    drive_sync.py
    Dockerfile
    requirements.txt
    templates/
      index.html
```

## Why The Bundle Is Ignored

`drive_upload/` is intentionally gitignored because it is a generated package that duplicates source files and includes a database snapshot. Keep the canonical source in the repo root; regenerate or refresh the bundle when you need to upload to Drive.

## Running Maintenance From Drive

After uploading the bundle to Drive, open Colab and run:

```python
from google.colab import drive
drive.mount("/content/drive")
%cd /content/drive/MyDrive/pubmed_llm
!pip install -r requirements-worker.txt
```

Then check the queue:

```python
!python scripts/check_queue_status.py
```

Process a small batch:

```python
!python scripts/process_queue.py --max-requests 1 --max-papers 25 --reset-processing --upload-at-end
```

## Secret Handling

Do not commit or share real secrets. If the Colab notebook needs Drive upload access, place a real service-account key in the uploaded Drive folder as:

```text
MyDrive/pubmed_llm/service-account.json
```

This file is not included in the generated bundle.

If the Hugging Face sync timestamp changes but the website still shows old
paper/gene counts, set `GOOGLE_DRIVE_DB_FILE_ID` in both Hugging Face Space
secrets and the worker runtime. This avoids accidentally syncing a different
`gene_function_lab.db` file with the same name.
