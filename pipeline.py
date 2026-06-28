# Imports & Config
import re
import time
import json
import gc
import os
import html
import hashlib
from urllib.error import HTTPError
from xml.etree import ElementTree as ET
from typing import Optional

import pandas as pd
from Bio import Entrez
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

from confidence import compute_confidence
from evidence_agents import adjudicator_agent, review_router_agent, run_pre_scoring_agents, serialize_agent_trace
from evidence_verifier import is_ambiguous_gene_symbol
from paper_type import classify_paper_type

# Entrez
Entrez.email       = os.environ.get("ENTREZ_EMAIL", "your_email@example.com")
Entrez.tool        = "functional-study-db"
Entrez.sleep_between_tries = True
Entrez.sleep_duration      = 0.4
PUBMED_MAX_RETSTART = 9998

# LLM Model
HF_MODEL   = "BioMistral/BioMistral-7B"
USE_LLM    = True
QUANTIZE   = "8bit"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# Optional second-pass LLM verifier. It is gated so Colab does not pay an
# extra generation call for every paper.
USE_AGENTIC_VERIFIER = _env_bool("USE_AGENTIC_VERIFIER", True)
AGENTIC_MODE = os.environ.get("AGENTIC_MODE", "borderline").strip().lower()
MAX_VERIFIER_CALLS = int(os.environ.get("MAX_VERIFIER_CALLS", "8"))
VERIFIER_ONLY_BORDERLINE = _env_bool("VERIFIER_ONLY_BORDERLINE", True)
_VERIFIER_STATE = {"calls": 0}

# Cache
GDRIVE_CACHE = os.environ.get(
    "GDRIVE_CACHE",
    "/content/drive/MyDrive/pubmed_llm/functional_study_cache"
)
LOCAL_CACHE  = "./cache_pubmed"
CACHE_DIR    = GDRIVE_CACHE if os.path.exists("/content/drive") else LOCAL_CACHE

# CSV
OUTPUT_CSV = "functional_papers.csv"

#  Cancer vocabulary
PANCREATIC_TERMS = ["pancreatic", "pdac", "pancreatic ductal", "pancreas"]

GI_TERMS = [
    "colon", "colorectal", "crc", "esophageal", "esophagus",
    "gastric", "stomach", "duodenal", "small intestine",
    "cholangiocarcinoma", "bile duct", "hepatocellular",
]

# Specific non-GI cancer terms — checked BEFORE GI to avoid misclassification
# from passing mentions (e.g. "intestine" in a breast cancer paper)
BREAST_TERMS   = ["breast cancer", "breast tumor", "breast carcinoma",
                   "mammary", "mcf-7", "t47d", "mda-mb"]
BRAIN_TERMS    = ["glioblastoma", "glioma", "medulloblastoma", "brain tumor",
                   "brain cancer", "gbm", "astrocytoma", "meningioma"]
LUNG_TERMS     = ["lung cancer", "lung tumor", "nsclc", "sclc",
                   "lung carcinoma", "pulmonary carcinoma"]
PROSTATE_TERMS = ["prostate cancer", "prostate tumor", "prostate carcinoma",
                  "crpc", "pca", "enzalutamide", "castration-resistant prostate",
                  "c4-2b", "22rv1", "lncap", "du145", "pc-3"]
LEUKEMIA_TERMS = ["leukemia", "lymphoma", "aml", "cll", "cml",
                   "myeloma", "hodgkin"]
OVARIAN_TERMS  = ["ovarian cancer", "ovarian tumor", "ovarian carcinoma"]

SPECIFIC_NON_GI_TERMS = (
    BREAST_TERMS + BRAIN_TERMS + LUNG_TERMS +
    PROSTATE_TERMS + LEUKEMIA_TERMS + OVARIAN_TERMS
)

GENERIC_CANCER_TERMS = ["cancer", "tumor", "tumour", "carcinoma", "neoplasm", "oncogene"]

PERTURBATION_SEARCH_TERMS = [
    "knockdown", "knockout", "CRISPR", "Cas9", "siRNA", "shRNA", "RNAi",
    "silencing", "deletion", "loss of function", "gene editing",
]

PHENOTYPE_SEARCH_TERMS = [
    "proliferation", "viability", "apoptosis", "migration", "invasion",
    "tumor growth", "tumour growth", "tumor volume", "tumour volume",
    "survival", "metastasis", "colony formation",
]

MODEL_SEARCH_TERMS = [
    "cell line", "cell culture", "xenograft", "mouse", "murine", "organoid",
    "in vivo", "in vitro",
]

REVIEW_EXCLUDE_QUERY = (
    "review[Publication Type] OR meta-analysis[Publication Type] OR "
    "editorial[Publication Type] OR comment[Publication Type]"
)

GENE_ALIAS_PATH = os.environ.get(
    "GENE_ALIAS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gene_aliases.tsv"),
)
_GENE_ALIAS_CACHE: dict[str, list[str]] | None = None

# Export columns for user-facing CSV
EXPORT_COLS = [
    "gene", "pmid", "pubmed_link", "title", "journal", "year",
    "cancer_type", "paper_type", "functional_study", "in_vitro", "in_vivo",
    "knockout", "knockdown", "shrna", "sirna", "crispr", "crispr_screen",
    "confidence", "best_evidence_quote", "evidence_functional_study", "evidence_in_vitro",
    "evidence_in_vivo", "evidence_crispr_screen", "overall_decision",
]

# Keywords
PATTERNS = {
    "knockout": re.compile(
        r"\b(knock[\s-]?out|KO\b|KO cells|gene deletion|deleted|deficient|"
        r"null mutant|null mice|conditional knockout|cKO|ablated|ablation|"
        r"loss[-\s]?of[-\s]?function|LOF\b)\b", re.I),

    "knockdown": re.compile(
        r"\b(knock[\s-]?down|KD\b|siRNA|shRNA|RNAi|morpholino|"
        r"antisense oligo|silenced|silencing|suppressed expression)\b", re.I),

    "crispr": re.compile(
        r"\b(crispr|cas9|sgrna|guide rna|gene editing|edited)\b", re.I),

    "crispr_screen": re.compile(
        r"\b(crispr screen|genome[-\s]?wide crispr|pooled crispr|crispr library|"
        r"dropout screen|loss[-\s]?of[-\s]?function screen)\b", re.I),

    "in_vivo": re.compile(
        r"\b(in vivo|xenograft|mouse model|murine|animal study|genetic model|"
        r"transgenic mice|knockin mice|knockout mice|orthotopic|subcutaneous)\b", re.I),

    "in_vitro": re.compile(
        r"\b(in vitro|cell line|cell culture|colony assay|MTT assay|"
        r"proliferation assay|growth viability|cell cycle|apoptosis|necrosis|"
        r"ferroptosis|BrdU|bromodeoxyuridine|organoid)\b", re.I),
}

# Confidence scoring lives in confidence.py so the live pipeline and maintenance
# recompute scripts use the same evidence-support rubric.


# SECTION 2: Entrez / PubMed helpers

def entrez_call(fn, *args, max_tries=5, base_sleep=0.4, **kwargs):
    for attempt in range(max_tries):
        try:
            time.sleep(base_sleep)
            return fn(*args, **kwargs)
        except HTTPError as e:
            if getattr(e, "code", None) == 429:
                wait = 2 ** attempt
                print(f"  [Rate limit] waiting {wait}s …")
                time.sleep(wait)
                continue
            if attempt == max_tries - 1:
                raise
            time.sleep((2 ** attempt) * 0.5)
        except Exception:
            if attempt == max_tries - 1:
                raise
            time.sleep((2 ** attempt) * 0.5)
    return None


STRICT_GENE_QUERY = os.environ.get("PUBMED_STRICT_GENE_QUERY", "1").strip() != "0"


def _unique_terms(terms: list[str]) -> list[str]:
    seen = set()
    out = []
    for term in terms:
        cleaned = str(term or "").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _quote_pubmed_term(term: str, field: str = "Title/Abstract") -> str:
    escaped = str(term).replace('"', '\\"')
    return f'"{escaped}"[{field}]'


