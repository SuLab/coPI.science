"""All review writes. Router-level gate = get_review_user; handlers that are
staff-only or admin-only declare a NARROWER singleton, which is the real gate
for them. Feedback/status writes now ALLOW an impersonated session (F4,
2026-09-10) and attribute the write to the impersonated user, with the real
admin recorded via ``recorded_by``; assign/unassign, suggestion-status and
the project-review and project-assignment writes still refuse impersonation
outright. The landscaping writes (``/landscape/...``) follow the assessment
ones, and also refuse impersonation on the expert judgment; every one keyed by
an opportunity answers 404 unless ``landscape_view.can_view`` admits the user.

Dependencies are module-level singletons (``_DB``/``_REVIEW``/``_STAFF``/
``_ADMIN``) rather than inline ``Depends(...)`` calls in argument defaults,
same reason as ``src/routers/manager.py``: ruff's B008 flags the latter, and
this router would otherwise chip away at a lint ceiling later tasks still
need headroom under.
"""

import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from src.database import get_db
from src.dependencies import (
    get_admin_user,
    get_review_user,
    get_staff_user,
    recorded_by,
    refuse_impersonation,
)
from src.models import AssessmentReview, OpportunityAssessment, PromptChangeSuggestion, User
from src.models.hub_project import HubProject, HubProjectReview
from src.models.landscape import LandscapeDecision, LandscapeOpportunity, LandscapeReview
from src.services import landscape_reviews, landscape_view
from src.services.assessment_reviews import (
    DuplicateFeedbackError,
    assign_reviewer,
    edit_feedback,
    enqueue_pending_analyses,
    record_status_event,
    submit_feedback,
    unassign_reviewer,
)
from src.services.directory import (
    ASSESSMENT_REVIEW_DEFAULT,
    ASSESSMENT_REVIEW_FILTERS,
    ASSESSMENT_SORTS,
)
from src.services.project_reviews import (
    DuplicateProjectReviewError,
    assign_project_reviewer,
    delete_project_review,
    edit_project_review,
    submit_project_review,
    unassign_project_reviewer,
)
from src.services.projects_view import project_list_query, project_list_state
from src.web.flash import flash
from src.web.urls import page_url

#: Mirrors PromptChangeSuggestion.status's docstring (src/models/review.py).
#: Status mutations and suggestion read filters share the persisted values.
_SUGGESTION_STATUSES = frozenset({"open", "dismissed", "implemented"})

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_review_user)])

_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)
_STAFF = Depends(get_staff_user)
_ADMIN = Depends(get_admin_user)


#: Canonical forms post workspace-list/workspace. Retain the old role tokens
#: for already-loaded forms, but return every writer to a canonical page.
_LIST_SURFACES = frozenset({"admin-list", "manager-list", "workspace-list"})


def _list_filter_query(
    run_id: str | None,
    sort: str | None,
    lab: str | None,
    review: str | None = None,
) -> str:
    """The `?run_id=&sort=&lab=&review=` the reader had on the list page, re-emitted.

    Validated exactly the way ``directory.list_assessments`` validates the
    same parameters, and anything that fails is DROPPED rather than 400ing: these
    values are echoed straight into a ``Location`` header, and silently
    dropping a junk one reproduces the list page's own "a stale bookmark
    renders the queue, not an error" behaviour. ``sort`` must name a real
    option, ``run_id`` must be ``"all"`` or parse as a UUID, and ``lab`` is
    opaque (the page drops an unknown lab itself).

    ``review`` (the reviewed/unreviewed sub-tab, spec 2026-09-21 §4) is emitted
    only when it is a recognised value AND not the default: an absent ``review``
    already means ``unreviewed``, so emitting it would only lengthen the
    ``Location`` for no gain — and would rewrite three exact-``Location``
    assertions in tests/integration/test_reviews_router.py that post no
    ``review`` field at all.

    ``urlencode`` — never string concatenation — is what makes CR/LF header
    injection and a smuggled ``&`` impossible here. Returns ``""``, not a bare
    ``"?"``, when nothing survives.
    """
    params: list[tuple[str, str]] = []
    if run_id == "all":
        params.append(("run_id", "all"))
    elif run_id:
        try:
            uuid.UUID(run_id)
        except ValueError:
            pass
        else:
            params.append(("run_id", run_id))
    if sort in ASSESSMENT_SORTS:
        params.append(("sort", sort))
    if lab:
        params.append(("lab", lab))
    if review in ASSESSMENT_REVIEW_FILTERS and review != ASSESSMENT_REVIEW_DEFAULT:
        params.append(("review", review))
    return f"?{urlencode(params)}" if params else ""


