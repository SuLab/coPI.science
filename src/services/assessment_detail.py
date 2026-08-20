"""One screening verdict, plus a reconstruction of the interview behind it.

The assessments list answers "which verdict deserves attention". This module
answers the next two questions, which nothing in the app could answer before:
*on what basis* was the verdict reached, and *who was asked*.

Two sources, deliberately kept separate:

* ``specialist_consults`` (``src/models/specialist_consult.py``) — the durable,
  forward-only record of a panel consult. It is EMPTY for every assessment that
  already exists: the table is written from the engine's consult success path,
  so rows only appear for runs that happen after it shipped.
* ``llm_call_logs.messages_json`` for the hub's ``thread_reply`` rows — the full
  tool conversation of every hub turn, ``tool_use`` blocks (tool + input) paired
  with ``tool_result`` blocks (including complete specialist opinions). This has
  been captured durably all along, which is what makes the timeline work
  RETROACTIVELY for the 29 assessments already on record. Parsing it at read
  time was chosen over a DB backfill (plan decision 3): no migration of
  inferred rows, and no risk of an inference becoming indistinguishable from a
  recorded fact.

Everything derived from ``llm_call_logs`` is admin-only (``admin_view``), along
with ``raw_opinion``: the LLM drill-down is an admin surface and managers
deliberately do not get one. Managers still see each consult's domain, signal,
confidence, concerns and questions_to_ask — the substance of what the panel
said. That split is a recorded policy decision, not an accident.

The redaction is done HERE, by omitting the values from the returned context,
rather than only by not rendering them in the manager template: a template that
never prints a value still ships it to anyone who can read the page source, and
a later template edit would silently widen the audience.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.specialists import PANEL_REQUIRED_FOR, parse_opinion
from src.models import AgentMessage, LlmCallLog, OpportunityAssessment, SpecialistConsult
from src.services.blackbird_rubric import (
    BANDING,
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
    load_rubric,
)

# Hard bounds. This page is a read of unbounded production data: a channel can
# hold hundreds of hub turns and a retrieve_full_text result can be an entire
# paper, so every list and every blob here is capped rather than trusted.
LOG_SCAN_LIMIT = 200
MESSAGE_SCAN_LIMIT = 500
RESULT_EXCERPT_CHARS = 200
RESULT_FULL_CHARS = 4000
INPUT_SUMMARY_CHARS = 200
# Padding on the thread's own time span when selecting log rows. Six of
# production's channels host 29 assessments — several interviews per channel —
# so (run, agent, channel) alone would pull in turns from a DIFFERENT interview
# and file them under this one. The thread's first/last message bracket the
# turns that produced it; the pad absorbs clock skew between the log row's
# server-side created_at and the message's writer-side posted_at (measured at
# ~0.15s, but a pad costs nothing).
LOG_WINDOW_PAD_SECONDS = 300.0
# A turn is matched to the message it produced by normalized-text equality;
# failing that, by a normalized PREFIX this long. Measured over production run
# 88d81cd8 (116 hub thread_reply rows): exact matched 111, the prefix fallback
# recovered 2 more, 3 stayed unplaced. 100 chars is long enough that two
# distinct replies colliding on it is not a realistic outcome, and short enough
# to survive a trailing edit (a stripped mention, a stripped sidecar).
PREFIX_MATCH_CHARS = 100


# ---------------------------------------------------------------------------
# Text helpers
#
# These two regex families are REIMPLEMENTED here rather than imported from
# src/agent/simulation.py (which owns the canonical posting-path versions).
# Importing them would pull the whole engine — src.agent.agent, slack_client,
# tools, services.llm, the Anthropic SDK — into the web tier's import graph for
# two regexes, and would couple a read-only page to the module most likely to
# be mid-edit. If the posting path's tag handling ever changes, this correlator
# degrades to "unplaced turns", which the page renders explicitly; it does not
# break.
# ---------------------------------------------------------------------------

_SIDECAR_RE = re.compile(
    r"<\s*assessment_json\s*>\s*(.*?)\s*<\s*/\s*assessment_json\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SIDECAR_UNCLOSED_RE = re.compile(r"<\s*assessment_json\s*>.*", re.DOTALL | re.IGNORECASE)
_SIDECAR_ORPHAN_TAG_RE = re.compile(r"<\s*/?\s*assessment_json\s*>", re.IGNORECASE)


def strip_assessment_sidecar(text: str) -> str:
    """Drop the ``<assessment_json>`` sidecar, closed or truncated.

    The posted message never carries it, so a correlator that left it in would
    compare a body that includes the verdict JSON against one that does not and
    never match.
    """
    text = _SIDECAR_RE.sub("", text or "")
    text = _SIDECAR_UNCLOSED_RE.sub("", text)
    return _SIDECAR_ORPHAN_TAG_RE.sub("", text)


def extract_slack_message(text: str) -> str:
    """The ``<slack_message>`` body, or the whole text when there is no block.

    Anchored on the LAST closing tag and the last opening tag before it, the
    same way the posting path is: a model that mentions ``<slack_message>``
    inside its reasoning must not anchor the match and drag the reasoning into
    the body.
    """
    raw = text or ""
    last_close = raw.rfind("</slack_message>")
    if last_close >= 0:
        last_open = raw.rfind("<slack_message>", 0, last_close)
        if last_open >= 0:
            return raw[last_open + len("<slack_message>") : last_close]
    return raw


def normalize_for_match(text: str) -> str:
    """Collapse every whitespace run to one space and trim.

    Not cosmetic: the logged response and the stored message differ by exactly
    this much in production (a leading newline inside the tag), and comparing
    them un-normalized matched 12 of 726 rows instead of 111 of 116.
    """
    return " ".join((text or "").split())


def visible_body(response_text: str) -> str:
    """The normalized text a hub turn actually posted, from its raw response."""
    return normalize_for_match(strip_assessment_sidecar(extract_slack_message(response_text)))


# ---------------------------------------------------------------------------
# Tool-conversation parsing
# ---------------------------------------------------------------------------

# The engine returns a successful consult as
# "<Specialist Title> — signal: <signal>\n\n<raw opinion>"
# (src/agent/tools.py::_execute_consult_specialist). Its absence is how a
# FAILED consult is told apart from an opinion: an unknown domain, a missing
# persona file, an API error and an empty reply all return prose with no signal
# line, and none of them may be shown as if a specialist had cleared anything.
_CONSULT_SIGNAL_RE = re.compile(r"signal:\s*(blocking|caution|clear)\b", re.IGNORECASE)

CONSULT_TOOL_NAME = "consult_specialist"

# Input keys worth showing first in a chip's one-line summary, in this order.
# Anything else the tool was passed follows, so a new tool still summarizes.
_SUMMARY_KEYS = (
    "domain", "query", "question", "agent_id", "pmid", "doi", "identifier", "title", "url",
)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _result_text(content: object) -> str:
    """Flatten a ``tool_result`` block's content to text.

    The engine writes a plain string; the API's own schema also permits a list
    of blocks, and a JSON column will happily hand back either.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _input_summary(tool_input: object) -> str:
    if isinstance(tool_input, str):
        return _truncate(normalize_for_match(tool_input), INPUT_SUMMARY_CHARS)
    if not isinstance(tool_input, dict):
        return ""
    ordered = [k for k in _SUMMARY_KEYS if k in tool_input]
    ordered += [k for k in tool_input if k not in _SUMMARY_KEYS]
    # Whole key/value pieces, dropped rather than sliced: truncating the joined
    # string mid-key left summaries ending in things like "· conte" (observed
    # against real consult inputs). The most identifying keys come first, so
    # what gets dropped is the least useful part.
    separator = " · "
    parts: list[str] = []
    used = 0
    dropped = False
    for key in ordered:
        value = tool_input.get(key)
        if value is None or value == "":
            continue
        piece = f"{key}: {_truncate(normalize_for_match(str(value)), 120)}"
        if parts and used + len(separator) + len(piece) > INPUT_SUMMARY_CHARS:
            dropped = True
            break
        used += (len(separator) if parts else 0) + len(piece)
        parts.append(piece)
    summary = separator.join(parts)
    return f"{summary} …" if dropped else summary


