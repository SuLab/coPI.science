# Help tab, staff documentation and onboarding tutorials: design

- **Status:** revision 3, 2026-10-14. **Uncommitted**, following the repo's convention for
  specs under review.
  - The decisions in §2 and the design in §4–§10 were approved section by section in
    brainstorming the same day.
  - Revision 2 folds in two read-only reviews of revision 1: a fact-check of every code
    claim, and an adversarial audit of security, deploy, completeness and feasibility.
  - Revision 3 folds in the adversarial re-audit of revision 2's repairs: the rollback image
    tags, the error-code AST scan, shot roles, the worker restart, the baseline rule, the
    `CLAUDE.md` patch procedure, the outbound-call recorder, and the capture ordering.
  - The owner approved the spec on 2026-10-14, together with D16's second hunk and D17.
  - The other changes correct facts or close gaps inside the approved decisions. The
    prompt's button is now labelled "Don't show again" (§5.2).
- **Baseline:** `blackbird` at `3bafb292` (schema head `0066`). Every `file:line` reference
  below was read at that commit. Re-read a line before relying on its number.
- **Source analysis:** a read-only, page-by-page inventory of every web surface a manager and
  a reviewer can reach. Each claim in it carries a `file:line`, and its findings are condensed
  in Appendix A.
- `origin` is public. This spec names no real person, and the screenshots it specifies are of
  invented data only (§8).

## 1. Goal and success criteria

**Goal:** a manager or reviewer who has never used Blackbird can find their way around it and
understand every page they can reach without anyone walking them through it. A returning user
can look up how something works. This is delivered as a **Help** tab with general
documentation plus two read-only onboarding tutorials, one for managers and one for reviewers.
The work also fixes the UI wording that would otherwise force the docs to explain
inconsistencies (§7).

**Done when:**

- A reviewer and a manager can each open Help from the top bar, walk their whole tutorial
  and press Finish. Browser phase 6 (§10.3) passes in Chromium with zero axe findings and no
  horizontal scroll at 375 px.
- Help is reachable by admins, managers and reviewers and refused to PIs (403) and
  anonymous visitors (redirect to login).
  - Reviewers see only the all-audience pages and the reviewer tutorial.
  - Managers and admins see everything.
- A manager or reviewer sees the first-sign-in prompt on every workspace page until they
  dismiss it or finish a tutorial. It never shows for an admin or during impersonation.
- Every page listed in §6.6 renders its Help link for every viewer who can open it, and the
  link lands on an existing anchor. The rendering test in §10.2 enforces both.
- The post-implementation content audit (§10.4) finds no claim in any help page or tutorial
  step that the code contradicts.
- The wording fixes in §7 are in place:
  - no template contains "CoPI";
  - none of the replaced labels remains **at the locations §7.1–§7.3 name**.

  The same words survive where §7 leaves them on purpose: the list tab "Unreviewed",
  `admin/users.html`'s "Pending Update" and `admin/agents.html`'s "Inactive (parked)", which
  are admin-only.
- `./scripts/ci.sh` is green, and browser phases 0–6 are green on this workstation.
- The agent engine runs unchanged code paths. The worker's only behavioural change is the
  chat record's approval-status line (D17). Nothing starts a simulation run.

**Non-goals:**

- documentation for PIs, or for admin-only features (the Admin tab, impersonation, feedback
  deletion, discussion exports, raw verdicts, LLM call logs);
- a practice sandbox, or any tutorial step that submits, adds, installs or activates
  anything;
- behaviour changes to existing pages (the gaps found are listed in §12 as follow-ups);
- renaming "CoPI" outside `templates/` (emails, CLI, FastAPI app title; §12);
- search, versioning or localisation of help content;
- any prompt, rubric, specialist, engine or worker change. The single exception is the
  approval-status line of the assessment chat record (D17, §7.3).

## 2. Owner decisions (brainstorming, 2026-10-14)

| # | Question | Decision |
|---|---|---|
| D1 | What should the tutorials achieve? | Both a self-serve first day **and** a reference for returning users. The reference pages carry the reference load. |
| D2 | Who sees Help? | Admins, managers and reviewers. Not PIs. |
| D3 | Filter by role? | Yes. A reviewer sees the general docs and the reviewer tutorial; managers and admins see everything, both tutorials included. |
| D4 | Tutorial form | A written, server-rendered walkthrough: step pages with "Open this page" links to the real page. No in-page guided tour and no progress checklist. |
| D5 | Real actions? | **Read-only tour.** Tutorials show where things are and what they mean. They never ask the user to submit, add, install or activate anything. |
| D6 | Entry point | A first-sign-in prompt on the Workspace, remembered per user in the database (§5). |
| D7 | Screenshots | Generated from a seeded throwaway instance with invented data, by a script (§8). |
| D8 | Who gets the prompt? | All managers and reviewers, existing accounts included. Admins never. |
| D9 | UI inconsistencies | Fix the wording in the same change (§7). |
| D10 | Per-page help links | Yes, one per page header (§6). |
| D11 | Screenshot staleness | Report it, don't block. `capture --check` lists stale shots and is not part of `ci.sh`. |
| D12 | Which wording fixes | All four groups: state labels, generic errors, reviewer-facing copy, staff sign-in copy plus the product name. |
| D13 | How far the rename goes | Every template (all 36 files that contain "CoPI"), PI pages included. |
| D14 | How content is authored | Jinja templates plus a Python registry. Markdown was rejected because it would need custom syntax for role-only sections and its own link checker. Static HTML was rejected because `/static` is public and cannot be role-filtered. |
| D15 | Prompt storage | A **separate table**, not a column on `users`. The owner changed D6's "one nullable column" after seeing the hazard (§5.1). |
| D16 | `CLAUDE.md` | Two hunks, committed alone without the file's existing uncommitted edits (§11): the help-upkeep row, and `help_assets` in the pre-build path list. The owner approved the second hunk on 2026-10-14. |
| D17 | Chat record wording | Change the assessment chat record's approval-status line in the same change, to match the page (§7.3). This is model input, and the owner approved changing it on 2026-10-14. |

## 3. Current state the design builds on

- **Roles and capabilities.**
  - `users.user_role` is one of `pi`, `manager`, `admin` or `reviewer` (`src/models/user.py:16-23`).
  - `src/services/web_permissions.py:40-53` derives `WebCapabilities` from the *effective*
    user:
    - `research` = admin, manager or reviewer;
    - `staff` = admin or manager;
    - `assign_reviews`, `manage_suggestions` and `chat` are all false while impersonating.
  - The route gates are in `src/dependencies.py`:
    - `get_review_user` (`:362-371`) returns 403 "Review access required";
    - `get_staff_user` (`:339-359`) returns 403 "Manager access required";
    - `refuse_impersonation` is at `:311-316`.
- **Top nav** (`templates/base.html:47-91`).
  - "Workspace" shows when `effective_user.is_staff or effective_user.is_reviewer`.
  - A manager or reviewer sees only Settings and Workspace.
  - The Workspace sub-nav is `src/web/navigation.py:16-36`: reviewers get PIs and Assessments, staff get seven items.
- **Page chrome** comes from `page_context` / `workspace_context` (`src/web/page_context.py`).
  Workspace page data passes through the allowlist projector `src/web/presentation.py`.
- **Landing.**
  - `/workspace` sends staff to `/workspace/pis` and everyone else to `/workspace/assessments`
    (`src/routers/workspace/__init__.py:25-30`).
  - At sign-in, a pre-created manager or reviewer skips onboarding and lands there too
    (`src/routers/auth.py:336-346`).
- **Constraints the design must respect.**
  - **CSP:** an enforced CSP, `script-src 'self' 'nonce-…'` (`src/web/security_headers.py`). There is no JS framework; behaviours are delegated handlers in `static/js/ui.js`.
  - **Tailwind:** compiled Tailwind with a drift check (`scripts/build_css.sh --check`, run by `ci.sh`). It has no typography plugin (`tailwind.config.js`).
  - **Reachability gate:** `tests/unit/test_reachability.py` requires that:
    - every template is rendered by a literal name in `src/` or included by a reachable template;
    - no template name is computed (`test_no_dynamic_template_references`);
    - every route is linked by `href`/`action`, named in a `src/` string, or allowlisted with a reason.
  - **Static files are public:** `src/main.py:324-333` mounts `/static` with no authentication.
  - **No help surface exists:** there are no help, tutorial or staff-onboarding routes or templates.
- **No practice environment exists.**
  - A reviewer's feedback is real review data. "Learn" makes it eligible for the prompt-suggestion analysis (`templates/assessments/_detail_body.html:1246-1251`).
  - A manager's Add PI creates an account and queues jobs (`src/services/pi_onboarding.py:80-138`).
  - Install Slack bot creates a Slack app (`src/services/admin_provisioning.py:171-191`).
  - Activate puts the agent on the live roster (`src/routers/workspace/pi_bots.py:151-231`).
- **The existing browser harness** (`tests/e2e/ui_audit/`) runs a throwaway Postgres container and an isolated working directory with no `.env`. It forges role cookies and serves the app with a fake model.
  - Its seed is **deliberately hostile**: XSS names and raw HTML (`tests/e2e/ui_audit/seed.py:1-8`).
  - Its fake chat answer is hostile too (`tests/e2e/ui_audit/serve.py:26-30`).
  - It cannot run on the host, which lacks Chromium's libraries (`tests/e2e/ui_audit/README.md`).

## 4. Routes, access and navigation

### 4.1 Top nav

`templates/base.html` gains a **Help** link after Workspace, under the same condition as
Workspace (`effective_user.is_staff or effective_user.is_reviewer`). It is active when
`active_page == 'help'`. Because the condition reads the *effective* user, an admin
impersonating a manager or reviewer sees Help for that role, and an admin impersonating a
PI does not see it.

