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

# `panel_is_owed`/`PANEL_REQUIRED_FOR` are deliberately NOT imported here. This
# module reads what the floor RECORDED (`OpportunityAssessment.panel_owed`); it
# must never re-derive the floor's decision from today's predicate. See
# `panel_state`.
from src.agent.specialists import parse_opinion
from src.models import AgentMessage, LlmCallLog, OpportunityAssessment, SpecialistConsult
from src.services.blackbird_rubric import BANDING, RUBRIC_VERSION
from src.services.interview_transcript import load_interview_thread
from src.services.rubric_revisions import resolve_revision

# Hard bounds. This page is a read of unbounded production data: a channel can
# hold hundreds of hub turns and a retrieve_full_text result can be an entire
# paper, so every list and every blob here is capped rather than trusted.
LOG_SCAN_LIMIT = 200
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
# Five values, not three: `gap`/`adequate` are the live vocabulary (2026-08-28)
# and `caution`/`clear` are what the pre-rename corpus this regex exists to read
# actually says. Dropping the historical pair would make every interview that
# predates `specialist_consults` — the only ones this parse is used for — report
# its consults as FAILED, which is precisely the "shown as if a specialist had
# cleared anything" error inverted.
_CONSULT_SIGNAL_RE = re.compile(
    r"signal:\s*(blocking|gap|adequate|caution|clear)\b", re.IGNORECASE
)

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

    ``concern_count`` rides alongside ``concerns`` because the chip renders the
    signal and not the list: after the 2026-08-28 rename ``adequate`` means
    "meets the bar for THIS STAGE", not "no concerns", and a bare label is
    exactly how its predecessor came to read as a clean bill of health.

    A RETRO reader, and one of only two that pass ``allow_historical=True``:
    ``result`` is a stored tool log, so a consult logged before the rename says
    ``caution``/``clear`` and must render as what it said. The live consult path
    shares ``parse_opinion`` and must NOT opt in — see ``_READABLE_SIGNALS`` in
    src/agent/specialists.py.
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
            "concern_count": 0,
            "questions_to_ask": [],
        }
    opinion = parse_opinion(text[brace:], domain=domain, allow_historical=True)
    return {
        "verdict_signal": opinion.verdict_signal,
        "confidence": opinion.confidence,
        "concerns": list(opinion.concerns),
        "concern_count": len(opinion.concerns),
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


#: Every value ``panel_state`` can return, most alarming first. Public because
#: two templates and one aggregate query now branch on these strings, and a
#: typo in any of them would land silently in the terminal ``{% else %}``.
PANEL_STATES: tuple[str, ...] = (
    "gap", "unverified", "unrecorded", "not_owed", "verified",
)

#: The states that are NOT a verified panel and NOT an exemption the floor
#: itself recorded — i.e. every row a reader should not treat as vetted.
#:
#: The Python half of a rule that unavoidably exists twice: a run-level COUNT
#: cannot join to a Python function, so ``unvetted_panel_filter`` below is its
#: SQL twin. The two are bound by
#: ``tests/unit/test_directory_assessments.py::
#: test_the_sql_unvetted_filter_matches_panel_state_row_for_row``, which walks
#: every combination of the three columns ``panel_state`` reads and asserts they
#: agree ROW FOR ROW. Without that alarm the coupling is only a comment: the
#: first version of this constant claimed ``src/services/directory.py`` used it
#: while ``directory.py`` carried its own hand-written predicate and never
#: referenced it, so adding a sixth state here would have looked like it updated
#: the banner and changed nothing.
PANEL_STATES_UNVETTED: frozenset[str] = frozenset({"gap", "unverified", "unrecorded"})


def unvetted_panel_filter():
    """The SQL twin of ``panel_state(row) in PANEL_STATES_UNVETTED``.

    Lives HERE, beside the state machine and the constant it mirrors, rather
    than inline in ``src/services/directory.py`` where the banner is built: a
    second copy of this rule in another module is exactly the drift this whole
    change exists to end, and the three pieces have to be reviewable together.

    Reads as the negation of the two states that HAVE an answer — ``verified``
    (``panel_owed`` recorded True, no gap, checkable) and ``not_owed``
    (``panel_owed`` recorded False, no gap) — so a row is unvetted when it
    carries a demonstrated gap, or the ``[]`` sentinel meaning the floor could
    not be checked, or no record of whether a panel was owed at all.

    Returns a bare column expression with NO run scoping: the caller adds that,
    because the warning deliberately follows the run and not the lab filter.
    """
    return or_(
        OpportunityAssessment.panel_incomplete.is_(True),
        OpportunityAssessment.missing_domains.is_not(None),
        OpportunityAssessment.panel_owed.is_(None),
    )


def panel_state(assessment: OpportunityAssessment) -> str:
    """The FIVE findings the specialist floor can leave behind.

    Three come straight from ``OpportunityAssessment.missing_domains``: names = a
    demonstrated gap, ``[]`` = the floor could not be checked at all, NULL = no
    gap recorded.

    The other two split that NULL, because it was answering two questions with
    one word: "the floor evaluated this verdict and found nothing owed and
    unconsulted" and "this verdict never faced a floor at all". Reporting the
    second as "verified" claimed an audit that never ran — production run
    60c53424's pearce ``route-to-incubation`` row rendered the green box while
    ``required_domains_for`` named ``clinical`` and no clinical consult existed
    on that thread.

    **The split is READ FROM THE ROW, never re-derived here.** An earlier fix
    asked ``panel_is_owed(recommendation, band)`` at render time, and that is a
    different question — "would a panel be owed under TODAY's rules" — so every
    time the predicate widens, every older row is silently relabelled. It widened
    twice in 2026-08 alone, and 12 production rows written by the
    recommendation-only floor (which stored "no panel was owed" as
    ``panel_incomplete=False, missing_domains=NULL``) were re-read by the
    band-aware page as completed audits; at least five had a demonstrable gap.
    ``opportunity_assessments.panel_owed`` records what the floor decided AT
    WRITE TIME, and this replays it rather than re-deriving it from today's
    rule.

    Putting ``panel_is_owed`` back ahead of the column test re-arms that bug
    exactly; ``tests/unit/test_panel_state.py``'s
    ``test_the_read_path_never_re_derives_the_floor_s_decision`` fails if anyone
    does.

    Order of authority, strongest evidence first:

    * ``gap`` — the floor looked and found domains owed and never consulted.
    * ``unverified`` — the floor could not check at all (``[]``).
    * ``verified`` — ``panel_owed is True``: a panel WAS owed, so the floor
      evaluated this verdict, and the two states above say it found nothing.
      This is the only state that has earned the green box.
    * ``not_owed`` — ``panel_owed is False``: the floor determined no panel was
      owed and recorded that. The weakest claim of the five, which is why it
      sits below the two evidence states: a stored gap or a stored ``[]`` is
      evidence about THIS row, and evidence outranks an exemption.
    * ``unrecorded`` — ``panel_owed is None``: the row predates 0036, or was
      backfilled, or was hand-built by a test. We do not know whether any floor
      ran, so no claim is available. Never green.
    """
    if assessment.panel_incomplete:
        return "gap"
    if assessment.missing_domains is not None:
        return "unverified"
    if assessment.panel_owed is True:
        return "verified"
    if assessment.panel_owed is False:
        return "not_owed"
    return "unrecorded"


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

    # Resolves to a live AgentRegistry row's user_id, so the template can
    # link to that PI's profile. Left None for a null subject_agent_id, a
    # stale/decommissioned slug with no AgentRegistry row, or an unlinked
    # agent whose AgentRegistry.user_id is itself NULL — all three are
    # "no link", not an error.
    pi_user_id: str | None = None
    if assessment.subject_agent_id:
        from src.models import AgentRegistry
        row = (await db.execute(
            select(AgentRegistry.user_id)
            .where(AgentRegistry.agent_id == assessment.subject_agent_id)
        )).scalar_one_or_none()
        if row is not None:
            pi_user_id = str(row)

    revision, revision_provenance = resolve_revision(
        assessment.rubric_version, assessment.rubric_content_hash
    )
    scores = assessment.scores if isinstance(assessment.scores, dict) else {}
    normalized_scores = {
        key.strip().lower(): value
        for key, value in scores.items()
        if isinstance(key, str)
    }

    def _score_value(raw: object) -> float | None:
        return (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )

    def _pct(value: float | None) -> float | None:
        # Bar width as a percentage of the revision's scale, clamped — a
        # verdict can carry an out-of-range score and a >100% width would
        # overflow the track. No revision -> no known scale -> no bar.
        if revision is None:
            return None
        if value is None:
            return 0.0
        return min(100.0, max(0.0, value / revision.scale_max * 100.0))

    dimensions = []
    named_keys: set[str] = set()
    if revision is not None:
        for dim in revision.dimensions:
            value = _score_value(normalized_scores.get(dim.key))
            named_keys.add(dim.key)
            dimensions.append({
                "key": dim.key,
                "title": dim.title,
                "weight": dim.weight,
                "weight_note": dim.weight_note,
                "score": value,
                "pct": _pct(value),
            })
    # Score keys the chosen revision does not name still render — a stored row
    # must show its data, never blanks (the pre-registry page dropped a v2
    # row's 13 scores on the floor).
    for key in sorted(normalized_scores):
        if key in named_keys:
            continue
        value = _score_value(normalized_scores[key])
        if value is None:
            continue
        dimensions.append({
            "key": key,
            "title": key.replace("_", " "),
            "weight": None,
            "weight_note": None,
            "score": value,
            "pct": _pct(value),
        })

    thread_id, messages = await load_interview_thread(db, assessment)
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
        "pi_user_id": pi_user_id,
        "dimensions": dimensions,
        "revision": revision,
        "revision_provenance": revision_provenance,
        "scale_max": revision.scale_max if revision is not None else None,
        "banding": BANDING,
        "rubric_version": RUBRIC_VERSION,
        "panel_state": panel_state(assessment),
        # The chips under the panel-state box. `reply_truncated` rides along
        # with the signal it qualifies: this row of chips is the compact answer
        # to "was this verdict's panel real", so a chip whose opinion was never
        # finished has to say so here, not only on the card further down.
        "panel_summary": [
            {
                "domain": c["domain"],
                "verdict_signal": c["verdict_signal"],
                "reply_truncated": c["reply_truncated"],
            }
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
            # Alongside the list, because the chip row shows the signal only.
            # `adequate` (2026-08-28) means "meets the bar for THIS STAGE" and
            # NOT "no concerns" — every `clear` opinion ever emitted carried 4-9
            # of them, one of which read "Succession risk is high as described"
            # under a ✅. The count is what keeps the label honest at a glance.
            "concern_count": len(list(row.concerns or [])),
            "questions_to_ask": list(row.questions_to_ask or []),
            # ADMIN ONLY (plan decision 2). Managers get the signal and the
            # structured lists; the specialist's verbatim text is drill-down,
            # so it is dropped HERE rather than merely left unrendered — a
            # value that reaches the context reaches anyone who can read the
            # page source the moment a template edit prints it.
            "raw_opinion": row.raw_opinion if admin_view else None,
            # Was this reply CUT OFF (0036's `specialist_consults.truncated`)?
            # Carried, not dropped, because `verdict_signal` above is a PARSE
            # DEFAULT on a truncated reply — `gap`, from
            # src/agent/specialists.py — and a chip that says `gap` is
            # indistinguishable from one a specialist actually gave. The
            # specialist floor, the Slack panel note and the durable row all
            # already know; this page was the last reader that did not, and it
            # is the one a human reads to decide whether a verdict's panel was
            # real.
            #
            # Named `reply_truncated`, not `truncated`: `thread_panel.py`'s
            # card dicts carry the same key, and there "truncated" already
            # means two other things (the row cap, and `raw_opinion` clipped
            # for display). One name across both card renderers, and none of
            # the three collide.
            #
            # NULL stays falsy on purpose — see the column's comment: NULL is
            # "written before 0036", read as not-truncated, because
            # retroactively invalidating history on no evidence is worse.
            "reply_truncated": bool(row.truncated),
            # 0038's `read_state`, carried verbatim INCLUDING None. None means
            # "written before 0038" — a third state, not "parsed" — so the
            # decision about what to show for it stays in the template rather
            # than being guessed at here. Same key and same rule as
            # `thread_panel.py`'s card dicts, so the two card renderers still
            # read alike.
            "read_state": row.read_state,
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
