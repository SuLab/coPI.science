"""All review writes. Router-level gate = get_review_user; handlers that are
staff-only or admin-only declare a NARROWER singleton, which is the real gate
for them. Feedback/status writes now ALLOW an impersonated session (F4,
2026-09-10) and attribute the write to the impersonated user, with the real
admin recorded via ``recorded_by``; assign/unassign and suggestion-status
still refuse impersonation outright.

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