### 4.2 Routes

A new router, `src/routers/help.py`, is mounted at `/help` in `src/main.py`. Every route
depends on `get_review_user`. Route names are fixed so `page_url` and the reachability gate
can address them.

| Method, path | Name | Behaviour |
|---|---|---|
| `GET /help` | `help_index` | The index for the viewer's role (§4.4). |
| `GET /help/{slug}` | `help_page` | A reference page. An unknown slug gets 404. A `staff`-audience page requested by a non-staff viewer gets 403 "Manager access required". |
| `GET /help/tutorials/{tutorial}` | `help_tutorial` | A 302 to step 1. An unknown tutorial gets 404; `manager` for a non-staff viewer gets 403. The index's tutorial cards and the sub-nav link here, so the route has a caller for the reachability gate. |
| `GET /help/tutorials/{tutorial}/{step}` | `help_tutorial_step` | One step, 1-based. A step out of range or not an integer gets 404. The gate is the same as above. |

`step` is declared as `str` and parsed in the handler. A declared `int` would turn a
non-integer into a 422 through the `RequestValidationError` handler
(`src/web/errors.py:118-121`) instead of a 404. Likewise `via` on the dismiss POST is
`Form("")`, and an empty or unknown value is checked in the handler, as
`src/routers/workspace/staff.py:46-51` does.
| `POST /help/intro/dismiss` | `help_intro_dismiss` | Records the first-sign-in prompt's answer (§5). |
| `GET /help/shots/{shot_id}.png` | `help_shot` | One screenshot (§8.4). |

A PI gets 403 "Review access required" and an anonymous visitor gets the usual redirect to
`/login?next=…`, both from `get_review_user` → `get_current_user`.

The fixed routes are declared before `/help/{slug}`. The segments `tutorials`, `intro` and
`shots` are reserved: a unit test asserts that no registry slug uses them.

### 4.3 Help sub-nav

`base.html` renders a Help sub-nav when `active_page == 'help'`, styled like the Workspace
sub-nav (`base.html:118-128`). It lists:

1. Overview (`/help`);
2. the role's tutorial or tutorials, each linking to `help_tutorial`;
3. the role's `HelpPage` entries in registry order.

Glossary and Troubleshooting are the last two registry entries, so they come last. The
current item is marked with `aria-current="page"`.

### 4.4 Index

`/help` shows two things:
- the viewer's tutorial cards first: the reviewer tutorial for reviewers; the manager
  tutorial, then the reviewer tutorial, for managers and admins;
- then the role's reference pages, each with its one-line summary from the registry.

### 4.5 Context

Help handlers build their context with `page_context(request, user, active_page="help",
section=<slug>, …)`. Help content is static. Its only inputs are:

- the capability flags;
- `microsoft_sign_in`, which is `request.app.state.microsoft_sign_in is not None`, the same
  test as `src/routers/settings.py:32-33`;
- the registry;
- the screenshot manifest, read through the Jinja global `help_shot_meta` rather than the
  context (§8.3).

Help loads no PI, assessment or account data, so nothing passes through
`project_workspace`.

## 5. First-sign-in prompt

### 5.1 Storage: table `help_intro_dismissals` (migration `0067`)

The answer is stored in its own table rather than on `users` (D15). The `User` model is
mapped by the web app, the worker and the agent engine. A new mapped column means any image
running the new code before the migration fails on every user load. `alembic/CLAUDE.md`
notes that on the engine's best-effort write paths such a failure is swallowed and verdicts
are lost. A table that only the Help code reads confines that hazard to the web app. A
missing row means "never answered", the analogue of NULL.

```
help_intro_dismissals
  user_id       uuid        PRIMARY KEY, REFERENCES users(id) ON DELETE CASCADE
  dismissed_at  timestamptz NOT NULL DEFAULT now()
  via           varchar(16) NOT NULL, CHECK (via IN ('dismiss', 'finish'))
```

- The model is `src/models/help.py::HelpIntroDismissal`, exported like the other models.
  **No relationship is declared on `User`.** A relationship without `passive_deletes` would
  make `db.delete(user)` (`src/services/user_deletion.py:193`) try to null the
  primary-key FK, and that fails.
- The CHECK constraint is named `ck_help_intro_dismissals_via` and listed in
  `PLANNED_OBJECTS`. The drift test parses only unique constraints
  (`tests/unit/test_migration_checks.py:1068-1094`), so an unnamed or unlisted check would
  pass silently.
- Revision `0067`, `down_revision = "0066"`. The file docstring states the two directions,
  following the shape of `0066`:
  - **Old code on the new schema** is safe: nothing old reads the table.
  - **New code on the old schema** gives a 500 on every manager and reviewer workspace page
    (§5.2's lookup). The migration must run before the new web image serves.
  - **Downgrade** drops the table and every recorded answer.
- Account deletion needs no change: `ON DELETE CASCADE` removes the row, whichever path
  deletes the user.
- Migration bookkeeping, per `alembic/CLAUDE.md`:
  - `scripts/migrate/preflight.py`: `DEFAULT_TARGET = "0067"`, `REVISION_ORDER`, `0066`
    joins `SUPPORTED_START_REVISIONS`, and the table goes into `PLANNED_OBJECTS`;
  - the pins in `tests/unit/test_migration_checks.py` and
    `tests/integration/test_harness_smoke.py`;
  - `tests/integration/test_migration_0067.py`, following `test_migration_0062.py` and its
    neighbours: upgrade, constraint, cascade on user delete, and downgrade;
  - a "Deploy order for `0067_help_intro_dismissals`" box in
    `docs/operations/migration-deploy-notes.md`. The box says three things:
    - the agent image needs no rebuild, because the engine never reads the table;
    - **the worker must be idle** (no `processing` `generate_profile` job) or stopped.
      `REFERENCES users` takes a lock on `users`, the worker holds one transaction across a
      pipeline run that updates `users` rows (`src/services/profile_pipeline.py:270-280`),
      and the chain's 10 s `lock_timeout` (`alembic/env.py:66, 88-93`) would roll the apply
      back. The 0066 box's claim that its CREATE TABLE "takes no lock on an existing table"
      is not copied;
    - sign-ins queue behind that lock while the apply waits.

### 5.2 When the prompt shows

A dependency, `src/web/help_intro.py::load_help_intro`, is added to the **workspace router**
(`src/routers/workspace/__init__.py`, `APIRouter(dependencies=[…])`).

- **It raises no `HTTPException` of its own.** It takes `Depends(get_current_user)`, not
  `get_review_user`. Router dependencies run before a route's own, so a raising gate here
  would replace a staff route's "Manager access required" with "Review access required"
  for a PI.
- **Database errors propagate.** The lookup is not wrapped in `try/except`. Swallowing an
  `UndefinedTable` error would leave the shared session in a failed transaction, and the
  handler would then fail confusingly instead of with the clear 500 that §5.1 describes.
- **It sees HEAD as GET.** `src/web/head_requests.py` routes a HEAD as the GET it mirrors.

It does nothing unless all of these hold:

- the request is a GET;
- the effective user's role is `manager` or `reviewer`;
- the session is not impersonating (`_is_impersonated` false).

When they hold, it does one primary-key lookup on `help_intro_dismissals`. If no row
exists, it sets `request.state.help_intro` to the role's tutorial key (`"manager"` or
`"reviewer"`). `page_context` copies `getattr(request.state, "help_intro", None)` into the
context as `help_intro`, so no workspace handler changes.

`base.html` renders the prompt inside `<main>`, above the flash messages, when `help_intro`
is set and `active_page == 'workspace'`. It is a `role="region"` with
`aria-label="Getting started"`. It contains **no heading element**: it comes before each
page's `<h1>`, and the rendered-page gate checks heading order
(`tests/integration/test_rendered_page_gate.py:268-272`).

> **New to Blackbird?** Take the {Reviewer | Manager} tutorial — a read-only tour of the pages
> you'll use. **[Start the tutorial]** · **[Don't show again]**

- **Start the tutorial** is a plain link to step 1 and records nothing.
- **Don't show again** is a `POST` form to `help_intro_dismiss` with `via=dismiss` and
  `next=<current path and query>`.
  - It is not labelled "Dismiss": the prompt-suggestion detail page has a "Dismiss" status
    button (`workspace/prompt_suggestion_detail.html:56`), and two controls with the same
    accessible name on one page invite a wrong click.
  - In the rest of this spec, "dismiss" means this control.

Admins never see the prompt, and they can still open Help.

### 5.3 `POST /help/intro/dismiss`

1. `get_review_user`, then `refuse_impersonation(user, "Help changes are disabled while
   impersonating.")`.
2. `via` must be `dismiss` or `finish`; anything else gets 400 "Invalid request".
3. `INSERT … ON CONFLICT (user_id) DO NOTHING`. The first answer is kept, and a repeat is
   harmless.
4. Redirect (302):
   - `via=dismiss`: to `next` when `src/routers/auth.py::is_safe_next_url(request, next)`
     accepts it, else to `/workspace`. Flash (info): "The tutorials are always under Help."
   - `via=finish`: to `/help`. Flash (success): "Tutorial finished."

The existing `OriginGuardMiddleware` already covers this POST. Admins may post; the row is
harmless, because §5.2 never consults it for them.

## 6. Help content

### 6.1 Registry: `src/web/help_pages.py`

