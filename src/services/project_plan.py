"""Rules for BlackbirdBot's projects that need no database and no model.

Anchors (a project rests on whole items of the lab's public profile), sources (only what a
research tool returned), names (no other lab, PI or bot), completeness of the plan template,
the posting text and its two-Slack-message limit, and the outcome of a project's close.
Spec: docs/specs/2026-10-22-idea-pitching-prototype-design.md §5.3–§5.6, §5.9.

Imports nothing from ``src.agent.engine`` or ``src.services.blackbird_rubric``; from
``src.models`` only the status constants of ``src.models.hub_project``; from ``src.agent``
only ``persona_sections`` and ``slack_client.split_for_wire``.
"""

from __future__ import annotations

import json
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
#: The template fields a close must fill for a project to be ready. Time and budget is left
#: out: staff settle it afterwards, so it never blocks (owner decision D72, 2026-10-29).
BLOCKING_FIELDS: tuple[tuple[str, str], ...] = tuple(
    pair for pair in TEMPLATE_FIELDS if pair[0] != "time_and_budget"
)
#: A field whose value STARTS with one of these is unfilled, whatever follows: the writer
#: deferred the answer rather than giving one ("to settle with the lab (likely $150K)").
DEFERRALS = frozenset(
    {"to settle with the lab", "to be settled with the lab", "tbd", "to be determined"}
)
#: A field equal to one of these is unfilled. Prefix matching would be wrong here: §5.5 of
#: the projects design makes "none exists, because …" a real answer for Target and Standard
#: of care, and "unknown aetiology, so the model is …" is an answer too.
NULLS = frozenset({"none", "n/a", "unknown", "-", "?"})
#: Normalised values that leave a plan field unfilled.
PLACEHOLDERS = DEFERRALS | NULLS
#: ``live_disagreements`` reads a disagreement entry as "there is none" when its first
#: SENTENCE carries one of these negations and nothing else. Both halves matter: without the
#: negation "Disputed" would drop, and without "nothing else" a real dispute that opens with
#: "None of the proposed controls …" would.
#: "not" is deliberately NOT here. With it, "Not agreed", "Not resolved" and "Nothing is
#: settled" read as "there is no disagreement" and grade a disputed project ready — the
#: inversion this whole field exists to prevent.
_NO_DISAGREEMENT_NEGATIONS = frozenset({"none", "no", "nothing", "nil", "zero"})
#: The other words such a sentence may contain. Anything outside this set is content, so the
#: entry is a disagreement. Erring here keeps an entry that could have dropped, which costs a
#: Ready verdict a reader can see and correct — the opposite error hides a dispute.
#: Only words that cannot themselves carry a dispute. A verb of resolution ("agreed",
#: "resolved", "settled") or a noun a dispute can be about ("lab", "change") must stay out:
#: with them, "No change agreed" and "Nothing settled" would drop.
_NO_DISAGREEMENT_TOKENS = _NO_DISAGREEMENT_NEGATIONS | {
    "outstanding", "remaining", "remain", "remains", "known", "left", "open", "further",
    "disagreement", "disagreements", "dispute", "disputes", "issue", "issues", "item",
    "items", "on", "the", "science", "scientific", "side", "sides", "both", "between",
    "us", "our", "at", "this", "stage", "live", "there", "are", "is",
    "were", "was", "major", "substantive", "material", "anything", "any",
}
#: Punctuation that means an entry carries more than a bare "there is none", so it is a
#: disagreement whatever its words. Erasing it instead made "No - issues remain open on both
#: sides" indistinguishable from "no issues remain", and "No disagreement on the science. The
#: lab will not run the NHP arm." drop on its first sentence.
_STRUCTURED_RE = re.compile(r"[,:;.\-]")
#: Brackets and quotes around a whole entry are not structure: "(none)" is "none".
_WRAPPER_CHARS = "()[]{}\"'“”‘’ "
#: What ``live_disagreements`` treats as an empty entry. Narrower than ``NULLS``: "unknown"
#: and "?" assert ignorance, not absence, and D73 must not grade a hub that does not know
#: better than one that stayed silent.
_NO_DISAGREEMENT_NULLS = frozenset({"none", "n/a", "-"})

