"""Consuming one assessment-chat answer
(docs/specs/2026-09-24-assessment-chat-design.md §5.4-§5.7).

Pure: no database and no HTTP. ``consume_stream`` relays a live stream's RAW events
to the SSE queue; ``outcome_from_final`` turns the SDK's final message into what is
persisted and shown; ``failure_outcome`` covers every exit that has no final
message. Kept apart from ``assessment_chat.py`` so each row of the spec's status and
cost tables is a unit test against ``tests/fakes.py::FakeAsyncAnthropic``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.models.assessment_chat import (
    CHAT_STATUS_COMPLETE,
    CHAT_STATUS_FAILED,
    CHAT_STATUS_REFUSED,
    CHAT_STATUS_TRUNCATED,
    TOKEN_FIELDS,
)
from src.services.assessment_chat_record import ChatRecord, normalize_url_token

logger = logging.getLogger(__name__)

#: `async emit(event_name, data)` — puts one SSE event on the answer's queue.
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

#: The drawer's citation markers are U+E000 and U+E001 (§8.3), so every code point of
#: the Basic Multilingual Plane's private-use area is removed from model text before
#: it is sent or stored — both as a literal code point and as a decimal or hex numeric
#: character reference (``&#57344;``, ``&#xE000;``), which a browser or the drawer's
#: own sanitizer would otherwise decode into one (SEC-2). Built with chr() so that no
#: escape sequence has to survive an editor.
_PRIVATE_USE_RE = re.compile(f"[{chr(0xE000)}-{chr(0xF8FF)}]")
#: A numeric character reference — decimal or hex, semicolon optional, leading zeros
#: allowed — checked against the private-use range and stripped only when it decodes
#: into it; every other reference (``&amp;``, ``&#65;``, ...) is left untouched.
#: The digit runs are bounded (leading zeros aside): a private-use code point has at
#: most five significant decimal or four hex digits, so a longer run is never one —
#: and an unbounded run would hand int() a string past Python's digit limit.
_NUMERIC_REF_RE = re.compile(
    r"&#(?:0*(?P<dec>[0-9]{1,7})|[xX]0*(?P<hex>[0-9A-Fa-f]{1,6}));?"
)

#: Constructs a URL has to be judged inside of. Code is left exactly as it is: marked
#: renders a code span or a fence as code, never as a link. Everything else that can
#: become a link survives only when its URL is, token for token, in the record (D20).
#: The "code" alternative matches ANY backtick-delimited span, not just a single
#: backtick: it has to recognize the multi-backtick spans `_code()` itself emits, or a
#: second `rewrite_links` pass would re-scan and mangle its own output (SB-2). "html"
#: is last so `<https://...>` is still taken by "auto" first; it covers a raw HTML tag
#: or comment, neither of which marked or DOMPurify treats as a link but which must
#: not reach the client verbatim (SB-3). "escape" is FIRST: a backslash-escaped ASCII
#: punctuation character is literal to marked (CommonMark), so ``\[`` never opens a
#: link and ``\``` never opens a code span — reading them otherwise would make the
#: second pass disagree with the first about what the text contains.
_MD_SPAN_RE = re.compile(
    r"(?P<escape>\\[!-/:-@\[-`{-~])"
    r"|(?P<fence>(?P<ticks>`{3,})[\s\S]*?(?P=ticks))"
    r"|(?P<code>(?P<cticks>`+)[^\n]*?(?P=cticks))"
    r"|(?P<refdef>^[ ]{0,3}(?:>[ ]?)*[ ]{0,3}(?:[-*+][ ]+|\d{1,9}[.)][ ]+)?\[[^\]\n]+\]:[^\n]*)"
    r"|(?P<image>!\[(?P<alt>[^\]\n]*)\]\((?P<idest>(?:[^()\n]|\([^()\n]*\))*)\))"
    r"|(?P<link>\[(?P<ltext>[^\]\n]*)\]\((?P<ldest>(?:[^()\n]|\([^()\n]*\))*)\))"
    r"|(?P<auto><(?P<adest>[A-Za-z][A-Za-z0-9+.-]*:[^>\s]*)>)"
    r"|(?P<html><!--[\s\S]*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>\n]*)?/?>)",
    re.MULTILINE,
)
#: A markdown link destination: optional angle brackets, then an optional title.
_LINK_DEST_RE = re.compile(r"""^\s*<?([^<>\s]*)>?(?:\s+(?:"[^"]*"|'[^']*'))?\s*$""")
#: A URL in a model's ANSWER: scheme-insensitive (`_is_allowed` still requires a
#: lowercase `https://`, so an uppercase scheme is isolated as its own token and then
#: coded, never left to blend into surrounding text) and widened to include `ftp://`
#: (PA1-9). `prose_citations._URL_RE` is the PAGE's tokenizer, shared with
#: `normalize_url_token`, and stays https/http-only and case-sensitive — it is a
#: different regex, not reused here.
_ANSWER_URL_RE = re.compile(r'(?:https?|ftp)://[^\s<>"`\\]+', re.IGNORECASE)
#: GFM autolinks a bare `www.` host as well as a scheme.
_WWW_RE = re.compile(r"(?<![\w/.@-])www\.[^\s<>`]+")
#: GFM also autolinks a bare e-mail address (as `mailto:`); the lookbehind keeps this
#: from firing mid-word or inside a URL's own path.
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
_BACKTICKS_RE = re.compile(r"`+")


