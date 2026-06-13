# Pipeline Details

This document describes the current extraction pipeline and its maintenance boundaries.

## Entry Points

| Task | Entry point |
| --- | --- |
| Process website queue | `scripts/process_queue.py` |
| Refresh stale existing genes | `scripts/process_queue.py --refresh-stale` |
| Verify DB/queue status | `scripts/check_queue_status.py` |
| Manual refresh fallback | `scripts/update_existing_genes.py` |
| Verify manual refresh chunks | `scripts/check_gene_refresh.py` |
| Low-code Colab maintenance | `pubmed_llm_maintenance_runner.ipynb` |

The core paper analysis function is:

```python
pipeline.analyze_gene(gene, max_papers=...)
```

## Retrieval

For each gene, the pipeline:

1. builds a PubMed query around the gene and cancer/function terms
2. retrieves PMID ids
3. fetches PubMed metadata and abstracts
4. attempts to fetch PMC full text when available
5. caches paper-level results in the configured cache directory

The worker should set `ENTREZ_EMAIL` so NCBI requests include a real contact email.

## Evidence Extraction

The rules look for:

- gene mentions
- cancer context
- perturbation methods
- in vitro and in vivo systems
- phenotype terms
- weak-evidence patterns such as review-only or association-only language

The goal is to extract evidence-bearing passages, not to summarize the whole article.

## LLM Classification

The current worker uses:

```text
BioMistral/BioMistral-7B
```

The LLM step produces structured labels from evidence text. It is one classifier in the pipeline, not an autonomous agent and not a RAG retriever.

Rules-only mode is available with `--no-llm` for faster triage, but it should not be treated as the preferred final workflow.

## Evidence-Support Score

The `confidence` column is an evidence-support score. It is not a calibrated
probability that the paper is correct. It is a structured triage score for how
well the extracted evidence supports the stored functional / not-functional
classification.

The current rubric is implemented in `confidence.py` and is shared by the live
pipeline and the score-recompute script. It scores:

- direct gene perturbation evidence
- phenotype model strength: both in vitro and in vivo > in vivo > in vitro
- evidence depth and diversity across extracted evidence categories
- perturbation method strength, such as knockout/CRISPR versus weaker evidence
- rule/LLM agreement or disagreement
- penalties for expression-only, correlation-only, review-only, or missing
  evidence patterns

Recommended interpretation:

| Score range | Label | Meaning |
| --- | --- | --- |
| `< 0.60` | weak | Needs human review before trusting the label. |
| `0.60-0.79` | moderate | Useful candidate evidence but still reviewable. |
| `>= 0.80` | strong | Multiple evidence signals support the label. |

When the scoring rubric changes, existing database rows keep their old numeric
scores until they are refreshed or recomputed. To update existing rows without
rerunning PubMed retrieval or BioMistral:

```bash
python -u scripts/recompute_confidence.py --db-path gene_function_lab/gene_function_lab.db --upload
```

## Database Writes

The scripts write rows through `db.upsert_papers_bulk(...)` and then call `db.update_gene_record(...)`.

Important behavior:

- `(gene, pmid)` is the primary key.
- Existing PMIDs are skipped before analysis when possible.
- Existing review fields are preserved by the DB layer during normal updates.
- Queue rows are marked `done` or `error` by `scripts/process_queue.py`.

## Known Bottlenecks

- Loading BioMistral can take minutes in a fresh Colab runtime.
- Common genes can return hundreds or thousands of PubMed hits.
- PMC full-text fetches add network overhead.
- Colab can disconnect before a large batch completes.

The safest operational response is chunking, logging, and verification rather than one huge run.

## Future Pipeline Improvements

Keep future work evidence-grounded:

- add a small gold-label evaluation set
- add a verifier pass for rule/LLM disagreement
- calibrate thresholds from human review labels
- separate retrieval, evidence extraction, classification, and scoring into smaller modules
- add tests for DB migrations and confidence scoring
