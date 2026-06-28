# Human Review Dataset

Use a small human-labeled CSV to measure search/classification quality. This is
the highest-value next step for making confidence scores more meaningful.

## Template

Start from:

```text
data/gold_labels_template.csv
```

Copy it to a working file, for example:

```text
data/gold_labels_lab_review.csv
```

Do not overwrite the template.

## Required Columns

| Column | Meaning |
| --- | --- |
| `gene` | Target gene symbol. |
| `pmid` | PubMed ID. |
| `human_label_functional` | `yes` or `no`. |

## Recommended Columns

| Column | Meaning |
| --- | --- |
| `title` | Paper title for reviewer context. |
| `abstract_or_evidence` | Abstract or strongest evidence snippet. |
| `cancer_type` | Human-reviewed cancer category. |
| `evidence_type` | `in_vitro`, `in_vivo`, `both`, or `none`. |
| `perturbation_method` | KO, KD, siRNA, shRNA, CRISPR, screen, none. |
| `gene_relevance` | direct, indirect, ambiguous, unrelated. |
| `search_relevance` | relevant, borderline, irrelevant. |
| `confidence_should_be` | high, medium, low. |
| `needs_review` | yes/no. |
| `notes` | Reviewer explanation. |

## Evaluation

Run:

```bash
python -u scripts/evaluate_gold_labels.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --labels data/gold_labels_lab_review.csv \
  --write-disagreements outputs/gold_label_disagreements.csv
```

The script reports precision, recall, F1, accuracy, missing rows, score-band
accuracy, and errors grouped by `paper_type`. If requested, it writes
false-positive / false-negative examples.

## Suggested Labeling Strategy

Start small:

1. Label 20 clearly functional papers.
2. Label 20 expression/correlation papers.
3. Label 20 borderline papers routed to review.
4. Include at least 5 papers from genes that users complained about.

This is enough to reveal the biggest failure modes before doing a large review.
