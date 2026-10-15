"""Lab mentions in agent text: who a message names (main SPEC GATE-8).

``find_lab_mentions`` returns every span of ``text`` that names a lab on the roster, by
one of six rules in priority order (``MENTION_KINDS``). A later match that overlaps an
earlier one is dropped. Names are compared through a length-preserving diacritic fold,
so ``text[m.start:m.end] == m.text`` always holds. An empty ``agent_ids`` means the
mention is unresolved (``@NobodyBot``, an unknown ``<@U…>``); it is never guessed.
Bare surnames and "X et al." are citation style, not mentions.

Stdlib only: the authorship check (``src.agent.authorship_rules``) is its only caller,
and the engine's mention routing (``message_log._extract_tagged_agent``) is separate.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

#: One kind per GATE-8 rule, in rule order.
MENTION_KINDS = (
    "slack_uid", "bot_name", "pi_name", "lab_group", "titled_surname", "coauthor_surname",
)

#: Words that precede "lab(s)" without naming one. Copied verbatim from main's
#: ``_PROSE_LAB_STOPWORDS`` (main:src/agent/simulation.py:303-307).
_PROSE_LAB_STOPWORDS = frozenset({
    "our", "my", "their", "your", "his", "her", "its", "the", "a", "an",
    "this", "that", "these", "those", "each", "every", "both", "all", "any",
    "other", "another", "wet", "dry", "which", "whose", "one", "two",
})

#: GATE-7's co-authorship stems. Rule 6 only fires in a sentence that carries one, and
#: ``authorship_rules.claims_coauthorship`` tests the same pattern.
COAUTHOR_STEM_RE = re.compile(
    r"\bco-?(?:first-?)?author(?:ed|ship|s|ing)?\b"
    r"|\bco-?publish(?:ed|ing)?\b|\bco-?wr(?:ote|itten|ite)\b"
    r"|\bjointly\s+(?:co-?)?(?:authored|wrote|published)\b"
    r"|\bjoint\s+(?:paper|publication|preprint|article|manuscript)s?\b"
    r"|\bshared\s+(?:authorship|byline)\b"
    r"|\b(?:wrote|authored|published)\b[^.!?\n]{0,60}?\b(?:together\s+)?with\b",
    re.IGNORECASE,
)

_GROUP_NOUN = r"(?:labs?|laborator(?:y|ies)|groups?|teams?)"
_TITLE = r"(?:Dr\.?|Prof\.?|Professor)\s+"
_NAME_TOK = r"[A-Z][\w-]*"
_UID_RE = re.compile(r"<@([A-Za-z0-9]+)>")
_BOT_RE = re.compile(r"@?\b(\w+bot)\b", re.IGNORECASE)
_COORD_RE = re.compile(
    rf"\b({_NAME_TOK}(?:\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s*&\s*|/)\s*{_NAME_TOK}){{0,5}})"
    rf"(?:['’]s)?\s+(?i:{_GROUP_NOUN})\b"
)
_NAME_TOK_RE = re.compile(_NAME_TOK)
_TITLED_RE = re.compile(
    rf"\b(?i:{_GROUP_NOUN})\s+of\s+(?:{_TITLE})?({_NAME_TOK})\b|\b{_TITLE}({_NAME_TOK})\b"
)
#: Sentence ends for rule 6's co-author window: a newline, or [.!?] before whitespace or
#: the end, except after a single capital initial ("B. Good") or a title.
_SENTENCE_END_RE = re.compile(r"\n|(?<!\b[A-Z])(?<!\bDr)(?<!\bProf)[.!?]+(?=\s|$)")


@dataclass(frozen=True)
class RosterEntry:
    agent_id: str
    bot_name: str
    pi_name: str | None = None


@dataclass(frozen=True)
class LabMention:
    start: int
    end: int
    text: str
    agent_ids: frozenset[str]
    kind: str


@dataclass(frozen=True)
class _RosterIndex:
    by_bot: dict[str, str]
    by_full_name: dict[tuple[str, str], frozenset[str]]
    by_surname: dict[str, frozenset[str]]
    by_surname_folded: dict[str, frozenset[str]]
    full_name_re: re.Pattern[str] | None
    coauthor_re: re.Pattern[str] | None


def _fold(s: str) -> str:
    """Length-preserving diacritic fold: each letter becomes the first character of its
    NFKD form (``Müller`` → ``Muller``), every other character is kept."""
    return "".join(unicodedata.normalize("NFKD", ch)[0] if ch.isalpha() else ch for ch in s)


def _add(index: dict, key, agent_id: str) -> None:
    index[key] = index.get(key, frozenset()) | {agent_id}


@functools.lru_cache(maxsize=8)
def _roster_pattern(roster: tuple[RosterEntry, ...]) -> _RosterIndex:
    """Everything the six rules need for one roster, compiled once: the bot names, the
    full PI names (one alternation), the case-sensitive surname and title-cased agent-id
    index, and rule 6's case-insensitive surname alternation."""
    by_bot: dict[str, str] = {}
    by_full: dict[tuple[str, str], frozenset[str]] = {}
    by_surname: dict[str, frozenset[str]] = {}
    by_surname_folded: dict[str, frozenset[str]] = {}
    full_alts: set[str] = set()
    for e in roster:
        if e.bot_name:
            by_bot[e.bot_name.lower()] = e.agent_id
        parts = _fold((e.pi_name or "").strip()).split()
        if parts:
            _add(by_surname, parts[-1], e.agent_id)
            _add(by_surname_folded, parts[-1].lower(), e.agent_id)
        if len(parts) >= 2:
            _add(by_full, (parts[0].lower(), parts[-1].lower()), e.agent_id)
            full_alts.add(rf"{re.escape(parts[0])}(?:\s+[a-z]\.?)*\s+{re.escape(parts[-1])}")
        if e.agent_id.strip():
            _add(by_surname, e.agent_id.strip().title(), e.agent_id)
    full_name_re = None
    if full_alts:
        full_name_re = re.compile(
            rf"(?:\b(?:the\s+)?{_GROUP_NOUN}\s+of\s+(?:{_TITLE})?)?"
            rf"\b(?P<name>{'|'.join(sorted(full_alts, key=len, reverse=True))})\b"
            rf"(?:['’]s\s+{_GROUP_NOUN}\b)?",
            re.IGNORECASE,
        )
    coauthor_re = None
    if by_surname_folded:
        surnames = "|".join(re.escape(s) for s in sorted(by_surname_folded, key=len, reverse=True))
        coauthor_re = re.compile(
            rf"\b[a-z]\.\s*(?P<a>{surnames})\b|\b(?P<b>{surnames})\s+labs?\b", re.IGNORECASE
        )
    return _RosterIndex(by_bot, by_full, by_surname, by_surname_folded, full_name_re, coauthor_re)


