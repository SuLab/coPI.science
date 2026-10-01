# ruff: noqa
"""Task 34: today's list_assessments, verbatim from the phase base (inline run query kept).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.services import directory as _live

# Every module-level name the frozen bodies use resolves to the live module's.
globals().update({k: v for k, v in vars(_live).items() if not k.startswith("__")})


async def list_assessments(
    db: AsyncSession,
    run_id: str | None,
    *,
    sort: str | None = None,
    lab: str | None = None,
    review: str | None = None,
) -> dict[str, Any]:
    """BlackbirdBot's screening verdicts against the Blackbird investment rubric.

    Ordered by weighted score descending (NULLs last), then most-recent-first,
    so the advance/conditional candidates are what a human sees on arrival —
    this page is a triage queue, not a log. ``sort`` picks a different order
    (see ``ASSESSMENT_SORT_OPTIONS``) and ``lab`` narrows to one
    ``subject_agent_id``.

    Both are UNVALIDATED user input off a query string, and both fall back to
    the default SILENTLY rather than raising: a stale bookmark or a hand-typed
    parameter must render the triage queue, not a 400 or an empty page. That
    goes for a ``lab`` naming a subject with no rows in this run too — it is
    dropped, so the reader gets the unfiltered queue instead of a blank table
    with no explanation.

    Defaults to the CURRENT simulation run (the most recently started
    ``SimulationRun``) — ``?run_id=all`` or picking an older run from the
    dropdown reaches everything else; nothing is ever deleted from this view,
    only filtered (one operator-run, backed-up purge on record: 2026-08-27,
    rubric v3). This is deliberate, not incidental: before the 2026-08-22 fix,
    ``--fresh`` (``src/agent/main.py``) ran three UNFILTERED deletes —
    ``agent_messages``, ``agent_channels`` and ``pi_dm_messages`` — with no
    ``simulation_run_id`` predicate, so every historical run's Slack messages
    went with it while ``opportunity_assessments`` (never touched by any
    ``--fresh``, before or after) survived pointing at threads that no longer
    existed. Today's ``--fresh`` deletes nothing at all — it only mints a new
    ``simulation_run_id``, and that new id is the isolation — but the old
    runs already orphaned that way are still on record, so after a fresh
    restart, old assessments whose Slack messages no longer exist would
    otherwise sit on this page with nothing to distinguish them from current
    ones. Scoping to the latest run excludes those by construction (their
    ``simulation_run_id`` is a run whose messages are gone), while the "All
    Runs" escape hatch and the per-run dropdown keep every row reachable.
    Mirrors the run-selector pattern already used by ``admin_discussions``.

    ``review`` splits the queue into the reviewed and unreviewed sub-tabs and
    narrows ``total_count``, the rendered rows and everything derived from them.
    It deliberately does NOT narrow ``incomplete_panel_count``, the drop counts,
    ``lab_options`` or ``assessment_counts_by_run`` — the first two are warnings
    and the failure mode of a warning is under-warning, and the last two are the
    controls' own option sets, which are computed pre-filter so a reader always
    has a way back. Like ``sort`` and ``lab`` it is unvalidated query-string
    input and falls back to ``ASSESSMENT_REVIEW_DEFAULT`` silently.
    """
    runs_result = await db.execute(
        select(SimulationRun).order_by(SimulationRun.started_at.desc())
    )
    runs = runs_result.scalars().all()

    # Stored rows per run — the dropdown's honesty device: an old run showing
    # "0 stored" is distinguishable from a populated one, and (post-purge) from
    # a run whose rows exist only in the offline backup.
    counts_result = await db.execute(
        select(OpportunityAssessment.simulation_run_id, func.count())
        .group_by(OpportunityAssessment.simulation_run_id)
    )
    assessment_counts_by_run = dict(counts_result.all())

    show_all_runs = run_id == "all"
    selected_run_id: uuid.UUID | str | None = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            selected_run_id = uuid.UUID(run_id)
        except ValueError:
            pass
    if not selected_run_id and runs:
        selected_run_id = runs[0].id

    sort_key = sort if sort in ASSESSMENT_SORTS else ASSESSMENT_SORT_DEFAULT
    review_key = (
        review if review in ASSESSMENT_REVIEW_FILTERS else ASSESSMENT_REVIEW_DEFAULT
    )

    query = select(OpportunityAssessment)
    if not show_all_runs and selected_run_id:
        query = query.where(OpportunityAssessment.simulation_run_id == selected_run_id)

    # The lab dropdown's options, from the RUN scope — before the lab filter is
    # applied and before the display LIMIT. Both matter: computing them after
    # the filter would leave the select holding only the lab already chosen (no
    # way back to any other one without editing the URL), and computing them
    # from the fetched rows would silently drop every lab whose verdicts all
    # scored below the 500-row cap.
    lab_options_query = select(OpportunityAssessment.subject_agent_id).where(
        OpportunityAssessment.subject_agent_id.is_not(None)
    )
    if not show_all_runs and selected_run_id:
        lab_options_query = lab_options_query.where(
            OpportunityAssessment.simulation_run_id == selected_run_id
        )
    lab_options = sorted(
        {row[0] for row in await db.execute(lab_options_query.distinct())}
    )
    lab_filter = lab if lab in lab_options else None
    if lab_filter:
        query = query.where(OpportunityAssessment.subject_agent_id == lab_filter)

    # The run+lab scope, kept so the tab counts can be taken from it. `query`
    # is about to be narrowed by the review filter; these counts must not be.
    scope_query = query

    # Applied BEFORE total_count and before the LIMIT, so the "top N of TOTAL"
    # note, the five recommendation cards and dimension_stats all describe the
    # tab the reader is on rather than the whole run.
    if review_key == "reviewed":
        query = query.where(has_review_filter())
    elif review_key == "unreviewed":
        query = query.where(~has_review_filter())

    # Counts the current filter — run AND lab AND the review tab — so the
    # "top N of TOTAL" note describes the table the reader is looking at.
    total_count = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0

    # Both tabs' sizes in one place, so the strip never has to derive "All" by
    # addition and cannot disagree with the rows below it. Scoped by run+lab —
    # the same scope as `total_count` — but deliberately NOT by the tab itself.
    reviewed_count = (await db.execute(
        select(func.count()).select_from(
            scope_query.where(has_review_filter()).subquery()
        )
    )).scalar() or 0
    all_count = (await db.execute(
        select(func.count()).select_from(scope_query.subquery())
    )).scalar() or 0
    review_counts = {
        "reviewed": reviewed_count,
        "unreviewed": all_count - reviewed_count,
        "all": all_count,
    }

    # Surfaced because Task 3 of docs/plans/2026-08-18-specialist-panel-remediation.md
    # stops the floor discarding a gapped verdict.
    # Storing it is only safe if the page distinguishes it from a vetted one.
    #
    # Counts every row whose panel is NOT verified complete — the same three
    # states `assessment_detail.panel_state` renders as `gap`, `unverified` and
    # `unrecorded`, and no others. It used to count `panel_incomplete IS TRUE`
    # alone, which excluded the other two BY CONSTRUCTION:
    #
    #   * `missing_domains=[]` — the floor could not be checked at all
    #     (`SimulationEngine._floor_verifiable`), the ordinary state after a
    #     restart, and production's normal exit is a SIGKILL.
    #   * `panel_owed IS NULL` — the row does not record whether a panel was
    #     owed at all (every row written before migration 0036, deliberately not
    #     backfilled).
    #
    # Neither is evidence of a gap; neither is evidence of a complete panel
    # either, and the banner exists to say "do not treat this score as vetted".
    # Leaving them out made 12 production rows look unremarkable on this page
    # while the detail page one click away called them verified. `not_owed` —
    # the floor's own RECORDED exemption — is the one non-verified state that is
    # genuinely fine, and it stays uncounted.
    #
    # The per-row `panel_state` attached below is what tells a reader WHICH of
    # the three a given row is; this number only says how many to look at.
    #
    # The predicate itself is `assessment_detail.unvetted_panel_filter()`, not a
    # hand-written `or_(...)` here: a COUNT cannot join to a Python function, so
    # the rule exists in both forms, and they are kept in one module and bound by
    # a row-for-row drift alarm rather than by a comment asking the next editor
    # to remember. See that function.
    #
    # Deliberately NOT narrowed by `lab`, unlike total_count above. This and
    # the dropped-verdict counts below are warnings, and the failure mode of a
    # warning is under-warning: a reader who filtered to one lab must still be
    # told that this run stored an unvetted verdict, because the fix is a run's
    # problem, not a lab's. Over-reporting is visible and checkable; silently
    # narrowing a warning to the current filter is neither.
    incomplete_query = select(func.count()).select_from(OpportunityAssessment).where(
        unvetted_panel_filter()
    )
    if not show_all_runs and selected_run_id:
        incomplete_query = incomplete_query.where(
            OpportunityAssessment.simulation_run_id == selected_run_id
        )
    incomplete_panel_count = (await db.execute(incomplete_query)).scalar_one()

    # Ordering (see _assessment_order_by, which documents the NULLS LAST
    # discipline) is applied AFTER total_count: a count over an ordered
    # subquery is the same number and more work.
    query = query.order_by(*_assessment_order_by(sort_key)).limit(ASSESSMENTS_LIMIT)

    result = await db.execute(query)
    assessments = result.scalars().all()

    # The five-state panel finding, per row, computed by the ONE definition the
    # detail page uses. Attached to each row rather than returned as a separate
    # context key on purpose: `_assessments_body.html` is included by an admin
    # template whose router allowlists every context key it forwards
    # (src/routers/admin/assessments.py) and by a manager template whose router splats the
    # whole view — so a new key would reach one surface and be Jinja `Undefined`
    # (silently falsy, never an error) on the other. Riding on `assessments`,
    # which both already forward, is the only shape that cannot half-arrive.
    #
    # `panel_state` is not a mapped column, so this writes an ordinary instance
    # attribute and persists nothing; these rows are read-only and are handed
    # straight to a template.
    #
    # Re-deriving the state in Jinja from the three columns was the alternative
    # and is exactly the drift this whole change exists to end: two copies of
    # the rule, one of which nobody updates.
    for _row in assessments:
        _row.panel_state = panel_state(_row)

    # Batched Assigned/Reviewed-by/status-chip columns (Task 7 of
    # docs/plans/2026-08-28-human-review-feedback-implementation-plan.md) — the exact
    # same attach-to-row pattern as `panel_state` above, and for the exact
    # same reason: `_assessments_body.html` is included by an admin template
    # that allowlists every context key it forwards (`src/routers/admin/assessments.py`)
    # and by a manager template that splats the whole view, so a new
    # top-level key would reach one surface and render as silently-falsy
    # Jinja `Undefined` on the other. Riding on `assessments` is the only
    # shape that reaches both.
    review_cols = await review_columns_for(db, [a.id for a in assessments])
    for _row in assessments:
        _row.review_cols = review_cols.get(_row.id, EMPTY_REVIEW_COLUMNS)

    # Batched agent_id -> user_id lookup, so the template can link a row's
    # lab to that PI's profile. Only ever resolvable for a LIVE roster entry
    # with a linked user: a stale/decommissioned subject_agent_id (no
    # AgentRegistry row) or an unlinked agent (AgentRegistry.user_id IS NULL)
    # is silently omitted here rather than raising, and the template must
    # treat a missing key the same as "no link".
    subject_ids = {a.subject_agent_id for a in assessments if a.subject_agent_id}
    pi_user_ids: dict[str, str] = {}
    if subject_ids:
        from src.models import AgentRegistry
        rows = (await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.user_id)
            .where(AgentRegistry.agent_id.in_(subject_ids))
        )).all()
        pi_user_ids = {
            r.agent_id: str(r.user_id) for r in rows if r.user_id is not None
        }

    # Per-dimension score rows for the card's collapsed disclosure (2026-09-14)
    # — the exact same attach-to-row pattern as `panel_state` above, and for
    # the exact same reason: a new top-level key would reach the admin
    # template (which allowlists every key it forwards,
    # `src/routers/admin/assessments.py`) or the manager template (which splats the whole
    # view) but not both, and would render as silently-falsy Jinja `Undefined`
    # on whichever it missed. Riding on `assessments` is the only shape that
    # reaches both. Each row resolves its OWN rubric revision (an archived one
    # for a stamped row, the live one for an unstamped row), so the titles and
    # weights shown here always match the revision that actually scored it.
    for _row in assessments:
        _row.dimension_rows, _row.revision_view = _assessment_dimension_rows(_row)

    # Per-dimension distribution. Four dimensions (external_signals, ip_fto,
    # exit_thesis, chemistry_dc_path) never exceeded 2 across the 18
    # assessments of run 1787010946 — 23 of 100 weight points pinned near
    # minimum, invisible on a page that shows only totals.
    #
    # `specialist` is the first runtime read maps_to_dimensions has ever had:
    # it names who to ask when a dimension is scoring badly.
    #
    # Keyed by DIMENSION, which is why the flattening below is not cosmetic: the
    # field became a tuple when `scientific` and `chemistry` each took ownership
    # of a second dimension (2026-08-22), and reading only the first entry would
    # have silently orphaned `mechanism_validation` and `toxicity_selectivity`
    # here while the floor could require their specialists. A dimension with two
    # owners would collapse to whichever came last in the table — forbidden by
    # `test_no_dimension_has_two_owning_specialists`.
    specialist_for = {
        dimension: domain
        for domain, spec in SPECIALIST_DOMAINS.items()
        for dimension in spec.maps_to_dimensions
    }
    # Fallback owners from the stage bars: `maps_to_dimensions` is empty for
    # clinical, legal and technologic since v3 folded market_unmet_need, ip_fto
    # and platform into the consolidated dimensions, and a dimension with no
    # owner at all renders no "who to ask" hint. setdefault, so an OWNING
    # specialist above always wins — a bar may quote a dimension it does not own
    # (legal quotes venture_potential, which commercial owns).
    #
    # Under the CURRENT table this loop changes nothing, deliberately and
    # measurably: all six dimensions already have an owner, and the extra names a
    # bar's `source` can contribute are gating keys plus `red_flags` /
    # `scoring_preamble`, none of which is ever a key of RUBRIC_WEIGHTS below. It
    # is here so that folding another dimension away cannot silently blank the
    # hint for it — the shape that produced the empty tuples above.
    for domain, bar in load_rubric().stage_bars.items():
        for named in (s.strip() for s in bar.source.split(",")):
            specialist_for.setdefault(named, domain)
    dimension_stats = []
    for dimension, weight in RUBRIC_WEIGHTS.items():
        values = [
            row.scores[dimension]
            for row in assessments
            if isinstance(row.scores, dict)
            and isinstance(row.scores.get(dimension), (int, float))
            and not isinstance(row.scores.get(dimension), bool)
        ]
        dimension_stats.append({
            "dimension": dimension,
            "weight": weight,
            "specialist": specialist_for.get(dimension),
            "n": len(values),
            "mean": round(sum(values) / len(values), 2) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        })

    band_counts = sorted(Counter(
        row.band for row in assessments if row.band
    ).items())

    # Rows whose scores share no key with the live document contribute n=0 to
    # every dimension_stats row and pool their bands from another threshold
    # regime — count them so the tables can disclose what they exclude. Keys
    # are compared strip+lower, matching the normalization the detail page
    # applies (``assessment_detail.py``'s ``normalized_scores``), so a stored
    # key that only differs by case or incidental whitespace is not
    # miscounted as off-rubric here while the detail page still recognizes it.
    live_keys = set(RUBRIC_WEIGHTS)
    off_rubric_count = sum(
        1
        for row in assessments
        if isinstance(row.scores, dict)
        and row.scores
        and not (
            live_keys
            & {k.strip().lower() for k in row.scores if isinstance(k, str)}
        )
    )

    # Verdicts that were lost — generated and discarded, or never produced at
    # all — scoped exactly like the rows above. Without this an empty page is
    # ambiguous: "nothing screened yet" and "everything screened and every
    # verdict discarded" look identical, and the latter is only visible as a
    # WARNING in a container log. Grouped by reason so the banner can say WHICH
    # failure is happening — they have different fixes (panel never convened /
    # sidecar truncated / no sidecar emitted / interview abandoned with no
    # reply at all).
    drops_result = await db.execute(
        select(AssessmentDrop.reason, func.count())
        .where(
            AssessmentDrop.simulation_run_id == selected_run_id
            if not show_all_runs and selected_run_id
            else sa_true()
        )
        .group_by(AssessmentDrop.reason)
        .order_by(func.count().desc())
    )
    drop_counts = list(drops_result.all())
    drops_total = sum(n for _, n in drop_counts)

    return {
        "assessments": assessments,
        # (The per-row score chips, and the rubric_weights / row_scales keys
        # that fed them, left this view with the inline detail rows on
        # 2026-08-27 — per-dimension scores are detail-page content. They came
        # back on 2026-09-14, at operator request, as a collapsed per-card
        # disclosure — but this time riding on each row as `dimension_rows` /
        # `revision_view` (see the attach loop above) rather than as the
        # removed `rubric_weights`/`row_scales` top-level keys, so the
        # admin/manager split above still cannot see one without the other.)
        # The band thresholds and the decline label the page's legend states,
        # read from the rubric document rather than typed into the template.
        # The legend used to hard-code "≥4.0 / 3.0–3.9 / <3.0"; the moment
        # prompts/rubric/blackbird-rubric.toml is recalibrated (which is the
        # whole point of it being a document) those literals become a page
        # that confidently states the wrong thresholds.
        "banding": BANDING,
        # Which rubric revision the reader is looking at. Per-row stamps live
        # on the assessment itself (rubric_version/rubric_content_hash); this
        # is the CURRENT document, which is what a row with no stamp is being
        # read against.
        "rubric_version": RUBRIC_VERSION,
        "runs": runs,
        "runs_by_id": {r.id: r for r in runs},
        "selected_run_id": selected_run_id,
        "show_all_runs": show_all_runs,
        # The controls' own state. `sort`/`lab_filter` are the values actually
        # APPLIED (after the silent fallback above), not the raw parameters, so
        # the select that renders from them can never show a filter the query
        # did not use.
        "sort": sort_key,
        "sort_options": ASSESSMENT_SORT_OPTIONS,
        "lab_filter": lab_filter,
        "lab_options": lab_options,
        # The review sub-tab actually APPLIED (after the silent fallback), and
        # all three tab sizes under the same run+lab scope.
        "review": review_key,
        "review_counts": review_counts,
        # Keyed by subject_agent_id, same key space as lab_options — a
        # missing key means "no resolvable PI" (stale slug or unlinked
        # agent), not an error.
        "pi_user_ids": pi_user_ids,
        "total_count": total_count,
        "assessments_limit": ASSESSMENTS_LIMIT,
        "drop_counts": drop_counts,
        "drops_total": drops_total,
        "incomplete_panel_count": incomplete_panel_count,
        "dimension_stats": dimension_stats,
        "band_counts": band_counts,
        "assessment_counts_by_run": assessment_counts_by_run,
        "off_rubric_count": off_rubric_count,
    }
