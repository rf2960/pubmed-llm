# Gene Function Lab — Setup Guide

A queryable lab tool for identifying functional cancer studies from PubMed.
Built on Google Drive + Google Colab (free GPU) + Hugging Face Spaces (free hosting).

---

## Architecture

```
Google Drive          ← stores the SQLite DB (gene_function_lab.db)
     ↑↓                    and cache files
Google Colab          ← runs the pipeline (BioMistral-7B, free T4 GPU)
     ↑↓
HF Spaces             ← serves the public/lab website (CPU, always free)
```

---

## One-time setup (do this once, then hand off to lab)

### Step 1 — Google Service Account (10 min)

1. Go to https://console.cloud.google.com
2. Create a new project (or use existing)
3. Enable the **Google Drive API**:
   - APIs & Services → Library → search "Google Drive API" → Enable
4. Create a Service Account:
   - APIs & Services → Credentials → Create Credentials → Service Account
   - Name it anything (e.g. "gene-lab-service")
   - Skip optional steps, click Done
5. Click the service account → Keys tab → Add Key → JSON
6. Download the JSON file — **keep this safe, treat like a password**
   - Do not commit this JSON file into the project folder. Store it as a Hugging Face/Colab secret or keep it in a private Drive location only.
7. Note the service account email (looks like `gene-lab@project.iam.gserviceaccount.com`)

### Step 2 — Google Drive folder

1. Create a folder on your Drive called `gene_function_lab`
2. Right-click the folder → Share → paste the service account email → Editor
3. Upload `gene_function_lab.db` to this folder (or let Colab create it on first run)
4. Also upload your cache folder if you have existing results

### Step 3 — Hugging Face Spaces

1. Go to https://huggingface.co → Sign up (free)
2. New Space → name it `gene-function-lab` → Docker SDK → CPU Basic (free)
3. Upload these files to the Space:
   - `app.py`
   - `db.py`
   - `drive_sync.py`
   - `Dockerfile`
   - `requirements.txt`
   - `templates/index.html`
4. Go to Space Settings → Repository secrets → add:
   - Name: `GOOGLE_SERVICE_ACCOUNT_JSON`
   - Value: paste the entire contents of your service account JSON file
5. The Space will build automatically (~2 min). Your URL is:
   `https://huggingface.co/spaces/YOUR_USERNAME/gene-function-lab`
6. **Share this URL with your lab** (only people with the link can access it)

### Step 4 — Colab notebook

1. Upload `pubmed_llm.ipynb` to Google Colab or Drive
2. In Cell 2: replace `hf_YOUR_TOKEN_HERE` with your HuggingFace token or use Colab secrets
3. In Cell 3: replace `your_email@example.com` with your email (for PubMed API)
4. If you have existing cache files, run Cell 6 once to migrate them to the DB

---

## Handing off to a lab member

Give them:
1. The link to the Colab notebook (share from Drive)
2. This README
3. Tell them to run Cell 4 periodically to process new requests from the website
4. Tell them to run Cell 5 monthly to refresh all genes with new PubMed papers

**They do NOT need:**
- The service account JSON (already embedded in HF Spaces secrets)
- Any passwords or API keys (Colab uses Drive automatically via mount)
- Any server access

---

## How the website works

- Lab members go to the HF Spaces URL
- They can search any gene already in the DB — results are instant
- If a gene isn't in the DB, they click "Request gene" to queue it
- The Colab notebook (Cell 4) picks up queued genes and processes them
- Once done, results appear on the website within 1 hour (hourly DB sync)

---

## Files

| File | Where it runs | Purpose |
|------|--------------|---------|
| `db.py` | Colab + HF Spaces | SQLite read/write helpers |
| `drive_sync.py` | HF Spaces | Downloads DB from Drive hourly |
| `app.py` | HF Spaces | Flask web app, CPU only |
| `pipeline.py` | Colab only | PubMed + BioMistral-7B pipeline |
| `templates/index.html` | HF Spaces | Web UI |
| `pubmed_llm.ipynb` | Colab | Worker notebook |
| `Dockerfile` | HF Spaces | Container config |
| `requirements.txt` | HF Spaces | Python deps (no torch) |
| `gene_function_lab/gene_function_lab.db` | Drive/local data | Current SQLite database snapshot |

Generated cache folders (`functional_study_cache/`, `cache_pubmed/`), CSV exports, Python bytecode, and credential JSON files are intentionally excluded from the clean project folder.

---

## Updating the database

**Processing new gene requests** (run when you get email/slack that someone requested a gene):
- Open Colab, run Cell 4
- It processes pending requests one by one and stops when the queue is empty

**Monthly refresh** (picks up new PubMed papers for existing genes):
- Open Colab, run Cell 5
- Takes ~30 min per gene depending on how many new papers exist

**Adding genes yourself** (bypasses the website queue):
- Open Colab, run:
```python
import pipeline
pipeline.run_pipeline(['YOUR_GENE'], max_papers=300)
```

---

## Troubleshooting

**Website shows "DB unavailable"**
→ The HF Space can't reach your Drive. Check the service account JSON secret is set correctly in HF Spaces Settings → Secrets.

**"gene_function_lab.db not found on Drive"**
→ Make sure you shared the `gene_function_lab` folder with the service account email (see Step 2).

**Pipeline fails with "out of memory"**
→ Switch to 4bit quantization: in pipeline.py change `QUANTIZE = "8bit"` to `QUANTIZE = "4bit"`

**Colab disconnects mid-run**
→ Results already written to DB are safe. Re-run Cell 4 — it skips already-processed PMIDs automatically.

**Website results are stale**
→ HF Spaces syncs DB from Drive every hour. To force an immediate refresh, POST to `/api/sync` or wait for the next hourly sync.
