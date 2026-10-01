# Web UI audit — 2026-10-01

> **Do not push.** This record and `raw/` describe exploitable weaknesses of the live
> site, with reproduction steps. Owner decision (remediation spec §2, Q14): commit on
> `blackbird` locally; scrub before any push, and only after Phase 0 is deployed.

Scope: every page, route, template and script that makes up the web UI of this repo at
`af1a6c0` (`src/main.py`, `src/dependencies.py`, `src/routers/**`, `templates/**`,
`static/js/**` and the services they call), plus the production response headers and
the deployed library versions.

Remediation: `docs/specs/2026-10-01-web-ui-remediation-design.md`.

## Method

1. **Code review, four reviewers in parallel**, each covering one surface from every
   perspective (security, correctness, accessibility, layout, performance, UX):
   auth/public/profile plus a cross-cutting authorization and output-encoding sweep;
   assessments, reviews and the assessment chat; admin operations; manager console and
   PI agent pages.
2. **Browser run** against a local copy of the current code: a throwaway Postgres
   migrated to head, the app on `127.0.0.1` with a fake model (no LLM spend), seeded
   with adversarial names, headlines, summaries and job errors (`raw/harness/`).
   - 64 GET routes x 8 roles (anon, admin, manager, reviewer, PI, second PI, delegate,
     pending) at 1280 px, plus 4 roles at 375 px: 744 page loads.
   - Recorded per load: status and final URL, console and page errors, failed
     subrequests, an XSS canary (`window.__xss`), horizontal overflow, and axe-core
     4.13.0 (WCAG 2.0/2.1/2.2 A and AA, best-practice) at 1280 px.
   - Scripted journeys: chat drawer (send, sanitizer, Esc, re-open, session expiry),
     simulation auto-refresh, keyboard tab order, timeline clamp, DOMPurify defaults,
     delete-confirm injection, injected transcript form, HTML export opened from disk.
   - Summaries: `raw/crawl-summary.json`, `raw/journeys.json`.
3. **Online and production checks (read-only):** OSV for every deployed web library
   and the CDN libraries; Tailwind's Play CDN documentation; production `HEAD`/`GET`
   response headers; the shared nginx configuration on the host; library versions in
   the running web container; counts (no content) from the production database.
4. **Adversarial audit:** two independent auditors tried to refute every finding
   (security; functional/accessibility), recalibrated severity and added new findings.
   The pre-audit register they worked from is `raw/pre-audit-register.md`.

Deployed web container (2026-10-01): fastapi 0.142.2, starlette 1.7.0, jinja2 3.1.6,
markupsafe 3.0.3, python-multipart 0.0.32, sqlalchemy 2.1.1, uvicorn 0.54.0 — OSV lists
no advisory for any of them. CDN: marked 12.0.2 (none), DOMPurify 3.1.6 (20, none
applicable to how it is used here), d3 7.9.0 (none).

## Findings

Severity is the final, post-audit severity. Phase is the remediation phase (spec §4).
`BROWSER` = reproduced in headless Chromium; `PROD` = observed on production.

### High

| ID | Finding | Evidence | Phase |
|---|---|---|---|
| A-01 | The Delete User confirm interpolates the user's name into an inline JS string (`templates/admin/user_detail.html:178`). Entity-decoded quotes break out: a crafted name runs script in the admin's session when Delete is clicked, even if the dialog is dismissed; a name with an apostrophe makes the handler fail to compile, so the user is deleted with no confirmation. Any ORCID account holder controls the name (`src/routers/auth.py`, `src/services/profile_edit.py`). The two cohort confirms use the same pattern but cohort names are restricted to `[a-z0-9-]`. | BROWSER: script ran; an apostrophe name was deleted without a dialog. One production user name contains an apostrophe. | 0 |
| A-02 | The page markdown renderer (`static/js/markdown.js`) passes raw HTML through marked and sanitizes with DOMPurify's default config, which keeps `form`, `input`, `button`, `img`, `style` and `id`. Sinks: assessment detail and list, discussions, discussions export, prompt-suggestion detail, graph modal. A same-origin form passes the Origin guard. | BROWSER: an injected transcript form became a full-viewport button after the timeline opened; one click anywhere promoted a PI to admin. | 0 |
| A-02b | The engine poller stores any Slack bot message in a polled channel and attributes it by the `username` it claims (`src/agent/engine/slack_io.py:181-209`); rows are persisted and rendered in transcripts. | Code confirmed for storage and rendering; Slack's handling of `username` overrides and entity escaping unverified. Production: all 2,483 stored messages resolve to known agents. | 1 |
| C-01 | The simulation panel's 30 s refresh replaces `#sim-body`, Start form included, unless focus is in an input (`templates/admin/simulation.html:741-750`). Edited Start values and the announce template are reset. | BROWSER: 60/20 reset to 0/0 within 33 s. | 0 |
| B-01 | Timeline "Show full message" toggles are measured only at load, inside a closed `<details>` (`templates/admin/_assessment_detail_body.html:1431-1448`); no `toggle` listener. | BROWSER: not reproduced in Chromium (closed content is measurable there). Firefox and Safari unverified. | 0 |

