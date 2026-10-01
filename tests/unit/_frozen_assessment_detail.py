# ruff: noqa
"""Task 32: today's derive_strengths_and_risks and build_assessment_detail, verbatim from the phase base.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.services import assessment_detail as _live

# Every module-level name the frozen bodies use resolves to the live module's.
globals().update({k: v for k, v in vars(_live).items() if not k.startswith("__")})


def derive_strengths_and_risks(
    assessment: OpportunityAssessment,
    *,
    dimensions: list[dict[str, Any]] | None,
    consults: list[dict[str, Any]] | None,
    revision: Any,
    revision_provenance: str | None = None,
) -> dict[str, Any]:
    """Four buckets, from STORED values only — never a new judgement.

    Classification, exactly:

    ==========================  ==========================================
    input                       bucket
    ==========================  ==========================================
    dimension score             strength at `>= 0.8 * scale_max` (4 on a
                                1-5 scale); risk at `<= 0.4 * scale_max`
                                (2 on a 1-5 scale)
    dimension score between     `mid_scale`: a real, neutral answer (a 3
                                of 5), listed but bucketed as neither a
                                strength nor a risk — and not an unknown
    dimension score is None     not established: "not scored — counted as
                                zero in the weighted score"
    `gating` value "met"        strength
    `gating` value "not_met"    risk
    `gating` "unconfirmed"      not established: "never asked"
    any other gating value      not established: "unrecognised gating value"
    `red_flags` entry           risk, carrying the flag's own full text
    consult signal `adequate`   strength (and the historical `clear`)
    or `blocking`/`gap`         risk (and the historical `caution`)
    consult `reply_truncated`   not established, REGARDLESS of signal
    consult signal NULL or
    unrecognised                not established: "signal not recognised —
                                nothing can be said about this consult"
    `revision is None`          dimensions contribute NOTHING to any
                                bucket, and `scale_known` is False
    ==========================  ==========================================

    Four things this function is built around, each a defect this repo has
    already paid for once:

    1. **The not-established bucket is not decoration.** `unconfirmed` means
       "never asked", an unscored dimension is not a scored zero, and a truncated
       consult's `verdict_signal` is `src/agent/specialists.py`'s PARSE
       DEFAULT (`gap`) rather than anything a specialist said. Filing any of
       the three as a strength or a risk manufactures a claim nobody made —
       the same error `panel_state`'s five states and
       `OpportunityAssessment.missing_domains`' three states exist to prevent.
    2. **Thresholds come from the ROW's own revision**, via the `revision`
       argument, never from a literal 4 and 2. A hardcoded threshold silently
       relabels every row scored on another scale, which is precisely the
       render-time re-derivation `panel_owed` was added to end.
    3. **Nothing here is stored.** This is presentation of stored values; it
       writes no column and must never be mistaken for a write-time finding.
    4. **It cannot raise.** A malformed `gating` value, a non-string red flag,
       a `scores` dict with a bool in it, a NULL `gating`, a consult dict
       missing a key, a malformed `dimension_rationales` — each degrades into
       the not-established bucket, loses its rationale, or is skipped. A brief
       card must never 500 a page.

    Returns `{"strengths": [...], "risks": [...], "unestablished": [...],
    "mid_scale": [...], "scale_known": bool, "thresholds": {...},
    "mid_scale_count": int, "scored_dimension_count": int}`, each entry being
    `{"source": str, "label": str, "detail": str, "body": list[str],
    "preview": str|None, "note": str|None, "rationale": str|None}` with
    `source` one of `dimension` / `gating` / `red_flag` / `consult`.
    `rationale` is ALWAYS present: the hub's stored one-sentence reason for a
    `dimension` entry (strength, risk, not scored or mid-scale), from
    `dimension_rationales` (migration 0052) matched on the dimension key, and
    None for every non-dimension source and for a dimension with no stored
    reason — a NULL column, or a stored key that matches no dimension, attaches
    to nothing. `body` is ALWAYS present — an empty list when
    there is nothing stored to quote, so the template can test truthiness
    without `.get`. The template renders an entry with a non-empty `body` as
    a COLLAPSED `<details>` whose summary is `label — detail`, the `note`
    badge and the one-line `preview`; an entry with an empty body is a plain
    bullet. Consults are ONE ENTRY PER DOMAIN, from the domain's latest
    consult (`_latest_consult_per_domain`), with `note` = "latest of N
    consults" when N > 1:

    ==========================  ==========================================
    entry                       `body`
    ==========================  ==========================================
    dimension strength/risk/    `["weight: " + weight_note]`, verbatim
    mid-scale                   (dual-scale notes are not parsed), only when
                                the dimension's `weight` is not None; else `[]`
    dimension not scored        `[]`, unchanged
    gating met / not_met        `[gating[key]["description"]]` and `label`
                                becomes `gating[key]["title"]`, but ONLY when
                                `revision_provenance == PROVENANCE_LIVE` AND
                                `key` is a live gating key; otherwise `[]` and
                                the label stays `key.replace("_", " ")`
    gating unconfirmed/other    `[]`, unchanged
    red flag                    `detail` is the flag's first sentence
                                (`_preview`, 160 chars); `body` = `[full
                                text]` ONLY when that clip shortened it
    consult adequate/clear      the consult's `established` items (non-empty
                                list of strings only), capped at 3 with an
                                "and N more" tail; else `[]`
    consult blocking/gap/       the consult's `concerns` items, same cap
    caution                     rule; else `[]`
    consult not-established     `[]`, unchanged
    ==========================  ==========================================

    `thresholds` is `{"strength": float|None, "risk": float|None,
    "scale_max": float|None}` — all None whenever `scale_known` is False or
    the revision's `scale_max` is not a usable number. `mid_scale_count` is
    the number of dimensions with a usable score strictly between the risk
    and strength thresholds (0 when the scale is unknown), and
    `scored_dimension_count` the number of dimensions with a usable score at
    all — the template words the mid-scale line as "N of M" and says "All"
    only when the two are equal. `mid_scale_count == len(mid_scale)` always.
    """
    strengths: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    unestablished: list[dict[str, Any]] = []
    mid_scale: list[dict[str, Any]] = []
    rationales = _dimension_rationale_map(assessment)
    scale_known = revision is not None
    thresholds: dict[str, float | None] = {
        "strength": None, "risk": None, "scale_max": None,
    }
    mid_scale_count = 0
    scored_dimension_count = 0

    def _add(
        bucket: list[dict[str, Any]],
        source: str,
        label: object,
        detail: object,
        body: list[str] | None = None,
        note: str | None = None,
        preview: str | None = None,
        rationale: str | None = None,
    ) -> None:
        bucket.append({
            "source": source,
            "label": str(label) if label else source.replace("_", " "),
            "detail": str(detail),
            "body": list(body) if body else [],
            # One collapsed line of the quoted text (consults only); None
            # when the entry has nothing to preview or the body is static
            # rubric metadata that belongs behind the click.
            "preview": preview,
            # A non-quote annotation rendered as a badge, never as a bullet:
            # "latest of N consults" — so it is not mistaken for text a
            # specialist wrote.
            "note": note,
            # The hub's own per-dimension reason (0052); None off dimensions.
            "rationale": rationale,
        })

    # Dimensions. Skipped wholesale when the row's revision is unknown: with no
    # scale there is no threshold, and guessing one is the re-derivation point 2
    # rules out.
    # "not scored, counted as zero in the weighted score" is only true when a
    # weighted score EXISTS. `_persist_assessment` writes `scores or None`
    # alongside a NULL `weighted_score`/`band` for a verdict that carried no
    # dimension scores at all, so for that row the claim would be made six
    # times about a number that was never computed. The detail page still
    # renders all six of the revision's dimensions for such a row, which is
    # why this has to be decided here rather than by the absence of rows.
    any_scored = any(
        isinstance(dim, dict) and _usable_score(dim.get("score")) is not None
        for dim in dimensions or ()
    )
    not_scored_detail = (
        _NOT_SCORED_DETAIL if any_scored else _NO_SCORES_AT_ALL_DETAIL
    )
    if scale_known:
        scale_max = _usable_score(getattr(revision, "scale_max", None))
        strength_threshold = risk_threshold = None
        if scale_max is not None:
            strength_threshold = STRENGTH_THRESHOLD_FRACTION * scale_max
            risk_threshold = RISK_THRESHOLD_FRACTION * scale_max
            thresholds = {
                "strength": strength_threshold,
                "risk": risk_threshold,
                "scale_max": scale_max,
            }
        for dim in dimensions or ():
            if not isinstance(dim, dict):
                continue
            label = dim.get("title") or dim.get("key")
            rationale = _rationale_for(rationales, dim.get("key"))
            weight_body = (
                [f"weight: {dim.get('weight_note')}"] if dim.get("weight") is not None else []
            )
            score = _usable_score(dim.get("score"))
            if score is None:
                _add(unestablished, "dimension", label, not_scored_detail,
                     rationale=rationale)
                continue
            scored_dimension_count += 1
            if scale_max is None:
                continue
            detail = _format_score(score, scale_max)
            if score >= strength_threshold:
                _add(strengths, "dimension", label, detail, body=weight_body,
                     rationale=rationale)
            elif score <= risk_threshold:
                _add(risks, "dimension", label, detail, body=weight_body,
                     rationale=rationale)
            else:
                # A mid-scale score is a real, neutral answer: listed in its
                # own bucket so every scored dimension appears on the page,
                # but never as a strength or a risk.
                mid_scale_count += 1
                _add(mid_scale, "dimension", label, detail, body=weight_body,
                     rationale=rationale)

    # Gating. The tri-state strings, plus a fourth branch for anything else —
    # `gating` is JSONB with no CHECK constraint behind it.
    gating = getattr(assessment, "gating", None)
    # Gate title/description come from the LIVE rubric document and are shown
    # only when this row was scored against it (`PROVENANCE_LIVE`) — an older
    # row's gating key may not even exist in the live document, and rendering
    # today's title/description against yesterday's decision would mislabel
    # it the same way a hardcoded score threshold would.
    live_gating = load_rubric().gating if revision_provenance == PROVENANCE_LIVE else {}
    if isinstance(gating, dict):
        for key, value in gating.items():
            label = str(key).replace("_", " ")
            gate_meta = live_gating.get(key) if isinstance(key, str) else None
            if value == "met":
                if gate_meta is not None:
                    _add(strengths, "gating", gate_meta["title"], "met", body=[gate_meta["description"]])
                else:
                    _add(strengths, "gating", label, "met")
            elif value == "not_met":
                if gate_meta is not None:
                    _add(risks, "gating", gate_meta["title"], "not met", body=[gate_meta["description"]])
                else:
                    _add(risks, "gating", label, "not met")
            elif value == "unconfirmed":
                _add(unestablished, "gating", label, "never asked")
            else:
                _add(unestablished, "gating", label, _UNRECOGNISED_GATING_DETAIL)

    # Red flags, full text. A non-string entry is skipped rather than coerced:
    # a rendered `None` or `{}` would read as a flag the hub never wrote.
    # Stored flags average ~535 characters (2026-09-14), so the summary line is
    # the first sentence and the full text sits behind the click — but ONLY
    # when the clip actually shortened it: a short flag renders as a plain,
    # uncollapsed bullet, which is what the red-flag card's own
    # never-collapsed rule expects of a disqualifier a reviewer must see.
    red_flags = getattr(assessment, "red_flags", None)
    if isinstance(red_flags, list):
        for flag in red_flags:
            if isinstance(flag, str) and flag.strip():
                full = flag.strip()
                short = _preview(full) or full
                _add(risks, "red_flag", "Red flag", short, body=[full] if short != full else [])

    for consult, n_in_domain in _latest_consult_per_domain(list(consults or ())):
        label = consult.get("domain") or "consult"
        note = f"latest of {n_in_domain} consults" if n_in_domain > 1 else None
        if consult.get("reply_truncated"):
            _add(unestablished, "consult", label, _TRUNCATED_CONSULT_DETAIL, note=note)
            continue
        # `read_state` (migration 0038) has THREE values, and two of them mean
        # the stored `verdict_signal` is not something a specialist said:
        # `truncated` (the reply was cut off) and `defaulted` (the reply
        # arrived complete but `parse_opinion` could not read a signal out of
        # it, so `_DEFAULT_SIGNAL` — `gap` — was substituted). The truncated
        # case is caught above by `reply_truncated`; the DEFAULTED case has
        # `truncated=False` and would otherwise land in `_RISK_SIGNALS` and
        # render under a red glyph as a specialist finding nobody made, which
        # is exactly what point 1 of this function's contract forbids and what
        # `parse_opinion`'s own docstring calls "the laundering that branch
        # exists to prevent". `read_state is None` is a pre-0038 row: the
        # question was never recorded, so it is NOT treated as defaulted and
        # stays on the signal path below, which is the only answer available
        # for it.
        if consult.get("read_state") == "defaulted":
            _add(unestablished, "consult", label, _DEFAULTED_CONSULT_DETAIL, note=note)
            continue
        signal = consult.get("verdict_signal")
        if isinstance(signal, str) and signal in _STRENGTH_SIGNALS:
            body = _capped_body(consult.get("established"))
            _add(strengths, "consult", label, signal, body=body, note=note,
                 preview=_preview(body[0]) if body else None)
        elif isinstance(signal, str) and signal in _RISK_SIGNALS:
            body = _capped_body(consult.get("concerns"))
            _add(risks, "consult", label, signal, body=body, note=note,
                 preview=_preview(body[0]) if body else None)
        else:
            _add(unestablished, "consult", label, _UNRECOGNISED_SIGNAL_DETAIL, note=note)

    return {
        "strengths": strengths,
        "risks": risks,
        "unestablished": unestablished,
        "mid_scale": mid_scale,
        "scale_known": scale_known,
        "thresholds": thresholds,
        "mid_scale_count": mid_scale_count,
        "scored_dimension_count": scored_dimension_count,
    }


async def build_assessment_detail(
    db: AsyncSession,
    assessment_id: uuid.UUID,
    *,
    admin_view: bool,
    viewer_is_staff: bool = False,
) -> dict[str, Any] | None:
    """One assessment, its dimension breakdown, and its interview timeline.

    Returns None when there is no such assessment (the router owns the 404 —
    this module stays HTTP-free, like src/services/directory.py).

    ``admin_view=False`` omits every admin-only value from the returned
    context: no ``raw_opinion``, no tool activity. See the module docstring.

    ``viewer_is_staff`` gates ``review_capable_users`` (the assignee roster
    for the Human-review card's assign form): it is queried ONLY when True,
    so a reviewer's render never enumerates the staff/reviewer roster — the
    same rule ``src/routers/manager.py``'s own PI-add form applies, and it
    saves a query on every non-staff render.
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

    # The hub's per-dimension reasons (0052), keyed the SAME way the scores map
    # is normalized above — `normalize_dimension_rationales` lower-cases on
    # write, and this lookup must match it or a stored reason renders nowhere.
    rationales = _dimension_rationale_map(assessment)

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
                "rationale": _rationale_for(rationales, dim.key),
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
            "rationale": _rationale_for(rationales, key),
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
        turns, logs_scanned = await _load_tool_turns(
            db, assessment, message_views, thread_id=thread_id,
        )
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
    # `_load_tool_turns` excludes rows stamped with another thread's
    # `llm_call_logs.thread_ts`, but rows logged before 0042 carry a NULL
    # `thread_ts` and are admitted on the (run, phase, agent, channel, time
    # window) heuristic alone. Several interviews share a channel, so those
    # rows can still be other threads' turns, which `correlate_turns_to_messages`
    # hands back as `unplaced`. Summing over `turns` would attribute other
    # interviews' consults to this one: production run 60c53424's kevrekidis
    # assessment reported 11 against 7 real consults under the heuristic alone,
    # the difference being its 4 unplaced turns exactly. Unplaced turns are
    # still SHOWN, under their own heading — they are evidence of what the hub
    # did — they are just not counted as this interview's panel.
    retro_consult_count = sum(
        1
        for placed in matched.values()
        for turn in placed
        for chip in turn["chips"]
        if chip["is_consult"]
    )

    review_feedback = await _load_review_feedback(db, assessment.id)
    review_status_history = await _load_review_status_history(db, assessment.id)
    review_assignments = await _load_review_assignments(db, assessment.id)
    review_capable_users = (
        await _load_review_capable_users(db) if viewer_is_staff else []
    )

    # The scoring form's own source of truth: the LIVE document, because that
    # is what the reviewer is about to score against and what
    # `submit_feedback` will stamp on the row. Deliberately NOT the revision
    # that scored the assessment — a human reviewing a v3.2.0 verdict today is
    # giving a v3.4.0 opinion, and the stamp on their row must say so.
    # `bot_score` rides along per dimension (A9): disagreement has to be
    # visible where the human is choosing, and it is labelled as the bot's.
    live_rubric = load_rubric()
    review_rubric = {
        "version": live_rubric.version,
        "scale_min": live_rubric.scale_min,
        "scale_max": live_rubric.scale_max,
        "dimensions": [
            {
                "key": d.key,
                "title": d.title,
                "weight": d.weight,
                "anchors": d.anchors,
                "bot_score": _score_value(normalized_scores.get(d.key)),
            }
            for d in live_rubric.dimensions
        ],
    }

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
        # The same chips GROUPED per domain (2026-09-15 visual audit M6): a
        # real interview has 25-40 consults, and 37 identical 12px chips in
        # four rows were unreadable as a set. One chip per domain carries the
        # signal tally in chronological order; a domain whose replies were
        # cut off gets its own neutral chip, so the "reply cut off" marker is
        # never merged into an opinion tally.
        "panel_domains": summarize_panel_domains(consults),
        # Live gate descriptions for the gating card (audit L1): the text used
        # to live only in a `title` tooltip on a non-focusable row. Same
        # provenance guard as `derive_strengths_and_risks` — an older row
        # keeps bare labels rather than today's definitions.
        "gating_descriptions": (
            {k: v for k, v in load_rubric().gating.items()}
            if revision_provenance == PROVENANCE_LIVE else {}
        ),
        # Request 3 / D2: the strengths-risks-not-established brief, DERIVED
        # from the three things already resolved above and stored nowhere.
        "verdict_signals": derive_strengths_and_risks(
            assessment,
            dimensions=dimensions,
            consults=consults,
            revision=revision,
            revision_provenance=revision_provenance,
        ),
        "consult_count": len(consults),
        "retro_consult_count": retro_consult_count,
        "thread_id": thread_id,
        "messages_available": bool(message_views),
        "timeline": timeline,
        "unplaced_turns": unplaced,
        "logs_scanned": logs_scanned,
        "log_scan_limit": LOG_SCAN_LIMIT,
        "admin_view": admin_view,
        # The hub's own `strengths`/`risks` bullets (0049) — and, since
        # scout_hub 1.7.0, `competitive_landscape`/`evidence_maturity` (0050)
        # — are STAFF-only on the page, matching what the prompt promises the
        # model: a reviewer account reaches the manager detail route but must
        # not see model text the hub was told may cite unpublished results or
        # Blackbird's own commercial diligence. All four are gated on this one
        # key in `templates/admin/_assessment_detail_body.html`; a fifth such
        # field belongs in the same place.
        "viewer_is_staff": viewer_is_staff,
        # Human-review card. All three review tables are ordered
        # (created_at, id) — Postgres `now()` is transaction-start, so ties
        # inside one write burst are real and `id` is the tiebreak.
        # ``review_status`` is the LATEST event (last of the ordered history),
        # or None when the assessment has never had one recorded.
        "review_feedback": review_feedback,
        "review_status": review_status_history[-1] if review_status_history else None,
        "review_status_history": review_status_history,
        "review_assignments": review_assignments,
        "review_capable_users": review_capable_users,
        "review_rubric": review_rubric,
        "revision_provenance_unknown": PROVENANCE_UNKNOWN,
    }
