"""HTTP-free read queries behind the admin and manager directory pages.

These six query bodies used to live directly inside the admin router's
handlers (now the `src/routers/admin/` package). They move here verbatim (Task 3 of
docs/plans/2026-08-17-user-account-types-plan.md) so
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

from sqlalchemy import String, Text, and_, case, cast, func, literal_column, or_, select, union_all
from sqlalchemy import true as sa_true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from src.agent.specialists import SPECIALIST_DOMAINS
from src.models import (
    AgentChannel,
    AgentMessage,
    AssessmentDrop,
    Job,
    OpportunityAssessment,
    PiGrant,
    PiGrantIdentity,
    PiIndustryEvidence,
    PiIndustryScore,
    PiOrcidFunding,
    Publication,
    ResearcherProfile,
    SimulationRun,
    ThreadDecision,
    User,
)
from src.services.assessment_detail import (
    build_dimension_rows,
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
from src.services.grant_sections import load_grant_sections
from src.services.rubric_revisions import RubricRevisionView, resolve_revision
from src.services.runs import runs_ordered
from src.services.tenure_scope import (
    publication_order_by,
    scoped_counts,
    scoped_publications_for,
)

# Hard cap on rows fetched for one render of the triage queue (B1). Scoped to
# the current run this is rarely close to binding — a single run's worth of
# :mag: assessments — but "All Runs" accumulates across every run this
# instance has ever done, and the table has no other bound. Capped rather
# than paginated because this is a triage queue: the highest-scoring rows
# (the ones that matter) are always first under the existing ORDER BY, so a
# cap only ever drops the least-actionable tail, and the "N of TOTAL" note
# below says so rather than hiding the truncation.
ASSESSMENTS_LIMIT = 500

#: Page sizes for the lists that used to load every row.
JOBS_PAGE_SIZE = 100
#: Rows per page of the admin users list and the manager PI directory (C-15).
PI_DIRECTORY_PAGE_SIZE = 100
RUN_MESSAGES_PAGE_SIZE = 200
DISCUSSIONS_PAGE_SIZE = 200
#: C-15: the HTML discussions page lists `run_id=all` only while every run together
#: holds at most this many threads; above it the page asks for one run. The admin
#: export (`page=None`) is never capped.
DISCUSSIONS_ALL_RUNS_MAX = 2_000
#: Upper bound for every `?page=` query parameter, so OFFSET stays in range.
MAX_PAGE = 100_000

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


def _pi_directory_query(*, status_filter, institution_filter, claimed_filter, roles):
    """One row per user, ``(User, profile_status)``, every filter applied in SQL.

    ``profile_status`` mirrors the Python rules it replaced: no profile row ->
    ``no_profile``; a non-empty ``pending_profile`` -> ``pending_update``; a non-empty
    ``research_summary`` -> ``complete``; else ``generating`` while a profile job is
    pending or processing, otherwise ``no_profile``. "Non-empty" for the JSON column
    excludes the JSON scalar ``null`` and an empty object or list, which Python read
    as falsy."""
    active_job = (
        select(Job.id)
        .where(Job.user_id == User.id, Job.status.in_(("pending", "processing")))
        .exists()
    )
    status_expr = case(
        (ResearcherProfile.id.is_(None), literal_column("'no_profile'")),
        (
            and_(
                ResearcherProfile.pending_profile.is_not(None),
                cast(ResearcherProfile.pending_profile, Text).not_in(("null", "{}", "[]")),
            ),
            literal_column("'pending_update'"),
        ),
        (func.coalesce(ResearcherProfile.research_summary, "") != "", literal_column("'complete'")),
        (active_job, literal_column("'generating'")),
        else_=literal_column("'no_profile'"),
    )
    query = select(User, status_expr.label("profile_status")).outerjoin(
        ResearcherProfile, ResearcherProfile.user_id == User.id
    )
    if roles is not None:
        query = query.where(User.user_role.in_(roles))
    if status_filter:
        query = query.where(status_expr == status_filter)
    if institution_filter:
        # strpos, not LIKE: a substring match with no wildcard escaping to get wrong.
        query = query.where(func.strpos(func.lower(User.institution), institution_filter.lower()) > 0)
    if claimed_filter == "claimed":
        query = query.where(User.claimed_at.is_not(None))
    elif claimed_filter == "unclaimed":
        query = query.where(User.claimed_at.is_(None))
    return query


async def count_pi_directory(
    db: AsyncSession,
    *,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    roles: tuple[str, ...] | None = None,
) -> int:
    """How many rows `list_pi_directory` returns over all pages for the same filters."""
    query = _pi_directory_query(
        status_filter=status_filter, institution_filter=institution_filter,
        claimed_filter=claimed_filter, roles=roles,
    )
    return (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()


async def list_pi_directory(
    db: AsyncSession,
    *,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    roles: tuple[str, ...] | None = None,
    page: int | None = None,
) -> list[dict[str, Any]]:
    """Admin users overview / manager PI directory, by name (C-15: filtered and paged
    in SQL). ``page=None`` returns every row; otherwise one page of
    ``PI_DIRECTORY_PAGE_SIZE``.

    ``roles=None`` means no role filter at all — the `/admin` behaviour.
    ``roles=(USER_ROLE_PI,)`` is what the manager directory passes to see PIs only.
    """
    query = (
        _pi_directory_query(
            status_filter=status_filter, institution_filter=institution_filter,
            claimed_filter=claimed_filter, roles=roles,
        )
        .options(selectinload(User.profile), selectinload(User.agent))
        .order_by(User.name, User.id)
    )
    if page is not None:
        query = query.limit(PI_DIRECTORY_PAGE_SIZE).offset((page - 1) * PI_DIRECTORY_PAGE_SIZE)
    pairs = (await db.execute(query)).unique().all()
    users = [u for u, _ in pairs]
    status_by_user = {u.id: status for u, status in pairs}

    # Publication counts, scoped to each PI's JHU tenure window (Task 13 and
    # D17 of docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md).
    # `user.agent` is already eager-loaded above, so the legacy
    # agent-keyed tenure fallback (`scoped_counts`'s `agent_ids` argument) can
    # be built with no extra query — omitting it would silently unscope the
    # 62 PIs who only have a legacy entry.
    agent_ids_by_user = {u.id: (u.agent.agent_id if u.agent else None) for u in users}
    pub_scope_by_user = await scoped_counts(db, [u.id for u in users], agent_ids_by_user)

    # Latest industry-interest score per listed user (Postgres DISTINCT ON).
    industry_result = await db.execute(
        select(PiIndustryScore)
        .where(PiIndustryScore.user_id.in_([u.id for u in users]))
        .distinct(PiIndustryScore.user_id)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc())
    )
    industry_by_user = {row.user_id: row for row in industry_result.scalars().all()}

    user_data = []
    for user in users:
        pub_scope = pub_scope_by_user.get(user.id)
        # `pub_count` is the SCOPED (in-tenure) count, not the full-career count;
        # `pub_scope` carries the split for the templates that explain the number.
        pub_count = pub_scope.in_tenure if pub_scope else 0
        if not user.agent:
            agent_status = "not_requested"
        elif user.agent.status == "pending":
            agent_status = "awaiting_token"
        else:
            agent_status = user.agent.status  # "active" or "suspended"
        industry_row = industry_by_user.get(user.id)
        user_data.append({
            "user": user,
            "profile": user.profile,
            "profile_status": status_by_user[user.id],
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
        .order_by(*publication_order_by())
    )
    publications = pub_result.scalars().all()

    # Tenure-scope the publication list (D17 of
    # docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md) using
    # the rows already loaded above, so this costs no second SELECT. Both detail routers
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
    grant_identity = (await db.execute(
        select(PiGrantIdentity).where(PiGrantIdentity.user_id == user_id)
    )).scalar_one_or_none()
    orcid_fundings = (await db.execute(
        select(PiOrcidFunding).where(PiOrcidFunding.user_id == user_id)
        .order_by(
            PiOrcidFunding.vetoed_at.is_(None).desc(),
            PiOrcidFunding.end_year.desc().nullsfirst(),
            PiOrcidFunding.title,
        )
    )).scalars().all()
    # What the persona renders (the export's own tenure year, provisional included),
    # so the manager card's counts and D57 note match the file.
    grant_sections = await load_grant_sections(db, user_id)

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
        # Now the IN-TENURE rows, not the full career. The rows the
        # window cuts are deliberately NOT returned: operator decision
        # 2026-09-22 — pre-tenure papers are listed nowhere in the UI. Use
        # `scripts/audit_tenure_scope.py` to see what a tenure year excludes.
        "publications": pub_scope.publications,
        "pub_scope": pub_scope,
        "jobs": sorted(user.jobs, key=lambda j: j.enqueued_at, reverse=True),
        "grants": grants,
        "grant_identity": grant_identity,
        "orcid_fundings": orcid_fundings,
        "grant_sections": grant_sections,
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
    title or bar length. It delegates to ``build_dimension_rows``, which owns the
    normalize-then-fall-back-to-untitled treatment of an off-rubric key.

    A row with no (dict) ``scores`` at all is NOT short-circuited: it falls
    through to the named-dimension loop and yields the revision's own
    dimensions with ``score=None``, which is exactly what the detail page
    renders for the same row. An earlier draft returned ``([], None)`` here,
    which made the card say "no per-dimension scores were stored" while the
    detail page one click away showed six "not scored" rows — the same verdict
    with two answers, which is the drift this mirroring exists to prevent.
    ``([], None)`` now happens only when there is nothing to say on EITHER
    surface: no scores AND no resolvable revision.

    The rows are the detail builder's (AP-10), so they also carry ``rationale``,
    which the directory template does not read.
    """
    revision, _provenance = resolve_revision(row.rubric_version, row.rubric_content_hash)
    return build_dimension_rows(row, revision), revision


