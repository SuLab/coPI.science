"""The Help screenshot list (spec docs/specs/2026-10-14-help-and-onboarding-design.md §8.2).

Each ``Shot`` names the seeded account it is captured as, the URL over the seed ids
(seed.py's JSON), the Playwright steps that open a disclosure or the chat drawer, an
optional element to clip to, the alt text, and the templates it depicts.

capture.py hashes each shot's static fields plus ``inspect.getsource`` of its ``path``
and ``prepare`` callables into the manifest entry ``shots.py#<id>``. Editing one of those
functions, docstring included, marks every shot that uses it stale. They are named
module-level functions, never lambdas: a lambda's source is the whole statement around
it, so editing one shot would mark its neighbours stale too.

Playwright is imported only for type checking: pyproject.toml keeps it out of every
pytest import, and tests/unit/test_help_shots.py imports this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.e2e.help_shots.seed import CHAT_ANSWER_MARKER, CHAT_QUESTION, STAFF_PREVIEW_ORCID

if TYPE_CHECKING:
    from playwright.async_api import Page

AUDIENCE_ALL = "all"
AUDIENCE_STAFF = "staff"

_ASSESSMENTS = "templates/workspace/assessments.html"
_ASSESSMENTS_BODY = "templates/assessments/_body.html"
_DETAIL = "templates/workspace/assessment_detail.html"
_DETAIL_BODY = "templates/assessments/_detail_body.html"
_PI_DETAIL = "templates/workspace/pi_detail.html"


async def _no_prepare(page: Page) -> None:
    """Nothing to open: the shot is the page as loaded."""


@dataclass(frozen=True)
class Shot:
    id: str
    audience: str
    #: A seed key (seed.ACCOUNT_ROLES): the account the shot is captured as.
    account: str
    #: The page's path and query over the seed ids.
    path: Callable[[dict], str] = field(repr=False, compare=False)
    #: Playwright selector (CSS, or chained with ``>>``) of the element to capture, first
    #: match; None = the viewport only, never the full page.
    clip: str | None
    alt: str
    #: Repo-relative templates the shot depicts; capture.py adds their include closure.
    sources: tuple[str, ...]
    prepare: Callable[[Page], Awaitable[None]] = field(
        default=_no_prepare, repr=False, compare=False)


# --------------------------------------------------------------------------- paths


def _assessments(ids: dict) -> str:
    return "/workspace/assessments?review=all&sort=score"


def _main_card(ids: dict) -> str:
    """The list narrowed to the main assessment's lab, so its card is the first one."""
    return f"/workspace/assessments?review=all&lab={ids['lab_main']}"


def _main_detail(ids: dict) -> str:
    return f"/workspace/assessments/{ids['assessment_main']}"


def _pis(ids: dict) -> str:
    return "/workspace/pis"


def _featured_pi(ids: dict) -> str:
    return f"/workspace/pis/{ids['pi_featured']}"


def _pending_pi(ids: dict) -> str:
    return f"/workspace/pis/{ids['pi_pending']}"


def _slack_bots(ids: dict) -> str:
    return "/workspace/slack-bots"


def _staff(ids: dict) -> str:
    return "/workspace/staff"


def _discussions(ids: dict) -> str:
    return "/workspace/discussions"


def _activity(ids: dict) -> str:
    return "/workspace/activity"


def _prompt_suggestions(ids: dict) -> str:
    return "/workspace/prompt-suggestions"


def _prompt_suggestion(ids: dict) -> str:
    return f"/workspace/prompt-suggestions/{ids['prompt_suggestion']}"


# --------------------------------------------------------------------------- prepares


async def _open_card_scores(page: Page) -> None:
    """Open the first card's "Rubric scores & gating" disclosure."""
    await page.locator(".assessment-card details.assessment-card-scores > summary").first.click()


async def _show_panel_and_gating(page: Page) -> None:
    """Open Gating criteria and scroll the panel banner to the top of the viewport."""
    await page.locator("#gating > summary").click()
    await page.evaluate("document.getElementById('panel').scrollIntoView({block: 'start'})")


async def _open_timeline(page: Page) -> None:
    await page.locator("#timeline > summary").click()


