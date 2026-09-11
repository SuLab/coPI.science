"""Conflict-of-interest statements from PubMed records.

Reads only ``rec["coi_statement"]``: the real PubMed parser puts affiliations
per-author under ``rec["authors"][i]["affiliations"]``, not as a top-level
list of strings, and company co-authors are already captured via OpenAlex
(``openalex_industry.evidence_from_work``)."""
import re

from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

_NEG = re.compile(r"\b(no|none|declare[sd]? no|not have any|have no)\b.*\b(conflict|competing|interest)", re.I)
# Each word of the candidate name must itself be capitalized (Title Case),
# so the lazy match cannot stretch backward across ordinary lowercase prose
# ("... are employees of Paratek Pharmaceuticals") to an earlier capital
# letter — it anchors on the capitalized word immediately before the suffix.
_COMPANY_SUFFIX = re.compile(
    r"\b([A-Z][\w&.\-']*(?:\s+[A-Z][\w&.\-']*){0,5}?"
    r"(?:,? Inc\.?|,? LLC|,? Ltd\.?|,? GmbH|,? AG|,? S\.?A\.?|,? Corp\.?|,? Co\.?|"
    r" Pharmaceuticals?| Therapeutics| Biosciences| Biotech\w*| Bio\b| Diagnostics))",
    re.M,
)
_REL = [
    ("founder", re.compile(r"\b(co-?founder|founded)\b", re.I)),
    ("equity", re.compile(r"\b(equity|shareholder|stock|shares|ownership)\b", re.I)),
    ("employee", re.compile(r"\b(employees?|employed by|employment)\b", re.I)),
    ("consultant", re.compile(r"\b(consult\w*|advisory board|scientific advisor|SAB|honorari\w*|speaker)\b", re.I)),
    ("inventor", re.compile(r"\b(patent|inventor|licens\w*|royalt\w*)\b", re.I)),
    ("funding", re.compile(r"\b(research (support|funding|grant)|sponsored|funded by)\b", re.I)),
]

# Guard sentence-splitting against initials ("Daniel H.") and common
# abbreviations ("Inc.") so a mid-name/mid-company period is not mistaken for
# a sentence boundary — without this, "Daniel H. Deck ... employees of
# Paratek Pharmaceuticals, Inc. All authors vouch ..." splits before the
# company name ever appears in the same sentence as "employees".
_PROTECT = re.compile(r"\b([A-Z]|Inc|Corp|Ltd|Co|LLC|Dr|Mr|Mrs|Ms|Jr|Sr|St|vs|etc|GmbH|AG)\.")
_PLACEHOLDER = ""


def _sentences(text: str) -> list[str]:
    protected = _PROTECT.sub(lambda m: m.group(1) + _PLACEHOLDER, text or "")
    parts = re.split(r"(?<=[.;])\s+", protected)
    return [p.replace(_PLACEHOLDER, ".").strip() for p in parts if p.strip()]


def evidence_from_record(rec: dict, *, pi_year_ok: bool) -> list[EvidenceItem]:
    if not pi_year_ok:
        return []
    items: list[EvidenceItem] = []
    pmid = str(rec.get("pmid"))
    year = rec.get("year")
    seen: set[str] = set()
    for sent in _sentences(rec.get("coi_statement") or ""):
        if _NEG.search(sent):
            continue
        companies = [m.group(1).strip(" ,.") for m in _COMPANY_SUFFIX.finditer(sent)]
        if not companies:
            continue
        rel = next((name for name, rx in _REL if rx.search(sent)), "other")
        for c in companies:
            key = f"{pmid}:{c.lower()}"
            if key in seen:
                continue
            seen.add(key)
            items.append(EvidenceItem("pubmed", "coi_relationship", key, c, None, classify_company(c, "company"), year, None, True,
                                      {"relationship": rel, "span": sent[:400], "pmid": pmid}))
    return items