def _resolve_run_selection(
    runs: list[SimulationRun], run_id: str | None
) -> tuple[bool, uuid.UUID | str | None]:
    """``(show_all_runs, selected_run_id)``: ``run_id == "all"`` selects every run;
    an absent or unparseable id, or one naming no run in ``runs``, falls back to the newest run
    (``runs[0]``)."""
    show_all_runs = run_id == "all"
    selected_run_id: uuid.UUID | str | None = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            parsed = uuid.UUID(run_id)
        except ValueError:
            parsed = None
        # B-13: a well-formed id that names no run (a stale bookmark, a purged
        # run) is treated like no id at all.
        if parsed is not None and any(r.id == parsed for r in runs):
            selected_run_id = parsed
    if not selected_run_id and runs:
        selected_run_id = runs[0].id
    return show_all_runs, selected_run_id


async def _filtered_assessment_query(
    db: AsyncSession,
    selected_run_id: uuid.UUID | str | None,
    show_all_runs: bool,
    lab: str | None,
    review_key: str,
) -> tuple[Any, Any, list[str], str | None]:
    """``(query, scope_query, lab_options, lab_filter)``: the assessment query
    narrowed by run, lab and review tab, plus the run+lab scope the tab counts
    are taken from and the lab dropdown's options."""
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
    return query, scope_query, lab_options, lab_filter


