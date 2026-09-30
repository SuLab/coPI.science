"""Names every admin submodule shares: the one ``router`` they all register on, the
template environment, and the dependency singletons."""

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from src.database import get_db
from src.dependencies import get_admin_user
from src.models import User
from src.services import display_format as fmt
from src.services.assessment_detail import key_point_sections
from src.services.llm import is_truncated_stop
from src.services.prose_citations import markdown_with_citation_links, plain_with_citation_links

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# `{{ dt | ts }}` — one UTC datetime rendering for the whole admin surface
# (src/services/display_format.timestamp). A filter, not a per-call helper, so
# a template can never accidentally print a raw `datetime.__str__`.
templates.env.filters["ts"] = fmt.timestamp

# "Did this API call stop before it finished?" — registered as a Jinja TEST so
# `admin/llm_calls.html` can `selectattr('stop_reason', 'truncated_stop')` and
# reach the real predicate rather than re-listing its stop reasons.
#
# The template used to test `stop_reason == 'max_tokens'` on its own, which
# rendered a `refusal`-truncated turn as complete on the one page an operator
# opens to audit truncation. `is_truncated_stop` (src/services/llm.py) is the
# single definition — the engine, the specialist floor and the Slack posting
# path all read it — so a third stop reason added there reaches this page for
# free. A test, not a filter or a global: `selectattr` takes a test name.
templates.env.tests["truncated_stop"] = is_truncated_stop

# The key-point sections a stored `key_points` value renders as (current or
# legacy labels, see `key_point_sections`), used by both
# `_assessments_body.html` and `_assessment_detail_body.html`. Registered as a
# Jinja global rather than a context key: the admin assessments handler
# forbids a new one (see the comment on `_assessments_body.html`'s card-list
# block).
templates.env.globals["key_point_sections"] = key_point_sections

# Render-time URL -> "cited paper" rewriting (spec 2026-09-21 §7). Registered
# as globals for the same reason `key_point_sections` is: the admin assessments
# handler allowlists its context keys and forbids a new one, and BOTH routers
# include the same two partials while each `Jinja2Templates` keeps its own
# globals. src/routers/manager.py carries the identical two lines.
templates.env.globals["md_citations"] = markdown_with_citation_links
templates.env.globals["plain_citations"] = plain_with_citation_links

# Valid AgentRegistry.status values (see src/models/agent_registry.py). Admins
# can move an already-approved agent between these from the edit page; the sim
# runs status=='active' agents (others are parked/excluded, reversibly).
VALID_AGENT_STATUSES = ("active", "inactive", "suspended", "pending")

# Module-level dependency singletons, the pattern src/routers/manager.py
# documents: ruff's B008 flags a `Depends(...)` call sitting in an argument
# default, and every such default counts against the src/ lint ratchet
# (scripts/ci.sh's SRC_LINT_MAX). New handlers here take these module-level
# singletons instead of adding to that debt.
_DB = Depends(get_db)
_ADMIN = Depends(get_admin_user)


def _template_context(
    request: Request, current_user: User, active_admin: str = "", **kwargs
) -> dict:
    """Build the template context, surfacing the impersonation banner.

    ``current_user`` here is the *effective* user from `get_admin_user`, which
    is the impersonated user when one admin impersonates another (both
    satisfy `is_admin`, so `get_admin_user` lets it through). Without this,
    every /admin/* page rendered with no banner and no Stop button. Mirrors
    the same pattern in onboarding.py / profile.py / agent_page.py /
    settings.py / manager.py.
    """
    impersonated = getattr(current_user, "_is_impersonated", False)
    real_admin = getattr(current_user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else current_user,
        "impersonation_banner": current_user if impersonated else None,
        "active_page": "admin",
        "active_admin": active_admin,
    }
    ctx.update(kwargs)
    return ctx
