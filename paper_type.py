"""Deterministic paper-type classification for review routing.

This module is intentionally lightweight. It does not claim to infer a formal
publication type; it produces a practical label that helps the pipeline and UI
separate likely functional experiments from reviews, expression/prognosis-only
papers, methods papers, and broad screens.
"""

from __future__ import annotations

import re
from typing import Any


REVIEW_TERMS = (
    "review", "systematic review", "meta-analysis", "editorial", "comment",
    "letter", "guideline",
)

METHODS_TERMS = (
    "database", "dataset", "atlas", "protocol", "method", "pipeline",
    "software", "benchmark", "resource", "web server",
)

CLINICAL_ASSOCIATION_TERMS = (
    "prognostic", "prognosis", "survival analysis", "overall survival",
    "hazard ratio", "kaplan-meier", "nomogram", "cohort",
)

EXPRESSION_ASSOCIATION_TERMS = (
    "expression", "expressed", "upregulated", "downregulated",
    "biomarker", "correlated with", "associated with",
    "immunohistochemistry", "methylation",
)

PERTURBATION_TERMS = (
    "knockdown", "knock-down", "knockout", "knock-out", "silencing",
    "silenced", "depletion", "deleted", "deletion", "crispr", "cas9",
    "sirna", "shrna", "rnai", "sgRNA".lower(), "loss-of-function",
)

PHENOTYPE_TERMS = (
    "proliferation", "viability", "apoptosis", "migration", "invasion",
    "tumor growth", "tumour growth", "tumor volume", "tumour volume",
    "colony formation", "metastasis", "xenograft", "organoid",
)

SCREEN_TERMS = (
    "screen", "screening", "genome-wide", "pooled crispr", "library",
    "dropout",
)


def _split(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_paper_type(
    title: str = "",
    abstract: str = "",
    publication_types: str | list[str] = "",
    evidence: dict | None = None,
) -> str:
    """Return a practical paper-type label used for triage.

    Labels are heuristic and meant to support human review:
    - functional_experiment
    - functional_screen
    - review
    - clinical_prognostic
    - expression_association
    - methods_or_dataset
    - unknown
    """
    evidence = evidence or {}
    title_l = str(title or "").lower()
    abstract_l = str(abstract or "").lower()
    pub_types_l = " ".join(_split(publication_types)).lower()
    evidence_l = " ".join(
        s.lower()
        for key in (
            "evidence_perturbation",
            "evidence_in_vitro",
            "evidence_in_vivo",
            "evidence_crispr_screen",
        )
        for s in _split(evidence.get(key))
    )
    text_l = f"{title_l}\n{abstract_l}\n{evidence_l}"

    if _contains(pub_types_l, REVIEW_TERMS) or _contains(title_l, REVIEW_TERMS):
        return "review"
    if _contains(title_l, METHODS_TERMS) and not _contains(evidence_l, PERTURBATION_TERMS):
        return "methods_or_dataset"

    has_perturbation = _contains(evidence_l or text_l, PERTURBATION_TERMS)
    has_phenotype = _contains(evidence_l or text_l, PHENOTYPE_TERMS)
    has_screen = _contains(evidence_l or title_l, SCREEN_TERMS)

    if has_perturbation and has_phenotype and has_screen:
        return "functional_screen"
    if has_perturbation and has_phenotype:
        return "functional_experiment"

    if _contains(text_l, CLINICAL_ASSOCIATION_TERMS):
        return "clinical_prognostic"
    if _contains(text_l, EXPRESSION_ASSOCIATION_TERMS):
        return "expression_association"
    if _contains(text_l, METHODS_TERMS):
        return "methods_or_dataset"
    return "unknown"


def paper_type_is_negative_evidence(paper_type: str) -> bool:
    return str(paper_type or "") in {
        "review",
        "clinical_prognostic",
        "expression_association",
        "methods_or_dataset",
    }
