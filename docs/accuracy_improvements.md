# Accuracy Improvements

This document records practical changes made to improve paper relevance and
classification trustworthiness.

## Weaknesses Found

- PubMed retrieval previously used a broad gene + cancer query, then processed
  the first `max_papers` records. For common genes, many papers were weakly
  related to functional gene experiments.
- The classifier often saw only a small evidence section. If no strict evidence
  sentence was found, the LLM had little useful context.
- Review, biomarker, expression, and correlation papers could appear close to
  true perturbation studies in the result table.
- The confidence score was heuristic and sometimes repeated similar values
  because important signals were coarse or missing.
- Old database rows could be rescored, but not fully rebuilt with improved
  search/evidence logic unless the gene was processed again manually.

## Implemented Changes

### Better PubMed Search

`pipeline.py` now runs an evidence-focused PubMed query before the broad cancer
query. The focused query includes perturbation, phenotype, and model terms and
excludes review-like publication types.

### Candidate Ranking

Candidate papers are ranked before the LLM step using:

- target gene mentions
- cancer terms
- perturbation terms
- phenotype terms
- experimental system terms
- review/expression-only penalties

The ranking is designed to improve the first `max_papers` papers sent to the
expensive worker.

### Evidence-Focused Retrieval

The evidence extractor now scores sentences and keeps high-value neighboring
context. This improves recall when perturbation and phenotype evidence are split
across nearby sentences.

### Verifier, Adjudicator, And Review Routing

The existing deterministic verifier remains the default. It checks direct
gene/evidence support and routes weak or contradictory cases to human review.
An adjudicator step now challenges internally inconsistent classifications, such
as functional labels without strong verifier support or high scores with weak
evidence.

### Rebuild Workflow

`scripts/reprocess_papers.py` can rebuild old rows with the current algorithm.
This is necessary when search, extraction, or classification logic changes.

## What Has Not Been Solved Yet

- There is still no formal gold-label benchmark unless the lab creates one.
- BioMistral is still a single local classifier, not a calibrated model.
- Full-text retrieval depends on PMC availability.
- Gene aliases remain manual and conservative.
- The system can still miss papers whose abstracts do not mention direct
  perturbation/phenotype evidence.
