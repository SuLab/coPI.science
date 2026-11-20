"""Readable, linked sources for the Landscape pages
(docs/plans/2026-11-19-landscape-ui-remediation-plan.md, contracts C3 and C4).

``resolve_support`` turns a key point's or a profile field's support list (S9's
``claim_support``, S6's ``support``) into labelled items and one kind: "sourced" only for a
dossier document, a database record or a verified quote, "analysis" for anything else the
pipeline wrote, "estimate" when nothing better is named. ``rewrite_doc_refs`` replaces the
``doc:<id>`` tokens the model writes in prose with the document's title and link.

A document's label is its short title (``short_title``); its link is the DOI, else the PMID,
built by ``src.services.publication_links``. Pure: no database access.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Literal, TypedDict

from src.services.publication_links import NCT_RE, TRIAL_URL, doi_url, pubmed_url


class DocInfo(TypedDict):
    """One document a member idea rests on, as ``landscape_view._sources`` lists it."""

    id: str
    title: str | None
    doi: str | None
    pmid: str | None
    kind: str | None


class Quote(TypedDict):
    """One verified quote of a member idea (C5)."""

    document: str
    document_id: str | None
    section: str
    quote: str
    basis: Literal["summary", "full_text"]


class SupportItem(TypedDict):
    """One resolved support reference: a readable label, a link when the repo builds one, and
    the quote when the reference was a verified quote."""

    label: str
    url: str | None
    quote: str | None


class SupportView(TypedDict):
    """A key point's or a profile field's support, classified (C3)."""

    kind: Literal["sourced", "analysis", "estimate"]
    items: list[SupportItem]


#: Characters of a short title before it is cut at a word boundary.
_SHORT_TITLE_CHARS = 80
#: What a ``doc:`` token that matches no document, or several, reads as (C4).
UNNAMED_DOCUMENT = "an unnamed dossier document"
#: A full uuid or a prefix of at least 8 hex digits; never followed by a letter or digit, so
#: ``doc:12345678xyz`` is not a reference.
_DOC_RE = re.compile(
    r"(?<![\w])doc:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8,})(?![0-9a-z])",
    re.IGNORECASE,
)
#: Markdown characters escaped in a link label: ``&``, ``<`` and ``>`` as entities, so the
#: output holds no raw angle bracket outside the link destination; the rest by backslash.
_MARKDOWN_ESCAPES = {
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    "\\": "\\\\", "`": "\\`", "*": "\\*", "_": "\\_", "~": "\\~",
}

#: Record prefixes (C3 classes 2-5) and how each reads. Only ``pmid:`` and ``nct:`` get a link.
_RECORD_LABELS: dict[str, str] = {
    "pmid": "PubMed {}",
    "nct": "{}",
    "patent": "Patent {}",
    "grant": "Grant {}",
    "orcid": "ORCID {}",
    "uniprot": "UniProt {}",
    "opentargets": "Open Targets {}",
    "hpa": "HPA {}",
    "mondo": "Mondo {}",
    "orphadata": "Orphadata {}",
    "chembl": "ChEMBL {}",
    "openfda": "openFDA {}",
    "trials": "ClinicalTrials.gov records ({})",
}
_RECORD_RE = re.compile(r"^(" + "|".join(_RECORD_LABELS) + r"):\s*(\S.*)$", re.IGNORECASE)


def _spaced(name: str) -> str:
    return " ".join(re.split(r"[_.\s]+", name)).strip()


#: Readable labels of the pipeline's own references (C3 class 8), tried in order.
_ANALYSIS_LABELS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (re.compile(r"^specialist:\s*([\w-]+)", re.I), lambda m: f"{_spaced(m[1])} specialist"),
    (re.compile(r"^([\w]+)[\s-]specialist\b", re.I), lambda m: f"{_spaced(m[1])} specialist"),
    (re.compile(r"^deep_analysis:\s*([\w.-]*)", re.I),
     lambda m: f"deep analysis ({_spaced(m[1])})" if m[1] else "deep analysis"),
    (re.compile(r"^(?:flag|flags|flags_reviewed):\s*([\w.-]*)", re.I),
     lambda m: f"deal-breaker review ({_spaced(m[1])})" if m[1] else "deal-breaker review"),
    (re.compile(r"^disease_profile:\s*([\w.-]*)", re.I),
     lambda m: f"disease profile ({_spaced(m[1])})" if m[1] else "disease profile"),
    (re.compile(r"^search_prior_art:", re.I), lambda m: "prior-art search"),
    (re.compile(r"^retrieved_sources:", re.I), lambda m: "retrieved sources"),
)


def short_title(title: str | None) -> str:
    """``title`` with its whitespace collapsed, cut at the last word boundary within 80
    characters and ending "…" when cut; "Untitled document" when there is no title."""
    text = " ".join((title or "").split())
    if not text:
        return "Untitled document"
    if len(text) <= _SHORT_TITLE_CHARS:
        return text
    cut = text[:_SHORT_TITLE_CHARS]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _doc_url(doc: DocInfo) -> str | None:
    return doi_url(doc.get("doi")) or pubmed_url(doc.get("pmid"))


def _match_doc(token: str, docs: Mapping[str, DocInfo]) -> DocInfo | None:
    """The one document a ``doc:`` token's id names: the full uuid, or a prefix (hyphens
    ignored) matching exactly one key of ``docs``; None when it matches none or several."""
    wanted = token.lower().replace("-", "")
    found = [doc for key, doc in docs.items() if key.lower().replace("-", "").startswith(wanted)]
    return found[0] if len(found) == 1 else None


def _record_item(prefix: str, value: str) -> SupportItem:
    value = value.strip()
    url = None
    if prefix == "pmid":
        url = pubmed_url(value)
    elif prefix == "nct":
        value = value.upper()
        url = TRIAL_URL.format(value) if NCT_RE.fullmatch(value) else None
    return {"label": _RECORD_LABELS[prefix].format(value), "url": url, "quote": None}


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _quote_item(quote: Quote, docs: Mapping[str, DocInfo]) -> SupportItem:
    doc = docs.get(quote["document_id"] or "")
    if doc is not None:
        return {"label": short_title(doc.get("title")), "url": _doc_url(doc),
                "quote": quote["quote"]}
    return {"label": short_title(quote["document"]), "url": None, "quote": quote["quote"]}


def _analysis_label(ref: str) -> str:
    for pattern, label in _ANALYSIS_LABELS:
        match = pattern.match(ref)
        if match:
            return label(match)
    return "analysis note"


def _classify(
    ref: str, docs: Mapping[str, DocInfo], quotes: Mapping[str, Quote]
) -> tuple[Literal["sourced", "analysis", "estimate"], SupportItem | None]:
    """One reference's class (C3, tried in table order) and its item; an estimate has none."""
    doc_match = _DOC_RE.fullmatch(ref)
    if doc_match:
        doc = _match_doc(doc_match[1], docs)
        if doc is not None:
            return "sourced", {"label": short_title(doc.get("title")), "url": _doc_url(doc),
                               "quote": None}
        return "analysis", {"label": UNNAMED_DOCUMENT, "url": None, "quote": None}
    record = _RECORD_RE.match(ref)
    if record:
        return "sourced", _record_item(record[1].lower(), record[2])
    quote = quotes.get(_norm(ref))
    if quote is not None:
        return "sourced", _quote_item(quote, docs)
    if ref.casefold() == "estimate" or re.match(r"estimate\s*:", ref, re.I):
        return "estimate", None
    return "analysis", {"label": _analysis_label(ref), "url": None, "quote": None}


