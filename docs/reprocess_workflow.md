# Reprocess / Rebuild Workflow

Use this workflow when algorithm changes should affect existing database rows.
`scripts/recompute_confidence.py` only refreshes score/verifier fields from
stored evidence. It does not rerun PubMed search, evidence retrieval, or
BioMistral.

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
