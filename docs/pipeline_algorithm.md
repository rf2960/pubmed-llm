# Pipeline Algorithm

This project is an evidence-grounded literature triage system. It is not a
generic RAG chatbot and it is not clinical decision support.

## Current Workflow

```text
gene
  -> PubMed candidate search
  -> candidate ranking
  -> PubMed metadata / abstract retrieval
  -> optional PMC full-text retrieval
  -> evidence-focused sentence retrieval
  -> rules classifier
  -> BioMistral structured classifier
  -> evidence verifier / adjudicator agents
  -> evidence-support score
  -> SQLite database / review UI
```

## Search

The search layer now uses two passes:

1. **Evidence-focused query**: gene + cancer terms + functional evidence terms
   such as knockdown, knockout, CRISPR, siRNA, shRNA, proliferation, apoptosis,
   xenograft, mouse, organoid, in vitro, and in vivo. Review-like publication
   types are excluded from this first pass.
2. **Broad cancer fallback**: gene + cancer terms. This preserves recall for
   papers whose abstracts do not use obvious perturbation vocabulary.

The two PMID lists are merged with the evidence-focused results first.

Gene aliases are optional and conservative. Add aliases to
`data/gene_aliases.tsv` only when the synonym is well known and unlikely to
increase false positives.

## Candidate Ranking

Before BioMistral runs, candidate papers are ranked by lightweight evidence
signals:

- target gene or curated alias mentions
- cancer context
- perturbation terms
- phenotype terms
- experimental model terms
- penalties for review-like and expression-only/biomarker-only language

This ranking helps the worker spend limited Colab/GPU time on better candidate
papers. It does not remove the broad fallback pool.

## Evidence Retrieval

For each paper, the pipeline retrieves evidence-centered snippets from the
abstract and available PMC full text. It prioritizes sentences that mention:

- target gene
- perturbation method
- experimental model
- phenotype or functional outcome
- cancer context

Neighboring sentences are retained for context. The classifier should judge the
paper from these evidence snippets, not only from the title.

## Classification

The rules classifier requires direct gene perturbation plus in vitro or in vivo
phenotype evidence for a functional label. BioMistral receives the extracted
evidence section and returns a structured JSON label. If BioMistral is
unavailable, the system falls back to rules-only mode and marks that fact in
diagnostics.

## Verification

The evidence agent workflow checks:

- whether evidence snippets are present
- whether rules and BioMistral agree
- whether the assigned label is actually supported
- whether a high-confidence label is internally consistent
- whether the row should be routed to human review

The verifier is deterministic by default so the workflow stays affordable and
auditable in Colab.

## Important Limitation

Most rows are classified from abstract-level evidence. Full text is used when
PMC text is available, but many papers do not expose full text through PMC.
Human review remains necessary for high-impact or borderline conclusions.