```python
@dataclass(frozen=True)
class HelpPage:
    slug: str            # URL segment and sub-nav key
    title: str
    summary: str         # one line for the index
    audience: str        # "all" | "staff"
    template: str        # LITERAL "help/pages/<slug>.html" (reachability gate, §3)

@dataclass(frozen=True)
class TutorialStep:
    title: str
    template: str        # LITERAL "help/tutorials/<key>/<nn>-<name>.html"
    open_route: str | None   # route NAME for "Open this page", no path params
    shots: tuple[str, ...]   # screenshot ids (§8)

@dataclass(frozen=True)
class Tutorial:
    key: str             # "reviewer" | "manager"
    title: str
    audience: str        # reviewer: "all"; manager: "staff"
    steps: tuple[TutorialStep, ...]
```

- `HELP_PAGES` and `TUTORIALS` are module-level tuples.
- `pages_for(permissions)` and `tutorials_for(permissions)` filter them by `permissions.staff`.
- `lookup_page(slug, permissions)` and `lookup_step(key, n, permissions)` raise the 404 or
  403 of §4.2.
- One registry drives the index, the sub-nav and the gate.

### 6.2 Templates (`templates/help/`)

| File | Role |
|---|---|
| `_layout.html` | Extends `base.html`. Holds the content column and an `{% block help_body %}`. |
| `index.html` | §4.4. |
| `pages/<slug>.html` | One per `HelpPage`. Extends `_layout.html`. Every `<h2>` and `<h3>` a link targets has a stable `id`. |
| `tutorials/<key>/<nn>-<name>.html` | One per step. Extends `_layout.html`, includes `_step_nav.html`. |
| `_step_nav.html` | `<nav aria-label="Tutorial steps">`:<br>• "Step n of N" with `aria-current="step"`;<br>• Previous and Next links;<br>• "Open this page ↗ (opens in a new tab)" when `open_route` is set (`target="_blank" rel="noopener"`);<br>• on the last step, a **Finish** `POST` form to `help_intro_dismiss` with `via=finish`. While impersonating, the form is replaced by a plain link to `/help`, because the POST would 403. |
| `_figure.html` | Macro `figure(request, shot_id, caption)`. Renders `<figure><img src="{{ page_url(request, 'help_shot', shot_id=…) }}" alt width height loading="lazy" class="max-w-full h-auto">` with a caption. Alt, width and height come from the manifest through the Jinja global `help_shot_meta(shot_id)` (§8.3). |
| `_link.html` | Macro `help_link(request, viewer, slug, anchor="")` for page headers (§6.6). `viewer` is the template's `effective_user`. |

The macros take `request` (and `viewer`) as **explicit arguments**. Jinja's
`{% from … import %}` does not pass the caller's context, and `request` is not a Jinja
global. This is the repo's existing pattern (`templates/pis/_bot_action.html:3`).

Styling uses Tailwind utility classes in the templates, so the existing content scan
compiles them. `static/css/app.css` is rebuilt and committed. No new JavaScript is added.

### 6.3 Content rules

1. **Every factual statement comes from the code at implementation time**, not from this spec
   or from memory. Appendix A is the starting inventory. §10.4's audit checks the result.
2. **Name the UI text exactly**, after the §7 changes: button labels, headings and badge
   words.
3. **Don't state values that come from settings or the rubric.** That covers band
   thresholds, dimension titles and weights, chat question, length, turn and cost limits,
   page sizes, and the prompt-suggestion batch size. Say where the page shows the value
   instead: the assessments header prints the bands, and the chat drawer prints "X of Y
   questions used today". These values change without a template change, so a stated
   number would silently go stale. The rule covers the help **text**. A screenshot may show
   whatever the page itself prints, such as the chat drawer's usage line or the list
   header's bands, because it depicts the page as it was when captured. The staleness check
   (§8.2) hashes the rubric file, so a rubric change marks those shots stale.
4. **Code constants** may be stated only if they are on this list. These are the 1–5 overall
   rating and the comment length limit (`src/services/assessment_reviews.py:128-131,
   158-159`), and a unit test pins each to its source (§10.1). Every other number is left
   out: point to the UI text that shows it, or describe it without the number. That covers:
   - the session lifetime: it is `max_age=30 * 24 * 3600` at `src/main.py:294`, but Starlette
     re-issues the cookie on every session write, so it is not "30 days from sign-in";
   - the 15-minute ORCID re-sign-in window for Microsoft linking;
   - the 1-hour Regenerate cooldown: the disabled button states its reason;
   - the ~30 s roster sync: the activation banner states it.
5. **Role-only sections inside an all-audience page** sit in
   `{% if permissions.staff %}…{% endif %}`. A manager-only feature mentioned to a reviewer
   is described only as "a manager can…", with no instructions.
6. **Microsoft sign-in content** renders only when `microsoft_sign_in` is true.
7. **Warn before real effects.** In the manager tutorial and the manager reference pages,
   every action with real, visible or permanent effects is introduced by an amber
   "Careful" box. The box says what happens and whether the action can be undone from the
   UI. Appendix A.2 lists which actions cannot.
8. **Admin-only features** are mentioned only where they bound what a manager can do, as
   "only an admin can…". They are never described.
9. **No real names**, in content or screenshots. Examples are invented. Never copy them from
   code comments or tests, which name real PIs.
10. **No repository internals.** Pages explain how to use the system and what its data means.
    They contain:
    - no file paths;
    - no pointers to code or tests;
    - no test counts;
    - no audit or verification history.

    The §10.4 audit reports its results in chat, never in the pages.

### 6.4 Reference pages

