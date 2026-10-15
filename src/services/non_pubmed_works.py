"""Non-PubMed works for a PubMed-thin PI (spec 2026-10-09 §5, Phase B).

The corpus (``corpus.py``) is PubMed-only: every row it stores carries a PMID, so a PI whose
work lives in conference proceedings, arXiv or astrophysics journals had nothing stored and a
profile written from the model's own knowledge. This module turns the works a resolve already
fetched — the PI's ORCID list (S1, ``CorpusResult.orcid_works``) and OpenAlex's works for the
ORCID (S2, ``CorpusResult.openalex_works``) — into storable records for the works PubMed does
not have. The pipeline stores them only for a PubMed-thin PI (``tenure_scope.pubmed_thin``),
through ``corpus_additions.apply_non_pubmed_works``.

Owner decisions (spec §2): D5, store DOI-bearing works with OpenAlex abstracts; D6, store
ORCID-listed works and OpenAlex's works for the ORCID without staff review. D6 as amended
after the 2026-10-09 rollout, where 25 of one PI's 38 OpenAlex-only works were other
people's: a work only OpenAlex attributes to the ORCID is stored only when the ORCID's own
authorship on it names Johns Hopkins or an organization on the PI's ORCID record (its name
as one run of words, in order), or the publisher's metadata carries the ORCID. The match is
by name, so a namesake at a similarly named institution can still pass.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from src.models.publication import PROVENANCE_OPENALEX
from src.services.corpus import CorpusResult, _normalize_title
from src.services.jhu_rules import is_hopkins_affiliation
from src.services.openalex import fetch_orcid_authorships, fetch_work_by_doi
from src.services.pubmed import _doi_fold, normalize_doi

#: OpenAlex ``type`` values kept (spec §5.2).
OPENALEX_KEPT_TYPES = frozenset(
    {"article", "preprint", "review", "conference-paper", "book", "book-chapter"}
)
#: ORCID work ``type`` values kept (spec §5.2).
ORCID_KEPT_TYPES = frozenset(
    {"journal-article", "conference-paper", "preprint", "book", "book-chapter"}
)
#: publications.doi and publications.journal are varchar(255).
_COLUMN_WIDTH = 255
#: A stored title is one line of at most this many characters: whoever registered a DOI
#: wrote it, and it reaches the persona and the synthesis prompt (security review
#: 2026-10-09).
_TITLE_MAX = 500
#: At most this many OpenAlex lookups per resolve, newest ORCID works first (security and
#: semantic review 2026-10-09). An ORCID-listed work past the cap is kept from ORCID's own
#: metadata, as one OpenAlex does not know is.
MAX_DOI_LOOKUPS = 50

#: Words that name no particular institution, left out when two names are compared.
_GENERIC_WORDS = frozenset("""
    an and at de del della der des di do du el en et for fur in la le les of the und van von
    university universitat universite universidad universidade universita universiteit univ
    college institute institut instituto istituto institution school faculty department dept
    division section unit program programme graduate center centre centrum zentrum hospital
    hopital clinic clinical medical medicine health research science sciences laboratory
    laboratories lab foundation inc ltd llc gmbh corporation corp company co group campus
