"""Evidence-support scoring for PubMed functional-study classification.

The score is a triage metric, not a calibrated probability. It estimates how
well the extracted evidence supports the stored classification decision using
interpretable signals:

- direct gene perturbation evidence
- in vitro / in vivo phenotype evidence
- evidence depth
- rule/LLM agreement
- penalties for expression-only, correlation-only, review-only, or missing
  evidence patterns

The same functions are used by the live pipeline and by maintenance scripts
that recompute scores for already-processed database rows.
"""

from __future__ import annotations

import math
import re
from typing import Any


EXPRESSION_WORDS = (
    "expression", "mrna level", "protein level", "upregulated",
    "downregulated", "overexpressed", "immunohistochemistry",
)

CORRELATION_WORDS = (
    "associated with", "correlated with", "prognostic", "biomarker",
    "signature", "survival analysis", "kaplan-meier", "hazard ratio",
)

REVIEW_WORDS = (
    "review", "meta-analysis", "systematic review", "literature review",
    "commentary", "editorial",
)

METHOD_KEYS = ("knockout", "knockdown", "shrna", "sirna", "crispr")


def _clamp_score(value: float, lo: float = 0.02, hi: float = 0.95) -> float:
    return round(max(lo, min(hi, value)), 3)


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _split_evidence(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def _has_any(mapping: dict, keys: tuple[str, ...]) -> bool:
    return any(_as_bool(mapping.get(k)) for k in keys)


def _count_methods(mapping: dict) -> int:
    return sum(_as_bool(mapping.get(k)) for k in METHOD_KEYS)


def _bounded_count_score(n: int, scale: float = 4.0) -> float:
    """Smooth 0-1 count score that avoids coarse stepwise plateaus."""
    if n <= 0:
        return 0.0
    return min(1.0, 1.0 - math.exp(-n / scale))


def _evidence_depth_score(n_sents: int, n_categories: int) -> float:
    """Return 0-1 evidence-depth score from amount and category diversity."""
    sentence_score = _bounded_count_score(n_sents, scale=4.5)
    diversity_score = min(1.0, n_categories / 4.0)
    return min(1.0, 0.78 * sentence_score + 0.22 * diversity_score)


def _text_flags(title: str, abstract: str, ev: dict) -> dict[str, bool]:
    text = f"{title or ''}\n{abstract or ''}".lower()
    evidence_text = "\n".join(
        s for key in (
            "evidence_perturbation",
            "evidence_in_vitro",
            "evidence_in_vivo",
            "evidence_crispr_screen",
        )
        for s in _split_evidence(ev.get(key))
    ).lower()
    combined = f"{text}\n{evidence_text}"

    return {
        "expression_only": (
            any(w in combined for w in EXPRESSION_WORDS)
            and not _split_evidence(ev.get("evidence_perturbation"))
        ),
        "correlation_only": any(w in combined for w in CORRELATION_WORDS),
        "review_paper": any(w in (title or "").lower() for w in REVIEW_WORDS),
    }


def _agreement_scores(primary: dict, llm_result: dict | None, rules_result: dict) -> tuple[float, float, bool]:
    """Return functional agreement score, nonfunctional agreement score, disagreement."""
    if llm_result is None:
        return 0.58, 0.62, False

    llm_func = _as_bool(llm_result.get("functional_study"))
    rules_func = _as_bool(rules_result.get("functional_study"))
    primary_func = _as_bool(primary.get("functional_study"))
    disagreement = llm_func != rules_func

    if not disagreement:
        if primary_func:
            return 1.0, 0.25, False
        return 0.25, 1.0, False

    # A disagreement should not erase the score, but it should clearly mark the
    # result as review-worthy.
    if primary_func:
        return 0.48, 0.45, True
    return 0.40, 0.50, True


def _support_components(gene, llm_result, rules_result, ev, title="", abstract="") -> dict:
    """Build interpretable scoring components used by compute_confidence.

    Each component is normalized to 0-1. The final score remains heuristic, but
    the components make the meaning auditable and reduce repeated default values.
    """
    primary = llm_result if llm_result is not None else rules_result
    primary = primary or {}
    rules_result = rules_result or {}
    ev = ev or {}

    evidence_pert = _split_evidence(ev.get("evidence_perturbation"))
    evidence_vitro = _split_evidence(ev.get("evidence_in_vitro"))
    evidence_vivo = _split_evidence(ev.get("evidence_in_vivo"))
    evidence_screen = _split_evidence(ev.get("evidence_crispr_screen"))
    evidence_texts = evidence_pert + evidence_vitro + evidence_vivo + evidence_screen

    method_count = _count_methods(primary)
    pert_count = len(evidence_pert)
    screen_count = len(evidence_screen)
    if evidence_pert and method_count:
        perturbation_score = 0.62 + 0.23 * _bounded_count_score(pert_count, 3.0) + 0.15 * min(1.0, method_count / 3.0)
    elif evidence_pert or method_count:
        perturbation_score = 0.43 + 0.22 * _bounded_count_score(pert_count + method_count, 2.5)
    elif _as_bool(primary.get("crispr_screen")) or evidence_screen:
        perturbation_score = 0.28 + 0.18 * _bounded_count_score(screen_count, 2.0)
    else:
        perturbation_score = 0.0
    perturbation_score = min(1.0, perturbation_score)

    in_vitro = _as_bool(primary.get("in_vitro")) or bool(evidence_vitro)
    in_vivo = _as_bool(primary.get("in_vivo")) or bool(evidence_vivo)
    vitro_strength = _bounded_count_score(len(evidence_vitro), 3.0)
    vivo_strength = _bounded_count_score(len(evidence_vivo), 2.5)
    if in_vitro and in_vivo:
        phenotype_score = 0.72 + 0.12 * vitro_strength + 0.16 * vivo_strength
    elif in_vivo:
        phenotype_score = 0.62 + 0.24 * vivo_strength
    elif in_vitro:
        phenotype_score = 0.48 + 0.22 * vitro_strength
    elif _as_bool(primary.get("crispr_screen")) or evidence_screen:
        phenotype_score = 0.28 + 0.12 * _bounded_count_score(screen_count, 2.0)
    else:
        phenotype_score = 0.0
    phenotype_score = min(1.0, phenotype_score)

    n_evidence_sents = int(ev.get("total_evidence_sents", 0) or 0)
    evidence_categories = sum(bool(x) for x in [evidence_pert, evidence_vitro, evidence_vivo, evidence_screen])
    depth_score = _evidence_depth_score(n_evidence_sents, evidence_categories)

    if _as_bool(primary.get("knockout")) or _as_bool(primary.get("crispr")):
        method_strength = 0.95
    elif _as_bool(primary.get("knockdown")) or _as_bool(primary.get("shrna")) or _as_bool(primary.get("sirna")):
        method_strength = 0.72
    elif _as_bool(primary.get("crispr_screen")):
        method_strength = 0.48
    else:
        method_strength = 0.0
    method_strength = min(1.0, method_strength + 0.05 * max(0, method_count - 1))

    agreement_func, agreement_nonfunc, disagreement = _agreement_scores(
        primary, llm_result, rules_result
    )
    # BioMistral does not return a probability. This is source reliability, not
    # model confidence. Rules-only rows remain usable but should rank slightly
    # below similarly strong LLM-checked rows.
    source_reliability = 1.0 if llm_result is not None else 0.76
    if disagreement:
        source_reliability = min(source_reliability, 0.58)

    gene_pattern = re.compile(rf"\b{re.escape(str(gene or '').lower())}\b") if gene else None
    gene_mentions = (
        sum(len(gene_pattern.findall(s.lower())) for s in evidence_texts)
        if gene_pattern else 0
    )
    mention_score = _bounded_count_score(gene_mentions, 3.0)
    if evidence_pert:
        gene_specificity = 0.68 + 0.24 * mention_score + 0.08 * min(1.0, method_count / 2.0)
    elif method_count:
        gene_specificity = 0.55 + 0.25 * mention_score
    elif evidence_vitro or evidence_vivo:
        gene_specificity = 0.25 + 0.25 * mention_score
    else:
        gene_specificity = 0.0
    gene_specificity = min(1.0, gene_specificity)

    evidence_chars = sum(len(s) for s in evidence_texts)
    has_fulltext_context = bool(ev.get("pmcid")) or bool(ev.get("has_fulltext_context"))
    context_strength = (
        0.62 if has_fulltext_context else 0.40
    ) + 0.26 * _bounded_count_score(evidence_chars, 900.0) + 0.12 * min(1.0, evidence_categories / 3.0)
    context_strength = min(1.0, context_strength)
    text_flags = _text_flags(title, abstract, ev)

    return {
        "perturbation_score": perturbation_score,
        "phenotype_score": phenotype_score,
        "evidence_depth_score": depth_score,
        "method_strength": method_strength,
        "agreement_functional": agreement_func,
        "agreement_not_functional": agreement_nonfunc,
        "source_reliability": source_reliability,
        "gene_specificity": gene_specificity,
        "context_strength": context_strength,
        "negative_text_score": min(
            1.0,
            (0.35 if text_flags["expression_only"] else 0.0)
            + (0.35 if text_flags["correlation_only"] else 0.0)
            + (0.20 if text_flags["review_paper"] else 0.0)
            + (0.20 if n_evidence_sents == 0 else 0.0),
        ),
        "in_vitro": in_vitro,
        "in_vivo": in_vivo,
        "disagreement": disagreement,
        "rules_only": llm_result is None,
        "n_evidence_sents": n_evidence_sents,
        "evidence_categories": evidence_categories,
        "gene_mentions": gene_mentions,
        "evidence_chars": evidence_chars,
        "has_fulltext_context": has_fulltext_context,
        **text_flags,
    }


def compute_confidence(gene, llm_result, rules_result, ev, title="", abstract=""):
    """Compute functional and non-functional evidence-support scores.

    Returns the legacy tuple expected by the pipeline:
    (conf_functional, conf_not_functional, pos_signals, neg_signals)
    """
    components = _support_components(gene, llm_result, rules_result, ev, title, abstract)

    conf_functional = (
        0.04
        + 0.27 * components["perturbation_score"]
        + 0.22 * components["phenotype_score"]
        + 0.14 * components["evidence_depth_score"]
        + 0.12 * components["agreement_functional"]
        + 0.07 * components["method_strength"]
        + 0.07 * components["source_reliability"]
        + 0.06 * components["gene_specificity"]
        + 0.04 * components["context_strength"]
    )

    if components["expression_only"]:
        conf_functional -= 0.12
    if components["correlation_only"] and components["perturbation_score"] < 0.70:
        conf_functional -= 0.12
    if components["review_paper"]:
        conf_functional -= 0.10
    if components["n_evidence_sents"] == 0:
        conf_functional -= 0.18

    # Hard caps only protect against missing essential evidence. We avoid a
    # rules-only cap because that created repeated artificial values such as 0.78.
    if components["perturbation_score"] == 0:
        conf_functional = min(conf_functional, 0.50)
    elif components["perturbation_score"] < 0.60:
        conf_functional = min(conf_functional, 0.64)
    if components["phenotype_score"] == 0:
        conf_functional = min(conf_functional, 0.55)
    if components["disagreement"]:
        conf_functional = min(conf_functional, 0.72 + 0.05 * components["evidence_depth_score"])

    no_perturbation = 1.0 - components["perturbation_score"]
    no_phenotype = 1.0 - components["phenotype_score"]

    conf_not_functional = (
        0.08
        + 0.28 * no_perturbation
        + 0.22 * no_phenotype
        + 0.20 * components["agreement_not_functional"]
        + 0.16 * components["negative_text_score"]
        + 0.06 * components["source_reliability"]
        + 0.04 * (1.0 - components["evidence_depth_score"])
        + 0.02 * (1.0 - components["context_strength"])
    )
    if components["perturbation_score"] >= 0.70 and components["phenotype_score"] > 0:
        conf_not_functional -= 0.25
    if components["disagreement"]:
        conf_not_functional = min(conf_not_functional, 0.72 + 0.05 * components["negative_text_score"])

    pos_signals = {
        "direct_gene_perturbation_score": round(components["perturbation_score"], 2),
        "phenotype_model_score": round(components["phenotype_score"], 2),
        "evidence_depth_score": round(components["evidence_depth_score"], 2),
        "classifier_agreement_score": round(components["agreement_functional"], 2),
        "source_reliability_score": round(components["source_reliability"], 2),
        "context_strength_score": round(components["context_strength"], 2),
        "strong_method_score": round(components["method_strength"], 2),
        "in_vitro_phenotype": components["in_vitro"],
        "in_vivo_phenotype": components["in_vivo"],
        "both_vitro_and_vivo": components["in_vitro"] and components["in_vivo"],
    }
    neg_signals = {
        "expression_only": components["expression_only"],
        "correlation_only": components["correlation_only"],
        "review_paper": components["review_paper"],
        "no_evidence_sents": components["n_evidence_sents"] == 0,
        "llm_rules_disagree": components["disagreement"],
        "rules_only": components["rules_only"],
    }

    return _clamp_score(conf_functional), _clamp_score(conf_not_functional), pos_signals, neg_signals


def compute_confidence_from_db_row(row: dict) -> tuple[float, float, float]:
    """Recompute scores from an already-stored papers row.

    This is approximate because old rows do not preserve the raw LLM JSON, but it
    uses the stored final decision, rule decision, extracted evidence, and model
    flags. It avoids rerunning BioMistral just to refresh confidence scores.
    """
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
        "pmcid": row.get("pmcid"),
    }
    conf_func, conf_nonfunc, _, _ = compute_confidence(
        row.get("gene", ""), llm_result, rules_result, ev,
        title=row.get("title", ""), abstract="",
    )
    confidence = conf_func if _as_bool(row.get("functional_study")) else conf_nonfunc
    return confidence, conf_func, conf_nonfunc