def _load_gene_aliases() -> dict[str, list[str]]:
    """Load optional curated gene aliases from data/gene_aliases.tsv.

    The file is deliberately optional. If it is absent, search uses only the
    submitted gene symbol. A curated TSV avoids broad synonym expansion that can
    make short gene symbols less precise.
    """
    global _GENE_ALIAS_CACHE
    if _GENE_ALIAS_CACHE is not None:
        return _GENE_ALIAS_CACHE
    aliases: dict[str, list[str]] = {}
    if os.path.exists(GENE_ALIAS_PATH):
        try:
            with open(GENE_ALIAS_PATH, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 2:
                        continue
                    gene = parts[0].strip().upper()
                    vals = []
                    for cell in parts[1:]:
                        vals.extend(x.strip() for x in re.split(r"[|,;]", cell) if x.strip())
                    aliases[gene] = _unique_terms(vals)
        except Exception as exc:
            print(f"  [Aliases] Could not load {GENE_ALIAS_PATH}: {exc}")
    _GENE_ALIAS_CACHE = aliases
    return aliases


def gene_search_terms(gene: str) -> list[str]:
    """Return conservative query terms for a gene symbol plus curated aliases."""
    gene = str(gene or "").strip().upper()
    aliases = _load_gene_aliases().get(gene, [])
    terms = [gene] + aliases
    # Avoid accidentally expanding short ambiguous symbols into broad English
    # words unless the alias file explicitly uses a multi-character synonym.
    if is_ambiguous_gene_symbol(gene):
        terms = [t for t in terms if t.upper() == gene or len(t) >= 4]
    return _unique_terms(terms)


def _or_title_abstract(terms: list[str]) -> str:
    return " OR ".join(_quote_pubmed_term(t) for t in _unique_terms(terms))


def _build_pubmed_query(
    gene: str,
    extra_terms: list[str],
    allow_all_fields: bool = False,
    require_functional_context: bool = False,
    exclude_reviews: bool = False,
) -> str:
    """Build a precision-oriented PubMed query for a gene and cancer terms.

    Older versions searched the gene in ``All Fields`` by default. That improves
    recall but can retrieve unrelated papers for short or ambiguous gene symbols.
    The default now requires a title/abstract gene mention; set
    PUBMED_STRICT_GENE_QUERY=0 to allow the broader fallback for non-ambiguous
    genes.
    """
    terms = gene_search_terms(gene)
    gene_ta = _or_title_abstract(terms)
    if allow_all_fields and not is_ambiguous_gene_symbol(gene):
        gene_q = f"({gene_ta} OR {_quote_pubmed_term(gene, 'All Fields')})"
    else:
        gene_q = f"({gene_ta})"
    kw_q = _or_title_abstract(extra_terms)
    clauses = [gene_q, f"({kw_q})"]
    if require_functional_context:
        functional_q = _or_title_abstract(
            PERTURBATION_SEARCH_TERMS + PHENOTYPE_SEARCH_TERMS + MODEL_SEARCH_TERMS
        )
        clauses.append(f"({functional_q})")
    query = " AND ".join(clauses)
    if exclude_reviews:
        query = f"({query}) NOT ({REVIEW_EXCLUDE_QUERY})"
    return f"({query})"


def _pubmed_count(term: str) -> int:
    with entrez_call(Entrez.esearch, db="pubmed", term=term, retmax=0, retmode="xml") as h:
        return int(Entrez.read(h)["Count"])


def _pubmed_fetch_ids(term: str, total: int, batch_size: int = 2000, sort: str = "relevance") -> list[str]:
    pmids = []
    fetch_limit = min(total, PUBMED_MAX_RETSTART + 1)
    if total > fetch_limit:
        print(f"  [PubMed] Limiting ID fetch to first {fetch_limit} hits due to PubMed retstart cap.")
    for start in range(0, fetch_limit, batch_size):
        retmax = min(batch_size, fetch_limit - start)
        with entrez_call(
            Entrez.esearch,
            db="pubmed",
            term=term,
            retstart=start,
            retmax=retmax,
            retmode="xml",
            sort=sort,
        ) as h:
            pmids.extend(Entrez.read(h)["IdList"])
    return pmids


def _merge_pmids(*groups: list[str]) -> list[str]:
    seen = set()
    merged = []
    for group in groups:
        for pmid in group:
            key = str(pmid)
            if key not in seen:
                seen.add(key)
                merged.append(key)
    return merged


def pubmed_search_ids(gene: str, extra_terms: list[str], batch_size=2000) -> list[str]:
    allow_all_fields = not STRICT_GENE_QUERY and not is_ambiguous_gene_symbol(gene)
    focused_term = _build_pubmed_query(
        gene,
        extra_terms,
        allow_all_fields=allow_all_fields,
        require_functional_context=True,
        exclude_reviews=True,
    )
    broad_term = _build_pubmed_query(
        gene,
        extra_terms,
        allow_all_fields=allow_all_fields,
        require_functional_context=False,
        exclude_reviews=False,
    )

    focused_total = _pubmed_count(focused_term)
    broad_total = _pubmed_count(broad_term)

    if broad_total == 0 and STRICT_GENE_QUERY and not is_ambiguous_gene_symbol(gene):
        fallback = _build_pubmed_query(gene, extra_terms, allow_all_fields=True)
        fallback_total = _pubmed_count(fallback)
        if fallback_total:
            broad_term = fallback
            broad_total = fallback_total
            print(f"  [PubMed] {gene}: strict query had 0 broad hits; using all-fields fallback.")

    query_mode = "title/abstract + all fields" if "[All Fields]" in broad_term else "title/abstract"
    print(
        f"  [PubMed] {gene}: {broad_total} cancer hits; "
        f"{focused_total} evidence-focused hits ({query_mode})"
    )

    focused_pmids = _pubmed_fetch_ids(focused_term, focused_total, batch_size=batch_size, sort="relevance")
    broad_pmids = _pubmed_fetch_ids(broad_term, broad_total, batch_size=batch_size, sort="relevance")
    return _merge_pmids(focused_pmids, broad_pmids)


def pubmed_fetch_metadata(pmids: list[str]) -> dict:
    out = {}
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i+200]
        with entrez_call(Entrez.efetch, db="pubmed",
                         id=",".join(batch), retmode="xml") as h:
            rec = Entrez.read(h)

        for art in rec.get("PubmedArticle", []):
            try:
                cit     = art["MedlineCitation"]
                pmid    = str(cit["PMID"])
                article = cit["Article"]

                title = str(article.get("ArticleTitle", ""))

                ab = article.get("Abstract", {}).get("AbstractText", [])
                abstract = " ".join(str(x) for x in ab) if isinstance(ab, list) else str(ab or "")

                try:
                    journal = str(article["Journal"]["Title"])
                except Exception:
                    journal = ""

                try:
                    year = str(article["Journal"]["JournalIssue"]["PubDate"].get("Year", ""))
                except Exception:
                    year = ""

                publication_types = []
                try:
                    for ptype in article.get("PublicationTypeList", []):
                        text = str(ptype).strip()
                        if text:
                            publication_types.append(text)
                except Exception:
                    publication_types = []

                doi = ""
                try:
                    for idobj in art["PubmedData"]["ArticleIdList"]:
                        if idobj.attributes.get("IdType") == "doi":
                            doi = str(idobj)
                            break
                except Exception:
                    pass

                out[pmid] = dict(title=title, abstract=abstract,
                                 journal=journal, year=year, doi=doi,
                                 publication_types="|".join(dict.fromkeys(publication_types)))
            except Exception:
                continue
    return out


def fetch_pmc_fulltext(pmid: str) -> tuple[str, Optional[str]]:
    try:
        with entrez_call(Entrez.elink, dbfrom="pubmed", db="pmc",
                         id=pmid, linkname="pubmed_pmc") as h:
            record = Entrez.read(h)

        if not record or not record[0].get("LinkSetDb"):
            return "", None

        pmcid = record[0]["LinkSetDb"][0]["Link"][0]["Id"]

        with entrez_call(Entrez.efetch, db="pmc", id=pmcid,
                         rettype="full", retmode="xml") as h:
            xml_bytes = h.read()

        text = _jats_to_text(xml_bytes)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text, str(pmcid)

    except Exception:
        return "", None


