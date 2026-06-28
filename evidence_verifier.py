"""Skeptical evidence verification for paper-level gene classifications.

This module is intentionally deterministic and auditable. BioMistral decides
whether a paper looks like a functional study; the verifier checks whether the
stored evidence snippets actually support that decision. The output should be
used for review routing and evidence-support scoring, not as a calibrated
statistical probability.
"""

from __future__ import annotations

import math
import re
from typing import Any

from paper_type import paper_type_is_negative_evidence


AMBIGUOUS_GENE_SYMBOLS = {
    # Very short or common-word symbols often retrieve papers where the symbol
    # appears as an abbreviation with a different meaning.
    "AI", "AR", "AT", "CA", "CAT", "CP", "GC", "KIT", "MET", "YES",
    "APP", "JUN", "FOS", "SRC", "MIF",
}

EXPRESSION_OR_ASSOCIATION_TERMS = (
    "expression", "expressed", "upregulated", "downregulated",
    "associated with", "correlated with", "biomarker", "prognostic",
    "survival analysis", "hazard ratio", "signature",
)

REVIEW_TERMS = (
    "review", "meta-analysis", "systematic review", "literature review",
    "commentary", "editorial",
)

PHENOTYPE_TERMS = (
    "growth", "proliferation", "viability", "apoptosis", "necrosis",
    "cell death", "organoid", "tumor size", "tumour size", "tumor volume",
    "tumour volume", "tumor growth", "xenograft", "survival",
)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _split_evidence(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def is_ambiguous_gene_symbol(gene: str) -> bool:
    g = str(gene or "").strip().upper()
    return len(g) <= 2 or g in AMBIGUOUS_GENE_SYMBOLS


def _gene_regex(gene: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(str(gene or '').strip())}\b", re.I)


def _direct_gene_perturbation_patterns(gene: str) -> list[re.Pattern]:
    g = re.escape(str(gene or "").strip())
    return [
        re.compile(rf"\b(?:knock(?:ed)?[\s-]?out|knock(?:ed)?[\s-]?down|silenc(?:ed|ing)|deplet(?:ed|ion)|delet(?:ed|ion)|ablat(?:ed|ion))\s+(?:of\s+)?{g}\b", re.I),
        re.compile(rf"\b{g}[\s-]?(?:knock(?:ed)?[\s-]?out|knock(?:ed)?[\s-]?down|silenc(?:ed|ing)|deplet(?:ed|ion)|delet(?:ed|ion)|deficien(?:t|cy)|null)\b", re.I),
        re.compile(rf"\b(?:siRNA|shRNA|sgRNA|RNAi|CRISPR|Cas9|guide RNA)[\s-]*(?:targeting\s+|against\s+)?{g}\b", re.I),
        re.compile(rf"\b(?:sh|si|sg)[\s_-]*{g}\b", re.I),
        re.compile(rf"\b{g}[\s_-]*(?:siRNA|shRNA|sgRNA)\b", re.I),
    ]


def _bounded_count_score(n: int, scale: float = 4.0) -> float:
    if n <= 0:
        return 0.0
    return min(1.0, 1.0 - math.exp(-n / scale))


def _evidence_text(ev: dict) -> str:
    parts = []
    for key in (
        "evidence_perturbation",
        "evidence_in_vitro",
        "evidence_in_vivo",
        "evidence_crispr_screen",
    ):
        parts.extend(_split_evidence(ev.get(key)))
    return "\n".join(parts)


def verify_evidence(
    gene: str,
    title: str,
    abstract: str,
    ev: dict,
    primary: dict,
    rules_result: dict | None = None,
    llm_result: dict | None = None,
) -> dict:
    """Return verification fields for one paper decision.

    Status values:
    - supported: evidence snippets are consistent with the stored decision
    - needs_review: plausible but contains ambiguity, disagreement, or weak cues
    - weak_support: functional label has partial evidence but misses a core cue
    - not_supported: functional label lacks direct perturbation or phenotype
    """
    ev = ev or {}
    primary = primary or {}
    rules_result = rules_result or {}

    evidence_text = _evidence_text(ev)
    combined_text = "\n".join([title or "", abstract or "", evidence_text])
    paper_type = str(ev.get("paper_type") or "").strip().lower()
    publication_types = str(ev.get("publication_types") or "").lower()
    gene_re = _gene_regex(gene)
    gene_mentions_evidence = len(gene_re.findall(evidence_text or ""))
    gene_mentions_total = len(gene_re.findall(combined_text or ""))

    evidence_pert = _split_evidence(ev.get("evidence_perturbation"))
    evidence_vitro = _split_evidence(ev.get("evidence_in_vitro"))
    evidence_vivo = _split_evidence(ev.get("evidence_in_vivo"))
    evidence_screen = _split_evidence(ev.get("evidence_crispr_screen"))

    direct_pattern_hit = any(p.search(evidence_text) for p in _direct_gene_perturbation_patterns(gene))
    method_flag = any(_as_bool(primary.get(k)) for k in ("knockout", "knockdown", "shrna", "sirna", "crispr"))
    direct_gene_perturbation = bool(direct_pattern_hit or (evidence_pert and method_flag and gene_mentions_evidence))

    phenotype_snippets = evidence_vitro + evidence_vivo
    phenotype_hit = bool(
        _as_bool(primary.get("in_vitro"))
        or _as_bool(primary.get("in_vivo"))
        or phenotype_snippets
        or any(term in evidence_text.lower() for term in PHENOTYPE_TERMS)
    )
    has_both_models = bool((_as_bool(primary.get("in_vitro")) or evidence_vitro) and (_as_bool(primary.get("in_vivo")) or evidence_vivo))

    llm_rules_disagree = (
        llm_result is not None
        and _as_bool(llm_result.get("functional_study")) != _as_bool(rules_result.get("functional_study"))
    )
    functional = _as_bool(primary.get("functional_study"))
    ambiguous_gene = is_ambiguous_gene_symbol(gene)
    review_like = (
        paper_type == "review"
        or any(term in (title or "").lower() for term in REVIEW_TERMS)
        or any(term in publication_types for term in REVIEW_TERMS)
    )
    association_language = paper_type in {"expression_association", "clinical_prognostic"} or any(term in combined_text.lower() for term in EXPRESSION_OR_ASSOCIATION_TERMS)
    negative_paper_type = paper_type_is_negative_evidence(paper_type)
    evidence_count = int(ev.get("total_evidence_sents", 0) or 0)

    reasons: list[str] = []
    if direct_gene_perturbation:
        reasons.append("direct gene perturbation supported")
    elif method_flag or evidence_pert:
        reasons.append("perturbation cue present but gene linkage is weak")
    else:
        reasons.append("no direct perturbation evidence")

    if has_both_models:
        reasons.append("in vitro and in vivo phenotype evidence")
    elif _as_bool(primary.get("in_vivo")) or evidence_vivo:
        reasons.append("in vivo phenotype evidence")
    elif _as_bool(primary.get("in_vitro")) or evidence_vitro:
        reasons.append("in vitro phenotype evidence")
    else:
        reasons.append("no functional phenotype evidence")

    if llm_rules_disagree:
        reasons.append("LLM/rules disagreement")
    if ambiguous_gene and gene_mentions_evidence < 2:
        reasons.append("ambiguous gene symbol with sparse evidence mentions")
    if review_like:
        reasons.append("review-like title")
    if association_language and not direct_gene_perturbation:
        reasons.append("association/expression language without perturbation")
    if negative_paper_type and paper_type:
        reasons.append(f"paper type looks like {paper_type.replace('_', ' ')}")

    if functional:
        if not direct_gene_perturbation or not phenotype_hit:
            status = "not_supported" if (not direct_gene_perturbation and not phenotype_hit) else "weak_support"
        elif llm_rules_disagree or review_like or negative_paper_type or (ambiguous_gene and gene_mentions_evidence < 2):
            status = "needs_review"
        else:
            status = "supported"
    else:
        if direct_gene_perturbation and phenotype_hit:
            status = "needs_review"
            reasons.append("non-functional label conflicts with extracted perturbation and phenotype evidence")
        elif llm_rules_disagree:
            status = "needs_review"
        else:
            status = "supported"

    gene_specificity = min(1.0, 0.35 * _bounded_count_score(gene_mentions_evidence, 2.0) + 0.65 * _bounded_count_score(gene_mentions_total, 5.0))
    model_score = 0.35 if has_both_models else 0.26 if (_as_bool(primary.get("in_vivo")) or evidence_vivo) else 0.18 if (_as_bool(primary.get("in_vitro")) or evidence_vitro) else 0.0
    quality = (
        0.16
        + (0.24 if direct_gene_perturbation else 0.08 if method_flag or evidence_pert else 0.0)
        + model_score
        + 0.13 * gene_specificity
        + 0.09 * _bounded_count_score(evidence_count, 4.0)
        + (0.07 if llm_result is not None else 0.02)
        - (0.12 if llm_rules_disagree else 0.0)
        - (0.10 if review_like else 0.0)
        - (0.08 if negative_paper_type else 0.0)
        - (0.10 if ambiguous_gene and gene_mentions_evidence < 2 else 0.0)
    )

    gene_match_quality = "strong" if gene_mentions_evidence >= 2 or direct_pattern_hit else "moderate" if gene_mentions_total >= 2 else "weak"

    return {
        "verification_status": status,
        "verification_reasons": "; ".join(dict.fromkeys(reasons)),
        "evidence_quality_score": round(max(0.02, min(0.95, quality)), 3),
        "gene_match_quality": gene_match_quality,
    }


def verify_db_row(row: dict) -> dict:
    """Verify an existing SQLite row using stored evidence fields only."""
    primary = {
        "functional_study": row.get("functional_study"),
        "in_vitro": row.get("in_vitro"),
        "in_vivo": row.get("in_vivo"),
        "knockout": row.get("knockout"),
        "knockdown": row.get("knockdown"),
        "shrna": row.get("shrna"),
        "sirna": row.get("sirna"),
        "crispr": row.get("crispr"),
        "crispr_screen": row.get("crispr_screen"),
    }
    rules_result = {
        **primary,
        "functional_study": row.get("rules_functional")
        if row.get("rules_functional") is not None
        else row.get("functional_study"),
    }
    llm_result = primary if _as_bool(row.get("classified_by_llm")) else None
    ev = {
        "evidence_perturbation": row.get("evidence_perturbation"),
        "evidence_in_vitro": row.get("evidence_in_vitro"),
        "evidence_in_vivo": row.get("evidence_in_vivo"),
        "evidence_crispr_screen": row.get("evidence_crispr_screen"),
        "total_evidence_sents": row.get("total_evidence_sents"),
        "paper_type": row.get("paper_type"),
        "publication_types": row.get("publication_types"),
    }
    return verify_evidence(
        row.get("gene", ""),
        row.get("title", ""),
        "",
        ev,
        primary,
        rules_result,
        llm_result,
    )
