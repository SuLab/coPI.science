"""One static navigation inventory, filtered by effective capabilities."""

from dataclasses import dataclass

from src.web.urls import page_url


@dataclass(frozen=True)
class NavigationItem:
    section: str
    label: str
    route_name: str
    staff_only: bool = False


WORKSPACE_NAVIGATION = (
    NavigationItem("pis", "PIs", "workspace_pis"),
    NavigationItem("assessments", "Assessments", "workspace_assessments"),
    NavigationItem("slack-bots", "Slack Bots", "workspace_slack_bots", True),
    NavigationItem("discussions", "Discussions", "workspace_discussions", True),
    NavigationItem("activity", "Activity", "workspace_activity", True),
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
        if permissions.staff or not item.staff_only
    )