def _jats_to_text(xml_bytes: bytes) -> str:
    if not xml_bytes:
        return ""
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8", errors="ignore")
    try:
        root = ET.fromstring(xml_bytes)
        body = root.find(".//body")
        node = body if body is not None else root
        chunks = [t.strip() for t in node.itertext() if t.strip()]
        out = html.unescape(" ".join(chunks))
    except Exception:
        raw = xml_bytes.decode("utf-8", errors="ignore")
        out = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    out = re.sub(r"Publisher.?s Note:.*?$", "", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()


# SECTION 3: Evidence extraction 
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    return [s.strip() for s in _SENT_SPLIT.split(re.sub(r"\s+", " ", text).strip())
            if len(s.strip()) > 20]


def _gene_mentioned(gene: str, sent: str) -> bool:
    if not gene or not sent:
        return False
    for term in gene_search_terms(gene):
        if re.search(rf"\b{re.escape(term.lower())}\b", sent.lower()):
            return True
    return False


def _sentence_is_evidence(gene: str, sent: str) -> bool:
    if not sent:
        return False
    s = sent.lower()
    if not _gene_mentioned(gene, sent):
        return False
    if any(p.search(sent) for p in [PATTERNS["knockout"], PATTERNS["knockdown"],
                                     PATTERNS["crispr"], PATTERNS["crispr_screen"]]):
        return True
    if PATTERNS["in_vitro"].search(sent) or PATTERNS["in_vivo"].search(sent):
        return True
    phenotype_words = [
        "growth", "proliferation", "viability", "apoptosis", "necrosis",
        "cell death", "organoid", "tumor size", "tumour size", "tumor volume",
        "tumour volume", "survival", "xenograft", "mouse", "murine",
    ]
    return any(w in s for w in phenotype_words)


def _sentence_evidence_score(gene: str, sent: str) -> tuple[float, dict]:
    """Score a sentence for evidence retrieval.

    This is a retrieval score, not a classification decision. It helps place
    the most useful abstract/full-text snippets in front of the classifier and
    verifier.
    """
    s = sent or ""
    sl = s.lower()
    details = {
        "gene": _gene_mentioned(gene, s),
        "perturbation": any(
            p.search(s)
            for p in [PATTERNS["knockout"], PATTERNS["knockdown"], PATTERNS["crispr"], PATTERNS["crispr_screen"]]
        ),
        "model": PATTERNS["in_vitro"].search(s) is not None or PATTERNS["in_vivo"].search(s) is not None,
        "phenotype": any(w in sl for w in PHENOTYPE_SEARCH_TERMS),
        "cancer": any(w in sl for w in GENERIC_CANCER_TERMS + PANCREATIC_TERMS + GI_TERMS + list(SPECIFIC_NON_GI_TERMS)),
        "review_like": any(w in sl for w in ["review", "meta-analysis", "systematic review"]),
        "correlation_like": any(w in sl for w in ["correlat", "prognostic", "biomarker", "signature"]),
    }
    score = 0.0
    if details["gene"]:
        score += 0.34
    if details["perturbation"]:
        score += 0.30
    if details["phenotype"]:
        score += 0.16
    if details["model"]:
        score += 0.12
    if details["cancer"]:
        score += 0.08
    if details["review_like"]:
        score -= 0.12
    if details["correlation_like"] and not details["perturbation"]:
        score -= 0.08
    return max(0.0, min(1.0, score)), details


def extract_evidence_pack(gene: str, abstract: str, fulltext: str,
                           max_sents=40, neighbors=1) -> dict:
    abs_sents  = split_sentences(abstract or "")
    full_sents = split_sentences(fulltext or "")
    all_sents  = abs_sents + full_sents

    if not all_sents:
        return dict(evidence_perturbation=[], evidence_in_vitro=[],
                    evidence_in_vivo=[], evidence_crispr_screen=[],
                    evidence_text="", total_evidence_sents=0,
                    best_evidence_quote="", gene_linked_evidence_sents=0,
                    evidence_retrieval_score=0.0)

    keep_idx = set()
    scored = []
    for i, s in enumerate(all_sents):
        score, detail = _sentence_evidence_score(gene, s)
        scored.append((score, i, detail))
        if _sentence_is_evidence(gene, s):
            for j in range(max(0, i - neighbors), min(len(all_sents), i + neighbors + 1)):
                keep_idx.add(j)

    # Add the strongest gene-centered evidence candidates even when the strict
    # rule extractor did not trigger. This improves recall for abstracts that
    # describe a perturbation and phenotype in adjacent or less formulaic text.
    top_scored = sorted(scored, key=lambda x: x[0], reverse=True)[:12]
    for score, i, detail in top_scored:
        if score >= 0.42 and (detail["gene"] or detail["perturbation"]):
            for j in range(max(0, i - neighbors), min(len(all_sents), i + neighbors + 1)):
                keep_idx.add(j)

    seen, kept = set(), []
    for s in (all_sents[i] for i in sorted(keep_idx)):
        if s not in seen:
            kept.append(s)
            seen.add(s)

    scored_by_sentence = {}
    for score, i, detail in scored:
        scored_by_sentence[all_sents[i]] = (score, detail)

    direct_candidates = [
        (score, sent, detail)
        for sent in kept
        for score, detail in [scored_by_sentence.get(sent, _sentence_evidence_score(gene, sent))]
        if detail.get("gene") and (detail.get("perturbation") or detail.get("phenotype") or detail.get("model"))
    ]
    direct_candidates.sort(key=lambda x: x[0], reverse=True)
    best_evidence_quote = direct_candidates[0][1] if direct_candidates else ""
    gene_linked_evidence_sents = len({sent for _, sent, _ in direct_candidates})

    g_lower = gene.lower()
    ev_pert, ev_vitro, ev_vivo, ev_screen = [], [], [], []
    for s in kept:
        sl = s.lower()
        score, detail = scored_by_sentence.get(s, _sentence_evidence_score(gene, s))
        directly_gene_linked = bool(detail.get("gene"))
        gene_specific_rnai = (
            re.search(rf"\bsh[-_\s]*{re.escape(g_lower)}\b", sl)
            or re.search(rf"\bsi[-_\s]*{re.escape(g_lower)}\b", sl)
            or re.search(rf"\bsg[-_\s]*{re.escape(g_lower)}\b", sl)
        )
        if ((directly_gene_linked and any(p.search(s) for p in [PATTERNS["knockout"], PATTERNS["knockdown"], PATTERNS["crispr"]]))
                or re.search(rf"\bsh[-_\s]*{re.escape(g_lower)}\b", sl)
                or re.search(rf"\bsi[-_\s]*{re.escape(g_lower)}\b", sl)
                or gene_specific_rnai):
            ev_pert.append(s)
        if directly_gene_linked and (PATTERNS["in_vitro"].search(s) or any(w in sl for w in
                ["growth", "proliferation", "viability", "apoptosis", "necrosis",
                 "cell death", "organoid"])):
            ev_vitro.append(s)
        if directly_gene_linked and (PATTERNS["in_vivo"].search(s) or any(w in sl for w in
                ["xenograft", "tumor size", "tumour size", "tumor volume",
                 "tumour volume", "tumor growth", "survival", "kaplan-meier"])):
            ev_vivo.append(s)
        if PATTERNS["crispr_screen"].search(s):
            ev_screen.append(s)

    retrieval_scores = [_sentence_evidence_score(gene, s)[0] for s in kept]
    evidence_retrieval_score = round(
        max(retrieval_scores) if retrieval_scores else 0.0,
        3,
    )

    def cap(lst, k): return lst[:k]
    ev_pert, ev_vitro, ev_vivo, ev_screen = (
        cap(ev_pert, 12), cap(ev_vitro, 12), cap(ev_vivo, 12), cap(ev_screen, 8))

    parts = []
    if ev_pert:
        parts.append("EVIDENCE — Perturbation:")
        parts += [f"- {x}" for x in ev_pert]
    if ev_screen:
        parts.append("EVIDENCE — CRISPR screen:")
        parts += [f"- {x}" for x in ev_screen]
    if ev_vitro:
        parts.append("EVIDENCE — In vitro phenotype:")
        parts += [f"- {x}" for x in ev_vitro]
    if ev_vivo:
        parts.append("EVIDENCE — In vivo phenotype:")
        parts += [f"- {x}" for x in ev_vivo]

    bullets = [p for p in parts if p.startswith("- ")]
    if len(bullets) > max_sents:
        new, n = [], 0
        for p in parts:
            if not p.startswith("- "):
                new.append(p)
            elif n < max_sents:
                new.append(p); n += 1
        parts = new

    return dict(
        evidence_perturbation=ev_pert,
        evidence_in_vitro=ev_vitro,
        evidence_in_vivo=ev_vivo,
        evidence_crispr_screen=ev_screen,
        evidence_text="\n".join(parts).strip(),
        total_evidence_sents=len(kept),
        best_evidence_quote=best_evidence_quote,
        gene_linked_evidence_sents=gene_linked_evidence_sents,
        evidence_retrieval_score=evidence_retrieval_score,
    )


# SECTION 4: Rules-based classifier 
def _detect_gene_perturbation(gene: str, text: str) -> bool:
    g = gene.lower()
    t = (text or "").lower()
    if re.search(rf"\bsh[-_\s]*{re.escape(g)}\b", t): return True
    if re.search(rf"\bsi[-_\s]*{re.escape(g)}\b", t): return True
    for term in ["knockout", "knockdown", "crispr", "sirna", "shrna",
                 "deleted", "deletion", "silenced", "deficient", "null"]:
        for m in re.finditer(term, t):
            window = t[max(0, m.start()-60):m.end()+60]
            if re.search(rf"\b{re.escape(g)}\b", window):
                return True
    return False


def _detect_in_vitro(text: str) -> list[str]:
    t = (text or "").lower()
    hits = []
    if any(w in t for w in ["growth", "proliferation"]): hits.append("growth")
    if "viability" in t: hits.append("viability")
    if any(w in t for w in ["death", "apoptosis", "necrosis", "cell death"]): hits.append("death")
    if "organoid" in t: hits.append("organoid")
    return sorted(set(hits))


def _detect_in_vivo(text: str) -> list[str]:
    t = (text or "").lower()
    hits = []
    if any(w in t for w in ["tumor size", "tumour size", "tumor volume",
                             "tumour volume", "tumor growth"]): hits.append("tumor_size")
    if any(w in t for w in ["tumor stage", "tumour stage", "tumor grade",
                             "tumour grade", "staging"]): hits.append("stage_grade")
    if any(w in t for w in ["survival", "kaplan-meier", "overall survival"]): hits.append("survival")
    return sorted(set(hits))


def classify_rules(gene: str, text: str) -> dict:
    t = (text or "").lower()
    perturbed   = _detect_gene_perturbation(gene, text)
    knockout    = perturbed and bool(PATTERNS["knockout"].search(t))
    knockdown   = perturbed and bool(PATTERNS["knockdown"].search(t))
    shrna       = "shrna" in t or bool(re.search(rf"\bsh[-_\s]*{re.escape(gene.lower())}\b", t))
    sirna       = "sirna" in t or bool(re.search(rf"\bsi[-_\s]*{re.escape(gene.lower())}\b", t))
    crispr      = perturbed and bool(PATTERNS["crispr"].search(t))
    crispr_screen = bool(PATTERNS["crispr_screen"].search(t))
    impact_vitro = _detect_in_vitro(text)
    impact_vivo  = _detect_in_vivo(text)
    in_vitro     = len(impact_vitro) > 0
    in_vivo      = len(impact_vivo)  > 0
    functional   = perturbed and (in_vitro or in_vivo)

    return dict(
        functional_study=functional,
        in_vitro=functional and in_vitro,
        in_vivo=functional and in_vivo,
        knockout=knockout, knockdown=knockdown,
        shrna=shrna, sirna=sirna, crispr=crispr, crispr_screen=crispr_screen,
        impact_in_vitro=impact_vitro if functional else [],
        impact_in_vivo=impact_vivo  if functional else [],
        source="rules",
    )


# SECTION 5: LLM classifier (BioMistral-7B)
_LLM: dict = {"tok": None, "model": None}

JSON_SCHEMA = """{
 "functional_study": true/false,
 "knockout": true/false,
 "knockdown": true/false,
 "crispr": true/false,
 "shrna": true/false,
 "sirna": true/false,
 "crispr_screen": true/false,
 "in_vitro_functional": true/false,
 "in_vivo_functional": true/false,
 "impact_in_vitro": ["growth","viability","death","organoid"] or [],
 "impact_in_vivo": ["tumor_size","stage_grade","survival"] or [],
 "cancer_type": "pancreatic" or "gi" or "cancer" or "unknown",
 "reasoning": "one sentence explaining your decision"
}"""


def load_llm():
    print(f"[LLM] Loading {HF_MODEL} ({QUANTIZE} quantization) …")
    tok = AutoTokenizer.from_pretrained(HF_MODEL, use_fast=True, trust_remote_code=True)
    tok.pad_token    = tok.eos_token
    tok.padding_side = "left"

    bnb_config = None
    if QUANTIZE == "4bit":
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
    elif QUANTIZE == "8bit":
        bnb_config = BitsAndBytesConfig(load_in_8bit=True, llm_int8_threshold=6.0)

    model = AutoModelForCausalLM.from_pretrained(
        HF_MODEL,
        device_map="auto",
        quantization_config=bnb_config,
        dtype=torch.float16,
        use_safetensors=False,
        low_cpu_mem_usage=True,
        trust_remote_code=True,
    )
    model.eval()
    print("[LLM] Model loaded.")
    return tok, model


def _get_llm():
    if _LLM["tok"] is None:
        _LLM["tok"], _LLM["model"] = load_llm()
    return _LLM["tok"], _LLM["model"]


def _extract_json(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        fixed = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        fixed = fixed.replace("'", '"')
        try:
            return json.loads(fixed)
        except Exception:
            return None


def _postprocess_llm_json(data: dict) -> dict:
    def b(x): return bool(x) if isinstance(x, bool) else str(x).lower() in {"true","yes","1"}
    knockout  = b(data.get("knockout", False))
    knockdown = b(data.get("knockdown", False))
    crispr    = b(data.get("crispr", False))
    shrna     = b(data.get("shrna", False))
    sirna     = b(data.get("sirna", False))
    crispr_screen = b(data.get("crispr_screen", False))
    in_vitro  = b(data.get("in_vitro_functional", False))
    in_vivo   = b(data.get("in_vivo_functional", False))
    perturbed = any([knockout, knockdown, crispr, shrna, sirna])
    impact    = in_vitro or in_vivo
    functional = perturbed and impact

    # Extract and validate cancer type from LLM
    raw_ct = str(data.get("cancer_type", "")).lower().strip()
    cancer_type = raw_ct if raw_ct in {"pancreatic", "gi", "cancer", "unknown"} else None

    return dict(
        functional_study=functional,
        knockout=knockout, knockdown=knockdown, crispr=crispr,
        shrna=shrna, sirna=sirna, crispr_screen=crispr_screen,
        in_vitro=in_vitro, in_vivo=in_vivo,
        impact_in_vitro=data.get("impact_in_vitro", []) if functional else [],
        impact_in_vivo=data.get("impact_in_vivo",  []) if functional else [],
        llm_reasoning=data.get("reasoning", ""),
        cancer_type=cancer_type,
        source="llm",
    )


@torch.inference_mode()
def classify_llm(gene: str, title: str, abstract: str,
                 evidence_text: str, max_new_tokens=220) -> Optional[dict]:
    tok, model = _get_llm()
    max_ctx = int(getattr(model.config, "max_position_embeddings", 4096))
    budget  = max(512, max_ctx - (max_new_tokens + 64))

    prompt = f"""<s>[INST] You are a biomedical expert. Classify whether this paper reports a FUNCTIONAL STUDY of gene {gene} in cancer.

DEFINITION — functional study requires BOTH:
1. Direct perturbation of {gene}: knockout, knockdown, CRISPR deletion, shRNA (e.g. sh{gene}), or siRNA
2. Measured cancer phenotype: in vitro (growth/viability/apoptosis/organoid) OR in vivo (tumor size/volume/survival)

Rules:
- Expression-only or correlation studies → functional_study: false
- Perturbation of a DIFFERENT gene → functional_study: false
- Only EVIDENCE section satisfies criteria; ABSTRACT is context only
- When uncertain → false
- cancer_type: classify based ONLY on the experimental model used (cell lines, animal models). Ignore background mentions of other cancers. Use "pancreatic" for pancreatic/PDAC experiments, "gi" for colon/gastric/colorectal/liver/bile duct experiments, "cancer" for all others (breast/brain/lung/prostate/etc), "unknown" if no clear experimental cancer model.

Return ONLY valid JSON matching this schema:
{JSON_SCHEMA}

TITLE: {(title or "").strip()}

EVIDENCE (use for decision):
\"\"\"{(evidence_text or "No evidence extracted.").strip()}\"\"\"

ABSTRACT (context only):
\"\"\"{(abstract or "").strip()[:1500]}\"\"\"
[/INST]
JSON:
"""
    inputs = tok(prompt, return_tensors="pt",
                 truncation=True, max_length=budget).to(model.device)

    out  = model.generate(**inputs, max_new_tokens=max_new_tokens,
                          do_sample=False, pad_token_id=tok.eos_token_id)
    resp = tok.decode(out[0], skip_special_tokens=True)

    if "[/INST]" in resp:
        resp = resp.split("[/INST]")[-1]

    data = _extract_json(resp)
    if data is None:
        return None
    return _postprocess_llm_json(data)


VERIFIER_JSON_SCHEMA = """
{
  "verifier_decision": "support|challenge|unclear",
  "target_gene_directly_studied": true,
  "direct_gene_perturbation": true,
  "phenotype_evidence": true,
  "paper_type": "functional_experiment|expression_only|prognosis_only|review|methods|unclear",
  "evidence_quote": "short exact supporting quote or empty string",
  "reason": "one sentence explaining the decision",
  "needs_review": true
}
"""


def _normalize_verifier_json(data: dict | None) -> Optional[dict]:
    if not isinstance(data, dict):
        return None

    def b(x):
        if isinstance(x, bool):
            return x
        return str(x).strip().lower() in {"1", "true", "yes", "y"}

    decision = str(data.get("verifier_decision") or "unclear").strip().lower()
    if decision not in {"support", "challenge", "unclear"}:
        decision = "unclear"
    paper_type = str(data.get("paper_type") or "unclear").strip().lower()
    if paper_type not in {
        "functional_experiment", "expression_only", "prognosis_only",
        "review", "methods", "unclear",
    }:
        paper_type = "unclear"

    return {
        "verifier_decision": decision,
        "target_gene_directly_studied": b(data.get("target_gene_directly_studied")),
        "direct_gene_perturbation": b(data.get("direct_gene_perturbation")),
        "phenotype_evidence": b(data.get("phenotype_evidence")),
        "paper_type": paper_type,
        "evidence_quote": str(data.get("evidence_quote") or "").strip()[:500],
        "reason": str(data.get("reason") or "").strip()[:500],
        "needs_review": b(data.get("needs_review")) or decision in {"challenge", "unclear"},
    }


def _should_run_agentic_verifier(
    gene: str,
    primary: dict,
    llm_result: dict | None,
    llm_rules_disagree: bool,
    verification: dict,
    confidence: float,
    ev: dict,
) -> bool:
    """Gate the second LLM verifier to high-value cases only."""
    if not USE_AGENTIC_VERIFIER or AGENTIC_MODE in {"off", "disabled", "none"}:
        return False
    if llm_result is None:
        return False
    if _VERIFIER_STATE["calls"] >= MAX_VERIFIER_CALLS:
        return False

    functional = bool(primary.get("functional_study"))
    status = str(verification.get("verification_status") or "").lower()
    weak_verifier = status in {"needs_review", "weak_support", "not_supported"}
    borderline = 0.45 <= float(confidence or 0) <= 0.76
    high_score_weak_evidence = (
        float(confidence or 0) >= 0.82
        and (
            float(verification.get("evidence_quality_score") or 0) < 0.66
            or int(ev.get("gene_linked_evidence_sents", 0) or 0) < 1
        )
    )
    ambiguous = is_ambiguous_gene_symbol(gene)

    if AGENTIC_MODE == "all":
        return True
    if AGENTIC_MODE == "functional":
        return functional or llm_rules_disagree

    # Default "borderline" mode.
    trigger = (
        functional
        or borderline
        or llm_rules_disagree
        or weak_verifier
        or high_score_weak_evidence
        or ambiguous
    )
    if VERIFIER_ONLY_BORDERLINE:
        trigger = trigger and (
            borderline
            or llm_rules_disagree
            or weak_verifier
            or high_score_weak_evidence
            or (functional and status != "supported")
            or ambiguous
        )
    return trigger


@torch.inference_mode()
def run_llm_skeptical_verifier(
    gene: str,
    title: str,
    abstract: str,
    evidence_text: str,
    primary: dict,
    rules_result: dict,
    llm_result: dict | None,
    verification: dict,
    confidence: float,
    max_new_tokens: int = 260,
) -> Optional[dict]:
    """Ask BioMistral to challenge the current classification for one paper.

    This is a verifier, not the primary classifier. It should be used only for
    papers where an extra check can materially improve human review routing.
    """
    tok, model = _get_llm()
    max_ctx = int(getattr(model.config, "max_position_embeddings", 4096))
    budget = max(512, max_ctx - (max_new_tokens + 64))

    label = "functional" if primary.get("functional_study") else "not functional"
    rules_label = "functional" if rules_result.get("functional_study") else "not functional"
    llm_label = "functional" if (llm_result or {}).get("functional_study") else "not functional"
    prompt = f"""<s>[INST] You are a skeptical biomedical evidence verifier.

Your job is to check whether the current label is actually supported for target gene {gene}.

Current label: {label}
Rules label: {rules_label}
BioMistral classifier label: {llm_label}
Current evidence-support score: {float(confidence or 0):.3f}
Deterministic verifier: {verification.get('verification_status', 'unknown')}
Deterministic verifier reasons: {verification.get('verification_reasons', '')}

Functional evidence requires BOTH:
1. Direct perturbation of {gene}, such as knockout, knockdown, CRISPR, shRNA, or siRNA.
2. Cancer phenotype measurement, such as proliferation, apoptosis, migration, invasion, tumor growth, survival, organoid growth, or xenograft outcome.

Challenge the label if:
- the paper is review/meta-analysis/methods/prognosis/expression-only,
- another gene is perturbed,
- {gene} is only mentioned in background,
- the evidence quote does not support perturbation plus phenotype.

Return ONLY valid JSON with this schema:
{VERIFIER_JSON_SCHEMA}

TITLE:
{(title or '').strip()}

EVIDENCE SNIPPETS:
\"\"\"{(evidence_text or 'No evidence extracted.').strip()[:2500]}\"\"\"

ABSTRACT CONTEXT:
\"\"\"{(abstract or '').strip()[:1500]}\"\"\"
[/INST]
JSON:
"""
    inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=budget).to(model.device)
    out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, pad_token_id=tok.eos_token_id)
    resp = tok.decode(out[0], skip_special_tokens=True)
    if "[/INST]" in resp:
        resp = resp.split("[/INST]")[-1]
    return _normalize_verifier_json(_extract_json(resp))


