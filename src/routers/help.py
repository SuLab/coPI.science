"""The Help tab: role-filtered reference pages, read-only tutorials, the first-sign-in
prompt's answer, and the screenshots they show
(spec docs/specs/2026-10-14-help-and-onboarding-design.md §4, §5.3, §8.4).

Every route requires review access (admin, manager, reviewer); the registry's audience
field adds the manager-only gate. Fixed routes are declared before /{slug}.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user, refuse_impersonation
from src.models.help import VIA_FINISH, VIA_VALUES, HelpIntroDismissal
from src.routers.auth import is_safe_next_url
from src.services.web_permissions import capabilities_for
from src.web import help_pages
from src.web.flash import flash
from src.web.page_context import page_context
from src.web.templating import make_templates
from src.web.urls import page_url

router = APIRouter()
templates = make_templates()
_REVIEW = Depends(get_review_user)
_DB = Depends(get_db)
# Both default to "": an empty or unknown value is refused in the handler (400), never a 422.
_VIA_FORM = Form("")
_NEXT_FORM = Form("", alias="next")


def _context(request: Request, user, *, section: str, help_title: str, **content) -> dict:
    permissions = capabilities_for(user)
    return page_context(
        request, user, active_page="help", section=section,
        help_navigation=help_pages.navigation_for(request, permissions),
        microsoft_sign_in=getattr(request.app.state, "microsoft_sign_in", None) is not None,
        help_title=help_title, **content,
    )


@router.get("", response_class=HTMLResponse, name="help_index")
async def help_index(request: Request, current_user=_REVIEW):
    permissions = capabilities_for(current_user)
    return templates.TemplateResponse(request, "help/index.html", _context(
        request, current_user, section="overview", help_title="Overview",
        pages=help_pages.pages_for(permissions),
        tutorials=help_pages.tutorials_for(permissions)))


@router.get("/tutorials/{tutorial}", name="help_tutorial")
async def help_tutorial(request: Request, tutorial: str, current_user=_REVIEW):
    help_pages.lookup_tutorial(tutorial, capabilities_for(current_user))
    return RedirectResponse(
        page_url(request, "help_tutorial_step", tutorial=tutorial, step=1), status_code=302)


@router.get("/tutorials/{tutorial}/{step}", response_class=HTMLResponse,
            name="help_tutorial_step")
async def help_tutorial_step(request: Request, tutorial: str, step: str, current_user=_REVIEW):
    """``step`` is a str so a non-number is lookup_step's 404, not a validation 422."""
    found, number, entry = help_pages.lookup_step(tutorial, step, capabilities_for(current_user))
    return templates.TemplateResponse(request, entry.template, _context(
        request, current_user, section=f"tutorial-{found.key}", help_title=entry.title,
        tutorial=found, step=entry, step_number=number, step_count=len(found.steps)))


@router.post("/intro/dismiss", name="help_intro_dismiss")
async def help_intro_dismiss(
    request: Request,
    via: str = _VIA_FORM,
    next_url: str = _NEXT_FORM,
    current_user=_REVIEW,
    db: AsyncSession = _DB,
):
    """Record the prompt's answer; the first answer is kept (spec §5.3)."""
    refuse_impersonation(current_user, "Help changes are disabled while impersonating.")
    if via not in VIA_VALUES:
        raise HTTPException(status_code=400, detail="Invalid request")
    await db.execute(
        insert(HelpIntroDismissal).values(user_id=current_user.id, via=via)
        .on_conflict_do_nothing(index_elements=["user_id"]))
    await db.commit()
    if via == VIA_FINISH:
        flash(request, "Tutorial finished.", "success")
        return RedirectResponse(page_url(request, "help_index"), status_code=302)
    flash(request, "The tutorials are always under Help.", "info")
    target = next_url if is_safe_next_url(request, next_url) else page_url(request, "workspace_root")
    return RedirectResponse(target, status_code=302)


@router.get("/shots/{shot_id}.png", name="help_shot")
async def help_shot(shot_id: str, current_user=_REVIEW):
    """A committed screenshot; only manifest ids reach the filesystem (spec §8.4)."""
    if shot_id not in help_pages.shot_ids():
        raise HTTPException(status_code=404, detail="Not Found")
    meta = help_pages.shot_meta(shot_id)
    if meta.get("audience") != help_pages.AUDIENCE_ALL and not capabilities_for(current_user).staff:
        raise HTTPException(status_code=403, detail=help_pages.MANAGER_ACCESS_REQUIRED)
    path = help_pages.shot_path(shot_id)
    if not path.is_file():  # a manifest entry whose PNG did not ship: 404, not a 500
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{slug}", response_class=HTMLResponse, name="help_page")
async def help_page(request: Request, slug: str, current_user=_REVIEW):
    page = help_pages.lookup_page(slug, capabilities_for(current_user))
    return templates.TemplateResponse(request, page.template, _context(
        request, current_user, section=page.slug, help_title=page.title, help_page=page))