""".split())

#: Generic words that name the same kind of place across languages and abbreviations: two
#: words of one class are equal inside a run of words (spec 2026-10-15 R3.9).
_GENERIC_CLASSES = (
    frozenset({"university", "universitat", "universite", "universidad", "universidade",
               "universita", "universiteit", "univ"}),
    frozenset({"institute", "institut", "instituto", "istituto"}),
    frozenset({"center", "centre", "centrum", "zentrum"}),
    frozenset({"hospital", "hopital"}),
    frozenset({"laboratory", "laboratories", "lab"}),
    frozenset({"corporation", "corp"}),
    frozenset({"program", "programme"}),
    frozenset({"department", "dept"}),
    frozenset({"science", "sciences"}),
)
_WORD_CLASS = {word: n for n, words in enumerate(_GENERIC_CLASSES) for word in words}

Lookup = Callable[[str], Awaitable[dict[str, Any] | None]]
AuthorshipLookup = Callable[[str], Awaitable[Mapping[str, dict[str, Any] | None]]]


async def collect_non_pubmed_works(
    result: CorpusResult, *, lookup: Lookup | None = None,
    enriched_dois: frozenset[str] = frozenset(), pi_orcid: str | None = None,
    institutions: Sequence[str] = (), authorship_lookup: AuthorshipLookup | None = None,
    unattributed: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The storable non-PubMed works of one resolve, newest first. Each record is
    ``{"doi", "title", "abstract", "journal", "year", "provenance"}``: ``doi`` in its source's
    spelling (ORCID's when the PI lists the work), ``provenance`` "s1" (ORCID-listed), "s1,s2"
    (ORCID-listed and in OpenAlex's list) or "openalex" (OpenAlex only).

    Skipped: works with a PMID or a DOI PubMed has (``result.pubmed_dois``), works with no DOI
    or no title, retracted works, and works of no kept type (the OpenAlex type decides when
    OpenAlex has one, else the ORCID type: ORCID users label errata as journal articles).
    One OpenAlex singleton lookup (free) per ORCID-listed DOI the S2 list lacks, through
    ``lookup`` (default ``fetch_work_by_doi``), newest first and at most MAX_DOI_LOOKUPS; a
    lookup failure other than "no such DOI" RAISES. ``enriched_dois`` (folded) are the PI's
    PMID-less rows that already carry an abstract: they are not looked up again, and an
    ORCID-listed one the S2 list lacks yields no record, so the cap goes to works not yet
    enriched and ORCID's text never overwrites OpenAlex's.

    An "openalex" work is kept only when the ORCID's own authorship on it is attributed
    (``_attributed``): it names Johns Hopkins or one of ``institutions`` (the organization
    names on the PI's ORCID record; its name as one run of words, in order), or the
    publisher's metadata carries ``pi_orcid``. The authorships come from one
    ``authorship_lookup(pi_orcid)`` (default ``fetch_orcid_authorships``, one charged request per page), made only when such a work
    exists; its failure RAISES. With no ``pi_orcid`` no "openalex" work is kept. The DOIs of
    the works dropped this way are appended to ``unattributed`` when it is given."""
    lookup = lookup or fetch_work_by_doi
    s2 = _by_doi(result.openalex_works, result.pubmed_dois)
    listed = _by_doi(result.orcid_works, result.pubmed_dois)
    looked_up: dict[str, dict[str, Any] | None] = {}
    missing = sorted(
        (fold for fold in listed if fold not in s2 and fold not in enriched_dois),
        key=lambda fold: (-(listed[fold].get("year") or 0), fold),
    )
    for fold in missing[:MAX_DOI_LOOKUPS]:
        looked_up[fold] = await lookup(listed[fold]["doi"])
    authorships: dict[str, dict[str, Any] | None] | None = None
    records = []
    for fold in sorted({*s2, *listed}):
        if fold in enriched_dois and fold not in s2:
            continue  # stored with an abstract; nothing new without a lookup
        record = _record(listed.get(fold), s2.get(fold), looked_up.get(fold))
        if record is None:
            continue
        if record["provenance"] == PROVENANCE_OPENALEX:
            if authorships is None:
                authorships = await _authorships(pi_orcid, authorship_lookup)
            if not _attributed(authorships.get(fold), institutions):
                if unattributed is not None:
                    unattributed.append(record["doi"])
                continue
        records.append(record)
    return _newest_first(_one_per_title(records))


async def _authorships(
    pi_orcid: str | None, authorship_lookup: AuthorshipLookup | None,
) -> dict[str, dict[str, Any] | None]:
    """The PI's authorships by folded DOI; empty without an ORCID."""
    if not pi_orcid:
        return {}
    found = await (authorship_lookup or fetch_orcid_authorships)(pi_orcid)
    out: dict[str, dict[str, Any] | None] = {}
    for doi, authorship in found.items():
        if doi := normalize_doi(doi):
            out.setdefault(_doi_fold(doi), authorship)
    return out


