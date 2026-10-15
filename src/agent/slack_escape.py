"""Escaping of model-written text for Slack (C9 of the 2026-10-15 publication-handling
design; Q6). A leaf module: it imports ``re`` only (tests/unit/test_leaf_modules.py).

``escape_for_slack`` runs on the wire form of every message ``AgentSlackClient`` posts,
after ``markdown_to_mrkdwn`` (``slack_client.wire_text``); the database keeps the source
text. Three steps, in order:

1. The DOI part of a doi.org URL is percent-encoded (``<``, ``>``, ``#``, ``&``, space),
   so a SICI DOI cannot end a Slack link early or open a mention.
2. ``<@U…>``, ``<#C…>``, ``<http(s)://…>`` and ``<mailto:…>`` tokens are kept, except
   that a bare ``&`` in a token's label becomes ``&amp;``.
3. Everywhere else ``&``, ``<`` and ``>`` are entity-escaped (an ``&amp;``, ``&lt;`` or
   ``&gt;`` already present is kept), which turns ``<!here>``, ``<!channel>``,
   ``<!everyone>``, ``<!subteam^…>`` and every other ``<…>`` into text. Code spans and
   fences are escaped too: Slack requires it.

Idempotent, and never more than 5 times the input's length
(``slack_client.WIRE_MAX_GROWTH``). Phase 2 replaces step 1's local DOI-part rule with
``src.agent.dois.find_dois`` (I6); tests/unit/test_slack_escape.py stays as the agreement
check.
"""

import re

_DOI_URL = re.compile(r"(?i)\bhttps?://(?:dx\.|www\.)?doi\.org/")
_DOI_PREFIX = re.compile(r"10\.\d{4,9}(?:\.\d+)*/")
_ENTITY = re.compile(r"&(?:amp|lt|gt);")
_ENTITY_CHAR = {"&amp;": "&", "&lt;": "<", "&gt;": ">"}
_ENTITY_TEXTS = tuple(_ENTITY_CHAR)
#: Characters that end a DOI part (besides whitespace and non-ASCII).
_STOPS = frozenset("\"'|`*?")
_OPENERS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_CLOSERS = frozenset(")]}>")
_TRAILING = frozenset(".,;:!_~")
_PERCENT = {"<": "%3C", ">": "%3E", "#": "%23", "&": "%26", " ": "%20"}
_TOKEN = re.compile(
    r"<(@[UWB][A-Z0-9]+|#C[A-Z0-9]+|(?:https?://|mailto:)[^\s<>|]+)(?:\|([^<>\n]*))?>"
)
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt);)")


def _units(text: str, start: int):
    """``(start, end, char)`` for each character of ``text`` from ``start``, an
    ``&amp;``/``&lt;``/``&gt;`` entity counting as the one character it stands for."""
    i = start
    while i < len(text):
        entity = _ENTITY.match(text, i)
        if entity:
            yield i, entity.end(), _ENTITY_CHAR[entity.group(0)]
            i = entity.end()
        else:
            yield i, i + 1, text[i]
            i += 1


def _doi_part_end(text: str, start: int) -> int:
    """Where the DOI part that starts at ``start`` ends: at whitespace, a stop
    character, a non-ASCII character, or a closer that does not match the innermost
    open ``(``, ``[``, ``{`` or ``<``; then trailing ``.,;:!_~`` are dropped (not the
    ``;`` that ends an entity). ``#`` is kept."""
    expected: list[str] = []
    end = start
    for _, unit_end, char in _units(text, start):
        if char.isspace() or char in _STOPS or not char.isascii():
            break
        if char in _OPENERS:
            expected.append(_OPENERS[char])
        elif char in _CLOSERS:
            if not expected or expected[-1] != char:
                break
            expected.pop()
        end = unit_end
    while end > start and text[end - 1] in _TRAILING and not text.endswith(_ENTITY_TEXTS, start, end):
        end -= 1
    return end


def _percent_encode(part: str) -> str:
    return "".join(_PERCENT.get(char, char) for _, _, char in _units(part, 0))


def _encode_doi_urls(text: str) -> str:
    """Step 1: percent-encode the DOI part of every doi.org URL whose part starts
    ``10.<digits>/``; any other match is left alone."""
    out: list[str] = []
    cursor = 0
    for match in _DOI_URL.finditer(text):
        if match.start() < cursor or not _DOI_PREFIX.match(text, match.end()):
            continue
        end = _doi_part_end(text, match.end())
        out.append(text[cursor:match.end()])
        out.append(_percent_encode(text[match.end():end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _escape_plain(text: str) -> str:
    return _BARE_AMP.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")


def _escape_outside_tokens(text: str) -> str:
    """Steps 2 and 3: keep the user, channel and link tokens (escaping ``&`` in their
    labels) and entity-escape everything else."""
    out: list[str] = []
    cursor = 0
    for match in _TOKEN.finditer(text):
        out.append(_escape_plain(text[cursor:match.start()]))
        target, label = match.group(1), match.group(2)
        out.append(f"<{target}>" if label is None
                   else f"<{target}|{_BARE_AMP.sub('&amp;', label)}>")
        cursor = match.end()
    out.append(_escape_plain(text[cursor:]))
    return "".join(out)


def escape_for_slack(text: str) -> str:
    """``text`` made safe to post as Slack mrkdwn (module docstring)."""
    return _escape_outside_tokens(_encode_doi_urls(text))
