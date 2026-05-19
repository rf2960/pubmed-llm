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

# Entrez
Entrez.email       = "ssd2184@columbia.edu"
Entrez.tool        = "functional-study-db"
Entrez.sleep_between_tries = True
Entrez.sleep_duration      = 0.4

# LLM Model
HF_MODEL   = "BioMistral/BioMistral-7B"
USE_LLM    = True
QUANTIZE   = "8bit"

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

# Export columns for user-facing CSV
EXPORT_COLS = [
    "gene", "pmid", "pubmed_link", "title", "journal", "year",
    "cancer_type", "functional_study", "in_vitro", "in_vivo",
    "knockout", "knockdown", "shrna", "sirna", "crispr", "crispr_screen",
    "confidence", "evidence_functional_study", "evidence_in_vitro",
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

# Confidence weights
CONFIDENCE_WEIGHTS = {
    "gene_specific_perturbation": 0.30,
    "in_vitro_phenotype":         0.20,
    "in_vivo_phenotype":          0.25,
    "both_vitro_and_vivo":        0.10,
    "crispr_screen":              0.10,
    "llm_rules_agree":            0.15,
    "multiple_evidence_sents":    0.05,
}

NOT_FUNCTIONAL_WEIGHTS = {
    "expression_only":        0.30,
    "correlation_only":       0.25,
    "no_evidence_sents":      0.20,
    "review_paper":           0.20,
    "llm_and_rules_agree_no": 0.20,
    "classified_by_llm":      0.05,
}


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


def pubmed_search_ids(gene: str, extra_terms: list[str], batch_size=2000) -> list[str]:
    gene_q = f'("{gene}"[Title/Abstract] OR "{gene}"[All Fields])'
    kw_q   = " OR ".join(f'"{k}"[Title/Abstract]' for k in extra_terms)
    term   = f"({gene_q} AND ({kw_q}))"

    with entrez_call(Entrez.esearch, db="pubmed", term=term, retmax=0, retmode="xml") as h:
        total = int(Entrez.read(h)["Count"])
    print(f"  [PubMed] {gene}: {total} hits")

    pmids = []
    for start in range(0, total, batch_size):
        with entrez_call(Entrez.esearch, db="pubmed", term=term,
                         retstart=start, retmax=batch_size, retmode="xml") as h:
            pmids.extend(Entrez.read(h)["IdList"])
    return pmids


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

                doi = ""
                try:
                    for idobj in art["PubmedData"]["ArticleIdList"]:
                        if idobj.attributes.get("IdType") == "doi":
                            doi = str(idobj)
                            break
                except Exception:
                    pass

                out[pmid] = dict(title=title, abstract=abstract,
                                 journal=journal, year=year, doi=doi)
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


def _sentence_is_evidence(gene: str, sent: str) -> bool:
    if not sent:
        return False
    g = gene.lower()
    s = sent.lower()
    if not re.search(rf"\b{re.escape(g)}\b", s):
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


def extract_evidence_pack(gene: str, abstract: str, fulltext: str,
                           max_sents=40, neighbors=1) -> dict:
    abs_sents  = split_sentences(abstract or "")
    full_sents = split_sentences(fulltext or "")
    all_sents  = abs_sents + full_sents

    if not all_sents:
        return dict(evidence_perturbation=[], evidence_in_vitro=[],
                    evidence_in_vivo=[], evidence_crispr_screen=[],
                    evidence_text="", total_evidence_sents=0)

    keep_idx = set()
    for i, s in enumerate(all_sents):
        if _sentence_is_evidence(gene, s):
            for j in range(max(0, i - neighbors), min(len(all_sents), i + neighbors + 1)):
                keep_idx.add(j)

    seen, kept = set(), []
    for s in (all_sents[i] for i in sorted(keep_idx)):
        if s not in seen:
            kept.append(s)
            seen.add(s)

    g_lower = gene.lower()
    ev_pert, ev_vitro, ev_vivo, ev_screen = [], [], [], []
    for s in kept:
        sl = s.lower()
        if (any(p.search(s) for p in [PATTERNS["knockout"], PATTERNS["knockdown"], PATTERNS["crispr"]])
                or re.search(rf"\bsh[-_\s]*{re.escape(g_lower)}\b", sl)
                or re.search(rf"\bsi[-_\s]*{re.escape(g_lower)}\b", sl)):
            ev_pert.append(s)
        if PATTERNS["in_vitro"].search(s) or any(w in sl for w in
                ["growth", "proliferation", "viability", "apoptosis", "necrosis",
                 "cell death", "organoid"]):
            ev_vitro.append(s)
        if PATTERNS["in_vivo"].search(s) or any(w in sl for w in
                ["xenograft", "tumor size", "tumour size", "tumor volume",
                 "tumour volume", "tumor growth", "survival", "kaplan-meier"]):
            ev_vivo.append(s)
        if PATTERNS["crispr_screen"].search(s):
            ev_screen.append(s)

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
        torch_dtype=torch.float16,
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


# SECTION 6: Ensemble + calibrated confidence score 
def compute_confidence(gene, llm_result, rules_result, ev, title="", abstract=""):
    primary = llm_result if llm_result is not None else rules_result
    text    = (abstract or "").lower()
    t_lower = (title or "").lower()

    pos_signals = {
        "gene_specific_perturbation": bool(
            ev.get("evidence_perturbation") and primary.get("functional_study")),
        "in_vitro_phenotype":  bool(primary.get("in_vitro")),
        "in_vivo_phenotype":   bool(primary.get("in_vivo")),
        "both_vitro_and_vivo": bool(primary.get("in_vitro") and primary.get("in_vivo")),
        "crispr_screen":       bool(primary.get("crispr_screen")),
        "llm_rules_agree":     (
            llm_result is not None and
            llm_result.get("functional_study") == True and
            rules_result.get("functional_study") == True
        ),
        "multiple_evidence_sents": ev.get("total_evidence_sents", 0) > 3,
    }

    expression_words = ["expression", "mrna level", "protein level",
                        "upregulated", "downregulated", "overexpressed"]
    correlation_words = ["associated with", "correlated with", "prognostic",
                         "biomarker", "signature", "survival analysis",
                         "kaplan-meier", "hazard ratio"]
    review_words = ["review", "meta-analysis", "systematic review",
                    "literature review", "commentary"]

    has_expression_only = (
        any(w in text for w in expression_words) and
        not ev.get("evidence_perturbation")
    )
    has_correlation_only = (
        any(w in text for w in correlation_words) and
        not primary.get("functional_study")
    )
    is_review = any(w in t_lower for w in review_words)

    neg_signals = {
        "expression_only":        has_expression_only,
        "correlation_only":       has_correlation_only,
        "no_evidence_sents":      ev.get("total_evidence_sents", 0) == 0,
        "review_paper":           is_review,
        "llm_and_rules_agree_no": (
            llm_result is not None and
            llm_result.get("functional_study") == False and
            rules_result.get("functional_study") == False
        ),
        "classified_by_llm": llm_result is not None,
    }

    conf_functional     = sum(CONFIDENCE_WEIGHTS[k]     for k, v in pos_signals.items() if v)
    conf_not_functional = sum(NOT_FUNCTIONAL_WEIGHTS[k] for k, v in neg_signals.items() if v)

    conf_functional     = round(min(1.0, conf_functional),     3)
    conf_not_functional = round(min(1.0, conf_not_functional), 3)

    if (llm_result is not None and
            llm_result.get("functional_study") != rules_result.get("functional_study")):
        conf_functional     = min(conf_functional,     0.60)
        conf_not_functional = min(conf_not_functional, 0.60)

    if llm_result is None:
        conf_functional     = min(conf_functional,     0.65)
        conf_not_functional = min(conf_not_functional, 0.65)

    return conf_functional, conf_not_functional, pos_signals, neg_signals


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

    conf_functional, conf_not_functional, pos_signals, neg_signals = compute_confidence(
        gene, llm_result, rules_result, ev, title, abstract
    )

    confidence = conf_functional if primary.get("functional_study") else conf_not_functional
    if llm_rules_disagree:
        confidence = min(confidence, 0.60)

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
    new_pmids = []
    for pmid in pmids:
        if str(pmid) in processed_pmids:
            continue
        cached = cache_get(gene, pmid)
        if cached is None:
            new_pmids.append(pmid)
    return new_pmids


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
        parts.append(f"Confidence: {round(confidence * 100)}%.")
        if reasoning:
            parts.append(f"LLM: {reasoning}")
        return " ".join(parts)
    else:
        parts = [f"NOT a functional study of {gene}."]
        parts.append(f"Confidence: {round(confidence * 100)}%.")
        if reasoning:
            parts.append(f"LLM: {reasoning}")
        elif not methods:
            parts.append("No direct gene perturbation detected.")
        return " ".join(parts)


def analyze_gene(gene: str, max_papers: int = 300) -> list[dict]:
    """Full pipeline for one gene. Returns list of row dicts (one per paper)."""
    print(f"\n{'='*60}")
    print(f"  Analyzing gene: {gene}")
    print(f"{'='*60}")

    pmids = pubmed_search_ids(gene, GENERIC_CANCER_TERMS + PANCREATIC_TERMS)
    pmids = check_new_pmids(gene, pmids)
    pmids = pmids[:max_papers]

    if not pmids:
        print(f"  No new papers to process for {gene}.")
        return []

    meta = pubmed_fetch_metadata(pmids)
    rows = []

    for idx, pmid in enumerate(pmids):
        print(f"  [{idx+1}/{len(pmids)}] PMID {pmid}", end=" ")

        cached = cache_get(gene, pmid)
        if cached is not None:
            if cached.get("_skip"):
                print("(cached skip)")
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
            "impact_in_vitro":           "|".join(str(x) for x in (decision.get("impact_in_vitro") or [])),
            "impact_in_vivo":            "|".join(str(x) for x in (decision.get("impact_in_vivo")  or [])),
            "total_evidence_sents":      ev.get("total_evidence_sents", 0),
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