async def _ask_chat(page: Page) -> None:
    """Open the drawer, ask CHAT_QUESTION and wait for the scripted answer to finish."""
    await page.locator("[data-chat-bubble]").click()
    await page.locator("#assessment-chat:not(.hidden)").wait_for(state="visible")
    await page.wait_for_function(
        "() => document.querySelector('[data-chat-usage]')"
        ".textContent.includes('questions used today')")
    box = page.locator("#assessment-chat-question")
    await box.fill(CHAT_QUESTION)
    await box.press("Enter")
    await page.wait_for_function(
        "(marker) => document.getElementById('assessment-chat').innerText.includes(marker)",
        arg=CHAT_ANSWER_MARKER, timeout=60000)
    await page.wait_for_function(
        "() => !document.getElementById('assessment-chat-question').readOnly", timeout=60000)


async def _show_account_and_edit(page: Page) -> None:
    """Scroll the staff Account card, followed by Edit Profile, to the top."""
    await page.evaluate(
        "() => [...document.querySelectorAll('main h2')]"
        ".find((h) => h.textContent.trim() === 'Account')"
        ".parentElement.scrollIntoView({block: 'start'})")


async def _open_industry_evidence(page: Page) -> None:
    await page.locator("#industry details > summary").click()


async def _preview_staff_account(page: Page) -> None:
    """Post the Add form for the seed's unused iD; the stubbed ORCID lookup answers."""
    await page.fill("#staff-orcid", STAFF_PREVIEW_ORCID)
    await page.select_option("#staff-role", "reviewer")
    async with page.expect_navigation(wait_until="networkidle"):
        await page.locator('section[aria-labelledby="staff-add-heading"] button[type="submit"]').click()


# --------------------------------------------------------------------------- the list

