"""HTTP-free read queries behind the admin and manager directory pages.

These six query bodies used to live directly inside `src/routers/admin.py`
handlers. They move here verbatim (Task 3 of the user-account-types plan) so
the `/manager` router can call the exact same code — most of it
by way of the `roles=` filter on `list_pi_directory` — instead of
carrying a second copy of a ~280-line discussions query. Nothing here knows
about `Request`, `HTTPException`, or Jinja2 templates: callers (routers) own
the HTTP concerns (404s, template rendering, query-param parsing) and pass in
already-parsed values.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy import true as sa_true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.agent.specialists import SPECIALIST_DOMAINS
from src.models import (
    AgentChannel,
    AgentMessage,
    AssessmentDrop,
    OpportunityAssessment,
    PiGrant,
    PiIndustryEvidence,
    PiIndustryScore,
    Publication,
    SimulationRun,
    ThreadDecision,
    User,
)
from src.services.assessment_detail import (
    has_review_filter,
    panel_state,
    unvetted_panel_filter,
)
from src.services.assessment_reviews import EMPTY_REVIEW_COLUMNS, review_columns_for
from src.services.blackbird_rubric import (
    BANDING,
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
    load_rubric,
)
from src.services.rubric_revisions import RubricRevisionView, resolve_revision
from src.services.tenure_scope import scoped_counts, scoped_publications_for

# Hard cap on rows fetched for one render of the triage queue (B1). Scoped to
# the current run this is rarely close to binding — a single run's worth of
# :mag: assessments — but "All Runs" accumulates across every run this
# instance has ever done, and the table has no other bound. Capped rather
# than paginated because this is a triage queue: the highest-scoring rows
# (the ones that matter) are always first under the existing ORDER BY, so a
# cap only ever drops the least-actionable tail, and the "N of TOTAL" note
# below says so rather than hiding the truncation.
ASSESSMENTS_LIMIT = 500

# The sort orders the triage queue offers, as (query-param value, label). ONE
# tuple, not a set of values here and a label map in each template: a sort the
# UI offers but the validator rejects would silently render the default order
# with the wrong option selected, and a sort the validator accepts but no
# template lists is unreachable.
#
# `score` stays first because it is the default and this page is a triage
# queue: the rows that need a decision are the ones that must be on top on
# arrival.
SORT_SCORE = "score"
SORT_RECENT = "recent"
SORT_RECOMMENDATION = "recommendation"
SORT_LAB = "lab"
ASSESSMENT_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    (SORT_SCORE, "Score (triage)"),
    (SORT_RECENT, "Most recent"),
    (SORT_RECOMMENDATION, "Recommendation"),
    (SORT_LAB, "Lab"),
)
ASSESSMENT_SORTS = tuple(value for value, _ in ASSESSMENT_SORT_OPTIONS)
ASSESSMENT_SORT_DEFAULT = ASSESSMENT_SORTS[0]

#: The review sub-tabs (spec 2026-09-21 §4). `unreviewed` is the default: this
#: page is a work queue, and the tab a reader lands on should be the work.
#: "Reviewed" means at least one `assessment_reviews` row (D4) — written
#: feedback, not an assignment and not a status event. The predicate itself is
#: `assessment_detail.has_review_filter()`.
ASSESSMENT_REVIEW_FILTERS: tuple[str, ...] = ("unreviewed", "reviewed", "all")
ASSESSMENT_REVIEW_DEFAULT = "unreviewed"

# Triage order for `sort=recommendation`: the model's own verdict, most
# actionable first. NOT alphabetical and NOT band order — `route-to-incubation`
# is the designed positive outcome for an incubation-stage population and sits
# inside the <3.0 band, so ranking by band would bury it under declines.
#
# A recommendation this tuple does not name (a new verdict word, a typo from
# the model) sorts WITH the NULLs, at the end: an unrecognized verdict is not
# evidence of anything, and floating it to the top of a triage queue would be.
RECOMMENDATION_TRIAGE_ORDER = ("advance", "conditional", "route-to-incubation", "pass")


def _assessment_order_by(sort: str) -> list[Any]:
    """The ORDER BY for one sort value. Ordering is done in SQL, not in Python:
    the query is LIMITed, so a Python sort would order the arbitrary 500 rows
    Postgres happened to return rather than the top 500 of the real order.

    NULLS LAST needs saying on every score term: a bare ``.desc()`` puts NULLs
    FIRST in Postgres, which would float every not-yet-scored assessment to the
    top of a triage queue instead of to the bottom.

    Branches on the SORT_* constants rather than on string literals: an option
    renamed in ``ASSESSMENT_SORT_OPTIONS`` and not here would still validate,
    still render as selected, and silently serve the default order.

    EVERY order ends in a unique column. ``created_at`` is not one: Postgres'
    ``now()`` is transaction-scoped, so verdicts written in a single transaction
    share it to the microsecond, and a run's rows really can tie. Under a LIMIT,
    an order that is not total lets the database pick which of the tied rows
    falls off the end — so the same page can gain and lose a row between two
    renders of identical data, and ``sort=recent`` (whose only term used to be
    ``created_at``) could reorder its whole first page on a refresh.
    """
    score_desc = OpportunityAssessment.weighted_score.desc().nullslast()
    created_desc = OpportunityAssessment.created_at.desc()
    id_desc = OpportunityAssessment.id.desc()
    if sort == SORT_RECENT:
        return [created_desc, id_desc]
    if sort == SORT_RECOMMENDATION:
        # Compared case- and whitespace-insensitively: the value comes from the
        # model's sidecar, and "Advance" must not fall through to the
        # unrecognized bucket. NULL matches no WHEN and lands in `else_`.
        rank = case(
            {name: index for index, name in enumerate(RECOMMENDATION_TRIAGE_ORDER)},
            value=func.lower(func.trim(OpportunityAssessment.recommendation)),
            else_=len(RECOMMENDATION_TRIAGE_ORDER),
        )
        return [rank, score_desc, created_desc, id_desc]
    if sort == SORT_LAB:
        return [
            OpportunityAssessment.subject_agent_id.asc().nullslast(),
            score_desc,
            created_desc,
            id_desc,
        ]
    return [score_desc, created_desc, id_desc]


async def list_pi_directory(
    db: AsyncSession,
    *,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    roles: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Admin users overview / manager PI directory.

    ``roles=None`` means no role filter at all — this is today's `/admin`
    behaviour, unchanged. ``roles=(USER_ROLE_PI,)`` is what the manager
    directory passes to see PIs only.
    """
    query = select(User).options(selectinload(User.profile), selectinload(User.jobs), selectinload(User.agent))
    if roles is not None:
        query = query.where(User.user_role.in_(roles))

    result = await db.execute(query)
    users = result.scalars().unique().all()

    # Publication counts, scoped to each PI's JHU tenure window (Task 13,
    # D17). `user.agent` is already eager-loaded above, so the legacy
    # agent-keyed tenure fallback (`scoped_counts`'s `agent_ids` argument) can
    # be built with no extra query — omitting it would silently unscope the
    # 62 PIs who only have a legacy entry.
    agent_ids_by_user = {u.id: (u.agent.agent_id if u.agent else None) for u in users}
    pub_scope_by_user = await scoped_counts(db, [u.id for u in users], agent_ids_by_user)

    # Latest industry-interest score per user (Postgres DISTINCT ON).
    industry_result = await db.execute(
        select(PiIndustryScore)
        .distinct(PiIndustryScore.user_id)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc())
    )
    industry_by_user = {row.user_id: row for row in industry_result.scalars().all()}

    user_data = []
    for user in users:
        profile = user.profile
        pub_scope = pub_scope_by_user.get(user.id)
        # Meaning of `pub_count` changes here (Task 13): it is now the
        # SCOPED (in-tenure) count, not the full-career count, so every
        # existing template reference to it stays correct without a rename.
        # `pub_scope` carries the split (tenure_start / before_tenure /
        # undated_excluded / scoped) for the templates that need to explain
        # the number rather than just print it.
        pub_count = pub_scope.in_tenure if pub_scope else 0

        # Profile status
        if not profile:
            profile_status = "no_profile"
        elif profile.pending_profile:
            profile_status = "pending_update"
        elif profile.research_summary:
            profile_status = "complete"
        else:
            # Check if there's a running job
            active_jobs = [j for j in user.jobs if j.status in ("pending", "processing")]
            profile_status = "generating" if active_jobs else "no_profile"

        # Apply filters
        if status_filter and profile_status != status_filter:
            continue
        if institution_filter and (not user.institution or institution_filter.lower() not in user.institution.lower()):
            continue
        if claimed_filter == "claimed" and not user.claimed_at:
            continue
        if claimed_filter == "unclaimed" and user.claimed_at:
            continue

        # Agent status
        if not user.agent:
            agent_status = "not_requested"
        elif user.agent.status == "pending":
            agent_status = "awaiting_token"
        else:
            agent_status = user.agent.status  # "active" or "suspended"

        industry_row = industry_by_user.get(user.id)
        user_data.append({
            "user": user,
            "profile": profile,
            "profile_status": profile_status,
            "pub_count": pub_count,
            "pub_scope": pub_scope,
            "agent_status": agent_status,
            "industry_score": industry_row.score if industry_row else None,
            "industry_reason": industry_row.reason if industry_row else None,
        })

    return user_data