#: A posting may take this many Slack messages (owner, 2026-10-27: a long project posts as
#: two rather than failing); the second continues in the first one's Slack thread.
POSTING_MAX_MESSAGES = 2
#: 3,500 characters per message, as when a posting had to fit one. With ``TITLE_MAX_CHARS``,
#: the only length limit on a choice: the summary and the plan fields have none of their own
#: (owner, 2026-10-27, after the model overran a 300- and then a 500-character field limit).
POSTING_MAX_CHARS = 7000
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


def fits_posting(text: str) -> bool:
    """At most ``POSTING_MAX_MESSAGES`` Slack messages, counted as ``fits_one_message``
    counts one. ``AgentSlackClient.post_message`` posts the rest of a longer root post in
    the first message's Slack thread, so a posting stays one top-level post."""
    return len(split_for_wire(text)) <= POSTING_MAX_MESSAGES


def posting_head(text: str) -> str:
    """The first Slack message of a posting: what ``_post_message`` logs as its root row
    when the posting takes two (each message's row keeps its own source text)."""
    chunks = split_for_wire(text)
    return chunks[0] if chunks else text


def _is_placeholder(value: str) -> bool:
    """Whether ``value`` defers the answer instead of giving one: a ``NULLS`` phrase exactly,
    or a ``DEFERRALS`` phrase the value starts with, ended by anything that is not a letter or
    a digit. "none exists, because no drug is approved" is an answer; "none" is not."""
    normalized = normalize_item(value)
    if not normalized:
        return True
    if normalized in NULLS:
        return True
    for phrase in DEFERRALS:
        if normalized == phrase:
            return True
        if normalized.startswith(phrase) and not normalized[len(phrase)].isalnum():
            return True
    return False


def field_filled(value: object) -> bool:
    """A list is filled when any item is filled; a string when it is neither empty nor a
    placeholder (``_is_placeholder``); anything else is unfilled."""
    if isinstance(value, list):
        return any(field_filled(item) for item in value)
    if isinstance(value, str):
        return not _is_placeholder(value)
    return False


def missing_fields(
    plan: Mapping[str, object], fields: Sequence[tuple[str, str]] = TEMPLATE_FIELDS
) -> list[str]:
    """Labels of ``fields`` the plan leaves unfilled, in the order given. ``BLOCKING_FIELDS``
    is what decides an outcome (D72); the default lists every template field, for display."""
    return [label for key, label in fields if not field_filled(plan.get(key))]


def _one_line(value: object) -> str:
    """``value`` as one line, for a gap a reader will see. Not clipped: the close block is
    the only copy once the sidecar is stripped from the posted reply."""
    return " ".join(str(value).split())


def _says_no_disagreement(entry: str) -> bool:
    """Whether the WHOLE of ``entry`` says there is no disagreement and says nothing else.

    Two tests, and both must hold. The entry may carry no sentence or clause punctuation
    (``_STRUCTURED_RE``), because anything that punctuates says more than "there is none" —
    this is what keeps "No - issues remain open on both sides" and "No disagreement on the
    science. The lab will not run the NHP arm." as the disagreements they are. And every
    word of it must be in ``_NO_DISAGREEMENT_TOKENS``, at least one of them a negation, so
    "No agreement on the dose" stays a disagreement on "agreement" and "dose".

    The error this leans away from is deliberate: an unrecognised way of writing "there is
    none" costs a visible Not agreed that a reader can correct, while the opposite error
    hides a dispute behind Ready. The close prompt asks for ``[]`` and nothing else, so what
    reaches here is a close that did not follow it.
    """
    normalized = normalize_item(entry).strip(_WRAPPER_CHARS)
    if not normalized or _STRUCTURED_RE.search(normalized):
        return False
    words = normalized.split()
    return bool(words) and all(word in _NO_DISAGREEMENT_TOKENS for word in words) and any(
        word in _NO_DISAGREEMENT_NEGATIONS for word in words
    )


def disagreement_note(value: object) -> str | None:
    """The close's one-sentence note on the disagreement question, or None when it wrote
    none (owner decision D75, 2026-10-29).

    The note is evidence, not the answer: ``remaining`` alone decides the verdict (M18), so
    a close that sets the boolean and forgets the note is still a close. Stored as
    ``final["disagreement_note"]`` rather than folded into the gaps (M22) — the gaps answer
    what is open, this answers why there is, or is not, a dispute.
    """
    if not isinstance(value, Mapping):
        return None
    note = value.get("note")
    return _one_line(note) if isinstance(note, str) and note.strip() else None