# SECTION 6: Ensemble + evidence-support score
def ensemble_classify(gene: str, title: str, abstract: str,
                      evidence_text: str, ev: dict) -> dict:
    full_text_for_rules = (abstract or "") + "\n" + (evidence_text or "")
    rules_result = classify_rules(gene, full_text_for_rules)

    llm_result = None
    if USE_LLM:
        try:
            llm_result = classify_llm(gene, title, abstract, evidence_text)
        except Exception as e:
            print(f"  [LLM] Error: {e} — using rules only")

    primary = llm_result if llm_result is not None else rules_result
    llm_rules_disagree = (
        llm_result is not None and
        llm_result.get("functional_study") != rules_result.get("functional_study")
    )

    agent_result = run_pre_scoring_agents(
        gene=gene,
        title=title,
        abstract=abstract,
        ev=ev,
        primary=primary,
        rules_result=rules_result,
        llm_result=llm_result,
    )
    verification = agent_result["verification"]
    ev_for_score = {**(ev or {}), **verification}

    conf_functional, conf_not_functional, pos_signals, neg_signals = compute_confidence(
        gene, llm_result, rules_result, ev_for_score, title, abstract
    )

    confidence = conf_functional if primary.get("functional_study") else conf_not_functional
    if llm_rules_disagree:
        # Disagreement is already penalized inside confidence.py. Keep an
        # additional soft ceiling so disagreement remains review-worthy without
        # flattening many rows to the same score.
        confidence = min(confidence, 0.72)

    agentic_verifier = None
    if _should_run_agentic_verifier(
        gene, primary, llm_result, llm_rules_disagree, verification, confidence, ev_for_score
    ):
        try:
            agentic_verifier = run_llm_skeptical_verifier(
                gene=gene,
                title=title,
                abstract=abstract,
                evidence_text=evidence_text,
                primary=primary,
                rules_result=rules_result,
                llm_result=llm_result,
                verification=verification,
                confidence=confidence,
            )
            _VERIFIER_STATE["calls"] += 1
        except Exception as e:
            print(f"  [Agentic verifier] Error: {e} - continuing with deterministic verifier")

    if agentic_verifier:
        decision = agentic_verifier["verifier_decision"]
        agentic_score = 0.92 if decision == "support" else 0.18 if decision == "challenge" else 0.48
        ev_for_score = {
            **ev_for_score,
            "agentic_verifier_decision": decision,
            "agentic_verifier_reason": agentic_verifier.get("reason", ""),
            "agentic_verifier_needs_review": agentic_verifier.get("needs_review", True),
            "evidence_quality_score": max(
                float(ev_for_score.get("evidence_quality_score") or 0.0),
                agentic_score if decision == "support" else 0.0,
            ),
        }
        if decision == "challenge":
            verification = {
                **verification,
                "verification_status": "needs_review",
                "verification_reasons": "; ".join(
                    x for x in [
                        verification.get("verification_reasons", ""),
                        "LLM skeptical verifier challenged the classification",
                        agentic_verifier.get("reason", ""),
                    ] if x
                ),
                "evidence_quality_score": min(
                    float(verification.get("evidence_quality_score") or 0.5),
                    0.48,
                ),
            }
            ev_for_score = {**ev_for_score, **verification}
        elif decision == "unclear":
            verification = {
                **verification,
                "verification_status": "needs_review",
                "verification_reasons": "; ".join(
                    x for x in [
                        verification.get("verification_reasons", ""),
                        "LLM skeptical verifier marked evidence unclear",
                        agentic_verifier.get("reason", ""),
                    ] if x
                ),
            }
            ev_for_score = {**ev_for_score, **verification}

        conf_functional, conf_not_functional, pos_signals, neg_signals = compute_confidence(
            gene, llm_result, rules_result, ev_for_score, title, abstract
        )
        confidence = conf_functional if primary.get("functional_study") else conf_not_functional
        if decision == "challenge":
            confidence = min(confidence, 0.62)
        elif decision == "unclear":
            confidence = min(confidence, 0.76)
        elif decision == "support" and primary.get("functional_study"):
            confidence = min(0.95, confidence + 0.03)

    adjudication = adjudicator_agent(confidence, primary, verification, llm_rules_disagree)
    route = review_router_agent(confidence, primary, verification, llm_rules_disagree, adjudication)
    agent_trace = agent_result["trace"]
    if agentic_verifier:
        agent_trace["agents"].append({
            "agent": "LLM Skeptical Verifier Agent",
            "status": agentic_verifier["verifier_decision"],
            "findings": [agentic_verifier.get("reason", "")],
            "metrics": {
                "target_gene_directly_studied": agentic_verifier.get("target_gene_directly_studied"),
                "direct_gene_perturbation": agentic_verifier.get("direct_gene_perturbation"),
                "phenotype_evidence": agentic_verifier.get("phenotype_evidence"),
                "paper_type": agentic_verifier.get("paper_type"),
                "needs_review": agentic_verifier.get("needs_review"),
            },
        })
    agent_trace["agents"].append(adjudication["agent"])
    agent_trace["agents"].append(route["agent"])

    return {
        **primary,
        "confidence":            confidence,
        "conf_functional":       conf_functional,
        "conf_not_functional":   conf_not_functional,
        "pos_signals":           pos_signals,
        "neg_signals":           neg_signals,
        "llm_rules_disagree":    llm_rules_disagree,
        "llm_available":         llm_result is not None,
        "rules_functional":      rules_result.get("functional_study"),
        "llm_reasoning":         (llm_result or {}).get("llm_reasoning", ""),
        "verification_status":   verification["verification_status"],
        "verification_reasons":  verification["verification_reasons"],
        "evidence_quality_score": verification["evidence_quality_score"],
        "gene_match_quality":    verification["gene_match_quality"],
        "adjudication_status":   adjudication["adjudication"],
        "adjudication_reasons":  adjudication["adjudication_reasons"],
        "review_recommendation": route["review_recommendation"],
        "review_reasons":        route["review_reasons"],
        "agentic_verifier_decision": (agentic_verifier or {}).get("verifier_decision", ""),
        "agentic_verifier_reason":   (agentic_verifier or {}).get("reason", ""),
        "agentic_verifier_quote":    (agentic_verifier or {}).get("evidence_quote", ""),
        "agentic_verifier_needs_review": bool((agentic_verifier or {}).get("needs_review", False)),
        "agent_trace":           serialize_agent_trace(agent_trace),
        "_llm_result":           llm_result,
        "_rules_result":         rules_result,
    }


