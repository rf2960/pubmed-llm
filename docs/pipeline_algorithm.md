# Pipeline Algorithm

This document describes the current PubMed-LLM paper search and classification
pipeline. It should be treated as the main technical reference for how the
project decides which papers are likely functional gene evidence.

The system is an evidence-grounded biomedical literature triage system. It is
not a generic RAG chatbot, not clinical decision support, and not a statistically
calibrated classifier.

## Current Pipeline, End To End

```text
Lab member submits/searches gene
  -> Hugging Face Flask website
  -> SQLite request_queue
  -> Colab/GPU maintenance runner
  -> PubMed candidate search
  -> PubMed candidate ranking
  -> PubMed metadata + abstract retrieval
  -> optional PMC full-text retrieval
  -> evidence-focused sentence retrieval
  -> deterministic paper-type classifier
  -> structured evidence extractor agent
  -> rules classifier
  -> BioMistral structured classifier
  -> evidence verifier agents
  -> optional gated LLM skeptical verifier
  -> adjudicator + human-review router
  -> evidence-support score
  -> SQLite papers/genes tables
  -> Google Drive DB sync
  -> Hugging Face review/search UI
```

## 1. Gene Request And Queue

Gene requests are submitted through the Hugging Face website and stored in the
SQLite `request_queue` table. The maintenance runner processes pending requests
using `scripts/process_queue.py`.

Queue states:

- `pending`: waiting to be processed.
- `processing`: currently running or interrupted mid-run.
- `done`: successfully processed.
- `error`: failed and needs retry or review.

The queue processor can also retry failed rows and refresh existing genes whose
`genes.last_run_at` is stale.

## 2. PubMed Search

Search is implemented in `pipeline.py`.

The search layer uses two passes:

1. **Evidence-focused query**
   - gene symbol plus curated aliases
   - cancer terms
   - perturbation terms: knockdown, knockout, CRISPR, siRNA, shRNA, RNAi
   - phenotype terms: proliferation, apoptosis, migration, invasion, tumor
     growth, survival, colony formation
   - model terms: cell line, xenograft, mouse, organoid, in vitro, in vivo
   - excludes review-like publication types in the focused pass

2. **Broad cancer fallback**
   - gene symbol plus cancer terms
   - preserves recall when abstracts do not use obvious functional vocabulary

The evidence-focused PMID list is merged before the broad fallback list, so the
worker spends GPU time on higher-value candidates first.

## 3. Gene Alias Handling

Aliases are loaded from `data/gene_aliases.tsv`.

Alias handling is intentionally conservative. Broad automatic synonym expansion
can hurt precision, especially for short or ambiguous gene symbols. Add aliases
only when the synonym is well known and unlikely to retrieve unrelated papers.

## 4. Candidate Ranking

Before BioMistral runs, candidate papers are ranked with a lightweight heuristic
ranker. This ranker scores:

- target gene or curated alias mentions
- cancer context
- perturbation keywords
- phenotype keywords
- model/system keywords
- penalties for review-like language
- penalties for expression/biomarker/prognosis-only language

The ranker does not make the final classification. It decides which candidate
papers should be processed first when `max_papers` is limited.

## 5. Metadata And Full-Text Retrieval

For each selected PMID, the worker retrieves:

- title
- abstract
- journal
- year
- DOI
- PubMed publication types

When available, the worker also retrieves PMC full text. Most papers still rely
on abstract-level evidence because not all full text is available through PMC.

## 6. Evidence Retrieval

The evidence extractor scores sentences from the abstract and available PMC full
text. It prioritizes sentences mentioning:

- the target gene
- perturbation method
- cancer context
- in vitro or in vivo experimental model
- phenotype or functional outcome

Neighboring sentences are retained for context, but phenotype evidence is only
counted as in vitro/in vivo evidence when the sentence is directly linked to the
queried gene.

Stored evidence fields include:

- `evidence_perturbation`
- `evidence_in_vitro`
- `evidence_in_vivo`
- `evidence_crispr_screen`
- `best_evidence_quote`
- `total_evidence_sents`
- `gene_linked_evidence_sents`
- `evidence_retrieval_score`

`best_evidence_quote` is the strongest extracted sentence that directly links
the target gene with experimental evidence. This helps reviewers quickly inspect
why a paper was scored.

## 7. Paper-Type Classifier

`paper_type.py` assigns a deterministic triage label:

- `functional_experiment`
- `functional_screen`
- `review`
- `clinical_prognostic`
- `expression_association`
- `methods_or_dataset`
- `unknown`

This is not a formal publication-type ontology. It is a practical paper-review
signal used by the confidence score, verifier, CSV export, and website UI.

Review/prognosis/expression/methods-like papers are penalized unless the
extracted evidence contains direct functional evidence.