def _strip_ref_if_private_use(match: re.Match) -> str:
    value = int(match.group("dec")) if match.group("dec") is not None else int(match.group("hex"), 16)
    return "" if 0xE000 <= value <= 0xF8FF else match.group()


def strip_private_use(text: str) -> str:
    """Remove every private-use code point, literal or numerically referenced
    (SEC-2: ``&#57344;``/``&#xE000;`` decode into one in a browser)."""
    # Literal code points first, then references until nothing changes: removing
    # either can join what surrounded it into a new reference (``&#&#xE000;57344;``).
    text = _PRIVATE_USE_RE.sub("", text)
    while True:
        stripped = _NUMERIC_REF_RE.sub(_strip_ref_if_private_use, text)
        if stripped == text:
            return text
        text = stripped


def display_cited_text(raw: object, label: str | None) -> str:
    """The Sources list's text for a citation: the cited block without its own label
    line and without the ``> `` quoting. Rendered only as text, never as markup."""
    text = strip_private_use(raw if isinstance(raw, str) else "")
    lines = text.split("\n")
    if label is not None and lines and lines[0].strip() == f"[{label}]":
        lines = lines[1:]
    cleaned = [line[2:] if line.startswith("> ") else line.removeprefix(">") for line in lines]
    return "\n".join(cleaned).strip()


@dataclass
class Segment:
    """One text block of an answer, and the citation numbers attached to it."""

    text: str
    cites: list[int] = field(default_factory=list)


def _int(value: object) -> int:
    return value if type(value) is int else 0


@dataclass
class UsageSnapshot:
    """The usage the stream has reported so far (§5.4): input and cache tokens from
    ``message_start``, the latest cumulative counts from ``message_delta``. It is what
    prices an answer that never reached a final message (§5.7).

    The API reports cumulative OUTPUT tokens only in ``message_delta``, sent once at
    the very end of the stream — ``message_start``'s usage is a placeholder (output
    tokens ≈ 1). ``delta_seen`` records whether a ``message_delta`` was ever absorbed,
    so a failure between the two (SEC-2/SB-5) can be told apart from a snapshot whose
    counts are the real, final ones."""

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    delta_seen: bool = False

    def absorb(self, usage: Any, *, model: str | None = None, delta: bool = False) -> None:
        if isinstance(model, str) and model:
            self.model = model
        if usage is None:
            return
        if delta:
            self.delta_seen = True
        for name in TOKEN_FIELDS:
            value = getattr(usage, name, None)
            # message_delta counts are cumulative and omit what does not apply,
            # so a present value overwrites and an absent one leaves the last.
            if type(value) is int:
                setattr(self, name, value)

    @property
    def captured(self) -> bool:
        return any(getattr(self, name) is not None for name in TOKEN_FIELDS)

    def entries(self, requested_model: str) -> list[dict[str, Any]]:
        """One ledger entry. Carries ``"partial": True`` when no ``message_delta`` was
        ever absorbed — the output-token count is then still ``message_start``'s
        placeholder, so the service prices this row at its reserve floor rather than
        trusting the count (SEC-4/SB-5)."""
        entry = {
            "model": self.model or requested_model,
            "billed": True,
            **{name: getattr(self, name) or 0 for name in TOKEN_FIELDS},
        }
        if not self.delta_seen:
            entry["partial"] = True
        return [entry]


