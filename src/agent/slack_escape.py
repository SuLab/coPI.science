"""Escaping of model-written text for Slack (C9 of the 2026-10-15 publication-handling
design; Q6). Its only ``src`` import is the leaf ``src.agent.dois`` (I6, I11;
tests/unit/test_leaf_modules.py).

``escape_for_slack`` runs on the wire form of every message ``AgentSlackClient`` posts,
after ``markdown_to_mrkdwn`` (``slack_client.wire_text``); the database keeps the source
text. Three steps, in order:

1. ``<``, ``>``, ``#``, ``&`` and spaces in the DOI part of doi.org URLs are
   percent-encoded, so a SICI DOI cannot end a Slack link early or open a mention. The
   DOI spans are C2's ``find_dois`` (I6).
2. ``<@U…>``, ``<#C…>``, ``<http(s)://…>`` and ``<mailto:…>`` tokens are kept, except
   that a bare ``&`` in a token's label becomes ``&amp;``.
3. Everywhere else ``&``, ``<`` and ``>`` are entity-escaped (an ``&amp;``, ``&lt;`` or
   ``&gt;`` already present is kept), which turns ``<!here>``, ``<!channel>``,
   ``<!everyone>``, ``<!subteam^…>`` and every other ``<…>`` into text. Code spans and
   fences are escaped too: Slack requires it.

Idempotent, and never more than 5 times the input's length
(``slack_client.WIRE_MAX_GROWTH``). tests/unit/test_slack_escape.py is the agreement check
for step 1.
"""

import re

from src.agent.dois import find_dois

_DOI_URL = re.compile(r"(?i)https?://(?:dx\.|www\.)?doi\.org/")
#: Entities first, then the raw characters (C9): after this a DOI part holds no <, >, # or &.
_DOI_PART_ENCODING = (
    ("&lt;", "%3C"), ("&gt;", "%3E"), ("&amp;", "%26"),
    ("<", "%3C"), (">", "%3E"), ("#", "%23"), ("&", "%26"), (" ", "%20"),
)
_TOKEN = re.compile(
    r"<(@[UWB][A-Z0-9]+|#C[A-Z0-9]+|(?:https?://|mailto:)[^\s<>|]+)(?:\|([^<>\n]*))?>"
)
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt);)")


def _encode_doi_urls(text: str) -> str:
    """Step 1: percent-encode the DOI part of each doi.org URL, the DOI spans being C2's
    ``find_dois`` (I6). A bare DOI, or one after a scheme-less ``doi.org/``, is left to
    step 3's entity escaping. Parts are rewritten last to first, so earlier offsets stay
    valid. Idempotent: ``find_dois`` measures spans on the raw text before decoding, so a
    second pass finds the same span and changes nothing."""
    for citation in reversed(find_dois(text)):
        span = text[citation.start:citation.end]
        url = _DOI_URL.match(span)
        if url is None:
            continue
        part = span[url.end():]
        for raw, encoded in _DOI_PART_ENCODING:
            part = part.replace(raw, encoded)
        text = text[:citation.start] + span[:url.end()] + part + text[citation.end:]
    return text


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
