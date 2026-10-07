"""Request-scoped chrome shared by workspace, administration and PI surfaces."""

from src.services.web_permissions import ROLE_DESCRIPTIONS
from src.web.identity import identity_for
from src.web.navigation import navigation_for


def page_context(request, viewer, *, active_page="workspace", section="", **content):
    identity = identity_for(viewer)
    return {
        "request": request,
        "identity": identity,
        "current_user": identity.actor,
        "effective_user": identity.effective,
        "impersonation_banner": identity.effective if identity.is_impersonating else None,
        "permissions": identity.permissions,
        "workspace_navigation": navigation_for(request, identity.permissions),
        "active_page": active_page,
        "active_section": section,
        "role_descriptions": ROLE_DESCRIPTIONS,
        **content,
    }


def workspace_context(request, user, *, section="", **content):
    from src.web.presentation import project_workspace

    permissions = identity_for(user).permissions
    return page_context(
        request,
        user,
        section=section,
        **project_workspace(section, content, permissions),
    )
