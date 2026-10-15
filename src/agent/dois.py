"""DOI and PMID citations in model and Slack text: contract C2 of the 2026-10-15
publication-handling design (main SPEC ID-6 to ID-10; PMIDs: spec 2026-10-05 D49).
Dependency-free: ``re``, ``dataclasses`` and ``urllib.parse`` only
(tests/unit/test_leaf_modules.py).

``find_dois`` reads a DOI as a reader would: it starts at ``10.<registrant>/`` with no
letter or digit before it (so "110.1234/x" and "v10.1234/x" are not DOIs), keeps balanced
``()[]{}<>`` (SICI and Cell DOIs), reads Slack's ``&lt;``/``&gt;``/``&amp;`` as one
character, stops at whitespace, ``"``, ``'``, ``|``, a backtick, ``*``, any non-ASCII
character or an unmatched closer, then cuts a ``?key=value`` query and trailing
punctuation and decodes ``%XX`` once. Values are canonical: a DOI lower-cased, a PMID
without leading zeros. A PMID written inside a DOI ("10.1038/pmid12345") is not a PMID.
``canonical_doi(v)`` equals ``identifiers.normalize_doi(v).lower()`` except for the
divergences pinned in tests/unit/test_agent_citations.py (a 3-digit registrant, a space or
quote, a trailing unmatched ``)``, trailing ``: ? ! * ~ _`` or backtick, a query)."""

import re
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class Citation:
    """One DOI or PMID in a text. ``text[start:end]`` is how it was written, a directly
    preceding doi.org or PubMed URL prefix included; ``kind`` is "doi" or "pmid"; ``value``
    is canonical."""

    start: int
    end: int
    kind: str
    value: str


_DOI_START = re.compile(r"(?<![0-9A-Za-z])10\.\d{4,9}(?:\.\d+)*(?:/|%2[Ff])")
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))
_STOP = frozenset("\"'|`*")
_OPENERS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_CLOSERS = {close: open_ for open_, close in _OPENERS.items()}
_QUERY = re.compile(r"\?[A-Za-z0-9_.-]+=.*$", re.DOTALL)
_TRAILING = ".,;:?!_~"
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
#: Only this many characters before a DOI are searched for its prefix (keeps scans linear).
_PREFIX_WINDOW = 24
_PREFIX_BEFORE = re.compile(
    r"(?:https?://(?:dx\.|www\.)?doi\.org/|(?:dx\.|www\.)?doi\.org/|info:doi/|doi:|doi )$",
    re.IGNORECASE,
)
#: identifiers.normalize_doi's prefix rule, repeated because this module is a leaf.
_CANONICAL_PREFIX = re.compile(
    r"^(?:doi\s*:\s*|doi\s+(?=10\.)|info:doi/|(?:https?://)?(?:www\.)?(?:dx\.)?doi\.org/)",
    re.IGNORECASE,
)
_PMID_URL = re.compile(
    r"(?:https?://)?(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/)"
    r"0*(\d{1,9})(?!\d)/?",
    re.IGNORECASE,
)
_PMID_LABEL = re.compile(r"\bPMID\s*:?\s*0*(\d{1,9})(?!\d)", re.IGNORECASE)
_BARE_PMID = re.compile(r"0*(\d{1,9})")


def _unit(text: str, i: int) -> tuple[str, int]:
    """The character at ``i`` and its raw width: an entity counts as the one it stands for."""
    for entity, char in _ENTITIES:
        if text.startswith(entity, i):
            return char, len(entity)
    return text[i], 1


def _scan(text: str, pos: int) -> tuple[str, list[int]]:
    """The DOI suffix read from ``pos``, and the raw offset just after each unit of it."""
    chars: list[str] = []
    ends: list[int] = []
    stack: list[str] = []
    i = pos
    while i < len(text):
        char, width = _unit(text, i)
        if width == 1 and (char.isspace() or char in _STOP or not char.isascii()):
            break
        if char in _OPENERS:
            stack.append(char)
        elif char in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[char]:
                break
            stack.pop()
        chars.append(char)
        i += width
        ends.append(i)
    return "".join(chars), ends


def _drop_unopened_closers(value: str) -> str:
    while value and value[-1] in _CLOSERS and (
        value.count(_CLOSERS[value[-1]]) < value.count(value[-1])
    ):
        value = value[:-1]
    return value