def attribution_verdict(
    authorship: Mapping[str, Any] | None, organizations: Sequence[str],
) -> Literal["attributed", "unattributed", "unknown"]:
    """Whether the PI's authorship on a work (``openalex.fetch_orcid_authorships``) ties the
    work to the PI. ``"unknown"`` when there is no authorship: no entry for the work, or an
    author list (capped at 100 in a list response) without the PI. ``"attributed"`` when the
    publisher's metadata carries the ORCID, an institution name or raw affiliation names
    Johns Hopkins, or one names an organization on the PI's ORCID record
    (``_names_organization``); otherwise ``"unattributed"``. Phase 4's re-check excludes on
    ``"unattributed"`` and leaves ``"unknown"`` alone (spec 2026-10-15 C4)."""
    if authorship is None:
        return "unknown"
    if authorship.get("orcid_in_metadata"):
        return "attributed"
    names = authorship.get("institutions") or []
    raw = authorship.get("raw_affiliations") or []
    if any(is_hopkins_affiliation(text) for text in (*names, *raw) if isinstance(text, str)):
        return "attributed"
    if any(_names_organization(org, names, raw) for org in organizations):
        return "attributed"
    return "unattributed"


def _attributed(authorship: Mapping[str, Any] | None, institutions: Sequence[str]) -> bool:
    """``attribution_verdict(...) == "attributed"``: at admission no authorship means the
    work is not stored."""
    return attribution_verdict(authorship, institutions) == "attributed"


def _run_words(text: object) -> list[str]:
    """``_words(text)`` without a leading "the"."""
    words = _words(text)
    return words[1:] if words[:1] == ["the"] else words


def _same_word(a: str, b: str) -> bool:
    """Equal, or two generic words of one ``_GENERIC_CLASSES`` class ("univ" = "university")."""
    return a == b or (a in _WORD_CLASS and _WORD_CLASS.get(b) == _WORD_CLASS[a])


def _distinctive(words: list[str]) -> list[str]:
    return [word for word in words if word not in _GENERIC_WORDS]


def _significant_run(words: list[str]) -> list[str]:
    """``words`` from the first distinctive one on, later generic words kept: "University of
    California San Diego" -> california san diego; "New York University" unchanged."""
    for n, word in enumerate(words):
        if word not in _GENERIC_WORDS:
            return words[n:]
    return []


def _rotated_run(run: list[str]) -> list[str] | None:
    """``run`` with its last distinctive word moved to the front, the rest in order
    ("Children's Hospital Boston" -> boston childrens hospital); None below two
    distinctive words (F11)."""
    positions = [n for n, word in enumerate(run) if word not in _GENERIC_WORDS]
    if len(positions) < 2:
        return None
    last = positions[-1]
    return [run[last], *run[:last], *run[last + 1:]]


def _holds_run(text_words: list[str], run: list[str]) -> bool:
    """Whether ``run`` occurs as consecutive words of ``text_words`` (``_same_word``)."""
    size = len(run)
    return size > 0 and any(
        all(_same_word(text_words[start + k], run[k]) for k in range(size))
        for start in range(len(text_words) - size + 1)
    )


def _names_organization(
    organization: str, names: Sequence[str], raw: Sequence[str],
) -> bool:
    """Whether an OpenAlex institution name (``names``) or a raw affiliation string names
    ``organization`` (Q11, F11). The organization's significant run of words must occur in
    the text as one run, in order: "New York University" no longer matches "Columbia
    University, New York, NY". A one-distinctive-word organization must occur as its whole
    name ("University of Washington" never matches "Washington University"). With two or
    more distinctive words the run may also occur with its last distinctive word first
    ("Boston Children's Hospital" for "Children's Hospital Boston"). An institution name
    whose own significant run has two or more distinctive words, and equals the
    organization's leading words, also matches, as OpenAlex's English short name begins a
    long native one ("Max Delbrück Center", "Max Delbrück Centrum für Molekulare Medizin
    Berlin Buch"); the rotation does not apply there."""
    words = _run_words(organization)
    distinctive = _distinctive(words)
    if not distinctive:
        return False
    run = words if len(distinctive) == 1 else _significant_run(words)
    rotated = _rotated_run(run)
    for text in (*names, *raw):
        found = _run_words(text)
        if _holds_run(found, run) or (rotated is not None and _holds_run(found, rotated)):
            return True
    for name in names:
        prefix = _significant_run(_run_words(name))
        if (
            len(_distinctive(prefix)) >= 2
            and len(prefix) <= len(run)
            and all(_same_word(a, b) for a, b in zip(prefix, run, strict=False))
        ):
            return True
    return False