#  SECTION 7: Cancer type classification
def classify_cancer_type(text: str) -> str:
    """
    Classify cancer type from text.
    Priority order:
    1. Pancreatic (lab focus)
    2. Specific non-GI cancers (breast, brain, lung, etc.) — checked BEFORE GI
       to prevent passing mentions of 'intestine' in non-GI papers from
       triggering a GI classification
    3. GI cancers
    4. Generic cancer
    5. Unknown
    """
    t = (text or "").lower()

    if any(w in t for w in PANCREATIC_TERMS):
        return "pancreatic"

    # Check specific non-GI cancers before GI
    if any(w in t for w in SPECIFIC_NON_GI_TERMS):
        return "cancer"

    if any(w in t for w in GI_TERMS):
        return "gi"

    if any(w in t for w in GENERIC_CANCER_TERMS):
        return "cancer"

    return "unknown"


#  SECTION 8: Cache
def _cache_path(gene: str, pmid: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.md5(f"{gene}_{pmid}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}.json")


def cache_get(gene: str, pmid: str) -> Optional[dict]:
    fp = _cache_path(gene, pmid)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp) as f:
            return json.load(f)
    except Exception:
        return None


def cache_set(gene: str, pmid: str, row: dict):
    fp = _cache_path(gene, pmid)
    try:
        with open(fp, "w") as f:
            json.dump(row, f)
    except Exception as e:
        print(f"  [Cache] Write failed: {e}")


