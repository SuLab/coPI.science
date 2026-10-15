"""The Help tab's registry: reference pages, tutorials and screenshots
(spec docs/specs/2026-10-14-help-and-onboarding-design.md §4, §6.1, §8.3).

One registry drives the index, the Help sub-nav and the role gate. Template names are
literals because tests/unit/test_reachability.py counts a template as rendered only when a
string in src/ names it in full.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, status

from src.models.user import USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from src.web.urls import page_url

AUDIENCE_ALL = "all"
AUDIENCE_STAFF = "staff"
#: Segments that fixed /help routes use; no page slug may take one (spec §4.2).
RESERVED_SLUGS = frozenset({"tutorials", "intro", "shots"})
#: Working-directory relative, like templates/ and static/ (spec §8.3).
SHOTS_DIR = Path("help_assets/shots")
MANAGER_ACCESS_REQUIRED = "Manager access required"
_STEP_NUMBER = re.compile(r"[1-9][0-9]{0,2}")


@dataclass(frozen=True)
class HelpPage:
    slug: str
    title: str
    summary: str
    audience: str
    template: str
    #: Section ids the page renders for at least one role and one configuration. Page
    #: Help links and in-content links may target only these.
    anchors: tuple[str, ...]


@dataclass(frozen=True)
class TutorialStep:
    title: str
    template: str
    #: Route NAME (no path parameters) the step's "Open this page" link opens, or None.
    open_route: str | None
    #: Screenshot ids the step template renders with figure(), in order.
    shots: tuple[str, ...]


@dataclass(frozen=True)
class Tutorial:
    key: str
    title: str
    summary: str
    audience: str
    steps: tuple[TutorialStep, ...]


HELP_PAGES: tuple[HelpPage, ...] = (
    HelpPage("getting-started", "Getting started",
             "Signing in, finding your way around, Settings, and what to do when access fails.",
             AUDIENCE_ALL, "help/pages/getting-started.html",
             ("signing-in", "microsoft", "first-sign-in", "navigation", "settings",
              "signing-out", "access-problems")),
    HelpPage("how-it-works", "How Blackbird works",
             "PIs, lab agents, simulation runs, interviews, the specialist panel and assessments.",
             AUDIENCE_ALL, "help/pages/how-it-works.html",
             ("pis-and-lab-agents", "data-sources", "what-agents-see", "simulation-runs",
              "interviews", "specialist-panel", "assessments", "human-review",
              "prompt-suggestions")),
    HelpPage("assessments", "Assessments",
             "Reading the assessment list and detail page, asking the chat, and recording a review.",
             AUDIENCE_ALL, "help/pages/assessments.html",
             ("list", "cards", "detail", "chat", "reviewing", "approval-status", "assigning")),
    HelpPage("pis", "PIs",
             "The PI directory and a PI's page.",
             AUDIENCE_ALL, "help/pages/pis.html",
             ("directory", "detail")),
    HelpPage("managing-pis", "Managing PIs and lab bots",
             "Adding a PI, reviewing their profile, and installing, activating and muting their bot.",
             AUDIENCE_STAFF, "help/pages/managing-pis.html",
             ("add-pi", "profile-status", "account", "edit-profile", "finding-papers",
              "writing-profile", "paper-review", "drafts", "regenerate", "persona", "jobs",
              "slack-bot", "activation", "mute", "slack-bots")),
    HelpPage("pi-evidence", "Grants, industry interest and companies",
             "Checking and correcting the grants, industry evidence and companies on a PI's page.",
             AUDIENCE_STAFF, "help/pages/pi-evidence.html",
             ("reporter-identity", "grants", "orcid-fundings", "grant-lines", "industry",
              "companies")),
    HelpPage("staff-accounts", "Staff accounts",
             "Adding reviewers, and revoking, restoring or deleting their access.",
             AUDIENCE_STAFF, "help/pages/staff-accounts.html",
             ("add", "revoke-restore-delete", "telling-new-staff", "recent-changes")),
    HelpPage("discussions-activity", "Discussions and Activity",
             "What the simulation's threads, outcomes and run statistics show.",
             AUDIENCE_STAFF, "help/pages/discussions-activity.html",
             ("discussions", "activity", "run-detail")),
    HelpPage("prompt-suggestions", "Prompt suggestions",
             "What prompt-change suggestions are, how to generate them and how to mark them.",
             AUDIENCE_STAFF, "help/pages/prompt-suggestions.html",
             ("what-they-are", "generate", "status", "detail")),
    HelpPage("glossary", "Glossary",
             "Every term the pages use, including older names for the same thing.",
             AUDIENCE_ALL, "help/pages/glossary.html", ()),
    HelpPage("troubleshooting", "Troubleshooting",
             "Messages you may see, what they mean and what to do.",
             AUDIENCE_ALL, "help/pages/troubleshooting.html",
             ("sign-in", "access", "assessments", "assessment-chat", "pi-pages", "staff-pages")),
)

_REVIEWER_STEPS = (
    TutorialStep("Welcome", "help/tutorials/reviewer/01-welcome.html", None, ()),
    TutorialStep("Signing in and finding your way",
                 "help/tutorials/reviewer/02-signing-in.html", "workspace_assessments",
                 ("topbar-help", "intro-prompt")),
    TutorialStep("The Assessments list", "help/tutorials/reviewer/03-assessments-list.html",
                 "workspace_assessments", ("assessments-list",)),
    TutorialStep("Reading an assessment card",
                 "help/tutorials/reviewer/04-assessment-card.html", "workspace_assessments",
                 ("assessment-card", "card-rubric-gating")),
    TutorialStep("The assessment detail page",
                 "help/tutorials/reviewer/05-assessment-detail.html", "workspace_assessments",
                 ("detail-brief", "detail-panel-gating", "detail-timeline")),
    TutorialStep("Asking the assessment chat",
                 "help/tutorials/reviewer/06-assessment-chat.html", "workspace_assessments",
                 ("chat-drawer",)),
    TutorialStep("Recording your review", "help/tutorials/reviewer/07-recording-a-review.html",
                 None, ("review-form",)),
    TutorialStep("PI pages, and getting help",
                 "help/tutorials/reviewer/08-pi-pages-and-help.html", "workspace_pis",
                 ("pis-list-reviewer", "pi-detail-reviewer")),
)

_MANAGER_STEPS = (
    TutorialStep("Your role", "help/tutorials/manager/01-your-role.html", None, ()),
    TutorialStep("Signing in and the Workspace", "help/tutorials/manager/02-workspace.html",
                 "workspace_pis", ("topbar-help",)),
    TutorialStep("The PIs list", "help/tutorials/manager/03-pis-list.html", "workspace_pis",
                 ("pis-list-manager",)),
    TutorialStep("A PI's lifecycle", "help/tutorials/manager/04-pi-lifecycle.html",
                 "workspace_pis", ("pi-header-pending",)),
    TutorialStep("Reviewing a PI's profile", "help/tutorials/manager/05-profile-review.html",
                 "workspace_pis", ("pi-account-edit", "pi-paper-review")),
    TutorialStep("Grants, industry interest and companies",
                 "help/tutorials/manager/06-evidence.html", "workspace_pis",
                 ("pi-grants", "pi-industry", "pi-companies")),
    TutorialStep("The Slack Bots page", "help/tutorials/manager/07-slack-bots.html",
                 "workspace_slack_bots", ("slack-bots",)),
    TutorialStep("Assessments as a manager", "help/tutorials/manager/08-assessments.html",
                 "workspace_assessments", ("assign-to",)),
    TutorialStep("Staff accounts", "help/tutorials/manager/09-staff-accounts.html",
                 "workspace_staff", ("staff-form", "staff-preview", "staff-table")),
    TutorialStep("Discussions, Activity and Prompt Suggestions",
                 "help/tutorials/manager/10-discussions-activity-suggestions.html",
                 "workspace_discussions", ("discussions", "activity", "prompt-suggestions")),
)

#: Manager first: staff see both, and the manager tutorial is theirs (spec §4.4).
TUTORIALS: tuple[Tutorial, ...] = (
    Tutorial("manager", "Manager tutorial",
             "A read-only tour of the pages managers use: PIs, lab bots, staff accounts and the "
             "simulation's records.", AUDIENCE_STAFF, _MANAGER_STEPS),
    Tutorial("reviewer", "Reviewer tutorial",
             "A read-only tour of the pages reviewers use: assessments, the chat and recording a "
             "review.", AUDIENCE_ALL, _REVIEWER_STEPS),
)


def _visible(audience: str, permissions) -> bool:
    return audience == AUDIENCE_ALL or bool(permissions.staff)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MANAGER_ACCESS_REQUIRED)


def pages_for(permissions) -> tuple[HelpPage, ...]:
    return tuple(page for page in HELP_PAGES if _visible(page.audience, permissions))


def tutorials_for(permissions) -> tuple[Tutorial, ...]:
    return tuple(t for t in TUTORIALS if _visible(t.audience, permissions))


def lookup_page(slug: str, permissions) -> HelpPage:
    """The page, 404 for an unknown slug, 403 for a staff page and a non-staff viewer."""
    for page in HELP_PAGES:
        if page.slug == slug:
            if not _visible(page.audience, permissions):
                raise _forbidden()
            return page
    raise _not_found()


def lookup_tutorial(key: str, permissions) -> Tutorial:
    for tutorial in TUTORIALS:
        if tutorial.key == key:
            if not _visible(tutorial.audience, permissions):
                raise _forbidden()
            return tutorial
    raise _not_found()


def lookup_step(key: str, step: str, permissions) -> tuple[Tutorial, int, TutorialStep]:
    """``step`` is the raw path segment: only a canonical 1-based number in range is a
    step (no sign, no leading zero, ASCII digits only); anything else is 404, never 422."""
    tutorial = lookup_tutorial(key, permissions)
    if not _STEP_NUMBER.fullmatch(step):
        raise _not_found()
    number = int(step)
    if number > len(tutorial.steps):
        raise _not_found()
    return tutorial, number, tutorial.steps[number - 1]


def tutorial_key_for(user_role: str) -> str | None:
    """The tutorial the first-sign-in prompt offers a role; None means no prompt."""
    return {USER_ROLE_MANAGER: "manager", USER_ROLE_REVIEWER: "reviewer"}.get(user_role)


def navigation_for(request, permissions) -> tuple[dict, ...]:
    """The Help sub-nav: Overview, the role's tutorials, then its pages in registry order."""
    items = [{"section": "overview", "label": "Overview", "url": page_url(request, "help_index")}]
    items += [
        {"section": f"tutorial-{t.key}", "label": t.title,
         "url": page_url(request, "help_tutorial", tutorial=t.key)}
        for t in tutorials_for(permissions)
    ]
    items += [
        {"section": p.slug, "label": p.title,
         "url": page_url(request, "help_page", slug=p.slug)}
        for p in pages_for(permissions)
    ]
    return tuple(items)


@lru_cache(maxsize=1)
def _manifest() -> dict:
    """Loaded on first use, not at import, so a missing file fails only the pages that
    show a screenshot (spec §8.3)."""
    return json.loads((SHOTS_DIR / "manifest.json").read_text(encoding="utf-8"))


def shot_ids() -> frozenset[str]:
    return frozenset(_manifest())


def shot_meta(shot_id: str) -> dict:
    """{audience, alt, width, height, sources, commit}; LookupError for an unknown id."""
    try:
        return _manifest()[shot_id]
    except KeyError:
        raise LookupError(f"unknown help screenshot {shot_id!r}") from None


def shot_path(shot_id: str) -> Path:
    return SHOTS_DIR / f"{shot_id}.png"