async def load_user_detail(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    """Admin user detail page. Returns None when the row is absent (the
    router turns that into its own 404 — this module stays HTTP-free)."""
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.profile), selectinload(User.jobs), selectinload(User.agent))
    )
    user = result.scalar_one_or_none()
    if not user:
        return None

    pub_result = await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id)
        .order_by(Publication.year.desc())
    )
    publications = pub_result.scalars().all()

    # Tenure-scope the publication list (Task 13, D17) using the rows already
    # loaded above, so this costs no second SELECT. Both detail routers
    # forward an explicitly-named set of context keys rather than splatting
    # this dict, so `pub_scope` is named there too — deliberately NOT stashed
    # as an ad hoc attribute on `user`. Arbitrary
    # attributes on a mapped instance survive neither an `expire`/`refresh`
    # nor a reader who only knows the model, and the point of this change is
    # that the tenure window stops being carried by convention.
    agent_id = user.agent.agent_id if user.agent else None
    pub_scope = await scoped_publications_for(
        db, user_id, agent_id, publications=publications
    )

    grants = (await db.execute(
        select(PiGrant).where(PiGrant.user_id == user_id)
        .order_by(PiGrant.vetoed_at.is_(None).desc(), PiGrant.last_fy.desc().nullslast())
    )).scalars().all()

    industry_score = (await db.execute(
        select(PiIndustryScore).where(PiIndustryScore.user_id == user_id)
        .order_by(PiIndustryScore.computed_at.desc()).limit(1)
    )).scalar_one_or_none()
    industry_evidence = (await db.execute(
        select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id)
        .order_by(PiIndustryEvidence.vetoed_at.is_(None).desc(), PiIndustryEvidence.year.desc().nullslast())
    )).scalars().all()

    return {
        "user": user,
        "profile": user.profile,
        # Now the IN-TENURE rows, not the full career (Task 13). The rows the
        # window cuts are deliberately NOT returned: operator decision
        # 2026-09-22 — pre-tenure papers are listed nowhere in the UI. Use
        # `scripts/audit_tenure_scope.py` to see what a tenure year excludes.
        "publications": pub_scope.publications,
        "pub_scope": pub_scope,
        "jobs": sorted(user.jobs, key=lambda j: j.enqueued_at, reverse=True),
        "grants": grants,
        "industry_score": industry_score,
        "industry_evidence": industry_evidence,
    }