def live_disagreements(value: object) -> list[str] | None:
    """The close's live scientific disputes, or None when it did not answer the question at
    all, which D73 makes "the hub did not say" rather than "there are none".

    The answer BlackbirdBot writes is a boolean (D75, 2026-10-29)::

        "disagreements": {"remaining": false, "note": "…"}
        "disagreements": {"remaining": true, "note": "…", "items": ["…"]}

    ``remaining`` decides, and nothing is read from prose. Run b70d0cff wrote a bare
    ``"disagreements": []`` in all twenty closes because the prompt's own skeleton supplied
    it: an empty array is a template default, not an answer, and no amount of care in the
    reader can tell the two apart. A boolean cannot be defaulted into — the prompt's skeleton
    now carries ``true`` (M20), so a close that copies it whole is Not agreed rather than
    silently Ready.

    ``remaining: true`` with no usable ``items`` falls back to the note, and to a fixed line
    when there is no note either (M19): "a disagreement remains" can never render as none.

    A list is the legacy shape (M21), kept for a close written by an older agent image during
    a mismatched deploy and graded exactly as D73 left it: an entry is dropped only when it is
    empty, is a ``NULLS`` phrase, or says there is no disagreement
    (``_says_no_disagreement``), and ``project_outcome`` keeps every dropped entry as a gap.
    A non-string entry is kept — it is content the hub wrote, so it must not read as
    agreement — rendered as JSON when it is a mapping or a list, and skipped only when it is
    empty or false. Under the current prompt this branch is unreachable.
    """
    if isinstance(value, Mapping):
        remaining = value.get("remaining")
        if not isinstance(remaining, bool):
            return None
        if not remaining:
            return []
        items = [entry for entry in _strings(value.get("items")) if normalize_item(entry)]
        if items:
            return items
        note = disagreement_note(value)
        return [note] if note else ["The close recorded a disagreement without naming it"]
    if not isinstance(value, list):
        return None
    kept: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            if not entry:
                continue
            kept.append(
                json.dumps(entry, default=str) if isinstance(entry, Mapping | list)
                else str(entry)
            )
            continue
        normalized = normalize_item(entry)
        if (
            not normalized
            or normalized in _NO_DISAGREEMENT_NULLS
            or _says_no_disagreement(entry)
        ):
            continue
        kept.append(entry)
    return kept