async def _review_counts(db: AsyncSession, scope_query: Any) -> dict[str, int]:
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
    return {
        "reviewed": reviewed_count,
        "unreviewed": all_count - reviewed_count,
        "all": all_count,
    }


async def _incomplete_panel_count(
    db: AsyncSession, selected_run_id: uuid.UUID | str | None, show_all_runs: bool
) -> int:
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
    return (await db.execute(incomplete_query)).scalar_one()


async def _annotate_rows(
    db: AsyncSession, assessments: list[OpportunityAssessment]
) -> dict[str, str]:
    """Attach ``panel_state``, ``review_cols`` and the dimension rows to each row
    and return the ``agent_id -> user_id`` map of the rows' subject labs."""
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
    return pi_user_ids


def _dimension_stats(assessments: list[OpportunityAssessment]) -> list[dict[str, Any]]:
    """Per-dimension score distribution over the displayed rows."""
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
    return dimension_stats


async def _drop_counts(
    db: AsyncSession, selected_run_id: uuid.UUID | str | None, show_all_runs: bool
) -> list[Any]:
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
    return list(drops_result.all())


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
    runs = await runs_ordered(db)

    # Stored rows per run — the dropdown's honesty device: an old run showing
    # "0 stored" is distinguishable from a populated one, and (post-purge) from
    # a run whose rows exist only in the offline backup.
    counts_result = await db.execute(
        select(OpportunityAssessment.simulation_run_id, func.count())
        .group_by(OpportunityAssessment.simulation_run_id)
    )
    assessment_counts_by_run = dict(counts_result.all())

    show_all_runs, selected_run_id = _resolve_run_selection(runs, run_id)

    sort_key = sort if sort in ASSESSMENT_SORTS else ASSESSMENT_SORT_DEFAULT
    review_key = (
        review if review in ASSESSMENT_REVIEW_FILTERS else ASSESSMENT_REVIEW_DEFAULT
    )

    query, scope_query, lab_options, lab_filter = await _filtered_assessment_query(
        db, selected_run_id, show_all_runs, lab, review_key
    )

    # Counts the current filter — run AND lab AND the review tab — so the
    # "top N of TOTAL" note describes the table the reader is looking at.
    total_count = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0

    review_counts = await _review_counts(db, scope_query)

    incomplete_panel_count = await _incomplete_panel_count(db, selected_run_id, show_all_runs)

    # Ordering (see _assessment_order_by, which documents the NULLS LAST
    # discipline) is applied AFTER total_count: a count over an ordered
    # subquery is the same number and more work.
    query = query.order_by(*_assessment_order_by(sort_key)).limit(ASSESSMENTS_LIMIT)

    result = await db.execute(query)
    assessments = result.scalars().all()

    pi_user_ids = await _annotate_rows(db, assessments)

    dimension_stats = _dimension_stats(assessments)

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

    drop_counts = await _drop_counts(db, selected_run_id, show_all_runs)
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


