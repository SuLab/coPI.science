# Help Tab and Onboarding Tutorials Implementation Plan

> **For agentic workers:** this plan is executed with **`/lathe:plan-execution`**. The owner's
> policy routes plan execution there, in place of superpowers' subagent-driven-development
> and executing-plans.
> - The main session writes the Task 0 contracts first.
> - It then dispatches packages P1–P14 in **one parallel batch** of `lathe:bulk-implementer`
>   agents. P7b is a `lathe:mechanical-worker`.
> - Implementers **do not build, run tests, lint or commit**. Verification happens once,
>   after merge (Task 15).
> - Checkbox steps track progress.

**Goal:** add a role-filtered Help tab with reference documentation and two read-only
onboarding tutorials, one for managers and one for reviewers. Add a first-sign-in prompt,
per-page Help links, generated screenshots, and the UI wording fixes. Then deploy, without
starting the simulation.

**Architecture:** `src/web/help_pages.py` is a Python registry of help pages and tutorial
steps. It drives one router, `src/routers/help.py`, that renders Jinja templates under
`templates/help/`.
- **Role gate:** `get_review_user` plus the registry's audience field.
- **Prompt:** a new table, `help_intro_dismissals` (migration 0067), stores whether a user
  dismissed the first-sign-in prompt. A workspace-router dependency reads it.
- **Screenshots:** they come from a seeded throwaway instance, are committed under
  `help_assets/shots/`, and are served by an authenticated route.

**Tech Stack:** FastAPI, Jinja2, SQLAlchemy 2 async, Alembic, compiled Tailwind v3.4.19
(`scripts/build_css.sh`), pytest with testcontainers, and Playwright through the
`tests/e2e/ui_audit` harness.

**Spec:** `docs/specs/2026-10-14-help-and-onboarding-design.md` (revision 3, approved
2026-10-14, plus D16/D17). **Every implementer reads the spec sections named in their
package before writing anything.** The spec's Appendix A is the starting inventory for
content.

## Global Constraints

- **Read before you write.**
  - Do not rely on memory or training for how this repo or its libraries behave. Read the
    current file, or the installed library under `.venv-test/lib/python3*/site-packages/`.
  - Every `file:line` in the spec and this plan was read at `3bafb292`. Re-read before
    relying on a line number.
- **Mount.** The checkout is an sshfs mount of the production host's working tree. Never
  touch:
  - `docker-compose.prod.yml` (uncommitted by decision D5; never checkout, stash or
    restore it);
  - `.env*`;
  - `profiles/`;
  - `data/`;
  - `docs/audits/`;
  - the existing uncommitted edits in `CLAUDE.md`.
- **Help audience.** Help is visible to admin, manager and reviewer; PIs get 403. The
  reviewer view excludes staff-audience pages. Managers and admins see everything.
- **Template names are literal strings in `src/`** (`tests/unit/test_reachability.py`).
  No computed template name and no `{% include some_var %}`.
- **Rendered-page gate** (`tests/integration/test_rendered_page_gate.py`) rules for every
  template:
  - one `<main>` and one `<h1>`;
  - headings never skip a level going down;
  - every `<nav>` uniquely named;
  - no `text-gray-300` or `text-gray-400`;
  - no inline `on…=` attribute;
  - every input, select and textarea has an accessible name;
  - no empty `<th>`.
- **Enforced CSP** (`src/web/security_headers.py`): no inline scripts or handlers, and no
  new JavaScript at all in this feature.
- **Help content rules (spec §6.3)**, which bind every content package:
  1. Every fact comes from reading the current code.
  2. Name the UI text exactly, after the §7 changes.
  3. State no number that comes from settings or the rubric: band thresholds, dimension
     titles and weights, chat limits, cost caps, page sizes, batch sizes, label strings
     such as `pass_label`. Point to where the page shows it.
  4. The **only** numbers allowed are the 1–5 overall rating and the comment limit of
     10,000 characters (`src/services/assessment_reviews.py:128-131, 158-159`). Leave out
     the session lifetime, the 15-minute re-sign-in, the 1-hour Regenerate cooldown, the
     ~30 s roster sync, and the count of Workspace tabs.
  5. Role-only sections sit in `{% if permissions.staff %}…{% endif %}`. A reviewer reads
     only "a manager can…".
  6. Microsoft content sits in `{% if microsoft_sign_in %}…{% endif %}`.
  7. Every manager action with real or permanent effects is introduced by
     `{% call careful() %}…{% endcall %}`, which says what happens and whether the UI can
     undo it (spec Appendix A.2).
  8. Admin-only features appear only as "only an admin can…".
  9. No real names. Invent examples, and never copy them from code comments or tests.
  10. No repository internals: no file paths, code or test pointers, test counts, or audit
      history.
- **Product name** in web pages is "Blackbird".
- **Commits:** `blackbird` gets plain commits. Never push: `origin` is public.
- **Simulation:** never start it. Deploys follow root `CLAUDE.md` and
  `docs/operations/host-and-simulation.md`.

## Review Focus

The five inputs most likely to bite users that no package's happy-path tests would
exercise. Each has a pinned test in the named package.

1. **An admin impersonating a manager or reviewer.**
   - The prompt is hidden.
   - The last step shows "Back to Help" instead of a Finish form.
   - A POST to dismiss returns 403 "Help changes are disabled while impersonating.", and no
     row is written.
   - Tests in P2: `test_prompt_hidden_while_impersonating`,
     `test_dismiss_refused_while_impersonating`, `test_finish_is_a_link_while_impersonating`.
2. **Hostile or odd `next` values on dismiss:** `https://evil.example/`, `//evil.example`,
   `/\evil`, `/help/intro/dismiss` (POST-only), `/workspace/assessments?run_id=x`.
   - Only a same-app GET page is followed; anything else lands on `/workspace`.
   - Test in P2: `test_dismiss_next_is_validated` (parametrized).
3. **Odd tutorial step segments:** `0`, `9` and `11` (out of range), `01`, `1.0`, `²`,
   `-1`, a 4-digit number, and an empty string.
   - `lookup_step` raises 404 for each, never a 422 or a 500.
   - Test in P2: `test_tutorial_step_rejects_non_canonical_numbers` (unit level).
   - Over HTTP, `/help/tutorials/reviewer/` with a trailing slash is Starlette's 307 to
     the bare URL, then a 302 to step 1. That is not an error.
4. **Odd screenshot ids:** `../manifest`, `x%2F..`, an unknown id, and a `staff` shot
   requested by a reviewer or by an admin impersonating a reviewer.
   - These get 404, 404, 404, 403 and 403.
   - An anonymous request redirects to `/login` with no `next`.
   - Tests in P2: `test_shot_route_rejects_unknown_and_traversal`,
     `test_shot_route_audience`, `test_shot_route_anonymous_has_no_next`.
5. **Narrow screens (375 px)** on help pages, tutorial steps, and workspace pages showing
   the prompt.
   - No horizontal scroll, the screenshots scale down, and there are zero axe findings.
   - Test in P9: phase-6 journey `journey_help_narrow_layout`.

---

## Execution order

1. **Task 0** (main session): write the shared contracts. These are the files every package
   codes against.
2. **Fan-out:** P1–P14 run in parallel in one batch. File ownership is disjoint (table
   below).
3. **Task 15** (main session): integrate.
   - rebuild CSS;
   - apply the `CLAUDE.md` hunks;
   - capture the screenshots;
   - run `ci.sh` on the host;
   - run browser phases 0–6 on the workstation.
4. **Task 16:** adversarial audits, in one batch:
   - `lathe:plan-auditor`;
   - `lathe:semantic-reviewer`;
   - `lathe:security-reviewer`;
   - the §10.4 content audit (`lathe:auditor`, split by page group).

   Fix what they find, then re-verify.
5. **Task 17:** final recapture if `capture --check` reports stale shots, then commit.
6. **Task 18:** deploy (spec §11). Never start the simulation.

## Package ownership (disjoint)

| Pkg | Owner files (create C / modify M) |
|---|---|
| T0 (main) | C `src/web/help_pages.py`, C `src/models/help.py`, C `templates/help/_layout.html`, C `templates/help/_step_nav.html`, C `templates/help/_figure.html`, C `templates/help/_link.html`, C `templates/help/_callouts.html`, C `help_assets/shots/manifest.json`, M `static/css/input.css` |
| P1 | C `alembic/versions/0067_help_intro_dismissals.py`, M `src/models/__init__.py`, M `scripts/migrate/preflight.py`, M `tests/unit/test_migration_checks.py`, M `tests/integration/test_harness_smoke.py`, C `tests/integration/test_migration_0067.py`, M `docs/operations/migration-deploy-notes.md`, C `docs/operations/help-content.md`, M `docs/specs/workspace-web-ui.md`, M `docs/operations/host-and-simulation.md` |
| P2 | C `src/routers/help.py`, C `src/web/help_intro.py`, M `src/main.py`, M `src/routers/workspace/__init__.py`, M `src/web/page_context.py`, M `src/web/templating.py`, M `src/dependencies.py`, M `templates/base.html`, C `templates/help/index.html`, C `tests/unit/test_help_registry.py`, C `tests/integration/test_help_routes.py`, C `tests/integration/test_help_intro.py` |
| P3a | C `templates/help/pages/getting-started.html`, `how-it-works.html`, `glossary.html`, `troubleshooting.html` |
| P3b | C `templates/help/pages/assessments.html`, `pis.html`; C `tests/unit/test_help_stated_constants.py` |
| P4a | C `templates/help/pages/managing-pis.html`, `pi-evidence.html` |
| P4b | C `templates/help/pages/staff-accounts.html`, `discussions-activity.html`, `prompt-suggestions.html` |
| P5a | C `templates/help/tutorials/reviewer/01-welcome.html` … `08-pi-pages-and-help.html` (8 files) |
| P5b | C `templates/help/tutorials/manager/01-your-role.html` … `10-discussions-activity-suggestions.html` (10 files) |
| P6a | M `templates/workspace/pis.html`, `pi_detail.html`, `slack_bots.html`, `staff.html`; C `templates/pis/_detail_errors.html`; C `tests/unit/test_pi_detail_error_codes.py`; M `tests/integration/test_reviewer_role.py`; C `tests/integration/test_help_wording.py` |
| P6b | M `templates/workspace/assessments.html`, `assessment_detail.html`, `discussions.html`, `activity.html`, `activity_detail.html`, `prompt_suggestions.html`, `prompt_suggestion_detail.html`; M `templates/assessments/_detail_body.html`; M `templates/settings.html` |
| P7a | M `templates/login.html`, `templates/access_pending.html`, `templates/profile/delete_account.html`; M `src/services/assessment_chat_record.py`; M `tests/unit/test_assessment_chat_record.py`; C `tests/integration/test_help_copy.py` |
| P7b | M the 22 remaining "CoPI" templates (listed in P7b); C `tests/unit/test_no_copi_in_templates.py` |
| P9 | C `tests/e2e/help_shots/__init__.py`, `seed.py`, `serve.py`, `shots.py`, `capture.py`, `README.md`; M `tests/e2e/ui_audit/env.py`; C `tests/unit/test_help_shots.py`; C `tests/e2e/ui_audit/journeys_phase6.py`; M `tests/e2e/ui_audit/journeys_phase5.py`; M `tests/e2e/ui_audit/README.md` |
| P11 | M `tests/unit/test_reachability.py`, M `tests/integration/test_rendered_page_gate.py`, M `tests/integration/test_workspace_permissions_matrix.py`, M `tests/unit/test_docker_build_context.py`, C `tests/integration/test_help_links_render.py` |
| T15 (main) | M `static/css/app.css` (rebuild), M `CLAUDE.md` (two hunks only), C/M `help_assets/shots/*.png` + `manifest.json` (capture output) |

(P8 and P10 were folded into P7a and P9.)

---

## Task 0: Shared contracts (main session, before fan-out)

**Files:** listed in the T0 row above.

**Interfaces produced (every package codes against these exactly):**
- **`src/web/help_pages.py`**
  - constants: `AUDIENCE_ALL`, `AUDIENCE_STAFF`, `RESERVED_SLUGS`, `SHOTS_DIR`,
    `MANAGER_ACCESS_REQUIRED`;
  - dataclasses: `HelpPage`, `TutorialStep`, `Tutorial`;
  - registries: `HELP_PAGES`, `TUTORIALS`;
  - functions:
    - `pages_for(permissions) -> tuple[HelpPage, ...]`
    - `tutorials_for(permissions) -> tuple[Tutorial, ...]`
    - `lookup_page(slug, permissions) -> HelpPage`
    - `lookup_tutorial(key, permissions) -> Tutorial`
    - `lookup_step(key, step: str, permissions) -> tuple[Tutorial, int, TutorialStep]`
    - `tutorial_key_for(user_role) -> str | None`
    - `navigation_for(request, permissions) -> tuple[dict, ...]`
    - `shot_ids() -> frozenset[str]`
    - `shot_meta(shot_id) -> dict`
    - `shot_path(shot_id) -> Path`
    - `reset_manifest_cache() -> None` (removed during integration: only tests used it, which
      `test_no_dead_src_symbols` refuses; the tests call `help_pages._manifest.cache_clear()`)
- **`src/models/help.py`:** `HelpIntroDismissal` (columns `user_id`, `dismissed_at`, `via`),
  plus `VIA_DISMISS`, `VIA_FINISH`, `VIA_VALUES`, `CHECK_NAME`.
