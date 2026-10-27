"""One static navigation inventory, filtered by effective capabilities."""

from dataclasses import dataclass

from src.web.urls import page_url


@dataclass(frozen=True)
class NavigationItem:
    section: str
    label: str
    route_name: str
    staff_only: bool = False
    admin_only: bool = False


WORKSPACE_NAVIGATION = (
    NavigationItem("pis", "PIs", "workspace_pis"),
    NavigationItem("assessments", "Assessments", "workspace_assessments"),
    NavigationItem("projects", "Projects", "workspace_projects"),
    NavigationItem("slack-bots", "Slack Bots", "workspace_slack_bots", True),
    NavigationItem("staff", "Staff", "workspace_staff", True),
    NavigationItem("discussions", "Discussions", "workspace_discussions", True),
    NavigationItem("interviews", "Interviews", "workspace_interviews", True),
    NavigationItem("activity", "Activity", "workspace_activity", True),
    # Admins only for now; managers later (owner, 2026-10-27).
    NavigationItem("simulation", "Simulation", "workspace_simulation", True, admin_only=True),
    NavigationItem(
        "prompt-suggestions", "Prompt Suggestions", "workspace_prompt_suggestions", True
    ),
)


def navigation_for(request, permissions):
    if not permissions.research:
        return ()
    return tuple(
        {"section": item.section, "label": item.label, "url": page_url(request, item.route_name)}
        for item in WORKSPACE_NAVIGATION
        if (permissions.staff or not item.staff_only)
        and (permissions.admin or not item.admin_only)
    )
