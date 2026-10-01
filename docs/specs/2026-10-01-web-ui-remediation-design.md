# Web UI remediation: design specification

- **Date:** 2026-10-01
- **Status:** design approved section by section in brainstorming (2026-10-01); written
  spec awaiting owner review.
- **Audit:** `docs/audits/2026-10-01-web-ui/README.md` (finding ids used below).
- **Do not push.** Owner decision Q14: committed on `blackbird` locally; scrubbed of
  exploit detail before any push, and only after Phase 0 is deployed.

## 1. Goal and scope

Fix every confirmed finding of the 2026-10-01 web UI audit, in three phases that each
deploy on their own. Refuted findings (C-30, B-18's defect claim, A-01's cohort half,
DOC-01) need no fix; B-18 and the cohort confirms get the hygiene change anyway. A-19 is
accepted with no change. Unverified findings (B-01 outside Chromium, A-02b's Slack
behaviour) are fixed defensively.

Non-goals: no change under `prompts/`; no prompt text, rubric, or bot behaviour change on
the normal run path except A-02b (D2); no change to the uncommitted
`docker-compose.prod.yml`; no change to the other deployment's nginx; no simulation start
as part of any deploy.

## 2. Decisions (owner, 2026-10-01)

| # | Decision |
|---|---|
| D1 (Q1) | Three phases, each deployed separately: Phase 0 hotfix (highs), Phase 1 (mediums, plus any low that shares a root cause with one), Phase 2 (lows, cleanup, enforced CSP). |
| D2 (Q2) | The 2026-09-29 freeze (B22/B24 of `docs/specs/2026-09-29-audit-remediation-design.md`) holds, **lifted for A-02b only**: the engine poller accepts bot messages only from known agent identities. |
| D3 (Q3) | The page markdown renderer escapes all raw HTML (a lone `<br>` becomes a line break) and sanitizes with an explicit allowlist; markdown images render as links. |
| D4 (Q4) | Replace the Tailwind Play CDN with a compiled stylesheet built by the pinned Tailwind v3.4.19 standalone CLI, committed, drift-checked in `ci.sh`. |
| D5 (Q5) | App-side CSP with per-request nonces; all inline handlers moved to listeners; `marked` and DOMPurify vendored; `POST /api/csp-report`; report-only in Phase 1, enforced in Phase 2. |
| D6 (Q6) | Sessions: `__Host-` cookie, impersonation inside the signed session, session cleared at login, `users.session_epoch` for server-side revocation (logout signs out everywhere). |
| D7 (Q7, Q15) | `users.email_verified_at`; verification only by an admin (any user) or a manager (PIs); no verification email; any address change clears it; invite acceptance and anything else that trusts the address require it. |
| D8 (Q7 amendment) | **Owner override of the repo rule "NULL means never asked and is not backfilled":** the migration marks every existing user that has an email as verified. |
| D9 (Q8) | Delete the four graph pages (`/cabo-graph`, `/scripps-graph`, `/schultz-alumni-pilot`, `/schultz-group-alumni`) and their code from the `blackbird` branch. |
| D10 (Q9) | Delete `scripts/build_cabo_sankey.py` and drop `plotly` from `pyproject.toml`. |
| D11 (Q10) | Confirm dialogs for every irreversible action; the announcing Stop names the owed-headline count; Finalize also requires the run's short id, checked server-side; "Stop — hold" keeps no dialog. |
| D12 (Q11) | WCAG 2.2 AA across the UI, held by a rendered-page gate in the test suite. |
| D13 (Q12) | Remove PostHog from this instance. |
| D14 (Q13) | Role changes on an active agent run the activation gate; no path may leave two `active` hub-role agents; inactive agents may hold a hub role. |
| D15 (Q14) | Audit record, spec and plans committed locally on `blackbird`; pushed only after Phase 0 is deployed and exploit detail is scrubbed. |
| D16 (Q16) | Remove Connect Slack; keep the `delegate_slack_ids` column and its data, unread. |
| D17 (Q17) | A committed browser harness under `tests/e2e/ui_audit/`, run before each deploy outside `ci.sh`; `ci.sh` keeps deterministic checks. |