### Medium

| ID | Finding | Phase |
|---|---|---|
| A-03 | Unversioned Tailwind Play CDN without SRI on every page, admin included (`templates/base.html:9`); Tailwind documents it as development-only. | 1 |
| A-04 | Invite acceptance compares the invited address with `users.email`, which `/profile/save` sets unverified. Tracked as `2026-09-30/A-invite-email-unverified` (raised to medium). | 1 |
| C-04 | The admin agent edit form offers `pending` for an active agent, re-opening the slug rename and auto-activation (regresses `2026-09-29/RA-03`). | 1 |
| C-07 | The cohort topology save diffs against the current DB, deleting memberships added after the page rendered (`src/routers/admin/cohorts.py:311-353`). | 1 |
| C-08 | The refresh has no `ok`/redirect check or catch; it stops silently on a 302, 500, or the 405 of the template-error URL. | 0 |
| C-09 | The refresh destroys a focused link, button or summary every 30 s. | 0 |
| C-10 | Finalize run (irreversible Slack posts) has no confirmation. | 1 |
| FN-01 | The announcing Stop sits beside "Stop — hold open interviews" with no confirmation. | 1 |
| B-06 | Chat send disables the focused textarea; focus drops to the body and is never restored. BROWSER. | 1 |
| D-01 | The manager PI form re-posts the tenure year and relabels a machine-derived source as `manual`; the re-derivation script then skips it. | 1 |
| D-08 | A profile save while that user's profile job holds the uncommitted profile row waits on the unique key, then fails with a 500. | 1 |
| D-16 | Tag fields split on commas on every save. Tracked as `2026-10-01/A-profile-form-comma-split`. | 1 |
| X-01 | Colour contrast: 965 nodes on 40 pages fail (gray-400 text, white on green-600). | 1 |
| X-02 | Form controls without an accessible name (select-name 42 nodes, label 38 nodes, label-title-only). | 1 |
| X-05 | `<tr onclick>` rows cannot be reached by keyboard (`admin/activity.html`, `manager/activity.html`, `admin/users.html`, `manager/pis.html`, `admin/_discussions_threads.html`). BROWSER. | 1 |

### Low and info