def explain_confidence_from_db_row(row: dict) -> dict:
    """Return concise support-score components for the website expanded row."""
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
        "pmcid": row.get("pmcid"),
    }
    c = _support_components(row.get("gene", ""), llm_result, rules_result, ev, row.get("title", ""), "")

    reasons = []
    if c["perturbation_score"] >= 0.75:
        reasons.append("direct perturbation evidence")
    elif c["perturbation_score"] > 0:
        reasons.append("partial perturbation signal")
    else:
        reasons.append("no direct perturbation evidence")

    if c["in_vitro"] and c["in_vivo"]:
        reasons.append("in vitro and in vivo phenotype evidence")
    elif c["in_vivo"]:
        reasons.append("in vivo phenotype evidence")
    elif c["in_vitro"]:
        reasons.append("in vitro phenotype evidence")
    else:
        reasons.append("no functional phenotype evidence")

    if c["rules_only"]:
        reasons.append("rules-only score; BioMistral confidence unavailable")
    elif c["disagreement"]:
        reasons.append("rule/LLM disagreement")
    else:
        reasons.append("rule/LLM agreement")

    if c["review_paper"] or c["expression_only"] or c["correlation_only"]:
        weak = []
        if c["review_paper"]:
            weak.append("review-like title")
        if c["expression_only"]:
            weak.append("expression-only signal")
        if c["correlation_only"]:
            weak.append("correlation/biomarker language")
        reasons.append("penalty: " + ", ".join(weak))

    return {
        "components": {
            "perturbation": round(c["perturbation_score"], 2),
            "phenotype": round(c["phenotype_score"], 2),
            "evidence_depth": round(c["evidence_depth_score"], 2),
            "agreement": round(c["agreement_functional"], 2),
            "source_reliability": round(c["source_reliability"], 2),
            "method_strength": round(c["method_strength"], 2),
            "context": round(c["context_strength"], 2),
        },
        "reasons": reasons,
    }