def _db_processed_pmids(gene: str) -> set[str]:
    """Return PMIDs already recorded in SQLite, if db.py is available."""
    try:
        import db as database
        return database.get_processed_pmids(gene)
    except Exception:
        return set()


def _db_upsert_rows(rows: list[dict]):
    """Persist rows to SQLite when this pipeline is running beside db.py."""
    if not rows:
        return
    try:
        import db as database
        database.upsert_papers_bulk(rows)
        for gene in sorted({str(r.get("gene", "")).upper() for r in rows if r.get("gene")}):
            database.update_gene_record(gene)
    except Exception as e:
        print(f"  [DB] Save skipped/failed: {e}")


def _db_mark_skipped(gene: str, pmid: str, reason: str):
    try:
        import db as database
        database.mark_skipped(gene, str(pmid), reason=reason)
    except Exception:
        pass


# SECTION 9: Per-gene analysis 
def check_new_pmids(gene: str, pmids: list[str]) -> list[str]:
    processed_pmids = _db_processed_pmids(gene)
    return [pmid for pmid in pmids if str(pmid) not in processed_pmids]


def _paper_relevance_score(gene: str, title: str, abstract: str) -> tuple[float, dict]:
    """Score a candidate paper before classification.

    This lightweight ranker is intentionally conservative. It prioritizes
    papers likely to contain functional evidence while keeping a broad fallback
    pool available so recall is not reduced to only obvious keyword hits.
    """
    text = f"{title or ''}\n{abstract or ''}"
    tl = text.lower()
    title_l = (title or "").lower()

    gene_hits = sum(
        len(re.findall(rf"\b{re.escape(term.lower())}\b", tl))
        for term in gene_search_terms(gene)
    )
    cancer_hits = sum(1 for w in GENERIC_CANCER_TERMS + PANCREATIC_TERMS + GI_TERMS + list(SPECIFIC_NON_GI_TERMS) if w in tl)
    perturb_hits = sum(1 for w in PERTURBATION_SEARCH_TERMS if w.lower() in tl)
    phenotype_hits = sum(1 for w in PHENOTYPE_SEARCH_TERMS if w.lower() in tl)
    model_hits = sum(1 for w in MODEL_SEARCH_TERMS if w.lower() in tl)
    review_like = any(w in title_l for w in ["review", "meta-analysis", "systematic review"])
    expression_only_like = (
        any(w in tl for w in ["expression", "upregulated", "downregulated", "biomarker", "prognostic"])
        and perturb_hits == 0
    )

    score = 0.0
    score += min(0.28, 0.10 * gene_hits)
    score += min(0.18, 0.05 * cancer_hits)
    score += min(0.28, 0.09 * perturb_hits)
    score += min(0.18, 0.045 * phenotype_hits)
    score += min(0.12, 0.04 * model_hits)
    if gene_hits and (perturb_hits or phenotype_hits):
        score += 0.08
    if perturb_hits and phenotype_hits:
        score += 0.08
    if review_like:
        score -= 0.22
    if expression_only_like:
        score -= 0.08

    details = {
        "gene_hits": gene_hits,
        "cancer_hits": cancer_hits,
        "perturbation_hits": perturb_hits,
        "phenotype_hits": phenotype_hits,
        "model_hits": model_hits,
        "review_like": review_like,
        "expression_only_like": expression_only_like,
    }
    return round(max(0.0, min(1.0, score)), 3), details