def resolve_support(
    refs: list[str] | str | None,
    docs: Mapping[str, DocInfo],
    quotes: Sequence[Quote],
    *,
    expert_hidden: bool,
) -> SupportView:
    """Classify a support list (C3).

    ``refs`` is one reference or a list of them; ``None``, blanks and non-strings are
    skipped. ``docs`` maps a full document uuid string to the member's ``DocInfo``;
    ``quotes`` are the member's verified quotes (C5). Each reference becomes one item, in
    order: a matched ``doc:`` token, a record prefix (``pmid:``, ``nct:``, ``uniprot:``, …)
    or a reference equal to a quote (whitespace and case ignored) is sourced; "estimate" or
    "estimate: …" adds no item; anything else is an analysis item with a readable label, or
    exactly "analysis" when ``expert_hidden``. A ``doc:`` token matching no document, or
    several, is an analysis item reading "an unnamed dossier document".

    The list's kind is "sourced" when any item is sourced, else "analysis" when any is,
    else "estimate" (an empty list included).
    """
    items_in = [refs] if isinstance(refs, str) else list(refs or [])
    by_text = {_norm(quote["quote"]): quote for quote in reversed(quotes) if quote["quote"]}
    kinds: set[str] = set()
    items: list[SupportItem] = []
    for ref in items_in:
        if not isinstance(ref, str) or not ref.strip():
            continue
        kind, item = _classify(ref.strip(), docs, by_text)
        kinds.add(kind)
        if item is None:
            continue
        if kind == "analysis" and expert_hidden:
            item = {"label": "analysis", "url": None, "quote": None}
        items.append(item)
    overall: Literal["sourced", "analysis", "estimate"] = (
        "sourced" if "sourced" in kinds else "analysis" if "analysis" in kinds else "estimate"
    )
    return {"kind": overall, "items": items}


def _markdown_label(title: str) -> str:
    title = " ".join(re.sub(r"[\[\]]", " ", title).split()) or "Untitled document"
    return "".join(_MARKDOWN_ESCAPES.get(char, char) for char in title)


def rewrite_doc_refs(
    text: str | None, docs: Mapping[str, DocInfo], *, markdown: bool
) -> str | None:
    """Replace ``doc:<uuid>`` and ``doc:<8+ hex>`` tokens with the document (C4).

    A token naming exactly one key of ``docs`` (full uuid, or a hex prefix with hyphens
    ignored) becomes the document's short title with its DOI, else PubMed, link; any other
    token becomes "an unnamed dossier document". ``markdown=True`` writes
    ``[title](<url>)``: ``[`` and ``]`` in the title become spaces, ``&``, ``<`` and ``>``
    become entities and Markdown emphasis characters are backslash-escaped, so the only raw
    angle brackets are the destination's; with no url the label is written alone.
    ``markdown=False`` writes ``title (url)`` or the bare title, unescaped, for templates to
    escape (``plain_citations``). ``None`` returns ``None``.
    """
    if text is None:
        return None

    def _replace(match: re.Match[str]) -> str:
        doc = _match_doc(match[1], docs)
        if doc is None:
            return UNNAMED_DOCUMENT
        title, url = short_title(doc.get("title")), _doc_url(doc)
        if markdown:
            label = _markdown_label(title)
            return f"[{label}](<{url}>)" if url else label
        return f"{title} ({url})" if url else title

    return _DOC_RE.sub(_replace, text)