| ID | Sev | Finding | Phase |
|---|---|---|---|
| A-05 | low | No `__Host-` cookie prefix; impersonation held in an unsigned cookie; sibling hosts can toss cookies. | 1 |
| A-06 | info | The app sets no frame protection; only the shared nginx does (PROD). | 1 |
| A-07 | low | Deny and delete can remove the last admin; deny allows self-lockout. Tracked as `2026-09-30/W-admin-cross-delete`, `2026-09-30/W-admin-access-denial`. | 2 |
| A-08 | info | Connect Slack links a Slack id by unverified email (feature removed). | 1 |
| A-09 | low | Invite routes read the raw session and skip the access-status check. | 1 |
| A-10 | low | Writes made while impersonating carry no impersonation note (profile, onboarding, access, simulation, cohorts). | 2 |
| A-11 | low | Login does not clear the session; logout is client-only on a 30-day stateless cookie. | 1 |
| A-12 | low | Latent PostHog snippet interpolates name and email into a JS string (key empty in production). | 1 |
| A-13 | low | Unauthenticated graph pages publish proposal summaries and internal ids (pages removed). | 1 |
| A-14 | low | Message text carried in query strings can be spoofed and is mangled by unquoted URL building. | 1, 2 |
| A-15 | low | Name, institution, department over 255 characters give a 500. | 2 |
| A-16 | info | Impersonating an unknown ORCID creates a pending user and a profile job. | 2 |
| A-17 | info | Grant and industry veto routes do not check the target is a PI. | 2 |
| A-19 | info | GET routes that write (onboarding enqueue, invite expiry, chat stale sweep, Slack callback): idempotent and own-data only. Accepted, no change. | — |
| SN-01 | low | Adding an ORCID to the allowlist silently reverses an earlier deny (`src/routers/admin/access.py:147`). | 2 |
| SN-02 | low | Delegate invite form: no cap on addresses per request or per day. | 2 |
| B-03 | low | Quick score redirects to an anchor that was filtered out of the tab. | 2 |
| B-04 | low | Double submit stores a review twice. | 2 |
| B-07 | info | Detail page loads up to 200 `messages_json` blobs (admin only). | 2 |
| B-08 | info | Chat polling runs the stale sweep and a full detail build. | 2 |
| B-09 | info | "Read-only view." copy under working write forms. | 2 |
| B-10 | low | Review delete is a hard delete with no confirmation. | 1 |
| B-11 | low | Comments silently cut at 10,000 characters; NUL handling unverified. | 2 |
| B-13 | info | An unknown `run_id` shows an empty list with the wrong option selected. | 2 |
| B-16, B-17, B-20 | low | Drawer modality not re-evaluated on resize; poll re-render scrolls and drops focus; "Show in page" target under the drawer. | 2 |
| C-02 | low | The HTML discussions export is blank when opened from disk (legacy rows only). BROWSER. | 2 |
| C-03 | low | `slack_error` on `/admin/agents` is never rendered. | 2 |
| C-05 | low | Role change skips the activation gate; nothing limits active hubs. | 1 |
| C-06 | low | Link-user accepts invalid, missing or already-linked users (500) and any role. | 2 |
| C-14 | low | Each live-run refresh recomputes about 16 aggregates and up to 20,000 call-stats rows. | 2 |
| C-15 | low | Discussions and the PI list load everything and slice in Python. | 2 |
| C-16 | low | `llm_calls` inlines full prompts and responses for 50 rows. | 2 |
| C-18 | low | Jobs filters miss two enum values; an unknown value returns 500. BROWSER. | 2 |
| C-19, C-20 | info | Unbounded page parameter; negative `max_runtime` accepted by a crafted POST. | 2 |
| C-21, C-22, C-23 | info | Latency average uses `latency_ms`; "calls" vs "rows" labels; hub pick has no order. | 2 |
| C-24 | low | Start-resume and finalize check each other without a lock (unverified outcome). | 2 |
| C-27 | low | `msg`/`error` flashes persist through refreshes. | 0 |
| C-29 | low | Reject suspends an agent in any status. | 2 |
| D-02 | low | Manager form commits the tenure upsert even when the email check fails. | 1 |
| D-03, D-04, D-05 | low | Connect Slack: errors never shown, banner never clears, a Slack lookup per view (feature removed). | 1 |
| D-06 | low | Reviewer and manager accounts can accept delegate invites. | 1 |
| D-07 | low | Activate can overwrite a concurrent suspend. | 2 |
| D-09, D-10 | low | `saved`, `slack_ok` flags never rendered. | 2 |
| D-11 | low | Two different "has token" predicates. | 2 |
| D-12 | low | Prompt-suggestion detail shows status buttons under impersonation, which 403. | 2 |
| D-14 | low | Thread fetch injects the login page on session expiry; `aria-expanded` stale on error. | 2 |
| D-15 | info | Public-profile save creates an empty profile when none exists. | 2 |
| D-17 | low | Concurrent Add-PI maps the ORCID unique violation to the wrong message. | 2 |
| D-18 | info | ORCID input rejects a lowercase `x` or an `orcid.org/` URL. | 2 |
| D-19 | low | Grant veto replaces the whole title list; re-veto overwrites `vetoed_at`. | 2 |
| D-20, D-21, D-26, D-27, D-28 | low/info | Listing loop for non-viewable agents; ignored email send result; no pager past 50 roots; run pager hidden past the end; stale copy. | 2 |
| M-01 | low | 403/404/422 reached by navigation render raw JSON. BROWSER. | 1 |
| M-02 | low | `HEAD` returns 405 on GET pages. PROD. | 2 |
| M-03 | low | Script CSP is report-only in the shared nginx; its `report-uri` reaches this app, which has no route (PROD); `/ingest` 404s. | 1 |
| M-04 | low | DOMPurify 3.1.6 has 20 advisories; none applies to the configs and the single `afterSanitizeAttributes` hook used here. | 1 |
| M-05 | low | Graph-page CSP allows any jsDelivr script and `'unsafe-eval'`, no `form-action` (pages removed). | 1 |
| M-08 | low | Managers (and reviewers on `/agent`) can open PI-only pages whose POSTs 403. | 2 |
| FN-02 | low | Onboarding under impersonation prefills the admin's email; saving fails. | 2 |
| FN-03 | low | Tables inside `overflow-hidden` wrappers are clipped at narrow widths. | 2 |
| FN-04 | low | Dropdowns that submit on `change`. | 2 |
| FN-05 | low | The run selector has no `action`, so after a template error it submits to a POST-only URL. | 0 |
| FN-06 | low | "Reset to file default" deletes a custom template with no confirmation. | 1 |
| FN-07 | low | Timestamps mix browser-local and UTC without a zone label. | 2 |
| FN-08 | info | Invite pages get no `current_user`, so a signed-in user sees "Sign in". | 2 |
| FN-09 | info | `base.html`'s flash block is never set by any handler. | 1 |
| X-03 | low | Links in text distinguished by colour only. | 2 |
| X-04 | low | Sub-navs outside landmarks; duplicate unnamed `<nav>`; no `h1` on login; heading order; empty table header. | 2 |
| X-06 | low | Colour-only signals (gantt "announced"); `aria-label` on generic spans; ungrouped dimension selects. | 2 |
| R-01 | low | At 375 px, filter rows and tables overflow (assessments +174 px, discussions +259 px, simulation +223 px). BROWSER. | 2 |
| R-02 | low | Long unbroken strings overflow profile, detail, tag pills and chat answers. BROWSER. | 2 |