def rank_candidate_pmids(gene: str, pmids: list[str], meta: dict) -> tuple[list[str], dict[str, float]]:
    scored = []
    for original_idx, pmid in enumerate(pmids):
        m = meta.get(str(pmid), {})
        score, _ = _paper_relevance_score(gene, m.get("title", ""), m.get("abstract", ""))
        # Preserve PubMed order as a tie-breaker.
        scored.append((score, -original_idx, str(pmid)))
    scored.sort(reverse=True)
    ranked = [pmid for _, _, pmid in scored]
    scores = {pmid: score for score, _, pmid in scored}
    return ranked, scores


def _build_overall_decision(gene: str, decision: dict,
                             confidence: float, cancer_type: str) -> str:
    """Build a plain-English explanation of the classification decision."""
    is_functional = decision.get("functional_study", False)
    reasoning     = decision.get("llm_reasoning", "").strip()

    methods = []
    if decision.get("knockout"):      methods.append("knockout")
    if decision.get("knockdown"):     methods.append("knockdown")
    if decision.get("shrna"):         methods.append("shRNA")
    if decision.get("sirna"):         methods.append("siRNA")
    if decision.get("crispr"):        methods.append("CRISPR")
    if decision.get("crispr_screen"): methods.append("CRISPR screen")

    locations = []
    if decision.get("in_vitro"): locations.append("in vitro")
    if decision.get("in_vivo"):  locations.append("in vivo")

    if is_functional:
        parts = [f"FUNCTIONAL STUDY of {gene} in {cancer_type} cancer."]
        if methods:
            parts.append(f"Perturbation: {', '.join(methods)}.")
        if locations:
            parts.append(f"Tested {' and '.join(locations)}.")
        parts.append(f"Evidence support: {confidence:.2f}.")
        if reasoning:
            parts.append(f"LLM: {reasoning}")
        return " ".join(parts)
    else:
        parts = [f"NOT a functional study of {gene}."]
        parts.append(f"Evidence support: {confidence:.2f}.")
        if reasoning:
            parts.append(f"LLM: {reasoning}")
        elif not methods:
            parts.append("No direct gene perturbation detected.")
        return " ".join(parts)


def analyze_gene(
    gene: str,
    max_papers: int = 300,
    *,
    force_pmids: list[str] | None = None,
    include_processed: bool = False,
    use_cache: bool = True,
) -> list[dict]:
    """Full pipeline for one gene. Returns list of row dicts (one per paper)."""
    _VERIFIER_STATE["calls"] = 0
    print(f"\n{'='*60}")
    print(f"  Analyzing gene: {gene}")
    print(f"{'='*60}")

    if force_pmids:
        pmids = [str(p) for p in force_pmids]
        print(f"  [Reprocess] Forced PMID list: {len(pmids)} paper(s)")
    else:
        pmids = pubmed_search_ids(gene, GENERIC_CANCER_TERMS + PANCREATIC_TERMS)
        if not include_processed:
            pmids = check_new_pmids(gene, pmids)

    # Fetch a wider candidate pool, rank it by lightweight evidence relevance,
    # and then apply max_papers. This makes each Colab run spend LLM time on
    # papers more likely to contain direct functional evidence.
    candidate_pool = pmids if force_pmids else pmids[: max(max_papers, min(len(pmids), max_papers * 3))]

    if not candidate_pool:
        print(f"  No new papers to process for {gene}.")
        return []

    meta = pubmed_fetch_metadata(candidate_pool)
    relevance_scores: dict[str, float] = {}
    if force_pmids:
        pmids = candidate_pool[:max_papers]
        relevance_scores = {
            str(pmid): _paper_relevance_score(
                gene,
                meta.get(str(pmid), {}).get("title", ""),
                meta.get(str(pmid), {}).get("abstract", ""),
            )[0]
            for pmid in pmids
        }
    else:
        pmids, relevance_scores = rank_candidate_pmids(gene, candidate_pool, meta)
        pmids = pmids[:max_papers]
        if pmids:
            top_score = relevance_scores.get(pmids[0], 0.0)
            bottom_score = relevance_scores.get(pmids[-1], 0.0)
            print(
                f"  [Ranking] Processing top {len(pmids)} candidate(s); "
                f"relevance range {top_score:.2f}-{bottom_score:.2f}"
            )
    rows = []

    for idx, pmid in enumerate(pmids):
        print(f"  [{idx+1}/{len(pmids)}] PMID {pmid}", end=" ")

        cached = cache_get(gene, pmid) if use_cache else None
        if cached is not None:
            if cached.get("_skip"):
                print("(cached skip)")
                _db_mark_skipped(gene, pmid, "cached skip")
                continue
            print("(cached)")
            rows.append(cached)
            continue

        m        = meta.get(str(pmid), {})
        title    = m.get("title",    "")
        abstract = m.get("abstract", "")

        if classify_cancer_type(f"{title} {abstract}") == "unknown":
            print("(skipped — not cancer)")
            cache_set(gene, pmid, {"_skip": True})
            _db_mark_skipped(gene, pmid, "not cancer")
            continue

        print("(processing …)")

        fulltext, pmcid = fetch_pmc_fulltext(pmid)
        ev = extract_evidence_pack(gene, abstract, fulltext)
        ev["pmcid"] = pmcid or ""
        ev["has_fulltext_context"] = bool(fulltext)
        ev["search_relevance_score"] = relevance_scores.get(str(pmid), 0.0)
        ev["publication_types"] = m.get("publication_types", "")
        ev["paper_type"] = classify_paper_type(
            title=title,
            abstract=abstract,
            publication_types=m.get("publication_types", ""),
            evidence=ev,
        )

        decision = ensemble_classify(gene, title, abstract, ev["evidence_text"], ev)

        conf_functional     = decision["conf_functional"]
        conf_not_functional = decision["conf_not_functional"]
        confidence = (
            conf_functional if decision.get("functional_study")
            else conf_not_functional
        )

        # Cancer type: LLM first, fallback to evidence-based keyword classifier
        llm_cancer_type = (decision.get("_llm_result") or {}).get("cancer_type")

        if llm_cancer_type:
            cancer_type = llm_cancer_type
        else:
            # Use only experimental evidence sentences, not full background text
            experimental_text = " ".join(filter(None, [
                " ".join(ev.get("evidence_perturbation", [])),
                " ".join(ev.get("evidence_in_vivo",      [])),
                " ".join(ev.get("evidence_in_vitro",     [])),
            ]))
            cancer_type = (
                classify_cancer_type(experimental_text)
                if experimental_text.strip()
                else classify_cancer_type(f"{title} {abstract}")
            )

        row = {
            # User-facing export columns
            "gene":        gene,
            "pmid":        str(pmid),
            "pubmed_link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "title":       title,
            "journal":     m.get("journal", ""),
            "year":        m.get("year",    ""),
            "cancer_type": cancer_type,
            "publication_types": m.get("publication_types", ""),
            "paper_type": ev.get("paper_type", "unknown"),

            # Store as booleans/floats for DB — export_to_df converts to YES/NO and %
            "functional_study": bool(decision.get("functional_study")),
            "in_vitro":         bool(decision.get("in_vitro")),
            "in_vivo":          bool(decision.get("in_vivo")),
            "knockout":         bool(decision.get("knockout")),
            "knockdown":        bool(decision.get("knockdown")),
            "shrna":            bool(decision.get("shrna")),
            "sirna":            bool(decision.get("sirna")),
            "crispr":           bool(decision.get("crispr")),
            "crispr_screen":    bool(decision.get("crispr_screen")),

            "confidence": confidence,  # raw float for DB queries
            "best_evidence_quote":      ev.get("best_evidence_quote", ""),

            "evidence_functional_study": "|".join(ev.get("evidence_perturbation",  [])),
            "evidence_in_vitro":         "|".join(ev.get("evidence_in_vitro",      [])),
            "evidence_in_vivo":          "|".join(ev.get("evidence_in_vivo",       [])),
            "evidence_crispr_screen":    "|".join(ev.get("evidence_crispr_screen", [])),

            "overall_decision": _build_overall_decision(
                gene, decision, confidence, cancer_type
            ),

            # Internal DB columns (not in export but needed by db.py) 
            "pmcid":    pmcid or "",
            "pmc_link": f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/" if pmcid else "",
            "doi":      m.get("doi", ""),
            "where_functional": (
                "both"      if decision.get("in_vitro") and decision.get("in_vivo")
                else "in_vitro" if decision.get("in_vitro")
                else "in_vivo"  if decision.get("in_vivo")
                else "no"
            ),
            "confidence_functional":     conf_functional,
            "confidence_not_functional": conf_not_functional,
            "classified_by_llm":         bool(decision.get("llm_available")),
            "llm_rules_disagree":        bool(decision.get("llm_rules_disagree")),
            "rules_functional":          bool(decision.get("rules_functional")),
            "llm_reasoning":             decision.get("llm_reasoning", ""),
            "verification_status":        decision.get("verification_status", ""),
            "verification_reasons":       decision.get("verification_reasons", ""),
            "evidence_quality_score":     decision.get("evidence_quality_score", 0),
            "search_relevance_score":     relevance_scores.get(str(pmid), 0.0),
            "evidence_retrieval_score":   ev.get("evidence_retrieval_score", 0.0),
            "gene_match_quality":         decision.get("gene_match_quality", ""),
            "adjudication_status":         decision.get("adjudication_status", ""),
            "adjudication_reasons":        decision.get("adjudication_reasons", ""),
            "agentic_verifier_decision":    decision.get("agentic_verifier_decision", ""),
            "agentic_verifier_reason":      decision.get("agentic_verifier_reason", ""),
            "agentic_verifier_quote":       decision.get("agentic_verifier_quote", ""),
            "agentic_verifier_needs_review": bool(decision.get("agentic_verifier_needs_review")),
            "review_recommendation":      decision.get("review_recommendation", ""),
            "review_reasons":             decision.get("review_reasons", ""),
            "agent_trace":                decision.get("agent_trace", ""),
            "impact_in_vitro":           "|".join(str(x) for x in (decision.get("impact_in_vitro") or [])),
            "impact_in_vivo":            "|".join(str(x) for x in (decision.get("impact_in_vivo")  or [])),
            "total_evidence_sents":      ev.get("total_evidence_sents", 0),
            "gene_linked_evidence_sents": ev.get("gene_linked_evidence_sents", 0),
        }

        cache_set(gene, pmid, row)
        rows.append(row)

    _db_upsert_rows(rows)
    return rows