def _assessments_redirect(
    surface: str,
    current_user: User,
    assessment_id: uuid.UUID,
    *,
    run_id: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    review: str | None = None,
    assignment: str | None = None,
    anchor_id: uuid.UUID | None = None,
    no_anchor: bool = False,
    request: Request,
) -> RedirectResponse:
    """Accept old surface tokens, return only to named canonical pages."""
    query = _list_filter_query(run_id, sort, lab, review)
    if surface in {"workspace", "workspace-list"} and review == ASSESSMENT_REVIEW_DEFAULT:
        query += ("&" if query else "?") + "review=" + ASSESSMENT_REVIEW_DEFAULT
    if assignment == "mine":
        query += ("&" if query else "?") + "assignment=mine"
    if surface in _LIST_SURFACES:
        fragment = "" if no_anchor else f"#a-{anchor_id or assessment_id}"
        path = page_url(request, "workspace_assessments")
        return RedirectResponse(path + query + fragment, status_code=302)
    path = page_url(request, "workspace_assessment_detail", assessment_id=assessment_id)
    # Detail-only forms preserve canonical return state; old forms have no state.
    return RedirectResponse(path + (query if surface == "workspace" else ""), status_code=302)


async def _return_state(request):
    if request is None:
        return {}
    form = await request.form()
    return {key: _form_str(form, key) for key in ("run_id", "sort", "lab", "review", "assignment")}