def _select_run(runs: list[SimulationRun], run_id: str | None) -> uuid.UUID | str | None:
    """The run (or ``"all"``) the page shows: the requested one when it names a run in
    ``runs``, else the newest (B-13, the assessments pages' rule)."""
    return _resolve_run_selection(runs, run_id)[1]


def _run_scope(column, selected_run_id, show_all_runs: bool) -> list[Any]:
    return [] if show_all_runs else [column == selected_run_id]


def _thread_rows(selected_run_id, show_all_runs: bool):
    """One SQL row per thread: every root post (``new_post``, no ``thread_ts``), then
    every thread whose decisions have no root post. A thread's decision is its LAST by
    ``decided_at``; reply counts span every run under ``run_id=all``. ``grp`` is 0 for
    a root, 1 for an orphan; ``row_key`` is a unique tiebreak for stable paging."""
    roots = (
        select(
            AgentMessage.message_ts.label("ts"),
            AgentMessage.channel_name.label("channel"),
            AgentMessage.agent_id.label("poster"),
            AgentMessage.created_at.label("at"),
            AgentMessage.id.label("row_key"),
        )
        .where(
            AgentMessage.phase == "new_post",
            AgentMessage.thread_ts.is_(None),
            *_run_scope(AgentMessage.simulation_run_id, selected_run_id, show_all_runs),
        )
        .cte("roots")
    )
    replies = (
        select(AgentMessage.thread_ts.label("ts"), func.count(AgentMessage.id).label("n"))
        .where(
            AgentMessage.phase == "thread_reply",
            *_run_scope(AgentMessage.simulation_run_id, selected_run_id, show_all_runs),
        )
        .group_by(AgentMessage.thread_ts)
        .cte("replies")
    )
    last_dec = (
        select(
            ThreadDecision.thread_id.label("ts"),
            ThreadDecision.id.label("decision_id"),
            cast(ThreadDecision.outcome, String).label("outcome"),
            ThreadDecision.channel,
            ThreadDecision.agent_a,
            ThreadDecision.agent_b,
            ThreadDecision.decided_at,
        )
        .where(*_run_scope(ThreadDecision.simulation_run_id, selected_run_id, show_all_runs))
        .distinct(ThreadDecision.thread_id)
        .order_by(ThreadDecision.thread_id, ThreadDecision.decided_at.desc(), ThreadDecision.id.desc())
        .cte("last_dec")
    )
    root_replies = func.coalesce(replies.c.n, literal_column("0"))
    root_rows = select(
        roots.c.ts.label("message_ts"),
        roots.c.channel.label("channel_name"),
        roots.c.poster.label("agent_id"),
        roots.c.at.label("created_at"),
        root_replies.label("reply_count"),
        case(
            (last_dec.c.decision_id.is_not(None), last_dec.c.outcome),
            (root_replies > 0, literal_column("'active'")),
            else_=literal_column("'no_replies'"),
        ).label("status"),
        last_dec.c.decision_id,
        last_dec.c.agent_a,
        last_dec.c.agent_b,
        literal_column("0").label("grp"),
        roots.c.row_key,
    ).select_from(
        roots.outerjoin(replies, replies.c.ts == roots.c.ts).outerjoin(
            last_dec, last_dec.c.ts == roots.c.ts
        )
    )
    orphan_rows = (
        select(
            last_dec.c.ts,
            last_dec.c.channel,
            last_dec.c.agent_a,
            last_dec.c.decided_at,
            func.coalesce(replies.c.n, literal_column("0")),
            last_dec.c.outcome,
            last_dec.c.decision_id,
            last_dec.c.agent_a,
            last_dec.c.agent_b,
            literal_column("1"),
            last_dec.c.decision_id,
        )
        .select_from(last_dec.outerjoin(replies, replies.c.ts == last_dec.c.ts))
        .where(~select(roots.c.ts).where(roots.c.ts == last_dec.c.ts).exists())
    )
    return union_all(root_rows, orphan_rows).subquery("threads")