def _coauthor_windows(text: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of every sentence of ``text`` that carries a co-author stem."""
    windows, last = [], 0
    for m in _SENTENCE_END_RE.finditer(text):
        windows.append((last, m.end()))
        last = m.end()
    windows.append((last, len(text)))
    return [(a, b) for a, b in windows if COAUTHOR_STEM_RE.search(text, a, b)]


def _group_mentions(folded: str, index: _RosterIndex
                    ) -> Iterator[tuple[int, int, frozenset[str]]]:
    """Rule 4's ``(start, end, agent_ids)``: each roster name in a coordinated run before
    a lab noun. The last name's span runs to the end of the noun."""
    for m in _COORD_RE.finditer(folded):
        run, base = m.group(1), m.start(1)
        for nm in _NAME_TOK_RE.finditer(run):
            token = nm.group(0)
            if token.lower() in _PROSE_LAB_STOPWORDS or token.lower() == "and":
                continue
            ids = index.by_surname.get(token)
            if ids:
                yield base + nm.start(), (m.end() if nm.end() == len(run) else base + nm.end()), ids


def _coauthor_mentions(folded: str, index: _RosterIndex
                       ) -> Iterator[tuple[int, int, frozenset[str]]]:
    """Rule 6's ``(start, end, agent_ids)``, inside co-author sentences only."""
    if index.coauthor_re is None:
        return
    for a, b in _coauthor_windows(folded):
        for m in index.coauthor_re.finditer(folded, a, b):
            ids = index.by_surname_folded.get((m.group("a") or m.group("b")).lower())
            if ids:
                yield m.start(), m.end(), ids


def find_lab_mentions(
    text: str,
    roster: Sequence[RosterEntry],
    bot_uid_to_agent: Mapping[str, str] | None = None,
) -> list[LabMention]:
    """Every lab mention in ``text``, sorted by start (see the module docstring)."""
    index = _roster_pattern(tuple(roster))
    folded = _fold(text)
    found: list[LabMention] = []
    taken: list[tuple[int, int]] = []

    def add(start: int, end: int, ids, kind: str) -> None:
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            return
        taken.append((start, end))
        found.append(LabMention(start, end, text[start:end], frozenset(ids), kind))

    # 1. <@U…>, through the bot-uid map.
    for m in _UID_RE.finditer(text):
        aid = (bot_uid_to_agent or {}).get(m.group(1))
        add(m.start(), m.end(), {aid} if aid else (), "slack_uid")
    # 2. @XBot (resolved or not) and a bare roster XBot, in any case.
    for m in _BOT_RE.finditer(text):
        aid = index.by_bot.get(m.group(1).lower())
        if m.group(0).startswith("@") or aid:
            add(m.start(), m.end(), {aid} if aid else (), "bot_name")
    # 3. The full PI name, any case, middle initials allowed, optional prefix and suffix.
    if index.full_name_re is not None:
        for m in index.full_name_re.finditer(folded):
            parts = m.group("name").split()
            ids = index.by_full_name.get((parts[0].lower(), parts[-1].lower()))
            if ids:
                add(m.start(), m.end(), ids, "pi_name")
    # 4. Up to six coordinated names, then lab(s)/group(s)/team(s); one mention per name.
    for start, end, ids in _group_mentions(folded, index):
        add(start, end, ids, "lab_group")
    # 5. "(lab|group|team) of (Dr./Prof.) Surname" and "Dr./Prof. Surname".
    for m in _TITLED_RE.finditer(folded):
        ids = index.by_surname.get(m.group(1) or m.group(2))
        if ids:
            add(m.start(), m.end(), ids, "titled_surname")
    # 6. Only in a sentence with a co-author stem: "Initial. Surname" and "surname lab".
    for start, end, ids in _coauthor_mentions(folded, index):
        add(start, end, ids, "coauthor_surname")
    return sorted(found, key=lambda x: x.start)