def _assessment_dimension_rows(
    row: OpportunityAssessment,
) -> tuple[list[dict[str, Any]], RubricRevisionView | None]:
    """The per-row dimension breakdown for the list page's collapsed
    per-card disclosure — the SAME shape (and, for a stamped row, the same
    revision) that ``build_assessment_detail`` produces for its own
    ``dimensions``, so the two surfaces cannot disagree about a dimension's
    title or bar length. Mirrors that function's ``_score_value``/``_pct``
    and its normalize-then-fall-back-to-untitled treatment of an off-rubric
    key precisely, rather than inventing a variant.

    A row with no (dict) ``scores`` at all is NOT short-circuited: it falls
    through to the named-dimension loop and yields the revision's own
    dimensions with ``score=None``, which is exactly what the detail page
    renders for the same row. An earlier draft returned ``([], None)`` here,
    which made the card say "no per-dimension scores were stored" while the
    detail page one click away showed six "not scored" rows — the same verdict
    with two answers, which is the drift this mirroring exists to prevent.
    ``([], None)`` now happens only when there is nothing to say on EITHER
    surface: no scores AND no resolvable revision.
    """
    scores = row.scores if isinstance(row.scores, dict) else {}
    normalized_scores = {
        key.strip().lower(): value
        for key, value in scores.items()
        if isinstance(key, str)
    }
    revision, _provenance = resolve_revision(row.rubric_version, row.rubric_content_hash)

    def _score_value(raw: object) -> float | None:
        return (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )

    def _pct(value: float | None) -> float | None:
        if revision is None:
            return None
        if value is None:
            return 0.0
        return min(100.0, max(0.0, value / revision.scale_max * 100.0))

    rows: list[dict[str, Any]] = []
    named_keys: set[str] = set()
    if revision is not None:
        for dim in revision.dimensions:
            value = _score_value(normalized_scores.get(dim.key))
            named_keys.add(dim.key)
            rows.append({
                "key": dim.key,
                "title": dim.title,
                "score": value,
                "weight": dim.weight,
                "weight_note": dim.weight_note,
                "pct": _pct(value),
            })
    for key in sorted(normalized_scores):
        if key in named_keys:
            continue
        value = _score_value(normalized_scores[key])
        if value is None:
            continue
        rows.append({
            "key": key,
            "title": key.replace("_", " "),
            "score": value,
            "weight": None,
            "weight_note": None,
            "pct": _pct(value),
        })
    return rows, revision


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

    # Surfaced because Task 3 stops the floor discarding a gapped verdict.
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
    # (src/routers/admin.py) and by a manager template whose router splats the
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

    # Batched Assigned/Reviewed-by/status-chip columns (Task 7) — the exact
    # same attach-to-row pattern as `panel_state` above, and for the exact
    # same reason: `_assessments_body.html` is included by an admin template
    # that allowlists every context key it forwards (`src/routers/admin.py`)
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
    # `src/routers/admin.py`) or the manager template (which splats the whole
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


