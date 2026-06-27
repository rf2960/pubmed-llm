# Evidence-Support Score

The `confidence` field is an **evidence-support score**, not a calibrated
probability. BioMistral does not return a probability in this project.

Use the score for triage:

| Band | Range | Meaning |
| --- | --- | --- |
| Weak | `< 0.60` | Needs review before trusting the label. |
| Moderate | `0.60-0.79` | Useful candidate, but evidence is incomplete or mixed. |
| Strong | `>= 0.80` | Multiple evidence signals support the stored label. |

## Components

The score is computed in `confidence.py` from normalized components:

- **Search relevance**: whether the paper looked relevant during candidate
  ranking.
- **Evidence retrieval strength**: whether extracted snippets contain strong
  gene-centered evidence.
- **Direct perturbation evidence**: knockout, knockdown, CRISPR, shRNA, siRNA,
  or related loss-of-function signal.
- **Phenotype/model evidence**: in vitro phenotype, in vivo phenotype, or both.
- **Evidence depth**: number and diversity of extracted evidence snippets.
- **Method strength**: stronger perturbation methods receive more support.
- **Rule/LLM agreement**: disagreement lowers confidence and routes review.
- **Gene specificity**: target gene appears in evidence snippets.
- **Context strength**: full-text evidence helps, abstract-only evidence is
  weaker.
- **Verifier score**: skeptical verifier support or penalties.
- **Adjudication/review routing**: internally inconsistent rows are routed for
  human review instead of being treated as routine.
- **Negative evidence**: review-like, expression-only, correlation-only, or
  missing-evidence patterns reduce support.

## Why Many Old Scores Repeated

Earlier scoring used coarser caps and default values, so many rows landed on
similar values such as `0.65` or `0.78`. The newer rubric uses more continuous
evidence counts, search relevance, retrieval strength, and verifier signals.

## Updating Old Rows

To refresh only the score/verifier fields from stored evidence:

```bash
python -u scripts/recompute_confidence.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --upload
```

This does **not** rerun PubMed search or BioMistral. To rebuild old rows with
the improved search and evidence retrieval algorithm, use the reprocess workflow
in `docs/reprocess_workflow.md`.

## Calibration Plan

The score should not be described as statistically calibrated until the lab has
a labeled validation set. Use `data/gold_labels_template.csv` and
`scripts/evaluate_gold_labels.py` to start measuring precision, recall, and
false-positive patterns.