async def _load_review(
    db: AsyncSession,
    feedback_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> AssessmentReview:
    """The review row, or 404. ``for_update=True`` locks it for the rest of the
    request's transaction: the edit route uses it so the review bot's predicate
    stamp (review_bot.consumed_at_predicates) waits for the edit to commit and
    then matches nothing, instead of landing between the edit's read and write
    and marking the edited feedback consumed."""
    stmt = select(AssessmentReview).where(AssessmentReview.id == feedback_id)
    if for_update:
        stmt = stmt.with_for_update()
    review = (await db.execute(stmt)).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return review


async def _load_assessment(db: AsyncSession, assessment_id: uuid.UUID) -> OpportunityAssessment:
    assessment = (
        await db.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == assessment_id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return assessment


async def _load_assignee(db: AsyncSession, assignee_user_id: uuid.UUID) -> User:
    """Load and validate a would-be assignee. 400, never a bare lookup
    failure: an unknown id, a PI, or a non-'allowed' account are all request
    errors, not server errors. Mirrors the last-admin guard's allowed-only
    counting rationale (``admin_set_user_role`` in ``routers/admin/users.py``): a denied/pending account is
    not actually reachable to do the review, regardless of its role.
    """
    assignee = (
        await db.execute(select(User).where(User.id == assignee_user_id))
    ).scalar_one_or_none()
    if assignee is None:
        raise HTTPException(status_code=400, detail="unknown user")
    if not ((assignee.is_staff or assignee.is_reviewer) and assignee.access_status == "allowed"):
        raise HTTPException(
            status_code=400,
            detail="assignee must be a staff member or reviewer with allowed access",
        )
    return assignee


def _parse_assignee_id(assignee_user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(assignee_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed assignee id") from exc


def _optional_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        return None


#: Prefix for the per-rubric-dimension score fields the Human-review card
#: posts. One field per dimension, named from the rubric document's own key
#: (`dim_scientific_credibility`), so the form and the validator cannot drift.
_DIM_FIELD_PREFIX = "dim_"


def _parse_dimension_scores(form: FormData) -> dict[str, int]:
    """Pull the `dim_<key>` fields out of a posted form.

    An empty value means "not scored" and is DROPPED, never coerced to 0: the
    rubric scale starts at 1, so a stored 0 is a score nobody gave. A
    non-integer is a 400 rather than a silent skip — recording a review that
    quietly omits what the reviewer typed is worse than refusing it. Unknown
    KEYS are not checked here; `assessment_reviews._validate` owns that, against
    the live document.

    A duplicate `dim_<key>` field (the same name posted twice) quietly keeps
    the LAST value seen, matching plain HTML form semantics — the real
    `<select>`-per-dimension form can never produce one, so this is a
    considered default rather than an oversight.
    """
    scores: dict[str, int] = {}
    for field, raw in form.multi_items():
        if not field.startswith(_DIM_FIELD_PREFIX):
            continue
        # A same-named field can arrive as an UploadFile in a crafted
        # multipart/form-data POST rather than a str. A file part is not a
        # score, so treat it the same as any other malformed value: 400,
        # not an unhandled AttributeError from calling .strip() on it.
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail=f"Malformed dimension score for {field}")
        value = raw.strip()
        if not value:
            continue
        try:
            scores[field[len(_DIM_FIELD_PREFIX) :]] = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Malformed dimension score for {field}"
            ) from exc
    return scores


def _form_str(form: FormData, field: str) -> str | None:
    """One posted field as a plain string, or ``None``.

    A crafted multipart POST can deliver an ``UploadFile`` where a string is
    expected; a file part is not a filter value, so it reads as absent rather
    than raising on ``.strip()`` later. Blank reads as absent too — the list
    forms post ``value=""`` for an unset lab (contract C6).
    """
    raw = form.get(field)
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


@router.post("/assessments/{assessment_id}/feedback")
async def submit_review_feedback(
    assessment_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    assessment = await _load_assessment(db, assessment_id)
    # One `await request.form()` for both jobs: the dimension fields and the
    # five list-page filters ride on the same POST (contract C6). `review` is
    # read through `_form_str` like the other filters rather than declared as a
    # `Form(...)` parameter: a declared field turns a non-string part of that
    # name into a 422, and these filters are display state that must
    # degrade to "no filter" rather than refuse the write the reader came to
    # make. `_list_filter_query` validates the value either way.
    form = await request.form()
    dimension_scores = _parse_dimension_scores(form)
    duplicate = False
    try:
        await submit_feedback(
            db,
            assessment=assessment,
            reviewer=current_user,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
            dimension_scores=dimension_scores,
            recorded_by=recorded_by(current_user),
        )
    except DuplicateFeedbackError:
        duplicate = True
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if duplicate:
        # A double submit lands where the first did, with a note instead of a
        # second row (B-04). Not an error page: the browser shows THIS response.
        flash(
            request,
            "That feedback was already recorded a moment ago; it was not saved twice.",
            "info",
        )
    else:
        await db.commit()
        logger.info(
            "Review feedback by %s (%s) on assessment %s: score=%s mode=%s dims=%d",
            current_user.name,
            current_user.id,
            assessment_id,
            score,
            feedback_mode,
            len(dimension_scores),
        )
    # B-03: scoring from the Unreviewed tab moves the card to Reviewed, so its own
    # anchor is gone from the page the reader lands on. The list form posts
    # `next_id` (the following card, "" on the last); land there instead. A post
    # without the field (the detail page, older pages) keeps the old anchor.
    next_raw = form.get("next_id")
    moved_out = (
        surface in _LIST_SURFACES
        and isinstance(next_raw, str)
        and (_form_str(form, "review") or ASSESSMENT_REVIEW_DEFAULT) == ASSESSMENT_REVIEW_DEFAULT
    )
    anchor_id = _optional_uuid(next_raw) if moved_out else None
    # A refused duplicate (Task 2A-12) already flashed its own note; one message only
    # (plan audit Q2-17).
    if moved_out and not duplicate:
        flash(request, "Moved to Reviewed.", "success")
    return _assessments_redirect(
        surface,
        current_user,
        assessment_id,
        run_id=_form_str(form, "run_id"),
        sort=_form_str(form, "sort"),
        lab=_form_str(form, "lab"),
        review=_form_str(form, "review"),
        assignment=_form_str(form, "assignment"),
        request=request,
        anchor_id=anchor_id,
        no_anchor=moved_out and anchor_id is None,
    )


@router.post("/feedback/{feedback_id}/edit")
async def edit_review_feedback(
    feedback_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    review = await _load_review(db, feedback_id, for_update=True)
    if review.reviewer_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author may edit this feedback")
    # The form re-posts every dimension on each edit, so parsing unconditionally
    # and always passing the result through (even `{}`) is deliberate: a
    # dimension the reviewer cleared must actually clear on the row, not be
    # left at its previous value. See edit_feedback's own docstring.
    dimension_scores = _parse_dimension_scores(await request.form())
    try:
        await edit_feedback(
            db,
            review=review,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
            dimension_scores=dimension_scores,
            recorded_by=recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review feedback %s edited by %s (%s): score=%s mode=%s dims=%d",
        feedback_id,
        current_user.name,
        current_user.id,
        score,
        feedback_mode,
        len(dimension_scores),
    )
    return _assessments_redirect(
        surface,
        current_user,
        review.assessment_id,
        request=request,
        **(await _return_state(request)),
    )


@router.post("/feedback/{feedback_id}/delete")
async def delete_review_feedback(
    feedback_id: uuid.UUID,
    request: Request,
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    review = await _load_review(db, feedback_id)
    assessment_id = review.assessment_id
    recorder = recorded_by(current_user)
    await db.delete(review)
    await db.commit()
    logger.info(
        "Review feedback %s deleted by %s (%s) recorded_by=%s",
        feedback_id,
        current_user.name,
        current_user.id,
        getattr(recorder, "id", None),
    )
    return _assessments_redirect(
        surface, current_user, assessment_id, request=request, **(await _return_state(request))
    )


@router.post("/assessments/{assessment_id}/status")
async def set_review_status(
    assessment_id: uuid.UUID,
    request: Request = None,
    action: str = Form(...),
    surface: str = Form("manager"),
    # Preserve the existing declared filters; the optional workflow state is
    # read from the raw form so malformed display state does not refuse a write.
    run_id: str | None = Form(None),
    sort: str | None = Form(None),
    lab: str | None = Form(None),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    assessment = await _load_assessment(db, assessment_id)
    try:
        await record_status_event(
            db,
            assessment=assessment,
            actor=current_user,
            action=action,
            recorded_by=recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review status %s recorded by %s (%s) on assessment %s",
        action,
        current_user.name,
        current_user.id,
        assessment_id,
    )
    state = await _return_state(request)
    return _assessments_redirect(
        surface,
        current_user,
        assessment_id,
        run_id=run_id,
        sort=sort,
        lab=lab,
        review=state.get("review"),
        assignment=state.get("assignment"),
        request=request,
    )


@router.post("/assessments/{assessment_id}/assign")
async def assign_review(
    assessment_id: uuid.UUID,
    request: Request = None,
    assignee_user_id: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    refuse_impersonation(current_user)
    assessment = await _load_assessment(db, assessment_id)
    assignee_id = _parse_assignee_id(assignee_user_id)
    assignee = await _load_assignee(db, assignee_id)
    await assign_reviewer(db, assessment=assessment, assignee=assignee, assigned_by=current_user)
    await db.commit()
    logger.info(
        "Assessment %s assigned to %s (%s) by %s (%s)",
        assessment_id,
        assignee.name,
        assignee.id,
        current_user.name,
        current_user.id,
    )
    return _assessments_redirect(
        surface, current_user, assessment_id, request=request, **(await _return_state(request))
    )


@router.post("/assessments/{assessment_id}/unassign")
async def unassign_review(
    assessment_id: uuid.UUID,
    request: Request = None,
    assignee_user_id: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    refuse_impersonation(current_user)
    assessment = await _load_assessment(db, assessment_id)
    assignee_id = _parse_assignee_id(assignee_user_id)
    await unassign_reviewer(db, assessment=assessment, assignee_user_id=assignee_id)
    await db.commit()
    logger.info(
        "Assessment %s unassigned from user %s by %s (%s)",
        assessment_id,
        assignee_id,
        current_user.name,
        current_user.id,
    )
    return _assessments_redirect(
        surface, current_user, assessment_id, request=request, **(await _return_state(request))
    )


def _parse_optional_assessment_id(assessment_id: str) -> uuid.UUID | None:
    """Blank means "every eligible assessment"; anything unparsable is a 400.

    Same posture as ``_parse_assignee_id``: a malformed id is a request error,
    not something to silently widen into a run-wide batch of Opus calls.
    """
    if not assessment_id.strip():
        return None
    try:
        return uuid.UUID(assessment_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed assessment id") from exc


@router.post("/suggestions/generate")
async def generate_prompt_suggestions(
    request: Request,
    assessment_id: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Enqueue the review-bot analysis jobs BY HAND (F2, 2026-09-14).

    Neither ``submit_feedback`` nor ``edit_feedback`` enqueues any more — a
    suggestion is a 70-90k-token Opus call and is now spent only when a human
    asks for one. ``_STAFF``, deliberately NOT ``_REVIEW``: a reviewer cannot
    see ``/workspace/prompt-suggestions`` (a suggestion can quote an unpublished
    PI disclosure verbatim), so a reviewer must not be able to create one
    either. Impersonation is refused for the same reason ``assign``/
    ``unassign``/suggestion-status refuse it — this spends real money — even
    though the review WRITES deliberately allow it.

    Pressing the button twice in SEQUENCE is safe: ``enqueue_pending_analyses`` goes
    through ``enqueue_analysis_if_absent``, so the second press enqueues
    nothing and the redirect's ``generated=0&eligible=N`` says so out loud
    rather than silently.
    """
    refuse_impersonation(current_user)
    scope = _parse_optional_assessment_id(assessment_id)
    enqueued, eligible = await enqueue_pending_analyses(
        db, requested_by=current_user, assessment_id=scope
    )
    await db.commit()
    logger.info(
        "Prompt-suggestion generation requested by %s (%s): enqueued=%d eligible=%d scope=%s",
        current_user.name,
        current_user.id,
        enqueued,
        eligible,
        scope,
    )
    return RedirectResponse(
        url=page_url(
            request,
            "workspace_prompt_suggestions",
            query={"generated": enqueued, "eligible": eligible},
        ),
        status_code=302,
    )


async def _load_suggestion(db: AsyncSession, suggestion_id: uuid.UUID) -> PromptChangeSuggestion:
    suggestion = (
        await db.execute(
            select(PromptChangeSuggestion).where(PromptChangeSuggestion.id == suggestion_id)
        )
    ).scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return suggestion


@router.post("/suggestions/{suggestion_id}/status")
async def set_suggestion_status(
    suggestion_id: uuid.UUID,
    request: Request,
    action: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Staff-set attribution only — never auto-applied, never touches
    the prompt files themselves. Return to the named shared detail page."""
    refuse_impersonation(current_user)
    if action not in _SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status action")
    suggestion = await _load_suggestion(db, suggestion_id)
    suggestion.status = action
    suggestion.status_set_by_user_id = current_user.id
    suggestion.status_set_by_name = current_user.name
    suggestion.status_set_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "Prompt suggestion %s status set to %s by %s (%s)",
        suggestion_id,
        action,
        current_user.name,
        current_user.id,
    )
    return RedirectResponse(
        url=page_url(request, "workspace_prompt_suggestion_detail", suggestion_id=suggestion_id),
        status_code=302,
    )


#: The project review form's fund radio values (spec §5.11: fund or don't fund).
_PROJECT_FUND_VALUES = {"fund": True, "no_fund": False}


def _parse_fund(fund: str) -> bool:
    if fund not in _PROJECT_FUND_VALUES:
        raise HTTPException(status_code=400, detail="fund must be 'fund' or 'no_fund'")
    return _PROJECT_FUND_VALUES[fund]


async def _load_project(db: AsyncSession, project_id: uuid.UUID) -> HubProject:
    project = await db.get(HubProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _load_project_review(db: AsyncSession, review_id: uuid.UUID) -> HubProjectReview:
    """The review row locked for the rest of the request's transaction, or 404."""
    review = (
        await db.execute(
            select(HubProjectReview).where(HubProjectReview.id == review_id).with_for_update()
        )
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


#: Where a project review form came from (spec §6.1, plan contract C6). A post with
#: neither (a tab opened before the field existed) returns as it always did.
_PROJECTS_LIST_SURFACE = "projects-list"
_PROJECT_DETAIL_SURFACE = "project-detail"


def _project_form_state(form: FormData) -> dict[str, str]:
    """The list state the form carried, validated by the list's own rules.

    ``project_list_state`` drops a junk or default value instead of echoing it, and
    ``project_list_query`` urlencodes what survives, so nothing posted here reaches the
    ``Location`` header raw (no CR/LF injection, no smuggled ``&``).
    """
    return project_list_state(
        run_id=_form_str(form, "run_id"),
        lab=_form_str(form, "lab"),
        outcome=_form_str(form, "outcome"),
        review=_form_str(form, "review"),
        sort=_form_str(form, "sort"),
        mine=_form_str(form, "mine"),
        assignment=_form_str(form, "assignment"),
    )


def _project_redirect(
    request: Request,
    project_id: uuid.UUID,
    *,
    surface: str | None = None,
    state: dict[str, str] | None = None,
) -> RedirectResponse:
    """The details page's Reviews card; with ``surface=project-detail``, keeping the state."""
    query = project_list_query(state or {}) if surface == _PROJECT_DETAIL_SURFACE else ""
    path = page_url(request, "workspace_project_detail", project_id=project_id)
    return RedirectResponse(path + query + "#reviews", status_code=303)


def _projects_list_redirect(
    request: Request, state: dict[str, str], fragment_id: uuid.UUID | None
) -> RedirectResponse:
    """The list with its state, at ``#p-<fragment_id>`` (no fragment when None)."""
    fragment = f"#p-{fragment_id}" if fragment_id is not None else ""
    return RedirectResponse(
        page_url(request, "workspace_projects") + project_list_query(state) + fragment,
        status_code=303,
    )


# Project reviews (spec §5.11). Unlike assessment feedback, every write refuses an
# impersonated session, and the pages show no form then. The handlers are not named
# like the services they call: a same-named handler would rebind the import.


@router.post("/projects/{project_id}/review")
async def project_review_submit(
    project_id: uuid.UUID,
    request: Request,
    rating: int = Form(...),
    comment: str = Form(""),
    fund: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """Save one review; return to where the form was (contract C6).

    From the list (``surface=projects-list``) the reader lands on the card's anchor, or on
    ``next_id`` when the reviewed card has left the list: the Unreviewed tab (no ``review``
    in the state) or "Not reviewed by me" (``mine=1``). A duplicate lands the same way with
    only its own note.
    """
    refuse_impersonation(current_user)
    fund_value = _parse_fund(fund)
    project = await _load_project(db, project_id)
    form = await request.form()
    surface = _form_str(form, "surface")
    state = _project_form_state(form)
    from_list = surface == _PROJECTS_LIST_SURFACE
    unreviewed_tab = from_list and "review" not in state
    leaves_list = unreviewed_tab or (from_list and state.get("mine") == "1")
    duplicate = False
    try:
        await submit_project_review(
            db,
            project=project,
            reviewer=current_user,
            rating=rating,
            comment=comment,
            fund=fund_value,
        )
    except DuplicateProjectReviewError:
        duplicate = True
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if duplicate:
        flash(
            request,
            "You have already reviewed this project; edit your review on its page."
            if from_list
            else "You have already reviewed this project; edit your review below.",
            "info",
        )
    else:
        await db.commit()
        logger.info(
            "Project review by %s (%s) on project %s: rating=%s fund=%s",
            current_user.name,
            current_user.id,
            project_id,
            rating,
            fund_value,
        )
        flash(
            request,
            "Review saved; moved to Reviewed." if unreviewed_tab else "Review saved.",
            "success",
        )
    if from_list:
        # next_id is "" on the last card; only a UUID is echoed into the fragment.
        next_raw = _form_str(form, "next_id")
        next_id = _optional_uuid(next_raw) if next_raw else None
        return _projects_list_redirect(request, state, next_id if leaves_list else project_id)
    return _project_redirect(request, project_id, surface=surface, state=state)


@router.post("/project-reviews/{review_id}/edit")
async def project_review_edit(
    review_id: uuid.UUID,
    request: Request,
    rating: int = Form(...),
    comment: str = Form(""),
    fund: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    refuse_impersonation(current_user)
    fund_value = _parse_fund(fund)
    review = await _load_project_review(db, review_id)
    if review.reviewer_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author may edit this review")
    try:
        await edit_project_review(
            db, review=review, rating=rating, comment=comment, fund=fund_value
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Project review %s edited by %s (%s): rating=%s fund=%s",
        review_id,
        current_user.name,
        current_user.id,
        rating,
        fund_value,
    )
    flash(request, "Review updated.", "success")
    form = await request.form()
    return _project_redirect(
        request,
        review.project_id,
        surface=_form_str(form, "surface"),
        state=_project_form_state(form),
    )


@router.post("/project-reviews/{review_id}/delete")
async def project_review_delete(
    review_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    refuse_impersonation(current_user)
    review = await _load_project_review(db, review_id)
    project_id = review.project_id
    await delete_project_review(db, review=review)
    await db.commit()
    logger.info(
        "Project review %s deleted by %s (%s)", review_id, current_user.name, current_user.id
    )
    flash(request, "Review deleted.", "success")
    form = await request.form()
    return _project_redirect(
        request, project_id, surface=_form_str(form, "surface"), state=_project_form_state(form)
    )


# Project review assignments (spec §7.2, contract C7). Staff only, never under
# impersonation, and only from the details page: both return to its Reviews card with
# the list state.


@router.post("/projects/{project_id}/assign")
async def project_assign(
    project_id: uuid.UUID,
    request: Request,
    assignee_user_id: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    refuse_impersonation(current_user)
    project = await _load_project(db, project_id)
    assignee = await _load_assignee(db, _parse_assignee_id(assignee_user_id))
    await assign_project_reviewer(
        db, project=project, assignee=assignee, assigned_by=current_user
    )
    await db.commit()
    logger.info(
        "Project %s assigned to %s (%s) by %s (%s)",
        project_id,
        assignee.name,
        assignee.id,
        current_user.name,
        current_user.id,
    )
    return _project_redirect(
        request,
        project_id,
        surface=_PROJECT_DETAIL_SURFACE,
        state=_project_form_state(await request.form()),
    )


@router.post("/projects/{project_id}/unassign")
async def project_unassign(
    project_id: uuid.UUID,
    request: Request,
    assignee_user_id: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    refuse_impersonation(current_user)
    project = await _load_project(db, project_id)
    assignee_id = _parse_assignee_id(assignee_user_id)
    await unassign_project_reviewer(db, project=project, assignee_user_id=assignee_id)
    await db.commit()
    logger.info(
        "Project %s unassigned from user %s by %s (%s)",
        project_id,
        assignee_id,
        current_user.name,
        current_user.id,
    )
    return _project_redirect(
        request,
        project_id,
        surface=_PROJECT_DETAIL_SURFACE,
        state=_project_form_state(await request.form()),
    )


# Landscaping decisions and reviews (spec §13.3, §10.6). Keyed on the stable opportunity;
# recorded, never learned from (D21). Every route keyed by an opportunity id answers 404 when
# the user may not see it (D50), so a reviewer cannot write outside the issued batches and their
# own expert sample. Each returns to the landscaping detail page.

#: The quick decision's multi-valued tag field.
_REASON_TAGS_FIELD = "reason_tags"
#: Prefixes of the expert-judgment form's per-gate and per-deal-breaker fields; the dimension
#: scores use ``dim_<key>`` as the review form does.
_GATE_FIELD_PREFIX = "gate_"
_DEAL_BREAKER_FIELD_PREFIX = "deal_breaker_"


def _landscape_redirect(opportunity_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(f"/workspace/landscape/{opportunity_id}", status_code=303)


async def _require_viewable(db: AsyncSession, user: User, opportunity_id: uuid.UUID) -> None:
    """404 unless the opportunity exists and ``user`` may see it (D50, D63)."""
    if await db.get(LandscapeOpportunity, opportunity_id) is None or not (
        await landscape_view.can_view(db, user, opportunity_id)
    ):
        raise HTTPException(status_code=404, detail="Opportunity not found")


async def _shown_assessment(
    db: AsyncSession, form: FormData, opportunity_id: uuid.UUID
) -> uuid.UUID | None:
    """The assessment the form's page showed (its ``assessment_id`` field), or the current one."""
    raw = _form_str(form, "assessment_id")
    posted = _optional_uuid(raw) if raw else None
    return await landscape_reviews.shown_assessment_id(db, opportunity_id, posted)


def _reason_tags(form: FormData) -> list[str]:
    tags = []
    for raw in form.getlist(_REASON_TAGS_FIELD):
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="Malformed reason tag")
        if raw.strip():
            tags.append(raw.strip())
    return tags


def _prefixed_choices(form: FormData, prefix: str) -> dict[str, str]:
    """The non-blank ``<prefix><key>`` fields as ``{key: value}``; values are checked by the
    service. A file part is a 400, as in ``_parse_dimension_scores``."""
    calls: dict[str, str] = {}
    for field, raw in form.multi_items():
        if not field.startswith(prefix):
            continue
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail=f"Malformed value for {field}")
        if raw.strip():
            calls[field[len(prefix) :]] = raw.strip()
    return calls


async def _load_landscape_decision(db: AsyncSession, decision_id: uuid.UUID) -> LandscapeDecision:
    row = await db.scalar(
        select(LandscapeDecision).where(LandscapeDecision.id == decision_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return row


async def _load_landscape_review(db: AsyncSession, review_id: uuid.UUID) -> LandscapeReview:
    row = await db.scalar(
        select(LandscapeReview).where(LandscapeReview.id == review_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return row


@router.post("/landscape/{opportunity_id}/decision")
async def landscape_decision_submit(
    opportunity_id: uuid.UUID,
    request: Request,
    decision: str = Form(...),
    reason: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """The quick decision (L39): ``decision`` pursue|pass, repeated ``reason_tags``, ``reason``,
    and the shown ``assessment_id``. A second submit by the same reviewer writes nothing."""
    await _require_viewable(db, current_user, opportunity_id)
    form = await request.form()
    try:
        await landscape_reviews.submit_decision(
            db,
            opportunity_id=opportunity_id,
            reviewer=current_user,
            decision=decision,
            reason_tags=_reason_tags(form),
            reason=reason,
            landscape_assessment_id=await _shown_assessment(db, form, opportunity_id),
            recorded_by=recorded_by(current_user),
        )
    except landscape_reviews.DuplicateLandscapeDecisionError:
        flash(request, "You have already decided on this opportunity; edit your decision.", "info")
        return _landscape_redirect(opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Landscape decision by %s (%s) on opportunity %s: %s",
        current_user.name, current_user.id, opportunity_id, decision,
    )
    flash(request, "Decision saved.", "success")
    return _landscape_redirect(opportunity_id)


@router.post("/landscape-decisions/{decision_id}/edit")
async def landscape_decision_edit(
    decision_id: uuid.UUID,
    request: Request,
    decision: str = Form(...),
    reason: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    row = await _load_landscape_decision(db, decision_id)
    if row.reviewer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author may edit this decision")
    form = await request.form()
    try:
        await landscape_reviews.edit_decision(
            db,
            row=row,
            decision=decision,
            reason_tags=_reason_tags(form),
            reason=reason,
            landscape_assessment_id=await _shown_assessment(db, form, row.opportunity_id),
            recorded_by=recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    opportunity_id = row.opportunity_id
    await db.commit()
    logger.info(
        "Landscape decision %s edited by %s (%s): %s",
        decision_id, current_user.name, current_user.id, decision,
    )
    flash(request, "Decision updated.", "success")
    return _landscape_redirect(opportunity_id)


@router.post("/landscape/{opportunity_id}/review")
async def landscape_review_submit(
    opportunity_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """The optional full review: ``score``, ``comment``, ``dim_<key>`` and the shown
    ``assessment_id``. A second submit by the same reviewer writes nothing."""
    await _require_viewable(db, current_user, opportunity_id)
    form = await request.form()
    dimension_scores = _parse_dimension_scores(form)
    try:
        await landscape_reviews.submit_review(
            db,
            opportunity_id=opportunity_id,
            reviewer=current_user,
            score=score,
            comment=comment,
            dimension_scores=dimension_scores,
            landscape_assessment_id=await _shown_assessment(db, form, opportunity_id),
            recorded_by=recorded_by(current_user),
        )
    except landscape_reviews.DuplicateLandscapeReviewError:
        flash(request, "You have already reviewed this opportunity; edit your review.", "info")
        return _landscape_redirect(opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Landscape review by %s (%s) on opportunity %s: score=%s dims=%d",
        current_user.name, current_user.id, opportunity_id, score, len(dimension_scores),
    )
    flash(request, "Review saved.", "success")
    return _landscape_redirect(opportunity_id)


@router.post("/landscape-reviews/{review_id}/edit")
async def landscape_review_edit(
    review_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    review = await _load_landscape_review(db, review_id)
    if review.reviewer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author may edit this review")
    form = await request.form()
    # Always passed through, even `{}`: the form re-posts every dimension, so a cleared one
    # must clear on the row.
    dimension_scores = _parse_dimension_scores(form)
    try:
        await landscape_reviews.edit_review(
            db,
            review=review,
            score=score,
            comment=comment,
            dimension_scores=dimension_scores,
            landscape_assessment_id=await _shown_assessment(db, form, review.opportunity_id),
            recorded_by=recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    opportunity_id = review.opportunity_id
    await db.commit()
    logger.info(
        "Landscape review %s edited by %s (%s): score=%s dims=%d",
        review_id, current_user.name, current_user.id, score, len(dimension_scores),
    )
    flash(request, "Review updated.", "success")
    return _landscape_redirect(opportunity_id)


@router.post("/landscape-reviews/{review_id}/delete")
async def landscape_review_delete(
    review_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    review = await _load_landscape_review(db, review_id)
    opportunity_id = review.opportunity_id
    recorder = recorded_by(current_user)
    await db.delete(review)
    await db.commit()
    logger.info(
        "Landscape review %s deleted by %s (%s) recorded_by=%s",
        review_id, current_user.name, current_user.id, getattr(recorder, "id", None),
    )
    flash(request, "Review deleted.", "success")
    return _landscape_redirect(opportunity_id)


@router.post("/landscape/{opportunity_id}/status")
async def landscape_status(
    opportunity_id: uuid.UUID,
    request: Request,
    action: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """``action`` approved|disapproved|cleared, with the shown ``assessment_id``."""
    await _require_viewable(db, current_user, opportunity_id)
    form = await request.form()
    try:
        await landscape_reviews.record_status_event(
            db,
            opportunity_id=opportunity_id,
            actor=current_user,
            action=action,
            landscape_assessment_id=await _shown_assessment(db, form, opportunity_id),
            recorded_by=recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Landscape status %s recorded by %s (%s) on opportunity %s",
        action, current_user.name, current_user.id, opportunity_id,
    )
    return _landscape_redirect(opportunity_id)


@router.post("/landscape/{opportunity_id}/assign")
async def landscape_assign(
    opportunity_id: uuid.UUID,
    assignee_user_id: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    refuse_impersonation(current_user)
    await _require_viewable(db, current_user, opportunity_id)
    assignee = await _load_assignee(db, _parse_assignee_id(assignee_user_id))
    await landscape_reviews.assign_reviewer(
        db, opportunity_id=opportunity_id, assignee=assignee, assigned_by=current_user
    )
    await db.commit()
    logger.info(
        "Landscape opportunity %s assigned to %s (%s) by %s (%s)",
        opportunity_id, assignee.name, assignee.id, current_user.name, current_user.id,
    )
    return _landscape_redirect(opportunity_id)


@router.post("/landscape/{opportunity_id}/unassign")
async def landscape_unassign(
    opportunity_id: uuid.UUID,
    assignee_user_id: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    refuse_impersonation(current_user)
    await _require_viewable(db, current_user, opportunity_id)
    assignee_id = _parse_assignee_id(assignee_user_id)
    await landscape_reviews.unassign_reviewer(
        db, opportunity_id=opportunity_id, assignee_user_id=assignee_id
    )
    await db.commit()
    logger.info(
        "Landscape opportunity %s unassigned from user %s by %s (%s)",
        opportunity_id, assignee_id, current_user.name, current_user.id,
    )
    return _landscape_redirect(opportunity_id)


@router.post("/landscape/{opportunity_id}/expert-judgment")
async def landscape_expert_judgment(
    opportunity_id: uuid.UUID,
    request: Request,
    recommendation: str = Form(""),
    comment: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """A marked expert's own calls on an item of their sample (L55, D63): ``gate_<key>``,
    ``dim_<key>``, ``deal_breaker_<key>``, ``recommendation`` and ``comment``.

    Refuses impersonation: the judgment measures the expert's own calls and its table has no
    ``recorded_by`` column. An item outside the user's own sample, or a user who is not a marked
    expert, is a 404 like an unviewable opportunity.
    """
    refuse_impersonation(current_user)
    await _require_viewable(db, current_user, opportunity_id)
    if not await landscape_reviews.is_marked_expert(db, current_user):
        raise HTTPException(status_code=404, detail="Not in your expert sample")
    form = await request.form()
    try:
        await landscape_reviews.submit_expert_judgment(
            db,
            opportunity_id=opportunity_id,
            expert=current_user,
            gates=_prefixed_choices(form, _GATE_FIELD_PREFIX),
            scores=_parse_dimension_scores(form),
            deal_breakers=_prefixed_choices(form, _DEAL_BREAKER_FIELD_PREFIX),
            recommendation=recommendation.strip() or None,
            comment=comment,
        )
    except landscape_reviews.NotInExpertSample as exc:
        raise HTTPException(status_code=404, detail="Not in your expert sample") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Landscape expert judgment by %s (%s) on opportunity %s",
        current_user.name, current_user.id, opportunity_id,
    )
    flash(request, "Judgment submitted.", "success")
    return _landscape_redirect(opportunity_id)