def consult_opinion_from_result(result: str, *, domain: str) -> dict[str, Any] | None:
    """The specialist opinion a ``consult_specialist`` result carries, or None.

    None means the call did not produce an opinion (refused domain, missing
    persona, API error, empty reply) — a state that must never render as a
    verdict signal.
    """
    text = result or ""
    match = _CONSULT_SIGNAL_RE.search(text)
    if match is None:
        return None
    brace = text.find("{")
    if brace < 0:
        # Prose opinion: the engine's own parse is in the prefix line, and
        # there is no JSON body to read concerns/questions out of.
        return {
            "verdict_signal": match.group(1).lower(),
            "confidence": None,
            "concerns": [],
            "questions_to_ask": [],
        }
    opinion = parse_opinion(text[brace:], domain=domain)
    return {
        "verdict_signal": opinion.verdict_signal,
        "confidence": opinion.confidence,
        "concerns": list(opinion.concerns),
        "questions_to_ask": list(opinion.questions_to_ask),
    }


def tool_chips_from_conversation(messages_json: object) -> list[dict[str, Any]]:
    """One chip per tool call in a logged hub turn, in call order.

    ``messages_json`` is the conversation ``generate_with_tools`` accumulated:
    a user string, then alternating assistant messages (a block list holding
    ``thinking``/``text``/``tool_use``) and user messages (a block list of
    ``tool_result``). Results are matched to calls by ``tool_use_id``, not by
    position — a round with two calls interleaves them.

    ``thinking`` blocks are skipped entirely: they carry a signature and no
    reader value, and they are the bulk of the payload's bytes.
    """
    if not isinstance(messages_json, list):
        return []
    uses: list[tuple[str | None, str, object]] = []
    results: dict[str | None, str] = {}
    for message in messages_json:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append((
                    block.get("id"),
                    str(block.get("name") or "tool"),
                    block.get("input"),
                ))
            elif block.get("type") == "tool_result":
                results[block.get("tool_use_id")] = _result_text(block.get("content"))

    chips: list[dict[str, Any]] = []
    for use_id, name, tool_input in uses:
        result = results.get(use_id, "")
        domain = None
        if isinstance(tool_input, dict) and tool_input.get("domain"):
            domain = str(tool_input["domain"])
        is_consult = name == CONSULT_TOOL_NAME
        opinion = (
            consult_opinion_from_result(result, domain=domain or "unknown")
            if is_consult
            else None
        )
        chips.append({
            "tool": name,
            "is_consult": is_consult,
            "domain": domain,
            "summary": _input_summary(tool_input),
            "question": (
                str(tool_input.get("question") or "")
                if isinstance(tool_input, dict)
                else ""
            ),
            "opinion": opinion,
            "result_excerpt": _truncate(normalize_for_match(result), RESULT_EXCERPT_CHARS),
            "result_full": _truncate(result, RESULT_FULL_CHARS),
            "result_truncated": len(result) > RESULT_FULL_CHARS,
            "no_result": not result,
        })
    return chips