## 8. Rules Classifier

The rules classifier requires:

1. direct perturbation of the target gene, and
2. measured cancer phenotype in vitro or in vivo.

Examples of accepted perturbation signals:

- knockout
- knockdown
- shRNA
- siRNA
- CRISPR/Cas9
- gene deletion/silencing/loss-of-function

Examples of accepted phenotype/model signals:

- proliferation
- viability
- apoptosis/cell death
- colony formation
- organoid growth
- xenograft tumor growth
- tumor size/volume
- survival

Expression-only, correlation-only, biomarker-only, and prognosis-only papers
should not be labeled functional by rules alone.

## 9. BioMistral Classifier

BioMistral-7B receives:

- paper title
- extracted evidence section
- truncated abstract as context

It returns structured JSON for:

- functional study yes/no
- perturbation method flags
- in vitro / in vivo flags
- impact type
- cancer type
- one-sentence reasoning

BioMistral does not return a calibrated probability. If BioMistral fails or is
disabled, the pipeline falls back to rules-only classification and records that
diagnostic.

## 10. Evidence Agents

The project uses small, auditable workflow agents. They are not autonomous
chatbots.

Agents:

- **Evidence Finder Agent**: summarizes evidence coverage.
- **Structured Evidence Extractor Agent**: converts snippets into reviewable
  fields: evidence type, perturbation methods, phenotype terms, cancer context,
  best quote, and missing evidence components.
- **Classifier Consensus Agent**: records whether rules and BioMistral agree.
- **Deterministic Skeptical Verifier Agent**: checks whether extracted evidence
  actually supports the label.
- **LLM Skeptical Verifier Agent**: optional second BioMistral pass that
  challenges risky classifications only.
- **Adjudicator Agent**: challenges high-risk or internally inconsistent labels.
- **Human Review Router**: assigns review priority and reasons.

Most agents are deterministic. The optional LLM verifier is gated so it does not
run for every paper.

The structured extractor is deterministic and runs for every processed paper.
It does not add runtime-heavy LLM calls. Its purpose is transparency: it records
what evidence the pipeline actually found and what is missing before a reviewer
trusts the label.

### LLM Verifier Runtime Strategy

The second LLM verifier runs only when it can materially improve review quality.
Default triggers include:

- functional paper labels
- borderline evidence-support scores
- rules/BioMistral disagreement
- deterministic verifier status of `needs_review`, `weak_support`, or
  `not_supported`
- ambiguous gene symbols
- high-confidence rows with weak extracted evidence

Runtime flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `USE_AGENTIC_VERIFIER` | `true` | Enables the optional LLM verifier in live processing/reprocessing. |
| `AGENTIC_MODE` | `borderline` | `borderline`, `functional`, `all`, or `off`. |
| `MAX_VERIFIER_CALLS` | `8` | Max LLM verifier calls per gene run. |
| `VERIFIER_ONLY_BORDERLINE` | `true` | Keeps verifier calls focused on risky rows. |

The verifier returns structured JSON:

- `verifier_decision`: `support`, `challenge`, or `unclear`
- target-gene directness
- direct perturbation yes/no
- phenotype evidence yes/no
- paper type judgment
- evidence quote
- reason
- needs-review flag

Verifier failures are non-fatal. The pipeline falls back to deterministic
verification and review routing.

## 11. Evidence-Support Score

The `confidence` column is an evidence-support score from `0.00` to `1.00`.

It is not a calibrated probability.

Main components:

- search relevance
- evidence retrieval strength
- direct perturbation evidence
- phenotype/model evidence
- evidence depth and diversity
- method strength
- rule/LLM agreement
- gene specificity
- direct gene-linked evidence count
- full-text vs abstract-only context
- paper type
- skeptical verifier score
- optional LLM verifier decision
- structured-evidence completeness
- negative evidence patterns

Website bands:

- weak: `< 0.60`
- moderate: `0.60-0.79`
- strong: `>= 0.80`

Use this score for triage and review prioritization, not as a scientific
probability.

## 12. Review Routing

Rows are routed for human review when signals suggest uncertainty or possible
false positives, including:

- rules/LLM disagreement
- weak verifier support
- adjudicator challenge
- LLM verifier challenge or unclear decision
- no direct gene-linked evidence
- missing structured evidence fields such as perturbation method, phenotype
  term, or direct gene quote
- negative paper type
- functional label without perturbation evidence
- ambiguous gene symbol
- borderline evidence-support score