def _repliers_of(threads, scope_for_reply):
    """(thread message_ts, agent_id) of every non-poster replier of a root thread."""
    rep = aliased(AgentMessage)
    return (
        select(threads.c.message_ts, rep.agent_id)
        .select_from(threads)
        .join(rep, and_(
            rep.thread_ts == threads.c.message_ts, rep.phase == "thread_reply", *scope_for_reply(rep),
        ))
        .where(
            threads.c.grp == 0,
            rep.agent_id.is_not(None),
            rep.agent_id.is_distinct_from(threads.c.agent_id),
        )
        .distinct()
    )


def _thread_conditions(threads, scope_for_reply, channel_filter, status_filter, agent_filter) -> list[Any]:
    """The channel, status and agent filters as SQL. An agent matches as poster, as
    either side of the thread's decision, or (root threads) as any non-poster replier."""
    conds: list[Any] = []
    if channel_filter:
        conds.append(threads.c.channel_name == channel_filter)
    if status_filter:
        conds.append(threads.c.status == status_filter)
    if agent_filter:
        agents = list(agent_filter)
        rep = aliased(AgentMessage)
        replier_match = (
            select(rep.id)
            .where(
                rep.phase == "thread_reply",
                rep.thread_ts == threads.c.message_ts,
                rep.agent_id.in_(agents),
                rep.agent_id.is_distinct_from(threads.c.agent_id),
                *scope_for_reply(rep),
            )
            .exists()
        )
        conds.append(or_(
            threads.c.agent_id.in_(agents),
            threads.c.agent_a.in_(agents),
            threads.c.agent_b.in_(agents),
            and_(threads.c.grp == 0, replier_match),
        ))
    return conds


async def _thread_summary(db: AsyncSession, threads, scope_for_reply) -> tuple[dict[str, int], list[str], list[str]]:
    """Status counts, channels and agents over the whole selection, before any filter:
    each summary card links to a status filter alone, so its number must be what that
    link lists."""
    counts = {
        status: n
        for status, n in (
            await db.execute(select(threads.c.status, func.count()).group_by(threads.c.status))
        ).all()
    }
    channels = sorted(
        c for c in (await db.execute(select(threads.c.channel_name).distinct())).scalars() if c
    )
    agents: set[str] = set()
    for row in (
        await db.execute(select(threads.c.agent_id, threads.c.agent_a, threads.c.agent_b).distinct())
    ).all():
        agents.update(a for a in row if a)
    agents.update(a for _ts, a in (await db.execute(_repliers_of(threads, scope_for_reply))).all() if a)
    return counts, channels, sorted(agents)