- **Template macros:**
  - `help/_figure.html` → `figure(request, shot_id, caption)`;
  - `help/_link.html` → `help_link(request, viewer, slug, anchor="")`;
  - `help/_callouts.html` → `careful(title="Careful")` and `note(title="Note")`, used with
    `{% call %}`.
- **`help/_layout.html`** (block `help_body`, wrapper `.help-prose`) and
  **`help/_step_nav.html`**.
- **Jinja global `help_shot_meta`** is registered by P2 in `src/web/templating.py` and
  points at `help_pages.shot_meta`.
- **Context keys** P2 provides to every help template:
  - from `page_context`: `request`, `permissions`, `effective_user`, `current_user`,
    `impersonation_banner`, `active_page="help"`, `active_section`;
  - Help-specific: `help_navigation`, `microsoft_sign_in` (bool), `help_title` (str);
  - per page: `help_page` (`HelpPage`) on reference pages; `tutorial`, `step`,
    `step_number`, `step_count` on steps; `pages` and `tutorials` on the index.

- [ ] **Step 1: write `src/models/help.py`**

```python
"""Whether a manager or reviewer has answered the Help tab's first-sign-in prompt
(migration 0067; spec docs/specs/2026-10-14-help-and-onboarding-design.md §5.1).

One row per user who dismissed the prompt or finished a tutorial; no row means never
answered. No relationship is declared on User: deleting the user removes the row through
the database's ON DELETE CASCADE, and a relationship without passive_deletes would make
db.delete(user) try to null this primary-key foreign key.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

VIA_DISMISS = "dismiss"
VIA_FINISH = "finish"
VIA_VALUES = (VIA_DISMISS, VIA_FINISH)
CHECK_NAME = "ck_help_intro_dismissals_via"


class HelpIntroDismissal(Base):
    __tablename__ = "help_intro_dismissals"
    __table_args__ = (
        CheckConstraint("via IN ('dismiss', 'finish')", name=CHECK_NAME),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    via: Mapped[str] = mapped_column(String(16), nullable=False)
```

- [ ] **Step 2: write `src/web/help_pages.py`**

```python
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
             ("pis-and-lab-agents", "simulation-runs", "interviews", "specialist-panel",
              "assessments", "human-review", "prompt-suggestions")),
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
             ("add-pi", "profile-status", "account", "edit-profile", "paper-review", "drafts",
              "regenerate", "persona", "jobs", "slack-bot", "activation", "mute", "slack-bots")),
    HelpPage("pi-evidence", "Grants, industry interest and companies",
             "Checking and correcting the grants, industry evidence and companies on a PI's page.",
             AUDIENCE_STAFF, "help/pages/pi-evidence.html",
             ("reporter-identity", "grants", "orcid-fundings", "industry", "companies")),
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


def reset_manifest_cache() -> None:
    _manifest.cache_clear()


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
```

- [ ] **Step 3: write the templates**

`templates/help/_layout.html`:

```jinja
{% extends "base.html" %}
{# Every help page and tutorial step extends this (spec §6.2). Content is semantic HTML
   inside .help-prose, styled once in static/css/input.css. #}
{% block title %}{{ help_title }} — Help — Blackbird{% endblock %}
{% block content %}
<article class="help-prose max-w-3xl">
{% block help_body %}{% endblock %}
</article>
{% endblock %}
```

`templates/help/_step_nav.html`. It is included by every step template after its body and
reads `request`, `tutorial`, `step`, `step_number`, `step_count` and `impersonation_banner`:

```jinja
{# Tutorial step navigation (spec §6.2). Finish posts the prompt answer; while
   impersonating that POST is refused, so a plain link replaces it. #}
<nav aria-label="Tutorial steps" class="mt-10 border-t border-gray-200 pt-4 text-sm">
    <p class="text-gray-600" aria-current="step">Step {{ step_number }} of {{ step_count }}</p>
    <div class="mt-3 flex flex-wrap items-center gap-3">
        {% if step.open_route %}
        <a href="{{ page_url(request, step.open_route) }}" target="_blank" rel="noopener"
           class="text-indigo-700 hover:text-indigo-900">Open this page ↗ (opens in a new tab)</a>
        {% endif %}
        {% if step_number > 1 %}
        <a href="{{ page_url(request, 'help_tutorial_step', tutorial=tutorial.key, step=step_number - 1) }}"
           class="text-indigo-700 hover:text-indigo-900">← Previous step</a>
        {% endif %}
        {% if step_number < step_count %}
        <a href="{{ page_url(request, 'help_tutorial_step', tutorial=tutorial.key, step=step_number + 1) }}"
           class="bg-indigo-600 text-white px-3 py-1.5 rounded hover:bg-indigo-700">Next step →</a>
        {% elif impersonation_banner %}
        <a href="{{ page_url(request, 'help_index') }}" class="text-indigo-700 hover:text-indigo-900">Back to Help</a>
        {% else %}
        <form method="post" action="{{ page_url(request, 'help_intro_dismiss') }}">
            <input type="hidden" name="via" value="finish">
            <button type="submit" class="bg-indigo-600 text-white px-3 py-1.5 rounded hover:bg-indigo-700">Finish</button>
        </form>
        {% endif %}
    </div>
</nav>
```

`templates/help/_figure.html`:

```jinja
{# figure(request, shot_id, caption): one screenshot from help_assets/shots (spec §6.2, §8.3).
   `request` is passed explicitly: an imported macro does not see the caller's context. #}
{% macro figure(request, shot_id, caption) -%}
{%- set meta = help_shot_meta(shot_id) -%}
<figure class="my-6">
    <img src="{{ page_url(request, 'help_shot', shot_id=shot_id) }}" alt="{{ meta.alt }}"
         width="{{ meta.width }}" height="{{ meta.height }}" loading="lazy"
         class="max-w-full h-auto rounded border border-gray-200 shadow-sm">
    <figcaption class="mt-2 text-sm text-gray-600">{{ caption }}</figcaption>
</figure>
{%- endmacro %}
```

`templates/help/_link.html`:

```jinja
{# help_link(request, viewer, slug, anchor): the "Help with this page" link in a page header
   (spec §6.6). Renders only for the roles that can open Help. #}
{% macro help_link(request, viewer, slug, anchor="") -%}
{%- if viewer and (viewer.is_staff or viewer.is_reviewer) -%}
<a href="{{ page_url(request, 'help_page', slug=slug, fragment=anchor) }}"
   class="text-sm text-indigo-700 hover:text-indigo-900 whitespace-nowrap"><span aria-hidden="true">?</span> Help with this page</a>
{%- endif -%}
{%- endmacro %}
```

`templates/help/_callouts.html`:

```jinja
{# Callouts for help content (spec §6.3 rule 7). Use with {% call careful() %}…{% endcall %}. #}
{% macro careful(title="Careful") -%}
<div class="help-callout my-4 rounded-md border border-amber-300 bg-amber-50 p-4 text-amber-900" role="note">
    <p class="font-semibold">{{ title }}</p>
    <div class="mt-1">{{ caller() }}</div>
</div>
{%- endmacro %}
{% macro note(title="Note") -%}
<div class="help-callout my-4 rounded-md border border-blue-200 bg-blue-50 p-4 text-blue-900" role="note">
    <p class="font-semibold">{{ title }}</p>
    <div class="mt-1">{{ caller() }}</div>
</div>
{%- endmacro %}
```

- [ ] **Step 4: write the shared stylesheet and the initial manifest**

Append to `static/css/input.css`:

```css
/* Help pages (templates/help/, spec 2026-10-14 §6.2): content templates are plain semantic
   HTML inside .help-prose; Tailwind's preflight strips heading and list styles, so they are
   restored here once instead of on every element. */
.help-prose h1 { @apply text-2xl font-bold text-gray-900 mb-4; }
.help-prose h2 { @apply text-xl font-semibold text-gray-900 mt-10 mb-3 scroll-mt-4; }
.help-prose h3 { @apply text-lg font-semibold text-gray-800 mt-6 mb-2 scroll-mt-4; }
.help-prose p { @apply text-gray-700 mb-4 leading-relaxed; }
.help-prose ul { @apply list-disc pl-6 mb-4 space-y-1 text-gray-700; }
.help-prose ol { @apply list-decimal pl-6 mb-4 space-y-1 text-gray-700; }
.help-prose dt { @apply font-semibold text-gray-900 mt-4 scroll-mt-4; }
.help-prose dd { @apply text-gray-700 mb-2; }
/* Button-styled links (a bg-* class, e.g. the step nav's "Next step") keep their own
   colours, as the main p a rule above does. */
.help-prose a:not([class*="bg-"]) { @apply text-indigo-700; }
.help-prose a:not([class*="bg-"]):hover { @apply text-indigo-900; }
.help-prose strong { @apply font-semibold text-gray-900; }
.help-prose .help-callout p { @apply mb-1 text-inherit; }
```

> Task 15 verifies `text-inherit` exists in Tailwind v3.4.19. If it does not, replace it
> with `color: inherit;`.

Create `help_assets/shots/manifest.json` containing exactly `{}` and a trailing newline.

- [ ] **Step 5: sanity-check the contracts (no tests)**

Run `python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" src/web/help_pages.py`,
and the same for `src/models/help.py`. Then dispatch the batch.

---

## P1: Migration, model export, bookkeeping and operations docs

**Read first:**
- spec §5.1, §11;
- `alembic/CLAUDE.md`;
- `alembic/versions/0066_user_microsoft_identities.py`;
- `scripts/migrate/preflight.py` (around `:78`, `:100-125`, `:555-595`);
- `tests/integration/test_migration_0062.py` (the precedent);
- `docs/operations/migration-deploy-notes.md` (the 0066 box and the deploy-order
  convention, `:686-700`);
- `docs/operations/host-and-simulation.md:405-415`;
- `docs/specs/workspace-web-ui.md`.

**Interfaces:** consumes `src/models/help.py` (T0) and produces revision `0067`.

**Requirements:**
1. **`alembic/versions/0067_help_intro_dismissals.py`:**
   - revision `"0067"`, `down_revision = "0066"`;
   - `upgrade()` creates `help_intro_dismissals` with the exact columns and named CHECK of
     `src/models/help.py`: `user_id` uuid PK, FK `users.id` ON DELETE CASCADE;
     `dismissed_at` timestamptz NOT NULL default `now()`; `via` varchar(16) NOT NULL;
     `ck_help_intro_dismissals_via`;
   - `downgrade()` drops the table;
   - the docstring states, as 0066 does, what old code on the new schema and new code on
     the old schema do (spec §5.1).
2. **`src/models/__init__.py`:** import and export `HelpIntroDismissal`, appended to
   `__all__`.
3. **`scripts/migrate/preflight.py`:**
   - `DEFAULT_TARGET = "0067"`;
   - add `"0066"` to `SUPPORTED_START_REVISIONS`;
   - append `"0067"` to `REVISION_ORDER`;
   - add `PlannedObject("0067", "table", "help_intro_dismissals")` and the constraint
     `ck_help_intro_dismissals_via` to `PLANNED_OBJECTS`, in the 0066 entries' format;
   - extend the comment block that lists each revision's objects.
4. **The pin tests move with it:**
   - `tests/unit/test_migration_checks.py:276-283`: the asserted
     `SUPPORTED_START_REVISIONS` tuple gains `"0066"`, and `DEFAULT_TARGET == "0067"`;
   - `tests/integration/test_harness_smoke.py:99-100`: a comment line for 0067, and
     `"0067"`;
   - grep both files for any other `0066` pin and move it too.
5. **`tests/integration/test_migration_0067.py`**, modelled on `test_migration_0062.py`:
   - the table and its columns exist at head;
   - an insert with `via='other'` raises `IntegrityError` naming
     `ck_help_intro_dismissals_via`;
   - deleting the user (raw `DELETE FROM users`) removes the row;
   - a second insert for the same user violates the PK;
   - `downgrade()` then `upgrade()` through `Operations.context` leaves the table recreated.
6. **The "Deploy order for `0067_help_intro_dismissals`" box** in
   `docs/operations/migration-deploy-notes.md`, written as spec §5.1 and §11 say:
   - old/new code directions;
   - **worker idle** (no `processing` `generate_profile` job) or stopped, because
     `REFERENCES users` locks `users` within the chain's 10 s `lock_timeout`;
   - sign-ins queue during the apply;
   - all three images rebuilt (D17 and the models import);
   - `up -d agent` only with no live run;
   - rollback per spec §11, including `:help-0067` and "never reuse id 0067".

   Do not copy 0066's "takes no lock" claim.
