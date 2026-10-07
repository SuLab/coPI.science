"""Relative named routes and feature-specific compatibility query contracts."""

from urllib.parse import urlencode

from fastapi import Request

QUERY_FIELDS = {
    "pis": ("institution_filter", "page", "status_filter", "claimed_filter", "error", "orcid"),
    "pi_detail": ("error", "activated", "activation_blocked", "email_verified"),
    "assessments": ("run_id", "sort", "lab", "review", "assignment"),
    "assessment_detail": ("run_id", "sort", "lab", "review", "assignment"),
    "discussions": ("run_id", "channel_filter", "status_filter", "agent_filter", "page", "export"),
    "activity": (),
    "activity_detail": ("page",),
    "prompt_suggestions": ("status",),
    "prompt_suggestion_detail": (),
    "slack_bots": (),
    "home": (),
}


def page_url(request: Request, name: str, *, query=None, fragment: str = "", **params) -> str:
    path = str(request.app.url_path_for(name, **{k: str(v) for k, v in params.items()}))
    pairs = query.items() if isinstance(query, dict) else (query or ())
    encoded = urlencode([(k, v) for k, v in pairs if v is not None], doseq=True)
    return path + ("?" + encoded if encoded else "") + ("#" + fragment if fragment else "")


def legacy_query(request: Request, feature: str, *, staff: bool, manager: bool = False):
    fields = QUERY_FIELDS[feature]
    if not staff and feature in ("pis", "pi_detail"):
        fields = ("institution_filter", "page") if feature == "pis" else ()
    return [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key in fields and not (manager and key == "export")
    ]