| Slug | Title | Audience | Sections (anchor ids) |
|---|---|---|---|
| `getting-started` | Getting started | all | `signing-in` (ORCID; a page you tried to open before signing in opens after you sign in), `microsoft` (linking: re-confirm with ORCID, then link in Settings; unlink signs you out everywhere — rendered only when enabled), `first-sign-in` (managers land on PIs and reviewers on Assessments, unless sign-in is resuming a page; managers and reviewers have no PI onboarding. Admins are not described: they keep the PI surfaces and land on onboarding or My Profile, `auth.py:332-347`), `navigation` (top bar; Workspace sub-nav per role; Help), `settings`, `signing-out` (signs you out on every device), `access-problems` (access pending or turned off; who can restore it, per §7.4.2) |
| `how-it-works` | How Blackbird works | all | `pis-and-lab-agents`, `simulation-runs`, `interviews` (BlackbirdBot / the hub screens a lab's pitch), `specialist-panel`, `assessments`, `human-review`, `prompt-suggestions` (concept only; for reviewers: "a manager decides when suggestions are generated") |
| `assessments` | Assessments | all | `list` (run selector, sort, Assigned to me, lab, Unreviewed / Reviewed / All tabs, banners, summary cards, dimension distribution), `cards` (headline, recommendation, In one minute, Key points, Why this score, footer badges, Rubric scores & gating, Quick scoring), `detail` (jump nav; Brief; Evidence summary; The ask; Verdict; Specialist panel; Gating; Red flags; Full rationale; Dimension scores; Interview timeline; printing), `chat` (opening, opening questions, inline "Ask:", Sources and Show in page, Clear conversation, who can read saved chats, the limits as the drawer shows them), `reviewing` (Add feedback, dimension scores, Learn vs "Don't learn — log only", editing your own feedback, no deletion), `approval-status`, `assigning` (staff block) |
| `pis` | PIs | all | `directory` (columns; staff block: filters, Add PI → link to `managing-pis#add-pi`), `detail` (Research profile, Details, Grants, Industry interest, Companies, Publications as a reader; staff block: what the manager-only cards are, with links to `managing-pis` and `pi-evidence`) |
| `managing-pis` | Managing PIs and lab bots | staff | `add-pi`, `profile-status`, `account` (the Account card: email Verified/Unverified and **Mark verified** with a Careful box; the cleaned-name note; Onboarding Complete, explained from what sets it, which is the PI's own onboarding save at `src/routers/onboarding.py:157`, verified at implementation; Claimed At; Joined), `edit-profile` (incl. JHU tenure start), `paper-review` (candidate / unanchored / excluded papers), `drafts`, `regenerate` (Regenerate vs Retry profile generation), `persona` (persona file, Re-export, revisions), `jobs`, `slack-bot` (Install Slack bot), `activation` (what Activate checks; refusals; no override for managers), `mute`, `slack-bots` (the Slack Bots page) |
| `pi-evidence` | Grants, industry interest and companies | staff | `reporter-identity` (statuses; This is the PI; PI has no RePORTER profile; Unpin), `grants` (awards; Not this PI), `orcid-fundings`, `industry` (percentile, reasons, partial, evidence, veto), `companies` (confirmed, suggested, Add a company, Confirm / Reject, Delete, Find companies, the daily budget wait) |
| `staff-accounts` | Staff accounts | staff | `add` (Preview then Confirm; managers create reviewers only; converting a PI account and when a manager must ask an admin), `revoke-restore-delete` (incl. the never-signed-in revoke rule), `telling-new-staff` (no notification is sent: tell the person to sign in with ORCID at the site and start at Help), `recent-changes` |
| `discussions-activity` | Discussions and Activity | staff | `discussions` (run selector, status cards, filters, thread rows, specialist panel cards), `activity`, `run-detail` |
| `prompt-suggestions` | Prompt suggestions | staff | `what-they-are`, `generate`, `status` (Reopen / Dismiss / Mark Implemented never change a prompt file), `detail` (Distilled From; Prompt Files Read: current / STALE / file missing) |
| `glossary` | Glossary | all | One `<dt id="term-<slug>">` per term. Staff-only terms sit in staff blocks. Every synonym the web UI used before §7 is listed under the new term, e.g. "Inactive (old label): see Muted". **"CoPI" is not listed**: after §7.4.4 no web page says it, and the emails that still do go to PIs, not to managers or reviewers. The no-"CoPI" test (§10.1) therefore has no exception. **Muted** means the agent's status is `inactive`. That covers a manager's Mute and an admin's park (`src/routers/admin/agents.py:248-251`), so the entry does not say a manager muted it. Unmute re-activates the agent through the activation gate whichever way it was muted. |
| `troubleshooting` | Troubleshooting | all | Messages a user can hit, grouped by page, each with what it means and what to do. Staff-only messages sit in staff blocks. Sources:<br>• the error banners and flashes in Appendix A;<br>• the error-page details "Review access required", "Manager access required", "Help changes are disabled while impersonating.", "Review actions are disabled while impersonating." and "Staff account changes are disabled while impersonating.";<br>• "Cross-site request refused." (`src/main.py:230`; shown when a form is posted from a stale or foreign page);<br>• the access-pending and "Access turned off" pages. |

### 6.5 Tutorials

All steps are read-only (D5). Each step template has four parts:

- **Where:** the nav path, plus "Open this page".
- **What you'll see:** one to three figures.
- **What it means**
- **Careful** boxes, where a real action is described.

Neither tutorial asks the user to submit, add, install or activate anything.

**Reviewer tutorial** (`reviewer`, audience `all`, 8 steps):

| # | Title | Open route | Covers |
|---|---|---|---|
| 1 | Welcome | — | What Blackbird is; what a reviewer does; what reviewers can and cannot reach |
| 2 | Signing in and finding your way | `workspace_assessments` | Landing on Assessments; Settings; Microsoft linking (when enabled); Sign out signs you out everywhere; Help |
| 3 | The Assessments list | `workspace_assessments` | Run selector, sort, Assigned to me, lab; the tabs, where "Reviewed" means written feedback exists; the banners |
| 4 | Reading an assessment card | `workspace_assessments` | Recommendation, In one minute, Key points, Why this score, panel badge, Rubric scores & gating; Quick scoring described, not used |
| 5 | The assessment detail page | `workspace_assessments` ("open any row") | Jump nav and each section, through the Interview timeline |
| 6 | Asking the assessment chat | `workspace_assessments` | Opening it; opening questions; inline "Ask:" sends at once; Sources and Show in page; Clear conversation; saved conversations are readable by administrators with database access |
| 7 | Recording your review | — | Overall rating, optional dimension scores, comment, Learn vs log only; edit only your own feedback; no deletion. A Careful box says feedback is real review data |
| 8 | PI pages, and getting help | `workspace_pis` | The PI directory and detail as context; Help, Glossary, Troubleshooting; **Finish** |

**Manager tutorial** (`manager`, audience `staff`, 10 steps):

| # | Title | Open route | Covers |
|---|---|---|---|
| 1 | Your role | — | Manager vs reviewer vs admin; what only an admin can do |
| 2 | Signing in and the Workspace | `workspace_pis` | Landing on PIs; each Workspace tab, named but not counted (§6.3 rule 4); Settings; Help |
| 3 | The PIs list | `workspace_pis` | Profile status, agent status, Claimed; filters; the Add PI form described |
| 4 | A PI's lifecycle | `workspace_pis` | Add PI → profile generation → Install Slack bot → Activate → Mute/Unmute. Careful boxes: Add PI creates an account and queues jobs; Install creates a Slack app; Activate puts the bot on the live roster |
| 5 | Reviewing a PI's profile | `workspace_pis` ("open any PI") | Account card; Edit Profile and the tenure year; candidate / unanchored / excluded papers; drafts; Regenerate vs Retry; persona file and revisions; Jobs |
| 6 | Grants, industry interest and companies | `workspace_pis` | RePORTER identity; vetoes, which cannot be undone; industry interest; companies. Careful boxes per Appendix A.2 |
| 7 | The Slack Bots page | `workspace_slack_bots` | Counts, statuses, actions; where each action lands |
| 8 | Assessments as a manager | `workspace_assessments` | Assigning reviewers ("Assign to"); everything else, via a link to the reviewer tutorial |
| 9 | Staff accounts | `workspace_staff` | Preview then Confirm; reviewers only; Revoke / Restore / Delete; the never-signed-in rule; no notification is sent, so tell the person |
| 10 | Discussions, Activity and Prompt Suggestions | `workspace_discussions` | What each page shows; Generate suggestions; **Finish** |

### 6.6 Per-page Help links

Each header below gets `{{ help_link(request, effective_user, slug, anchor) }}`, a small
"? Help" link. It renders only when `effective_user.is_staff or effective_user.is_reviewer`.

| Template | Target |
|---|---|
| `workspace/pis.html` | `/help/pis#directory` |
| `workspace/pi_detail.html` | `/help/pis#detail` |
| `workspace/assessments.html` | `/help/assessments#list` |
| `workspace/assessment_detail.html` | `/help/assessments#detail` |
| `workspace/slack_bots.html` | `/help/managing-pis#slack-bots` |
| `workspace/staff.html` | `/help/staff-accounts` |
| `workspace/discussions.html` | `/help/discussions-activity#discussions` |
| `workspace/activity.html`, `workspace/activity_detail.html` | `/help/discussions-activity#activity` (detail: `#run-detail`) |
| `workspace/prompt_suggestions.html`, `workspace/prompt_suggestion_detail.html` | `/help/prompt-suggestions` (detail: `#detail`) |
| `settings.html` | `/help/getting-started#settings` (hidden for PIs, who cannot open Help) |

Links use `page_url(request, 'help_page', slug=…, fragment=…)`.

A **rendering** test (§10.2) checks every help link. A static scan cannot do it, because
ids and links sit inside `{% if permissions.staff %}` and `{% if microsoft_sign_in %}`
blocks, the same blind spot `tests/unit/test_reachability.py:35-47` records. The test:

1. renders every help page, every tutorial step and every page in the table above;
2. does so as a reviewer, as a manager and as an admin, each with Microsoft sign-in off and
   on;
3. for every `/help/...` link in the rendered HTML, asserts that the target opens for that
   viewer and that the fragment is an `id` in the target as rendered for that viewer;
4. asserts that each page in the table above renders **its listed** help link for every
   viewer who can open that page. Without this step, a page that leaves out its link would
   pass.

## 7. Wording fixes

The fixes change copy and labels. Three of them also change something a user or a test can
observe:

- the reviewer's empty PI filter form is no longer rendered (§7.3);
- the `pending_access` session payload gains two keys (§7.4.2);
- the staff delete-account page's Cancel goes to `/settings` (§7.4.3).

No route's authorization or effect changes.

### 7.1 State labels

| Where | Today | After |
|---|---|---|
| `workspace/pis.html:129-140`, agent badge | `inactive` renders "Inactive" (via `replace('_',' ') \| title`) | "Muted", same colour as Slack Bots' muted badge |
| `workspace/pis.html:129-140`, agent badge | `awaiting_token` renders "Awaiting Token" for **every** pending agent, token or not (`src/services/directory.py:302-309`) | "Pending" |
| `workspace/pis.html:129-140`, agent badge | `not_requested`, `active`, `suspended` | "Not requested", "Active", "Suspended" (an explicit label map, no `title` filter) |
| `workspace/slack_bots.html:43-53`, status badge | "active", "pending", "muted", raw status | "Active", "Pending", "Muted"; other statuses title-cased |
| `workspace/pis.html:61-68` and `:117-128`, profile status | "Pending Update" (filter option and badge) | "Pending draft", matching "Pending draft" on PI detail (`pis/_profile_review.html:30-51`). The filter **value** `pending_update` is unchanged. |

### 7.2 Generic error fallbacks on PI detail

- `industry_busy` is redirected by `src/routers/workspace/pi_evidence.py:440-450` but has
  no branch in `workspace/pi_detail.html:94-124`, so it shows "Something went wrong saving
  changes." It gets its own branch: "This PI's industry evidence is being refreshed — try
  the veto again in a moment."
- The implementation enumerates **every** `error` code a manager or reviewer can reach
  `workspace_pi_detail` with. The sources are:
  - the literals in `src/routers/workspace/pi_profile.py`, `pi_evidence.py`, `pi_bots.py`
    and `pi_directory.py`;
  - the codes returned by the services those routes pass through:
    - `profile_edit.apply_profile_edits`;
    - `profile_review.accept_draft` (`no_draft`, `profile_generating`, `draft_stale`,
      `summary_too_long`);
    - `agent_mute.set_agent_mute_state` (`agent_not_mutable`, `no_token`,
      `activation_blocked`, `no_spoke`);
    - `email_verification.mark_email_verified` (`no_email`, `email_changed`);
  - the codes forwarded from `/manager/pis/{id}` (`src/routers/manager.py:119`) through
    `QUERY_FIELDS["pi_detail"]`.

  Each code must have its own sentence in `pi_detail.html` or `_profile_errors.html`. As of
  `3bafb292` the only one missing is `industry_busy`. Codes that `/admin/users/{id}` forwards
  (`src/routers/admin/users.py:88`) reach only admins and are out of scope.
- The §10.1 test does two things:
  1. It holds the enumerated codes as an explicit tuple, renders the PI-detail error banner
     for each one, and asserts that none falls through to "Something went wrong saving
     changes."
  2. It runs an **AST scan** of the four router files and collects:
     - string literals passed as arguments to the PI-detail redirect helpers
       (`_review_redirect`, `_grants_redirect`, `_persona_busy_redirect`, and any other
       helper whose body builds a `workspace_pi_detail` URL; the scan finds these helpers
       itself, by that test);
     - `{"error": <const>}` literals inside `page_url(..., "workspace_pi_detail", ...)` calls.

     It asserts every collected code is in the tuple.
     - Codes redirected to `workspace_pis` are excluded by construction: `create_failed`,
       `exists`, `agent_conflict` and `name_required` in `pi_directory.py:209-238, 265`.
       PI detail has no sentence for them, and needs none.
     - A code added later through a helper or a direct `page_url` call fails the test.
     - A code added only inside one of the services above is not caught;
       `docs/operations/help-content.md` names this gap.
- `email_required` stays unhandled on this page. Only onboarding passes
  `email_required=True` (`src/routers/onboarding.py:142`), so this page cannot receive it.

### 7.3 Reviewer-facing copy

| Where | Today | After |
|---|---|---|
| `assessments/_detail_body.html:1246-1251`, Learn help | One sentence for everyone, pointing to "the prompt-suggestions page" (manager-only) | Staff: unchanged. Others: "Choosing **Learn** marks this feedback as eligible for the prompt-suggestion bot and nothing more. A manager decides when suggestions are generated." The file's "no absolute manager path" rule (its comment at `:1241-1245`) still holds. |
| `assessments/_detail_body.html:1077-1081`, review status | Heading "Status"; "Unreviewed" when there is no approval event, even when feedback exists | Heading "Approval status"; "No approval decision". The template comment is updated to match. The assessment chat's record changes with it (D17), in `src/services/assessment_chat_record.py:733-745`:<br>• the line prefix "Review status — " becomes "Approval status — ";<br>• the cleared case "Unreviewed (the last approve or disapprove was cleared)" becomes "No approval decision (the last approve or disapprove was cleared)";<br>• approved, disapproved and unknown keep their wording after the new prefix.<br>`tests/unit/test_assessment_chat_record.py:388` and `tests/integration/test_assessment_chat_parity.py` move with it. The record's other documents and the prompts are unchanged. |
| `assessments/_detail_body.html:1288`, assign label | "Assign reviewer", though the roster also lists managers and admins (`src/services/assessment_detail.py:2104-2119`) | "Assign to" |
| `assessments/_body.html:629`, Quick scoring note | "Adds a new note; edit or remove one on the detail page." Only an admin can delete feedback | "Adds a new note; you can edit your own notes on the detail page." (added during integration, 2026-10-14) |
| `workspace/pis.html:57-79`, filter form | Rendered for reviewers as a bare "Apply" button with no controls | The whole form renders only for staff. `institution_filter` URL behaviour is unchanged. |

### 7.4 Staff sign-in copy and the product name

1. **`login.html`**
   - The intro (`:17-20`) becomes "We use ORCID to securely identify you. For PIs, your
     ORCID record also seeds your research profile."
   - "How it works" (`:53`) becomes "How it works for PIs".
   - A second info block is added: "**Managers and reviewers:** sign in with the ORCID iD your
     account was created with{% if microsoft_enabled %}, or with Microsoft once you have
     linked it in Settings{% endif %}. You'll land in the Workspace, or on the page you were
     trying to open; the Help tab has a tutorial for your role." It says "managers and
     reviewers", not "staff", because an admin lands on onboarding or My Profile
     (`auth.py:332-347`).
2. **Access pending for revoked staff.**
   - `src/dependencies.py::pending_access_payload` gains `user_role` and `access_status`.
     Both callers already go through it (`dependencies.py:128`, `auth.py:360`).
   - `access_pending.html` shows a different message when the payload says
     `access_status == "denied"` and `user_role` is admin, manager or reviewer:
     - heading "Access turned off";
     - body, for a reviewer: "Your access to Blackbird has been turned off. Ask a manager or
       an admin to restore it.";
     - body, for a manager or admin: "Your access to Blackbird has been turned off. Ask an
       admin to restore it." The Staff page's Restore acts only on reviewers
       (`staff_accounts.py:416-427`), and "Only an admin can change an admin or manager
       account" (`workspace/staff.html:15`);
     - no email box.
   - Every other case, including a session saved without the new keys, keeps today's copy
     with the product name changed. `/access-pending` stays free of auth dependencies
     (`dependencies.py:115-125` explains the loop this avoids).
3. **Delete account for staff.**
   - When `not effective_user.may_use_pi_surfaces`, `profile/delete_account.html` replaces
     the PI list (`:10-21`) with what deletion does to an account that has no lab agent.
   - The wording states what `delete_user_account(db, user)` does with its default
     `remove_from_allowlist=False` (`src/routers/profile.py:236`;
     `src/services/user_deletion.py:197-202`). It starts from the Staff page's delete dialog
     (`workspace/staff.html:166`): reviews and approve/disapprove history are kept with the
     name; review assignments and assessment-chat history are deleted.
   - It must **not** say the ORCID iD leaves the access allowlist. Only the Staff page's
     Delete removes it (`staff_accounts.py:477`); self-deletion does not.
   - Each clause is **verified per role (manager, reviewer)** before it is written.
     - What is kept or removed is decided by `src/services/user_deletion.py` **and** by the
       `ondelete` rules of every foreign key to `users.id` across `src/models` (SET NULL
       attributions; CASCADE chats and assignments).
     - The Staff dialog the wording starts from describes a reviewer only (Delete there is
       reviewer-only, `staff_accounts.py:416-427`).
     - If the facts differ between the two roles, the page shows the copy for the viewer's
       role.
   - For staff, Cancel links to `/settings` instead of `/profile/edit`.
4. **Product name.** "CoPI" becomes "Blackbird" in all 36 templates that contain it, titles
   and body copy alike. That includes the PI pages, the admin pages and
   `discussions/export.html`. A grep test (§10.1) asserts no template contains "CoPI".

## 8. Screenshots

### 8.1 Demo seed: `tests/e2e/help_shots/seed.py`

- **Data rules.** The seed is built with `tests/factories.py` and contains invented people,
  labs and companies only.
  - ORCID iDs: format-valid, with a **deliberately wrong check digit**. An ORCID iD's last
    character is an ISO 7064 MOD 11-2 checksum, which the repo already implements for PubMed
    author iDs (`src/services/pubmed.py:513`). Such an iD can never be issued to anyone. Whether a numeric block is "never issued" is not something the repo can
    verify, so the design does not rely on it. The seed accepts these iDs because the
    factories insert directly and `validate_orcid` checks format only
    (`src/services/pi_onboarding.py:54-60`); this is verified at implementation. The iDs
    appear in screenshots (the PI list shows an ORCID column).
  - Emails: `@example.org`.
  - PMIDs and RePORTER ids: fake.
  - Nothing is copied from production.
- **Guard.** It refuses to run unless `DATABASE_URL` names the harness database
  (`tests/e2e/ui_audit/env.py:DB_NAME`), as `tests/e2e/ui_audit/seed.py` does.
- **Contents.** It seeds, at minimum:
  - one manager and one reviewer, each with a `help_intro_dismissals` row so the prompt
    stays out of their shots;
  - a separate "new reviewer" account with no row, used for the prompt shot;
  - PIs covering every profile status (No Profile, Generating, Complete, Pending draft) and
    agent status (Not requested, Pending without and with a token, Active, Muted);
  - one fully populated PI: profile, candidate, unanchored and excluded papers, a pending
    draft, a RePORTER identity with awards, ORCID fundings, industry evidence, confirmed and
    suggested companies, and jobs;
  - at least four scored PIs, so the industry percentile renders instead of "unscored";
  - a simulation run whose assessments fill every detail section: verdict, key points,
    evidence, gating, red flags, panel, dimension scores and interview timeline;
  - feedback rows in both modes, and an assignment;
  - discussion threads with decisions;
  - one prompt suggestion with distilled feedback;
  - staff accounts in each access state.

### 8.2 Capture tool: `tests/e2e/help_shots/capture.py`

- **Reuse.** It reuses the harness pieces:
  - `ThrowawayPostgres` (`ui_audit/postgres.py`);
  - `isolated_workdir` and `harness_env` (`ui_audit/env.py`): no `.env`, no outbound email,
    no AWS or Slack;
  - forged role cookies and `Harness.page(role, width=1280)` (`ui_audit/harness.py`).
- **Server.** It runs its own serve module, `tests/e2e/help_shots/serve.py`.
  - It scripts a **benign** fake chat answer with one citation instead of the hostile one in
    `ui_audit/serve.py`.
  - It **stubs every outbound fetcher** a shot's preparation can reach, returning invented
    records: ORCID (`fetch_orcid_profile`, which the Staff page's Preview calls for an iD
    with no account, `src/services/staff_accounts.py:257-262`), PubMed, RePORTER and
    OpenAlex.
  - **Outbound guard.** The capture tool starts a small local listener that records every
    connection attempt and answers none. It sets `HTTP_PROXY` and `HTTPS_PROXY` to that
    listener **in the serve process's environment only**:
    - `harness_env` strips inherited variables (`tests/e2e/ui_audit/env.py:52-55`);
    - the capture process's own readiness probe must not use the proxy (`run.py:88-101`).

    The capture **fails if the listener recorded any attempt**. A missed stub would
    otherwise not fail visibly: callers turn connection errors into page states, such as
    `fetch_failed` (`staff_accounts.py:257-262`), and the shot would quietly show a degraded
    page.
  - **What the guard reaches.** The outbound clients in `src/` honour these variables:
    `httpx` (when no custom `transport=` is passed), `slack_sdk` and `boto3`. The guard
    covers the paths a shot can trigger; the stubs remain the primary defence.
- **Shot list.** The list is declared in `tests/e2e/help_shots/shots.py`. Each `Shot`
  carries:
  - `id` and `audience` (`all` | `staff`);
  - `role` (a seed key) and a URL builder over the seed ids;
  - `prepare`, the Playwright steps that open a disclosure or the chat drawer;
  - `clip`, an optional element selector;
  - `alt`;
  - `sources`, the template paths the shot depicts.

  **The capture role follows the audience.** An `all` shot is captured as a seeded account
  whose `user_role` is **reviewer**: `reviewer`, or `new_reviewer` for the `intro-prompt`
  shot. A `staff` shot is captured as an account whose `user_role` is **manager**. A unit
  test over `shots.py` and the seed enforces this. It checks the seeded account's role, not
  its key. Otherwise an all-audience shot taken as a manager would show reviewers
  staff-only UI: "In the hub's words", "Assign to", the staff chat tier.
- **Output.**
  - `help_assets/shots/<id>.png` at a 1280 px viewport.
  - `help_assets/shots/manifest.json` with
    `{id: {audience, alt, width, height, sources: {path: sha256}, commit}}`.
  - Both are written to the **repository root's** `help_assets/`, not to the harness's
    temporary working directory, which `Stack.down()` deletes.
  - The first commit of the feature carries `manifest.json` as `{}`, so the directory and
    file exist before any capture.
- **What `sources` covers.** For each shot, the hashed files are:
  - the depicted templates and their full `extends`/`include`/`import` closure, including
    `base.html`;
  - `static/css/app.css`;
  - `prompts/rubric/blackbird-rubric.toml` (bands and labels are rendered into shots);
  - `tests/e2e/help_shots/seed.py`;
  - the shot's own entry in `shots.py`.
- **Commands** (documented in `tests/e2e/help_shots/README.md`):

  | Command | Needs | Does |
  |---|---|---|
  | `python -m tests.e2e.help_shots.capture [--only ID]… [--executable PATH]` | Docker plus Chromium, so a workstation; the host cannot run it (§3) | Regenerates the PNGs and their manifest entries |
  | `python -m tests.e2e.help_shots.capture --check` | Neither Docker nor a browser | Rehashes the sources and lists stale and missing shots; exit 1 if any. **Not** run by `ci.sh` (D11) |
- **Initial shot list.** The plan may refine it, keeping one or more shots per tutorial step
  where a page is shown:
  - **Audience `all`:**
    - `topbar-help`
    - `intro-prompt`
    - `assessments-list`
    - `assessment-card`
    - `card-rubric-gating`
    - `detail-brief`
    - `detail-evidence` (reviewer view)
    - `detail-panel-gating`
    - `detail-timeline`
    - `chat-drawer`
    - `review-form`
    - `pis-list-reviewer`
    - `pi-detail-reviewer`
  - **Audience `staff`:**
    - `pis-list-manager`
    - `pi-header-pending`
    - `pi-account-edit`
    - `pi-paper-review`
    - `pi-grants`
    - `pi-industry`
    - `pi-companies`
    - `slack-bots`
    - `assign-to`
    - `staff-form`
    - `staff-preview`
    - `staff-table`
    - `discussions`
    - `activity`
    - `prompt-suggestions`
    - `prompt-suggestion-detail`
- **Microsoft.** There is no Microsoft shot: the harness has no Microsoft configuration, so
  that section stays text-only.

### 8.3 Manifest use at render time

`src/web/help_pages.py` exposes `shot_meta(shot_id)`, which
`src/web/templating.py::make_templates` registers as the Jinja global `help_shot_meta`, so
`figure()` has alt, width and height. Every router's templates come from `make_templates`,
and a context key would not reach an imported macro.

The manifest is loaded **lazily**, on the first `shot_meta` call, and then cached for the
process. Loading it eagerly when `make_templates` runs would make every router import fail
whenever the file is missing. The path is resolved relative to the working directory, the same
convention as `templates/` and `static/` (`Jinja2Templates(directory="templates")`, the
`/static` mount). In the image that directory is `/app`, which holds the committed tree. The
harness serves from an isolated working directory, so `"help_assets"` is added to
`tests/e2e/ui_audit/env.py::LINKED`. Neither the help pages nor the shots route ever reads a missing or
malformed manifest silently: the §10.1 test keeps it complete. At runtime an unknown id
raises, which surfaces as a 500 in tests rather than a broken image.

### 8.4 `GET /help/shots/{shot_id}.png`

The images live outside `static/`, because `/static` is public (§3).

- **Who can fetch:**
  - `get_review_user` is required;
  - a `staff`-audience shot needs `permissions.staff`, else 403.
- **Path safety:** `shot_id` must be a manifest key (else 404), so no client string reaches
  the filesystem path.
- **Response:** a `FileResponse` with `image/png` and `Cache-Control: private, max-age=3600`.
- **Reachability gate:** the route is linked by `<img src>`, which the gate does not credit,
  so it goes into `ROUTE_ALLOWLIST` with that reason.
- **Login resume:** `src/dependencies.py::_login_location` skips `/help/shots/` in the same
  way it skips `/assessment-chat/` (`:31-51`). Without that, a lazy image fetched after the
  session ended would store a PNG URL as `next` and land the user on a raw image after
  sign-in.

## 9. Interfaces touched (summary)

| Area | Files |
|---|---|
| New | `src/routers/help.py`, `src/web/help_pages.py`, `src/web/help_intro.py`, `src/models/help.py`, `alembic/versions/0067_help_intro_dismissals.py`, `templates/help/**`, `help_assets/shots/**`, `tests/e2e/help_shots/**`, `tests/e2e/ui_audit/journeys_phase6.py`, `docs/operations/help-content.md` |
| Changed | `src/main.py` (mount); `src/models/__init__.py`; `src/web/page_context.py` (`help_intro`); `src/web/templating.py` (global `help_shot_meta`); `src/routers/workspace/__init__.py` (router dependency); `src/dependencies.py` (`pending_access_payload`, `_login_location`); `tests/unit/test_docker_build_context.py` (`MUST_KEEP` gains `help_assets`); `tests/integration/test_rendered_page_gate.py`; `tests/integration/test_reviewer_role.py`; `docs/operations/host-and-simulation.md` (pre-build path list); `templates/base.html` (nav, sub-nav, prompt); the templates in §6.6 and §7; the 36 "CoPI" templates; `static/css/app.css` (rebuild); `scripts/migrate/preflight.py`; `docs/operations/migration-deploy-notes.md`; `docs/specs/workspace-web-ui.md` (add Help to its permission table); `tests/unit/test_reachability.py` (`ROUTE_ALLOWLIST`); `tests/e2e/ui_audit/run.py` (phase 6); `tests/e2e/ui_audit/env.py` (`LINKED` gains `help_assets`); root `CLAUDE.md` (two hunks, §11) |
| Unchanged | worker, agent engine, prompts, rubric, every POST route's behaviour, the projection allowlist |

## 10. Testing and verification

### 10.1 Unit (no database)

- **Registry:**
  - `pages_for` and `tutorials_for` per capability set;
  - every `template` is a literal that exists;
  - slugs are unique;
  - tutorial step counts are 8 and 10.
- **Nav:** Help is shown for admin, manager and reviewer, and not for a PI, from the
  `base.html` condition. It is active on help pages.
- **Reserved slugs:** no registry slug is `tutorials`, `intro` or `shots`.
- **Shot roles:** in `shots.py`, every `all` shot is captured as a seeded account whose
  `user_role` is `reviewer`, and every `staff` shot as one whose `user_role` is `manager`.
- **Manifest:**
  - every `figure()` id exists in the manifest and on disk;
  - no PNG is orphaned;
  - alt text is non-empty;
  - PNG IHDR width and height equal the manifest's;
  - an `all`-audience page or step uses only `all` shots;
  - the shots total 4 MB or less.
- **PI-detail error coverage (§7.2):** the set of codes redirected to `workspace_pi_detail`
  equals the set with a sentence.
- **Stated constants (§6.3 rule 4):** each stated constant is pinned to its source.
- **"CoPI":** absent from `templates/`.

### 10.2 Integration (database)

- **Anchors (rendering, §6.6):** every `/help/...#frag` link in every rendered help page,
  tutorial step and §6.6 page:
  - rendered as a reviewer, a manager and an admin, each with Microsoft sign-in off and on;
  - opens for that viewer;
  - its fragment is an `id` in the target as rendered for that viewer.
- **`/help` routes per role:**
  - HEAD on `/help` mirrors GET (status and headers);
  - admin, manager and reviewer get 200;
  - a PI gets 403;
  - an anonymous visitor gets 302 to `/login?next=`;
  - a reviewer gets 403 on `managing-pis` and on `tutorials/manager/1`;
  - an unknown slug or step gets 404;
  - `/help/tutorials/reviewer` redirects to step 1.
- **Microsoft section:** absent when `app.state.microsoft_sign_in` is None, present when it
  is set.
- **Dismiss:**
  - the first POST creates the row and records `via`;
  - a second POST keeps the first answer;
  - an impersonated session gets 403;
  - `via=bogus` gets 400;
  - an external `next` falls back to `/workspace`;
  - Finish lands on `/help`.
- **Prompt:**
  - shown on a workspace GET for a manager or reviewer with no row;
  - not shown after dismissal, for an admin, or while an admin impersonates a manager;
  - workspace POST routes are unaffected.
- **Shots route:**
  - a reviewer, and an admin impersonating a reviewer, get 403 on a `staff` shot;
  - a reviewer gets 200 on an `all` shot;
  - an unknown id gets 404;
  - a PI gets 403;
  - an anonymous GET redirects to `/login` with no `next`.
- **Finish while impersonating:** the last step renders a plain link, not the form.
- **§7 copy:**
  - access-pending shows "Access turned off … Ask a manager or an admin" for a denied
    reviewer, "… Ask an admin" for a denied manager, and today's copy for a pending PI and
    for a session payload without the new keys;
  - the staff delete-account page shows staff consequences and Cancel to `/settings`;
  - the login page shows both blocks;
  - the PI-list labels "Muted", "Pending" and "Pending draft" appear;
  - the reviewer PIs page has no filter form;
  - the detail page shows "Approval status" and "Assign to".
- **The permission-matrix test** (`test_workspace_permissions_matrix.py`) gains the `/help`
  routes. `test_workspace_contract.py` does not: it covers the legacy alias routes only
  (`:19-105`).
  - `test_all_workspace_get_and_head_routes_follow_effective_role`
    (`test_workspace_permissions_matrix.py:359-398`) rewrites each path to its `/manager`
    twin and expects a 302 for staff. `/help` has no twin, so it gets a **separate**
    per-role case rather than being added to that loop.
- **Rendered-page gate:** `tests/integration/test_rendered_page_gate.py::test_every_get_route_is_walked_or_skipped`
  (`:319-322`) requires every new GET to be in `PAGES` or `SKIPPED`.
  - The help index, one page and one tutorial step go into `PAGES`.
  - `help_tutorial` (a redirect) and `help_shot` (a PNG) go into `SKIPPED`, with reasons.
- **Structural assertions that §7 breaks:**
  - `tests/integration/test_reviewer_role.py:441` asserts that the PI filter form is present
    for an admin impersonating a reviewer. §7.3 removes that form for non-staff, so the
    assertion is inverted.
  - The implementation also searches for structural assertions (form actions, element
    presence) on every element §7 touches, not only for the old strings.
- **Migration:** `ci.sh`'s upgrade→downgrade→upgrade round trip includes `0067`, and the
  preflight pins move.
- **Existing tests that assert the replaced strings** are updated in the same change. The
  implementation enumerates them with a grep over `tests/` for each old string.

### 10.3 Browser

`tests/e2e/ui_audit/journeys_phase6.py`, run with `python -m tests.e2e.ui_audit.run all
--phase 6`, on the existing hostile seed plus a dismissal-free reviewer and manager:

- **Reviewer:**
  1. the prompt shows on Assessments;
  2. Start the tutorial;
  3. walk steps 1–8, with every "Open this page" link returning 200;
  4. Finish;
  5. the flash shows on `/help`;
  6. the prompt is gone.
- **Manager:** the same over 10 steps. Then a Help link on each page in §6.6 lands on its
  anchor.
- **Dismiss path:** a fresh account dismisses from a workspace page and returns to the same
  URL.
- **Every help page per role:**
  - zero axe findings;
  - no console errors;
  - no unexpected network failures;
  - no horizontal scroll at 375 px.

Phases 0–5 are rerun. Their manager and reviewer accounts have no dismissal row, so the
prompt now appears on workspace pages, and it must add **zero axe findings** there.

Phase 5 compares against a local baseline report (`--baseline`, `tests/e2e/ui_audit/run.py:58-71`).
That report holds only crawl violations, 375 px overflow entries and axe counts
(`run.py:46-67`, `crawl.py:153-157`), so any difference is a regression.

1. Produce the `3bafb292` baseline in a separate `git worktree`. Never check out the mounted
   production checkout, which holds the uncommitted D5 compose edit.
2. Run phase 5 at the feature commit.
3. On violations, overflow and axe findings, the two reports must be **equal**. Nothing new
   is ever baselined. A difference caused by the prompt, the Help nav item or a §7 label is a
   defect in this feature and gets fixed.

### 10.4 Content audit and security review

- **Content audit.** After the implementation merges and the gates pass, a read-only
  adversarial audit checks every help page and tutorial step against the templates, routes
  and services they describe. It reports any claim the code contradicts, any reachable page
  or action left undocumented, and any value that breaks §6.3 rule 3. Each finding is fixed
  before deploy.
- **Security review.** It covers `src/routers/help.py`: the role gates, the shots route's
  path handling and audience check, the dismiss POST (`next` validation, impersonation
  refusal), and the session-payload change in `pending_access_payload`.

### 10.5 Gate

`./scripts/ci.sh` must be green. Browser phases 0–6 must be green in Chromium on this
workstation. `capture --check` must report no stale shots at release.

**Order.** Every shot's hashed sources include `base.html` and `static/css/app.css` (§8.2).
Any template or class change after a capture therefore marks every shot stale. So the
capture is the **last** step before deploy: after the §10.4 audit and security review, and
after their fixes. A fix made after the capture means a full recapture.
`docs/operations/help-content.md` states this ordering.

## 11. Deploy, rollback and upkeep

**Deploy.** This follows `CLAUDE.md`'s order. The change is app-only plus one migration.

1. Commit. Then check two things:
   - `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts help_assets pyproject.toml alembic.ini`
     prints nothing. The builder's `git clean -ffdx` would drop an untracked shot while a
     tracked `manifest.json` that names it still ships.
   - `python -m tests.e2e.help_shots.capture --check` reports no stale or missing shots.
2. **Tag before building**, because the build overwrites `:latest`
   (`docs/operations/migration-deploy-notes.md:691-695`). For each of `blackbird-app`,
   `worker` and `agent`, tag the image its running container actually uses (not `:latest`)
   as `copi-blackbird-<service>:rollback-pre-help`. Use
   `docker inspect -f '{{.Image}}' "$($DC ps -q <service>)"`; for the agent, use
   `$DC --profile agent ps -q agent`.
3. Build the three images:
   - `$DC build blackbird-app worker`;
   - `$DC --profile agent build agent`.

   Then tag the new `blackbird-app` image `copi-blackbird-blackbird-app:help-0067`. That
   keeps an image containing `0067` after any rollback re-tags `:latest`.

   All three images are rebuilt, for two reasons:
   - the worker reads the assessment chat record (D17) when it generates opening questions;
   - the worker and the agent both import `src.models`, which gains the new model.

   Root `CLAUDE.md` requires image/tree parity for anything the agent imports.
4. Make sure the worker is idle (no `processing` `generate_profile` job) or stopped (§5.1).
   Then run `./scripts/migrate/run_migration.sh` from the new image: rehearse first, then
   `--apply`.
5. `$DC up -d blackbird-app worker`.
6. `$DC up -d agent`, **only** when `/admin/simulation` shows no live run, because
   recreating the supervisor ends a live run. If a run is live, leave the agent on its old
   image and report it. Old agent code on the new schema is safe (§5.1).

Nothing starts a simulation run.

**Rollback.**
1. Re-tag each `:rollback-pre-help` as `:latest`. Then run `$DC up -d blackbird-app
   worker`, and `$DC up -d agent` only with no live run.
   - Old code on the new schema is safe (§5.1), so `0067` stays applied.
   - **Never downgrade first:** the new image would then 500 on manager and reviewer
     workspace pages.
2. **If the feature is reverted in git**, the `0067` revision file must stay in the tree, or
   the downgrade must run first.
   - Without the file, every later preflight BLOCKs on "at 0067, which is not a supported
     starting point" (`scripts/migrate/preflight.py:832-838`).
   - To downgrade, run a one-off container off `:help-0067` (`docker run --rm` on the
     compose network with the stack's `.env`) after the old image serves. `$DC run` and
     `run_migration.sh` always use `:latest`, which is then the old image
     (`run_migration.sh:155-157`).
3. **Revision id `0067` is never reused**, whatever happens to this feature. A later
   revision reusing it would count as already applied, and its DDL would be skipped
   silently.

**Upkeep.**
- **`docs/operations/help-content.md`** (new) says:
  - when you change a workspace, settings, login, access-pending or delete-account page,
    update its help section and the glossary or troubleshooting entries it affects;
  - run `capture --check` and recapture stale shots from a workstation;
  - keep §6.3's rules.
- **`help-content.md` also states:**
  - recaptured PNGs and `manifest.json` must be committed together;
  - capture is the last step before a deploy (§10.5);
  - the §7.2 test cannot see an error code added only inside a service.
- **Root `CLAUDE.md`** gets two hunks:
  1. one row in the "Before you… / read" table: "change a workspace, settings or login page,
     or anything under `templates/help/`" → `docs/operations/help-content.md`;
  2. `help_assets` added to the standing pre-build path list (`CLAUDE.md:90`). Every future
     recapture deploy follows that list, not the one-time command in this spec.

  `docs/operations/host-and-simulation.md:411` gets the same path-list change.

  `CLAUDE.md` already carries uncommitted edits.
  - Write the two hunks as one patch `p` against the committed file.
  - Apply it to both index and working tree: `git apply --cached p && git apply p`.
    `--cached` alone would leave the working tree without the hunks. The tree would then
    show them as pending deletions, and `tests/unit/test_claude_md_references.py` reads the
    working-tree file (`:25, 215-220`).
  - Commit only the index. The owner's existing edits stay unstaged.
  - `tests/unit/test_claude_md_references.py` must stay green.

## 12. Follow-ups found during analysis (not in this change)

1. There is no undo in the manager UI for grant and ORCID-funding vetoes, the industry veto,
   rejecting a candidate paper, or company Reject/Delete. A company can only be revived by
   re-adding it.
2. Slack Bots actions redirect to the PI detail page instead of back to Slack Bots.
3. `institution_filter` works only from the URL; no input field sets it.
4. `POST /reviews/assessments/{id}/status` (approve, disapprove, clear) is accepted from
   reviewers and managers, but no page has a control for it.
5. "Retry profile generation" is offered only for a pending agent. An active agent's
   profile can only be regenerated, which has a 1-hour cooldown.
6. The JHU tenure year cannot be set before a profile exists.
7. Mute, Exclude, the vetoes and Reject have no confirm dialog.
8. The activation-refused banner lists only profile and persona blockers. Hub-limit and
   spoke refusals appear only in the flash.
9. The prompt-suggestion status buttons ignore the current status. Pressing the current one
   re-stamps who set it.
10. The feedback form scores against the live rubric while the page shows the assessment's
    own rubric revision.
11. Managers cannot delete any feedback, including their own.
12. New staff accounts get no notification.
13. Emails (`src/services/email.py`), the CLI and the FastAPI app title still say "CoPI".
    The welcome email reads "Welcome to CoPI — pitch your lab's work to Blackbird".
14. A reviewer's PI detail omits ORCID, institution and department, which the list shows.
15. "Onboarding Complete" on the manager Account card is not explained anywhere in the UI.
    The `managing-pis#account` help section explains it; the page itself still does not.
16. The PI page's "Activation refused" box tells a manager to "ask an admin to override on
    /admin/agents": it names an admin-only page, and an admin's override waives only the
    profile checks, not the persona file, account or hub checks.
17. The assessment detail page's Dimension scores note prints an internal file path to
    users.

## Appendix A. Inventory the content starts from

Condensed from the 2026-10-14 read-only inventory, at `3bafb292`. **Every item must be
re-verified against the code when its help text is written (§6.3 rule 1).**

### A.1 What each role can reach

| Surface | Reviewer | Manager | Source |
|---|---|---|---|
| Top bar: Settings, Workspace, Sign out | Yes | Yes | `base.html:47-91` |
| Workspace: PIs, Assessments | Yes | Yes | `navigation.py:16-36` |
| Workspace: Slack Bots, Staff, Discussions, Activity, Prompt Suggestions | No (403 "Manager access required") | Yes | `navigation.py:20-25`; route gates |
| PI list: Add PI, Status / Claimed filters, and the Status, Agent, Version, Claimed, Joined, Last Login columns | No | Yes | `pis.html:12-79, 86-166` |
| PI detail: agent controls, banners, Account card, Edit Profile, profile review, revisions, Jobs, pin and veto controls, suggested companies, Add / Delete / Find companies | No | Yes | `pi_detail.html` (see A.2) |
| PI detail: Research Profile, Details, Grants (read), Industry percentile and raw score, the industry "Evidence (n)" list (read; the veto is staff-only), Companies (confirmed), Publications | Yes | Yes | `pi_detail.html:250-699, 478-489`; `directory.py:432-453` |
| Assessments list and Quick scoring | Yes | Yes | `assessments.html`; `assessments/_body.html:604-631` |
| Detail: "In the hub's words", Competitive landscape, Evidence maturity | No (removed from page data) | Yes | `presentation.py:130, 244-245`; `verdict_fields.py:63-66` |
| Feedback: add; edit own | Yes | Yes | `_detail_body.html:1176-1255`; `reviews.py:275-408` |
| Feedback: delete | No | No (admin only) | `_detail_body.html:1202-1208`; `reviews.py:411-433` |
| Assign / unassign | No | Yes (not while impersonating) | `_detail_body.html:1257-1294`; `reviews.py:483-534` |
| Assessment chat | Yes (`reviewer` tier) | Yes (`staff` tier) | `assessment_chat_record.py:221-224` |
| Staff page: create | — | Reviewer accounts only | `staff_accounts.py:173-179` |
| Discussions export, verbatim specialist reply, run held / in-doubt, LLM logs | No | No (admin only) | `discussions.py:38-39`; `_threads.html:246-260`; `activity.py:47-49` |

### A.2 Manager actions with real or permanent effects (for "Careful" boxes)

| Action | Effect | Undo in the UI | Confirm dialog | Source |
|---|---|---|---|---|
| Add PI | Creates a PI account (or adopts an agentless one), queues `generate_profile`, mints a pending agent, publishes a persona | No | No | `pi_directory.py:160-259`; `pi_onboarding.py:80-255` |
| Install Slack bot | Creates a Slack app and goes to Slack's install screen | No | No | `pi_bots.py:127-148`; `admin_provisioning.py:171-191` |
| Activate agent | The agent goes live; the running simulation picks it up at the next roster sync (~30 s); hub spoke ensured; no manager override | Mute | No | `pi_bots.py:151-231`; `agent_activation.py:43-167`; `pi_detail.html:78-82` |
| Mute / Unmute | active ↔ inactive; Unmute re-runs the activation gate | Each undoes the other | No | `pi_bots.py:65-124`; `agent_mute.py:17-59` |
| Save profile edits | New version, persona revision; a tenure change regenerates; later regenerations become drafts | Edit again | No | `profile_edit.py:380-511` |
| Regenerate / Retry | Queues generation (Regenerate: not while one runs, nor within 1 h) | No | No | `pi_profile.py:89-134, 447-475` |
| Accept / Discard draft | Writes or drops the draft | No | No | `pi_profile.py:382-444` |
| Accept / Reject candidate paper | Accept stores as manual; Reject is permanent ("never offered again") | Reject: no | No | `pi_profile.py:196-277` |
| Keep / Exclude / Restore paper | Exclude removes it from synthesis and persona; Restore undoes it | Yes (Restore) | No | `pi_profile.py:280-379` |
| Re-export persona | Rewrites the persona file | n/a | No | `pi_profile.py:478-507` |
| This is the PI / PI has no RePORTER profile / Unpin | Sets the identity and queues `enrich_grants` | Unpin | No | `pi_evidence.py:130-156, 208-339` |
| Grant / ORCID funding / industry veto | Removes the item from the persona and re-runs; industry rescoring is immediate | **No** | No | `pi_evidence.py:159-205, 342-455` |
| Add / Confirm company | Visible to BlackbirdBot and the assessment pages | Delete | No | `pi_evidence.py:458-490, 563-599` |
| Reject / Delete company | Recorded as rejected; discovery never suggests it again | Re-add by hand only | Delete: yes; Reject: no | `pi_evidence.py:536-626`; `pi_companies.py:430-445` (revive by re-adding), `:469-488` (delete), `:534-551` (reject) |
| Find companies | Queues discovery; may wait on the daily COI extraction budget | n/a | No | `pi_evidence.py:493-533` |
| Mark verified (email) | Audit event; refused while impersonating | No | No | `pi_directory.py:273-304` |
| Create / convert staff account | Creates or converts with access allowed; audit row; no notification | Revoke / Delete | Preview then Confirm | `staff.py:140-199`; `staff_accounts.py:352-406` |
| Revoke / Restore / Delete reviewer | Revoke signs them out everywhere; Delete is permanent | Revoke ↔ Restore; Delete: no | Revoke and Delete: yes | `staff.html:152-171`; `staff_accounts.py:435-479` |
| Assign / Remove | Workflow metadata only; never grants access | Each undoes the other | No | `reviews.py:483-534`; `web_permissions.py:16` |
| Generate suggestions | Queues analysis jobs (capped per press); never edits prompt files | No | No | `reviews.py:551-596`; `prompt_suggestions.html:19-55` |
| Suggestion status | Attribution only | Set another status | No | `reviews.py:609-635` |

### A.3 Sign-in and account facts

- **ORCID sign-in:** `/login` → `/login/start` → `/auth/callback`. An unauthenticated GET
  resumes at `?next=` after sign-in (`auth.py:166-169, 339-342`).
- **Session cookie:** `max_age` is the code constant `30 * 24 * 3600` (`src/main.py:294`),
  and Starlette re-issues the cookie on every session write. Not stated in help (§6.3
  rule 4).
- **First sign-in of a pre-created manager or reviewer:**
  - sets `claimed_at` and `last_login_at`;
  - skips onboarding;
  - lands on the resumed `next` page if there is one, else on `/workspace/pis` (manager)
    or `/workspace/assessments` (reviewer) (`auth.py:262-263, 302-303, 332-347`).
- **An admin** with incomplete onboarding goes to `/onboarding`, and otherwise to
  `/profile` (`auth.py:332-347`).
- **Microsoft:**
  - only for a linked admin, manager or reviewer account (`microsoft_auth.py:146-153`);
  - linking needs an ORCID re-sign-in within 15 minutes (`sign_in_session.py:22-44`);
  - unlinking signs you out everywhere (`settings.py:55-69`);
  - the first sign-in cannot be with Microsoft (`microsoft_identity.py:90-93`).
- **Sign out** bumps the session epoch, so every device is signed out (`auth.py:494-520`). A
  role change or access denial does the same (`dependencies.py:134-145`).
- **Revoked access:** any request redirects to `/access-pending` (`dependencies.py:126-132`).
- **Delete account** is offered to every role from Settings (`settings.html:8-14`). Typing
  "delete" is required, and it is refused while impersonating (`profile.py:195-239`).

### A.4 Assessment vocabulary sources

These are the sources for the glossary. Values come from the rubric at render time (§6.3
rule 3).

- **Recommendation values:** advance, conditional, route-to-incubation, and pass
  (`directory.py:125`).
  - The card chip and the list header show pass through the rubric's `banding.pass_label`
    (`assessments/_body.html:316-324`; `assessments.html:23`). That label is rubric data
    and has changed before (`prompts/rubric/blackbird-rubric.toml:59`), so it is not
    stated (§6.3 rule 3).
  - Only the summary card hard-codes "Decline" (`_body.html:174`).
- **Bands:** from the rubric, via `src/services/bands.py` and the list header
  (`assessments.html:23`).
- **Gating:**
  - states are met / not met / unconfirmed, with the legend at `_body.html:25-32` and
    `_detail_body.html:858-864`;
  - definitions come from `prompts/rubric/blackbird-rubric.toml` `[gating.*]`.
- **Panel states** (`_detail_body.html:749-815`; `_body.html:479-488`):
  - incomplete, unverified, not required, no gap recorded, not recorded.
- **Panel signals:** blocking / gap / adequate, plus the legacy caution / clear
  (`_threads.html:94-245`).
- **Lost-verdict reasons:** `_body.html:38-87`.
- **Feedback modes:** Learn, and "Don't learn — log only" (`_detail_body.html:1218-1255`).
- **Chat:**
  - error texts: `static/js/assessment_chat.js:66-91`;
  - its record documents: `assessment_chat_record.py:74-86`;
  - Clear deletes the user's turns on that assessment in every tier (`assessment_chat.py:1040-1072`).