async def build_discussions_view(
    db: AsyncSession,
    *,
    run_id: str | None,
    channel_filter: str | None,
    status_filter: str | None,
    agent_filter: list[str],
) -> dict[str, Any]:
    """Discussion summary: threads grouped by status.

    Stops before the router's ``if export:`` branch — export is admin-only,
    returns a different response type (``PlainTextResponse`` / an export
    template) entirely, and consumes ``threads`` from this function's return
    value. The manager router will never pass an export parameter.

    Both the "no simulation runs exist at all" early-return and the normal
    return below yield the same 9 keys (including ``agents`` and
    ``agent_filter``, which the early return used to omit) so a template can
    rely on their presence rather than on Jinja2's lenient ``Undefined``.
    """
    # Pick which simulation run to show
    runs_result = await db.execute(
        select(SimulationRun).order_by(SimulationRun.started_at.desc())
    )
    runs = runs_result.scalars().all()

    show_all_runs = run_id == "all"
    selected_run_id = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            selected_run_id = uuid.UUID(run_id)
        except ValueError:
            pass
    if not selected_run_id and runs:
        selected_run_id = runs[0].id

    if not selected_run_id:
        return {
            "runs": runs,
            "selected_run_id": None,
            "threads": [],
            "counts": {},
            "channels": [],
            "agents": [],
            "channel_filter": channel_filter,
            "status_filter": status_filter,
            "agent_filter": [],
        }

    # Get all root posts (new_post phase, no thread_ts)
    roots_query = select(AgentMessage).where(
        AgentMessage.phase == "new_post",
        AgentMessage.thread_ts.is_(None),
    )
    if not show_all_runs:
        roots_query = roots_query.where(AgentMessage.simulation_run_id == selected_run_id)
    roots_result = await db.execute(roots_query.order_by(AgentMessage.created_at)
    )
    root_posts = roots_result.scalars().all()

    # Get reply counts and replier agent IDs per thread
    reply_query = select(
        AgentMessage.thread_ts,
        func.count(AgentMessage.id).label("reply_count"),
    ).where(AgentMessage.phase == "thread_reply")
    if not show_all_runs:
        reply_query = reply_query.where(AgentMessage.simulation_run_id == selected_run_id)
    reply_counts_result = await db.execute(reply_query.group_by(AgentMessage.thread_ts))
    reply_count_map = {r.thread_ts: r.reply_count for r in reply_counts_result}

    # Get distinct replier agent IDs per thread
    replier_query = select(AgentMessage.thread_ts, AgentMessage.agent_id).where(
        AgentMessage.phase == "thread_reply",
    )
    if not show_all_runs:
        replier_query = replier_query.where(AgentMessage.simulation_run_id == selected_run_id)
    repliers_result = await db.execute(replier_query.distinct())
    replier_map: dict[str, set[str]] = {}
    for r in repliers_result:
        replier_map.setdefault(r.thread_ts, set()).add(r.agent_id)

    # Get thread decisions
    decisions_query = select(ThreadDecision)
    if not show_all_runs:
        decisions_query = decisions_query.where(ThreadDecision.simulation_run_id == selected_run_id)
    decisions_result = await db.execute(decisions_query.order_by(ThreadDecision.decided_at))
    all_decisions = decisions_result.scalars().all()

    # Build a map: thread_id -> final outcome (last decision wins)
    decision_map: dict[str, ThreadDecision] = {}
    for d in all_decisions:
        decision_map[d.thread_id] = d

    # Build thread list
    threads = []
    available_channels = set()
    for post in root_posts:
        ts = post.message_ts
        available_channels.add(post.channel_name)
        reply_count = reply_count_map.get(ts, 0)
        repliers = replier_map.get(ts, set())
        decision = decision_map.get(ts)

        # Find the other agent (replier who isn't the poster)
        other_agents = repliers - {post.agent_id}
        replier = next(iter(other_agents), None) if other_agents else None

        if decision:
            if decision.outcome == "proposal":
                thread_status = "proposal"
            elif decision.outcome == "no_proposal":
                thread_status = "no_proposal"
            elif decision.outcome == "timeout":
                thread_status = "timeout"
            else:
                thread_status = decision.outcome
        elif reply_count > 0:
            thread_status = "active"
        else:
            thread_status = "no_replies"

        threads.append({
            "message_ts": ts,
            "channel_name": post.channel_name,
            "agent_id": post.agent_id,
            "created_at": post.created_at,
            "reply_count": reply_count,
            "replier": replier,
            "status": thread_status,
            "decision": decision,
        })

    # Apply filters
    if channel_filter:
        threads = [t for t in threads if t["channel_name"] == channel_filter]
    if status_filter:
        threads = [t for t in threads if t["status"] == status_filter]

    # Get proposal reviews
    from src.models import ProposalReview as PR
    reviews_query = select(PR).join(ThreadDecision, PR.thread_decision_id == ThreadDecision.id)
    if not show_all_runs:
        reviews_query = reviews_query.where(ThreadDecision.simulation_run_id == selected_run_id)
    reviews_result = await db.execute(reviews_query.order_by(PR.reviewed_at))
    all_reviews = reviews_result.scalars().all()
    reviews_by_decision: dict[str, list] = {}
    for rev in all_reviews:
        reviews_by_decision.setdefault(str(rev.thread_decision_id), []).append(rev)

    # Attach reviews to threads
    for t in threads:
        if t["decision"]:
            t["reviews"] = reviews_by_decision.get(str(t["decision"].id), [])
        else:
            t["reviews"] = []

    # Add orphaned decisions (thread_decisions with no matching root post in agent_messages)
    known_thread_ids = {t["message_ts"] for t in threads}
    for td in all_decisions:
        if td.thread_id not in known_thread_ids:
            other_agents = replier_map.get(td.thread_id, set())
            poster_id = td.agent_a
            replier = td.agent_b if td.agent_a == poster_id else td.agent_a
            threads.append({
                "message_ts": td.thread_id,
                "channel_name": td.channel,
                "agent_id": poster_id,
                "created_at": td.decided_at,
                "reply_count": reply_count_map.get(td.thread_id, 0),
                "replier": replier,
                "status": td.outcome,
                "decision": td,
                "reviews": reviews_by_decision.get(str(td.id), []),
            })
            known_thread_ids.add(td.thread_id)
            available_channels.add(td.channel)

    # Count by status (before filtering)
    counts: dict[str, int] = {}
    for t in threads:
        s = t["status"]
        counts[s] = counts.get(s, 0) + 1

    # Collect available agents from threads.
    #
    # Every add is None-guarded, including the poster's. `agent_id` is nullable
    # on agent_messages and really is NULL in production: _rebuild_state_from_slack
    # records a real Slack message whose sender maps to no known bot as
    # `is_bot=True, agent_id=NULL` (measured: 7 rows, all from one raw Slack user
    # id). This set is sorted() below, so a single None took the whole page down
    # with "'<' not supported between instances of 'NoneType' and 'str'". The
    # replier and decision adds were already guarded; the poster's was not.
    available_agents = set()
    for t in threads:
        for candidate in (
            t["agent_id"],
            t.get("replier"),
            t["decision"].agent_a if t.get("decision") else None,
            t["decision"].agent_b if t.get("decision") else None,
        ):
            if candidate:
                available_agents.add(candidate)

    # Apply filters
    if channel_filter:
        threads = [t for t in threads if t["channel_name"] == channel_filter]
    if status_filter:
        threads = [t for t in threads if t["status"] == status_filter]
    if agent_filter:
        agent_set = set(agent_filter)
        threads = [
            t for t in threads
            if t["agent_id"] in agent_set
            or (t.get("replier") and t["replier"] in agent_set)
            or (t.get("decision") and (
                t["decision"].agent_a in agent_set or t["decision"].agent_b in agent_set
            ))
        ]

    return {
        "runs": runs,
        "selected_run_id": selected_run_id,
        "threads": threads,
        "counts": counts,
        "channels": sorted(available_channels),
        "agents": sorted(available_agents),
        "channel_filter": channel_filter,
        "status_filter": status_filter,
        "agent_filter": agent_filter or [],
    }


