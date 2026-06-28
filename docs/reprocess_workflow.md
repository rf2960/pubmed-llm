# Reprocess / Rebuild Workflow

Use this workflow when algorithm changes should affect existing database rows.
`scripts/recompute_confidence.py` only refreshes score/verifier fields from
stored evidence. It does not rerun PubMed search, evidence retrieval, or
BioMistral.

After the paper-type / best-evidence update, recompute can also backfill:

- `paper_type`
- `best_evidence_quote` from already stored snippets
- `gene_linked_evidence_sents`
- `adjudication_status` / `adjudication_reasons`
- `structured_evidence_json` from already stored snippets

However, it still cannot recover better snippets from PubMed/PMC. Use rebuild
commands below when you want the improved evidence retrieval to affect old rows.

The optional LLM skeptical verifier only runs during live processing or full
reprocessing. It does not run during `recompute_confidence.py`.

Colab flags for the verifier:

```bash
export USE_AGENTIC_VERIFIER=true
export AGENTIC_MODE=borderline
export MAX_VERIFIER_CALLS=8
export VERIFIER_ONLY_BORDERLINE=true
```

In notebook Python cells, set them with `os.environ[...]` before running the
reprocess command.

## Dry Run

Preview a gene rebuild:

```bash
python -u scripts/reprocess_papers.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --gene ADAM10 \
  --max-papers 50 \
  --dry-run
```

## Rebuild Top Ranked Papers For One Gene

```bash
python -u scripts/reprocess_papers.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --gene ADAM10 \
  --max-papers 100 \
  --ignore-cache \
  --upload
```

This reruns PubMed search with the current algorithm, ranks candidates, rebuilds
the top papers, writes a DB backup, updates changed rows, and uploads if Drive
credentials are available.

If `USE_AGENTIC_VERIFIER=true`, selected risky papers also receive the optional
LLM skeptical verifier pass.

## Rebuild All Stored PMIDs For One Gene

```bash
python -u scripts/reprocess_papers.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --gene ADAM10 \
  --all-papers-for-gene \
  --ignore-cache \
  --upload
```

This is useful when the lab wants old rows reclassified without changing which
PMIDs are currently stored for that gene.

## Recompute Evidence-Support Fields Only

```bash
python -u scripts/recompute_confidence.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --gene ADAM10 \
  --upload
```

Use this when the lab wants the new support rubric, paper-type labels,
structured evidence summaries, and review routing fields applied to existing
stored snippets without rerunning the slow BioMistral worker.

## Audit Backfill Coverage

After recompute or selected reprocessing, run:

```bash
python -u scripts/check_algorithm_fields.py \
  --db-path gene_function_lab/gene_function_lab.db
```

This is read-only. It reports missing structured evidence, missing review
reasons, unknown paper type, functional rows without best quotes, weak gene
matches, and risky rows that do not have LLM verifier outputs.

Interpretation:

- missing `structured_evidence_json`: run `scripts/recompute_confidence.py`.
- risky rows without `agentic_verifier_*`: use selected full reprocessing only
  when those rows matter. Fast recompute cannot create real LLM verifier calls.

## Rebuild Selected PMIDs

```bash
python -u scripts/reprocess_papers.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --gene ADAM10 \
  --pmids 39950287 40339955 \
  --ignore-cache \
  --upload
```

## Safety Behavior

- A timestamped DB backup is created before writing unless `--no-backup` is
  provided.
- Changed labels, cancer type, confidence, verifier status, and review routing
  are logged.
- Use `--ignore-cache` after algorithm changes so cached old classifications do
  not hide new behavior.

## Recommended Use

For a major algorithm update, start with:

1. One gene with known user complaints.
2. One high-volume gene.
3. One gene with many false positives.
4. Review disagreement logs before rebuilding many genes.