def _clean(suffix: str) -> tuple[str, int]:
    """(cleaned suffix, units kept before decoding): the query cut, trailing punctuation
    stripped, ``%XX`` decoded once, then trailing punctuation and unopened closers dropped
    until stable. The kept count fixes the span end, so a decoded and trimmed ``%29`` stays
    inside the span."""
    value = _QUERY.sub("", suffix).rstrip(_TRAILING)
    kept = len(value)
    if _PERCENT.search(value):
        value = urllib.parse.unquote(value)
    while (cleaned := _drop_unopened_closers(value.rstrip(_TRAILING))) != value:
        value = cleaned
    return value, kept


def _span_start(text: str, start: int) -> int:
    """``start`` moved back over a DOI prefix that ends exactly there (earliest-starting
    first), searched only in the ``_PREFIX_WINDOW`` characters before it."""
    window = text[max(0, start - _PREFIX_WINDOW):start]
    prefix = _PREFIX_BEFORE.search(window)
    return start - (len(window) - prefix.start()) if prefix else start


def find_dois(text: str | None) -> list[Citation]:
    """The DOIs in ``text``, in order and non-overlapping."""
    if not text:
        return []
    found: list[Citation] = []
    pos = 0
    while (match := _DOI_START.search(text, pos)) is not None:
        suffix, ends = _scan(text, match.end())
        cleaned, kept = _clean(suffix)
        if not cleaned:
            pos = match.end()
            continue
        head = match.group(0)
        if not head.endswith("/"):
            head = head[:-3] + "/"  # an encoded %2F separator is written as "/"
        end = ends[kept - 1]
        found.append(Citation(_span_start(text, match.start()), end, "doi", (head + cleaned).lower()))
        pos = end
    return found


def extract_dois(text: str | None) -> set[str]:
    """The canonical DOIs in ``text`` (``find_dois``)."""
    return {c.value for c in find_dois(text)}


def canonical_doi(raw: str | None) -> str | None:
    """The canonical DOI ``raw`` is, when the whole of it (after one ``%XX`` decode and a
    DOI prefix) starts with a DOI; else None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if _PERCENT.search(value):
        value = urllib.parse.unquote(value)
    value = _CANONICAL_PREFIX.sub("", value, count=1).strip()
    found = find_dois(value)
    return found[0].value if found and found[0].start == 0 else None


def find_pmids(text: str | None) -> list[Citation]:
    """PMIDs written as a PubMed URL or as ``PMID n``/``PMID: n``; 0 is skipped, and of two
    overlapping matches the earlier-starting wins."""
    if not text:
        return []
    hits = [
        Citation(m.start(), m.end(), "pmid", str(int(m.group(1))))
        for pattern in (_PMID_URL, _PMID_LABEL)
        for m in pattern.finditer(text)
        if int(m.group(1))
    ]
    hits.sort(key=lambda c: c.start)
    kept: list[Citation] = []
    for citation in hits:
        if not kept or citation.start >= kept[-1].end:
            kept.append(citation)
    return kept


def find_citations(text: str | None) -> list[Citation]:
    """The DOIs, plus every PMID that starts outside every DOI span, by start."""
    dois = find_dois(text)
    pmids = [p for p in find_pmids(text) if not any(d.start <= p.start < d.end for d in dois)]
    return sorted([*dois, *pmids], key=lambda c: c.start)


def extract_pmids(text: str | None) -> set[str]:
    """The PMIDs of ``find_citations``: a PMID inside a DOI is not one (A-D3)."""
    return {c.value for c in find_citations(text) if c.kind == "pmid"}


def canonical_pmid(raw: str | int | None) -> str | None:
    """The canonical PMID ``raw`` is: bare digits (leading zeros dropped), or one PubMed
    URL or ``PMID`` label spanning the whole string; else None (0 included)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if (bare := _BARE_PMID.fullmatch(value)) is not None:
        return bare.group(1) if int(bare.group(1)) else None
    found = find_pmids(value)
    if found and found[0].start == 0 and found[0].end == len(value):
        return found[0].value
    return None


def paper_ids_in(text: str | None) -> set[str]:
    """The values of ``find_citations``: canonical DOIs and PMIDs, which never collide (a DOI
    starts with ``10.``)."""
    return {c.value for c in find_citations(text)}