## 3. Verified constraints

Each fact below was checked on 2026-10-01; re-check any that a task depends on.

- `./scripts/ci.sh` is the whole gate: alembic sanity and round trip, `ruff` on `tests/`
  (zero) and `src/` (ratchet), the C901 zero gate, the 200-line function gate, pytest
  with a branch-coverage floor. The reachability gate (`tests/unit/test_reachability.py`)
  credits routes from `href`/`action`/`location.href`/`fetch` in reachable templates.
- Existing JS tests assert source text only; nothing executes JavaScript in CI. The CI
  host has no Node and no Playwright browsers; `.venv-test` has the `playwright` package
  (1.62.0); host disk is 82 % used (12 GB free).
- Production web container: fastapi 0.142.2, starlette 1.7.0, sqlalchemy 2.1.1. Starlette
  1.7.0 `SessionMiddleware` takes `session_cookie`, `path` (default `/`), `https_only`,
  `domain` (default none); `Jinja2Templates` takes `context_processors`.
- `Settings` uses `extra="ignore"` (`src/config.py:103`), so removing a field never breaks
  a `.env` that still sets it.
- marked 12.0.2's renderer methods are positional: `html(html, block)`,
  `image(href, title, text)`, `link(href, title, text)`.
- Production data (read-only counts): 2 of 2,483 `agent_messages` contain raw HTML, both
  `<br>`; none in assessments, decisions or suggestions; every message resolves to a known
  agent; 74 of 79 users have no email; no delegates and no delegate invitations exist;
  production has 1 active `scout_hub` and 74 `pi_lab` agents.
- `OUTBOUND_EMAIL_ALLOWLIST` in production holds exactly one address
  (`src/services/email.py:92-99` suppresses every other recipient).
- The shared nginx (other deployment) sets, on the blackbird vhost, an enforced
  `frame-ancestors 'none'; base-uri 'self'; object-src 'none'`, `X-Frame-Options: DENY`,
  and a report-only policy whose `report-uri /api/csp-report` is proxied to this app.
  It has no `/ingest` location for blackbird.
- Templates hold 23 inline `<script>` blocks and 26 inline `on…=` handlers in 14 files;
  16 inline `<style>` blocks, none using `@apply`, `@layer`, `theme()` or
  `text/tailwindcss`; no external `<img>`.
- Each `SlackClient` learns its own `bot_id` and bot user id from `auth.test` at connect
  (`src/agent/slack_client.py:508-510`).
- Tailwind release `v3.4.19` asset `tailwindcss-linux-x64`, sha256
  `4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e` (GitHub release
  metadata).
- Head migration is `0056`; the next is `0057`.

## 4. Program structure

- **Branches:** `webui/phase-0`, `webui/phase-1`, `webui/phase-2`, each cut from
  `blackbird` after the previous phase merged, merged back when its gates pass.
- **Gates for every phase:** `./scripts/ci.sh` green; the browser harness (§9) green for
  the phase's journeys; an adversarial audit of the merged phase; every finding the phase
  closes gets its `open-findings.md` row set to `fixed` with evidence.
- **Deploy units** (`CLAUDE.md` "Deploying"):
  - Phase 0: web and worker images; no migration; no agent rebuild.
  - Phase 1: migration `0057` (rehearse without `--apply`, then apply from a one-off
    container off the new image), then web and worker, then the agent image (A-02b),
    recreated only when `/admin/simulation` shows no live run. Everyone is signed out
    once (cookie rename).
  - Phase 2: web and worker images.

## 5. Phase 0 — hotfix

### 5.1 Shared confirm (A-01)

- New `static/js/confirm.js`, loaded by `templates/base.html` (and by any standalone
  template with a confirmable form). One `submit` listener on `document`: if the form
  has `data-confirm`, call `confirm(form.dataset.confirm)` and `preventDefault()` on a
  false answer.
- Replace the three inline confirms with `data-confirm`:
  `templates/admin/user_detail.html:178`, `templates/admin/cohorts.html:108`,
  `templates/admin/cohort_detail.html:28`. The message is an autoescaped attribute value
  and is never evaluated as script.