def tool_marks(
    lab_tools: Sequence[str],
    items: Mapping[str, Sequence[str]],
    anchors: Sequence[Mapping] = (),
) -> list[dict]:
    """``{"item", "in_profile", "matched"}`` per string tool, in order.

    Candidates are the profile's ``METHOD_MODEL_SECTIONS`` items and the project's anchors
    from those sections (targets and keywords never mark a tool), normalised and kept when at
    least two words long. A tool is in the profile when its normalised text contains a
    candidate as whole words; the longest such candidate is ``matched`` (its original text),
    else ``in_profile`` is False and ``matched`` None. A non-string tool is left out.

    Containment, not equality: the close writes a tool as prose ("pan-expansion microscopy
    (pan-ExM) … (contingent, requires re-training)"), and the close prompt never asks for a
    verbatim copy. ``match_anchor``'s equality stays right for §5.3 anchors, which the model
    picks from a list.
    """
    candidates: dict[str, str] = {}
    texts: list[object] = [item for section in METHOD_MODEL_SECTIONS
                           for item in items.get(section, ())]
    texts += [
        anchor.get("item") for anchor in anchors
        if isinstance(anchor, Mapping) and anchor.get("section") in METHOD_MODEL_SECTIONS
    ]
    for text in texts:
        if not isinstance(text, str):
            continue
        normalized = normalize_item(text)
        if len(normalized.split()) >= 2:
            candidates.setdefault(normalized, text)
    # Longest first; a stable sort keeps the first-seen candidate among equal lengths.
    patterns = [
        (re.compile(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)"), original)
        for normalized, original in sorted(candidates.items(), key=lambda kv: -len(kv[0]))
    ]
    marks = []
    for tool in lab_tools:
        if not isinstance(tool, str):
            continue
        normalized_tool = normalize_item(tool)
        matched = next(
            (original for pattern, original in patterns if pattern.search(normalized_tool)),
            None,
        )
        marks.append({"item": tool, "in_profile": matched is not None, "matched": matched})
    return marks


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
    are listed. While the text is longer than ``POSTING_MAX_CHARS`` or not ``fits_posting``
    (at most ``POSTING_MAX_MESSAGES`` Slack messages), the last source is dropped (at least
    one is kept); then each title is shortened to 80 characters ending ``…``; if it still
    does not fit, raises ``ValueError("posting too long")``. With no sources the
    ``"Sources:"`` line is left out.

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
        return len(text) <= POSTING_MAX_CHARS and fits_posting(text)

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


def project_outcome(*, close: Mapping[str, object]) -> tuple[str, list[str], list[str]]:
    """``(status, gaps, disagreements)`` for a close whose ``<project_json>`` block parsed.

    Only two things decide the status (owner decisions D69–D73, 2026-10-29): whether the
    close filled every ``BLOCKING_FIELDS`` field, and whether it records a live scientific
    disagreement. Neither bot's ✅/⚠️ marker, nor ``close["position"]``, nor the lab's last
    position enters it — they are commentary the page shows.

    * ``STATUS_NOT_AGREED`` when ``live_disagreements`` returns None: the close did not
      answer the disagreement question (D73) — it omitted the key, or its ``remaining`` is
      not a boolean (D75) — so silence cannot read as "there are none".
      It is still a verdict on the plan rather than an unreadable close — the block parsed
      and the plan is there — which keeps the project counted and keeps a formatting slip
      out of the session's failure streak. Whatever the field held is written into ``gaps``
      so the text survives, and the returned disagreements are empty.
    * ``STATUS_READY`` when no blocking field is missing and no live disagreement remains.
    * ``STATUS_NOT_AGREED`` otherwise.

    A close field of the wrong type counts as empty (a non-string plan value is unfilled;
    non-list ``gaps`` is ``[]``, non-string items dropped). ``gaps`` is the close's own gaps
    plus ``"<label> is not filled"`` per missing blocking field.

    ``gaps`` never decides anything, D76 (2026-10-29) included: the prompt requires at least
    one open item with an owner, but an empty list still reads as "nothing open" and leaves
    the verdict alone. Enforcing it here would make a formatting slip a verdict and would
    pressure the hub into inventing a gap to pass, which is worse than an honest blank.
    """
    plan = close.get("plan")
    missing = missing_fields(plan if isinstance(plan, Mapping) else {}, BLOCKING_FIELDS)
    gaps = _strings(close.get("gaps"))
    gaps.extend(f"{label} is not filled" for label in missing)
    raw = close.get("disagreements")
    disagreements = live_disagreements(raw)
    if disagreements is None:
        gaps.append("The close did not say whether a scientific disagreement remains")
        # Whole, as the dropped-entry lines below are: the sidecar is stripped before
        # posting, so `final` is the only place this text survives for a reader.
        if raw is not None and str(raw).strip():
            gaps.append(f"The close's disagreements field read: {_one_line(raw)}")
        return STATUS_NOT_AGREED, gaps, []
    # An entry live_disagreements dropped said there was no disagreement, but it may carry a
    # second sentence that does not. Recording it as a gap keeps every word the hub wrote in
    # front of a reader without letting the sentence that says "none" block a ready verdict.
    # Whole, not clipped: the sidecar is stripped before posting, so `final` is the only
    # place a dropped entry survives, and the close block already bounds its length.
    recorded: list[str] = []
    for entry in _strings(raw):
        line = f"The close recorded, as no disagreement: {entry}"
        if entry not in disagreements and normalize_item(entry) and line not in recorded:
            recorded.append(line)
    gaps.extend(recorded)
    if not missing and not disagreements:
        return STATUS_READY, gaps, disagreements
    return STATUS_NOT_AGREED, gaps, disagreements


def _strings(value: object) -> list[str]:
    """The string items of a list; ``[]`` for anything that is not a list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