class CitationBook:
    """Numbers one answer's citations in order of first appearance (§5.5); a repeat
    of the same ``(document, start block, end block)`` reuses its number."""

    def __init__(self, record: ChatRecord) -> None:
        self._record = record
        self._numbers: dict[tuple[Any, ...], int] = {}
        self.entries: list[dict[str, Any]] = []

    def add(self, citation: Any) -> tuple[int, dict[str, Any]] | None:
        if citation is None:
            return None
        kind = getattr(citation, "type", None)
        document_index = getattr(citation, "document_index", None)
        start = getattr(citation, "start_block_index", None)
        end = getattr(citation, "end_block_index", None)
        cited = getattr(citation, "cited_text", None)
        if kind == "content_block_location":
            key: tuple[Any, ...] = (kind, document_index, start, end)
        else:
            key = (kind, document_index, cited if isinstance(cited, str) else None)
        if key in self._numbers:
            number = self._numbers[key]
            return number, self.entries[number - 1]
        target = (
            self._record.target(document_index, start)
            if kind == "content_block_location"
            else None
        )
        if target is None:
            logger.warning(
                "Assessment chat: a %s citation (document %r, block %r) maps to no record block",
                kind, document_index, start,
            )
        number = len(self.entries) + 1
        entry = {
            "n": number,
            "doc": target.doc if target else None,
            "anchor": target.anchor if target else None,
            "label": target.label if target else "the record",
            "cited_text": display_cited_text(cited, target.label if target else None),
        }
        self._numbers[key] = number
        self.entries.append(entry)
        return number, entry


@dataclass
class StreamOutcome:
    """Everything that is persisted for one answer (§5.6, §5.7)."""

    status: str
    stop_reason: str | None = None
    refusal_category: str | None = None
    error_code: str | None = None
    answer_text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    allowed_links: list[str] = field(default_factory=list)
    served_by_model: str | None = None
    fallback_used: bool = False
    #: None = no usage recorded (counted at the reserve); [] = known unbilled.
    usage_by_model: list[dict[str, Any]] | None = None