### 5.2 Page markdown renderer (A-02)

- In `static/js/markdown.js`, the `page` profile of `createSanitizingMarked` gets the
  chat profile's raw-HTML handling: the `tag` tokenizer that never enters the raw-block
  state, and an `html` renderer that escapes the token — except a token matching
  `^<br\s*/?>$` (case-insensitive), which renders `<br>`. The `image` renderer returns an
  escaped link: `<a href="…">alt text or the URL</a>`.
- `renderMarkdown` uses a private `page` instance and
  `DOMPurify.sanitize(html, PAGE_PURIFY)`:
  `ALLOWED_TAGS = p br strong em code pre blockquote ul ol li a h1 h2 h3 h4 h5 h6 hr
  table thead tbody tr th td`, `ALLOWED_ATTR = href`,
  `ALLOWED_URI_REGEXP = /^(?:https?:|mailto:|#)/i`.
- `templates/cabo_graph.html`'s modal switches to the same sanitize config until the page
  is deleted in Phase 1.

### 5.3 Simulation panel refresh (C-01, C-08, C-09, C-27, FN-05)

In `templates/admin/simulation.html`:

- Refresh by unit, not `#sim-body` wholesale. The units are the jump nav
  (`nav.sim-jump-nav`) and every `<section id>` (today `sec-status`, `sec-controls`,
  `sec-announce`, `sec-history`, `sec-run`, and, only when a run is selected,
  `sec-cost`, `sec-outcomes`, `sec-panel`, `sec-calls`, `sec-agents`, `sec-timeline`).
  Reconcile by `id` against the fetched page: replace a unit present in both, insert a
  new one at its position in the fetched page, remove one the fetched page no longer
  has. Never replace or remove a unit that contains `document.activeElement`, or that
  contains a form any of whose controls differs from its default (`value` vs
  `defaultValue`, `checked` vs `defaultChecked`, `selected` vs `defaultSelected`). Keep
  the existing restore of open `details[data-sc-key]`.
- The `msg`/`error` banners above the status card are not refresh units; the first
  successful refresh removes them.
- Fetch `/admin/simulation` with the current `run` query parameter only, never
  `location.href`. Accept a response only if `ok`, not `redirected`, and `text/html`.
  Otherwise stop the timer and show a visible notice in a `role="status"` element:
  "Auto-refresh stopped — reload the page." A network error retries on the next tick
  and stops after three consecutive failures. Add a "last updated" time.
- On first load, `history.replaceState` removes `msg` and `error` from the URL.
- The run selector form gets `action="/admin/simulation"`.

### 5.4 Timeline toggle (B-01)

In `templates/admin/_assessment_detail_body.html`, one `toggle` listener on `document`
in the capture phase: when a `<details>` opens, re-run the clamp `check` for every
`[data-unclamp]` inside it. Setting `open` from "Expand all", `data-open-details` links
or the chat's "Show in page" fires the same event.

### 5.5 Harness and records

- `tests/e2e/ui_audit/`: the audit harness from `docs/audits/2026-10-01-web-ui/raw/harness/`
  made reusable (§9).
- The audit record and its `open-findings.md` rows are committed with this spec (D15).

## 6. Phase 1

### 6.1 Compiled Tailwind (A-03)