Merged into the rows above: B-02 (A-02); B-05, C-26 (X-01); C-11, D-22, A-18 (X-05, X-02);
C-12, D-23 (X-02); C-13, B-12, B-21 (X-06); C-17, D-24, B-14 (R-01); B-15 (R-02);
C-25 (A-10); C-28, B-19 (FN-07); D-13 (A-14); D-25 (C-15).

### Refuted

| ID | Reason |
|---|---|
| C-30 | Jinja's `getitem` returns Undefined on IndexError; the label renders blank, no crash. |
| B-18 | `lab_filter` can only be an existing slug (`[a-z0-9_-]`); URL-encoding is still added as hygiene in Phase 2. |
| A-01 (cohort half) | Cohort names are restricted to `^[a-z0-9-]{1,48}$`; converted anyway in Phase 0. |
| DOC-01 | `CLAUDE.md` says the uncommitted compose "pins the `blackbird-app` and `agent` container names" — the service names; the pinned names are `copi-blackbird-app-1` and `copi-blackbird-agent-1`, which match production. Not drift. |

### Verified clean

No XSS canary fired in 744 loads; no uncaught page errors; the role x route status
matrix matched the declared gates apart from M-08; the chat sanitizer stripped images,
`javascript:` links and raw HTML from a streamed adversarial answer; no open redirect
(`is_safe_next_url`); every form field name matches its handler, and every absolute
`href`/`action` resolves to a route; the deployed server libraries have no advisory.

### Unverified

Firefox and Safari (B-01); Slack's handling of `username` overrides, broadcast replies
and HTML escaping (A-02b); whether a model emits injected HTML (A-02's PI and reviewer
actors); payload sizes at production volume (C-14, C-16, B-07); PostgreSQL NUL handling
(B-11).
