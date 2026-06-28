"""Small, auditable agents for the paper classification workflow.

These are not autonomous chat agents. They are role-specific workflow agents
that produce structured, inspectable outputs around the existing rules +
BioMistral classifier:

1. Evidence Finder Agent: summarizes extracted evidence coverage.
2. Classifier Consensus Agent: records rules/LLM agreement.
3. Skeptical Verifier Agent: tries to disprove the assigned label.
4. Adjudicator Agent: challenges high-risk or internally inconsistent labels.
5. Review Router Agent: decides whether a human should inspect the paper.

The design keeps runtime low and avoids adding a second LLM pass by default.
"""

from __future__ import annotations

import json
import re
from typing import Any

from evidence_verifier import is_ambiguous_gene_symbol, verify_evidence


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _split_evidence(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def _gene_mentions(gene: str, text: str) -> int:
    if not gene:
        return 0
    return len(re.findall(rf"\b{re.escape(str(gene))}\b", text or "", flags=re.I))


def evidence_finder_agent(gene: str, ev: dict) -> dict:
    """Summarize evidence snippet coverage before classification scoring."""
    ev = ev or {}
    perturbation = _split_evidence(ev.get("evidence_perturbation"))
    vitro = _split_evidence(ev.get("evidence_in_vitro"))
    vivo = _split_evidence(ev.get("evidence_in_vivo"))
    screen = _split_evidence(ev.get("evidence_crispr_screen"))
    all_text = "\n".join(perturbation + vitro + vivo + screen)
    categories = {
        "perturbation": len(perturbation),
        "in_vitro": len(vitro),
        "in_vivo": len(vivo),
        "crispr_screen": len(screen),
    }
    covered = [k for k, v in categories.items() if v]
    status = "sufficient" if "perturbation" in covered and ("in_vitro" in covered or "in_vivo" in covered) else "partial" if covered else "missing"
    findings = [
        f"{len(covered)} evidence category/categories found",
        f"{_gene_mentions(gene, all_text)} gene mention(s) in extracted evidence",
    ]
    if is_ambiguous_gene_symbol(gene):
        findings.append("gene symbol is potentially ambiguous")
    return {
        "agent": "Evidence Finder Agent",
        "status": status,
        "findings": findings,
        "metrics": {
            **categories,
            "total_evidence_sents": int(ev.get("total_evidence_sents", 0) or 0),
            "gene_linked_evidence_sents": int(ev.get("gene_linked_evidence_sents", 0) or 0),
            "gene_mentions_in_evidence": _gene_mentions(gene, all_text),
            "has_fulltext_context": bool(ev.get("has_fulltext_context") or ev.get("pmcid")),
            "paper_type": ev.get("paper_type") or "unknown",
        },
    }


def classifier_consensus_agent(primary: dict, rules_result: dict, llm_result: dict | None) -> dict:
    """Record which classifier decided the label and whether rules/LLM agree."""
    primary = primary or {}
    rules_result = rules_result or {}
    llm_available = llm_result is not None
    disagreement = (
        llm_available
        and _as_bool(llm_result.get("functional_study")) != _as_bool(rules_result.get("functional_study"))
    )
    if disagreement:
        status = "disagreement"
    elif llm_available:
        status = "agreement"
    else:
        status = "rules_only"
    findings = [
        f"primary label: {'functional' if _as_bool(primary.get('functional_study')) else 'not functional'}",
        f"rules label: {'functional' if _as_bool(rules_result.get('functional_study')) else 'not functional'}",
    ]
    if llm_available:
        findings.append(f"BioMistral label: {'functional' if _as_bool(llm_result.get('functional_study')) else 'not functional'}")
    else:
        findings.append("BioMistral unavailable; rules-only classification")
    return {
        "agent": "Classifier Consensus Agent",
        "status": status,
        "findings": findings,
        "metrics": {
            "llm_available": llm_available,
            "llm_rules_disagree": disagreement,
            "primary_functional": _as_bool(primary.get("functional_study")),
            "rules_functional": _as_bool(rules_result.get("functional_study")),
        },
    }


def run_pre_scoring_agents(
    gene: str,
    title: str,
    abstract: str,
    ev: dict,
    primary: dict,
    rules_result: dict,
    llm_result: dict | None,
) -> dict:
    """Run evidence, consensus, and verifier agents before confidence scoring."""
    evidence_agent = evidence_finder_agent(gene, ev)
    consensus_agent = classifier_consensus_agent(primary, rules_result, llm_result)
    verification = verify_evidence(gene, title, abstract, ev, primary, rules_result, llm_result)
    verifier_agent = {
        "agent": "Skeptical Verifier Agent",
        "status": verification["verification_status"],
        "findings": [x.strip() for x in verification["verification_reasons"].split(";") if x.strip()],
        "metrics": {
            "evidence_quality_score": verification["evidence_quality_score"],
            "gene_match_quality": verification["gene_match_quality"],
        },
    }
    return {
        "verification": verification,
        "trace": {
            "workflow": "evidence_agent_workflow_v1",
            "agents": [evidence_agent, consensus_agent, verifier_agent],
        },
    }


def adjudicator_agent(
    confidence: float,
    primary: dict,
    verification: dict,
    llm_rules_disagree: bool,
) -> dict:
    """Second-stage deterministic adjudicator for risky classifications.

    This agent does not add a new LLM call. It challenges internally
    inconsistent rows so the score and review queue do not overstate certainty.
    """
    primary = primary or {}
    verification = verification or {}
    functional = _as_bool(primary.get("functional_study"))
    status = str(verification.get("verification_status") or "")
    score = float(confidence or 0)
    findings: list[str] = []

    if functional and status in {"not_supported", "weak_support", "needs_review"}:
        findings.append("functional label lacks strong verifier support")
    if score >= 0.82 and status != "supported":
        findings.append("high score without supported verifier status")
    if llm_rules_disagree:
        findings.append("rules and BioMistral disagree")
    if not functional and status == "supported":
        findings.append("possible false negative: verifier found support but label is non-functional")

    if findings:
        adjudication = "challenge"
    else:
        adjudication = "accept"
        findings.append("classification and verifier are internally consistent")

    return {
        "adjudication": adjudication,
        "adjudication_reasons": "; ".join(dict.fromkeys(findings)),
        "agent": {
            "agent": "Adjudicator Agent",
            "status": adjudication,
            "findings": findings,
            "metrics": {
                "confidence": round(score, 3),
                "verification_status": status,
                "functional_label": functional,
                "llm_rules_disagree": bool(llm_rules_disagree),
            },
        },
    }


def review_router_agent(
    confidence: float,
    primary: dict,
    verification: dict,
    llm_rules_disagree: bool,
    adjudication: dict | None = None,
) -> dict:
    """Route the paper to routine review or elevated human review."""
    primary = primary or {}
    verification = verification or {}
    adjudication = adjudication or {}
    status = str(verification.get("verification_status") or "")
    reasons: list[str] = []

    if adjudication.get("adjudication") == "challenge":
        recommendation = "high_priority_review"
        reasons.append(adjudication.get("adjudication_reasons") or "adjudicator challenged classification")
    elif status in {"not_supported", "weak_support"}:
        recommendation = "high_priority_review"
        reasons.append(f"verifier marked {status.replace('_', ' ')}")
    elif llm_rules_disagree:
        recommendation = "high_priority_review"
        reasons.append("rules and BioMistral disagree")
    elif status == "needs_review":
        recommendation = "medium_priority_review"
        reasons.append("verifier marked needs review")
    elif 0.45 <= float(confidence or 0) <= 0.70:
        recommendation = "medium_priority_review"
        reasons.append("borderline evidence-support score")
    else:
        recommendation = "routine"
        reasons.append("no major automated review flags")

    if _as_bool(primary.get("functional_study")) and status != "supported":
        reasons.append("functional label should be checked before biological interpretation")

    return {
        "review_recommendation": recommendation,
        "review_reasons": "; ".join(dict.fromkeys(reasons)),
        "agent": {
            "agent": "Human Review Router Agent",
            "status": recommendation,
            "findings": reasons,
            "metrics": {
                "confidence": round(float(confidence or 0), 3),
                "functional_label": _as_bool(primary.get("functional_study")),
            },
        },
    }


def serialize_agent_trace(trace: dict) -> str:
    """Serialize the agent trace compactly for SQLite."""
    try:
        return json.dumps(trace or {}, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return "{}"
