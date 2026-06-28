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
- **Gene-linked evidence count**: extracted sentences directly connect the
  target gene to perturbation/model/phenotype evidence.
- **Context strength**: full-text evidence helps, abstract-only evidence is
  weaker.
- **Paper type**: review, clinical/prognostic, expression-association, and
  methods/dataset-like papers are penalized unless direct functional evidence is
  present.
- **Verifier score**: skeptical verifier support or penalties.
- **LLM skeptical verifier**: optional second-pass BioMistral verifier support,
  challenge, or unclear decision for risky rows.
- **Structured evidence completeness**: whether the pipeline found direct
  target-gene evidence, perturbation method, phenotype/outcome terms, and a
  usable quote.
- **Adjudication/review routing**: internally inconsistent rows are routed for
  human review instead of being treated as routine.
- **Negative evidence**: review-like, expression-only, correlation-only, or
  missing-evidence patterns reduce support.

## Why Many Old Scores Repeated

Earlier scoring used coarser caps and default values, so many rows landed on
similar values such as `0.65` or `0.78`. The newer rubric uses more continuous
evidence counts, search relevance, retrieval strength, and verifier signals.
The latest version also uses direct gene-linked evidence counts and paper type,
which should reduce repeated scores caused by rows sharing the same generic
rule/LLM pattern.

## LLM Verifier Effect

When enabled during live processing or full reprocessing, the LLM skeptical
verifier can affect the score:

- `support`: small positive adjustment when the primary label is functional and
  evidence is coherent.
- `challenge`: lowers functional evidence support and caps suspicious
  functional claims.
- `unclear`: keeps the row in a moderate/borderline band and routes it for
  review.

The verifier decision is stored separately from the score so reviewers can see
why the score changed. The score remains heuristic and should not be interpreted
as a calibrated probability.

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

It also does not run the optional LLM verifier. To populate
`agentic_verifier_*` fields for old rows, use full reprocessing on selected
genes/PMIDs.

`recompute_confidence.py` can backfill paper type and a best available evidence
quote from stored evidence snippets, but it cannot recover full-text snippets
that were not stored originally. Use `scripts/reprocess_papers.py --ignore-cache`
when the lab wants old rows fully rebuilt with the newest evidence retrieval.

The same script also backfills `structured_evidence_json` from stored snippets.
This is useful for website review, but it remains limited by what evidence was
already stored in the database.

## Calibration Plan

The score should not be described as statistically calibrated until the lab has
a labeled validation set. Use `data/gold_labels_template.csv` and
`scripts/evaluate_gold_labels.py` to start measuring precision, recall, and
false-positive patterns.