# SECTION 10: Evaluation module
def evaluate(labeled_csv: str, results_df: pd.DataFrame,
             label_col: str = "true_functional") -> dict:
    labels = pd.read_csv(labeled_csv)[["pmid", label_col]]
    labels["pmid"] = labels["pmid"].astype(str)

    merged = results_df.merge(labels, on="pmid", how="inner")
    if merged.empty:
        print("[Eval] No overlapping PMIDs between results and labels.")
        return {}

    y_true = merged[label_col].astype(int)
    y_pred = merged["functional_study"].apply(lambda x: 1 if (x is True or str(x).upper() == "YES") else 0)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1        = (2 * precision * recall / (precision + recall)
                 if (precision + recall) > 0 else 0.0)
    accuracy  = (tp + tn) / len(merged)

    tp_conf = merged[(y_pred == 1) & (y_true == 1)]["confidence"].mean()
    fp_conf = merged[(y_pred == 1) & (y_true == 0)]["confidence"].mean()

    errors = merged[y_pred != y_true]
    disagree_rate_on_errors = errors["llm_rules_disagree"].mean() if len(errors) else 0.0

    metrics = dict(
        n_labeled=len(merged),
        precision=round(precision, 3), recall=round(recall, 3),
        f1=round(f1, 3), accuracy=round(accuracy, 3),
        tp=tp, fp=fp, fn=fn, tn=tn,
        mean_confidence_tp=round(float(tp_conf) if not pd.isna(tp_conf) else 0, 3),
        mean_confidence_fp=round(float(fp_conf) if not pd.isna(fp_conf) else 0, 3),
        disagree_rate_on_errors=round(float(disagree_rate_on_errors), 3),
    )

    print("\n── Evaluation Results ────────────────────────")
    for k, v in metrics.items():
        print(f"  {k:<35} {v}")

    return metrics


def create_evaluation_template(results_df: pd.DataFrame,
                                n_sample: int = 50,
                                out_path: str = "eval_template.csv"):
    _fs = results_df["functional_study"].apply(lambda x: x is True or str(x).upper()=="YES")
    pred_true_high  = results_df[(_fs) &
                                  (results_df["confidence"] >= 0.6)].head(n_sample // 3)
    pred_true_low   = results_df[(_fs) &
                                  (results_df["confidence"] < 0.6)].head(n_sample // 3)
    pred_false      = results_df[~_fs].head(n_sample // 3)

    sample = pd.concat([pred_true_high, pred_true_low, pred_false]).drop_duplicates("pmid")
    sample = sample[["pmid", "gene", "title", "pubmed_link",
                      "functional_study", "confidence",
                      "evidence_functional_study", "llm_reasoning"]]
    sample["true_functional"] = ""

    sample.to_csv(out_path, index=False)
    print(f"[Eval] Template saved to {out_path}")
    return sample


# SECTION 11: Main run_pipeline entry point
def run_pipeline(genes: list[str],
                 max_papers: int = 300,
                 out_csv: str = OUTPUT_CSV) -> pd.DataFrame:
    if USE_LLM:
        _get_llm()

    all_rows = []
    for i, gene in enumerate(genes):
        print(f"\n[{i+1}/{len(genes)}] Gene: {gene}")
        try:
            if USE_LLM and torch.cuda.is_available():
                torch.cuda.empty_cache()
                gc.collect()

            rows = analyze_gene(gene, max_papers=max_papers)
            all_rows.extend(rows)

            _df = pd.DataFrame(all_rows)
            export_df = _df[[c for c in EXPORT_COLS if c in _df.columns]]
            export_df.to_csv(out_csv, index=False)
            print(f"  Saved {len(export_df)} rows to {out_csv}")

        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                print(f"  [OOM] Skipping {gene}")
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                gc.collect()
            else:
                print(f"  [Error] {gene}: {e}")
        except Exception as e:
            print(f"  [Error] {gene}: {e}")

    df = pd.DataFrame(all_rows)
    export_df = df[[c for c in EXPORT_COLS if c in df.columns]]
    export_df.to_csv(out_csv, index=False)
    print(f"\nDone. {len(df)} rows saved to {out_csv}")
    return df