7. **`docs/operations/help-content.md`** (new). It opens with the house header used by the
   other `docs/operations/*.md` files ("Reference detail behind the rules in the root
   `CLAUDE.md`…"). It states:
   - what to update when a workspace, settings, login, access-pending or delete-account
     page changes: the page's help section, the glossary, troubleshooting, and the
     tutorial step;
   - the §6.3 content rules, restated in full;
   - the capture commands, and that capture is the last step before a deploy;
   - that PNGs and `manifest.json` are committed together;
   - that the PI-detail error-code test cannot see a code added only inside a service;
   - that `help_assets` is in the pre-build path list.
8. **`docs/specs/workspace-web-ui.md`:** add rows to the capability table: "Help: general
   pages and the reviewer tutorial" (Yes/Yes/Yes), and "Help: manager pages and the manager
   tutorial" (No/Yes/Yes).
9. **`docs/operations/host-and-simulation.md:411`:** add `help_assets` to the
   `git status --porcelain` path list.

**Acceptance:**
- Task 15's verification passes `test_migration_0067.py`, `test_migration_checks.py`,
  `test_harness_smoke.py` and `ci.sh`'s upgrade→downgrade→upgrade round trip.
- The deploy-notes box names `0067_help_intro_dismissals`, which
  `tests/unit/test_claude_md_references.py` checks.

## P2: Help router, prompt dependency, core wiring, nav and index

**Read first:**
- spec §4, §5.2, §5.3, §6.1, §6.2, §8.3, §8.4, §10.1, §10.2;
- `src/routers/settings.py` (router and template pattern);
- `src/dependencies.py:31-58` (`_login_location`, `pending_access_payload`) and
  `:296-371`;
- `src/routers/auth.py:97-130` (`is_safe_next_url`);
- `src/web/page_context.py`, `src/web/templating.py`;
- `src/routers/workspace/__init__.py`;
- `src/main.py:338-354`;
- `src/web/head_requests.py`;
- `templates/base.html`;
- `tests/integration/test_staff_routes.py` (client, factories, session headers and the
  savepoint pattern);
- `tests/session_support.py`.

**Interfaces:**
- **Consumes:** all of T0.
- **Produces:**
  - route names `help_index`, `help_page`, `help_tutorial`, `help_tutorial_step`,
    `help_intro_dismiss`, `help_shot`;
  - context keys `help_navigation`, `microsoft_sign_in`, `help_title`, `help_page`,
    `tutorial`, `step`, `step_number`, `step_count`, `pages`, `tutorials`, `help_intro`;
  - Jinja global `help_shot_meta`;
  - `pending_access_payload` keys `user_role` and `access_status`.

- [ ] **Step 1: tests first**

  **(a)** `tests/unit/test_help_registry.py` (pure unit tests, no database):

```python
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.services.web_permissions import WebCapabilities
from src.web import help_pages as hp

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
STAFF = WebCapabilities(True, True, False, False, True, True, True, True)
REVIEWER = WebCapabilities(True, False, False, False, False, False, False, True)


def test_slugs_unique_and_not_reserved():
    slugs = [p.slug for p in hp.HELP_PAGES]
    assert len(slugs) == len(set(slugs))
    assert not set(slugs) & hp.RESERVED_SLUGS


def test_every_template_is_a_literal_existing_file():
    names = [p.template for p in hp.HELP_PAGES] + [
        s.template for t in hp.TUTORIALS for s in t.steps]
    for name in names:
        assert name.startswith("help/") and name.endswith(".html")
        assert (TEMPLATES / name).is_file(), name


def test_tutorial_shapes():
    keys = {t.key: t for t in hp.TUTORIALS}
    assert len(keys["reviewer"].steps) == 8
    assert len(keys["manager"].steps) == 10
    assert [t.key for t in hp.tutorials_for(STAFF)] == ["manager", "reviewer"]
    assert [t.key for t in hp.tutorials_for(REVIEWER)] == ["reviewer"]


def test_each_page_template_renders_its_declared_anchors():
    for page in hp.HELP_PAGES:
        source = (TEMPLATES / page.template).read_text(encoding="utf-8")
        for anchor in page.anchors:
            assert f'id="{anchor}"' in source, (page.slug, anchor)


def test_each_step_template_shows_its_declared_shots():
    for tutorial in hp.TUTORIALS:
        for step in tutorial.steps:
            source = (TEMPLATES / step.template).read_text(encoding="utf-8")
            for shot in step.shots:
                assert f'figure(request, "{shot}"' in source or f"figure(request, '{shot}'" in source, (step.template, shot)


def test_reviewer_cannot_open_staff_pages_or_manager_tutorial():
    with pytest.raises(HTTPException) as exc:
        hp.lookup_page("managing-pis", REVIEWER)
    assert exc.value.status_code == 403 and exc.value.detail == "Manager access required"
    with pytest.raises(HTTPException) as exc:
        hp.lookup_tutorial("manager", REVIEWER)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("step", ["0", "9", "01", "1.0", "²", "-1", "1000", "", " 1"])
def test_tutorial_step_rejects_non_canonical_numbers(step):
    with pytest.raises(HTTPException) as exc:
        hp.lookup_step("reviewer", step, REVIEWER)
    assert exc.value.status_code == 404


def test_tutorial_key_for_roles():
    assert hp.tutorial_key_for("manager") == "manager"
    assert hp.tutorial_key_for("reviewer") == "reviewer"
    assert hp.tutorial_key_for("admin") is None
    assert hp.tutorial_key_for("pi") is None
```

> Before writing the two `WebCapabilities(...)` positional constructors, confirm the field
> order in `src/services/web_permissions.py:28-37`. If it differs, use keyword arguments.

  **(b)** `tests/integration/test_help_routes.py` (`pytestmark = pytest.mark.integration`).
  It uses the `client` and `db_session` fixtures and `tests.factories.make_user`, with
  setup committed before requests as in `test_staff_routes.py`. Each test is named here
  with the exact assertion it makes:
  - `test_help_index_per_role`:
    - admin, manager and reviewer get 200;
    - a reviewer's page lacks the link to `/help/managing-pis`;
    - a manager's has it;
    - a PI gets 403 "Review access required";
    - an anonymous request gets 302 to `/login?next=%2Fhelp`.
  - `test_head_mirrors_get_on_help`: `client.head("/help")` returns the same status as GET
    for a reviewer, with the same `content-type`, and an empty body.
  - `test_help_nav_link_per_role`:
    - `/workspace/assessments` as a reviewer, `/workspace/pis` as a manager and
      `/admin/users` as an admin each contain `href="/help"` in `nav[aria-label="Main"]`;
    - `/profile` as a PI does not;
    - `/help/assessments` as a reviewer marks the Help top-nav link active (the
      `text-indigo-600 font-semibold` classes) and has `aria-current="page"` on the
      "Assessments" item of `nav[aria-label="Help sections"]`.
  - `test_staff_page_forbidden_to_reviewer`: GET `/help/managing-pis` as a reviewer gives
    403. Then GET `/help/tutorials/manager/1` gives 403.
  - `test_unknown_slug_and_step_404`: `/help/nope`, `/help/tutorials/nope` and
    `/help/tutorials/reviewer/99` each give 404.
  - `test_bare_tutorial_redirects_to_step_one`: `/help/tutorials/reviewer` gives 302 with
    `location` ending `/help/tutorials/reviewer/1`.
  - `test_finish_is_a_link_while_impersonating`: an admin impersonating a reviewer GETs
    `/help/tutorials/reviewer/8`. The body has "Back to Help" and no
    `action="/help/intro/dismiss"`.
  - `test_microsoft_section_follows_feature_flag`:
    - with `app.state.microsoft_sign_in = None`, `/help/getting-started` lacks
      `id="microsoft"`;
    - with a non-None sentinel object, it has it.
    - Find how the existing tests toggle it (`rg -n "microsoft_sign_in" tests/`) and follow
      that.
  - `test_shot_route_rejects_unknown_and_traversal`:
    - monkeypatch `help_pages.SHOTS_DIR` to a `tmp_path` holding `manifest.json`
      `{"a": {"audience": "all", "alt": "A", "width": 1, "height": 1, "sources": {}, "commit": "x"}, "s": {...audience "staff"...}}`
      and 1×1 PNGs `a.png` and `s.png`;
    - call `help_pages.reset_manifest_cache()` before and after;
    - `/help/shots/nope.png`, `/help/shots/..%2Fmanifest.png` and `/help/shots/x%2F...png`
      each give 404.
  - `test_shot_route_audience`, with the same fixture:
    - a reviewer gets 200 on `a` (`content-type` `image/png`, `cache-control` contains
      `private`);
    - a reviewer gets 403 on `s`;
    - an admin impersonating a reviewer gets 403 on `s`;
    - a manager gets 200 on `s`;
    - a PI gets 403.
  - `test_shot_route_anonymous_has_no_next`: an anonymous GET `/help/shots/a.png` gives 302
    with `location` exactly `/login`.

  **(c)** `tests/integration/test_help_intro.py`:
  - `test_prompt_shown_until_answered`:
    - a reviewer GET `/workspace/assessments` contains `aria-label="Getting started"` and a
      link to `/help/tutorials/reviewer`;
    - POST `/help/intro/dismiss` with `via=dismiss` and `next=/workspace/assessments` (with
      the Origin header the client sets) gives 302 to `/workspace/assessments`;
    - one row has `via == "dismiss"`;
    - the next GET has no prompt.
  - `test_prompt_offers_manager_tutorial_to_manager`: a manager GET `/workspace/pis` has a
    link to `/help/tutorials/manager`.
  - `test_prompt_never_for_admin`: an admin GET `/workspace/pis` has no prompt.
  - `test_prompt_hidden_while_impersonating`: an admin impersonating a reviewer with no row
    gets no prompt.
  - `test_dismiss_refused_while_impersonating`: a POST while impersonating gives 403, the
    detail is "Help changes are disabled while impersonating.", and no row is written.
  - `test_dismiss_rejects_bad_via`: `via=other` and a missing `via` each give 400, with no
    row.
  - `test_first_answer_kept`: `via=finish` then `via=dismiss` leaves one row with
    `via == "finish"`. The finish response is a 302 to `/help`.
  - `test_dismiss_next_is_validated`, parametrized over:
    - `https://evil.example/` → `/workspace`
    - `//evil.example` → `/workspace`
    - `/\\evil` → `/workspace`
    - `/help/intro/dismiss` → `/workspace`
    - `/workspace/assessments?run_id=x` → itself
  - `test_workspace_post_routes_unaffected`: a reviewer POST to a staff route, e.g.
    `/workspace/pis/{id}/mute`, still gives 403 "Manager access required". A PI GET
    `/workspace/slack-bots` gives 403 "Manager access required", not "Review access
    required".
  - `test_pending_access_payload_has_role_and_status`:
    `pending_access_payload(user)` includes `user_role` and `access_status`.
  - `test_login_location_skips_help_shots`: an anonymous GET `/help/shots/a.png` redirects
    to `/login`, the same as the shots test in (b).

- [ ] **Step 2: `src/routers/help.py`**

```python
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
    found, number, entry = help_pages.lookup_step(tutorial, step, capabilities_for(current_user))
    return templates.TemplateResponse(request, entry.template, _context(
        request, current_user, section=f"tutorial-{found.key}", help_title=entry.title,
        tutorial=found, step=entry, step_number=number, step_count=len(found.steps)))


@router.post("/intro/dismiss", name="help_intro_dismiss")
async def help_intro_dismiss(
    request: Request,
    via: str = Form(""),
    next_url: str = Form("", alias="next"),
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
    return FileResponse(help_pages.shot_path(shot_id), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{slug}", response_class=HTMLResponse, name="help_page")
async def help_page(request: Request, slug: str, current_user=_REVIEW):
    page = help_pages.lookup_page(slug, capabilities_for(current_user))
    return templates.TemplateResponse(request, page.template, _context(
        request, current_user, section=page.slug, help_title=page.title, help_page=page))
```

> Confirm three things before using this code:
> - `capabilities_for` reads `_is_impersonated` from the effective user (it does, at
>   `web_permissions.py:40-53`);
> - `src.routers.auth` imports cleanly from `src.routers.help`;
> - `Form("", alias="next")` is honoured by the installed FastAPI.
>
> If the `auth` import is circular, move only the import into the function body.

- [ ] **Step 3: `src/web/help_intro.py`**

```python
"""The first-sign-in prompt's state for workspace pages (spec §5.2).

A workspace-router dependency: it raises no HTTPException of its own (router dependencies
run before a route's own gate, so a raising check here would change a staff route's
refusal), and database errors propagate (a swallowed error would leave the shared session
in a failed transaction). HEAD reaches it as GET (src/web/head_requests.py).
"""

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models.help import HelpIntroDismissal
from src.web.help_pages import tutorial_key_for

# Module-level Depends, as src/routers/auth.py does: a call in a default argument is a
# ruff B008 finding, and src/ is under a ratcheted lint ceiling.
_USER = Depends(get_current_user)
_DB = Depends(get_db)


async def load_help_intro(request: Request, user=_USER, db: AsyncSession = _DB) -> None:
    if request.method != "GET" or getattr(user, "_is_impersonated", False):
        return
    key = tutorial_key_for(user.user_role)
    if key is None:
        return
    answered = await db.scalar(
        select(HelpIntroDismissal.user_id).where(HelpIntroDismissal.user_id == user.id))
    if answered is None:
        request.state.help_intro = key
```

- [ ] **Step 4: wiring edits**
  - **`src/main.py`:** import `help` from `src.routers` alongside the others. Add
    `application.include_router(help.router, prefix="/help", tags=["help"])` beside the
    workspace include.
  - **`src/routers/workspace/__init__.py`:** `router = APIRouter(dependencies=[Depends(load_help_intro)])`,
    importing `load_help_intro` from `src.web.help_intro`.
  - **`src/web/page_context.py`:** add `"help_intro": getattr(request.state, "help_intro", None)`
    to the returned dict, before `**content`.
  - **`src/web/templating.py`:** in `make_templates()`, add
    `templates.env.globals["help_shot_meta"] = shot_meta` (imported from
    `src.web.help_pages`), with a one-line comment that the figure macro reads screenshot
    metadata through it.
  - **`src/dependencies.py`:**
    - in `_login_location`, change `if target.startswith("/assessment-chat/")` to
      `if target.startswith(("/assessment-chat/", "/help/shots/"))`, and extend the
      docstring sentence to name screenshots;
    - in `pending_access_payload`, add `"user_role": user.user_role` and
      `"access_status": user.access_status`;
    - update its docstring.

- [ ] **Step 5: `templates/base.html`**
  1. After the Workspace link (inside the same `{% if current_user %}` block), add:

```jinja
{% if effective_user.is_staff or effective_user.is_reviewer %}
<a href="{{ page_url(request, 'help_index') }}" class="text-gray-600 hover:text-indigo-600 px-3 py-2 rounded-md text-sm font-medium {% if active_page == 'help' %}text-indigo-600 font-semibold{% endif %}">Help</a>
{% endif %}
```

  2. After the Workspace sub-nav block, add:

```jinja
{% if active_page == 'help' and help_navigation %}
<nav aria-label="Help sections" class="bg-gray-50 border-b border-gray-200">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="flex flex-wrap gap-x-6 gap-y-1 min-h-10 py-2 items-center text-sm">
            {% for item in help_navigation %}
            <a href="{{ item.url }}" {% if active_section == item.section %}aria-current="page" class="text-indigo-600 font-semibold"{% else %}class="text-gray-600 hover:text-gray-800"{% endif %}>{{ item.label }}</a>
            {% endfor %}
        </div>
    </div>
</nav>
{% endif %}
```

  3. Inside `<main>`, immediately before the flash block, add the prompt. It has **no
     heading element**:

```jinja
{% if active_page == 'workspace' and help_intro %}
<div role="region" aria-label="Getting started" class="mb-6 rounded-md border border-indigo-200 bg-indigo-50 p-4 text-indigo-900 flex flex-wrap items-center gap-3">
    <p class="flex-1 min-w-0"><strong>New to Blackbird?</strong> Take the {{ 'Manager' if help_intro == 'manager' else 'Reviewer' }} tutorial — a read-only tour of the pages you'll use.</p>
    <a href="{{ page_url(request, 'help_tutorial', tutorial=help_intro) }}" class="bg-indigo-600 text-white px-3 py-1.5 rounded text-sm hover:bg-indigo-700">Start the tutorial</a>
    <form method="post" action="{{ page_url(request, 'help_intro_dismiss') }}">
        <input type="hidden" name="via" value="dismiss">
        <input type="hidden" name="next" value="{{ request.url.path }}{% if request.url.query %}?{{ request.url.query }}{% endif %}">
        <button type="submit" class="text-sm text-indigo-800 underline hover:text-indigo-950">Don't show again</button>
    </form>
</div>
{% endif %}
```

- [ ] **Step 6: `templates/help/index.html`**
  - It extends `help/_layout.html`, block `help_body`.
  - `<h1>Help</h1>`, then one sentence saying what Help covers for this role.
  - `<h2 id="tutorials">Start here</h2>`: for each `tutorials` item, its title as a link to
    `page_url(request, 'help_tutorial', tutorial=t.key)`, its summary, and
    "{{ t.steps|length }} steps".
  - `<h2 id="reference">Reference</h2>`: a `<ul>` with one `<li>` per `pages` item, holding
    a link to `page_url(request, 'help_page', slug=p.slug)` and the summary.
  - No other content.

**Acceptance:**
- (a), (b) and (c) pass in Task 15.
- `test_reachability.py` and `test_rendered_page_gate.py` pass with P11's entries.

## P3a: Reference pages: getting-started, how-it-works, glossary, troubleshooting

**Read first:**
- spec §6.3, §6.4 (rows `getting-started`, `how-it-works`, `glossary`,
  `troubleshooting`), §7 (new labels), Appendix A.3, A.4;
- the code each section describes:
  - `templates/login.html`, `templates/settings.html`, `templates/access_pending.html`,
    `templates/base.html`;
  - `src/routers/auth.py:160-520`, `src/routers/microsoft_auth.py`,
    `src/routers/settings.py`;
  - `src/dependencies.py:60-145`;
  - `src/web/navigation.py`;
  - for how-it-works: `docs/agent-flow.md`, `docs/operations/blackbird-hub.md` and
    `prompts/rubric/blackbird-rubric.toml`, for concepts only, with no numbers;
  - for the glossary: every template the glossary terms come from (spec Appendix A, the
    §6.4 glossary row and §7);
  - for troubleshooting: the error and flash texts in Appendix A, `src/web/errors.py` and
    `src/main.py:225-235`.

**Template skeleton (all four pages):**

```jinja
{% extends "help/_layout.html" %}
{% from "help/_figure.html" import figure %}
{% from "help/_callouts.html" import careful, note %}
{% block help_body %}
<h1>…title from the registry…</h1>
<p>…one-paragraph purpose…</p>
<h2 id="…">…</h2>
…
{% endblock %}
```

Import only the macros you use.

**Requirements:**
1. **`getting-started.html`** has exactly the anchors in the registry. In order:
   - `signing-in`: ORCID sign-in, and that a page you tried to open before signing in
     opens afterwards.
   - `microsoft`: the whole `<h2>` and section inside `{% if microsoft_sign_in %}`. Cover
     linking in Settings after re-confirming with ORCID, signing in with "Sign in with
     Microsoft (staff)", and that unlinking signs you out everywhere.
   - `first-sign-in`: managers land on PIs and reviewers on Assessments, unless sign-in is
     resuming a page. There is no PI onboarding.
   - `navigation`: the top bar (Settings, Workspace, Help, Sign out). The Workspace
     sub-nav per role: reviewers see PIs and Assessments; the staff-only items sit inside
     `{% if permissions.staff %}`, named but not counted.
   - `settings`: Delete account (Settings shows it to every role, and it is permanent).
     The Microsoft subsection sits inside `{% if microsoft_sign_in %}`.
   - `signing-out`: signs you out on every device.
   - `access-problems`:
     - "access pending" (the queue copy);
     - "Access turned off": reviewers ask a manager or admin, managers ask an admin;
     - "Cross-site request refused." (reload the page and try again).

   Figures: `figure(request, "topbar-help", …)` in `navigation`, and
   `figure(request, "intro-prompt", …)` in `first-sign-in`.
2. **`how-it-works.html`**, anchors in registry order. A concept page:
   - what a PI is in Blackbird;
   - that each PI has a lab agent, a Slack bot that pitches the lab's work;
   - simulation runs, which staff start and stop (only "an admin starts runs");
   - BlackbirdBot, the hub, interviewing a lab's pitch;
   - the specialist panel's consults;
   - the stored assessment (verdict, recommendation, score, gates, key points);
   - human review (rating, dimension scores, Learn vs log only);
   - prompt suggestions, staff-generated from Learn feedback, which never edit a prompt
     automatically. For non-staff, end that section with "A manager decides when
     suggestions are generated."

   No figures. No numbers (rule 3).
3. **`glossary.html`:** `<h1>Glossary</h1>`, then a `<dl>`.
   - One `<dt id="term-<kebab>">` and `<dd>` per term, alphabetical.
   - Staff-only terms are wrapped in `{% if permissions.staff %}` (each `dt`/`dd` pair
     inside its own block).
   - It covers at least the glossary candidates in spec Appendix A.4 and the §6.4 glossary
     row: assessment, verdict, recommendation, band, weighted score, rubric, rubric
     version, dimension, gate/gating, met / not met / unconfirmed, red flag, key points,
     "In one minute", "Why this score", "The ask", evidence summary, specialist panel,
     consult, panel signals, panel states, confidence, run, lab, BlackbirdBot/hub,
     Unreviewed / Reviewed tabs, Approval status / "No approval decision", feedback, Learn,
     "Don't learn — log only", Quick scoring, assignment ("Assign to"), assessment chat,
     opening questions, Sources, PI, ORCID iD, JHU tenure start, Pubs (tenure), full
     career, industry interest, cohort percentile, RePORTER, companies.
   - Staff-only terms: profile status (No Profile / Generating / Complete / Pending draft,
     with "Pending Update" as the old name), agent status (Not requested / Pending /
     Active / Muted / Suspended, with "Awaiting Token" and "Inactive" as old names; Muted
     covers both a manager's Mute and an admin's park, and Unmute goes through the
     activation gate), persona, persona file, corpus, candidate / unanchored / excluded
     papers, grounded, draft, Regenerate, Retry profile generation, Install Slack bot,
     Activate, roster sync, hub spoke, veto, RePORTER identity statuses, suggested /
     confirmed companies, discovery, staff account, access approved / pending / revoked,
     Discussions thread statuses, prompt suggestion, STALE.
   - Old names point to the new term: "**Inactive** — older name for Muted."
   - "CoPI" is **not** listed.
4. **`troubleshooting.html`**, anchors `sign-in`, `access`, `assessments`,
   `assessment-chat`, `pi-pages` (whole section in a staff block) and `staff-pages` (staff
   block). Each entry is a `<h3>` with the **exact** message text, then "What it means" and
   "What to do". Sources:
   - **sign-in:** the login error texts (`login.html:22-30`), the Microsoft errors
     (`src/services/microsoft_identity.py:82-99`), "Could not confirm your ORCID sign-in.
     Please try again.";
   - **access:** "Review access required", "Manager access required", the access-pending
     copy, "Access turned off", "Cross-site request refused.";
   - **assessments:** the feedback validation messages (`src/services/assessment_reviews.py`)
     and the "already recorded a moment ago" flash, stating only the allowed numbers;
   - **assessment-chat:** the fixed error texts in `static/js/assessment_chat.js:66-91`,
     quoted exactly, with no numbers;
   - **pi-pages:** the PI-detail error sentences (P6a's `_detail_errors.html` macro, which
     includes the new `industry_busy` sentence) and the activation refusal banner;
   - **staff-pages:** the refusal messages at `src/services/staff_accounts.py:75-93` and
     "Staff account changes are disabled while impersonating." Leave out the
     admin-impersonation-only texts except "Help changes are disabled while impersonating."

**Acceptance:**
- The registry anchors are present (P2's unit test).
- The render test passes (P11).
- The rendered-page gate passes.
- The §10.4 content audit finds no contradiction with code.

## P3b: Reference pages: assessments, pis

**Read first:**
- spec §6.3, §6.4 (rows `assessments`, `pis`), §7.3, Appendix A.1, A.4;
- `templates/workspace/assessments.html`, `templates/assessments/_body.html`,
  `templates/workspace/assessment_detail.html`,
  `templates/assessments/_detail_body.html`, `templates/assessments/_chat_drawer.html`;
- `static/js/assessment_chat.js`, `static/js/assessment_print.js`;
- `src/routers/workspace/assessments.py`, `src/routers/reviews.py`,
  `src/services/assessment_reviews.py`, `src/routers/assessment_chat.py`,
  `src/services/assessment_chat_record.py:74-106, 221-224`, `src/services/directory.py`
  (the assessment list functions);
- `templates/workspace/pis.html`, `templates/workspace/pi_detail.html`;
- `src/routers/workspace/pi_directory.py`, `src/web/presentation.py:126-143` (what
  reviewers do not receive).

**Requirements:**
1. **`assessments.html`**, anchors `list`, `cards`, `detail`, `chat`, `reviewing`,
   `approval-status`, and `assigning` (the whole `assigning` section in a staff block).
   Content per spec §6.4:
   - **`list`:**
     - the run selector, sort options, "Assigned to me", the lab filter;
     - the Unreviewed / Reviewed / All tabs, where "Reviewed" means written feedback exists;
     - the banners (lost verdicts, incomplete panel);
     - the summary cards, the dimension distribution;
     - that the bands are printed in the header (no numbers).
   - **`cards`:**
     - headline and confidence;
     - the recommendation chip, which shows the rubric's own label for "pass" (no
       literal);
     - "In one minute", Key points, the detail and chat buttons;
     - "Why this score";
     - the footer badges;
     - "Rubric scores & gating", Quick scoring.
   - **`detail`:**
     - the jump nav and Expand/Collapse all;
     - each section in page order: Brief, Evidence summary, The ask, Verdict, Specialist
       panel, Gating, Red flags, Full rationale, Dimension scores, Interview timeline;
     - printing (browser print opens every section);
     - **staff block:** "In the hub's words", Competitive landscape, Evidence maturity, which
       only staff see.
   - **`chat`:**
     - three ways to open it;
     - opening questions; an inline "Ask:" sends at once;
     - Sources and "Show in page"; "Verdict updated after this point";
     - the drawer shows today's usage, and the limits are not stated;
     - Clear conversation deletes your conversation on that assessment;
     - "administrators with database access can read" saved conversations;
     - the chat is unavailable while impersonating.
   - **`reviewing`:**
     - the overall rating 1–5 is required; dimension scores are optional; the comment
       limit is 10,000 characters;
     - Learn vs "Don't learn — log only";
     - you can edit only your own feedback, and nobody but an admin can delete;
     - submitting from the Unreviewed tab moves the card to Reviewed;
     - a duplicate within a minute is not saved twice. State it without the number:
       "an identical review submitted again straight away".
   - **`approval-status`:** "Approval status" and "No approval decision", plus the history.
     No page has a control to change it.
   - **`assigning`:** "Assign to"; the list includes managers and admins; assignments are
     workflow metadata and never grant access.

   Figures, all `all`-audience: `assessments-list` in `list`, `assessment-card` in `cards`,
   `detail-evidence` in `detail`, `chat-drawer` in `chat`, `review-form` in `reviewing`.
2. **`pis.html`**, anchors `directory` and `detail`.
   - **`directory`:** the columns everyone sees. A staff block names the extra staff
     columns and filters, and links to `managing-pis#add-pi` and
     `managing-pis#profile-status`.
   - **`detail`:** what a reader sees (Research Profile, Details, Grants, Industry
     interest percentile and evidence list, confirmed Companies, Publications). A staff
     block lists the manager-only cards, with links to `managing-pis` and `pi-evidence`
     anchors.

   Figures: `pis-list-reviewer`, `pi-detail-reviewer`.
3. **`tests/unit/test_help_stated_constants.py`** (spec §6.3 rule 4). Read
   `src/services/assessment_reviews.py:120-165` for the names that hold the score bounds
   and the comment limit. If they are inline literals there, assert against the
   validation behaviour instead: call the validator with 0, 1, 5 and 6, and with comments
   of 10,000 and 10,001 characters. The test asserts two things:
   - the bounds are 1 and 5 and the limit is 10,000;
   - `templates/help/pages/assessments.html` mentions "1" to "5" and "10,000" in the
     `reviewing` section. Search the template source between `id="reviewing"` and the next
     `<h2`.

   If a later change moves either value, this test fails and the help text is updated.

**Acceptance:** same as P3a, plus `test_help_stated_constants.py` passes.

## P4a: Reference pages: managing-pis, pi-evidence (staff)

**Read first:**
- spec §6.3, §6.4 (rows `managing-pis`, `pi-evidence`), §7.1, §7.2, Appendix A.2;
- `templates/workspace/pis.html`, `templates/workspace/pi_detail.html`,
  `templates/pis/_profile_review.html`, `templates/pis/_bot_action.html`,
  `templates/pis/_revision_history.html`, `templates/workspace/slack_bots.html`,
  `templates/_profile_errors.html`;
- `src/routers/workspace/pi_directory.py`, `pi_profile.py`, `pi_evidence.py`,
  `pi_bots.py`;
- `src/services/pi_onboarding.py`, `agent_activation.py`, `agent_mute.py`,
  `profile_edit.py`, `profile_review.py`, `profile_jobs.py`, `pi_companies.py`,
  `email_verification.py`, `directory.py:174-309`;
- `src/routers/onboarding.py:114-175` (what sets Onboarding Complete);
- `src/routers/admin/agents.py:240-260, 520-565`;
- `docs/operations/pis-and-access.md`.

**Requirements:**
1. **`managing-pis.html`**, anchors in registry order. Every action in spec Appendix A.2
   that this page covers gets a `careful()` callout: what happens, and whether the UI can
   undo it.
   - **`add-pi`:**
     - the ORCID iD field and the name-only-if-ORCID-shows-none field;
     - adopting an account with no lab agent;
     - the error texts;
     - what is created.

     Callout: "creates a real PI account and queues profile jobs".
   - **`profile-status`:** No Profile / Generating / Complete / Pending draft, and the PI
     list's agent statuses: Not requested / Pending / Active / Muted / Suspended.
   - **`account`:** the Account card; email Verified/Unverified and **Mark verified**
     (callout); the cleaned-name note; Onboarding Complete, explained from what sets it;
     Claimed At; Joined.
   - **`edit-profile`:** the fields, the JHU tenure start (blank leaves it unchanged; never
     set means full career; a provisional year), that a tenure change regenerates, and
     that the form appears once a profile exists.
   - **`paper-review`:**
     - candidate papers: Accept / Reject, and Reject is permanent (callout);
     - unanchored papers: Keep / Exclude;
     - excluded papers: Restore.
   - **`drafts`:** Pending draft, Accept / Discard, and a stale draft.
   - **`regenerate`:** Regenerate (the disabled button states its reason; no numbers) vs
     Retry profile generation (only while the agent is pending).
   - **`persona`:** "Persona file out of date", Re-export, persona revisions.
   - **`jobs`:** the Jobs table.
   - **`slack-bot`:** Install Slack bot (callout: creates a Slack app), the return message.
   - **`activation`:**
     - Activate agent, and what the check refuses;
     - there is no override for managers ("ask an admin");
     - the green banner's wording, without a number.

     Callout: the bot goes live on the running simulation's roster.
   - **`mute`:** Mute / Unmute. Muted also covers an admin's park; Unmute re-runs the
     activation check.
   - **`slack-bots`:** the Slack Bots page; its actions land on the PI's page.

   Figures, all `staff`: `pis-list-manager` (add-pi), `pi-header-pending` (slack-bot or
   activation), `pi-account-edit` (account), `pi-paper-review` (paper-review), `slack-bots`
   (slack-bots).
2. **`pi-evidence.html`**, anchors in registry order:
   - **`reporter-identity`:** the statuses in plain words; "This is the PI", "PI has no
     RePORTER profile", "Unpin".
   - **`grants`:** NIH RePORTER awards; the tenure filter; "Not this PI" has no undo
     (callout).
   - **`orcid-fundings`:** the same veto, with a callout.
   - **`industry`:**
     - the cohort percentile, raw score and evidence rows;
     - rescoring, unscored and partial;
     - the evidence list and its veto (callout, no undo).
   - **`companies`:**
     - what reaches BlackbirdBot;
     - Confirmed and Suggested;
     - Add a company (fields);
     - Confirm and Reject (Reject is permanent unless re-added by hand; callout);
     - Delete (confirm dialog; recorded as rejected);
     - Find companies, which may wait for the daily extraction budget (no amount).

   Figures: `pi-grants`, `pi-industry`, `pi-companies`.

**Acceptance:** same as P3a.

## P4b: Reference pages: staff-accounts, discussions-activity, prompt-suggestions (staff)

**Read first:**
- spec §6.3, §6.4 (the three rows), Appendix A.2;
- `templates/workspace/staff.html`, `src/routers/workspace/staff.py`,
  `src/services/staff_accounts.py`;
- `templates/workspace/discussions.html`, `templates/discussions/_threads.html`,
  `src/routers/workspace/discussions.py`;
- `templates/workspace/activity.html`, `templates/workspace/activity_detail.html`,
  `templates/activity/_detail_body.html`, `src/routers/workspace/activity.py`;
- `templates/workspace/prompt_suggestions.html`,
  `templates/workspace/prompt_suggestion_detail.html`,
  `src/routers/workspace/prompt_suggestions.py`, `src/routers/reviews.py:540-638`;
- `src/services/directory.py` (the discussion and activity loaders).

**Requirements:**
1. **`staff-accounts.html`:**
   - **`add`:**
     - ORCID iD, Role, Name;
     - Preview, then Create account / Convert account, or Cancel;
     - managers create reviewers only;
     - converting a PI account, and when a manager is told to ask an admin;
     - "Something changed since the preview".

     Callout: creates an account with access granted.
   - **`revoke-restore-delete`:**
     - Revoke signs them out everywhere (callout);
     - a manager can revoke only a reviewer who has signed in;
     - Restore;
     - Delete is permanent and keeps reviews (callout).

     Take the wording from the page's confirm texts.
   - **`telling-new-staff`:** no notification is sent. Tell the person to sign in with the
     ORCID iD the account was created with, then open Help.
   - **`recent-changes`**

   Figures: `staff-form`, `staff-preview`, `staff-table`.
2. **`discussions-activity.html`:**
   - **`discussions`:**
     - the run selector;
     - the status cards (No Replies, Active, Proposal, No Proposal, Timeout);
     - filters;
     - thread rows and expanding a row;
     - specialist panel cards (signals, concerns, confidence, read state, "reply cut off");
     - export and the verbatim reply are admin-only ("only an admin can…").
   - **`activity`:** summary cards and the runs table.
   - **`run-detail`:**
     - cards, messages by agent and by channel, channels created;
     - the message timeline (metadata only, no message text);
     - admin-only extras named only as such.

   Figures: `discussions`, `activity`.
3. **`prompt-suggestions.html`:**
   - **`what-they-are`:** nothing ever edits a prompt file automatically.
   - **`generate`:**
     - "Generate suggestions from current reviews";
     - what counts as eligible (Learn feedback not yet consumed);
     - that a press is capped and pressing again continues (no number);
     - jobs run in the background;
     - refused while impersonating.
   - **`status`:** Open / Dismissed / Implemented; Reopen, Dismiss, Mark Implemented. The
     status is attribution only.
   - **`detail`:** Suggested Change, Raw model response, Distilled From, and Prompt Files
     Read (current / STALE / file missing).

   Figures: `prompt-suggestions`, `prompt-suggestion-detail`.

**Acceptance:** same as P3a.

## P5a: Reviewer tutorial (8 steps)

**Read first:**
- spec §6.3, §6.5 (reviewer table), §5.2;
- the registry `_REVIEWER_STEPS` in `src/web/help_pages.py`;
- `templates/help/_step_nav.html`;
- the same code sources as P3b, plus `templates/base.html` and `templates/settings.html`.

**Template skeleton (each step):**

```jinja
{% extends "help/_layout.html" %}
{% from "help/_figure.html" import figure %}
{% from "help/_callouts.html" import careful, note %}
{% block help_body %}
<p class="text-sm text-gray-600">Reviewer tutorial</p>
<h1>{{ step.title }}</h1>
<h2 id="where">Where</h2>
<p>…nav path…</p>
<h2 id="what-you-see">What you'll see</h2>
{{ figure(request, "<shot id>", "<caption>") }}
<h2 id="what-it-means">What it means</h2>
…
{% include "help/_step_nav.html" %}
{% endblock %}
```

- Steps with no shots omit "What you'll see".
- Step 1 omits "Where".

**Requirements:**
- Files `01-welcome.html`, `02-signing-in.html`, `03-assessments-list.html`,
  `04-assessment-card.html`, `05-assessment-detail.html`, `06-assessment-chat.html`,
  `07-recording-a-review.html`, `08-pi-pages-and-help.html`.
- Each covers its row of the spec §6.5 reviewer table, and renders **every** shot in its
  registry entry with `figure(request, "<id>", …)`, exactly that spelling.
- No step asks the reader to submit anything. Step 7 has a `note()` saying feedback is real
  review data, so the tutorial only shows the form.
- Each step links to the matching reference section with
  `page_url(request, 'help_page', slug='assessments', fragment='…')`, targeting only
  registry anchors.
- Step 8 links to Glossary and Troubleshooting.

**Acceptance:**
- P2's unit tests (shots present) pass.
- The rendered-page gate passes.
- The phase-6 reviewer journey walks steps 1–8 and Finish.

## P5b: Manager tutorial (10 steps)

**Read first:**
- spec §6.3, §6.5 (manager table), Appendix A.2;
- the registry `_MANAGER_STEPS`;
- the same code sources as P4a and P4b, plus the assessments pages.

**Requirements:**
- Files `01-your-role.html`, `02-workspace.html`, `03-pis-list.html`,
  `04-pi-lifecycle.html`, `05-profile-review.html`, `06-evidence.html`,
  `07-slack-bots.html`, `08-assessments.html`, `09-staff-accounts.html`,
  `10-discussions-activity-suggestions.html`.
- Use the P5a skeleton, with the eyebrow "Manager tutorial".
- Each covers its row of the spec §6.5 manager table. Each renders every registry shot.
- `careful()` callouts per Appendix A.2 wherever a step describes such an action (steps 3,
  4, 5, 6, 7, 9 and 10). No step asks the reader to perform one.
- Step 2 names the Workspace tabs without counting them.
- Step 8 links to the reviewer tutorial
  (`page_url(request, 'help_tutorial', tutorial='reviewer')`).
- Step 9 states that no notification is sent.
- Links to reference sections target registry anchors only.

**Acceptance:** same as P5a, for the manager journey.

## P6a: PI pages: labels, error sentences, help links, title rename

**Read first:**
- spec §6.6, §7.1, §7.2, §7.3 (the filter-form row), §7.4.4;
- `templates/workspace/pis.html`, `pi_detail.html`, `slack_bots.html`, `staff.html`;
- `templates/_profile_errors.html`;
- `src/routers/workspace/pi_profile.py`, `pi_evidence.py`, `pi_bots.py`,
  `pi_directory.py`;
- `src/services/profile_review.py:250-274`, `agent_mute.py:25-57`,
  `email_verification.py:34-37`, `profile_edit.py`;
- `src/routers/manager.py:114-125`;
- `tests/integration/test_reviewer_role.py:430-450`.

**Requirements:**
1. **`templates/pis/_detail_errors.html`** (new). Macro `pi_detail_error_text(code)`
   returns the sentence for every PI-detail code:
   - move today's `{% if %}` chain from `pi_detail.html:96-121` into it. Rewrite each
     condition to compare the macro's `code` argument: an imported macro has no `request`.
     The sentences stay word-for-word;
   - add `industry_busy`: "This PI's industry evidence is being refreshed — try the veto
     again in a moment.";
   - fall back to `profile_error_text(code)` (import `templates/_profile_errors.html`);
   - then fall back to "Something went wrong saving changes."

   `pi_detail.html`'s banner calls the macro.
2. **`pi_detail.html`:**
   - the title becomes `Workspace — {{ target_user.name }} — Blackbird`;
   - add `{{ help_link(request, effective_user, 'pis', 'detail') }}` in the page header,
     after `{% from "help/_link.html" import help_link %}`.
3. **`pis.html`:**
   - title "Workspace — PIs — Blackbird";
   - help link to `pis#directory`;
   - the filter `<form>` renders only `{% if permissions.staff %}` (move the condition
     outside the form);
   - the profile-status option label "Pending Update" becomes "Pending draft". The value
     `pending_update` is unchanged;
   - the profile-status badge uses an explicit label map: no_profile "No Profile",
     generating "Generating", complete "Complete", pending_update "Pending draft";
   - the agent badge uses an explicit label map: not_requested "Not requested",
     awaiting_token "Pending", active "Active", inactive "Muted", suspended "Suspended",
     anything else title-cased. Add an `inactive` colour entry matching Slack Bots' muted
     badge (`bg-gray-200 text-gray-700`).
4. **`slack_bots.html`:** title; help link to `managing-pis#slack-bots`; badge texts
   Active / Pending / Muted, other statuses `| title`.
5. **`staff.html`:** title; help link to `staff-accounts`.
6. **`tests/unit/test_pi_detail_error_codes.py`:**

```python
"""Every error code a PI-detail route can redirect with has its own sentence
(spec §7.2): an AST scan of the routers plus the service codes, rendered through the
macro the page uses."""
import ast
from pathlib import Path

import pytest

from src.web.templating import make_templates

ROOT = Path(__file__).resolve().parents[2]
ROUTERS = [ROOT / "src/routers/workspace" / n for n in
           ("pi_profile.py", "pi_evidence.py", "pi_bots.py", "pi_directory.py")]
GENERIC = "Something went wrong saving changes."
#: Codes returned by services the PI-detail routes pass through, and forwarded by
#: /manager/pis/{id}: keep in step with spec §7.2's list.
SERVICE_CODES = {
    "no_draft", "profile_generating", "draft_stale", "summary_too_long",
    "agent_not_mutable", "no_token", "activation_blocked", "no_spoke",
    "no_email", "email_changed",
    "invalid_email", "email_taken", "invalid_tenure_year", "profile_changed",
    "field_too_long", "profile_version_missing", "invalid_name", "too_many_tags",
    "tag_too_long", "tag_invalid", "nothing_to_save",
}


def _pi_detail_helpers(tree) -> set[str]:
    """Functions whose body builds a workspace_pi_detail URL."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(c, ast.Constant) and c.value == "workspace_pi_detail"
                   for c in ast.walk(node)):
                names.add(node.name)
    return names


def _router_codes() -> set[str]:
    codes = set()
    for path in ROUTERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = _pi_detail_helpers(tree) - {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith(("pi_", "workspace_"))}
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name in helpers:
                codes |= {a.value for a in call.args if isinstance(a, ast.Constant)
                          and isinstance(a.value, str) and a.value.islower()
                          and a.value != "workspace_pi_detail"}
            if name == "page_url" and any(isinstance(a, ast.Constant)
                                           and a.value == "workspace_pi_detail"
                                           for a in call.args):
                for kw in call.keywords:
                    if kw.arg == "query" and isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                            if isinstance(k, ast.Constant) and k.value == "error" \
                                    and isinstance(v, ast.Constant):
                                codes.add(v.value)
    return codes


def _text(code: str) -> str:
    module = make_templates().env.get_template("pis/_detail_errors.html").module
    return str(module.pi_detail_error_text(code)).strip()


def test_router_codes_are_listed_or_have_sentences():
    for code in _router_codes():
        assert _text(code) != GENERIC, code


@pytest.mark.parametrize("code", sorted(SERVICE_CODES | {"industry_busy"}))
def test_every_known_code_has_a_sentence(code):
    assert _text(code) != GENERIC


def test_unknown_code_falls_back():
    assert _text("definitely_not_a_code") == GENERIC
```

> Before relying on `_pi_detail_helpers`, read the four routers. Confirm that the helper
> names (`_review_redirect`, `_grants_redirect`, `_persona_busy_redirect`, …) are caught
> and that route handlers are not mistaken for helpers. Adjust the exclusion rule so that
> only the helpers that **take the code as an argument** count. Confirm that the codes
> `pi_directory.py` sends to `workspace_pis` (`create_failed`, `exists`, `agent_conflict`,
> `name_required`) are **not** collected.
>
> `SERVICE_CODES` must equal the set you enumerate from the services named in "Read
> first" and the code paths into `pi_detail.html`, minus the admin-only forwards. Correct
> the literal list above to match what the code returns. It is a starting list, not a
> fact.

7. **`tests/integration/test_reviewer_role.py:441`:** invert the assertion. For an admin
   impersonating a reviewer, `/workspace/pis` must **not** contain the filter form. Keep
   the surrounding test's intent and update its comment.
8. **`tests/integration/test_help_wording.py`** (spec §10.2 "§7 copy"; it covers P6b's
   templates too). Seed with `tests.factories`: PIs whose agents are `inactive`, `pending`
   (with and without a token) and `active`, and one PI with a staged draft. Assert:
   - **PI list as a manager:** has the badges "Muted", "Pending", "Active" and
     "Pending draft" and the filter option "Pending draft"; does not contain
     ">Inactive<", "Awaiting Token" or "Pending Update".
   - **PI list as a plain reviewer, and as an admin impersonating a reviewer:** no
     `<form method="get"` filter form.
   - **Slack Bots as a manager:** has "Muted" and "Pending".
   - **Assessment detail as a reviewer:**
     - contains "Approval status" and "No approval decision";
     - does not contain "Assign to" (reviewers have no assign form);
     - contains the reviewer Learn sentence "A manager decides when suggestions are
       generated.".
   - **Assessment detail as a manager:** contains "Assign to" and not "Assign reviewer";
     contains today's staff Learn sentence.
   - **Titles:** every `/workspace/...` page title ends "— Blackbird".

   Follow `tests/integration/test_staff_routes.py` for the client, setup commits and
   headers.

**Acceptance:**
- The new unit test passes.
- `test_reviewer_role.py` passes.
- The rendered-page gate passes.
- Grepping these four templates finds no "CoPI".

## P6b: Assessment and other workspace pages, settings

**Read first:**
- spec §6.6, §7.3, §7.4.4;
- `templates/workspace/assessments.html`, `assessment_detail.html`, `discussions.html`,
  `activity.html`, `activity_detail.html`, `prompt_suggestions.html`,
  `prompt_suggestion_detail.html`;
- `templates/assessments/_detail_body.html:1060-1295`;
- `templates/settings.html`.

**Requirements:**
1. **Help links**, each imported with `{% from "help/_link.html" import help_link %}` and
   placed in the page header, with targets per spec §6.6:
   - `assessments.html` → `assessments#list`;
   - `assessment_detail.html` → `assessments#detail`;
   - `discussions.html` → `discussions-activity#discussions`;
   - `activity.html` → `discussions-activity#activity`;
   - `activity_detail.html` → `discussions-activity#run-detail`;
   - `prompt_suggestions.html` → `prompt-suggestions`;
   - `prompt_suggestion_detail.html` → `prompt-suggestions#detail`;
   - `settings.html` → `getting-started#settings`. The macro hides it for PIs.
2. Every "CoPI" in these 8 templates becomes "Blackbird".
3. **`_detail_body.html`:**
   - **Status block** (`:1070-1125`):
     - the heading "Status" becomes "Approval status";
     - "Unreviewed" becomes "No approval decision";
     - update the `{# #}` comment above it to match.
   - **Learn help** (`:1240-1252`): keep today's sentence for staff. For everyone else
     write: "Choosing **Learn** marks this feedback as eligible for the prompt-suggestion
     bot and nothing more. A manager decides when suggestions are generated."
     - Branch on the same staff test the partial already uses for assignment
       (`effective_user.is_staff`).
     - Keep the file's no-absolute-manager-path rule.
   - **Assign label** (`:1288`): "Assign reviewer" becomes "Assign to". Keep `for="assign-user"`.

**Acceptance:**
- The rendered-page gate passes.
- P11's render test finds every link.
- `test_assessment_review_ui.py` and the other assessment tests pass, updated only if they
  assert the replaced strings. Search for them with
  `rg -n "Assign reviewer|>Status<|Unreviewed</span>" tests/` and report the hits;
  P6b does **not** own those test files.

## P7a: Sign-in, access, delete-account copy, and the chat record line (D17)

**Read first:**
- spec §7.4.1–§7.4.3, §7.3 (the D17 row), §10.2 (§7 copy);
- `templates/login.html`, `templates/access_pending.html`,
  `templates/profile/delete_account.html`;
- `src/routers/auth.py:160-176, 330-347`, `src/routers/public.py`,
  `src/routers/profile.py:195-239`;
- `src/services/user_deletion.py` (whole file);
- every `ForeignKey("users.id"` in `src/models/` (`rg -n 'ForeignKey\("users.id"' src/models`),
  with its `ondelete`;
- `src/services/staff_accounts.py:416-480`;
- `src/services/assessment_chat_record.py:700-750`;
- `tests/unit/test_assessment_chat_record.py:370-400`;
- `tests/integration/test_assessment_chat_parity.py`.

**Requirements:**
1. **`login.html`:**
   - the intro (`:17-20`) becomes "We use ORCID to securely identify you. For PIs, your
     ORCID record also seeds your research profile.";
   - "How it works" becomes "How it works for PIs";
   - add a second info box, styled like the first:

     > **Managers and reviewers:** sign in with the ORCID iD your account was created
     > with{% if microsoft_enabled %}, or with Microsoft once you have linked it in
     > Settings{% endif %}. You'll land in the Workspace, or on the page you were trying to
     > open; the Help tab has a tutorial for your role.
2. **`access_pending.html`:**
   - the title becomes "Access pending — Blackbird", and "CoPI is in pre-release" becomes
     "Blackbird is in pre-release";
   - when `pending_info.access_status == 'denied'` and `pending_info.user_role` is in
     `('admin', 'manager', 'reviewer')`, render this instead of the whole body:
     - `<h1>Access turned off</h1>`;
     - for a reviewer: "Your access to Blackbird has been turned off. Ask a manager or an
       admin to restore it.";
     - for a manager or admin: "Your access to Blackbird has been turned off. Ask an admin
       to restore it.";
     - the "← Back to home" link;
     - no email box.
   - Every other case keeps today's copy, including a payload without the keys. The page
     takes no auth dependency.
3. **`profile/delete_account.html`:**
   - the title becomes "Delete Account — Blackbird";
   - when `not effective_user.may_use_pi_surfaces`, replace the PI list (`:10-21`) with the
     staff consequences.
     - Derive every clause from `delete_user_account(db, user)` with its default
       `remove_from_allowlist=False`, and from each users-FK `ondelete` rule.
     - **Never** say the ORCID iD is removed from the allowlist.
     - If the facts differ for a manager and a reviewer (for example, attribution columns
       only managers fill), branch on `effective_user.is_manager`.
   - Cancel links to `/settings` when `not effective_user.may_use_pi_surfaces`, and to
     `/profile/edit` otherwise.
4. **`src/services/assessment_chat_record.py`** (D17):
   - the cleared text becomes
     `"No approval decision (the last approve or disapprove was cleared)"`;
   - the line becomes `doc.add(f"Approval status — {text}", anchor="review")`;
   - the approved, disapproved and unknown texts are unchanged.

   Update `tests/unit/test_assessment_chat_record.py:388` to
   `"[Approval status — Approved by Manny Manager]"`. If
   `tests/integration/test_assessment_chat_parity.py` matches the literal "Review status"
   or "Unreviewed", report it: P7a owns this one test file too, so update it.
5. **`tests/integration/test_help_copy.py`:**
   - **access-pending:**
     - "Access turned off … Ask a manager or an admin" for a denied reviewer;
     - "… Ask an admin" for a denied manager;
     - today's copy for a pending PI;
     - today's copy for a session whose `pending_access` lacks the new keys.

     Set the session with `tests.session_support.raw_session_headers({"pending_access": {...}})`.
   - **delete-account:** a reviewer sees the staff consequences, no "lab agent", and Cancel
     to `/settings`. A PI sees today's list.
   - **login:** both info boxes render. The Microsoft clause appears only when
     `app.state.microsoft_sign_in` is set.
   - **chat record:** the cleared status renders "Approval status — No approval decision …".
     Reuse the fixtures in `test_assessment_chat_record.py` if it is a unit test there;
     otherwise assert it in that file.

**Acceptance:** `test_help_copy.py`, `test_assessment_chat_record.py` and
`test_assessment_chat_parity.py` pass.

## P7b: Mechanical rename "CoPI" → "Blackbird" (lathe:mechanical-worker)

**Files (22):**
- `templates/admin/access_requests.html`, `admin/agent_detail.html`, `admin/agents.html`,
  `admin/cohort_detail.html`, `admin/cohort_topology.html`, `admin/cohorts.html`,
  `admin/jobs.html`, `admin/llm_calls.html`, `admin/simulation.html`,
  `admin/user_detail.html`, `admin/users.html`;
- `templates/agent/conversations.html`, `agent/dashboard.html`, `agent/listing.html`,
  `agent/public_profile.html`, `agent/request.html`;
- `templates/discussions/export.html`;
- `templates/invite/accept.html`, `invite/error.html`;
- `templates/onboarding/profile_review.html`;
- `templates/profile/edit.html`, `profile/view.html`;
- plus create `tests/unit/test_no_copi_in_templates.py`.

**Pattern:**
- Replace the exact token `CoPI` with `Blackbird` everywhere in these files, in titles and
  body text alike.
- Then reread each changed line. A sentence must still read correctly. For example
  "Welcome to CoPI!" becomes "Welcome to Blackbird!" and "Where CoPI sends collaboration
  proposals" becomes "Where Blackbird sends collaboration proposals".
- Report any line where the result reads wrongly, and leave that line unchanged.

**Test:**

```python
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def test_no_template_says_copi():
    hits = [f"{p.relative_to(TEMPLATES)}:{i}" for p in TEMPLATES.rglob("*.html")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "CoPI" in line]
    assert not hits, hits
```

**Deterministic check (allowed for this package only):** afterwards,
`rg -c CoPI <the 22 files>` prints nothing.

## P9: Screenshot tooling and browser phase 6

**Read first:**
- spec §8 (all), §10.3, Review Focus 5;
- `tests/e2e/ui_audit/` (all of `env.py`, `postgres.py`, `harness.py`, `run.py`,
  `serve.py`, `seed.py:1-120, 380-411`, `journeys_phase5.py`, `journeys_phase3.py`
  `_clean`/`_drain_csp`/`_open`, `crawl.py`, `README.md`);
- `tests/factories.py`;
- `tests/fakes.py`;
- `tests/assessment_chat_support.py`;
- every outbound fetcher a shot can reach: `src/services/orcid.py`, `nih_reporter.py`,
  `openalex.py`, `pubmed.py`, and `staff_accounts.py:250-265`;
- `src/services/pubmed.py:509-527` (MOD 11-2).

**Requirements:**
1. **`tests/e2e/ui_audit/env.py`:** add `"help_assets"` to `LINKED`, and update the comment.
2. **`tests/e2e/help_shots/seed.py`.** Inside `main()`, not at import (as
   `ui_audit/seed.py:227-229` does, because `test_help_shots.py` imports this module), it
   refuses unless `DATABASE_URL` ends with `/` + `DB_NAME`. It prints seed ids as JSON on the last stdout line, like the ui_audit
   seed. Contents, per spec §8.1:
   - **Names:** invented only, from a fixed list in the module.
   - **ORCID iDs:** `0000-0002-` followed by digits, with a deliberately **wrong** MOD 11-2
     check digit. A helper `_bad_checksum_orcid(n)` computes the right digit with the same
     algorithm as `pubmed.py:509-527` and emits a different one. A module-level assert
     checks that none passes the checksum.
   - **Emails:** `@example.org`.
   - **Accounts:**
     - `manager` and `reviewer`, each with a `help_intro_dismissals` row;
     - `new_reviewer` without a row;
     - an `admin` (not used for shots).
   - **PIs:** every profile and agent state, including pending with and without a token
     and muted; one fully populated PI; at least four scored PIs for the industry
     percentile.
   - **A run** with assessments that fill every detail section.
   - **Review data:** feedback in both modes; an assignment of the reviewer.
   - **Discussion threads** with decisions.
   - **One prompt suggestion.**
   - **Staff accounts** in the approved, pending and revoked states.

   Build rows with `tests/factories.py`. Reuse the ui_audit seed's row shapes where they
   exist (its phase 3/4/5 helpers), but with benign text.
3. **`tests/e2e/help_shots/serve.py`:** modelled on `ui_audit/serve.py`.
   - It refuses unless `DATABASE_URL` names the harness database.
   - Its `FakeAsyncAnthropic` script has a benign answer with one citation
     (`tests.assessment_chat_support.citation`).
   - It monkeypatches every outbound fetcher a shot's preparation reaches, to return
     invented records: `staff_accounts.fetch_orcid_profile`, `pi_profile.fetch_pubmed_records`,
     and the RePORTER, OpenAlex and ORCID-fundings fetchers if any shot route calls them.
     Enumerate them from the code.
4. **`tests/e2e/help_shots/shots.py`:** no Playwright import at module scope, because
   `pyproject.toml` says nothing under `tests/` imports it, and `test_help_shots.py`
   imports this module. Type hints may name Playwright types only under `TYPE_CHECKING`.
   `@dataclass(frozen=True) class Shot` has these fields:
   - `id`, `audience`, `account` (a seed key);
   - `path`: `Callable[[dict], str]` over the seed ids;
   - `prepare`: async callable `(page) -> None`, default no-op;
   - `clip`: CSS selector or None;
   - `alt`;
   - `sources`: tuple of repo-relative template paths depicted.

   `SHOTS` covers exactly the 29 ids in spec §8.2: the 27 that tutorial steps register in
   `src/web/help_pages.py`, plus `detail-evidence` and `prompt-suggestion-detail`, which
   only reference pages use. Rules:
   - `all` shots use `reviewer`, or `new_reviewer` for `intro-prompt`;
   - `staff` shots use `manager`;
   - `topbar-help` clips `nav[aria-label="Main"]`;
   - `staff-preview` posts the Staff form for a new invented iD (the stubbed fetch
     answers);
   - `chat-drawer` opens the drawer and waits for the scripted answer;
   - `detail-*` and `card-rubric-gating` open their disclosures.
5. **`tests/e2e/help_shots/capture.py`:** CLI
   `python -m tests.e2e.help_shots.capture [--only ID]... [--executable PATH] | --check`.
   - **Capture mode:**
     - start a stack modelled on `ui_audit/run.py::Stack`, with this package's seed and
       serve modules;
     - before importing anything from `src`, run `os.chdir(stack.work)` and
       `os.environ.update(stack.env())`, exactly as `ui_audit/run.py:148-150` does. The
       repo root holds the production `.env`, and the session-cookie name must match the
       server's;
     - start a **local recorder** on a free 127.0.0.1 port: a TCP listener that records
       every connection attempt and closes it;
     - put `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` set to `http://127.0.0.1:<port>` and
       `NO_PROXY=127.0.0.1,localhost` **only in the serve subprocess's env**;
     - forge cookies per account (as `Harness.cookie_for` does);
     - for each shot, open a 1280×900 page, go to the path and run `prepare`;
     - take the screenshot: the element named by `clip` when set, otherwise **the viewport
       only, never `full_page`**. Long pages would break the 4 MB cap and make very tall
       figures, so give every shot of a long page a `clip`;
     - save it to `REPO_ROOT / "help_assets/shots/<id>.png"`;
     - write the manifest entry: `audience`, `alt`, `width`, `height` (read from the PNG
       IHDR), `sources`, `commit` (`git rev-parse HEAD` in `REPO_ROOT`);
     - at the end, **exit 1 if the recorder saw any connection**, and print them.
   - **`sources` hashing:** sha256 of each depicted template **plus its full
     `extends`/`include`/`import` closure** (parse Jinja with
     `jinja2.Environment(loader=FileSystemLoader(REPO_ROOT / "templates"))` and
     `jinja2.meta.find_referenced_templates`, recursively), plus `static/css/app.css`,
     `prompts/rubric/blackbird-rubric.toml` and `tests/e2e/help_shots/seed.py`.
   - **The shot's own entry** is hashed under the key `shots.py#<id>`. Hash the sha256 of
     `json.dumps({"id", "audience", "account", "clip", "alt", "sources"}, sort_keys=True)`
     plus `inspect.getsource(shot.path)` plus `inspect.getsource(shot.prepare)`.
     - **Never** hash `repr(shot)`: a callable's repr contains its memory address, which
       changes on every run, so every shot would read as stale.
     - Mark the callables `field(repr=False, compare=False)`.
     - Verify by running `--check` twice in fresh processes with no edits; both must exit 0.
   - **`--check` mode:** no Docker, no browser. Recompute every entry's hashes. List stale
     and missing shots (in `SHOTS` but not in the manifest or on disk). Exit 1 if any.
   - `--only` recaptures named shots and leaves the other manifest entries as they are.
6. **`tests/e2e/help_shots/README.md`:**
   - prerequisites: Docker, a Chromium headless shell, and a venv with the project's deps;
   - the commands;
   - that it runs on a workstation, not the host;
   - the outbound recorder;
   - that capture is the last step before a deploy;
   - commit the PNGs and the manifest together.
7. **`tests/unit/test_help_shots.py`** (no browser, no Docker):
   - every `figure(request, "<id>"` in `templates/help/**/*.html` has a manifest entry and
     a PNG on disk;
   - no PNG under `help_assets/shots/` lacks a manifest entry;
   - every entry has non-empty alt;
   - each PNG's IHDR width and height equal the manifest's;
   - templates whose registry page or step audience is `all` reference only `all` shots;
   - the PNGs total at most 4 MB;
   - every `Shot` in `shots.py` with audience `all` uses an account whose seeded
     `user_role` is `reviewer`, and every `staff` shot one whose role is `manager`. Read
     the seed module's declared account roles, so expose them as a module-level dict
     `ACCOUNT_ROLES` in `seed.py`;
   - `set(s.id for s in SHOTS)` equals the set of ids the templates reference.

   This test will fail until Task 15's capture runs. That is expected.
8. **`tests/e2e/ui_audit/journeys_phase6.py`.** `JOURNEYS` list, with helpers reused from
   phase 5 or phase 3. **Every journey creates its own fresh account(s) with no dismissal
   row**, through the harness env as other journeys seed. Journeys never share an account,
   so the result does not depend on their order.
   - `journey_reviewer_tutorial`:
     - a reviewer with no dismissal row (create one through the harness env, as other
       journeys seed);
     - the prompt shows on `/workspace/assessments`;
     - click "Start the tutorial";
     - walk Next through step 8; each "Open this page" href GETs 200;
     - click Finish;
     - the flash shows "Tutorial finished." on `/help`;
     - the prompt is gone on `/workspace/assessments`.
   - `journey_manager_tutorial`: the same over 10 steps. Then each §6.6 page's "Help with
     this page" link lands on an element whose `id` equals the fragment.
   - `journey_dismiss_returns`: a fresh reviewer clicks "Don't show again" on
     `/workspace/assessments?review=all` and is back on that exact URL with no prompt.
   - `journey_help_narrow_layout`:
     - at 375 px, for reviewer and manager, every help page, every step, and
       `/workspace/assessments` and `/workspace/pis` with the prompt;
     - `document.documentElement.scrollWidth <= window.innerWidth`;
     - zero axe violations (`h.axe_source`);
     - no console errors, no unexpected network.
9. **`tests/e2e/ui_audit/journeys_phase5.py:79, 93`.** The test collects
   `main form button` as the seeded reviewer and asserts that list is empty. The prompt's
   "Don't show again" form now renders inside `<main>` for that account, because it has
   no dismissal row.
   - Exclude `form[action="/help/intro/dismiss"]` from that selector, for example
     `main form:not([action="/help/intro/dismiss"]) button`.
   - Change nothing else in the file.
   - Then read phases 0–4's journeys for any other assertion the prompt breaks: a
     `main form`, `main a`, a first-element or a count assertion on workspace pages viewed
     as the seeded manager or reviewer. Report each one. P9 may edit only phase 5.
10. **`tests/e2e/ui_audit/README.md`:** add phase 6, and the baseline rule from spec §10.3.
   Reports must be equal on violations, overflow and axe. The `3bafb292` baseline comes
   from a separate export, never from checking out the mounted checkout.

**Acceptance:**
- `test_help_shots.py` passes after capture.
- `capture --check` exits 0 after capture.
- Phase 6 passes.
- The recorder saw zero connections.

## P11: Test-gate registrations and the rendering link test

**Read first:**
- spec §6.6, §10.1, §10.2;
- `tests/unit/test_reachability.py:100-170`;
- `tests/integration/test_rendered_page_gate.py:60-140, 300-420`;
- `tests/integration/test_workspace_permissions_matrix.py` (whole file);
- `tests/unit/test_docker_build_context.py:1-60`;
- `tests/integration/test_manager_access.py` (`auth_headers`).

**Requirements:**
1. **`test_reachability.py` → `ROUTE_ALLOWLIST`:** add `("GET", "/help/shots/{shot_id}.png")`
   with the reason: "Help screenshots are linked by `<img src>` from the figure macro
   (templates/help/_figure.html); the gate credits href/action/fetch, not img src." If the
   gate turns out to credit it, the stale-entry test will say so; then remove the entry.
2. **`test_rendered_page_gate.py`:**
   - `PAGES` gains:
     - `"/help": lambda w: "/help"`;
     - `"/help/{slug}": lambda w: "/help/assessments"`;
     - `"/help/tutorials/{tutorial}/{step}": lambda w: "/help/tutorials/reviewer/1"`;
   - `SKIPPED` gains:
     - `"/help/tutorials/{tutorial}": "redirects to step 1"`;
     - `"/help/shots/{shot_id}.png": "PNG image, renders no HTML page"`.
   - Do **not** add anything to `NO_200_EXPECTED`. Help answers 200 for three roles.
3. **`test_workspace_permissions_matrix.py`:** add a separate test,
   `test_help_routes_follow_effective_role`, parametrized over
   reviewer / manager / admin / pi / admin-impersonating-reviewer /
   admin-impersonating-manager / admin-impersonating-pi. It GETs `/help`,
   `/help/managing-pis` and `/help/tutorials/manager/1` and asserts:
   - reviewer: 200, 403, 403;
   - manager and admin: 200, 200, 200;
   - pi: 403 on all three;
   - impersonations follow the effective role.

   Do not add `/help` to the existing `/manager`-twin loop.
4. **`test_docker_build_context.py`:** add `"help_assets"` to `MUST_KEEP`.
5. **`tests/integration/test_help_links_render.py`.** Reuse the `gate_world` fixture by
   importing it from `tests.integration.test_rendered_page_gate`, or factor the minimum it
   needs. For each viewer in (reviewer, manager, admin) and each Microsoft setting
   (`app.state.microsoft_sign_in = None` / a sentinel; restore it after):
   1. Collect pages: `/help`, every `pages_for` slug, every step of every
      `tutorials_for` tutorial, and the §6.6 pages (`/workspace/pis`,
      `/workspace/pis/{pi}`, `/workspace/assessments`, `/workspace/assessments/{id}`,
      `/settings`, plus `/workspace/slack-bots`, `/workspace/staff`,
      `/workspace/discussions`, `/workspace/activity`, `/workspace/activity/{run}`,
      `/workspace/prompt-suggestions` and `/workspace/prompt-suggestions/{id}` for staff).
   2. GET each page. Parse every `href` starting `/help/` (a `/help/{slug}` path with an
      optional `#frag`).
   3. GET the target as the same viewer with `follow_redirects=True`: it must end at 200.
      `/help/tutorials/<key>` redirects to step 1. If there is a fragment, the final HTML
      must contain `id="<frag>"`.
   4. For **every help page and step HTML it renders** (index, each page, each step), per
      viewer and per Microsoft setting, assert `page_problems(html) == []`. Import
      `page_problems` from `tests.integration.test_rendered_page_gate`. The rendered-page
      gate's `PAGES` walks only three help routes, so this is where the other
      reference pages and steps get the heading, nav-name, accessible-name and grey-text
      checks.
   5. Assert that each §6.6 page renders the specific link listed in spec §6.6 for that
      viewer (`help_link` targets).
   6. Assert that `/settings` as a PI has no `Help with this page` link.

   **Lint:** import the `gate_world` fixture with `# noqa: F401` (ruff runs on `tests/` with
   zero findings allowed), and use `zip(..., strict=True)` wherever you zip.

   This test needs the committed manifest and PNGs, since help pages render figures. It
   passes only after Task 15's capture.

**Acceptance:** all five pass in Task 15.

---

## Task 15: Integrate and verify (main session)

- [ ] **Merge the reports.** Collect every package report, reconcile any conflicting
  assumptions by editing directly, and diff the tree against the ownership table.
  ```bash
  cd /home/a/mounts/ubuntu/blackbird-copi-science && git status --porcelain
  ```
  A file changed outside its package is a defect. Fix or revert that hunk. Never touch
  `docker-compose.prod.yml`, `.env*` or `docs/audits/`.
**Host commands.** `HOST` means the production host. Its address and SSH user are in the
operator's private notes (the session memory "Blackbird host & DB access"). They are
deliberately not written in this committed plan, because `origin` is public. Every host
command below runs as:

```bash
ssh -4 -o StrictHostKeyChecking=yes "$HOST" 'cd ~/blackbird-copi-science && …'
```

- **Strict checking aborts on a changed host key** (open-findings row R-ssh-host-key).
  Never accept a new key. Retry once after a minute. If it is still refused, stop and tell
  the owner.
- **Never run a deploy or `docker` command against the workstation's own Docker daemon**,
  which runs unrelated containers.

- [ ] **Rebuild the CSS on the host**, where the pinned Tailwind binary runs:
  `scripts/build_css.sh && scripts/build_css.sh --check`. If `text-inherit` is reported
  unknown, apply the T0 fallback and rerun.
- [ ] **Apply the `CLAUDE.md` hunks** (spec §11, D16).
  1. Write `$SCRATCH/claude-md.patch` against `git show HEAD:CLAUDE.md`, holding exactly two
     hunks:
     - a table row **appended after the last existing row** of the "Before you… | read"
       table: `| change a workspace, settings or login page, or anything under \`templates/help/\` | \`docs/operations/help-content.md\` |`.
       Appending keeps the patch context away from the owner's edited lines 10-11;
     - `help_assets` inserted into the `git status --porcelain … -- src templates static prompts alembic scripts help_assets pyproject.toml alembic.ini` line.
  2. Run `git apply --check --cached` on it, then `git apply --cached`, then `git apply`.
  3. Confirm that `git diff --cached CLAUDE.md` shows only those two hunks and that
     `git diff CLAUDE.md` shows only the owner's pre-existing edits.
  4. **Never run `git add CLAUDE.md`.**
- [ ] **Build the workstation harness venv** (scratch, never inside the mount):
  ```bash
  uv venv --python 3.12 $SCRATCH/uiaudit-venv && uv pip install --python $SCRATCH/uiaudit-venv/bin/python -r pyproject.toml --extra dev
  ```
- [ ] **Capture the screenshots** (workstation; Docker plus the cached headless shell):
  ```bash
  cd /home/a/mounts/ubuntu/blackbird-copi-science && PYTHONPATH=$PWD PYTHONDONTWRITEBYTECODE=1 $SCRATCH/uiaudit-venv/bin/python -m tests.e2e.help_shots.capture --executable ~/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell
  ```
  Expect 29 PNGs, a full manifest, and the recorder reporting zero connections. Then:
  - open every PNG with the Read tool. **Look for real names, raw markup, error pages and
    empty states.** Any such shot is a seed or prepare defect: fix it and recapture;
  - run `capture --check`, which must exit 0.
- [ ] **Run the full gate on the host** (background, logged to a file):
  `ENGINE_REFUSAL_FALLBACK_ENABLED=false nohup ./scripts/ci.sh > /tmp/ci-help.log 2>&1 &`.
  - The variable is set explicitly because `docs/operations/testing.md` does not mention
    it, and `test_prompt_freeze_gm` fails without it.
  - Wait on the log's last line. Do not use `pgrep -f`, which matches its own ssh command
    line; use a bracket pattern (`'[s]cripts/ci.sh'`).
  - On failure, dispatch `lathe:test-triage` with the log path, repair, rerun the failing
    test, then the full gate.
- [ ] **Run the browser phases on the workstation.**
  1. Export the baseline tree:
     ```bash
     mkdir -p $SCRATCH/base && git -C /home/a/mounts/ubuntu/blackbird-copi-science archive 3bafb292 | tar -x -C $SCRATCH/base
     ```
  2. Run phase 5 there with `--out $SCRATCH/phase5-base.json` (`PYTHONPATH=$SCRATCH/base`,
     same venv and executable).
     - **This run exits 1 by design**: its default `--baseline` file is missing
       (`run.py:32-35, 243`).
     - Use only its report file. Confirm that its journeys and crawl themselves passed.
  3. Run phases 0–4 and phase 6 at the feature tree.
  4. Run phase 5 with `--baseline $SCRATCH/phase5-base.json`.
  5. Every phase must pass. Phase 5 must report no new violation, overflow or axe finding.
- [ ] **Self-check:**
  - `git status --porcelain` lists only:
    - plan-owned paths;
    - the three files that were dirty beforehand (`CLAUDE.md`, `docker-compose.prod.yml`,
      `docs/audits/open-findings.md`), left as they were apart from `CLAUDE.md`'s staged
      hunks;
    - the untracked files that were present beforehand.
  - Compare against the session-start status.

## Task 16: Adversarial audits (one batch), fixes, re-verify

- [ ] Dispatch in one message:
  - `lathe:plan-auditor` with this plan, the spec and the full diff;
  - `lathe:semantic-reviewer` with the diff of `src/` and `templates/base.html`;
  - `lathe:security-reviewer` with the diff of `src/routers/help.py`, `src/web/help_intro.py`,
    `src/dependencies.py`, `src/main.py` and `templates/access_pending.html`. Supplied
    facts: public origin behind org1's nginx, `OriginGuardMiddleware`, enforced CSP;
  - **three `lathe:auditor` content audits (spec §10.4), model `opus`**, each with spec
    §6.3 and Appendix A. Each checks its pages for claims the code contradicts,
    undocumented reachable actions, forbidden numbers, real names and repository
    internals:
    1. `getting-started`, `how-it-works`, `glossary`, `troubleshooting`, `assessments`,
       `pis` and the reviewer tutorial;
    2. `managing-pis`, `pi-evidence` and manager steps 1–7;
    3. `staff-accounts`, `discussions-activity`, `prompt-suggestions` and manager steps
       8–10.
- [ ] Merge the reports and drop duplicates, recording why. Fix every confirmed finding.
  Rerun the affected tests, then the full gate. **Rerun every browser phase (0–6)** if any
  fix touched a template, CSS or route. Re-audit only the fixed areas.

## Task 17: Final capture and commit

- [ ] Run `capture --check`. If anything is stale:
  - recapture it (`--only` or all) and re-read the PNGs;
  - rerun `tests/unit/test_help_shots.py` and `test_help_links_render.py` on the host;
  - rerun browser phase 6.
- [ ] **Stage** the plan's files explicitly by path.
  - Never run `git add -A`, and **never `git add CLAUDE.md`**: its two hunks are already
    staged by `git apply --cached`.
  - Leave `docker-compose.prod.yml`, `docs/audits/`, the owner's `CLAUDE.md` edits, and the
    untracked `docs/plans`/`docs/specs` files from earlier work unstaged.
  - Exception: this spec and this plan **are** staged. Before staging them, replace any
    host address in them with `HOST`.
- [ ] **Immediately before committing**, run `git diff --cached CLAUDE.md`: it shows exactly
  the two hunks. Then run `git diff --cached --stat`: it lists only plan-owned paths.
- [ ] Commit on `blackbird`.
  - Check `git config user.name` and `git config user.email` first. The repo's commits are
    authored `alan`.
  - Message: `feat(help): Help tab, staff documentation and onboarding tutorials`, plus a
    body and the attribution lines.
  - Never push.
- [ ] **After the commit**, this must print nothing:
  ```bash
  git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts help_assets pyproject.toml alembic.ini
  ```
  This is the root `CLAUDE.md` pre-build check, and it runs after the commit.

## Task 18: Deploy (spec §11)

**Every command in this task runs on the host** through the Task 15 ssh form, from
`~/blackbird-copi-science`. None of them runs on the workstation.

- [ ] Read `docs/operations/host-and-simulation.md` and the new 0067 box first.
- [ ] Count the assessments that have an approval event:
  `SELECT count(DISTINCT assessment_id) FROM assessment_review_events`.
  - D17 changes the chat record digest for these assessments, so their saved chats show a
    "record changed" notice (`src/services/assessment_chat.py:323`).
  - Report the count.
- [ ] Check that the simulation has no live run. Read `/admin/simulation` state through SQL
  or the page as the doc instructs, and note the result.
- [ ] Tag the rollback images, then build, then tag the new app image:
  ```bash
  DC="docker compose -f docker-compose.prod.yml"
  for s in blackbird-app worker; do docker image tag "$(docker inspect -f '{{.Image}}' "$($DC ps -q $s)")" copi-blackbird-$s:rollback-pre-help; done
  docker image tag "$(docker inspect -f '{{.Image}}' "$($DC --profile agent ps -q agent)")" copi-blackbird-agent:rollback-pre-help
  $DC build blackbird-app worker && $DC --profile agent build agent
  docker image tag copi-blackbird-blackbird-app:latest copi-blackbird-blackbird-app:help-0067
  ```
  Each `ps -q` must be non-empty. Confirm that the agent container is this stack's
  (`com.docker.compose.project` = `copi-blackbird`) before tagging.
- [ ] Confirm the worker is idle: no `jobs` row with `status='processing'` and type
  `generate_profile`. Otherwise wait.
- [ ] Run `./scripts/migrate/run_migration.sh` (rehearse), read its output, then run it with
  `--apply`. Expect `alembic current` = `0067`.
- [ ] Run `$DC up -d blackbird-app worker`. Then run `$DC up -d agent` **only** if no live
  run; otherwise leave the agent on its old image and report it.
- [ ] **Smoke test:**
  - `/api/health` is 200;
  - on the live site, as an anonymous visitor (`curl`), `/help` redirects to
    `/login?next=%2Fhelp`;
  - in the container, the manifest the image ships is checked:
    `docker exec copi-blackbird-app-1 python -c "from src.web.help_pages import shot_ids, shot_path; ids = shot_ids(); print(len(ids), all(shot_path(i).is_file() for i in ids))"`
    prints `29 True`;
  - `docker exec copi-blackbird-app-1 python -c "import src.routers.help"` succeeds;
  - the container logs show no traceback.

  Do **not** start the simulation.

---

## Self-review against the spec

| Spec | Covered by |
|---|---|
| §4.1 | P2 step 5 |
| §4.2 | P2 step 2, T0 |
| §4.3 | P2 step 5, T0 `navigation_for` |
| §4.4 | P2 step 6 |
| §4.5 | P2 `_context` |
| §5.1 | T0 model, P1 |
| §5.2 | P2 steps 3, 4, 5 |
| §5.3 | P2 step 2 |
| §6.1–§6.2 | T0 |
| §6.3 | Global Constraints, P3–P5 |
| §6.4 | P3a, P3b, P4a, P4b |
| §6.5 | P5a, P5b |
| §6.6 | P6a, P6b, P11 render test |
| §7.1 | P6a |
| §7.2 | P6a |
| §7.3 | P6a (filter form), P6b, P7a (chat record) |
| §7.4 | P7a, P7b, P6a/P6b titles |
| §8.1–§8.4 | P9, P2 (route), T0 (manifest, lazy loading), P2 (`_login_location`) |
| §9 | ownership table |
| §10.1–§10.3 | P2, P6a, P7b, P9, P11, Task 15 |
| §10.4 | Task 16 |
| §10.5 | Tasks 15 and 17 |
| §11 | P1 docs, Task 15 (`CLAUDE.md`), Tasks 17 and 18 |
| §12 | no task (follow-ups) |
