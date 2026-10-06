"""Conflict-of-interest statements from PubMed records, as industry evidence (spec
2026-10-05 §6.2, P4, NEW-1, D10).

Reads `rec["coi_statement"]` and the record's author list. A company the statement names
(a capitalised name ending in a corporate suffix, `_COMPANY_SUFFIX`) is credited to the PI
only when the run of named persons nearest before it, inside its clause, names the PI
(located on the record by `coi_founders.locate_pi`, then written as initials, surname or
full name) or says "all/each author(s)" (`coi_attribution.Statement.credit`). A company
whose nearest run names other authors only, or no person at all ("The authors declare…"),
credits nothing. A clause that is a pure negative disclosure ("…has no competing
interests") credits nothing unless it also states a relationship.

Matching runs on the sentence's normalised form (`coi_attribution.norm`: NFKC,
typographic dashes and quotes folded, whitespace collapsed; then `accent_free`), and the
suffix alternation ends in `(?!\\w)`, so " Co", " Corp" and " SA" no longer match inside
Consortium, Corporate or SAIL (ported from the final fix wave of
`feat/pi-external-enrichment`). Each row records the run (`attribution`) and the PI's own
mention in it (`pi_mention`), with `attributed: true`; the scorer counts only such rows
(`industry_score`). Company co-authors come from OpenAlex (`openalex_industry`)."""
from __future__ import annotations

import re

from src.services.coi_attribution import Statement, accent_free, norm
from src.services.company_sources import PiName
from src.services.company_sources.coi_founders import locate_pi
from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.companies import classify_company

_NEG = re.compile(r"\b(no|none|declare[sd]? no|not have any|have no)\b.*\b(conflict|competing|interest)", re.I)
# Each word of the candidate name must itself be capitalized (Title Case), so the lazy
# match cannot stretch backward across ordinary lowercase prose ("... are employees of
# Paratek Pharmaceuticals") to an earlier capital letter — it anchors on the capitalized
# word immediately before the suffix.
#
# The trailing `(?!\w)` is load-bearing: without it the short suffixes match INSIDE longer
# words, so "Johns Hopkins Consortium on Aging" became the company "Johns Hopkins Co",
# "the NIH SAIL trial" became "NIH SA", and "Mount Sinai Corporate Education" became
# "Mount Sinai Corp" — academic prose manufactured into industry evidence. It is a
# lookahead rather than `\b` so that a trailing period ("Paratek Pharmaceuticals, Inc.")
# still matches: `\b` after `\.?` would require a word character next.
_COMPANY_SUFFIX = re.compile(
    r"\b([A-Z][\w&.\-']*(?:\s+[A-Z][\w&.\-']*){0,5}?"
    r"(?:,? Inc\.?|,? LLC|,? Ltd\.?|,? GmbH|,? AG|,? S\.?A\.?|,? Corp\.?|,? Co\.?|"
    r" Pharmaceuticals?| Therapeutics| Biosciences| Biotech\w*| Bio\b| Diagnostics)(?!\w))",
    re.M,
)
_REL = [
    ("founder", re.compile(r"\b(co-?)?(founders?|founded)\b", re.I)),
    ("equity", re.compile(r"\b(equity|shareholder|stock|shares|ownership)\b", re.I)),
    ("employee", re.compile(r"\b(employees?|employed by|employment)\b", re.I)),
    ("consultant", re.compile(r"\b(consult\w*|advisory board|scientific advisor|SAB|honorari\w*|speaker)\b", re.I)),
    ("inventor", re.compile(r"\b(patent|inventor|licens\w*|royalt\w*)\b", re.I)),
    ("funding", re.compile(r"\b(research (support|funding|grant)|sponsored|funded by)\b", re.I)),
]
#: How much of the enclosing sentence a row keeps as `span`.
SPAN_CHARS = 400


def _relationship(near: str, clause: str) -> str | None:
    """The relationship ("founder", "equity", …) whose wording ends nearest the company in
    `near`, the words from the PI's run to the company ("G.L. is a co-founder of Acme Bio
    and a consultant for Beta Therapeutics" gives Beta "consultant"; "G.L. is a consultant
    for Acme Inc and J.S. is an employee of Beta Inc" never lends J.S.'s "employee" to
    Acme); else the first the clause states; "other" when neither states one; None for a
    clause that is a pure negative disclosure stating none."""
    rel = max(((m.end(), name) for name, rx in _REL for m in rx.finditer(near)), default=(0, None))[1]
    if rel is None:
        rel = next((name for name, rx in _REL if rx.search(clause)), None)
    if rel is None and _NEG.search(clause):
        return None
    return rel or "other"


def evidence_from_record(
    rec: dict, *, pi_year_ok: bool, pi: PiName | None, year: int | None = None,
) -> list[EvidenceItem]:
    """The COI relationships `rec`'s statement credits to `pi` (module docstring). [] when
    the year gate fails, there is no usable name or statement, or the PI is not located on
    the record (absent, matched twice, or an initials collision). `year` defaults to the
    record's own; the job passes the year its tenure gate used."""
    statement = (rec.get("coi_statement") or "").strip()
    if not pi_year_ok or pi is None or not statement:
        return []
    forms = locate_pi(rec, pi)
    if forms is None:
        return []
    prepared = Statement.of(statement, forms, rec.get("authors") or [], count_all_authors=True)
    pmid = str(rec.get("pmid"))
    year = rec.get("year") if year is None else year
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    for start, end in prepared.spans:
        sentence = statement[start:end]
        shown = norm(sentence)
        text = accent_free(shown)
        for match in _COMPANY_SUFFIX.finditer(text):
            company = (match.start(1), match.end(1))
            credit = prepared.credit(text, company)
            if credit is None:
                continue
            run_start, run_end, mine_start, mine_end = credit
            clause_start, clause_end = prepared.clause(text, company)
            rel = _relationship(text[run_start:company[1]], text[clause_start:clause_end])
            if rel is None:
                continue
            name = shown[company[0]:company[1]].strip(" ,.")
            key = f"{pmid}:{name.lower()}"
            if key in seen:
                continue
            seen.add(key)
            items.append(EvidenceItem(
                "pubmed", "coi_relationship", key, name, None, classify_company(name, "company"),
                year, None, True,
                {"relationship": rel, "span": sentence[:SPAN_CHARS], "pmid": pmid,
                 "attributed": True, "attribution": shown[run_start:run_end],
                 "pi_mention": shown[mine_start:mine_end]},
            ))
    return items
