# Web UI remediation — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo, plan execution goes through `/engineering:plan-execution`.

**Goal:** Close the medium-severity web UI findings (and the lows that share their root causes): compiled CSS, vendored scripts, an app-side CSP in report-only mode, removal of the graph pages, PostHog and Connect Slack, the engine's sender gate, email verification, hardened sessions, admin-control integrity, HTML error pages with one flash mechanism, and WCAG 2.2 AA for the medium accessibility items.

**Architecture:** Three parts, executed in order. **Part 1A** changes the asset pipeline and the response headers and deletes three features. **Part 1B** adds migration `0057` (`users.email_verified_at`, `users.session_epoch`), gates the engine poller on agent identity, and moves impersonation into the signed session. **Part 1C** builds on both: confirmations, the single-active-hub invariant, error pages, flash, profile-save integrity, tag fields, and the accessibility gate.

**Tech Stack:** FastAPI 0.142.2 / Starlette 1.7.0, SQLAlchemy 2.1.1 (async), Alembic, Jinja2 3.1.6, vanilla JS, marked 12.0.2 and DOMPurify 3.4.x (vendored), Tailwind CSS 3.4.19 standalone CLI, Playwright harness (Phase 0).

**Spec:** `docs/specs/2026-10-01-web-ui-remediation-design.md` §6, with amendments A1–A16 in §12. Audit: `docs/audits/2026-10-01-web-ui/README.md`. Prerequisite: Phase 0 merged and deployed (`docs/plans/2026-10-01-web-ui-remediation-phase-0.md`).

## Global Constraints

- Branch `webui/phase-1` cut from `blackbird` after Phase 0 merged. Never push `origin` (D15).
- Freeze (D2): no prompt, rubric or bot behaviour change except Task 1B-2 (A-02b).
- Deploy order (spec §4): migration `0057` (rehearse without `--apply`, then apply from a one-off container off the new image) → web and worker → agent image recreated only when `/admin/simulation` shows no live run. Everyone is signed out once.
- `./scripts/ci.sh` green at every merge point, including the new CSS drift check (Task 1A-9).
- Any task that adds or changes a Tailwind class in `templates/`, `static/js/` or `src/` after Task 1A-9 reruns `scripts/build_css.sh` and commits `static/css/app.css` in the same commit.
- Never touch `docker-compose.prod.yml`; no change under `prompts/`; no simulation start.

## Execution order and shared files

Run the parts in order **1A → 1B → 1C**, and **every task sequentially in numbered order — no
parallel execution**. The table below lists every file named in more than one task's **Files**
block (computed from this document, 2026-10-01), but it is not exhaustive: some tasks edit
files by pattern rather than by name (Task 1A-7 adds nonces to every template with an inline script; Task 1C-12 rewrites contrast classes in every matching file),
so no safe parallel partition can be derived from it (assembly audit PX-09). A later task's
"before" text is written against the earlier task's result.

| File | Tasks |
|---|---|
| `tests/e2e/ui_audit/journeys_phase1.py` | 1A-5, 1A-6, 1A-8, 1A-9, 1B-10, 1C-16 |
| `src/main.py` | 1A-3, 1A-7, 1A-8, 1B-3, 1C-2 |
| `templates/base.html` | 1A-3, 1A-6, 1A-9, 1C-1, 1C-2 |
| `templates/manager/pi_detail.html` | 1B-7, 1C-5, 1C-9, 1C-10, 1C-13 |
| `tests/integration/test_agent_page.py` | 1A-4, 1B-8, 1C-2, 1C-5, 1C-10 |
| `src/routers/manager.py` | 1B-7, 1C-5, 1C-6, 1C-10 |
| `templates/admin/simulation.html` | 1A-6, 1C-4, 1C-8, 1C-13 |
| `templates/manager/pis.html` | 1A-6, 1C-5, 1C-13, 1C-14 |
| `src/routers/agent_page.py` | 1A-4, 1C-5, 1C-10 |
| `templates/admin/agent_detail.html` | 1C-5, 1C-6, 1C-13 |
| `templates/admin/cohort_topology.html` | 1C-3, 1C-7, 1C-13 |
| `templates/admin/cohorts.html` | 1A-6, 1C-3, 1C-13 |
| `templates/admin/discussions.html` | 1A-6, 1C-12, 1C-13 |
| `templates/admin/jobs.html` | 1A-6, 1C-12, 1C-13 |
| `templates/admin/users.html` | 1A-6, 1C-13, 1C-14 |
| `templates/agent/dashboard.html` | 1A-4, 1C-5, 1C-13 |
| `templates/agent/public_profile.html` | 1C-9, 1C-10, 1C-13 |
| `templates/manager/discussions.html` | 1A-6, 1C-12, 1C-13 |
| `templates/onboarding/profile_review.html` | 1C-9, 1C-10, 1C-13 |
| `templates/profile/edit.html` | 1C-9, 1C-10, 1C-13 |
| `tests/integration/test_cohort_admin.py` | 1B-3, 1C-3, 1C-7 |
| `tests/unit/test_phase1_removals.py` | 1A-2, 1A-3, 1A-4 |
| `CLAUDE.md` | 1A-9, 1-Z |
| `nginx/nginx.conf` | 1A-2, 1A-3 |
| `src/dependencies.py` | 1B-4, 1B-5 |
| `src/models/user.py` | 1A-4, 1B-1 |
| `src/routers/admin/agents.py` | 1C-5, 1C-6 |
| `src/routers/admin/cohorts.py` | 1C-3, 1C-7 |
| `src/routers/admin/simulation.py` | 1C-4, 1C-8 |
| `src/routers/admin/users.py` | 1B-5, 1B-7 |
| `src/routers/auth.py` | 1B-4, 1B-5 |
| `src/routers/invite.py` | 1A-4, 1B-8 |
| `src/routers/profile.py` | 1B-4, 1C-10 |
| `src/services/profile_edit.py` | 1C-9, 1C-10 |
| `src/services/user_email.py` | 1A-4, 1B-6 |
| `src/web/errors.py` | 1C-2, 1C-2b |
| `static/css/app.css` | 1A-9, 1C-12 |
| `static/css/input.css` | 1A-9, 1C-10 |
| `static/js/ui.js` | 1A-6, 1C-14 |
| `templates/admin/_assessment_chat_drawer.html` | 1A-3, 1C-11 |
| `templates/admin/_discussions_threads.html` | 1A-6, 1C-14 |
| `templates/admin/activity.html` | 1A-6, 1C-14 |
| `templates/admin/activity_detail.html` | 1C-4, 1C-8 |
| `templates/admin/cohort_detail.html` | 1C-3, 1C-13 |
| `templates/admin/user_detail.html` | 1B-7, 1C-13 |
| `templates/manager/activity.html` | 1A-6, 1C-14 |
| `tests/characterization/test_auth_and_admin_routes.py` | 1B-3, 1B-4 |
| `tests/integration/test_double_submits.py` | 1A-4, 1B-8 |
| `tests/integration/test_error_pages.py` | 1C-2, 1C-2b |
| `tests/integration/test_finalize_run_route.py` | 1C-4, 1C-8 |
| `tests/integration/test_onboarding_flow.py` | 1B-4, 1C-10 |
| `tests/integration/test_origin_guard.py` | 1A-7, 1A-8 |
| `tests/integration/test_prompt_suggestions_page.py` | 1A-5, 1B-4 |
| `tests/integration/test_rendered_page_gate.py` | 1C-13, 1C-15 |
| `tests/session_support.py` | 1B-3, 1B-4 |
| `tests/unit/test_csp_templates.py` | 1A-7, 1A-9 |
| `tests/unit/test_no_dead_src_symbols.py` | 1A-3, 1C-10 |
| `tests/unit/test_reachability.py` | 1A-2, 1A-3 |
| `tests/unit/test_ui_behaviours.py` | 1A-6, 1C-14 |

## Review Focus (cross-part)

Each part has its own review-focus section. The cross-part risks, verified by Task 1-Z's full harness run and adversarial audit:

1. **Confirm and ui.js together:** a form with `data-confirm` that `ui.js` also auto-submits (`data-autosubmit`) must show the dialog once and submit only on Yes.
2. **Session reset and flash:** login clears the session (1B-5) and flash lives in the session (1C-1): a flash set just before a login redirect is dropped. That is acceptable; a flash set after login must survive one page view.
3. **Impersonation in the session and the error page:** an admin impersonating a PI who hits a 403 sees the error page, and "Stop impersonating" still works from any page.
4. **CSP report-only on every page:** the full crawl must produce no report for any directive other than ones already accepted in Task 1A-8's review.
5. **Compiled CSS covers runtime-built classes:** the parity journey (Task 1A-9) and the contrast journey (Task 1C-16) both pass on the same build.

---

## Part 1A: Compiled CSS, vendored scripts, CSP and removals

**Scope:** spec §6.1 (compiled Tailwind), §6.2 (vendored marked and DOMPurify), §6.3 (CSP,
nonces, inline handlers moved to `static/js/ui.js`, security headers, `POST /api/csp-report`),
§6.4 (removal of the graph pages, the Sankey script and plotly, PostHog, and Connect Slack).
Findings: A-03, M-04, M-03, A-06, A-12, A-13, M-05, A-08, D-03, D-04, D-05.

### Global constraints (this part)

- Tailwind: release `v3.4.19`, asset `tailwindcss-linux-x64`, sha256
  `4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e`. The CLI goes in `.tools/`,
  which is gitignored. `tailwind.config.js` `content`: `templates/**/*.html`,
  `static/js/**/*.js`, `src/**/*.py`. The output is the committed, minified
  `static/css/app.css`. `ci.sh` rebuilds to a temporary file and fails when it differs.
- marked `12.0.2` (stays on 12.x because later majors replace the positional renderer API).
  DOMPurify: the newest 3.4.x when the task runs (3.4.16 on 2026-10-01).
  `static/vendor/MANIFEST.md` records source URL, version and sha384, and a unit test
  recomputes each hash.
- The enforced header, verbatim: `Content-Security-Policy: frame-ancestors 'none'; base-uri 'none';
  object-src 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`.
- The report-only header in Phase 1, verbatim: `default-src 'self'; script-src 'self' 'nonce-<n>';
  style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self';
  form-action 'self' https://slack.com https://*.slack.com; report-uri /api/csp-report`. Phase 2 enforces it, and this part only
  defines the switch.
- `POST /api/csp-report`: no auth. Exempt from `OriginGuardMiddleware` for that exact path only.
  Accepts `application/csp-report` and `application/reports+json`. Body capped at 16 KB. Logs one
  structured line per report (directive, blocked URI, document URI). Returns 204.
- `agents.delegate_slack_ids` (`AgentRegistry.delegate_slack_ids`, `src/models/agent_registry.py:45`)
  keeps its column and its data. No migration in this part.
- Line numbers below are from `blackbird@e8f475f`, before Phase 0. Phase 0 edits
  `templates/base.html`, `templates/admin/simulation.html`, `templates/admin/user_detail.html`,
  `templates/admin/cohorts.html`, `templates/admin/cohort_detail.html`,
  `templates/admin/_assessment_detail_body.html`, `static/js/markdown.js` and
  `templates/cabo_graph.html`. In those files, find each edit by its quoted text, not by its
  line number.
- **Ordering against Part 1C:** Tasks 1A-6 and 1A-7 rewrite attributes and `<script>` tags in
  templates that Part 1C later edits for accessibility. Run them before any Part 1C template
  task.
- **Ordering against Part 1B:** Part 1B also edits `src/main.py` (the `SessionMiddleware` block
  and `SESSION_COOKIE`), `src/routers/invite.py` (above line 200),
  `src/models/user.py` and `src/services/user_email.py`. This part's hunks in those files are
  only the ones quoted below.
- **After all Phase 1 parts merge:** `static/css/app.css` is generated from every template,
  JS file and `src/` file. Parts 1B and 1C change those files, so the parent must run
  `bash scripts/build_css.sh` and commit `static/css/app.css` before the integrated
  `./scripts/ci.sh`. Otherwise the drift step fails, correctly.

### Review focus (this part)

1. **Bypass shapes for the exact-path Origin exemption**: a trailing slash, a different case,
   a prefix match, and another method. The owning task, 1A-8, adds
   `test_only_the_csp_report_path_is_exempt_from_the_origin_check`.
2. **Hostile report bodies**:
   - an over-cap body, both declared by `Content-Length` and streamed chunked without one;
   - deeply nested JSON, which raises `RecursionError` and not `ValueError`;
   - a wrong content type, and malformed JSON;
   - 100 reports in one request, which multiplies log lines;
   - a newline inside a field, which could forge a log line.

   The owning task, 1A-8, adds a test for each case.
3. **A click on a control inside a `data-row-href` row**: the ORCID link used to call
   `stopPropagation()`. 1A-6 pins the `INTERACTIVE` selector at source level. Its
   `journey_ui_behaviours` clicks the ORCID link and asserts the list page stays put.
4. **A runtime-built utility class missing from the safelist**: it renders unstyled once the
   CDN is gone. 1A-9's tests derive every built class from its source (`band_class`, the
   `status_meta` dicts, the jobs colour loop). They also fail on any new class-building site
   in templates, `src/` or JS. `journey_css_parity` screenshots the CDN build against the
   compiled build.
5. **An inline `<script>` added after this part with no nonce**, for example by Phase 0 or
   Parts 1B and 1C. 1A-7's source-level test scans every template, so the integrated
   `ci.sh` catches a script that a later part adds. The rendered test checks the nonce
   against the header value.

Not a review-focus test, but handed on: Starlette's `ServerErrorMiddleware` sits outside every
user middleware (`fastapi/applications.py` `build_middleware_stack`, lines 1033-1063 in fastapi
0.141.1; the same structure in 0.142.2). An unhandled-exception 500 therefore never passes
through `SecurityHeadersMiddleware`. This part exports `security_header_items(nonce)` so Part
1C's 500 handler can add the same headers to its response. `request.state.csp_nonce` is already
set on that request, because the middleware writes into the shared `scope["state"]` dict.

### Inventory (Task 1A-1 output; the later tasks cite these ids)

#### Runtime-built Tailwind class names (need `safelist`)

| id | site | how it is built | values | classes produced |
|---|---|---|---|---|
| R1 | `templates/admin/discussions.html:52-54` | `hover:border-{{ meta.color }}-300`, `ring-{{ meta.color }}-400`, `text-{{ meta.color }}-600`; `meta` from `status_meta` (lines 41-47) | gray, blue, green, amber, red | `hover:border-{c}-300`, `ring-{c}-400`, `text-{c}-600` |
| R2 | `templates/manager/discussions.html:52-54` | same as R1; `status_meta` at lines 41-47 | gray, blue, green, amber, red | same as R1 |
| R3 | `templates/admin/_discussions_threads.html:76` | `bg-{{ meta.color }}-100 text-{{ meta.color }}-700`; `meta = status_meta.get(t.status, {'label': t.status, 'color': 'gray'})` (line 49), with `status_meta` from the including page (R1/R2) | gray, blue, green, amber, red | `bg-{c}-100`, `text-{c}-700` |
| R4 | `templates/admin/jobs.html:19` | `text-{{ color }}-600` from the loop tuple at lines 11-17 | yellow, blue, green, red, gray | `text-{c}-600` |
| R5 | `src/services/bands.py:38-44` `band_class(band, strong, muted)`, called at `templates/admin/_assessments_body.html:467` (600, 400) and `templates/admin/_assessment_detail_body.html:624,631` (700, 600) | `f"text-gray-{muted}"` / `f"text-{b.css_class}-{strong}"`, with `BANDS` css_class `green`, `amber`, `gray` | (600,400), (700,600) | `text-green-600`, `text-amber-600`, `text-gray-400`, `text-green-700`, `text-amber-700`, `text-gray-600` |

`safelist` is the union of R1-R5, 27 names:
`hover:border-{gray,blue,green,amber,red}-300`, `ring-{gray,blue,green,amber,red}-400`,
`text-{gray,blue,green,amber,red,yellow}-600`, `bg-{gray,blue,green,amber,red}-100`,
`text-{gray,blue,green,amber,red}-700`, `text-gray-400`.

#### Class names chosen at runtime but written out in full (content scan finds them; no safelist)

| site | construct |
|---|---|
| `templates/admin/activity.html:64-65`, `templates/manager/activity.html:64-65` | `{% set sc = {...} %}` / `sc.get(run.status, '')` |
| `templates/admin/activity_detail.html:13-14`, `templates/manager/activity_detail.html:13-14` | `sc` dict |
| `templates/admin/jobs.html:68,75`, `templates/admin/user_detail.html:209-211`, `templates/manager/pi_detail.html:403-405` | `sc` dict |
| `templates/admin/users.html:83,89,95,101`, `templates/manager/pis.html:116,122,128,134` | `status_colors` / `agent_colors` dicts |
| `templates/manager/prompt_suggestions.html:83,102`, `templates/manager/prompt_suggestion_detail.html:39,44` | `status_colors` dict |
| `templates/admin/_assessments_body.html:344,350`, `templates/admin/_assessment_detail_body.html:618` | `rec_chip` dict |
| `templates/admin/_discussions_threads.html:92,106,152,178`, `templates/admin/_assessment_detail_body.html:535,747,1312` | `panel_color` / `signal_chip` dicts |
| `templates/admin/_assessment_detail_body.html:277,287,315` | `glyph` tuple dict |
| `templates/admin/agent_detail.html:139,152,159` | `'text-…' if … else '…'` literals |
| `static/js/assessment_chat.js:52-53` | `QUESTION_CLASS`, `ANSWER_CLASS` constants |
| `static/js/assessment_chat.js:316,355,398,400,1002-1031` | literal `el(...)` classes and `classList` literals |
| `templates/admin/_assessment_detail_body.html:1441-1443`, `templates/agent/conversations.html:68,84,90` | `classList.toggle/remove` literals |
| `templates/onboarding/profile_review.html:267,271`, `templates/agent/public_profile.html:278,282`, `templates/profile/edit.html:229,233` | `className = 'tag-pill' / 'tag-remove'` (custom CSS) |

Runtime-built names that are **not** Tailwind utilities, so they need nothing. Each is styled by
the page's inline `<style>` or is only a selector hook:
- `signal-{…}` and `signal-source-{{ item.source }}` (`templates/admin/_assessment_detail_body.html:279`);
- `gating-{{ … }}` (`_assessment_detail_body.html:774`, `_assessments_body.html:582`);
- `sc-*` (`src/services/svg_charts.py`);
- `citation-link` (`src/services/prose_citations.py:308`).

#### Inline `on…=` handlers (27 in 15 files; the spec's "26 in 14" misses H17)

| id | file:line | handler | becomes (task) |
|---|---|---|---|
| H1 | `templates/manager/pis.html:58` | `onchange="applyFilter()"` on `#status-filter` | `data-filter-param="status_filter"` inside `data-filter-nav="/manager/pis"` (1A-6) |
| H2 | `templates/manager/pis.html:69` | `onchange="applyFilter()"` on `#claimed-filter` | `data-filter-param="claimed_filter"` (1A-6) |
| H3 | `templates/manager/pis.html:100` | `onclick="location.href='/manager/pis/{{ item.user.id }}'"` on `<tr>` | `data-row-href="/manager/pis/{{ item.user.id }}"` (1A-6) |
| H4 | `templates/manager/pis.html:112` | `onclick="event.stopPropagation()"` on the ORCID `<a>` | deleted; `ui.js` ignores a click inside an `a` (1A-6) |
| H5 | `templates/manager/discussions.html:23` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H6 | `templates/manager/activity.html:58` | `onclick="location.href='/manager/activity/{{ run.id }}'"` | `data-row-href` (1A-6) |
| H7 | `templates/manager/prompt_suggestions.html:10` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H8 | `templates/manager/assessments.html:103` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H9 | `templates/manager/assessments.html:113` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H10 | `templates/manager/assessments.html:121` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H11 | `templates/admin/cohorts.html:12` | `onclick="document.getElementById('new-cohort-form').classList.toggle('hidden')"` | `data-toggle-target="new-cohort-form"` (1A-6) |
| H12 | `templates/admin/cohorts.html:108` | `onsubmit="return confirm(…)"` | Phase 0 (§5.1, `data-confirm`) |
| H13 | `templates/admin/user_detail.html:178` | `onsubmit="return confirm(…)"` | Phase 0 (§5.1) |
| H14 | `templates/admin/discussions.html:23` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H15 | `templates/admin/simulation.html:409` | `onchange="this.form.submit()"` on `select[name=run]` | `data-autosubmit` (1A-6) |
| H16 | `templates/admin/activity.html:58` | `onclick="location.href='/admin/activity/{{ run.id }}'"` | `data-row-href` (1A-6) |
| H17 | `templates/admin/_discussions_threads.html:74` | `{% if has_detail %}onclick="document.getElementById('detail-{{ loop.index }}').classList.toggle('hidden')"{% endif %}` | `data-toggle-target="detail-{{ loop.index }}"` (1A-6) |
| H18 | `templates/admin/jobs.html:33` | `onchange="applyFilter()"` on `#status-filter` | `data-filter-param="status_filter"` in `data-filter-nav="/admin/jobs"` (1A-6) |
| H19 | `templates/admin/jobs.html:43` | `onchange="applyFilter()"` on `#type-filter` | `data-filter-param="type_filter"` (1A-6) |
| H20 | `templates/admin/users.html:26` | `onchange="applyFilter()"` on `#status-filter` | `data-filter-param="status_filter"` in `data-filter-nav="/admin/users"` (1A-6) |
| H21 | `templates/admin/users.html:37` | `onchange="applyFilter()"` on `#claimed-filter` | `data-filter-param="claimed_filter"` (1A-6) |
| H22 | `templates/admin/users.html:67` | `onclick="location.href='/admin/users/{{ item.user.id }}'"` | `data-row-href` (1A-6) |
| H23 | `templates/admin/users.html:79` | `onclick="event.stopPropagation()"` on the ORCID `<a>` | deleted (1A-6) |
| H24 | `templates/admin/cohort_detail.html:28` | `onsubmit="return confirm(…)"` | Phase 0 (§5.1) |
| H25 | `templates/admin/assessments.html:102` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H26 | `templates/admin/assessments.html:116` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |
| H27 | `templates/admin/assessments.html:127` | `onchange="this.form.submit()"` | `data-autosubmit` (1A-6) |

The `ui.js` behaviours derived from this table are:
- `data-row-href`, from H3, H6, H16 and H22. It also covers H4 and H23.
- `data-autosubmit`, from H5, H7-H10, H14, H15 and H25-H27.
- `data-filter-nav` / `data-filter-param`, from H1, H2 and H18-H21. Each replaces one page's
  `applyFilter()`.
- `data-toggle-target`, from H11 and H17.
- `data-confirm`, from H12, H13 and H24. This one is Phase 0's `confirm.js`.

No JS file or `src/` string builds an inline handler. Neither does any template `<script>`:
`grep -rnE "\.on[a-z]+\s*=|setAttribute\('on|javascript:" templates static/js` is empty.

#### Inline `<script>` blocks (23)

| id | file:line | content | disposition (task) |
|---|---|---|---|
| S1 | `templates/base.html:15-18` | PostHog loader + `posthog.init` | deleted (1A-3) |
| S2 | `templates/base.html:20` | `posthog.identify` | deleted (1A-3) |
| S3 | `templates/base.html:190-213` | `[data-utc]` local-time rendering | kept, nonce (1A-7) |
| S4 | `templates/cabo_graph.html:273` | `graph-data` JSON | deleted with the template (1A-2) |
| S5 | `templates/cabo_graph.html:274` | `color-map` JSON | deleted (1A-2) |
| S6 | `templates/cabo_graph.html:275` | graph page script | deleted (1A-2) |
| S7 | `templates/manager/pis.html:183-192` | `applyFilter()` | deleted; `data-filter-nav` (1A-6) |
| S8 | `templates/admin/users.html:147-156` | `applyFilter()` | deleted (1A-6) |
| S9 | `templates/admin/jobs.html:115-124` | `applyFilter()` | deleted (1A-6) |
| S10 | `templates/manager/assessment_detail.html:69-90` | print: open every `<details>` | kept, nonce (1A-7) |
| S11 | `templates/manager/assessments.html:68-89` | print: open every `<details>` | kept, nonce (1A-7) |
| S12 | `templates/onboarding/profile_review.html:47-49` | `setTimeout(… location.reload() …)` | kept, nonce (1A-7) |
| S13 | `templates/onboarding/profile_review.html:239-307` | tag-field editor | kept, nonce (1A-7) |
| S14 | `templates/admin/_assessment_chat_drawer.html:84-90` | `window.ASSESSMENT_CHAT` URLs | kept, nonce (1A-7) |
| S15 | `templates/admin/simulation.html:733-754` | panel refresh (Phase 0 rewrites it) | kept, nonce (1A-7) |
| S16 | `templates/admin/assessment_detail.html:69-90` | print: open every `<details>` | kept, nonce (1A-7) |
| S17 | `templates/admin/_assessment_detail_body.html:1412-1451` | details toggles, unclamp (Phase 0 adds the toggle listener) | kept, nonce (1A-7) |
| S18 | `templates/admin/cohort_topology.html:124-136` | column toggle | kept, nonce (1A-7) |
| S19 | `templates/admin/assessments.html:68-89` | print: open every `<details>` | kept, nonce (1A-7) |
| S20 | `templates/agent/conversations.html:50-98` | on-demand thread replies | kept, nonce (1A-7) |
| S21 | `templates/agent/public_profile.html:251-318` | tag-field editor | kept, nonce (1A-7) |
| S22 | `templates/agent/dashboard.html:198-201` | comment only | kept, nonce (1A-7) |
| S23 | `templates/profile/edit.html:195-288` | tag-field editor | kept, nonce (1A-7) |

After this part, 15 inline blocks remain (S3, S10-S23), each opened by a bare `<script>` today.
Any block that Phase 0 adds is found by 1A-7's source test.

#### References removed by 1A-2/1A-3/1A-4 (grep over `src tests scripts templates static nginx pyproject.toml docs/operations CLAUDE.md`)

The graph pages, the Sankey script and plotly (1A-2):
- `src/routers/public.py:1, 27-414, 490-858`;
- `templates/cabo_graph.html`;
- `static/js/markdown.js:18-19, 48-50` (the `graph` profile);
- `tests/integration/test_public_graph.py`;
- `tests/characterization/test_public_routes.py:34-45`;
- `tests/unit/test_reachability.py:130-146`;
- `tests/unit/test_markdown_renderer_config.py:1-3, 18, 29-59`;
- `tests/e2e/seed.py:26, 53-66, 207-310`;
- `tests/e2e/test_browser_flows.py:160-178, 361-380`;
- `tests/e2e/README.md:132`;
- `nginx/nginx.conf:24-26, 30, 109-126`;
- `scripts/build_cabo_sankey.py`;
- `pyproject.toml:27`.

`docs/operations/` and `CLAUDE.md` have no hits.

PostHog (1A-3):
- `src/main.py:207-222, 270-272`;
- `src/config.py:294-295`;
- `templates/base.html:14-22`;
- `templates/admin/_assessment_chat_drawer.html:13-14, 38, 45` (`ph-no-capture`);
- `tests/integration/test_posthog_snippet.py`;
- `tests/unit/test_posthog_middleware.py`;
- `tests/unit/test_config_secret_redaction.py:25, 34`;
- `tests/unit/test_no_dead_src_symbols.py:78-81, 431`;
- `tests/unit/test_reachability.py:116-119`. `/api/health` was src-referenced only through
  `PostHogContextMiddleware`, so it needs a `ROUTE_ALLOWLIST` entry.
- `tests/integration/test_assessment_chat_templates.py:112`;
- `nginx/nginx.conf:152-164` (`/ingest`).

Connect Slack (1A-4):
- `src/routers/agent_page.py:3, 13-14, 40-41, 194, 213-223, 244-254, 267-268, 271, 277-282, 680-780, 946-968`;
- `src/routers/invite.py:219-260`;
- `templates/agent/dashboard.html:59-75, 161-177`;
- `tests/integration/test_agent_page.py:40, 119-124, 527-561, 592-626, 690-691, 722`;
- `tests/integration/test_double_submits.py:43-76`;
- `tests/integration/test_atomic_rmw.py:1-3, 20, 117-230`;
- `specs/web-delegates.md:151`.

Consequences forced by `tests/unit/test_no_dead_src_symbols.py`. Deleting the routes leaves
`src/services/slack_web.py`'s `lookup_user_by_email`, `get_user_info` and
`lookup_user_by_email_async` with no caller, so they go too, with their tests in
`tests/unit/test_slack_web.py`. Then `users.lookupByEmail` is no longer called. As a result,
`tests/unit/test_slack_provisioning.py::test_method_scope_table_matches_the_methods_src_calls`
and `test_manifest_requests_no_scope_nothing_needs` force the removal of `users:read.email` from
`BOT_SCOPES` (`src/services/slack_provisioning.py:43`). Installed apps keep their grant until
reinstalled (the comment at `slack_provisioning.py:25-29`). The doc comments that name
`lookupByEmail` are updated: `src/models/user.py:33-37`, `src/services/user_email.py:3-4, 10-11`,
`src/services/slack_tokens.py:39`.

### File map

| file | action | responsibility |
|---|---|---|
| `src/routers/public.py` | modify (shrinks to about 90 lines) | root redirect and access-pending only |
| `templates/cabo_graph.html` | delete | graph page |
| `scripts/build_cabo_sankey.py` | delete | Sankey export |
| `tests/integration/test_public_graph.py` | delete | graph tests |
| `tests/unit/test_phase1_removals.py` | create | pins that the graph pages, Sankey, plotly, PostHog and Connect Slack stay gone |
| `static/js/markdown.js` | modify | drop the `graph` profile |
| `pyproject.toml` | modify | drop `plotly` |
| `nginx/nginx.conf` | modify | drop the graph rate-limit zone/location and the PostHog `/ingest` proxy |
| `tests/characterization/test_public_routes.py`, `tests/unit/test_reachability.py`, `tests/unit/test_markdown_renderer_config.py`, `tests/e2e/seed.py`, `tests/e2e/test_browser_flows.py`, `tests/e2e/README.md` | modify | graph parts removed; `/api/health` allowlisted |
| `src/main.py` | modify | PostHog middleware gone; `SecurityHeadersMiddleware` outermost; exact-path guard exemption; CSP router included |
| `src/config.py` | modify | `posthog_api_key` gone |
| `templates/base.html` | modify | PostHog gone; `app.css` link; `ui.js` loaded; nonce on S3 |
| `templates/admin/_assessment_chat_drawer.html` | modify | `ph-no-capture` gone; nonce on S14 |
| `tests/integration/test_posthog_snippet.py`, `tests/unit/test_posthog_middleware.py` | delete | PostHog tests |
| `tests/unit/test_config_secret_redaction.py`, `tests/unit/test_no_dead_src_symbols.py`, `tests/integration/test_assessment_chat_templates.py` | modify | PostHog references gone |
| `src/routers/agent_page.py` | modify | Connect Slack route, banner state, Slack-only delegate lookup and Slack cleanup on remove gone |
| `src/routers/invite.py` | modify | Slack lookup on accept gone |
| `src/services/slack_web.py` | modify | `lookup_user_by_email`, `get_user_info` and `lookup_user_by_email_async` gone |
| `src/services/slack_provisioning.py` | modify | `users:read.email` scope gone |
| `src/models/user.py`, `src/services/user_email.py`, `src/services/slack_tokens.py` | modify (comments) | stop naming `users.lookupByEmail` as a consumer |
| `templates/agent/dashboard.html` | modify | banner and Slack-only list gone; nonce on S22 |
| `tests/integration/test_agent_page.py`, `tests/integration/test_double_submits.py`, `tests/integration/test_atomic_rmw.py`, `tests/unit/test_slack_web.py`, `tests/unit/test_slack_provisioning.py` | modify | Connect Slack tests removed or replaced |
| `specs/web-delegates.md` | modify | note that Slack linkage was removed |
| `static/vendor/marked-12.0.2.min.js`, `static/vendor/purify-<v>.min.js`, `static/vendor/MANIFEST.md` | create | vendored scripts and their provenance |
| `templates/_head_assets.html` | modify | load from `/static/vendor/` |
| `tests/unit/test_vendored_assets.py` | create | hash and manifest checks |
| `tests/integration/test_prompt_suggestions_page.py`, `tests/integration/test_assessment_list_chrome.py`, `tests/integration/test_assessment_detail_page.py`, `tests/unit/test_templating_factory.py` | modify | expect the vendored paths |
| `tests/e2e/ui_audit/journeys_phase1.py` | create or append | `journey_chat_sanitizer_vendored`, `journey_ui_behaviours`, `journey_csp_report_only`, `journey_css_parity` |
| `static/js/ui.js` | create | the data-attribute behaviours |
| 13 templates of H-table | modify | handlers replaced by data attributes; S7-S9 deleted |
| `tests/unit/test_ui_behaviours.py` | create | `ui.js` source pins; no inline handlers; attribute wiring |
| `tests/integration/test_discussions_panel_cards.py` | modify (comment) | "onclick" → "row toggle" |
| `src/web/security_headers.py` | create | nonce, policies, headers middleware |
| 14 templates of S-table (S3, S10-S23) | modify | `nonce="{{ request.state.csp_nonce }}"` |
| `tests/unit/test_security_headers.py` | create | policy constants, header items, nonce shape |
| `tests/unit/test_csp_templates.py` | create | every inline script carries the nonce; no off-origin script |
| `tests/integration/test_security_headers.py` | create | headers on pages, 403s, 404s, static; nonce matches |
| `tests/integration/test_origin_guard.py` | modify | middleware order; exact-path exemption |
| `src/routers/csp_report.py` | create | `POST /api/csp-report` |
| `tests/integration/test_csp_report.py` | create | the report sink |
| `tailwind.config.js`, `static/css/input.css`, `static/css/app.css`, `scripts/build_css.sh` | create | compiled stylesheet and its pinned build |
| `scripts/ci.sh` | modify | the CSS drift step |
| `.gitignore`, `.dockerignore` | modify | `.tools/` |
| `tests/unit/test_compiled_css.py`, `tests/unit/test_ci_script.py` | create / modify | build pins, safelist derivation, drift step |
| `AGENT.md`, `specs/tech-stack.md`, `docs/operations/testing.md`, `CLAUDE.md` | modify | Tailwind is compiled, and the drift step |

---

### Task 1A-1: Inventory of runtime-built classes, inline handlers and inline scripts

The inventory itself is the tables above. This task re-verifies them on the branch, after
Phase 0 merges and before any other 1A task. It produces no repository change.

**Files:** none (read-only).
**Interfaces:** Consumes: Phase 0 merged into `webui/phase-1`. Produces: the confirmed R-, H- and
S-tables used by Tasks 1A-6, 1A-7 and 1A-9.

- [ ] **Step 1: List inline handlers and inline scripts**

```bash
python3 - <<'EOF'
import re, pathlib
H = re.compile(r'''(?<![\w-])on[a-z]+\s*=\s*["']''')
S = re.compile(r"<script\b(?![^>]*\bsrc=)[^>]*>")
for p in sorted(pathlib.Path("templates").rglob("*.html")):
    t = p.read_text(encoding="utf-8")
    for rx, tag in ((H, "handler"), (S, "script")):
        for m in rx.finditer(t):
            print(tag, f"{p}:{t.count(chr(10), 0, m.start()) + 1}")
EOF
```

Expected after Phase 0: 24 `handler` lines, the H-table less H12, H13 and H24, which Phase 0
replaced with `data-confirm`. Line numbers may differ only in the files Phase 0 edited. The
`script` lines should be S1-S23, plus any inline block Phase 0 added. Record each added
block. 1A-7 gives it a nonce, because the bulk edit there covers every bare `<script>`.

- [ ] **Step 2: List runtime-built utility classes**

```bash
python3 - <<'EOF'
import re, pathlib
T = re.compile(r"(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border|ring|from|via|to|fill|stroke|divide|outline|decoration|shadow|accent|caret|placeholder)-\{\{\s*([\w.]+)\s*\}\}")
P = re.compile(r"""(?<![\w-])(?:bg|text|border|ring)-(?:[a-z]+-)?(?:\{|["'`]\s*\+|\$\{)""")
for p in sorted(pathlib.Path("templates").rglob("*.html")):
    for m in T.finditer(p.read_text(encoding="utf-8")):
        print("template", p, m.group(1))
for p in sorted([*pathlib.Path("src").rglob("*.py"), *pathlib.Path("static/js").rglob("*.js")]):
    if "__pycache__" in p.parts:
        continue
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if P.search(line):
            print("code", f"{p}:{i}")
EOF
```

Expected, verified on `e8f475f`:
- template hits: `admin/_discussions_threads.html meta.color` (×2), `admin/discussions.html meta.color` (×3),
  `manager/discussions.html meta.color` (×3), and `admin/jobs.html color`;
- code hits: `src/services/bands.py:43` and `src/services/bands.py:44`.

Any other hit is a new R-row. Add its classes to the `safelist` in 1A-9 and its site to
`CONSTRUCTED_SITES` / `CONSTRUCTED_CODE_LINES` there.

- [ ] **Step 3: Stop if the tables do not match**

If Steps 1-2 differ from the tables beyond the Phase 0 changes named in Step 1, stop and
report the difference to the parent. Later tasks quote exact before-text from these
tables. No commit.

---

### Task 1A-2: Remove the graph pages, the Sankey script and plotly (A-13, M-05, D9, D10)

**Files:**
- Modify: `src/routers/public.py` (whole file), `static/js/markdown.js:18-19, 48-50`,
  `pyproject.toml:27`, `nginx/nginx.conf:24-26, 30, 109-126`,
  `tests/characterization/test_public_routes.py:34-46`, `tests/unit/test_reachability.py:130-146`,
  `tests/unit/test_markdown_renderer_config.py:1-3, 17-59`, `tests/e2e/seed.py:26, 33, 53-66, 117-124, 207-310`,
  `tests/e2e/test_browser_flows.py:160-178, 361-380`, `tests/e2e/README.md:132`
- Delete: `templates/cabo_graph.html`, `scripts/build_cabo_sankey.py`, `tests/integration/test_public_graph.py`
- Create: `tests/unit/test_phase1_removals.py`

**Interfaces:** Consumes: nothing. Produces: `tests/unit/test_phase1_removals.py` with the helper
`_routes() -> set[tuple[str, str]]` and the constant `ROOT`, which 1A-3 and 1A-4 append to.

- [ ] **Step 1: Write the failing test** `tests/unit/test_phase1_removals.py`:

```python
"""Surfaces removed in Phase 1 of the web UI remediation (spec §6.4) stay removed."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

GRAPH_PATHS = (
    "/cabo-graph",
    "/scripps-graph",
    "/schultz-alumni-pilot",
    "/schultz-group-alumni",
)


def _routes() -> set[tuple[str, str]]:
    from src.main import create_app

    return {
        (method, route.path)
        for route in create_app().routes
        for method in (getattr(route, "methods", None) or ())
    }


def test_the_graph_pages_are_gone():
    """D9: the four unauthenticated graph pages, their template and their nginx
    rate-limit block (A-13, M-05)."""
    paths = {path for _, path in _routes()}
    assert not set(GRAPH_PATHS) & paths
    assert not (ROOT / "templates" / "cabo_graph.html").exists()
    nginx = (ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    assert "cabo-graph" not in nginx
    assert "req_graph" not in nginx


def test_the_markdown_factory_has_no_graph_profile():
    js = (ROOT / "static" / "js" / "markdown.js").read_text(encoding="utf-8")
    assert '"graph"' not in js


def test_the_sankey_script_and_plotly_are_gone():
    """D10."""
    assert not (ROOT / "scripts" / "build_cabo_sankey.py").exists()
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    assert not [d for d in deps if d.lower().startswith("plotly")]
```

- [ ] **Step 2: Run it and see it fail**

`.venv-test/bin/python -m pytest tests/unit/test_phase1_removals.py -v`. Expected: 3 failures.
The routes are registered, `cabo_graph.html` exists, `"graph"` is in `markdown.js`, and
`plotly>=5.20.0` is a dependency.

- [ ] **Step 3: Replace `src/routers/public.py` with**

```python
"""Public-facing routes: the root redirect and the access-pending page."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import User
from src.services.validators import is_valid_email
from src.web.templating import make_templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()


@router.get("/")
async def root(request: Request):
    """Site root: signed-in users go to their profile, everyone else to login.
    This instance has no marketing landing page."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/profile", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/access-pending", response_class=HTMLResponse)
async def access_pending(request: Request):
    """Shown after ORCID login when the user is not yet approved."""
    pending_info = request.session.get("pending_access") or {}
    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
        },
    )


@router.post("/access-pending/email", response_class=HTMLResponse)
async def access_pending_email(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Record an address a pending-access user typed (users.contact_email_unverified; never users.email)."""
    pending_info = request.session.get("pending_access") or {}
    user_id = pending_info.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=302)

    email_clean = (email or "").strip().lower()
    if not is_valid_email(email_clean):
        return templates.TemplateResponse(
            request,
            "access_pending.html",
            {
                "request": request,
                "pending_info": pending_info,
                "email_error": "Please enter a valid email address.",
            },
            status_code=400,
        )

    import uuid as _uuid

    result = await db.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user and not user.email and user.access_status == "pending":
        # Only while the request is still pending: a stale session after an
        # approval or denial must not rewrite what admins see.
        # Unverified by construction (anyone holding this browser session can
        # type any address), so it lands in its own column for admins to see
        # and is never copied to users.email.
        user.contact_email_unverified = email_clean
        await db.commit()
        pending_info["email"] = email_clean
        request.session["pending_access"] = pending_info

    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
            "email_saved": True,
        },
    )
```

The three handlers are line-for-line the current lines 416-488. If Part 1B has already edited
`access_pending_email` in this file, keep 1B's body for that function and delete only lines
1-414 and 490-858 of the original.

- [ ] **Step 4: Delete the template, the script and the graph test module**

```bash
git rm templates/cabo_graph.html scripts/build_cabo_sankey.py tests/integration/test_public_graph.py
```

- [ ] **Step 5: Edit `static/js/markdown.js`**

Delete these three lines. They are unchanged by Phase 0:

```js
    } else if (profile === "graph") {
      ext.gfm = true;
      ext.breaks = true;
```

In the factory's comment (lines 18-21 before Phase 0), replace

```js
  // One factory for every sanitizing renderer (LC-02): the detail pages ("page"),
  // the assessment chat ("chat") and the collaboration graph ("graph"). Each call
```

with

```js
  // One factory for every sanitizing renderer (LC-02): the detail pages ("page")
  // and the assessment chat ("chat"). Each call
```

If Phase 0 rewrote that comment, the rule is that the comment names exactly the profiles the
function accepts, `"page"` and `"chat"`. Also change the factory's `throw` path only if
Phase 0 restructured it. It must still throw `unknown markdown profile: ` for any other
name.

- [ ] **Step 6: Edit `pyproject.toml`**

Delete the line `    "plotly>=5.20.0",` (line 27).

- [ ] **Step 7: Edit `nginx/nginx.conf`**

Delete lines 24-26:

```
#   - req_graph:   tighter cap for the four public collaboration-graph routes,
#                  which are unauthenticated and DB-heavy (seq-scans + O(V^2)
#                  clustering); paired with the app-side payload cache.
```

Delete line 30 `limit_req_zone  $binary_remote_addr zone=req_graph:10m   rate=2r/s;`. Delete lines
109-126: the block from `    # Tighter limit for the unauthenticated, DB-heavy collaboration-graph`
through the closing `    }` of `location ~ ^/(cabo-graph|…)$`, and the blank line after it.

- [ ] **Step 8: Edit the tests that named the graph**

- `tests/characterization/test_public_routes.py`: delete lines 34-46, from
  `# --- public collaboration graphs (DB-only, no network) ----------------------` through the end
  of `test_graph_routes_render_200_with_csp`, and the blank line after it.
- `tests/unit/test_reachability.py`: delete the four `ROUTE_ALLOWLIST` entries at lines 130-146,
  `("GET", "/cabo-graph")` through the `("GET", "/schultz-group-alumni")` entry's closing `),`.
- `tests/unit/test_markdown_renderer_config.py`, which Phase 0 may also have edited:
  - In the module docstring, replace `All three markdown renderers` with `Both markdown renderers`.
  - Delete the line `CABO = (ROOT / "templates" / "cabo_graph.html").read_text()`.
  - Delete `test_graph_profile_keeps_gfm_and_breaks`.
  - Replace `test_factory_defines_three_profiles`, `test_chat_and_graph_use_the_factory` and
    `test_chat_profile_keeps_its_raw_html_hardening` with:

```python
def test_factory_defines_the_page_and_chat_profiles_only():
    assert "window.createSanitizingMarked" in JS
    for profile in ('"page"', '"chat"'):
        assert profile in JS
    assert '"graph"' not in JS


def test_chat_uses_the_factory():
    assert 'createSanitizingMarked("chat")' in CHAT_JS
    assert "new window.marked.Marked(" not in CHAT_JS


def test_chat_profile_keeps_its_raw_html_hardening():
    factory = JS[JS.index("function createSanitizingMarked"):JS.index("window.createSanitizingMarked =")]
    start = factory.index('profile === "chat"')
    chat = factory[start:factory.index("} else", start)]
    # Phase 0 Task 0-4 shares the raw-block-free tag tokenizer with the page profile.
    assert "tokenizer.tag = rawTagTokenizer" in chat
    assert "html: function (html)" in chat
    tokenizer = JS[JS.index("function rawTagTokenizer"):JS.index("function createSanitizingMarked")]
    assert "inRawBlock: false" in tokenizer
```

- `tests/e2e/test_browser_flows.py`: delete the `"public_graph": {…},` entry of `FLOWS`
  (lines 160-178). Delete `test_public_graph_renders_with_real_data` (lines 361-380) with its
  `@requires_server` decorator and the two blank lines after it.
- `tests/e2e/README.md`: delete line 132, `| public graph | yes | unauthenticated GET |`.
- `tests/e2e/seed.py`. The agents in the graph fixture stay, because the cohort-topology flow
  ticks the `su` and `wiseman` cells (`test_browser_flows.py:406`). The run, channel, messages
  and decisions go, since only the graph read them.
  - Replace the docstring row
    `5 Scripps agents + edges     ``/scripps-graph`` and ``/cabo-graph`` render` with
    `5 Scripps agents             the cohort/topology flow's ``su`` and ``wiseman`` cells`.
  - Delete `from datetime import UTC, datetime` (line 33).
  - Replace lines 53-66 (`# Graph fixture. …` through `DECIDED_AT = …`) with:

```python
# Agents the cohort/topology flow ticks (FLOWS['admin_cohort_and_topology']).
COHORT_AGENTS = ["su", "wiseman", "grotjahn", "ward", "briney"]
```

  - In `seed()`, the local import becomes:

```python
    from src.models import (
        USER_ROLE_ADMIN,
        AgentRegistry,
        Job,
        ResearcherProfile,
    )
```

  - Replace lines 207-310 (`    # --- graph fixture ---…` through `    out["graph_edges"] = str(len(GRAPH_EDGES))`) with:

```python
    # --- cohort/topology agents -------------------------------------------
    for i, agent_id in enumerate(COHORT_AGENTS):
        user, _ = await _get_or_create_user(
            session,
            f"0000-0002-0000-91{i:02d}",
            name=f"PI {agent_id.title()}",
            email=f"e2e-{agent_id}@example.org",
            institution="Scripps Research",
            access_status="allowed",
            onboarding_complete=True,
        )
        agent = (
            await session.execute(
                select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        if agent is None:
            session.add(
                AgentRegistry(
                    agent_id=agent_id,
                    user_id=user.id,
                    bot_name=f"{agent_id.title()}Bot",
                    pi_name=f"PI {agent_id.title()}",
                    status="active",
                )
            )
    await session.commit()
    out["cohort_agents"] = str(len(COHORT_AGENTS))
```

- [ ] **Step 9: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_phase1_removals.py tests/unit/test_markdown_renderer_config.py tests/characterization/test_public_routes.py tests/unit/test_reachability.py tests/unit/test_no_dead_src_symbols.py tests/e2e -v
.venv-test/bin/python -m ruff check tests/unit/test_phase1_removals.py tests/unit/test_markdown_renderer_config.py tests/e2e src/routers/public.py
```

Expected: all pass; ruff reports nothing.

- [ ] **Step 10: Commit**

```bash
git add src/routers/public.py static/js/markdown.js pyproject.toml nginx/nginx.conf \
  tests/unit/test_phase1_removals.py tests/characterization/test_public_routes.py \
  tests/unit/test_reachability.py tests/unit/test_markdown_renderer_config.py \
  tests/e2e/seed.py tests/e2e/test_browser_flows.py tests/e2e/README.md
git commit -m "feat(webui-1A): remove the four graph pages, the Sankey script and plotly (A-13, M-05, D9, D10)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(The three `git rm` paths of Step 4 are already staged.)

---

### Task 1A-3: Remove PostHog (A-12, D13)

**Files:**
- Modify: `src/main.py:207-222, 270-272`, `src/config.py:294-295`, `templates/base.html:14-22`,
  `templates/admin/_assessment_chat_drawer.html:13-14, 38, 45`, `nginx/nginx.conf:152-164`,
  `tests/unit/test_config_secret_redaction.py:25, 34`, `tests/unit/test_no_dead_src_symbols.py:78-81, 431`,
  `tests/unit/test_reachability.py:116-119`, `tests/integration/test_assessment_chat_templates.py:112`,
  `tests/unit/test_phase1_removals.py` (append)
- Delete: `tests/integration/test_posthog_snippet.py`, `tests/unit/test_posthog_middleware.py`

**Interfaces:** Consumes: `ROOT` and `_routes` in `tests/unit/test_phase1_removals.py` (1A-2).
Produces: none.

- [ ] **Step 1: Append the failing test to `tests/unit/test_phase1_removals.py`**

```python
def test_posthog_is_gone():
    """D13 / A-12: no snippet, no middleware, no setting, no /ingest proxy."""
    import src.main as main_mod
    from src.config import Settings

    assert not hasattr(main_mod, "PostHogContextMiddleware")
    assert not hasattr(main_mod, "AgentBadgeMiddleware")
    assert "posthog_api_key" not in Settings.model_fields
    names = [m.cls.__name__ for m in main_mod.create_app().user_middleware]
    assert "PostHogContextMiddleware" not in names
    for rel in (
        "templates/base.html",
        "templates/admin/_assessment_chat_drawer.html",
        "nginx/nginx.conf",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        assert "posthog" not in text, rel
        assert "ph-no-capture" not in text, rel
```

- [ ] **Step 2: Run it and see it fail**

`.venv-test/bin/python -m pytest tests/unit/test_phase1_removals.py::test_posthog_is_gone -v`.
Expected: FAIL at `assert not hasattr(main_mod, "PostHogContextMiddleware")`.

- [ ] **Step 3: Edit `src/main.py`**

Delete the whole `class PostHogContextMiddleware(BaseHTTPMiddleware):` block (lines 207-222)
and the two blank lines after it. In `create_app()`, delete:

```python
    # PostHog context (added first, so it runs innermost, inside the session
    # middleware).
    application.add_middleware(PostHogContextMiddleware)

```

- [ ] **Step 4: Edit `src/config.py`**

Delete:

```python
    # Analytics
    posthog_api_key: str = ""

```

`Settings` uses `extra="ignore"` (`src/config.py:103`), so a `.env` that still sets
`POSTHOG_API_KEY` keeps loading.

- [ ] **Step 5: Edit `templates/base.html`**

Delete lines 14-22: from `    {% if request.state.posthog_api_key %}` through its closing
`    {% endif %}`, the second `{% endif %}`. This removes inline scripts S1 and S2.

- [ ] **Step 6: Edit `templates/admin/_assessment_chat_drawer.html`**

Delete the two comment lines:

```
   * `ph-no-capture` keeps the drawer out of PostHog recordings if PostHog is ever
     configured (POSTHOG_API_KEY is empty in production today).
```

In both class lists (line 38, the `<button … data-chat-bubble` class, and line 45, the `<aside
id="assessment-chat"` class), delete the leading `ph-no-capture ` token. The class lists then
start `print:hidden fixed …`.

- [ ] **Step 7: Edit `nginx/nginx.conf`**

Delete lines 152-164: the blank line, then `    # PostHog reverse proxy — routes through
copi.science to bypass ad blockers`, then the two `location /ingest…` blocks, up to the
`    }` before the server's closing `}`.

- [ ] **Step 8: Edit the tests**

```bash
git rm tests/integration/test_posthog_snippet.py tests/unit/test_posthog_middleware.py
```

- `tests/unit/test_config_secret_redaction.py`: delete line 25, `        posthog_api_key="phc-LEAKME",`,
  and line 34, `        assert "phc-LEAKME" not in rendered`.
- `tests/unit/test_no_dead_src_symbols.py`: delete the `ALLOWLIST` entry
  `"src.main:PostHogContextMiddleware.dispatch": (…),` (lines 78-81). Delete the `ENTRY_POINTS`
  line `        "src.main:PostHogContextMiddleware.dispatch",` (line 431).
- `tests/unit/test_reachability.py`: replace lines 116-119

```python
    # /api/health is no longer allowlisted here (issue #25 P1, middleware
    # short-circuit): PostHogContextMiddleware.dispatch compares request.url.path
    # against the literal string "/api/health", which makes src_referenced_paths()
    # pick it up as src/-referenced — genuinely so, not a false positive.
```

with

```python
    ("GET", "/api/health"): (
        "Container healthcheck: docker-compose.prod.yml's web service GETs "
        "http://127.0.0.1:8000/api/health. Nothing in the app links it. Its only src/ "
        "reference was PostHogContextMiddleware's skip list, removed with PostHog (D13)."
    ),
```

- `tests/integration/test_assessment_chat_templates.py:112`: replace
  `    assert "ph-no-capture" in opening.group() and "print:hidden" in opening.group()` with
  `    assert "print:hidden" in opening.group()`.

- [ ] **Step 9: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_phase1_removals.py tests/unit/test_config_secret_redaction.py tests/unit/test_no_dead_src_symbols.py tests/unit/test_reachability.py tests/integration/test_assessment_chat_templates.py tests/integration/test_origin_guard.py tests/integration/test_health_route.py -v
```

Expected: all pass. `test_route_allowlist_has_no_stale_entries` stays green, because
`/api/health` is now referenced by nothing in `src/`.

- [ ] **Step 10: Commit**

```bash
git add src/main.py src/config.py templates/base.html templates/admin/_assessment_chat_drawer.html \
  nginx/nginx.conf tests/unit/test_phase1_removals.py tests/unit/test_config_secret_redaction.py \
  tests/unit/test_no_dead_src_symbols.py tests/unit/test_reachability.py \
  tests/integration/test_assessment_chat_templates.py
git commit -m "feat(webui-1A): remove PostHog: snippet, middleware, setting, /ingest proxy (A-12, D13)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A-4: Remove Connect Slack (A-08, D-03, D-04, D-05, D16)

**Files:**
- Modify: `src/routers/agent_page.py:3, 13-14, 40-41, 193-195, 213-224, 244-255, 266-272, 277-282, 680-780, 946-969`,
  `src/routers/invite.py:219-260`, `src/services/slack_web.py:18-23, 47-55, 57-63, 115-135, 165-175`,
  `src/services/slack_provisioning.py:43`, `src/models/user.py:33-37`, `src/services/user_email.py:3-4, 10-11`,
  `src/services/slack_tokens.py:39`, `templates/agent/dashboard.html:59-75, 161-177`,
  `tests/integration/test_agent_page.py:40, 119-124, 527-561, 592-626, 690-691, 722`,
  `tests/integration/test_double_submits.py:43-76`, `tests/integration/test_atomic_rmw.py:1-3, 20, 117-230`,
  `tests/unit/test_slack_web.py`, `tests/unit/test_slack_provisioning.py:69, 145-148`,
  `specs/web-delegates.md:151`, `tests/unit/test_phase1_removals.py` (append)

**Interfaces:** Consumes: `ROOT` and `_routes` (1A-2). Produces: none. `AgentRegistry.delegate_slack_ids`
stays and is no longer read by `src/`.

- [ ] **Step 1: Append the failing tests to `tests/unit/test_phase1_removals.py`**

```python
def test_connect_slack_is_gone():
    """D16 (A-08, D-03, D-04, D-05): no route, no banner, no Slack lookup by email."""
    from src.services import slack_web

    paths = {path for _, path in _routes()}
    assert "/agent/{agent_id}/delegates/connect-slack" not in paths
    for name in ("lookup_user_by_email", "lookup_user_by_email_async", "get_user_info"):
        assert not hasattr(slack_web, name), name
    for rel in (
        "src/routers/agent_page.py",
        "src/routers/invite.py",
        "templates/agent/dashboard.html",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for needle in ("lookup_user_by_email", "connect-slack", "delegate_has_slack"):
            assert needle not in text, (rel, needle)


def test_the_delegate_slack_ids_column_is_kept():
    """D16 keeps the column and its data, unread."""
    from src.models import AgentRegistry

    assert "delegate_slack_ids" in AgentRegistry.__table__.columns
```

- [ ] **Step 2: Run them and see them fail**

`.venv-test/bin/python -m pytest tests/unit/test_phase1_removals.py -k "connect_slack or delegate_slack_ids" -v`.
Expected: `test_connect_slack_is_gone` fails on the registered route. `test_the_delegate_slack_ids_column_is_kept` passes.

- [ ] **Step 3: Edit `src/routers/agent_page.py`**

These line numbers are from `e8f475f`. Apply the edits bottom-up so they stay valid.
1. In `remove_delegate`, delete lines 947-969. That runs from `        # Remove Slack ID if present`
   through `                logger.warning("Delegate Slack sync is best-effort; skipped: %s", exc)`
   and the blank line after it. `if delegate:` is then followed directly by `await db.delete(delegate)`.
2. Delete lines 680-780: `def _resolve_delegate_names(…)`, the `# Delegate Slack connection`
   section header, and `delegate_connect_slack` through its final `    )`, with the two blank
   lines after it. The file then goes from the `public-profile` save handler's
   `    )` straight to the `# Delegate management — invitation-based` header.
3. Delete lines 277-282: `def _user_slack_id_in_list(…)` and its body, with the two blank
   lines after it.
4. In `agent_dashboard`:
   - delete line 194, `    slack_error = request.query_params.get("slack_error")`, and the blank line after it;
   - delete lines 213-224, from `    # Resolve delegate display names (legacy Slack-only delegates)` through the blank
     line after `        )`;
   - delete lines 244-255, from `    # Check if current delegate user has Slack linked` through the blank line after
     `        )`;
   - in the `_template_context(…)` call, delete the three keyword lines
     `            slack_error=slack_error,`, `            delegates=delegates,` and
     `            delegate_has_slack=delegate_has_slack,`.
5. Imports. Delete `import asyncio` (line 3), `from sqlalchemy import text as sa_text` and
   `from sqlalchemy import update as sa_update` (lines 13-14),
   `from src.services.slack_tokens import get_any_bot_token` (line 40), and
   `from src.services.slack_web import get_user_info, lookup_user_by_email_async` (line 41).
   Each name was used only by the removed code. `AgentRegistry`, `selectinload` and
   `impersonation_note` are still used elsewhere in the module.

- [ ] **Step 4: Edit `src/routers/invite.py`**

Delete lines 219-260. That runs from `    # Slack sync runs after the commit: the delegation must not wait on (or roll`
through the second `    await db.commit()` (line 259) and the blank line after it. The function
then reads:

```python
    await db.commit()

    logger.info(
        "Delegate %s accepted invitation for agent %s",
        user.id, agent.agent_id,
    )

    return RedirectResponse(url=f"/agent/{agent.agent_id}/dashboard", status_code=302)
```

- [ ] **Step 5: Edit `templates/agent/dashboard.html`**

Delete lines 161-177, from `            {% if delegates %}` (the "Slack-Only Delegates" list) through its
`            {% endif %}` and the blank line after it. Delete lines 59-75, from
`    {% if not is_owner and not delegate_has_slack %}` (the "Connect Slack Account" banner)
through its `    {% endif %}` and the blank line after it.

- [ ] **Step 6: Edit `src/services/slack_web.py`**

- Delete `lookup_user_by_email` (lines 115-123) and `get_user_info` (lines 126-134), each with
  the two blank lines after it.
- Delete `lookup_user_by_email_async` (lines 172-174) and the two blank lines after it.
- `__all__` becomes `__all__ = ["revoke_token", "revoke_token_async"]`.
- Replace lines 47-55 with:

```python
# Errors that mean "this call will never work", so retrying is pointless: a revoked
# token does not become valid on attempt four.
_TERMINAL = frozenset({
    "invalid_auth", "account_inactive", "token_revoked", "no_permission",
    "channel_not_found", "not_in_channel",
})
```

- Replace module-docstring lines 18-23 with:

```
The core is synchronous, because ``slack_sdk.WebClient`` is. **Async callers must
use the ``_async`` wrappers at the bottom of this module, not the sync functions.**
The one call site (the account-deletion teardown) runs on the event loop, and a
synchronous ``time.sleep`` inside it stalls the whole event loop, not just that
request — see ``_call``.
```

- Replace comment lines 164-168 (from `# asyncio.to_thread moves the whole thing …` through
  `# that whole helper through asyncio.to_thread.`) with:

```python
# asyncio.to_thread moves the whole thing to a worker thread, so the wait costs
# that request its latency and nothing else.
```

- [ ] **Step 7: Edit `src/services/slack_provisioning.py`**

Delete line 43, `    "users:read.email",   # users.lookupByEmail`. Nothing calls
`users.lookupByEmail` any more. Existing installs keep the grant until they are reinstalled,
per the comment at lines 25-29.

- [ ] **Step 8: Edit the comments that named `users.lookupByEmail` as a consumer**

- `src/models/user.py:36-37`. Replace
  `    #: never copied to `email`, which delegate-invitation acceptance and` /
  `    #: `users.lookupByEmail` trust.` with
  `    #: never copied to `email`, which delegate-invitation acceptance trusts.`.
- `src/services/user_email.py:3-4`. Replace
  `` ``users.email`` is a plain, case-sensitive UNIQUE column that delegate-invitation `` /
  `` acceptance binds to and ``users.lookupByEmail`` reads. A login whose ORCID `` with
  `` ``users.email`` is a plain, case-sensitive UNIQUE column that delegate-invitation `` /
  `acceptance binds to. A login whose ORCID`.
- `src/services/user_email.py:10-11`. Replace
  `` lowercase; the ORCID login and the CLI store ORCID's value as-is), so no stored `` /
  `` address and no ``lookupByEmail`` input changes. It refuses an address another `` with
  `` lowercase; the ORCID login and the CLI store ORCID's value as-is), so no stored `` /
  `address changes. It refuses an address another`.
- `src/services/slack_tokens.py:39`. Replace
  `    """Any valid bot token, for workspace-wide lookups (e.g. users.lookupByEmail).` with
  `    """Any valid bot token, for workspace-wide calls (e.g. the team lookup at provisioning).`.

If Part 1B has rewritten one of these sentences, apply the same deletion to 1B's text. Drop only
the `lookupByEmail` clause.

- [ ] **Step 9: Edit `tests/unit/test_slack_web.py`**

Delete `test_lookup_user_by_email_retries_a_rate_limit`,
`test_lookup_user_by_email_returns_none_when_not_found` and
`test_get_user_info_returns_none_without_retrying` (lines 24-56). Replace lines 59-134, from the
`# The async wrappers.` comment block to the end of the file, with:

```python
# ---------------------------------------------------------------------------
# The async wrapper. Its one call site is the account-deletion teardown, which
# runs on the event loop, and _call sleeps synchronously between retries, so
# calling the sync function from an `async def` would stall every request the
# process is serving.
# ---------------------------------------------------------------------------


async def test_the_async_wrapper_runs_the_blocking_call_off_the_event_loop(monkeypatch):
    """The sync body must execute on a worker thread, not the loop's thread."""
    import threading

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _record(**kw):
        seen["thread"] = threading.get_ident()
        return _resp({"revoked": True})

    client = MagicMock()
    client.auth_revoke.side_effect = _record
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert await slack_web.revoke_token_async("xoxb-test") is True
    assert seen["thread"] != loop_thread, (
        "the blocking Slack call ran on the event loop's own thread — one 429 "
        "would freeze every other request in the process"
    )


async def test_every_sync_entry_point_has_an_async_wrapper():
    """A future call site must not have to choose the blocking variant by accident."""
    for name in ("revoke_token",):
        assert hasattr(slack_web, f"{name}_async"), f"missing {name}_async"
        assert f"{name}_async" in slack_web.__all__


def test_the_email_lookups_are_gone():
    """Connect Slack (their only callers) was removed (D16)."""
    for name in ("lookup_user_by_email", "lookup_user_by_email_async", "get_user_info"):
        assert not hasattr(slack_web, name), name


def test_an_outsized_retry_after_is_capped(monkeypatch):
    """Slack can ask for a minute. Three of those would hold a request for minutes.

    The cap bounds request latency; it is only safe to sleep at all because the
    async callers reach this through the _async wrappers.
    """
    slept: list[float] = []
    monkeypatch.setattr(slack_web.time, "sleep", lambda d: slept.append(d))

    err = SlackApiError("ratelimited", _resp({"error": "ratelimited"}))
    err.response.headers = {"Retry-After": "600"}
    client = MagicMock()
    client.auth_revoke.side_effect = [err, _resp({"revoked": True})]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.revoke_token("xoxb-test") is True
    assert slept == [slack_web._MAX_RETRY_AFTER], (
        f"slept {slept} instead of capping at {slack_web._MAX_RETRY_AFTER}s"
    )


def test_a_modest_retry_after_is_honoured_exactly(monkeypatch):
    """Under the cap, obey Slack — guessing is how a throttled bot gets blocked."""
    slept: list[float] = []
    monkeypatch.setattr(slack_web.time, "sleep", lambda d: slept.append(d))

    err = SlackApiError("ratelimited", _resp({"error": "ratelimited"}))
    err.response.headers = {"Retry-After": "7"}
    client = MagicMock()
    client.auth_revoke.side_effect = [err, _resp({"revoked": True})]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    slack_web.revoke_token("xoxb-test")
    assert slept == [7.0]
```

- [ ] **Step 10: Edit `tests/unit/test_slack_provisioning.py`**

Delete the `METHOD_SCOPES` row `    "users.lookupByEmail": "users:read.email",` (line 69). In
`test_method_scope_table_is_not_trivially_satisfiable` (lines 145-148), replace
`    assert len({s for s in METHOD_SCOPES.values() if s}) >= 8` with:

```python
    # 7 since users:read.email left with Connect Slack (D16).
    assert len({s for s in METHOD_SCOPES.values() if s}) >= 7
```

- [ ] **Step 11: Edit `tests/integration/test_agent_page.py`**

- Delete `test_a_delegate_can_link_their_slack_account` (lines 527-543) and
  `test_accepting_an_invitation_syncs_the_delegates_slack_id` (lines 546-561).
- Replace `test_env_bot_tokens_never_reach_the_delegate_lookup` (lines 592-626) with the three tests below.
- Delete the `ENDPOINTS` entry `    Ep("POST", "/agent/{agent_id}/delegates/connect-slack",` /
  `       "/agent/{agent}/delegates/connect-slack"),` (lines 690-691).
- In `test_the_endpoint_table_matches_the_registered_routes` (line 722), change
  `assert len(ENDPOINTS) == 12` to `assert len(ENDPOINTS) == 11`.
- Delete line 40, `from src.services.slack_tokens import is_valid_token`. Its only use was the
  replaced test. `get_settings` stays, since `_auth` uses it.
- In the `_no_env_bot_tokens` docstring, replace lines 119-124 (from
  `not the workspace-wide one, so a dev host …` through
  ``` ``test_env_bot_tokens_never_reach_the_delegate_lookup``. ```) with these two lines:

```
    not the workspace-wide one. Stubbed to ``{}``, the only tokens a route can find
    are the DB rows and a test's own ``world.agent.slack_bot_token``.
```

```python
async def test_the_dashboard_makes_no_slack_call_for_slack_only_delegates(
    client, db_session, world, slack
):
    """D-05: agents.delegate_slack_ids stays in the database, unread (D16), so a
    dashboard view costs no Slack lookup and lists no Slack-only delegate."""
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    world.agent.delegate_slack_ids = ["U1"]
    await db_session.flush()

    page = await client.get(f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(world.pi.id))
    assert page.status_code == 200
    assert slack.calls == []
    assert "Slack-Only Delegates" not in page.text
    assert "(U1)" not in page.text


async def test_a_delegate_sees_no_connect_slack_banner(client, world, delegated):
    """D-03/D-04: the banner whose errors were never shown and which never cleared."""
    page = await client.get(
        f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(delegated.user.id)
    )
    assert page.status_code == 200
    # Control: this really is the delegate's view of the dashboard.
    assert "as a delegate of" in page.text
    assert "Connect Slack Account" not in page.text
    assert "/delegates/connect-slack" not in page.text

    gone = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/connect-slack", headers=_auth(delegated.user.id)
    )
    assert gone.status_code == 404


async def test_removing_a_delegate_makes_no_slack_call(
    client, db_session, world, delegated, slack
):
    """The Slack-id cleanup on remove is gone; the column keeps its data."""
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    world.agent.delegate_slack_ids = ["U-LEGACY"]
    await db_session.flush()

    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/{delegated.row.id}/remove",
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    assert slack.calls == []
    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.delegate_slack_ids == ["U-LEGACY"]
    remaining = (await db_session.execute(
        select(AgentDelegate).where(AgentDelegate.agent_registry_id == agent.id)
    )).scalars().all()
    assert remaining == []
```

- [ ] **Step 12: Edit `tests/integration/test_double_submits.py`**

Replace `test_double_accept_creates_one_delegate` (lines 43-76) with:

```python
async def test_double_accept_creates_one_delegate(engine):
    """Review Focus 3."""
    from src.routers import invite as invite_routes

    f, pi, d, agent, inv = await _committed(engine, bot_token="xoxb-test-accept")
    try:
        async def accept():
            async with f() as s:
                row = await s.get(DelegateInvitation, inv.id)
                user = await s.get(User, d.id)
                return await invite_routes._accept_invitation(row, user, s, request=None)

        results = await asyncio.gather(accept(), accept(), return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)]
        async with f() as s:
            n = (await s.execute(select(func.count()).select_from(AgentDelegate)
                                 .where(AgentDelegate.agent_registry_id == agent.id))).scalar_one()
        assert n == 1
    finally:
        await _cleanup(f, [pi.id, d.id], [agent.id])
```

- [ ] **Step 13: Edit `tests/integration/test_atomic_rmw.py`**

These tests raced SQL that no code runs any more.
- Delete lines 117-230: `_append_delegate`, `test_concurrent_delegate_appends_both_land_and_dedup` and
  `test_concurrent_delegate_removal_leaves_the_other_id`.
- Replace docstring lines 1-3 with
  `"""Concurrent read-modify-writes on ResearcherProfile.profile_version. Pre-fix,` /
  `both racers read the same prior value and one write is lost.`.
- Change line 20 to `from src.models import ResearcherProfile, User`.

- [ ] **Step 14: Edit `specs/web-delegates.md`**

Insert after line 151 (`## Slack Linkage`):

```
> **Removed 2026-10-01 (D16 of `docs/specs/2026-10-01-web-ui-remediation-design.md`).** Nothing
> reads or writes `delegate_slack_ids` any more; the column and its data are kept. The text
> below describes the removed behaviour.
```

- [ ] **Step 15: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_phase1_removals.py tests/unit/test_slack_web.py tests/unit/test_slack_provisioning.py tests/unit/test_no_dead_src_symbols.py tests/unit/test_reachability.py tests/integration/test_agent_page.py tests/integration/test_double_submits.py tests/integration/test_atomic_rmw.py -v
.venv-test/bin/python -m ruff check tests/unit tests/integration
```

Expected: all pass, and ruff reports nothing. `test_src_has_no_unlisted_dead_definitions` is
green because the three Slack functions are deleted, not orphaned.

- [ ] **Step 16: Commit**

```bash
git add src/routers/agent_page.py src/routers/invite.py src/services/slack_web.py \
  src/services/slack_provisioning.py src/models/user.py src/services/user_email.py \
  src/services/slack_tokens.py templates/agent/dashboard.html specs/web-delegates.md \
  tests/unit/test_phase1_removals.py tests/unit/test_slack_web.py tests/unit/test_slack_provisioning.py \
  tests/integration/test_agent_page.py tests/integration/test_double_submits.py tests/integration/test_atomic_rmw.py
git commit -m "feat(webui-1A): remove Connect Slack; keep delegate_slack_ids unread (A-08, D-03, D-04, D-05, D16)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A-5: Vendor marked 12.0.2 and DOMPurify 3.4.x (M-04)

**Files:**
- Create: `static/vendor/marked-12.0.2.min.js`, `static/vendor/purify-<v>.min.js`, `static/vendor/MANIFEST.md`,
  `tests/unit/test_vendored_assets.py`, `tests/e2e/ui_audit/journeys_phase1.py` (or append to it)
- Modify: `templates/_head_assets.html` (whole file), `tests/integration/test_prompt_suggestions_page.py:11-14, 125-126`,
  `tests/integration/test_assessment_list_chrome.py:49-61`, `tests/integration/test_assessment_detail_page.py:311-312`,
  `tests/unit/test_templating_factory.py:35`

**Interfaces:** Consumes: Phase 0's `createSanitizingMarked("chat")`, `window.copiRenderMarkdown`, and the harness
contract (`h.base_url`, `h.ids`, `await h.page(role)`). Produces: `static/vendor/MANIFEST.md`, whose row format is
`| `file` | package | version | tarball URL | `sha512-…` | `sha384-…` |`. Also `_seed_path(template: str, ids: dict) -> str`
and `journey_chat_sanitizer_vendored(h) -> dict` in `journeys_phase1.py`. `_seed_path` is reused by 1A-8 and 1A-9.

- [ ] **Step 1: Write the failing test** `tests/unit/test_vendored_assets.py`:

```python
"""Vendored front-end scripts (spec §6.2, M-04): the files are the ones the manifest
names, byte for byte, and the pages load them from /static/vendor/."""

import base64
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "static" / "vendor"
MANIFEST = VENDOR / "MANIFEST.md"

_ROW = re.compile(
    r"^\| `(?P<file>[^`]+)` \| (?P<package>[a-z]+) \| (?P<version>\d+\.\d+\.\d+) \| "
    r"(?P<url>https://\S+) \| `(?P<integrity>sha512-[A-Za-z0-9+/=]+)` \| "
    r"`(?P<sha384>sha384-[A-Za-z0-9+/=]+)` \|$",
    re.M,
)

#: The SRI the old jsDelivr tag for marked 12.0.2 carried (templates/_head_assets.html
#: before this change). The vendored file must be that same file.
MARKED_CDN_SRI = "sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi"


def _rows() -> dict[str, dict[str, str]]:
    return {
        m["package"]: m.groupdict()
        for m in _ROW.finditer(MANIFEST.read_text(encoding="utf-8"))
    }


def test_manifest_lists_exactly_the_vendored_files():
    listed = {r["file"] for r in _rows().values()}
    on_disk = {p.name for p in VENDOR.iterdir() if p.name != "MANIFEST.md"}
    assert listed == on_disk
    assert set(_rows()) == {"marked", "dompurify"}


def test_every_vendored_file_matches_its_recorded_sha384():
    for row in _rows().values():
        digest = hashlib.sha384((VENDOR / row["file"]).read_bytes()).digest()
        assert row["sha384"] == "sha384-" + base64.b64encode(digest).decode(), row["file"]


def test_versions_are_the_pinned_ones():
    rows = _rows()
    assert rows["marked"]["version"] == "12.0.2"
    assert rows["marked"]["file"] == "marked-12.0.2.min.js"
    assert rows["dompurify"]["version"].startswith("3.4.")
    assert rows["dompurify"]["file"] == f"purify-{rows['dompurify']['version']}.min.js"


def test_marked_is_the_file_the_cdn_pin_named():
    assert _rows()["marked"]["sha384"] == MARKED_CDN_SRI


def test_head_assets_load_the_vendored_files_before_the_renderer():
    head = (ROOT / "templates" / "_head_assets.html").read_text(encoding="utf-8")
    rows = _rows()
    marked = f'<script src="/static/vendor/{rows["marked"]["file"]}"></script>'
    purify = f'<script src="/static/vendor/{rows["dompurify"]["file"]}"></script>'
    renderer = '<script src="/static/js/markdown.js"></script>'
    assert marked in head and purify in head and renderer in head
    assert head.index(marked) < head.index(renderer)
    assert head.index(purify) < head.index(renderer)
    assert "cdn.jsdelivr.net" not in head
```

- [ ] **Step 2: Run it and see it fail**

`.venv-test/bin/python -m pytest tests/unit/test_vendored_assets.py -v`. Expected: every test errors
with `FileNotFoundError` on `static/vendor/MANIFEST.md`.

- [ ] **Step 3: Fetch, verify and record the files**

Run from the repo root (needs network to `registry.npmjs.org`):

```bash
python3 - <<'EOF'
import base64, hashlib, io, json, tarfile, urllib.request
from pathlib import Path

VENDOR = Path("static/vendor")
VENDOR.mkdir(parents=True, exist_ok=True)


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def newest(pkg, prefix):
    versions = json.loads(get(f"https://registry.npmjs.org/{pkg}"))["versions"]
    picks = [v for v in versions if v.startswith(prefix) and "-" not in v]
    return max(picks, key=lambda v: tuple(int(x) for x in v.split(".")))


purify_version = newest("dompurify", "3.4.")
rows = []
for pkg, version, member, name in (
    ("marked", "12.0.2", "package/marked.min.js", "marked-12.0.2.min.js"),
    ("dompurify", purify_version, "package/dist/purify.min.js", f"purify-{purify_version}.min.js"),
):
    dist = json.loads(get(f"https://registry.npmjs.org/{pkg}/{version}"))["dist"]
    blob = get(dist["tarball"])
    got = "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
    assert got == dist["integrity"], (pkg, got, dist["integrity"])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        data = tf.extractfile(member).read()
    (VENDOR / name).write_bytes(data)
    sha384 = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    rows.append(f"| `{name}` | {pkg} | {version} | {dist['tarball']} | `{dist['integrity']}` | `{sha384}` |")

(VENDOR / "MANIFEST.md").write_text(
    "# Vendored front-end scripts\n\n"
    "Served from `/static/vendor/` by `templates/_head_assets.html` (spec §6.2, M-04). Each file\n"
    "is extracted unchanged from its npm tarball, after the tarball was checked against the\n"
    "registry's `dist.integrity`. `sha384` is the hash of the file itself, and\n"
    "`tests/unit/test_vendored_assets.py` recomputes every one.\n\n"
    "marked stays on 12.x. Later majors replace the positional renderer API (`html(html, block)`,\n"
    "`image(href, title, text)`, `link(href, title, text)`) that `static/js/markdown.js` uses.\n"
    "To update DOMPurify, re-run the fetch in Task 1A-5 of\n"
    "`docs/plans/2026-10-01-web-ui-remediation-phase-1.md`. Commit the new file, this table and\n"
    "the `_head_assets.html` change together.\n\n"
    "| file | package | version | source | tarball integrity | sha384 |\n"
    "|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n",
    encoding="utf-8",
)
print("\n".join(rows))
EOF
```

Expected output: two table rows, with a DOMPurify version of `3.4.16` or newer. There are three
stop conditions. Report each to the parent rather than working around it:
- an `AssertionError`, which means the tarball does not match its registry integrity;
- a `KeyError` on the tarball member, which means the package layout differs;
- a marked `sha384` other than `MARKED_CDN_SRI`.

- [ ] **Step 4: Replace `templates/_head_assets.html` with**

```html
<script src="/static/vendor/marked-12.0.2.min.js"></script>
<script src="/static/vendor/purify-PURIFY_VERSION.min.js"></script>
<script src="/static/js/markdown.js"></script>
```

Replace `PURIFY_VERSION` with the version Step 3 printed, for example
`purify-3.4.16.min.js`. Same-origin files need no `integrity` or `crossorigin`. The
manifest test holds the hashes.

- [ ] **Step 5: Update the tests that asserted the jsDelivr tags**

- `tests/integration/test_prompt_suggestions_page.py`: add `import re` after `import hashlib`
  (line 11). Replace lines 125-126 with:

```python
    assert '<script src="/static/vendor/marked-12.0.2.min.js"></script>' in body
    assert re.search(r'<script src="/static/vendor/purify-3\.4\.\d+\.min\.js"></script>', body)
```

- `tests/integration/test_assessment_list_chrome.py`: replace the body of
  `test_both_list_pages_carry_the_sanitizing_markdown_scripts`'s loop (lines 49-61) with:

```python
    for html in await _both_surfaces(client, db_session):
        assert 'src="/static/vendor/marked-12.0.2.min.js"' in html
        assert re.search(r'src="/static/vendor/purify-3\.4\.\d+\.min\.js"', html)
        assert "cdn.jsdelivr.net" not in html
        assert 'src="/static/js/markdown.js"' in html
```

- `tests/integration/test_assessment_detail_page.py:311-312`: replace
  `        assert "marked@12.0.2/marked.min.js" in html` / `        assert "dompurify@3.1.6/dist/purify.min.js" in html` with:

```python
        assert "/static/vendor/marked-12.0.2.min.js" in html
        assert re.search(r"/static/vendor/purify-3\.4\.\d+\.min\.js", html)
```

- `tests/unit/test_templating_factory.py:35`: replace
  `    marked = 'src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"'` with
  `    marked = 'src="/static/vendor/marked-12.0.2.min.js"'`.

- [ ] **Step 6: Add the harness journey**

If `tests/e2e/ui_audit/journeys_phase1.py` does not exist yet, create it with this header. If
another Phase 1 part created it, keep its header and `JOURNEYS` and add only the code after the
header:

```python
"""Phase 1 browser journeys (spec §9).

Run ``python -m tests.e2e.ui_audit.run journeys --phase 1`` against the instance that
``python -m tests.e2e.ui_audit.run serve`` starts. Each journey takes the harness
object and returns ``{"ok": bool, ...evidence}``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

JOURNEYS: list = []
```

Code to add:

```python
def _seed_path(template: str, ids: dict) -> str:
    """A path template with the seed's ids filled in: {pi}, {run}, {a0}."""
    return template.format(pi=ids["pi"], run=ids["run"], a0=ids["assessments"][0])


# --- §6.2: the chat and page sanitizers on the vendored copies -----------------

_SANITIZER_CHAT_JS = REPO / "static" / "js" / "assessment_chat.js"
_SANITIZER_PURIFY_RE = re.compile(r"const PURIFY = (\{.*?\n  \});", re.S)
_SANITIZER_OLD_PURIFY = "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"

SANITIZER_PROBES = [
    "<img src=x onerror=window.__xss=1>",
    "<script>window.__xss=1</script>",
    "<code>x</code><img src=x onerror=window.__xss=1>",
    "[js](javascript:window.__xss=1)",
    "[http](http://example.org/)",
    "![alt](https://example.org/a.png)",
    '<svg><a href="javascript:window.__xss=1">x</a></svg>',
    '<a href="https://example.org/" title="t" data-x="1" aria-label="l">a</a>',
    "~30-37% and ~x~",
]
#: Positive controls: a sanitizer that stripped everything would pass the probes.
SANITIZER_CONTROLS = [
    ["[ok](https://example.org/x)", 'a[href="https://example.org/x"]'],
    ["**bold**", "strong"],
]

_SANITIZER_INSPECT_JS = """
  const inspect = (html) => {
    const t = document.createElement("template");
    t.innerHTML = html;
    const all = [...t.content.querySelectorAll("*")];
    return {
      html: html.slice(0, 300),
      bad_tags: all.filter((e) => /^(img|svg|script|iframe|form|style|object|embed|math|video|audio)$/i.test(e.tagName)).length,
      bad_attrs: all.flatMap((e) => [...e.attributes].map((a) => a.name)).filter((n) => n !== "href").length,
      bad_hrefs: all.filter((e) => e.hasAttribute("href") && !/^https:/i.test(e.getAttribute("href"))).length,
      del: t.content.querySelectorAll("del").length,
    };
  };
  const count = (html, sel) => {
    const t = document.createElement("template");
    t.innerHTML = html;
    return t.content.querySelectorAll(sel).length;
  };
"""

_SANITIZER_LIVE_BODY = """
  const md = window.createSanitizingMarked("chat");
  const render = (p) => window.DOMPurify.sanitize(md.parse(p), PURIFY);
  return {
    purify_version: window.DOMPurify.version,
    probes: probes.map((p) => Object.assign({probe: p}, inspect(render(p)))),
    controls: controls.map(([p, sel]) => ({probe: p, selector: sel, found: count(render(p), sel)})),
    page: inspect(window.copiRenderMarkdown("<img src=x onerror=window.__xss=1> [js](javascript:window.__xss=1)")),
    xss: window.__xss || 0,
  };
}
"""

_SANITIZER_COMPARE_BODY = """
  const md = window.createSanitizingMarked("chat");
  const all = probes.concat(controls.map((c) => c[0]));
  const chat = all.map((p) => {
    const html = md.parse(p);
    return {probe: p, old: window.OldPurify.sanitize(html, PURIFY), now: window.DOMPurify.sanitize(html, PURIFY)};
  });
  const vendored = window.DOMPurify;
  const page = all.map((p) => {
    window.DOMPurify = window.OldPurify;
    const old = window.copiRenderMarkdown(p);
    window.DOMPurify = vendored;
    return {probe: p, old: old, now: window.copiRenderMarkdown(p)};
  });
  return {chat: chat.filter((r) => r.old !== r.now), page: page.filter((r) => r.old !== r.now)};
}
"""


def _sanitizer_fn(purify_literal: str, body: str) -> str:
    return (
        "([probes, controls]) => {\n"
        f"  const PURIFY = {purify_literal};\n"
        + _SANITIZER_INSPECT_JS
        + body
    )


def _sanitizer_clean(r: dict) -> bool:
    return r["bad_tags"] == 0 and r["bad_attrs"] == 0 and r["bad_hrefs"] == 0 and r["del"] == 0


async def journey_chat_sanitizer_vendored(h) -> dict:
    """§6.2: the chat's PURIFY profile and the page renderer, on the vendored marked and
    DOMPurify. Part 1 runs on a real assessment page (the files actually served).
    Part 2 sanitizes the same markup with DOMPurify 3.1.6 (the CDN copy this replaces)
    and with the vendored copy, and reports every output that differs."""
    match = _SANITIZER_PURIFY_RE.search(_SANITIZER_CHAT_JS.read_text(encoding="utf-8"))
    if match is None:
        return {"ok": False, "error": "no `const PURIFY = {...};` block in assessment_chat.js"}
    purify_literal = match.group(1)
    vendor = REPO / "static" / "vendor"
    purify_file = sorted(vendor.glob("purify-*.min.js"))[-1]
    args = [SANITIZER_PROBES, SANITIZER_CONTROLS]

    context, page, _log = await h.page("admin")
    try:
        await page.goto(
            h.base_url + _seed_path("/admin/assessments/{a0}", h.ids), wait_until="networkidle"
        )
        srcs = await page.evaluate(
            "() => [...document.scripts].map((s) => s.getAttribute('src')).filter(Boolean)"
        )
        live = await page.evaluate(_sanitizer_fn(purify_literal, _SANITIZER_LIVE_BODY), args)

        await page.goto("about:blank")
        await page.add_script_tag(url=_SANITIZER_OLD_PURIFY)
        await page.evaluate("() => { window.OldPurify = window.DOMPurify; delete window.DOMPurify; }")
        await page.add_script_tag(path=str(vendor / "marked-12.0.2.min.js"))
        await page.add_script_tag(path=str(purify_file))
        await page.add_script_tag(path=str(REPO / "static" / "js" / "markdown.js"))
        changed = await page.evaluate(_sanitizer_fn(purify_literal, _SANITIZER_COMPARE_BODY), args)
    finally:
        await context.close()

    vendored = "/static/vendor/marked-12.0.2.min.js" in srcs and any(
        re.fullmatch(r"/static/vendor/purify-3\.4\.\d+\.min\.js", s) for s in srcs
    )
    ok = (
        vendored
        and not any("cdn." in s for s in srcs)
        and live["purify_version"].startswith("3.4.")
        and all(_sanitizer_clean(p) for p in live["probes"])
        and all(c["found"] == 1 for c in live["controls"])
        and _sanitizer_clean(live["page"])
        and live["xss"] == 0
        and not changed["chat"]
        and not changed["page"]
    )
    return {"ok": ok, "script_srcs": srcs, "live": live, "changed_vs_3_1_6": changed}


JOURNEYS += [journey_chat_sanitizer_vendored]
```

Not run in this task. The journey needs a browser, the harness instance and network to
`cdn.jsdelivr.net`. It runs in the pre-deploy harness pass (§9). A non-empty
`changed_vs_3_1_6` gets a review before merge: either show the change is harmless and record
that, or reopen the DOMPurify pick.

- [ ] **Step 7: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_vendored_assets.py tests/unit/test_templating_factory.py tests/integration/test_prompt_suggestions_page.py tests/integration/test_assessment_list_chrome.py tests/integration/test_assessment_detail_page.py -v
.venv-test/bin/python -m ruff check tests/unit/test_vendored_assets.py tests/e2e tests/integration/test_prompt_suggestions_page.py
```

Expected: all pass; ruff reports nothing.

- [ ] **Step 8: Commit**

```bash
git add static/vendor templates/_head_assets.html tests/unit/test_vendored_assets.py \
  tests/integration/test_prompt_suggestions_page.py tests/integration/test_assessment_list_chrome.py \
  tests/integration/test_assessment_detail_page.py tests/unit/test_templating_factory.py \
  tests/e2e/ui_audit/journeys_phase1.py
git commit -m "feat(webui-1A): vendor marked 12.0.2 and DOMPurify 3.4.x with a hashed manifest (M-04)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A-6: `static/js/ui.js` replaces every inline handler (M-03 groundwork)

**Run before any Part 1C template task.** It edits 13 of 1C's templates.

**Files:**
- Create: `static/js/ui.js`, `tests/unit/test_ui_behaviours.py`
- Modify: `templates/base.html` (head), `templates/manager/pis.html:54-58, 68-69, 99-100, 111-113, 182-192`,
  `templates/admin/users.html:22-26, 36-37, 66-67, 78-80, 146-156`, `templates/admin/jobs.html:28-33, 42-43, 114-124`,
  `templates/admin/activity.html:57-58`, `templates/manager/activity.html:57-58`, `templates/admin/cohorts.html:12`,
  `templates/admin/_discussions_threads.html:74`, `templates/manager/discussions.html:23`,
  `templates/manager/prompt_suggestions.html:10`, `templates/manager/assessments.html:103, 113, 121`,
  `templates/admin/discussions.html:23`, `templates/admin/simulation.html:409`,
  `templates/admin/assessments.html:102, 116, 127`, `tests/integration/test_discussions_panel_cards.py:253-254`,
  `tests/e2e/ui_audit/journeys_phase1.py` (append)

**Interfaces:** Consumes: Phase 0's `data-confirm` and `confirm.js`, both already in place.
Produces these `ui.js` behaviours, delegated on `document`:
- `data-row-href="/path"`: a click on the element navigates, unless the click target is inside
  `a, button, input, select, textarea, label, summary` within it.
- `data-autosubmit` on a form control: `change` calls `control.form.requestSubmit()`.
- `data-filter-nav="/path"` on a container, with `data-filter-param="<query name>"` on its
  controls: `change` on a param control navigates to `/path?<name>=<value>&…`, leaving out
  empty values.
- `data-toggle-target="<element id>"`: a click toggles `hidden` on that element, and sets
  `aria-expanded` on the clicked element when it carries one. Clicks on interactive
  descendants are excluded, as for `data-row-href`.

Phase 2 consumes these (B-04 extends `ui.js`; FN-04 removes `data-autosubmit`), and so does Part 1C.

- [ ] **Step 1: Write the failing test** `tests/unit/test_ui_behaviours.py`:

```python
"""Inline handlers moved to static/js/ui.js (spec §6.3). There is no JS runner in
this repo, so this pins the source: the listeners exist, the templates carry no
on…= attribute, and every data attribute is wired to something that exists."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
UI_JS = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")

#: An inline event-handler attribute (or a string assigned to an on… property).
INLINE_HANDLER = re.compile(r"""(?<![\w-])on[a-z]+\s*=\s*["']""")


def _templates():
    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")


def test_no_template_carries_an_inline_handler():
    offenders = [
        f"{name}:{text.count(chr(10), 0, m.start()) + 1}"
        for name, text in _templates()
        for m in INLINE_HANDLER.finditer(text)
    ]
    assert offenders == []


def test_base_loads_ui_js():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '<script src="/static/js/ui.js" defer></script>' in base


def test_ui_js_delegates_every_behaviour_on_document():
    assert 'document.addEventListener("click"' in UI_JS
    assert 'document.addEventListener("change"' in UI_JS
    assert 'const INTERACTIVE = "a, button, input, select, textarea, label, summary";' in UI_JS
    for needle in (
        '"[data-row-href]"',
        '"[data-toggle-target]"',
        '"[data-filter-nav]"',
        '"data-filter-param"',
        '"data-autosubmit"',
        "control.form.requestSubmit()",
        'classList.toggle("hidden")',
    ):
        assert needle in UI_JS, needle
    assert "innerHTML" not in UI_JS


def test_row_hrefs_are_local_paths():
    hrefs = [
        (name, m.group(1))
        for name, text in _templates()
        for m in re.finditer(r'data-row-href="([^"]*)"', text)
    ]
    assert {name for name, _ in hrefs} == {
        "admin/users.html",
        "manager/pis.html",
        "admin/activity.html",
        "manager/activity.html",
    }
    assert all(h.startswith("/") and not h.startswith("//") for _, h in hrefs)


def test_every_toggle_target_names_an_id_in_the_same_template():
    pairs = [
        (name, text, m.group(1))
        for name, text in _templates()
        for m in re.finditer(r'data-toggle-target="([^"]+)"', text)
    ]
    assert {name for name, _, _ in pairs} == {"admin/cohorts.html", "admin/_discussions_threads.html"}
    for name, text, target in pairs:
        assert f'id="{target}"' in text, (name, target)


def test_every_filter_nav_holds_its_params():
    expected = {
        "admin/users.html": ("/admin/users", {"status_filter", "claimed_filter"}),
        "manager/pis.html": ("/manager/pis", {"status_filter", "claimed_filter"}),
        "admin/jobs.html": ("/admin/jobs", {"status_filter", "type_filter"}),
    }
    found = {}
    for name, text in _templates():
        nav = re.search(r'data-filter-nav="([^"]+)"', text)
        if nav:
            found[name] = (nav.group(1), set(re.findall(r'data-filter-param="([^"]+)"', text)))
    assert found == expected


def test_autosubmit_replaces_every_form_submit_handler():
    counts = {name: text.count("data-autosubmit") for name, text in _templates() if "data-autosubmit" in text}
    assert counts == {
        "admin/assessments.html": 3,
        "manager/assessments.html": 3,
        "admin/discussions.html": 1,
        "manager/discussions.html": 1,
        "manager/prompt_suggestions.html": 1,
        "admin/simulation.html": 1,
    }
```

- [ ] **Step 2: Run it and see it fail**

`.venv-test/bin/python -m pytest tests/unit/test_ui_behaviours.py -v`. Expected: collection error,
`FileNotFoundError` on `static/js/ui.js`.

- [ ] **Step 3: Create `static/js/ui.js`**

```js
// Page behaviours that used to be inline on…= handlers (spec §6.3, M-03). A CSP
// without 'unsafe-inline' refuses every inline handler, so each one is a data
// attribute read here. Every listener is delegated on `document`, so markup swapped
// in later (the simulation panel's refresh) keeps working without re-binding.
//
//   data-row-href="/path"    a click on the element navigates there, unless it
//                            landed on (or inside) a control of its own
//   data-autosubmit          a change on this control submits its form through
//                            requestSubmit(), so submit listeners (confirm.js) run
//   data-filter-nav="/path"  a change on a [data-filter-param] control inside it
//                            navigates to /path?<param>=<value>…, empty values omitted
//   data-toggle-target="id"  a click toggles `hidden` on #id, and aria-expanded on
//                            the clicked element when it carries one
(function () {
  "use strict";

  // A click on one of these inside a [data-row-href] or [data-toggle-target]
  // belongs to that control (the ORCID link in a user row), not to the row.
  const INTERACTIVE = "a, button, input, select, textarea, label, summary";

  function ownControl(target, host) {
    const inner = target.closest(INTERACTIVE);
    return inner !== null && inner !== host && host.contains(inner);
  }

  function localPath(value) {
    return typeof value === "string" && value.charAt(0) === "/" && value.charAt(1) !== "/";
  }

  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const row = target.closest("[data-row-href]");
    if (row && !ownControl(target, row)) {
      const href = row.getAttribute("data-row-href");
      if (localPath(href)) window.location.assign(href);
      return;
    }

    const toggler = target.closest("[data-toggle-target]");
    if (toggler && !ownControl(target, toggler)) {
      const panel = document.getElementById(toggler.getAttribute("data-toggle-target"));
      if (!panel) return;
      const hidden = panel.classList.toggle("hidden");
      if (toggler.hasAttribute("aria-expanded")) {
        toggler.setAttribute("aria-expanded", hidden ? "false" : "true");
      }
    }
  });

  document.addEventListener("change", function (event) {
    const control = event.target;
    if (!(control instanceof Element)) return;

    if (control.hasAttribute("data-autosubmit") && control.form) {
      control.form.requestSubmit();
      return;
    }

    const nav = control.closest("[data-filter-nav]");
    if (nav && control.hasAttribute("data-filter-param")) {
      const base = nav.getAttribute("data-filter-nav");
      if (!localPath(base)) return;
      const params = new URLSearchParams();
      nav.querySelectorAll("[data-filter-param]").forEach(function (field) {
        if (field.value) params.set(field.getAttribute("data-filter-param"), field.value);
      });
      const query = params.toString();
      window.location.assign(query ? base + "?" + query : base);
    }
  });
})();
```

- [ ] **Step 4: Load it from `templates/base.html`**

Insert directly before `    {% block extra_head %}{% endblock %}` (line 13):

```html
    <script src="/static/js/ui.js" defer></script>
```

- [ ] **Step 5: Replace the handlers (H-table)**

For H5, H7-H10, H14, H15 and H25-H27, run in each of `templates/manager/discussions.html`,
`templates/manager/prompt_suggestions.html`, `templates/manager/assessments.html`,
`templates/admin/discussions.html`, `templates/admin/simulation.html` and
`templates/admin/assessments.html`:

```bash
sed -i 's/ onchange="this.form.submit()"/ data-autosubmit/' \
  templates/manager/discussions.html templates/manager/prompt_suggestions.html \
  templates/manager/assessments.html templates/admin/discussions.html \
  templates/admin/simulation.html templates/admin/assessments.html
```

For the rows (H3, H6, H16, H22), make these exact replacements:
- `templates/manager/pis.html`: `onclick="location.href='/manager/pis/{{ item.user.id }}'"` → `data-row-href="/manager/pis/{{ item.user.id }}"`
- `templates/admin/users.html`: `onclick="location.href='/admin/users/{{ item.user.id }}'"` → `data-row-href="/admin/users/{{ item.user.id }}"`
- `templates/admin/activity.html`: `onclick="location.href='/admin/activity/{{ run.id }}'"` → `data-row-href="/admin/activity/{{ run.id }}"`
- `templates/manager/activity.html`: `onclick="location.href='/manager/activity/{{ run.id }}'"` → `data-row-href="/manager/activity/{{ run.id }}"`

The reachability gate still credits these four routes. `_ANCHOR_ATTR_RE`
(`\b(?:href)\s*=`, `tests/unit/test_reachability.py:485`) matches the `href=` inside
`data-row-href=`.

For the ORCID links (H4, H23), in `templates/manager/pis.html` and `templates/admin/users.html`
delete the line `                       onclick="event.stopPropagation()"`.

For the filters (H1, H2, H18-H21):
- `templates/manager/pis.html`: replace `<!-- Filters -->\n<div class="bg-white rounded-lg border border-gray-200 p-4 mb-4 flex flex-wrap gap-4">`
  with `<!-- Filters -->\n<div data-filter-nav="/manager/pis" class="bg-white rounded-lg border border-gray-200 p-4 mb-4 flex flex-wrap gap-4">`.
  Replace `<select onchange="applyFilter()" id="status-filter"` with `<select data-filter-param="status_filter" id="status-filter"`
  and `<select onchange="applyFilter()" id="claimed-filter"` with `<select data-filter-param="claimed_filter" id="claimed-filter"`.
  Delete the `<script>` block at lines 183-192, from `<script>` through `</script>`.
- `templates/admin/users.html`: the same three replacements, with `data-filter-nav="/admin/users"`.
  Delete the `<script>` block at lines 147-156.
- `templates/admin/jobs.html`: replace `<!-- Filters -->\n<div class="bg-white border border-gray-200 rounded-lg p-4 mb-4 flex gap-4">`
  with `<!-- Filters -->\n<div data-filter-nav="/admin/jobs" class="bg-white border border-gray-200 rounded-lg p-4 mb-4 flex gap-4">`.
  Replace `<select onchange="applyFilter()" id="status-filter"` with `<select data-filter-param="status_filter" id="status-filter"`
  and `<select onchange="applyFilter()" id="type-filter"` with `<select data-filter-param="type_filter" id="type-filter"`.
  Delete the `<script>` block at lines 115-124.

For the toggles (H11, H17):
- `templates/admin/cohorts.html:12`: replace
  `<button type="button" onclick="document.getElementById('new-cohort-form').classList.toggle('hidden')"` with
  `<button type="button" data-toggle-target="new-cohort-form" aria-controls="new-cohort-form" aria-expanded="false"`.
  `#new-cohort-form` starts with class `hidden` (line 52).
- `templates/admin/_discussions_threads.html:74`: replace
  `{% if has_detail %}onclick="document.getElementById('detail-{{ loop.index }}').classList.toggle('hidden')"{% endif %}>` with
  `{% if has_detail %}data-toggle-target="detail-{{ loop.index }}"{% endif %}>`.
- `tests/integration/test_discussions_panel_cards.py:253-254`: replace
  `    # No decision and no panel means nothing to expand — and therefore no` /
  `    # onclick pointing at an element that does not exist.` with
  `    # No decision and no panel means nothing to expand — and therefore no` /
  `    # row toggle pointing at an element that does not exist.`.

- [ ] **Step 6: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_ui_behaviours.py tests/unit/test_reachability.py tests/integration/test_discussions_panel_cards.py tests/integration/test_manager_views.py tests/integration/test_admin_simulation_page.py tests/integration/test_cohort_admin.py -v
```

Expected: all pass. `test_no_template_carries_an_inline_handler` is green only once Phase 0's
three `data-confirm` edits are present. If it lists `admin/cohorts.html`,
`admin/user_detail.html` or `admin/cohort_detail.html` with `onsubmit`, Phase 0 has not merged.
Stop and report.

- [ ] **Step 7: Add the harness journey (append to `tests/e2e/ui_audit/journeys_phase1.py`)**

```python
# --- §6.3: the ui.js behaviours that replaced inline handlers --------------------


async def _ui_abort(route) -> None:
    await route.abort()


async def journey_ui_behaviours(h) -> dict:
    """Each ui.js behaviour, driven in a real browser: a row click, a click on a link
    inside a row, a filter change, a show/hide toggle, a submit-on-change select."""
    context, page, _log = await h.page("admin")
    errors: list = []
    page.on("pageerror", errors.append)
    await context.route("https://orcid.org/**", _ui_abort)
    base = h.base_url
    out: dict = {}
    try:
        await page.goto(f"{base}/admin/users", wait_until="networkidle")
        row = page.locator("tr[data-row-href]").first
        want = await row.get_attribute("data-row-href")
        await row.locator("td").first.click()
        await page.wait_for_url(f"{base}{want}")
        out["row_href"] = page.url == f"{base}{want}"

        await page.goto(f"{base}/admin/users", wait_until="networkidle")
        async with context.expect_page() as popup_info:
            await page.locator("tr[data-row-href] a[target=_blank]").first.click()
        popup = await popup_info.value
        await popup.close()
        await page.wait_for_timeout(300)
        out["inner_link_kept_list"] = page.url == f"{base}/admin/users"

        await page.select_option("#status-filter", "complete")
        await page.wait_for_url(f"{base}/admin/users?status_filter=complete")
        out["filter_nav"] = page.url == f"{base}/admin/users?status_filter=complete"

        await page.goto(f"{base}/admin/cohorts", wait_until="networkidle")
        button = page.locator('[data-toggle-target="new-cohort-form"]')
        await button.click()
        shown = await page.locator("#new-cohort-form").is_visible()
        expanded = await button.get_attribute("aria-expanded")
        await button.click()
        hidden = not await page.locator("#new-cohort-form").is_visible()
        out["toggle"] = shown and hidden and expanded == "true"

        await page.goto(f"{base}/admin/assessments", wait_until="networkidle")
        sort = page.locator("#assessments-sort-select")
        current = await sort.input_value()
        values = await sort.locator("option").evaluate_all("(os) => os.map((o) => o.value)")
        other = next(v for v in values if v != current)
        await sort.select_option(other)
        await page.wait_for_url(f"**sort={other}**")
        out["autosubmit"] = f"sort={other}" in page.url
    finally:
        await context.close()
    checks = ("row_href", "inner_link_kept_list", "filter_nav", "toggle", "autosubmit")
    ok = all(out.get(k) for k in checks) and not errors
    return {"ok": ok, **out, "page_errors": [str(e)[:300] for e in errors]}


JOURNEYS += [journey_ui_behaviours]
```

`.venv-test/bin/python -m ruff check tests/e2e`. Expected: no findings.

- [ ] **Step 8: Commit**

```bash
git add static/js/ui.js templates/base.html templates/manager/pis.html templates/admin/users.html \
  templates/admin/jobs.html templates/admin/activity.html templates/manager/activity.html \
  templates/admin/cohorts.html templates/admin/_discussions_threads.html \
  templates/manager/discussions.html templates/manager/prompt_suggestions.html \
  templates/manager/assessments.html templates/admin/discussions.html templates/admin/simulation.html \
  templates/admin/assessments.html tests/unit/test_ui_behaviours.py \
  tests/integration/test_discussions_panel_cards.py tests/e2e/ui_audit/journeys_phase1.py
git commit -m "feat(webui-1A): move every inline on…= handler to static/js/ui.js data attributes (M-03)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A-7: Security headers and per-request nonces (M-03, A-06)

**Run before any Part 1C template task.** It edits `<script>` tags in 1C's templates.

**Files:**
- Create: `src/web/security_headers.py`, `tests/unit/test_security_headers.py`, `tests/unit/test_csp_templates.py`,
  `tests/integration/test_security_headers.py`
- Modify: `src/main.py` (imports; `OriginGuardMiddleware` docstring lines 127-130; `create_app` lines 284-293),
  `tests/integration/test_origin_guard.py:180-213`, and the templates of S3 and S10-S23 (the `<script>` opening tag only)

**Interfaces:** Consumes: nothing. Produces, in `src/web/security_headers.py`:
- `CSP_REPORT_PATH: str = "/api/csp-report"`
- `ENFORCED_POLICY: str`
- `REPORT_ONLY_POLICY: str` (a `str.format` template with `{nonce}`)
- `SCRIPT_POLICY_ENFORCED: bool = False`. Phase 2 sets it `True`; nothing else changes.
- `new_nonce() -> str`
- `security_header_items(nonce: str, *, enforce_script_policy: bool = SCRIPT_POLICY_ENFORCED) -> list[tuple[str, str]]`.
  Part 1C's 500 handler may call it.
- `class SecurityHeadersMiddleware` (pure ASGI). It sets `request.state.csp_nonce: str`.

Templates use `nonce="{{ request.state.csp_nonce }}"`.

**Middleware position, and why.** `SecurityHeadersMiddleware` is added after
`OriginGuardMiddleware`, so it is the outermost user middleware. The resulting order is
`SecurityHeadersMiddleware` → `OriginGuardMiddleware` → `SessionMiddleware`:
- The headers must be on every response, the guard's own `403 Cross-site request refused.`
  included. Only a layer outside the guard sees that response.
- The nonce must be in `scope["state"]` before any template renders, and any position outside
  the router satisfies that.
- The guard's invariant is "refuse before any session is decoded"
  (`test_the_guard_is_the_outermost_middleware`). It still holds: this layer refuses nothing,
  reads no session and no body, and costs one `secrets.token_urlsafe(16)` per request.

The structural test becomes `order[:2] == ["SecurityHeadersMiddleware", "OriginGuardMiddleware"]`.
A pure ASGI class was chosen over `BaseHTTPMiddleware` because it rewrites only the
`http.response.start` message and leaves streamed bodies alone. `request.state` is backed by
`scope["state"]` (`starlette/requests.py:189-196`, identical in 1.4.1 and 1.7.0), so
`scope.setdefault("state", {})["csp_nonce"]` is what `request.state.csp_nonce` reads. Setting a
header uses `MutableHeaders(scope=message).__setitem__`, which replaces any existing value
(`starlette/datastructures.py`, `MutableHeaders.__setitem__`, the same in both versions).

- [ ] **Step 1: Write the failing unit test** `tests/unit/test_security_headers.py`:

```python
"""Policies and header values of src/web/security_headers.py (spec §6.3)."""

import re

from src.web.security_headers import (
    CSP_REPORT_PATH,
    ENFORCED_POLICY,
    REPORT_ONLY_POLICY,
    SCRIPT_POLICY_ENFORCED,
    new_nonce,
    security_header_items,
)


def test_the_enforced_policy_is_the_spec_text():
    assert ENFORCED_POLICY == "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


def test_the_report_only_policy_is_the_spec_text():
    assert CSP_REPORT_PATH == "/api/csp-report"
    assert REPORT_ONLY_POLICY.format(nonce="N") == (
        "default-src 'self'; script-src 'self' 'nonce-N'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; form-action 'self' https://slack.com https://*.slack.com; report-uri /api/csp-report"
    )


def test_phase_1_reports_the_script_policy_and_enforces_the_rest():
    assert SCRIPT_POLICY_ENFORCED is False
    items = security_header_items("N")
    assert dict(items) == {
        "Content-Security-Policy": ENFORCED_POLICY,
        "Content-Security-Policy-Report-Only": REPORT_ONLY_POLICY.format(nonce="N"),
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    assert len(items) == len(dict(items))


def test_enforcing_moves_the_script_policy_into_the_enforced_header():
    """What Phase 2's flip produces: one enforced header, no report-only header."""
    items = dict(security_header_items("N", enforce_script_policy=True))
    assert "Content-Security-Policy-Report-Only" not in items
    assert items["Content-Security-Policy"] == (
        ENFORCED_POLICY + "; " + REPORT_ONLY_POLICY.format(nonce="N")
    )
    assert "report-uri /api/csp-report" in items["Content-Security-Policy"]


def test_nonces_are_fresh_and_need_no_escaping():
    nonces = {new_nonce() for _ in range(200)}
    assert len(nonces) == 200
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", n) for n in nonces)
```

- [ ] **Step 2: Write the failing source-level template test** `tests/unit/test_csp_templates.py`:

```python
"""Every inline <script> in templates/ carries the per-request nonce (spec §6.3).

Scans the source, so an inline block added by any later change (Phase 0, Parts 1B/1C)
fails here without a page that renders it."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
NONCE_ATTR = 'nonce="{{ request.state.csp_nonce }}"'
_SCRIPT_TAG = re.compile(r"<script\b[^>]*>", re.I)


def test_every_inline_script_carries_the_request_nonce():
    offenders = []
    checked = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _SCRIPT_TAG.finditer(text):
            tag = m.group(0)
            if re.search(r"\bsrc\s*=", tag):
                continue
            checked += 1
            if NONCE_ATTR not in tag:
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(TEMPLATES)}:{line}: {tag}")
    assert offenders == []
    # Control: the scan finds inline scripts at all. Not pinned to the inventory's count:
    # later tasks move inline scripts into files (1C-10 tag_widget.js, 2B-2), so a fixed
    # floor would break without any regression (assembly audit PX-08a).
    assert checked >= 1
```

- [ ] **Step 3: Write the failing integration test** `tests/integration/test_security_headers.py`:

```python
"""SecurityHeadersMiddleware on real responses (spec §6.3, M-03, A-06)."""

import re

import pytest

from src.models import USER_ROLE_ADMIN
from src.web.security_headers import ENFORCED_POLICY, REPORT_ONLY_POLICY
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _nonce(response) -> str:
    m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", response.headers["content-security-policy-report-only"])
    assert m, response.headers["content-security-policy-report-only"]
    return m.group(1)


def _assert_headers(response) -> None:
    assert response.headers["content-security-policy"] == ENFORCED_POLICY
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["content-security-policy-report-only"] == REPORT_ONLY_POLICY.format(
        nonce=_nonce(response)
    )
    assert len(response.headers.get_list("content-security-policy")) == 1


async def test_a_page_carries_every_security_header(client):
    r = await client.get("/login")
    assert r.status_code == 200
    _assert_headers(r)


async def test_every_inline_script_carries_this_responses_nonce(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.flush()
    for path, headers in (("/login", {}), ("/admin/users", auth_headers(admin.id))):
        r = await client.get(path, headers=headers)
        assert r.status_code == 200, path
        nonce = _nonce(r)
        inline = [t for t in re.findall(r"<script\b[^>]*>", r.text) if "src=" not in t]
        assert inline, f"{path} rendered no inline script; base.html has one"
        assert all(f'nonce="{nonce}"' in t for t in inline), (path, inline)


async def test_each_response_gets_a_fresh_nonce(client):
    first = await client.get("/login")
    second = await client.get("/login")
    assert _nonce(first) != _nonce(second)


@pytest.mark.parametrize("path", ["/static/js/ui.js", "/no-such-page", "/api/health"])
async def test_non_page_responses_carry_the_headers_too(client, path):
    r = await client.get(path)
    _assert_headers(r)


async def test_the_origin_guards_refusal_carries_the_headers(client_without_origin):
    r = await client_without_origin.post("/logout")
    assert r.status_code == 403
    assert r.text == "Cross-site request refused."
    _assert_headers(r)
```

- [ ] **Step 4: Run them and see them fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_security_headers.py tests/unit/test_csp_templates.py tests/integration/test_security_headers.py -v
```

Expected:
- `tests/unit/test_security_headers.py` fails at collection with
  `ModuleNotFoundError: No module named 'src.web.security_headers'`, and so does the integration
  file;
- `test_every_inline_script_carries_the_request_nonce` lists 15 or more offenders.

- [ ] **Step 5: Create `src/web/security_headers.py`**

```python
"""Security response headers and the per-request CSP nonce (spec §6.3; M-03, A-06).

``SecurityHeadersMiddleware`` gives every HTTP request a fresh nonce on
``request.state.csp_nonce`` (templates mark each inline ``<script>`` with it) and sets,
on every response it sends:

* ``Content-Security-Policy: ENFORCED_POLICY`` — framing, ``<base>`` and plugins off;
* ``Content-Security-Policy-Report-Only`` — ``REPORT_ONLY_POLICY`` with this request's
  nonce, reporting to ``CSP_REPORT_PATH``;
* ``X-Frame-Options``, ``X-Content-Type-Options`` and ``Referrer-Policy``.

Phase 2 enforces the script policy by setting ``SCRIPT_POLICY_ENFORCED = True``: the
same directives then travel in the enforced header and the report-only header is no
longer sent. Nothing else changes.

A plain ASGI middleware, not ``BaseHTTPMiddleware``: it only rewrites the
``http.response.start`` message, so a streamed body passes through untouched. A 500
from an unhandled exception is sent by Starlette's ``ServerErrorMiddleware``, which
sits outside every user middleware, so that response carries these headers only if
the app's 500 handler adds ``security_header_items`` itself. ``csp_nonce`` is already
on that request's state.
"""

from __future__ import annotations

import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The violation-report sink (src/routers/csp_report.py). Named in REPORT_ONLY_POLICY
#: and exempted, for this exact path, from OriginGuardMiddleware.
CSP_REPORT_PATH = "/api/csp-report"

#: Always enforced (spec §6.3).
ENFORCED_POLICY = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"

#: The script policy: a ``str.format`` template with one ``{nonce}`` field.
REPORT_ONLY_POLICY = (
    "default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; form-action 'self' https://slack.com https://*.slack.com; report-uri " + CSP_REPORT_PATH
)

#: False in Phase 1 (report only). Phase 2 sets it True and changes nothing else.
SCRIPT_POLICY_ENFORCED = False

_NONCE_BYTES = 16


def new_nonce() -> str:
    """16 random bytes, URL-safe base64 (22 characters): valid in a CSP nonce-source
    and in an HTML attribute without escaping."""
    return secrets.token_urlsafe(_NONCE_BYTES)


def security_header_items(
    nonce: str, *, enforce_script_policy: bool = SCRIPT_POLICY_ENFORCED
) -> list[tuple[str, str]]:
    """The headers every response carries, for one request's nonce."""
    script_policy = REPORT_ONLY_POLICY.format(nonce=nonce)
    items = [
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ]
    if enforce_script_policy:
        items.append(("Content-Security-Policy", f"{ENFORCED_POLICY}; {script_policy}"))
    else:
        items.append(("Content-Security-Policy", ENFORCED_POLICY))
        items.append(("Content-Security-Policy-Report-Only", script_policy))
    return items


class SecurityHeadersMiddleware:
    """Sets ``request.state.csp_nonce`` and the headers of ``security_header_items``.

    A header a route already set under one of these names is replaced, so these are
    the only policies the app sends. Non-HTTP scopes pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = new_nonce()
        scope.setdefault("state", {})["csp_nonce"] = nonce
        items = security_header_items(nonce)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in items:
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
```

- [ ] **Step 6: Wire it in `src/main.py`**

Add to the imports, after `from src.services.assessment_chat import drain_live_tasks`:

```python
from src.web.security_headers import SecurityHeadersMiddleware
```

In `create_app()`, replace the CSRF-guard block (lines 284-293):

```python
    # CSRF guard. Added LAST, so it is the OUTERMOST middleware: Starlette's
    # add_middleware prepends. It reads headers only and needs no session, so
    # running it outside SessionMiddleware is both correct and cheaper — a
    # forged POST is refused before the session middleware decodes a cookie.
    #
    # Outermost is a REQUIREMENT, not a preference, and no request-level
    # assertion can see it (a refused request never modifies the session, so
    # SessionMiddleware emits no Set-Cookie either way). It is pinned
    # structurally by test_origin_guard.py::test_the_guard_is_the_outermost_middleware.
    application.add_middleware(OriginGuardMiddleware)
```

with

```python
    # CSRF guard: the outermost middleware that can REFUSE a request (Starlette's
    # add_middleware prepends, so later calls wrap earlier ones). It reads headers
    # only and needs no session, so running it outside SessionMiddleware is both
    # correct and cheaper — a forged POST is refused before the session middleware
    # decodes a cookie.
    #
    # Outside the session is a REQUIREMENT, not a preference, and no request-level
    # assertion can see it (a refused request never modifies the session, so
    # SessionMiddleware emits no Set-Cookie either way). Pinned structurally by
    # test_origin_guard.py::test_the_guard_is_the_outermost_refusing_middleware.
    application.add_middleware(OriginGuardMiddleware)

    # Security headers and the per-request CSP nonce (spec §6.3). Added after the
    # guard, so it is the one layer OUTSIDE it: the guard's own 403 carries the
    # headers too, and every template sees request.state.csp_nonce. It refuses
    # nothing and reads no session or body, so the guard still refuses before any
    # session is decoded.
    application.add_middleware(SecurityHeadersMiddleware)
```

In the `OriginGuardMiddleware` docstring, replace

```
    Added LAST in create_app(), because Starlette's ``add_middleware``
    *prepends*: last added is outermost. Outermost is both correct and cheaper
    here — this reads headers only and needs no session, so it refuses before
    the session is decoded or any route runs.
```

with

```
    Added after SessionMiddleware in create_app(), because Starlette's
    ``add_middleware`` *prepends*: later added is further out. Only
    SecurityHeadersMiddleware sits outside it, and that layer refuses nothing.
    Outside the session is both correct and cheaper here — this reads headers
    only, so it refuses before the session is decoded or any route runs.
```

- [ ] **Step 7: Update the order test in `tests/integration/test_origin_guard.py`**

Replace `test_the_guard_is_the_outermost_middleware` (lines 180-213) with:

```python
def test_the_guard_is_the_outermost_refusing_middleware():
    """Structural, because no request-level assertion in this file can see it.

    The obvious behavioural proxy — "a refused request set no session cookie",
    asserted in ``test_a_post_without_an_origin_is_refused`` — carries ZERO
    ordering signal. Starlette's ``SessionMiddleware.send_wrapper`` emits
    ``Set-Cookie`` only ``if session.modified and session``, and a request the
    guard refuses never touches the session at all. So the invariant needs a
    direct look at the stack.

    Why outside the session is the requirement: everything the guard is in front
    of costs something on a request it is going to refuse — the session cookie is
    decoded and a route handler may open a database session.

    The one layer outside it is SecurityHeadersMiddleware (spec §6.3), so the
    guard's own 403 carries the security headers. That layer refuses nothing and
    reads no session or body.

    ``user_middleware`` is in outermost-to-innermost order (Starlette's
    ``build_middleware_stack`` wraps in reverse), and ``add_middleware``
    PREPENDS — so "outermost" means "added last in create_app()".
    """
    from src.main import create_app

    order = [m.cls.__name__ for m in create_app().user_middleware]
    assert order[:2] == ["SecurityHeadersMiddleware", "OriginGuardMiddleware"], (
        f"the CSRF guard is not directly inside the headers layer; stack is {order}"
    )
    assert order.index("OriginGuardMiddleware") < order.index("SessionMiddleware")
```

- [ ] **Step 8: Add the nonce to every inline script**

First list the openings: `grep -rn '<script>' templates`. Expected: exactly the S3 and S10-S23
lines from the inventory, plus any bare inline block Phase 0 added, each on a line with no other
`<script>` text. Then:

```bash
grep -rl '<script>' templates | xargs sed -i 's|<script>|<script nonce="{{ request.state.csp_nonce }}">|g'
```

If Step 1 of Task 1A-1 recorded a Phase 0 inline block opened with attributes (`<script type=…>`),
add ` nonce="{{ request.state.csp_nonce }}"` to that tag by hand.

- [ ] **Step 9: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/unit/test_security_headers.py tests/unit/test_csp_templates.py tests/integration/test_security_headers.py tests/integration/test_origin_guard.py tests/unit/test_no_dead_src_symbols.py tests/unit/test_reachability.py tests/integration/test_assessment_chat_templates.py tests/integration/test_admin_simulation_page.py -v
```

Expected: all pass. `test_no_dead_src_symbols` is green because `__call__` (a dunder) is a live
root and it reaches `new_nonce` and `security_header_items`.

- [ ] **Step 10: Commit**

```bash
git add src/web/security_headers.py src/main.py templates tests/unit/test_security_headers.py \
  tests/unit/test_csp_templates.py tests/integration/test_security_headers.py tests/integration/test_origin_guard.py
git commit -m "feat(webui-1A): security headers and per-request CSP nonce on every inline script (M-03, A-06)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A-8: `POST /api/csp-report` with an exact-path Origin-guard exemption (M-03)

**Files:**
- Create: `src/routers/csp_report.py`, `tests/integration/test_csp_report.py`
- Modify: `src/main.py` (router import list lines 15-26; `OriginGuardMiddleware.dispatch` after line 140;
  its docstring lines 132-133; `include_router` block lines 315-327), `tests/integration/test_origin_guard.py:173-177`,
  `tests/e2e/ui_audit/journeys_phase1.py` (append)

**Interfaces:** Consumes: `CSP_REPORT_PATH` (1A-7) and `_seed_path` (1A-5). Produces:
- the route `POST /api/csp-report`;
- `src/routers/csp_report.py`: `router`, `MAX_BODY_BYTES = 16 * 1024`, `MAX_REPORTS_PER_REQUEST = 20` and
  `MAX_FIELD_CHARS = 512`;
- the log line `csp_report {"blocked_uri": …, "directive": …, "document_uri": …}` on logger
  `src.routers.csp_report`;
- `journey_csp_report_only(h) -> dict`.

- [ ] **Step 1: Write the failing tests** `tests/integration/test_csp_report.py`:

```python
"""POST /api/csp-report (spec §6.3, M-03): the browsers' CSP violation reports."""

import json
import logging

import pytest

pytestmark = pytest.mark.integration

LOGGER = "src.routers.csp_report"

LEGACY = {
    "csp-report": {
        "document-uri": "https://blackbird.copi.science/admin/users",
        "violated-directive": "script-src-elem",
        "effective-directive": "script-src-elem",
        "blocked-uri": "inline",
        "original-policy": "default-src 'self'",
    }
}
REPORTING_API = [
    {
        "type": "csp-violation",
        "age": 0,
        "url": "https://blackbird.copi.science/admin/jobs",
        "body": {
            "documentURL": "https://blackbird.copi.science/admin/jobs",
            "blockedURL": "https://cdn.example/x.js",
            "effectiveDirective": "script-src-elem",
            "disposition": "report",
        },
    }
]


def _lines(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        if rec.name == LOGGER and rec.getMessage().startswith("csp_report {"):
            out.append(json.loads(rec.getMessage()[len("csp_report "):]))
    return out


async def test_a_legacy_report_is_logged_and_answered_204(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    r = await client_without_origin.post(
        "/api/csp-report",
        content=json.dumps(LEGACY),
        headers={"Content-Type": "application/csp-report"},
    )
    assert r.status_code == 204
    assert r.content == b""
    assert _lines(caplog) == [
        {
            "directive": "script-src-elem",
            "blocked_uri": "inline",
            "document_uri": "https://blackbird.copi.science/admin/users",
        }
    ]


async def test_a_reporting_api_batch_is_logged_one_line_per_report(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    other = {"type": "deprecation", "body": {"id": "x"}}
    r = await client_without_origin.post(
        "/api/csp-report",
        content=json.dumps(REPORTING_API + [other]),
        headers={"Content-Type": "application/reports+json; charset=utf-8"},
    )
    assert r.status_code == 204
    assert _lines(caplog) == [
        {
            "directive": "script-src-elem",
            "blocked_uri": "https://cdn.example/x.js",
            "document_uri": "https://blackbird.copi.science/admin/jobs",
        }
    ]


async def test_another_content_type_is_refused_unread(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    r = await client_without_origin.post(
        "/api/csp-report", content=json.dumps(LEGACY), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 415
    assert _lines(caplog) == []


async def test_a_declared_oversize_body_is_refused(client_without_origin):
    r = await client_without_origin.post(
        "/api/csp-report",
        content=b"{" + b" " * (16 * 1024) + b"}",
        headers={"Content-Type": "application/csp-report"},
    )
    assert r.status_code == 413


async def test_a_streamed_oversize_body_is_refused(client_without_origin):
    """No Content-Length: the cap is enforced while reading, not only from the header."""

    async def chunks():
        for _ in range(3):
            yield b" " * 8192

    r = await client_without_origin.post(
        "/api/csp-report", content=chunks(), headers={"Content-Type": "application/csp-report"}
    )
    assert r.status_code == 413


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[" * 5000 + b"]" * 5000, b"\xff\xfe"],
    ids=["garbage", "deep-nesting", "bad-utf8"],
)
async def test_an_unparseable_body_is_a_400_not_a_500(client_without_origin, body):
    r = await client_without_origin.post(
        "/api/csp-report", content=body, headers={"Content-Type": "application/reports+json"}
    )
    assert r.status_code == 400


async def test_a_flood_of_reports_is_capped(client_without_origin, caplog, monkeypatch):
    """100 small reports (about 10 KB, under the body cap) log 20 lines: the rest of
    that request is dropped, and the process-wide window caps a sustained flood."""
    import src.routers.csp_report as csp_route

    monkeypatch.setattr(csp_route, "_window", {"start": 0.0, "logged": 0, "suppressed": 0})
    caplog.set_level(logging.INFO, logger=LOGGER)
    small = {"type": "csp-violation", "body": {"effectiveDirective": "img-src", "blockedURL": "x", "documentURL": "/"}}
    batch = [small] * 100
    assert len(json.dumps(batch)) < 16 * 1024
    r = await client_without_origin.post(
        "/api/csp-report",
        content=json.dumps(batch),
        headers={"Content-Type": "application/reports+json"},
    )
    assert r.status_code == 204
    assert len(_lines(caplog)) == 20
    for _ in range(4):  # 80 more admissible lines; the window admits 40 of them
        await client_without_origin.post(
            "/api/csp-report", content=json.dumps(batch),
            headers={"Content-Type": "application/reports+json"},
        )
    assert len(_lines(caplog)) == csp_route.MAX_LINES_PER_WINDOW
    later = csp_route._clock() + 3600.0
    monkeypatch.setattr(csp_route, "_clock", lambda: later)  # the next window
    await client_without_origin.post(
        "/api/csp-report", content=json.dumps([small]),
        headers={"Content-Type": "application/reports+json"},
    )
    assert any(rec.getMessage() == "csp_report suppressed=40 in the last window"
               for rec in caplog.records)


async def test_fields_cannot_forge_or_bloat_a_log_line(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    hostile = {
        "csp-report": {
            "document-uri": "https://x/\nCRITICAL forged line",
            "effective-directive": "script-src",
            "blocked-uri": "https://x/" + "a" * 5000,
        }
    }
    r = await client_without_origin.post(
        "/api/csp-report", content=json.dumps(hostile), headers={"Content-Type": "application/csp-report"}
    )
    assert r.status_code == 204
    [rec] = [rec for rec in caplog.records if rec.name == LOGGER]
    assert "\n" not in rec.getMessage()
    [line] = _lines(caplog)
    assert line["document_uri"] == "https://x/\nCRITICAL forged line"
    assert len(line["blocked_uri"]) == 512
```

- [ ] **Step 2: Replace `test_no_path_is_exempt_from_the_origin_check` in `tests/integration/test_origin_guard.py`** (lines 173-177) with:

```python
async def test_only_the_csp_report_path_is_exempt_from_the_origin_check(client_without_origin):
    """One exemption, POST /api/csp-report exactly: a browser's violation report
    carries no Origin of ours. Every neighbouring shape stays guarded, including the
    path that used to carry the one-click-unsubscribe exemption."""
    body = json.dumps({"csp-report": {"effective-directive": "script-src", "blocked-uri": "inline"}})
    ctype = {"Content-Type": "application/csp-report"}

    for origin in (None, "https://evil.example", "null"):
        headers = ctype if origin is None else {**ctype, "Origin": origin}
        r = await client_without_origin.post("/api/csp-report", content=body, headers=headers)
        assert r.status_code == 204, (origin, r.status_code)

    for path in (
        "/settings/unsubscribe/some-token",
        "/api/csp-report/",
        "/API/csp-report",
        "/api/csp-report-x",
        "/api/csp-reportx",
        "/api/csp",
        "/logout",
    ):
        r = await client_without_origin.post(path, content=body, headers=ctype)
        assert r.status_code == 403, (path, r.status_code)
        assert r.text == "Cross-site request refused."

    r = await client_without_origin.put("/api/csp-report", content=body, headers=ctype)
    assert r.status_code == 403
```

Add `import json` to the module's imports, before `from http.cookies import SimpleCookie`.

- [ ] **Step 3: Run them and see them fail**

```bash
.venv-test/bin/python -m pytest tests/integration/test_csp_report.py tests/integration/test_origin_guard.py::test_only_the_csp_report_path_is_exempt_from_the_origin_check -v
```

Expected: each csp-report test gets `403` (the guard refuses, since no route is exempt) instead
of 204, 415, 413 or 400. The exemption test fails on its first `204` assertion.

- [ ] **Step 4: Create `src/routers/csp_report.py`**

```python
"""``POST /api/csp-report``: the sink for browsers' CSP violation reports (spec §6.3).

Browsers post here because ``report-uri`` names this path, both in the app's own
report-only policy (src/web/security_headers.py) and in the shared nginx's policy on
this vhost. No authentication, and exempt from ``OriginGuardMiddleware`` for this
exact path only: a violation report is not a form post from one of our pages and
may carry no Origin, or an opaque one. It stores nothing. Each report becomes one
JSON log line, bounded in size and count, so the endpoint cannot be turned into a
log flood or a log forgery.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.web.security_headers import CSP_REPORT_PATH

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 16 * 1024
MAX_REPORTS_PER_REQUEST = 20
MAX_FIELD_CHARS = 512
#: Process-wide cap on logged report lines (plan audit Q1-10): the route is anonymous
#: and Origin-exempt, so without it a flood could rotate the json-file logs and erase
#: recent warnings. Suppressed reports are counted and summarised once per window.
MAX_LINES_PER_WINDOW = 60
WINDOW_SECONDS = 60.0
_window = {"start": 0.0, "logged": 0, "suppressed": 0}
#: The window's clock; a module attribute so a test can move it without patching the
#: time module the event loop also uses.
_clock = time.monotonic


def _admit(n: int, now: float) -> int:
    """How many of ``n`` report lines may be logged now; counts the rest."""
    if now - _window["start"] >= WINDOW_SECONDS:
        if _window["suppressed"]:
            logger.info("csp_report suppressed=%d in the last window", _window["suppressed"])
        _window.update(start=now, logged=0, suppressed=0)
    room = max(0, MAX_LINES_PER_WINDOW - _window["logged"])
    take = min(n, room)
    _window["logged"] += take
    _window["suppressed"] += n - take
    return take

#: ``report-uri`` sends the first; the Reporting API (``report-to``) the second.
_CONTENT_TYPES = frozenset({"application/csp-report", "application/reports+json"})


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None once it is known to exceed MAX_BODY_BYTES (the rest is
    never read)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def _field(value: Any) -> str:
    return "" if value is None else str(value)[:MAX_FIELD_CHARS]


def _reports(content_type: str, payload: Any) -> list[dict[str, str]]:
    """(directive, blocked URI, document URI) for each CSP report in the payload."""
    if content_type == "application/csp-report":
        body = payload.get("csp-report") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return []
        return [{
            "directive": _field(body.get("effective-directive") or body.get("violated-directive")),
            "blocked_uri": _field(body.get("blocked-uri")),
            "document_uri": _field(body.get("document-uri")),
        }]
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "csp-violation":
            continue
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        out.append({
            "directive": _field(body.get("effectiveDirective")),
            "blocked_uri": _field(body.get("blockedURL")),
            "document_uri": _field(body.get("documentURL")),
        })
    return out


@router.post(CSP_REPORT_PATH)
async def csp_report(request: Request) -> Response:
    """Log each report (one JSON line apiece) and answer 204."""
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type not in _CONTENT_TYPES:
        return Response(status_code=415)
    raw = await _read_capped(request)
    if raw is None:
        return Response(status_code=413)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON and undecodable bytes; RecursionError is
        # what CPython's json raises on nesting a 16 KB body can still hold.
        return Response(status_code=400)
    reports = _reports(content_type, payload)[:MAX_REPORTS_PER_REQUEST]
    for report in reports[: _admit(len(reports), _clock())]:
        logger.info("csp_report %s", json.dumps(report, sort_keys=True))
    return Response(status_code=204)
```

- [ ] **Step 5: Edit `src/main.py`**

- Router import list (lines 15-26): add `csp_report,` after `auth,`.
- The security-headers import line becomes
  `from src.web.security_headers import CSP_REPORT_PATH, SecurityHeadersMiddleware`.
- In `OriginGuardMiddleware.dispatch`, directly after `        path = request.url.path`, insert:

```python

        if path == CSP_REPORT_PATH and request.method.upper() == "POST":
            # The CSP report sink (src/routers/csp_report.py): a browser's violation
            # report is not a form post from one of our pages and may carry no Origin
            # or an opaque one. Exact path and POST only; that route reads no session
            # and writes nothing but a bounded log line.
            return await call_next(request)
```

- In the class docstring, replace
  `    Not affected, verified rather than assumed: the ORCID callback is a GET;` /
  `    there is no inbound Slack POST route, and there is no CORSMiddleware.` with:

```
    One exemption, by exact path: ``POST CSP_REPORT_PATH`` (the CSP report sink),
    pinned by test_origin_guard.py::test_only_the_csp_report_path_is_exempt_from_the_origin_check.

    Not affected, verified rather than assumed: the ORCID callback is a GET;
    there is no inbound Slack POST route, and there is no CORSMiddleware.
```

- After `    application.include_router(auth.router, tags=["auth"])`, add
  `    application.include_router(csp_report.router, tags=["csp"])`.

The reachability gate credits the new route through the `src/` strings `CSP_REPORT_PATH = "/api/csp-report"`
and `REPORT_ONLY_POLICY`'s `report-uri /api/csp-report` (`_candidate_paths` splits on `;` and
spaces). The decorator takes a name, not a literal, so it adds no self-credit.

- [ ] **Step 6: Run the tests and see them pass**

```bash
.venv-test/bin/python -m pytest tests/integration/test_csp_report.py tests/integration/test_origin_guard.py tests/integration/test_security_headers.py tests/unit/test_reachability.py tests/unit/test_no_dead_src_symbols.py -v
```

Expected: all pass. httpx's ASGI transport sends an async-generator body without
`Content-Length`, as several `http.request` messages, which is what
`test_a_streamed_oversize_body_is_refused` relies on.

- [ ] **Step 7: Add the harness journey (append to `tests/e2e/ui_audit/journeys_phase1.py`)**

```python
# --- §6.3 / §9: CSP report-only violations over a crawl --------------------------

CSP_PAGES: tuple[tuple[str, str], ...] = (
    ("admin", "/admin/users"),
    ("admin", "/admin/users/{pi}"),
    ("admin", "/admin/jobs"),
    ("admin", "/admin/activity"),
    ("admin", "/admin/activity/{run}"),
    ("admin", "/admin/discussions"),
    ("admin", "/admin/agents"),
    ("admin", "/admin/assessments"),
    ("admin", "/admin/assessments/{a0}"),
    ("admin", "/admin/cohorts"),
    ("admin", "/admin/cohorts/topology"),
    ("admin", "/admin/access-requests"),
    ("admin", "/admin/simulation"),
    ("admin", "/admin/simulation?run={run}"),
    ("admin", "/manager/prompt-suggestions"),
    ("manager", "/manager/pis"),
    ("manager", "/manager/pis/{pi}"),
    ("manager", "/manager/assessments"),
    ("manager", "/manager/assessments/{a0}"),
    ("manager", "/manager/discussions"),
    ("manager", "/manager/activity"),
    ("manager", "/manager/activity/{run}"),
    ("reviewer", "/manager/assessments/{a0}"),
    ("pi", "/profile"),
    ("pi", "/profile/edit"),
    ("pi", "/settings"),
)

_CSP_COLLECTOR_JS = (
    "window.__cspViolations = [];\n"
    "document.addEventListener('securitypolicyviolation', (e) => {\n"
    "  window.__cspViolations.push({directive: e.effectiveDirective, blocked: e.blockedURI,\n"
    "    source: e.sourceFile, line: e.lineNumber, sample: e.sample, disposition: e.disposition});\n"
    "});\n"
)


async def _csp_agent_paths(page, base_url: str) -> list[str]:
    """/agent and up to five /agent/... pages it links (the PI's own agent pages)."""
    await page.goto(base_url + "/agent", wait_until="networkidle")
    hrefs = await page.evaluate(
        "() => [...document.querySelectorAll('a[href^=\"/agent/\"]')].map((a) => a.getAttribute('href'))"
    )
    return ["/agent", *list(dict.fromkeys(hrefs))[:5]]


async def journey_csp_report_only(h) -> dict:
    """Every page with an inline script or a moved handler, per role, collecting each
    report-only violation the browser raises. Phase 1 expects none; any entry is what
    Phase 2's enforced policy would block, and is reviewed before deploy."""
    violations: list[dict] = []
    errors: list = []
    visited: list[str] = []
    for role in ("admin", "manager", "reviewer", "pi"):
        context, page, _log = await h.page(role)
        await context.add_init_script(_CSP_COLLECTOR_JS)
        page.on("pageerror", errors.append)
        try:
            paths = [_seed_path(p, h.ids) for r, p in CSP_PAGES if r == role]
            if role == "pi":
                paths += await _csp_agent_paths(page, h.base_url)
            for path in paths:
                await page.goto(h.base_url + path, wait_until="networkidle")
                await page.wait_for_timeout(300)
                visited.append(f"{role} {path}")
                for v in await page.evaluate("() => window.__cspViolations || []"):
                    violations.append({"role": role, "path": path, **v})
        finally:
            await context.close()
    return {
        "ok": not violations and not errors,
        "visited": visited,
        "violations": violations,
        "page_errors": [str(e)[:300] for e in errors],
    }


JOURNEYS += [journey_csp_report_only]
```

`.venv-test/bin/python -m ruff check tests/e2e`. Expected: no findings.

- [ ] **Step 8: Commit**

```bash
git add src/routers/csp_report.py src/main.py tests/integration/test_csp_report.py \
  tests/integration/test_origin_guard.py tests/e2e/ui_audit/journeys_phase1.py
git commit -m "feat(webui-1A): POST /api/csp-report with an exact-path Origin-guard exemption (M-03)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1A-9: Compiled Tailwind replaces the Play CDN (A-03, D4)

Do this last among the template-touching tasks of this part. The build reads every template,
JS file and `src/` file.

**Files:**
- Create: `tailwind.config.js`, `static/css/input.css`, `static/css/app.css` (generated), `scripts/build_css.sh`,
  `tests/unit/test_compiled_css.py`
- Modify: `templates/base.html:7-9`, `scripts/ci.sh:7-22, 128-130`, `.gitignore` (end), `.dockerignore` (end),
  `tests/unit/test_ci_script.py` (append), `tests/unit/test_csp_templates.py` (append), `AGENT.md:49, 100-102`,
  `specs/tech-stack.md:11`, `docs/operations/testing.md:7-17`, `CLAUDE.md:24-28`,
  `tests/e2e/ui_audit/journeys_phase1.py` (append)

**Interfaces:** Consumes: the R-table, and `_seed_path` (1A-5). Produces:
- `scripts/build_css.sh [--check]`. It exits 0 when it is built and, with `--check`, unchanged;
  it exits 1 on drift, a checksum mismatch or an unsupported platform.
- `static/css/app.css`, linked from `base.html`.
- `journey_css_parity(h) -> dict`.

- [ ] **Step 1: Write the failing test** `tests/unit/test_compiled_css.py`:

```python
"""Compiled Tailwind (spec §6.1; A-03, D4): pinned build, committed output, and a
safelist derived from every place a utility class is assembled at runtime."""

import re
from pathlib import Path

from src.services.bands import BANDS, band_class

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
CONFIG = (ROOT / "tailwind.config.js").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_css.sh").read_text(encoding="utf-8")
CSS_PATH = ROOT / "static" / "css" / "app.css"

#: The (strong, muted) shapes band_class is called with in templates (R5).
BAND_CALLS = {(600, 400), (700, 600)}

#: Template sites that splice a value into a utility name (R1-R4). A new site must
#: add its classes to tailwind.config.js's safelist and its entry here.
CONSTRUCTED_SITES = {
    ("admin/discussions.html", "meta.color"),
    ("manager/discussions.html", "meta.color"),
    ("admin/_discussions_threads.html", "meta.color"),
    ("admin/jobs.html", "color"),
}
#: The same for src/ and static/js (R5).
CONSTRUCTED_CODE_LINES = {"src/services/bands.py:43", "src/services/bands.py:44"}

_TEMPLATE_SPLICE = re.compile(
    r"(?<![\w-])(?:[a-z-]+:)*(?:bg|text|border|ring|from|via|to|fill|stroke|divide|outline"
    r"|decoration|shadow|accent|caret|placeholder)-\{\{\s*([\w.]+)\s*\}\}"
)
_CODE_SPLICE = re.compile(r"""(?<![\w-])(?:bg|text|border|ring)-(?:[a-z]+-)?(?:\{|["'`]\s*\+|\$\{)""")


def _safelist() -> set[str]:
    block = CONFIG[CONFIG.index("safelist: [") :]
    block = block[: block.index("],")]
    return set(re.findall(r'"([^"]+)"', block))


def _text(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _status_meta_colours(name: str) -> set[str]:
    text = _text(name)
    block = text[text.index("{% set status_meta = {") :]
    block = block[: block.index("} %}")]
    return set(re.findall(r"'color':\s*'(\w+)'", block))


def _job_colours() -> set[str]:
    text = _text("admin/jobs.html")
    block = text[text.index("{% for status, label, color in [") :]
    block = block[: block.index("] %}")]
    return set(re.findall(r"\('\w+', '[^']+', '(\w+)'\)", block))


def test_the_build_is_pinned_and_verified():
    assert 'TAILWIND_VERSION="v3.4.19"' in BUILD
    assert 'TAILWIND_ASSET="tailwindcss-linux-x64"' in BUILD
    assert 'TAILWIND_SHA256="4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e"' in BUILD
    assert "sha256sum --check --status" in BUILD
    assert '.tools' in BUILD


def test_the_config_scans_the_spec_globs():
    for glob in ('"./templates/**/*.html"', '"./static/js/**/*.js"', '"./src/**/*.py"'):
        assert glob in CONFIG, glob


def test_the_tools_directory_is_ignored_and_kept_out_of_the_build_context():
    assert ".tools/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".tools" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_band_classes_are_safelisted():
    produced = {band_class(b.name, s, m) for b in BANDS for s, m in BAND_CALLS}
    produced |= {band_class(None, s, m) for s, m in BAND_CALLS}
    assert produced <= _safelist(), sorted(produced - _safelist())


def test_band_class_is_called_only_with_the_inventoried_shapes():
    calls = set()
    for path in TEMPLATES.rglob("*.html"):
        for a, b in re.findall(r"band_class\([^,()]+,\s*(\d+),\s*(\d+)\)", path.read_text(encoding="utf-8")):
            calls.add((int(a), int(b)))
    assert calls == BAND_CALLS


def test_discussion_status_colours_are_safelisted():
    colours = (
        _status_meta_colours("admin/discussions.html")
        | _status_meta_colours("manager/discussions.html")
        | {"gray"}  # _discussions_threads.html's fallback
    )
    wanted = set()
    for c in colours:
        wanted |= {f"hover:border-{c}-300", f"ring-{c}-400", f"text-{c}-600", f"bg-{c}-100", f"text-{c}-700"}
    assert wanted <= _safelist(), sorted(wanted - _safelist())


def test_job_status_colours_are_safelisted():
    wanted = {f"text-{c}-600" for c in _job_colours()}
    assert wanted
    assert wanted <= _safelist(), sorted(wanted - _safelist())


def test_no_new_runtime_built_classes_in_templates():
    found = set()
    for path in TEMPLATES.rglob("*.html"):
        rel = path.relative_to(TEMPLATES).as_posix()
        for m in _TEMPLATE_SPLICE.finditer(path.read_text(encoding="utf-8")):
            found.add((rel, m.group(1)))
    assert found == CONSTRUCTED_SITES


def test_no_new_runtime_built_classes_in_src_or_js():
    hits = set()
    for path in [*(ROOT / "src").rglob("*.py"), *(ROOT / "static" / "js").rglob("*.js")]:
        if "__pycache__" in path.parts:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _CODE_SPLICE.search(line):
                hits.add(f"{path.relative_to(ROOT).as_posix()}:{i}")
    assert hits == CONSTRUCTED_CODE_LINES


def test_the_committed_stylesheet_is_the_pinned_build_and_holds_the_safelist():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert css.startswith("/*! tailwindcss v3.4.19")
    missing = [c for c in sorted(_safelist()) if "." + c.replace(":", "\\:") not in css]
    assert missing == []


def test_base_links_the_compiled_stylesheet_and_no_template_loads_the_cdn():
    base = _text("base.html")
    assert '<link rel="stylesheet" href="/static/css/app.css">' in base
    for path in TEMPLATES.rglob("*.html"):
        assert "cdn.tailwindcss.com" not in path.read_text(encoding="utf-8"), path
```

Append to `tests/unit/test_csp_templates.py`:

```python
def test_no_script_is_loaded_from_another_origin():
    """With Tailwind compiled (§6.1) and marked/DOMPurify vendored (§6.2), every
    external script is ours, as script-src 'self' requires."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for m in re.finditer(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)", path.read_text(encoding="utf-8")):
            if not m.group(1).startswith("/static/"):
                offenders.append(f"{path.relative_to(TEMPLATES)}: {m.group(1)}")
    assert offenders == []
```

Append to `tests/unit/test_ci_script.py`:

```python
def test_the_css_drift_check_runs_before_the_lint_and_test_steps():
    text = CI.read_text()
    step = 'bash "$REPO_ROOT/scripts/build_css.sh" --check'
    assert step in text
    assert text.index(step) < text.index('echo "==> ruff (test-suite lint)"')
```

- [ ] **Step 2: Run them and see them fail**

```bash
.venv-test/bin/python -m pytest tests/unit/test_compiled_css.py tests/unit/test_csp_templates.py tests/unit/test_ci_script.py -v
```

Expected:
- `test_compiled_css.py` fails at collection with `FileNotFoundError` on `tailwind.config.js`;
- `test_no_script_is_loaded_from_another_origin` lists `base.html: https://cdn.tailwindcss.com`;
- the ci test fails on the missing step.

- [ ] **Step 3: Create `tailwind.config.js`**

```js
/**
 * Tailwind v3.4.19 configuration for the compiled stylesheet (spec §6.1, D4).
 * Built by scripts/build_css.sh into static/css/app.css; ci.sh fails when the
 * committed file differs from a fresh build.
 *
 * `safelist` holds every utility class the code assembles at runtime, which a
 * content scan cannot see (inventory R1-R5, Task 1A-1 of
 * docs/plans/2026-10-01-web-ui-remediation-phase-1.md). tests/unit/test_compiled_css.py
 * derives the same set from its sources and fails on a new assembling site.
 */
module.exports = {
  content: ["./templates/**/*.html", "./static/js/**/*.js", "./src/**/*.py"],
  safelist: [
    // R1, R2: discussions status cards (templates/{admin,manager}/discussions.html)
    "hover:border-gray-300", "hover:border-blue-300", "hover:border-green-300",
    "hover:border-amber-300", "hover:border-red-300",
    "ring-gray-400", "ring-blue-400", "ring-green-400", "ring-amber-400", "ring-red-400",
    "text-gray-600", "text-blue-600", "text-green-600", "text-amber-600", "text-red-600",
    // R3: thread status chips (templates/admin/_discussions_threads.html)
    "bg-gray-100", "bg-blue-100", "bg-green-100", "bg-amber-100", "bg-red-100",
    "text-gray-700", "text-blue-700", "text-green-700", "text-amber-700", "text-red-700",
    // R4: job status counts (templates/admin/jobs.html)
    "text-yellow-600",
    // R5: band_class (src/services/bands.py) muted shades
    "text-gray-400",
  ],
  theme: { extend: {} },
  plugins: [],
};
```

R4's other colours and R5's strong shades are already in the R1-R3 lines. The tests check the
union.

- [ ] **Step 4: Create `static/css/input.css`**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

- [ ] **Step 5: Create `scripts/build_css.sh`**

```bash
#!/usr/bin/env bash
#
# Build static/css/app.css with the pinned Tailwind standalone CLI (spec §6.1, D4).
#
#   scripts/build_css.sh          rebuild static/css/app.css in place
#   scripts/build_css.sh --check  build to a temporary file; exit 1 if it differs from
#                                 the committed static/css/app.css (scripts/ci.sh runs this)
#
# The CLI is one self-contained binary, so the host needs no Node. It is downloaded once
# to .tools/ (gitignored) and refused unless its sha256 matches TAILWIND_SHA256; the
# check runs on every invocation, not only after a download. Only the linux-x64 asset is
# pinned, which is what the host and the CI machine run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TAILWIND_VERSION="v3.4.19"
TAILWIND_ASSET="tailwindcss-linux-x64"
TAILWIND_SHA256="4af3198c015616ea7d6617974ec3d70d987ecc00c1ca8463b0a30fd65cc7c06e"
TAILWIND_URL="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${TAILWIND_ASSET}"
TOOLS_DIR="$REPO_ROOT/.tools"
CLI="$TOOLS_DIR/tailwindcss-${TAILWIND_VERSION}-linux-x64"
OUT="static/css/app.css"

mode="build"
case "${1:-}" in
  "") ;;
  --check) mode="check" ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
  echo "ERROR: only the ${TAILWIND_ASSET} asset is pinned; this is $(uname -s)-$(uname -m)." >&2
  exit 1
fi

cleanup_files=()
cleanup() { rm -f "${cleanup_files[@]}"; }
trap cleanup EXIT

verify() { printf '%s  %s\n' "$TAILWIND_SHA256" "$1" | sha256sum --check --status; }

if [ ! -f "$CLI" ] || ! verify "$CLI"; then
  mkdir -p "$TOOLS_DIR"
  download="$(mktemp "$TOOLS_DIR/.download.XXXXXX")"
  cleanup_files+=("$download")
  echo "    downloading Tailwind ${TAILWIND_VERSION} (${TAILWIND_ASSET})"
  curl -fsSL --retry 3 -o "$download" "$TAILWIND_URL"
  if ! verify "$download"; then
    echo "ERROR: ${TAILWIND_ASSET} ${TAILWIND_VERSION} does not match TAILWIND_SHA256; refusing to run it." >&2
    exit 1
  fi
  chmod +x "$download"
  mv "$download" "$CLI"
fi

build_to() {
  local target="$1" log
  log="$(mktemp)"
  cleanup_files+=("$log")
  if ! "$CLI" --config tailwind.config.js --input static/css/input.css \
      --output "$target" --minify >"$log" 2>&1; then
    cat "$log" >&2
    echo "ERROR: the Tailwind build failed." >&2
    exit 1
  fi
}

if [ "$mode" = "build" ]; then
  build_to "$OUT"
  echo "    wrote $OUT"
  exit 0
fi

fresh="$(mktemp)"
cleanup_files+=("$fresh")
build_to "$fresh"
if ! cmp -s "$fresh" "$OUT"; then
  echo "ERROR: $OUT is stale: a template, script or src/ string changed the set of" >&2
  echo "utility classes. Run scripts/build_css.sh and commit $OUT with that change." >&2
  exit 1
fi
echo "    $OUT matches a fresh build"
```

`bash -n scripts/build_css.sh`. Expected: no output.

- [ ] **Step 6: Ignore `.tools/`**

Append to `.gitignore`:

```
# Pinned build tools fetched by scripts/build_css.sh (the Tailwind standalone CLI).
.tools/
```

Append `.tools` as the last line of `.dockerignore`. It is untracked, so
`test_dockerignore_excludes_only_untracked_paths` holds.

- [ ] **Step 7: Build the stylesheet and check it is reproducible**

```bash
bash scripts/build_css.sh
bash scripts/build_css.sh --check
```

Expected: `wrote static/css/app.css`, then `static/css/app.css matches a fresh build`. The file
starts `/*! tailwindcss v3.4.19`. If the download or the checksum fails, stop and report. Do not
change `TAILWIND_SHA256`.

- [ ] **Step 8: Switch `templates/base.html` to the compiled stylesheet**

Replace lines 7-9:

```html
    <!-- Tailwind Play CDN: the JIT build is unversioned and cannot carry an SRI
         hash. Vendoring a compiled build (SEC-18) is deferred. -->
    <script src="https://cdn.tailwindcss.com"></script>
```

with

```html
    <!-- Compiled Tailwind v3.4.19 (scripts/build_css.sh; spec §6.1). -->
    <link rel="stylesheet" href="/static/css/app.css">
```

Then rebuild, because `base.html` is part of the content scan:
`bash scripts/build_css.sh && bash scripts/build_css.sh --check`.

- [ ] **Step 9: Add the drift step to `scripts/ci.sh`**

In the header's step list (lines 7-22), insert after the `#   1. Alembic sanity …` item, which
ends `See specs/cohort-system-v2.md §14.`:

```
#   1b. Compiled-CSS drift: static/css/app.css must equal a fresh build by the pinned
#      Tailwind standalone CLI (scripts/build_css.sh --check). No Node needed.
```

Insert between `echo "    single head: $(printf '%s\n' "$heads_out" | tr -d '\n')"` (line 128)
and the blank line before `# Round trip against a THROWAWAY database.` (line 130):

```bash

echo "==> compiled CSS drift (static/css/app.css vs a fresh Tailwind build)"
# The stylesheet is generated (spec §6.1). scripts/build_css.sh runs the pinned Tailwind
# standalone CLI, a single self-contained binary (the host has no Node), fetched once to
# .tools/ and refused unless its sha256 matches. Cheap and offline after the first run,
# so it runs before the database round trip. A template, script or src/ string that adds
# or drops a utility class changes the build: commit the rebuilt file with that change.
bash "$REPO_ROOT/scripts/build_css.sh" --check
```

- [ ] **Step 10: Update the documentation that describes Tailwind or the gate**

- `AGENT.md:49`: `| Styling | Tailwind CSS (CDN) |` → `| Styling | Tailwind CSS v3.4.19, compiled (`scripts/build_css.sh`) |`.
- `AGENT.md`: after the `### 2026-03-20: Tailwind via CDN` entry (ends line 102), insert:

```

### 2026-10-01: Compiled Tailwind replaces the CDN
**Decision:** `static/css/app.css` is built by the pinned Tailwind v3.4.19 standalone CLI (`scripts/build_css.sh`), committed, and drift-checked in `scripts/ci.sh`.
**Reason:** The Play CDN is unversioned, cannot carry SRI and is documented as development-only (web UI audit A-03; spec `docs/specs/2026-10-01-web-ui-remediation-design.md` §6.1). The standalone CLI needs no Node.
```

- `specs/tech-stack.md:11`: `- **Styling:** Tailwind CSS (via CDN)` → `- **Styling:** Tailwind CSS v3.4.19, compiled to `static/css/app.css` by `scripts/build_css.sh``.
- `docs/operations/testing.md`, first paragraph (lines 7-17): replace
  `` Run `./scripts/ci.sh` before committing — alembic sanity (single head, no `` /
  `duplicate revision ids), an upgrade→downgrade→upgrade round trip against a` with
  `` Run `./scripts/ci.sh` before committing — alembic sanity (single head, no `` /
  `` duplicate revision ids), the compiled-CSS drift check (`scripts/build_css.sh --check`: `` /
  `` `static/css/app.css` must equal a fresh build by the pinned Tailwind CLI; rebuild and `` /
  `commit it with any change that adds or drops a utility class), an`
  `upgrade→downgrade→upgrade round trip against a`.
- `CLAUDE.md:24-25`: replace
  `` - `./scripts/ci.sh` is the whole gate; there is no server-side CI. It runs alembic `` /
  `  sanity (one head, no duplicate revision ids), an upgrade→downgrade→upgrade round trip` with
  `` - `./scripts/ci.sh` is the whole gate; there is no server-side CI. It runs alembic `` /
  `` sanity (one head, no duplicate revision ids), the compiled-CSS drift check `` /
  `` (`scripts/build_css.sh --check`), an upgrade→downgrade→upgrade round trip``.
  The file stays under `MAX_CLAUDE_MD_LINES` (182 + 1).

- [ ] **Step 11: Add the parity journey (append to `tests/e2e/ui_audit/journeys_phase1.py`)**

If the module does not yet import `base64`, `json`, `re`, `tempfile` and `from pathlib import Path`, add them to its top import block (never below code: ruff E402).

```python
# --- §6.1: compiled CSS against the Play CDN, pixel by pixel -----------------------

PARITY_PAGES: tuple[tuple[str, str], ...] = (
    ("admin", "/admin/users"),
    ("admin", "/admin/jobs"),
    ("admin", "/admin/activity"),
    ("admin", "/admin/discussions"),
    ("admin", "/admin/agents"),
    ("admin", "/admin/assessments"),
    ("admin", "/admin/assessments/{a0}"),
    ("admin", "/admin/cohorts"),
    ("admin", "/admin/cohorts/topology"),
    ("manager", "/manager/pis"),
    ("manager", "/manager/assessments/{a0}"),
    ("pi", "/profile"),
    ("pi", "/settings"),
)
PARITY_DIR = Path(tempfile.gettempdir()) / "ui_audit_parity"
#: Reviewed pixel differences (spec §6.1: "every pixel difference is reviewed before
#: merge"): committed JSON, slug -> {"diff_pixels": int, "reason": str}. Absent = none.
PARITY_REVIEWED = Path(__file__).with_name("css_parity_reviewed.json")

_PARITY_CDN_JS = (
    "(() => {\n"
    "  const add = () => {\n"
    "    const s = document.createElement('script');\n"
    "    s.src = 'https://cdn.tailwindcss.com';\n"
    "    document.head.appendChild(s);\n"
    "  };\n"
    "  if (document.head) { add(); } else { document.addEventListener('DOMContentLoaded', add, {once: true}); }\n"
    "})();\n"
)

_PARITY_DIFF_JS = """async ([a, b]) => {
  const load = (data) => new Promise((resolve, reject) => {
    const img = new Image();
    img.addEventListener("load", () => resolve(img));
    img.addEventListener("error", reject);
    img.src = "data:image/png;base64," + data;
  });
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  if (ia.width !== ib.width || ia.height !== ib.height) {
    return {size: [ia.width, ia.height, ib.width, ib.height], diff: null};
  }
  const canvas = document.createElement("canvas");
  canvas.width = ia.width;
  canvas.height = ia.height;
  const g = canvas.getContext("2d");
  g.drawImage(ia, 0, 0);
  const da = g.getImageData(0, 0, canvas.width, canvas.height).data;
  g.clearRect(0, 0, canvas.width, canvas.height);
  g.drawImage(ib, 0, 0);
  const db = g.getImageData(0, 0, canvas.width, canvas.height).data;
  let diff = 0;
  for (let i = 0; i < da.length; i += 4) {
    if (da[i] !== db[i] || da[i + 1] !== db[i + 1] || da[i + 2] !== db[i + 2] || da[i + 3] !== db[i + 3]) diff += 1;
  }
  return {size: [ia.width, ia.height], diff: diff};
}"""


async def _parity_blank_css(route) -> None:
    await route.fulfill(status=200, content_type="text/css", body="")


async def _parity_shot(h, role: str, url: str, *, cdn: bool) -> bytes:
    """A full-page screenshot with the compiled stylesheet, or with it blanked and
    the Play CDN (what production served before §6.1) injected instead. CSP is
    bypassed for BOTH shots: the comparison is about CSS, and the injected CDN script
    must load after Phase 2 enforces script-src."""
    context, page, _log = await h.page(role, bypass_csp=True)
    try:
        if cdn:
            await context.route("**/static/css/app.css", _parity_blank_css)
            await context.add_init_script(_PARITY_CDN_JS)
        await page.goto(url, wait_until="networkidle")
        if cdn:
            await page.wait_for_function("() => window.tailwind !== undefined", timeout=15000)
        await page.wait_for_timeout(500)
        return await page.screenshot(full_page=True, animations="disabled")
    finally:
        await context.close()


async def journey_css_parity(h) -> dict:
    """§6.1: the compiled CSS against the CDN on a fixed page set. Every page with a
    non-zero diff (or a size change) is reviewed from its PNG pair before merge:
    fix the config or safelist, or record why the difference is acceptable in
    PARITY_REVIEWED (slug -> {"diff_pixels": n, "reason": "..."}). The journey passes
    when every page either matches or has a reviewed entry with the same diff count."""
    PARITY_DIR.mkdir(parents=True, exist_ok=True)
    pages = []
    diff_ctx, diff_page, _log = await h.page("admin")
    try:
        await diff_page.goto("about:blank")
        for role, template in PARITY_PAGES:
            path = _seed_path(template, h.ids)
            url = h.base_url + path
            compiled = await _parity_shot(h, role, url, cdn=False)
            cdn = await _parity_shot(h, role, url, cdn=True)
            slug = re.sub(r"[^a-z0-9]+", "_", f"{role}{path}".lower()).strip("_")
            compiled_png = PARITY_DIR / f"{slug}.compiled.png"
            cdn_png = PARITY_DIR / f"{slug}.cdn.png"
            compiled_png.write_bytes(compiled)
            cdn_png.write_bytes(cdn)
            result = await diff_page.evaluate(
                _PARITY_DIFF_JS,
                [base64.b64encode(compiled).decode(), base64.b64encode(cdn).decode()],
            )
            pages.append({
                "role": role,
                "path": path,
                "size": result["size"],
                "diff_pixels": result["diff"],
                "compiled_png": str(compiled_png),
                "cdn_png": str(cdn_png),
            })
    finally:
        await diff_ctx.close()
    reviewed = json.loads(PARITY_REVIEWED.read_text()) if PARITY_REVIEWED.exists() else {}
    unreviewed = []
    for p in pages:
        slug = Path(p["compiled_png"]).name.removesuffix(".compiled.png")
        entry = reviewed.get(slug)
        if p["diff_pixels"] and not (entry and entry.get("diff_pixels") == p["diff_pixels"]
                                     and entry.get("reason")):
            unreviewed.append(slug)
    return {"ok": not unreviewed, "unreviewed": unreviewed, "dir": str(PARITY_DIR),
            "pages": pages}


JOURNEYS += [journey_css_parity]
```

`.venv-test/bin/python -m ruff check tests/e2e`. Expected: no findings.

- [ ] **Step 12: Run the tests and see them pass**

```bash
bash scripts/build_css.sh --check
.venv-test/bin/python -m pytest tests/unit/test_compiled_css.py tests/unit/test_csp_templates.py tests/unit/test_ci_script.py tests/unit/test_docker_build_context.py tests/unit/test_repo_hygiene.py tests/unit/test_claude_md_references.py tests/integration/test_static_cache_headers.py -v
```

Expected: the check prints `matches a fresh build`, and every test passes.

- [ ] **Step 13: Commit**

```bash
git add tailwind.config.js static/css/input.css static/css/app.css scripts/build_css.sh scripts/ci.sh \
  .gitignore .dockerignore templates/base.html tests/unit/test_compiled_css.py tests/unit/test_csp_templates.py \
  tests/unit/test_ci_script.py AGENT.md specs/tech-stack.md docs/operations/testing.md CLAUDE.md \
  tests/e2e/ui_audit/journeys_phase1.py
git commit -m "feat(webui-1A): compiled Tailwind v3.4.19 replaces the Play CDN; ci.sh drift check (A-03, D4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Self-review

**Spec coverage**

| spec bullet | task |
|---|---|
| §6.1 `scripts/build_css.sh` downloads the pinned CLI to `.tools/` (gitignored) and verifies sha256 | 1A-9 Steps 5-6 |
| §6.1 `tailwind.config.js` content globs; `safelist` from the inventory made in the first task | 1A-1, 1A-9 Step 3 |
| §6.1 `static/css/input.css`, committed minified `static/css/app.css` | 1A-9 Steps 4, 7 |
| §6.1 `base.html` `<link rel="stylesheet" href="/static/css/app.css">` | 1A-9 Step 8 |
| §6.1 `ci.sh` rebuilds to a temp file and fails on difference (host has no Node) | 1A-9 Steps 5, 9 |
| §6.1 parity screenshots, CDN vs compiled | 1A-9 Step 11 (`journey_css_parity`) |
| §6.2 `static/vendor/marked-12.0.2.min.js`, DOMPurify newest 3.4.x, `MANIFEST.md` (URL, version, sha384), hash unit test | 1A-5 Steps 1, 3 |
| §6.2 `_head_assets.html` loads from `/static/vendor/` | 1A-5 Step 4 |
| §6.2 (parent) the chat PURIFY config behaves the same; Phase 0 sanitizer checks on the vendored copy | 1A-5 Step 6 (`journey_chat_sanitizer_vendored`) |
| §6.3 middleware creates `request.state.csp_nonce`; every inline `<script>` carries it | 1A-7 Steps 5, 8 |
| §6.3 every inline `on…=` handler moves to `static/js/ui.js` (`data-confirm` from §5.1, `data-row-href`, `data-autosubmit`, page-specific) | 1A-6 |
| §6.3 enforced headers (CSP frame-ancestors/base-uri/object-src, XFO, nosniff, Referrer-Policy) | 1A-7 Step 5 |
| §6.3 report-only policy (Phase 1), Phase 2 only flips the header | 1A-7 Step 5 (`SCRIPT_POLICY_ENFORCED`) |
| §6.3 `POST /api/csp-report`: no auth; exact-path Origin exemption; two content types; 16 KB cap; one structured line per report; 204 | 1A-8 |
| §6.4 graph routes, `_graph_csp`, `_render_graph`, payload/component/institution helpers and constants; `cabo_graph.html`; `graph` profile; graph tests; nginx graph block | 1A-2 |
| §6.4 `scripts/build_cabo_sankey.py`; `plotly` | 1A-2 |
| §6.4 PostHog block, middleware, setting, its tests, redaction-test references | 1A-3 |
| §6.4 Connect Slack route, banner and `delegate_has_slack`, `_resolve_delegate_names`, `_user_slack_id_in_list`, invite-accept lookup, remove-delegate cleanup; column kept | 1A-4 |
| §9 Phase 1 journeys: CSP reports from a crawl; CSS parity | 1A-8 Step 7, 1A-9 Step 11 |
| §9 deterministic: no inline handlers, `ui.js` loaded, nonces on every inline script, headers present, vendored hashes, CSS drift | 1A-6 Step 1, 1A-7 Steps 1-3, 1A-5 Step 1, 1A-9 Steps 1, 9 |

**Placeholder scan.** One deliberate implementation-time value remains: `PURIFY_VERSION` in
1A-5 Step 4. The step that fetches it determines it, and the tests accept any `3.4.N`. No
TBD, TODO, "handle edge cases" or "same as Task" appears.

**Interface names.** These names are the same everywhere they appear:
- in 1A-7 and 1A-8: `CSP_REPORT_PATH`, `ENFORCED_POLICY`, `REPORT_ONLY_POLICY`,
  `SCRIPT_POLICY_ENFORCED`, `new_nonce`, `security_header_items`,
  `SecurityHeadersMiddleware`, `request.state.csp_nonce`;
- in the brief contract: `nonce="{{ request.state.csp_nonce }}"`;
- in 1A-6 and its tests: `data-row-href`, `data-autosubmit`, `data-filter-nav`,
  `data-filter-param`, `data-toggle-target`;
- across 1A-5, 1A-8 and 1A-9: `_seed_path`, used by `journey_csp_report_only` and
  `journey_css_parity`. Each journey registers itself through `JOURNEYS += [...]`.
- `tests/unit/test_phase1_removals.py`'s `ROOT` and `_routes` are defined in 1A-2 and
  used in 1A-3 and 1A-4.

---

## Part 1B: Engine poller identity gate, email verification, session hardening

**Scope:** spec §6.5 (A-02b), §6.6 (A-04, A-09, D-06; decision D7, owner override D8), §6.7
(A-05, A-11; decision D6), and the Phase 1 harness journeys of §9 for login / logout /
second-device sign-out, impersonate and stop, invite accept verified and unverified.
Task prefix `1B-`.

### Global constraints (this part)

- Freeze (D2): "The 2026-09-29 freeze (B22/B24 ...) holds, **lifted for A-02b only**: the
  engine poller accepts bot messages only from known agent identities." Task 1B-2 is the
  only engine change; nothing under `prompts/`, no thread guidance, no verdict path.
- Poller (§6.5): "build `{bot_id: agent_id}` and `{bot_user_id: agent_id}` from the
  connected clients each poll; accept a message only if its `bot_id` or `user` is in either
  map; take `sender_agent_id` from the map and `sender_name` from the registry's
  `bot_name`; skip an unknown sender, advance the cursor, log one INFO line with channel
  and ts and no content."
- Migration `0057` (§6.6): "`users.email_verified_at timestamptz NULL`; backfill
  `email_verified_at = now()` where `email IS NOT NULL` (D8); `users.session_epoch
  integer NULL` (§6.7). Downgrade drops both columns." D8 is an explicit owner override of
  the rule "NULL means never asked and is not backfilled"; the migration says so.
- Unverified-invite text, verbatim: "An administrator must verify your email address
  before you can accept this invitation."
- Cookie (§6.7): "`__Host-copi-session` when `https_only` is on; `copi-session` when
  `allow_http_sessions` is set." Session keys (brief contract): `session["user_id"]`,
  `session["epoch"]`, `session["impersonate_user_id"]`.
- "Logout bumps the epoch by reading `session["user_id"]` directly (it keeps no auth
  dependency)." `/access-pending` and `POST /logout` must stay free of `get_current_user`
  (comment in `src/dependencies.py` access block; `tests/integration/test_access_revocation.py`).
- Tests run with `allow_http_sessions = False`: the suite reads `.env`
  (`src/config.py:103`), the host's `.env` sets `ALLOW_HTTP_SESSIONS=false` (line 37), and
  the field defaults to `False` (`src/config.py:143`). **Tests therefore use
  `__Host-copi-session`**, always obtained through `tests.session_support.session_cookie_name()`,
  never spelled. The browser harness sets `ALLOW_HTTP_SESSIONS="true"` (Phase 0 `env.py`),
  so it uses `copi-session`.
- New handlers take module-level dependency singletons (`_DB`, `_ADMIN` in
  `src/routers/admin/_common.py`, `_DB`, `_STAFF` in `src/routers/manager.py`, a new `_DB`
  in `src/routers/auth.py`); no new `Depends(...)` in an argument default.
- Deploy (spec §4): migrate `0057` from a one-off container off the new image (rehearse
  without `--apply`), then web + worker, then the agent image only when `/admin/simulation`
  shows no live run. Everyone is signed out once (cookie rename). The agent image MUST be
  rebuilt: 1B-2 changes `src/agent/`.
- Mutation-harness anchors that this part's edits must keep exactly once in the tree
  (`tests/unit/test_mutation_harness_targets.py`): in `src/dependencies.py` the lines
  `    # Impersonation: admin can view as another user` and
  `    if impersonate_id and session_user.is_admin:`; in `src/agent/engine/slack_io.py`
  `                visibility=visibility,\n                slack_ts=slack_ts,`,
  `        return root.slack_ts`, `        visibility = self._resolve_channel_visibility(channel)`;
  in `src/agent/slack_client.py` `            self._client = None` and
  `        last_exc: SlackApiError | None = None`. No task below adds another copy.

### Review focus (this part)

1. **Malformed or missing `epoch` in a signed session.** A missing key must read as 0 (every
   forged test session and the harness rely on it); a `bool`, string, float or `null`
   must be refused, not coerced. Pinned by `test_a_session_with_no_epoch_key_counts_as_epoch_zero`
   and `test_a_malformed_epoch_is_refused` (Task 1B-5).
2. **Replaying a revoked cookie at `POST /logout`.** A stale session must not bump the epoch
   and so sign the account out of its newer sessions. Pinned by
   `test_a_revoked_session_cannot_sign_out_the_newer_ones` (Task 1B-5).
3. **Address changes that are not changes.** A case-only rewrite (profile save lowercases
   an ORCID-cased address) keeps verification; a refused assignment (address held by
   another account) leaves address and stamp untouched. Pinned by
   `test_a_case_only_change_keeps_the_verification` and
   `test_a_refused_change_keeps_address_and_verification` (Task 1B-6).
4. **Identity edge cases in the poller.** A disconnected client's ids are not trusted; a
   message identified only by `user` (`subtype: bot_message`, no `bot_id`) from our bot
   is accepted; the human, run-marker and dedup paths are unchanged. Pinned by
   `test_a_disconnected_clients_identity_is_not_trusted`,
   `test_identity_by_user_id_alone_is_accepted`, `test_the_other_paths_are_unchanged`
   (Task 1B-2).
5. **Bounce termination.** A stale session at `/login` must end on the login page (no
   `/login` -> `/` -> `/profile` loop), and a lapsed impersonation must be dropped from the
   session. Pinned by `test_a_stale_session_at_login_ends_on_the_login_page` (1B-5) and
   `test_a_lapsed_impersonation_is_dropped` (1B-4).

### File map

| File | Change | Responsibility |
|---|---|---|
| `alembic/versions/0057_email_verification_and_session_epoch.py` | Create | Adds `users.email_verified_at`, `users.session_epoch`; D8 backfill; downgrade |
| `src/models/user.py` | Modify | Maps the two columns |
| `scripts/migrate/preflight.py` | Modify | Head moves to `0057`; `0056` becomes a supported start; plans the two columns |
| `tests/unit/test_migration_checks.py` | Modify | Pins move to `0057`; `0057` sizes only `users` |
| `tests/integration/test_harness_smoke.py` | Modify | Head pin `0057` |
| `tests/integration/test_migration_0057.py` | Create | Column shapes, backfill, downgrade |
| `docs/operations/migration-deploy-notes.md` | Modify | "Deploy order for `0057_...`" box |
| `src/agent/transport.py` | Modify | `Transport.bot_id` / `bot_user_id`; `NullTransport` returns None |
| `src/agent/slack_client.py` | Modify | `AgentSlackClient.bot_id` / `bot_user_id` properties |
| `src/agent/engine/slack_io.py` | Modify | Identity maps; poller drops unknown senders; attribution by identity |
| `tests/fakes.py` | Modify | `FakeSlackClient` has a bot id and the two properties |
| `tests/unit/test_poller_identity_gate.py` | Create | A-02b behaviour |
| `tests/unit/test_run_marker_ingest_skip.py` | Modify | Its "ordinary bot post" is now one of ours |
| `tests/integration/test_message_persistence.py` | Modify | Its polled message is now one of ours |
| `tests/unit/test_engine_import_graph.py` | Modify | Registers the two new `SlackIO` methods |
| `src/main.py` | Modify | `SESSION_COOKIE_HTTP`, `SESSION_COOKIE_HTTPS`, `session_cookie_name()`; middleware uses it |
| `tests/session_support.py` | Create | Forge/read the session cookie under the app's name |
| `tests/integration/test_session_cookie.py` | Create | Cookie name and `__Host-` flags by setting |
| 17 test files forging `copi-session` (listed in 1B-3) | Modify | Name from `session_cookie_name()` |
| `tests/e2e/session.py` | Modify | `COOKIE_NAME` is `SESSION_COOKIE_HTTP` |
| `tests/e2e/ui_audit/run.py` | Modify | Harness cookie name from `session_cookie_name(get_settings())` |
| `src/dependencies.py` | Modify | Impersonation from the signed session (24 h bound); epoch check |
| `src/routers/admin/impersonation.py` | Modify | Start/stop write the session; no cookie |
| `src/routers/auth.py` | Modify | Login resets the session and stores the epoch; logout bumps it |
| `src/routers/profile.py` | Modify | Delete-account stops deleting the legacy cookie |
| `src/services/session_epoch.py` | Create | `SESSION_EPOCH_KEY`, `current_epoch`, `epoch_in_session`, `bump_session_epoch` |
| `src/routers/admin/access.py` | Modify | Deny bumps the epoch |
| `src/routers/admin/users.py` | Modify | Role change bumps the epoch; `POST /users/{id}/verify-email` |
| `src/cli.py` | Modify | `admin:grant`, `admin:revoke`, `role:set` bump the epoch on a change |
| 10 test files appending `copi-impersonate` (listed in 1B-4) | Modify | `auth_headers(..., impersonate=...)` |
| `tests/integration/test_manager_access.py` | Modify | `auth_headers(user_id, *, impersonate=None)` |
| `tests/integration/test_manager_industry_panel.py`, `test_onboarding_flow.py`, `test_account_deletion_routes.py` | Modify | Impersonation signed into the session |
| `tests/characterization/test_auth_and_admin_routes.py` | Modify | Cookie-flag pin replaced by "no cookie of its own" |
| `tests/integration/test_impersonation_session.py` | Create | Impersonation in the session |
| `tests/integration/test_session_epoch.py` | Create | Login reset, epoch, logout, deny, role |
| `tests/integration/test_cli.py` | Modify | CLI role commands bump the epoch |
| `scripts/mutate_system.sh` | Modify | M8 description names the session key |
| `src/services/user_email.py` | Modify | Clears `email_verified_at` on an address change |
| `tests/integration/test_user_email.py` | Modify | Clearing at the writer and at the three forms |
| `src/services/email_verification.py` | Create | `mark_email_verified` + audit event |
| `src/routers/manager.py` | Modify | `POST /pis/{id}/verify-email` |
| `templates/admin/user_detail.html`, `templates/manager/pi_detail.html` | Modify | Verified status and "Mark verified" control |
| `tests/integration/test_manager_views.py` | Modify | Allowlist gains the verify route (and 1B-4's impersonation rewrite) |
| `tests/integration/test_email_verification.py` | Create | Verify routes and controls |
| `src/routers/invite.py` | Modify | `get_current_user`, PI-only, verified address |
| `tests/integration/test_agent_page.py`, `tests/integration/test_double_submits.py` | Modify | Their delegates are verified |
| `tests/unit/test_invite_email_binding.py` | Modify | `_invite_refusal` order |
| `tests/integration/test_invite_verification.py` | Create | Invite acceptance requirements |
| `docs/operations/pis-and-access.md` | Modify | Sessions, impersonation, verification |
| `tests/e2e/ui_audit/journeys_phase1_1b.py` | Create | Three Phase 1 journeys, `JOURNEYS_1B` |
| `tests/e2e/ui_audit/journeys_phase1.py` | Modify (or Create) | Registers `JOURNEYS_1B` |

**Cross-part file overlaps the parent must sequence or merge** (each 1B hunk is small and
named in its task): `src/main.py` (1A adds `SecurityHeadersMiddleware`, 1C
`install_error_handlers`, §6.4 removes `PostHogContextMiddleware`); `src/routers/invite.py`
and `tests/integration/test_agent_page.py` (§6.4 removes the invite-accept Slack lookup,
`src/routers/invite.py:221-249`, and the connect-slack tests; 1B touches lines 20-37, 83-108,
159-166, 175-187 only); `templates/admin/user_detail.html`, `templates/manager/pi_detail.html`,
`src/routers/admin/users.py`, `src/routers/manager.py` (any 1A inline-handler or 1C §6.8
edit); `tests/e2e/ui_audit/journeys_phase1.py` (every Phase 1 part registers journeys). The
1B template additions use only classes already present in `templates/`
(`text-amber-700`, `text-green-700`, `text-red-700`, `ml-2`, `mt-1`, `text-xs`,
`text-indigo-600`, `hover:underline`), so 1A's compiled CSS needs no new class; re-run
1A's CSS build check after merge anyway.

---

### Task 1B-1: Migration `0057`, model columns, preflight and the deploy box

**Files:**
- Create: `alembic/versions/0057_email_verification_and_session_epoch.py`
- Modify: `src/models/user.py:6` (import), `:32-37` (after `contact_email_unverified`), `:56-59` (after `access_status`)
- Modify: `scripts/migrate/preflight.py:78`, `:112`, `:113-118`, `:437-446`, `:448-450`, `:455-460`
- Modify: `tests/unit/test_migration_checks.py:276-282`, end of file
- Modify: `tests/integration/test_harness_smoke.py:82-83`
- Modify: `docs/operations/migration-deploy-notes.md` (append after line 933)
- Test: `tests/integration/test_migration_0057.py`

**Interfaces:**
- Consumes: alembic revision `0056` (head today).
- Produces: revision `0057` (`down_revision = "0056"`); `User.email_verified_at: datetime | None`,
  `User.session_epoch: int | None`; `pf.DEFAULT_TARGET == "0057"`.

- [ ] **Step 1: Write the failing migration test**

Create `tests/integration/test_migration_0057.py`:

```python
"""0057: both users columns exist after upgrade, the D8 backfill stamps exactly the users
that have an email, and the downgrade drops both. The testcontainers database is migrated
to head by tests/conftest.py; each step here runs on the test's own connection, so the
outer transaction rolls every DDL statement back."""
import importlib.util
import sys
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests import factories

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/0057_email_verification_and_session_epoch.py"
)
_COLUMNS = {"email_verified_at", "session_epoch"}


def _load_migration():
    spec = importlib.util.spec_from_file_location("_m0057", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run_migration_step(db_session, name):
    mod = _load_migration()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _user_columns(db_session) -> set[str]:
    return set((await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users'"
    ))).scalars())


async def test_head_has_both_nullable_columns_without_defaults(db_session):
    rows = (await db_session.execute(text(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'users' AND column_name IN ('email_verified_at', 'session_epoch')"
    ))).all()
    assert sorted(tuple(r) for r in rows) == [
        ("email_verified_at", "timestamp with time zone", "YES", None),
        ("session_epoch", "integer", "YES", None),
    ]


async def test_downgrade_drops_and_upgrade_backfills_only_users_with_an_email(db_session):
    with_email = await factories.make_user(db_session, email="d8@example.edu")
    without_email = await factories.make_user(db_session, email=None)
    await db_session.flush()

    await _run_migration_step(db_session, "downgrade")
    assert not (await _user_columns(db_session)) & _COLUMNS

    await _run_migration_step(db_session, "upgrade")
    assert _COLUMNS <= await _user_columns(db_session)
    stamped = dict((await db_session.execute(text(
        "SELECT id, email_verified_at IS NOT NULL FROM users WHERE id IN (:a, :b)"
    ), {"a": with_email.id, "b": without_email.id})).all())
    assert stamped == {with_email.id: True, without_email.id: False}
    bumped = (await db_session.execute(text(
        "SELECT count(*) FROM users WHERE session_epoch IS NOT NULL"
    ))).scalar_one()
    assert bumped == 0, "session_epoch is never backfilled"
```

- [ ] **Step 2: Run it; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_migration_0057.py -v`
Expected: both tests FAIL — the first with `[]` != the two rows (columns absent at head
`0056`); the second with `FileNotFoundError` on the migration path.

- [ ] **Step 3: Create the migration**

Create `alembic/versions/0057_email_verification_and_session_epoch.py`:

```python
"""Email verification and server-side session revocation (docs/specs/2026-10-01-web-ui-remediation-design.md §6.6, §6.7)

Two additive nullable columns on users:

- users.email_verified_at: when an administrator (any user) or a manager (PIs)
  verified the address in users.email. Cleared by src/services/user_email.py
  whenever the address changes; delegate-invitation acceptance requires it.
- users.session_epoch: bumped by logout, access denial and a role change; a
  session whose stored epoch differs is refused (src/dependencies.py). NULL
  counts as 0. Never backfilled.

BACKFILL, BY OWNER DECISION D8 — an explicit override of this repo's rule that
NULL means "never asked" and is not backfilled: every user that has an email
when this runs is marked verified (email_verified_at = now()).

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads either column (an address
changed through old code after this runs keeps its stamp, so keep the window
between --apply and the new web image short). NEW CODE ON THE OLD SCHEMA is not:
User maps both columns, so every select of User raises UndefinedColumn. Migrate
BEFORE the new code serves, with the WORKER IDLE: ALTER TABLE users queues behind
any open transaction that has read users, and the worker holds its transaction
across a whole pipeline run, past the chain's 10 s lock_timeout.

Downgrade drops both columns, and with them every verification stamp.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-01
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("session_epoch", sa.Integer(), nullable=True))
    # D8 (owner override, see the module docstring).
    op.execute("UPDATE users SET email_verified_at = now() WHERE email IS NOT NULL")


def downgrade() -> None:
    op.drop_column("users", "session_epoch")
    op.drop_column("users", "email_verified_at")
```

- [ ] **Step 4: Map the columns on `User`**

In `src/models/user.py` line 6, before:
```python
from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, func, text
```
after:
```python
from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func, text
```

Before (lines 37-38):
```python
    contact_email_unverified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
```
after:
```python
    contact_email_unverified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When an administrator (any user) or a manager (PIs) verified `email`
    #: (migration 0057, spec 2026-10-01 §6.6). Cleared by
    #: src/services/user_email.py whenever the address changes; delegate-invitation
    #: acceptance requires it. Users with an email at migration time were stamped
    #: by owner decision D8.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
```

Before (lines 56-59):
```python
    access_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # allowed, pending, denied
```
after:
```python
    access_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # allowed, pending, denied
    #: Server-side session revocation (migration 0057, spec 2026-10-01 §6.7). Login
    #: copies it into session["epoch"]; get_current_user refuses a session whose
    #: epoch differs. Bumped by logout, access denial and a role change
    #: (src/services/session_epoch.py). NULL counts as 0.
    session_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 5: Run the migration test; expect pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_migration_0057.py tests/integration/test_model_migration_parity.py -v`
Expected: PASS (the conftest migrates the fresh testcontainers database to the new head).

- [ ] **Step 6: Move preflight's head, with its failing pins first**

In `tests/unit/test_migration_checks.py`, before (lines 280-282):
```python
        "0049", "0050", "0051", "0052", "0053", "0054", "0055",
    )
    assert pf.DEFAULT_TARGET == "0056"
```
after:
```python
        "0049", "0050", "0051", "0052", "0053", "0054", "0055", "0056",
    )
    assert pf.DEFAULT_TARGET == "0057"
```
Append to the end of the same file:
```python


# --------------------------------------------------------------------------- #
# 0057: two nullable users columns
# --------------------------------------------------------------------------- #


def test_0057_sizes_only_users_and_takes_no_agent_messages_lock():
    assert pf.tables_sized_between("0056", "0057") == ["users"]
    assert pf.agent_messages_ddl_pending("0056", "0057") is False
```
In `tests/integration/test_harness_smoke.py`, before (lines 82-83):
```python
        # 0056 jobs.priority, the one-active-job index, four uniques, two indexes, rubric_documents
        assert v == "0056"
```
after:
```python
        # 0056 jobs.priority, the one-active-job index, four uniques, two indexes, rubric_documents
        # 0057 users.email_verified_at / users.session_epoch (web UI remediation §6.6-§6.7)
        assert v == "0057"
```
Run: `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py -k "supported_start_revisions_are_exactly or 0057_sizes" -v`
Expected: FAIL — `DEFAULT_TARGET` is `"0056"` and `tables_sized_between("0056", "0057")` is `[]`.

- [ ] **Step 7: Edit `scripts/migrate/preflight.py`**

Line 78, before `DEFAULT_TARGET = "0056"`, after `DEFAULT_TARGET = "0057"`.

Line 112, before:
```python
#: tables, no backfill), and the 0036-0056 objects enumerated in PLANNED_OBJECTS below.
```
after:
```python
#: tables, no backfill), and the 0036-0057 objects enumerated in PLANNED_OBJECTS below.
```
Lines 117-118, before:
```python
    "0051", "0052", "0053", "0054", "0055",
)
```
after:
```python
    "0051", "0052", "0053", "0054", "0055", "0056",
)
```
Lines 445-446, before:
```python
    PlannedObject("0056", "table", "rubric_documents"),
)
```
after:
```python
    PlannedObject("0056", "table", "rubric_documents"),
    # 0057_email_verification_and_session_epoch
    PlannedObject("0057", "column", "email_verified_at", "users"),
    PlannedObject("0057", "column", "session_epoch", "users"),
)
```
Lines 449-450, before:
```python
#: must never treat a drop's precondition (the object exists) as a collision. 0026 is
#: the only upgrade-time ``drop_table`` in 0019-0056.
```
after:
```python
#: must never treat a drop's precondition (the object exists) as a collision. 0026 is
#: the only upgrade-time ``drop_table`` in 0019-0057.
```
Line 459, before:
```python
    "0051", "0052", "0053", "0054", "0055", "0056",
)
```
(the `REVISION_ORDER` tuple) after:
```python
    "0051", "0052", "0053", "0054", "0055", "0056", "0057",
)
```

- [ ] **Step 8: Run the migration-check suite; expect pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_migration_checks.py tests/integration/test_harness_smoke.py -v`
Expected: PASS.

- [ ] **Step 9: Append the deploy box**

Append to `docs/operations/migration-deploy-notes.md` (after the `0056` box, one blank line
between):

```markdown

> **Deploy order for `0057_email_verification_and_session_epoch` — migrate BEFORE the
> new code serves, with the WORKER IDLE; then web and worker; the AGENT image last and
> only with no live run. Everyone is signed out once.** `0057` adds
> `users.email_verified_at` (timestamptz, NULL) and `users.session_epoch` (integer, NULL)
> and, by owner decision D8 — an explicit override of "NULL is not backfilled" — stamps
> `email_verified_at = now()` on every user that has an `email` when it is applied.
> *Old code on the new schema* is safe: nothing old reads either column; an address
> changed through the old web image after `--apply` keeps its stamp, so bring the new web
> image up straight after. *New code on the old schema* is not: `User` maps both
> columns, so every `select(User)` raises `UndefinedColumn` — every signed-in page, the
> worker and the engine. Design: `docs/specs/2026-10-01-web-ui-remediation-design.md`
> §6.5-§6.7.
>
> **The worker must be idle** (no `processing` row) when `--apply` runs: `ALTER TABLE
> users` queues behind any open transaction that has read `users`, the worker holds its
> transaction across a whole pipeline run, and the chain's 10 s `lock_timeout` then rolls
> the chain back. Check with
> `$DC exec -T postgres psql -U copi -d copi -c "select count(*) from jobs where status='processing'"`
> (must print 0), or `$DC stop worker` for the migration.
>
> **Everyone is signed out once.** The new web image names the session cookie
> `__Host-copi-session` (production runs `ALLOW_HTTP_SESSIONS=false`) and keeps
> impersonation inside the signed session, so the old `copi-session` and
> `copi-impersonate` cookies are ignored from `up -d blackbird-app` on and expire on their
> own. **Agent image:** rebuild in the same deploy — the engine's Slack poller now
> mirrors only messages from known agent identities (A-02b, `src/agent/engine/slack_io.py`).
>
>     DC="docker compose -f docker-compose.prod.yml"
>     for s in blackbird-app worker agent; do
>       docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-webui-1
>     done
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     ./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
>     ./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0057)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # ONLY when /admin/simulation shows no live run
>
> No prompt or rubric file changes in this deploy, so no fresh-run requirement.
> Rollback: redeploy the `rollback-pre-webui-1` images; the columns are harmless to old
> code, and rolling the web image back signs everyone out again (the cookie name
> reverts). `alembic downgrade 0056` drops both columns and every verification stamp; a
> later re-upgrade re-stamps only the addresses present then.
```

Run: `.venv-test/bin/python -m pytest tests/unit/test_claude_md_references.py -v`
Expected: PASS (the box names a migration that exists).

- [ ] **Step 10: Commit**

```bash
git add alembic/versions/0057_email_verification_and_session_epoch.py src/models/user.py \
  scripts/migrate/preflight.py tests/unit/test_migration_checks.py \
  tests/integration/test_harness_smoke.py tests/integration/test_migration_0057.py \
  docs/operations/migration-deploy-notes.md
git commit -m "feat(webui-1B): migration 0057 adds users.email_verified_at and users.session_epoch

D8 backfill stamps every user that has an email; session_epoch is never
backfilled. Preflight head moves to 0057; deploy box added.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-2: Engine poller accepts only known agent identities (A-02b)

**Files:**
- Modify: `src/agent/transport.py:36-39` (Protocol), `:106-111` (NullTransport)
- Modify: `src/agent/slack_client.py:529-532` (after `is_connected`)
- Modify: `src/agent/engine/slack_io.py:90-100` (after `_next_poll_client`), `:118-123`, `:181-190`
- Modify: `tests/fakes.py:367-395`
- Modify: `tests/unit/test_run_marker_ingest_skip.py:31-32`
- Modify: `tests/integration/test_message_persistence.py:329-347`
- Modify: `tests/unit/test_engine_import_graph.py:195-197`
- Test: `tests/unit/test_poller_identity_gate.py`

**Interfaces:**
- Consumes: `AgentSlackClient._bot_id` / `_bot_user_id` set by `connect()` from `auth.test`
  (`src/agent/slack_client.py:508-510`); `SimulationEngine.agents: dict[str, Agent]`,
  `Agent.bot_name`.
- Produces: `Transport.bot_id -> str | None`, `Transport.bot_user_id -> str | None`
  (properties on `Transport`, `NullTransport`, `AgentSlackClient`, `tests.fakes.FakeSlackClient`);
  `SlackIO._bot_identity_maps(self) -> tuple[dict[str, str], dict[str, str]]`;
  `SlackIO._known_sender(self, msg: dict, by_bot_id: dict[str, str], by_user_id: dict[str, str]) -> tuple[str, str] | None`;
  `FakeSlackClient._bot_id == f"B_{agent_id}"` (its `_bot_user_id` stays `f"U_{agent_id}"`).

What does not change in the poller (each pinned by an existing test or by
`test_the_other_paths_are_unchanged` below): the poll throttle and round-robin client
choice, the channel set (`_polled_channel_ids()`, pinned by `tests/unit/test_polled_channel_ids.py`),
the run-marker skip, the human classification (`bot_id` / `subtype == "bot_message"` /
`ais_bot_user`) and its drop, the `get_entry` dedup, the Slack-mirror mapping fields, the
cursor advance, and the per-channel `except` with `_log_poll_error`. The only new branch
is the identity gate between the human drop and the append; the only changed fields are
`sender_agent_id` and `sender_name`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_poller_identity_gate.py`:

```python
"""A-02b (spec 2026-10-01 §6.5): the live Slack poller mirrors only messages from our
own agents' bot identities and attributes them by identity, never by the `username` a
message claims. Everything else the poller does is unchanged."""
import logging

import pytest

from src.agent.agent import Agent
from src.agent.run_marker import RUN_START_MARKER_PREFIX
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import AgentSlackClient
from src.agent.transport import NullTransport
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio

CH_NAME = "general"
CH_ID = "C_general"


class _Disconnected(FakeSlackClient):
    @property
    def is_connected(self) -> bool:
        return False


def _engine(monkeypatch, tmp_path, messages, *, wang_client=None):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang")
    hub_client = FakeSlackClient(agent_id="blackbird")
    lab_client = wang_client or FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab], slack_clients={"blackbird": hub_client, "wang": lab_client},
    )
    eng._channel_id_map[CH_NAME] = CH_ID
    for client in (hub_client, lab_client):
        client.channel_history[CH_ID] = list(messages)
    eng._last_channel_poll = 0.0
    return eng


def _msg(ts, **fields):
    return {"ts": ts, "text": f"text of {ts}", **fields}


async def test_an_own_agents_message_is_accepted(monkeypatch, tmp_path):
    ts = "1700000100.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, bot_id="B_wang", user="U_wang", username="WangBot"),
    ])
    await eng._poll_slack_for_bot_messages()
    entry = eng.message_log.get_entry(ts)
    assert entry is not None
    assert (entry.sender_agent_id, entry.sender_name, entry.is_bot) == ("wang", "WangBot", True)
    assert eng._poll_cursors[CH_ID] == ts


async def test_a_foreign_bot_is_dropped_with_the_cursor_advanced(monkeypatch, tmp_path, caplog):
    ts = "1700000101.000000"
    eng = _engine(monkeypatch, tmp_path, [
        {"ts": ts, "bot_id": "B_EVIL", "user": "U_EVIL", "username": "EvilBot",
         "text": "SECRET-CONTENT"},
    ])
    caplog.set_level(logging.INFO, logger="src.agent.simulation")
    await eng._poll_slack_for_bot_messages()
    assert eng.message_log.get_entry(ts) is None
    assert eng._poll_cursors[CH_ID] == ts, "the dropped message would be re-fetched every tick"
    lines = [r.getMessage() for r in caplog.records if "unknown Slack identity" in r.getMessage()]
    assert len(lines) == 1, lines
    assert f"#{CH_NAME}" in lines[0] and ts in lines[0]
    assert caplog.records and not any("SECRET-CONTENT" in r.getMessage() for r in caplog.records)
    assert next(r for r in caplog.records if "unknown Slack identity" in r.getMessage()).levelno \
        == logging.INFO


async def test_a_spoofed_username_of_a_real_agent_is_dropped(monkeypatch, tmp_path):
    ts = "1700000102.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, bot_id="B_EVIL", user="U_EVIL", username="BlackbirdBot"),
    ])
    await eng._poll_slack_for_bot_messages()
    assert eng.message_log.get_entry(ts) is None
    assert eng._poll_cursors[CH_ID] == ts


async def test_attribution_comes_from_identity_not_username(monkeypatch, tmp_path):
    ts = "1700000103.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, bot_id="B_wang", user="U_wang", username="BlackbirdBot"),
    ])
    await eng._poll_slack_for_bot_messages()
    entry = eng.message_log.get_entry(ts)
    assert (entry.sender_agent_id, entry.sender_name) == ("wang", "WangBot")


async def test_identity_by_user_id_alone_is_accepted(monkeypatch, tmp_path):
    ts = "1700000104.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, user="U_blackbird", subtype="bot_message", username="Whatever"),
    ])
    await eng._poll_slack_for_bot_messages()
    entry = eng.message_log.get_entry(ts)
    assert (entry.sender_agent_id, entry.sender_name) == ("blackbird", "BlackbirdBot")


async def test_a_disconnected_clients_identity_is_not_trusted(monkeypatch, tmp_path):
    ts = "1700000105.000000"
    eng = _engine(
        monkeypatch, tmp_path, [_msg(ts, bot_id="B_wang", user="U_wang", username="WangBot")],
        wang_client=_Disconnected(agent_id="wang"),
    )
    await eng._poll_slack_for_bot_messages()
    assert eng.message_log.get_entry(ts) is None
    assert eng._poll_cursors[CH_ID] == ts


async def test_the_other_paths_are_unchanged(monkeypatch, tmp_path):
    """Human, foreign, marker and own messages in one poll: only ours is mirrored, the
    cursor ends on the newest, and a second poll re-fetches nothing."""
    human, foreign, own, marker = (f"17000002{i:02d}.000000" for i in range(4))
    eng = _engine(monkeypatch, tmp_path, [
        _msg(human, user="UHUMAN"),
        _msg(foreign, bot_id="B_EVIL", user="U_EVIL", username="WangBot"),
        _msg(own, bot_id="B_blackbird", user="U_blackbird", username="BlackbirdBot"),
        {"ts": marker, "text": f"{RUN_START_MARKER_PREFIX}\nRun: x", "bot_id": "B_blackbird",
         "user": "U_blackbird"},
    ])
    await eng._poll_slack_for_bot_messages()
    assert [e.ts for e in eng.message_log._entries] == [own]
    assert eng._poll_cursors[CH_ID] == marker
    eng._last_channel_poll = 0.0
    await eng._poll_slack_for_bot_messages()
    assert [e.ts for e in eng.message_log._entries] == [own]


def test_transports_expose_their_identity():
    client = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    assert (client.bot_id, client.bot_user_id) == (None, None)
    client._bot_id, client._bot_user_id = "B1", "U1"
    assert (client.bot_id, client.bot_user_id) == ("B1", "U1")
    assert (NullTransport("su").bot_id, NullTransport("su").bot_user_id) == (None, None)
    fake = FakeSlackClient(agent_id="su")
    assert (fake.bot_id, fake.bot_user_id) == ("B_su", "U_su")
```

- [ ] **Step 2: Run them; expect failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_poller_identity_gate.py -v`
Expected: FAIL — foreign/spoofed/disconnected tests find an ingested entry; attribution
tests see `sender_name` from `username` (`"BlackbirdBot"`, `"Whatever"`);
`test_transports_expose_their_identity` raises `AttributeError: ... has no attribute 'bot_id'`.

- [ ] **Step 3: Add the identity to the transports**

`src/agent/transport.py`, before (lines 36-39):
```python
    # Identity / lifecycle
    def connect(self) -> bool: ...
    @property
    def is_connected(self) -> bool: ...
    def is_bot_user(self, user_id: str) -> bool: ...
```
after:
```python
    # Identity / lifecycle
    def connect(self) -> bool: ...
    @property
    def is_connected(self) -> bool: ...
    # This token's own Slack identity, learned from ``auth.test`` at connect; None
    # before a successful connect. The poller trusts a message only when one of these
    # matches a connected client (A-02b).
    @property
    def bot_id(self) -> str | None: ...
    @property
    def bot_user_id(self) -> str | None: ...
    def is_bot_user(self, user_id: str) -> bool: ...
```
`NullTransport`, before (lines 106-111):
```python
    @property
    def is_connected(self) -> bool:
        return False

    def is_bot_user(self, user_id: str) -> bool:
        return False
```
after:
```python
    @property
    def is_connected(self) -> bool:
        return False

    @property
    def bot_id(self) -> str | None:
        return None

    @property
    def bot_user_id(self) -> str | None:
        return None

    def is_bot_user(self, user_id: str) -> bool:
        return False
```
`src/agent/slack_client.py`, before (lines 529-531):
```python
    @property
    def is_connected(self) -> bool:
        return self._client is not None
```
after:
```python
    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def bot_id(self) -> str | None:
        """This token's bot id (``B…``) from ``auth.test`` at connect; None before."""
        return self._bot_id

    @property
    def bot_user_id(self) -> str | None:
        """This token's bot user id (``U…``) from ``auth.test`` at connect; None before."""
        return self._bot_user_id
```
`tests/fakes.py`, before (lines 371-372):
```python
        self._bot_user_id = f"U_{agent_id}"
        self.posted: list[dict] = []
```
after:
```python
        self._bot_user_id = f"U_{agent_id}"
        # The real client learns both ids from auth.test; the poller trusts only
        # messages carrying one of them (A-02b).
        self._bot_id = f"B_{agent_id}"
        self.posted: list[dict] = []
```
and before (lines 393-395):
```python
    @property
    def is_connected(self) -> bool:
        return True
```
after:
```python
    @property
    def is_connected(self) -> bool:
        return True

    @property
    def bot_id(self) -> str | None:
        return self._bot_id

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id
```

- [ ] **Step 4: Gate the poller**

`src/agent/engine/slack_io.py`, after `_next_poll_client` (insert after line 99,
`        return client`), add:
```python

    def _bot_identity_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """``({bot_id: agent_id}, {bot_user_id: agent_id})`` over the connected clients.

        Each client learns its own ids from ``auth.test`` at connect
        (``AgentSlackClient.connect``). Rebuilt on every poll, so a live roster change
        (``_sync_roster_from_db`` adds and drops clients) applies on the next tick. A
        client whose ``auth.test`` reported no ``bot_id`` contributes only its user id.
        """
        by_bot_id: dict[str, str] = {}
        by_user_id: dict[str, str] = {}
        for agent_id, client in self.slack_clients.items():
            if not (client and client.is_connected):
                continue
            if client.bot_id:
                by_bot_id[client.bot_id] = agent_id
            if client.bot_user_id:
                by_user_id[client.bot_user_id] = agent_id
        return by_bot_id, by_user_id

    def _known_sender(
        self, msg: dict, by_bot_id: dict[str, str], by_user_id: dict[str, str],
    ) -> tuple[str, str] | None:
        """``(agent_id, bot_name)`` when ``msg`` carries one of our agents' Slack
        identities, else None.

        The sender comes from the identity Slack stamps on the message (``bot_id``,
        ``user``), never from ``username``, which the posting app chooses (A-02b). The
        name is the registry's ``bot_name``, with ``_post_message``'s fallback for an
        agent that has a client but no loaded ``Agent``.
        """
        agent_id = by_bot_id.get(msg.get("bot_id") or "") or by_user_id.get(msg.get("user") or "")
        if agent_id is None:
            return None
        agent = self.agents.get(agent_id)
        return agent_id, (agent.bot_name if agent else f"{agent_id}Bot")
```
Before (lines 121-123):
```python
        default_client = self._next_poll_client()
        if not default_client:
            return
```
after:
```python
        default_client = self._next_poll_client()
        if not default_client:
            return
        by_bot_id, by_user_id = self._bot_identity_maps()
```
Before (lines 181-190):
```python
                    bot_name = msg.get("username", "bot")
                    # Resolve agent_id from bot name
                    bot_agent_id = self.message_log._bot_name_to_id.get(
                        bot_name.lower()
                    )
                    entry = LogEntry(
                        ts=ts,
                        channel=ch_name,
                        sender_agent_id=bot_agent_id,
                        sender_name=bot_name,
```
after:
```python
                    # Only our own agents' bot identities are mirrored (A-02b). An
                    # unknown bot is dropped like a human message: cursor advanced,
                    # nothing appended, one INFO line that carries no content.
                    sender = self._known_sender(msg, by_bot_id, by_user_id)
                    if sender is None:
                        logger.info(
                            "Dropped a bot message from an unknown Slack identity in #%s (ts %s)",
                            ch_name, ts,
                        )
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue
                    bot_agent_id, bot_name = sender
                    entry = LogEntry(
                        ts=ts,
                        channel=ch_name,
                        sender_agent_id=bot_agent_id,
                        sender_name=bot_name,
```
In the method docstring (lines 102-116), before:
```python
        exactly what the name says: mirror another bot's Slack-native post (a
        message this process did not itself write) into the shared
        ``MessageLog``, recording the Slack-mirror mapping so a reply to it
        can still be threaded. See the removal cycle's PI-interaction audit
```
after:
```python
        exactly what the name says: mirror another bot's Slack-native post (a
        message this process did not itself write) into the shared
        ``MessageLog``, recording the Slack-mirror mapping so a reply to it
        can still be threaded. Only our own agents' bot identities count
        (``_known_sender``, A-02b): any other bot's post is dropped the way a
        human one is. See the removal cycle's PI-interaction audit
```

- [ ] **Step 5: Re-point the two existing tests whose "other bot" was foreign**

`tests/unit/test_run_marker_ingest_skip.py`, before (lines 31-32):
```python
        {"ts": NORMAL_TS, "text": "an ordinary bot post",
         "bot_id": "B1", "user": "U1", "username": "OtherBot"},
```
after:
```python
        # One of our agents' bot posts (the poller mirrors nothing else, A-02b).
        {"ts": NORMAL_TS, "text": "an ordinary bot post",
         "bot_id": "B_blackbird", "user": "U_blackbird", "username": "BlackbirdBot"},
```
`tests/integration/test_message_persistence.py`, before (lines 329-338):
```python
class _HistoryClient:
    """Connected transport that returns one canned bot message from history."""

    def __init__(self, messages):
        self.agent_id = "su"
        self._messages = messages

    @property
    def is_connected(self):
        return True
```
after:
```python
class _HistoryClient:
    """Connected transport that returns one canned bot message from history.

    Its bot id is the message's, so the message counts as one of our agents'
    posts made by another process (the poller mirrors nothing else, A-02b).
    """

    def __init__(self, messages):
        self.agent_id = "su"
        self._messages = messages

    @property
    def is_connected(self):
        return True

    @property
    def bot_id(self):
        return "B0DIGEST"

    @property
    def bot_user_id(self):
        return "U0SU"
```
`tests/unit/test_engine_import_graph.py`, before (lines 196-197):
```python
    "_poll_slack_for_bot_messages": "slack_io", "_log_poll_error": "slack_io",
    "_seed_slack_cursors_without_ingest": "slack_io",
```
after:
```python
    "_poll_slack_for_bot_messages": "slack_io", "_log_poll_error": "slack_io",
    "_seed_slack_cursors_without_ingest": "slack_io",
    "_bot_identity_maps": "slack_io", "_known_sender": "slack_io",
```

- [ ] **Step 6: Run the new and the affected tests; expect pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_poller_identity_gate.py tests/unit/test_run_marker_ingest_skip.py tests/integration/test_message_persistence.py::test_polled_bot_message_keeps_its_slack_mapping tests/unit/test_transport.py tests/unit/test_engine_import_graph.py tests/unit/test_polled_channel_ids.py tests/unit/test_fresh_start_cursor_seed.py tests/unit/test_fresh_start_slack_restore.py tests/unit/test_poll_error_visibility.py tests/integration/test_concurrent_thread_safety.py::test_poller_is_bot_lookup_does_not_block_the_loop tests/unit/test_mutation_harness_targets.py -v`
Expected: PASS. (`test_transport.py::test_every_transport_member_has_an_engine_caller`
passes because `slack_io.py` reads `client.bot_id` / `client.bot_user_id`;
`test_null_transport_defines_only_protocol_members` passes because both are on the Protocol.)

- [ ] **Step 7: Commit**

```bash
git add src/agent/transport.py src/agent/slack_client.py src/agent/engine/slack_io.py \
  tests/fakes.py tests/unit/test_poller_identity_gate.py tests/unit/test_run_marker_ingest_skip.py \
  tests/integration/test_message_persistence.py tests/unit/test_engine_import_graph.py
git commit -m "fix(webui-1B): engine poller mirrors only known agent identities (A-02b)

Senders are resolved from bot_id/user against the connected clients'
auth.test identities and named from the registry, never from username.
An unknown bot is dropped with the cursor advanced and one content-free
INFO line. Freeze lifted for this item only (D2). Needs the agent image
rebuilt.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-3: Session cookie name by setting, shared test helper, every forger updated

**Files:**
- Modify: `src/main.py:14` (import), `:41-42`, `:110`, `:278`
- Create: `tests/session_support.py`
- Create: `tests/integration/test_session_cookie.py`
- Modify (scripted, Step 5): `tests/integration/test_cohort_admin.py`, `test_onboarding_flow.py`,
  `test_proposal_review.py`, `test_access_revocation.py`, `test_reviewer_role.py`,
  `test_discussions_filters.py`, `test_orphaned_agent_surfaces.py`, `test_manager_onboarding.py`,
  `test_origin_guard.py`, `test_auth_allowlist_gate.py`, `test_manager_access.py`,
  `test_agent_page.py`, `test_opportunity_assessment_persistence.py`,
  `test_account_deletion_routes.py`, `test_star_topology.py`, `test_manager_industry_panel.py`
  (all under `tests/integration/`), `tests/characterization/test_auth_and_admin_routes.py`
- Modify: `tests/e2e/session.py:1-21`
- Modify: `tests/e2e/ui_audit/run.py` (`_run`: the `from src.main import SESSION_COOKIE` line and `cookie_name=SESSION_COOKIE`)

**Interfaces:**
- Consumes: `Settings.allow_http_sessions` (`src/config.py:143`).
- Produces: `src.main.SESSION_COOKIE_HTTP = "copi-session"`,
  `src.main.SESSION_COOKIE_HTTPS = "__Host-copi-session"`,
  `src.main.session_cookie_name(settings: Settings) -> str` (replaces `src.main.SESSION_COOKIE`,
  which is deleted); `tests.session_support.session_cookie_name() -> str`,
  `sign_session(payload: dict) -> str`, `raw_session_headers(payload: dict) -> dict[str, str]`,
  `session_headers(user_id, **extra) -> dict[str, str]`,
  `session_from_response(response) -> dict | None`, `SESSION_MAX_AGE = 2592000`.
  Consumed by Tasks 1B-4..1B-8 and by any later part that forges a session.

Inventory (grep `copi-session` / `copi-impersonate` over `tests/`, `scripts/`, `src/`):
17 test files forge or name the session cookie (Step 5's list); `tests/e2e/session.py`
and the Phase 0 harness (`tests/e2e/ui_audit/run.py`, `harness.py`) forge it for http
instances; `tests/e2e/auth_helper.py` (stdlib-only, plants `copi-session` on an
`ALLOW_HTTP_SESSIONS=true` dev container) stays correct and is not changed;
`scripts/dev/*` forge nothing (`assessment_chat_demo.py` overrides the auth dependency).

- [ ] **Step 1: Write the failing cookie test**

Create `tests/integration/test_session_cookie.py`:

```python
"""The session cookie's name follows the setting (spec 2026-10-01 §6.7, A-05):
`__Host-copi-session` when it is Secure, `copi-session` over plain HTTP."""
import httpx
import pytest
from httpx import ASGITransport

import src.main as main_module
from src.config import get_settings

pytestmark = pytest.mark.integration


def test_the_name_follows_allow_http_sessions():
    real = get_settings()
    secure = real.model_copy(update={"allow_http_sessions": False})
    plain = real.model_copy(update={"allow_http_sessions": True})
    assert main_module.session_cookie_name(secure) == "__Host-copi-session"
    assert main_module.session_cookie_name(plain) == "copi-session"


async def _session_set_cookies(monkeypatch, *, allow_http: bool) -> list[str]:
    real = get_settings()
    monkeypatch.setattr(
        main_module, "get_settings",
        lambda: real.model_copy(update={"allow_http_sessions": allow_http}),
    )
    app = main_module.create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        # A safe `next` makes /login write the session, so a Set-Cookie is issued.
        r = await c.get("/login", params={"next": "/profile"})
    return [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]


async def test_a_secure_session_cookie_meets_the_host_prefix_rules(monkeypatch):
    cookies = await _session_set_cookies(monkeypatch, allow_http=False)
    assert len(cookies) == 1, cookies
    cookie = cookies[0]
    assert cookie.startswith("__Host-copi-session="), cookie
    assert "; path=/;" in cookie
    assert "secure" in cookie.lower()
    assert "domain=" not in cookie.lower()


async def test_a_plain_http_session_cookie_keeps_the_old_name(monkeypatch):
    cookies = await _session_set_cookies(monkeypatch, allow_http=True)
    assert len(cookies) == 1, cookies
    assert cookies[0].startswith("copi-session=")
    assert "secure" not in cookies[0].lower()
```

- [ ] **Step 2: Run it; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_session_cookie.py -v`
Expected: FAIL — `AttributeError: module 'src.main' has no attribute 'session_cookie_name'`.

- [ ] **Step 3: Implement the name in `src/main.py`**

Line 14, before `from src.config import get_settings`, after
`from src.config import Settings, get_settings`.

Before (lines 41-42):
```python
#: The session cookie, spelled the same way create_app() configures it below.
SESSION_COOKIE = "copi-session"
```
after:
```python
#: The session cookie's name over plain HTTP (``ALLOW_HTTP_SESSIONS=true``: local
#: development and the browser harness).
SESSION_COOKIE_HTTP = "copi-session"
#: Its name when the cookie is Secure. The ``__Host-`` prefix makes a browser refuse
#: the cookie unless it is Secure, has ``Path=/`` and carries no ``Domain``, so a
#: sibling host on the shared registrable domain cannot set or shadow it (A-05).
#: SessionMiddleware's defaults (path "/", no domain) satisfy all three.
SESSION_COOKIE_HTTPS = "__Host-copi-session"


def session_cookie_name(settings: Settings) -> str:
    """The session cookie name create_app() configures for ``settings``."""
    return SESSION_COOKIE_HTTP if settings.allow_http_sessions else SESSION_COOKIE_HTTPS
```
Line 110 (OriginGuardMiddleware docstring), before:
```python
    ``copi-session`` cookie would ride along. ``POST /profile/delete-account``
```
after:
```python
    session cookie would ride along. ``POST /profile/delete-account``
```
Line 278, before `        session_cookie=SESSION_COOKIE,`, after
`        session_cookie=session_cookie_name(settings),`.

- [ ] **Step 4: Create the shared test helper**

Create `tests/session_support.py`:

```python
"""Forge and read the signed session cookie the way ``SessionMiddleware`` does.

``create_app()`` (src/main.py) stores the session as
``TimestampSigner(secret_key).sign(base64(json(session)))`` under
``session_cookie_name(settings)``. The suite reads ``.env``; the host's sets
``ALLOW_HTTP_SESSIONS=false`` and the setting defaults to false, so tests normally run
under ``__Host-copi-session``. Every helper asks the app for the name instead of
spelling it. A forged session with no ``epoch`` key reads as epoch 0, which every
account whose ``users.session_epoch`` was never bumped accepts.
"""

from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie

from itsdangerous import TimestampSigner

from src.config import get_settings
from src.main import session_cookie_name as _cookie_name_for

#: SessionMiddleware's max_age in create_app() (30 days).
SESSION_MAX_AGE = 30 * 24 * 3600


def session_cookie_name() -> str:
    """The session cookie name create_app() uses under the current settings."""
    return _cookie_name_for(get_settings())


def sign_session(payload: dict) -> str:
    """The cookie value SessionMiddleware would issue for ``payload``."""
    signer = TimestampSigner(get_settings().secret_key)
    return signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()


def raw_session_headers(payload: dict) -> dict[str, str]:
    """Request headers carrying a session holding exactly ``payload``."""
    return {"Cookie": f"{session_cookie_name()}={sign_session(payload)}"}


def session_headers(user_id, **extra) -> dict[str, str]:
    """Request headers carrying a signed-in session for ``user_id`` plus ``extra`` keys."""
    return raw_session_headers({"user_id": str(user_id), **extra})


def session_from_response(response) -> dict | None:
    """Decode the session a response set.

    None: the response set no session cookie (the request's session carried forward
    unchanged). ``{}``: the session was cleared — Starlette re-sets the cookie to the
    literal string "null".
    """
    name = session_cookie_name()
    raw = None
    for k, v in response.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{name}="):
            raw = v
    if raw is None:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    value = jar[name].value
    if not value or value == "null":
        return {}
    signer = TimestampSigner(get_settings().secret_key)
    return json.loads(base64.b64decode(signer.unsign(value, max_age=SESSION_MAX_AGE)))
```

- [ ] **Step 5: Rewrite every forger of the cookie name (scripted)**

Run from the repository root:

```bash
.venv-test/bin/python - <<'EOF'
import re
from pathlib import Path

FILES = [
    "tests/integration/test_cohort_admin.py",
    "tests/integration/test_onboarding_flow.py",
    "tests/integration/test_proposal_review.py",
    "tests/integration/test_access_revocation.py",
    "tests/integration/test_reviewer_role.py",
    "tests/integration/test_discussions_filters.py",
    "tests/integration/test_orphaned_agent_surfaces.py",
    "tests/integration/test_manager_onboarding.py",
    "tests/integration/test_origin_guard.py",
    "tests/integration/test_auth_allowlist_gate.py",
    "tests/integration/test_manager_access.py",
    "tests/integration/test_agent_page.py",
    "tests/integration/test_opportunity_assessment_persistence.py",
    "tests/integration/test_account_deletion_routes.py",
    "tests/integration/test_star_topology.py",
    "tests/integration/test_manager_industry_panel.py",
    "tests/characterization/test_auth_and_admin_routes.py",
]
REPLACEMENTS = (
    ('f"copi-session=', 'f"{session_cookie_name()}='),
    ('session_cookie="copi-session"', "session_cookie=session_cookie_name()"),
    ('client.cookies.set("copi-session",', "client.cookies.set(session_cookie_name(),"),
    ('SESSION_COOKIE = "copi-session"', "SESSION_COOKIE = session_cookie_name()"),
    ("victim's ``copi-session``", "victim's session"),
    ("Forge a 'copi-session' cookie", "Forge the session cookie"),
)
IMPORT = "from tests.session_support import session_cookie_name\n"
IMPORT_LINE = re.compile(r"(import [\w.]+|from [\w.]+ import )")

for name in FILES:
    path = Path(name)
    text = path.read_text(encoding="utf-8")
    new = text
    for old, repl in REPLACEMENTS:
        new = new.replace(old, repl)
    assert new != text, f"{name}: nothing replaced"
    assert '"copi-session' not in new, f"{name}: a spelling survived"
    # Insert after the LAST import of the FIRST contiguous import block (blank and
    # comment lines allowed inside it), never after a later module-level import,
    # so no new E402 can appear.
    lines = new.splitlines(keepends=True)
    i = next(n for n, line in enumerate(lines) if IMPORT_LINE.match(line))
    last = i
    while i < len(lines):
        line = lines[i]
        if IMPORT_LINE.match(line):
            if line.rstrip().endswith("("):
                while lines[i].strip() != ")":
                    i += 1
            last = i
        elif line.strip() and not line.startswith("#"):
            break
        i += 1
    lines.insert(last + 1, IMPORT)
    path.write_text("".join(lines), encoding="utf-8")
    print("updated", name)
EOF
.venv-test/bin/python -m ruff check --select I --fix \
  tests/integration/test_cohort_admin.py tests/integration/test_onboarding_flow.py \
  tests/integration/test_proposal_review.py tests/integration/test_access_revocation.py \
  tests/integration/test_reviewer_role.py tests/integration/test_discussions_filters.py \
  tests/integration/test_orphaned_agent_surfaces.py tests/integration/test_manager_onboarding.py \
  tests/integration/test_origin_guard.py tests/integration/test_auth_allowlist_gate.py \
  tests/integration/test_manager_access.py tests/integration/test_agent_page.py \
  tests/integration/test_opportunity_assessment_persistence.py \
  tests/integration/test_account_deletion_routes.py tests/integration/test_star_topology.py \
  tests/integration/test_manager_industry_panel.py \
  tests/characterization/test_auth_and_admin_routes.py
```
Expected: 17 `updated ...` lines, then ruff reports the import blocks it sorted (or
nothing). The script asserts that no file is left unchanged and that no quoted
`"copi-session` spelling survives.

- [ ] **Step 6: The e2e forger and the harness**

`tests/e2e/session.py`, before (lines 1-21, docstring head and constant):
```python
"""Forge the signed session cookie ``SessionMiddleware`` would issue.

Identical construction to ``tests/integration/test_cohort_admin.py::_auth`` —
``itsdangerous.TimestampSigner(secret_key)`` over ``base64(json(session))``,
under cookie name ``copi-session`` (see ``src/main.py``). Kept in its own module
so both the pytest flows and the host-side ``auth_helper`` can use it.
```
after:
```python
"""Forge the signed session cookie ``SessionMiddleware`` would issue.

Identical construction to ``tests/session_support.py`` —
``itsdangerous.TimestampSigner(secret_key)`` over ``base64(json(session))``,
under ``src.main.SESSION_COOKIE_HTTP`` (``copi-session``): every instance these
flows and the ui_audit harness drive runs ``ALLOW_HTTP_SESSIONS=true``. Kept in
its own module so both the pytest flows and the host-side ``auth_helper`` can use it.
```
and before:
```python
from itsdangerous import TimestampSigner

COOKIE_NAME = "copi-session"
```
after:
```python
from itsdangerous import TimestampSigner

from src.main import SESSION_COOKIE_HTTP

COOKIE_NAME = SESSION_COOKIE_HTTP
```
`tests/e2e/ui_audit/run.py` (`_run`), before:
```python
    from playwright.async_api import async_playwright

    from src.main import SESSION_COOKIE
```
after:
```python
    from playwright.async_api import async_playwright

    from src.config import get_settings
    from src.main import session_cookie_name
```
and before:
```python
        h = Harness(base_url=stack.base_url, ids=stack.ids, browser=browser,
                    cookie_name=SESSION_COOKIE, secret_key=stack.secret, axe_source=axe)
```
after:
```python
        h = Harness(base_url=stack.base_url, ids=stack.ids, browser=browser,
                    cookie_name=session_cookie_name(get_settings()),
                    secret_key=stack.secret, axe_source=axe)
```

- [ ] **Step 7: Lint and run the cookie test plus the heaviest forgers**

Run: `.venv-test/bin/python -m ruff check tests/ && grep -rn "SESSION_COOKIE\b" src/ tests/ scripts/ --include='*.py' | grep -v "SESSION_COOKIE_HTTP\|SESSION_COOKIE = session_cookie_name()\|{SESSION_COOKIE}\|\[SESSION_COOKIE\]"`
Expected: `All checks passed!`, and the grep prints nothing (no import of the deleted
`src.main.SESSION_COOKIE` remains).

Run: `.venv-test/bin/python -m pytest tests/integration/test_session_cookie.py tests/integration/test_access_revocation.py tests/integration/test_origin_guard.py tests/integration/test_manager_access.py tests/integration/test_reviewer_role.py tests/integration/test_manager_onboarding.py tests/integration/test_auth_allowlist_gate.py tests/characterization/test_auth_and_admin_routes.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/main.py tests/session_support.py tests/integration/test_session_cookie.py \
  tests/integration/test_cohort_admin.py tests/integration/test_onboarding_flow.py \
  tests/integration/test_proposal_review.py tests/integration/test_access_revocation.py \
  tests/integration/test_reviewer_role.py tests/integration/test_discussions_filters.py \
  tests/integration/test_orphaned_agent_surfaces.py tests/integration/test_manager_onboarding.py \
  tests/integration/test_origin_guard.py tests/integration/test_auth_allowlist_gate.py \
  tests/integration/test_manager_access.py tests/integration/test_agent_page.py \
  tests/integration/test_opportunity_assessment_persistence.py \
  tests/integration/test_account_deletion_routes.py tests/integration/test_star_topology.py \
  tests/integration/test_manager_industry_panel.py \
  tests/characterization/test_auth_and_admin_routes.py tests/e2e/session.py tests/e2e/ui_audit/run.py
git commit -m "feat(webui-1B): __Host-copi-session when the session cookie is Secure (A-05)

session_cookie_name(settings) replaces the SESSION_COOKIE constant; every
test forger asks tests/session_support.py for the name.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-4: Impersonation inside the signed session

**Files:**
- Modify: `src/dependencies.py:3-13` (imports), after `:15` (constants), `:41-48` (docstring), `:117-134` (impersonation block), new helpers after `get_current_user`
- Modify: `src/routers/admin/impersonation.py` (whole module body, lines 1-74)
- Modify: `src/routers/auth.py:367-371` (logout tail)
- Modify: `src/routers/profile.py:236-239` (delete-account tail)
- Modify: `tests/session_support.py` (`session_headers`)
- Modify: `tests/integration/test_manager_access.py:6-14, 22-30` (as left by 1B-3)
- Modify: `tests/integration/test_manager_industry_panel.py:14, 127`
- Modify: `tests/integration/test_onboarding_flow.py:71-86, 1049, 1135, 1153`
- Modify: `tests/integration/test_account_deletion_routes.py:38-52`
- Modify (scripted): `tests/integration/test_prompt_suggestions_page.py`, `test_assessment_review_ui.py`,
  `test_assessment_chat_routes.py`, `test_manager_slack_provisioning.py`, `test_assessment_queue_controls.py`,
  `test_reviewer_role.py`, `test_assessment_chat_templates.py`, `test_reviews_router.py`,
  `test_manager_views.py`, `test_impersonation_guards.py` (19 sites)
- Modify: `tests/characterization/test_auth_and_admin_routes.py:283-347`
- Modify: `scripts/mutate_system.sh:295` (description field only)
- Test: `tests/integration/test_impersonation_session.py`

**Interfaces:**
- Consumes: `tests.session_support.session_headers`, `session_from_response`, `session_cookie_name` (1B-3).
- Produces: `src.dependencies.IMPERSONATE_KEY = "impersonate_user_id"`,
  `IMPERSONATE_EXPIRES_KEY = "impersonate_expires_at"` (int, unix seconds),
  `IMPERSONATION_MAX_AGE = 86400`, `start_impersonation(request: Request, target_id: uuid.UUID) -> None`,
  `end_impersonation(request: Request) -> None`,
  `_impersonated_user(request, db, session_user) -> User | None`;
  `tests.session_support.session_headers(user_id, *, impersonate=None, **extra)`;
  `tests.integration.test_manager_access.auth_headers(user_id, *, impersonate=None) -> dict`.
  `IMPERSONATE_EXPIRES_KEY` keeps the 24 h bound the old cookie's `max_age=86400` gave;
  an impersonation key without a valid expiry is not honoured.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_impersonation_session.py`:

```python
"""Impersonation lives in the signed session (spec 2026-10-01 §6.7, A-05); the unsigned
`copi-impersonate` cookie is neither read nor written."""
import time

import pytest

from src.dependencies import IMPERSONATE_EXPIRES_KEY, IMPERSONATE_KEY, IMPERSONATION_MAX_AGE
from src.models import USER_ROLE_ADMIN, USER_ROLE_PI
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.session_support import session_cookie_name, session_from_response, session_headers

pytestmark = pytest.mark.integration

BANNER = "Viewing as Imp Target"


def _set_cookie_names(response) -> list[str]:
    return [
        v.split("=", 1)[0] for k, v in response.headers.multi_items()
        if k.lower() == "set-cookie"
    ]


async def _admin_and_pi(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Imp Admin")
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Imp Target")
    await factories.make_profile(db_session, user=pi)
    await db_session.flush()
    return admin, pi


async def test_starting_impersonation_writes_the_session_and_no_cookie(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    before = int(time.time())
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid}, headers=auth_headers(admin.id)
    )
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert _set_cookie_names(r) == [session_cookie_name()]
    session = session_from_response(r)
    assert session["user_id"] == str(admin.id)
    assert session[IMPERSONATE_KEY] == str(pi.id)
    assert (before + IMPERSONATION_MAX_AGE <= session[IMPERSONATE_EXPIRES_KEY]
            <= int(time.time()) + IMPERSONATION_MAX_AGE)


async def test_an_impersonating_session_views_as_the_target(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.get("/profile", headers=auth_headers(admin.id, impersonate=pi.id))
    assert r.status_code == 200
    assert BANNER in r.text


async def test_stop_drops_the_impersonation_and_writes_no_other_cookie(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.post(
        "/admin/impersonate/stop", headers=auth_headers(admin.id, impersonate=pi.id)
    )
    assert r.status_code == 302 and r.headers["location"] == "/admin/users"
    assert _set_cookie_names(r) == [session_cookie_name()]
    session = session_from_response(r)
    assert session["user_id"] == str(admin.id)
    assert IMPERSONATE_KEY not in session and IMPERSONATE_EXPIRES_KEY not in session


async def test_the_legacy_cookie_is_inert(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={pi.id}"
    r = await client.get("/profile", headers=headers)
    assert BANNER not in r.text
    control = await client.get("/profile", headers=auth_headers(admin.id, impersonate=pi.id))
    assert BANNER in control.text, "the control never impersonated, so the negative is vacuous"


async def test_a_lapsed_impersonation_is_dropped(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    headers = session_headers(
        admin.id, **{IMPERSONATE_KEY: str(pi.id), IMPERSONATE_EXPIRES_KEY: int(time.time()) - 1}
    )
    r = await client.get("/profile", headers=headers)
    assert BANNER not in r.text
    session = session_from_response(r)
    assert session is not None and IMPERSONATE_KEY not in session
    assert IMPERSONATE_EXPIRES_KEY not in session


async def test_an_impersonation_without_an_expiry_is_not_honoured(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.get("/profile", headers=session_headers(admin.id, **{IMPERSONATE_KEY: str(pi.id)}))
    assert BANNER not in r.text


async def test_a_non_admin_session_cannot_impersonate(client, db_session):
    _admin, pi = await _admin_and_pi(db_session)
    other = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Not Admin")
    await factories.make_profile(db_session, user=other)
    await db_session.flush()
    r = await client.get("/profile", headers=auth_headers(other.id, impersonate=pi.id))
    assert r.status_code == 200
    assert BANNER not in r.text


async def test_logout_clears_the_impersonation_with_the_session(client, db_session):
    admin, pi = await _admin_and_pi(db_session)
    r = await client.post("/logout", headers=auth_headers(admin.id, impersonate=pi.id))
    assert "copi-impersonate" not in _set_cookie_names(r)
    assert session_from_response(r) == {}
```

- [ ] **Step 2: Run them; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonation_session.py -v`
Expected: collection ERROR — `ImportError: cannot import name 'IMPERSONATE_EXPIRES_KEY' from 'src.dependencies'`.

- [ ] **Step 3: Move impersonation into the session in `src/dependencies.py`**

Before (lines 3-5):
```python
import logging
import uuid
from urllib.parse import quote
```
after:
```python
import logging
import time
import uuid
from urllib.parse import quote
```
Before (line 15):
```python
logger = logging.getLogger(__name__)
```
after:
```python
logger = logging.getLogger(__name__)

#: Session keys holding an admin's impersonation (spec 2026-10-01 §6.7). They live
#: in the signed session, so only POST /admin/impersonate writes them; the old
#: unsigned ``copi-impersonate`` cookie is neither read nor written.
IMPERSONATE_KEY = "impersonate_user_id"
IMPERSONATE_EXPIRES_KEY = "impersonate_expires_at"
#: An impersonation lapses this many seconds after it starts: the 24 h the old
#: cookie's max_age gave it.
IMPERSONATION_MAX_AGE = 24 * 3600
```
Before (lines 45-48):
```python
    """
    Auth dependency. Checks session cookie for user_id.
    Handles impersonation via copi-impersonate cookie (admin only).
    """
```
after:
```python
    """Auth dependency: the user the signed session holds, or a 302 to /login or
    /access-pending.

    For an admin session holding an impersonation (``IMPERSONATE_KEY``, set by
    POST /admin/impersonate), returns the impersonated user, tagged for the banner.
    """
```
Before (lines 117-134):
```python
    # Impersonation: admin can view as another user
    impersonate_id = request.cookies.get("copi-impersonate")
    if impersonate_id and session_user.is_admin:
        try:
            imp_uuid = uuid.UUID(impersonate_id)
            result = await db.execute(
                select(User).options(selectinload(User.profile)).where(User.id == imp_uuid)
            )
            imp_user = result.scalar_one_or_none()
            if imp_user:
                # Tag so templates can show impersonation banner
                imp_user._is_impersonated = True  # type: ignore[attr-defined]
                imp_user._real_admin = session_user  # type: ignore[attr-defined]
                return imp_user
        except (ValueError, Exception) as exc:
            logger.warning("Invalid impersonate cookie: %s", exc)

    return session_user
```
after:
```python
    # Impersonation: admin can view as another user
    impersonated = await _impersonated_user(request, db, session_user)
    return impersonated if impersonated is not None else session_user


def start_impersonation(request: Request, target_id: uuid.UUID) -> None:
    """Record in the signed session that this admin session views the site as
    ``target_id``, for IMPERSONATION_MAX_AGE seconds."""
    request.session[IMPERSONATE_KEY] = str(target_id)
    request.session[IMPERSONATE_EXPIRES_KEY] = int(time.time()) + IMPERSONATION_MAX_AGE


def end_impersonation(request: Request) -> None:
    """Drop the session's impersonation, if it holds one."""
    request.session.pop(IMPERSONATE_KEY, None)
    request.session.pop(IMPERSONATE_EXPIRES_KEY, None)


async def _impersonated_user(
    request: Request, db: AsyncSession, session_user: User
) -> User | None:
    """The user an admin session is viewing as, tagged for the banner; None when the
    session is not impersonating, its holder is not an admin, the impersonation has
    lapsed or is malformed (both dropped from the session), or the target is gone."""
    impersonate_id = request.session.get(IMPERSONATE_KEY)
    if impersonate_id and session_user.is_admin:
        expires_at = request.session.get(IMPERSONATE_EXPIRES_KEY)
        if not isinstance(expires_at, int) or expires_at <= time.time():
            end_impersonation(request)
            return None
        try:
            imp_uuid = uuid.UUID(str(impersonate_id))
        except ValueError:
            logger.warning("Invalid impersonate_user_id in session: %r", impersonate_id)
            end_impersonation(request)
            return None
        result = await db.execute(
            select(User).options(selectinload(User.profile)).where(User.id == imp_uuid)
        )
        imp_user = result.scalar_one_or_none()
        if imp_user:
            # Tag so templates can show impersonation banner
            imp_user._is_impersonated = True  # type: ignore[attr-defined]
            imp_user._real_admin = session_user  # type: ignore[attr-defined]
            return imp_user
    return None
```

- [ ] **Step 4: Rewrite the start/stop routes**

Replace the whole of `src/routers/admin/impersonation.py` with:

```python
"""Admin impersonation start/stop routes.

The impersonation is held in the signed session (``IMPERSONATE_KEY`` in
src/dependencies.py, spec 2026-10-01 §6.7). It used to be an unsigned
``copi-impersonate`` cookie, which anything able to set a cookie for this host
could point at any user for an admin's browser (A-05).
"""

import logging

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import (
    end_impersonation,
    get_admin_user,
    get_current_user,
    start_impersonation,
)
from src.models import User
from src.routers.admin._common import router
from src.services.pi_onboarding import find_or_create_pi_by_orcid

logger = logging.getLogger("src.routers.admin")


@router.post("/impersonate")
async def impersonate_user(
    request: Request,
    orcid: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Start impersonating a user by ORCID."""
    # Security: this route requires admin
    orcid = orcid.strip()

    result = await db.execute(select(User).where(User.orcid == orcid))
    target = result.scalar_one_or_none()

    if not target:
        try:
            target = await find_or_create_pi_by_orcid(db, orcid)
            await db.commit()
        except ValueError as exc:
            logger.error("Failed to fetch ORCID profile for impersonation: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ORCID {orcid} not found",
            )

    start_impersonation(request, target.id)
    return RedirectResponse(url="/", status_code=302)



@router.post("/impersonate/stop")
async def stop_impersonating(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Stop impersonating — drop the impersonation from the session."""
    end_impersonation(request)
    return RedirectResponse(url="/admin/users", status_code=302)
```

`src/routers/auth.py`, before (lines 367-371):
```python
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
```
after:
```python
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
```
`src/routers/profile.py`, before (lines 236-239):
```python
    request.session.clear()
    response = RedirectResponse(url="/login?deleted=1", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
```
after:
```python
    request.session.clear()
    return RedirectResponse(url="/login?deleted=1", status_code=302)
```

- [ ] **Step 5: Teach the test helpers to sign an impersonation**

`tests/session_support.py`: add `import time` after `import json`, and after the import of
`_cookie_name_for` add `from src.dependencies import IMPERSONATE_EXPIRES_KEY, IMPERSONATE_KEY, IMPERSONATION_MAX_AGE`.
Before:
```python
def session_headers(user_id, **extra) -> dict[str, str]:
    """Request headers carrying a signed-in session for ``user_id`` plus ``extra`` keys."""
    return raw_session_headers({"user_id": str(user_id), **extra})
```
after:
```python
def session_headers(user_id, *, impersonate=None, **extra) -> dict[str, str]:
    """Request headers carrying a signed-in session for ``user_id`` plus ``extra`` keys.

    ``impersonate`` signs an impersonation of that user into the session, with the
    expiry POST /admin/impersonate would give it; it is honoured only for an admin.
    """
    payload = {"user_id": str(user_id), **extra}
    if impersonate is not None:
        payload[IMPERSONATE_KEY] = str(impersonate)
        payload[IMPERSONATE_EXPIRES_KEY] = int(time.time()) + IMPERSONATION_MAX_AGE
    return raw_session_headers(payload)
```
`tests/integration/test_manager_access.py` (as left by 1B-3), before:
```python
def _session_cookie(user_id) -> str:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return signer.sign(data).decode("utf-8")


def auth_headers(user_id) -> dict:
    """Imported by test_manager_views.py and the other integration suites."""
    return {"Cookie": f"{session_cookie_name()}={_session_cookie(user_id)}"}
```
after:
```python
def auth_headers(user_id, *, impersonate=None) -> dict:
    """Imported by test_manager_views.py and the other integration suites.

    ``impersonate`` signs an admin's impersonation of that user into the session, as
    POST /admin/impersonate does.
    """
    return session_headers(user_id, impersonate=impersonate)
```
and in its imports delete `import base64`, `import json`, `from itsdangerous import TimestampSigner`,
and change `from tests.session_support import session_cookie_name` to
`from tests.session_support import session_cookie_name, session_headers`.
`tests/integration/test_manager_industry_panel.py`, line 14 before
`from tests.integration.test_manager_access import _session_cookie, auth_headers`, after
`from tests.integration.test_manager_access import auth_headers`; line 127 before:
```python
    headers = {"Cookie": f"{session_cookie_name()}={_session_cookie(admin.id)}; copi-impersonate={mgr.id}"}
```
after:
```python
    headers = auth_headers(admin.id, impersonate=mgr.id)
```
`tests/integration/test_onboarding_flow.py` `_auth_as` (as left by 1B-3), before:
```python
def _auth_as(user_id, impersonate_id) -> dict:
    """Session for ``user_id`` plus the copi-impersonate cookie pointed at another user.

    src/dependencies.get_current_user honours that cookie *only* when the session
    user is an admin. It is the one handle any of these 10 endpoints gives a
    caller on somebody else's identity, so it is the vector the sweep attacks.
    """
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {
        "Cookie": (
            f"{session_cookie_name()}={signer.sign(data).decode()}; "
            f"copi-impersonate={impersonate_id}"
        )
    }
```
after:
```python
def _auth_as(user_id, impersonate_id) -> dict:
    """Session for ``user_id`` with an impersonation of another user signed into it.

    src/dependencies.get_current_user honours that impersonation *only* when the
    session user is an admin. It is the one handle any of these 10 endpoints gives a
    caller on somebody else's identity, so it is the vector the sweep attacks.
    """
    return session_headers(user_id, impersonate=impersonate_id)
```
In the same file: line 1050 `    on another identity is the ``copi-impersonate`` cookie, which` becomes
`    on another identity is the session's ``impersonate_user_id``, which`; both copies of
`            "the copi-impersonate cookie is inert even for an admin, so the "` (lines 1135
and 1153) become `            "impersonation is inert even for an admin, so the "`; add
`session_headers` to its `from tests.session_support import ...` line.
`tests/integration/test_account_deletion_routes.py` `_auth_as`, before:
```python
def _auth_as(admin_id, impersonate_id) -> dict:
    """Admin session plus the copi-impersonate cookie.

    Byte-identical to tests/integration/test_onboarding_flow.py:74 — the
    impersonate cookie is a PLAIN unsigned UUID (src/dependencies.py reads it
    with uuid.UUID(cookie_value), no signer).
    """
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(admin_id)}).encode())
    return {
        "Cookie": (
            f"{session_cookie_name()}={signer.sign(data).decode()}; "
            f"copi-impersonate={impersonate_id}"
        )
    }
```
after:
```python
def _auth_as(admin_id, impersonate_id) -> dict:
    """Admin session with an impersonation of ``impersonate_id`` signed into it."""
    return session_headers(admin_id, impersonate=impersonate_id)
```
and add `session_headers` to its `from tests.session_support import ...` line.

Then rewrite the 19 two-line impersonation sites (scripted):

```bash
.venv-test/bin/python - <<'EOF'
import re
from pathlib import Path

PATTERN = re.compile(
    r'(?P<var>\w+) = auth_headers\((?P<user>[\w.]+)\)\n'
    r'[ \t]+(?P=var)\["Cookie"\] \+= f"; copi-impersonate=\{(?P<target>[\w.]+)\}"'
)
EXPECTED = {
    "tests/integration/test_prompt_suggestions_page.py": 1,
    "tests/integration/test_assessment_review_ui.py": 1,
    "tests/integration/test_assessment_chat_routes.py": 2,
    "tests/integration/test_manager_slack_provisioning.py": 2,
    "tests/integration/test_assessment_queue_controls.py": 1,
    "tests/integration/test_reviewer_role.py": 1,
    "tests/integration/test_assessment_chat_templates.py": 1,
    "tests/integration/test_reviews_router.py": 3,
    "tests/integration/test_manager_views.py": 6,
    "tests/integration/test_impersonation_guards.py": 1,
}
for name, count in EXPECTED.items():
    path = Path(name)
    new, n = PATTERN.subn(r"\g<var> = auth_headers(\g<user>, impersonate=\g<target>)",
                          path.read_text(encoding="utf-8"))
    assert n == count, f"{name}: {n} sites, expected {count}"
    path.write_text(new, encoding="utf-8")
    print("updated", name, n)
EOF
```
Expected: ten `updated` lines whose counts sum to 19.

- [ ] **Step 6: Replace the characterization pin and the mutant's description**

In `tests/characterization/test_auth_and_admin_routes.py`, replace everything from line 283
(`# --- impersonate cookie flags (E1.5) ---...`) to the end of the file with:

```python
# --- impersonation is not a cookie of its own (spec 2026-10-01 §6.7, A-05) ---

async def test_impersonation_sets_no_cookie_of_its_own(client, db_session):
    """Replaces the E1.5 pin on the `copi-impersonate` Secure flag: the impersonation
    now rides in the signed session cookie, whose Secure flag follows
    `allow_http_sessions` (tests/integration/test_session_cookie.py)."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    target = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await db_session.flush()

    r = await client.post(
        "/admin/impersonate", data={"orcid": target.orcid}, headers=_auth_headers(admin.id)
    )
    assert r.status_code == 302
    names = [v.split("=", 1)[0] for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert names == [session_cookie_name()]
```
`scripts/mutate_system.sh` line 295, the fifth `~~` field only, before
`M8 copi-impersonate is honoured for non-admins — any logged-in user can become any other user`,
after `M8 the session's impersonate_user_id is honoured for non-admins — any logged-in user can become any other user`
(the target and replacement fields stay byte-identical; both still match exactly once).

- [ ] **Step 7: Lint, grep and run; expect pass**

Run: `.venv-test/bin/python -m ruff check --select I,F401 --fix tests/session_support.py tests/integration/test_manager_access.py tests/integration/test_manager_industry_panel.py tests/integration/test_onboarding_flow.py tests/integration/test_account_deletion_routes.py && .venv-test/bin/python -m ruff check tests/`
Expected: `All checks passed!` (the fix pass may remove now-unused `base64`/`json`/`TimestampSigner`/`get_settings` imports in the two `_auth_as` files only if their `_auth` no longer uses them; it does, so nothing is removed there).

Run: `grep -rn "copi-impersonate" src/ tests/ scripts/ --include='*.py' --include='*.sh' --include='*.html'`
Expected: only `tests/integration/test_impersonation_session.py` (the legacy-inert test and
the logout check) and the `src/routers/admin/impersonation.py` / `src/dependencies.py`
comments that name the retired cookie.

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonation_session.py tests/integration/test_impersonation_guards.py tests/integration/test_manager_views.py tests/integration/test_onboarding_flow.py tests/integration/test_account_deletion_routes.py tests/integration/test_reviewer_role.py tests/integration/test_reviews_router.py tests/integration/test_manager_industry_panel.py tests/characterization/test_auth_and_admin_routes.py tests/unit/test_mutation_harness_targets.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/dependencies.py src/routers/admin/impersonation.py src/routers/auth.py \
  src/routers/profile.py tests/session_support.py tests/integration/test_impersonation_session.py \
  tests/integration/test_manager_access.py tests/integration/test_manager_industry_panel.py \
  tests/integration/test_onboarding_flow.py tests/integration/test_account_deletion_routes.py \
  tests/integration/test_prompt_suggestions_page.py tests/integration/test_assessment_review_ui.py \
  tests/integration/test_assessment_chat_routes.py tests/integration/test_manager_slack_provisioning.py \
  tests/integration/test_assessment_queue_controls.py tests/integration/test_reviewer_role.py \
  tests/integration/test_assessment_chat_templates.py tests/integration/test_reviews_router.py \
  tests/integration/test_manager_views.py tests/integration/test_impersonation_guards.py \
  tests/characterization/test_auth_and_admin_routes.py scripts/mutate_system.sh
git commit -m "fix(webui-1B): impersonation lives in the signed session (A-05)

session[\"impersonate_user_id\"] with a 24 h expiry replaces the unsigned
copi-impersonate cookie, which is no longer read or written.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-5: Session epoch — login reset, revocation check, logout / deny / role bumps

**Files:**
- Create: `src/services/session_epoch.py`
- Modify: `src/dependencies.py` (imports; access-block comment `:83-88`; epoch check before `# Impersonation: admin can view as another user`)
- Modify: `src/routers/auth.py:3-5, 7, 20-22, 28` (imports, `_DB`), `:231-236` (`_post_login_redirect`), `:340-342` (`auth_callback`), `:355-371` (logout), new `_start_fresh_session`, `_session_user_id`
- Modify: `src/routers/admin/access.py:16, 112-113`
- Modify: `src/routers/admin/users.py:175-177`
- Modify: `src/cli.py:126-136, 160-180, 199-211`
- Modify: `tests/integration/test_cli.py` (append after `test_role_set_round_trips_through_all_roles`)
- Test: `tests/integration/test_session_epoch.py`

**Interfaces:**
- Consumes: `User.session_epoch` (1B-1); `session_headers`, `raw_session_headers`, `session_from_response` (1B-3);
  `tests.integration.test_auth_allowlist_gate._fake_oauth`; `pop_post_login_redirect`, `POST_LOGIN_KEY` (`src/routers/auth.py`).
- Produces (brief contract, exact names): `src/services/session_epoch.py` with
  `SESSION_EPOCH_KEY = "epoch"`, `def current_epoch(user: User) -> int`,
  `def epoch_in_session(session: Mapping[str, Any]) -> int | None`,
  `async def bump_session_epoch(db: AsyncSession, user_id: uuid.UUID, *, expected_epoch: int | None = None) -> None`
  (the brief's `bump_session_epoch(db, user_id)` plus one optional keyword; does not commit);
  `src.routers.auth._start_fresh_session(request) -> None`, `_session_user_id(request) -> uuid.UUID | None`.

**Spec deviation, stated:** §6.7 says "Login clears the session and keeps only the vetted
`next`." `_start_fresh_session` also keeps `pending_invite_token`: `GET /invite/{token}`
stores it before sending an anonymous visitor to `/login/start`
(`src/routers/invite.py:85-87`), and `_post_login_redirect` resumes the invite from it
(`src/routers/auth.py:238-241`). Dropping it would break delegate onboarding. The token
is only a lookup key; `/invite/{token}` re-validates it against the database.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_session_epoch.py`:

```python
"""Sessions are revocable server-side and reset at login (spec 2026-10-01 §6.7, A-11)."""
import pytest

from src.database import get_db
from src.dependencies import get_current_user
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.routers import auth as auth_module
from tests import factories
from tests.integration.test_auth_allowlist_gate import _fake_oauth
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration


async def _pi(db_session, **overrides):
    user = await factories.make_user(db_session, user_role=USER_ROLE_PI, **overrides)
    await factories.make_profile(db_session, user=user)
    await db_session.flush()
    return user


async def _callback(client, monkeypatch, orcid: str, session: dict):
    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _fake_oauth(orcid))

    async def _profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _profile)
    return await client.get(
        "/auth/callback?code=c&state=s",
        headers=raw_session_headers({"oauth_state": "s", **session}),
    )


# --- login --------------------------------------------------------------------

async def test_login_clears_the_pre_login_session_and_keeps_the_vetted_next(
    client, db_session, monkeypatch
):
    user = await _pi(db_session)
    r = await _callback(client, monkeypatch, user.orcid, {
        "post_login_redirect": "/profile/edit",
        "impersonate_user_id": str(user.id), "impersonate_expires_at": 9_999_999_999,
        "planted": "by an attacker",
    })
    assert r.status_code == 302 and r.headers["location"] == "/profile/edit"
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 0}


async def test_login_drops_an_unsafe_next(client, db_session, monkeypatch):
    user = await _pi(db_session)
    r = await _callback(client, monkeypatch, user.orcid,
                        {"post_login_redirect": "https://evil.example/"})
    assert r.headers["location"] == "/profile"
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 0}


async def test_login_keeps_a_pending_invite_token(client, db_session, monkeypatch):
    user = await _pi(db_session)
    r = await _callback(client, monkeypatch, user.orcid,
                        {"pending_invite_token": "tok-9", "planted": 1})
    assert r.headers["location"] == "/invite/tok-9"
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 0}


async def test_login_stores_the_accounts_epoch(client, db_session, monkeypatch):
    user = await _pi(db_session, session_epoch=4)
    r = await _callback(client, monkeypatch, user.orcid, {})
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 4}


async def test_a_refused_login_carries_only_pending_access(client, db_session, monkeypatch):
    user = await _pi(db_session, access_status="denied")
    r = await _callback(client, monkeypatch, user.orcid, {"planted": 1})
    assert r.headers["location"] == "/access-pending"
    assert set(session_from_response(r)) == {"pending_access"}


# --- the epoch check ------------------------------------------------------------

async def test_a_session_from_an_older_epoch_is_refused_and_cleared(client, db_session):
    user = await _pi(db_session, session_epoch=1)
    stale = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert stale.status_code == 302 and stale.headers["location"] == "/login?next=%2Fprofile"
    assert session_from_response(stale) == {}
    current = await client.get("/profile", headers=session_headers(user.id, epoch=1))
    assert current.status_code == 200


async def test_a_session_with_no_epoch_key_counts_as_epoch_zero(client, db_session):
    user = await _pi(db_session)
    assert (await client.get("/profile", headers=session_headers(user.id))).status_code == 200
    user.session_epoch = 1
    await db_session.flush()
    refused = await client.get("/profile", headers=session_headers(user.id))
    assert refused.headers["location"] == "/login?next=%2Fprofile"


@pytest.mark.parametrize("epoch", ["0", True, None, 0.0])
async def test_a_malformed_epoch_is_refused(client, db_session, epoch):
    user = await _pi(db_session)
    r = await client.get("/profile", headers=session_headers(user.id, epoch=epoch))
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fprofile"


async def test_a_stale_session_at_login_ends_on_the_login_page(client, db_session):
    user = await _pi(db_session, session_epoch=1)
    stale = session_headers(user.id, epoch=0)
    assert (await client.get("/login", headers=stale)).headers["location"] == "/"
    assert (await client.get("/", headers=stale)).headers["location"] == "/profile"
    bounced = await client.get("/profile", headers=stale)
    assert bounced.headers["location"] == "/login?next=%2Fprofile"
    assert session_from_response(bounced) == {}
    # The browser now holds no session, so the next hop renders the login page.
    assert (await client.get("/login?next=%2Fprofile")).status_code == 200


# --- logout -------------------------------------------------------------------

async def test_logout_signs_out_every_device(client, db_session):
    user = await _pi(db_session)
    device_a = session_headers(user.id, epoch=0)
    device_b = session_headers(user.id, epoch=0)
    assert (await client.get("/profile", headers=device_b)).status_code == 200

    r = await client.post("/logout", headers=device_a)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert session_from_response(r) == {}
    await db_session.refresh(user)
    assert user.session_epoch == 1

    other = await client.get("/profile", headers=device_b)
    assert other.status_code == 302 and other.headers["location"] == "/login?next=%2Fprofile"
    relogin = await client.get("/profile", headers=session_headers(user.id, epoch=1))
    assert relogin.status_code == 200


async def test_a_revoked_session_cannot_sign_out_the_newer_ones(client, db_session):
    user = await _pi(db_session, session_epoch=2)
    r = await client.post("/logout", headers=session_headers(user.id, epoch=0))
    assert r.status_code == 302 and r.headers["location"] == "/login"
    await db_session.refresh(user)
    assert user.session_epoch == 2


async def test_anonymous_logout_still_redirects(client):
    r = await client.post("/logout")
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_logout_takes_no_auth_dependency():
    route = next(r for r in auth_module.router.routes if getattr(r, "path", None) == "/logout")
    calls = {d.call for d in route.dependant.dependencies}
    assert calls == {get_db}, calls
    assert get_current_user not in calls


# --- deny and role change -------------------------------------------------------

async def test_denying_access_bumps_the_epoch(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    user = await _pi(db_session)
    r = await client.post(
        f"/admin/access-requests/{user.id}/deny", headers=session_headers(admin.id)
    )
    assert r.status_code == 302
    await db_session.refresh(user)
    assert (user.access_status, user.session_epoch) == ("denied", 1)


async def test_a_role_change_bumps_the_epoch_and_ends_the_old_session(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    user = await _pi(db_session)
    r = await client.post(
        f"/admin/users/{user.id}/role", data={"user_role": USER_ROLE_MANAGER},
        headers=session_headers(admin.id),
    )
    assert r.status_code == 302
    await db_session.refresh(user)
    assert (user.user_role, user.session_epoch) == (USER_ROLE_MANAGER, 1)
    old = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert old.headers["location"] == "/login?next=%2Fprofile"


async def test_saving_the_same_role_signs_nobody_out(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    user = await _pi(db_session)
    r = await client.post(
        f"/admin/users/{user.id}/role", data={"user_role": USER_ROLE_PI},
        headers=session_headers(admin.id),
    )
    assert r.status_code == 302
    await db_session.refresh(user)
    assert user.session_epoch is None
```
Append to `tests/integration/test_cli.py`, after `test_role_set_round_trips_through_all_roles`:

```python


def test_cli_role_changes_bump_the_session_epoch_only_on_a_change(db, runner):
    """A role change signs the account out everywhere (spec 2026-10-01 §6.7); a no-op
    role:set does not."""
    target_orcid = _orcid("role-epoch")

    async def _seed(session):
        await factories.make_user(
            session, orcid=target_orcid, name="Epoch Target", user_role=USER_ROLE_PI
        )

    db(_seed)

    def _epoch():
        return db(lambda s: _user_by_orcid(s, target_orcid)).session_epoch

    _ok(runner.invoke(cli_app, ["role:set", "--orcid", target_orcid, "--role", "pi"]))
    assert _epoch() is None
    _ok(runner.invoke(cli_app, ["role:set", "--orcid", target_orcid, "--role", "manager"]))
    assert _epoch() == 1
    _ok(runner.invoke(cli_app, ["admin:grant", "--orcid", target_orcid]))
    assert _epoch() == 2
    _ok(runner.invoke(cli_app, ["admin:grant", "--orcid", target_orcid]))
    assert _epoch() == 2
    _ok(runner.invoke(cli_app, ["admin:revoke", "--orcid", target_orcid]))
    assert _epoch() == 3
```

- [ ] **Step 2: Run them; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_session_epoch.py tests/integration/test_cli.py::test_cli_role_changes_bump_the_session_epoch_only_on_a_change -v`
Expected: FAIL — login tests see the planted keys survive and no `epoch`; epoch tests get
200 for stale sessions; `test_logout_signs_out_every_device` sees `session_epoch` None;
`test_logout_takes_no_auth_dependency` sees `set()` (no `get_db`); deny/role/CLI see None.

- [ ] **Step 3: Create `src/services/session_epoch.py`**

```python
"""Server-side revocation for the signed session cookie (spec 2026-10-01 §6.7, A-11).

The session (src/main.py) is a signed, 30-day cookie with no server-side store.
``users.session_epoch`` is the server-side handle on it: login copies the account's
epoch into ``session[SESSION_EPOCH_KEY]``, ``get_current_user`` (src/dependencies.py)
refuses a session whose epoch differs, and bumping the epoch therefore ends every
session the account holds, on every device. Bumped by logout (src/routers/auth.py),
access denial (src/routers/admin/access.py) and a role change
(src/routers/admin/users.py, src/cli.py). NULL counts as 0, and so does a session with
no epoch key.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User

#: The session key holding the epoch the session was issued under.
SESSION_EPOCH_KEY = "epoch"


def current_epoch(user: User) -> int:
    """The account's epoch, NULL read as 0."""
    return user.session_epoch or 0


def epoch_in_session(session: Mapping[str, Any]) -> int | None:
    """The epoch a session was issued under: 0 when the key is absent, None when the
    value is not an integer (bool included), which no session this app wrote holds."""
    value = session.get(SESSION_EPOCH_KEY, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def bump_session_epoch(
    db: AsyncSession, user_id: uuid.UUID, *, expected_epoch: int | None = None
) -> None:
    """Increment ``users.session_epoch`` (NULL as 0), ending every session the account
    holds. Does not commit.

    With ``expected_epoch``, bumps only while the stored epoch still equals it, so a
    session that is already stale cannot end the account's newer sessions.
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(session_epoch=func.coalesce(User.session_epoch, 0) + 1)
    )
    if expected_epoch is not None:
        stmt = stmt.where(func.coalesce(User.session_epoch, 0) == expected_epoch)
    await db.execute(stmt.execution_options(synchronize_session="fetch"))
```

- [ ] **Step 4: Check the epoch in `get_current_user`**

`src/dependencies.py`: after `from src.models import User` add
`from src.services.session_epoch import current_epoch, epoch_in_session`.

Access-block comment, before (lines 83-88):
```python
    # Revocation. Sessions are unkeyed signed cookies with a 30-day max_age and
    # no server-side store, so there is no session to invalidate and
    # `access_status` is the ONLY revocation signal there is. Nothing read it
    # after login, so admin_deny_access set the column and changed nothing a
    # signed-in user could observe: a denied user's GET /profile returned 200
    # for up to thirty more days (E1.2).
```
after:
```python
    # Revocation by access status. Sessions are signed cookies with a 30-day
    # max_age and no server-side store, and nothing read `access_status` after
    # login, so admin_deny_access set the column and changed nothing a signed-in
    # user could observe: a denied user's GET /profile returned 200 for up to
    # thirty more days (E1.2). The session-epoch check below is the other
    # revocation signal.
```
Before:
```python
    # Impersonation: admin can view as another user
    impersonated = await _impersonated_user(request, db, session_user)
```
after:
```python
    # Revocation by session epoch (spec 2026-10-01 §6.7). Login copies
    # users.session_epoch into the session; logout, access denial and a role
    # change bump it, so a session holding any other epoch is over, on every
    # device. After the access check above, so a denied user still lands on
    # /access-pending; before impersonation, because the epoch belongs to the
    # account that holds the session.
    if epoch_in_session(request.session) != current_epoch(session_user):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": _login_location(request)},
        )

    # Impersonation: admin can view as another user
    impersonated = await _impersonated_user(request, db, session_user)
```

- [ ] **Step 5: Reset at login, store the epoch, bump at logout**

`src/routers/auth.py` imports, before:
```python
import logging
from datetime import datetime, timezone
```
after:
```python
import logging
import uuid
from datetime import datetime, timezone
```
and before:
```python
from src.services.profile_jobs import enqueue_profile_job_if_absent
from src.services.user_email import assign_user_email
```
after:
```python
from src.services.profile_jobs import enqueue_profile_job_if_absent
from src.services.session_epoch import (
    SESSION_EPOCH_KEY,
    bump_session_epoch,
    current_epoch,
    epoch_in_session,
)
from src.services.user_email import assign_user_email
```
Before (line 28):
```python
router = APIRouter()
```
after:
```python
router = APIRouter()
# Module-level dependency singleton (no Depends(...) in an argument default; the
# src/ lint ratchet counts each one).
_DB = Depends(get_db)
```
After `pop_post_login_redirect` (after its `return` line), add:
```python


def _start_fresh_session(request: Request) -> None:
    """Clear the session at login, keeping only what the login itself must carry.

    Nothing from a session that predates the login — an anonymous visitor's, or one
    planted by someone else (session fixation, A-11) — reaches the signed-in session:
    the post-login destination is re-stored only if it passes is_safe_next_url, and a
    pending delegate-invite token survives so the invite flow (src/routers/invite.py)
    can resume after ORCID. Every other key, an impersonation included, is dropped.
    """
    next_url = pop_post_login_redirect(request)
    pending_token = request.session.get("pending_invite_token")
    request.session.clear()
    if next_url:
        request.session[POST_LOGIN_KEY] = next_url
    if isinstance(pending_token, str) and pending_token:
        request.session["pending_invite_token"] = pending_token
```
`_post_login_redirect`, before:
```python
    # Set session
    request.session["user_id"] = str(user.id)
    request.session.pop("pending_access", None)
```
after:
```python
    # Set session. The epoch is what get_current_user compares with
    # users.session_epoch on every request (spec 2026-10-01 §6.7).
    request.session["user_id"] = str(user.id)
    request.session[SESSION_EPOCH_KEY] = current_epoch(user)
    request.session.pop("pending_access", None)
```
`auth_callback`, before:
```python
    await db.commit()

    # Access gate: users who aren't allowed do not get a session
```
after:
```python
    await db.commit()

    # Session fixation (A-11): nothing from the pre-login session survives the
    # login except what _start_fresh_session keeps.
    _start_fresh_session(request)

    # Access gate: users who aren't allowed do not get a session
```
Logout (as left by 1B-4), before:
```python
@router.post("/logout")
async def logout(request: Request):
    """Clear session and redirect to login.

    POST-only: logout mutates session state, so exposing it over GET made it a
    cross-site request-forgery target (a third-party page could log a victim
    out via an <img>/<a> to /logout). SameSite=lax on the session cookie blocks
    forged cross-site POSTs but not same-site ones from a sibling subdomain;
    OriginGuardMiddleware (src/main.py) refuses those. The "Sign out" control
    posts this form (see base.html). (SEC-8)
    """
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
```
after:
```python
def _session_user_id(request: Request) -> uuid.UUID | None:
    """``session["user_id"]`` as a UUID; None when absent or malformed."""
    raw = request.session.get("user_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = _DB):
    """Sign out on every device and redirect to login.

    POST-only: logout mutates session state, so exposing it over GET made it a
    cross-site request-forgery target (a third-party page could log a victim
    out via an <img>/<a> to /logout). SameSite=lax on the session cookie blocks
    forged cross-site POSTs but not same-site ones from a sibling subdomain;
    OriginGuardMiddleware (src/main.py) refuses those. The "Sign out" control
    posts this form (see base.html). (SEC-8)

    Bumping ``users.session_epoch`` makes get_current_user refuse every other
    session of the account (spec 2026-10-01 §6.7). The account comes from
    ``session["user_id"]`` directly, never from get_current_user: a denied user is
    bounced from every authenticated route to /access-pending and must still be
    able to sign out, and an auth dependency here would turn that bounce into a
    loop (tests/integration/test_access_revocation.py). Only a session still
    holding the current epoch bumps it, so replaying an already-revoked cookie
    cannot sign the account out of its newer sessions.
    """
    user_id = _session_user_id(request)
    epoch = epoch_in_session(request.session)
    if user_id is not None and epoch is not None:
        await bump_session_epoch(db, user_id, expected_epoch=epoch)
        await db.commit()
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
```

- [ ] **Step 6: Bump on deny and on a role change**

`src/routers/admin/access.py`: after `from src.services.profile_jobs import enqueue_profile_job_if_absent`
add `from src.services.session_epoch import bump_session_epoch`. Before (lines 112-113):
```python
    user.access_status = "denied"
    await db.commit()
```
after:
```python
    user.access_status = "denied"
    # Ends every session the user holds (spec 2026-10-01 §6.7). get_current_user's
    # access check already bounces them; the bump keeps a later re-allow from
    # reviving those sessions.
    await bump_session_epoch(db, user.id)
    await db.commit()
```
`src/routers/admin/users.py`: after `from src.services.directory import list_pi_directory, load_user_detail`
add `from src.services.session_epoch import bump_session_epoch`. Before (lines 175-177):
```python
    previous = user.user_role
    user.user_role = user_role
    await db.commit()
```
after:
```python
    previous = user.user_role
    user.user_role = user_role
    if previous != user_role:
        # A role change signs the account out everywhere (spec 2026-10-01 §6.7).
        await bump_session_epoch(db, user.id)
    await db.commit()
```
`src/cli.py` `admin_grant._grant`, before:
```python
        from src.models import USER_ROLE_ADMIN, User
        engine, factory = await _get_db()
        try:
            async with factory() as db:
                result = await db.execute(select(User).where(User.orcid == orcid))
                user = result.scalar_one_or_none()
                if not user:
                    console.print(f"[red]User with ORCID {orcid} not found[/red]")
                    return False
                user.user_role = USER_ROLE_ADMIN
                await db.commit()
```
after:
```python
        from src.models import USER_ROLE_ADMIN, User
        from src.services.session_epoch import bump_session_epoch
        engine, factory = await _get_db()
        try:
            async with factory() as db:
                result = await db.execute(select(User).where(User.orcid == orcid))
                user = result.scalar_one_or_none()
                if not user:
                    console.print(f"[red]User with ORCID {orcid} not found[/red]")
                    return False
                changed = user.user_role != USER_ROLE_ADMIN
                user.user_role = USER_ROLE_ADMIN
                if changed:
                    await bump_session_epoch(db, user.id)
                await db.commit()
```
`admin_revoke._revoke`, before:
```python
        from src.models import USER_ROLE_ADMIN, USER_ROLE_PI, User
```
after:
```python
        from src.models import USER_ROLE_ADMIN, USER_ROLE_PI, User
        from src.services.session_epoch import bump_session_epoch
```
and before:
```python
                user.user_role = USER_ROLE_PI
                await db.commit()
```
after:
```python
                user.user_role = USER_ROLE_PI
                await bump_session_epoch(db, user.id)
                await db.commit()
```
`role_set._set`, before:
```python
        from src.models import VALID_USER_ROLES, User
```
after:
```python
        from src.models import VALID_USER_ROLES, User
        from src.services.session_epoch import bump_session_epoch
```
and before:
```python
                user.user_role = role
                await db.commit()
```
after:
```python
                changed = user.user_role != role
                user.user_role = role
                if changed:
                    await bump_session_epoch(db, user.id)
                await db.commit()
```

- [ ] **Step 7: Run the new and the guarding tests; expect pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_session_epoch.py tests/integration/test_access_revocation.py tests/integration/test_origin_guard.py tests/integration/test_auth_allowlist_gate.py tests/integration/test_user_email.py tests/integration/test_onboarding_flow.py tests/integration/test_cli.py tests/integration/test_role_appointment.py tests/characterization/test_auth_and_admin_routes.py tests/unit/test_login_redirect.py tests/unit/test_mutation_harness_targets.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/services/session_epoch.py src/dependencies.py src/routers/auth.py \
  src/routers/admin/access.py src/routers/admin/users.py src/cli.py \
  tests/integration/test_session_epoch.py tests/integration/test_cli.py
git commit -m "fix(webui-1B): session epoch revokes sessions; login resets the session (A-11)

Login keeps only the vetted next and a pending invite token, and stores
users.session_epoch; get_current_user refuses any other epoch. Logout
bumps it from session[\"user_id\"] with no auth dependency, as do access
denial and a role change (web and CLI).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-6: Any address change clears the verification

**Files:**
- Modify: `src/services/user_email.py:1-14` (docstring), `:29-63` (`assign_user_email`)
- Modify: `tests/integration/test_user_email.py` (imports, append tests)

**Interfaces:**
- Consumes: `User.email_verified_at` (1B-1); `auth_headers` (1B-4).
- Produces: `assign_user_email(db, user, email) -> bool` — unchanged signature; now clears
  `user.email_verified_at` when the address changes case-insensitively, or is cleared.
  Every writer of `users.email` goes through it (`tests/unit/test_email_writer_tripwire.py`):
  `src/routers/auth.py:165, 212` (ORCID login), `src/services/profile_edit.py:117`
  (`/profile/save`, `/onboarding/save-profile`, `/manager/pis/{id}/profile`,
  `/agent/{id}/public-profile/save`), `src/services/pi_onboarding.py:86`, `src/cli.py:71`.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_user_email.py`, before:
```python
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, User
```
after:
```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, User
```
Append:
```python


# --- verification follows the address (spec 2026-10-01 §6.6) --------------------

async def test_a_changed_address_loses_its_verification(db_session):
    user = await factories.make_user(
        db_session, email="old@example.edu", email_verified_at=datetime.now(UTC)
    )
    assert await assign_user_email(db_session, user, "new@example.edu") is True
    assert (user.email, user.email_verified_at) == ("new@example.edu", None)


async def test_a_case_only_change_keeps_the_verification(db_session):
    stamp = datetime.now(UTC)
    user = await factories.make_user(db_session, email="Mixed@Example.edu", email_verified_at=stamp)
    assert await assign_user_email(db_session, user, "mixed@example.edu") is True
    assert (user.email, user.email_verified_at) == ("mixed@example.edu", stamp)


async def test_clearing_the_address_clears_the_verification(db_session):
    user = await factories.make_user(
        db_session, email="gone@example.edu", email_verified_at=datetime.now(UTC)
    )
    assert await assign_user_email(db_session, user, None) is True
    assert (user.email, user.email_verified_at) == (None, None)


async def test_a_refused_change_keeps_address_and_verification(db_session):
    stamp = datetime.now(UTC)
    await factories.make_user(db_session, email="held@example.edu")
    user = await factories.make_user(db_session, email="mine@example.edu", email_verified_at=stamp)
    assert await assign_user_email(db_session, user, "HELD@example.edu") is False
    assert (user.email, user.email_verified_at) == ("mine@example.edu", stamp)


async def test_every_address_form_clears_the_verification(client, db_session):
    stamp = datetime.now(UTC)
    pi = await factories.make_user(db_session, email="a1@example.edu", email_verified_at=stamp)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    form = {
        "name": pi.name, "institution": "", "department": "", "research_summary": "",
        "techniques": "", "experimental_models": "", "disease_areas": "", "key_targets": "",
        "keywords": "",
    }

    r = await client.post("/profile/save", data={**form, "email": "a2@example.edu"},
                          headers=auth_headers(pi.id))
    assert r.headers["location"] == "/profile?saved=1"
    await db_session.refresh(pi)
    assert (pi.email, pi.email_verified_at) == ("a2@example.edu", None)

    pi.email_verified_at = stamp
    await db_session.flush()
    r = await client.post(f"/manager/pis/{pi.id}/profile", data={**form, "email": "a3@example.edu"},
                          headers=auth_headers(mgr.id))
    assert r.headers["location"] == f"/manager/pis/{pi.id}?saved=1"
    await db_session.refresh(pi)
    assert (pi.email, pi.email_verified_at) == ("a3@example.edu", None)

    pi.email_verified_at = stamp
    await db_session.flush()
    await client.post("/onboarding/save-profile",
                      data={"email": "a4@example.edu", "research_summary": "# Mine"},
                      headers=auth_headers(pi.id))
    await db_session.refresh(pi)
    assert (pi.email, pi.email_verified_at) == ("a4@example.edu", None)
```

- [ ] **Step 2: Run them; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_user_email.py -v`
Expected: the four tests that expect `None` FAIL (the stamp survives); the case-only and
refused tests PASS already.

- [ ] **Step 3: Clear the stamp in the one writer**

`src/services/user_email.py` docstring, before (lines 12-14):
```python
user holds in ANY case, and confines a racing unique violation to a savepoint.
tests/unit/test_email_writer_tripwire.py fails on any other ``users.email``
write in src/.
"""
```
after:
```python
user holds in ANY case, and confines a racing unique violation to a savepoint.
tests/unit/test_email_writer_tripwire.py fails on any other ``users.email``
write in src/.

It also owns the rule that verification follows the address (spec 2026-10-01
§6.6): ``users.email_verified_at`` is cleared whenever the address changes,
compared case-insensitively as invitation acceptance compares it, and when it is
cleared. A refused assignment changes neither.
"""
```
Function, before:
```python
    """Set ``user.email``; False — nothing changed — when another user holds it.

    ``None`` clears the address and always succeeds. ``user`` must already be
    flushed (it needs an id). Never raises on a conflict: the login path must be
    able to proceed without an email.
    """
    if email is None:
        user.email = None
        return True
```
after:
```python
    """Set ``user.email``; False — nothing changed — when another user holds it.

    ``None`` clears the address and always succeeds. ``user`` must already be
    flushed (it needs an id). Never raises on a conflict: the login path must be
    able to proceed without an email. A different address (case-insensitively), or
    none, also clears ``user.email_verified_at``.
    """
    if email is None:
        user.email = None
        user.email_verified_at = None
        return True
```
and before:
```python
    user_id = user.id
    try:
        async with db.begin_nested():
            user.email = email
            await db.flush()
```
after:
```python
    user_id = user.id
    same_address = (user.email or "").lower() == email.lower()
    try:
        async with db.begin_nested():
            user.email = email
            if not same_address:
                user.email_verified_at = None
            await db.flush()
```

- [ ] **Step 4: Run; expect pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_user_email.py tests/unit/test_email_writer_tripwire.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/user_email.py tests/integration/test_user_email.py
git commit -m "fix(webui-1B): any users.email change clears email_verified_at (A-04)

The one writer clears the stamp on a case-insensitive change or a clear;
a case-only rewrite or a refused assignment keeps it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-7: Admin and manager verify-email routes

**Files:**
- Create: `src/services/email_verification.py`
- Modify: `src/routers/admin/users.py:12, 14, 16` (imports), append after `admin_set_user_role`
- Modify: `src/routers/manager.py:52` (import), after line 77 (import), `:131-142` (docstring phrase), after `manager_unmute_pi` (line 387)
- Modify: `templates/admin/user_detail.html:17-20`
- Modify: `templates/manager/pi_detail.html:106, 121-124`
- Modify: `tests/integration/test_manager_views.py:55-76`
- Test: `tests/integration/test_email_verification.py`

**Interfaces:**
- Consumes: `refuse_impersonation` (`src/dependencies.py`), `AdminAuditEvent` (`src/models`),
  `_DB`, `_ADMIN` (`src/routers/admin/_common.py`), `_DB`, `_STAFF` (`src/routers/manager.py`).
- Produces: `src.services.email_verification.VERIFY_EMAIL_ACTION = "verify_email"`,
  `async def mark_email_verified(db: AsyncSession, *, target: User, actor: User) -> str | None`
  (`"no_email"` or None; commits); routes `POST /admin/users/{user_id}/verify-email`
  (`admin_verify_user_email`) and `POST /manager/pis/{user_id}/verify-email`
  (`manager_verify_pi_email`), each redirecting with `?email_verified=1` or `?error=no_email`.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_email_verification.py`:

```python
"""Only an administrator (any user) or a manager (PIs) verifies an address, never under
impersonation, always with an audit event (spec 2026-10-01 §6.6, D7)."""
import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AdminAuditEvent,
)
from src.services.email_verification import VERIFY_EMAIL_ACTION
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _events(db_session):
    return (await db_session.execute(
        select(AdminAuditEvent).where(AdminAuditEvent.action == VERIFY_EMAIL_ACTION)
    )).scalars().all()


async def _user(db_session, role, **overrides):
    user = await factories.make_user(db_session, user_role=role, **overrides)
    await db_session.flush()
    return user


# --- admin route ----------------------------------------------------------------

async def test_an_admin_verifies_any_users_address(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_REVIEWER, email="rev@example.edu")
    r = await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(admin.id))
    assert r.status_code == 302
    assert r.headers["location"] == f"/admin/users/{target.id}?email_verified=1"
    await db_session.refresh(target)
    assert target.email_verified_at is not None
    assert [(e.actor_user_id, e.payload) for e in await _events(db_session)] == [
        (admin.id, {"user_id": str(target.id), "email": "rev@example.edu"})
    ]


async def test_the_admin_route_refuses_under_impersonation(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    other_admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/admin/users/{target.id}/verify-email",
                          headers=auth_headers(admin.id, impersonate=other_admin.id))
    assert r.status_code == 403
    await db_session.refresh(target)
    assert target.email_verified_at is None and await _events(db_session) == []


async def test_an_account_without_an_address_is_not_verified(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_PI, email=None)
    r = await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(admin.id))
    assert r.headers["location"] == f"/admin/users/{target.id}?error=no_email"
    await db_session.refresh(target)
    assert target.email_verified_at is None and await _events(db_session) == []


async def test_the_admin_route_is_admin_only(client, db_session):
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    target = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(mgr.id))
    assert r.status_code == 403
    admin = await _user(db_session, USER_ROLE_ADMIN)
    missing = await client.post(
        "/admin/users/00000000-0000-0000-0000-000000000000/verify-email",
        headers=auth_headers(admin.id),
    )
    assert missing.status_code == 404


# --- manager route --------------------------------------------------------------

async def test_a_manager_verifies_a_pis_address(client, db_session):
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    pi = await _user(db_session, USER_ROLE_PI, email="pi@example.edu")
    r = await client.post(f"/manager/pis/{pi.id}/verify-email", headers=auth_headers(mgr.id))
    assert r.headers["location"] == f"/manager/pis/{pi.id}?email_verified=1"
    await db_session.refresh(pi)
    assert pi.email_verified_at is not None
    assert [e.actor_user_id for e in await _events(db_session)] == [mgr.id]


@pytest.mark.parametrize("role", [USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_a_manager_cannot_verify_a_non_pi(client, db_session, role):
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    target = await _user(db_session, role)
    r = await client.post(f"/manager/pis/{target.id}/verify-email", headers=auth_headers(mgr.id))
    assert r.status_code == 404
    await db_session.refresh(target)
    assert target.email_verified_at is None


@pytest.mark.parametrize("role", [USER_ROLE_PI, USER_ROLE_REVIEWER])
async def test_the_manager_route_refuses_non_staff(client, db_session, role):
    caller = await _user(db_session, role)
    pi = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/manager/pis/{pi.id}/verify-email", headers=auth_headers(caller.id))
    assert r.status_code == 403


async def test_the_manager_route_refuses_under_impersonation(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    pi = await _user(db_session, USER_ROLE_PI)
    r = await client.post(f"/manager/pis/{pi.id}/verify-email",
                          headers=auth_headers(admin.id, impersonate=mgr.id))
    assert r.status_code == 403
    await db_session.refresh(pi)
    assert pi.email_verified_at is None and await _events(db_session) == []


# --- the controls ----------------------------------------------------------------

async def test_the_admin_page_offers_verification_until_it_is_done(client, db_session):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    target = await _user(db_session, USER_ROLE_PI, email="ctl@example.edu")
    action = f'action="/admin/users/{target.id}/verify-email"'
    page = (await client.get(f"/admin/users/{target.id}", headers=auth_headers(admin.id))).text
    assert action in page and "Unverified" in page
    await client.post(f"/admin/users/{target.id}/verify-email", headers=auth_headers(admin.id))
    page = (await client.get(f"/admin/users/{target.id}", headers=auth_headers(admin.id))).text
    assert action not in page and "Verified " in page


async def test_the_manager_page_offers_verification_only_to_staff_not_impersonating(
    client, db_session
):
    admin = await _user(db_session, USER_ROLE_ADMIN)
    mgr = await _user(db_session, USER_ROLE_MANAGER)
    rev = await _user(db_session, USER_ROLE_REVIEWER)
    pi = await _user(db_session, USER_ROLE_PI, email="ctl2@example.edu")
    action = f'action="/manager/pis/{pi.id}/verify-email"'
    assert action in (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert action not in (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(rev.id))).text
    imp = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(admin.id, impersonate=mgr.id))
    assert imp.status_code == 200 and action not in imp.text
```

- [ ] **Step 2: Run them; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_email_verification.py -v`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'src.services.email_verification'`.

- [ ] **Step 3: Create the service**

Create `src/services/email_verification.py`:

```python
"""Verifying ``users.email`` (spec 2026-10-01 §6.6, decision D7).

An administrator may verify any user's address and a manager a PI's; there is no
verification email. ``users.email_verified_at`` is cleared by
``src/services/user_email.py::assign_user_email`` whenever the address changes, and
delegate-invitation acceptance (src/routers/invite.py) requires it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AdminAuditEvent, User

#: ``admin_audit_events.action`` recorded for a verification.
VERIFY_EMAIL_ACTION = "verify_email"


async def mark_email_verified(db: AsyncSession, *, target: User, actor: User) -> str | None:
    """Stamp ``target.email_verified_at``, record who vouched for it, and commit.

    Returns ``"no_email"`` and writes nothing when the target has no address.
    Verifying an already verified address re-stamps it and records another event. The
    payload names the address, because it is that address that was vouched for and a
    later change clears the stamp.
    """
    if not target.email:
        return "no_email"
    target.email_verified_at = datetime.now(UTC)
    db.add(AdminAuditEvent(
        action=VERIFY_EMAIL_ACTION,
        actor_user_id=actor.id,
        payload={"user_id": str(target.id), "email": target.email},
    ))
    await db.commit()
    return None
```

- [ ] **Step 4: Add the admin route**

`src/routers/admin/users.py` imports, before:
```python
from src.routers.admin._common import _template_context, router, templates
from src.services.admin_invariant import LastAdminError, ensure_admin_remains
from src.services.directory import list_pi_directory, load_user_detail
```
after (the `session_epoch` line was added by 1B-5):
```python
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services.admin_invariant import LastAdminError, ensure_admin_remains
from src.services.directory import list_pi_directory, load_user_detail
from src.services.email_verification import mark_email_verified
```
Append at the end of the module:
```python



@router.post("/users/{user_id}/verify-email")
async def admin_verify_user_email(
    user_id: uuid.UUID,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Mark a user's email address verified (spec 2026-10-01 §6.6; any user).

    Refused under impersonation: the audit event must name the admin who vouched
    for the address, and under impersonation ``current_user`` is someone else.
    """
    refuse_impersonation(current_user, "Email verification is disabled while impersonating.")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    error = await mark_email_verified(db, target=user, actor=current_user)
    if error:
        return RedirectResponse(url=f"/admin/users/{user_id}?error={error}", status_code=302)
    logger.info("Admin %s verified the email address of user %s", current_user.id, user_id)
    return RedirectResponse(url=f"/admin/users/{user_id}?email_verified=1", status_code=302)
```

- [ ] **Step 5: Add the manager route**

`src/routers/manager.py` line 52, before
`from src.dependencies import get_review_user, get_staff_user`, after
`from src.dependencies import get_review_user, get_staff_user, refuse_impersonation`.
After the `from src.services.directory import (...)` block (ends line 77) add
`from src.services.email_verification import mark_email_verified`.
In `_template_context`'s docstring, before:
```python
    because an admin CAN impersonate a reviewer (the impersonate cookie
    carries no role restriction), and under impersonation this dict's
```
after:
```python
    because an admin CAN impersonate a reviewer (impersonation carries no
    role restriction), and under impersonation this dict's
```
After `manager_unmute_pi` (after its `return await _manager_set_mute(user_id, db, current_user, muted=False)`), add:
```python


@router.post("/pis/{user_id}/verify-email")
async def manager_verify_pi_email(
    user_id: uuid.UUID, db: AsyncSession = _DB, current_user: User = _STAFF,
):
    """Mark a PI's email address verified (spec 2026-10-01 §6.6).

    PI targets only: staff and reviewer addresses are verified by an admin on
    /admin/users/{id}. Refused under impersonation, so the audit event names the
    staff member who actually vouched for the address — one of the few manager
    controls hidden while impersonating.
    """
    refuse_impersonation(current_user, "Email verification is disabled while impersonating.")
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None or target.user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
    error = await mark_email_verified(db, target=target, actor=current_user)
    if error:
        return RedirectResponse(url=f"/manager/pis/{user_id}?error={error}", status_code=302)
    logger.info("Staff user %s verified the email address of PI %s", current_user.id, user_id)
    return RedirectResponse(url=f"/manager/pis/{user_id}?email_verified=1", status_code=302)
```
`tests/integration/test_manager_views.py`, before (lines 55-76):
```python
def test_manager_router_mutations_are_an_explicit_allowlist():
    """D12 amended, not abolished (design decision D1): the manager router may
    have non-GET routes now, but only these eight, named exactly. A future
    accidental ninth write route still fails this test loudly. The two
```
after:
```python
def test_manager_router_mutations_are_an_explicit_allowlist():
    """D12 amended, not abolished (design decision D1): the manager router may
    have non-GET routes now, but only these nine, named exactly. A future
    accidental tenth write route still fails this test loudly. The two
```
and before:
```python
    one industry-evidence row as "not this PI / not industry".
    """
    allowed_post_paths = {
```
after:
```python
    one industry-evidence row as "not this PI / not industry". The email
    verification joined 2026-10-01 (web UI remediation spec §6.6): a manager
    may vouch for a PI's address, which delegate-invitation acceptance requires.
    """
    allowed_post_paths = {
```
and before:
```python
        "/pis/{user_id}/industry/{evidence_id}/veto",
    }
```
after:
```python
        "/pis/{user_id}/industry/{evidence_id}/veto",
        "/pis/{user_id}/verify-email",
    }
```

- [ ] **Step 6: Add the controls to both detail pages**

`templates/admin/user_detail.html`, before (lines 17-20):
```html
            <div>
                <dt class="text-gray-500">Email</dt>
                <dd class="font-medium">{{ target_user.email or '—' }}</dd>
            </div>
```
after:
```html
            <div>
                <dt class="text-gray-500">Email</dt>
                <dd class="font-medium">{{ target_user.email or '—' }}</dd>
                {% if target_user.email %}
                <dd class="text-xs mt-1">
                    {% if target_user.email_verified_at %}
                    <span class="text-green-700">Verified {{ target_user.email_verified_at.strftime('%b %d, %Y') }}</span>
                    {% else %}
                    <span class="text-amber-700">Unverified</span>
                    {% if not impersonation_banner %}
                    <form method="POST" action="/admin/users/{{ target_user.id }}/verify-email" class="inline ml-2">
                        <button type="submit" class="text-indigo-600 hover:underline">Mark verified</button>
                    </form>
                    {% endif %}
                    {% endif %}
                </dd>
                {% endif %}
                {% if request.query_params.get('email_verified') %}
                <dd class="text-xs text-green-700 mt-1">Email address marked verified.</dd>
                {% elif request.query_params.get('error') == 'no_email' %}
                <dd class="text-xs text-red-700 mt-1">This account has no email address to verify.</dd>
                {% endif %}
            </div>
```
`templates/manager/pi_detail.html`, before (line 106):
```html
            {% elif request.query_params.get('error') == 'email_taken' %}That email address is already in use by another account.
```
after:
```html
            {% elif request.query_params.get('error') == 'email_taken' %}That email address is already in use by another account.
            {% elif request.query_params.get('error') == 'no_email' %}This account has no email address to verify.
```
and before (lines 121-124):
```html
            <div>
                <dt class="text-gray-500">Email</dt>
                <dd class="font-medium">{{ target_user.email or '—' }}</dd>
            </div>
```
after:
```html
            <div>
                <dt class="text-gray-500">Email</dt>
                <dd class="font-medium">{{ target_user.email or '—' }}</dd>
                {% if target_user.email %}
                <dd class="text-xs mt-1">
                    {% if target_user.email_verified_at %}
                    <span class="text-green-700">Verified {{ target_user.email_verified_at.strftime('%b %d, %Y') }}</span>
                    {% else %}
                    <span class="text-amber-700">Unverified</span>
                    {% if effective_user.is_staff and not impersonation_banner %}
                    <form method="post" action="/manager/pis/{{ target_user.id }}/verify-email" class="inline ml-2">
                        <button type="submit" class="text-indigo-600 hover:underline">Mark verified</button>
                    </form>
                    {% endif %}
                    {% endif %}
                </dd>
                {% endif %}
                {% if request.query_params.get('email_verified') %}
                <dd class="text-xs text-green-700 mt-1">Email address marked verified.</dd>
                {% endif %}
            </div>
```

- [ ] **Step 7: Run; expect pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_email_verification.py tests/integration/test_manager_views.py tests/unit/test_reachability.py tests/integration/test_reviewer_role.py -v`
Expected: PASS (both routes are credited by the `action` attributes in reachable templates).

- [ ] **Step 8: Commit**

```bash
git add src/services/email_verification.py src/routers/admin/users.py src/routers/manager.py \
  templates/admin/user_detail.html templates/manager/pi_detail.html \
  tests/integration/test_manager_views.py tests/integration/test_email_verification.py
git commit -m "feat(webui-1B): admins and managers verify email addresses (A-04, D7)

POST /admin/users/{id}/verify-email (any user) and
POST /manager/pis/{id}/verify-email (PIs only), both refused under
impersonation and audited as admin_audit_events action verify_email.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-8: Invite acceptance requires a PI account and a verified matching address

**Files:**
- Modify: `src/routers/invite.py:13-14` (imports), `:20-23` (messages), after `:36` (`_invite_refusal`), `:83-108` (GET), `:159-166` (POST), `:175-187` (`_accept_invitation`)
- Modify: `tests/integration/test_agent_page.py:21-27` (imports), `:258-260`, `:486-487`, `:552`
- Modify: `tests/integration/test_double_submits.py:22`
- Modify: `tests/unit/test_invite_email_binding.py` (imports, append)
- Test: `tests/integration/test_invite_verification.py`

**Interfaces:**
- Consumes: `get_current_user`, `refuse_impersonation` (`src/dependencies.py`);
  `User.may_use_pi_surfaces`; `User.email_verified_at` (1B-1); `session_headers`,
  `session_from_response` (1B-3/1B-4); `auth_headers(..., impersonate=)` (1B-4); epoch (1B-5).
- Produces: `src.routers.invite._invite_refusal(invitation: DelegateInvitation, user: User) -> str | None`;
  constants `_INVITE_UNVERIFIED_MSG`, `_INVITE_NOT_PI_MSG`, `_INVITE_IMPERSONATION_DETAIL`.
  `_invite_matches_user` is unchanged and still used by `_invite_refusal`.

Resolution order (spec §6.6 "resolves the user through `get_current_user`"): an anonymous
visitor still stores `pending_invite_token` and goes to `/login/start` (calling
`get_current_user` there would send them to `/login` and lose the token); a signed-in
visitor is resolved by calling `get_current_user(request, db)`, whose 302s (access
pending/denied, stale epoch, unknown user) propagate as on any page. Impersonation is
refused: before this change both routes read the raw session (the real admin), and
resolving through `get_current_user` would otherwise let an admin accept as the
impersonated PI.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_invite_verification.py`:

```python
"""Invite acceptance (spec 2026-10-01 §6.6; A-04, A-09, D-06): the user comes from
get_current_user, must be a PI-surface account, and must hold the invited address,
verified."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AgentDelegate,
    DelegateInvitation,
)
from src.routers.invite import _INVITE_NOT_PI_MSG, _INVITE_UNVERIFIED_MSG
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


async def _invitation(db_session, email: str):
    inviter = await factories.make_user(db_session, name="Inviting PI")
    agent = await factories.make_agent(db_session, user=inviter)
    token = uuid.uuid4().hex
    db_session.add(DelegateInvitation(
        agent_registry_id=agent.id, invited_by_user_id=inviter.id, email=email, token=token,
        status="pending", expires_at=datetime.now(UTC) + timedelta(days=1),
    ))
    await db_session.flush()
    return agent, token


async def _delegates(db_session, agent):
    return (await db_session.execute(
        select(AgentDelegate.user_id).where(AgentDelegate.agent_registry_id == agent.id)
    )).scalars().all()


async def test_a_verified_pi_holding_the_invited_address_accepts(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org",
                                    email_verified_at=datetime.now(UTC))
    page = await client.get(f"/invite/{token}", headers=auth_headers(dee.id))
    assert page.status_code == 200 and f'action="/invite/{token}/accept"' in page.text
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(dee.id))
    assert r.status_code == 302 and r.headers["location"] == f"/agent/{agent.agent_id}/dashboard"
    assert await _delegates(db_session, agent) == [dee.id]


async def test_the_address_match_is_case_insensitive(client, db_session):
    agent, token = await _invitation(db_session, "Dee@Example.org")
    dee = await factories.make_user(db_session, email="dee@example.org",
                                    email_verified_at=datetime.now(UTC))
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(dee.id))
    assert r.status_code == 302
    assert await _delegates(db_session, agent) == [dee.id]


async def test_an_unverified_address_is_refused(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org")
    page = await client.get(f"/invite/{token}", headers=auth_headers(dee.id))
    assert page.status_code == 200 and _INVITE_UNVERIFIED_MSG in page.text
    assert f'action="/invite/{token}/accept"' not in page.text
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(dee.id))
    assert r.status_code == 200 and _INVITE_UNVERIFIED_MSG in r.text
    assert await _delegates(db_session, agent) == []


async def test_a_different_address_is_still_refused(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    other = await factories.make_user(db_session, email="other@example.org",
                                      email_verified_at=datetime.now(UTC))
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(other.id))
    assert r.status_code == 200 and "different email address" in r.text
    assert await _delegates(db_session, agent) == []


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_a_staff_or_reviewer_account_cannot_accept(client, db_session, role):
    agent, token = await _invitation(db_session, "staff@example.org")
    user = await factories.make_user(db_session, user_role=role, email="staff@example.org",
                                     email_verified_at=datetime.now(UTC))
    page = await client.get(f"/invite/{token}", headers=auth_headers(user.id))
    assert _INVITE_NOT_PI_MSG in page.text
    r = await client.post(f"/invite/{token}/accept", headers=auth_headers(user.id))
    assert _INVITE_NOT_PI_MSG in r.text
    assert await _delegates(db_session, agent) == []


async def test_a_denied_account_is_bounced_like_any_page(client, db_session):
    _agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org", access_status="denied",
                                    email_verified_at=datetime.now(UTC))
    r = await client.get(f"/invite/{token}", headers=auth_headers(dee.id))
    assert r.status_code == 302 and r.headers["location"] == "/access-pending"


async def test_a_signed_out_session_goes_to_login_and_comes_back(client, db_session):
    _agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, email="dee@example.org", session_epoch=1,
                                    email_verified_at=datetime.now(UTC))
    r = await client.get(f"/invite/{token}", headers=session_headers(dee.id, epoch=0))
    assert r.status_code == 302
    assert r.headers["location"] == f"/login?next=%2Finvite%2F{token}"


async def test_an_impersonating_admin_cannot_accept_for_the_pi(client, db_session):
    agent, token = await _invitation(db_session, "dee@example.org")
    dee = await factories.make_user(db_session, user_role=USER_ROLE_PI, email="dee@example.org",
                                    email_verified_at=datetime.now(UTC))
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    headers = auth_headers(admin.id, impersonate=dee.id)
    assert (await client.get(f"/invite/{token}", headers=headers)).status_code == 403
    assert (await client.post(f"/invite/{token}/accept", headers=headers)).status_code == 403
    assert await _delegates(db_session, agent) == []


async def test_an_anonymous_visitor_is_sent_to_sign_in_with_the_token_kept(client, db_session):
    _agent, token = await _invitation(db_session, "dee@example.org")
    r = await client.get(f"/invite/{token}")
    assert r.status_code == 302 and r.headers["location"] == "/login/start"
    assert session_from_response(r) == {"pending_invite_token": token}
```
Append to `tests/unit/test_invite_email_binding.py` (and add
`from src.routers.invite import _INVITE_EMAIL_MISMATCH_MSG, _INVITE_NOT_PI_MSG, _INVITE_UNVERIFIED_MSG, _invite_refusal`
to its imports, plus `from src.models.user import USER_ROLE_PI, USER_ROLE_REVIEWER, User` in place of
`from src.models.user import User`):

```python


def _account(email, *, role=USER_ROLE_PI, verified=True):
    return User(orcid="0000-0000-0000-0001", name="Y", email=email, user_role=role,
                email_verified_at=datetime.now(UTC) if verified else None)


def test_refusal_order_role_then_address_then_verification():
    inv = _inv("pi@scripps.edu")
    assert _invite_refusal(inv, _account("pi@scripps.edu")) is None
    assert _invite_refusal(inv, _account("PI@Scripps.edu")) is None
    assert _invite_refusal(inv, _account("pi@scripps.edu", verified=False)) == _INVITE_UNVERIFIED_MSG
    assert _invite_refusal(inv, _account("x@evil.com", verified=False)) == _INVITE_EMAIL_MISMATCH_MSG
    assert _invite_refusal(inv, _account("pi@scripps.edu", role=USER_ROLE_REVIEWER)) == _INVITE_NOT_PI_MSG
```

- [ ] **Step 2: Run them; expect failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_invite_verification.py tests/unit/test_invite_email_binding.py -v`
Expected: collection ERROR — `ImportError: cannot import name '_INVITE_NOT_PI_MSG' from 'src.routers.invite'`.

- [ ] **Step 3: Implement in `src/routers/invite.py`**

Imports, before:
```python
from src.database import get_db
from src.models import AgentDelegate, AgentRegistry, DelegateInvitation, User
```
after:
```python
from src.database import get_db
from src.dependencies import get_current_user, refuse_impersonation
from src.models import AgentDelegate, AgentRegistry, DelegateInvitation, User
```
Before (lines 20-23):
```python
_INVITE_EMAIL_MISMATCH_MSG = (
    "This invitation was sent to a different email address. Please sign in with "
    "the ORCID account whose email matches the invitation."
)
```
after:
```python
_INVITE_EMAIL_MISMATCH_MSG = (
    "This invitation was sent to a different email address. Please sign in with "
    "the ORCID account whose email matches the invitation."
)
_INVITE_UNVERIFIED_MSG = (
    "An administrator must verify your email address before you can accept this "
    "invitation."
)
_INVITE_NOT_PI_MSG = "Only a PI account can accept a delegate invitation."
_INVITE_IMPERSONATION_DETAIL = "Invitations cannot be accepted while impersonating."
```
After `_invite_matches_user` (after its `return` line), add:
```python


def _invite_refusal(invitation: DelegateInvitation, user: User) -> str | None:
    """Why ``user`` may not accept ``invitation``, or None when they may.

    Spec 2026-10-01 §6.6: a PI-surface account (D-06: a manager or reviewer has no
    lab to delegate into), holding the invited address (SEC-6), verified by an
    administrator or manager (A-04: ``users.email`` is user-editable and the
    verification is cleared whenever it changes). Every branch refuses; the order
    only picks the most useful message.
    """
    if not user.may_use_pi_surfaces:
        return _INVITE_NOT_PI_MSG
    if not _invite_matches_user(invitation, user):
        return _INVITE_EMAIL_MISMATCH_MSG
    if user.email_verified_at is None:
        return _INVITE_UNVERIFIED_MSG
    return None
```
GET, before (lines 83-108):
```python
    # Valid invitation — check if user is logged in
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        # Store token and redirect to login
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/login/start", status_code=302)

    # User is logged in — check onboarding
    user_result = await db.execute(
        select(User).where(User.id == user_id_str)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/login/start", status_code=302)

    # Bind the invite to the address it was sent to — a forwarded/leaked link
    # opened by a different account must not reach the acceptance page.
    if not _invite_matches_user(invitation, user):
        logger.warning(
            "Invite %s (for %r) opened by user %s (%r) — email mismatch",
            invitation.id, invitation.email, user.id, user.email,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": _INVITE_EMAIL_MISMATCH_MSG},
        )
```
after:
```python
    # Valid invitation. An anonymous visitor signs in first and is brought back
    # here: the token survives the login's session reset
    # (src/routers/auth.py::_start_fresh_session).
    if not request.session.get("user_id"):
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/login/start", status_code=302)

    # A signed-in visitor is resolved through get_current_user (A-09), so a
    # denied, pending, signed-out-elsewhere or deleted account is bounced exactly
    # as on every other page rather than read from the raw session.
    user = await get_current_user(request, db)
    refuse_impersonation(user, _INVITE_IMPERSONATION_DETAIL)

    # Bind the invite to the address it was sent to — a forwarded/leaked link
    # opened by a different account must not reach the acceptance page.
    refusal = _invite_refusal(invitation, user)
    if refusal is not None:
        logger.warning(
            "Invite %s (for %r) refused for user %s (%r): %s",
            invitation.id, invitation.email, user.id, user.email, refusal,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": refusal},
        )
```
POST, before (lines 159-166):
```python
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        return RedirectResponse(url=f"/invite/{token}", status_code=302)

    user_result = await db.execute(select(User).where(User.id == user_id_str))
    user = user_result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url=f"/invite/{token}", status_code=302)
```
after:
```python
    if not request.session.get("user_id"):
        return RedirectResponse(url=f"/invite/{token}", status_code=302)

    user = await get_current_user(request, db)
    refuse_impersonation(user, _INVITE_IMPERSONATION_DETAIL)
```
`_accept_invitation`, before (lines 175-187):
```python
    # Enforce the email binding at the mutation chokepoint (defense in depth
    # behind the GET-side check): never grant delegate access to an account
    # whose email differs from the invited address. See SEC-6.
    if not _invite_matches_user(invitation, user):
        logger.warning(
            "Rejecting invite acceptance: invitation %s for %r, user %s has %r",
            invitation.id, invitation.email, user.id, user.email,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": _INVITE_EMAIL_MISMATCH_MSG},
        )
```
after:
```python
    # Enforce the binding at the mutation chokepoint (defense in depth behind the
    # GET-side check): a PI-surface account whose verified address is the invited
    # one, or nothing. See SEC-6 and spec 2026-10-01 §6.6.
    refusal = _invite_refusal(invitation, user)
    if refusal is not None:
        logger.warning(
            "Rejecting invite acceptance: invitation %s for %r, user %s (%r): %s",
            invitation.id, invitation.email, user.id, user.email, refusal,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": refusal},
        )
```

- [ ] **Step 4: Verify the delegates of the existing invite tests**

`tests/integration/test_agent_page.py` imports, before:
```python
from dataclasses import dataclass, field
from types import SimpleNamespace
```
after:
```python
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
```
Before (lines 258-260):
```python
    delegate = await factories.make_user(
        db_session, name="Dee Legate", email="dee@example.org"
    )
```
after:
```python
    delegate = await factories.make_user(
        db_session, name="Dee Legate", email="dee@example.org",
        email_verified_at=datetime.now(UTC),
    )
```
Before (lines 486-487):
```python
    doomed = await factories.make_user(db_session, name="Dana Doomed", email="doomed@example.org")
    keeper = await factories.make_user(db_session, name="Kim Keeper", email="keeper@example.org")
```
after:
```python
    doomed = await factories.make_user(db_session, name="Dana Doomed", email="doomed@example.org",
                                       email_verified_at=datetime.now(UTC))
    keeper = await factories.make_user(db_session, name="Kim Keeper", email="keeper@example.org",
                                       email_verified_at=datetime.now(UTC))
```
Before (line 552):
```python
    delegate = await factories.make_user(db_session, name="Dee Legate", email="dee@example.org")
```
after:
```python
    delegate = await factories.make_user(db_session, name="Dee Legate", email="dee@example.org",
                                         email_verified_at=datetime.now(UTC))
```
(If §6.4's owner has already deleted `test_accepting_an_invitation_syncs_the_delegates_slack_id`,
which holds line 552, skip that edit.)
`tests/integration/test_double_submits.py`, before (line 22):
```python
        d = await factories.make_user(s, name="Del", email=f"d{uuid.uuid4().hex[:6]}@x.edu")
```
after:
```python
        d = await factories.make_user(s, name="Del", email=f"d{uuid.uuid4().hex[:6]}@x.edu",
                                      email_verified_at=datetime.now(UTC))
```

- [ ] **Step 5: Run; expect pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_invite_verification.py tests/unit/test_invite_email_binding.py tests/integration/test_agent_page.py tests/integration/test_double_submits.py tests/integration/test_onboarding_flow.py tests/unit/test_delegates.py tests/characterization/test_auth_and_admin_routes.py::test_invite_invalid_token_renders_error_200 -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/routers/invite.py tests/integration/test_invite_verification.py \
  tests/unit/test_invite_email_binding.py tests/integration/test_agent_page.py \
  tests/integration/test_double_submits.py
git commit -m "fix(webui-1B): invite acceptance needs a PI account and a verified address

Both invite routes resolve the user through get_current_user (A-09),
refuse impersonation, refuse non-PI accounts (D-06) and require the
invited address, verified (A-04).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-9: Operator documentation

**Files:**
- Modify: `docs/operations/pis-and-access.md:241-242`, after `:292`, `:299-303`, after `:312`

**Interfaces:**
- Consumes: names produced by 1B-3..1B-8.
- Produces: none.

- [ ] **Step 1: Edit `docs/operations/pis-and-access.md`**

Before (lines 241-242):
```markdown
**Impersonation.** `refuse_impersonation`, `recorded_by` and `impersonation_note`
live in `src/dependencies.py`. Role changes (`POST /admin/users/{id}/role`) are
```
after:
```markdown
**Impersonation.** It is held in the signed session (`session["impersonate_user_id"]`,
with `impersonate_expires_at` 24 h after the start; `start_impersonation` /
`end_impersonation` in `src/dependencies.py`) and honoured only while the session holder
is an admin; the old unsigned `copi-impersonate` cookie is neither read nor written.
`refuse_impersonation`, `recorded_by` and `impersonation_note`
live in `src/dependencies.py`. Role changes (`POST /admin/users/{id}/role`) are
```
After the paragraph ending "other writer." (line 292) insert:
```markdown

**Email verification** (migration `0057`, web UI remediation spec §6.6).
`users.email_verified_at` is set only by an admin (`POST /admin/users/{id}/verify-email`,
any user) or a manager (`POST /manager/pis/{id}/verify-email`, PIs only), both refused
under impersonation and each recorded as an `admin_audit_events` row with action
`verify_email`; there is no verification email. `assign_user_email` clears it whenever
the address changes (compared case-insensitively) or is cleared. Delegate-invitation
acceptance requires a PI-surface account (`may_use_pi_surfaces`) whose verified address
equals the invited one; otherwise the page says "An administrator must verify your email
address before you can accept this invitation." Every account that had an email when
`0057` was applied was marked verified (owner decision D8).
```
Before (lines 301-303):
```markdown
fixed 2026-08-22 as E1.2). Sessions are unkeyed signed cookies with a 30-day
`max_age` and no server-side store, so `users.access_status` is the only
revocation signal there is — and nothing read it after login, so
```
after:
```markdown
fixed 2026-08-22 as E1.2). Sessions are unkeyed signed cookies with a 30-day
`max_age` and no server-side store, so before `0057` `users.access_status` was the only
revocation signal there was — and nothing read it after login, so
```
After the paragraph ending "commented at the\ncheck." (line 312) insert:
```markdown

**Sessions** (web UI remediation spec §6.7). The cookie is `__Host-copi-session` when
`ALLOW_HTTP_SESSIONS=false` (production) and `copi-session` otherwise
(`src/main.py::session_cookie_name`). Login clears the pre-login session, keeping only the
vetted `next` and a pending invite token, and stores `users.session_epoch` (NULL as 0) as
`session["epoch"]`; `get_current_user` refuses a session whose epoch differs, after the
access check above. `POST /logout` bumps the epoch, signing the account out on every
device, reading `session["user_id"]` directly and still with no auth dependency; access
denial and a role change (`/admin/users/{id}/role`, `admin:grant`, `admin:revoke`,
`role:set`) bump it too (`src/services/session_epoch.py`).
```

- [ ] **Step 2: Check the doc references**

Run: `.venv-test/bin/python -m pytest tests/unit/test_claude_md_references.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/operations/pis-and-access.md
git commit -m "docs(webui-1B): sessions, impersonation and email verification in pis-and-access

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1B-10: Phase 1 harness journeys (sessions, impersonation, invites)

**Files:**
- Create: `tests/e2e/ui_audit/journeys_phase1_1b.py`
- Modify (or Create if absent): `tests/e2e/ui_audit/journeys_phase1.py`

**Interfaces:**
- Consumes: Phase 0 `Harness` (`base_url`, `ids`, `cookie_name`, `secret_key`,
  `async page(role, width=1280, bypass_csp=False) -> (context, page, log)`);
  `tests.e2e.session.forge_session_cookie(user_id, **extra)`; seed id `admin`; the harness
  process's `os.environ` (set to the stack env by `run.py::_run`); 1B-4, 1B-5, 1B-8 behaviour.
- Produces: `journey_login_logout_second_device`, `journey_impersonate_and_stop`,
  `journey_invite_accept_verified_and_unverified`, `JOURNEYS_1B: list`.

- [ ] **Step 1: Write the journeys**

Create `tests/e2e/ui_audit/journeys_phase1_1b.py`:

```python
"""Phase 1 journeys owned by Part 1B (spec 2026-10-01 §9): sessions, impersonation and
invite acceptance. Each returns {"ok": bool, ...evidence}.

Registered from journeys_phase1.py (``JOURNEYS = [..., *JOURNEYS_1B]``). Each journey
creates its own users in the harness database, so it never moves a seeded user's
``session_epoch``: the crawl and the other journeys forge sessions with no epoch key,
which only an account that was never bumped accepts.

ORCID cannot be driven from the harness, so "login" here is the login page rendering
for a visitor plus the session a login writes after a sign-out (epoch 1) being accepted;
the callback itself is covered by tests/integration/test_session_epoch.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

UNVERIFIED_TEXT = (
    "An administrator must verify your email address before you can accept this invitation."
)

_PREAMBLE = (
    "import asyncio, json\n"
    "from datetime import UTC, datetime, timedelta\n"
    "from src.database import get_engine, get_session_factory\n"
    "from src.models import DelegateInvitation\n"
    "from tests import factories\n"
)


def _seed(body: str) -> dict:
    """Run ``body`` as the inside of ``async def m(s)`` on the harness database, commit,
    and return the dict it returns."""
    script = (
        _PREAMBLE
        + "async def m(s):\n"
        + "".join(f"    {line}\n" for line in body.strip("\n").splitlines())
        + "async def main():\n"
        + "    async with get_session_factory()() as s:\n"
        + "        out = await m(s)\n"
        + "        await s.commit()\n"
        + "    await get_engine().dispose()\n"
        + "    print(json.dumps(out))\n"
        + "asyncio.run(main())\n"
    )
    made = subprocess.run([sys.executable, "-c", script], env=os.environ, check=True,
                          capture_output=True, text=True)
    return json.loads(made.stdout.strip().splitlines()[-1])


def _cookie(h, user_id: str, **extra) -> str:
    os.environ["SECRET_KEY"] = h.secret_key
    from tests.e2e.session import forge_session_cookie

    return forge_session_cookie(user_id, **extra)


async def _page_as(h, user_id: str, **extra):
    ctx, page, log = await h.page(None)
    await ctx.add_cookies([{"name": h.cookie_name, "value": _cookie(h, user_id, **extra),
                            "url": h.base_url}])
    return ctx, page, log


async def journey_login_logout_second_device(h) -> dict:
    """Two devices on one account: signing out on one signs out the other; a session
    issued at the bumped epoch (what the next login writes) is accepted."""
    ids = _seed('''
u = await factories.make_user(s, name="Two Device", orcid="0000-0002-4444-0001", email="two@uiaudit.test")
await factories.make_profile(s, user=u)
return {"user": str(u.id)}
''')
    uid = ids["user"]
    a_ctx, a, a_log = await _page_as(h, uid)
    b_ctx, b, b_log = await _page_as(h, uid)
    anon_ctx, anon, anon_log = await h.page(None)
    out: dict = {}
    try:
        await anon.goto(f"{h.base_url}/login", wait_until="networkidle")
        out["login_page"] = anon.url
        await a.goto(f"{h.base_url}/profile", wait_until="networkidle")
        await b.goto(f"{h.base_url}/profile", wait_until="networkidle")
        out["before"] = [a.url, b.url]
        await a.click("form[action='/logout'] button[type=submit]")
        await a.wait_for_url("**/login**")
        out["a_after_logout"] = a.url
        await b.goto(f"{h.base_url}/profile", wait_until="networkidle")
        out["b_after_other_logout"] = b.url
        c_ctx, c, c_log = await _page_as(h, uid, epoch=1)
        try:
            await c.goto(f"{h.base_url}/profile", wait_until="networkidle")
            out["relogin"] = c.url
        finally:
            await c_ctx.close()
        out["pageerror"] = a_log["pageerror"] + b_log["pageerror"] + anon_log["pageerror"] \
            + c_log["pageerror"]
        ok = (out["login_page"].endswith("/login")
              and all(url.endswith("/profile") for url in out["before"])
              and "/login" in out["a_after_logout"]
              and "/login" in out["b_after_other_logout"]
              and out["relogin"].endswith("/profile")
              and not out["pageerror"])
        return {"ok": ok, **out}
    finally:
        for ctx in (a_ctx, b_ctx, anon_ctx):
            await ctx.close()


async def journey_impersonate_and_stop(h) -> dict:
    """An admin impersonates by ORCID, sees the banner, stops; no cookie but the session."""
    _seed('''
t = await factories.make_user(s, name="Imp Target", orcid="0000-0002-4444-0002", email="imp@uiaudit.test")
await factories.make_profile(s, user=t)
return {"target": str(t.id)}
''')
    ctx, page, log = await h.page("admin")
    try:
        await page.goto(f"{h.base_url}/admin/users", wait_until="networkidle")
        await page.fill("form[action='/admin/impersonate'] input[name=orcid]",
                        "0000-0002-4444-0002")
        await page.click("form[action='/admin/impersonate'] button[type=submit]")
        await page.wait_for_load_state("networkidle")
        during_url = page.url
        banner = await page.locator("form[action='/admin/impersonate/stop']").count()
        viewing = await page.get_by_text("Viewing as Imp Target").count()
        cookies_during = sorted(c["name"] for c in await ctx.cookies())
        if banner:
            await page.click("form[action='/admin/impersonate/stop'] button[type=submit]")
            await page.wait_for_load_state("networkidle")
        after_url = page.url
        banner_after = await page.locator("form[action='/admin/impersonate/stop']").count()
        cookies_after = sorted(c["name"] for c in await ctx.cookies())
        ok = (banner == 1 and viewing >= 1 and after_url.endswith("/admin/users")
              and banner_after == 0
              and cookies_during == [h.cookie_name] and cookies_after == [h.cookie_name]
              and not log["pageerror"])
        return {"ok": ok, "during_url": during_url, "banner": banner, "viewing": viewing,
                "after_url": after_url, "banner_after": banner_after,
                "cookies_during": cookies_during, "cookies_after": cookies_after,
                "pageerror": log["pageerror"]}
    finally:
        await ctx.close()


async def journey_invite_accept_verified_and_unverified(h) -> dict:
    """A verified invitee accepts and lands on the agent dashboard; an unverified one is
    told an administrator must verify the address and gets no accept form."""
    ids = _seed('''
pi = await factories.make_user(s, name="Inviting PI", orcid="0000-0002-4444-0003", email="inviter@uiaudit.test")
agent = await factories.make_agent(s, user=pi, agent_id="inviter1b", bot_name="Inviter1bBot", pi_name="Inviting PI")
good = await factories.make_user(s, name="Verified Delegate", orcid="0000-0002-4444-0004", email="verified@uiaudit.test", email_verified_at=datetime.now(UTC))
bad = await factories.make_user(s, name="Unverified Delegate", orcid="0000-0002-4444-0005", email="unverified@uiaudit.test")
for email, token in (("verified@uiaudit.test", "uiaudit-1b-verified"), ("unverified@uiaudit.test", "uiaudit-1b-unverified")):
    s.add(DelegateInvitation(agent_registry_id=agent.id, invited_by_user_id=pi.id, email=email, token=token, status="pending", expires_at=datetime.now(UTC) + timedelta(days=1)))
await s.flush()
return {"good": str(good.id), "bad": str(bad.id), "agent": agent.agent_id}
''')
    good_ctx, good, good_log = await _page_as(h, ids["good"])
    bad_ctx, bad, bad_log = await _page_as(h, ids["bad"])
    try:
        accept = "form[action='/invite/uiaudit-1b-verified/accept']"
        await good.goto(f"{h.base_url}/invite/uiaudit-1b-verified", wait_until="networkidle")
        good_form = await good.locator(accept).count()
        if good_form:
            await good.click(f"{accept} button[type=submit]")
            await good.wait_for_load_state("networkidle")
        accepted_url = good.url

        await bad.goto(f"{h.base_url}/invite/uiaudit-1b-unverified", wait_until="networkidle")
        bad_text = await bad.content()
        bad_form = await bad.locator("form[action='/invite/uiaudit-1b-unverified/accept']").count()
        pageerror = good_log["pageerror"] + bad_log["pageerror"]
        ok = (good_form == 1
              and f"/agent/{ids['agent']}/dashboard" in accepted_url
              and UNVERIFIED_TEXT in bad_text and bad_form == 0
              and not pageerror)
        return {"ok": ok, "good_form": good_form, "accepted_url": accepted_url,
                "unverified_message": UNVERIFIED_TEXT in bad_text, "bad_form": bad_form,
                "pageerror": pageerror}
    finally:
        for ctx in (good_ctx, bad_ctx):
            await ctx.close()


JOURNEYS_1B = [
    journey_login_logout_second_device,
    journey_impersonate_and_stop,
    journey_invite_accept_verified_and_unverified,
]
```

- [ ] **Step 2: Register them**

If `tests/e2e/ui_audit/journeys_phase1.py` exists, add
`from tests.e2e.ui_audit.journeys_phase1_1b import JOURNEYS_1B` to its imports and append
`*JOURNEYS_1B` as the last element of its `JOURNEYS` list. If it does not exist yet, create it:

```python
"""Phase 1 journeys (spec 2026-10-01 §9). Each returns {"ok": bool, ...evidence}."""

from __future__ import annotations

from tests.e2e.ui_audit.journeys_phase1_1b import JOURNEYS_1B

JOURNEYS: list = [*JOURNEYS_1B]
```

- [ ] **Step 3: Lint; then run the journeys (red before 1B-4..1B-8, green after)**

Run: `.venv-test/bin/python -m ruff check tests/e2e/ui_audit/`
Expected: `All checks passed!`

Run: `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 1`
Expected (on the merged Phase 1 branch): the three `journey_*` names absent from the
printed `"failed"` list, exit 0 if no other part's journey fails. On a tree without
1B-4..1B-8 the impersonation journey fails on `cookies_during` (a `copi-impersonate`
cookie) and the invite journey on `unverified_message`.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/ui_audit/journeys_phase1_1b.py tests/e2e/ui_audit/journeys_phase1.py
git commit -m "test(webui-1B): harness journeys for sign-out, impersonation and invites

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Self-review

**Spec coverage**

| Spec bullet | Task |
|---|---|
| §6.5 maps from connected clients each poll | 1B-2 (`_bot_identity_maps`) |
| §6.5 accept only `bot_id` or `user` in a map | 1B-2 (`_known_sender`, tests own/foreign/user-only/disconnected) |
| §6.5 `sender_agent_id` from map, `sender_name` from registry `bot_name` | 1B-2 (`test_attribution_comes_from_identity_not_username`) |
| §6.5 unknown sender: skip, advance cursor, one INFO line with channel and ts, no content | 1B-2 (`test_a_foreign_bot_is_dropped_with_the_cursor_advanced`) |
| §6.6 migration `0057` columns, D8 backfill, downgrade | 1B-1 |
| §6.6 every `users.email` write clears verification on change | 1B-6 (single writer; route-level test of the three forms; login/CLI/pi_onboarding go through the same writer) |
| §6.6 admin verify route, `get_admin_user`, impersonation refusal, audit event | 1B-7 |
| §6.6 manager verify route, `get_staff_user`, PI targets only, refusal, audit | 1B-7 |
| §6.6 manager allowlist test extended | 1B-7 Step 5 |
| §6.6 invite routes via `get_current_user`, `may_use_pi_surfaces`, case-insensitive verified match, message | 1B-8 |
| §6.7 `__Host-copi-session` / `copi-session` | 1B-3 |
| §6.7 impersonation in `session["impersonate_user_id"]`; cookie not read or written at the four cited sites | 1B-4 |
| §6.7 login clears the session, keeps vetted `next` | 1B-5 (plus `pending_invite_token`, stated deviation) |
| §6.7 login stores epoch; `get_current_user` rejects a differing epoch | 1B-5 |
| §6.7 logout bumps from `session["user_id"]`, no auth dependency | 1B-5 (`test_logout_takes_no_auth_dependency`) |
| §6.7 deny and role change bump | 1B-5 (web deny, web role, CLI roles) |
| §4 deploy unit (migrate, web+worker, agent with no live run, signed out once) | 1B-1 Step 9 box |
| §9 journeys: login/logout/second device; impersonate and stop; invite verified and unverified | 1B-10 |
| Brief: find and update every cookie forger; state the tests' cookie name | 1B-3 (17 files scripted, e2e, harness); Global constraints |

**Placeholder scan:** no TBD/TODO; every edit has before/after text or full code; the
two scripted rewrites assert their exact counts (17 files; 19 sites) and fail otherwise.

**Interface-name consistency:** `session_cookie_name` (src: `(settings)`, tests: `()`),
`SESSION_COOKIE_HTTP`, `SESSION_COOKIE_HTTPS`, `session_headers(user_id, *, impersonate=None, **extra)`,
`raw_session_headers`, `session_from_response`, `IMPERSONATE_KEY`, `IMPERSONATE_EXPIRES_KEY`,
`IMPERSONATION_MAX_AGE`, `start_impersonation`, `end_impersonation`, `SESSION_EPOCH_KEY`,
`current_epoch`, `epoch_in_session`, `bump_session_epoch(db, user_id, *, expected_epoch=None)`,
`mark_email_verified`, `VERIFY_EMAIL_ACTION`, `_invite_refusal`, `_INVITE_UNVERIFIED_MSG`,
`_INVITE_NOT_PI_MSG`, `Transport.bot_id` / `bot_user_id`, `_bot_identity_maps`,
`_known_sender`, `JOURNEYS_1B` — each defined once and used with the same spelling in
every later task. `src.main.SESSION_COOKIE` is deleted in 1B-3; its only consumers (the
Phase 0 `run.py` and the `SessionMiddleware` call) are updated in the same task.

**Ordering:** 1B-1 first (columns); 1B-3 before 1B-4..1B-8 (they import
`tests/session_support.py`); 1B-4 before 1B-5 (logout tail) and 1B-7/1B-8 (impersonation
in tests); 1B-5 before 1B-7 (the `users.py` import line) and 1B-8 (stale-epoch test);
1B-10 last. 1B-2 is independent.

**Documentation outside this part's ownership:** `CLAUDE.md` "Deploying" names `0056`
as head and its `0056` remediation bullet; after this part the head is `0057`. Not edited
here (CLAUDE.md is the owner's file); the parent decides.

---

## Part 1C: Admin controls, data integrity, error pages, flash, accessibility

**Scope:** spec §6.8 (C-04, C-05 with D14, C-07, C-10, FN-01, FN-06, B-10, D-01, D-02, D-08,
D-16, B-06), §6.9 (M-01, FN-09, A-14 text-carrying messages), §6.10 (X-01, X-02, X-05 and the
rendered-page gate). Task prefix `1C-`.

### Global constraints (this part)

- **Order against other parts.** Every 1C task that edits a template, `templates/base.html`,
  `static/js/ui.js`, `static/css/input.css`, `tailwind.config.js` or `src/main.py` runs after
  all of Part 1A's tasks (nonces, inline-handler removal, compiled CSS, `ui.js`) and after the
  §6.4 removal tasks (PostHog block in `base.html` and `src/main.py`, graph pages, Connect
  Slack in `src/routers/agent_page.py` and `templates/agent/dashboard.html`). Tasks 1C-9 and
  1C-10 (`src/services/profile_edit.py`) run after Part 1B's §6.6 email-verification edits.
  **Edit by the quoted anchor text, not by line number:** the line numbers below are those
  of `blackbird` at `e8f475f` and will have moved by the time this part runs.
- **CSS rebuild.** Any task that adds or changes a Tailwind class in `templates/`,
  `static/js/` or `src/` runs `scripts/build_css.sh` (Part 1A) and commits
  `static/css/app.css` in the same commit; `ci.sh`'s drift check fails otherwise.
- **Verbatim spec values:**
  - Stop dialog (amended 2026-10-01 at plan assembly — a Stop posts at most `HEADLINES_MAX_AT_SHUTDOWN = 25`, `src/agent/engine/constants.py:157`, so "Posts N" overstated): `Posts K of N owed headlines to Slack (M from interviews still open; at most 25 per stop); cannot be undone` with K = min(N, 25),
    N and M from the LIVE run's funnel (`headlines_owed`, `provisional` in
    `src/services/simulation_stats.py`). "Stop — hold open interviews" keeps no dialog (D11).
  - Finalize: `confirm_run` must equal the run id's first 8 characters, checked in
    `admin_simulation_finalize_run`.
  - D-08: refuse while that user has a `generate_profile` job `pending` or `processing`
    ("Profile is being generated — try again shortly"); the profile-row insert runs under
    `SET LOCAL lock_timeout = '5s'` and the lock error maps to the same message.
  - X-01: `text-gray-300`/`text-gray-400` become `text-gray-600`; white text on
    `bg-green-600` becomes `bg-green-700`; the harness's axe run reports zero
    `color-contrast` violations.
  - M-01: a 3xx passes through with its `Location`; JSON for paths under `/assessment-chat/`
    and `/api/` and for requests whose `Accept` names `application/json`; otherwise
    `templates/error.html`, same status code.
- **What moves to flash.** Only query parameters that carry message TEXT move:
  `slack_error`, `delegate_error`, and the free-text `error=` of the cohort, simulation and
  Finalize routes. `error=<code>` redirects (profile save, agent form, manager PI routes,
  onboarding, login, account deletion) stay; they are A-14's Phase 2 remainder, as are
  `msg=`, `notice=`, `spoke_error=` and `role_error=`. The Connect Slack producers of
  `slack_error` (`src/routers/agent_page.py:725,777`) and its dashboard consumer
  (`agent_page.py:194,267`) are deleted by the §6.4 removal, not migrated here.
- **Library facts checked in source.** Starlette `Jinja2Templates.TemplateResponse` runs every
  context processor on every call, partials included (`starlette/templating.py:145-146`,
  identical in 1.4.1 and 1.7.0). Starlette `Session` marks itself modified on `__setitem__`,
  on `pop` of a present key, `setdefault` of a new key, `update` and `clear`, not on in-place
  mutation of a stored list; 1.7.0 adds only `popitem`, `__ior__` and the `partitioned`
  flag. FastAPI registers its default handlers with `setdefault` on Starlette's
  `HTTPException` and on `RequestValidationError` (`fastapi/applications.py:1003-1005`), so
  `add_exception_handler` for the same classes replaces them; FastAPI's
  `http_exception_handler` already returns a body-less response for 1xx/204/304 and copies
  `exc.headers`. asyncpg's lock-timeout error is `LockNotAvailableError` (sqlstate `55P03`);
  SQLAlchemy's asyncpg adapter copies `sqlstate` onto the translated error it wraps as
  `DBAPIError.orig` (`sqlalchemy/dialects/postgresql/asyncpg.py:781-795`).

### Review focus (this part)

1. **A fragment eating a flash.** A context processor runs for every `TemplateResponse`, so
   an eager pop would let a script-fetched partial (`agent/_thread_replies.html`) swallow a
   message meant for the next page. Pinned in 1C-1 by
   `test_context_processor_pops_only_when_called`.
2. **Two concurrent hub activations.** The single-active-hub check is only race-proof if the
   advisory lock is taken before the count. Pinned in 1C-6 by
   `test_a_lone_hub_may_activate_and_the_roster_lock_is_taken` (lock held after the call)
   and `test_the_lock_precedes_the_hub_count` (source order).
3. **A tag containing a comma.** Generated profiles carry tags like `1,2-dichloroethane`.
   Pinned in 1C-10 by `test_a_comma_inside_a_tag_survives_the_save`.
4. **A stale second tab.** Pinned in 1C-7 by `test_a_stale_tab_does_not_undo_another_tabs_add`
   and the harness journey `journey_cohort_two_tab_save`.
5. **A refusal that half-writes.** `get_db` commits on a clean return
   (`src/database.py:95-104`), so every refusal path of `apply_profile_edits` must run before
   any write. Pinned in 1C-9 by `test_an_invalid_email_writes_no_tenure` and
   `test_a_pending_generation_job_refuses_the_save_and_writes_nothing`.

### File map

| File | Change | Responsibility |
|---|---|---|
| `src/web/flash.py` | create | `flash`, `pop_flashes`, `flash_context` |
| `src/web/templating.py` | modify | register `flash_context` |
| `src/web/errors.py` | create | `install_error_handlers`, HTML/JSON error responses |
| `templates/error.html` | create | browser error page |
| `templates/base.html` | modify | flash block, `account` block, footer contrast |
| `src/main.py` | modify | call `install_error_handlers` |
| `src/services/advisory_locks.py` | modify | `HUB_ROSTER_LOCK_KEY` |
| `src/services/agent_activation.py` | modify | `ensure_activation_allowed`; `activation_blockers(role=)`; `activate_agent` uses the gate |
| `src/routers/admin/agents.py` | modify | C-04 refusal, role-route gate, flash for refusals and Slack errors |
| `src/routers/manager.py` | modify | flash for Slack errors and activation refusals; tag lists |
| `src/routers/admin/cohorts.py` | modify | flash for errors; C-07 diff against rendered-checked cells |
| `src/routers/admin/simulation.py` | modify | flash for errors; `confirm_run`; Stop counts |
| `src/routers/admin/runs.py` | modify | drop the `error` query consumer |
| `src/routers/agent_page.py` | modify | flash for `delegate_error`; tag lists |
| `src/routers/profile.py` | modify | tag lists; delete dead `_parse_list` |
| `src/routers/onboarding.py` | modify | tag lists |
| `src/services/profile_edit.py` | modify | D-01, D-02, D-08; list fields; `list_fields_from_form` |
| `templates/_tag_field.html` | create | tag widget macros |
| `static/js/tag_widget.js` | create | tag widget behaviour (repeated hidden inputs) |
| `static/css/input.css` | modify | tag widget styles (moved out of three inline `<style>` blocks) |
| `static/js/assessment_chat.js` | modify | B-06 readOnly/aria-busy/refocus |
| `static/js/ui.js` | modify | `data-toggles` / `data-row-toggles` disclosure |
| `templates/admin/agent_detail.html` | modify | pending option, banners, labels |
| `templates/admin/cohort_topology.html` | modify | `was_checked` markers, banners, checkbox names |
| `templates/admin/cohorts.html`, `templates/admin/cohort_detail.html` | modify | error banner removal, labels |
| `templates/admin/simulation.html` | modify | error banner removal, Stop and Reset confirms, labels |
| `templates/admin/activity_detail.html` | modify | error banner removal, Finalize confirm and `confirm_run` |
| `templates/admin/_assessment_detail_body.html` | modify | review delete confirm |
| `templates/admin/_assessment_chat_drawer.html` | modify | chat question label |
| `templates/manager/pis.html`, `templates/manager/pi_detail.html`, `templates/manager/slack_bots.html` | modify | Slack banner removal, tag widgets, labels, profile message |
| `templates/agent/dashboard.html` | modify | `delegate_error` removal, label |
| `templates/profile/edit.html`, `templates/onboarding/profile_review.html`, `templates/agent/public_profile.html` | modify | tag widgets, labels, profile message |
| `templates/admin/activity.html`, `templates/manager/activity.html`, `templates/admin/users.html`, `templates/admin/_discussions_threads.html` | modify | row links / disclosure button |
| every template, `static/js/*.js` and `src/` class string in the 1C-12 map | modify | contrast classes |
| remaining templates in the 1C-13 table | modify | accessible names |
| `tailwind.config.js` | modify (only if 1C-12 Step 4 finds a class missing) | safelist |
| `static/css/app.css` | regenerate | compiled CSS |
| `tests/flash_support.py` | create | read flashes from a response's session cookie |
| `tests/unit/test_flash.py` | create | flash unit tests |
| `tests/integration/test_flash_render.py` | create | flash round trip through a page |
| `tests/integration/test_error_pages.py` | create | M-01 |
| `tests/integration/test_hub_activation_gate.py` | create | C-04, C-05 |
| `tests/integration/test_simulation_confirms.py` | create | C-10, FN-01, FN-06, B-10 |
| `tests/integration/test_profile_edit_integrity.py` | create | D-01, D-02, D-08 |
| `tests/integration/test_profile_tag_fields.py` | create | D-16 |
| `tests/unit/test_chat_busy_input.py` | rewrite | B-06 source pins |
| `tests/unit/test_contrast_classes.py` | create | X-01 source guard |
| `tests/unit/test_row_links.py` | create | X-05 source pins |
| `tests/integration/test_rendered_page_gate.py` | create | §6.10 gate |
| `tests/unit/test_advisory_locks.py` | modify | new key distinct |
| `tests/unit/test_no_dead_src_symbols.py` | modify | drop the `src.routers.profile:_parse_list` entry |
| `tests/unit/test_profile_edit_tenure.py`, `tests/integration/test_profile_single_writer.py`, `tests/integration/test_profile_version_guard.py`, `tests/integration/test_onboarding_flow.py`, `tests/integration/test_agent_page.py` | modify | list-valued tag fields; JSON `Accept`; delegate flash |
| `tests/integration/test_cohort_admin.py` | modify | flash assertions, `was_checked` |
| `tests/integration/test_admin_simulation_liveness.py`, `tests/integration/test_finalize_run_route.py` | modify | flash assertions, `confirm_run` |
| `tests/integration/test_manager_slack_provisioning.py`, `tests/integration/test_admin_agent_form.py` | modify | flash assertions |
| `tests/e2e/ui_audit/journeys_phase1.py` | modify (shared with 1A/1B) | Part 1C journeys |

### Task 1C-1: Flash messages in the session

**Files:**
- Create: `src/web/flash.py`
- Modify: `src/web/templating.py:25` (the `Jinja2Templates(...)` line in `make_templates`)
- Modify: `templates/base.html:163-173` (the `<!-- Flash messages -->` block); runs after Part 1A
- Create: `tests/flash_support.py`
- Test: `tests/unit/test_flash.py`, `tests/integration/test_flash_render.py`

**Interfaces:**
- Consumes: Starlette `SessionMiddleware` (`src/main.py`); `tests.integration.test_manager_access.auth_headers`
  (kept working by Part 1B for its session changes).
- Produces:
  - `src/web/flash.py`: `FLASH_SESSION_KEY: str = "_flashes"`, `FLASH_KINDS = ("info", "success", "error")`,
    `MAX_FLASH_CHARS = 300`, `MAX_FLASHES = 5`,
    `def flash(request: Request, text: str, kind: str = "info") -> None`,
    `def pop_flashes(request: Request) -> list[dict[str, str]]`,
    `def flash_context(request: Request) -> dict[str, Any]` (returns `{"get_flashes": <callable>}`).
  - Template global per render: `get_flashes() -> list[dict[str, str]]` (each `{"text", "kind"}`).
  - `tests/flash_support.py`: `session_after(response) -> dict`,
    `session_flashes(response) -> list[dict[str, str]]`,
    `session_cookie_header(response) -> dict[str, str]`. Consumed by 1C-3, 1C-4, 1C-5, 1C-6, 1C-7, 1C-8.

- [ ] **Step 1: Write the failing unit test.** Create `tests/unit/test_flash.py`:

```python
"""src/web/flash.py: queueing, draining, bounds, and the lazy context processor."""
import pytest
from starlette.middleware.sessions import Session
from starlette.requests import Request

from src.web.flash import (
    FLASH_SESSION_KEY,
    MAX_FLASH_CHARS,
    MAX_FLASHES,
    flash,
    flash_context,
    pop_flashes,
)


def _request(session=None) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    if session is not None:
        scope["session"] = session
    return Request(scope)


def test_flash_queues_in_order_and_pop_drains():
    request = _request(Session())
    flash(request, "first")
    flash(request, "second", "error")
    assert pop_flashes(request) == [
        {"text": "first", "kind": "info"},
        {"text": "second", "kind": "error"},
    ]
    assert pop_flashes(request) == []


def test_flash_marks_the_session_modified():
    """Starlette re-sends the cookie only for a modified session."""
    session = Session()
    flash(_request(session), "saved", "success")
    assert session.modified


def test_text_is_capped_and_the_queue_is_bounded():
    request = _request(Session())
    flash(request, "x" * (MAX_FLASH_CHARS + 50))
    assert len(pop_flashes(request)[0]["text"]) == MAX_FLASH_CHARS
    for n in range(MAX_FLASHES + 2):
        flash(request, f"m{n}")
    queue = pop_flashes(request)
    assert len(queue) == MAX_FLASHES
    assert queue[-1]["text"] == f"m{MAX_FLASHES + 1}"


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        flash(_request(Session()), "x", "warning")


def test_pop_without_a_session_is_empty():
    assert pop_flashes(_request(None)) == []


def test_context_processor_pops_only_when_called():
    """A partial rendered through TemplateResponse runs the processor too; only
    base.html's call to get_flashes() may drain the queue."""
    session = Session()
    request = _request(session)
    flash(request, "kept for the next page")
    context = flash_context(request)
    assert FLASH_SESSION_KEY in session
    assert context["get_flashes"]() == [{"text": "kept for the next page", "kind": "info"}]
    assert FLASH_SESSION_KEY not in session
```

- [ ] **Step 2: Run it and see it fail.** `.venv-test/bin/python -m pytest tests/unit/test_flash.py -v`
  Expected: collection error, `ModuleNotFoundError: No module named 'src.web.flash'`.

- [ ] **Step 3: Implement `src/web/flash.py`.**

```python
"""One-shot page messages carried across a POST-redirect-GET in the signed session
(FN-09, A-14).

A message used to ride in the redirect's query string (``?error=...``,
``?slack_error=...``), where anyone could link a victim to a page saying whatever they
liked, and unquoted URL building mangled the text. ``flash`` stores it in the session,
which ``SessionMiddleware`` signs, and ``base.html`` shows and removes it on the next full
page render.

The context processor hands templates a callable rather than the popped list: Starlette
runs every context processor for every ``TemplateResponse``, partials included
(``agent/_thread_replies.html`` is fetched by script and extends nothing), and an eager pop
there would swallow a message meant for the next real page. Only ``base.html`` calls
``get_flashes()``.
"""
from typing import Any

from starlette.requests import Request

#: Session key holding the pending queue: a list of ``{"text", "kind"}`` dicts.
FLASH_SESSION_KEY = "_flashes"
#: The kinds ``base.html`` styles; anything else is a programming error.
FLASH_KINDS = ("info", "success", "error")
#: The session is one signed cookie (browsers cap a cookie near 4 KB), so a message is
#: cut to this many characters and at most ``MAX_FLASHES`` are kept, newest last.
MAX_FLASH_CHARS = 300
MAX_FLASHES = 5


def flash(request: Request, text: str, kind: str = "info") -> None:
    """Queue ``text`` for the next full page this session renders."""
    if kind not in FLASH_KINDS:
        raise ValueError(f"unknown flash kind {kind!r}; expected one of {FLASH_KINDS}")
    queue = list(request.session.get(FLASH_SESSION_KEY) or [])
    queue.append({"text": str(text)[:MAX_FLASH_CHARS], "kind": kind})
    # Reassigned, not appended in place: Starlette's Session marks itself modified (and
    # re-sends the cookie) only on item assignment, in 1.4.1 and 1.7.0 alike.
    request.session[FLASH_SESSION_KEY] = queue[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Remove and return the pending messages; ``[]`` when the request has no session."""
    if "session" not in request.scope:
        return []
    return list(request.session.pop(FLASH_SESSION_KEY, None) or [])


def flash_context(request: Request) -> dict[str, Any]:
    """Context processor: ``get_flashes()`` drains the queue when ``base.html`` calls it."""

    def get_flashes() -> list[dict[str, str]]:
        return pop_flashes(request)

    return {"get_flashes": get_flashes}
```

- [ ] **Step 4: Run the unit test.** `.venv-test/bin/python -m pytest tests/unit/test_flash.py -v`
  Expected: 6 passed.

- [ ] **Step 5: Register the processor.** In `src/web/templating.py`, `make_templates`:
  before:
  ```python
      templates = Jinja2Templates(directory="templates")
  ```
  after:
  ```python
      from src.web.flash import flash_context

      # `get_flashes()` for base.html's flash block (src/web/flash.py). A context
      # processor, not a global, because it closes over the request.
      templates = Jinja2Templates(directory="templates", context_processors=[flash_context])
  ```

- [ ] **Step 6: Replace the flash block in `templates/base.html`.** Replace the whole block from
  `<!-- Flash messages -->` through its closing `{% endif %}` (today lines 163-173; nothing sets
  `flash_message`/`flash_type`, FN-09) with:

```jinja
<!-- Flash messages (src/web/flash.py): shown once, then gone from the session. -->
{% set flashes = get_flashes() if get_flashes is defined else [] %}
{% if flashes %}
<div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-4 space-y-2" data-flash-region>
    {% for f in flashes %}
    <div class="rounded-md p-4 border
        {% if f.kind == 'error' %}bg-red-50 border-red-200 text-red-800
        {% elif f.kind == 'success' %}bg-green-50 border-green-200 text-green-800
        {% else %}bg-blue-50 border-blue-200 text-blue-800{% endif %}"
         role="{{ 'alert' if f.kind == 'error' else 'status' }}" data-flash-kind="{{ f.kind }}">{{ f.text }}</div>
    {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 7: Write the test helper.** Create `tests/flash_support.py`:

```python
"""Read the flash queue (src/web/flash.py) out of the session cookie a response set.

The cookie is matched by a name ending in ``copi-session`` so the helper keeps working
whichever of ``copi-session`` / ``__Host-copi-session`` (Part 1B, spec §6.7) is in use.
"""
import base64
import json

from itsdangerous import TimestampSigner

from src.config import get_settings
from src.web.flash import FLASH_SESSION_KEY


def _session_set_cookie(response) -> tuple[str, str] | None:
    for header in response.headers.get_list("set-cookie"):
        name, _, value = header.split(";", 1)[0].partition("=")
        if name.strip().endswith("copi-session"):
            return name.strip(), value.strip()
    return None


def session_after(response) -> dict:
    """The session the response's Set-Cookie carries; ``{}`` when it set none or cleared it."""
    found = _session_set_cookie(response)
    if found is None or found[1] in ("", "null"):
        return {}
    # The same signer SessionMiddleware builds (str() of the configured secret).
    signer = TimestampSigner(str(get_settings().secret_key))
    return json.loads(base64.b64decode(signer.unsign(found[1].encode())))


def session_flashes(response) -> list[dict[str, str]]:
    """The pending flash queue after ``response``."""
    return list(session_after(response).get(FLASH_SESSION_KEY) or [])


def session_cookie_header(response) -> dict[str, str]:
    """A ``Cookie`` header replaying the session ``response`` set, for the next request."""
    found = _session_set_cookie(response)
    assert found is not None, "the response set no session cookie"
    return {"Cookie": f"{found[0]}={found[1]}"}
```

- [ ] **Step 8: Write the failing round-trip test.** Create `tests/integration/test_flash_render.py`:

```python
"""A flash set by one request renders once, escaped, on the next full page (FN-09)."""
import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse

from src.web.flash import flash
from tests import factories
from tests.flash_support import session_cookie_header, session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_a_flash_renders_once_on_the_next_page(asgi_app, client, db_session):
    async def _flash_then_redirect(request: Request):
        flash(request, "Saved <b>ok</b>", "success")
        return RedirectResponse(url="/settings", status_code=302)

    asgi_app.add_api_route("/__test_flash", _flash_then_redirect, methods=["GET"])
    user = await factories.make_user(db_session)

    first = await client.get("/__test_flash", headers=auth_headers(user.id), follow_redirects=False)
    assert first.status_code == 302
    assert session_flashes(first) == [{"text": "Saved <b>ok</b>", "kind": "success"}]

    page = await client.get("/settings", headers=session_cookie_header(first))
    assert page.status_code == 200
    assert 'data-flash-kind="success"' in page.text
    assert "Saved &lt;b&gt;ok&lt;/b&gt;" in page.text
    assert session_flashes(page) == []

    again = await client.get("/settings", headers=session_cookie_header(page))
    assert "Saved &lt;b&gt;ok&lt;/b&gt;" not in again.text
```

- [ ] **Step 9: Run it.** `.venv-test/bin/python -m pytest tests/integration/test_flash_render.py tests/unit/test_flash.py tests/unit/test_reachability.py -v`
  Expected: all pass. (`/__test_flash` is added to the per-test app only; the reachability
  gate builds its own app.)

- [ ] **Step 10: Commit.**
```bash
git add src/web/flash.py src/web/templating.py templates/base.html tests/flash_support.py tests/unit/test_flash.py tests/integration/test_flash_render.py
git commit -m "feat(webui): session flash messages rendered once by base.html (FN-09)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-2: Browser error pages (M-01)

**Files:**
- Create: `src/web/errors.py`, `templates/error.html`
- Modify: `templates/base.html:111-120` (wrap the right-hand account `<div class="flex items-center">` in a block); runs after Part 1A
- Modify: `src/main.py:327-329` (after the last `include_router`, before `@application.get("/api/health")`); runs after Parts 1A and 1B and the §6.4 PostHog removal
- Modify: `tests/integration/test_agent_page.py:782-787` and `:806-811` (the two `.json()["detail"]` assertions)
- Test: `tests/integration/test_error_pages.py`

**Interfaces:**
- Consumes: `make_templates` with `flash_context` (1C-1); `get_settings().base_url`.
- Produces: `src/web/errors.py`: `JSON_PATH_PREFIXES: tuple[str, ...] = ("/assessment-chat/", "/api/")`,
  `ERROR_TITLES: dict[int, str]`, `def wants_json(request: Request) -> bool`,
  `def error_page(request: Request, status_code: int, detail: str | None, headers: Mapping[str, str] | None = None) -> Response`,
  `def install_error_handlers(app: FastAPI) -> None`. Template `templates/error.html` with
  context `status_code`, `title`, `detail`, `back_href`. Base block `{% block account %}`.

Existing tests checked: the only browser-style requests asserting a JSON error body are
`tests/integration/test_agent_page.py:787` and `:811` (`r.json()["detail"]` on a 403 from a
non-API path; httpx sends `Accept: */*`). Both gain `Accept: application/json`. No other test
under `tests/` reads `.json()` from a 4xx outside `/assessment-chat/` and `/api/` (grep of
`\.json()` and `"detail"` across `tests/unit`, `tests/integration`, `tests/characterization`).
The `OriginGuardMiddleware` 403 is a `PlainTextResponse`, not an exception, and is unchanged.

- [ ] **Step 1: Write the failing tests.** Create `tests/integration/test_error_pages.py`:

```python
"""M-01: a browser gets an HTML error page; scripts keep FastAPI's JSON; redirects pass."""
from urllib.parse import urlsplit

import pytest

from src.config import get_settings
from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _origin() -> str:
    parts = urlsplit(get_settings().base_url)
    return f"{parts.scheme}://{parts.netloc}"


async def test_a_browser_403_renders_the_error_page(client, db_session):
    pi = await factories.make_user(db_session)
    r = await client.get("/admin/users", headers=auth_headers(pi.id))
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/html")
    assert "<h1" in r.text and "Access denied" in r.text
    assert "Admin access required" in r.text  # the exception's own detail
    assert 'href="/"' in r.text
    assert "Sign out" not in r.text and 'href="/login"' not in r.text


async def test_accept_json_keeps_the_json_body(client, db_session):
    pi = await factories.make_user(db_session)
    r = await client.get(
        "/admin/users", headers={**auth_headers(pi.id), "Accept": "application/json"}
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "Admin access required"}


async def test_an_unknown_page_is_an_html_404_without_the_generic_phrase(client):
    r = await client.get("/no-such-page-here")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "Page not found" in r.text
    assert "Not Found" not in r.text


async def test_paths_under_api_and_assessment_chat_stay_json(client):
    for path in ("/api/no-such-endpoint", "/assessment-chat/a/b/c"):
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("application/json"), path
        assert r.json() == {"detail": "Not Found"}, path


async def test_a_validation_error_is_an_html_422(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get("/admin/activity/not-a-uuid", headers=auth_headers(admin.id))
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("text/html")
    assert "Invalid request" in r.text


async def test_a_validation_error_with_accept_json_keeps_the_error_list(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(
        "/admin/activity/not-a-uuid",
        headers={**auth_headers(admin.id), "Accept": "application/json"},
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


async def test_the_login_redirect_passes_through(client):
    r = await client.get("/profile", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


async def test_a_405_keeps_its_allow_header(client):
    r = await client.put("/login")
    assert r.status_code == 405
    assert "allow" in r.headers
    assert "Method not allowed" in r.text


async def test_back_goes_to_a_same_origin_referer_only(client, db_session):
    pi = await factories.make_user(db_session)
    same = await client.get(
        "/admin/users",
        headers={**auth_headers(pi.id), "Referer": f"{_origin()}/settings?tab=email"},
    )
    assert 'href="/settings?tab=email"' in same.text
    foreign = await client.get(
        "/admin/users",
        headers={**auth_headers(pi.id), "Referer": "https://evil.example/x"},
    )
    assert "evil.example" not in foreign.text
```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_error_pages.py -v`
  Expected: `test_a_browser_403_renders_the_error_page`, `test_an_unknown_page_...`,
  `test_a_validation_error_is_an_html_422`, `test_a_405_...`, `test_back_...` FAIL (JSON
  bodies, `content-type: application/json`); the JSON, redirect and API-path tests pass.

- [ ] **Step 3: Implement `src/web/errors.py`.**

```python
"""Error responses for browser navigation (M-01).

FastAPI answers every ``HTTPException`` and ``RequestValidationError`` with JSON, so a 403,
404 or 422 reached by clicking a link showed raw ``{"detail": ...}``. One handler per
exception class now decides:

* a 3xx (``get_current_user`` raises ``HTTPException(302, headers={"Location": ...})``)
  and anything else below 400 goes to FastAPI's own handler, unchanged;
* JSON, exactly as before, for paths under ``JSON_PATH_PREFIXES`` (the assessment chat's
  ``fetch`` calls and the API) and for any request whose ``Accept`` names
  ``application/json``;
* otherwise ``templates/error.html`` with the same status code and headers (``Allow`` on a
  405 survives).

The page shows the exception's own detail when it is a sentence a handler wrote, and
nothing when it is Starlette's generic phrase ("Not Found") or a validation error list.
"""
from collections.abc import Mapping
from http import HTTPStatus
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from src.config import get_settings
from src.web.templating import make_templates

#: Path prefixes whose callers are scripts; they keep FastAPI's JSON error bodies.
JSON_PATH_PREFIXES: tuple[str, ...] = ("/assessment-chat/", "/api/")

#: The page heading per status; any other status uses its HTTP reason phrase.
ERROR_TITLES: dict[int, str] = {
    400: "Bad request",
    401: "Sign-in required",
    403: "Access denied",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Request too large",
    422: "Invalid request",
    429: "Too many requests",
}

_templates = make_templates()


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def wants_json(request: Request) -> bool:
    """True for a script caller: a JSON path prefix, or ``Accept`` naming JSON."""
    if request.url.path.startswith(JSON_PATH_PREFIXES):
        return True
    return "application/json" in request.headers.get("accept", "").lower()


def _back_href(request: Request) -> str:
    """The same-origin ``Referer`` as a local path, else ``/``: a foreign referer is never
    echoed into a link."""
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    parts = urlsplit(referer)
    base = urlsplit(get_settings().base_url)
    if (parts.scheme, parts.netloc.lower()) != (base.scheme, base.netloc.lower()):
        return "/"
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    return path + (f"?{parts.query}" if parts.query else "")


def error_page(
    request: Request,
    status_code: int,
    detail: str | None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """``templates/error.html`` for ``status_code``."""
    return _templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": status_code,
            "title": ERROR_TITLES.get(status_code) or _phrase(status_code),
            "detail": detail,
            "back_href": _back_href(request),
        },
        status_code=status_code,
        headers=dict(headers) if headers else None,
    )


async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code < 400 or wants_json(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else None
    if detail == _phrase(exc.status_code):
        detail = None
    return error_page(request, exc.status_code, detail, getattr(exc, "headers", None))


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    if wants_json(request):
        return await request_validation_exception_handler(request, exc)
    return error_page(request, 422, None)


def install_error_handlers(app: FastAPI) -> None:
    """Replace FastAPI's JSON-only handlers with the browser-aware pair above."""
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
```

- [ ] **Step 4: Create `templates/error.html`.**

```jinja
{% extends "base.html" %}
{# Browser error page (src/web/errors.py, M-01). Rendered with no current_user, so the
   nav shows no links; the account block is emptied too, so a signed-in user is not
   offered "Sign in". #}
{% block title %}{{ title }} — Blackbird{% endblock %}
{% block account %}{% endblock %}
{% block content %}
<div class="max-w-xl mx-auto text-center py-16">
    <p class="text-sm font-medium text-gray-600">Error {{ status_code }}</p>
    <h1 class="mt-2 text-2xl font-bold text-gray-900">{{ title }}</h1>
    {% if detail %}<p class="mt-4 text-base text-gray-700">{{ detail }}</p>{% endif %}
    <div class="mt-8 flex justify-center gap-6 text-base">
        <a href="{{ back_href }}" class="text-indigo-700 hover:underline">Back</a>
        <a href="/" class="text-indigo-700 hover:underline">Home</a>
    </div>
</div>
{% endblock %}
```

- [ ] **Step 5: Add the `account` block to `templates/base.html`.** Wrap the right-hand
  `<div class="flex items-center">` that holds `{% if current_user %}…Sign out…{% else %}…Sign in…{% endif %}`
  (today lines 111-120):
  before:
  ```jinja
              <div class="flex items-center">
                  {% if current_user %}
  ```
  after:
  ```jinja
              {% block account %}
              <div class="flex items-center">
                  {% if current_user %}
  ```
  and after that div's closing `</div>` (the one following `{% endif %}` of the Sign in link)
  add `{% endblock %}` on its own line.

- [ ] **Step 6: Install the handlers in `src/main.py`.** Add the import
  `from src.web.errors import install_error_handlers` beside the other `src.` imports, and in
  `create_app()` directly after
  `application.include_router(settings_router.router, prefix="/settings", tags=["settings"])`
  add:
  ```python
      # HTML error pages for browser navigation; JSON stays for scripts (src/web/errors.py).
      install_error_handlers(application)
  ```

- [ ] **Step 7: Update the two JSON-body assertions in `tests/integration/test_agent_page.py`.**
  In `test_a_stranger_cannot_touch_an_agent_they_do_not_own`:
  before: `headers=_auth(world.stranger.id))`
  after: `headers={**_auth(world.stranger.id), "Accept": "application/json"})`
  In `test_delegate_write_access_matches_the_spec`:
  before: `data=ep.data, headers=_auth(delegated.user.id))`
  after: `data=ep.data, headers={**_auth(delegated.user.id), "Accept": "application/json"})`

- [ ] **Step 8: Run the tests.** `.venv-test/bin/python -m pytest tests/integration/test_error_pages.py tests/integration/test_agent_page.py tests/unit/test_reachability.py tests/integration/test_origin_guard.py -v`
  Expected: all pass (`error.html` is reachable through the `"error.html"` literal in
  `src/web/errors.py`).

- [ ] **Step 9: Commit.**
```bash
git add src/web/errors.py templates/error.html templates/base.html src/main.py tests/integration/test_error_pages.py tests/integration/test_agent_page.py
git commit -m "feat(webui): HTML error pages for browser navigation, JSON for scripts (M-01)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-2b: Unhandled 500s get the error page and the security headers (M-01, spec §6.3)

Added at plan assembly (2026-10-01). Part 1A found that Starlette's `ServerErrorMiddleware`
sits outside every user middleware (`fastapi/applications.py` `build_middleware_stack`), so an
unhandled exception's 500 never passes through `SecurityHeadersMiddleware`; Part 1A exports
`security_header_items(nonce)` for this handler. A handler registered for `Exception` is the
one `ServerErrorMiddleware` calls. Runs after Task 1A-7 and Task 1C-2.

**Files:**
- Modify: `src/web/errors.py` (from Task 1C-2)
- Test: `tests/integration/test_error_pages.py` (from Task 1C-2)

**Interfaces:**
- Consumes: `security_header_items(nonce: str, *, enforce_script_policy: bool = SCRIPT_POLICY_ENFORCED) -> list[tuple[str, str]]` and `new_nonce() -> str` from `src/web/security_headers.py` (Task 1A-7); `error_page`, `wants_json`, `ERROR_TITLES` (Task 1C-2).
- Produces: `_server_error(request, exc) -> Response`, registered by `install_error_handlers`.

- [ ] **Step 1: Write the failing test** (append to `tests/integration/test_error_pages.py`)

```python
async def test_an_unhandled_error_renders_the_page_with_security_headers(asgi_app):
    import httpx
    from httpx import ASGITransport

    async def boom():
        raise RuntimeError("boom")

    asgi_app.add_api_route("/__uiaudit_boom", boom)
    transport = ASGITransport(app=asgi_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        page = await c.get("/__uiaudit_boom", headers={"Accept": "text/html"})
        api = await c.get("/__uiaudit_boom", headers={"Accept": "application/json"})
    assert page.status_code == 500
    assert page.headers["content-type"].startswith("text/html")
    assert "<h1" in page.text and "boom" not in page.text
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert api.status_code == 500 and api.json() == {"detail": "Internal Server Error"}
    assert api.headers["X-Content-Type-Options"] == "nosniff"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_error_pages.py::test_an_unhandled_error_renders_the_page_with_security_headers -v`
Expected: FAIL — the response is Starlette's plain-text "Internal Server Error" without `X-Frame-Options`.

- [ ] **Step 3: Implement** — in `src/web/errors.py` add the imports
`from starlette.responses import JSONResponse` and
`from src.web.security_headers import new_nonce, security_header_items`; add
`500: "Something went wrong",` to `ERROR_TITLES`; add above `install_error_handlers`:

```python
async def _server_error(request: Request, exc: Exception) -> Response:
    """Unhandled exceptions. ``ServerErrorMiddleware`` calls this OUTSIDE every user
    middleware, so the security headers are added here. The exception's text is never
    shown; the server logs it when the middleware re-raises."""
    nonce = getattr(request.state, "csp_nonce", None) or new_nonce()
    request.state.csp_nonce = nonce
    if wants_json(request):
        response: Response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    else:
        response = error_page(request, 500, None)
    for name, value in security_header_items(nonce):
        response.headers[name] = value
    return response
```

and make this the last line of `install_error_handlers`:

```python
    app.add_exception_handler(Exception, _server_error)
```

- [ ] **Step 4: Run the error-page tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_error_pages.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/web/errors.py tests/integration/test_error_pages.py
git commit -m "feat(webui): unhandled 500s render the error page with the security headers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-3: Cohort error messages move to flash (A-14)

**Files:**
- Modify: `src/routers/admin/cohorts.py` — `admin_cohorts` (`error=` context key, line 147),
  `admin_cohort_create` (lines 165-175), `admin_cohort_topology` (line 232),
  `admin_cohort_topology_save` (lines 283-310), `admin_ensure_star_spokes` (lines 383-396),
  `admin_cohort_detail` (line 466), `admin_cohort_delete` (lines 470-510),
  `admin_cohort_add_agent` (lines 514-560)
- Modify: `templates/admin/cohorts.html:24-26`, `templates/admin/cohort_detail.html:37-39`,
  `templates/admin/cohort_topology.html:19-21` (the `{% if error %}` banners); after Part 1A
- Modify: `tests/integration/test_cohort_admin.py:202`, `:211`, `:250`, `:505`, `:522`, and the docstring at `:1107`
- Test: `tests/integration/test_cohort_admin.py`

**Interfaces:**
- Consumes: `flash` (1C-1); `session_flashes` (1C-1).
- Produces: no new names. Every cohort route that used to redirect with `?error=<text>` now
  calls `flash(request, <same text>, "error")` and redirects to the same path without the
  query. `notice=` is unchanged.

- [ ] **Step 1: Change the five tests to the flash contract.** In `tests/integration/test_cohort_admin.py`
  add `from tests.flash_support import session_flashes` to the imports, then:
  - `test_create_rejects_a_bad_name`: replace
    `assert r.status_code == 302 and "error=Invalid+name" in r.headers["location"]` with
    ```python
        assert r.status_code == 302 and r.headers["location"] == "/admin/cohorts"
        assert session_flashes(r) == [{
            "text": "Invalid name (lowercase letters, numbers, hyphens; max 48)", "kind": "error",
        }]
    ```
  - `test_create_rejects_a_duplicate_name`: replace the `error=A+cohort+with+that+name` assert with
    `assert session_flashes(r) == [{"text": "A cohort with that name already exists", "kind": "error"}]`
  - `test_add_unknown_agent_is_refused`: replace the `error=Unknown+agent` assert with
    `assert session_flashes(r) == [{"text": "Unknown agent", "kind": "error"}]`
  - `test_topology_save_rejects_an_empty_submission`: replace the `error=Nothing+to+save` assert with
    ```python
        assert r.headers["location"] == "/admin/cohorts/topology"
        assert session_flashes(r) == [{"text": "Nothing to save", "kind": "error"}]
    ```
  - `test_topology_save_rejects_a_tick_outside_the_rendered_set`: replace the
    `error=Malformed+submission` assert with
    `assert session_flashes(r) == [{"text": "Malformed submission", "kind": "error"}]`
  - In the docstring of the test at line 1100-1108 replace
    "``?error=`` redirects stay reserved for bad form input against a cohort that really exists."
    with "Error flashes stay reserved for bad form input against a cohort that really exists."
  Add one test at the end of the membership section:
  ```python
  async def test_a_query_string_error_is_no_longer_rendered(client, db_session, admin):
      """A-14: a link carrying ?error= cannot put words on the page any more."""
      r = await client.get("/admin/cohorts?error=Forged+message", headers=_auth(admin.id))
      assert r.status_code == 200
      assert "Forged message" not in r.text
  ```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_cohort_admin.py -v -k "bad_name or duplicate_name or unknown_agent or empty_submission or outside_the_rendered or no_longer_rendered"`
  Expected: 6 FAIL (locations still carry `?error=`; the forged text still renders).

- [ ] **Step 3: Flash in `src/routers/admin/cohorts.py`.** Add `from src.web.flash import flash`.
  Replace each redirect (same text, no query):
  - `admin_cohort_create`:
    ```python
        if not _COHORT_NAME_RE.match(name):
            flash(request, "Invalid name (lowercase letters, numbers, hyphens; max 48)", "error")
            return RedirectResponse(url="/admin/cohorts", status_code=302)
        existing = await db.execute(select(Cohort).where(Cohort.name == name))
        if existing.scalar_one_or_none():
            flash(request, "A cohort with that name already exists", "error")
            return RedirectResponse(url="/admin/cohorts", status_code=302)
    ```
  - `admin_cohort_topology_save`, the empty-submission guard:
    ```python
            flash(request, "Nothing to save", "error")
            return RedirectResponse(url="/admin/cohorts/topology", status_code=302)
    ```
    and the `if ticked - rendered:` guard:
    ```python
            flash(request, "Malformed submission", "error")
            return RedirectResponse(url="/admin/cohorts/topology", status_code=302)
    ```
  - `admin_ensure_star_spokes`: the `except ValueError` branch becomes
    ```python
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse(url="/admin/cohorts", status_code=302)
    ```
    and the anomaly branch becomes
    ```python
        url = f"/admin/cohorts?notice={quote(notice)}"
        if report.anomalies:
            flash(request, "; ".join(report.anomalies), "error")
        return RedirectResponse(url=url, status_code=302)
    ```
  - `admin_cohort_delete`: add `request: Request,` after `cohort_id: uuid.UUID,`; the populated
    branch becomes
    ```python
        if cohort.memberships:
            flash(
                request,
                f"Remove all {len(cohort.memberships)} members before deleting this cohort",
                "error",
            )
            return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)
    ```
  - `admin_cohort_add_agent`: add `request: Request,` after `cohort_id: uuid.UUID,`; the two
    refusals become `flash(request, "Unknown agent", "error")` and
    `flash(request, "Agent is already a member", "error")`, each followed by
    `return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)`.
  - Delete the `error=request.query_params.get("error"),` line from the context of
    `admin_cohorts`, `admin_cohort_topology` and `admin_cohort_detail`.
  `MAX_FLASH_CHARS` (300) bounds the anomaly text the old code cut with `[:300]`/`[:200]`.

- [ ] **Step 4: Remove the three banners.** Delete these blocks (the flash block in
  `base.html` shows the message):
  - `templates/admin/cohorts.html`: `{% if error %}` … `{% endif %}` (the red `{{ error }}` div)
  - `templates/admin/cohort_detail.html`: the same block
  - `templates/admin/cohort_topology.html`: the same block

- [ ] **Step 5: Run the module.** `.venv-test/bin/python -m pytest tests/integration/test_cohort_admin.py tests/unit/test_reachability.py -v`
  Expected: all pass.

- [ ] **Step 6: Commit.**
```bash
git add src/routers/admin/cohorts.py templates/admin/cohorts.html templates/admin/cohort_detail.html templates/admin/cohort_topology.html tests/integration/test_cohort_admin.py
git commit -m "fix(webui): cohort error messages travel by flash, not query string (A-14)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-4: Simulation and Finalize error messages move to flash (A-14)

**Files:**
- Modify: `src/routers/admin/simulation.py` — `_simulation_context` (`error` parameter and
  context key, lines 92-93 and 175), `admin_simulation` (line 199),
  `admin_simulation_start` (lines 236-271), `admin_simulation_finalize_run` `_refuse`
  (lines 295-298), `admin_simulation_stop` (lines 352-366),
  `admin_simulation_announce_settings` (lines 412-416)
- Modify: `src/routers/admin/runs.py:83` (`error=request.query_params.get("error"),`)
- Modify: `templates/admin/simulation.html:100-102`, `templates/admin/activity_detail.html:19`; after Part 1A and Phase 0
- Modify: `tests/integration/test_admin_simulation_liveness.py:46`, `:55`;
  `tests/integration/test_finalize_run_route.py:93`, `:129-130`
- Test: the two files above

**Interfaces:**
- Consumes: `flash`, `session_flashes` (1C-1).
- Produces: `_simulation_context(db, request, current_user, *, msg=None, template_error=None, template_value_override=None) -> dict`
  (the `error` keyword is gone). Consumed by 1C-8.

- [ ] **Step 1: Change the tests.** In `tests/integration/test_admin_simulation_liveness.py`
  add `from tests.flash_support import session_flashes`, then in
  `test_stop_refused_when_no_engine_holds_the_lock` replace `assert "error=" in resp.headers["location"]` with
  ```python
      assert resp.headers["location"] == "/admin/simulation"
      assert session_flashes(resp) == [{"text": "Nothing is running.", "kind": "error"}]
  ```
  and in `test_start_refused_while_alive` replace it with
  ```python
      assert session_flashes(resp) == [
          {"text": "A run is already starting or in progress.", "kind": "error"}
      ]
  ```
  In `tests/integration/test_finalize_run_route.py` add the same import; in
  `test_finalize_is_refused_while_an_engine_is_alive` replace
  `assert resp.status_code == 302 and "error=" in resp.headers["location"]` with
  ```python
      assert resp.status_code == 302 and resp.headers["location"] == f"/admin/activity/{run.id}"
      assert session_flashes(resp) == [{
          "text": "An engine is running — Finalize run applies to a stopped run.", "kind": "error",
      }]
  ```
  and in `test_start_is_refused_while_a_finalize_stop_is_pending` replace its two location
  asserts with
  ```python
      assert resp.status_code == 302
      assert session_flashes(resp) == [
          {"text": "A Finalize run is pending; start after it finishes.", "kind": "error"}
      ]
  ```
  and drop the now-unused `from urllib.parse import unquote` in that test.

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_admin_simulation_liveness.py tests/integration/test_finalize_run_route.py -v`
  Expected: the four changed tests FAIL (no flash in the session; `?error=` in the location).

- [ ] **Step 3: Flash in `src/routers/admin/simulation.py`.** Add `from src.web.flash import flash`.
  Add one helper above `admin_simulation`:
  ```python
  def _refuse_to(request: Request, url: str, message: str) -> RedirectResponse:
      """Flash ``message`` as an error and redirect to ``url`` (A-14: never in the query)."""
      flash(request, message, "error")
      return RedirectResponse(url=url, status_code=302)
  ```
  Then replace each `RedirectResponse(url=f"/admin/simulation?error={quote(<message>)}", status_code=302)`
  with `_refuse_to(request, "/admin/simulation", <message>)`:
  `'A run is already starting or in progress.'`, `'A Finalize run is pending; start after it finishes.'`,
  `'A start is already pending.'`, `'Nothing is running.'`, `'A stop is already pending.'`, and
  `'Invalid channel name(s): ' + ', '.join(bad)`. In `admin_simulation_finalize_run` replace
  `_refuse`'s body with `return _refuse_to(request, f"/admin/activity/{run_id}", message)`.
  In `_simulation_context` delete the `error: str | None = None,` parameter and the
  `error=error,` context entry; in `admin_simulation` delete `error=request.query_params.get("error"),`.
  `quote` stays imported (the `msg=` redirects use it).

- [ ] **Step 4: Drop the consumers.** In `src/routers/admin/runs.py` delete
  `error=request.query_params.get("error"),`. In `templates/admin/simulation.html` delete the
  `{% if error %}` … `{% endif %}` banner beside the `msg` banner (the `msg` banner stays; it is
  conditional already, so Phase 0's refresh script tolerates its absence). In
  `templates/admin/activity_detail.html` delete the line
  `{% if error %}<div class="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-base text-red-800">{{ error }}</div>{% endif %}`.

- [ ] **Step 5: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_admin_simulation_liveness.py tests/integration/test_finalize_run_route.py tests/integration/test_admin_simulation_page.py tests/integration/test_simulation_page_queries.py -v`
  Expected: all pass.

- [ ] **Step 6: Commit.**
```bash
git add src/routers/admin/simulation.py src/routers/admin/runs.py templates/admin/simulation.html templates/admin/activity_detail.html tests/integration/test_admin_simulation_liveness.py tests/integration/test_finalize_run_route.py
git commit -m "fix(webui): simulation and Finalize refusals travel by flash (A-14)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-5: `slack_error` and `delegate_error` move to flash (A-14)

**Files:**
- Modify: `src/routers/admin/agents.py` — `admin_agent_detail` (line 168), `admin_provision_slack`
  (lines 375-379), `admin_provision_slack_callback.surface_error` (lines 415-419)
- Modify: `src/routers/manager.py` — `manager_pis` (lines 195-199), `manager_pi_detail` (line 244),
  `manager_provision_slack` (lines 549-554), `manager_activate_agent` (lines 573-577),
  `manager_slack_bots` (line 631)
- Modify: `src/routers/agent_page.py` — `agent_dashboard` context (line 272), `invite_delegate`
  (lines 883-889); runs after the §6.4 Connect Slack removal, which edits the same handler
- Modify: `templates/admin/agent_detail.html:23-27`, `templates/manager/pis.html:10-20`,
  `templates/manager/pi_detail.html:81-85`, `templates/manager/slack_bots.html:24-28`,
  `templates/agent/dashboard.html:178-180`; after Part 1A
- Modify: `tests/integration/test_manager_slack_provisioning.py:79-81`, `:207`, `:264`, `:370`, `:409-421`;
  `tests/integration/test_admin_agent_form.py:74`; `tests/integration/test_agent_page.py:237`, `:400-402`
- Test: the files above

**Interfaces:**
- Consumes: `flash`, `session_flashes`, `session_cookie_header` (1C-1).
- Produces: flash texts `"Slack provisioning failed: <message>"` (admin and manager Slack
  routes and the callback) and the joined invite errors (`"; ".join(errors)`).

- [ ] **Step 1: Change the tests.** Add `from tests.flash_support import session_cookie_header, session_flashes`
  to `tests/integration/test_manager_slack_provisioning.py`, `tests/integration/test_admin_agent_form.py`
  and `tests/integration/test_agent_page.py`. Then:
  - `test_provisioning_failure_returns_to_the_manager_page`: replace the last three lines with
    ```python
        assert r.headers["location"] == f"/manager/pis/{pi.id}"
        assert session_flashes(r) == [{
            "text": "Slack provisioning failed: Could not create the Slack app: boom",
            "kind": "error",
        }]
    ```
  - The two callback tests asserting `r.headers["location"].startswith("/manager/pis?slack_error=")`
    (lines 207 and 370): replace that line with
    ```python
        assert r.headers["location"] == "/manager/pis"
        assert session_flashes(r)[0]["text"].startswith("Slack provisioning failed: ")
    ```
  - `test_activate_without_a_slack_token_is_refused`: replace `assert "slack_error" in r.headers["location"]` with
    ```python
        assert r.headers["location"] == f"/manager/pis/{pi.id}"
        assert session_flashes(r) == [
            {"text": "Slack provisioning failed: Install the Slack bot first.", "kind": "error"}
        ]
    ```
  - Replace `test_the_pi_directory_renders_a_slack_error_banner` with:
    ```python
    async def test_the_pi_directory_renders_the_callbacks_flashed_error(client, db_session):
        """The callback's manager-surface errors land on /manager/pis as a flash; a
        ?slack_error= query string no longer puts any text on the page (A-14)."""
        manager = await _manager(db_session)
        r = await client.get(
            "/admin/agents/slack/callback?error=access_denied",
            headers=auth_headers(manager.id), follow_redirects=False,
        )
        assert r.status_code == 302 and r.headers["location"] == "/manager/pis"
        page = await client.get("/manager/pis", headers=session_cookie_header(r))
        assert "Slack provisioning failed: Slack returned: access_denied" in page.text

        forged = await client.get(
            "/manager/pis?slack_error=Forged+text", headers=auth_headers(manager.id)
        )
        assert "Forged text" not in forged.text
    ```
  - `tests/integration/test_admin_agent_form.py::test_provision_error_is_quoted`: replace the
    last assert with
    ```python
        assert r.headers["location"] == f"/admin/agents/{agent.id}"
        assert session_flashes(r) == [
            {"text": "Slack provisioning failed: bad & worse #fragment", "kind": "error"}
        ]
    ```
  - `tests/integration/test_agent_page.py::_invite`: replace
    `assert "delegate_error" not in r.headers["location"], r.headers["location"]` with
    `assert session_flashes(r) == [], session_flashes(r)`.
  - `test_inviting_a_delegate_creates_a_pending_invitation_and_one_email`: replace
    `assert "delegate_error" in r.headers["location"]` and the `unquote(...)` assert with
    ```python
        assert r.headers["location"] == f"/agent/{OWNER_AGENT}/dashboard"
        assert session_flashes(r) == [{"text": "Invalid email: not-an-email", "kind": "error"}]
    ```
    and remove `unquote` from that module's imports if no other test uses it.

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_manager_slack_provisioning.py tests/integration/test_admin_agent_form.py tests/integration/test_agent_page.py -v`
  Expected: the changed tests FAIL (query-string messages, no flash).

- [ ] **Step 3: Flash in the routers.** Add `from src.web.flash import flash` to
  `src/routers/admin/agents.py`, `src/routers/manager.py` and `src/routers/agent_page.py`.
  - `admin_provision_slack`:
    ```python
        except ProvisioningError as exc:
            flash(request, f"Slack provisioning failed: {exc}", "error")
            return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
    ```
  - `admin_provision_slack_callback.surface_error`:
    ```python
        def surface_error(msg: str) -> RedirectResponse:
            flash(request, f"Slack provisioning failed: {msg}", "error")
            return RedirectResponse(
                url="/admin/agents" if is_admin else "/manager/pis", status_code=302
            )
    ```
  - `manager_provision_slack`:
    ```python
        except ProvisioningError as exc:
            flash(request, f"Slack provisioning failed: {exc}", "error")
            return RedirectResponse(url=f"/manager/pis/{user_id}", status_code=302)
    ```
    (add `request: Request,` to its signature after `user_id: uuid.UUID,` if it lacks one).
  - `manager_activate_agent`, the no-token branch:
    ```python
        if not agent.slack_bot_token:
            flash(request, "Slack provisioning failed: Install the Slack bot first.", "error")
            return RedirectResponse(url=f"/manager/pis/{user_id}", status_code=302)
    ```
  - `invite_delegate`:
    ```python
        if errors:
            flash(request, "; ".join(errors), "error")
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
    ```
    replacing the `error_msg = ...` line and both returns after the email loop.
  - Delete the context entries `slack_error=request.query_params.get("slack_error"),` from
    `admin_agent_detail`, `manager_pis` (with the four-line comment above it), `manager_pi_detail`
    and `manager_slack_bots`, and `delegate_error=request.query_params.get("delegate_error"),`
    from `agent_dashboard`.
  - Delete `quote` from `src/routers/admin/agents.py`'s and `src/routers/manager.py`'s imports
    only if `ruff check src/routers/admin/agents.py src/routers/manager.py` reports it unused.

- [ ] **Step 4: Remove the banners.** Delete:
  - `templates/admin/agent_detail.html`: `{% if slack_error %}` … `{% endif %}`
  - `templates/manager/pis.html`: the `{# Same markup as manager/pi_detail.html's … could only ever be a fake. #}`
    comment and the `{% if slack_error and effective_user.is_staff %}` … `{% endif %}` block
  - `templates/manager/pi_detail.html`: `{% if slack_error %}` … `{% endif %}`
  - `templates/manager/slack_bots.html`: `{% if slack_error %}` … `{% endif %}`
  - `templates/agent/dashboard.html`: `{% if delegate_error %}` … `{% endif %}`

- [ ] **Step 5: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_manager_slack_provisioning.py tests/integration/test_admin_agent_form.py tests/integration/test_agent_page.py tests/integration/test_manager_views.py tests/unit/test_reachability.py -v`
  Expected: all pass, the manager write-allowlist test included (no route added).

- [ ] **Step 6: Commit.**
```bash
git add src/routers/admin/agents.py src/routers/manager.py src/routers/agent_page.py templates/admin/agent_detail.html templates/manager/pis.html templates/manager/pi_detail.html templates/manager/slack_bots.html templates/agent/dashboard.html tests/integration/test_manager_slack_provisioning.py tests/integration/test_admin_agent_form.py tests/integration/test_agent_page.py
git commit -m "fix(webui): Slack and delegate errors travel by flash, not query string (A-14)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-6: One active hub; role changes run the gate; no return to `pending` (C-04, C-05, D14)

**Files:**
- Modify: `src/services/advisory_locks.py:40-41` (after `ADMIN_INVARIANT_LOCK_KEY`)
- Modify: `src/services/agent_activation.py` (module docstring, `activation_blockers`, `activate_agent`; add `ensure_activation_allowed`)
- Modify: `src/routers/admin/agents.py` — `admin_approve_agent` (lines 278-312), `admin_set_agent_role` (lines 468-496)
- Modify: `src/routers/manager.py:579-583` (`manager_activate_agent` refusal)
- Modify: `templates/admin/agent_detail.html:32-36` (form_error codes) and `:101-105` (status options); after Part 1A
- Modify: `tests/unit/test_advisory_locks.py:13`
- Test: `tests/integration/test_hub_activation_gate.py`

**Interfaces:**
- Consumes: `flash`, `session_flashes` (1C-1); `hub_role_names`, `star_role`, `requires_linked_user` (`src/agent/role_capabilities.py`); `fixed_key`, `advisory_lock_held` (`src/services/advisory_locks.py`).
- Produces:
  - `src/services/advisory_locks.py`: `HUB_ROSTER_LOCK_KEY: int = fixed_key("hub_roster")`.
  - `src/services/agent_activation.py`:
    `async def activation_blockers(db: AsyncSession, agent: AgentRegistry, *, role: str | None = None) -> list[str]`
    (`role` defaults to `agent.role`);
    `async def ensure_activation_allowed(db: AsyncSession, agent: AgentRegistry, *, new_role: str, new_status: str, override: bool = False) -> list[str]`
    (the contract signature plus a keyword `override`, default False; `override` waives only
    the profile blockers, never the hub limit);
    `HUB_ALREADY_ACTIVE = "another hub-role agent ({agent_id}) is already active; deactivate it first"`.
  - `activate_agent(db, agent, *, actor, override) -> list[str]` keeps its signature and now
    gates through `ensure_activation_allowed`.
  - Agent-form error code `pending_not_allowed`.

- [ ] **Step 1: Write the failing tests.** Create `tests/integration/test_hub_activation_gate.py`:

```python
"""One active hub at a time (spec §6.8 C-05, decision D14), role changes on an active
agent run the activation gate for the new role, and an approved agent cannot be sent
back to `pending` (C-04)."""
import inspect

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, AgentRegistry
from src.services import agent_activation
from src.services.advisory_locks import HUB_ROSTER_LOCK_KEY, advisory_lock_held
from src.services.agent_activation import ensure_activation_allowed
from src.services.agent_form import agent_form_version
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_LIVE_HUB_BLOCKER = "another hub-role agent (hub-live) is already active; deactivate it first"


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


async def _live_hub(db_session):
    return await factories.make_agent(
        db_session, agent_id="hub-live", role="scout_hub", status="active"
    )


async def _role_of(db_session, agent):
    return (await db_session.execute(
        select(AgentRegistry.role).where(AgentRegistry.id == agent.id)
    )).scalar_one()


async def test_a_second_active_hub_is_refused(db_session):
    await _live_hub(db_session)
    spare = await factories.make_agent(
        db_session, agent_id="hub-spare", role="scout_hub", status="inactive"
    )
    assert await ensure_activation_allowed(
        db_session, spare, new_role="scout_hub", new_status="active"
    ) == [_LIVE_HUB_BLOCKER]


async def test_the_hub_limit_survives_the_override(db_session):
    await _live_hub(db_session)
    spare = await factories.make_agent(
        db_session, agent_id="hub-spare2", role="scout_hub", status="inactive"
    )
    assert await ensure_activation_allowed(
        db_session, spare, new_role="scout_hub", new_status="active", override=True
    ) == [_LIVE_HUB_BLOCKER]


async def test_a_lone_hub_may_activate_and_the_roster_lock_is_taken(db_session):
    spare = await factories.make_agent(
        db_session, agent_id="hub-only", role="scout_hub", status="inactive"
    )
    assert await ensure_activation_allowed(
        db_session, spare, new_role="scout_hub", new_status="active"
    ) == []
    assert await advisory_lock_held(db_session, HUB_ROSTER_LOCK_KEY)


def test_the_lock_precedes_the_hub_count():
    """Race-proof only if the transaction lock is taken before the count reads."""
    src = inspect.getsource(agent_activation.ensure_activation_allowed)
    assert src.index("pg_advisory_xact_lock") < src.index("hub_role_names()")


async def test_a_status_other_than_active_needs_no_gate(db_session):
    agent = await factories.make_agent(db_session, status="active")
    assert await ensure_activation_allowed(
        db_session, agent, new_role="pi_lab", new_status="inactive"
    ) == []


async def test_an_inactive_agent_may_take_a_hub_role(client, db_session):
    admin = await _admin(db_session)
    await _live_hub(db_session)
    parked = await factories.make_agent(db_session, agent_id="parked", status="inactive")
    r = await client.post(
        f"/admin/agents/{parked.id}/role", data={"role": "scout_hub"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, parked) == "scout_hub"


async def test_a_role_change_runs_the_gate_for_the_new_role(client, db_session):
    """An active hub has no linked user; as a pi_lab it would fail the profile gate."""
    admin = await _admin(db_session)
    hub = await _live_hub(db_session)
    r = await client.post(
        f"/admin/agents/{hub.id}/role", data={"role": "pi_lab"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == f"/admin/agents/{hub.id}"
    assert await _role_of(db_session, hub) == "scout_hub"
    flashes = session_flashes(r)
    assert flashes[0]["kind"] == "error"
    assert flashes[0]["text"].startswith("Role not changed: not linked to a user account")


async def test_an_active_lab_cannot_become_a_second_hub(client, db_session):
    admin = await _admin(db_session)
    await _live_hub(db_session)
    user = await factories.make_user(db_session)
    lab = await factories.make_agent(db_session, user=user, agent_id="lab-x", status="active")
    r = await client.post(
        f"/admin/agents/{lab.id}/role", data={"role": "scout_hub"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, lab) == "pi_lab"
    assert session_flashes(r) == [
        {"text": f"Role not changed: {_LIVE_HUB_BLOCKER}", "kind": "error"}
    ]


async def test_the_edit_form_cannot_activate_a_second_hub_even_with_the_override(
    client, db_session
):
    admin = await _admin(db_session)
    await _live_hub(db_session)
    spare = await factories.make_agent(
        db_session, agent_id="hub-spare3", role="scout_hub", status="inactive"
    )
    r = await client.post(
        f"/admin/agents/{spare.id}/approve",
        data={
            "agent_slug": spare.agent_id, "bot_name": spare.bot_name,
            "form_version": agent_form_version(spare), "agent_status": "active",
            "activation_override": "1",
        },
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked=1" in r.headers["location"]
    await db_session.refresh(spare)
    assert spare.status == "inactive"
    assert session_flashes(r) == [
        {"text": f"Activation refused: {_LIVE_HUB_BLOCKER}", "kind": "error"}
    ]


async def test_the_edit_form_offers_pending_only_to_a_pending_agent(client, db_session):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=user, status="active")
    r = await client.get(f"/admin/agents/{agent.id}", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert '<option value="inactive"' in r.text
    assert '<option value="pending"' not in r.text


async def test_an_approved_agent_cannot_be_sent_back_to_pending(client, db_session):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=user, status="active")
    r = await client.post(
        f"/admin/agents/{agent.id}/approve",
        data={
            "agent_slug": agent.agent_id, "bot_name": agent.bot_name,
            "form_version": agent_form_version(agent), "agent_status": "pending",
        },
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/agents/{agent.id}?error=pending_not_allowed"
    await db_session.refresh(agent)
    assert agent.status == "active"
```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_hub_activation_gate.py -v`
  Expected: collection error, `ImportError: cannot import name 'HUB_ROSTER_LOCK_KEY'`.

- [ ] **Step 3: Register the lock key.** In `src/services/advisory_locks.py` after
  `ADMIN_INVARIANT_LOCK_KEY`:
  ```python
  #: Taken per transaction by every activation of a hub-role agent, so two concurrent
  #: activations cannot both see "no other active hub" (spec §6.8 C-05, D14).
  HUB_ROSTER_LOCK_KEY: int = fixed_key("hub_roster")
  ```
  In `tests/unit/test_advisory_locks.py::test_fixed_keys_are_distinct_from_each_other_and_the_spend_lock`:
  before: `keys = [al.ENGINE_LOCK_KEY, al.WORKER_LOCK_KEY, al.ADMIN_INVARIANT_LOCK_KEY, _SPEND_LOCK_KEY]`
  after: `keys = [al.ENGINE_LOCK_KEY, al.WORKER_LOCK_KEY, al.ADMIN_INVARIANT_LOCK_KEY, al.HUB_ROSTER_LOCK_KEY, _SPEND_LOCK_KEY]`

- [ ] **Step 4: Implement the gate in `src/services/agent_activation.py`.**
  - Module docstring: replace its last paragraph ("``pi_lab``-scoped: … logged by the caller.")
    with:
    ```text
    ``pi_lab``-scoped: the hub and specialist roles have no PI profile by design.
    The override is an explicit form field; ``activate_agent`` logs it with the actor.

    ``ensure_activation_allowed`` adds the roster rule of D14 (spec §6.8 C-05): no path may
    leave two ``active`` hub-role agents, and a role change on an active agent is checked
    against the NEW role. Inactive agents may hold a hub role. The hub check runs under
    a transaction-scoped advisory lock (``HUB_ROSTER_LOCK_KEY``) taken before the count, so
    two concurrent activations serialize and the second sees the first's committed row.
    ```
  - Imports: add `from sqlalchemy import select, text` (replacing `from sqlalchemy import select`),
    `from src.agent.role_capabilities import hub_role_names, requires_linked_user, star_role`
    (replacing the single-name import), and `from src.services.advisory_locks import HUB_ROSTER_LOCK_KEY`.
  - `activation_blockers`:
    before:
    ```python
    async def activation_blockers(db: AsyncSession, agent: AgentRegistry) -> list[str]:
        """Reasons this agent must not be flipped to ``active``; [] when clear."""
        if not requires_linked_user(agent.role):
            return []
    ```
    after:
    ```python
    async def activation_blockers(
        db: AsyncSession, agent: AgentRegistry, *, role: str | None = None
    ) -> list[str]:
        """Reasons this agent must not be flipped to ``active`` as ``role`` (default: its
        current role); [] when clear."""
        if not requires_linked_user(role if role is not None else agent.role):
            return []
    ```
  - Add after `activation_blockers`:
    ```python
    #: The hub-limit refusal; ``agent_id`` is the slug of the hub already active.
    HUB_ALREADY_ACTIVE = "another hub-role agent ({agent_id}) is already active; deactivate it first"


    async def ensure_activation_allowed(
        db: AsyncSession, agent: AgentRegistry, *, new_role: str, new_status: str,
        override: bool = False,
    ) -> list[str]:
        """Reasons ``agent`` must not end up ``new_status`` with ``new_role``; [] when allowed.

        Only ``new_status == "active"`` is gated. The profile blockers of
        ``activation_blockers`` are checked for ``new_role``; ``override`` waives them
        (``activate_agent`` logs what it waived), never the hub limit. A hub ``new_role`` takes ``HUB_ROSTER_LOCK_KEY``
        for the rest of the caller's transaction BEFORE counting other active hubs. Writes
        nothing; the caller applies the change and commits.
        """
        if new_status != "active":
            return []
        blockers = [] if override else await activation_blockers(db, agent, role=new_role)
        if star_role(new_role) == "hub":
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": HUB_ROSTER_LOCK_KEY}
            )
            other = await db.scalar(
                select(AgentRegistry.agent_id)
                .where(
                    AgentRegistry.status == "active",
                    AgentRegistry.role.in_(hub_role_names()),
                    AgentRegistry.id != agent.id,
                )
                .limit(1)
            )
            if other is not None:
                blockers.append(HUB_ALREADY_ACTIVE.format(agent_id=other))
        if blockers:
            logger.warning(
                "Refused activation of agent %s (%s) as %s: %s",
                agent.agent_id, agent.id, new_role, "; ".join(blockers),
            )
        return blockers
    ```
  - `activate_agent`: replace its body down to `was_pending = ...` with:
    ```python
        if override:
            # The override waives the profile blockers only; log what it waived, with
            # the actor, as before. The hub limit is never waived.
            waived = await activation_blockers(db, agent)
            if waived:
                logger.warning(
                    "Activation OVERRIDE by %s for agent %s (%s) despite: %s",
                    actor.id, agent.agent_id, agent.id, "; ".join(waived),
                )
        blockers = await ensure_activation_allowed(
            db, agent, new_role=agent.role, new_status="active", override=override,
        )
        if blockers:
            return blockers
        was_pending = agent.status == "pending"
    ```
    and in its docstring replace "Check the gate and flip ``agent`` to ``active``, or refuse."
    with "Check ``ensure_activation_allowed`` and flip ``agent`` to ``active``, or refuse."

- [ ] **Step 5: Gate the routes.** In `src/routers/admin/agents.py` add
  `from src.web.flash import flash` and
  `from src.services.agent_activation import activate_agent, activation_blockers, ensure_activation_allowed`
  (replacing the two-name import).
  - `admin_approve_agent`, directly after the `if form_version != agent_form_version(agent):` return:
    ```python
        if agent_status == "pending" and agent.status != "pending":
            # C-04: `pending` re-opens the slug rename and the auto-activation branch.
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?error=pending_not_allowed", status_code=302
            )
    ```
    and the `if blockers:` branch of the activation becomes:
    ```python
            if blockers:
                flash(request, "Activation refused: " + "; ".join(blockers), "error")
                return RedirectResponse(
                    url=f"/admin/agents/{agent_id}?activation_blocked=1",
                    status_code=302,
                )
    ```
    Docstring: append "``agent_status=pending`` is refused for an agent that is no longer pending
    (C-04). A hub-role activation is refused while another hub is active, override or not
    (D14)."
  - `admin_set_agent_role`, between the unknown-role check and `agent.role = role`:
    ```python
        if role != agent.role and agent.status == "active":
            blockers = await ensure_activation_allowed(
                db, agent, new_role=role, new_status="active"
            )
            if blockers:
                flash(request, "Role not changed: " + "; ".join(blockers), "error")
                return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
    ```
    Docstring: append "On an ACTIVE agent the new role must pass the activation gate,
    including the one-active-hub limit (D14); an inactive agent may take any valid role."
  - `src/routers/manager.py::manager_activate_agent`, the `if blockers:` branch:
    ```python
        if blockers:
            flash(request, "Activation refused: " + "; ".join(blockers), "error")
            return RedirectResponse(
                url=f"/manager/pis/{user_id}?activation_blocked=1", status_code=302
            )
    ```

- [ ] **Step 6: Template.** In `templates/admin/agent_detail.html`:
  - status options: before `{% for s in valid_statuses %}` after
    `{% for s in valid_statuses if s != 'pending' %}` (the select only renders for a non-pending
    agent, so `pending` is never offered; C-04);
  - form_error codes: before
    `{% elif form_error == 'invalid_slug' %}An agent ID (slug) is 1–50 lowercase letters, digits, hyphens or underscores.`
    add the line
    `{% elif form_error == 'pending_not_allowed' %}An approved agent cannot be returned to pending.`

- [ ] **Step 7: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_hub_activation_gate.py tests/unit/test_advisory_locks.py tests/unit/test_agent_activation.py tests/integration/test_agent_activation_gate.py tests/integration/test_admin_agent_form.py tests/integration/test_cohort_admin.py tests/integration/test_manager_slack_provisioning.py -v`
  Expected: all pass.

- [ ] **Step 8: Commit.**
```bash
git add src/services/advisory_locks.py src/services/agent_activation.py src/routers/admin/agents.py src/routers/manager.py templates/admin/agent_detail.html tests/unit/test_advisory_locks.py tests/integration/test_hub_activation_gate.py
git commit -m "fix(webui): one active hub under an advisory lock; role changes gated; no return to pending (C-04, C-05)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-7: Topology save diffs against what the form showed (C-07)

**Files:**
- Modify: `src/routers/admin/cohorts.py` — `admin_cohort_topology_save` (docstring lines 247-274, body lines 275-353)
- Modify: `templates/admin/cohort_topology.html:12-17` (intro paragraph) and `:84-92` (cell); after Part 1A and 1C-3
- Modify: `tests/integration/test_cohort_admin.py` — `test_topology_save_applies_adds_and_removes_in_one_pass`,
  `test_topology_save_audits_every_change`, `test_topology_save_only_touches_rendered_cells`,
  `test_matrix_save_never_touches_an_unrendered_cohort`, `test_topology_save_round_trips_with_marker_payload`
- Test: `tests/integration/test_cohort_admin.py`

**Interfaces:**
- Consumes: `flash` (1C-3 already imports it in `cohorts.py`); `_hidden_marker_values` (existing helper in `test_cohort_admin.py`).
- Produces: form field `was_checked` (one per cell rendered checked, value `"{cohort_id}:{agent_id}"`).
  Consumed by `journey_cohort_two_tab_save` (1C-16) through the rendered page.

- [ ] **Step 1: Write the failing tests and move the existing ones to the new payload.** In
  `tests/integration/test_cohort_admin.py`:
  - add `"was_checked": [f"{a.id}:su"],` to the posted `data` of
    `test_topology_save_applies_adds_and_removes_in_one_pass`, `test_topology_save_audits_every_change`,
    `test_topology_save_only_touches_rendered_cells` and `test_matrix_save_never_touches_an_unrendered_cohort`
    (each removes `alpha:su`, which the real page would have rendered checked);
  - add `"was_checked": [f"{c1.id}:ta2"],` to `test_topology_save_round_trips_with_marker_payload`;
  - append these tests after `test_topology_save_ignores_unknown_ids`:
  ```python
  async def test_a_stale_tab_does_not_undo_another_tabs_add(client, db_session, admin, roster):
      """C-07: tab B rendered before tab A ticked su; B never showed su ticked, so its
      save must not remove it."""
      a = await _cohort(db_session, "two-tab", admin)
      await db_session.commit()
      markers = {"present_cohort": [str(a.id)], "present_agent": ["su", "wiseman"]}
      first = await client.post(
          "/admin/cohorts/topology", data={**markers, "cell": [f"{a.id}:su"]},
          headers=_auth(admin.id),
      )
      second = await client.post(
          "/admin/cohorts/topology", data={**markers, "cell": [f"{a.id}:wiseman"]},
          headers=_auth(admin.id),
      )
      assert first.status_code == second.status_code == 302
      rows = {
          (str(m.cohort_id), m.agent_id)
          for m in (await db_session.execute(select(CohortMembership))).scalars().all()
      }
      assert rows == {(str(a.id), "su"), (str(a.id), "wiseman")}


  async def test_a_cell_shown_checked_and_still_checked_is_left_alone(
      client, db_session, admin, roster
  ):
      """Another tab removed alpha:su after this form rendered it checked; leaving the box
      ticked is not a request to re-add it."""
      a = await _cohort(db_session, "kept-out", admin)
      await db_session.commit()
      r = await client.post(
          "/admin/cohorts/topology",
          data={
              "present_cohort": [str(a.id)], "present_agent": ["su"],
              "cell": [f"{a.id}:su"], "was_checked": [f"{a.id}:su"],
          },
          headers=_auth(admin.id),
      )
      assert "0+added,+0+removed" in r.headers["location"]
      assert (await db_session.execute(select(CohortMembership))).scalars().all() == []


  async def test_a_was_checked_marker_outside_the_rendered_set_is_malformed(
      client, db_session, admin, roster
  ):
      a = await _cohort(db_session, "alpha", admin, members=["wiseman"])
      await db_session.commit()
      r = await client.post(
          "/admin/cohorts/topology",
          data={
              "present_cohort": [str(a.id)], "present_agent": ["su"],
              "was_checked": [f"{a.id}:wiseman"],
          },
          headers=_auth(admin.id),
      )
      assert session_flashes(r) == [{"text": "Malformed submission", "kind": "error"}]
      assert len((await db_session.execute(select(CohortMembership))).scalars().all()) == 1


  async def test_the_matrix_marks_each_cell_it_renders_checked(client, db_session, admin, roster):
      a = await _cohort(db_session, "alpha", admin, members=["su"])
      r = await client.get("/admin/cohorts/topology", headers=_auth(admin.id))
      assert _hidden_marker_values(r.text, "was_checked") == {f"{a.id}:su"}
  ```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_cohort_admin.py -v -k "topology or matrix or stale_tab or still_checked or was_checked"`
  Expected: `test_a_stale_tab_does_not_undo_another_tabs_add` FAILS (the second save deletes
  `su`), `test_a_cell_shown_checked_and_still_checked_is_left_alone` FAILS (`1+added`),
  `test_a_was_checked_marker_outside_...` FAILS (no flash), `test_the_matrix_marks_...` FAILS
  (no markers); the edited existing tests still pass (an extra field is ignored today).

- [ ] **Step 3: Implement the diff.** In `admin_cohort_topology_save`:
  - after `present_cohorts = {...}` add
    ```python
        was_checked = {v for v in form.getlist("was_checked") if isinstance(v, str)}
    ```
  - after `ticked = {t for t in ticked if _known_cell(t)}` add
    ```python
        was_checked = {t for t in was_checked if _known_cell(t)}
    ```
  - before: `if ticked - rendered:` after: `if (ticked | was_checked) - rendered:`
  - in the loop, before:
    ```python
            want = cell in ticked
            have = (cid, aid) in existing
            if want and not have:
    ```
    after:
    ```python
            want = cell in ticked
            was = cell in was_checked
            have = (cid, aid) in existing
            if want and not was and not have:
    ```
    and before: `elif have and not want:` after: `elif was and not want and have:`
  - Docstring: replace its first two paragraphs ("Apply a whole-matrix edit as a diff against
    the cells that were rendered." through "...is why the matrix could not be saved at all.")
    with:
    ```text
    Apply a whole-matrix edit as a diff against what the form SHOWED (C-07).

    The form posts one ``cell`` per box ticked now (``{cohort_id}:{agent_id}``), one
    ``was_checked`` per box it rendered ticked, one ``present_agent`` per rendered row and one
    ``present_cohort`` per rendered column; the rendered cell set is the cross product of the
    markers, which is what the template renders (an unconditional nested loop). A cell is
    added only when ticked now and not rendered ticked, and removed only when rendered ticked
    and unticked now. Diffing against the current table instead let a second tab, rendered
    before another tab's save, delete every membership that save added. A form from before
    this field existed posts no ``was_checked`` and can therefore only add. Markers instead
    of one hidden input per cell keep the payload at agents + cohorts + memberships fields:
    60x56 posted 3,528 fields and hit Starlette's ``max_fields=1000``.
    ```
    and in the paragraph starting "``present_cohort``/``present_agent`` are filtered down"
    replace "``ticked`` is filtered the same way" with "``ticked`` and ``was_checked`` are filtered the same way".

- [ ] **Step 4: Template.** In `templates/admin/cohort_topology.html`:
  - the cell, before:
    ```jinja
                        <td class="px-3 py-3 text-center">
                            <input type="checkbox" name="cell" value="{{ cell }}"
    ```
    after:
    ```jinja
                        <td class="px-3 py-3 text-center">
                            {% if cell in membership_set %}<input type="hidden" name="was_checked" value="{{ cell }}">{% endif %}
                            <input type="checkbox" name="cell" value="{{ cell }}"
    ```
  - the intro paragraph's last sentence, before:
    "Only the cells shown here are compared, so a filtered or stale view can never delete a membership it did not display."
    after:
    "Only boxes you change are saved: a box shown ticked and unticked by you is removed, a box you tick is added, so a second tab or a stale view never undoes another save."

- [ ] **Step 5: Run the module.** `.venv-test/bin/python -m pytest tests/integration/test_cohort_admin.py -v`
  Expected: all pass, `test_matrix_save_is_one_transaction` included (one commit, after
  `for cell in sorted(rendered)`), and `test_full_matrix_payload_stays_under_the_field_limit`.

- [ ] **Step 6: Commit.**
```bash
git add src/routers/admin/cohorts.py templates/admin/cohort_topology.html tests/integration/test_cohort_admin.py
git commit -m "fix(webui): topology save changes only the boxes the form changed (C-07)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-8: Confirm dialogs for Finalize, the announcing Stop, Reset and review delete (C-10, FN-01, FN-06, B-10)

**Where the Stop counts come from.** `_simulation_context` renders the Live tab through
`live_tab_context` (`src/services/simulation_view.py:471`), whose funnel is for
`_resolve_selected_run` — the `?run=` selection, defaulting to the newest run by
`started_at` (`simulation_view.py:139-157`). That is not necessarily the run the engine is
executing. The live run is the heartbeat row's `SimulationProcessStatus.simulation_run_id`
(written by the engine's poll, `src/models/simulation_control.py:53-62`), read by
`read_panel_state` into `status_row`. The Stop counts are therefore computed separately from
`funnel(db, status_row.simulation_run_id)` when `engine_is_alive`.

**Files:**
- Modify: `src/routers/admin/simulation.py` — imports, add `_stop_counts`, `_simulation_context`
  (context entry), `admin_simulation_finalize_run` (signature and first check)
- Modify: `templates/admin/simulation.html` — the announcing Stop form (`<form action="/admin/simulation/stop" method="post">`
  without `hold_open`, lines 247-253) and the reset form (`<input type="hidden" name="reset" value="true">`, lines 331-338); after Phase 0 and Part 1A
- Modify: `templates/admin/activity_detail.html:26-33` (Finalize form); after 1C-4
- Modify: `templates/admin/_assessment_detail_body.html:1134` (review delete form); after Part 1A
- Modify: `tests/integration/test_finalize_run_route.py:74`, `:91` (post `confirm_run`)
- Test: `tests/integration/test_simulation_confirms.py`

**Interfaces:**
- Consumes: `data-confirm` handling in `static/js/confirm.js` (Phase 0); `funnel`
  (`src/services/simulation_stats.py:722`); `_refuse_to` (1C-4); `session_flashes` (1C-1);
  `_alive` (`tests/integration/test_admin_simulation_liveness.py:13`).
- Produces: `async def _stop_counts(db: AsyncSession, status_row, engine_is_alive: bool) -> dict[str, int] | None`
  (`{"owed": headlines_owed + provisional, "open": provisional, "cap": STOP_ANNOUNCE_CAP, "posts": min(owed, STOP_ANNOUNCE_CAP)}`); module constant `STOP_ANNOUNCE_CAP = 25`; context key `stop_counts`;
  form field `confirm_run` on `POST /admin/simulation/finalize-run`; `id="confirm-run"` on the
  activity page.

- [ ] **Step 1: Write the failing tests.** Create `tests/integration/test_simulation_confirms.py`:

```python
"""Confirm dialogs for the irreversible admin actions (spec §6.8, D11)."""
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    AssessmentReview,
    OpportunityAssessment,
    SimulationCommand,
    SimulationProcessStatus,
    SimulationRun,
    ThreadDecision,
)
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.flash_support import session_flashes
from tests.integration.test_admin_simulation_liveness import _alive
from tests.integration.test_finalize_run_route import _stopped_run
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _forms(html: str) -> list[tuple[str, str]]:
    """``(opening <form> tag, whole form markup)`` for every form on the page."""
    out = []
    for match in re.finditer(r"<form\b[^>]*>.*?</form>", html, re.S):
        out.append((re.match(r"<form\b[^>]*>", match.group(0)).group(0), match.group(0)))
    return out


def _form_tag(html: str, marker: str) -> str:
    """The opening <form ...> tag of the first form whose markup contains ``marker``."""
    for tag, body in _forms(html):
        if marker in body:
            return tag
    raise AssertionError(f"no form containing {marker!r}")


def _stop_forms(html: str) -> tuple[str, str]:
    """The opening tags of the announcing Stop form and of "Stop — hold open interviews"."""
    stops = [(t, b) for t, b in _forms(html) if 'action="/admin/simulation/stop"' in t]
    announcing = [t for t, b in stops if 'name="hold_open"' not in b]
    hold = [t for t, b in stops if 'name="hold_open"' in b]
    assert len(announcing) == 1 and len(hold) == 1, stops
    return announcing[0], hold[0]


def _verdict(run, thread_id, posted=False):
    return OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="c", thread_id=thread_id,
        summary_posted_at=datetime.now(UTC) if posted else None,
    )


def _closed(run, thread_id):
    return ThreadDecision(simulation_run_id=run.id, thread_id=thread_id, channel="c",
                          agent_a="blackbird", agent_b="lab", outcome="timeout")


async def test_the_announcing_stop_names_the_live_runs_counts(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    now = datetime.now(UTC)
    live = SimulationRun(status="running", started_at=now - timedelta(hours=3))
    newer = SimulationRun(status="stopped", started_at=now - timedelta(hours=1))
    db_session.add_all([live, newer])
    await db_session.flush()
    # Live run: one closed and owed, one closed and posted, two still open -> 3 owed, 2 open.
    db_session.add_all([
        _verdict(live, "closed-owed"), _closed(live, "closed-owed"),
        _verdict(live, "closed-posted", posted=True), _closed(live, "closed-posted"),
        _verdict(live, "open-1"), _verdict(live, "open-2"),
    ])
    # The newer run is the page's default selection; its numbers must not leak in.
    db_session.add_all([_verdict(newer, f"newer-{n}") for n in range(5)])
    db_session.add(SimulationProcessStatus(id=1, state="running", simulation_run_id=live.id,
                                           updated_at=now))
    await db_session.commit()

    r = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    stop, hold = _stop_forms(r.text)
    assert ('data-confirm="Posts 3 of 3 owed headlines to Slack (2 from interviews '
            'still open; at most 25 per stop); cannot be undone"') in stop
    assert "data-confirm" not in hold


async def test_the_stop_dialog_without_a_live_run_id_still_warns(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    db_session.add(SimulationProcessStatus(id=1, state="starting", updated_at=datetime.now(UTC)))
    await db_session.commit()
    r = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    stop, _hold = _stop_forms(r.text)
    assert ('data-confirm="Posts every owed headline to Slack, including interviews still '
            'open; cannot be undone"') in stop


async def test_reset_to_file_default_asks_first(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    reset = _form_tag(r.text, 'name="reset" value="true"')
    assert 'data-confirm="Reset the run-start announcement to the file default? ' in reset


async def test_finalize_asks_and_carries_the_short_id_field(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    short = str(run.id)[:8]
    r = await client.get(f"/admin/activity/{run.id}", headers=auth_headers(admin.id))
    form = _form_tag(r.text, 'action="/admin/simulation/finalize-run"')
    assert f'data-confirm="Finalize run {short}: ' in form
    assert 'name="confirm_run"' in r.text and '<label for="confirm-run"' in r.text


async def test_finalize_with_the_wrong_short_id_is_refused(client, db_session, monkeypatch):
    _alive(monkeypatch, False)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    short = str(run.id)[:8]
    for wrong in ("", "00000000", str(run.id)):
        r = await client.post(
            "/admin/simulation/finalize-run", data={"run_id": str(run.id), "confirm_run": wrong},
            headers=auth_headers(admin.id), follow_redirects=False,
        )
        assert r.headers["location"] == f"/admin/activity/{run.id}", wrong
        assert session_flashes(r) == [{
            "text": f"Type the run's short id ({short}) to confirm Finalize run.", "kind": "error",
        }], wrong
    assert (await db_session.execute(select(SimulationCommand))).scalars().all() == []


async def test_finalize_with_the_right_short_id_enqueues(client, db_session, monkeypatch):
    _alive(monkeypatch, False)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    r = await client.post(
        "/admin/simulation/finalize-run",
        data={"run_id": str(run.id), "confirm_run": f"  {str(run.id)[:8].upper()} "},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    cmd = (await db_session.execute(select(SimulationCommand))).scalar_one()
    assert cmd.payload == {"finalize": True, "run_id": str(run.id)}


async def test_review_delete_asks_first(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    interview = await seed_interview(db_session)
    review = AssessmentReview(assessment_id=interview.assessment_id, reviewer_user_id=admin.id,
                              reviewer_name=admin.name, score=3, comment="c", feedback_mode="learn")
    db_session.add(review)
    await db_session.flush()
    r = await client.get(f"/admin/assessments/{interview.assessment_id}",
                         headers=auth_headers(admin.id))
    form = _form_tag(r.text, f'action="/reviews/feedback/{review.id}/delete"')
    assert 'data-confirm="Delete this review? This cannot be undone."' in form
```

Also create `tests/unit/test_stop_announce_cap.py`:

```python
"""The Stop dialog's cap must equal the engine's shutdown cap."""
from src.agent.engine.constants import HEADLINES_MAX_AT_SHUTDOWN
from src.routers.admin.simulation import STOP_ANNOUNCE_CAP


def test_the_stop_dialog_cap_matches_the_engine():
    assert STOP_ANNOUNCE_CAP == HEADLINES_MAX_AT_SHUTDOWN
```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_simulation_confirms.py tests/unit/test_stop_announce_cap.py -v`
  Expected: every test FAILS except `test_finalize_with_the_right_short_id_enqueues` (no
  `data-confirm` anywhere; no `confirm_run` check, so the wrong-id posts enqueue).

- [ ] **Step 3: Server.** In `src/routers/admin/simulation.py`:
  - import `from src.services.simulation_stats import funnel`;
  - add above `_simulation_context`:
    ```python
    #: The engine posts at most this many owed headlines on a Stop
    #: (src/agent/engine/constants.py HEADLINES_MAX_AT_SHUTDOWN). Duplicated, not
    #: imported: that module imports src.agent.agent; the equality is pinned by
    #: tests/unit/test_stop_announce_cap.py.
    STOP_ANNOUNCE_CAP = 25


    async def _stop_counts(db: AsyncSession, status_row, engine_is_alive: bool) -> dict[str, int] | None:
        """The announcing Stop's dialog numbers, from the LIVE run's funnel (FN-01).

        The page's Live tab shows the ``?run=`` selection, which defaults to the newest run
        by ``started_at`` and need not be the one the engine is executing; the heartbeat
        row names the live run. A Stop announces every owed headline of that run, open
        interviews included: ``owed`` is the terminal ones owed plus the provisional
        (still open) ones, ``open`` the provisional ones. None when no engine holds the
        lock or the heartbeat has not named a run yet; the dialog then words it without
        numbers.
        """
        if not engine_is_alive or status_row is None or status_row.simulation_run_id is None:
            return None
        live = await funnel(db, status_row.simulation_run_id)
        owed = live.headlines_owed + live.provisional
        return {"owed": owed, "open": live.provisional, "cap": STOP_ANNOUNCE_CAP,
                "posts": min(owed, STOP_ANNOUNCE_CAP)}
    ```
  - in `_simulation_context`, after `live_tab = await live_tab_context(...)`:
    `stop_counts = await _stop_counts(db, status_row, engine_is_alive)` and add
    `stop_counts=stop_counts,` to the returned context beside `held_counts=held_counts,`;
  - `admin_simulation_finalize_run`: add the parameter `confirm_run: str = Form(""),` after
    `run_id: uuid.UUID = _RUN_ID_FORM,`, and directly after `_refuse`'s definition:
    ```python
        short_id = str(run_id)[:8]
        if confirm_run.strip().lower() != short_id:
            # C-10: the run's short id, typed, is the confirmation; checked here, not only
            # in the browser's dialog.
            return _refuse(f"Type the run's short id ({short_id}) to confirm Finalize run.")
    ```
    and add to its docstring: "The form must also carry ``confirm_run`` equal to the run id's
    first 8 characters (case and surrounding spaces ignored)."

- [ ] **Step 4: Templates.**
  - `templates/admin/simulation.html`, the announcing Stop form, before
    `<form action="/admin/simulation/stop" method="post">` (the one without `hold_open`) after:
    ```jinja
            <form action="/admin/simulation/stop" method="post"
                  data-confirm="{% if stop_counts %}Posts {{ stop_counts.posts }} of {{ stop_counts.owed }} owed headlines to Slack ({{ stop_counts.open }} from interviews still open; at most {{ stop_counts.cap }} per stop); cannot be undone{% else %}Posts every owed headline to Slack, including interviews still open; cannot be undone{% endif %}">
    ```
    (the `hold_open` form gets nothing, D11);
  - the reset form, before `<form action="/admin/simulation/announce-template" method="post" class="mt-2">` after:
    ```jinja
        <form action="/admin/simulation/announce-template" method="post" class="mt-2"
              data-confirm="Reset the run-start announcement to the file default? The saved custom template is deleted; this cannot be undone.">
    ```
  - `templates/admin/activity_detail.html`, replace the Finalize `<form>` … `</form>` with:
    ```jinja
            {% set short_id = (run.id | string)[:8] %}
            <form action="/admin/simulation/finalize-run" method="post" class="flex flex-wrap items-center gap-2"
                  data-confirm="Finalize run {{ short_id }}: posts its held headlines to Slack, and the run can never be resumed. This cannot be undone.">
                <input type="hidden" name="run_id" value="{{ run.id }}">
                <label for="confirm-run" class="text-sm text-gray-700">Type <code>{{ short_id }}</code> to confirm</label>
                <input type="text" id="confirm-run" name="confirm_run" required autocomplete="off" size="10"
                       class="border border-gray-300 rounded px-2 py-1 text-sm font-mono">
                <button type="submit" class="inline-flex items-center px-4 py-2 bg-amber-600 text-white text-sm font-medium rounded-lg hover:bg-amber-700">
                    Finalize run
                </button>
            </form>
    ```
    (1C-12 then maps `bg-amber-600`/`hover:bg-amber-700` like every other occurrence);
  - `templates/admin/_assessment_detail_body.html`, before
    `<form method="post" action="/reviews/feedback/{{ review.id }}/delete" class="mt-1">` after
    `<form method="post" action="/reviews/feedback/{{ review.id }}/delete" class="mt-1" data-confirm="Delete this review? This cannot be undone.">`.

- [ ] **Step 5: Existing Finalize tests post the short id.** In `tests/integration/test_finalize_run_route.py`,
  in `test_finalize_enqueues_a_stop_with_the_run_id` and `test_finalize_is_refused_while_an_engine_is_alive`
  before: `data={"run_id": str(run.id)},` after: `data={"run_id": str(run.id), "confirm_run": str(run.id)[:8]},`

- [ ] **Step 6: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_simulation_confirms.py tests/integration/test_finalize_run_route.py tests/integration/test_admin_simulation_page.py tests/integration/test_assessment_detail_page.py tests/unit/test_stop_announce_cap.py -v`
  Expected: all pass.

- [ ] **Step 7: Commit.**
```bash
git add src/routers/admin/simulation.py templates/admin/simulation.html templates/admin/activity_detail.html templates/admin/_assessment_detail_body.html tests/integration/test_simulation_confirms.py tests/integration/test_finalize_run_route.py tests/unit/test_stop_announce_cap.py
git commit -m "feat(webui): confirm Finalize (with the run's short id), the announcing Stop, Reset and review delete (C-10, FN-01, FN-06, B-10)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-9: Profile saves refuse before writing; tenure keeps provenance; no 500 during generation (D-01, D-02, D-08)

**Files:**
- Modify: `src/services/profile_edit.py:8-20` (imports), `:80-146` (`apply_profile_edits` up to its first commit); runs after Part 1B's §6.6 edits to the email block
- Modify: `templates/profile/edit.html:14-17`, `templates/manager/pi_detail.html:104-111`,
  `templates/onboarding/profile_review.html:21-24` (error-code chains), `templates/agent/public_profile.html:31-35`; after Part 1A
- Test: `tests/integration/test_profile_edit_integrity.py`

**Interfaces:**
- Consumes: `get_tenure_start`, `set_tenure_start`, `TENURE_KEY_PREFIX` (`src/services/jhu_rules.py`); `Job` (`src/models/job.py`); `engine`, `db_session`, `client` fixtures.
- Produces (in `src/services/profile_edit.py`): `PROFILE_GENERATING = "profile_generating"`,
  `PROFILE_INSERT_LOCK_TIMEOUT = "5s"`, `_LOCK_NOT_AVAILABLE = "55P03"`,
  `async def _profile_generation_in_flight(db, user_id) -> bool`,
  `async def _apply_email(db, target_user, raw, email_required) -> str | None`,
  `async def _write_tenure_if_changed(db, user, year: int, export_agent) -> None`,
  `async def _load_or_create_profile(db, user_id) -> ResearcherProfile | None`.
  `apply_profile_edits` keeps its signature and gains the return code `"profile_generating"`.
  All four callers (`/profile/save`, `/manager/pis/{id}/profile`, `/onboarding/save-profile`,
  `/agent/{id}/public-profile/save`) redirect with `?error=<code>` as today; the templates map it.

- [ ] **Step 1: Write the failing tests.** Create `tests/integration/test_profile_edit_integrity.py`:

```python
"""apply_profile_edits: refusals write nothing (D-02), a re-posted tenure year keeps its
provenance (D-01), and a save that races a profile generation is refused with a message
instead of a 500 (D-08)."""
import json

import pytest
from sqlalchemy import select, text

from src.models import AppSetting, Job, ResearcherProfile, User
from src.services import profile_edit, profile_export
from src.services.jhu_rules import TENURE_KEY_PREFIX, get_tenure_start, set_tenure_start
from src.services.profile_edit import PROFILE_GENERATING, apply_profile_edits
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _exports_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)


async def _edit(db_session, user, **kwargs):
    form = kwargs.pop("form", {"research_summary": "Edited."})
    return await apply_profile_edits(
        db_session, target_user=user, changed_by_user_id=user.id, form=form,
        expected_version=None, **kwargs,
    )


async def _tenure_source(db_session, user_id):
    raw = await db_session.scalar(
        select(AppSetting.value).where(AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}")
    )
    return json.loads(raw)["source"] if raw else None


async def test_a_reposted_tenure_year_keeps_its_machine_source(db_session):
    user = await factories.make_user(db_session)
    await set_tenure_start(user.id, 2018, "orcid_employment", db=db_session)
    await db_session.flush()
    assert await _edit(db_session, user, jhu_tenure_start="2018") is None
    assert await _tenure_source(db_session, user.id) == "orcid_employment"


async def test_a_changed_tenure_year_is_recorded_as_manual(db_session):
    user = await factories.make_user(db_session)
    await set_tenure_start(user.id, 2018, "orcid_employment", db=db_session)
    await db_session.flush()
    assert await _edit(db_session, user, jhu_tenure_start="2016") is None
    assert await get_tenure_start(db_session, user.id) == 2016
    assert await _tenure_source(db_session, user.id) == "manual"


async def test_an_invalid_email_writes_no_tenure(db_session):
    user = await factories.make_user(db_session)
    error = await _edit(db_session, user, form={"email": "not-an-email"}, jhu_tenure_start="2016")
    assert error == "invalid_email"
    assert await get_tenure_start(db_session, user.id) is None


async def test_a_taken_email_writes_no_tenure(db_session):
    await factories.make_user(db_session, email="taken@example.org")
    user = await factories.make_user(db_session)
    error = await _edit(db_session, user, form={"email": "taken@example.org"}, jhu_tenure_start="2016")
    assert error == "email_taken"
    assert await get_tenure_start(db_session, user.id) is None


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_a_pending_generation_job_refuses_the_save_and_writes_nothing(db_session, status):
    user = await factories.make_user(db_session, name="Before")
    db_session.add(Job(type="generate_profile", user_id=user.id, status=status,
                       payload={"user_id": str(user.id)}))
    await db_session.flush()
    error = await _edit(db_session, user, form={"name": "After", "research_summary": "x"},
                        jhu_tenure_start="2016")
    assert error == PROFILE_GENERATING
    assert (await db_session.scalar(select(User.name).where(User.id == user.id))) == "Before"
    assert await get_tenure_start(db_session, user.id) is None
    assert await db_session.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
    ) is None


async def test_a_finished_generation_job_does_not_block(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="generate_profile", user_id=user.id, status="completed",
                       payload={"user_id": str(user.id)}))
    await db_session.flush()
    assert await _edit(db_session, user) is None


async def test_the_profile_insert_gives_up_on_a_held_lock(db_session, engine, monkeypatch):
    """The worker's uncommitted profile row stands in as a table lock held by another
    connection; the insert must time out into the refusal, not wait or 500."""
    monkeypatch.setattr(profile_edit, "PROFILE_INSERT_LOCK_TIMEOUT", "200ms")
    user = await factories.make_user(db_session)
    await db_session.commit()  # savepoint release: the refusal's rollback keeps the user
    # Read before the refusal's rollback expires `user` (a lazy load under asyncio
    # raises MissingGreenlet; plan audit Q1-05).
    user_id = user.id
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(text("LOCK TABLE researcher_profiles IN SHARE ROW EXCLUSIVE MODE"))
        try:
            error = await _edit(db_session, user)
        finally:
            await blocker.rollback()
    assert error == PROFILE_GENERATING
    assert await db_session.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user_id)
    ) is None


async def test_the_profile_page_explains_the_refusal(client, db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="generate_profile", user_id=user.id, status="pending",
                       payload={"user_id": str(user.id)}))
    await db_session.flush()
    r = await client.post(
        "/profile/save",
        data={"name": user.name, "email": user.email, "research_summary": "x"},
        headers=auth_headers(user.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/profile/edit?error=profile_generating"
    page = await client.get(r.headers["location"], headers=auth_headers(user.id))
    assert "Profile is being generated — try again shortly" in page.text
```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_profile_edit_integrity.py -v`
  Expected: collection error, `ImportError: cannot import name 'PROFILE_GENERATING'`.

- [ ] **Step 3: Implement.** In `src/services/profile_edit.py`:
  - imports: `from sqlalchemy import func, select, text, update`;
    `from sqlalchemy.exc import DBAPIError`;
    `from src.models import AgentRegistry, Job, ResearcherProfile, User`;
    `from src.services.jhu_rules import get_tenure_start, set_tenure_start`.
  - add after `parse_expected_version`:
    ```python
    #: The refusal code for a save that would race a profile generation (D-08); every profile
    #: form maps it to "Profile is being generated — try again shortly".
    PROFILE_GENERATING = "profile_generating"
    #: ``SET LOCAL lock_timeout`` for the profile-row insert (D-08). A generation job holds the
    #: uncommitted row for this user for its whole run (the worker keeps one transaction across
    #: the pipeline), so without a bound the insert waits on the unique key until the run ends.
    PROFILE_INSERT_LOCK_TIMEOUT = "5s"
    #: Postgres SQLSTATE ``lock_not_available``: what an expired ``lock_timeout`` raises.
    _LOCK_NOT_AVAILABLE = "55P03"


    async def _profile_generation_in_flight(db: AsyncSession, user_id: uuid.UUID) -> bool:
        """True while a ``generate_profile`` job for ``user_id`` is pending or processing."""
        return await db.scalar(
            select(Job.id).where(
                Job.user_id == user_id,
                Job.type == "generate_profile",
                Job.status.in_(("pending", "processing")),
            ).limit(1)
        ) is not None


    async def _apply_email(
        db: AsyncSession, target_user: User, raw: str | None, email_required: bool,
    ) -> str | None:
        """Validate, then assign, the posted address; an error code or None. A refusal writes
        nothing: the checks run first, and ``assign_user_email`` writes nothing when another
        user holds the address."""
        email_clean = (raw or "").strip().lower()
        if email_required and not email_clean:
            return "email_required"
        changed = email_clean != (target_user.email or "")
        if email_clean and (changed or email_required) and not is_valid_email(email_clean):
            return "invalid_email"
        if changed and not await assign_user_email(db, target_user, email_clean or None):
            return "email_taken"
        return None


    async def _write_tenure_if_changed(
        db: AsyncSession, user: User, year: int, export_agent: AgentRegistry | None,
    ) -> None:
        """Upsert a ``manual`` tenure entry only when ``year`` differs from the recorded one
        (D-01). The manager form re-posts the displayed year on every save; relabelling a
        machine-derived entry ``manual`` made the re-derivation script skip it."""
        agent_slug = export_agent.agent_id if export_agent is not None else await db.scalar(
            select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user.id)
        )
        if await get_tenure_start(db, user.id, agent_id=agent_slug) != year:
            await set_tenure_start(user.id, year, "manual", db=db)


    async def _load_or_create_profile(
        db: AsyncSession, user_id: uuid.UUID,
    ) -> ResearcherProfile | None:
        """The user's profile row, inserted when missing; None when the insert's lock wait
        timed out (the session is then rolled back)."""
        profile = (await db.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        )).scalar_one_or_none()
        if profile is not None:
            return profile
        # Before db.add(): an execute autoflushes, and the INSERT must run under the bound.
        await db.execute(text(f"SET LOCAL lock_timeout = '{PROFILE_INSERT_LOCK_TIMEOUT}'"))
        profile = ResearcherProfile(user_id=user_id)
        db.add(profile)
        try:
            # Flush the row into existence before the SQL-side bump in
            # write_profile_text_fields: on a pending object the expression would render
            # inside the INSERT's VALUES, which cannot reference its own target table.
            await db.flush()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            await db.rollback()
            return None
        return profile
    ```
    If Part 1B added lines to the email block of `apply_profile_edits` (for example clearing
    `email_verified_at`), move them unchanged into `_apply_email` at the same position.
  - In `apply_profile_edits`, replace everything from the comment
    `# Optional JHU tenure-start correction` through `await db.flush()` of the profile insert
    (today lines 100-135) with:
    ```python
        if await _profile_generation_in_flight(db, target_user.id):
            return PROFILE_GENERATING
        # Optional JHU tenure-start correction (manager form only; the PI's own
        # /profile/save never sends the field). Blank = leave unchanged.
        tenure_field = (jhu_tenure_start or "").strip()
        if tenure_field and not re.fullmatch(r"\d{4}", tenure_field):
            return "invalid_tenure_year"
        if form.get("email") is not None:
            email_error = await _apply_email(db, target_user, form["email"], email_required)
            if email_error:
                return email_error
        # Only after every refusal: get_db commits on a clean return, so a tenure upsert made
        # before a refused email used to be committed with the refusal (D-02).
        if tenure_field:
            await _write_tenure_if_changed(db, target_user, int(tenure_field), export_agent)

        if form.get("name"):
            target_user.name = form["name"]
        for field in ("institution", "department"):
            if form.get(field) is not None:
                setattr(target_user, field, form[field] or None)

        profile = await _load_or_create_profile(db, target_user.id)
        if profile is None:
            return PROFILE_GENERATING
    ```
  - In its docstring, after the sentence "Returns an error code, or None after committing and
    exporting." add:
    ```text
    Every refusal comes before any write, because ``get_db`` commits on a clean return: a
    ``generate_profile`` job pending or processing for ``target_user`` (``PROFILE_GENERATING``,
    D-08), a malformed tenure year, then the email checks (D-02). The tenure year is written
    only when it differs from the recorded one (D-01). The profile-row insert runs under
    ``PROFILE_INSERT_LOCK_TIMEOUT``; a lock timeout rolls back and also returns
    ``PROFILE_GENERATING``.
    ```

- [ ] **Step 4: Map the code in the four templates.** Message text: `Profile is being generated — try again shortly.`
  - `templates/profile/edit.html` and `templates/onboarding/profile_review.html`: before the
    `{% else %}Something went wrong saving your changes.` line add
    `{% elif error == 'profile_generating' %}Profile is being generated — try again shortly.`
  - `templates/manager/pi_detail.html`: before `{% else %}Something went wrong saving changes.{% endif %}` add
    `{% elif request.query_params.get('error') == 'profile_generating' %}Profile is being generated — try again shortly.`
  - `templates/agent/public_profile.html`: after the `profile_changed` block add
    ```jinja
        {% if request.query_params.get('error') == 'profile_generating' %}
        <div class="bg-red-50 border border-red-200 rounded-lg p-3 mb-6 text-sm text-red-800">
            Profile is being generated — try again shortly.
        </div>
        {% endif %}
    ```

- [ ] **Step 5: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_profile_edit_integrity.py tests/unit/test_profile_edit_tenure.py tests/integration/test_profile_single_writer.py tests/integration/test_profile_version_guard.py tests/integration/test_onboarding_flow.py tests/integration/test_manager_pi_writes.py tests/integration/test_user_email.py tests/integration/test_agent_page.py tests/integration/test_provisional_tenure.py -v`
  Expected: all pass. A test that saves while its own fixture left a `generate_profile` job
  `pending` or `processing` now gets `profile_generating`; fix such a test by setting that
  job's `status = "completed"` before the save (the state a real save happens in), never by
  weakening the check.

- [ ] **Step 6: Commit.**
```bash
git add src/services/profile_edit.py templates/profile/edit.html templates/manager/pi_detail.html templates/onboarding/profile_review.html templates/agent/public_profile.html tests/integration/test_profile_edit_integrity.py
git commit -m "fix(webui): profile saves refuse before writing, keep tenure provenance, and refuse during generation (D-01, D-02, D-08)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-10: Tag widgets post one field per tag; the comma split is gone (D-16)

**Files:**
- Modify: `src/services/profile_edit.py:22-32` (`_LIST_FIELDS` comment, `_parse_list`), `:80-86` (`form` type), the `values = {...}` comprehension; after 1C-9
- Modify: `src/routers/profile.py:40-41` (delete the dead `_parse_list`), `:133-170` (`profile_save`)
- Modify: `src/routers/onboarding.py:114-150` (`save_profile`)
- Modify: `src/routers/manager.py:310-347` (`manager_edit_pi_profile`)
- Modify: `src/routers/agent_page.py:623-665` (`save_public_profile`)
- Create: `templates/_tag_field.html`, `static/js/tag_widget.js`
- Modify: `static/css/input.css` (append the tag styles; Part 1A's file)
- Modify: `templates/profile/edit.html` (the five `data-tag-field` blocks, lines 63-122; the `<style>` block 149-193; the tag `<script>` 195-288), `templates/onboarding/profile_review.html` (blocks 99-163; `<style>` 193-237; `<script>` 239-307), `templates/agent/public_profile.html` (blocks 52-114; `<style>` 204-248; `{% if editing %}<script>…</script>{% endif %}` 250-319), `templates/manager/pi_detail.html` (the five comma text inputs, lines 185-209); after Part 1A (which added nonces to the deleted scripts) and 1C-9
- Modify: `tests/unit/test_no_dead_src_symbols.py:114-117` (delete the `src.routers.profile:_parse_list` entry)
- Modify: `tests/unit/test_profile_edit_tenure.py:44-49`, `tests/integration/test_profile_single_writer.py:14-15` and `:62-68`, `:85`, `tests/integration/test_profile_version_guard.py:13-16`, `tests/integration/test_onboarding_flow.py:379-383`, `:701-705`, `:959`, `tests/integration/test_agent_page.py:640-641`, `:689`
- Test: `tests/integration/test_profile_tag_fields.py`

**Interfaces:**
- Consumes: `apply_profile_edits` (1C-9).
- Produces:
  - `src/services/profile_edit.py`: `TAG_FIELDS_MARKER = "tag_fields"`,
    `def list_fields_from_form(form: FormData) -> dict[str, list[str] | None]`,
    `def _clean_tags(values: Sequence[str]) -> list[str]` (raises `TypeError` on a `str`);
    `apply_profile_edits(..., form: Mapping[str, str | Sequence[str] | None], ...)`: list
    fields take a list of tags or None (unchanged).
  - `templates/_tag_field.html`: macros `tag_field(name, label, values, hint, wrapper_class="mb-5")`
    and `profile_tag_fields(profile, wrapper_class="mb-5")`.
  - `static/js/tag_widget.js` (no exports; wires every `[data-tag-field]`).
  - Posted shape: per widget one `tag_fields=<field>` marker and one `<field>=<tag>` per tag.

- [ ] **Step 1: Write the failing tests.** Create `tests/integration/test_profile_tag_fields.py`:

```python
"""D-16: each tag is posted as its own field, so a tag with a comma survives a save."""
from pathlib import Path

import pytest
from sqlalchemy import select
from starlette.datastructures import FormData

from src.models import USER_ROLE_MANAGER, ResearcherProfile
from src.services import profile_export
from src.services.profile_edit import apply_profile_edits, list_fields_from_form
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

TAG_JS = Path(__file__).resolve().parents[2] / "static/js/tag_widget.js"
_ALL = ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"]


@pytest.fixture(autouse=True)
def _exports_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)


async def _profile(db_session, user_id):
    return (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )).scalar_one()


def test_list_fields_read_repeated_values_and_the_marker():
    form = FormData([
        ("tag_fields", "techniques"), ("techniques", "1,2-dichloroethane"),
        ("techniques", "cryo-EM"), ("tag_fields", "keywords"),
    ])
    assert list_fields_from_form(form) == {
        "techniques": ["1,2-dichloroethane", "cryo-EM"],
        "experimental_models": None,
        "disease_areas": None,
        "key_targets": None,
        "keywords": [],
    }


async def test_a_comma_string_is_refused_by_the_writer(db_session):
    user = await factories.make_user(db_session)
    with pytest.raises(TypeError):
        await apply_profile_edits(
            db_session, target_user=user, changed_by_user_id=user.id,
            form={"techniques": "a, b"}, expected_version=None,
        )


async def test_a_comma_inside_a_tag_survives_the_save(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, techniques=["old"])
    r = await client.post(
        "/profile/save",
        data={
            "name": user.name, "email": user.email, "research_summary": "s",
            "tag_fields": _ALL,
            "techniques": ["1,2-dichloroethane", " cryo-EM ", ""],
            "keywords": ["kinase"],
        },
        headers=auth_headers(user.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/profile?saved=1"
    profile = await _profile(db_session, user.id)
    await db_session.refresh(profile)
    assert profile.techniques == ["1,2-dichloroethane", "cryo-EM"]
    assert profile.keywords == ["kinase"]
    assert profile.disease_areas == []


async def test_a_form_without_the_widget_leaves_the_tags_alone(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, techniques=["kept, intact"])
    await client.post(
        "/profile/save",
        data={"name": user.name, "email": user.email, "research_summary": "s",
              "techniques": "a, b"},
        headers=auth_headers(user.id),
    )
    profile = await _profile(db_session, user.id)
    await db_session.refresh(profile)
    assert profile.techniques == ["kept, intact"]


async def test_the_manager_form_saves_tags_whole(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "email": pi.email, "research_summary": "s",
              "tag_fields": ["key_targets"], "key_targets": ["PD-1, PD-L1 axis"]},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?saved=1"
    profile = await _profile(db_session, pi.id)
    await db_session.refresh(profile)
    assert profile.key_targets == ["PD-1, PD-L1 axis"]


async def test_the_edit_page_renders_one_hidden_input_per_tag(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, techniques=["1,2-dichloroethane", "cryo-EM"])
    r = await client.get("/profile/edit", headers=auth_headers(user.id))
    assert '<input type="hidden" name="techniques" value="1,2-dichloroethane">' in r.text
    assert '<input type="hidden" name="techniques" value="cryo-EM">' in r.text
    assert '<input type="hidden" name="tag_fields" value="techniques">' in r.text
    assert 'aria-label="Remove 1,2-dichloroethane"' in r.text
    assert '<label for="tag-input-techniques"' in r.text
    assert 'src="/static/js/tag_widget.js"' in r.text
    assert "1,2-dichloroethane, cryo-EM" not in r.text


def test_the_widget_script_adds_a_hidden_input_and_never_joins():
    js = TAG_JS.read_text()
    assert 'hidden.name = name;' in js
    assert 'btn.setAttribute("aria-label", "Remove " + text);' in js
    assert ".join(" not in js
```

- [ ] **Step 2: Run them.** `.venv-test/bin/python -m pytest tests/integration/test_profile_tag_fields.py -v`
  Expected: collection error, `ImportError: cannot import name 'list_fields_from_form'`.

- [ ] **Step 3: The writer.** In `src/services/profile_edit.py`:
  - imports: `from collections.abc import Mapping, Sequence`; `from starlette.datastructures import FormData`;
  - replace
    ```python
    #: List-valued profile fields (comma-separated in the forms).
    _LIST_FIELDS = frozenset(PROFILE_FIELDS) - {"research_summary"}


    def _parse_list(val: str) -> list[str]:
        return [s.strip() for s in val.split(",") if s.strip()]
    ```
    with
    ```python
    #: List-valued profile fields. Each tag is posted as its own form field (D-16): the
    #: comma split this replaced broke every tag containing a comma ("1,2-dichloroethane").
    _LIST_FIELDS = frozenset(PROFILE_FIELDS) - {"research_summary"}
    #: Each tag widget (templates/_tag_field.html) posts one marker naming its field, so a
    #: widget whose tags were all removed (posting no values) still clears the field, while
    #: a form without the widget, or a page rendered before it existed, changes nothing.
    TAG_FIELDS_MARKER = "tag_fields"


    def list_fields_from_form(form: FormData) -> dict[str, list[str] | None]:
        """The list fields of a posted profile form: the repeated values of each field whose
        widget marker was posted, else None (leave unchanged)."""
        present = set(form.getlist(TAG_FIELDS_MARKER))
        return {
            field: ([v for v in form.getlist(field) if isinstance(v, str)]
                    if field in present else None)
            for field in PROFILE_FIELDS if field in _LIST_FIELDS
        }


    def _clean_tags(values: Sequence[str]) -> list[str]:
        """Stripped, non-empty tags. A ``str`` is refused: it would iterate as characters,
        and a comma string is exactly the format this replaced."""
        if isinstance(values, str):
            raise TypeError("list fields take a list of tags, not a comma-separated string")
        return [v.strip() for v in values if v.strip()]
    ```
  - `apply_profile_edits` signature: before `form: Mapping[str, str | None], expected_version: int | None,`
    after `form: Mapping[str, str | Sequence[str] | None], expected_version: int | None,`; in its
    docstring after "A key carried as "" clears a user field (``or None``) exactly as the full forms
    always did." add "List fields take a list of tags (``list_fields_from_form``)."
  - the comprehension, before:
    ```python
        values = {
            f: (_parse_list(form[f]) if f in _LIST_FIELDS else form[f])
            for f in PROFILE_FIELDS if form.get(f) is not None
        }
    ```
    after:
    ```python
        values = {
            f: (_clean_tags(form[f]) if f in _LIST_FIELDS else form[f])
            for f in PROFILE_FIELDS if form.get(f) is not None
        }
    ```
  - module docstring of `src/services/profile_edit.py`: no change needed.

- [ ] **Step 4: The four handlers.** In each, delete the five list-field `Form("")`
  parameters (`techniques`, `experimental_models`, `disease_areas`, `key_targets`, `keywords`),
  import `list_fields_from_form` next to `apply_profile_edits`, and build the form dict from the
  posted list fields:
  - `src/routers/profile.py::profile_save` (has `request`):
    ```python
        form={
            "name": name, "email": email, "institution": institution,
            "department": department, "research_summary": research_summary,
            **list_fields_from_form(await request.form()),
        },
    ```
    and delete the module-level `_parse_list` (lines 40-41, dead; the dead-symbol allowlist
    entry goes in Step 7).
  - `src/routers/onboarding.py::save_profile` (has `request`):
    ```python
        form={
            "email": email, "research_summary": research_summary,
            **list_fields_from_form(await request.form()),
        },
    ```
  - `src/routers/manager.py::manager_edit_pi_profile`: add `request: Request,` after
    `user_id: uuid.UUID,` (`Request` is already imported there), then
    ```python
        form={
            "name": name, "email": email, "institution": institution,
            "department": department, "research_summary": research_summary,
            **list_fields_from_form(await request.form()),
        },
    ```
  - `src/routers/agent_page.py::save_public_profile` (has `request`):
    ```python
        form={
            "research_summary": research_summary,
            **list_fields_from_form(await request.form()),
        },
    ```
  `request.form()` is cached by Starlette after FastAPI's own `Form(...)` parsing, so this is
  no second read of the body.

- [ ] **Step 5: The macro and the script.** Create `templates/_tag_field.html`:

```jinja
{# Tag widgets (D-16). Every tag is its own hidden <input name="{{ name }}">, and one
   `tag_fields` marker says the widget was on the page, so a field whose tags were all
   removed still posts the marker and is cleared (src/services/profile_edit.py,
   list_fields_from_form). Behaviour: static/js/tag_widget.js; styles: static/css/input.css. #}
{% macro tag_field(name, label, values, hint, wrapper_class="mb-5") %}
<div class="{{ wrapper_class }}" data-tag-field="{{ name }}">
    <input type="hidden" name="tag_fields" value="{{ name }}">
    <label for="tag-input-{{ name }}" class="block text-sm font-medium text-gray-700 mb-1">{{ label }}</label>
    <div class="tag-container border border-gray-300 rounded-lg px-2 py-1.5 flex flex-wrap gap-1.5 min-h-[38px] focus-within:ring-2 focus-within:ring-indigo-500 focus-within:border-transparent cursor-text">
        {% for t in values %}
        <span class="tag-pill">{{ t }}<input type="hidden" name="{{ name }}" value="{{ t }}"><button type="button" class="tag-remove" aria-label="Remove {{ t }}">&times;</button></span>
        {% endfor %}
        <input type="text" id="tag-input-{{ name }}" class="tag-input" placeholder="Type and press Enter to add...">
    </div>
    {% if hint %}<p class="text-xs text-gray-600 mt-1">{{ hint }}</p>{% endif %}
</div>
{% endmacro %}

{# The five list fields of a ResearcherProfile, as every profile form shows them. #}
{% macro profile_tag_fields(profile, wrapper_class="mb-5") %}
{{ tag_field("techniques", "Techniques & Methods", (profile.techniques if profile else None) or [], "e.g. RNA-seq, CRISPR-Cas9, mass spectrometry", wrapper_class) }}
{{ tag_field("experimental_models", "Model Systems", (profile.experimental_models if profile else None) or [], "Cell lines, model organisms, databases", wrapper_class) }}
{{ tag_field("disease_areas", "Disease Areas", (profile.disease_areas if profile else None) or [], "e.g. neurodegeneration, osteoarthritis", wrapper_class) }}
{{ tag_field("key_targets", "Key Molecular Targets", (profile.key_targets if profile else None) or [], "e.g. ATF6, UPR, FoxO", wrapper_class) }}
{{ tag_field("keywords", "Keywords", (profile.keywords if profile else None) or [], "Additional MeSH-style keywords", wrapper_class) }}
{% endmacro %}
```

  Create `static/js/tag_widget.js`:

```javascript
// Tag widgets (templates/_tag_field.html, D-16). Every tag is its own hidden
// <input name="<field>">, so a tag containing a comma is posted whole and the
// handler reads the field with form.getlist(). Typing a comma or pasting a
// comma-separated list still splits it into several tags, as before.
(function () {
  "use strict";

  function tagText(pill) {
    var hidden = pill.querySelector('input[type="hidden"]');
    return hidden ? hidden.value : "";
  }

  function init(wrapper) {
    var name = wrapper.getAttribute("data-tag-field");
    var container = wrapper.querySelector(".tag-container");
    var textInput = wrapper.querySelector(".tag-input");
    if (!name || !container || !textInput) return;

    function addTag(raw) {
      var text = raw.trim();
      if (!text) return;
      var pills = container.querySelectorAll(".tag-pill");
      for (var i = 0; i < pills.length; i++) {
        if (tagText(pills[i]).toLowerCase() === text.toLowerCase()) return;
      }
      var pill = document.createElement("span");
      pill.className = "tag-pill";
      pill.appendChild(document.createTextNode(text));
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = name;
      hidden.value = text;
      pill.appendChild(hidden);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tag-remove";
      btn.setAttribute("aria-label", "Remove " + text);
      btn.textContent = "×";
      pill.appendChild(btn);
      container.insertBefore(pill, textInput);
    }

    container.addEventListener("click", function (event) {
      var remove = event.target.closest(".tag-remove");
      if (remove && container.contains(remove)) {
        remove.closest(".tag-pill").remove();
      }
      textInput.focus();
    });

    textInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addTag(textInput.value.replace(/,/g, ""));
        textInput.value = "";
      } else if (event.key === "Backspace" && textInput.value === "") {
        var pills = container.querySelectorAll(".tag-pill");
        if (pills.length > 0) pills[pills.length - 1].remove();
      }
    });

    textInput.addEventListener("paste", function (event) {
      event.preventDefault();
      var pasted = (event.clipboardData || window.clipboardData).getData("text");
      pasted.split(",").forEach(addTag);
    });

    textInput.addEventListener("blur", function () {
      if (textInput.value.trim()) {
        addTag(textInput.value);
        textInput.value = "";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-tag-field]").forEach(init);
  });
})();
```

  Append to `static/css/input.css` (the rules moved verbatim from the three inline `<style>`
  blocks, with the placeholder darkened from `#9ca3af` to `#4b5563`):

```css
/* Tag widgets (templates/_tag_field.html), moved from three inline <style> blocks. */
.tag-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background-color: #eef2ff;
    color: #4338ca;
    font-size: 0.8125rem;
    padding: 2px 8px;
    border-radius: 9999px;
    white-space: nowrap;
    line-height: 1.5;
}
.tag-remove {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: transparent;
    border: none;
    color: #4f46e5;
    font-size: 14px;
    line-height: 1;
    cursor: pointer;
    padding: 0;
}
.tag-remove:hover {
    background-color: #c7d2fe;
    color: #3730a3;
}
.tag-input {
    flex: 1 1 120px;
    min-width: 120px;
    border: none;
    outline: none;
    font-size: 0.875rem;
    padding: 2px 4px;
    background: transparent;
}
.tag-input::placeholder {
    color: #4b5563;
}
```
  (`.tag-remove`'s `#6366f1` becomes `#4f46e5`, indigo-600, which passes 3:1 against the pill.)

- [ ] **Step 6: Use the macro in the four templates.**
  - `templates/profile/edit.html`: add `{% from "_tag_field.html" import profile_tag_fields %}`
    as the line after `{% extends "base.html" %}`; replace the five `<div class="mb-5" data-tag-field="…">`
    blocks with `{{ profile_tag_fields(profile) }}`; delete the `<style>` … `</style>` block that
    holds only `.tag-pill`, `.tag-remove`, `.tag-remove:hover`, `.tag-input` and
    `.tag-input::placeholder`; replace the `<script …>document.addEventListener('DOMContentLoaded', … '[data-tag-field]' …</script>`
    block with `<script src="/static/js/tag_widget.js" defer></script>`.
  - `templates/onboarding/profile_review.html`: the same import; replace the five
    `<div class="mb-6" data-tag-field="…">` blocks (with their `<!-- Techniques -->`-style
    comments) with `{{ profile_tag_fields(profile, "mb-6") }}`; delete its tag `<style>` block;
    replace its tag `<script>` block (the one querying `[data-tag-field]`, not the one at
    lines 47-49) with `<script src="/static/js/tag_widget.js" defer></script>`.
  - `templates/agent/public_profile.html`: the same import; replace its five blocks with
    `{{ profile_tag_fields(profile) }}`; delete its tag `<style>` block; replace
    `{% if editing %}<script>…</script>{% endif %}` with
    `{% if editing %}<script src="/static/js/tag_widget.js" defer></script>{% endif %}`.
  - `templates/manager/pi_detail.html`: the same import; replace the five
    `<div>` blocks holding `name="techniques"`, `name="experimental_models"`,
    `name="disease_areas"`, `name="key_targets"`, `name="keywords"` text inputs (labels ending
    "(comma-separated)") with `{{ profile_tag_fields(profile, "") }}`, and add
    `<script src="/static/js/tag_widget.js" defer></script>` directly after that form's closing `</form>`.

- [ ] **Step 7: Move the existing tests to list-valued tags.**
  - `tests/unit/test_no_dead_src_symbols.py`: delete the `"src.routers.profile:_parse_list": (...)` entry.
  - `tests/unit/test_profile_edit_tenure.py::_edit_kwargs`: before
    `research_summary="Summary.", techniques="t1, t2",` /
    `experimental_models="m", disease_areas="d", key_targets="k",` / `keywords="w",`
    after `research_summary="Summary.", techniques=["t1", "t2"],` /
    `experimental_models=["m"], disease_areas=["d"], key_targets=["k"],` / `keywords=["w"],`
  - `tests/integration/test_profile_single_writer.py`: replace `_FORM` with
    ```python
    _FORM = {"research_summary": "New summary", "techniques": ["a", "b"], "experimental_models": ["m"],
             "disease_areas": ["d"], "key_targets": ["k"], "keywords": ["x", "y"]}
    #: What a rendered form posts: the tag lists as repeated fields plus one marker per widget.
    _POSTED = {**_FORM, "tag_fields": ["techniques", "experimental_models", "disease_areas",
                                       "key_targets", "keywords"]}
    ```
    and in `_routes` and in the `which == 3` branch replace `**_FORM` / `dict(_FORM)` with
    `**_POSTED` / `dict(_POSTED)`.
  - `tests/integration/test_profile_version_guard.py`: `_FIELDS` becomes
    ```python
    _FIELDS = {
        "research_summary": "Edited summary.", "techniques": ["a", "b"], "experimental_models": ["m"],
        "disease_areas": ["d"], "key_targets": ["k"], "keywords": ["w"],
        "tag_fields": ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"],
    }
    ```
  - `tests/integration/test_onboarding_flow.py`: in the save at lines 379-383 use
    `"techniques": ["cryo-EM", "mass spec"], "experimental_models": ["mouse"], "disease_areas": ["cancer"], "key_targets": ["KRAS"], "keywords": ["kinase", "structure"], "tag_fields": ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"],`;
    at lines 701-705 use
    `"techniques": ["t1", "t2"], "experimental_models": ["m1"], "disease_areas": ["d1", "d2"], "key_targets": ["k1"], "keywords": ["kw1", "kw2"], "tag_fields": ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"],`;
    at line 959 use `"techniques": ["route-technique"], "tag_fields": ["techniques"],`.
  - `tests/integration/test_agent_page.py`: at lines 640-641 use
    `"techniques": ["cryo-EM", "mass spec"], "keywords": ["proteostasis"], "tag_fields": ["techniques", "keywords"],`;
    the `Ep(...public-profile/save...)` data at line 689 becomes
    `{"research_summary": "s", "techniques": ["a", "b"], "keywords": ["k"], "tag_fields": ["techniques", "keywords"]}`.
  Tests that post a tag field as `""` with no marker (`test_manager_pi_writes.py`,
  `test_provisional_tenure.py`, `test_impersonation_guards.py`, `test_transactional_email.py`,
  `test_user_email.py`, `test_pi_only_writes.py`) need no change: without the marker the
  field is left unchanged, and none of them asserts a list value.

- [ ] **Step 8: Rebuild CSS and run.** `scripts/build_css.sh` then
  `.venv-test/bin/python -m pytest tests/integration/test_profile_tag_fields.py tests/unit/test_no_dead_src_symbols.py tests/unit/test_profile_edit_tenure.py tests/integration/test_profile_single_writer.py tests/integration/test_profile_version_guard.py tests/integration/test_onboarding_flow.py tests/integration/test_agent_page.py tests/integration/test_manager_pi_writes.py tests/unit/test_reachability.py -v`
  Expected: all pass.

- [ ] **Step 9: Commit.**
```bash
git add src/services/profile_edit.py src/routers/profile.py src/routers/onboarding.py src/routers/manager.py src/routers/agent_page.py templates/_tag_field.html static/js/tag_widget.js static/css/input.css static/css/app.css templates/profile/edit.html templates/onboarding/profile_review.html templates/agent/public_profile.html templates/manager/pi_detail.html tests/integration/test_profile_tag_fields.py tests/unit/test_no_dead_src_symbols.py tests/unit/test_profile_edit_tenure.py tests/integration/test_profile_single_writer.py tests/integration/test_profile_version_guard.py tests/integration/test_onboarding_flow.py tests/integration/test_agent_page.py
git commit -m "fix(webui): tag widgets post one field per tag; drop the comma split (D-16)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-11: Chat input stays focusable while busy and gets focus back (B-06)

**Files:**
- Modify: `static/js/assessment_chat.js:168-173` (`setBusy`), `:691-697` (`abandon` in `ask`), `:796` (the `setBusy(false);` after `readStream`)
- Modify: `templates/admin/_assessment_chat_drawer.html:70` (label the question textarea); after Part 1A
- Rewrite: `tests/unit/test_chat_busy_input.py`
- Test: `tests/unit/test_chat_busy_input.py`; harness `journey_chat_focus_after_answer` (1C-16)

**Interfaces:**
- Consumes: `state.open`, `els.input` (existing in `assessment_chat.js`).
- Produces: `function refocusInput()` inside the chat module closure.

- [ ] **Step 1: Replace the source test.** `tests/unit/test_chat_busy_input.py` becomes:

```python
"""B-06: while an answer streams the question box is read-only, not disabled (a disabled
control drops focus to <body>), says so with aria-busy, and gets focus back when the
answer finishes or is abandoned. JS has no runner here; these pin the source, and the
harness journey `journey_chat_focus_after_answer` exercises it in a browser."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / "static/js/assessment_chat.js").read_text()


def _function_body(name: str) -> str:
    start = JS.index(f"function {name}(")
    open_at = JS.index("{", start)
    depth = 0
    for i in range(open_at, len(JS)):
        if JS[i] == "{":
            depth += 1
        elif JS[i] == "}":
            depth -= 1
            if depth == 0:
                return JS[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_input_is_read_only_not_disabled_while_busy():
    body = _function_body("setBusy")
    assert "els.input.readOnly = busy;" in body
    assert 'els.input.setAttribute("aria-busy", "true");' in body
    assert 'els.input.removeAttribute("aria-busy");' in body
    assert "els.input.disabled" not in body


def test_focus_returns_when_the_answer_finishes_or_is_abandoned():
    assert "els.input.focus(" in _function_body("refocusInput")
    ask = _function_body("ask")
    abandon = ask[ask.index("function abandon("):]
    abandon = abandon[: abandon.index("}") + 1]
    assert "refocusInput();" in abandon
    after_stream = ask[ask.index("await readStream(resp, handlers);"):]
    assert after_stream.index("setBusy(false);") < after_stream.index("refocusInput();")
```

- [ ] **Step 2: Run it.** `.venv-test/bin/python -m pytest tests/unit/test_chat_busy_input.py -v`
  Expected: both FAIL (`setBusy` still sets `disabled`; no `refocusInput`).

- [ ] **Step 3: Implement.** In `static/js/assessment_chat.js` replace `setBusy` with:

```javascript
  function setBusy(busy) {
    state.busy = busy;
    els.send.disabled = busy || hasStreaming();
    els.clear.disabled = busy;
    // readOnly, not disabled (B-06): disabling the focused control drops focus to
    // <body>. aria-busy tells assistive technology why typing is refused;
    // refocusInput() hands focus back when the answer settles.
    els.input.readOnly = busy;
    if (busy) {
      els.input.setAttribute("aria-busy", "true");
    } else {
      els.input.removeAttribute("aria-busy");
    }
  }

  function refocusInput() {
    if (state.open) {
      els.input.focus({ preventScroll: true });
    }
  }
```

  In `ask()`, `abandon` becomes:

```javascript
    function abandon(code) {
      wrap.remove();
      els.input.value = question;
      setBusy(false);
      render();
      showError(code);
      refocusInput();
    }
```

  and after `await readStream(resp, handlers);`'s `try`/`catch` and the `cancelAnimationFrame`
  block, before: `    setBusy(false);` after:
  ```javascript
      setBusy(false);
      refocusInput();
  ```

- [ ] **Step 4: Label the question box.** In `templates/admin/_assessment_chat_drawer.html`
  insert directly before `<textarea id="assessment-chat-question"`:
  `<label for="assessment-chat-question" class="sr-only">Your question</label>`

- [ ] **Step 5: Run.** `.venv-test/bin/python -m pytest tests/unit/test_chat_busy_input.py tests/integration/test_assessment_chat_templates.py -v`
  Expected: all pass.

- [ ] **Step 6: Commit.**
```bash
git add static/js/assessment_chat.js templates/admin/_assessment_chat_drawer.html tests/unit/test_chat_busy_input.py
git commit -m "fix(webui): chat question box stays focused across an answer (B-06)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-12: Contrast classes (X-01)

**Evidence for the map.** The audit's axe samples (`crawl.json` of the 2026-10-01 audit)
show, besides gray-400 text (`#9ca3af`) and white on green-600 (`#16a34a`), failing pairs
`#16a34a` text on white/gray-50 (text-green-600, 3.3:1), `#d97706` text (text-amber-600,
3.2:1), white on `#d97706` (bg-amber-600), white on `#ca8a04` (bg-yellow-600, 2.9:1) and
`#6b7280` on `#f3f4f6` (text-gray-500 on bg-gray-100, 4.4:1). The two classes the spec
names alone cannot reach "zero `color-contrast` violations", so the map also moves each of
those to the next shade that passes 4.5:1 on white (Tailwind v3 palette:
green-700 `#15803d` 5.0, amber-700 `#b45309` 5.0, yellow-700 `#a16207` 4.9, red-600 `#dc2626`
4.8, blue-600 `#2563eb` 5.2, indigo-600 `#4f46e5` 6.3, gray-600 `#4b5563` 7.6). Today every
`text-gray-400` (133) sits in `templates/`; none in `static/js/` or `src/`, but
`src/services/bands.py::band_class` builds gray/colour classes from shades passed by
templates, and two templates build `text-{{ color }}-600`.

**Files:**
- Modify: every file under `templates/` and `static/js/` matching the map (by script, Step 3)
- Modify: `templates/admin/_assessments_body.html:467` (`band_class(a.band, 600, 400)` twice),
  `templates/admin/jobs.html:19`, `templates/admin/discussions.html:54`, `templates/manager/discussions.html:54`
- Modify: `tailwind.config.js` (safelist, only if Step 4 finds a class missing); `static/css/app.css` (rebuilt)
- Test: `tests/unit/test_contrast_classes.py`

All after Part 1A and every other 1C template task (1C-3 … 1C-11), so the script also maps
classes those tasks added.

**Interfaces:**
- Consumes: `scripts/build_css.sh`, `tailwind.config.js` (Part 1A).
- Produces: `tests/unit/test_contrast_classes.py::FORBIDDEN` (the retired classes).

- [ ] **Step 1: Write the failing guard.** Create `tests/unit/test_contrast_classes.py`:

```python
"""X-01 source guard: the text/background classes that fail WCAG AA 4.5:1 for normal
text are not used anywhere a page is built from. The rendered-page gate
(tests/integration/test_rendered_page_gate.py) checks what actually renders; this scans
every template branch, script and src/ class string, rendered or not."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Bare (unprefixed) classes retired by X-01, with the replacement each now has.
FORBIDDEN = {
    "text-gray-300": "text-gray-600",
    "text-gray-400": "text-gray-600",
    "text-green-500": "text-green-700",
    "text-green-600": "text-green-700",
    "text-amber-600": "text-amber-700",
    "text-yellow-600": "text-yellow-700",
    "text-red-500": "text-red-600",
    "text-blue-500": "text-blue-600",
    "text-indigo-500": "text-indigo-600",
    "bg-green-600": "bg-green-700",
    "bg-amber-600": "bg-amber-700",
    "bg-yellow-600": "bg-yellow-700",
    "bg-gray-400": "bg-gray-600",
}
_TOKEN = re.compile(r"(?<![\w:-])(" + "|".join(map(re.escape, FORBIDDEN)) + r")(?![\w-])")


def _sources():
    yield from (ROOT / "templates").rglob("*.html")
    yield from (p for p in (ROOT / "static/js").rglob("*.js"))
    yield from (ROOT / "src").rglob("*.py")


def test_no_retired_contrast_class_is_used():
    found = []
    for path in _sources():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _TOKEN.finditer(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: {m.group(1)} -> {FORBIDDEN[m.group(1)]}")
            if "bg-gray-100 text-gray-500" in line:
                found.append(f"{path.relative_to(ROOT)}:{n}: text-gray-500 on bg-gray-100 -> text-gray-600")
    assert not found, "\n".join(found)


def test_dynamic_shades_are_the_passing_ones():
    body = (ROOT / "templates/admin/_assessments_body.html").read_text()
    assert "band_class(a.band, 600, 400)" not in body
    for rel in ("templates/admin/jobs.html", "templates/admin/discussions.html",
                "templates/manager/discussions.html"):
        assert "}}-600" not in (ROOT / rel).read_text(), rel
```

- [ ] **Step 2: Run it.** `.venv-test/bin/python -m pytest tests/unit/test_contrast_classes.py -v`
  Expected: both FAIL, listing every occurrence.

- [ ] **Step 3: Apply the map.** Run once from the repo root (not committed):

```bash
.venv-test/bin/python - <<'PY'
import re
from pathlib import Path

# Hover shades first, so moving a base shade up never lands on its own hover class.
MAP = [
    ("hover:bg-green-700", "hover:bg-green-800"), ("bg-green-600", "bg-green-700"),
    ("hover:bg-amber-700", "hover:bg-amber-800"), ("bg-amber-600", "bg-amber-700"),
    ("bg-yellow-600", "bg-yellow-700"),
    ("hover:bg-gray-500", "hover:bg-gray-700"), ("bg-gray-400", "bg-gray-600"),
    ("text-gray-300", "text-gray-600"), ("text-gray-400", "text-gray-600"),
    ("text-green-500", "text-green-700"), ("text-green-600", "text-green-700"),
    ("text-amber-600", "text-amber-700"), ("text-yellow-600", "text-yellow-700"),
    ("text-red-500", "text-red-600"), ("text-blue-500", "text-blue-600"),
    ("text-indigo-500", "text-indigo-600"),
]
files = [*Path("templates").rglob("*.html"), *Path("static/js").rglob("*.js"),
         *Path("src").rglob("*.py")]
for path in files:
    text = path.read_text(encoding="utf-8")
    new = text
    for old, repl in MAP:
        if old.startswith("hover:"):
            new = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", repl, new)
        else:
            new = re.sub(rf"(?<![\w:-]){re.escape(old)}(?![\w-])", repl, new)
    new = new.replace("bg-gray-100 text-gray-500", "bg-gray-100 text-gray-600")
    if new != text:
        path.write_text(new, encoding="utf-8")
        print("updated", path)
PY
```
  `hover:bg-green-700` and `hover:bg-amber-700` occur only beside `bg-green-600` /
  `bg-amber-600` today (grep on `e8f475f`), and `hover:bg-gray-500` only on the
  prompt-suggestion "Reopen" button with `bg-gray-400`, so the hover moves track their bases.
  Then by hand:
  - `templates/admin/_assessments_body.html`: both `band_class(a.band, 600, 400)` become
    `band_class(a.band, 700, 600)` (the detail page already uses 700/600);
  - `templates/admin/jobs.html`: `text-{{ color }}-600` becomes `text-{{ color }}-700`;
  - `templates/admin/discussions.html` and `templates/manager/discussions.html`:
    `text-{{ meta.color }}-600` becomes `text-{{ meta.color }}-700` (yellow-600 fails even as
    large text, 2.9:1).

- [ ] **Step 4: Rebuild CSS and check the runtime-built classes compiled.** Run
  `scripts/build_css.sh`, then
  `for c in text-yellow-700 text-blue-700 text-green-700 text-red-700 text-gray-700 text-amber-700; do grep -q -- "\.$c[{,:]" static/css/app.css || echo "missing $c"; done`.
  For every class printed as missing, add it to the `safelist` array of `tailwind.config.js`
  and rerun `scripts/build_css.sh` until nothing prints. These six are the names
  `jobs.html` and the two `discussions.html` build at runtime; every other new class
  (`hover:bg-green-800`, `bg-amber-700`, …) is a literal in a template and compiles from
  the `content` scan.

- [ ] **Step 5: Run.** `.venv-test/bin/python -m pytest tests/unit/test_contrast_classes.py tests/unit/test_bands.py tests/integration/test_admin_simulation_page.py tests/integration/test_assessment_detail_page.py tests/integration/test_assessment_chat_templates.py -v`
  Expected: all pass (`test_bands.py` pins `band_class`'s arithmetic, which is unchanged).

- [ ] **Step 6: Commit.**
```bash
git add -u templates static/js src tailwind.config.js static/css/app.css
git add tests/unit/test_contrast_classes.py
git commit -m "fix(webui): AA contrast for gray, green, amber, yellow, red, blue, indigo text and buttons (X-01)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-13: Every form control has an accessible name (X-02)

**Inventory.** A static scan of every template on `e8f475f` (each `input` other than
hidden/submit/button, each `select` and `textarea`, accepted when wrapped by a `<label>`,
pointed at by `<label for>`, or carrying `aria-label`/`aria-labelledby`) found the controls
below unnamed; the axe crawl's `label`, `select-name` and `label-title-only` samples are a
subset of it. Controls replaced by 1C-8 (Finalize), 1C-10 (tag inputs) and 1C-11 (chat) are
already named there. A visible `<label>` that sits beside its control gets `for`; a control
with no visible label gets `aria-label`.

**Files:** (all after Part 1A, whose handler removal changes the same tags, and after 1C-12)
- Modify: `templates/access_pending.html`, `templates/admin/access_requests.html`,
  `templates/admin/agent_detail.html`, `templates/admin/agents.html`,
  `templates/admin/cohort_detail.html`, `templates/admin/cohort_topology.html`,
  `templates/admin/cohorts.html`, `templates/admin/discussions.html`,
  `templates/manager/discussions.html`, `templates/admin/jobs.html`,
  `templates/admin/llm_calls.html`, `templates/admin/simulation.html`,
  `templates/admin/user_detail.html`, `templates/admin/users.html`,
  `templates/manager/pis.html`, `templates/agent/dashboard.html`,
  `templates/manager/pi_detail.html`, `templates/profile/edit.html`,
  `templates/onboarding/profile_review.html`, `templates/agent/public_profile.html`,
  `templates/profile/delete_account.html`
- Test: `tests/integration/test_rendered_page_gate.py` (1C-15) is the deterministic check;
  this task adds a template-level scan so the edit is verified before the gate exists.
- Create: `tests/unit/test_control_names.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: element ids listed in the table (stable; the harness journeys use `confirm-run`
  and `assessment-chat-question` from earlier tasks).

- [ ] **Step 1: Write the failing template scan.** Create `tests/unit/test_control_names.py`:

```python
"""X-02 template scan: every form control in templates/ has an accessible name.

Static and template-level, so it also covers branches a seeded page does not render; the
rendered-page gate (tests/integration/test_rendered_page_gate.py) checks real pages.
Jinja tags are blanked before parsing, so a control inside {% if %} is still seen."""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SKIP_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
_JINJA_STMT = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels: list[dict] = []
        self.label_for: set[str] = set()
        self.controls: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "label":
            self.labels.append({"for": a.get("for"), "text": [], "controls": []})
        elif tag in ("input", "select", "textarea"):
            if tag == "input" and a.get("type", "text").lower() in _SKIP_TYPES:
                return
            self.controls.append({"tag": tag, "a": a, "line": self.getpos()[0], "wrapped": False})
            for label in self.labels:
                label["controls"].append(len(self.controls) - 1)

    def handle_endtag(self, tag):
        if tag == "label" and self.labels:
            label = self.labels.pop()
            if "".join(label["text"]).strip():
                for i in label["controls"]:
                    self.controls[i]["wrapped"] = True
                if label["for"]:
                    self.label_for.add(label["for"])

    def handle_data(self, data):
        for label in self.labels:
            label["text"].append(data)


def _unnamed(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # Keep line numbers: replace each Jinja construct with the same number of newlines.
    text = _JINJA_STMT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = _JINJA_EXPR.sub(lambda m: "X" + "\n" * m.group(0).count("\n"), text)
    scan = _Scan()
    scan.feed(text)
    out = []
    for c in scan.controls:
        a = c["a"]
        if a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip():
            continue
        if c["wrapped"] or (a.get("id") and a["id"] in scan.label_for):
            continue
        out.append(f"{path.relative_to(ROOT)}:{c['line']}: <{c['tag']} name={a.get('name')!r}>")
    return out


def test_every_template_control_has_an_accessible_name():
    found = [msg for path in sorted((ROOT / "templates").rglob("*.html")) for msg in _unnamed(path)]
    assert not found, "\n".join(found)
```

  A `<label for="x">` whose `x` is a Jinja expression (`for="{{ edit_prefix }}comment"`) is
  blanked to `X…`; such controls carry the same expression in `id`, which is blanked the same
  way, so they still match.

- [ ] **Step 2: Run it.** `.venv-test/bin/python -m pytest tests/unit/test_control_names.py -v`
  Expected: FAIL, listing the controls of the table below.

- [ ] **Step 3: Name each control.** Locate each by the anchor text; "for/id `v`" means add
  `for="v"` to the label and `id="v"` to the control.

| Template | Control (anchor) | Edit |
|---|---|---|
| `access_pending.html` | `<input type="email" name="email"` | `aria-label="Your email address"` |
| `admin/access_requests.html` | `name="orcid" required placeholder="0000-0000-0000-0000"` | `aria-label="ORCID iD to allow"` |
| `admin/access_requests.html` | `name="note" placeholder="note (optional)"` | `aria-label="Note (optional)"` |
| `admin/agent_detail.html` | label `Agent ID (slug)` / `name="agent_slug"` | for/id `agent-slug` |
| `admin/agent_detail.html` | label `Bot Name` / `name="bot_name"` | for/id `agent-bot-name` |
| `admin/agent_detail.html` | label `Status` / `<select name="agent_status"` | for/id `agent-status` |
| `admin/agent_detail.html` | label `Slack Bot Token` / `name="replace_slack_bot_token"` | for/id `agent-token` |
| `admin/agent_detail.html` | label `Role` / `<select name="role"` | for/id `agent-role` |
| `admin/agents.html` | `<select name="user_id"` | `aria-label="Link {{ a.bot_name }} to a user"` |
| `admin/cohort_detail.html` | label `Agent` / `<select name="agent_id"` | for/id `add-agent-id` |
| `admin/cohort_topology.html` | `<input type="checkbox" name="cell" value="{{ cell }}"` | `aria-label="{{ a.bot_name }} in {{ c.name }}"`, placed after `value="{{ cell }}"` (keeps `name="cell" value=` adjacent for `test_cohort_admin.py`'s regex) |
| `admin/cohorts.html` | label `Name` / `name="name" required pattern=` | for/id `cohort-name` |
| `admin/cohorts.html` | label `Description (optional)` / `<textarea name="description"` | for/id `cohort-description` |
| `admin/discussions.html`, `manager/discussions.html` | label `Run:` / `<select name="run_id"` | for/id `discussions-run` |
| same two | `<select name="channel_filter"` | `aria-label="Channel"` |
| same two | `<select name="status_filter"` | `aria-label="Status"` |
| same two | `<select name="agent_filter" multiple` | `aria-label="Agents (hold Ctrl or Cmd to pick several)"` (its `title` stays) |
| `admin/jobs.html` | label `Status` / `id="status-filter"` | `for="status-filter"` on the label |
| `admin/jobs.html` | label `Type` / `id="type-filter"` | `for="type-filter"` on the label |
| `admin/llm_calls.html` | labels `Agent`, `Phase`, `Model`, `Interview (channel)` / selects `agent`, `phase`, `model`, `channel` | for/id `llm-agent`, `llm-phase`, `llm-model`, `llm-channel` |
| `admin/simulation.html` | label `Max runtime (minutes, 0 = indefinite)` / `name="max_runtime"` | for/id `start-max-runtime` |
| `admin/simulation.html` | label `Max proposals (0 = no limit)` / `name="max_proposals"` | for/id `start-max-proposals` |
| `admin/simulation.html` | label `Channels` / `name="channels"` | for/id `announce-channels` |
| `admin/simulation.html` | `<textarea name="body"` | `aria-label="Run-start announcement template"` |
| `admin/simulation.html` | `<select name="run"` | `aria-label="Simulation run"` |
| `admin/user_detail.html` | `<select name="user_role"` | `aria-label="Role for {{ target_user.name }}"` |
| `admin/users.html` | impersonate `<input type="text" name="orcid"` | `aria-label="ORCID iD of the user to impersonate"` |
| `admin/users.html`, `manager/pis.html` | label `Profile Status` / `id="status-filter"` | `for="status-filter"` on the label |
| `admin/users.html`, `manager/pis.html` | label `Claimed` / `id="claimed-filter"` | `for="claimed-filter"` on the label |
| `agent/dashboard.html` | `<textarea name="emails"` | `aria-label="Email addresses to invite"` |
| `manager/pi_detail.html` | labels `Display Name`, `Email`, `Institution`, `Department`, `Research Summary` / their controls | for/id `pi-name`, `pi-email`, `pi-institution`, `pi-department`, `pi-summary` |
| `manager/pi_detail.html` | label `id="jhu_tenure_start_field"` / `name="jhu_tenure_start"` | `for="pi-tenure"` on the label (its `id` stays), `id="pi-tenure"` on the input |
| `profile/edit.html` | labels `Display Name`, `Email`, `Institution`, `Department`, `Research Summary` | for/id `profile-name`, `profile-email`, `profile-institution`, `profile-department`, `profile-summary` |
| `onboarding/profile_review.html` | labels `Email`, `Research Summary` | for/id `onboarding-email`, `onboarding-summary` |
| `agent/public_profile.html` | label `Research Summary` / `<textarea name="research_summary"` | for/id `public-summary` |
| `profile/delete_account.html` | label `Type delete to confirm` / `name="confirm"` | for/id `delete-confirm` |

- [ ] **Step 4: Run.** `.venv-test/bin/python -m pytest tests/unit/test_control_names.py tests/integration/test_cohort_admin.py tests/integration/test_manager_views.py tests/integration/test_admin_simulation_page.py -v`
  Expected: all pass. A control the scan still lists that is not in the table gets the same
  treatment (visible label → `for`/`id`; none → `aria-label` naming what it holds).

- [ ] **Step 5: Commit.**
```bash
git add -u templates
git add tests/unit/test_control_names.py
git commit -m "fix(webui): accessible names for every form control (X-02)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-14: Keyboard-reachable rows (X-05)

**Files:**
- Modify: `templates/admin/activity.html:57-59`, `templates/manager/activity.html:57-59`,
  `templates/admin/users.html:66-73`, `templates/manager/pis.html:99-106`,
  `templates/admin/_discussions_threads.html:73-79`; after Part 1A (which turned the row
  `onclick`s into `data-row-href`) and 1C-13
- Modify: `static/js/ui.js` (append; Part 1A's file)
- Modify: `tests/unit/test_ui_behaviours.py` (Task 1A-6): in
  `test_every_toggle_target_names_an_id_in_the_same_template` the expected set becomes
  `{"admin/cohorts.html"}`, because this task replaces `_discussions_threads.html`'s
  `data-toggle-target` with `button[data-toggles]` (assembly audit PX-08b)
- Test: `tests/unit/test_row_links.py`

**Interfaces:**
- Consumes: `data-row-href` behaviour in `static/js/ui.js` (Part 1A): a click on the row
  navigates unless it lands inside `a, button, input, select, textarea, label, summary`.
- Produces: `ui.js` behaviours `button[data-toggles="<id>"]` (toggles `hidden` on `#<id>` and
  `aria-expanded`) and `tr[data-row-toggles]` (a click elsewhere in the row clicks the row's
  `[data-toggles]` button).

- [ ] **Step 1: Write the failing source test.** Create `tests/unit/test_row_links.py`:

```python
"""X-05: a clickable row is reachable by keyboard: a navigating row has a real link in its
first cell (ui.js keeps the whole-row click through data-row-href); the discussions row
that expands a detail row does it through a button with aria-expanded."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAVIGATING = {
    "templates/admin/activity.html": "/admin/activity/{{ run.id }}",
    "templates/manager/activity.html": "/manager/activity/{{ run.id }}",
    "templates/admin/users.html": "/admin/users/{{ item.user.id }}",
    "templates/manager/pis.html": "/manager/pis/{{ item.user.id }}",
}


def _row_and_first_cell(text: str, marker: str) -> tuple[str, str]:
    start = text.index(marker)
    row_start = text.rindex("<tr", 0, start)
    row_tag = text[row_start: text.index(">", row_start) + 1]
    first_td = text[text.index("<td", row_start): text.index("</td>", row_start)]
    return row_tag, first_td


def test_navigating_rows_link_from_their_first_cell():
    for rel, href in NAVIGATING.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        row, cell = _row_and_first_cell(text, f'data-row-href="{href}"')
        assert "onclick" not in row, rel
        assert f'<a href="{href}"' in cell, rel


def test_the_discussion_row_expands_through_a_button():
    text = (ROOT / "templates/admin/_discussions_threads.html").read_text(encoding="utf-8")
    assert "onclick" not in text
    row, cell = _row_and_first_cell(text, "data-row-toggles")
    assert 'data-toggles="detail-{{ loop.index }}"' in cell
    assert 'aria-controls="detail-{{ loop.index }}"' in cell
    assert 'aria-expanded="false"' in cell


def test_ui_js_drives_the_disclosure():
    js = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
    assert re.search(r'closest\("button\[data-toggles\]"\)', js)
    assert 'setAttribute("aria-expanded"' in js
    assert re.search(r'closest\("tr\[data-row-toggles\]"\)', js)
```

- [ ] **Step 2: Run it.** `.venv-test/bin/python -m pytest tests/unit/test_row_links.py -v`
  Expected: FAIL (no `<a href>` in the first cells; no `data-toggles`).

- [ ] **Step 3: Links in the first cell.** End state of each navigating row (keep whatever
  `data-row-href` value Part 1A wrote; it equals the href below):
  - `templates/admin/activity.html` and `templates/manager/activity.html` (prefix `/admin` or `/manager`):
    ```jinja
                <tr class="hover:bg-gray-50 cursor-pointer" data-row-href="/admin/activity/{{ run.id }}">
                    <td class="px-4 py-3 text-sm"><a href="/admin/activity/{{ run.id }}" class="text-indigo-700 hover:underline"><span data-utc="{{ run.started_at.isoformat() }}" data-utc-fmt="short">{{ run.started_at.strftime('%b %d %H:%M') }}</span></a></td>
    ```
  - `templates/admin/users.html` and `templates/manager/pis.html` (prefix `/admin/users` or `/manager/pis`), the first cell's name line, before
    `<div class="text-sm font-medium text-gray-900">{{ item.user.name }}</div>` after
    `<div class="text-sm font-medium text-gray-900"><a href="/admin/users/{{ item.user.id }}" class="hover:text-indigo-700 hover:underline">{{ item.user.name }}</a></div>`
    with the `<tr>` reading `<tr class="hover:bg-gray-50 cursor-pointer" data-row-href="/admin/users/{{ item.user.id }}">`.

- [ ] **Step 4: The discussion row.** In `templates/admin/_discussions_threads.html` the row
  and first cell become (remove whatever row-level toggle attribute Part 1A put on this
  `<tr>`, and delete that attribute's handler from `ui.js` if it handles nothing else):
  ```jinja
              <tr class="hover:bg-gray-50 {% if has_detail %}cursor-pointer{% endif %}"
                  {% if has_detail %}data-row-toggles{% endif %}>
                  <td class="px-4 py-3">
                      {% if has_detail %}<button type="button" data-toggles="detail-{{ loop.index }}"
                              aria-controls="detail-{{ loop.index }}" aria-expanded="false"
                              class="text-left">{% endif %}
                      <span class="px-2 py-0.5 rounded-full text-xs bg-{{ meta.color }}-100 text-{{ meta.color }}-700">
                          {{ meta.label }}
                      </span>
                      {% if has_detail %}<span class="sr-only">Show details</span></button>{% endif %}
                  </td>
  ```
  and in the comment block above it replace "A decision without one left an onclick whose
  getElementById returns null" with "A decision without one left a click handler whose
  getElementById returned null".
  Append to `static/js/ui.js`:
  ```javascript
  // Disclosure (X-05): <button data-toggles="<id>" aria-controls="<id>"
  // aria-expanded="false"> shows or hides #<id> and keeps aria-expanded in step; a
  // <tr data-row-toggles> forwards a click anywhere else in the row to its button, so
  // the whole row stays clickable while the button is the keyboard path.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-toggles]");
    if (button) {
      var target = document.getElementById(button.getAttribute("data-toggles"));
      if (!target) return;
      var opening = target.classList.contains("hidden");
      target.classList.toggle("hidden", !opening);
      button.setAttribute("aria-expanded", opening ? "true" : "false");
      return;
    }
    var row = event.target.closest("tr[data-row-toggles]");
    if (!row || event.target.closest("a, button, input, select, textarea, label, summary")) return;
    var rowButton = row.querySelector("button[data-toggles]");
    if (rowButton) rowButton.click();
  });
  ```

- [ ] **Step 5: Rebuild CSS and run.** `scripts/build_css.sh` then
  `.venv-test/bin/python -m pytest tests/unit/test_row_links.py tests/unit/test_reachability.py tests/integration/test_manager_views.py tests/integration/test_conversation_feed.py -v`
  Expected: all pass (the new `<a href>`s also credit their routes in the reachability gate).

- [ ] **Step 6: Commit.**
```bash
git add templates/admin/activity.html templates/manager/activity.html templates/admin/users.html templates/manager/pis.html templates/admin/_discussions_threads.html static/js/ui.js static/css/app.css tests/unit/test_row_links.py tests/unit/test_ui_behaviours.py
git commit -m "fix(webui): keyboard-reachable table rows: first-cell links and a disclosure button (X-05)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-15: The rendered-page gate (§6.10, D12)

**Route list: the app's route table, not a hand list.** The gate imports
`tests.unit.test_reachability.http_routes` (the same flattened table the reachability gate
walks, FastAPI 0.140+ `_IncludedRouter` shape included) and requires every GET path to be
either in `PAGES` (with a URL built from the seed) or in `SKIPPED` (with the reason it
renders no HTML page). A route added later fails `test_every_get_route_is_walked_or_skipped`
until someone decides how the gate renders it, which an explicit list alone would not force;
the explicit `PAGES` map is still needed because path parameters need real seeded ids.

**Files:**
- Create: `tests/integration/test_rendered_page_gate.py`
- Test: itself

**Interfaces:**
- Consumes: Part 1A's removal of every inline `on…=` handler and `<tr onclick>` (the gate's
  inline-handler check fails until 1A has run); the §6.4 removal of the four graph routes
  (otherwise `test_every_get_route_is_walked_or_skipped` reports them); 1C-12 (no gray-300/400),
  1C-13 (names), 1C-14 (rows), 1C-2 (error pages are HTML and are checked too);
  `client`, `db_session` fixtures; `tests/factories.py` (`make_user`, `make_profile`,
  `make_agent`, `make_simulation_run`); `tests.assessment_chat_support.seed_interview`;
  `tests.integration.test_manager_access.auth_headers`; `tests.unit.test_reachability.http_routes`.
- Produces: `tests/integration/test_rendered_page_gate.py` with `page_problems(html: str) -> list[str]`,
  `PAGES: dict[str, Callable[[GateWorld], str]]`, `SKIPPED: dict[str, str]`,
  `NO_200_EXPECTED: dict[str, str]`, `ROLES`, fixture `gate_world`. Phase 2 extends
  `page_problems` and the role set.

- [ ] **Step 1: Write the gate.** Create `tests/integration/test_rendered_page_gate.py`:

```python
"""The rendered-page gate (spec §6.10, decision D12; X-01, X-02, X-05, and §6.3's
inline-handler removal).

Every GET page the app serves is rendered against factory data as an anonymous visitor, a
PI who owns an agent, a reviewer, a manager and an admin. Every HTML response that is not a
redirect (error pages included) must hold four properties:

* every ``input`` (other than hidden/submit/button/reset/image), ``select`` and ``textarea``
  has an accessible name: a non-empty ``aria-label`` or ``aria-labelledby``, a wrapping
  ``<label>`` with text, or a ``<label for>`` with text pointing at its ``id`` (a ``title``
  or ``placeholder`` alone does not count);
* no ``text-gray-300``/``text-gray-400`` anywhere in the document, scripts included (X-01);
* no ``<tr onclick>`` (X-05);
* no inline ``on…=`` attribute on any element (the Phase 2 enforced CSP blocks them).

The page list is the app's own route table (``http_routes``, shared with
``tests/unit/test_reachability.py``): a new GET route fails
``test_every_get_route_is_walked_or_skipped`` until it is added to ``PAGES`` with a URL
built from the seed, or to ``SKIPPED`` with the reason it renders no HTML page. Phase 2
extends ``page_problems``.
"""
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    Cohort,
    CohortMembership,
    DelegateInvitation,
    PromptChangeSuggestion,
)
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.integration.test_manager_access import auth_headers
from tests.unit.test_reachability import http_routes

pytestmark = pytest.mark.integration

ROLES = ("anon", "pi", "reviewer", "manager", "admin")
_PROMPT_FILE = "prompts/agent-system.md"


@dataclass
class GateWorld:
    users: dict[str, Any]
    agent: Any
    run: Any
    assessment_id: uuid.UUID
    root_ts: str
    cohort: Any
    suggestion: Any
    invite_token: str


PAGES: dict[str, Callable[[GateWorld], str]] = {
    "/": lambda w: "/",
    "/access-pending": lambda w: "/access-pending",
    "/admin": lambda w: "/admin",
    "/admin/access-requests": lambda w: "/admin/access-requests",
    "/admin/activity": lambda w: "/admin/activity",
    "/admin/activity/{run_id}": lambda w: f"/admin/activity/{w.run.id}",
    "/admin/activity/{run_id}/llm-calls": lambda w: f"/admin/activity/{w.run.id}/llm-calls",
    "/admin/agents": lambda w: "/admin/agents",
    "/admin/agents/{agent_id}": lambda w: f"/admin/agents/{w.agent.id}",
    "/admin/assessments": lambda w: "/admin/assessments",
    "/admin/assessments/{assessment_id}": lambda w: f"/admin/assessments/{w.assessment_id}",
    "/admin/cohorts": lambda w: "/admin/cohorts",
    "/admin/cohorts/topology": lambda w: "/admin/cohorts/topology",
    "/admin/cohorts/{cohort_id}": lambda w: f"/admin/cohorts/{w.cohort.id}",
    "/admin/discussions": lambda w: "/admin/discussions",
    "/admin/jobs": lambda w: "/admin/jobs",
    "/admin/simulation": lambda w: "/admin/simulation",
    "/admin/users": lambda w: "/admin/users",
    "/admin/users/{user_id}": lambda w: f"/admin/users/{w.users['pi'].id}",
    "/agent": lambda w: "/agent",
    "/agent/{agent_id}/conversations": lambda w: f"/agent/{w.agent.agent_id}/conversations",
    "/agent/{agent_id}/dashboard": lambda w: f"/agent/{w.agent.agent_id}/dashboard",
    "/agent/{agent_id}/public-profile": lambda w: f"/agent/{w.agent.agent_id}/public-profile",
    "/agent/{agent_id}/public-profile/edit": lambda w: f"/agent/{w.agent.agent_id}/public-profile/edit",
    "/agent/{agent_id}/thread/{message_ts}": lambda w: f"/agent/{w.agent.agent_id}/thread/{w.root_ts}",
    "/invite/{token}": lambda w: f"/invite/{w.invite_token}",
    "/login": lambda w: "/login",
    "/manager": lambda w: "/manager",
    "/manager/activity": lambda w: "/manager/activity",
    "/manager/activity/{run_id}": lambda w: f"/manager/activity/{w.run.id}",
    "/manager/assessments": lambda w: "/manager/assessments",
    "/manager/assessments/{assessment_id}": lambda w: f"/manager/assessments/{w.assessment_id}",
    "/manager/discussions": lambda w: "/manager/discussions",
    "/manager/pis": lambda w: "/manager/pis",
    "/manager/pis/{user_id}": lambda w: f"/manager/pis/{w.users['pi'].id}",
    "/manager/prompt-suggestions": lambda w: "/manager/prompt-suggestions",
    "/manager/prompt-suggestions/{suggestion_id}": lambda w: f"/manager/prompt-suggestions/{w.suggestion.id}",
    "/manager/slack-bots": lambda w: "/manager/slack-bots",
    "/onboarding": lambda w: "/onboarding",
    "/profile": lambda w: "/profile",
    "/profile/delete-account": lambda w: "/profile/delete-account",
    "/profile/edit": lambda w: "/profile/edit",
    "/settings": lambda w: "/settings",
}

SKIPPED: dict[str, str] = {
    "/admin/agents/slack/callback": "Slack OAuth redirect target; redirects, renders no page",
    "/auth/callback": "ORCID OAuth redirect target; redirects, renders no page",
    "/login/start": "redirects to ORCID",
    "/assessment-chat/{assessment_id}": "JSON for static/js/assessment_chat.js",
    "/api/health": "JSON health probe",
}

#: Walked (any HTML they return is still checked) but expected to answer every role with a
#: redirect or an error page, so the "something rendered 200" control skips them.
NO_200_EXPECTED: dict[str, str] = {
    "/": "redirects to /profile or /login",
    "/manager": "redirects to /manager/pis",
    "/onboarding": "an onboarded user is redirected to /profile; anonymous to /login",
    "/invite/{token}": "accepting needs the invitee's own verified address (spec §6.6); no gate role is the invitee",
}

_SKIP_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
_LOW_CONTRAST = re.compile(r"(?<![\w-])text-gray-[34]00(?![\w-])")


class _GateParser(HTMLParser):
    """Collects form controls, the labels naming them, and inline event attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[dict[str, Any]] = []
        self.label_text_for: dict[str, str] = {}
        self.inline_handlers: set[str] = set()
        self.tr_onclick = 0
        self._labels: list[dict[str, Any]] = []

    def handle_starttag(self, tag, attrs):
        a = {name: (value or "") for name, value in attrs}
        for name in a:
            if name.startswith("on"):
                self.inline_handlers.add(f"<{tag} {name}=>")
                if tag == "tr" and name == "onclick":
                    self.tr_onclick += 1
        if tag == "label":
            self._labels.append({"for": a.get("for"), "text": [], "controls": []})
            return
        if tag in ("input", "select", "textarea"):
            if tag == "input" and a.get("type", "text").lower() in _SKIP_INPUT_TYPES:
                return
            self.controls.append({"tag": tag, "attrs": a, "line": self.getpos()[0], "wrapped": False})
            for label in self._labels:
                label["controls"].append(len(self.controls) - 1)

    def handle_endtag(self, tag):
        if tag != "label" or not self._labels:
            return
        label = self._labels.pop()
        text = " ".join("".join(label["text"]).split())
        if not text:
            return
        for index in label["controls"]:
            self.controls[index]["wrapped"] = True
        if label["for"]:
            self.label_text_for[label["for"]] = text

    def handle_data(self, data):
        for label in self._labels:
            label["text"].append(data)

    def unnamed_controls(self) -> list[str]:
        out = []
        for control in self.controls:
            a = control["attrs"]
            if a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip():
                continue
            if control["wrapped"] or (a.get("id") and a["id"] in self.label_text_for):
                continue
            out.append(
                f"line {control['line']}: <{control['tag']} name={a.get('name')!r} "
                f"id={a.get('id')!r}> has no accessible name"
            )
        return out


def page_problems(html: str) -> list[str]:
    """Every gate violation in one HTML document."""
    parser = _GateParser()
    parser.feed(html)
    parser.close()
    problems = parser.unnamed_controls()
    if _LOW_CONTRAST.search(html):
        problems.append("uses text-gray-300/text-gray-400 (X-01)")
    if parser.tr_onclick:
        problems.append(f"{parser.tr_onclick} <tr onclick> row(s) (X-05)")
    problems += [f"inline handler {h}" for h in sorted(parser.inline_handlers)]
    return problems


def test_page_problems_catches_each_defect_and_accepts_each_naming():
    bad = (
        '<input name="a"><select name="b"></select><textarea name="c" title="t"></textarea>'
        '<p class="text-gray-400">x</p><table><tr onclick="go()"><td>1</td></tr></table>'
        '<button onclick="x()">b</button>'
    )
    problems = page_problems(bad)
    assert sum("has no accessible name" in p for p in problems) == 3
    assert any("text-gray-300/text-gray-400" in p for p in problems)
    assert any("<tr onclick>" in p for p in problems)
    assert "inline handler <button onclick=>" in problems
    good = (
        '<label for="a">A</label><input id="a" name="a">'
        '<label>B <select name="b"></select></label>'
        '<textarea name="c" aria-label="C"></textarea>'
        '<input type="hidden" name="h"><input type="submit" value="Go">'
        '<p class="text-gray-600">x</p><script>var s = "<tr onclick>";</script>'
    )
    assert page_problems(good) == []


def test_every_get_route_is_walked_or_skipped():
    routes = {r.path for r in http_routes() if r.method == "GET"}
    assert not set(PAGES) & set(SKIPPED)
    missing = routes - set(PAGES) - set(SKIPPED)
    stale = (set(PAGES) | set(SKIPPED)) - routes
    assert not missing, f"GET routes the gate neither walks nor skips: {sorted(missing)}"
    assert not stale, f"gate entries for routes that no longer exist: {sorted(stale)}"
    assert set(NO_200_EXPECTED) <= set(PAGES)


@pytest.fixture
async def gate_world(db_session) -> GateWorld:
    users = {
        "admin": await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Gate Admin"),
        "manager": await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Gate Manager"),
        "reviewer": await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Gate Reviewer"),
        "pi": await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Gate PI"),
    }
    await factories.make_profile(
        db_session, user=users["pi"], evidence_pub_count=1,
        techniques=["cryo-EM", "1,2-dichloroethane"], keywords=["kinase"],
    )
    agent = await factories.make_agent(
        db_session, user=users["pi"], agent_id="gatepi", bot_name="GatePiBot", pi_name="Gate PI",
    )
    await factories.make_agent(
        db_session, agent_id="blackbird", bot_name="BlackbirdBot", pi_name="Blackbird",
        role="scout_hub",
    )
    now = datetime.now(UTC)
    run = await factories.make_simulation_run(
        db_session, status="stopped", started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=1),
    )
    interview = await seed_interview(db_session, run=run, subject="gatepi", channel="gate-interview")
    db_session.add(AssessmentReview(
        assessment_id=interview.assessment_id, reviewer_user_id=users["reviewer"].id,
        reviewer_name="Gate Reviewer", score=3, comment="Gate review", feedback_mode="learn",
    ))
    cohort = Cohort(name="gate-cohort", created_by=users["admin"].id)
    db_session.add(cohort)
    await db_session.flush()
    db_session.add(CohortMembership(cohort_id=cohort.id, agent_id="gatepi", added_by=users["admin"].id))
    suggestion = PromptChangeSuggestion(
        assessment_id=interview.assessment_id,
        subject_label="Gate PI — Widget Co",
        feedback_snapshot=[{
            "id": str(uuid.uuid4()), "reviewer_name": "Gate Reviewer", "score": 3,
            "feedback_mode": "learn", "comment": "Missed the IP angle.",
            "created_at": "2026-10-01T00:00:00+00:00",
        }],
        target="scout_hub",
        prompt_files=[{
            "path": _PROMPT_FILE,
            "sha256_12": hashlib.sha256(Path(_PROMPT_FILE).read_bytes()).hexdigest()[:12],
        }],
        suggestion="Ask about the assay.",
        transcript_available=True,
    )
    db_session.add(suggestion)
    db_session.add(DelegateInvitation(
        agent_registry_id=agent.id, invited_by_user_id=users["pi"].id,
        email="invitee@example.org", token="gate-invite-token", status="pending",
        expires_at=now + timedelta(days=1),
    ))
    await db_session.flush()
    return GateWorld(
        users=users, agent=agent, run=run, assessment_id=interview.assessment_id,
        root_ts=interview.root_ts, cohort=cohort, suggestion=suggestion,
        invite_token="gate-invite-token",
    )


async def test_every_page_passes_the_gate(client, gate_world):
    problems: list[str] = []
    rendered_200: set[str] = set()
    for route, build in sorted(PAGES.items()):
        url = build(gate_world)
        for role in ROLES:
            headers = {} if role == "anon" else auth_headers(gate_world.users[role].id)
            r = await client.get(url, headers=headers, follow_redirects=False)
            if r.status_code >= 500:
                problems.append(f"{role} {url}: HTTP {r.status_code}")
                continue
            if 300 <= r.status_code < 400:
                continue
            if not r.headers.get("content-type", "").startswith("text/html"):
                continue
            if r.status_code == 200:
                rendered_200.add(route)
            problems += [f"{role} {url}: {p}" for p in page_problems(r.text)]
    never = set(PAGES) - rendered_200 - set(NO_200_EXPECTED)
    assert not never, f"no role rendered these with 200, so the gate checked nothing there: {sorted(never)}"
    assert not problems, "\n".join(problems)
```

- [ ] **Step 2: Run it.** `.venv-test/bin/python -m pytest tests/integration/test_rendered_page_gate.py -v`
  Expected: all four pass when 1A, the §6.4 removals and 1C-1 … 1C-14 are merged. A
  failure lists `role url: problem`; fix the template it names (the same treatment as
  1C-12/1C-13/1C-14), never the gate. If `never` names a route, its seed or role is wrong
  for that page: extend `gate_world`, or, when the page cannot render 200 for any role by
  design, add it to `NO_200_EXPECTED` with the reason.

- [ ] **Step 3: Commit.**
```bash
git add tests/integration/test_rendered_page_gate.py
git commit -m "test(webui): rendered-page accessibility and inline-handler gate over every GET route (§6.10)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1C-16: Harness journeys for Part 1C (spec §9, Phase 1)

**Files:**
- Modify (shared with Parts 1A and 1B): `tests/e2e/ui_audit/journeys_phase1.py` — append the
  functions below and add them to its `JOURNEYS` list; if the file does not exist yet, create
  it with the module docstring below and `JOURNEYS = [...]` holding these seven.
- Requirement on Phase 0's harness seed: the ids printed by `run serve` include `run` (a
  completed run), `assessments` (a list of assessment ids), `pi` (a PI user id) and
  `stopped_run` (a run with `status="stopped"` and `finalized_at` NULL). The first three are in
  the audit seed (`docs/audits/2026-10-01-web-ui/raw/harness/uiaudit_seed.py`); if Phase 0's
  seed lacks `stopped_run`, add to it
  `stopped = await factories.make_simulation_run(s, status="stopped", started_at=datetime.now(UTC) - timedelta(hours=5), ended_at=datetime.now(UTC) - timedelta(hours=4))`
  and `stopped_run=str(stopped.id)` in its printed ids.
- Requirement: `tests/e2e/ui_audit/axe.min.js` (the axe-core build the audit crawl used,
  `docs/audits/2026-10-01-web-ui/raw/harness/crawl.py` reads it beside itself); if Phase 0
  stores it elsewhere, set `AXE_PATH` below to that path.

**Interfaces:**
- Consumes: the Harness contract (`h.base_url`, `h.ids`, `await h.page(role, width=1280) -> (context, page, log)`);
  `static/js/confirm.js` (Phase 0); every 1C task above; the assessment chat enabled with the
  harness's fake model (Phase 0's `run serve`).
- Produces: `journey_confirm_stop`, `journey_confirm_reset_template`,
  `journey_confirm_review_delete`, `journey_cohort_two_tab_save`,
  `journey_chat_focus_after_answer`, `journey_contrast_zero`, `journey_confirm_finalize`, each
  `async def journey_<name>(h) -> dict` returning `{"ok": bool, ...evidence}`. Run with
  `python -m tests.e2e.ui_audit.run journeys --phase 1` before deploy (outside `ci.sh`, D17).

`journey_confirm_finalize` is listed last: its accepting leg leaves a pending Finalize stop
command in the throwaway database, which would make any later Start journey refuse.

- [ ] **Step 1: Add the journeys.** The module already exists (Parts 1A and 1B). Add
`import time` and `from tests.e2e.ui_audit.harness import AXE_PATH` to its TOP import block
if they are not there (ruff E402 forbids imports below code; assembly audit PX-14), and append
the code below after its last journey. `AXE_PATH` is the checksum-pinned bundle Phase 0's
`run.py` downloads before any journey runs.

```python
# --- Part 1C: confirm dialogs accepted and dismissed (announcing Stop, Reset to file
# default, review delete, Finalize with a wrong and the right short id), the two-tab
# topology save, chat focus after an answer, zero axe color-contrast violations.
_ROLES = ("anon", "pi", "reviewer", "manager", "admin")


class _Dialogs:
    """Records every dialog and answers it with the current ``accept`` setting."""

    def __init__(self, page):
        self.messages: list[str] = []
        self.accept = False
        page.on("dialog", self._on_dialog)

    async def _on_dialog(self, dialog):
        self.messages.append(dialog.message)
        if self.accept:
            await dialog.accept()
        else:
            await dialog.dismiss()


def _posts(page, path: str) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if r.method == "POST" and r.url.endswith(path) else None)
    return seen


async def _flash_text(page) -> str:
    region = page.locator("[data-flash-region]")
    return (await region.inner_text()) if await region.count() else ""


async def journey_confirm_stop(h) -> dict:
    """FN-01: the announcing Stop asks first; "Stop — hold open interviews" does not (D11).
    The harness runs no engine, so both buttons render disabled; the journey enables them in
    the DOM to reach the dialog, and the server then refuses with "Nothing is running."."""
    context, page, log = await h.page("admin")
    dialogs = _Dialogs(page)
    posts = _posts(page, "/admin/simulation/stop")
    enable = """() => document.querySelectorAll('form[action="/admin/simulation/stop"] button')
                         .forEach(b => { b.disabled = false; })"""
    announcing = page.locator('form[action="/admin/simulation/stop"]:not(:has(input[name="hold_open"])) button')
    hold = page.locator('form[action="/admin/simulation/stop"]:has(input[name="hold_open"]) button')

    await page.goto(h.base_url + "/admin/simulation")
    await page.evaluate(enable)
    await announcing.click()
    await page.wait_for_timeout(500)
    dismissed_posted = len(posts)

    dialogs.accept = True
    async with page.expect_navigation():
        await announcing.click()
    accepted_posted = len(posts) - dismissed_posted
    refusal = await _flash_text(page)

    asked_before_hold = len(dialogs.messages)
    await page.evaluate(enable)
    async with page.expect_navigation():
        await hold.click()
    hold_asked = len(dialogs.messages) > asked_before_hold
    await context.close()

    first = dialogs.messages[0] if dialogs.messages else ""
    ok = (first.startswith("Posts ") and "cannot be undone" in first
          and dismissed_posted == 0 and accepted_posted == 1
          and "Nothing is running." in refusal and not hold_asked)
    return {"ok": ok, "dialogs": dialogs.messages, "dismissed_posted": dismissed_posted,
            "accepted_posted": accepted_posted, "flash": refusal, "hold_asked": hold_asked,
            "log": log}


async def journey_confirm_reset_template(h) -> dict:
    """FN-06: Reset to file default asks first; dismissing posts nothing."""
    context, page, log = await h.page("admin")
    dialogs = _Dialogs(page)
    posts = _posts(page, "/admin/simulation/announce-template")
    reset = page.locator('form[action="/admin/simulation/announce-template"]:has(input[name="reset"][value="true"]) button')
    await page.goto(h.base_url + "/admin/simulation")
    await reset.click()
    await page.wait_for_timeout(500)
    dismissed_posted = len(posts)
    dialogs.accept = True
    async with page.expect_navigation():
        await reset.click()
    url_after = page.url
    confirmed = "Template reset to file default." in await page.content()
    await context.close()
    ok = (len(dialogs.messages) == 2 and dialogs.messages[0].startswith("Reset the run-start announcement")
          and dismissed_posted == 0 and len(posts) == 1 and confirmed)
    return {"ok": ok, "dialogs": dialogs.messages, "posts": posts, "url_after": url_after, "log": log}


async def journey_confirm_review_delete(h) -> dict:
    """B-10: deleting a review asks first; dismissing keeps it, accepting removes it."""
    context, page, log = await h.page("admin")
    dialogs = _Dialogs(page)
    marker = f"journey-delete-{int(time.time())}"
    url = f"{h.base_url}/admin/assessments/{h.ids['assessments'][0]}"
    await page.goto(url)
    await page.select_option("#add-score", "3")
    await page.select_option("#add-mode", "learn")
    await page.fill("#add-comment", marker)
    async with page.expect_navigation():
        await page.click('button:has-text("Submit feedback")')
    await page.goto(url)
    delete = page.locator(f'li:has-text("{marker}") form[action$="/delete"] button')
    if await delete.count() != 1:
        await context.close()
        return {"ok": False, "error": "the submitted review or its delete form was not found", "log": log}
    await delete.click()
    await page.wait_for_timeout(500)
    kept = await page.locator(f'li:has-text("{marker}")').count() == 1
    dialogs.accept = True
    async with page.expect_navigation():
        await delete.click()
    await page.goto(url)
    gone = await page.locator(f'li:has-text("{marker}")').count() == 0
    await context.close()
    ok = (len(dialogs.messages) == 2
          and dialogs.messages[0] == "Delete this review? This cannot be undone."
          and kept and gone)
    return {"ok": ok, "dialogs": dialogs.messages, "kept_after_dismiss": kept,
            "gone_after_accept": gone, "log": log}


async def journey_cohort_two_tab_save(h) -> dict:
    """C-07: two tabs open the topology; each ticks a different box and saves; both
    memberships survive."""
    context, tab1, log = await h.page("admin")
    name = f"two-tab-{int(time.time())}"
    created = await tab1.request.post(
        h.base_url + "/admin/cohorts/create", form={"name": name},
        headers={"Origin": h.base_url}, max_redirects=0,
    )
    tab2 = await context.new_page()
    topology = h.base_url + "/admin/cohorts/topology"
    await tab1.goto(topology)
    await tab2.goto(topology)
    href = await tab1.locator(f'thead a:text-is("{name}")').get_attribute("href")
    cohort_id = href.rsplit("/", 1)[1]
    agents = await tab1.eval_on_selector_all(
        'input[name="present_agent"]', "els => els.map(e => e.value)"
    )
    if len(agents) < 2:
        await context.close()
        return {"ok": False, "error": "the seed has fewer than two agents", "log": log}
    first, second = f'input[name="cell"][value="{cohort_id}:{agents[0]}"]', f'input[name="cell"][value="{cohort_id}:{agents[1]}"]'
    await tab1.check(first)
    async with tab1.expect_navigation():
        await tab1.click('button:has-text("Save topology")')
    await tab2.check(second)
    async with tab2.expect_navigation():
        await tab2.click('button:has-text("Save topology")')
    await tab1.goto(topology)
    both = await tab1.is_checked(first) and await tab1.is_checked(second)
    await context.close()
    return {"ok": created.status == 302 and both, "cohort": name, "create_status": created.status,
            "both_memberships_kept": both, "log": log}


async def journey_chat_focus_after_answer(h) -> dict:
    """B-06: the question box is read-only (not disabled) while answering and holds focus
    once the answer finishes."""
    context, page, log = await h.page("admin")
    await page.goto(f"{h.base_url}/admin/assessments/{h.ids['assessments'][0]}")
    opener = page.locator("[data-chat-open]").first
    if await opener.count() == 0:
        await context.close()
        return {"ok": False, "error": "no chat opener: is the assessment chat enabled in the harness?", "log": log}
    await opener.click()
    box = page.locator("#assessment-chat-question")
    await box.fill("What is being proposed, in plain terms?")
    await box.press("Enter")
    during = await page.evaluate(
        """() => { const t = document.getElementById('assessment-chat-question');
                   return {readOnly: t.readOnly, ariaBusy: t.getAttribute('aria-busy'),
                           disabled: t.disabled, focused: document.activeElement === t}; }"""
    )
    await page.wait_for_function(
        "() => !document.getElementById('assessment-chat-question').readOnly", timeout=60000
    )
    await page.wait_for_timeout(300)
    focused_id = await page.evaluate("() => document.activeElement && document.activeElement.id")
    await context.close()
    ok = focused_id == "assessment-chat-question" and not during["disabled"]
    return {"ok": ok, "during": during, "focused_after": focused_id, "log": log}


async def journey_contrast_zero(h) -> dict:
    """X-01: axe reports no color-contrast violation on the Phase 1 crawl pages, any role."""
    if not AXE_PATH.exists():
        return {"ok": False, "error": f"{AXE_PATH} is missing"}
    axe = AXE_PATH.read_text(encoding="utf-8")
    run, pi = h.ids["run"], h.ids["pi"]
    routes = [
        "/login", "/access-pending", "/settings", "/profile", "/profile/edit", "/agent",
        "/admin/users", f"/admin/users/{pi}", "/admin/jobs", "/admin/activity",
        f"/admin/activity/{run}", f"/admin/activity/{run}/llm-calls", "/admin/discussions",
        "/admin/agents", "/admin/assessments", "/admin/cohorts", "/admin/cohorts/topology",
        "/admin/access-requests", "/admin/simulation", "/manager/pis", f"/manager/pis/{pi}",
        "/manager/assessments", "/manager/discussions", "/manager/activity",
        f"/manager/activity/{run}", "/manager/slack-bots", "/manager/prompt-suggestions",
        *[f"/admin/assessments/{a}" for a in h.ids["assessments"]],
        *[f"/manager/assessments/{a}" for a in h.ids["assessments"]],
    ]
    violations: list[str] = []
    for role in _ROLES:
        # CSP bypassed: axe is injected as an inline script, which an enforced script-src
        # (Phase 2) would block. CSP is checked by its own journeys.
        context, page, log = await h.page(role, bypass_csp=True)
        for route in routes:
            response = await page.goto(h.base_url + route, wait_until="networkidle")
            if response is None or "text/html" not in response.headers.get("content-type", ""):
                continue
            # Report-only CSP in Phase 1 lets the injected script run; Phase 2's enforced
            # CSP needs a CSP-exempt injection (page.evaluate) instead.
            await page.add_script_tag(content=axe)
            found = await page.evaluate(
                """async () => {
                    const r = await axe.run(document, {runOnly: {type: 'rule', values: ['color-contrast']}});
                    return r.violations.flatMap(v => v.nodes.map(n =>
                        n.target.join(' ') + ' :: ' + ((n.any[0] && n.any[0].message) || '').slice(0, 160)));
                }"""
            )
            violations += [f"{role} {route}: {v}" for v in found]
        await context.close()
    return {"ok": not violations, "count": len(violations), "violations": violations[:200]}


async def journey_confirm_finalize(h) -> dict:
    """C-10: Finalize asks first and the server refuses a wrong short id; the right one,
    confirmed, enqueues the finalize."""
    run_id = h.ids.get("stopped_run")
    if not run_id:
        return {"ok": False, "error": "the harness seed has no stopped_run id"}
    context, page, log = await h.page("admin")
    dialogs = _Dialogs(page)
    posts = _posts(page, "/admin/simulation/finalize-run")
    url = f"{h.base_url}/admin/activity/{run_id}"
    button = page.locator('form[action="/admin/simulation/finalize-run"] button')

    await page.goto(url)
    dialogs.accept = True
    await page.fill("#confirm-run", "00000000")
    async with page.expect_navigation():
        await button.click()
    wrong_refused = f"Type the run's short id ({run_id[:8]})" in await _flash_text(page)

    dialogs.accept = False
    await page.fill("#confirm-run", run_id[:8])
    await button.click()
    await page.wait_for_timeout(500)
    dismissed_posted = len(posts) - 1

    dialogs.accept = True
    async with page.expect_navigation():
        await button.click()
    requested = "Finalize run requested." in await page.content()
    await context.close()
    ok = (wrong_refused and dismissed_posted == 0 and requested and len(dialogs.messages) == 3
          and all(m.startswith(f"Finalize run {run_id[:8]}: ") for m in dialogs.messages))
    return {"ok": ok, "dialogs": dialogs.messages, "wrong_refused": wrong_refused,
            "dismissed_posted": dismissed_posted, "requested": requested, "log": log}
```

  and add to `JOURNEYS` (keep the other parts' entries; Finalize last):
  ```python
  JOURNEYS += [
      journey_confirm_stop, journey_confirm_reset_template, journey_confirm_review_delete,
      journey_cohort_two_tab_save, journey_chat_focus_after_answer, journey_contrast_zero,
      journey_confirm_finalize,
  ]
  ```
  (if this part creates the file, write `JOURNEYS = [` … `]` with the same seven instead).

- [ ] **Step 2: Lint.** `.venv-test/bin/ruff check tests/e2e/ui_audit/journeys_phase1.py`
  Expected: no findings (`ruff` on `tests/` must stay at zero).

- [ ] **Step 3: Run the journeys** (needs the harness; outside `ci.sh`):
  `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 1`
  Expected: every Part 1C journey reports `"ok": true`. A `journey_contrast_zero` failure
  lists `role route: selector :: message`; fix the named element's class the 1C-12 way.

- [ ] **Step 4: Commit.**
```bash
git add tests/e2e/ui_audit/journeys_phase1.py
git commit -m "test(webui): Phase 1 journeys for confirms, two-tab topology, chat focus, contrast

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Self-review

**Spec coverage**

| Spec bullet | Task |
|---|---|
| §6.8 C-04: edit form offers `pending` only for a pending agent; server refuses `agent_status=pending` otherwise | 1C-6 |
| §6.8 C-05/D14: `ensure_activation_allowed(db, agent, new_role, new_status)` runs `activation_blockers` for the new role, refuses a second active hub, under a transaction lock registered in `advisory_locks.py`; called by approve, edit form, role route, manager activate | 1C-6 (approve and edit form through `activate_agent`; role route directly; manager activate and manager unmute through `activate_agent`) |
| §6.8 C-07: topology posts the cells it rendered checked; save deletes only rendered-checked-now-unchecked, adds only now-checked | 1C-7 |
| §6.8 C-10/FN-01/FN-06/B-10: `data-confirm` on Finalize, the announcing Stop (live run's `headlines_owed`/`provisional`), Reset, review delete; Finalize `confirm_run` = first 8 chars, server-checked | 1C-8 (dialogs), 1C-16 (browser accept/dismiss) |
| §6.8 D-01/D-02/D-08: email validated before any write; tenure only when the year differs; refuse during a pending/processing `generate_profile`; insert under `SET LOCAL lock_timeout = '5s'`, lock error → same message; all profile callers | 1C-9 |
| §6.8 D-16: one hidden field per tag; `form.getlist`; comma split removed (profile edit, onboarding review, public profile, manager PI form) | 1C-10 |
| §6.8 B-06: `readOnly` + `aria-busy` while busy; refocus when finished or abandoned | 1C-11 (+ 1C-16 journey) |
| §6.9 M-01: one handler for `HTTPException` and `RequestValidationError`; 3xx passes; JSON for `/assessment-chat/`, `/api/`, `Accept: application/json`; else `templates/error.html` without the account area, title per status, Back and Home, same status | 1C-2 |
| §6.9 FN-09/A-14: `flash(request, text, kind)` in the session; context processor in `make_templates()` pops into `base.html`'s flash block; `delegate_error`, `slack_error`, `error=` text messages moved | 1C-1 (mechanism), 1C-3 (cohorts), 1C-4 (simulation, Finalize), 1C-5 (Slack, delegates) |
| §6.10 X-01: gray-300/400 → gray-600, white on green-600 → green-700, in templates, `static/js` and `src/` class strings; axe `color-contrast` zero | 1C-12 (+ 1C-16 `journey_contrast_zero`) |
| §6.10 X-02: every control named; `aria-label` on matrix checkboxes (agent and cohort), tag-remove buttons (the tag), impersonate input | 1C-13 (table), 1C-10 (tag buttons and inputs), 1C-8 (Finalize), 1C-11 (chat) |
| §6.10 X-05: `<a href>` in each clickable row's first cell; `ui.js` keeps row clicks via `data-row-href` | 1C-14 |
| §6.10 gate: DB-backed, every GET page as admin, manager, reviewer, PI with factory data; fails on unnamed control, gray-300/400, `<tr onclick>`, inline `on…=` | 1C-15 (adds the anonymous role too) |
| §9 Phase 1 harness: confirm dialogs accepted and dismissed; cohort two-tab save; chat focus after an answer; zero `color-contrast` | 1C-16 |

**Spec points found false or not achievable as written** (evidence in the owning task):
1. §6.10 X-01: the two named replacements cannot make the axe run report zero
   `color-contrast` violations. The audit's axe samples also fail on `text-green-600`
   (`#16a34a`, 3.3:1), `text-amber-600` (3.2:1), white on `bg-amber-600`, white on
   `bg-yellow-600` (2.9:1) and `text-gray-500` on `bg-gray-100` (4.4:1). 1C-12 extends the map.
   `text-gray-300` occurs nowhere; `static/js/` and `src/` hold no gray-300/400 literal, but
   `src/services/bands.py::band_class` builds `text-gray-400` from the shades
   `_assessments_body.html` passes, fixed at the call.
2. §6.8 FN-01: the dialog's N is all owed headlines of the live run, but the engine posts at
   most `HEADLINES_MAX_AT_SHUTDOWN = 25` during `stop()` (`src/agent/engine/constants.py:157`).
   Above 25 the text overstates what this Stop posts. The spec wording is kept verbatim.
3. §6.9 "moves `slack_error` … onto it": the Connect Slack producers and the dashboard
   consumer of `slack_error` are deleted by §6.4, not moved; everything else is moved here.

**Assumptions**
- The error page shows the exception's own `detail` when it is a handler-written sentence
  (it was in the JSON body before); spec §6.9 names only "a plain title per status".
- "Adds only now-checked cells" is read as cells ticked now that the form did not render
  ticked; a box rendered ticked and still ticked, whose membership another tab removed, is
  not re-added (pinned by `test_a_cell_shown_checked_and_still_checked_is_left_alone`).
- `ensure_activation_allowed` takes a keyword `override=False` beyond the contract signature;
  the contract call `ensure_activation_allowed(db, agent, new_role=..., new_status=...)` is unchanged.
- The manager PI form's five comma text inputs become tag widgets (spec lists that form under D-16).

**Cross-part requirements (for the parent to reconcile)**
- Part 1A: every inline `on…=` gone and `<tr onclick>` rows carrying `data-row-href`
  (consumed by 1C-14, 1C-15); `static/js/ui.js`, `static/css/input.css`, `tailwind.config.js`,
  `scripts/build_css.sh` exist (1C-10, 1C-12, 1C-14). Whatever toggle attribute 1A gave the
  `_discussions_threads.html` row is replaced in 1C-14.
- Part 1B: `tests.integration.test_manager_access.auth_headers` keeps producing a valid session
  cookie after the cookie rename and epoch (used throughout); its email-block edits in
  `apply_profile_edits` move into `_apply_email` (1C-9).
- §6.4 removal tasks (owner outside this part): graph routes gone before 1C-15; Connect Slack
  gone before 1C-5.
- Phase 0 harness: seed ids `run`, `assessments`, `pi`, `stopped_run`; `axe.min.js` beside the
  journeys; `tests/e2e/ui_audit/journeys_phase1.py` is shared by Parts 1A, 1B and 1C.
- Incidental: C-03 (Phase 2, "`slack_error` on `/admin/agents` is never rendered") is closed
  by 1C-5, since the admin callback's error now renders through the base flash block.

**Placeholder scan:** searched for TBD, TODO, FIXME, "similar to", "same as Task", "handle
edge", "add validation": none. Every step has its code or an exact before/after.

**Interface consistency:** `flash(request, text, kind)`, `pop_flashes(request)`,
`flash_context(request)` (registered in `make_templates`), `install_error_handlers(app)`,
`templates/error.html`, `ensure_activation_allowed(db, agent, *, new_role, new_status)` and
`tests/integration/test_rendered_page_gate.py` match the brief's Part 1C contract. Names
defined here and used later: `session_flashes`/`session_cookie_header` (1C-1 → 1C-3…1C-8),
`_refuse_to` (1C-4 → 1C-8), `HUB_ROSTER_LOCK_KEY` (1C-6), `PROFILE_GENERATING` (1C-9 → 1C-10's
callers), `list_fields_from_form`/`TAG_FIELDS_MARKER` (1C-10), `refocusInput` (1C-11),
`page_problems`/`PAGES`/`SKIPPED` (1C-15 → Phase 2), the seven `journey_*` (1C-16).

---

### Task 1-Z: Phase gate, records, merge, deploy

**Files:**
- Modify: `docs/audits/open-findings.md` (the rows listed in Step 5)
- Modify: `CLAUDE.md` ("Deploying": the head migration line; owner approval required)

- [ ] **Step 1: Rebuild the CSS and commit it if it changed (assembly audit PX-15).**
  `bash scripts/build_css.sh` then `git status --porcelain static/css/app.css`; if it lists the
  file, `git add static/css/app.css && git commit -m "build(webui-1): rebuild app.css"` with the
  attribution line.
- [ ] **Step 2: Full gate.** Run `./scripts/ci.sh`. Expected: exit 0 (the alembic round trip
  includes `0057`; the CSS drift check passes).
- [ ] **Step 3: Harness gate.** Run `.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 1`
  and `.venv-test/bin/python -m tests.e2e.ui_audit.run journeys --phase 0`. Expected: exit 0 for
  both. Review the CSP reports collected by `journey_csp_report_only` (Task 1A-8), the CSS parity
  pairs (record each reviewed difference in `tests/e2e/ui_audit/css_parity_reviewed.json`, Task
  1A-9) and the cross-part review focus above.
- [ ] **Step 4: Adversarial audit.** Dispatch `engineering:auditor` (model `opus`) with
  `git diff blackbird...webui/phase-1`, spec §6 and §12, this plan and the harness report; ask it
  to refute each fixed finding and look for regressions across parts. Fix confirmed defects first.
- [ ] **Step 5: Register (assembly audit PX-13).** Set `status` to `fixed`, with the backticked
  commit of the fixing task appended to `evidence`, on exactly these rows:
  `2026-10-01/` + A-02b, A-03, A-05, A-06, A-08, A-09, A-11, A-12, A-13, B-06, B-10, C-03, C-04,
  C-05, C-07, C-10, D-01, D-02, D-03, D-06, D-08, FN-01, FN-06, FN-09, M-01, M-04, M-05, X-01,
  X-02, X-05; and the pre-existing `2026-09-30/A-invite-email-unverified` (A-04) and
  `2026-10-01/A-profile-form-comma-split` (D-16). Keep `2026-10-01/A-14` and `2026-10-01/M-03`
  `open`, appending "Phase 1 part fixed in `<hash>`; Phase 2 completes it." Run
  `.venv-test/bin/python -m pytest tests/unit/test_open_findings_register.py -v` (PASS) and commit
  `docs(webui-1): register rows for Phase 1 fixes` with the attribution line.
- [ ] **Step 6: CLAUDE.md head line (owner approval).** Propose to the owner the edit of the
  "Deploying" bullet that names `0056` as head so it names `0057` (one line); commit only if the
  owner approves.
- [ ] **Step 7: Merge.** `git switch blackbird && git merge --no-ff webui/phase-1` (message ends
  with the attribution line).
- [ ] **Step 8: Deploy (operator).** Follow the `0057` box that Task 1B-1 Step 9 adds to
  `docs/operations/migration-deploy-notes.md`, in this order:
  ```bash
  git tag rollback-pre-webui-1 HEAD^1            # the pre-merge blackbird commit (spec §10)
  git rev-parse rollback-pre-webui-1             # must equal blackbird before the merge
  git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini
  # must print nothing; then the box's image-tag loop (copi-blackbird-<svc>:rollback-pre-webui-1),
  # the builds, the rehearsal without --apply, the apply, and $DC up -d blackbird-app worker
  curl -sI 'https://blackbird.copi.science/login?next=/profile' | grep -i '^set-cookie: __Host-copi-session=.*secure'
  ```
  The `next` makes the login page write the session, so the cookie name and `Secure` flag are
  visible (a bare `/login` sets no cookie; assembly audit PX-17). Recreate the agent
  (`$DC up -d agent`) **only** when `/admin/simulation` shows no live run. Tell the owner: every
  user has been signed out once, and the agent image changed (A-02b).
