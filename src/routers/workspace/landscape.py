"""The Landscape pages (spec §13.1, §13.2, §13.4): the batch and staff lists, an opportunity's
detail page and the staff pipeline page.

GET only. The forms post to ``/reviews/landscape...`` (``src/routers/reviews.py``) and the
pipeline controls to ``/landscape/...`` (``src/routers/landscape.py``). Every view is a plain
dict from ``src/services/landscape_view.py``, which applies the hiding rules (blind batches,
expert samples, independent votes) itself; nothing here re-derives them.

Every page answers 503 until migration 0077 has run (spec §12.5). The check runs once per
process: once the schema is there it stays there, so only the True answer is cached.

The list's state (``view``, ``tab``, ``assignment``, ``sort``, ``lab``, ``cards``) travels to
the detail page and back through the ``/reviews`` redirects (contract C9 of
``docs/plans/2026-11-19-landscape-ui-remediation-plan.md``); ``list_state`` is the one validator
both routers use. ``cards=compact`` is the comparison page (D95 of
``docs/specs/2026-10-30-blackbird-landscaping-design.md``): the same lists with the shorter
card of the remediation design's option A.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user, get_staff_user
from src.landscaping.criteria import load_criteria
from src.landscaping.schema_check import schema_ready
from src.models import User
from src.services import landscape_view
from src.services.blackbird_rubric import BANDING, load_rubric
from src.services.landscape_reviews import EXPERT_RECOMMENDATIONS
from src.services.web_permissions import is_staff
from src.web.identity import identity_for
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)
_STAFF = Depends(get_staff_user)

#: Set once ``schema_ready`` has answered True in this process.
_SCHEMA = {"ready": False}
_NOT_MIGRATED = "The Landscape pages are not available yet: the database has not been migrated."


async def _require_schema(db: AsyncSession) -> None:
    """503 while migration 0077 has not run (spec §12.5)."""
    if _SCHEMA["ready"]:
        return
    if not await schema_ready(db):
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED)
    _SCHEMA["ready"] = True


def _chat_enabled(request: Request) -> bool:
    """Both switches on: the assessment chat's and the landscaping chat's (spec §13.5)."""
    state = request.app.state
    return (
        getattr(state, "assessment_chat_enabled", False) is True
        and getattr(state, "landscape_chat_enabled", False) is True
    )


#: The list state's allowed values (C9, C14); ``workspace_landscape`` reads the same sets.
LIST_VIEWS = ("current", "all")
LIST_TABS = ("unreviewed", "reviewed", "all")
LIST_ASSIGNMENTS = ("all", "mine")
LIST_SORTS = ("pipeline", "score", "recommendation")
LIST_CARDS = ("full", "compact")
_LAB_MAX = 200


def list_state(
    view: str | None,
    tab: str | None,
    assignment: str | None,
    sort: str | None,
    lab: str | None,
    cards: str | None = None,
) -> dict[str, str]:
    """The list state worth carrying, as ``{view, tab, assignment, sort, lab, cards}`` in that
    order with only the surviving keys.

    A value outside its allowlist is dropped, never echoed: the result reaches ``Location``
    headers and links, so a junk bookmark degrades to the default list instead of an error.
    ``lab`` is opaque (any non-blank string up to 200 characters; ``lists_view`` matches it
    against the cards' labs). The defaults ``assignment=all``, ``sort=pipeline`` and
    ``cards=full`` are dropped; ``tab`` is kept even when it is ``unreviewed``, so a return URL
    names the tab the reader was on.
    """
    state: dict[str, str] = {}
    if view in LIST_VIEWS:
        state["view"] = view
    if tab in LIST_TABS:
        state["tab"] = tab
    if assignment in LIST_ASSIGNMENTS and assignment != "all":
        state["assignment"] = assignment
    if sort in LIST_SORTS and sort != "pipeline":
        state["sort"] = sort
    lab = lab.strip() if isinstance(lab, str) else ""
    if lab and len(lab) <= _LAB_MAX:
        state["lab"] = lab
    if cards in LIST_CARDS and cards != "full":
        state["cards"] = cards
    return state


def _reason_tags() -> list[str]:
    """The quick decision's tag choices, from the criteria file (L49)."""
    return list(load_criteria().reason_tags)


@router.get("/landscape", response_class=HTMLResponse, name="workspace_landscape")
async def workspace_landscape(
    request: Request,
    view: str | None = None,
    tab: str | None = None,
    assignment: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    cards: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """This week's batch, carry-overs and earlier batches for everyone; with ``view`` set to
    ``current`` or ``all``, a staff viewer gets that list instead (L48), sorted by ``sort``
    and narrowed to ``lab`` (C14). A reviewer's ``view`` is ignored. A marked expert's open
    sample items show on either (D63). ``cards=compact`` draws the same cards in the compact
    style, for comparison (D95)."""
    await _require_schema(db)
    state = list_state(view, tab, assignment, sort, lab, cards)
    lists = None
    if view in LIST_VIEWS and is_staff(current_user):
        lists = await landscape_view.lists_view(
            db, current_user, view=view, tab=tab or "unreviewed", assignment=assignment or "all",
            sort=state.get("sort", "pipeline"), lab=state.get("lab"),
        )
    batch = None if lists is not None else await landscape_view.batch_view(db, current_user)
    expert = await landscape_view.expert_view(db, current_user)
    return templates.TemplateResponse(
        request,
        "workspace/landscape.html",
        workspace_context(
            request,
            current_user,
            section="landscape",
            selected_view=lists["view"] if lists is not None else "batch",
            batch=batch,
            lists=lists,
            expert=expert,
            reason_tag_choices=_reason_tags(),
            banding=BANDING,
            list_state=state,
        ),
    )


# Registered before the detail route so `/landscape/pipeline` never reaches it (D46).
@router.get(
    "/landscape/pipeline", response_class=HTMLResponse, name="workspace_landscape_pipeline"
)
async def workspace_landscape_pipeline(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    await _require_schema(db)
    try:
        view = await landscape_view.pipeline_view(db, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Managers and admins only") from exc
    return templates.TemplateResponse(
        request,
        "workspace/landscape_pipeline.html",
        workspace_context(request, current_user, section="landscape", **view),
    )


def _form_choices() -> dict:
    """What the review and expert forms offer: the live rubric's dimensions and gates and the
    criteria file's deal-breakers, the sets the POST routes validate against."""
    rubric = load_rubric()
    return {
        "review_dimensions": [{"key": d.key, "title": d.title} for d in rubric.dimensions],
        "review_scale_min": rubric.scale_min,
        "review_scale_max": rubric.scale_max,
        "expert_gates": [
            {"key": key, "title": meta.get("title") or key, "description": meta.get("description")}
            for key, meta in rubric.gating.items()
        ],
        "expert_deal_breakers": [
            {"key": d.key, "description": d.description} for d in load_criteria().deal_breakers
        ],
        "expert_recommendations": list(EXPERT_RECOMMENDATIONS),
    }


@router.get(
    "/landscape/{opportunity_id:uuid}",
    response_class=HTMLResponse,
    name="workspace_landscape_detail",
)
async def workspace_landscape_detail(
    request: Request,
    opportunity_id: uuid.UUID,
    assessment: str | None = None,
    view: str | None = None,
    tab: str | None = None,
    assignment: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    cards: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """The detail page (§13.2); 404 unless ``landscape_view.can_view`` admits the viewer.

    ``assessment`` opens one of the page's listed History rows read-only (C8): a malformed
    id, or one that is not a listed row (a candidate epoch's, another opportunity's), is a
    404. The list state is validated and passed as ``list_state`` for the back link and the
    forms (C9)."""
    await _require_schema(db)
    history_id = None
    if assessment is not None:
        try:
            history_id = uuid.UUID(assessment)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Assessment not found") from exc
    try:
        detail = await landscape_view.detail_view(
            db, current_user, opportunity_id, history_assessment_id=history_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Assessment not found") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    identity = identity_for(current_user)
    chat_enabled = _chat_enabled(request)
    # The chat answers 404 without a current completed assessment in the active epoch, so a
    # card with none, a retired opportunity read as history, or an earlier assessment opened
    # from History gets no drawer.
    chat_available = (
        chat_enabled and not identity.is_impersonating and detail.get("assessment") is not None
        and not detail.get("no_assessment") and not detail.get("is_history")
        and not detail.get("history_view")
    )
    return templates.TemplateResponse(
        request,
        "workspace/landscape_detail.html",
        workspace_context(
            request,
            current_user,
            section="landscape",
            chat_enabled=chat_enabled,
            chat_available=chat_available,
            reason_tag_choices=_reason_tags(),
            banding=BANDING,
            list_state=list_state(view, tab, assignment, sort, lab, cards),
            **_form_choices(),
            **detail,
        ),
    )