async def _thread_page(db: AsyncSession, threads, conds: list[Any], page: int | None, scope: list[Any]) -> list[dict[str, Any]]:
    """The filtered threads of one page (every one when ``page`` is None), with each
    row's decision loaded for that page only."""
    query = select(threads).where(*conds).order_by(
        threads.c.grp, threads.c.created_at, threads.c.message_ts, threads.c.row_key
    )
    if page is not None:
        query = query.limit(DISCUSSIONS_PAGE_SIZE).offset((page - 1) * DISCUSSIONS_PAGE_SIZE)
    rows = (await db.execute(query)).all()
    decision_ids = [r.decision_id for r in rows if r.decision_id is not None]
    decisions = (
        {d.id: d for d in (await db.execute(select(ThreadDecision).where(ThreadDecision.id.in_(decision_ids)))).scalars()}
        if decision_ids else {}
    )
    page_ts = [r.message_ts for r in rows if r.grp == 0]
    repliers: dict[str, set[str]] = {}
    if page_ts:
        for ts, agent in (
            await db.execute(
                select(AgentMessage.thread_ts, AgentMessage.agent_id)
                .where(
                    AgentMessage.phase == "thread_reply",
                    AgentMessage.thread_ts.in_(page_ts),
                    AgentMessage.agent_id.is_not(None),
                    *scope,
                )
                .distinct()
            )
        ).all():
            repliers.setdefault(ts, set()).add(agent)
    return [
        {
            "message_ts": r.message_ts,
            "channel_name": r.channel_name,
            "agent_id": r.agent_id,
            "created_at": r.created_at,
            "reply_count": r.reply_count,
            "replier": (
                min(repliers.get(r.message_ts, set()) - {r.agent_id}, default=None)
                if r.grp == 0 else r.agent_b
            ),
            "status": r.status,
            "decision": decisions.get(r.decision_id),
        }
        for r in rows
    ]