def correlate_turns_to_messages(
    turns: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Attach each logged turn to the message it posted.

    ``turns`` need a ``body`` (normalized posted text); ``messages`` need a
    ``key`` and a ``content_normalized``. Returns ``({key: [turn, ...]},
    unplaced)``. An unmatched turn is RETURNED, never dropped: a turn whose
    reply was edited, mention-stripped or truncated is still evidence of what
    the hub did, and silently discarding it would make the page look complete
    when it is not.
    """
    exact: dict[str, str] = {}
    by_prefix: dict[str, str] = {}
    for message in messages:
        norm = message.get("content_normalized") or ""
        if not norm:
            continue
        exact.setdefault(norm, message["key"])
        if len(norm) >= PREFIX_MATCH_CHARS:
            by_prefix.setdefault(norm[:PREFIX_MATCH_CHARS], message["key"])

    matched: dict[str, list[dict[str, Any]]] = {}
    unplaced: list[dict[str, Any]] = []
    for turn in turns:
        body = turn.get("body") or ""
        key = exact.get(body)
        if key is None and len(body) >= PREFIX_MATCH_CHARS:
            key = by_prefix.get(body[:PREFIX_MATCH_CHARS])
        if key is None:
            unplaced.append(turn)
        else:
            matched.setdefault(key, []).append(turn)
    return matched, unplaced


# ---------------------------------------------------------------------------
# The detail view
#
# `panel_summary_by_thread` used to live above this line and served the
# discussions pages' per-thread indicator. It is gone: both callers now use
# `src/services/thread_panel.py::panel_cards_by_thread`, which answers the same
# question with the full cards those pages expanded to need, keyed on the
# threads a render is actually showing rather than on the whole run — so the
# indicator and the cards are one query and cannot disagree. This module's own
# `panel_summary` (below) is built from the assessment's consults, and never
# called that function.
# ---------------------------------------------------------------------------


def _epoch(value: datetime | None) -> float:
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _message_at(message: AgentMessage) -> float:
    """When a message was posted, as an epoch float.

    ``posted_at`` is the writer's clock and is the ordering key everywhere else
    in the app; it carries a server_default of 0, so rows written before that
    column was populated fall back to the DB clock.
    """
    return float(message.posted_at or 0.0) or _epoch(message.created_at)


def _panel_state(assessment: OpportunityAssessment) -> str:
    """The FOUR findings the specialist floor can leave behind.

    Three come straight from ``OpportunityAssessment.missing_domains``: names = a
    demonstrated gap, ``[]`` = the floor could not be checked at all, NULL = no
    gap recorded. Rows written before 2026-08-19 carry NULL for both of the last
    two, so "verified" on an old row means "no gap recorded", which the page says.

    The fourth splits that NULL, because it was answering two questions with one
    word. A NULL row is a real verification only if the verdict was held to the
    floor in the first place, and ``PANEL_REQUIRED_FOR`` covers just
    ``advance``/``conditional`` — for a ``pass`` or ``route-to-incubation``,
    ``_specialist_floor_gap`` returns an empty set before it looks at a single
    consult. Reporting that as "verified" claimed an audit that never ran:
    production run 60c53424's pearce ``route-to-incubation`` row rendered the
    green box while ``required_domains_for`` named ``clinical`` and no clinical
    consult existed on that thread.

    ``not_owed`` is the weakest claim of the four and yields to every other one.
    A stored gap or a stored ``[]`` is evidence about this row, and evidence
    outranks the exemption — otherwise reading a flagged exempt row (which
    today's floor cannot produce, but historical rows and a future floor change
    both can) would silently unflag it. An absent ``recommendation`` also lands
    here: a verdict whose recommendation we cannot read cannot be shown to have
    faced the floor.
    """
    if assessment.panel_incomplete:
        return "gap"
    if assessment.missing_domains is not None:
        return "unverified"
    if assessment.recommendation not in PANEL_REQUIRED_FOR:
        return "not_owed"
    return "verified"


async def build_assessment_detail(
    db: AsyncSession, assessment_id: uuid.UUID, *, admin_view: bool
) -> dict[str, Any] | None:
    """One assessment, its dimension breakdown, and its interview timeline.

    Returns None when there is no such assessment (the router owns the 404 —
    this module stays HTTP-free, like src/services/directory.py).

    ``admin_view=False`` omits every admin-only value from the returned
    context: no ``raw_opinion``, no tool activity. See the module docstring.
    """
    assessment = (
        await db.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == assessment_id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        return None

    rubric = load_rubric()
    scores = assessment.scores if isinstance(assessment.scores, dict) else {}
    normalized_scores = {
        key.strip().lower(): value for key, value in scores.items() if isinstance(key, str)
    }
    dimensions = []
    for key, weight in RUBRIC_WEIGHTS.items():
        raw = normalized_scores.get(key)
        value = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )
        dimensions.append({
            "key": key,
            "weight": weight,
            "score": value,
            # Bar width as a percentage of the scale, clamped: a verdict can
            # carry an out-of-range score and a >100% width would overflow the
            # track. The weighted score itself clamps the same way.
            "pct": (
                min(100.0, max(0.0, value / rubric.scale_max * 100.0))
                if value is not None
                else 0.0
            ),
        })

    thread_id, messages = await _load_thread_messages(db, assessment)
    consults = await _load_consults(db, assessment, thread_id, admin_view=admin_view)

    message_views = [
        {
            "key": str(message.id),
            "agent_id": message.agent_id,
            "sender_name": message.sender_name,
            "channel_name": message.channel_name,
            "is_hub": message.agent_id == assessment.agent_id,
            "phase": message.phase,
            "content": message.content,
            "content_normalized": normalize_for_match(message.content),
            "at": _message_at(message),
            "is_verdict_message": bool(
                assessment.slack_ts
                and assessment.slack_ts in (message.slack_ts, message.message_ts)
            ),
        }
        for message in messages
    ]

    turns: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    matched: dict[str, list[dict[str, Any]]] = {}
    logs_scanned = 0
    if admin_view and message_views:
        turns, logs_scanned = await _load_tool_turns(db, assessment, message_views)
        matched, unplaced = correlate_turns_to_messages(turns, message_views)

    timeline: list[dict[str, Any]] = []
    for view in message_views:
        timeline.append({
            "kind": "message",
            "at": view["at"],
            "message": view,
            "tool_turns": matched.get(view["key"], []),
        })
    for consult in consults:
        timeline.append({
            "kind": "consult",
            "at": _epoch(consult["created_at"]),
            "consult": consult,
        })
    # Stable sort: messages were appended before consults, so a consult and the
    # reply it informed landing on the same timestamp read message-then-consult
    # rather than in an arbitrary order.
    timeline.sort(key=lambda entry: entry["at"])

    # Counted over PLACED turns only, not over every scanned turn.
    # `_load_tool_turns` selects log rows by (run, phase, agent, channel, time
    # window) because `llm_call_logs` carries no thread id, and several
    # interviews share a channel — so the scan legitimately returns other
    # threads' turns and `correlate_turns_to_messages` hands them back as
    # `unplaced`. Summing over `turns` therefore attributed other interviews'
    # consults to this one: production run 60c53424's kevrekidis assessment
    # reported 11 against 7 real consults, the difference being its 4 unplaced
    # turns exactly. The unplaced turns are still SHOWN, under their own heading
    # — they are evidence of what the hub did — they are just not counted as
    # this interview's panel.
    retro_consult_count = sum(
        1
        for placed in matched.values()
        for turn in placed
        for chip in turn["chips"]
        if chip["is_consult"]
    )

    return {
        "assessment": assessment,
        "dimensions": dimensions,
        "scale_max": rubric.scale_max,
        "rubric_weights": RUBRIC_WEIGHTS,
        "banding": BANDING,
        "rubric_version": RUBRIC_VERSION,
        "panel_state": _panel_state(assessment),
        "panel_summary": [
            {"domain": c["domain"], "verdict_signal": c["verdict_signal"]}
            for c in consults
        ],
        "consult_count": len(consults),
        "retro_consult_count": retro_consult_count,
        "thread_id": thread_id,
        "messages_available": bool(message_views),
        "timeline": timeline,
        "unplaced_turns": unplaced,
        "logs_scanned": logs_scanned,
        "log_scan_limit": LOG_SCAN_LIMIT,
        "admin_view": admin_view,
    }


async def _load_thread_messages(
    db: AsyncSession, assessment: OpportunityAssessment
) -> tuple[str | None, list[AgentMessage]]:
    """The interview thread this verdict came out of.

    Anchored on the message the verdict was POSTED as: ``slack_ts`` is written
    from the Slack post (or the locally minted ts when Slack is off), and it
    can land in either ``AgentMessage.slack_ts`` or ``AgentMessage.message_ts``
    depending on which side minted it — so both are tried.

    Returns ``(None, [])`` whenever the thread cannot be reconstructed, which
    is a NORMAL outcome, not an error: ``--fresh`` wipes ``agent_messages`` and
    never wipes ``opportunity_assessments``, so an older verdict legitimately
    outlives its own transcript. The caller renders the verdict either way.
    """
    if not assessment.slack_ts:
        return None, []
    anchor = (
        await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == assessment.simulation_run_id,
                AgentMessage.channel_name == assessment.channel_name,
                or_(
                    AgentMessage.slack_ts == assessment.slack_ts,
                    AgentMessage.message_ts == assessment.slack_ts,
                ),
            )
            .order_by(AgentMessage.posted_at, AgentMessage.created_at)
            .limit(1)
        )
    ).scalars().first()
    if anchor is None:
        return None, []
    # The thread's id is the ROOT's ts: a reply carries it in thread_ts, and the
    # root itself carries None there and is its own thread.
    thread_id = anchor.thread_ts or anchor.message_ts
    if thread_id is None:
        return None, [anchor]
    rows = (
        await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == assessment.simulation_run_id,
                AgentMessage.channel_name == anchor.channel_name,
                or_(
                    AgentMessage.thread_ts == thread_id,
                    AgentMessage.message_ts == thread_id,
                ),
            )
            .order_by(AgentMessage.posted_at, AgentMessage.created_at)
            .limit(MESSAGE_SCAN_LIMIT)
        )
    ).scalars().all()
    return thread_id, list(rows)


async def _load_consults(
    db: AsyncSession,
    assessment: OpportunityAssessment,
    thread_id: str | None,
    *,
    admin_view: bool,
) -> list[dict[str, Any]]:
    """Recorded panel consults for this interview.

    Keyed on (run, thread) — never on the channel alone, which in production
    holds several interviews. Empty for every assessment that predates the
    table, which is what the read-time tool-log parse exists to cover.
    """
    if thread_id is None:
        return []
    rows = (
        await db.execute(
            select(SpecialistConsult)
            .where(
                SpecialistConsult.simulation_run_id == assessment.simulation_run_id,
                SpecialistConsult.thread_id == thread_id,
            )
            .order_by(SpecialistConsult.created_at)
        )
    ).scalars().all()
    return [
        {
            "domain": row.domain,
            "verdict_signal": row.verdict_signal,
            "confidence": row.confidence,
            "question": row.question,
            "concerns": list(row.concerns or []),
            "questions_to_ask": list(row.questions_to_ask or []),
            # ADMIN ONLY (plan decision 2). Managers get the signal and the
            # structured lists; the specialist's verbatim text is drill-down,
            # so it is dropped HERE rather than merely left unrendered — a
            # value that reaches the context reaches anyone who can read the
            # page source the moment a template edit prints it.
            "raw_opinion": row.raw_opinion if admin_view else None,
            "created_at": row.created_at,
        }
        for row in rows
    ]


async def _load_tool_turns(
    db: AsyncSession,
    assessment: OpportunityAssessment,
    message_views: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """The hub's logged tool conversations for this thread's time span.

    ``system_prompt`` is deliberately NOT selected: it is the largest column in
    the table, it is already readable on the LLM-calls page, and nothing here
    renders it — loading it would put tens of kilobytes per row into the
    template context for nothing.
    """
    channel = message_views[0].get("channel_name") or assessment.channel_name
    first = min(view["at"] for view in message_views)
    last = max(view["at"] for view in message_views)
    query = (
        select(
            LlmCallLog.id,
            LlmCallLog.created_at,
            LlmCallLog.model,
            LlmCallLog.messages_json,
            LlmCallLog.response_text,
        )
        .where(
            LlmCallLog.simulation_run_id == assessment.simulation_run_id,
            LlmCallLog.phase == "thread_reply",
            LlmCallLog.agent_id == assessment.agent_id,
            LlmCallLog.channel == channel,
            LlmCallLog.created_at
            >= datetime.fromtimestamp(first - LOG_WINDOW_PAD_SECONDS, UTC),
            LlmCallLog.created_at
            <= datetime.fromtimestamp(last + LOG_WINDOW_PAD_SECONDS, UTC),
        )
        .order_by(LlmCallLog.created_at.desc())
        .limit(LOG_SCAN_LIMIT)
    )
    # Newest LOG_SCAN_LIMIT rows, not earliest: dropping the newest turns would
    # lose the concluding turn's consults, the most load-bearing ones, while the
    # banner tells the admin these are the "most recent" scanned turns. Fetch
    # descending so the LIMIT keeps the newest, then reverse in Python so
    # display stays chronological.
    rows = list(reversed((await db.execute(query)).all()))
    turns = []
    for row in rows:
        chips = tool_chips_from_conversation(row.messages_json)
        if not chips:
            # A turn that called no tool adds nothing the message itself does
            # not already say.
            continue
        turns.append({
            "log_id": str(row.id),
            "at": _epoch(row.created_at),
            "created_at": row.created_at,
            "model": row.model,
            "body": visible_body(row.response_text),
            "chips": chips,
        })
    return turns, len(rows)
