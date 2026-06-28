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
- Paper type was only implied by title keywords and verifier reasons, so review
  and prognosis/expression-only papers were not easy to separate in the UI.
- Evidence snippets could include neighboring phenotype sentences that were
  useful context but did not directly mention the target gene.

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

### Gated LLM Skeptical Verifier

The pipeline now supports an optional second BioMistral verifier pass. This is
not run for every paper. It is gated to high-value cases:

- functional labels
- borderline support scores
- rules/LLM disagreement
- weak deterministic verifier support
- ambiguous gene symbols
- high scores with sparse evidence

The verifier returns structured JSON: decision, direct target-gene study,
perturbation evidence, phenotype evidence, paper-type judgment, quote, reason,
and needs-review flag. Challenge or unclear decisions lower evidence support and
raise human-review priority.

This should reduce false positives where a paper is review-like, expression-only,
prognosis-only, about another gene, or only loosely mentions the target gene.

### Paper Type And Direct Evidence Signals

The pipeline now stores a deterministic `paper_type` label and PubMed
publication types. Likely review, clinical/prognostic, expression-association,
and methods/dataset papers are penalized in the evidence-support score and shown
in the website table.

Evidence retrieval also stores a `best_evidence_quote` and
`gene_linked_evidence_sents`. These fields help reviewers answer the practical
question: "Where is the strongest sentence that actually links this gene to a
functional experiment?"

### More Diagnostic Evaluation

`scripts/evaluate_gold_labels.py` now reports score-band accuracy and errors by
paper type when a small human-labeled dataset is available. This helps the lab
see whether false positives cluster in review-like, prognosis-only, or
expression-only papers.

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
- The optional verifier uses the same local model family as the classifier, so
  it improves reasoning/verification but is not an independent ensemble model.

## Open-Source Resources Reviewed

These resources are relevant for future accuracy work:

- **NCBI E-utilities**: current authoritative PubMed search/fetch API already
  used by the project.
- **PubTator / PubTator3**: useful future source for gene/disease annotations
  and normalized biomedical entities.
- **NCBI Gene alias data**: useful future source for safer alias expansion.
- **scispaCy**: local biomedical entity detection; useful later, but dependency
  and model size make it less urgent than verifier/routing changes.
- **PubMedBERT/BioBERT-family models**: possible future classifiers or sentence
  rankers; would require validation against lab labels before replacing
  BioMistral.