def _words(text: object) -> list[str]:
    """The lower-case ASCII words of ``text``, in order, of two or more characters: accents and
    apostrophes dropped ("Delbrück" -> "delbruck", "Children’s" and "Children's" ->
    "childrens"), single letters (initials) left out; none for anything but text."""
    if not isinstance(text, str):
        return []
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.replace("'", "").lower())
    return [word for word in words if len(word) > 1]


def _by_doi(
    works: Iterable[dict[str, Any]], pubmed_dois: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Works with a DOI and no PMID whose DOI PubMed does not have, by folded DOI (the first
    of a repeated DOI wins), each with its DOI normalised."""
    out: dict[str, dict[str, Any]] = {}
    for work in works:
        doi = normalize_doi(work.get("doi"))
        if not doi or work.get("pmid") or len(doi) > _COLUMN_WIDTH:
            continue
        fold = _doi_fold(doi)
        if fold not in pubmed_dois:
            out.setdefault(fold, {**work, "doi": doi})
    return out


def _first(*values: Any) -> Any:
    """The first value that is neither None nor blank text, stripped when it is text."""
    for value in values:
        if isinstance(value, str):
            if value.strip():
                return value.strip()
        elif value is not None:
            return value
    return None


def _one_line(limit: int, *values: Any) -> str | None:
    """The first non-blank text value collapsed to single spaces and cut to ``limit``."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:limit]
    return None


def _record(
    orcid_work: dict[str, Any] | None, s2_work: dict[str, Any] | None,
    looked_up: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """One storable record, or None when the work must be skipped."""
    oa = s2_work or looked_up or {}
    if oa.get("pmid") or oa.get("is_retracted"):
        return None  # PubMed has it after all, or it was retracted
    listed = orcid_work or {}
    oa_type = oa.get("type")
    kept = (oa_type in OPENALEX_KEPT_TYPES) if oa_type else (
        listed.get("type") in ORCID_KEPT_TYPES
    )
    if not kept:
        return None
    title = _one_line(_TITLE_MAX, oa.get("title"), listed.get("title"))
    if title is None:
        return None
    return {
        "doi": (orcid_work or s2_work or {}).get("doi") or oa.get("doi"),
        "title": title,
        "abstract": _first(oa.get("abstract")),
        "journal": _one_line(_COLUMN_WIDTH, oa.get("venue"), listed.get("journal")),
        "year": _first(oa.get("year"), listed.get("year")),
        "provenance": _provenance(orcid_work is not None, s2_work is not None),
    }


def _provenance(in_orcid: bool, in_s2: bool) -> str:
    if in_orcid:
        return "s1,s2" if in_s2 else "s1"
    return PROVENANCE_OPENALEX


def _rank(rec: dict[str, Any]) -> tuple:
    """Smaller is better: the later year, then an ORCID-listed record, then the lower DOI."""
    return (-(rec.get("year") or 0), rec["provenance"] == PROVENANCE_OPENALEX,
            _doi_fold(rec["doi"]))


def _one_per_title(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per normalised title (spec §5.2): the published version over its preprint."""
    best: dict[str, dict[str, Any]] = {}
    for rec in records:
        key = _normalize_title(rec["title"]) or f"doi:{_doi_fold(rec['doi'])}"
        held = best.get(key)
        if held is None or _rank(rec) < _rank(held):
            best[key] = rec
    return list(best.values())


def _newest_first(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: (-(r.get("year") or 0), _doi_fold(r["doi"])))
