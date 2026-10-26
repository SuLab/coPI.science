"""Rules for BlackbirdBot's projects that need no database and no model.

Anchors (a project rests on whole items of the lab's public profile), sources (only what a
research tool returned), names (no other lab, PI or bot), completeness of the plan template,
the posting text and its one-Slack-message limit, and the outcome of a project's close.
Spec: docs/specs/2026-10-22-idea-pitching-prototype-design.md §5.3–§5.6, §5.9.

Imports nothing from ``src.agent.engine`` or ``src.services.blackbird_rubric``; from
``src.models`` only the status constants of ``src.models.hub_project``; from ``src.agent``
only ``persona_sections`` and ``slack_client.split_for_wire``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import NamedTuple

from src.agent import persona_sections
from src.agent.slack_client import split_for_wire
from src.models.hub_project import STATUS_NOT_AGREED, STATUS_READY

#: ``persona_sections.section_items``, re-exported: the profile items the rules below take.
section_items = persona_sections.section_items

#: The plan template, in order: (key, label).
TEMPLATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("indication", "Indication"),
    ("target", "Target or mechanism"),
    ("model", "Model"),
    ("positive_control", "Positive control"),
    ("negative_control", "Negative control"),
    ("standard_of_care", "Standard of care"),
    ("readout", "Readout"),
    ("design", "Design"),
    ("time_and_budget", "Time and budget"),
    ("lab_tools", "The lab's tools and models used"),
)
#: The public-profile sections an anchor may come from, in match order.
ANCHOR_SECTIONS = (
    "Key Methods and Technologies",
    "Model Systems",
    "Disease Areas / Biological Processes",
    "Key Molecular Targets",
    "Keywords",
)
#: A kept candidate needs at least one anchor from one of these.
METHOD_MODEL_SECTIONS = ("Key Methods and Technologies", "Model Systems")
#: Normalised values that leave a plan field unfilled.
PLACEHOLDERS = frozenset(
    {"to settle with the lab", "tbd", "to be determined", "unknown", "n/a", "none", "-", "?"}
)

POSTING_MAX_CHARS = 3500
SUMMARY_MAX_CHARS = 600
FIELD_MAX_CHARS = 300
TITLE_MAX_CHARS = 120
POSTING_MAX_SOURCES = 5
POSTING_HEADER = ":test_tube: *Blackbird project for the {surname} lab*"
REPLY_HEADER = ":test_tube: *Blackbird project:* {title}"
POSTING_CLOSING = (
    "Reply with your own post: what your lab would use, what you would change, and what you "
    "cannot do."
)
#: The fixed starts of the two headers. ``markdown_to_mrkdwn`` leaves single asterisks alone
#: and ``escape_for_slack`` touches only ``&``, ``<`` and ``>``, so Slack returns them as written.
PROJECT_POST_PREFIXES = (
    ":test_tube: *Blackbird project for the ",
    ":test_tube: *Blackbird project:* ",
)

#: U+2010–U+2015 (hyphens and dashes) and U+2212 (minus), each folded to ``-``.
_DASHES = str.maketrans(dict.fromkeys([*map(chr, range(0x2010, 0x2016)), "\u2212"], "-"))
_WHITESPACE_RE = re.compile(r"\s+")
_PMID_PREFIX_RE = re.compile(r"^PMID:?\s*", re.IGNORECASE)
_PATENT_STRIP_RE = re.compile(r"[\s,\-]")
#: What ``has_link`` refuses: a URL, or a Slack token ``escape_for_slack`` keeps live
#: (``<http…|…>``, ``<mailto:…|…>``, ``<#C…|…>``; ``<@U…>`` is refused as an ``@``).
_LINK_MARKERS = ("http://", "https://", "www.", "<http", "mailto:", "<#")
#: A bare domain in a third-party title (``evil-site.com/login``), which Slack may make live.
_BARE_DOMAIN = re.compile(
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|gov|edu|info|biz|co|us|uk|de|eu)\b",
    re.IGNORECASE,
)
#: A title longer than this is cut to it, ending ``…``, when a posting does not fit.
_SHORT_TITLE_CHARS = 80
_AGREE_MARKERS = ("✅", ":white_check_mark:")
#: ``⚠`` and ``⏸`` also cover their forms with the U+FE0F variation selector.
_DISAGREE_MARKERS = ("⚠", ":warning:", "⏸", ":pause_button:")
_GROUP_WORDS = "lab|Lab|group|Group|laboratory|Laboratory"


def is_project_post_text(text: str | None) -> bool:
    """True when ``text`` starts with either of ``PROJECT_POST_PREFIXES``."""
    return bool(text) and text.startswith(PROJECT_POST_PREFIXES)


def normalize_item(text: str) -> str:
    """NFKC; every character in U+2010–U+2015 and U+2212 becomes ``-``; ``casefold()``;
    whitespace runs collapse to one space; stripped; one trailing ``.`` dropped."""
    folded = unicodedata.normalize("NFKC", text).translate(_DASHES).casefold()
    folded = _WHITESPACE_RE.sub(" ", folded).strip()
    return folded[:-1] if folded.endswith(".") else folded


def _find_item(item: str, items: Mapping[str, Sequence[str]]) -> tuple[str, str] | None:
    """``(section, the profile's own text)`` of the first profile item, in
    ``ANCHOR_SECTIONS`` order, that equals ``item`` once both are normalised; an item that
    normalises to nothing matches nothing."""
    wanted = normalize_item(item)
    if not wanted:
        return None
    for section in ANCHOR_SECTIONS:
        for own in items.get(section, ()):
            if isinstance(own, str) and normalize_item(own) == wanted:
                return section, own
    return None


def match_anchor(item: str, items: Mapping[str, Sequence[str]]) -> str | None:
    """The section whose normalised item equals ``normalize_item(item)``, searched in
    ``ANCHOR_SECTIONS`` order; else None."""
    found = _find_item(item, items)
    return found[0] if found else None


def check_anchors(
    anchors: Sequence[Mapping[str, str]], items: Mapping[str, Sequence[str]]
) -> tuple[list[dict], list[dict]]:
    """Split model-cited anchors (``{"section", "item"}``) into kept and dropped.

    Kept ones become ``{"section": <matched section>, "item": <the profile's own text>}``;
    dropped ones carry ``"why": "not an item of the lab's profile"``. The model's own
    ``section`` is not trusted: a kept anchor takes the section its item is found in."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for anchor in anchors:
        cited = anchor if isinstance(anchor, Mapping) else {}
        item = cited.get("item")
        found = _find_item(item, items) if isinstance(item, str) else None
        if found is None:
            dropped.append({**cited, "why": "not an item of the lab's profile"})
        else:
            kept.append({"section": found[0], "item": found[1]})
    return kept, dropped


def has_method_or_model_anchor(kept: Sequence[Mapping[str, str]]) -> bool:
    """True when any kept anchor's section is in ``METHOD_MODEL_SECTIONS``."""
    return any(anchor.get("section") in METHOD_MODEL_SECTIONS for anchor in kept)


def source_key(raw: str, kind: str) -> str | None:
    """The key a tool records for one of its own results: ``"pmid:<digits>"`` (kind
    ``pubmed``), ``"nct:NCT<8 digits>"`` (kind ``trial``, upper-cased), ``"patent:" +
    raw.upper()`` with spaces, commas and dashes removed (kind ``patent``); None for an
    empty string (or one of only stripped characters); None too for an unknown kind."""
    text = raw.strip()
    if kind == "pubmed":
        return f"pmid:{text}" if text else None
    if kind == "trial":
        return f"nct:{text.upper()}" if text else None
    if kind == "patent":
        number = _PATENT_STRIP_RE.sub("", text.upper())
        return f"patent:{number}" if number else None
    return None


def resolve_source(raw: str, retrieved: Mapping[str, Mapping[str, str]]) -> str | None:
    """The retrieved key a model-cited identifier names.

    The cited text, with an optional ``PMID``/``PMID:`` prefix removed, is tried as
    ``pmid:``, then ``nct:``, then ``patent:`` (each normalised as ``source_key`` does), and
    the first key present in ``retrieved`` wins; None when none is. An unpublished filing's
    application number is eight bare digits (``src/services/patents.py:588``), so the kind
    comes from what was retrieved, never from the digits."""
    cited = _PMID_PREFIX_RE.sub("", raw.strip())
    for kind in ("pubmed", "trial", "patent"):
        key = source_key(cited, kind)
        if key is not None and key in retrieved:
            return key
    return None


def display_id(key: str) -> str:
    """How a source key is shown: ``"pmid:31500698"`` → ``"PMID 31500698"``,
    ``"nct:NCT01903460"`` → ``"NCT01903460"``, ``"patent:US11234567B2"`` →
    ``"US11234567B2"``; any other key unchanged."""
    if key.startswith("pmid:"):
        return "PMID " + key[len("pmid:"):]
    for prefix in ("nct:", "patent:"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def has_link(text: str) -> bool:
    """True for ``http://``, ``https://``, ``www.``, ``mailto:`` or a Slack ``<http`` or
    ``<#`` token, in any case."""
    lowered = text.casefold()
    return any(marker in lowered for marker in _LINK_MARKERS)


def fits_one_message(text: str) -> bool:
    """``len(split_for_wire(text)) == 1``: the split is decided on the mrkdwn-converted,
    escaped text Slack receives, not on the source (``src/agent/slack_client.py:299``)."""
    return len(split_for_wire(text)) == 1


def field_filled(value: object) -> bool:
    """A list is filled when any item is filled; a string when ``normalize_item(value)`` is
    non-empty and not in ``PLACEHOLDERS``; anything else is unfilled."""
    if isinstance(value, list):
        return any(field_filled(item) for item in value)
    if isinstance(value, str):
        normalized = normalize_item(value)
        return bool(normalized) and normalized not in PLACEHOLDERS
    return False


def missing_fields(plan: Mapping[str, object]) -> list[str]:
    """Labels of the unfilled template fields, in template order."""
    return [label for key, label in TEMPLATE_FIELDS if not field_filled(plan.get(key))]


def tool_marks(lab_tools: Sequence[str], items: Mapping[str, Sequence[str]]) -> list[dict]:
    """``{"item": text, "in_profile": bool}`` per tool, ``in_profile`` true when
    ``match_anchor`` finds it. A non-string entry is left out."""
    return [
        {"item": tool, "in_profile": match_anchor(tool, items) is not None}
        for tool in lab_tools
        if isinstance(tool, str)
    ]


class NameEntry(NamedTuple):
    agent_id: str
    bot_name: str
    pi_name: str


def foreign_names(
    text: str,
    *,
    roster: Iterable[NameEntry],
    own_agent_id: str,
    hub_agent_ids: AbstractSet[str],
) -> list[str]:
    """Names of other labs, PIs or bots that ``text`` mentions; returns the matched strings.

    Case-sensitive whole-word matches of each other entry's ``bot_name``, full ``pi_name``,
    ``Dr. <surname>``, ``Prof. <surname>``, and ``<surname>`` or ``<surname>'s`` followed by
    ``lab``, ``Lab``, ``group``, ``Group``, ``laboratory`` or ``Laboratory``. The surname is
    ``pi_name``'s last whitespace token; surname patterns apply only when ``pi_name`` has two
    or more tokens and the surname differs from the own lab's surname (two PIs who share a
    surname cannot be told apart by it; their full names and bot names are still checked).
    Entries whose ``agent_id`` is ``own_agent_id`` or in ``hub_agent_ids`` are skipped. A
    match is whole-word when no letter, digit or underscore touches either end; each matched
    string is returned once, in the order the patterns found them."""
    entries = list(roster)
    own_surname = next(
        (_lab_surname(entry.pi_name) for entry in entries if entry.agent_id == own_agent_id),
        None,
    )
    found: list[str] = []
    for entry in entries:
        if entry.agent_id == own_agent_id or entry.agent_id in hub_agent_ids:
            continue
        for pattern in _name_patterns(entry, own_surname):
            for match in re.finditer(rf"(?<!\w){pattern}(?!\w)", text):
                if match.group(0) not in found:
                    found.append(match.group(0))
    return found


def _lab_surname(pi_name: str) -> str | None:
    """The last whitespace token of ``pi_name``, or None when it has none."""
    tokens = pi_name.split()
    return tokens[-1] if tokens else None


def _name_patterns(entry: NameEntry, own_surname: str | None) -> list[str]:
    """The regexes (without word boundaries) ``foreign_names`` searches for one entry."""
    patterns: list[str] = []
    if entry.bot_name.strip():
        patterns.append(re.escape(entry.bot_name.strip()))
    tokens = entry.pi_name.split()
    if tokens:
        patterns.append(r"\s+".join(map(re.escape, tokens)))
    if len(tokens) >= 2 and tokens[-1] != own_surname:
        surname = re.escape(tokens[-1])
        patterns.append(rf"(?:Dr|Prof)\.\s*{surname}")
        patterns.append(rf"{surname}(?:'s)?\s+(?:{_GROUP_WORDS})")
    return patterns


def assemble_posting(
    *,
    surname: str,
    summary: str,
    anchors: Sequence[str],
    plan: Mapping[str, object],
    sources: Sequence[Mapping[str, str]],
) -> str:
    """The posting, assembled from checked fields (spec §5.6).

    Lines: ``POSTING_HEADER``; the summary; ``"Builds on: "`` + anchors joined by ``"; "``;
    ``"Opening plan:"`` then one ``"• <label>: <value>"`` line per template field (a list value
    joined by ``"; "``); ``"Sources:"`` then one ``"• " + display_id(source["key"]) + " — " +
    source["title"]`` per source; ``POSTING_CLOSING``. At most ``POSTING_MAX_SOURCES`` sources
    are listed. While the text is longer than ``POSTING_MAX_CHARS`` or not
    ``fits_one_message``, the last source is dropped (at least one is kept); then each title is
    shortened to 80 characters ending ``…``; if it still does not fit, raises
    ``ValueError("posting too long")``. With no sources the ``"Sources:"`` line is left
    out.

    Source titles and ids come from PubMed, ClinicalTrials.gov and USPTO, not from the model,
    and no other check reads them: ``_plain`` removes ``<``, ``>`` and ``@`` from both, so no
    Slack mention, channel or link token can survive ``escape_for_slack``, and a title that
    still carries a link is left out (the line shows the id alone). Raises
    ``ValueError("posting carries a link or an @")`` if the assembled text has either anyway."""
    listed = list(sources)[:POSTING_MAX_SOURCES]

    def render(titles: Sequence[str]) -> str:
        lines = [POSTING_HEADER.format(surname=surname), summary]
        lines.append("Builds on: " + "; ".join(anchors))
        lines.append("Opening plan:")
        for key, label in TEMPLATE_FIELDS:
            lines.append(f"• {label}: {_field_text(plan.get(key))}")
        if listed:
            lines.append("Sources:")
            for source, title in zip(listed, titles, strict=False):
                ident = _plain(display_id(str(source["key"])))
                lines.append("• " + ident + (" — " + title if title else ""))
        lines.append(POSTING_CLOSING)
        return "\n".join(lines)

    def fits(text: str) -> bool:
        return len(text) <= POSTING_MAX_CHARS and fits_one_message(text)

    def checked(text: str) -> str:
        if has_link(text) or "@" in text:
            raise ValueError("posting carries a link or an @")
        return text

    titles = [_source_title(source.get("title")) for source in listed]
    text = render(titles)
    while not fits(text) and len(listed) > 1:
        listed.pop()
        titles.pop()
        text = render(titles)
    if fits(text):
        return checked(text)
    text = render([_short_title(title) for title in titles])
    if fits(text):
        return checked(text)
    raise ValueError("posting too long")


def _field_text(value: object) -> str:
    """A plan value as one posting line: a list joined by ``"; "``."""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _plain(text: str) -> str:
    """``text`` without ``<``, ``>`` and ``@``, whitespace runs collapsed."""
    return " ".join(text.replace("<", "").replace(">", "").replace("@", "").split())


def _source_title(title: object) -> str:
    """A third-party source title fit for the posting: ``_plain``, and empty when it still
    carries a link or a bare domain (Slack would make either live)."""
    text = _plain(title if isinstance(title, str) else "")
    return "" if has_link(text) or _BARE_DOMAIN.search(text) else text


def _short_title(title: str) -> str:
    """``title`` cut to ``_SHORT_TITLE_CHARS`` characters, the last being ``…``."""
    if len(title) <= _SHORT_TITLE_CHARS:
        return title
    return title[: _SHORT_TITLE_CHARS - 1] + "…"


def position_marker(text: str | None) -> str | None:
    """After leading whitespace: ``"agree"`` for ``✅`` or ``:white_check_mark:``;
    ``"disagree"`` for ``⚠️``, ``⚠``, ``:warning:``, ``⏸️``, ``⏸`` or ``:pause_button:``;
    else None."""
    start = (text or "").lstrip()
    if start.startswith(_AGREE_MARKERS):
        return "agree"
    if start.startswith(_DISAGREE_MARKERS):
        return "disagree"
    return None


def project_outcome(
    *,
    lab_position: str | None,
    hub_marker: str | None,
    close: Mapping[str, object],
) -> tuple[str, list[str]]:
    """``(STATUS_READY, gaps)`` or ``(STATUS_NOT_AGREED, gaps)`` for a readable close.

    A close field that is missing or of the wrong type counts as empty (a non-string plan
    value is unfilled; non-list ``gaps``/``disagreements`` are ``[]``, non-string items
    dropped). Ready only when ``lab_position == "agree"``, ``hub_marker == "agree"``,
    ``close["position"] == "agree"``, ``missing_fields(close["plan"]) == []`` and
    ``close["disagreements"]`` is empty. ``gaps`` is the close's own gaps (strings) plus, in
    this order: ``"<label> is not filled"`` per missing field, ``"The lab did not agree"`` when
    the lab's position is not agree, ``"BlackbirdBot did not agree"`` when either hub signal is
    not agree."""
    plan = close.get("plan")
    missing = missing_fields(plan if isinstance(plan, Mapping) else {})
    gaps = _strings(close.get("gaps"))
    disagreements = _strings(close.get("disagreements"))
    lab_agrees = lab_position == "agree"
    hub_agrees = hub_marker == "agree" and close.get("position") == "agree"
    gaps.extend(f"{label} is not filled" for label in missing)
    if not lab_agrees:
        gaps.append("The lab did not agree")
    if not hub_agrees:
        gaps.append("BlackbirdBot did not agree")
    if lab_agrees and hub_agrees and not missing and not disagreements:
        return STATUS_READY, gaps
    return STATUS_NOT_AGREED, gaps


def _strings(value: object) -> list[str]:
    """The string items of a list; ``[]`` for anything that is not a list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