SHOTS: tuple[Shot, ...] = (
    # Audience "all": captured as a reviewer.
    Shot("topbar-help", AUDIENCE_ALL, "reviewer", _assessments, 'nav[aria-label="Main"]',
         "The top navigation bar, with Workspace, Help, Settings and Sign out.",
         ("templates/base.html",)),
    Shot("intro-prompt", AUDIENCE_ALL, "new_reviewer", _assessments,
         '[role="region"][aria-label="Getting started"]',
         "The first-sign-in prompt offering the Reviewer tutorial, with Start the tutorial "
         "and Don't show again.",
         ("templates/base.html",)),
    Shot("assessments-list", AUDIENCE_ALL, "reviewer", _assessments, None,
         "The Opportunity Assessments page: the Run, Sort, Assignment and Lab filters, the "
         "review tabs and the first assessment cards.",
         (_ASSESSMENTS, _ASSESSMENTS_BODY)),
    Shot("assessment-card", AUDIENCE_ALL, "reviewer", _main_card, ".assessment-card",
         "One assessment card: the recommendation, the headline, the summary and the "
         "quick-scoring form.",
         (_ASSESSMENTS, _ASSESSMENTS_BODY)),
    Shot("card-rubric-gating", AUDIENCE_ALL, "reviewer", _main_card,
         ".assessment-card details.assessment-card-scores[open]",
         "An assessment card's rubric scores and gating criteria, opened.",
         (_ASSESSMENTS, _ASSESSMENTS_BODY), _open_card_scores),
    Shot("detail-brief", AUDIENCE_ALL, "reviewer", _main_detail, "#brief",
         "The brief at the top of an assessment's detail page: headline, pitch and key points.",
         (_DETAIL, _DETAIL_BODY)),
    Shot("detail-evidence", AUDIENCE_ALL, "reviewer", _main_detail, "#signals",
         "The Evidence summary on an assessment's detail page: strengths, risks and what "
         "was not established.",
         (_DETAIL, _DETAIL_BODY)),
    Shot("detail-panel-gating", AUDIENCE_ALL, "reviewer", _main_detail, None,
         "The specialist panel banner and the opened Gating criteria on an assessment's "
         "detail page.",
         (_DETAIL, _DETAIL_BODY), _show_panel_and_gating),
    Shot("detail-timeline", AUDIENCE_ALL, "reviewer", _main_detail, "#timeline",
         "The opened Interview timeline: the lab's pitch, the hub's questions and the "
         "specialist consults in order.",
         (_DETAIL, _DETAIL_BODY), _open_timeline),
    Shot("chat-drawer", AUDIENCE_ALL, "reviewer", _main_detail, "#assessment-chat",
         "The assessment chat drawer with a question and its answer.",
         (_DETAIL, _DETAIL_BODY, "templates/assessments/_chat_drawer.html"), _ask_chat),
    Shot("review-form", AUDIENCE_ALL, "reviewer", _main_detail, "#review",
         "The Human review section: other reviewers' feedback and the form for an overall "
         "rating, dimension scores, a comment and the feedback mode.",
         (_DETAIL, _DETAIL_BODY)),
    Shot("pis-list-reviewer", AUDIENCE_ALL, "reviewer", _pis, None,
         "The PIs directory as a reviewer sees it.",
         ("templates/workspace/pis.html",)),
    Shot("pi-detail-reviewer", AUDIENCE_ALL, "reviewer", _featured_pi, None,
         "The top of a PI's page as a reviewer sees it.",
         (_PI_DETAIL,)),
    # Audience "staff": captured as a manager.
    Shot("pis-list-manager", AUDIENCE_STAFF, "manager", _pis, None,
         "The PIs page as a manager sees it: the Add PI form, the filters and the PI table "
         "with profile and agent status.",
         ("templates/workspace/pis.html",)),
    Shot("pi-header-pending", AUDIENCE_STAFF, "manager", _pending_pi,
         "main div.max-w-4xl > div:first-child",
         "The header of a PI whose agent is pending, with its status and next action.",
         (_PI_DETAIL, "templates/pis/_bot_action.html")),
    Shot("pi-account-edit", AUDIENCE_STAFF, "manager", _featured_pi, None,
         "A PI's Account card and the Edit Profile form.",
         (_PI_DETAIL,), _show_account_and_edit),
    Shot("pi-paper-review", AUDIENCE_STAFF, "manager", _featured_pi, "#review",
         "Corpus and profile review: the pending draft, a candidate paper, an unanchored "
         "paper and an excluded one.",
         (_PI_DETAIL, "templates/pis/_profile_review.html")),
    Shot("pi-grants", AUDIENCE_STAFF, "manager", _featured_pi, "#grants",
         "The Grants card: the RePORTER identity, NIH awards and ORCID fundings.",
         (_PI_DETAIL,)),
    Shot("pi-industry", AUDIENCE_STAFF, "manager", _featured_pi, "#industry",
         "The Industry interest card with its evidence list opened.",
         (_PI_DETAIL,), _open_industry_evidence),
    Shot("pi-companies", AUDIENCE_STAFF, "manager", _featured_pi, "#companies",
         "The Companies card: a confirmed company, a suggestion to confirm or reject, and the "
         "form to add one.",
         (_PI_DETAIL,)),
    Shot("slack-bots", AUDIENCE_STAFF, "manager", _slack_bots, None,
         "The Slack Bots page: counts by status and each PI's bot with its action.",
         ("templates/workspace/slack_bots.html", "templates/pis/_bot_action.html")),
    Shot("assign-to", AUDIENCE_STAFF, "manager", _main_detail,
         '#review form[action$="/assign"] >> xpath=..',
         "The assignment list on an assessment, with the Assign to control.",
         (_DETAIL, _DETAIL_BODY)),
    Shot("staff-form", AUDIENCE_STAFF, "manager", _staff,
         'section[aria-labelledby="staff-add-heading"]',
         "The Add a staff account form: ORCID iD, role and an optional name.",
         ("templates/workspace/staff.html",)),
    Shot("staff-preview", AUDIENCE_STAFF, "manager", _staff,
         'section[aria-labelledby="staff-confirm-heading"]',
         "The Confirm box after Preview: who the iD belongs to, with Create account and Cancel.",
         ("templates/workspace/staff.html",), _preview_staff_account),
    Shot("staff-table", AUDIENCE_STAFF, "manager", _staff,
         'section[aria-labelledby="staff-accounts-heading"]',
         "The staff Accounts table with approved, pending and revoked accounts.",
         ("templates/workspace/staff.html",)),
    Shot("discussions", AUDIENCE_STAFF, "manager", _discussions, None,
         "The Discussions page: thread counts by outcome, the filters and the thread list.",
         ("templates/workspace/discussions.html", "templates/discussions/_threads.html")),
    Shot("activity", AUDIENCE_STAFF, "manager", _activity, None,
         "The Agent Activity page: run totals and the list of simulation runs.",
         ("templates/workspace/activity.html",)),
    Shot("prompt-suggestions", AUDIENCE_STAFF, "manager", _prompt_suggestions, None,
         "The Prompt Suggestions page: the status filter, Generate suggestions and the list.",
         ("templates/workspace/prompt_suggestions.html",)),
    Shot("prompt-suggestion-detail", AUDIENCE_STAFF, "manager", _prompt_suggestion, None,
         "One prompt suggestion: the suggested change, its status buttons and the feedback "
         "it was distilled from.",
         ("templates/workspace/prompt_suggestion_detail.html",)),
)

SHOTS_BY_ID: dict[str, Shot] = {shot.id: shot for shot in SHOTS}
assert len(SHOTS_BY_ID) == len(SHOTS), "duplicate shot id"
