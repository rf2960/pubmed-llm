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


def _evidence_depth_score(n_sents: int, n_categories: int) -> float:
    """Return 0-1 depth score from amount and diversity of extracted evidence."""
    if n_sents <= 0:
        sentence_score = 0.0
    elif n_sents == 1:
        sentence_score = 0.25
    elif n_sents == 2:
        sentence_score = 0.45
    elif n_sents <= 4:
        sentence_score = 0.65
    elif n_sents <= 7:
        sentence_score = 0.85
    else:
        sentence_score = 1.0

    category_bonus = min(0.20, max(0, n_categories - 1) * 0.07)
    return min(1.0, sentence_score + category_bonus)


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


def compute_confidence(gene, llm_result, rules_result, ev, title="", abstract=""):
    """Compute functional and non-functional evidence-support scores.

    Returns the legacy tuple expected by the pipeline:
    (conf_functional, conf_not_functional, pos_signals, neg_signals)
    """
    primary = llm_result if llm_result is not None else rules_result
    primary = primary or {}
    rules_result = rules_result or {}
    ev = ev or {}

    evidence_pert = _split_evidence(ev.get("evidence_perturbation"))
    evidence_vitro = _split_evidence(ev.get("evidence_in_vitro"))
    evidence_vivo = _split_evidence(ev.get("evidence_in_vivo"))
    evidence_screen = _split_evidence(ev.get("evidence_crispr_screen"))

    method_present = _has_any(primary, METHOD_KEYS)
    perturbation_score = 0.0
    if evidence_pert and method_present:
        perturbation_score = 1.0
    elif evidence_pert or method_present:
        perturbation_score = 0.75
    elif _as_bool(primary.get("crispr_screen")) or evidence_screen:
        perturbation_score = 0.45

    in_vitro = _as_bool(primary.get("in_vitro")) or bool(evidence_vitro)
    in_vivo = _as_bool(primary.get("in_vivo")) or bool(evidence_vivo)
    if in_vitro and in_vivo:
        phenotype_score = 1.0
    elif in_vivo:
        phenotype_score = 0.85
    elif in_vitro:
        phenotype_score = 0.65
    elif _as_bool(primary.get("crispr_screen")) or evidence_screen:
        phenotype_score = 0.40
    else:
        phenotype_score = 0.0

    n_evidence_sents = int(ev.get("total_evidence_sents", 0) or 0)
    evidence_categories = sum(bool(x) for x in [evidence_pert, evidence_vitro, evidence_vivo, evidence_screen])
    depth_score = _evidence_depth_score(n_evidence_sents, evidence_categories)

    strong_method_score = 0.0
    if _as_bool(primary.get("knockout")) or _as_bool(primary.get("crispr")):
        strong_method_score = 1.0
    elif _as_bool(primary.get("knockdown")) or _as_bool(primary.get("shrna")) or _as_bool(primary.get("sirna")):
        strong_method_score = 0.70
    elif _as_bool(primary.get("crispr_screen")):
        strong_method_score = 0.45

    agreement_func, agreement_nonfunc, disagreement = _agreement_scores(
        primary, llm_result, rules_result
    )
    text_flags = _text_flags(title, abstract, ev)

    conf_functional = (
        0.08
        + 0.30 * perturbation_score
        + 0.25 * phenotype_score
        + 0.16 * depth_score
        + 0.16 * agreement_func
        + 0.05 * strong_method_score
    )

    if text_flags["expression_only"]:
        conf_functional -= 0.12
    if text_flags["correlation_only"] and perturbation_score < 0.75:
        conf_functional -= 0.12
    if text_flags["review_paper"]:
        conf_functional -= 0.10
    if n_evidence_sents == 0:
        conf_functional -= 0.18

    # Hard caps make missing core evidence visible instead of allowing generic
    # agreement/depth signals to create artificial confidence.
    if perturbation_score == 0:
        conf_functional = min(conf_functional, 0.50)
    elif perturbation_score < 0.75:
        conf_functional = min(conf_functional, 0.64)
    if phenotype_score == 0:
        conf_functional = min(conf_functional, 0.55)
    if disagreement:
        conf_functional = min(conf_functional, 0.68)
    if llm_result is None:
        conf_functional = min(conf_functional, 0.78)

    no_perturbation = 1.0 - perturbation_score
    no_phenotype = 1.0 - phenotype_score
    negative_text_score = 0.0
    if text_flags["expression_only"]:
        negative_text_score += 0.35
    if text_flags["correlation_only"]:
        negative_text_score += 0.35
    if text_flags["review_paper"]:
        negative_text_score += 0.20
    if n_evidence_sents == 0:
        negative_text_score += 0.20
    negative_text_score = min(1.0, negative_text_score)

    conf_not_functional = (
        0.12
        + 0.28 * no_perturbation
        + 0.22 * no_phenotype
        + 0.22 * agreement_nonfunc
        + 0.16 * negative_text_score
    )
    if perturbation_score >= 0.75 and phenotype_score > 0:
        conf_not_functional -= 0.25
    if disagreement:
        conf_not_functional = min(conf_not_functional, 0.68)
    if llm_result is None:
        conf_not_functional = min(conf_not_functional, 0.78)

    pos_signals = {
        "direct_gene_perturbation_score": round(perturbation_score, 2),
        "phenotype_model_score": round(phenotype_score, 2),
        "evidence_depth_score": round(depth_score, 2),
        "classifier_agreement_score": round(agreement_func, 2),
        "strong_method_score": round(strong_method_score, 2),
        "in_vitro_phenotype": in_vitro,
        "in_vivo_phenotype": in_vivo,
        "both_vitro_and_vivo": in_vitro and in_vivo,
        "crispr_screen": _as_bool(primary.get("crispr_screen")) or bool(evidence_screen),
    }
    neg_signals = {
        **text_flags,
        "no_evidence_sents": n_evidence_sents == 0,
        "llm_rules_disagree": disagreement,
        "rules_only": llm_result is None,
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
    }
    conf_func, conf_nonfunc, _, _ = compute_confidence(
        row.get("gene", ""), llm_result, rules_result, ev,
        title=row.get("title", ""), abstract="",
    )
    confidence = conf_func if _as_bool(row.get("functional_study")) else conf_nonfunc
    return confidence, conf_func, conf_nonfunc