- `scripts/build_css.sh`: download the pinned CLI to `.tools/` (gitignored), verify its
  sha256 (§3), run it with a new `tailwind.config.js` (`content`: `templates/**/*.html`,
  `static/js/**/*.js`, `src/**/*.py`; `safelist` from an inventory of runtime-built
  class names, made in the plan's first task) and `static/css/input.css`, writing the
  committed, minified `static/css/app.css`.
- `templates/base.html` replaces the CDN `<script>` with
  `<link rel="stylesheet" href="/static/css/app.css">`.
- `ci.sh` rebuilds to a temporary file and fails when it differs from the committed one.
- Parity: the harness screenshots a fixed page set under the CDN and under the compiled
  CSS; every pixel difference is reviewed before merge.

### 6.2 Vendored scripts (M-04)

`static/vendor/marked-12.0.2.min.js` and DOMPurify 3.4.16 (the npm `latest` on
2026-10-01; re-check at implementation and take the newest 3.4.x), with
`static/vendor/MANIFEST.md` (source URL, version, sha384) and a unit test that recomputes
each hash. `templates/_head_assets.html` loads them from `/static/vendor/`. marked stays
on 12.x because later majors replace the positional renderer API used in §5.2.

### 6.3 CSP and security headers (M-03, A-06)

- A middleware creates a per-request nonce (`request.state.csp_nonce`); every inline
  `<script>` carries `nonce="{{ request.state.csp_nonce }}"`.
- Every inline `on…=` handler moves to `static/js/ui.js`, driven by data attributes
  (`data-confirm` from §5.1, `data-row-href`, `data-autosubmit` until Phase 2 removes it,
  and the page-specific ones the inventory finds).
- Response headers on every response:
  - Enforced: `Content-Security-Policy: frame-ancestors 'none'; base-uri 'none';
    object-src 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
    `Referrer-Policy: strict-origin-when-cross-origin`.
  - Report-only (Phase 1): `default-src 'self'; script-src 'self' 'nonce-<n>';
    style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self';
    connect-src 'self'; form-action 'self'; report-uri /api/csp-report`.
- `POST /api/csp-report`: no auth; exempted from `OriginGuardMiddleware` for that exact
  path; accepts `application/csp-report` and `application/reports+json`; body capped at
  16 KB; logs one structured line per report (directive, blocked URI, document URI);
  returns 204.

### 6.4 Removals (A-12, A-13, M-05, D9, D10, D13, D16)

- Graph pages: the four routes, `_graph_csp`, `_render_graph`, the graph payload,
  component and institution helpers and constants in `src/routers/public.py`;
  `templates/cabo_graph.html`; the `graph` profile in `static/js/markdown.js`;
  `tests/integration/test_public_graph.py`; the graph parts of
  `tests/characterization/test_public_routes.py`, `tests/unit/test_reachability.py`,
  `tests/unit/test_markdown_renderer_config.py`, `tests/e2e/seed.py`,
  `tests/e2e/test_browser_flows.py`; the graph rate-limit block in `nginx/nginx.conf`.
- `scripts/build_cabo_sankey.py`; `plotly` from `pyproject.toml`.
- PostHog: the block in `templates/base.html`, `PostHogContextMiddleware`, the
  `posthog_api_key` setting, `tests/integration/test_posthog_snippet.py`,
  `tests/unit/test_posthog_middleware.py`, and its references in
  `tests/unit/test_config_secret_redaction.py`.
- Connect Slack: `delegate_connect_slack` (`src/routers/agent_page.py:712`), the dashboard
  banner and `delegate_has_slack`, `_resolve_delegate_names`, `_user_slack_id_in_list`,
  the Slack lookup on invite accept (`src/routers/invite.py:221-249`), and the Slack-id
  cleanup in `remove_delegate`. `agents.delegate_slack_ids` stays, unread.

### 6.5 Engine poller (A-02b)

In `src/agent/engine/slack_io.py` `_poll_slack_for_bot_messages`: build
`{bot_id: agent_id}` and `{bot_user_id: agent_id}` from the connected clients each poll;
accept a message only if its `bot_id` or `user` is in either map; take `sender_agent_id`
from the map and `sender_name` from the registry's `bot_name`; skip an unknown sender,
advance the cursor, log one INFO line with channel and ts and no content.

### 6.6 Email trust (A-04, A-09, D-06)

- Migration `0057`: `users.email_verified_at timestamptz NULL`; backfill
  `email_verified_at = now()` where `email IS NOT NULL` (D8); `users.session_epoch
  integer NULL` (§6.7). Downgrade drops both columns.
- Every write that changes `users.email` (`assign_user_email`, `/profile/save`, the
  manager PI form, onboarding) clears `email_verified_at` when the address changes.
- `POST /admin/users/{id}/verify-email` (`get_admin_user`) and
  `POST /manager/pis/{id}/verify-email` (`get_staff_user`, PI targets only): both refuse
  under impersonation, write an `AdminAuditEvent`, and the manager write-allowlist test
  (`tests/integration/test_manager_views.py::test_manager_router_mutations_are_an_explicit_allowlist`)
  is extended.
- Invite accept (`GET /invite/{token}`, `POST /invite/{token}/accept`) resolves the user
  through `get_current_user`, requires `may_use_pi_surfaces`, and requires
  `users.email` to equal the invited address case-insensitively with
  `email_verified_at` set. Unverified: "An administrator must verify your email address
  before you can accept this invitation."

### 6.7 Sessions (A-05, A-11)

- Cookie name `__Host-copi-session` when `https_only` is on; `copi-session` when
  `allow_http_sessions` is set.
- Impersonation in `session["impersonate_user_id"]`; the `copi-impersonate` cookie is no
  longer read or written (`src/dependencies.py:118`,
  `src/routers/admin/impersonation.py:48,73`, `src/routers/auth.py:370`,
  `src/routers/profile.py:238`).
- Login clears the session and keeps only the vetted `next`.
- Login stores `users.session_epoch` (NULL counts as 0) in the session;
  `get_current_user` rejects a session whose epoch differs. Logout bumps the epoch by
  reading `session["user_id"]` directly (it keeps no auth dependency). Deny and role
  change bump it too; a deleted user's sessions already fail the user lookup.

### 6.8 Admin controls and data integrity

- **C-04:** the edit form offers `pending` only for a pending agent; the server refuses
  `agent_status=pending` otherwise.
- **C-05, D14:** `ensure_activation_allowed(db, agent, new_role, new_status)` runs
  `activation_blockers` for the new role and refuses a change that would leave two
  `active` hub-role agents, under a transaction-scoped advisory lock with a key
  registered in `src/services/advisory_locks.py`. Called by approve, the edit form, the
  role route and the manager's activate.
- **C-07:** the topology form posts the cells it rendered checked; the save deletes only
  rendered-checked-now-unchecked memberships and adds only now-checked cells.
- **C-10, FN-01, FN-06, B-10:** `data-confirm` on Finalize, the announcing Stop
  ("Posts N owed headlines to Slack, M of them from interviews still open; cannot be
  undone", from the live run's funnel `headlines_owed` and `provisional` in
  `src/services/simulation_stats.py`), Reset to file default and review delete.
  Finalize also carries `confirm_run`, which
  `admin_simulation_finalize_run` requires to equal the run id's first 8 characters.
- **D-01, D-02, D-08:** `apply_profile_edits` validates the email before any write;
  writes tenure only when the year differs from `get_tenure_start`; refuses while that
  user has a `generate_profile` job `pending` or `processing` ("Profile is being
  generated — try again shortly"); the profile-row insert runs under
  `SET LOCAL lock_timeout = '5s'` and the lock error maps to the same message. Applies
  to `/profile/save`, the manager form and the onboarding save.
- **D-16:** tag widgets post one repeated hidden field per tag; handlers read
  `form.getlist(...)`; the comma split in `src/services/profile_edit.py:31-32` is
  removed (profile edit, onboarding review, public profile, manager PI forms).
- **B-06:** the chat textarea is `readOnly` with `aria-busy="true"` while busy, and is
  refocused when the answer finishes or is abandoned.

### 6.9 Error pages and flash (M-01, FN-09, A-14)

- One exception handler for `HTTPException` and `RequestValidationError`: a 3xx passes
  through with its `Location`; JSON for paths under `/assessment-chat/` and `/api/` and
  for requests whose `Accept` names `application/json`; otherwise `templates/error.html`
  (base layout without the account area, a plain title per status, "Back" and "Home"
  links), same status code.
- `src/web/flash.py`: `flash(request, text, kind)` stores in the session; a context
  processor registered in `make_templates()` pops messages into `base.html`'s flash
  block. Phase 1 moves `delegate_error`, `slack_error` and `error=` text messages onto
  it.

### 6.10 Accessibility, medium items (X-01, X-02, X-05)

- Text classes `text-gray-300`/`text-gray-400` become `text-gray-600` in templates,
  `static/js` class strings and `src/` class strings; white text on `bg-green-600`
  becomes `bg-green-700`. The harness's axe run must report zero `color-contrast`
  violations.
- Every form control gets an accessible name (`label for`/`id`; `aria-label` on matrix
  checkboxes naming agent and cohort, on tag-remove buttons naming the tag, and on the
  impersonate input).
- Each clickable row gets an `<a href>` in its first cell; `ui.js` keeps row clicks via
  `data-row-href`.
- Gate: a DB-backed integration test renders every GET page as admin, manager, reviewer
  and PI with factory data and fails on a control without an accessible name,
  `text-gray-300`/`text-gray-400`, `<tr onclick>`, or any inline `on…=` attribute.

## 7. Phase 2

- **CSP:** switch the report-only policy of §6.3 to enforced, keeping `report-uri`.
- **Auth and validation:** A-07 (`ensure_admin_remains` in deny and delete; deny refuses
  self); SN-01 (allowlist add promotes only `pending`); A-10 (impersonation note or
  `mechanism="web_impersonated"` on every write made while impersonating); A-15 (length
  checks with a form error); A-16 (404 for an unknown ORCID); A-17 (PI-target check);
  SN-02 (at most 10 addresses per submission and 25 invitations per agent per 24 h);
  B-04 (`ui.js` disables a submitted form's buttons; the server rejects an identical
  review by the same reviewer on the same assessment within 60 s); B-11 (`maxlength`;
  400 on overlength or NUL); D-18 (strip an `orcid.org/` prefix, uppercase a trailing
  `x`).
- **Data integrity:** C-06 (valid UUID, existing unlinked user, role that may own a
  lab); C-24 (`SELECT … FOR UPDATE` on the run row in start and finalize); C-29 (reject
  only `pending`); D-07 (`UPDATE … WHERE status='pending'` with a rowcount check);
  D-15 (refuse an empty save when no profile exists); D-17 (map the `users.orcid`
  violation to "already exists"); D-19 (remove only the vetoed title; idempotent veto);
  D-11 (one token predicate, `token_for_agent_row`).
- **Feedback and flow:** move `saved`, `slack_ok`, `msg` flags onto flash (C-03, D-09,
  D-10); B-03 (flash "Moved to Reviewed" and anchor the next card); B-13 (unknown
  `run_id` falls back to the current run); D-12; D-14 (reject redirected or non-HTML
  fragment responses; set `aria-expanded` on error); D-20; D-21; D-26 (conversations
  pager); D-27 (pager whenever `message_total > 0`, page clamped); M-08 (managers and
  reviewers redirected from PI-only pages); FN-02 (onboarding shows the effective user's
  email); FN-08 (invite pages get the normal context); C-02 (export renders summaries
  server-side).
- **Performance:** C-14 (25 s cache of the live-run stats context per run); C-15
  (discussions and PI list paged in SQL; HTML refuses `run_id=all` above a cap); C-16
  (`llm_calls` defers large text columns and loads one call's bodies on expand through
  a new fragment route); B-07 (select only the tool-use slice); B-08 (polling skips the
  stale sweep).
- **Correctness and copy:** C-18 (filters from the enum; unknown value refused without a
  500); C-19 (`le=MAX_PAGE`); C-20 (server-side `>= 0`); C-21, C-22 (labels and the
  latency source made consistent); C-23 (deterministic hub choice); B-09, D-28 (copy);
  B-18 (`urlencode`); M-02 (`HEAD` on every GET route, answered without a body); FN-07
  (one timestamp formatter that always shows the zone).
- **JS:** B-16 (re-evaluate drawer modality on resize); B-17 (keep scroll and focus on
  re-render); B-20 (scroll the target clear of the drawer).
- **Accessibility, low items:** X-03 (underline links in text); X-04 (labelled `<nav>`
  sub-navs, `h1` on login and error pages, heading order, empty header; the §6.10 gate
  adds landmark and `h1` checks); X-06 ("announced" as a text column, `role="img"` on
  glyphs, `fieldset`/`legend` for the dimension selects); FN-04 (Apply buttons instead of
  submit-on-change).
- **Responsive:** R-01, FN-03 (wrapping filter rows; `overflow-x-auto` table wrappers;
  `grid-cols-2 sm:grid-cols-5` summary tiles); R-02 (`break-words` on markdown, chat
  bubbles, profile fields, tag pills; `overflow-x-auto` on markdown `pre` and `table`).

## 8. Finding-to-phase map

Phase 0: A-01, A-02, B-01, C-01, C-08, C-09, C-27, FN-05.

Phase 1: A-02b, A-03, A-04, A-05, A-06, A-08, A-09, A-11, A-12, A-13, A-14 (text
messages), B-06, B-10, C-04, C-05, C-07, C-10, D-01, D-02, D-03, D-04, D-05, D-06, D-08,
D-16, FN-01, FN-06, FN-09, M-01, M-03, M-04, M-05, X-01, X-02, X-05.

Phase 2: A-07, A-10, A-14 (remaining flags), A-15, A-16, A-17, B-03, B-04, B-07, B-08,
B-09, B-11, B-13, B-16, B-17, B-18, B-20, C-02, C-03, C-06, C-14, C-15, C-16, C-18,
C-19, C-20, C-21, C-22, C-23, C-24, C-29, D-07, D-09, D-10, D-11, D-12, D-14, D-15,
D-17, D-18, D-19, D-20, D-21, D-26, D-27, D-28, FN-02, FN-03, FN-04, FN-07, FN-08, M-02,
M-08, R-01, R-02, SN-01, SN-02, X-03, X-04, X-06, and the enforced CSP.

No change: A-19 (accepted), C-30 and DOC-01 (refuted).

## 9. Verification

- **Deterministic (in `ci.sh`):** a test per refusal and per allowed path in §5-§7;
  rendered-HTML assertions (no inline handlers or `onsubmit` with data, `data-confirm`
  present, `confirm.js` and `ui.js` loaded, nonces on every inline script, headers
  present, error pages by `Accept` and path); the markdown allowlist and escaping in
  `markdown.js` source; vendored-asset hashes; the CSS drift check; the §6.10 gate;
  migration `0057` round trip via `ci.sh`.
- **Browser harness (`tests/e2e/ui_audit/`, before each deploy):** a throwaway Postgres
  named `uiaudit-pg-<pid>` on a free port, migrated to head; the app with a fake model;
  the adversarial seed; the role x route crawl (expected-status table, XSS canary 0, no
  page errors, overflow report); axe-core; and journeys per phase:
  - Phase 0: delete with a script-breaking name and with an apostrophe name (the dialog
    shows, nothing runs, cancel keeps the user); injected transcript form (no form in
    the DOM); Start-form values survive two refreshes; the notice appears after the
    session cookie is removed; the timeline toggle is visible after opening (Chromium;
    Firefox attempted and reported unverified if its download fails).
  - Phase 1: login, logout, second-device sign-out; impersonate and stop; invite accept
    verified and unverified; confirm dialogs accepted and dismissed; cohort two-tab
    save; chat focus after an answer; zero `color-contrast` violations; CSP reports from
    a full crawl collected and reviewed; CSS parity screenshots.
  - Phase 2: the enforced CSP produces no console violation on any crawled page; 375 px
    crawl reports no document overflow on the seeded realistic data.
- **Adversarial audit** after each phase merges, before deploy.

## 10. Rollback

Each phase is tagged before deploy (`rollback-pre-webui-<n>`). Phase 1's migration is
additive; rolling the code back leaves the two columns unused. Rolling back Phase 1 after
deploy signs everyone out again (cookie name reverts).

## 11. Risks

- **CSS parity (§6.1):** a runtime-built class missed by the safelist renders unstyled;
  mitigated by the inventory and the screenshot comparison.
- **CSP (§6.3, §7):** a missed inline handler breaks a control once enforced; mitigated by
  the report-only phase and the gate's inline-handler check.
- **Sign-out at deploy (§6.7):** every user signs in again once.
- **Email verification (§6.6):** an existing account whose address was changed by its
  holder is grandfathered as verified (D8).
- **Firefox and Safari (B-01):** the fix is defensive; browser coverage beyond Chromium
  depends on the harness obtaining those browsers.