The Hugging Face website displays review status, support components, evidence
quote, verifier reasons, adjudicator reasons, and reviewer notes.
Expanded paper rows also show the structured evidence extractor output when
available.
The API also returns a short query-time `review_summary` string generated from
the strongest review signals, so lab users see a plain-English reason before
lower-level diagnostics.
The same query-time annotation now also exposes a stable `review_category` and
`recommended_action`. These fields are not human labels. They are maintenance
and triage helpers that separate algorithmic uncertainty from reviewer status.
Examples include `weak_evidence`, `classification_conflict`,
`paper_type_risk`, `gene_or_method_unclear`, `borderline_support`, and
`routine`.

## 13. Database Writes

Final outputs are written to SQLite.

- `papers`: one row per gene/PMID pair
- `genes`: per-gene summary and refresh metadata
- `request_queue`: requested genes and processing status
- `skipped_pmids`: papers skipped by filtering

Agent/verifier fields in `papers` include:

- `verification_status`
- `verification_reasons`
- `evidence_quality_score`
- `gene_match_quality`
- `adjudication_status`
- `adjudication_reasons`
- `agentic_verifier_decision`
- `agentic_verifier_reason`
- `agentic_verifier_quote`
- `agentic_verifier_needs_review`
- `structured_evidence_json`
- `review_recommendation`
- `review_reasons`
- `agent_trace`

`review_summary`, `review_category`, and `recommended_action` are generated by
`db.annotate_paper_row` at query time. They are not stored as separate SQLite
columns.

The DB is then uploaded/synced through Google Drive and read by the Hugging Face
website.

## 14. Reprocessing Existing Rows

There are two levels of applying algorithm updates to old database rows.

### Fast Recompute

```bash
python -u scripts/recompute_confidence.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --upload
```

This updates support score, verifier/adjudicator fields, paper type, and best
available evidence quote from stored snippets. It does not rerun PubMed,
PMC retrieval, BioMistral, or the optional LLM verifier.

### Full Reprocess

```bash
python -u scripts/reprocess_papers.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --gene ADAM10 \
  --max-papers 100 \
  --ignore-cache \
  --upload
```

This reruns search, ranking, evidence retrieval, classification, verification,
optional LLM verifier, and scoring. Use this for complaint genes, high-value
genes, or when search and evidence extraction changed substantially.

## 15. Recommended Reprocessing Policy

After an algorithm update, do not immediately rebuild the full database unless
the lab has enough compute and review time.

Recommended sequence:

1. Run `scripts/recompute_confidence.py --upload` for all rows.
2. Run `scripts/check_algorithm_fields.py` to confirm deterministic fields were
   backfilled and identify suspicious rows.
3. Run `scripts/plan_reprocess.py` to rank genes/PMIDs for selected reprocess.
4. Reprocess 3-5 known complaint genes or planner-selected PMIDs with
   `--ignore-cache`.
5. Review changed classifications and evidence quotes on the website.
6. If quality improves, reprocess high-priority genes in batches.
7. Only rebuild all genes after the lab accepts the new behavior on samples.

This avoids spending many GPU hours and possibly changing thousands of rows
before humans confirm the new logic is better.

Useful audit/planner commands:

```bash
python -u scripts/check_algorithm_fields.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --csv-out outputs/algorithm_audit_rows.csv \
  --gene-csv-out outputs/algorithm_audit_genes.csv

python -u scripts/plan_reprocess.py \
  --db-path gene_function_lab/gene_function_lab.db \
  --top-genes 10 \
  --top-pmids 25 \
  --csv-out outputs/reprocess_plan.csv
```

## Algorithm Change Log

### Before June 26, 2026

The project was mainly:

- PubMed gene/cancer search
- rule-based evidence detection
- one BioMistral classifier
- heuristic confidence score
- SQLite result storage
- Hugging Face review website

Main limitations:

- broad candidate search could include many weakly related papers
- paper type was not explicit
- confidence was coarse and often repeated values
- review routing was limited
- old rows could be hard to rebuild systematically

### June 26, 2026

Added stronger review and UI infrastructure:

- skeptical evidence verifier
- evidence-agent workflow
- adjudicator/review-router concept
- result sorting controls
- clearer human review support in the website

Impact:

- better review routing for weak or contradictory rows
- more transparent evidence diagnostics
- easier workflow for comparing genes and papers

### June 27, 2026

Improved search and reprocessing workflow:

- evidence-focused PubMed query before broad fallback
- candidate ranking by gene, cancer, perturbation, phenotype, and model signals
- evidence-focused snippet retrieval
- search relevance score
- evidence retrieval score
- force reprocess workflow for genes or selected PMIDs
- gold-label evaluation support

Impact:

- better use of limited Colab/GPU time
- safer way to rebuild old rows under new algorithm logic
- starting point for validation with human labels

### June 28, 2026

Improved evidence triage and interpretability:

- deterministic `paper_type.py`
- PubMed publication type storage
- `paper_type` stored in SQLite and shown in the website
- `best_evidence_quote`
- `gene_linked_evidence_sents`
- tighter in vitro/in vivo evidence counting based on direct gene linkage
- paper-type and gene-linked-evidence factors added to evidence-support scoring
- adjudication status/reasons stored as first-class DB fields
- website expanded rows show best quote, paper type, adjudicator reasons, and
  additional support components
- gold-label evaluator reports score-band accuracy and errors by paper type

Impact:

- easier identification of review/prognosis/expression-only false positives
- stronger explanation for why a paper was classified
- better human-review prioritization

### June 28, 2026 - Agentic Verifier Update

Added a practical gated LLM verifier rather than a broad multi-agent RAG system:

- optional BioMistral-based **LLM Skeptical Verifier Agent**
- verifier runs only on risky rows by default
- configurable flags: `USE_AGENTIC_VERIFIER`, `AGENTIC_MODE`,
  `MAX_VERIFIER_CALLS`, `VERIFIER_ONLY_BORDERLINE`
- verifier outputs saved as structured fields in SQLite
- verifier result added to evidence-support scoring
- LLM verifier challenge/unclear decisions routed to higher human review
- website expanded rows show verifier decision, quote, and reason

Impact:

- stronger false-positive control for papers that look functional but may be
  expression-only, prognosis-only, review-like, or about another gene
- better transparency for lab reviewers
- bounded runtime cost on Colab because only selected rows receive the second
  LLM call

### June 28, 2026 - Structured Evidence Extractor Update

Added a deterministic structured evidence extractor to make the agent workflow
more reviewable without adding another LLM call:

- new **Structured Evidence Extractor Agent** in `evidence_agents.py`
- extracts evidence type, perturbation methods, phenotype terms, cancer context,
  best quote, direct-gene evidence flag, and missing evidence components
- stores output in `papers.structured_evidence_json`
- `scripts/recompute_confidence.py` can backfill structured evidence from stored
  snippets for old rows
- review routing now adds reasons when functional labels are missing structured
  evidence components
- website expanded rows show the structured evidence summary
- CSV exports include the structured evidence JSON for downstream audit

Impact:

- easier reviewer inspection of why a paper was labeled functional
- clearer difference between "has a score" and "has direct perturbation +
  phenotype evidence"
- no extra model calls, so Colab runtime stays essentially unchanged

### June 28, 2026 - Backfill Audit And Review Summary Update

Added maintainability and review-clarity improvements without changing
classification thresholds:

- new read-only `scripts/check_algorithm_fields.py`
- reports missing `structured_evidence_json`, malformed structured evidence,
  missing review reasons, unknown paper type, functional rows without best
  quote, functional rows with weak gene match, and risky rows without LLM
  verifier outputs
- website/API rows now include a short `review_summary` generated from review
  signals
- expanded website rows show the review summary before lower-level diagnostics

Impact:

- easier to verify whether fast recompute/backfill populated expected fields
- clearer handoff for lab members reviewing papers
- no additional PubMed or LLM runtime
- makes explicit that `agentic_verifier_*` fields require selected
  reprocessing; fast recompute cannot invent real LLM verifier decisions

### June 28, 2026 - Review Workflow And Reprocess Planning Milestone

Added a stronger maintenance layer for deciding what to trust, what to review,
and what to reprocess:

- query-time `review_category` and `recommended_action` in `db.py`
- website expanded rows show review priority, category, and recommended action
- expanded `scripts/check_algorithm_fields.py` into a broader read-only audit
  for missing structured evidence, missing review reasons, missing paper type,
  high-confidence weak-evidence rows, functional rows without perturbation or
  phenotype evidence, suspicious paper types, repeated confidence buckets, and
  genes with many weak/unclear rows
- added optional CSV exports for suspicious rows and per-gene audit summaries
- added `scripts/plan_reprocess.py` to recommend `recompute_only`,
  `selected_pmid_reprocess`, `selected_gene_reprocess`,
  `optional_selected_reprocess`, or `no_action`
- no schema change and no silent classifier-threshold change

Impact:

- lab maintainers can decide between recompute and selected full reprocessing
  without guessing
- high-risk rows are easier to find before spending GPU time
- expanded website rows separate algorithmic review priority from human review
  status
- full database reprocessing remains discouraged until audit/planner output and
  small-gene tests justify it

## Current Limitations

- The score is heuristic and not calibrated against a large gold-label set.
- Full-text retrieval depends on PMC availability.
- Gene aliases are manual and conservative.
- BioMistral is still a single local LLM classifier.
- The optional LLM verifier uses the same BioMistral model, so it is not an
  independent model-family check.
- The system can miss papers whose abstracts omit direct perturbation or
  phenotype language.
- Major algorithm changes should be validated on human-labeled examples before
  full database rebuilds.