async def list_runs_overview(db: AsyncSession) -> dict[str, Any]:
    """Agent activity overview."""
    runs_result = await db.execute(
        select(SimulationRun).order_by(SimulationRun.started_at.desc())
    )
    runs = runs_result.scalars().all()

    # Summary stats
    total_messages_result = await db.execute(
        select(func.sum(SimulationRun.total_messages))
    )
    total_messages = total_messages_result.scalar() or 0

    total_channels_result = await db.execute(
        select(func.count(AgentChannel.id))
    )
    total_channels = total_channels_result.scalar() or 0

    # Most active agent
    agent_count_result = await db.execute(
        select(AgentMessage.agent_id, func.count(AgentMessage.id).label("count"))
        .group_by(AgentMessage.agent_id)
        .order_by(func.count(AgentMessage.id).desc())
        .limit(1)
    )
    most_active = agent_count_result.first()

    return {
        "runs": runs,
        "total_runs": len(runs),
        "total_messages": total_messages,
        "total_channels": total_channels,
        "most_active_agent": most_active.agent_id if most_active else None,
        "most_active_count": most_active.count if most_active else 0,
    }


async def build_run_detail(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Simulation run detail. Returns None when the row is absent (the
    router turns that into its own 404 — this module stays HTTP-free)."""
    run_result = await db.execute(
        select(SimulationRun).where(SimulationRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        return None

    # Messages for this run
    messages_result = await db.execute(
        select(AgentMessage)
        .where(AgentMessage.simulation_run_id == run_id)
        .order_by(AgentMessage.created_at)
    )
    messages = messages_result.scalars().all()

    # Channels for this run
    channels_result = await db.execute(
        select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
    )
    channels = channels_result.scalars().all()

    # Aggregate by agent
    agent_stats: dict[str, dict] = {}
    for msg in messages:
        if msg.agent_id not in agent_stats:
            agent_stats[msg.agent_id] = {"count": 0, "total_length": 0}
        agent_stats[msg.agent_id]["count"] += 1
        agent_stats[msg.agent_id]["total_length"] += msg.message_length

    for agent_id, stats in agent_stats.items():
        stats["avg_length"] = (
            stats["total_length"] // stats["count"] if stats["count"] > 0 else 0
        )

    # Aggregate by channel
    #
    # The agent add is None-guarded: `agent_id` is nullable on agent_messages
    # and really is NULL in production — _rebuild_state_from_slack records a
    # real Slack message whose sender maps to no known bot as
    # `is_bot=True, agent_id=NULL`. This set is sorted() in the template
    # (activity_detail.html), so an unguarded add of a single None took the
    # whole page down with "'<' not supported between instances of
    # 'NoneType' and 'str'" — the same bug class fixed for /admin/discussions
    # in 73a78c3.
    channel_stats: dict[str, dict] = {}
    for msg in messages:
        if msg.channel_name not in channel_stats:
            channel_stats[msg.channel_name] = {"count": 0, "agents": set()}
        channel_stats[msg.channel_name]["count"] += 1
        if msg.agent_id:
            channel_stats[msg.channel_name]["agents"].add(msg.agent_id)

    return {
        "run": run,
        "messages": messages,
        "channels": channels,
        "agent_stats": agent_stats,
        "channel_stats": channel_stats,
    }