def _code(text: str) -> str:
    """``text`` as an inline code span that no backtick inside it can close early."""
    longest = max((len(run) for run in _BACKTICKS_RE.findall(text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _is_allowed(url: str, allowed: frozenset[str]) -> bool:
    return url.startswith("https://") and url in allowed


def _keep(found: list[str], url: str) -> None:
    if url not in found:
        found.append(url)


def _escape_bang(text: str) -> str:
    """``![`` reads as an image opener to marked wherever it lands, even in text
    this module only meant as a label or trailing punctuation; escape it so a
    rewritten answer can never accidentally form an image (SB-4)."""
    return text.replace("![", "!\\[")


def _neutralize_plain(text: str) -> str:
    """A plain-text chunk between two URL matches: bare ``www.`` hosts and e-mail
    addresses (both autolinked by GFM) are coded, and ``![`` is escaped (PA1-9,
    SB-4). Never applied to text this module already generated — a code span or a
    ``<url>`` autolink must never be re-scanned, which is what keeps a second
    ``rewrite_links`` pass idempotent (SB-2)."""
    text = _escape_bang(text)
    text = _WWW_RE.sub(lambda m: _code(m.group()), text)
    return _EMAIL_RE.sub(lambda m: _code(m.group()), text)


def _peel_emphasis(token: str, allowed: frozenset[str]) -> str:
    """Strip trailing markdown emphasis markers speculatively (SW-8): ``**url**``
    or ``_url_`` tokenizes with the closing markers attached, so a URL wrapped in
    emphasis never matches ``allowed`` on the first try. Returns the first shorter
    candidate that is allowed, or ``token`` unchanged once stripping stops
    changing anything."""
    candidate = token
    while True:
        stripped = candidate.rstrip("*_~")
        if stripped == candidate:
            return token
        candidate = normalize_url_token(stripped)
        if _is_allowed(candidate, allowed):
            return candidate


def _rewrite_bare(
    text: str, allowed: frozenset[str], found: list[str], *, bracket: bool = True
) -> str:
    """Bare URLs in ``text`` (no markdown link/image/autolink syntax around them).
    An allowed URL is emitted as an angle-bracket autolink, ``<url>``, so its extent
    never depends on what follows it (B1) — except inside a link label, where marked
    never autolinks and an autolink would nest anchors; callers pass
    ``bracket=False`` there."""
    out: list[str] = []
    pos = 0
    for match in _ANSWER_URL_RE.finditer(text):
        raw = match.group()
        out.append(_neutralize_plain(text[pos : match.start()]))
        token = normalize_url_token(raw)
        chosen = token
        if token and not _is_allowed(token, allowed):
            chosen = _peel_emphasis(token, allowed)
        if chosen and _is_allowed(chosen, allowed):
            out.append(f"<{chosen}>" if bracket else chosen)
            _keep(found, chosen)
        elif token:
            out.append(_code(token))
        out.append(_escape_bang(raw[len(chosen) :]))
        pos = match.end()
    out.append(_neutralize_plain(text[pos:]))
    return "".join(out)


def _destination(raw: str) -> str:
    match = _LINK_DEST_RE.match(raw)
    return match.group(1) if match else raw.strip()


def rewrite_links(text: str, allowed: frozenset[str]) -> tuple[str, list[str]]:
    """Make every link in ``text`` inert unless its URL is, token for token, one of
    ``allowed`` (the tier's record URLs) and https (§5.5, D20).

    Returns the rewritten text and the allowed URLs it kept, in order of first
    appearance. An inert URL becomes inline code, which marked never autolinks;
    ``[text](url)`` becomes ``text (`url`)``; an image is never kept; a reference
    definition, and a raw HTML tag or comment, become code. This is the server half
    of the rule — the drawer enforces it again on the rendered DOM.

    Idempotent by construction (SB-2: ``outcome_from_final`` runs this a second time
    across a segment boundary): every construct this function emits — the ``<url>``
    autolink, a coded token, a coded reference-definition line, a coded HTML
    tag/comment — is itself one of the spans ``_MD_SPAN_RE`` recognizes and passes
    through unchanged on a later pass.
    """
    found: list[str] = []
    return _rewrite(text, allowed, found), found


def _rewrite(text: str, allowed: frozenset[str], found: list[str], *, bracket: bool = True) -> str:
    """`rewrite_links`' body, reused for a link's label and an image's alt text so a
    code span or an escape INSIDE them is recognized as one (a bare pass over a label
    would code a URL already inside backticks). ``bracket=False`` is an allowed
    link's label: marked never autolinks inside a link, and an autolink there would
    nest anchors, so an allowed URL stays bare."""
    out: list[str] = []
    pos = 0
    for match in _MD_SPAN_RE.finditer(text):
        out.append(_rewrite_bare(text[pos : match.start()], allowed, found, bracket=bracket))
        if (
            match.group("escape") is not None
            or match.group("fence") is not None
            or match.group("code") is not None
        ):
            out.append(match.group())
        elif match.group("refdef") is not None:
            out.append(_code(match.group().strip()))
        elif match.group("image") is not None:
            alt = _rewrite(match.group("alt"), allowed, found)
            dest = _destination(match.group("idest"))
            out.append(f"{alt} ({_code(dest)})" if dest else alt)
        elif match.group("link") is not None:
            dest = _destination(match.group("ldest"))
            allowed_dest = bool(dest) and _is_allowed(dest, allowed)
            label = _rewrite(match.group("ltext"), allowed, found, bracket=not allowed_dest)
            if allowed_dest:
                out.append(f"[{label}]({dest})")
                _keep(found, dest)
            elif dest:
                out.append(f"{label} ({_code(dest)})")
            else:
                out.append(label)
        elif match.group("auto") is not None:
            dest = match.group("adest")
            if _is_allowed(dest, allowed):
                out.append(f"<{dest}>" if bracket else dest)
                _keep(found, dest)
            else:
                out.append(_code(dest))
        else:
            out.append(_code(match.group()))
        pos = match.end()
    out.append(_rewrite_bare(text[pos:], allowed, found, bracket=bracket))
    return "".join(out)


def map_stop(stop_reason: object, text: str) -> tuple[str, str | None]:
    """(status, error_code) for an exit that produced a final message (§5.6)."""
    blank = not text.strip()
    if stop_reason == "end_turn":
        return (CHAT_STATUS_FAILED, "empty_answer") if blank else (CHAT_STATUS_COMPLETE, None)
    if stop_reason == "max_tokens":
        # Adaptive thinking can spend the whole budget before any text; a blank
        # truncated answer must never be replayed (the API rejects empty text).
        return (CHAT_STATUS_FAILED, "empty_answer") if blank else (CHAT_STATUS_TRUNCATED, None)
    if stop_reason == "refusal":
        return CHAT_STATUS_REFUSED, None
    # Any other stop reason (e.g. "pause_turn") is not one the client or the history
    # view can name from the reason alone, so it gets the same fixed error text both
    # surfaces already know how to render (PA1-6).
    return CHAT_STATUS_FAILED, "unexpected_stop"


def _tokens(source: object) -> dict[str, int]:
    return {name: _int(getattr(source, name, None)) for name in TOKEN_FIELDS}


def usage_entries(final: Any) -> list[dict[str, Any]]:
    """The final message's usage as ledger entries (§5.7): one per
    ``usage.iterations`` entry, each under its own model (a fallback bills at the
    fallback model's rates), else the top-level usage under ``final.model``. A
    ``message`` iteration with no output that precedes a ``fallback_message`` is a
    pre-output decline — "reported but not billed" — kept with ``billed: False``."""
    usage = getattr(final, "usage", None)
    model = getattr(final, "model", None)
    iterations = list(getattr(usage, "iterations", None) or [])
    if not iterations:
        return [{"model": model, "billed": True, **_tokens(usage)}]
    entries = []
    for i, iteration in enumerate(iterations):
        tokens = _tokens(iteration)
        declined_before_output = (
            getattr(iteration, "type", None) == "message"
            and tokens["output_tokens"] == 0
            and any(
                getattr(later, "type", None) == "fallback_message" for later in iterations[i + 1 :]
            )
        )
        entries.append(
            {
                "model": getattr(iteration, "model", None) or model,
                "billed": not declined_before_output,
                **tokens,
            }
        )
    return entries


def billed_sums(entries: list[dict[str, Any]] | None) -> dict[str, int | None]:
    """The ledger's four summed columns: billed entries only; all NULL when no usage
    was recorded at all."""
    if entries is None:
        return dict.fromkeys(TOKEN_FIELDS)
    return {
        name: sum(_int(entry.get(name)) for entry in entries if entry.get("billed", True))
        for name in TOKEN_FIELDS
    }


def served_by(final: Any) -> str | None:
    """The model that produced the answer's end: the last message iteration's model
    when iterations exist (a mid-stream fallback does not change ``final.model``),
    else ``final.model``."""
    iterations = list(getattr(getattr(final, "usage", None), "iterations", None) or [])
    for iteration in reversed(iterations):
        model = getattr(iteration, "model", None)
        if getattr(iteration, "type", None) in ("message", "fallback_message") and model:
            return model
    return getattr(final, "model", None)


def _clip(value: object, limit: int = 40) -> str | None:
    return None if value is None else str(value)[:limit]


def outcome_from_final(final: Any, *, record: ChatRecord, requested_model: str) -> StreamOutcome:
    """What is persisted and shown for an answer that reached a final message.
    ``final.content``'s text blocks and their citations are authoritative; the
    streamed deltas were a preview."""
    book = CitationBook(record)
    segments: list[Segment] = []
    for block in getattr(final, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        cites: list[int] = []
        for citation in getattr(block, "citations", None) or []:
            added = book.add(citation)
            if added is not None and added[0] not in cites:
                cites.append(added[0])
        segments.append(Segment(text=strip_private_use(getattr(block, "text", "") or ""), cites=cites))
    allowed_links: list[str] = []
    for segment in segments:
        segment.text, kept = rewrite_links(segment.text, record.url_tokens)
        for url in kept:
            _keep(allowed_links, url)
    joined = "".join(segment.text for segment in segments)
    # A construct (a link, a URL) can be split across the citation boundary the API
    # drew between two text blocks, invisible to each segment's own rewrite. Run the
    # joined text back through rewrite_links; if that changes anything, joining made
    # something no single segment showed, and the only fail-closed answer is to
    # collapse to one segment so nothing can straddle a boundary again (SB-2).
    rejoined, rejoined_kept = rewrite_links(joined, record.url_tokens)
    answer_text = joined
    if rejoined != joined:
        logger.warning(
            "Assessment chat: a link spanned a citation boundary; the answer's "
            "citations were merged into one segment"
        )
        merged_cites: list[int] = []
        for segment in segments:
            for number in segment.cites:
                if number not in merged_cites:
                    merged_cites.append(number)
        segments = [Segment(text=rejoined, cites=merged_cites)]
        allowed_links = []
        for url in rejoined_kept:
            _keep(allowed_links, url)
        answer_text = rejoined
    stop_reason = getattr(final, "stop_reason", None)
    status, error_code = map_stop(stop_reason, answer_text)
    citations = book.entries
    refusal_category = None
    if status == CHAT_STATUS_REFUSED:
        details = getattr(final, "stop_details", None)
        refusal_category = _clip(getattr(details, "category", None)) if details is not None else None
        segments, citations, allowed_links, answer_text = [], [], [], ""
    served = served_by(final)
    iterations = list(getattr(getattr(final, "usage", None), "iterations", None) or [])
    fallback_used = any(getattr(it, "type", None) == "fallback_message" for it in iterations) or (
        served is not None and served != requested_model
    )
    return StreamOutcome(
        status=status,
        stop_reason=_clip(stop_reason),
        refusal_category=refusal_category,
        error_code=error_code,
        answer_text=answer_text,
        segments=[{"text": s.text, "cites": s.cites} for s in segments],
        citations=citations,
        allowed_links=allowed_links,
        served_by_model=served,
        fallback_used=fallback_used,
        usage_by_model=usage_entries(final),
    )


def failure_outcome(
    *,
    error_code: str | None,
    requested_model: str,
    snapshot: UsageSnapshot,
    known_unbilled: bool = False,
    status: str = CHAT_STATUS_FAILED,
) -> StreamOutcome:
    """An exit with no final message (§5.6, §5.7). Usage comes from the stream's
    snapshot when it reported any; otherwise it is ``[]`` when the request is known
    not to have been billed (an HTTP error before the stream started) and ``None``
    — counted at the reserve — when nobody knows."""
    if snapshot.captured:
        usage: list[dict[str, Any]] | None = snapshot.entries(requested_model)
    elif known_unbilled:
        usage = []
    else:
        usage = None
    return StreamOutcome(
        status=status,
        error_code=error_code,
        served_by_model=snapshot.model,
        fallback_used=bool(snapshot.model and snapshot.model != requested_model),
        usage_by_model=usage,
    )


def _model_of(value: object) -> str | None:
    if isinstance(value, str):
        return value
    model = getattr(value, "model", None)
    return model if isinstance(model, str) else None


async def consume_stream(
    stream: Any, *, emit: Emit, snapshot: UsageSnapshot, book: CitationBook
) -> None:
    """Relay one live stream (§5.4). Acts on RAW events only: the SDK also yields
    convenience events (``text``, ``citation``, ``thinking``, ``signature``) for the
    same deltas, and handling both would double every character."""
    segment_of: dict[int, int] = {}
    answering = False
    async for event in stream:
        kind = getattr(event, "type", None)
        if kind == "message_start":
            message = getattr(event, "message", None)
            snapshot.absorb(getattr(message, "usage", None), model=getattr(message, "model", None))
        elif kind == "message_delta":
            snapshot.absorb(getattr(event, "usage", None), delta=True)
        elif kind == "content_block_start":
            block = getattr(event, "content_block", None)
            block_type = getattr(block, "type", None)
            if block_type == "text":
                segment_of[getattr(event, "index", -1)] = len(segment_of)
                if not answering:
                    answering = True
                    await emit("status", {"state": "answering"})
            elif block_type in ("thinking", "redacted_thinking"):
                await emit("status", {"state": "thinking"})
            elif block_type == "fallback":
                await emit(
                    "notice",
                    {
                        "kind": "fallback",
                        "from_model": _model_of(getattr(block, "from_", None)),
                        "to_model": _model_of(getattr(block, "to", None)),
                    },
                )
        elif kind == "content_block_delta":
            segment = segment_of.get(getattr(event, "index", -1))
            if segment is None:
                continue
            delta = getattr(event, "delta", None)
            delta_type = getattr(delta, "type", None)
            if delta_type == "text_delta":
                text = strip_private_use(getattr(delta, "text", "") or "")
                if text:
                    await emit("text", {"seg": segment, "text": text})
            elif delta_type == "citations_delta":
                added = book.add(getattr(delta, "citation", None))
                if added is not None:
                    await emit("citation", {"seg": segment, "citation": added[1]})