async def build_discussions_view(
    db: AsyncSession,
    *,
    run_id: str | None,
    channel_filter: str | None,
    status_filter: str | None,
    agent_filter: list[str],
    page: int | None = 1,
) -> dict[str, Any]:
    """Discussion summary: threads grouped by status, filtered and paged in SQL (C-15).

    ``page=None`` returns every thread (the admin export); otherwise ``threads`` is
    one page of ``DISCUSSIONS_PAGE_SIZE`` and the result carries ``page``,
    ``page_count`` and ``thread_total`` (the filtered thread count). ``counts``,
    ``channels`` and ``agents`` cover the whole selection before any filter or paging.
    ``all_runs_refused`` is True when ``run_id="all"`` is asked for a page and every
    run together holds more than ``DISCUSSIONS_ALL_RUNS_MAX`` threads; ``threads`` is
    then empty and the page asks for one run.

    Thread semantics are those of the frozen oracle
    (tests/unit/_frozen_discussions.py): a root post or an orphaned decision; the last
    decision by ``decided_at``; replies across every run under ``run_id="all"``. Where
    that version depended on set order, this one is deterministic: the shown
    ``replier`` is the alphabetically first non-poster replier, the agent filter and
    the agent list consider every non-poster replier, and an orphan sorts by its last
    decision.

    Stops before the router's ``if export:`` branch, and both the no-runs early return
    and the normal return yield the same keys, so a template can rely on them.
    """
    runs = await runs_ordered(db)
    selected_run_id = _select_run(runs, run_id)

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
            "page": 1,
            "page_count": 1,
            "thread_total": 0,
            "all_runs_refused": False,
        }

    show_all_runs = run_id == "all"
    scope = _run_scope(AgentMessage.simulation_run_id, selected_run_id, show_all_runs)

    def scope_for_reply(rep):
        return _run_scope(rep.simulation_run_id, selected_run_id, show_all_runs)

    threads = _thread_rows(selected_run_id, show_all_runs)
    counts, channels, agents = await _thread_summary(db, threads, scope_for_reply)
    conds = _thread_conditions(threads, scope_for_reply, channel_filter, status_filter, agent_filter)
    thread_total = (
        await db.execute(select(func.count()).select_from(threads).where(*conds))
    ).scalar_one()
    page_count = max(1, -(-thread_total // DISCUSSIONS_PAGE_SIZE))
    page_out = 1 if page is None else max(1, page)
    refused = show_all_runs and page is not None and sum(counts.values()) > DISCUSSIONS_ALL_RUNS_MAX
    listed = [] if refused else await _thread_page(
        db, threads, conds, None if page is None else page_out, scope
    )

    return {
        "runs": runs,
        "selected_run_id": selected_run_id,
        "threads": listed,
        "counts": counts,
        "channels": channels,
        "agents": agents,
        "channel_filter": channel_filter,
        "status_filter": status_filter,
        "agent_filter": agent_filter or [],
        "page": page_out,
        "page_count": page_count,
        "thread_total": thread_total,
        "all_runs_refused": refused,
    }


async def list_runs_overview(db: AsyncSession) -> dict[str, Any]:
    """Agent activity overview."""
    runs = await runs_ordered(db)

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


async def build_run_detail(
    db: AsyncSession, run_id: uuid.UUID, *, page: int = 1
) -> dict[str, Any] | None:
    """Simulation run detail. Returns None when the row is absent (the
    router turns that into its own 404 — this module stays HTTP-free).

    The per-agent and per-channel stats are SQL aggregates over the whole run;
    ``messages`` is one page (``RUN_MESSAGES_PAGE_SIZE``) of column rows, with
    no ``content``, ordered by time. ``page`` is clamped to
    ``1..page_count``.
    """
    run_result = await db.execute(
        select(SimulationRun).where(SimulationRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        return None

    message_total = await db.scalar(
        select(func.count(AgentMessage.id)).where(AgentMessage.simulation_run_id == run_id)
    ) or 0
    # D-27: clamp to the last page, so a stale or hand-edited ?page= past the end
    # shows that page (and its pager) instead of an empty timeline with no way back.
    page_count = max(1, -(-message_total // RUN_MESSAGES_PAGE_SIZE))
    page = min(max(1, page), page_count)
    messages = (await db.execute(
        select(
            AgentMessage.agent_id,
            AgentMessage.channel_name,
            AgentMessage.created_at,
            AgentMessage.phase,
            AgentMessage.message_length,
        )
        .where(AgentMessage.simulation_run_id == run_id)
        .order_by(AgentMessage.created_at, AgentMessage.id)
        .limit(RUN_MESSAGES_PAGE_SIZE)
        .offset((page - 1) * RUN_MESSAGES_PAGE_SIZE)
    )).all()

    # Channels for this run
    channels_result = await db.execute(
        select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
    )
    channels = channels_result.scalars().all()

    # Aggregate by agent
    agent_stats: dict[str | None, dict[str, int]] = {
        agent_id: {
            "count": n,
            "total_length": int(total_len or 0),
            "avg_length": int(total_len or 0) // n if n else 0,
        }
        for agent_id, n, total_len in (await db.execute(
            select(AgentMessage.agent_id, func.count(), func.sum(AgentMessage.message_length))
            .where(AgentMessage.simulation_run_id == run_id)
            .group_by(AgentMessage.agent_id)
            # First-appearance order, as the per-message loop this replaced built it.
            .order_by(func.min(AgentMessage.created_at))
        )).all()
    }

    # Aggregate by channel
    #
    # The agent aggregate is None-guarded (the FILTER clause): `agent_id` is
    # nullable on agent_messages and really is NULL in production — the retired
    # Slack reconcile (`_rebuild_state_from_slack`, removed 2026-09-29) recorded
    # a real Slack message whose sender maps to no known bot as
    # `is_bot=True, agent_id=NULL`. This set is sorted() in the template
    # (activity_detail.html), so a single None took the whole page down with
    # "'<' not supported between instances of 'NoneType' and 'str'" — the same
    # bug class fixed for /admin/discussions in 73a78c3.
    channel_stats: dict[str, dict[str, Any]] = {
        name: {"count": n, "agents": set(agents or [])}
        for name, n, agents in (await db.execute(
            select(
                AgentMessage.channel_name,
                func.count(),
                func.array_agg(func.distinct(AgentMessage.agent_id))
                .filter(AgentMessage.agent_id.isnot(None)),
            )
            .where(AgentMessage.simulation_run_id == run_id)
            .group_by(AgentMessage.channel_name)
            .order_by(func.min(AgentMessage.created_at))
        )).all()
    }

    return {
        "run": run,
        "messages": messages,
        "channels": channels,
        "agent_stats": agent_stats,
        "channel_stats": channel_stats,
        "message_total": message_total,
        "page": page,
        "page_count": page_count,
    }
