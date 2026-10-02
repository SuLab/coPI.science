# Web UI remediation — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo, plan execution goes through `/engineering:plan-execution`.

**Goal:** Close the remaining low and info findings, then enforce the Content-Security-Policy.

**Architecture:** Two parts. **Part 2A** tightens authorization and validation, data-integrity races and the feedback flow (flash replaces the last query-string flags). **Part 2B** handles performance, correctness and copy, drawer JS, the low accessibility items and narrow screens, and ends by switching the CSP from report-only to enforced.

**Tech Stack:** as Phase 1, plus `markdown-it-py` declared directly (spec amendment A12).

**Spec:** `docs/specs/2026-10-01-web-ui-remediation-design.md` §7, with amendments A1–A16 in §12. Prerequisite: Phase 1 merged and deployed (`docs/plans/2026-10-01-web-ui-remediation-phase-1.md`).

## Global Constraints

- Branch `webui/phase-2` cut from `blackbird` after Phase 1 merged. Never push `origin` (D15).
- Deploy unit: web, worker and agent images (Task 2B-7 edits `src/services/assessment_detail.py`, which the engine imports); no migration; the agent is recreated only with no live run.
- Every task is written against the post-Phase-1 tree: flash instead of query flags, inline handlers in `static/js/ui.js`, Connect Slack and the graph pages gone, sessions and email verification as Phase 1 left them, compiled CSS with its drift check.
- Any task that adds or changes a Tailwind class reruns `scripts/build_css.sh` and commits `static/css/app.css` in the same commit.
- `form-action` in the enforced policy is `'self' https://slack.com` (amendment A4).
- Never touch `docker-compose.prod.yml`; no change under `prompts/`; no simulation start.

## Execution order and shared files

Run the parts in order **2A → 2B**, with Task 2B-15 (CSP enforcement) last of all, and **every task sequentially in numbered order — no
parallel execution**. The table below lists every file named in more than one task's **Files**
block (computed from this document, 2026-10-01). Its rows touch most templates, and the
parts were written separately against the post-Phase-1 tree, so sequential order is the only
safe one (assembly audit PX-09). A later task's "before" text is written against the earlier
task's result.

| File | Tasks |
|---|---|
| `src/routers/manager.py` | 2A-6, 2A-9, 2A-17, 2A-19, 2A-20, 2A-21, 2B-5 |
| `src/routers/admin/agents.py` | 2A-5, 2A-14, 2A-15, 2A-21, 2A-22b |
| `src/routers/agent_page.py` | 2A-10, 2A-18, 2A-20, 2A-28, 2A-30 |
| `templates/admin/simulation.html` | 2A-22, 2B-6, 2B-11, 2B-12, 2B-14 |
| `templates/manager/pi_detail.html` | 2A-8, 2A-17, 2A-21, 2B-2, 2B-14 |
| `src/routers/admin/simulation.py` | 2A-7, 2A-16, 2A-22, 2B-9 |
| `src/services/directory.py` | 2A-24, 2A-29, 2B-4, 2B-5 |
| `templates/admin/_assessment_detail_body.html` | 2A-11, 2B-2, 2B-12, 2B-13 |
| `templates/admin/_assessments_body.html` | 2A-11, 2A-23, 2B-2, 2B-14 |
| `templates/admin/agent_detail.html` | 2A-21, 2A-22b, 2B-2, 2B-13 |
| `templates/admin/assessments.html` | 2B-2, 2B-9, 2B-11, 2B-14 |
| `templates/admin/jobs.html` | 2B-2, 2B-9, 2B-11, 2B-14 |
| `templates/admin/users.html` | 2B-2, 2B-5, 2B-11, 2B-14 |
| `templates/manager/assessments.html` | 2B-2, 2B-9, 2B-11, 2B-14 |
| `templates/manager/pis.html` | 2B-2, 2B-5, 2B-11, 2B-14 |
| `tests/e2e/ui_audit/journeys_phase2_2b.py` | 2B-10, 2B-13, 2B-14, 2B-15 |
| `src/routers/admin/access.py` | 2A-2, 2A-3, 2A-4 |
| `src/routers/profile.py` | 2A-6, 2A-20, 2A-30 |
| `static/js/ui.js` | 2A-13, 2B-6, 2B-11 |
| `templates/admin/_discussions_threads.html` | 2B-2, 2B-4, 2B-14 |
| `templates/admin/_run_detail_body.html` | 2A-29, 2B-2, 2B-14 |
| `templates/admin/access_requests.html` | 2B-2, 2B-13, 2B-14 |
| `templates/admin/cohort_detail.html` | 2A-22b, 2B-2, 2B-14 |
| `templates/admin/cohorts.html` | 2A-22b, 2B-2, 2B-14 |
| `templates/admin/discussions.html` | 2B-2, 2B-11, 2B-14 |
| `templates/manager/discussions.html` | 2B-2, 2B-11, 2B-14 |
| `templates/manager/prompt_suggestion_detail.html` | 2A-25, 2B-2, 2B-14 |
| `templates/manager/prompt_suggestions.html` | 2B-2, 2B-11, 2B-14 |
| `tests/integration/test_agent_listing_and_pagers.py` | 2A-27, 2A-28, 2A-29 |
| `tests/integration/test_impersonated_writes.py` | 2A-5, 2A-6, 2A-7 |
| `tests/integration/test_query_flags_to_flash.py` | 2A-20, 2A-21, 2A-22 |
| `tests/integration/test_rendered_page_gate.py` | 2B-6, 2B-13, 2B-14 |
| `tests/integration/test_reviews_router.py` | 2A-11, 2A-12, 2A-23 |
| `src/dependencies.py` | 2A-5, 2A-30 |
| `src/routers/admin/discussions.py` | 2A-33, 2B-4 |
| `src/routers/admin/runs.py` | 2A-22, 2B-6 |
| `src/routers/admin/users.py` | 2A-2, 2B-5 |
| `src/routers/onboarding.py` | 2A-6, 2A-31 |
| `src/routers/reviews.py` | 2A-12, 2A-23 |
| `src/services/assessment_reviews.py` | 2A-11, 2A-12 |
| `static/css/app.css` | 2B-12, 2B-14 |
| `static/css/input.css` | 2B-12, 2B-14 |
| `static/js/assessment_chat.js` | 2B-8, 2B-10 |
| `templates/admin/activity.html` | 2B-2, 2B-14 |
| `templates/admin/activity_detail.html` | 2A-22, 2B-2 |
| `templates/admin/agents.html` | 2B-2, 2B-14 |
| `templates/admin/llm_calls.html` | 2B-2, 2B-6 |
| `templates/admin/user_detail.html` | 2B-2, 2B-14 |
| `templates/agent/conversations.html` | 2A-26, 2A-28 |
| `templates/agent/request.html` | 2B-2, 2B-9 |
| `templates/base.html` | 2B-2, 2B-13 |
| `templates/manager/activity.html` | 2B-2, 2B-14 |
| `templates/manager/slack_bots.html` | 2A-21, 2B-14 |
| `templates/onboarding/profile_review.html` | 2A-31, 2B-2 |
| `tests/e2e/test_browser_flows.py` | 2A-21, 2A-22b |
| `tests/e2e/ui_audit/journeys_phase2.py` | 2A-34, 2B-10 |
| `tests/integration/test_admin_agent_link_reject.py` | 2A-14, 2A-15 |
| `tests/integration/test_manager_pi_writes.py` | 2A-19, 2A-20 |
| `tests/integration/test_manager_slack_provisioning.py` | 2A-17, 2A-21 |
| `tests/unit/test_chat_drawer_js.py` | 2B-8, 2B-10 |

## Review Focus (cross-part)

1. **Enforced CSP against every journey:** after 2B-15, the Phase 0, Phase 1 and Phase 2 journeys all run with the policy enforced (Task 2-Z, Step 2).
2. **Double-submit guard and confirm:** a confirm-cancelled submit must not leave the form's buttons disabled (2A-13 with Phase 0's `confirm.js`).
3. **HEAD on redirects and error pages:** `HEAD` on a gated page as an anonymous user returns the same 302 as `GET` (2B-1).
4. **Caches and impersonation:** the 25 s stats cache (2B-3) is keyed by run only and holds no user data.
5. **Pagers and the gate:** every new pager link is credited by `tests/unit/test_reachability.py`.

---

## Part 2A: Auth and validation, data integrity, feedback and flow

**Scope:** spec §7 bullets "Auth and validation" (A-07, SN-01, A-10, A-15, A-16, A-17, SN-02,
B-04, B-11, D-18), "Data integrity" (C-06, C-24, C-29, D-07, D-15, D-17, D-19, D-11) and
"Feedback and flow" (C-03, D-09, D-10, A-14 remaining flags; B-03, B-13, D-12, D-14, D-20,
D-21, D-26, D-27, M-08, FN-02, FN-08, C-02). Phase 2 starts from the tree after Phase 1 merged.

### Global constraints (this part)

Copied from spec §7 (verbatim values):

- A-07: "`ensure_admin_remains` in deny and delete; deny refuses self".
- SN-01: "allowlist add promotes only `pending`".
- A-10: "impersonation note or `mechanism="web_impersonated"` on every write made while impersonating".
- A-15: "length checks with a form error". A-16: "404 for an unknown ORCID". A-17: "PI-target check".
- SN-02: "at most 10 addresses per submission and 25 invitations per agent per 24 h".
- B-04: "`ui.js` disables a submitted form's buttons; the server rejects an identical review by the
  same reviewer on the same assessment within 60 s".
- B-11: "`maxlength`; 400 on overlength or NUL". D-18: "strip an `orcid.org/` prefix, uppercase a trailing `x`".
- C-06: "valid UUID, existing unlinked user, role that may own a lab". C-24: "`SELECT … FOR UPDATE`
  on the run row in start and finalize". C-29: "reject only `pending`". D-07: "`UPDATE … WHERE
  status='pending'` with a rowcount check". D-15: "refuse an empty save when no profile exists".
  D-17: "map the `users.orcid` violation to "already exists"". D-19: "remove only the vetoed title;
  idempotent veto". D-11: "one token predicate, `token_for_agent_row`".
- Feedback: "move `saved`, `slack_ok`, `msg` flags onto flash (C-03, D-09, D-10); B-03 (flash
  "Moved to Reviewed" and anchor the next card); B-13 (unknown `run_id` falls back to the current
  run); D-12; D-14 (reject redirected or non-HTML fragment responses; set `aria-expanded` on
  error); D-20; D-21; D-26 (conversations pager); D-27 (pager whenever `message_total > 0`, page
  clamped); M-08 (managers and reviewers redirected from PI-only pages); FN-02 (onboarding shows
  the effective user's email); FN-08 (invite pages get the normal context); C-02 (export renders
  summaries server-side)".
- No migration in this part. No change under `prompts/`. No `Depends(...)` in a new argument default.

Post-Phase-1 assumptions every task below relies on (from spec §6 and the brief's contract):

- `src/web/flash.py` exists with `flash(request, text, kind="info")`; `base.html` renders popped
  flashes (autoescaped) on the next page. Phase 1 already moved `delegate_error`, `slack_error`
  (where §6.9 reached) and `error=` text messages onto it; `?error=<code>` codes on the profile
  forms (`invalid_email`, `email_taken`, `profile_changed`, …) are codes, not text, and remain.
- Impersonation lives in `session["impersonate_user_id"]`; `get_current_user` still tags the
  returned user with `_is_impersonated` / `_real_admin`. `tests/integration/test_manager_access.py::auth_headers(user_id)`
  still mints a valid session cookie for the post-Phase-1 cookie name and epoch rule.
- `ensure_activation_allowed(db, agent, *, new_role, new_status) -> list[str]` exists in
  `src/services/agent_activation.py` and is imported into `src/routers/manager.py` by name.
- Finalize requires `confirm_run` = first 8 characters of the run id.
- Connect Slack (`delegate_connect_slack`, `_resolve_delegate_names`, `_user_slack_id_in_list`,
  `delegate_has_slack`) is gone; PostHog is gone; inline `<script>` blocks carry
  `nonce="{{ request.state.csp_nonce }}"`; `static/js/ui.js` exists and is loaded by `base.html`.

Line numbers below are the pre-Phase-1 numbers read on 2026-10-01; locate every edit by the quoted
anchor text, which Phase 1 does not change unless the task says so.

### Review focus (this part)

1. **User-controlled text inside a flash.** SN-02/D-21 messages echo submitted addresses; the
   flash block must autoescape them. Pinned by
   `test_an_unsent_invitation_email_is_reported_and_escaped` (Task 2A-10).
2. **Duplicate-review false positives.** A different comment, a different reviewer, or the same
   review after 60 s must still be stored. Pinned by `test_a_different_comment_within_a_minute_is_a_second_review`,
   `test_another_reviewer_may_submit_the_same_review`, `test_the_same_review_after_a_minute_is_stored_again` (Task 2A-12).
3. **Self-deny while impersonating.** `current_user` is the worn account, so "self" must also
   mean the real admin. Pinned by `test_deny_under_impersonation_refuses_the_real_admins_own_account` (Task 2A-2).
4. **ORCID normalization feeds lookups, not only validation.** Impersonating by an `orcid.org/`
   URL with a lowercase `x` must find the existing user. Pinned by
   `test_impersonating_by_orcid_url_finds_the_existing_user` (Task 2A-4).
5. **Two grants sharing one title.** Removing "only the vetoed title" must not drop a title another
   un-vetoed grant still backs. Pinned by `test_veto_keeps_a_title_another_grant_still_backs` (Task 2A-9).

### File map

| File | Change | Responsibility |
|---|---|---|
| `tests/integration/_webui_helpers.py` | Create | Test helpers: impersonated session headers, session-following GET (flash). |
| `tests/integration/test_webui_helpers.py` | Create | Smoke test of the helpers. |
| `src/routers/admin/access.py` | Modify | A-07 deny guards; SN-01 promote only pending; D-18 normalized allowlist ORCID. |
| `src/routers/admin/users.py` | Modify | A-07 last-admin guard on delete. |
| `tests/integration/test_last_admin_deny_delete.py` | Create | A-07 tests. |
| `tests/integration/test_access_allowlist.py` | Create | SN-01 tests. |
| `src/services/pi_onboarding.py` | Modify | D-18 `normalize_orcid`, used by `validate_orcid`. |
| `src/routers/admin/impersonation.py` | Modify | A-16 404 for unknown ORCID; normalized lookup. |
| `tests/unit/test_orcid_normalization.py` | Create | D-18 unit tests. |
| `tests/integration/test_impersonate_lookup.py` | Create | A-16 tests. |
| `tests/unit/test_pi_onboarding.py` | Modify (docstring, lines 1-2) | Drop the retired "impersonate-if-new path". |
| `src/dependencies.py` | Modify | A-10 central write log; M-08 `staff_landing_redirect`. |
| `src/routers/admin/agents.py` | Modify | A-10 callback note; C-06 link validation; C-29 reject only pending; D-10/C-03 callback flash. |
| `src/routers/profile.py` | Modify | A-10 attribution; D-09 flash; M-08 redirect. |
| `src/routers/onboarding.py` | Modify | A-10 attribution; FN-02 effective user. |
| `src/routers/manager.py` | Modify | A-10 attribution; A-17/D-19 veto; D-07/D-11 activate; D-17 race mapping; D-09/D-10 flash. |
| `src/routers/admin/simulation.py` | Modify | A-10 audit payload note; C-24 run-row locks; `msg` flash. |
| `src/routers/admin/runs.py` | Modify (line 82) | Drop `msg` query flag. |
| `tests/integration/test_impersonated_writes.py` | Create | A-10 tests. |
| `src/services/profile_edit.py` | Modify | A-15 length check. |
| `templates/profile/edit.html` | Modify | A-15 `maxlength`, error copy. |
| `templates/manager/pi_detail.html` | Modify | A-15 `maxlength`, error copy; D-11 `has_bot_token`; D-10 banner removal. |
| `tests/integration/test_profile_field_lengths.py` | Create | A-15 tests. |
| `tests/integration/test_manager_grants_panel.py` | Modify (append) | A-17/D-19 tests. |
| `src/routers/agent_page.py` | Modify | SN-02 caps; D-21 send result; D-15 empty save; D-09 flash; D-26 pager; M-08 redirect. |
| `tests/integration/test_delegate_invite_limits.py` | Create | SN-02/D-21 tests. |
| `src/services/assessment_reviews.py` | Modify | B-11 comment refusal; B-04 duplicate check. |
| `src/routers/reviews.py` | Modify | B-04 duplicate handling; B-03 next-card anchor + flash. |
| `templates/admin/_assessments_body.html` | Modify | B-11 `maxlength`; B-03 `next_id`. |
| `templates/admin/_assessment_detail_body.html` | Modify | B-11 `maxlength`. |
| `tests/integration/test_reviews_router.py` | Modify | B-11, B-04, B-03 tests (replaces the truncation test). |
| `tests/unit/test_comment_maxlength.py` | Create | B-11 template test. |
| `static/js/ui.js` | Modify (append) | B-04 submit guard. |
| `tests/unit/test_ui_js_submit_guard.py` | Create | B-04 source test. |
| `tests/integration/test_admin_agent_link_reject.py` | Create | C-06, C-29 tests. |
| `tests/integration/test_finalize_run_route.py` | Modify (append) | C-24 tests. |
| `src/services/agent_mute.py` | Modify (line 34) | D-11 token predicate. |
| `tests/integration/test_manager_slack_provisioning.py` | Modify | D-07/D-11 tests; `slack_ok` assertions. |
| `tests/integration/test_public_profile_empty_save.py` | Create | D-15 tests. |
| `tests/integration/test_manager_pi_writes.py` | Modify | D-17 tests; `saved=1` assertion. |
| `templates/agent/public_profile.html` | Modify | D-09 banner removal. |
| `templates/admin/agent_detail.html` | Modify | D-10 banner removal. |
| `templates/manager/slack_bots.html` | Modify | D-10 banner removal. |
| `templates/admin/simulation.html` | Modify | `msg` banner removal. |
| `templates/admin/activity_detail.html` | Modify | `msg` banner removal. |
| `tests/integration/test_query_flags_to_flash.py` | Create | D-09, D-10, C-03, `msg` flash tests. |
| `tests/integration/test_onboarding_flow.py`, `test_profile_version_guard.py`, `test_agent_page.py`, `test_admin_simulation_liveness.py` | Modify | Location assertions after the flag moves. |
| `tests/e2e/test_browser_flows.py` (line 223), `tests/e2e/README.md` (line 166) | Modify | Callback landing URL without `slack_ok`. |
| `src/services/directory.py` | Modify | B-13 run fallback; D-27 page clamp. |
| `tests/unit/test_run_selection.py` | Create | B-13 tests. |
| `templates/manager/prompt_suggestion_detail.html` | Modify | D-12. |
| `tests/integration/test_prompt_suggestions_page.py` | Modify (append) | D-12 test. |
| `templates/agent/conversations.html` | Modify | D-14 fetch checks; D-26 pager. |
| `tests/unit/test_thread_fetch_js.py` | Create | D-14 source test. |
| `templates/agent/listing.html` | Modify | D-20. |
| `templates/admin/_run_detail_body.html` | Modify | D-27. |
| `tests/integration/test_agent_listing_and_pagers.py` | Create | D-20, D-26, D-27 tests. |
| `tests/integration/test_staff_pi_page_redirects.py` | Create | M-08 tests. |
| `templates/onboarding/profile_review.html` | Modify (line 84) | FN-02. |
| `tests/integration/test_onboarding_impersonated_email.py` | Create | FN-02 test. |
| `src/routers/invite.py` | Modify | FN-08 context. |
| `tests/integration/test_invite_page_context.py` | Create | FN-08 tests. |
| `src/services/export_markdown.py` | Create | C-02 server-side markdown. |
| `src/routers/admin/discussions.py` | Modify | C-02 `summary_html`. |
| `templates/admin/discussions_export.html` | Modify | C-02 render server HTML; drop client renderer. |
| `pyproject.toml` | Modify | Declare `markdown-it-py>=2.2.0` (already installed as `rich`'s dependency). |
| `tests/integration/test_discussions_export_reviews.py` | Modify (append) | C-02 test. |
| `tests/e2e/ui_audit/journeys_phase2.py` | Create (or extend) | D-14, B-04, M-08 browser journeys. |

### A-10 enumeration: writes reachable while impersonating (post-Phase-1)

| Route | Reachable as | Before this part | Treatment |
|---|---|---|---|
| `POST /profile/save` | impersonated PI/admin | none | Task 2A-6: `change_summary` note + `mechanism`; 2A-5 log |
| `POST /profile/refresh` | PI/admin | none (job row, no note column) | 2A-5 log |
| `POST /profile/delete-account` | — | refused | unchanged |
| `POST /onboarding/save-profile` | PI/admin | fixed summary, `web` | 2A-6: summary + note, `mechanism`; 2A-5 log |
| `POST /onboarding/retry` | PI/admin | none (job row) | 2A-5 log |
| `POST /agent/request` | PI/admin | note logged | 2A-5 log |
| `POST /agent/{id}/public-profile/save` | PI/delegate | note + `web_impersonated` | unchanged |
| `POST /agent/{id}/delegates/invite`, `…/revoke`, `…/remove` | PI | note logged | 2A-5 log |
| `POST /invite/{token}/accept` | any (Phase 1: `get_current_user`) | none | 2A-5 log |
| `POST /assessment-chat/{id}/messages`, `…/clear` | — | refused (`_refused`) | unchanged |
| `POST /reviews/assessments/{id}/feedback`, `/reviews/feedback/{id}/edit`, `/reviews/assessments/{id}/status` | reviewer/staff | `recorded_by` | unchanged |
| `POST /reviews/feedback/{id}/delete` | admin | `recorded_by` logged | 2A-5 log |
| `POST /reviews/assessments/{id}/assign`, `…/unassign`, `/reviews/suggestions/generate`, `/reviews/suggestions/{id}/status` | — | refused | unchanged |
| `POST /manager/pis` | manager/admin | none | 2A-5 log |
| `POST /manager/pis/{id}/profile` | manager/admin | none | 2A-6: note + `mechanism`; 2A-5 log |
| `POST /manager/pis/{id}/mute`, `…/unmute`, `…/grants/{gid}/veto`, `…/slack/provision`, `…/activate` | manager/admin | none | 2A-5 log |
| `POST /manager/pis/{id}/industry/{eid}/veto` | manager/admin | own warning | 2A-5 log |
| `POST /manager/pis/{id}/verify-email`, `POST /admin/users/{id}/verify-email` | — | refused (Phase 1) | unchanged |
| `GET /admin/agents/slack/callback` (a GET write, A-19) | manager/admin | none | 2A-5: note in a success log line |
| `POST /admin/access-requests/{id}/approve`, `…/deny`, `/admin/access-allowlist/add`, `…/{id}/remove` | admin | none | 2A-5 log |
| `POST /admin/agents/{id}/approve`, `…/reject`, `…/slack/provision`, `…/link`, `…/role`, `…/ensure-spoke` | admin | none | 2A-5 log |
| `POST /admin/cohorts/create`, `/topology`, `/ensure-star-spokes`, `/{id}/delete`, `/{id}/add-agent`, `/{id}/remove-agent` | admin | `cohort_audit_events.actor_id` = worn admin | 2A-5 log |
| `POST /admin/simulation/start`, `/finalize-run`, `/stop`, `/announce-settings`, `/announce-template` | admin | `admin_audit_events.actor_user_id` = worn admin | 2A-7: `payload.impersonation_note`; 2A-5 log |
| `POST /admin/users/{id}/delete`, `…/role` | — | refused | unchanged |
| `POST /admin/impersonate`, `/admin/impersonate/stop` | admin | session only | 2A-5 log |

`cohort_audit_events`, `delegate_invitations`, `jobs`, `agents`, `access_allowlist` have no note
column; without a migration (out of scope for Phase 2) the persisted note for those writes is the
2A-5 log line, which names the method, path, real admin id and worn user id.

### Task 2A-1: Phase 2 test helpers

**Files:**
- Create: `tests/integration/_webui_helpers.py`
- Test: `tests/integration/test_webui_helpers.py`

**Interfaces:**
- Consumes: `tests/integration/test_manager_access.py::auth_headers(user_id) -> dict`; post-Phase-1
  session key `session["impersonate_user_id"]`; `src.config.get_settings().secret_key`.
- Produces (consumed by Tasks 2A-2 … 2A-33):
  - `impersonation_headers(admin_id, target_id) -> dict[str, str]`
  - `session_from(response) -> dict[str, str]`
  - `async follow(client, response) -> httpx.Response`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_webui_helpers.py
"""The Phase 2 helpers mint a session the app accepts and follow a redirect with
the session it set (which is where a flash lives)."""

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration._webui_helpers import follow, impersonation_headers, session_from
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_impersonation_headers_make_an_impersonated_session(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, name="Worn Pi")
    r = await client.get("/profile", headers=impersonation_headers(admin.id, pi.id))
    assert r.status_code == 200
    assert "Viewing as Worn Pi" in r.text


async def test_a_session_without_the_expiry_key_is_not_impersonated(client, db_session):
    """Control for the helper above: the id alone (no expiry) must not impersonate."""
    from tests.session_support import session_headers

    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, name="Unexpired Pi")
    headers = session_headers(admin.id, impersonate_user_id=str(pi.id))
    r = await client.get("/profile", headers=headers)
    assert "Viewing as Unexpired Pi" not in r.text


async def test_follow_carries_the_session_the_redirect_set(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, name="Followed Pi")
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert session_from(r)["Cookie"].split("=", 1)[0] == auth_headers(admin.id)["Cookie"].split("=", 1)[0]
    page = await follow(client, r)
    assert page.status_code in (200, 302)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_webui_helpers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.integration._webui_helpers'`.

- [ ] **Step 3: Implement**

```python
# tests/integration/_webui_helpers.py
"""Helpers for the web UI remediation Phase 2 tests (no ``test_`` prefix, so not
collected).

``impersonation_headers`` builds on ``auth_headers`` rather than re-deriving the
cookie, so it follows whatever cookie name and session keys Phase 1 settled on
(``__Host-copi-session`` / ``copi-session``, ``epoch``) and adds only
``session["impersonate_user_id"]`` (spec §6.7).

``follow`` exists because httpx ignores its cookie jar whenever a request carries an
explicit ``Cookie`` header, which every ``auth_headers`` request does: a flash set
by a POST lives in the Set-Cookie of that response and must be re-sent by hand.
"""

import uuid
from http.cookies import SimpleCookie

from tests.integration.test_manager_access import auth_headers


def _cookie_parts(header_value: str) -> tuple[str, str]:
    name, value = header_value.split("=", 1)
    return name, value


def _session_cookie_name() -> str:
    return _cookie_parts(auth_headers(uuid.uuid4())["Cookie"])[0]


def impersonation_headers(admin_id, target_id) -> dict[str, str]:
    """Request headers for ``admin_id``'s session impersonating ``target_id``: Phase 1's
    ``auth_headers(..., impersonate=...)`` (Task 1B-4), which signs both
    ``impersonate_user_id`` and the integer ``impersonate_expires_at`` that
    ``_impersonated_user`` requires."""
    return auth_headers(admin_id, impersonate=target_id)


def session_from(response) -> dict[str, str]:
    """A ``Cookie`` header re-sending the session ``response`` set, as a browser would."""
    name = _session_cookie_name()
    for key, header in response.headers.multi_items():
        if key.lower() == "set-cookie" and header.startswith(f"{name}="):
            jar = SimpleCookie()
            jar.load(header)
            return {"Cookie": f"{name}={jar[name].value}"}
    raise AssertionError(f"no {name} cookie on the response: {response.headers}")


async def follow(client, response):
    """GET the redirect target of ``response`` with the session it set."""
    assert response.status_code in (302, 303), response.status_code
    return await client.get(
        response.headers["location"], headers=session_from(response), follow_redirects=False
    )
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `.venv-test/bin/python -m pytest tests/integration/test_webui_helpers.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/_webui_helpers.py tests/integration/test_webui_helpers.py
git commit -m "test(webui-2): impersonation and flash-following helpers for Phase 2

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-2: A-07 — deny and delete keep an admin; deny refuses self

**Files:**
- Modify: `src/routers/admin/access.py:1-18` (imports), `:99-115` (`admin_deny_access`)
- Modify: `src/routers/admin/users.py:86-116` (`admin_delete_user`)
- Test: `tests/integration/test_last_admin_deny_delete.py`

**Interfaces:**
- Consumes: `src.services.admin_invariant.ensure_admin_remains(db, *, user)`, `LastAdminError`;
  `impersonation_headers` (2A-1).
- Produces: deny → `HTTPException(400, "Cannot deny your own access")` and
  `HTTPException(400, "Cannot deny the last remaining admin")`; delete →
  `HTTPException(400, "Cannot delete the last remaining admin")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_last_admin_deny_delete.py
"""A-07: the two remaining doors out of a loginable admin — deny and an admin's
delete of another account — keep at least one allowed admin, and deny refuses the
caller's own account (including the real admin behind an impersonated session).
The last-admin branches are unreachable over HTTP without a race (the actor is an
allowed admin), so they are driven by calling the handler with a non-admin actor,
as tests/integration/test_role_appointment.py does."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from src.routers.admin.access import admin_deny_access
from src.routers.admin.users import admin_delete_user
from tests import factories
from tests.integration._webui_helpers import impersonation_headers
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _status(db, user_id):
    return (await db.execute(select(User.access_status).where(User.id == user_id))).scalar_one()


async def test_an_admin_cannot_deny_their_own_access(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/access-requests/{admin.id}/deny",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 400
    assert await _status(db_session, admin.id) == "allowed"


async def test_deny_under_impersonation_refuses_the_real_admins_own_account(client, db_session):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/access-requests/{real.id}/deny",
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    assert r.status_code == 400
    assert await _status(db_session, real.id) == "allowed"


async def test_deny_still_denies_a_pi(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, access_status="pending")
    r = await client.post(
        f"/admin/access-requests/{pi.id}/deny",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status(db_session, pi.id) == "denied"


async def test_deny_refuses_to_remove_the_last_allowed_admin(db_session):
    sole = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    with pytest.raises(HTTPException) as exc:
        await admin_deny_access(user_id=sole.id, request=None, db=db_session, current_user=actor)
    assert exc.value.status_code == 400
    assert "last remaining admin" in exc.value.detail
    assert await _status(db_session, sole.id) == "allowed"


async def test_denying_an_already_denied_admin_is_not_a_last_admin_refusal(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    denied_admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, access_status="denied"
    )
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await admin_deny_access(user_id=denied_admin.id, request=None, db=db_session, current_user=actor)
    assert await _status(db_session, denied_admin.id) == "denied"


async def test_delete_refuses_to_remove_the_last_allowed_admin(db_session):
    sole = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    with pytest.raises(HTTPException) as exc:
        await admin_delete_user(
            user_id=sole.id, request=None, remove_from_allowlist="",
            db=db_session, current_user=actor,
        )
    assert exc.value.status_code == 400
    assert "last remaining admin" in exc.value.detail
    assert (await db_session.execute(select(User.id).where(User.id == sole.id))).scalar_one_or_none()


async def test_one_admin_can_still_delete_another(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    other = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        f"/admin/users/{other.id}/delete", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert (await db_session.execute(select(User.id).where(User.id == other.id))).scalar_one_or_none() is None
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_last_admin_deny_delete.py -v`
Expected: `test_an_admin_cannot_deny_their_own_access`, `test_deny_under_impersonation_…`,
`test_deny_refuses_to_remove_the_last_allowed_admin` and
`test_delete_refuses_to_remove_the_last_allowed_admin` FAIL (302 instead of 400 / no
`HTTPException` raised); the other three pass.

- [ ] **Step 3: Implement**

In `src/routers/admin/access.py`, add after `from src.models.job import INTERACTIVE_PRIORITY`:

```python
from src.services.admin_invariant import LastAdminError, ensure_admin_remains
```

Replace the body of `admin_deny_access` from `user.access_status = "denied"` through
`await db.commit()` — as Phase 1 Task 1B-5 Step 6 left it, with the
`await bump_session_epoch(db, user.id)` line between them — with:

```python
    # A-07. "Your own account" is the session holder's: under impersonation
    # `current_user` is the worn account, so the real admin is checked too.
    real_admin = getattr(current_user, "_real_admin", None)
    if user.id == current_user.id or (real_admin is not None and user.id == real_admin.id):
        raise HTTPException(status_code=400, detail="Cannot deny your own access")
    # Denying an allowed admin is a way out of adminhood, like a demotion; the
    # invariant lock is held to the commit below. An admin who is already not
    # allowed is not counted, so re-denying one is never refused.
    if user.access_status == "allowed":
        try:
            await ensure_admin_remains(db, user=user)
        except LastAdminError:
            raise HTTPException(
                status_code=400, detail="Cannot deny the last remaining admin"
            ) from None

    user.access_status = "denied"
    # Ends every session the user holds (spec 2026-10-01 §6.7; kept from Task 1B-5).
    await bump_session_epoch(db, user.id)
    await db.commit()
```

In `src/routers/admin/users.py` `admin_delete_user`, insert after the block

```python
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
```

this:

```python
    # A-07: an admin deleting another admin is a way out of adminhood too. The
    # invariant lock (ensure_admin_remains) is held until delete_user_account
    # commits, so two admins deleting each other cannot both succeed.
    if user.access_status == "allowed":
        try:
            await ensure_admin_remains(db, user=user)
        except LastAdminError:
            raise HTTPException(
                status_code=400, detail="Cannot delete the last remaining admin"
            ) from None
```

Update the module docstring of `src/services/admin_invariant.py` lines 1-7: replace
"Both doors out of adminhood, a role change and a self-deletion, call this inside" with
"Every door out of adminhood — a role change, a self-deletion, an admin's delete of another
account and a deny (A-07) — calls this inside".

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_last_admin_deny_delete.py tests/integration/test_last_admin.py tests/integration/test_role_appointment.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/access.py src/routers/admin/users.py src/services/admin_invariant.py tests/integration/test_last_admin_deny_delete.py
git commit -m "fix(webui-2): A-07 deny and delete keep a loginable admin; deny refuses self

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-3: SN-01 — allowlisting promotes only a pending user

**Files:**
- Modify: `src/routers/admin/access.py:144-153` (`admin_allowlist_add`)
- Test: `tests/integration/test_access_allowlist.py`

**Interfaces:** Consumes `auth_headers`. Produces no new name.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_access_allowlist.py
"""SN-01: adding an ORCID to the allowlist promotes a PENDING request; it never
silently reverses an earlier deny (the Approve button on the denied row does that)."""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, AccessAllowlist, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _status(db, user_id):
    return (await db.execute(select(User.access_status).where(User.id == user_id))).scalar_one()


async def test_allowlisting_a_denied_orcid_does_not_reverse_the_deny(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    denied = await factories.make_user(db_session, access_status="denied")
    r = await client.post(
        "/admin/access-allowlist/add", data={"orcid": denied.orcid, "note": ""},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status(db_session, denied.id) == "denied"
    row = (await db_session.execute(
        select(AccessAllowlist).where(AccessAllowlist.orcid == denied.orcid)
    )).scalar_one()
    assert row.added_by_user_id == admin.id


async def test_allowlisting_a_pending_orcid_promotes_it(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pending = await factories.make_user(
        db_session, access_status="pending", onboarding_complete=False
    )
    r = await client.post(
        "/admin/access-allowlist/add", data={"orcid": pending.orcid, "note": ""},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status(db_session, pending.id) == "allowed"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_access_allowlist.py -v`
Expected: `test_allowlisting_a_denied_orcid_does_not_reverse_the_deny` FAILS (`'allowed' == 'denied'`).

- [ ] **Step 3: Implement**

In `admin_allowlist_add` replace

```python
    # If a user with this ORCID already exists and is pending, promote them.
    user_result = await db.execute(select(User).where(User.orcid == orcid_clean))
    user = user_result.scalar_one_or_none()
    if user and user.access_status != "allowed":
```

with

```python
    # Promote an existing PENDING request (SN-01). A denied user stays denied: a
    # deny is reversed only by the Approve button on its row, never as a side
    # effect of allowlisting the ORCID.
    user_result = await db.execute(select(User).where(User.orcid == orcid_clean))
    user = user_result.scalar_one_or_none()
    if user and user.access_status == "pending":
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_access_allowlist.py tests/integration/test_auth_allowlist_gate.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/access.py tests/integration/test_access_allowlist.py
git commit -m "fix(webui-2): SN-01 allowlist add promotes only a pending request

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-4: D-18 ORCID normalization; A-16 impersonation never creates a user

**Files:**
- Modify: `src/services/pi_onboarding.py:28-36` (`_ORCID_RE`, `validate_orcid`), its module docstring (lines 1-3) and the `find_or_create_pi_by_orcid` docstring (lines 51-52), which still describe the impersonate-creates-a-user path this task removes (plan audit Q2-13)
- Modify: `src/routers/admin/impersonation.py:15` (import), `:27-43` (lookup)
- Modify: `src/routers/admin/access.py:128` (`orcid_clean`)
- Modify: `tests/unit/test_pi_onboarding.py:1-2` (docstring)
- Test: `tests/unit/test_orcid_normalization.py`, `tests/integration/test_impersonate_lookup.py`

**Interfaces:**
- Produces: `src.services.pi_onboarding.normalize_orcid(raw: str) -> str` (consumed by
  `validate_orcid`, `impersonate_user`, `admin_allowlist_add`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_orcid_normalization.py
"""D-18: the ORCID forms people paste — an orcid.org URL, a lowercase check-digit
x — normalize to the canonical iD before validation."""

import pytest

from src.services.pi_onboarding import normalize_orcid, validate_orcid


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("  0000-0002-1825-0097\n", "0000-0002-1825-0097"),
        ("0000-0002-1694-233x", "0000-0002-1694-233X"),
        ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("http://orcid.org/0000-0002-1694-233x", "0000-0002-1694-233X"),
        ("orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("HTTPS://WWW.ORCID.ORG/0000-0002-1825-0097", "0000-0002-1825-0097"),
    ],
)
def test_validate_orcid_accepts_the_forms_people_paste(raw, expected):
    assert validate_orcid(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "orcid.org/",
        "0000-0002-1825-009",
        "https://evil.example/0000-0002-1825-0097",
        "0000-0002-1825-0097/extra",
    ],
)
def test_validate_orcid_still_rejects_malformed_input(raw):
    with pytest.raises(ValueError, match="Invalid ORCID"):
        validate_orcid(raw)


def test_normalize_orcid_does_not_validate():
    assert normalize_orcid(" not-an-orcid ") == "not-an-orcid"
```

```python
# tests/integration/test_impersonate_lookup.py
"""A-16: impersonation looks an account up by ORCID and never creates one."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, Job, User
from tests import factories
from tests.integration._webui_helpers import session_from
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_impersonating_an_unknown_orcid_is_a_404_and_creates_nothing(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    fetch = AsyncMock(return_value={"name": "Ghost"})
    with patch("src.services.pi_onboarding.fetch_orcid_profile", new=fetch):
        r = await client.post(
            "/admin/impersonate", data={"orcid": "0000-0003-9999-0001"},
            headers=auth_headers(admin.id), follow_redirects=False,
        )
    assert r.status_code == 404
    fetch.assert_not_awaited()
    assert (await db_session.execute(
        select(User).where(User.orcid == "0000-0003-9999-0001")
    )).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(Job).where(Job.type == "generate_profile")
    )).scalars().all() == []


async def test_impersonating_by_orcid_url_finds_the_existing_user(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    target = await factories.make_user(db_session, name="Url Target", orcid="0000-0003-0000-002X")
    r = await client.post(
        "/admin/impersonate", data={"orcid": "https://orcid.org/0000-0003-0000-002x"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    page = await client.get("/profile", headers=session_from(r))
    assert page.status_code == 200
    assert f"Viewing as {target.name}" in page.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_orcid_normalization.py tests/integration/test_impersonate_lookup.py -v`
Expected: `ImportError: cannot import name 'normalize_orcid'` for the unit file; the 404 test
FAILS with status 302 (a pending PI is created) or `fetch` awaited.

- [ ] **Step 3: Implement**

In `src/services/pi_onboarding.py` replace

```python
def validate_orcid(orcid: str) -> str:
    """Format-only check shared by Add-PI and `cli seed-profile` (MD-14)."""
    orcid = orcid.strip()
```

with

```python
#: An ``orcid.org/`` prefix as pasted from a profile page, with or without a
#: scheme or ``www.`` (D-18).
_ORCID_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?orcid\.org/", re.IGNORECASE)


def normalize_orcid(raw: str) -> str:
    """The canonical spelling of a typed ORCID iD: trimmed, an ``orcid.org/`` URL
    prefix removed, a lowercase check digit ``x`` uppercased (D-18). Does not
    validate the result — ``validate_orcid`` does. Used wherever an ORCID is looked
    up, so a pasted URL finds the same row as the bare iD."""
    orcid = _ORCID_URL_PREFIX.sub("", raw.strip())
    if orcid.endswith("x"):
        orcid = orcid[:-1] + "X"
    return orcid


def validate_orcid(orcid: str) -> str:
    """Format-only check shared by Add-PI and `cli seed-profile` (MD-14), applied
    after ``normalize_orcid``; returns the normalized iD."""
    orcid = normalize_orcid(orcid)
```

In `src/routers/admin/impersonation.py` replace the import
`from src.services.pi_onboarding import find_or_create_pi_by_orcid` with
`from src.services.pi_onboarding import normalize_orcid`, and replace

```python
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
```

with

```python
    orcid = normalize_orcid(orcid)
    result = await db.execute(select(User).where(User.orcid == orcid))
    target = result.scalar_one_or_none()
    if target is None:
        # A-16: impersonation looks an account up; it never creates one. An unknown
        # ORCID used to mint a pending PI and enqueue a profile job.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No user has that ORCID iD"
        )
```

(If `logger` is then unused in that module, delete `import logging` and the `logger = …` line.)

In `src/routers/admin/access.py` add `from src.services.pi_onboarding import normalize_orcid`
to the imports and replace `orcid_clean = orcid.strip()` with
`orcid_clean = normalize_orcid(orcid)`.

In `tests/unit/test_pi_onboarding.py` replace the docstring lines 1-2 with
`"""find_or_create_pi_by_orcid: the shared ORCID-onboarding logic used by the manager Add-PI route."""`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_orcid_normalization.py tests/integration/test_impersonate_lookup.py tests/unit/test_pi_onboarding.py tests/unit/test_cli_seed_profile.py tests/integration/test_manager_pi_writes.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/pi_onboarding.py src/routers/admin/impersonation.py src/routers/admin/access.py tests/unit/test_pi_onboarding.py tests/unit/test_orcid_normalization.py tests/integration/test_impersonate_lookup.py
git commit -m "fix(webui-2): D-18 normalize pasted ORCIDs; A-16 impersonation 404s an unknown ORCID

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-5: A-10 — one log line for every write made while impersonating

**Files:**
- Modify: `src/dependencies.py:117-134` (impersonation block of `get_current_user`)
- Modify: `src/routers/admin/agents.py:1-28` (imports, logger), `:426-441` (`admin_provision_slack_callback` success path)
- Test: `tests/integration/test_impersonated_writes.py` (create)

**Interfaces:**
- Consumes: `impersonation_headers` (2A-1); `src.dependencies.impersonation_note`;
  `tests/integration/test_manager_slack_provisioning.py::_pending_pi`.
- Produces: WARNING record on logger `src.dependencies`:
  `"Write %s %s by admin %s while impersonating %s"` (method, path, real admin id, worn user id);
  INFO record on logger `src.routers.admin`: `"Slack bot token stored for agent %s by %s (%s)"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_impersonated_writes.py
"""A-10: every write made while impersonating carries an impersonation note — a
column where the table has one (Tasks 2A-6, 2A-7), and always the central log line
from get_current_user (this task) naming the real admin and the worn account."""

import logging

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, SlackAppProvision
from tests import factories
from tests.integration._webui_helpers import impersonation_headers
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_manager_slack_provisioning import _pending_pi

pytestmark = pytest.mark.integration


async def test_a_write_under_impersonation_is_logged_with_both_identities(client, db_session, caplog):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    caplog.set_level(logging.WARNING, logger="src.dependencies")
    r = await client.post(
        "/admin/access-allowlist/add", data={"orcid": "0000-0004-0000-0001", "note": ""},
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert (
        f"Write POST /admin/access-allowlist/add by admin {real.id} while impersonating {worn.id}"
        in caplog.messages
    )


async def test_a_read_under_impersonation_is_not_logged_as_a_write(client, db_session, caplog):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    caplog.set_level(logging.WARNING, logger="src.dependencies")
    r = await client.get("/admin/access-requests", headers=impersonation_headers(real.id, worn.id))
    assert r.status_code == 200
    assert not [m for m in caplog.messages if m.startswith("Write ")]


async def test_a_write_without_impersonation_logs_no_impersonation_line(client, db_session, caplog):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    caplog.set_level(logging.WARNING, logger="src.dependencies")
    await client.post(
        "/admin/access-allowlist/add", data={"orcid": "0000-0004-0000-0002", "note": ""},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert not [m for m in caplog.messages if "while impersonating" in m]


async def test_the_slack_callback_records_the_impersonation_note(
    client, db_session, caplog, monkeypatch
):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    _pi, agent = await _pending_pi(db_session, agent_id="impcb")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="imp-s", client_id="cid", client_secret="secret",
        initiated_by_user_id=manager.id,
    ))
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.admin_provisioning.exchange_code", lambda *a, **k: "xoxb-imp"
    )
    caplog.set_level(logging.INFO, logger="src.routers.admin")
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=imp-s",
        headers=impersonation_headers(real.id, manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert any(
        "Slack bot token stored for agent impcb" in m and f"impersonated by admin {real.id}" in m
        for m in caplog.messages
    )
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonated_writes.py -v`
Expected: `test_a_write_under_impersonation_…` and `test_the_slack_callback_…` FAIL (message absent);
the other two pass.

- [ ] **Step 3: Implement**

In `src/dependencies.py` `_impersonated_user` (Phase 1 Task 1B-4 moved the impersonation
lookup there; the block is indented 12 spaces; plan audit Q2-08), replace

```python
            imp_user._real_admin = session_user  # type: ignore[attr-defined]
            return imp_user
```

with

```python
            imp_user._real_admin = session_user  # type: ignore[attr-defined]
            if request.method not in ("GET", "HEAD"):
                # A-10: the one impersonation note every write gets, whatever
                # table it lands in. Tables with a recorded_by column or a
                # revision summary also carry it (impersonation_note below);
                # for the rest (jobs, agents, cohort audit, allowlist,
                # delegate invitations) this line is the record.
                logger.warning(
                    "Write %s %s by admin %s while impersonating %s",
                    request.method, request.url.path, session_user.id, imp_user.id,
                )
            return imp_user
```

In `src/routers/admin/agents.py`: add `import logging` to the stdlib imports, change
`from src.dependencies import get_admin_user, get_staff_user` to
`from src.dependencies import get_admin_user, get_staff_user, impersonation_note`, and add
after the imports (before `_SLUG_RE`):

```python
logger = logging.getLogger("src.routers.admin")
```

In `admin_provision_slack_callback`, insert directly after the `except ProvisioningError as exc:
return surface_error(str(exc))` block:

```python
    # A GET write (A-19) reachable under impersonation: the token row has no note
    # column, so the note goes on this line (A-10).
    logger.info(
        "Slack bot token stored for agent %s by %s (%s)",
        agent.agent_id, current_user.id, impersonation_note(current_user) or "direct",
    )
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonated_writes.py tests/integration/test_impersonation_guards.py tests/integration/test_manager_slack_provisioning.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dependencies.py src/routers/admin/agents.py tests/integration/test_impersonated_writes.py
git commit -m "feat(webui-2): A-10 log every write made while impersonating

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-6: A-10 — profile writes record `web_impersonated` and the note

**Files:**
- Modify: `src/routers/profile.py:11` (import), `:133-173` (`profile_save`)
- Modify: `src/routers/onboarding.py` imports, `:114-152` (`save_profile`)
- Modify: `src/routers/manager.py:52` (import), `:308-347` (`manager_edit_pi_profile`)
- Test: `tests/integration/test_impersonated_writes.py` (append)

**Interfaces:**
- Consumes: `impersonation_note(current_user) -> str | None`;
  `apply_profile_edits(..., change_summary=..., mechanism=...)`.
- Produces: `ProfileRevision.mechanism == "web_impersonated"` and the note in
  `ProfileRevision.change_summary` for these three routes.

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/test_impersonated_writes.py`;
  add `from sqlalchemy import select` to its third-party imports, and
  `from src.models import ProfileRevision` / `from src.services import profile_export` to the
  first-party imports, keeping the `I` sort order)

```python
async def _latest_revision(db, agent):
    return (await db.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
        .order_by(ProfileRevision.created_at.desc())
    )).scalars().first()


async def test_profile_save_under_impersonation_is_attributed(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        "/profile/save",
        data={"name": pi.name, "email": pi.email, "institution": "", "department": "",
              "research_summary": "Imp profile edit",
              "profile_version": str(profile.profile_version)},
        headers=impersonation_headers(admin.id, pi.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web_impersonated"
    assert f"impersonated by admin {admin.id}" in (rev.change_summary or "")


async def test_profile_save_without_impersonation_stays_web(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        "/profile/save",
        data={"name": pi.name, "email": pi.email, "institution": "", "department": "",
              "research_summary": "Own edit", "profile_version": str(profile.profile_version)},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web"
    assert "impersonated" not in (rev.change_summary or "")


async def test_onboarding_save_under_impersonation_keeps_both_summaries(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, onboarding_complete=False)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        "/onboarding/save-profile",
        data={"email": pi.email, "research_summary": "Onboarding imp",
              "profile_version": str(profile.profile_version)},
        headers=impersonation_headers(admin.id, pi.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web_impersonated"
    assert "Profile saved during onboarding" in rev.change_summary
    assert f"impersonated by admin {admin.id}" in rev.change_summary


async def test_manager_profile_edit_under_impersonation_is_attributed(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "email": pi.email, "research_summary": "Manager imp edit",
              "profile_version": str(profile.profile_version)},
        headers=impersonation_headers(admin.id, manager.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web_impersonated"
    assert f"impersonated by admin {admin.id}" in (rev.change_summary or "")
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonated_writes.py -v -k "profile or onboarding"`
Expected: the three `…_under_impersonation_…` tests FAIL (`'web' == 'web_impersonated'`);
`test_profile_save_without_impersonation_stays_web` passes.

- [ ] **Step 3: Implement**

`src/routers/profile.py`: change the import to
`from src.dependencies import get_current_user, get_pi_user, impersonation_note, refuse_impersonation`,
and in `profile_save` replace

```python
    error = await apply_profile_edits(
        db, target_user=current_user, changed_by_user_id=current_user.id,
```

with

```python
    note = impersonation_note(current_user)
    error = await apply_profile_edits(
        db, target_user=current_user, changed_by_user_id=current_user.id,
```

and replace `        expected_version=parse_expected_version(profile_version),\n    )` (the end of that
call) with

```python
        expected_version=parse_expected_version(profile_version),
        change_summary=note,
        mechanism="web_impersonated" if note else "web",
    )
```

`src/routers/onboarding.py`: add `impersonation_note` to its `from src.dependencies import …`
line; in `save_profile` insert `    note = impersonation_note(current_user)` immediately before
`    error = await apply_profile_edits(`, and replace

```python
        change_summary="Profile saved during onboarding",
```

with

```python
        change_summary="; ".join(filter(None, ["Profile saved during onboarding", note])),
        mechanism="web_impersonated" if note else "web",
```

`src/routers/manager.py`: change `from src.dependencies import get_review_user, get_staff_user`
to `from src.dependencies import get_review_user, get_staff_user, impersonation_note`; in
`manager_edit_pi_profile` insert `    note = impersonation_note(current_user)` immediately before
`    error = await apply_profile_edits(`, and replace

```python
        jhu_tenure_start=jhu_tenure_start,
        expected_version=parse_expected_version(profile_version),
    )
```

with

```python
        jhu_tenure_start=jhu_tenure_start,
        expected_version=parse_expected_version(profile_version),
        change_summary=note,
        mechanism="web_impersonated" if note else "web",
    )
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonated_writes.py tests/integration/test_profile_single_writer.py tests/integration/test_onboarding_flow.py tests/integration/test_manager_pi_writes.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/profile.py src/routers/onboarding.py src/routers/manager.py tests/integration/test_impersonated_writes.py
git commit -m "feat(webui-2): A-10 profile writes under impersonation record web_impersonated

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-7: A-10 — simulation audit rows carry the impersonation note

**Files:**
- Modify: `src/routers/admin/simulation.py` imports; add `_audit_payload` after `_hash12`; the six
  `record_audit(` calls (start ~:270, finalize ~:322, stop ~:367, announce-settings ~:424,
  template reset ~:460, template update ~:480)
- Test: `tests/integration/test_impersonated_writes.py` (append)

**Interfaces:**
- Produces: `src.routers.admin.simulation._audit_payload(payload: dict | None, current_user: User) -> dict | None`;
  `AdminAuditEvent.payload["impersonation_note"] == "impersonated by admin <id>"` under impersonation.

- [ ] **Step 1: Write the failing tests** (append; add `from sqlalchemy import select` if not
  already imported and `AdminAuditEvent, SimulationCommand` to the `src.models` import)

```python
async def test_simulation_start_under_impersonation_notes_the_audit_row(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        "/admin/simulation/start", data={"max_runtime": "0", "max_proposals": "0"},
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    assert r.status_code == 302
    event = (await db_session.execute(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "simulation_start_requested")
    )).scalar_one()
    assert event.actor_user_id == worn.id
    assert event.payload["impersonation_note"] == f"impersonated by admin {real.id}"
    cmd = (await db_session.execute(
        select(SimulationCommand).where(SimulationCommand.command == "start")
    )).scalar_one()
    assert "impersonation_note" not in cmd.payload


async def test_simulation_start_without_impersonation_has_no_note(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await client.post(
        "/admin/simulation/start", data={"max_runtime": "0", "max_proposals": "0"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    event = (await db_session.execute(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "simulation_start_requested")
    )).scalar_one()
    assert "impersonation_note" not in event.payload


async def test_announce_settings_under_impersonation_notes_the_audit_row(client, db_session):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await client.post(
        "/admin/simulation/announce-settings", data={"channels": "", "disable": "true"},
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    event = (await db_session.execute(
        select(AdminAuditEvent).where(
            AdminAuditEvent.action == "simulation_announce_channels_updated"
        )
    )).scalar_one()
    assert event.payload["impersonation_note"] == f"impersonated by admin {real.id}"
    assert event.payload["new"] == ""
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonated_writes.py -v -k "simulation or announce"`
Expected: the two `…_under_impersonation_…` tests FAIL with `KeyError: 'impersonation_note'`.

- [ ] **Step 3: Implement**

In `src/routers/admin/simulation.py` add `from src.dependencies import impersonation_note` to
the first-party imports, and add after the `_hash12` function:

```python
def _audit_payload(payload: dict | None, current_user: User) -> dict | None:
    """The audit row's payload plus ``impersonation_note`` when an admin made this
    request while impersonating another admin (A-10). ``actor_user_id`` stays the
    worn account (operator decision 2026-09-10). Never applied to a command
    payload: the supervisor reads those."""
    note = impersonation_note(current_user)
    if note is None:
        return payload
    return {**(payload or {}), "impersonation_note": note}
```

Then wrap the `payload=` argument of each of the six `record_audit(` calls:

| Route | Before | After |
|---|---|---|
| `admin_simulation_start` | `payload=payload` | `payload=_audit_payload(payload, current_user)` |
| `admin_simulation_finalize_run` | `payload=payload,` | `payload=_audit_payload(payload, current_user),` |
| `admin_simulation_stop` | `payload=payload` | `payload=_audit_payload(payload, current_user)` |
| `admin_simulation_announce_settings` | `payload={"old": old_value, "new": new_value},` | `payload=_audit_payload({"old": old_value, "new": new_value}, current_user),` |
| `admin_simulation_announce_template` (reset) | `payload={"old_hash": _hash12(old_value), "new_hash": None},` | `payload=_audit_payload({"old_hash": _hash12(old_value), "new_hash": None}, current_user),` |
| `admin_simulation_announce_template` (save) | `payload={"old_hash": _hash12(old_value), "new_hash": _hash12(body)},` | `payload=_audit_payload({"old_hash": _hash12(old_value), "new_hash": _hash12(body)}, current_user),` |

Leave every `enqueue_command(… payload=payload …)` call unchanged.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_impersonated_writes.py tests/integration/test_admin_simulation_page.py tests/integration/test_finalize_run_route.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/simulation.py tests/integration/test_impersonated_writes.py
git commit -m "feat(webui-2): A-10 simulation audit rows carry the impersonation note

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-8: A-15 — name, institution, department over 255 characters are a form error

**Files:**
- Modify: `src/services/profile_edit.py:20-28` (constant), `:99-100` (first statement of `apply_profile_edits`)
- Modify: `templates/profile/edit.html` (inputs `name="name"`, `name="institution"`, `name="department"`; error block)
- Modify: `templates/manager/pi_detail.html` (same three inputs; error block)
- Test: `tests/integration/test_profile_field_lengths.py`

**Interfaces:**
- Produces: `src.services.profile_edit.USER_FIELD_MAX_CHARS = 255`; error code `"field_too_long"`
  returned by `apply_profile_edits` before any write.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_profile_field_lengths.py
"""A-15: users.name / institution / department are String(255). An overlong value
used to reach the INSERT and 500; it is now a form error and nothing is written."""

import re

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _form(pi, **overrides):
    data = {"name": pi.name, "email": pi.email, "institution": "", "department": "",
            "research_summary": "Studies the thing."}
    data.update(overrides)
    return data


@pytest.mark.parametrize("field", ["name", "institution", "department"])
async def test_an_overlong_field_is_a_form_error_not_a_500(client, db_session, field):
    pi = await factories.make_user(db_session, name="Short Name")
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        "/profile/save", data=_form(pi, **{field: "N" * 256}),
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=field_too_long" in r.headers["location"]
    row = (await db_session.execute(select(User).where(User.id == pi.id))).scalar_one()
    await db_session.refresh(row)
    assert row.name == "Short Name"
    page = await client.get(r.headers["location"], headers=auth_headers(pi.id))
    assert "255 characters" in page.text


async def test_exactly_255_characters_is_accepted(client, db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        "/profile/save", data=_form(pi, institution="I" * 255),
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]


async def test_the_manager_form_reports_the_same_error(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile", data={"name": "M" * 300, "research_summary": "x"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=field_too_long" in r.headers["location"]
    page = await client.get(r.headers["location"], headers=auth_headers(manager.id))
    assert "255 characters" in page.text


@pytest.mark.parametrize("field", ["name", "institution", "department"])
async def test_both_forms_cap_the_inputs_at_255(client, db_session, field):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    pattern = re.compile(
        rf'<input(?=[^>]*\bname="{field}")(?=[^>]*\bmaxlength="255")[^>]*>'
    )
    own = await client.get("/profile/edit", headers=auth_headers(pi.id))
    managed = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert pattern.search(own.text)
    assert pattern.search(managed.text)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_profile_field_lengths.py -v`
Expected: the overlong tests FAIL (500 / `DataError` from asyncpg, or no `error=` in the
location); `test_both_forms_cap_the_inputs_at_255` FAILS (no `maxlength`).

- [ ] **Step 3: Implement**

`src/services/profile_edit.py`, after `_LIST_FIELDS = …`:

```python
#: users.name / institution / department are String(255) (src/models/user.py).
#: Checked before any write so an overlong value is a form error, not a 500 (A-15).
USER_FIELD_MAX_CHARS = 255
```

and as the first statements of `apply_profile_edits` (directly after its docstring):

```python
    for field in ("name", "institution", "department"):
        if len(form.get(field) or "") > USER_FIELD_MAX_CHARS:
            return "field_too_long"
```

Add to the docstring of `apply_profile_edits`, before "Returns an error code": "A name,
institution or department longer than ``USER_FIELD_MAX_CHARS`` returns ``field_too_long``
before anything is written."

`templates/profile/edit.html`: add ` maxlength="255"` directly after `name="name"`,
`name="institution"` and `name="department"` in their `<input>` tags; in the error block,
insert before `{% else %}Something went wrong saving your changes. Please try again.{% endif %}`:

```html
            {% elif error == 'field_too_long' %}Name, institution and department are limited to 255 characters each.
```

`templates/manager/pi_detail.html`: the same three ` maxlength="255"` additions in the Edit
Profile form; in the error block insert before `{% else %}Something went wrong saving changes.{% endif %}`:

```html
            {% elif request.query_params.get('error') == 'field_too_long' %}Name, institution and department are limited to 255 characters each.
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_profile_field_lengths.py tests/integration/test_profile_single_writer.py tests/integration/test_manager_pi_writes.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/profile_edit.py templates/profile/edit.html templates/manager/pi_detail.html tests/integration/test_profile_field_lengths.py
git commit -m "fix(webui-2): A-15 overlong name, institution, department are a form error

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-9: A-17 PI-target check on vetoes; D-19 veto removes only its title, idempotently

**Files:**
- Modify: `src/routers/manager.py:79` (drop `GrantRecord, derive_grant_titles` import),
  `:421-461` (`manager_veto_grant`), `:464-498` (`manager_veto_industry_evidence`),
  `:501-531` (`_pending_pi_agent`, reuse the new helper)
- Test: `tests/integration/test_manager_grants_panel.py` (append)

**Interfaces:**
- Produces: `src.routers.manager._require_pi(db: AsyncSession, user_id: uuid.UUID) -> User`
  (404 `"PI not found"` for a missing or non-PI account).

- [ ] **Step 1: Write the failing tests** (append; add `USER_ROLE_ADMIN`, `PiIndustryEvidence`
  to the `src.models` import)

```python
def _grant(user_id, title, project):
    return PiGrant(
        user_id=user_id, core_project_num=project, title=title,
        org_name="JOHNS HOPKINS UNIVERSITY", activity_code="R01", last_fy=2024,
        tenure_filter_mode="org_only",
    )


async def test_veto_removes_only_the_vetoed_title_while_other_grants_remain(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(
        user_id=pi.id, grant_titles=["ORCID-only title", "Wrong person grant", "Right grant"],
    ))
    wrong = _grant(pi.id, "Wrong person grant", "R01XX000010")
    db_session.add_all([wrong, _grant(pi.id, "Right grant", "R01XX000011")])
    await db_session.commit()
    r = await client.post(
        f"/manager/pis/{pi.id}/grants/{wrong.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r.status_code == 302
    prof = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    await db_session.refresh(prof)
    assert prof.grant_titles == ["ORCID-only title", "Right grant"]


async def test_a_second_veto_keeps_the_first_vetoed_at(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    g = _grant(pi.id, "Twice vetoed", "R01XX000012")
    db_session.add(g)
    await db_session.commit()
    url = f"/manager/pis/{pi.id}/grants/{g.id}/veto"
    await client.post(url, headers=auth_headers(mgr.id), follow_redirects=False)
    await db_session.refresh(g)
    first = g.vetoed_at
    r = await client.post(url, headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    await db_session.refresh(g)
    assert first is not None and g.vetoed_at == first


async def test_veto_keeps_a_title_another_grant_still_backs(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    db_session.add(ResearcherProfile(user_id=pi.id, grant_titles=["Shared title"]))
    one = _grant(pi.id, "Shared title", "R01XX000013")
    db_session.add_all([one, _grant(pi.id, "Shared title", "R01XX000014")])
    await db_session.commit()
    await client.post(
        f"/manager/pis/{pi.id}/grants/{one.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    prof = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    await db_session.refresh(prof)
    assert prof.grant_titles == ["Shared title"]


async def test_veto_on_a_non_pi_account_is_404(client, db_session):
    staff = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    g = _grant(staff.id, "Staff grant", "R01XX000015")
    db_session.add(g)
    e = PiIndustryEvidence(
        user_id=staff.id, source="openalex", kind="coauthor_company", external_id="W9:I9",
        company_name="Pfizer", company_class="pharma_biotech", year=2021, pi_role="last",
        in_tenure=True, evidence={},
    )
    db_session.add(e)
    await db_session.commit()
    r1 = await client.post(
        f"/manager/pis/{staff.id}/grants/{g.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    r2 = await client.post(
        f"/manager/pis/{staff.id}/industry/{e.id}/veto", headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r1.status_code == 404 and r2.status_code == 404
    await db_session.refresh(g)
    await db_session.refresh(e)
    assert g.vetoed_at is None and e.vetoed_at is None
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_grants_panel.py -v`
Expected: `…only_the_vetoed_title…` FAILS (`['Right grant']`), `…second_veto…` FAILS (timestamps
differ), `…another_grant_still_backs` FAILS (`[]`), `…non_pi_account_is_404` FAILS (302).

- [ ] **Step 3: Implement**

Delete the line `from src.services.grant_resolution import GrantRecord, derive_grant_titles`.
Add above `_pending_pi_agent`:

```python
async def _require_pi(db: AsyncSession, user_id: uuid.UUID) -> User:
    """The PI account ``user_id``, or 404 — never a staff account, so a manager
    cannot act on (or probe) an admin's row by UUID (A-17)."""
    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None or target.user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
    return target
```

In `_pending_pi_agent`, replace

```python
    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None or target.user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
```

with `    await _require_pi(db, user_id)`.

Replace the body of `manager_veto_grant` (after its docstring) with:

```python
    await _require_pi(db, user_id)
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        .with_for_update()
    )).scalar_one_or_none()
    grant = (await db.execute(
        select(PiGrant).where(PiGrant.id == grant_id, PiGrant.user_id == user_id)
    )).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    if grant.vetoed_at is not None:
        # Idempotent (D-19): a replayed veto keeps the first vetoed_at.
        return RedirectResponse(url=f"/manager/pis/{user_id}#grants", status_code=302)
    grant.vetoed_at = datetime.now(UTC)
    still_backed = await db.scalar(
        select(func.count(PiGrant.id)).where(
            PiGrant.user_id == user_id, PiGrant.id != grant.id,
            PiGrant.vetoed_at.is_(None), PiGrant.title == grant.title,
        )
    )
    if profile is not None and not still_backed:
        # Remove only the vetoed title (D-19). Re-deriving the list from the
        # remaining PiGrant rows dropped every title that never came from one
        # (ORCID- or publication-sourced).
        profile.grant_titles = [t for t in (profile.grant_titles or []) if t != grant.title]
    await db.commit()
    await _reexport_profile_markdown_best_effort(db, user_id)
    return RedirectResponse(url=f"/manager/pis/{user_id}#grants", status_code=302)
```

Update the docstring of `manager_veto_grant`: append "Removes only the vetoed grant's title from
``grant_titles`` (unless another un-vetoed grant carries the same title); a second veto is a no-op.
404 for a non-PI account (A-17)."

In `manager_veto_industry_evidence`, insert as the first statement after its docstring:
`    await _require_pi(db, user_id)`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_grants_panel.py tests/integration/test_manager_industry_panel.py tests/integration/test_manager_slack_provisioning.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/manager.py tests/integration/test_manager_grants_panel.py
git commit -m "fix(webui-2): A-17 vetoes require a PI target; D-19 veto drops only its title

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-10: SN-02 delegate-invite caps; D-21 report an unsent invitation email

**Files:**
- Modify: `src/routers/agent_page.py` imports (`flash`), module constants after `_ROOT_LIMIT`,
  `:787-890` (`invite_delegate`)
- Test: `tests/integration/test_delegate_invite_limits.py`

**Interfaces:**
- Consumes: `src.web.flash.flash`; `DelegateInvitation.created_at` (server default `now()`),
  `send_transactional_email(...) -> bool`.
- Produces: `src.routers.agent_page._INVITES_PER_SUBMISSION = 10`,
  `_INVITES_PER_AGENT_PER_DAY = 25`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_delegate_invite_limits.py
"""SN-02: at most 10 addresses per submission and 25 invitations per agent per
rolling 24 h, counted from delegate_invitations rows of any status (so revoke and
resend cannot get around it). D-21: an invitation whose email was not sent says so."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.models import DelegateInvitation
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _owner(db):
    pi = await factories.make_user(db, name="Cap Pi")
    agent = await factories.make_agent(db, user=pi, status="active")
    return pi, agent


async def _count(db, agent):
    return (await db.execute(
        select(func.count(DelegateInvitation.id))
        .where(DelegateInvitation.agent_registry_id == agent.id)
    )).scalar_one()


async def _prior(db, pi, agent, n, *, status="pending", created_at=None):
    for _ in range(n):
        row = DelegateInvitation(
            agent_registry_id=agent.id, invited_by_user_id=pi.id,
            email=f"prior-{uuid.uuid4().hex[:8]}@example.org", token=uuid.uuid4().hex,
            status=status, expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
    await db.flush()


async def _invite(client, pi, agent, emails):
    return await client.post(
        f"/agent/{agent.agent_id}/delegates/invite", data={"emails": emails},
        headers=auth_headers(pi.id), follow_redirects=False,
    )


async def test_more_than_ten_addresses_are_refused_whole(client, db_session):
    pi, agent = await _owner(db_session)
    r = await _invite(client, pi, agent, ",".join(f"cap{i}@example.org" for i in range(11)))
    assert r.status_code == 302
    assert await _count(db_session, agent) == 0
    page = await follow(client, r)
    assert "At most 10 addresses per invitation" in page.text


async def test_ten_addresses_are_accepted(client, db_session):
    pi, agent = await _owner(db_session)
    await _invite(client, pi, agent, "\n".join(f"ten{i}@example.org" for i in range(10)))
    assert await _count(db_session, agent) == 10


async def test_the_daily_cap_stops_at_25_per_agent(client, db_session):
    pi, agent = await _owner(db_session)
    await _prior(db_session, pi, agent, 24)
    r = await _invite(client, pi, agent, "a1@example.org,a2@example.org,a3@example.org")
    assert await _count(db_session, agent) == 25
    page = await follow(client, r)
    assert "Daily limit of 25 invitations per agent reached" in page.text
    assert "a2@example.org was not invited" in page.text


async def test_revoked_invitations_still_count(client, db_session):
    pi, agent = await _owner(db_session)
    await _prior(db_session, pi, agent, 25, status="revoked")
    await _invite(client, pi, agent, "late@example.org")
    assert await _count(db_session, agent) == 25


async def test_invitations_older_than_a_day_do_not_count(client, db_session):
    pi, agent = await _owner(db_session)
    await _prior(
        db_session, pi, agent, 25, status="expired",
        created_at=datetime.now(UTC) - timedelta(hours=25),
    )
    await _invite(client, pi, agent, "fresh@example.org")
    assert await _count(db_session, agent) == 26


async def test_an_unsent_invitation_email_is_reported_and_escaped(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.routers.agent_page.send_transactional_email", AsyncMock(return_value=False)
    )
    pi, agent = await _owner(db_session)
    r = await _invite(client, pi, agent, "<b>tag</b>@example.org")
    assert await _count(db_session, agent) == 1
    page = await follow(client, r)
    assert "no email was sent to" in page.text
    assert "&lt;b&gt;tag&lt;/b&gt;@example.org" in page.text
    assert "<b>tag</b>@example.org" not in page.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_delegate_invite_limits.py -v`
Expected: `…more_than_ten…`, `…daily_cap…`, `…revoked…still_count`, `…unsent…` FAIL; the
ten-address and older-than-a-day tests pass.

- [ ] **Step 3: Implement**

Add after `_ROOT_LIMIT = 50` in `src/routers/agent_page.py`:

```python
#: Delegate-invite caps (SN-02): per submission, and per agent over any rolling
#: 24 hours. The window counts every delegate_invitations row created in it,
#: whatever its status, so revoke-and-resend cannot get around it; the rows are
#: the ledger, no table of their own.
_INVITES_PER_SUBMISSION = 10
_INVITES_PER_AGENT_PER_DAY = 25
```

Add `from src.web.flash import flash` to the imports if Phase 1 has not.

In `invite_delegate`, replace from `    # Parse comma/newline-separated emails` through the end of the
function with:

```python
    # Parse comma/newline-separated emails
    email_list = [
        e.strip().lower()
        for e in re.split(r"[,\n]+", emails)
        if e.strip()
    ]
    if len(email_list) > _INVITES_PER_SUBMISSION:
        flash(
            request,
            f"At most {_INVITES_PER_SUBMISSION} addresses per invitation; nothing was sent.",
            "error",
        )
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
    # The agent row is locked FOR UPDATE above, so this count and the inserts
    # below cannot interleave with another invite for the same agent.
    created_last_day = await db.scalar(
        select(func.count(DelegateInvitation.id)).where(
            DelegateInvitation.agent_registry_id == agent.id,
            DelegateInvitation.created_at >= datetime.now(UTC) - timedelta(hours=24),
        )
    ) or 0
    remaining_today = max(0, _INVITES_PER_AGENT_PER_DAY - created_last_day)

    errors = []
    to_send: list[tuple[str, str]] = []
    for email in email_list:
        # Basic validation (length-capped to avoid ReDoS; see SEC-16)
        if not is_valid_email(email):
            errors.append(f"Invalid email: {email}")
            continue

        # Don't invite yourself
        if current_user.email and email == current_user.email.lower():
            errors.append("You can't invite yourself.")
            continue

        # Check if already an active delegate
        existing_delegate = await db.execute(
            select(AgentDelegate)
            .join(User, AgentDelegate.user_id == User.id)
            .where(
                AgentDelegate.agent_registry_id == agent.id,
                func.lower(User.email) == email,
            )
        )
        if existing_delegate.scalar_one_or_none():
            errors.append(f"{email} is already a delegate.")
            continue

        # Check for pending invitation
        existing_invite = await db.execute(
            select(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == agent.id,
                DelegateInvitation.email == email,
                DelegateInvitation.status == "pending",
            )
        )
        if existing_invite.scalar_one_or_none():
            errors.append(f"Invitation already pending for {email}.")
            continue

        if remaining_today <= 0:
            errors.append(
                f"Daily limit of {_INVITES_PER_AGENT_PER_DAY} invitations per agent "
                f"reached; {email} was not invited."
            )
            continue

        # Create invitation
        token = secrets.token_urlsafe(48)
        invitation = DelegateInvitation(
            agent_registry_id=agent.id,
            invited_by_user_id=current_user.id,
            email=email,
            token=token,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(invitation)
        await db.flush()  # Get the ID
        remaining_today -= 1

        to_send.append((email, f"{settings.base_url}/invite/{token}"))

    await db.commit()

    # The invitation exists regardless of whether the email gets through; the PI
    # is told which ones did not go out (D-21). send_transactional_email returns
    # False when the outbound allowlist suppresses the address or SES fails.
    unsent: list[str] = []
    for email, invite_url in to_send:
        sent = await send_transactional_email(
            build_delegate_invitation(email, agent.pi_name, agent.bot_name, invite_url)
        )
        if not sent:
            unsent.append(email)
    if unsent:
        # A count plus the first three addresses: flash text is capped at
        # MAX_FLASH_CHARS (Task 1C-1), so a full list could be cut off (plan audit Q2-12).
        shown = ", ".join(unsent[:3]) + (f" and {len(unsent) - 3} more" if len(unsent) > 3 else "")
        errors.append(
            f"Invitation saved, but no email was sent for {len(unsent)} address(es): {shown}."
        )

    if errors:
        flash(request, "; ".join(errors), "error")
    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
```

(`settings = get_settings()` stays where it is, above the parse.) Update the `invite_delegate`
docstring: append "At most ``_INVITES_PER_SUBMISSION`` addresses per submission (else nothing is
created) and ``_INVITES_PER_AGENT_PER_DAY`` invitation rows per agent in any 24 hours (SN-02)."

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_delegate_invite_limits.py tests/integration/test_agent_page.py tests/integration/test_double_submits.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/agent_page.py tests/integration/test_delegate_invite_limits.py
git commit -m "fix(webui-2): SN-02 cap delegate invites; D-21 report unsent invitation email

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-11: B-11 — overlong or NUL review comments are a 400; forms carry `maxlength`

**Files:**
- Modify: `src/services/assessment_reviews.py:88-90` (`_MAX_COMMENT_CHARS` comment), add
  `_validate_comment` after `_validate` (`:111-137`), `submit_feedback` (`:317-365`), `edit_feedback` (`:367-422`)
- Modify: `templates/admin/_assessments_body.html:627`, `templates/admin/_assessment_detail_body.html:1123`, `:1161`
- Modify: `tests/integration/test_reviews_router.py:393-416` (replace the truncation test)
- Test: `tests/unit/test_comment_maxlength.py`

**Interfaces:**
- Produces: `src.services.assessment_reviews._validate_comment(comment: str) -> None` (raises
  `ValueError`, which both routers already turn into a 400).

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_reviews_router.py`, replace the whole function
`test_an_overlong_comment_is_truncated_not_rejected` with:

```python
async def test_an_overlong_comment_is_refused_and_nothing_is_written(client, db_session):
    """B-11: refused with a 400, never silently cut. ``_MAX_COMMENT_CHARS`` is
    imported rather than hardcoded so this tracks the cap, which the forms also
    carry as ``maxlength``."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "x" * (_MAX_COMMENT_CHARS + 1), "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id), follow_redirects=False,
    )
    assert r.status_code == 400
    assert (await db_session.execute(select(AssessmentReview))).scalars().all() == []


async def test_a_comment_at_the_cap_is_stored_whole(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "y" * _MAX_COMMENT_CHARS, "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id), follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    review = (await db_session.execute(select(AssessmentReview))).scalar_one()
    assert len(review.comment) == _MAX_COMMENT_CHARS


async def test_a_nul_in_a_comment_is_a_400_not_a_500(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "before\x00after", "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id), follow_redirects=False,
    )
    assert r.status_code == 400
    assert (await db_session.execute(select(AssessmentReview))).scalars().all() == []


async def test_an_overlong_edit_is_refused_and_the_comment_is_kept(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    review = AssessmentReview(
        assessment_id=assessment.id, reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name, score=3, feedback_mode="log_only", comment="original",
    )
    db_session.add(review)
    await db_session.flush()
    r = await client.post(
        f"/reviews/feedback/{review.id}/edit",
        data={"score": "3", "comment": "z" * (_MAX_COMMENT_CHARS + 1), "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id), follow_redirects=False,
    )
    assert r.status_code == 400
    await db_session.refresh(review)
    assert review.comment == "original"
```

```python
# tests/unit/test_comment_maxlength.py
"""B-11: every review-comment textarea carries the server's cap as maxlength, so
the browser stops typing where the server would refuse."""

import re
from pathlib import Path

from src.services.assessment_reviews import _MAX_COMMENT_CHARS

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = (
    "templates/admin/_assessments_body.html",
    "templates/admin/_assessment_detail_body.html",
)
TEXTAREA = re.compile(r'<textarea\b[^>]*\bname="comment"[^>]*>')


def test_every_review_comment_textarea_carries_the_cap():
    tags = [
        (rel, tag)
        for rel in TEMPLATES
        for tag in TEXTAREA.findall((ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert len(tags) == 3
    for rel, tag in tags:
        assert f'maxlength="{_MAX_COMMENT_CHARS}"' in tag, (rel, tag)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py -v -k "comment or edit_is_refused" tests/unit/test_comment_maxlength.py`
Expected: the overlong, NUL and overlong-edit tests FAIL (302, or 500 for NUL); the
template test FAILS (no `maxlength`); the at-the-cap test passes.

- [ ] **Step 3: Implement**

`src/services/assessment_reviews.py`: replace

```python
#: Comment rows are capped, not rejected — a reviewer pasting an overlong
#: transcript should still get a saved row, just truncated.
_MAX_COMMENT_CHARS = 10_000
```

with

```python
#: Longest review comment, in characters. Refused, not truncated (B-11): the old
#: silent cut lost the reviewer's text with no error. The comment textareas carry
#: the same number as ``maxlength``.
_MAX_COMMENT_CHARS = 10_000
```

Add after `_validate`:

```python
def _validate_comment(comment: str) -> None:
    """ValueError for a comment over ``_MAX_COMMENT_CHARS`` or one containing NUL
    (B-11). PostgreSQL text cannot store U+0000, so a NUL used to reach the INSERT
    and surface as a 500."""
    # Browsers count a textarea's maxlength with LF line breaks but submit CRLF, so
    # the length is measured on the LF form (plan audit Q2-11).
    if len(comment.replace("\r\n", "\n")) > _MAX_COMMENT_CHARS:
        raise ValueError(f"comment is longer than {_MAX_COMMENT_CHARS} characters")
    if "\x00" in comment:
        raise ValueError("comment contains a NUL character")
```

In `submit_feedback` replace `    _validate(score, feedback_mode, dimension_scores)` with

```python
    _validate(score, feedback_mode, dimension_scores)
    _validate_comment(comment)
```

and `        comment=comment[:_MAX_COMMENT_CHARS],` with `        comment=comment,`. In `edit_feedback`
make the same `_validate_comment(comment)` addition after its `_validate(...)` line and replace
`    review.comment = comment[:_MAX_COMMENT_CHARS]` with `    review.comment = comment`. In both
docstrings, change "Raises ``ValueError`` on an out-of-range score or an unrecognized mode" (in
`submit_feedback`) to "Raises ``ValueError`` on an out-of-range score, an unrecognized mode, or a
comment ``_validate_comment`` refuses".

Templates: add ` maxlength="10000"` directly after `name="comment"` in
`templates/admin/_assessments_body.html` (the quick-score textarea, `id="{{ qs }}comment"`) and in
both textareas of `templates/admin/_assessment_detail_body.html` (`id="{{ edit_prefix }}comment"`
and `id="add-comment"`).

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py tests/unit/test_comment_maxlength.py tests/integration/test_review_edit_lock.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/assessment_reviews.py templates/admin/_assessments_body.html templates/admin/_assessment_detail_body.html tests/integration/test_reviews_router.py tests/unit/test_comment_maxlength.py
git commit -m "fix(webui-2): B-11 refuse overlong or NUL review comments; maxlength on the forms

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-12: B-04 — an identical review within 60 s is stored once

**Files:**
- Modify: `src/services/assessment_reviews.py:54-78` (imports), constants after `_MAX_COMMENT_CHARS`, `submit_feedback`
- Modify: `src/routers/reviews.py:34-41` (import), `:284-330` (`submit_review_feedback`)
- Test: `tests/integration/test_reviews_router.py` (append)

**Interfaces:**
- Consumes: `src.services.advisory_locks.entity_key_sql(namespace) -> str`; `src.web.flash.flash`.
- Produces: `src.services.assessment_reviews.DUPLICATE_WINDOW_SECONDS = 60`,
  `class DuplicateFeedbackError(Exception)`; per-entity advisory namespace `review_submit`
  (id `"<reviewer_id>:<assessment_id>"`).

- [ ] **Step 1: Write the failing tests** (append; change the file's
  `from sqlalchemy import select, text` to `from sqlalchemy import func, select, text, update`, add
  `from datetime import timedelta` to the stdlib imports and
  `from tests.integration._webui_helpers import follow` to the first-party imports)

```python
_SAME = {"score": "4", "comment": "same words", "feedback_mode": "log_only",
         "surface": "manager-list"}


async def _reviews_of(db, assessment):
    return (await db.execute(
        select(AssessmentReview).where(AssessmentReview.assessment_id == assessment.id)
    )).scalars().all()


async def _post(client, assessment, user, data):
    return await client.post(
        f"/reviews/assessments/{assessment.id}/feedback", data=data,
        headers=auth_headers(user.id), follow_redirects=False,
    )


async def test_an_identical_review_within_a_minute_is_stored_once(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r1 = await _post(client, assessment, reviewer, _SAME)
    r2 = await _post(client, assessment, reviewer, _SAME)
    assert r1.status_code == 302 and r2.status_code == 302
    assert len(await _reviews_of(db_session, assessment)) == 1
    page = await follow(client, r2)
    assert "already recorded" in page.text


async def test_a_different_comment_within_a_minute_is_a_second_review(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    await _post(client, assessment, reviewer, _SAME)
    await _post(client, assessment, reviewer, {**_SAME, "comment": "second thought"})
    assert len(await _reviews_of(db_session, assessment)) == 2


async def test_another_reviewer_may_submit_the_same_review(client, db_session):
    first = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    second = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    await _post(client, assessment, first, _SAME)
    await _post(client, assessment, second, _SAME)
    assert len(await _reviews_of(db_session, assessment)) == 2


async def test_the_same_review_after_a_minute_is_stored_again(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    await _post(client, assessment, reviewer, _SAME)
    await db_session.execute(
        update(AssessmentReview)
        .where(AssessmentReview.assessment_id == assessment.id)
        .values(created_at=func.now() - timedelta(seconds=61))
    )
    await _post(client, assessment, reviewer, _SAME)
    assert len(await _reviews_of(db_session, assessment)) == 2
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py -v -k "identical or different_comment or another_reviewer or after_a_minute"`
Expected: `test_an_identical_review_within_a_minute_is_stored_once` FAILS (`2 == 1`); the other three pass.

- [ ] **Step 3: Implement**

`src/services/assessment_reviews.py` imports: add `from datetime import timedelta` to the stdlib
group, change `from sqlalchemy import delete, func, select` to
`from sqlalchemy import delete, func, select, text`, and add
`from src.services.advisory_locks import entity_key_sql` to the first-party group (sorted).
After `_MAX_COMMENT_CHARS = 10_000` add:

```python
#: An identical review by the same reviewer on the same assessment within this
#: many seconds is a double submit, not a second opinion (B-04).
DUPLICATE_WINDOW_SECONDS = 60


class DuplicateFeedbackError(Exception):
    """An identical review by this reviewer on this assessment was stored less than
    ``DUPLICATE_WINDOW_SECONDS`` ago; nothing was written."""
```

In `submit_feedback`, after `_validate_comment(comment)` insert:

```python
    normalized = _normalized_dimension_scores(dimension_scores)
    # Serialize this reviewer's submissions on this assessment, so two racing
    # POSTs of a double click cannot both pass the check below (B-04).
    await db.execute(
        text(f"SELECT pg_advisory_xact_lock({entity_key_sql('review_submit')})"),
        {"id": f"{reviewer.id}:{assessment.id}"},
    )
    recent = (await db.execute(
        select(AssessmentReview).where(
            AssessmentReview.assessment_id == assessment.id,
            AssessmentReview.reviewer_user_id == reviewer.id,
            AssessmentReview.created_at
            >= func.now() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS),
        )
    )).scalars().all()
    if any(
        r.score == score
        and r.comment == comment
        and r.feedback_mode == feedback_mode
        and (r.dimension_scores or None) == normalized
        for r in recent
    ):
        raise DuplicateFeedbackError
```

and in the `AssessmentReview(...)` constructor replace
`dimension_scores=_normalized_dimension_scores(dimension_scores),` with
`dimension_scores=normalized,`. Add to the `submit_feedback` docstring: "Raises
``DuplicateFeedbackError`` (nothing written) when the same reviewer stored an identical review on
this assessment within ``DUPLICATE_WINDOW_SECONDS`` (B-04)."

`src/routers/reviews.py`: add `DuplicateFeedbackError` to the `from src.services.assessment_reviews
import (...)` list and `from src.web.flash import flash` to the imports if absent. In
`submit_review_feedback`, replace

```python
    try:
        await submit_feedback(
```

through

```python
    await db.commit()
    logger.info(
        "Review feedback by %s (%s) on assessment %s: score=%s mode=%s dims=%d",
        current_user.name, current_user.id, assessment_id, score, feedback_mode,
        len(dimension_scores),
    )
```

with

```python
    duplicate = False
    try:
        await submit_feedback(
            db,
            assessment=assessment,
            reviewer=current_user,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
            dimension_scores=dimension_scores,
            recorded_by=recorded_by(current_user),
        )
    except DuplicateFeedbackError:
        duplicate = True
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if duplicate:
        # A double submit lands where the first did, with a note instead of a
        # second row (B-04). Not an error page: the browser shows THIS response.
        flash(
            request,
            "That feedback was already recorded a moment ago; it was not saved twice.",
            "info",
        )
    else:
        await db.commit()
        logger.info(
            "Review feedback by %s (%s) on assessment %s: score=%s mode=%s dims=%d",
            current_user.name, current_user.id, assessment_id, score, feedback_mode,
            len(dimension_scores),
        )
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py tests/integration/test_review_pipeline_races.py tests/integration/test_review_dimension_scores.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/assessment_reviews.py src/routers/reviews.py tests/integration/test_reviews_router.py
git commit -m "fix(webui-2): B-04 store an identical review submitted twice within 60 s once

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-13: B-04 — `ui.js` disables a submitted POST form's buttons

**Files:**
- Modify: `static/js/ui.js` (append; file created by Phase 1 Part 1A)
- Test: `tests/unit/test_ui_js_submit_guard.py`; browser journey in Task 2A-34

**Interfaces:**
- Consumes: Phase 0 `static/js/confirm.js` (capture-phase `submit` listener that calls
  `preventDefault()` on a dismissed dialog).
- Produces: opt-out attribute `data-allow-resubmit` on a `<form>`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ui_js_submit_guard.py
"""B-04: the client half of the double-submit guard, pinned at source level (no JS
runner in this repo; the browser journey journey_review_double_click_stores_one
exercises it)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")


def _guard() -> str:
    start = UI.index("// B-04:")
    return UI[start:]


def test_the_guard_listens_for_submit_on_the_document():
    assert "document.addEventListener('submit', function (event) {" in _guard()


def test_a_cancelled_submission_is_left_alone():
    assert "if (event.defaultPrevented" in _guard()


def test_only_post_forms_that_leave_the_page_are_guarded():
    guard = _guard()
    assert "(form.getAttribute('method') || 'get').toLowerCase() !== 'post'" in guard
    assert "form.hasAttribute('data-allow-resubmit')" in guard
    assert "target !== '_self'" in guard


def test_buttons_are_disabled_after_the_entry_list_is_built():
    guard = _guard()
    assert "window.setTimeout(function () {" in guard
    assert "b.disabled = true;" in guard


def test_a_page_restored_from_the_bfcache_re_enables_them():
    guard = _guard()
    assert "window.addEventListener('pageshow', function (event) {" in guard
    assert "b.disabled = false;" in guard
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_ui_js_submit_guard.py -v`
Expected: FAIL with `ValueError: substring not found` (`// B-04:` absent).

- [ ] **Step 3: Implement** — append to `static/js/ui.js`:

```js
// B-04: once a POST form's submission is under way its submit buttons are
// disabled, so a double click sends one request. Deferred with setTimeout
// because a control disabled during the submit event is left out of the form
// data, and the clicked button's own name/value must still be sent. Left alone:
// a submission a handler cancelled (confirm.js's dismissed dialog), GET forms,
// forms that target another window, and forms marked data-allow-resubmit. The
// server's duplicate check (assessment_reviews.submit_feedback) is the backstop.
document.addEventListener('submit', function (event) {
  var form = event.target;
  if (event.defaultPrevented || !(form instanceof HTMLFormElement)) { return; }
  if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') { return; }
  if (form.hasAttribute('data-allow-resubmit')) { return; }
  var target = form.getAttribute('target');
  if (target && target !== '_self') { return; }
  window.setTimeout(function () {
    form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]')
      .forEach(function (b) {
        b.disabled = true;
        b.setAttribute('data-submit-guarded', '');
      });
  }, 0);
});

// A page restored from the back-forward cache would keep those buttons disabled.
window.addEventListener('pageshow', function (event) {
  if (!event.persisted) { return; }
  document.querySelectorAll('[data-submit-guarded]').forEach(function (b) {
    b.disabled = false;
    b.removeAttribute('data-submit-guarded');
  });
});
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_ui_js_submit_guard.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add static/js/ui.js tests/unit/test_ui_js_submit_guard.py
git commit -m "feat(webui-2): B-04 ui.js disables a submitted POST form's buttons

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-14: C-06 — link-user accepts only a valid, existing, unlinked lab owner

**Files:**
- Modify: `src/routers/admin/agents.py` imports (`flash`), `:445-464` (`admin_link_agent`), new `_link_target`
- Test: `tests/integration/test_admin_agent_link_reject.py` (create)

**Interfaces:**
- Produces: `src.routers.admin.agents._link_target(db, agent, raw_user_id: str) -> tuple[str | None, User | None]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_admin_agent_link_reject.py
"""C-06: POST /admin/agents/{id}/link refuses, with a message and nothing written,
a malformed or unknown user id, a user already linked to another agent, and a role
that cannot own a lab. C-29 (Task 2A-15): reject applies only to a pending request."""

import uuid

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AgentRegistry,
)
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _link(client, admin, agent, user_id):
    return await client.post(
        f"/admin/agents/{agent.id}/link", data={"user_id": user_id},
        headers=auth_headers(admin.id), follow_redirects=False,
    )


async def _linked_user(db, agent):
    return (await db.execute(
        select(AgentRegistry.user_id).where(AgentRegistry.id == agent.id)
    )).scalar_one()


@pytest.mark.parametrize(
    "value,message",
    [
        ("", "Choose a user to link."),
        ("not-a-uuid", "That user id is not valid."),
        (str(uuid.uuid4()), "No such user."),
    ],
)
async def test_link_refuses_a_missing_malformed_or_unknown_user(client, db_session, value, message):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, agent, value)
    assert r.status_code == 302
    assert await _linked_user(db_session, agent) is None
    page = await follow(client, r)
    assert message in page.text


async def test_link_refuses_a_user_already_linked_to_another_agent(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi)
    orphan = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, orphan, str(pi.id))
    assert await _linked_user(db_session, orphan) is None
    page = await follow(client, r)
    assert "already linked to another agent" in page.text


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_link_refuses_a_role_that_cannot_own_a_lab(client, db_session, role):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    staff = await factories.make_user(db_session, user_role=role)
    agent = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, agent, str(staff.id))
    assert await _linked_user(db_session, agent) is None
    page = await follow(client, r)
    assert "cannot own a lab" in page.text


async def test_link_links_an_unlinked_pi(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, agent, str(pi.id))
    assert r.status_code == 302
    assert await _linked_user(db_session, agent) == pi.id
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_agent_link_reject.py -v`
Expected: `""` → 422; `not-a-uuid` → `ValueError` 500; unknown uuid → `IntegrityError` 500;
already-linked → `IntegrityError`; staff roles → linked (assert fails); the happy path passes.

- [ ] **Step 3: Implement**

Add `from src.web.flash import flash` to `src/routers/admin/agents.py` imports if it is not there (Phase 1 Tasks 1C-5/1C-6 add it; a duplicate is ruff F811; plan audit Q2-16). Replace
`admin_link_agent` with:

```python
@router.post("/agents/{agent_id}/link")
async def admin_link_agent(
    agent_id: uuid.UUID,
    request: Request,
    user_id: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Link an agent to a user account (C-06).

    Refused with a flash and nothing written unless ``user_id`` names an existing
    account whose role may own a lab and that no other agent is linked to; see
    ``_link_target``. A malformed or unknown id used to surface as a 500.
    """
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    refusal, user = await _link_target(db, agent, user_id)
    if refusal is not None:
        flash(request, refusal, "error")
        return RedirectResponse(url="/admin/agents", status_code=302)
    agent.user_id = user.id
    try:
        await db.commit()
    except IntegrityError:
        # agents.user_id is unique: a concurrent link of the same user won.
        await db.rollback()
        flash(request, "That user is already linked to another agent.", "error")
    return RedirectResponse(url="/admin/agents", status_code=302)


async def _link_target(
    db: AsyncSession, agent: AgentRegistry, raw_user_id: str
) -> tuple[str | None, User | None]:
    """``(refusal, user)`` for the link form; exactly one is None. The role rule is
    ``User.may_use_pi_surfaces`` (PI or admin): a manager or reviewer has no lab (D7)."""
    raw = raw_user_id.strip()
    if not raw:
        return "Choose a user to link.", None
    try:
        target_id = uuid.UUID(raw)
    except ValueError:
        return "That user id is not valid.", None
    user = (
        await db.execute(select(User).where(User.id == target_id))
    ).scalar_one_or_none()
    if user is None:
        return "No such user.", None
    if not user.may_use_pi_surfaces:
        return "That account's role cannot own a lab.", None
    linked_elsewhere = await db.scalar(
        select(AgentRegistry.id).where(
            AgentRegistry.user_id == user.id, AgentRegistry.id != agent.id
        )
    )
    if linked_elsewhere is not None:
        return "That user is already linked to another agent.", None
    return None, user
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_agent_link_reject.py tests/integration/test_admin_agents_queries.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/agents.py tests/integration/test_admin_agent_link_reject.py
git commit -m "fix(webui-2): C-06 link-user refuses invalid, unknown, linked or non-lab users

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-15: C-29 — reject applies only to a pending request

**Files:**
- Modify: `src/routers/admin/agents.py:334-352` (`admin_reject_agent`)
- Test: `tests/integration/test_admin_agent_link_reject.py` (append)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests** (append)

```python
async def _status_of(db, agent):
    return (await db.execute(
        select(AgentRegistry.status).where(AgentRegistry.id == agent.id)
    )).scalar_one()


async def test_reject_refuses_an_active_agent(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="active")
    r = await client.post(
        f"/admin/agents/{agent.id}/reject", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status_of(db_session, agent) == "active"
    page = await follow(client, r)
    assert "Only a pending request can be rejected" in page.text


async def test_reject_suspends_a_pending_request(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="pending")
    await client.post(
        f"/admin/agents/{agent.id}/reject", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert await _status_of(db_session, agent) == "suspended"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_agent_link_reject.py -v -k reject`
Expected: `test_reject_refuses_an_active_agent` FAILS (`'suspended' == 'active'`).

- [ ] **Step 3: Implement** — in `admin_reject_agent` replace the docstring and

```python
    agent.status = "suspended"
    await db.commit()
```

with docstring `"""Reject a pending agent request. Any other status is refused (C-29): an
approved agent is parked or suspended from its edit form, not by "Reject"."""` and

```python
    if agent.status != "pending":
        flash(
            request,
            "Only a pending request can be rejected; change an approved agent's status "
            "on its edit page.",
            "error",
        )
        return RedirectResponse(url="/admin/agents", status_code=302)
    agent.status = "suspended"
    await db.commit()
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_agent_link_reject.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/agents.py tests/integration/test_admin_agent_link_reject.py
git commit -m "fix(webui-2): C-29 reject applies only to a pending agent request

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-16: C-24 — start and finalize lock the run row before their checks

**Files:**
- Modify: `src/routers/admin/simulation.py` `admin_simulation_start` (`:205-279`), `admin_simulation_finalize_run` (`:283-327`)
- Test: `tests/integration/test_finalize_run_route.py` (append)

**Interfaces:** none new. Lock order: the latest run row first, then (finalize only) the target
run row, so the two routes cannot deadlock.

- [ ] **Step 1: Write the failing tests** (append; change the file's `from sqlalchemy import select`
  to `from sqlalchemy import event, select`)

```python
def _capture_statements(sync_conn):
    seen: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(sync_conn, "before_cursor_execute", _before)
    return seen, lambda: event.remove(sync_conn, "before_cursor_execute", _before)


def _lock_precedes_command_reads(seen: list[str]) -> bool:
    lock_at = next(
        i for i, s in enumerate(seen) if "FROM simulation_runs" in s and "FOR NO KEY UPDATE" in s
    )
    commands_at = next(i for i, s in enumerate(seen) if "FROM simulation_commands" in s)
    return lock_at < commands_at


async def test_start_locks_the_latest_run_before_reading_pending_commands(
    client, db_session, monkeypatch
):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="lock-a@example.org")
    await _stopped_run(db_session)
    conn = await db_session.connection()
    seen, stop = _capture_statements(conn.sync_connection)
    try:
        r = await client.post(
            "/admin/simulation/start", data={"max_runtime": "0", "max_proposals": "0"},
            headers=auth_headers(admin.id), follow_redirects=False,
        )
    finally:
        stop()
    assert r.status_code == 302
    assert _lock_precedes_command_reads(seen)


async def test_finalize_locks_the_run_before_reading_pending_commands(
    client, db_session, monkeypatch
):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="lock-b@example.org")
    run = await _stopped_run(db_session)
    conn = await db_session.connection()
    seen, stop = _capture_statements(conn.sync_connection)
    try:
        r = await client.post(
            "/admin/simulation/finalize-run",
            data={"run_id": str(run.id), "confirm_run": str(run.id)[:8]},
            headers=auth_headers(admin.id), follow_redirects=False,
        )
    finally:
        stop()
    assert r.status_code == 302
    assert _lock_precedes_command_reads(seen)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_finalize_run_route.py -v -k locks`
Expected: both FAIL with `StopIteration` (no `FOR NO KEY UPDATE` statement on `simulation_runs`).

- [ ] **Step 3: Implement**

In `admin_simulation_start`, insert as the first statements after the docstring (before
`alive = await engine_alive(db)`):

```python
    # C-24: lock the latest run row before any check. Finalize takes the same
    # lock first (admin_simulation_finalize_run), so a start that would resume
    # this run and a finalize of it serialize: the second waits for the first's
    # commit, then sees its pending command and refuses.
    latest_id = await latest_run_id(db)
    latest = None
    if latest_id is not None:
        latest = (
            await db.execute(
                select(SimulationRun).where(SimulationRun.id == latest_id).with_for_update(key_share=True)
            )
        ).scalar_one_or_none()
```

and delete the two later lines

```python
    latest_id = await latest_run_id(db)
    latest = await db.get(SimulationRun, latest_id) if latest_id is not None else None
```

In `admin_simulation_finalize_run`, insert directly after the nested `def _refuse(...)` block
(before `if await engine_alive(db):`):

```python
    # C-24: the same latest-run lock admin_simulation_start takes, then this run's
    # row. Always in that order, so the two routes cannot deadlock.
    latest_id = await latest_run_id(db)
    if latest_id is not None and latest_id != run_id:
        await db.execute(
            select(SimulationRun.id).where(SimulationRun.id == latest_id).with_for_update(key_share=True)
        )
    run = (
        await db.execute(
            select(SimulationRun).where(SimulationRun.id == run_id).with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
```

and delete the later line `    run = await db.get(SimulationRun, run_id)`. Append to both
docstrings: "Takes ``SELECT … FOR NO KEY UPDATE`` on the run row(s) before its checks (C-24): it serializes start and finalize against each other without conflicting with the FOR KEY SHARE locks the engine's inserts take on the referenced run row (plan audit Q2-06)."

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_finalize_run_route.py tests/integration/test_admin_simulation_page.py tests/integration/test_admin_simulation_liveness.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/simulation.py tests/integration/test_finalize_run_route.py
git commit -m "fix(webui-2): C-24 start and finalize lock the run row before their checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-17: D-07 activate never overwrites a concurrent change; D-11 one token predicate

**Files:**
- Modify: `src/routers/manager.py:46` (`update` import), `:63` (drop `activate_agent` if unused),
  `manager_pi_detail` (`:203-250`, add `has_bot_token`), `manager_activate_agent` (`:558-587`)
- Modify: `templates/manager/pi_detail.html:49`
- Modify: `src/services/agent_mute.py:34` and its imports
- Test: `tests/integration/test_manager_slack_provisioning.py` (append)

**Interfaces:**
- Consumes: `ensure_activation_allowed(db, agent, *, new_role, new_status) -> list[str]` (Phase 1C,
  imported by name into `src.routers.manager`); `token_for_agent_row(agent) -> str | None`.
- Produces: template context key `has_bot_token: bool` on `manager/pi_detail.html`.

- [ ] **Step 1: Write the failing tests** (append; change the file's `from sqlalchemy import select`
  to `from sqlalchemy import select, text` and add
  `from tests.integration._webui_helpers import follow` after `from tests import factories`)

```python
async def _grounded(db_session, pi):
    await factories.make_profile(
        db_session, user=pi, evidence_pmid_count=10, evidence_pub_count=8,
    )
    await db_session.flush()


async def test_activate_does_not_overwrite_a_concurrent_suspend(client, db_session, monkeypatch):
    import src.routers.manager as manager_routes

    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="raced", slack_bot_token="xoxb-raced")
    await _grounded(db_session, pi)
    # Committed (a savepoint release under conftest's create_savepoint mode), so the
    # route's rollback on a lost race cannot undo the fixtures (plan audit Q2-02).
    await db_session.commit()
    real_gate = manager_routes.ensure_activation_allowed

    async def gate_then_suspend(db, a, **kwargs):
        blockers = await real_gate(db, a, **kwargs)
        # The concurrent writer: committed before the route's conditional UPDATE runs,
        # so the route's own rollback leaves it in place.
        await db.execute(text("UPDATE agents SET status = 'suspended' WHERE id = :id"), {"id": a.id})
        await db.commit()
        return blockers

    monkeypatch.setattr(manager_routes, "ensure_activation_allowed", gate_then_suspend)
    r = await client.post(
        f"/manager/pis/{pi.id}/activate", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    await db_session.refresh(agent)
    assert agent.status == "suspended"
    assert agent.approved_by is None
    page = await follow(client, r)
    assert "changed while you were activating it" in page.text


async def test_activate_accepts_an_env_only_bot_token(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.services.slack_tokens.env_token",
        lambda aid: "xoxb-env-only" if aid == "envonly" else None,
    )
    manager = await _manager(db_session)
    pi, agent = await _pending_pi(db_session, agent_id="envonly")
    await _grounded(db_session, pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/activate", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activated=1"
    await db_session.refresh(agent)
    assert agent.status == "active"


async def test_pi_detail_offers_activate_for_an_env_only_token(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.services.slack_tokens.env_token",
        lambda aid: "xoxb-env-only" if aid == "envdetail" else None,
    )
    manager = await _manager(db_session)
    pi, _agent = await _pending_pi(db_session, agent_id="envdetail")
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert f'action="/manager/pis/{pi.id}/activate"' in r.text
    assert f'action="/manager/pis/{pi.id}/slack/provision"' not in r.text


async def test_unmute_accepts_an_env_only_bot_token(client, db_session, monkeypatch):
    monkeypatch.setattr(
        "src.services.slack_tokens.env_token",
        lambda aid: "xoxb-env-only" if aid == "envmute" else None,
    )
    manager = await _manager(db_session)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(db_session, user=pi, agent_id="envmute", status="inactive")
    await _grounded(db_session, pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/unmute", headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    await db_session.refresh(agent)
    assert agent.status == "active"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_slack_provisioning.py -v -k "concurrent or env_only"`
Expected: the suspend test FAILS (`'active' == 'suspended'`); the three env-only tests FAIL
(refused as "no token" / provision form shown / `error=no_token`).

- [ ] **Step 3: Implement**

`src/routers/manager.py`: `from sqlalchemy import func, select` → `from sqlalchemy import func, select, update`;
`from src.services.agent_activation import activate_agent, activation_blockers` →
`from src.services.agent_activation import activation_blockers, ensure_activation_allowed`
(Phase 1 Task 1C-6 imports `ensure_activation_allowed` only into `src/routers/admin/agents.py`;
drop `activate_agent` only if `ruff check src/routers/manager.py` then reports it unused).
In `manager_pi_detail`'s `_template_context(...)` call add the keyword
`has_bot_token=bool(agent is not None and token_for_agent_row(agent)),`.

In `manager_activate_agent`: change the condition `if not agent.slack_bot_token:` to
`if not token_for_agent_row(agent):` (leave the refusal body as Phase 1 left it), then replace
everything from the activation gate call (post-Phase-1: the `ensure_activation_allowed(...)` /
`activate_agent(...)` call) to the end of the function with:

```python
    blockers = await ensure_activation_allowed(
        db, agent, new_role=agent.role, new_status="active"
    )
    if blockers:
        # Kept from Phase 1 Task 1C-6: the hub limit and profile checks name themselves.
        flash(request, "Activation refused: " + "; ".join(blockers), "error")
        return RedirectResponse(
            url=f"/manager/pis/{user_id}?activation_blocked=1", status_code=302
        )
    # D-07: the write is conditional on the row still being pending, so a suspend
    # (or any status change) committed after _pending_pi_agent read it is never
    # overwritten. approved_at/approved_by are stamped here because this is the
    # pending -> active transition (see activate_agent's docstring).
    activated = await db.execute(
        update(AgentRegistry)
        .where(AgentRegistry.id == agent.id, AgentRegistry.status == "pending")
        .values(status="active", approved_at=datetime.now(UTC), approved_by=current_user.id)
        .execution_options(synchronize_session=False)
    )
    if activated.rowcount != 1:
        await db.rollback()
        flash(
            request,
            "This agent changed while you were activating it (someone else acted first). "
            "Reload the page and check its status.",
            "error",
        )
        return RedirectResponse(url=f"/manager/pis/{user_id}", status_code=302)
    await db.commit()
    return RedirectResponse(
        url=f"/manager/pis/{user_id}?activated=1", status_code=302
    )
```

Add `from src.web.flash import flash` to the imports if Phase 1 has not. If `activate_agent` is no
longer referenced in `manager.py` (`grep -n "activate_agent(" src/routers/manager.py` prints only
`manager_activate_agent`), remove it from the `from src.services.agent_activation import …` line.
Append to the `manager_activate_agent` docstring: "The status write is ``UPDATE … WHERE status =
'pending'`` (D-07). The token check is ``token_for_agent_row`` (D-11), the predicate
/manager/slack-bots shows."

`templates/manager/pi_detail.html:49`: `{% if not target_user.agent.slack_bot_token %}` →
`{% if not has_bot_token %}`.

`src/services/agent_mute.py`: add `from src.services.slack_tokens import token_for_agent_row`
to the imports and change `        if not agent.slack_bot_token:` to
`        if not token_for_agent_row(agent):`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_slack_provisioning.py tests/integration/test_manager_pi_writes.py tests/integration/test_agent_activation_gate.py tests/integration/test_manager_views.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/manager.py src/services/agent_mute.py templates/manager/pi_detail.html tests/integration/test_manager_slack_provisioning.py
git commit -m "fix(webui-2): D-07 conditional activate; D-11 one bot-token predicate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-18: D-15 — an all-blank public-profile save does not create a profile

**Files:**
- Modify: `src/routers/agent_page.py` (`save_public_profile`, `:623-680`; new `_has_content`)
- Test: `tests/integration/test_public_profile_empty_save.py`

**Interfaces:**
- Produces: `src.routers.agent_page._has_content(value) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_public_profile_empty_save.py
"""D-15: a public-profile save with every field blank, for a PI who has no
ResearcherProfile yet, is refused instead of minting an empty profile."""

import pytest
from sqlalchemy import select

from src.models import ResearcherProfile
from src.services import profile_export
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_BLANK = {"research_summary": "", "techniques": "", "experimental_models": "",
          "disease_areas": "", "key_targets": "", "keywords": "", "profile_version": ""}


async def _profile_of(db, user):
    return (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )).scalar_one_or_none()


async def test_an_empty_save_with_no_profile_is_refused(client, db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save", data=_BLANK,
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/agent/{agent.agent_id}/public-profile/edit"
    assert await _profile_of(db_session, pi) is None
    page = await follow(client, r)
    assert "nothing to save yet" in page.text


async def test_a_non_empty_first_save_still_creates_the_profile(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save",
        data={**_BLANK, "research_summary": "First words"},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    profile = await _profile_of(db_session, pi)
    assert profile is not None and profile.research_summary == "First words"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_public_profile_empty_save.py -v`
Expected: `test_an_empty_save_with_no_profile_is_refused` FAILS (a profile row exists).

- [ ] **Step 3: Implement**

Add near the other module helpers in `src/routers/agent_page.py`:

```python
def _has_content(value) -> bool:
    """True for a non-blank string, or a list holding one (the tag fields post
    one hidden input per tag since Phase 1, D-16)."""
    if isinstance(value, str):
        return bool(value.strip())
    return any(isinstance(v, str) and v.strip() for v in (value or []))
```

In `save_public_profile`, bind the mapping currently passed inline as `form={…}` to a local
`fields` immediately before `note = impersonation_note(current_user)` and pass `form=fields` to
`apply_profile_edits`; then insert between the binding and `note = …`:

```python
    profile_exists = await db.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == agent.user_id)
    )
    if profile_exists is None and not any(_has_content(v) for v in fields.values()):
        # D-15: an all-blank first save would mint an empty ResearcherProfile; the
        # profile job, or a save with content, creates it instead.
        flash(request, "There is nothing to save yet — fill in at least one field.", "error")
        return RedirectResponse(
            url=f"/agent/{agent_id}/public-profile/edit", status_code=302
        )
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_public_profile_empty_save.py tests/integration/test_agent_page.py tests/integration/test_impersonation_guards.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/agent_page.py tests/integration/test_public_profile_empty_save.py
git commit -m "fix(webui-2): D-15 an all-blank public-profile save creates no profile

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-19: D-17 — a racing Add-PI of the same ORCID reports "already exists"

**Files:**
- Modify: `src/routers/manager.py:290-302` (`manager_create_pi` `except IntegrityError`)
- Test: `tests/integration/test_manager_pi_writes.py` (append)

**Interfaces:**
- Consumes: Postgres default name of the `users.orcid` unique constraint, `users_orcid_key`
  (`alembic/versions/0001_initial.py:32` declares `unique=True` inline; no naming convention
  exists in `src/` or `alembic/`). Pinned by the second test.

- [ ] **Step 1: Write the failing tests** (append; add `from sqlalchemy.exc import IntegrityError`
  to the imports)

```python
async def test_a_concurrent_add_of_the_same_orcid_reports_exists(client, db_session):
    manager = await _manager(db_session)
    dup = IntegrityError(
        "INSERT INTO users", None,
        Exception('duplicate key value violates unique constraint "users_orcid_key"'),
    )
    with patch(
        "src.routers.manager.find_or_create_pi_by_orcid", new=AsyncMock(side_effect=dup)
    ):
        r = await client.post(
            "/manager/pis", data={"orcid": "0000-0012-0000-0001"},
            headers=auth_headers(manager.id), follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis?error=exists"


async def test_the_users_orcid_unique_constraint_is_named_users_orcid_key(db_session):
    await factories.make_user(db_session, orcid="0000-0012-0000-0002")
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            await factories.make_user(db_session, orcid="0000-0012-0000-0002")
    assert "users_orcid_key" in str(exc.value.orig)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_pi_writes.py -v -k "orcid"`
Expected: `test_a_concurrent_add_of_the_same_orcid_reports_exists` FAILS (`…error=agent_conflict`);
the constraint-name test passes.

- [ ] **Step 3: Implement** — in `manager_create_pi` replace

```python
    except IntegrityError:
        # Two managers adding same-surname PIs can race the identity
        # derivation's SELECT-then-INSERT; the loser rolls the WHOLE creation
        # back (User + Job + agent together — the atomicity is the feature).
        await db.rollback()
```

with

```python
    except IntegrityError as exc:
        # Two managers adding same-surname PIs can race the identity
        # derivation's SELECT-then-INSERT; the loser rolls the WHOLE creation
        # back (User + Job + agent together — the atomicity is the feature).
        await db.rollback()
        if "users_orcid_key" in str(exc.orig):
            # Two adds of the SAME ORCID raced past the existence check (D-17):
            # that is "already exists", not an agent-identity clash.
            return RedirectResponse(url="/manager/pis?error=exists", status_code=302)
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_manager_pi_writes.py -v`
Expected: all pass (including `test_a_collision_race_rolls_back_cleanly_instead_of_500ing`).

- [ ] **Step 5: Commit**

```bash
git add src/routers/manager.py tests/integration/test_manager_pi_writes.py
git commit -m "fix(webui-2): D-17 a racing same-ORCID Add-PI reports already exists

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-20: D-09 — `saved` becomes a flash

**Files:**
- Modify: `src/routers/profile.py` (`profile_save` return, `flash` import)
- Modify: `src/routers/agent_page.py` (`view_public_profile` ctx `saved=` at `:583`; `save_public_profile` return at `:675-677`)
- Modify: `src/routers/manager.py` (`manager_edit_pi_profile`: add `request: Request` parameter; return at `:347`)
- Modify: `templates/agent/public_profile.html:25-29` (delete the `{% if saved %}` block)
- Modify: `tests/integration/test_onboarding_flow.py:708,759`, `tests/integration/test_profile_version_guard.py:82`,
  `tests/integration/test_manager_pi_writes.py:401`, `tests/integration/test_agent_page.py:645`
- Test: `tests/integration/test_query_flags_to_flash.py` (create)

**Interfaces:** Consumes `flash`, `follow`. Produces flash texts `"Profile saved."` and
`"Public profile saved and exported."`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_query_flags_to_flash.py
"""Status flags carried in query strings (`saved`, `slack_ok`, `msg`) were either
never rendered (D-09, D-10) or could be spoofed by any URL (A-14). They are flash
messages now: set by the handler that did the work, shown once on the next page."""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from src.services import profile_export
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_profile_save_flashes_instead_of_a_query_flag(client, db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        "/profile/save",
        data={"name": pi.name, "email": pi.email, "institution": "", "department": "",
              "research_summary": "Saved words"},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/profile"
    page = await follow(client, r)
    assert "Profile saved." in page.text


async def test_public_profile_save_flashes_and_the_query_flag_is_ignored(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save",
        data={"research_summary": "Pub words", "profile_version": str(profile.profile_version)},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/agent/{agent.agent_id}/public-profile"
    page = await follow(client, r)
    assert "Public profile saved and exported." in page.text
    spoof = await client.get(
        f"/agent/{agent.agent_id}/public-profile?saved=1", headers=auth_headers(pi.id)
    )
    assert "Public profile saved and exported." not in spoof.text


async def test_manager_profile_save_flashes(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "email": pi.email, "research_summary": "Manager words"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    page = await follow(client, r)
    assert "Profile saved." in page.text


async def test_an_admin_profile_save_flash_is_shown_once(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_profile(db_session, user=admin)
    r = await client.post(
        "/profile/save",
        data={"name": admin.name, "email": admin.email, "institution": "", "department": "",
              "research_summary": "Admin words"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    first = await follow(client, r)
    assert "Profile saved." in first.text
    again = await client.get("/profile", headers=auth_headers(admin.id))
    assert "Profile saved." not in again.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_query_flags_to_flash.py -v`
Expected: all four FAIL (locations carry `?saved=1`; no flash text).

- [ ] **Step 3: Implement**

`src/routers/profile.py`: add `from src.web.flash import flash` if absent; replace
`    return RedirectResponse(url="/profile?saved=1", status_code=302)` with

```python
    flash(request, "Profile saved.", "success")
    return RedirectResponse(url="/profile", status_code=302)
```

`src/routers/agent_page.py`: in `view_public_profile` delete the context line
`            saved=request.query_params.get("saved"),`; in `save_public_profile` replace

```python
    return RedirectResponse(
        url=f"/agent/{agent_id}/public-profile?saved=1", status_code=302
    )
```

with

```python
    flash(request, "Public profile saved and exported.", "success")
    return RedirectResponse(url=f"/agent/{agent_id}/public-profile", status_code=302)
```

`src/routers/manager.py` `manager_edit_pi_profile`: add `request: Request,` as the second
parameter (after `user_id: uuid.UUID,`) only if it is not already there (Phase 1 Task 1C-9 adds it;
a second copy is a SyntaxError; plan audit Q2-07), and replace
`    return RedirectResponse(url=f"/manager/pis/{user_id}?saved=1", status_code=302)` with

```python
    flash(request, "Profile saved.", "success")
    return RedirectResponse(url=f"/manager/pis/{user_id}", status_code=302)
```

`templates/agent/public_profile.html`: delete

```html
    {% if saved %}
    <div class="bg-green-50 border border-green-200 rounded-lg p-3 mb-6 text-sm text-green-700">
        Public profile saved and exported.
    </div>
    {% endif %}
```

Existing assertions (exact edits):
- `tests/integration/test_onboarding_flow.py:708`: `r.headers["location"] == "/profile?saved=1"` → `r.headers["location"] == "/profile"`.
- `tests/integration/test_onboarding_flow.py:759`: `assert r.headers["location"] == "/profile?saved=1"` → `assert r.headers["location"] == "/profile"`.
- `tests/integration/test_profile_version_guard.py:82`: `assert resp.headers["location"] == "/profile?saved=1"` → `assert resp.headers["location"] == "/profile"`.
- `tests/integration/test_manager_pi_writes.py:401`: `assert r.status_code == 302 and "saved=1" in r.headers["location"]` → `assert r.status_code == 302 and r.headers["location"] == f"/manager/pis/{pi.id}"`.
- `tests/integration/test_agent_page.py:645`: `assert r.status_code == 302 and "saved=1" in r.headers["location"]` → `assert r.status_code == 302 and r.headers["location"].endswith("/public-profile")`.

- [ ] **Step 3b: `error=no_agent` gets its own message (pre-audit D-09; assembly audit PX-21).**
  `_manager_set_mute` (`src/routers/manager.py`, the `agent is None` branch) redirects with
  `?error=no_agent`, which `templates/manager/pi_detail.html` shows only as the generic "Something
  went wrong saving changes." The code is a fixed value mapped to fixed text (not spoofable text),
  so it stays a query code: in the pi_detail error `<p>` add, before `{% else %}`,
  `{% elif request.query_params.get('error') == 'no_agent' %}This PI has no lab agent yet, so there is nothing to mute or unmute.`
  (if Phase 1 moved this block to flash, add the same text as the mapping for `no_agent` there).
  Append to this task's test file:

```python
async def test_muting_a_pi_without_an_agent_explains_why(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await db_session.commit()
    r = await client.get(f"/manager/pis/{pi.id}?error=no_agent", headers=auth_headers(manager.id))
    assert "This PI has no lab agent yet, so there is nothing to mute or unmute." in r.text
    assert "Something went wrong saving changes." not in r.text
```

  (import `USER_ROLE_MANAGER` from `src.models` in the test file's top import block if absent).

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_query_flags_to_flash.py tests/integration/test_onboarding_flow.py tests/integration/test_profile_version_guard.py tests/integration/test_manager_pi_writes.py tests/integration/test_agent_page.py -v`
Expected: all pass. Then `grep -rn "saved=1" src templates tests` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add src/routers/profile.py src/routers/agent_page.py src/routers/manager.py templates/agent/public_profile.html tests/integration/test_query_flags_to_flash.py tests/integration/test_onboarding_flow.py tests/integration/test_profile_version_guard.py tests/integration/test_manager_pi_writes.py tests/integration/test_agent_page.py
git commit -m "fix(webui-2): D-09 profile saves report through flash, not ?saved=1

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-21: D-10 `slack_ok` becomes a flash; C-03 callback errors reach /admin/agents

**Files:**
- Modify: `src/routers/admin/agents.py` (`admin_agent_detail` ctx `slack_ok=` at `:171`;
  `admin_provision_slack_callback` `surface_error` `:415-419` and success redirects `:433-441`)
- Modify: `src/routers/manager.py` (`manager_pi_detail` ctx `slack_ok=` at `:243`; `manager_slack_bots` ctx `slack_ok=` at `:632`)
- Modify: `templates/admin/agent_detail.html:18-22`, `templates/manager/pi_detail.html:66-80` (comment + `slack_ok` block), `templates/manager/slack_bots.html:19-23`
- Modify: `tests/integration/test_manager_slack_provisioning.py:158,184,336,404`; `tests/e2e/test_browser_flows.py:223`; `tests/e2e/README.md:166`
- Test: `tests/integration/test_query_flags_to_flash.py` (append)

**Interfaces:** Produces flash texts `"Slack bot provisioned — the token is saved on the agent (it is not shown)."`,
`"Slack bot installed — the token is saved on this PI's agent. Activate the agent to bring it live."`,
`"Slack provisioning failed: <message>"`.

- [ ] **Step 1: Write the failing tests** (append; add `SlackAppProvision` to the `src.models` import and
  `from tests.integration.test_manager_slack_provisioning import _pending_pi` to the imports)

```python
async def _provision(db, agent, initiator, state):
    db.add(SlackAppProvision(
        agent_registry_id=agent.id, state=state, client_id="cid", client_secret="secret",
        initiated_by_user_id=initiator.id,
    ))
    await db.flush()


async def test_the_callback_flashes_success_for_a_manager(client, db_session, monkeypatch):
    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", lambda *a, **k: "xoxb-f1")
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi, agent = await _pending_pi(db_session, agent_id="flashmgr")
    await _provision(db_session, agent, manager, "flash-1")
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=flash-1",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    page = await follow(client, r)
    assert "Slack bot installed" in page.text


async def test_the_callback_flashes_success_for_an_admin(client, db_session, monkeypatch):
    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", lambda *a, **k: "xoxb-f2")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    _pi, agent = await _pending_pi(db_session, agent_id="flashadm")
    await _provision(db_session, agent, admin, "flash-2")
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=flash-2",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/agents/{agent.id}"
    page = await follow(client, r)
    assert "Slack bot provisioned" in page.text


async def test_a_slack_ok_query_flag_renders_nothing(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="pending")
    r = await client.get(f"/admin/agents/{agent.id}?slack_ok=1", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert "Slack bot provisioned" not in r.text


async def test_a_callback_error_reaches_the_admin_agents_page(client, db_session):
    """C-03: the callback's admin-surface error used to land on
    /admin/agents?slack_error=…, which never rendered it."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(
        "/admin/agents/slack/callback?error=access_denied",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/admin/agents"
    page = await follow(client, r)
    assert "Slack returned: access_denied" in page.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_query_flags_to_flash.py -v -k "callback or slack_ok"`
Expected: the two success tests FAIL (`?slack_ok=1` in the location); `test_a_slack_ok_query_flag_renders_nothing`
FAILS (banner rendered). `test_a_callback_error_reaches_the_admin_agents_page` FAILS unless
Phase 1 already moved this `surface_error` onto flash (spec §6.9 names `slack_error`); either way
Step 3's `surface_error` is the required end state.

- [ ] **Step 3: Implement**

`src/routers/admin/agents.py` `admin_provision_slack_callback`: replace the nested
`surface_error` with

```python
    def surface_error(msg: str) -> RedirectResponse:
        flash(request, f"Slack provisioning failed: {msg[:200]}", "error")
        return RedirectResponse(
            url="/admin/agents" if is_admin else "/manager/pis", status_code=302
        )
```

and replace the three success returns (from `    if is_admin:` to the end of the function) with

```python
    if is_admin:
        flash(
            request,
            "Slack bot provisioned — the token is saved on the agent (it is not shown).",
            "success",
        )
        return RedirectResponse(url=f"/admin/agents/{agent.id}", status_code=302)
    flash(
        request,
        "Slack bot installed — the token is saved on this PI's agent. "
        "Activate the agent to bring it live.",
        "success",
    )
    if agent.user_id is None:
        return RedirectResponse(url="/manager/pis", status_code=302)
    return RedirectResponse(url=f"/manager/pis/{agent.user_id}", status_code=302)
```

Delete the context lines `            slack_ok=request.query_params.get("slack_ok"),` in
`admin_agent_detail` (`src/routers/admin/agents.py`), `manager_pi_detail` and `manager_slack_bots`
(`src/routers/manager.py`).

Templates — delete these blocks:
- `templates/admin/agent_detail.html`: the `{% if slack_ok %} … ✅ Slack bot provisioned … {% endif %}` block.
- `templates/manager/slack_bots.html`: the `{% if slack_ok %} … ✅ Slack bot installed … {% endif %}` block.
- `templates/manager/pi_detail.html`: the `{% if slack_ok %} … ✅ Slack bot installed … {% endif %}` block inside
  `{% if effective_user.is_staff %}`, and in the Jinja comment above it replace "The four status banners are
  STAFF-ONLY" with "The status banners below are STAFF-ONLY" and "could summon "Slack bot installed" or
  "Agent activated"" with "could summon "Agent activated"".

Existing assertions:
- `tests/integration/test_manager_slack_provisioning.py:158` and `:336`:
  `== f"/manager/pis/{pi.id}?slack_ok=1"` → `== f"/manager/pis/{pi.id}"`.
- `tests/integration/test_manager_slack_provisioning.py:184` and `:404`:
  `== f"/admin/agents/{agent.id}?slack_ok=1"` → `== f"/admin/agents/{agent.id}"`.
- `tests/e2e/test_browser_flows.py:223`: `("land", "/admin/agents/{id}?slack_ok=1",` → `("land", "/admin/agents/{id}",`.
- `tests/e2e/README.md:166`: `` `/admin/agents/<id>?slack_ok=1`. `` → `` `/admin/agents/<id>`, where the "Slack bot provisioned" message is a one-time flash. ``

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_query_flags_to_flash.py tests/integration/test_manager_slack_provisioning.py tests/integration/test_admin_agent_form.py -v`
Expected: all pass. Then `grep -rn "slack_ok" src templates tests` prints only the reviewer
banner test's query string in `tests/integration/test_manager_slack_provisioning.py`.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/agents.py src/routers/manager.py templates/admin/agent_detail.html templates/manager/pi_detail.html templates/manager/slack_bots.html tests/integration/test_query_flags_to_flash.py tests/integration/test_manager_slack_provisioning.py tests/e2e/test_browser_flows.py tests/e2e/README.md
git commit -m "fix(webui-2): D-10 slack_ok and C-03 callback errors report through flash

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-22: A-14 (remaining) — simulation `msg` becomes a flash

**Files:**
- Modify: `src/routers/admin/simulation.py` (`_simulation_context` `:87-96` param, `:174` key; GET route `:198`;
  returns at `:276`, `:326`, `:371`, `:430`, `:468`, `:486`)
- Modify: `src/routers/admin/runs.py:82`
- Modify: `templates/admin/simulation.html:97-99`, `templates/admin/activity_detail.html:18`
- Modify: `tests/integration/test_admin_simulation_liveness.py:34`
- Test: `tests/integration/test_query_flags_to_flash.py` (append)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests** (append; add
  `from tests.integration.test_finalize_run_route import _stopped_run` to the imports)

```python
async def test_stop_flashes_and_a_msg_query_flag_renders_nothing(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _alive(db):
        return True

    monkeypatch.setattr(sim_routes, "engine_alive", _alive)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        "/admin/simulation/stop", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/admin/simulation"
    page = await follow(client, r)
    assert "Stop requested." in page.text
    spoof = await client.get(
        "/admin/simulation?msg=Spoofed-banner-text", headers=auth_headers(admin.id)
    )
    assert "Spoofed-banner-text" not in spoof.text


async def test_finalize_requested_flashes_on_the_run_page(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    r = await client.post(
        "/admin/simulation/finalize-run",
        data={"run_id": str(run.id), "confirm_run": str(run.id)[:8]},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/activity/{run.id}"
    page = await follow(client, r)
    assert "Finalize run requested." in page.text
    spoof = await client.get(
        f"/admin/activity/{run.id}?msg=Spoofed-run-text", headers=auth_headers(admin.id)
    )
    assert "Spoofed-run-text" not in spoof.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_query_flags_to_flash.py -v -k "stop_flashes or finalize_requested"`
Expected: both FAIL (`?msg=` in the location).

- [ ] **Step 3: Implement**

`src/routers/admin/simulation.py` (add `from src.web.flash import flash` if absent):
- `_simulation_context`: delete the parameter `    msg: str | None = None,` and the dict entry `        msg=msg,`.
- GET `/admin/simulation` handler: delete the argument `        msg=request.query_params.get("msg"),`.
- Returns:

| Route | Before | After |
|---|---|---|
| start | `return RedirectResponse(\n        url=f"/admin/simulation?msg={quote('Start requested.')}", status_code=302\n    )` | `flash(request, "Start requested.", "success")`<br>`return RedirectResponse(url="/admin/simulation", status_code=302)` |
| finalize | `return RedirectResponse(\n        url=f"/admin/activity/{run_id}?msg={quote('Finalize run requested.')}", status_code=302,\n    )` | `flash(request, "Finalize run requested.", "success")`<br>`return RedirectResponse(url=f"/admin/activity/{run_id}", status_code=302)` |
| stop | `return RedirectResponse(url=f"/admin/simulation?msg={quote(message)}", status_code=302)` | `flash(request, message, "success")`<br>`return RedirectResponse(url="/admin/simulation", status_code=302)` |
| announce-settings | `return RedirectResponse(url=f"/admin/simulation?msg={quote(msg)}", status_code=302)` | `flash(request, msg, "success")`<br>`return RedirectResponse(url="/admin/simulation", status_code=302)` |
| template reset | `return RedirectResponse(\n            url=f"/admin/simulation?msg={quote('Template reset to file default.')}",\n            status_code=302,\n        )` | `flash(request, "Template reset to file default.", "success")`<br>`return RedirectResponse(url="/admin/simulation", status_code=302)` |
| template save | `return RedirectResponse(url=f"/admin/simulation?msg={quote('Template saved.')}", status_code=302)` | `flash(request, "Template saved.", "success")`<br>`return RedirectResponse(url="/admin/simulation", status_code=302)` |

- If `quote` is no longer referenced in the module (`grep -n "quote(" src/routers/admin/simulation.py`
  prints nothing), delete `from urllib.parse import quote`.

`src/routers/admin/runs.py:82`: delete `            msg=request.query_params.get("msg"),`.

`templates/admin/simulation.html`: delete

```html
{% if msg %}
<div class="mb-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-base text-green-800">{{ msg }}</div>
{% endif %}
```

`templates/admin/activity_detail.html`: delete the line
`    {% if msg %}<div class="mb-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-base text-green-800">{{ msg }}</div>{% endif %}`.

If the Phase 0 refresh script in `templates/admin/simulation.html` looks the `msg` banner up to
remove it, it must already tolerate its absence (it is absent whenever no `msg` was passed);
leave that script unchanged. Its `history.replaceState` removal of `msg` from the URL stays
harmless.

`tests/integration/test_admin_simulation_liveness.py:34`: `assert "msg=" in resp.headers["location"]` →
`assert resp.headers["location"] == "/admin/simulation"`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_query_flags_to_flash.py tests/integration/test_admin_simulation_liveness.py tests/integration/test_admin_simulation_page.py tests/integration/test_finalize_run_route.py tests/integration/test_simulation_page_queries.py -v`
Expected: all pass. Then `grep -rn "msg=" src/routers templates/admin/simulation.html templates/admin/activity_detail.html`
prints nothing.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/simulation.py src/routers/admin/runs.py templates/admin/simulation.html templates/admin/activity_detail.html tests/integration/test_query_flags_to_flash.py tests/integration/test_admin_simulation_liveness.py
git commit -m "fix(webui-2): simulation and run messages report through flash, not ?msg=

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-22b: A-14 (remaining) — `notice`, `spoke_error`, `role_error` become flashes

Added by the assembly audit (PX-10): Phase 1 Task 1C-3 states that `notice=` is unchanged and
leaves `spoke_error=` and `role_error=` to Phase 2, and Task 2A-22 moves only the simulation
`msg`. These three still render query-string text inside trusted banners, so A-14 is not
closed until this task lands.

**Files:**
- Modify: `src/routers/admin/cohorts.py` — the three `notice=` redirects (`admin_cohort_topology_save`'s
  final redirect, `admin_ensure_star_spokes`'s `url = f"/admin/cohorts?notice={quote(notice)}"`,
  `admin_cohort_delete`'s `?notice=Deleted+cohort+{name}`) and the three
  `notice=request.query_params.get("notice"),` context keywords (cohorts list, topology, detail)
- Modify: `src/routers/admin/agents.py` — the three `?spoke_error=` redirects in
  `admin_ensure_agent_spoke`, the `?role_error=Unknown+role` redirect in `admin_set_agent_role`,
  and the `role_error=` / `spoke_error=` context keywords in `admin_agent_detail`
- Modify: `templates/admin/cohorts.html`, `templates/admin/cohort_topology.html`,
  `templates/admin/cohort_detail.html` (delete the `{% if notice %}…{% endif %}` banner),
  `templates/admin/agent_detail.html` (delete the `{% if role_error %}` and `{% if spoke_error %}` blocks)
- Modify: `tests/integration/test_cohort_admin.py` (the assertions on `notice=` in a `Location`,
  including Task 1C-7's `"0+added,+0+removed"` assertion), `tests/e2e/test_browser_flows.py:128`
  (the flow description string)
- Test: Create `tests/integration/test_admin_notice_flags_to_flash.py`

**Interfaces:**
- Consumes: `flash(request, text, kind)` with kinds `info`, `success`, `error` (`src/web/flash.py`,
  Task 1C-1); `tests/flash_support.py` `session_flashes(response)` (Task 1C-1);
  `tests.integration.test_manager_access.auth_headers`; `tests.factories`.
- Produces: no query parameter named `notice`, `spoke_error` or `role_error` is read by any handler.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_admin_notice_flags_to_flash.py`:

```python
"""A-14 remainder: cohort and agent notices travel as session flashes, never as query text."""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, Cohort
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


@pytest.mark.parametrize("path", ["/admin/cohorts", "/admin/cohorts/topology"])
async def test_cohort_pages_ignore_a_notice_in_the_query(client, db_session, path):
    admin = await _admin(db_session)
    await db_session.commit()
    r = await client.get(f"{path}?notice=SPOOFED-NOTICE", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert "SPOOFED-NOTICE" not in r.text


async def test_agent_detail_ignores_role_and_spoke_errors_in_the_query(client, db_session):
    admin = await _admin(db_session)
    agent = await factories.make_agent(db_session)
    await db_session.commit()
    r = await client.get(
        f"/admin/agents/{agent.id}?role_error=SPOOFED-ROLE&spoke_error=SPOOFED-SPOKE",
        headers=auth_headers(admin.id),
    )
    assert r.status_code == 200
    assert "SPOOFED-ROLE" not in r.text and "SPOOFED-SPOKE" not in r.text


async def test_deleting_a_cohort_flashes_instead_of_a_notice_param(client, db_session):
    admin = await _admin(db_session)
    cohort = Cohort(name="flashdelete", created_by=admin.id)
    db_session.add(cohort)
    await db_session.commit()
    r = await client.post(f"/admin/cohorts/{cohort.id}/delete", headers=auth_headers(admin.id),
                          follow_redirects=False)
    assert r.status_code == 302
    assert "notice=" not in r.headers["location"]
    assert {"text": "Deleted cohort flashdelete", "kind": "success"} in session_flashes(r)
    assert (await db_session.execute(select(Cohort).where(Cohort.id == cohort.id))).first() is None


async def test_an_unknown_role_flashes_an_error(client, db_session):
    admin = await _admin(db_session)
    agent = await factories.make_agent(db_session)
    await db_session.commit()
    r = await client.post(f"/admin/agents/{agent.id}/role", data={"role": "no-such-role"},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302
    assert "role_error" not in r.headers["location"]
    assert {"text": "Role not changed: unknown role", "kind": "error"} in session_flashes(r)
```

Before running, read `admin_set_agent_role`'s form parameter name in `src/routers/admin/agents.py`
(today `role: str = Form(...)`); if Phase 1 renamed it, use that name in the last test.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_notice_flags_to_flash.py -v`
Expected: FAIL — the spoofed text appears in each page; the delete and role redirects carry
`notice=` / `role_error=` and set no flash.

- [ ] **Step 3: Implement the producers.** In `src/routers/admin/cohorts.py` (add
`from src.web.flash import flash` to the imports if Phase 1 did not):
  - `admin_cohort_topology_save`: replace
    ```python
        return RedirectResponse(
            url=f"/admin/cohorts/topology?notice={added}+added,+{removed}+removed",
            status_code=302,
        )
    ```
    with
    ```python
        flash(request, f"{added} added, {removed} removed", "success")
        return RedirectResponse(url="/admin/cohorts/topology", status_code=302)
    ```
  - `admin_ensure_star_spokes`: replace `url = f"/admin/cohorts?notice={quote(notice)}"` with
    `flash(request, notice, "success")` followed by `url = "/admin/cohorts"`, and keep the
    anomalies line as Phase 1 left it (an `error` flash after Task 1C-3, or `&error=`; if it is
    still `url += "&error=" + ...`, change it to `flash(request, "; ".join(report.anomalies)[:300], "error")`).
  - `admin_cohort_delete`: replace the `?notice=Deleted+cohort+{name}` redirect with
    `flash(request, f"Deleted cohort {name}", "success")` and
    `return RedirectResponse(url="/admin/cohorts", status_code=302)`.
  - Delete the three `notice=request.query_params.get("notice"),` context keywords.

  In `src/routers/admin/agents.py` (add `from src.web.flash import flash` if absent):
  - in `admin_ensure_agent_spoke`, each of the three `RedirectResponse(url=f"/admin/agents/{agent_id}?spoke_error=" + quote(MESSAGE), ...)`
    becomes `flash(request, "Star spoke: " + MESSAGE, "error")` then
    `return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)`, where MESSAGE is
    the expression each one quotes today (`"Only pi_lab agents have star spokes."`,
    `str(exc)[:200]`, `"; ".join(report.anomalies)[:300]`); add a `request: Request` parameter to
    the handler if it has none;
  - in `admin_set_agent_role`, the unknown-role redirect becomes
    `flash(request, "Role not changed: unknown role", "error")` then
    `return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)`;
  - delete the `role_error=` and `spoke_error=` context keywords in `admin_agent_detail`.

- [ ] **Step 4: Delete the consumers in the templates.** Remove the `{% if notice %}…{% endif %}`
  banner from `templates/admin/cohorts.html`, `templates/admin/cohort_topology.html` and
  `templates/admin/cohort_detail.html`, and the `{% if role_error %}…{% endif %}` and
  `{% if spoke_error %}…{% endif %}` blocks from `templates/admin/agent_detail.html`
  (`base.html`'s flash block, Task 1C-1, now shows these messages).

- [ ] **Step 5: Update the tests that pinned the query strings.** In
  `tests/integration/test_cohort_admin.py`: every `assert ... "notice=..." in r.headers["location"]`
  becomes an assertion on `session_flashes(r)` with the same text (e.g.
  `{"text": "Deleted cohort realdelete", "kind": "success"} in session_flashes(r)`;
  Task 1C-7's `"0+added,+0+removed"` becomes `{"text": "0 added, 0 removed", "kind": "success"}`),
  importing `from tests.flash_support import session_flashes`. In
  `tests/e2e/test_browser_flows.py:128` change the description `"302s with ?notice=2+added,+0+removed"`
  to `"302s; flashes 2 added, 0 removed"`.

- [ ] **Step 6: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_admin_notice_flags_to_flash.py tests/integration/test_cohort_admin.py tests/integration/test_admin_agent_form.py tests/e2e/test_browser_flows.py tests/unit/test_reachability.py -v`
Expected: PASS. Then `grep -rn 'query_params.get("notice"\|query_params.get("spoke_error"\|query_params.get("role_error"' src` prints nothing.

- [ ] **Step 7: Commit**

```bash
git add src/routers/admin/cohorts.py src/routers/admin/agents.py templates/admin/cohorts.html \
  templates/admin/cohort_topology.html templates/admin/cohort_detail.html templates/admin/agent_detail.html \
  tests/integration/test_admin_notice_flags_to_flash.py tests/integration/test_cohort_admin.py \
  tests/e2e/test_browser_flows.py
git commit -m "fix(webui-2): cohort and agent notices are flashes, not query text (A-14 remainder)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-23: B-03 — quick score flashes "Moved to Reviewed" and anchors the next card

**Files:**
- Modify: `src/routers/reviews.py:116-171` (`_assessments_redirect`), `submit_review_feedback` (as left by 2A-12), new `_optional_uuid`
- Modify: `templates/admin/_assessments_body.html:289` (loop head), `:622` (after `{{ qs_filters() }}`)
- Test: `tests/integration/test_reviews_router.py` (append)

**Interfaces:**
- Produces: `_assessments_redirect(..., anchor_id: uuid.UUID | None = None, no_anchor: bool = False)`;
  form field `next_id` on the quick-score form (`""` on the last card); flash `"Moved to Reviewed."`.

- [ ] **Step 1: Write the failing tests** (append)

```python
async def test_quick_score_on_the_unreviewed_tab_anchors_the_next_card(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    next_id = uuid.uuid4()
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "log_only",
              "surface": "manager-list", "review": "unreviewed", "next_id": str(next_id)},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/assessments#a-{next_id}"
    page = await follow(client, r)
    assert "Moved to Reviewed." in page.text


async def test_quick_score_on_the_last_unreviewed_card_has_no_anchor(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "log_only",
              "surface": "manager-list", "next_id": ""},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/manager/assessments"


async def test_quick_score_on_the_all_tab_keeps_the_scored_card_anchor(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "log_only",
              "surface": "manager-list", "review": "all", "next_id": str(uuid.uuid4())},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/assessments?review=all#a-{assessment.id}"


async def test_each_quick_score_form_names_the_following_card(client, db_session):
    import re

    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    first = await _seed_assessment(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=first.simulation_run_id, agent_id="blackbird", channel_name="c2",
    ))
    await db_session.flush()
    page = await client.get(
        f"/manager/assessments?run_id={first.simulation_run_id}&review=all",
        headers=auth_headers(manager.id),
    )
    cards = re.findall(r'id="a-([0-9a-f-]{36})"', page.text)
    nexts = re.findall(r'name="next_id" value="([^"]*)"', page.text)
    assert len(cards) == 2
    assert nexts == cards[1:] + [""]
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py -v -k "quick_score or following_card"`
Expected: the first two FAIL (location still `#a-<scored id>`); the `all`-tab test passes; the
template test FAILS (`nexts == []`).

- [ ] **Step 3: Implement**

`src/routers/reviews.py` — add after `_parse_assignee_id`:

```python
def _optional_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        return None
```

In `_assessments_redirect`, add the keyword parameters
`    anchor_id: uuid.UUID | None = None,` and `    no_anchor: bool = False,` after
`review: str | None = None,`, and replace `        fragment = f"#a-{assessment_id}"` with

```python
        fragment = "" if no_anchor else f"#a-{anchor_id or assessment_id}"
```

Append to its docstring: "``anchor_id`` replaces the scored card as the fragment target, and
``no_anchor`` drops the fragment: the feedback path uses them when the scored card has just left
the Unreviewed tab (B-03)."

In `submit_review_feedback` replace the final `return _assessments_redirect(...)` with:

```python
    # B-03: scoring from the Unreviewed tab moves the card to Reviewed, so its own
    # anchor is gone from the page the reader lands on. The list form posts
    # `next_id` (the following card, "" on the last); land there instead. A post
    # without the field (the detail page, older pages) keeps the old anchor.
    next_raw = form.get("next_id")
    moved_out = (
        surface in _LIST_SURFACES
        and isinstance(next_raw, str)
        and (_form_str(form, "review") or ASSESSMENT_REVIEW_DEFAULT) == ASSESSMENT_REVIEW_DEFAULT
    )
    anchor_id = _optional_uuid(next_raw) if moved_out else None
    # A refused duplicate (Task 2A-12) already flashed its own note; one message only
    # (plan audit Q2-17).
    if moved_out and not duplicate:
        flash(request, "Moved to Reviewed.", "success")
    return _assessments_redirect(
        surface, current_user, assessment_id,
        run_id=_form_str(form, "run_id"),
        sort=_form_str(form, "sort"),
        lab=_form_str(form, "lab"),
        review=_form_str(form, "review"),
        anchor_id=anchor_id,
        no_anchor=moved_out and anchor_id is None,
    )
```

`templates/admin/_assessments_body.html`: directly after `{% for a in assessments %}` (line 289)
insert `    {% set next_card = loop.nextitem %}`; inside the quick-score form, directly after
`{{ qs_filters() }}`, insert

```html
                <input type="hidden" name="next_id" value="{{ next_card.id if next_card else '' }}">
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_reviews_router.py tests/integration/test_assessment_queue_controls.py tests/integration/test_assessment_list_chrome.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/reviews.py templates/admin/_assessments_body.html tests/integration/test_reviews_router.py
git commit -m "fix(webui-2): B-03 quick score lands on the next card and says Moved to Reviewed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-24: B-13 — an unknown `run_id` falls back to the current run

**Files:**
- Modify: `src/services/directory.py:355-369` (`_resolve_run_selection`)
- Test: `tests/unit/test_run_selection.py`

**Interfaces:** `_resolve_run_selection(runs, run_id) -> tuple[bool, uuid.UUID | str | None]` (unchanged signature).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_run_selection.py
"""B-13: a run_id that parses but names no run falls back to the newest run, so
the selector and the list agree instead of showing an empty list under the
wrong option."""

import uuid

from src.models import SimulationRun
from src.services.directory import _resolve_run_selection


def _runs(n):
    return [SimulationRun(id=uuid.uuid4()) for _ in range(n)]


def test_an_unknown_run_id_falls_back_to_the_newest_run():
    runs = _runs(2)
    assert _resolve_run_selection(runs, str(uuid.uuid4())) == (False, runs[0].id)


def test_a_known_older_run_id_is_kept():
    runs = _runs(2)
    assert _resolve_run_selection(runs, str(runs[1].id)) == (False, runs[1].id)


def test_all_is_kept():
    assert _resolve_run_selection(_runs(1), "all") == (True, "all")


def test_garbage_falls_back_to_the_newest_run():
    runs = _runs(1)
    assert _resolve_run_selection(runs, "nope") == (False, runs[0].id)


def test_no_runs_selects_nothing():
    assert _resolve_run_selection([], str(uuid.uuid4())) == (False, None)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_run_selection.py -v`
Expected: `test_an_unknown_run_id_falls_back_to_the_newest_run` and `test_no_runs_selects_nothing` FAIL
(the unknown UUID is returned).

- [ ] **Step 3: Implement** — replace the body of `_resolve_run_selection` with:

```python
    show_all_runs = run_id == "all"
    selected_run_id: uuid.UUID | str | None = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            parsed = uuid.UUID(run_id)
        except ValueError:
            parsed = None
        # B-13: a well-formed id that names no run (a stale bookmark, a purged
        # run) is treated like no id at all.
        if parsed is not None and any(r.id == parsed for r in runs):
            selected_run_id = parsed
    if not selected_run_id and runs:
        selected_run_id = runs[0].id
    return show_all_runs, selected_run_id
```

and its docstring with: `"""``(show_all_runs, selected_run_id)``: ``run_id == "all"`` selects every run;
an absent or unparseable id, or one naming no run in ``runs``, falls back to the newest run
(``runs[0]``)."""`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_run_selection.py tests/integration/test_opportunity_assessment_persistence.py tests/integration/test_directory_service.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/directory.py tests/unit/test_run_selection.py
git commit -m "fix(webui-2): B-13 an unknown run_id falls back to the newest run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-25: D-12 — no suggestion status buttons while impersonating

**Files:**
- Modify: `templates/manager/prompt_suggestion_detail.html:51-63`
- Test: `tests/integration/test_prompt_suggestions_page.py` (append)

**Interfaces:** Consumes `impersonation_banner` (manager `_template_context`).

- [ ] **Step 1: Write the failing test** (append; add
  `from tests.integration._webui_helpers import impersonation_headers` to the imports)

```python
async def test_status_buttons_are_hidden_while_impersonating(client, db_session):
    """D-12: POST /reviews/suggestions/{id}/status refuses impersonation, so the
    page must not offer buttons that 403."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    s = await _seed_suggestion(db_session)
    worn = await client.get(
        f"/manager/prompt-suggestions/{s.id}", headers=impersonation_headers(admin.id, manager.id)
    )
    assert worn.status_code == 200
    assert "Mark Implemented" not in worn.text
    assert "Status changes are disabled while impersonating." in worn.text
    own = await client.get(f"/manager/prompt-suggestions/{s.id}", headers=auth_headers(manager.id))
    assert "Mark Implemented" in own.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_prompt_suggestions_page.py -v -k impersonating`
Expected: FAIL (`"Mark Implemented"` present).

- [ ] **Step 3: Implement** — in the form whose `action` is `/reviews/suggestions/{{ suggestion.id }}/status`,
  insert, on their own lines, immediately BEFORE the line containing `name="action" value="open"`:

```html
    {% if impersonation_banner %}
    <span class="text-xs text-gray-600">Status changes are disabled while impersonating.</span>
    {% else %}
```

  and immediately AFTER the line containing `>Mark Implemented</button>`:

```html
    {% endif %}
```

  The three button tags themselves, and the provenance `{% if suggestion.status_set_by_name %}` span after
  them, are left exactly as Phase 1 left them.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_prompt_suggestions_page.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add templates/manager/prompt_suggestion_detail.html tests/integration/test_prompt_suggestions_page.py
git commit -m "fix(webui-2): D-12 hide suggestion status buttons while impersonating

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-26: D-14 — thread fetch rejects redirected or non-HTML responses

**Files:**
- Modify: `templates/agent/conversations.html:76-91` (the `fetch(...)` chain in the nonce'd inline script)
- Test: `tests/unit/test_thread_fetch_js.py`; browser journey in Task 2A-34

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_thread_fetch_js.py
"""D-14: the conversations page's thread expander must not inject the login page
(a redirected fetch) or any non-HTML body, and must mark the link expanded when it
shows an error. Source-level pins; journey_thread_fetch_after_session_expiry runs it."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "templates" / "agent" / "conversations.html").read_text(encoding="utf-8")


def _catch_block() -> str:
    start = SRC.index(".catch(function(err)")
    return SRC[start:SRC.index(".finally(", start)]


def test_a_redirected_response_is_rejected():
    assert "if (r.redirected) { throw new Error('session'); }" in SRC


def test_a_non_html_response_is_rejected():
    assert "type.indexOf('text/html') !== 0" in SRC


def test_the_error_path_marks_the_link_expanded():
    assert "link.setAttribute('aria-expanded', 'true');" in _catch_block()


def test_the_error_path_says_the_session_ended():
    assert "Your session has ended — reload the page." in _catch_block()


def test_the_error_text_is_set_as_text():
    block = _catch_block()
    assert "p.textContent = text;" in block
    assert "innerHTML = '<" not in block
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_thread_fetch_js.py -v`
Expected: FAIL (`.catch(function(err)` not found; no redirect check).

- [ ] **Step 3: Implement** — replace

```js
                .then(function(r) {
                    if (!r.ok) { throw new Error('HTTP ' + r.status); }
                    return r.text();
                })
```

with

```js
                .then(function(r) {
                    // D-14: an expired session redirects the fragment fetch to the
                    // login page; never inject that, or any non-HTML body.
                    var type = r.headers.get('content-type') || '';
                    if (r.redirected) { throw new Error('session'); }
                    if (!r.ok || type.indexOf('text/html') !== 0) { throw new Error('HTTP ' + r.status); }
                    return r.text();
                })
```

and replace

```js
                .catch(function() {
                    panel.innerHTML = '<p class="mt-2 pl-3 text-xs text-red-600">Could not load replies.</p>';
                    panel.classList.remove('hidden');
                })
```

with

```js
                .catch(function(err) {
                    var text = (err && err.message === 'session')
                        ? 'Your session has ended — reload the page.'
                        : 'Could not load replies.';
                    var p = document.createElement('p');
                    p.className = 'mt-2 pl-3 text-xs text-red-600';
                    p.setAttribute('role', 'alert');
                    p.textContent = text;
                    panel.replaceChildren(p);
                    panel.classList.remove('hidden');
                    link.setAttribute('aria-expanded', 'true');
                })
```

(`panel.dataset.loaded` stays unset on error, so the next click retries.)

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_thread_fetch_js.py tests/integration/test_conversation_feed.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add templates/agent/conversations.html tests/unit/test_thread_fetch_js.py
git commit -m "fix(webui-2): D-14 thread fetch rejects redirected and non-HTML responses

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-27: D-20 — the agent listing does not link an agent that has no dashboard

**Files:**
- Modify: `templates/agent/listing.html` (whole file; written against the post-Phase-1 colours, `text-gray-400` → `text-gray-600`)
- Test: `tests/integration/test_agent_listing_and_pagers.py` (create)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_agent_listing_and_pagers.py
"""D-20: the My Agents listing links only agents whose dashboard opens (active or
inactive); any other status used to bounce /agent/<id>/dashboard -> /agent -> the
same listing. D-26 (Task 2A-28): conversations page past the first 50 roots.
D-27 (Task 2A-29): the run timeline pager survives a page past the end."""

import pytest

from src.models import USER_ROLE_ADMIN, AgentDelegate
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_the_listing_does_not_link_an_agent_without_a_dashboard(client, db_session):
    pi = await factories.make_user(db_session)
    own = await factories.make_agent(db_session, user=pi, status="active")
    other = await factories.make_user(db_session)
    parked = await factories.make_agent(db_session, user=other, status="suspended")
    db_session.add(AgentDelegate(agent_registry_id=parked.id, user_id=pi.id))
    await db_session.flush()
    r = await client.get("/agent", headers=auth_headers(pi.id), follow_redirects=False)
    assert r.status_code == 200
    assert f'href="/agent/{own.agent_id}/dashboard"' in r.text
    assert f'href="/agent/{parked.agent_id}/dashboard"' not in r.text
    assert "Not available while suspended" in r.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_agent_listing_and_pagers.py -v -k listing`
Expected: FAIL (the suspended agent is linked).

- [ ] **Step 3: Implement** — replace `templates/agent/listing.html` with:

```html
{% extends "base.html" %}
{% block title %}My Agents — CoPI{% endblock %}

{% block content %}
{# D-20: only an active or inactive agent has a dashboard (agent_dashboard sends
   any other status back to /agent), so linking one looped back to this page.
   Such agents are listed with their status and no link. #}
{% macro agent_card(agent, is_delegate) %}
{% set viewable = agent.status in ('active', 'inactive') %}
{% if viewable %}
<a href="/agent/{{ agent.agent_id }}/dashboard"
   class="block bg-white rounded-xl border border-gray-200 p-5 hover:border-indigo-300 transition">
{% else %}
<div class="block bg-white rounded-xl border border-gray-200 p-5">
{% endif %}
    <div class="flex items-center justify-between">
        <div>
            <div class="flex items-center gap-2">
                <span class="text-lg font-semibold text-gray-800">{{ agent.bot_name }}</span>
                {% if is_delegate %}<span class="px-2 py-0.5 rounded-full text-xs bg-indigo-50 text-indigo-600">Delegate</span>{% endif %}
            </div>
            <p class="text-sm text-gray-500">{{ agent.pi_name }} lab</p>
        </div>
        <div class="flex items-center gap-3">
            <span class="px-2.5 py-1 rounded-full text-xs
                {% if agent.status == 'active' %}bg-green-100 text-green-700
                {% elif agent.status == 'pending' %}bg-amber-100 text-amber-700
                {% else %}bg-gray-100 text-gray-600{% endif %}">
                {{ agent.status | capitalize }}
            </span>
            {% if viewable %}
            <span class="text-gray-600" aria-hidden="true">&rarr;</span>
            {% else %}
            <span class="text-xs text-gray-600">Not available while {{ agent.status }}</span>
            {% endif %}
        </div>
    </div>
{% if viewable %}</a>{% else %}</div>{% endif %}
{% endmacro %}
<div class="max-w-3xl mx-auto">
    <h1 class="text-2xl font-bold text-gray-900 mb-6">My Agents</h1>

    {% if own_agent %}
    <div class="mb-6">
        <h2 class="text-sm font-medium text-gray-500 uppercase tracking-wide mb-3">Your Agent</h2>
        {{ agent_card(own_agent, False) }}
    </div>
    {% endif %}

    {% if delegated_agents %}
    <div>
        <h2 class="text-sm font-medium text-gray-500 uppercase tracking-wide mb-3">Delegated Agents</h2>
        <div class="space-y-3">
            {% for agent in delegated_agents %}
            {{ agent_card(agent, True) }}
            {% endfor %}
        </div>
    </div>
    {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_agent_listing_and_pagers.py tests/integration/test_agent_page.py tests/unit/test_reachability.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add templates/agent/listing.html tests/integration/test_agent_listing_and_pagers.py
git commit -m "fix(webui-2): D-20 the agent listing links only agents with a dashboard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-28: D-26 — conversations pager past the first 50 roots

**Files:**
- Modify: `src/routers/agent_page.py:10` (import `Query`), imports (`MAX_PAGE`), constants, `agent_conversations` (`:333-460`)
- Modify: `templates/agent/conversations.html` (pager after the messages list)
- Test: `tests/integration/test_agent_listing_and_pagers.py` (append)

**Interfaces:**
- Produces: `src.routers.agent_page._CONV_PAGE = Query(1, ge=1, le=MAX_PAGE)`; template context keys `page: int`, `has_older: bool`.

- [ ] **Step 1: Write the failing test** (append)

```python
async def _roots(db, run, agent_id, n):
    for i in range(n):
        await factories.make_agent_message(
            db, run=run, agent_id=agent_id, channel_name="general", channel_id="C1",
            visibility="public", message_ts=f"9.{i:04d}", phase="new_post",
            content=f"ROOT-{i + 1:03d}", sender_name="PagedBot", posted_at=1000.0 + i,
        )


async def test_conversations_page_past_the_first_fifty_roots(client, db_session):
    from src.routers.agent_page import _ROOT_LIMIT

    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="paged", status="active")
    run = await factories.make_simulation_run(db_session)
    await _roots(db_session, run, "paged", _ROOT_LIMIT + 1)
    await db_session.flush()

    first = await client.get("/agent/paged/conversations", headers=auth_headers(pi.id))
    assert first.status_code == 200
    assert f"ROOT-{_ROOT_LIMIT + 1:03d}" in first.text and "ROOT-002" in first.text
    assert "ROOT-001" not in first.text
    assert 'href="?page=2"' in first.text

    second = await client.get("/agent/paged/conversations?page=2", headers=auth_headers(pi.id))
    assert "ROOT-001" in second.text and "ROOT-002" not in second.text
    assert 'href="?page=1"' in second.text and 'href="?page=3"' not in second.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_agent_listing_and_pagers.py -v -k conversations`
Expected: FAIL (no `href="?page=2"`; page 2 still shows the newest 50).

- [ ] **Step 3: Implement**

`src/routers/agent_page.py`: `from fastapi import APIRouter, Depends, Form, HTTPException, Request` →
`from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request`; add
`from src.services.directory import MAX_PAGE` (sorted among the `src.services` imports); after
`_ROOT_LIMIT = 50` add:

```python
#: Conversations page number (D-26); a module singleton so the default is not a
#: call in the signature (scripts/ci.sh SRC_LINT_MAX).
_CONV_PAGE = Query(1, ge=1, le=MAX_PAGE)
```

In `agent_conversations`: add `    page: int = _CONV_PAGE,` after `request: Request,`; insert
`    has_older = False` directly before `    if run_id:`; in the roots query replace
`            .limit(_ROOT_LIMIT)` with

```python
            .limit(_ROOT_LIMIT + 1)
            .offset((page - 1) * _ROOT_LIMIT)
```

and replace `        roots = list(reversed(root_rows.scalars().all()))` with

```python
        # One row past the page says whether an older page exists.
        fetched = root_rows.scalars().all()
        has_older = len(fetched) > _ROOT_LIMIT
        roots = list(reversed(fetched[:_ROOT_LIMIT]))
```

In the `TemplateResponse` context add `page=page, has_older=has_older,` after
`messages=messages, has_run=run_id is not None,`. Append to the docstring: "Paged by ``page``
(D-26): ``_ROOT_LIMIT`` roots per page, newest first."

`templates/agent/conversations.html`: directly after the `{% endif %}` that closes
`{% if messages %} … {% else %} … {% endif %}` (before the closing `</div>` of the page container), insert:

```html
    {% if page > 1 or has_older %}
    <nav aria-label="Conversation pages" class="mt-6 flex items-center justify-between text-sm">
        {% if page > 1 %}<a href="?page={{ page - 1 }}" class="text-indigo-600 hover:underline">&larr; Newer</a>{% else %}<span></span>{% endif %}
        {% if has_older %}<a href="?page={{ page + 1 }}" class="text-indigo-600 hover:underline">Older &rarr;</a>{% endif %}
    </nav>
    {% endif %}
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_agent_listing_and_pagers.py tests/integration/test_conversation_feed.py tests/unit/test_reachability.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/agent_page.py templates/agent/conversations.html tests/integration/test_agent_listing_and_pagers.py
git commit -m "feat(webui-2): D-26 page the conversations view past 50 thread roots

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-29: D-27 — run timeline: page clamped, pager whenever there are messages

**Files:**
- Modify: `src/services/directory.py:1144-1218` (`build_run_detail`)
- Modify: `templates/admin/_run_detail_body.html:109` (`{% if messages %}` → `{% if message_total > 0 %}`)
- Test: `tests/integration/test_agent_listing_and_pagers.py` (append)

**Interfaces:** `build_run_detail(...)["page"]` is now within `1..page_count`.

- [ ] **Step 1: Write the failing tests** (append)

```python
async def test_a_page_past_the_end_is_clamped_and_keeps_its_pager(client, db_session, monkeypatch):
    monkeypatch.setattr("src.services.directory.RUN_MESSAGES_PAGE_SIZE", 2)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    for i in range(3):
        await factories.make_agent_message(
            db_session, run=run, agent_id="su", channel_name="general",
            message_ts=f"5.{i:04d}", phase="new_post",
        )
    r = await client.get(f"/admin/activity/{run.id}?page=99", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert "Page 2 of 2" in r.text
    assert 'href="?page=1"' in r.text


async def test_a_run_with_no_messages_shows_no_pager(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    r = await client.get(f"/admin/activity/{run.id}?page=5", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert 'id="run-messages-pager"' not in r.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_agent_listing_and_pagers.py -v -k "clamped or no_pager"`
Expected: `test_a_page_past_the_end_is_clamped_and_keeps_its_pager` FAILS (timeline and pager hidden);
the no-messages control passes.

- [ ] **Step 3: Implement** — in `build_run_detail`, delete `    page = max(1, page)` and, directly
after the `message_total = await db.scalar(...) or 0` statement, insert:

```python
    # D-27: clamp to the last page, so a stale or hand-edited ?page= past the end
    # shows that page (and its pager) instead of an empty timeline with no way back.
    page_count = max(1, -(-message_total // RUN_MESSAGES_PAGE_SIZE))
    page = min(max(1, page), page_count)
```

and in the returned dict replace `        "page_count": max(1, -(-message_total // RUN_MESSAGES_PAGE_SIZE)),`
with `        "page_count": page_count,`. Add to the docstring: "``page`` is clamped to
``1..page_count``."

`templates/admin/_run_detail_body.html`: the `{% if messages %}` that opens the
`<!-- Message timeline -->` card becomes `{% if message_total > 0 %}`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_agent_listing_and_pagers.py tests/integration/test_directory_service.py tests/integration/test_manager_views.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/directory.py templates/admin/_run_detail_body.html tests/integration/test_agent_listing_and_pagers.py
git commit -m "fix(webui-2): D-27 clamp the run timeline page and keep its pager

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-30: M-08 — managers and reviewers are redirected from PI-only pages

**Files:**
- Modify: `src/dependencies.py` (imports; new `staff_landing_redirect` after `get_pi_user`)
- Modify: `src/routers/profile.py` (`profile_view` `:53-58`, `profile_edit` `:111-114`)
- Modify: `src/routers/agent_page.py` (`agent_landing`, first statement)
- Test: `tests/integration/test_staff_pi_page_redirects.py`; browser journey in Task 2A-34

**Interfaces:**
- Produces: `src.dependencies.staff_landing_redirect(user: User) -> RedirectResponse | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_staff_pi_page_redirects.py
"""M-08: a manager or reviewer has no lab, so the PI-only pages (whose POSTs
get_pi_user refuses) send each to its own landing page instead of rendering."""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "role,path,target",
    [
        (USER_ROLE_MANAGER, "/profile", "/manager/pis"),
        (USER_ROLE_MANAGER, "/profile/edit", "/manager/pis"),
        (USER_ROLE_MANAGER, "/agent", "/manager/pis"),
        (USER_ROLE_REVIEWER, "/profile", "/manager/assessments"),
        (USER_ROLE_REVIEWER, "/profile/edit", "/manager/assessments"),
        (USER_ROLE_REVIEWER, "/agent", "/manager/assessments"),
    ],
)
async def test_staff_are_redirected_from_pi_only_pages(client, db_session, role, path, target):
    user = await factories.make_user(db_session, user_role=role)
    r = await client.get(path, headers=auth_headers(user.id), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == target


@pytest.mark.parametrize("path", ["/profile", "/profile/edit", "/agent"])
async def test_an_admin_keeps_the_pi_pages(client, db_session, path):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_profile(db_session, user=admin)
    r = await client.get(path, headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 200
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_staff_pi_page_redirects.py -v`
Expected: the three manager cases and the reviewer `/agent` case FAIL (200); the reviewer
`/profile` cases and the admin cases pass.

- [ ] **Step 3: Implement**

`src/dependencies.py`: add `from fastapi.responses import RedirectResponse` after the `fastapi`
import and, after `get_pi_user`:

```python
def staff_landing_redirect(user: User) -> RedirectResponse | None:
    """Where a GET of a PI-only page sends an account with no lab (M-08): a
    manager to /manager/pis, a reviewer to /manager/assessments. None for a PI or
    an admin (``User.may_use_pi_surfaces``). The PI-only POSTs keep get_pi_user's
    403; this is the navigation half."""
    if user.may_use_pi_surfaces:
        return None
    return RedirectResponse(
        url="/manager/pis" if user.is_manager else "/manager/assessments", status_code=302
    )
```

`src/routers/profile.py`: import `staff_landing_redirect` from `src.dependencies`. In
`profile_view` replace

```python
    # A REVIEWER is neither staff nor PI and has no lab profile to
    # view — bounce before the onboarding check, which would otherwise send
    # it to a page it can never complete (get_pi_user gates the only writer
    # of onboarding_complete).
    if current_user.is_reviewer:
        return RedirectResponse(url="/manager/assessments", status_code=302)
```

with

```python
    # A manager or reviewer has no lab profile to view (M-08) — bounce before the
    # onboarding check, which would otherwise send it to a page it can never
    # complete (get_pi_user gates the only writer of onboarding_complete).
    bounce = staff_landing_redirect(current_user)
    if bounce is not None:
        return bounce
```

and in `profile_edit` replace

```python
    # Same reviewer bounce as GET /profile: without it a reviewer renders a
    # profile-edit form whose POST /profile/save 403s (get_pi_user).
    if current_user.is_reviewer:
        return RedirectResponse(url="/manager/assessments", status_code=302)
```

with

```python
    # Same bounce as GET /profile: without it a manager or reviewer renders a
    # profile-edit form whose POST /profile/save 403s (get_pi_user).
    bounce = staff_landing_redirect(current_user)
    if bounce is not None:
        return bounce
```

`src/routers/agent_page.py`: import `staff_landing_redirect` from `src.dependencies` and insert as
the first statements of `agent_landing` (after its docstring):

```python
    # M-08: a manager or reviewer has no lab; the request form here would 403.
    bounce = staff_landing_redirect(current_user)
    if bounce is not None:
        return bounce
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_staff_pi_page_redirects.py tests/integration/test_manager_onboarding.py tests/integration/test_reviewer_role.py tests/integration/test_pi_only_writes.py tests/integration/test_agent_page.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dependencies.py src/routers/profile.py src/routers/agent_page.py tests/integration/test_staff_pi_page_redirects.py
git commit -m "fix(webui-2): M-08 redirect managers and reviewers away from PI-only pages

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-31: FN-02 — onboarding prefills the effective user's email

**Files:**
- Modify: `src/routers/onboarding.py:25-35` (`_template_context` adds `user`)
- Modify: `templates/onboarding/profile_review.html:84`
- Test: `tests/integration/test_onboarding_impersonated_email.py`

**Interfaces:** template context key `user` (the effective user) on `onboarding/profile_review.html`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_onboarding_impersonated_email.py
"""FN-02: under impersonation the onboarding form must prefill the impersonated
user's email, not the real admin's (saving the admin's address then failed as
"already in use")."""

import pytest

from src.models import USER_ROLE_ADMIN, Job
from tests import factories
from tests.integration._webui_helpers import impersonation_headers

pytestmark = pytest.mark.integration


async def test_onboarding_under_impersonation_prefills_the_worn_users_email(client, db_session):
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="real-admin@example.org"
    )
    pi = await factories.make_user(
        db_session, onboarding_complete=False, email="worn-pi@example.org"
    )
    await factories.make_profile(db_session, user=pi)
    db_session.add(Job(type="generate_profile", status="completed", user_id=pi.id, payload={}))
    await db_session.flush()
    r = await client.get("/onboarding", headers=impersonation_headers(admin.id, pi.id))
    assert r.status_code == 200
    assert 'value="worn-pi@example.org"' in r.text
    assert 'value="real-admin@example.org"' not in r.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_onboarding_impersonated_email.py -v`
Expected: FAIL (the admin's address is prefilled).

- [ ] **Step 3: Implement** — in `src/routers/onboarding.py` `_template_context`, add
`        "user": user,` to the `ctx` dict after `"current_user": …`; in
`templates/onboarding/profile_review.html:84` replace `value="{{ current_user.email or '' }}"` with
`value="{{ user.email or '' }}"`. Add to `_template_context` a docstring:
`"""``current_user`` is the real admin under impersonation (it drives the nav); ``user`` is the
effective account, which every form field must read (FN-02)."""`.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_onboarding_impersonated_email.py tests/integration/test_onboarding_flow.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/onboarding.py templates/onboarding/profile_review.html tests/integration/test_onboarding_impersonated_email.py
git commit -m "fix(webui-2): FN-02 onboarding prefills the impersonated user's email

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-32: FN-08 — invite pages render with the normal page context

**Files:**
- Modify: `src/routers/invite.py` (imports; new `_viewer`, `_invite_context`; every `TemplateResponse` context; first statement of `accept_invite` and `confirm_accept_invite`)
- Test: `tests/integration/test_invite_page_context.py`

**Interfaces:**
- Produces: `src.routers.invite._viewer(request, db) -> User | None`,
  `src.routers.invite._invite_context(request, viewer: User | None, **kwargs) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_invite_page_context.py
"""FN-08: invite pages are rendered with the same current_user / banner context
as every other page, so a signed-in user sees their own nav, not "Sign in"."""

import pytest

from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_a_signed_in_user_sees_their_nav_on_an_invite_error_page(client, db_session):
    pi = await factories.make_user(db_session, name="Inviteviewer Pi")
    r = await client.get("/invite/no-such-token", headers=auth_headers(pi.id))
    assert r.status_code == 200
    assert "Inviteviewer Pi" in r.text
    assert ">Sign in</a>" not in r.text


async def test_a_visitor_still_sees_sign_in(client):
    r = await client.get("/invite/no-such-token")
    assert r.status_code == 200
    assert ">Sign in</a>" in r.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/integration/test_invite_page_context.py -v`
Expected: the signed-in test FAILS (`Sign in` shown); the visitor test passes.

- [ ] **Step 3: Implement** — in `src/routers/invite.py`: change the fastapi import to
`from fastapi import APIRouter, Depends, HTTPException, Request`, add
`from src.dependencies import get_current_user` (if Phase 1 has not), and add after
`_invite_matches_user`:

```python
async def _viewer(request: Request, db: AsyncSession) -> User | None:
    """The signed-in account for the page chrome, or None (FN-08). Never
    redirects: an invite page must render for a signed-out visitor too."""
    if not request.session.get("user_id"):
        return None
    try:
        return await get_current_user(request, db)
    except HTTPException:
        return None


def _invite_context(request: Request, viewer: User | None, **kwargs) -> dict:
    """The context every other router builds (current_user, impersonation banner),
    so base.html shows the account rather than "Sign in" (FN-08)."""
    impersonated = getattr(viewer, "_is_impersonated", False)
    ctx = {
        "request": request,
        "current_user": getattr(viewer, "_real_admin", None) if impersonated else viewer,
        "impersonation_banner": viewer if impersonated else None,
    }
    ctx.update(kwargs)
    return ctx
```

Insert `    viewer = await _viewer(request, db)` as the first statement after the docstring of both
`accept_invite` and `confirm_accept_invite`. Then replace every context dict passed to
`templates.TemplateResponse(...)` in this module:
- `{"request": request, "error": <expr>}` → `_invite_context(request, viewer, error=<expr>)`
  (in `_accept_invitation`, whose scope has `user` and no `viewer`, use
  `_invite_context(request, user, error=_INVITE_EMAIL_MISMATCH_MSG)`);
- the `invite/accept.html` dict → `_invite_context(request, viewer, pi_name=agent.pi_name,
  bot_name=agent.bot_name, token=token, invitation_email=invitation.email)`.

After the edit, `grep -n '{"request": request' src/routers/invite.py` prints nothing.

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/integration/test_invite_page_context.py tests/integration/test_agent_page.py tests/integration/test_double_submits.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/invite.py tests/integration/test_invite_page_context.py
git commit -m "fix(webui-2): FN-08 invite pages render with the signed-in user's context

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-33: C-02 — the HTML discussions export renders summaries on the server

**Files:**
- Create: `src/services/export_markdown.py`
- Modify: `src/routers/admin/discussions.py` (imports; the `proposals.append({...})` dict in the export branch)
- Modify: `templates/admin/discussions_export.html:6` (drop `_head_assets.html`), `:40`, `:60-61`
- Modify: `pyproject.toml` (dependencies)
- Test: `tests/unit/test_export_markdown.py`, `tests/integration/test_discussions_export_reviews.py` (append)

**Interfaces:**
- Produces: `src.services.export_markdown.render_export_markdown(text: str) -> markupsafe.Markup`;
  export proposal key `summary_html`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_export_markdown.py
"""C-02: the downloadable export is opened from disk, where /static/js cannot load,
so proposal markdown is rendered on the server — raw HTML escaped, images as
links, script URLs dropped."""

from src.services.export_markdown import render_export_markdown


def test_markdown_is_rendered():
    assert "<strong>Bold plan</strong>" in render_export_markdown("**Bold plan**")


def test_raw_html_is_escaped():
    html = render_export_markdown("<script>window.__x=1</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;window.__x=1&lt;/script&gt;" in html


def test_an_image_renders_as_a_link_not_an_img():
    html = render_export_markdown("![pic](https://evil.example/p.png)")
    assert "<img" not in html
    assert 'href="https://evil.example/p.png"' in html


def test_a_javascript_link_is_not_a_link():
    assert 'href="javascript:' not in render_export_markdown("[js](javascript:alert(1))")


def test_empty_text_renders_empty():
    assert render_export_markdown("") == ""
```

Append to `tests/integration/test_discussions_export_reviews.py`:

```python
async def test_the_html_export_renders_summaries_on_the_server(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", channel_name="general",
        phase="new_post", message_ts="1700000000.000200", content="root",
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id="1700000000.000200", channel="general",
        agent_a="su", agent_b="lotz", outcome="proposal",
        summary_text="**Bold plan**\n\n<script>window.__x=1</script>", decided_at=REVIEWED,
    )
    await db_session.flush()
    r = await client.get(
        f"/admin/discussions?run_id={run.id}&export=html", headers=auth_headers(admin.id)
    )
    assert r.status_code == 200
    assert "<strong>Bold plan</strong>" in r.text
    assert "<script>window.__x=1</script>" not in r.text
    assert "data-markdown" not in r.text
    assert "/static/js/markdown.js" not in r.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv-test/bin/python -m pytest tests/unit/test_export_markdown.py tests/integration/test_discussions_export_reviews.py -v`
Expected: `ModuleNotFoundError: No module named 'src.services.export_markdown'`; the new integration
test FAILS (`data-markdown` present, no `<strong>`).

- [ ] **Step 3: Implement**

```python
# src/services/export_markdown.py
"""Server-side markdown for the downloadable discussions export (C-02).

The HTML export is saved and opened from disk, where /static/js/markdown.js
cannot load, so its proposal summaries are rendered here. CommonMark with raw
HTML off (a tag in the text is escaped, never passed through — the page
renderer's rule, spec D3) and the image rule off (``![alt](url)`` renders as
"!" and a link, never an <img> that would fetch a remote URL when the file is
opened). markdown-it's own link validation drops javascript:, vbscript:, file:
and non-image data: URLs.

markdown-it-py is declared in pyproject.toml; it was already installed as a
dependency of rich.
"""

from markdown_it import MarkdownIt
from markupsafe import Markup

_MD = MarkdownIt("commonmark", {"html": False}).disable("image")


def render_export_markdown(text: str) -> Markup:
    """``text`` as HTML that is safe to emit unescaped in a Jinja template."""
    return Markup(_MD.render(text or ""))
```

`src/routers/admin/discussions.py`: add `from src.services.export_markdown import render_export_markdown`
and, in the export branch's `proposals.append({...})`, add after `"summary": d.summary_text.strip(),`:

```python
                "summary_html": render_export_markdown(d.summary_text.strip()),
```

`templates/admin/discussions_export.html`: delete `    {% include "_head_assets.html" %}`; replace
`        <div class="proposal-body" data-markdown="{{ p.summary | e }}"></div>` with
`        <div class="proposal-body">{{ p.summary_html }}</div>`; delete the two comment lines

```html
    <!-- [data-markdown] rendering is handled by the shared sanitizing renderer
         (/static/js/markdown.js, loaded in <head>) — see SEC-2. -->
```

and put in their place `    <!-- Summaries are rendered on the server (src/services/export_markdown.py, C-02): this file is opened from disk, where /static cannot load. -->`.

`pyproject.toml`: in `dependencies`, add `    "markdown-it-py>=2.2.0",` directly after `    "rich>=13.7.0",`
(2.2.0 is `rich`'s own floor, so the resolved version does not move).

- [ ] **Step 4: Run and confirm pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_export_markdown.py tests/integration/test_discussions_export_reviews.py tests/integration/test_discussions_filters.py tests/characterization/test_auth_and_admin_routes.py tests/unit/test_reachability.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/export_markdown.py src/routers/admin/discussions.py templates/admin/discussions_export.html pyproject.toml tests/unit/test_export_markdown.py tests/integration/test_discussions_export_reviews.py
git commit -m "fix(webui-2): C-02 render the HTML export's summaries on the server

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2A-34: Phase 2 browser journeys for D-14, B-04, M-08

**Files:**
- Create (or extend, if another Phase 2 part created it): `tests/e2e/ui_audit/journeys_phase2.py`

**Interfaces:**
- Consumes: the Phase 0 harness contract — `h.base_url`, `h.ids` (seed keys `pi2`, `reviewer`,
  `manager`, `assessments`), `await h.page(role, width=1280) -> (context, page, log)`; the seed's
  agent slug `vogelstein` owned by `pi2`, whose interview root in `interview-one` has hub replies
  (`tests/assessment_chat_support.py::seed_interview`).
- Produces: `journey_thread_fetch_after_session_expiry`, `journey_review_double_click_stores_one`,
  `journey_staff_redirected_from_pi_pages`, each appended to `JOURNEYS`.

- [ ] **Step 1: Write the journeys** (these are the test; the harness is not collected by pytest)

```python
# tests/e2e/ui_audit/journeys_phase2.py
"""Phase 2 browser journeys (spec §9). Run with
``python -m tests.e2e.ui_audit.run journeys --phase 2``.

Part 2A: D-14 thread fetch after the session ends, B-04 double-click on a review
submit, M-08 staff redirects away from PI-only pages."""

import time


async def journey_thread_fetch_after_session_expiry(h) -> dict:
    """D-14: with the session gone, expanding a thread shows a session notice (not
    the login page) and marks the link expanded."""
    ctx, page, log = await h.page("pi2")
    try:
        await page.goto(h.base_url + "/agent/vogelstein/conversations", wait_until="networkidle")
        link = page.locator("[data-thread-expand]").first
        if not await link.count():
            return {"ok": False, "reason": "the seed has no expandable thread for pi2", "log": log}
        panel_id = await link.get_attribute("aria-controls")
        await ctx.clear_cookies()
        await link.click()
        panel = page.locator(f'[id="{panel_id}"]')
        await panel.wait_for(state="visible", timeout=5000)
        text = await panel.inner_text()
        expanded = await link.get_attribute("aria-expanded")
        injected = await panel.locator("form, a[href^='/login']").count()
        return {
            "ok": "session has ended" in text and expanded == "true" and injected == 0,
            "panel_text": text[:200],
            "aria_expanded": expanded,
            "injected_login_nodes": injected,
            "log": log,
        }
    finally:
        await ctx.close()


async def journey_review_double_click_stores_one(h) -> dict:
    """B-04: a double click on "Submit feedback" stores one review."""
    ctx, page, log = await h.page("reviewer")
    try:
        assessment_id = h.ids["assessments"][0]
        await page.goto(
            f"{h.base_url}/manager/assessments/{assessment_id}", wait_until="networkidle"
        )
        comment = f"double-click-{time.time_ns()}"
        await page.select_option("#add-score", "4")
        await page.fill("#add-comment", comment)
        await page.select_option("#add-mode", "log_only")
        submit = page.locator("#add-comment").locator("xpath=ancestor::form").locator(
            "button[type=submit]"
        )
        await submit.dblclick()
        await page.wait_for_load_state("networkidle")
        await page.goto(
            f"{h.base_url}/manager/assessments/{assessment_id}", wait_until="networkidle"
        )
        stored = await page.get_by_text(comment, exact=True).count()
        return {"ok": stored == 1, "stored": stored, "log": log}
    finally:
        await ctx.close()


async def journey_staff_redirected_from_pi_pages(h) -> dict:
    """M-08: managers and reviewers land on their own pages, not PI forms."""
    expected = {
        ("manager", "/profile"): "/manager/pis",
        ("manager", "/profile/edit"): "/manager/pis",
        ("manager", "/agent"): "/manager/pis",
        ("reviewer", "/agent"): "/manager/assessments",
    }
    landed = {}
    for (role, path), _target in expected.items():
        ctx, page, _log = await h.page(role)
        try:
            await page.goto(h.base_url + path, wait_until="networkidle")
            landed[f"{role} {path}"] = page.url[len(h.base_url):]
        finally:
            await ctx.close()
    ok = all(
        landed[f"{role} {path}"].startswith(target) for (role, path), target in expected.items()
    )
    return {"ok": ok, "landed": landed}


JOURNEYS = [
    journey_thread_fetch_after_session_expiry,
    journey_review_double_click_stores_one,
    journey_staff_redirected_from_pi_pages,
]
```

If another Phase 2 part already created this module, keep its imports and journeys, add these three
functions, and extend its `JOURNEYS` list with the three names in the order above.

- [ ] **Step 2: Lint**

Run: `.venv-test/bin/ruff check tests/e2e/ui_audit/journeys_phase2.py`
Expected: no findings.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/ui_audit/journeys_phase2.py
git commit -m "test(webui-2): browser journeys for D-14, B-04 and M-08

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(The parent runs `python -m tests.e2e.ui_audit.run journeys --phase 2` after the phase merges; each
of the three must return `"ok": true`.)

### Self-review

**Spec coverage (§7 bullets owned by this part → task):**

| Finding | Spec requirement | Task |
|---|---|---|
| A-07 | `ensure_admin_remains` in deny and delete; deny refuses self | 2A-2 |
| SN-01 | allowlist add promotes only `pending` | 2A-3 |
| A-10 | impersonation note or `mechanism="web_impersonated"` on every write made while impersonating | 2A-5 (central log + callback), 2A-6 (profile writes), 2A-7 (audit payload); enumeration table above |
| A-15 | length checks with a form error | 2A-8 |
| A-16 | 404 for an unknown ORCID | 2A-4 |
| A-17 | PI-target check | 2A-9 |
| SN-02 | ≤10 addresses per submission, ≤25 invitations per agent per 24 h | 2A-10 |
| B-04 | `ui.js` disables buttons; server rejects identical review within 60 s | 2A-13 (client), 2A-12 (server), 2A-34 (journey) |
| B-11 | `maxlength`; 400 on overlength or NUL | 2A-11 |
| D-18 | strip `orcid.org/` prefix, uppercase trailing `x` | 2A-4 |
| C-06 | valid UUID, existing unlinked user, role that may own a lab | 2A-14 |
| C-24 | `SELECT … FOR UPDATE` on the run row in start and finalize | 2A-16 |
| C-29 | reject only `pending` | 2A-15 |
| D-07 | `UPDATE … WHERE status='pending'` with rowcount check | 2A-17 |
| D-15 | refuse an empty save when no profile exists | 2A-18 |
| D-17 | map the `users.orcid` violation to "already exists" | 2A-19 |
| D-19 | remove only the vetoed title; idempotent veto | 2A-9 |
| D-11 | one token predicate, `token_for_agent_row` | 2A-17 |
| C-03 | `slack_error` reaches /admin/agents | 2A-21 |
| D-09 | `saved` onto flash | 2A-20 |
| D-10 | `slack_ok` onto flash | 2A-21 |
| A-14 (remaining) | `msg` onto flash | 2A-22 |
| B-03 | flash "Moved to Reviewed", anchor the next card | 2A-23 |
| B-13 | unknown `run_id` falls back to the current run | 2A-24 |
| D-12 | no status buttons under impersonation | 2A-25 |
| D-14 | reject redirected / non-HTML fragments; `aria-expanded` on error | 2A-26, 2A-34 |
| D-20 | listing loop for non-viewable agents | 2A-27 |
| D-21 | report the ignored email send result | 2A-10 |
| D-26 | conversations pager | 2A-28 |
| D-27 | pager whenever `message_total > 0`, page clamped | 2A-29 |
| M-08 | managers and reviewers redirected from PI-only pages | 2A-30, 2A-34 |
| FN-02 | onboarding shows the effective user's email | 2A-31 |
| FN-08 | invite pages get the normal context | 2A-32 |
| C-02 | export renders summaries server-side | 2A-33 |

**Placeholder scan:** no TBD/TODO; every implementation step gives full code or an exact
before/after; the ellipses that remain are in prose (abbreviated test names in "Expected" lines,
route-family shorthand in the A-10 table, and the spec's own `SELECT … FOR UPDATE` /
`UPDATE … WHERE` wording). Three edits are written structurally because Phase 1 rewrites the
surrounding code: the gate call in `manager_activate_agent` (2A-17, replaced wholesale from the gate
on), the `form=` mapping in `save_public_profile` (2A-18, bound to `fields`), and the context dicts
in `invite.py` (2A-32, a mechanical rule with a grep that must print nothing).

**Interface-name consistency:** `impersonation_headers`, `session_from`, `follow` (2A-1) are the
names every later test imports. `normalize_orcid` (2A-4) is used by 2A-4's three call sites only.
`_audit_payload` (2A-7), `USER_FIELD_MAX_CHARS` (2A-8), `_require_pi` (2A-9),
`_INVITES_PER_SUBMISSION` / `_INVITES_PER_AGENT_PER_DAY` (2A-10), `_validate_comment` (2A-11),
`DUPLICATE_WINDOW_SECONDS` / `DuplicateFeedbackError` (2A-12), `_link_target` (2A-14),
`_has_content` (2A-18), `_optional_uuid` and `_assessments_redirect(anchor_id=, no_anchor=)` (2A-23),
`_CONV_PAGE` (2A-28), `staff_landing_redirect` (2A-30), `_viewer` / `_invite_context` (2A-32),
`render_export_markdown` (2A-33) are each defined once and used under the same name. Consumed
Phase 0/1 names: `flash` (`src/web/flash.py`), `ensure_activation_allowed`,
`session["impersonate_user_id"]`, `static/js/ui.js`, `confirm.js`'s capture listener, the harness
`h.page` / `h.ids` / `JOURNEYS`, and the finalize `confirm_run` field.

---

## Part 2B: Enforced CSP, performance, correctness and copy, drawer JS, low accessibility items, responsive

**Scope:** spec §7 bullets "CSP", "Performance" (C-14, C-15, C-16, B-07, B-08), "Correctness and copy"
(C-18, C-19, C-20, C-21, C-22, C-23, B-09, D-28, B-18, M-02, FN-07), "JS" (B-16, B-17, B-20),
"Accessibility, low items" (X-03, X-04, X-06, FN-04), "Responsive" (R-01, FN-03, R-02); §9 Phase 2
harness journeys (enforced-CSP crawl, 375 px crawl).

### Global constraints (this part)

- CSP (spec §7, §6.3): "switch the report-only policy of §6.3 to enforced, keeping `report-uri`". The
  §6.3 policy is `default-src 'self'; script-src 'self' 'nonce-<n>'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; report-uri
  /api/csp-report`, plus the enforced `frame-ancestors 'none'; base-uri 'none'; object-src 'none'`.
  **Deviation, required for correctness:** `form-action` becomes `'self' https://slack.com`. Both Slack
  provisioning forms (`POST /admin/agents/{agent_id}/slack/provision`, `src/routers/admin/agents.py:380`
  `RedirectResponse(url=oauth_url, ...)`, and the manager twin) answer with a 302 to Slack's
  `oauth_authorize_url`, and Chromium applies `form-action` to the redirect of a form submission, so the
  §6.3 value enforced verbatim would block provisioning. The CSP task's test pins this.
- Phase 2 deploys web, worker and agent images (Task 2B-7 edits `src/services/assessment_detail.py`,
  which `src/agent/engine/verdicts.py` imports; spec §4 as amended); no migration. Nothing here touches
  `prompts/`, `src/agent/`, or `docker-compose.prod.yml`.
- C-14: "25 s cache of the live-run stats context per run". The web container runs ONE uvicorn process:
  `Dockerfile:53` `CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]` and the
  uncommitted `docker-compose.prod.yml:35` `command:` are identical, with no `--workers` (uvicorn's
  default is `$WEB_CONCURRENCY` or 1; neither file sets it). The cache is per process; with N workers
  each process keeps its own copy and recomputes at most once per run per TTL; no cross-process
  invalidation is needed because the key carries the run's status and `ended_at` and the TTL bounds
  staleness.
- FN-07: "one timestamp formatter that always shows the zone" — `src/services/display_format.py`
  `timestamp`, already registered as the Jinja filter `ts` in `src/web/templating.py:28`. Every style
  ends in ` UTC` (the date-only style included).
- FN-04: "Apply buttons instead of submit-on-change": every `data-autosubmit` use and the `ui.js`
  behaviour are removed (spec §6.3 "`data-autosubmit` until Phase 2 removes it").
- R-02 is applied as CSS appended to `static/css/input.css` (outside any `@layer`, so Tailwind always
  emits it): `main { overflow-wrap: anywhere }` with `main table { overflow-wrap: break-word }` reaches
  the markdown, chat bubbles, profile fields and tag pills at once. `anywhere` rather than the
  `break-words` utility's `break-word` because only `anywhere` lowers min-content width, which is what
  a flex item or grid cell holding a 300-character URL needs; tables keep `break-word` so their columns
  are not squeezed and they scroll in their `overflow-x-auto` wrappers instead.
- Every task that adds a Tailwind class to `templates/`, `static/js/` or `src/` class strings, or edits
  `static/css/input.css`, runs `scripts/build_css.sh` and commits `static/css/app.css` in that task
  (Phase 1 `ci.sh` drift check).
- New handlers take module-level dependency singletons (`_DB`, `_ADMIN` in
  `src/routers/admin/_common.py`); no `Depends(...)`/`Query(...)`/`Form(...)` call is added in an
  argument default.
- Test command: `.venv-test/bin/python -m pytest <path>::<name> -v` on the host. DB-backed tests use
  `client`, `asgi_app`, `db_session`, `engine` (`tests/conftest.py`), `tests/factories.py`, and
  `auth_headers` from `tests/integration/test_manager_access.py` (assumed still the signed-cookie helper
  after Part 1B's cookie rename).

### Review focus (this part)

1. HEAD through redirects, static files and POST-only paths: a HEAD must mirror the GET status and
   `Location`, never emit a body, and a HEAD on a POST-only path must stay 405. Pinned in Task 2B-1
   (`test_head_on_an_authenticated_page_and_its_unauthenticated_redirect`,
   `test_head_on_a_post_only_path_is_405_and_on_an_unknown_path_404`).
2. The C-14 cache across a run status change: a stopped run must not be served the running run's
   cached figures. Pinned in Task 2B-3 (`test_a_status_change_is_a_new_cache_key`).
3. Discussions SQL paging on `run_id=all` with the same root `message_ts` in two runs and an orphaned
   thread with two decisions: the SQL rewrite must match the frozen oracle order-free. Pinned in Task 2B-4
   (`test_all_runs_parity_with_duplicate_ts_and_a_twice_decided_orphan`).
4. A lazy-loaded LLM-call body after the session expired (fetch follows the redirect to `/login`): the
   page must not inject the login page. Pinned in Task 2B-6 (`test_ui_js_lazy_fragment_rejects_redirects_and_non_html`)
   and the CSP journey's llm-calls expand.
5. Enforced CSP on Slack provisioning (form POST redirected off-site). Pinned in Task 2B-15
   (`test_the_enforced_policy_lets_the_slack_provisioning_redirect_through`).

### File map

| File | Action | Responsibility |
|---|---|---|
| `src/web/head_requests.py` | Create | `HeadAsGetMiddleware`: HEAD runs as GET, body dropped (M-02) |
| `src/main.py` | Modify | Register `HeadAsGetMiddleware` innermost |
| `src/services/display_format.py` | Modify | `timestamp(dt, style)` — the one zone-labelled formatter (FN-07) |
| `src/services/assessment_chat_record.py` | Modify | Comment on `_minute` updated for FN-07 |
| `src/services/simulation_view.py` | Modify | 25 s per-run cache of the Live tab's DB aggregates (C-14); deterministic hub pick (C-23) |
| `src/services/directory.py` | Modify | Discussions threads paged in SQL + `run_id=all` cap (C-15); PI directory paged in SQL (C-15) |
| `src/routers/admin/discussions.py` | Modify | Pass `all_runs_refused` |
| `src/routers/admin/users.py` | Modify | Page the users list |
| `src/routers/manager.py` | Modify | Page the PI directory |
| `src/routers/admin/runs.py` | Modify | Defer bodies, fragment route, `le=MAX_PAGE`, latency/labels (C-16, C-19, C-21, C-22) |
| `src/routers/admin/jobs.py` | Modify | Filters from the enums; unknown value is 400 (C-18) |
| `src/routers/admin/simulation.py` | Modify | `max_runtime`/`max_proposals` `>= 0` server-side (C-20) |
| `src/services/assessment_detail.py` | Modify | Select only the tool-use slice of `messages_json` (B-07) |
| `src/services/assessment_chat.py` | Modify | `list_history(sweep=...)` (B-08) |
| `src/routers/assessment_chat.py` | Modify | `?poll=1` skips the sweep (B-08) |
| `src/web/security_headers.py` | Modify | Enforce the script policy; drop the report-only header (CSP) |
| `static/js/assessment_chat.js` | Modify | B-08 poll flag, B-16, B-17, B-20 |
| `static/js/ui.js` | Modify | Add `data-lazy-fragment`; remove `data-autosubmit` |
| `static/css/input.css`, `static/css/app.css` | Modify | X-03 underline, R-01 select width, R-02 wrapping; rebuilt |
| `templates/base.html` | Modify | Remove the `[data-utc]` converter; `<header>`, labelled navs, flash inside `<main>` |
| `templates/**` (files listed per task) | Modify | Timestamps, Apply buttons, copy, landmarks, table wrappers, tiles |
| `templates/admin/_llm_call_bodies.html` | Create | Fragment: one call's system prompt, messages, response |
| `tests/integration/test_head_requests.py` | Create | M-02 |
| `tests/unit/test_timestamps_have_one_formatter.py` | Create | FN-07 source gate |
| `tests/unit/test_display_format.py` | Modify | `timestamp` styles |
| `tests/integration/test_simulation_page_queries.py` | Modify | C-14, C-23 |
| `tests/unit/test_discussions_split.py` | Modify | Extra parity case |
| `tests/integration/test_discussions_sql_paging.py` | Create | C-15 discussions |
| `tests/integration/test_pi_directory_paging.py` | Create | C-15 PI list |
| `tests/integration/test_llm_call_bodies.py` | Create | C-16, C-19, C-21, C-22 |
| `tests/integration/test_llm_calls_channel_filter.py`, `tests/integration/test_llm_call_stats_storage.py` | Modify | Follow C-16/C-21/C-22 |
| `tests/unit/test_admin_route_table.py` | Modify | New fragment route |
| `tests/unit/test_ui_js_behaviours.py` | Create | `ui.js` source pins |
| `tests/integration/test_tool_slice.py` | Create | B-07 parity |
| `tests/integration/test_assessment_chat_poll.py` | Create | B-08 |
| `tests/integration/test_admin_correctness_low.py` | Create | C-18, C-20 |
| `tests/unit/test_copy_and_links.py` | Create | B-09, D-28, B-18 |
| `tests/unit/test_chat_drawer_js.py` | Create | B-08, B-16, B-17, B-20 source pins |
| `tests/unit/test_filter_forms_apply.py` | Create | FN-04 |
| `tests/unit/test_low_a11y_markup.py` | Create | X-03, X-06 |
| `tests/integration/test_rendered_page_gate.py` | Modify (Part 1C file) | X-04 landmark/h1 checks; R-01 table-scroll check |
| `tests/unit/test_responsive_markup.py` | Create | R-01, FN-03, R-02 source pins |
| `tests/integration/test_csp_enforced.py` | Create | CSP |
| `tests/e2e/ui_audit/journeys_phase2_2b.py` | Create | Part 2B journeys |
| `tests/e2e/ui_audit/journeys_phase2.py` | Modify | created by Task 2A-34; 2B-10 appends Part 2B's `JOURNEYS` |

---

### Task 2B-1: HEAD on every GET route (M-02)

**Files:**
- Create: `src/web/head_requests.py`
- Modify: `src/main.py` (imports near line 39; `create_app()` before the first `application.add_middleware(` call, today line 272)
- Test: `tests/integration/test_head_requests.py`

**Interfaces:**
- Consumes: `create_app()` (`src/main.py`), `SAFE_METHODS` (`src/main.py:39`, already contains `"HEAD"`).
- Produces: `class HeadAsGetMiddleware` (pure ASGI, `__init__(self, app: ASGIApp)`), registered innermost.

Library check (read in source): FastAPI's `APIRoute` sets `route.methods = {method.upper() for method in methods}`
with no HEAD (`fastapi/routing.py:1021` in 0.142.2; `self.methods` likewise in 0.141.1), so every
`@router.get` answers HEAD with 405. Starlette's plain `Route` adds HEAD to a GET route
(`starlette/routing.py:240` in 1.7.0, `:234` in 1.4.1) and `StaticFiles` answers HEAD itself; neither
covers the API routers. No difference between the two versions for this mechanism. Mutating
`route.methods` after `include_router` is not used: 0.142.2 resolves included routes through an
effective route context (`_get_scope_effective_route_context`, `fastapi/routing.py:924`) whose
`methods` are populated at include time, so a post-hoc mutation is version-dependent. A pure ASGI
middleware is version-independent and the smallest correct mechanism.

- [ ] **Step 1: Write the failing test** — create `tests/integration/test_head_requests.py`:

```python
"""M-02: HEAD answers every GET route with the GET's status and headers and no body."""

import pytest

from src.models import USER_ROLE_ADMIN
from src.web.head_requests import HeadAsGetMiddleware
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

#: Headers that legitimately differ between two requests: the clock, a refreshed
#: session cookie, and the per-request CSP nonce.
_VOLATILE = {"date", "set-cookie", "content-security-policy", "content-security-policy-report-only"}


def _stable(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() not in _VOLATILE}


@pytest.mark.parametrize(
    "path", ["/login", "/manager", "/static/js/assessment_chat.js", "/api/health"]
)
async def test_head_mirrors_get_without_a_body(client, path):
    get = await client.get(path)
    head = await client.head(path)
    assert head.status_code == get.status_code
    assert _stable(head.headers) == _stable(get.headers)
    assert head.content == b""


async def test_head_on_an_authenticated_page_and_its_unauthenticated_redirect(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    get = await client.get("/admin/jobs", headers=headers)
    head = await client.head("/admin/jobs", headers=headers)
    assert (head.status_code, head.content) == (get.status_code, b"")
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["content-length"] == get.headers["content-length"]

    anon_get = await client.get("/admin/jobs")
    anon_head = await client.head("/admin/jobs")
    assert anon_head.status_code == anon_get.status_code
    assert anon_head.headers.get("location") == anon_get.headers.get("location")
    assert anon_head.content == b""


async def test_head_on_a_post_only_path_is_405_and_on_an_unknown_path_404(client):
    assert (await client.head("/logout")).status_code == (await client.get("/logout")).status_code == 405
    missing = await client.head("/no-such-page")
    assert (missing.status_code, missing.content) == (404, b"")


def test_the_head_middleware_is_innermost():
    """`user_middleware` runs outermost-first; HEAD must turn into GET only after the
    CSRF guard and the session middleware have seen the real method."""
    from src.main import create_app

    order = [m.cls for m in create_app().user_middleware]
    assert order[-1] is HeadAsGetMiddleware
```

- [ ] **Step 2: Run it** — `.venv-test/bin/python -m pytest tests/integration/test_head_requests.py -v`. Expected: collection error `ModuleNotFoundError: No module named 'src.web.head_requests'`.

- [ ] **Step 3: Implement** — create `src/web/head_requests.py`:

```python
"""HEAD on every GET route (M-02, docs/specs/2026-10-01-web-ui-remediation-design.md §7).

FastAPI's ``APIRoute`` registers exactly the methods it is given, so a
``@router.get`` route answers ``HEAD`` with 405; Starlette's own ``Route`` and
``StaticFiles`` handle ``HEAD`` but cover none of the API routers. This middleware
runs a ``HEAD`` as the ``GET`` it mirrors and drops the body, so status,
``Location`` and every header (``Content-Length`` included) are the ``GET``'s.

Registered FIRST in ``create_app()``, i.e. innermost: the CSRF guard and the
session middleware still see the real method (``HEAD`` is in ``SAFE_METHODS``);
only routing and handlers see ``GET``. A ``HEAD`` on a path with no ``GET`` route
gets the same 405 a ``GET`` would.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BODY_MESSAGES = ("http.response.body", "http.response.pathsend")
_EMPTY_FINAL_BODY: Message = {"type": "http.response.body", "body": b"", "more_body": False}


class HeadAsGetMiddleware:
    """Pure ASGI, so it works the same on every Starlette version."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] in _BODY_MESSAGES:
                if not message.get("more_body", False):
                    await send(_EMPTY_FINAL_BODY)
                return
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)
```

In `src/main.py`, add the import beside the other `src.` imports:

```python
from src.web.head_requests import HeadAsGetMiddleware
```

and insert, immediately before the first `application.add_middleware(` call in `create_app()`:

```python
    # HEAD on every GET route (M-02). Added FIRST, so it is the innermost
    # middleware and only routing sees the GET: see src/web/head_requests.py.
    application.add_middleware(HeadAsGetMiddleware)
```

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/integration/test_head_requests.py tests/integration/test_origin_guard.py::test_the_guard_is_the_outermost_middleware -v`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/web/head_requests.py src/main.py tests/integration/test_head_requests.py
git commit -m "fix(webui-2): answer HEAD on every GET route without a body (M-02)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-2: One zone-labelled timestamp formatter (FN-07)

**Files:**
- Modify: `src/services/display_format.py:72-75` (`timestamp`)
- Modify: `src/services/assessment_chat_record.py:285-288` (comment only)
- Modify: `templates/base.html:191-214` (delete the `[data-utc]` converter `<script>` block)
- Modify (every `strftime(` and every `data-utc`): `templates/manager/prompt_suggestion_detail.html`,
  `templates/manager/pis.html`, `templates/manager/discussions.html`, `templates/manager/activity.html`,
  `templates/manager/pi_detail.html`, `templates/manager/prompt_suggestions.html`,
  `templates/manager/activity_detail.html`, `templates/manager/assessments.html`,
  `templates/onboarding/profile_review.html`, `templates/admin/cohorts.html`,
  `templates/admin/user_detail.html`, `templates/admin/agents.html`, `templates/admin/agent_detail.html`,
  `templates/admin/llm_calls.html`, `templates/admin/access_requests.html`,
  `templates/admin/discussions.html`, `templates/admin/_run_detail_body.html`,
  `templates/admin/_assessments_body.html`, `templates/admin/activity.html`,
  `templates/admin/_discussions_threads.html`, `templates/admin/jobs.html`, `templates/admin/users.html`,
  `templates/admin/_assessment_detail_body.html`, `templates/admin/_cohort_gate_banner.html`,
  `templates/admin/cohort_detail.html`, `templates/admin/activity_detail.html`,
  `templates/admin/assessments.html`, `templates/agent/request.html`, `templates/agent/dashboard.html`,
  `templates/profile/view.html`
- Test: `tests/unit/test_display_format.py`, Create `tests/unit/test_timestamps_have_one_formatter.py`

**Interfaces:**
- Consumes: Jinja filter `ts` = `display_format.timestamp` (`src/web/templating.py:28`).
- Produces: `def timestamp(dt: datetime | None, style: str = "minute") -> str`, styles `"minute"`
  (`2026-10-01 14:05 UTC`), `"second"` (`2026-10-01 14:05:07 UTC`), `"date"` (`2026-10-01 UTC`); None → `"—"`.
  Template use: `{{ dt | ts }}`, `{{ dt | ts("second") }}`, `{{ dt | ts("date") }}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_display_format.py`:

```python
def test_timestamp_styles_always_name_the_zone():
    dt = datetime(2026, 10, 1, 14, 5, 7, tzinfo=UTC)
    assert f.timestamp(dt) == "2026-10-01 14:05 UTC"
    assert f.timestamp(dt, "minute") == "2026-10-01 14:05 UTC"
    assert f.timestamp(dt, "second") == "2026-10-01 14:05:07 UTC"
    assert f.timestamp(dt, "date") == "2026-10-01 UTC"
    assert f.timestamp(None, "date") == "—"
    # A non-UTC aware value is converted, never printed in its own zone.
    plus_two = datetime(2026, 10, 1, 16, 5, tzinfo=timezone(timedelta(hours=2)))
    assert f.timestamp(plus_two) == "2026-10-01 14:05 UTC"
```

Create `tests/unit/test_timestamps_have_one_formatter.py`:

```python
"""FN-07: every human-facing timestamp goes through `display_format.timestamp` (the
`ts` filter) and so always shows its zone; nothing converts to browser-local time."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"


def test_no_template_formats_a_timestamp_itself():
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in sorted(TEMPLATES.rglob("*.html"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "strftime(" in line or "data-utc" in line
    ]
    assert offenders == []


def test_nothing_converts_to_browser_local_time():
    sources = [*TEMPLATES.rglob("*.html"), *(ROOT / "static/js").rglob("*.js")]
    offenders = [
        str(path.relative_to(ROOT))
        for path in sources
        if "toLocaleTimeString" in path.read_text(encoding="utf-8")
        or "toLocaleDateString" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/unit/test_display_format.py tests/unit/test_timestamps_have_one_formatter.py -v`. Expected: `test_timestamp_styles_always_name_the_zone` fails with `TypeError: timestamp() takes 1 positional argument but 2 were given`; both source tests fail listing every template line.

- [ ] **Step 3: Implement the formatter** — in `src/services/display_format.py` replace

```python
def timestamp(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return _utc(dt).strftime("%Y-%m-%d %H:%M UTC")
```

with

```python
_TIMESTAMP_FORMATS = {
    "minute": "%Y-%m-%d %H:%M UTC",
    "second": "%Y-%m-%d %H:%M:%S UTC",
    "date": "%Y-%m-%d UTC",
}


def timestamp(dt: datetime | None, style: str = "minute") -> str:
    """The one human-facing rendering of a moment (FN-07): converted to UTC and
    always labelled "UTC", including the date-only style, so no page mixes zones.
    Templates reach it as the `ts` filter. `style` is "minute", "second" or "date"."""
    if dt is None:
        return "—"
    return _utc(dt).strftime(_TIMESTAMP_FORMATS[style])
```

In `src/services/assessment_chat_record.py` replace the comment in `_minute`

```python
    # The page renders `created_at.strftime('%Y-%m-%d %H:%M')` with no conversion;
    # asyncpg hands timestamptz back in UTC.
```

with

```python
    # The same UTC minute the page shows through display_format.timestamp, whose
    # " UTC" suffix this record carries in its block labels instead; asyncpg hands
    # timestamptz back in UTC.
```

- [ ] **Step 4: Convert every template timestamp** — apply these rules to every line the source test
  lists (all files in **Files** above). `X` is the datetime expression.
  - `X.strftime('%b %d')`, `X.strftime('%b %d, %Y')`, `X.strftime('%b %d %Y')` → `X | ts("date")`
  - `X.strftime('%b %d %H:%M')`, `X.strftime('%Y-%m-%d %H:%M')`, `X.strftime("%Y-%m-%d %H:%M")`,
    `X.strftime('%b %d, %Y %H:%M')`, `X.strftime('%b %d, %Y at %H:%M')`,
    `X.strftime('%Y-%m-%d %H:%M UTC')`, `X.strftime('%H:%M')` → `X | ts`
  - `X.strftime('%b %d %H:%M:%S')`, `X.strftime('%H:%M:%S')` → `X | ts("second")`
  - `X.strftime(...) if X else 'Unclaimed'` keeps its fallback: `(X | ts) if X else 'Unclaimed'`;
    `X.strftime(...) if X else '—'` → `X | ts` (`ts` already returns `—` for None).
  - `<span data-utc="{{ X.isoformat() }}" data-utc-fmt="F">INNER</span>` → INNER converted by the rules
    above, the `<span …>`/`</span>` pair removed. On a `<td …>` carrying `data-utc="…" data-utc-fmt="…"`,
    delete those two attributes only.
  - `templates/admin/simulation.html` uses server-formatted values (`fmt.timestamp`) except one line
    Phase 0 Task 0-5 added to the refresh script:
    `if (updated) updated.textContent = '· last updated ' + new Date().toLocaleTimeString();`. Change it
    to `if (updated) updated.textContent = '· last updated ' + new Date().toISOString().slice(11, 19) + ' UTC';`
    (plan audit Q2-05), so the panel's own clock is UTC-labelled like every other timestamp.
  Then delete from `templates/base.html` the whole block

```html
<script>
// Convert all elements with data-utc attribute to local time
document.addEventListener('DOMContentLoaded', function() {
```

  through its closing `</script>` (Part 1A gave that `<script>` a `nonce=` attribute; the block is deleted
  with its tag whatever its attributes).

- [ ] **Step 5: Run** — `.venv-test/bin/python -m pytest tests/unit/test_display_format.py tests/unit/test_timestamps_have_one_formatter.py tests/unit/test_reachability.py -v`, then `.venv-test/bin/python -m pytest tests/integration/test_admin_jobs_page.py tests/integration/test_manager_views.py -v`. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/services/display_format.py src/services/assessment_chat_record.py templates tests/unit/test_display_format.py tests/unit/test_timestamps_have_one_formatter.py
git commit -m "fix(webui-2): one UTC-labelled timestamp formatter for every page (FN-07)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-3: Cache the live run's stats for 25 s; deterministic hub pick (C-14, C-23)

**Files:**
- Modify: `src/services/simulation_view.py:1-12` (imports), `:443-547` (cache and `live_tab_context`)
- Test: `tests/integration/test_simulation_page_queries.py`

**Interfaces:**
- Consumes: `fetch_call_stat_rows`, the `simulation_stats` aggregates already imported; `_ENDED_RUN_STATS`.
- Produces: `LIVE_RUN_STATS_TTL_SECONDS = 25.0`; `_LIVE_RUN_STATS: OrderedDict[tuple, tuple[float, dict]]`;
  `_LIVE_RUN_STATS_MAX = 4`; `_clock` (= `time.monotonic`, the test seam);
  `async def _hub_agent_id(db) -> str | None`; `async def _run_aggregates(db, run) -> dict[str, Any]`;
  `async def _cached_run_aggregates(db, run) -> dict[str, Any]`.

Design: key `(run.id, run.status, run.ended_at)`; only a `running` run is cached here (an ended run keeps
the existing `_ENDED_RUN_STATS` for its call-stats pair). TTL 25 s (below the panel's 30 s refresh, so one
tab still sees new figures every refresh while N tabs share one computation). Invalidation: a stop,
finalize or resume changes `status`/`ended_at`, hence the key; nothing else invalidates, so the stats
sections may lag the status card by up to 25 s (the status card, forms, command and audit lists are not
cached). The per-agent live columns (`active_threads`, `calls_in_window`) are recomputed from the
heartbeat row on every render. Bounded to 4 entries (one engine at a time means one running run).
Multi-worker: see Global constraints. Anything Part 1A/1C reads from `live_tab_context`'s return value
(e.g. the Stop dialog's owed-headline count) is up to 25 s old for a running run.
Known, accepted staleness (plan audit Q2-18): the key is the same before a Stop and after a Resume of the same run, so figures can lag a resume by up to the 25 s TTL; the status card is not cached.

- [ ] **Step 1: Write the failing tests** — append to `tests/integration/test_simulation_page_queries.py`:

```python
async def test_a_running_runs_stats_are_cached_for_the_ttl(client, db_session, engine, monkeypatch):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session, status="running")
    await factories.make_llm_call_log(db_session, run=run, call_stats=[{"stop_reason": "end_turn", "latency_ms": 5}])
    clock = [1000.0]
    monkeypatch.setattr(simulation_view, "_clock", lambda: clock[0])
    simulation_view._LIVE_RUN_STATS.clear()
    seen, stop = _count_call_stats_selects(engine)
    url = f"/admin/simulation?run={run.id}"
    try:
        assert (await client.get(url, headers=auth_headers(admin.id))).status_code == 200
        clock[0] += simulation_view.LIVE_RUN_STATS_TTL_SECONDS - 1
        await client.get(url, headers=auth_headers(admin.id))
        within_ttl = len(seen)
        clock[0] += 2
        await client.get(url, headers=auth_headers(admin.id))
    finally:
        stop()
        simulation_view._LIVE_RUN_STATS.clear()
    assert within_ttl == 1, "the second render inside the TTL is served from the cache"
    assert len(seen) == 2, "past the TTL the stats are recomputed"


async def test_a_status_change_is_a_new_cache_key(client, db_session, engine, monkeypatch):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session, status="running")
    await factories.make_llm_call_log(db_session, run=run, call_stats=[{"stop_reason": "end_turn", "latency_ms": 5}])
    monkeypatch.setattr(simulation_view, "_clock", lambda: 1000.0)
    simulation_view._LIVE_RUN_STATS.clear()
    simulation_view._ENDED_RUN_STATS.clear()
    seen, stop = _count_call_stats_selects(engine)
    url = f"/admin/simulation?run={run.id}"
    try:
        await client.get(url, headers=auth_headers(admin.id))
        run.status = "stopped"
        run.ended_at = datetime.now(UTC)
        await db_session.flush()
        await client.get(url, headers=auth_headers(admin.id))
    finally:
        stop()
        simulation_view._LIVE_RUN_STATS.clear()
        simulation_view._ENDED_RUN_STATS.clear()
    assert len(seen) == 2, "the stopped run is not served the running run's cached figures"


async def test_the_burn_chart_hub_is_deterministic(db_session):
    from sqlalchemy import select

    from src.agent.role_capabilities import hub_role_names
    from src.models import AgentRegistry

    hub_role = hub_role_names()[0]
    await factories.make_agent(db_session, agent_id="zz-live-hub", role=hub_role, status="active")
    await factories.make_agent(db_session, agent_id="aa-parked-hub", role=hub_role, status="inactive")
    rows = (
        await db_session.execute(
            select(AgentRegistry.agent_id, AgentRegistry.status).where(
                AgentRegistry.role.in_(hub_role_names())
            )
        )
    ).all()
    expected = sorted(rows, key=lambda r: (r.status != "active", r.agent_id))[0].agent_id
    assert await simulation_view._hub_agent_id(db_session) == expected
    assert expected != "aa-parked-hub", "an active hub outranks an alphabetically earlier parked one"
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_simulation_page_queries.py -v`. Expected: the three new tests fail with `AttributeError: ... has no attribute '_clock'` / `'_LIVE_RUN_STATS'` / `'_hub_agent_id'`.

- [ ] **Step 3: Implement** — in `src/services/simulation_view.py` add `import time` after `import uuid`.
  After `_call_stat_aggregates` (ends `return cached["taxonomy"], cached["latency"]`) insert:

```python
#: C-14: the Live tab's DB aggregates for a RUNNING run, per process, keyed by
#: (run id, status, ended_at) so a stop, finalize or resume is a new key, and kept
#: LIVE_RUN_STATS_TTL_SECONDS, below the panel's 30 s refresh: one open tab still
#: sees fresh figures on every refresh, and N tabs (or N admins) share one
#: computation. Per uvicorn process; the web container runs one. Bounded because a
#: resume or a fresh run adds a key and nothing else removes one.
LIVE_RUN_STATS_TTL_SECONDS = 25.0
_LIVE_RUN_STATS: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_LIVE_RUN_STATS_MAX = 4
#: Monotonic clock for the TTL; a module attribute so tests can move it.
_clock = time.monotonic


async def _hub_agent_id(db: AsyncSession) -> str | None:
    """The hub whose lab burn the Live tab charts (C-23): the active hub-role agent,
    else the first hub-role agent by agent_id — never an unordered pick."""
    return (
        await db.execute(
            select(AgentRegistry.agent_id)
            .where(AgentRegistry.role.in_(hub_role_names()))
            .order_by((AgentRegistry.status == "active").desc(), AgentRegistry.agent_id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _run_aggregates(db: AsyncSession, run: SimulationRun) -> dict[str, Any]:
    """Every DB-derived figure the Live tab renders for ONE run. All values are
    frozen dataclasses, lists of them, or ints, so a cached copy is safe to share."""
    run_id = run.id
    taxonomy, latency = await _call_stat_aggregates(db, run)
    total_call_rows = (
        await db.execute(
            select(func.count())
            .select_from(LlmCallLog)
            .where(LlmCallLog.simulation_run_id == run_id)
        )
    ).scalar_one()
    hub_agent_id = await _hub_agent_id(db)
    return {
        "overview": await run_overview(db, run_id),
        "cost": await cost_summary(db, run_id),
        "hours": await hourly_activity(db, run_id),
        "fun": await funnel(db, run_id),
        "domains": await specialist_mix(db, run_id),
        "fanout": await consult_fanout(db, run_id),
        "agents": await per_agent(db, run_id),
        "taxonomy": taxonomy,
        "latency": latency,
        "timeline": await interview_timeline(db, run_id),
        "per_interview": await cost_per_interview(db, run_id),
        "stage_costs": await cost_by_stage(db, run_id),
        "specialist_costs": await cost_by_specialist(db, run_id),
        "call_kind_costs": await cost_by_call_kind(db, run_id),
        "total_call_rows": total_call_rows,
        "burn_points": await hub_lab_burn(db, run_id, hub_agent_id) if hub_agent_id else [],
    }


async def _cached_run_aggregates(db: AsyncSession, run: SimulationRun) -> dict[str, Any]:
    """`_run_aggregates`, served from `_LIVE_RUN_STATS` for a running run inside the TTL."""
    if run.status != "running":
        return await _run_aggregates(db, run)
    key = (run.id, run.status, run.ended_at)
    now = _clock()
    hit = _LIVE_RUN_STATS.get(key)
    if hit is not None and now - hit[0] < LIVE_RUN_STATS_TTL_SECONDS:
        _LIVE_RUN_STATS.move_to_end(key)
        return hit[1]
    bundle = await _run_aggregates(db, run)
    _LIVE_RUN_STATS[key] = (now, bundle)
    _LIVE_RUN_STATS.move_to_end(key)
    while len(_LIVE_RUN_STATS) > _LIVE_RUN_STATS_MAX:
        _LIVE_RUN_STATS.popitem(last=False)
    return bundle
```

  In `live_tab_context`, replace the block from `run_id = selected_run.id` through
  `burn_points = await hub_lab_burn(db, run_id, hub_agent_id) if hub_agent_id else []` with:

```python
    stats = await _cached_run_aggregates(db, selected_run)
    overview, cost, fun = stats["overview"], stats["cost"], stats["fun"]
    latency_capped = stats["total_call_rows"] > CALL_STATS_ROW_LIMIT
```

  and replace the composition lines that follow with:

```python
    kpi = _kpi_tiles(overview, cost, fun)
    cost_time = _cost_over_time(stats["hours"])
    breakdowns = _cost_breakdowns(cost, stats["stage_costs"], stats["specialist_costs"], stats["call_kind_costs"])
    panel = _funnel_panel_and_latency(fun, stats["domains"], stats["fanout"], stats["taxonomy"], stats["latency"])
    per_agent_rows = _per_agent_rows(stats["agents"], status_row, now)
    gantt_parts = _interview_gantt(stats["timeline"], stats["per_interview"])
    burn_line_html = _burn_line(stats["burn_points"])
```

  (the `run_facts`/`process_facts` and the return dict are unchanged). Update the `live_tab_context`
  docstring's first sentence to: "Every value the Live tab's stats sections render, for ONE run — see the
  module comment above; a running run's DB aggregates come from a 25 s cache (`_cached_run_aggregates`)."

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/integration/test_simulation_page_queries.py tests/integration/test_admin_simulation_page.py -v`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/simulation_view.py tests/integration/test_simulation_page_queries.py
git commit -m "perf(webui-2): cache a running run's Live-tab stats for 25 s; order the hub pick (C-14, C-23)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-4: Discussions paged in SQL; `run_id=all` capped for HTML (C-15)

**Files:**
- Modify: `src/services/directory.py:20-23` (imports), `:67-72` (constants), `:832-1090` (replace
  `_load_thread_inputs`, `_thread_status`, `_build_threads`, `_status_counts`, `_collect_agents`,
  `_apply_filters`, `build_discussions_view`; keep `_select_run`)
- Modify: `src/routers/admin/discussions.py` (normal-path template context, after `thread_total=view["thread_total"],`)
- Modify: `templates/admin/_discussions_threads.html` (before `<!-- Threads table -->`)
- Test: `tests/unit/test_discussions_split.py`, Create `tests/integration/test_discussions_sql_paging.py`

**Interfaces:**
- Consumes: `_select_run`, `runs_ordered`, `DISCUSSIONS_PAGE_SIZE`, frozen oracle `tests/unit/_frozen_discussions.py`.
- Produces: `DISCUSSIONS_ALL_RUNS_MAX = 2_000`; `build_discussions_view(...)` (same signature) returns the
  same keys plus `all_runs_refused: bool`; thread dicts unchanged (`message_ts, channel_name, agent_id,
  created_at, reply_count, replier, status, decision`).

Semantics kept from the Python version (the frozen oracle): a thread is a root post (`new_post`, no
`thread_ts`) or an orphaned decision; its decision is the last by `decided_at`; reply counts and
repliers span every run under `run_id=all`; `counts`, `channels`, `agents` are computed before filters.
Two nondeterminisms become deterministic (documented in the docstring): among several non-poster
repliers the shown `replier` is the alphabetically first and the agent filter/agent list consider all
of them (the Python version picked one by set order); orphaned threads sort by their last decision.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_discussions_split.py`:

```python
async def test_all_runs_parity_with_duplicate_ts_and_a_twice_decided_orphan(db_session):
    from datetime import UTC, datetime

    run_a = await factories.make_simulation_run(db_session)
    run_b = await factories.make_simulation_run(db_session)
    for run in (run_a, run_b):
        root = await factories.make_agent_message(
            db_session, run=run, phase="new_post", thread_ts=None,
            message_ts="dup.1", channel_name="dup", agent_id="lab1",
        )
        await factories.make_agent_message(
            db_session, run=run, phase="thread_reply", thread_ts=root.message_ts,
            message_ts=f"dup.1.{run.id.hex[:4]}", agent_id="hub",
        )
    await factories.make_thread_decision(
        db_session, run=run_a, thread_id="orphan.2", outcome="timeout",
        agent_a="lab7", agent_b="hub", channel="c7", decided_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    # Distinct decided_at: inside one test transaction server_default now() ties, and
    # "last decision" would then be the database's arbitrary pick in both versions.
    await factories.make_thread_decision(
        db_session, run=run_b, thread_id="orphan.2", outcome="proposal",
        agent_a="lab7", agent_b="hub", channel="c7", decided_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    for kw in (
        dict(run_id="all", channel_filter=None, status_filter=None, agent_filter=[]),
        dict(run_id="all", channel_filter=None, status_filter=None, agent_filter=["lab7"]),
        dict(run_id=str(run_a.id), channel_filter="dup", status_filter="active", agent_filter=["hub"]),
    ):
        new = await directory.build_discussions_view(db_session, page=None, **kw)
        old = await frozen(db_session, **kw)
        assert {k: new[k] for k in old if k != "threads"} == {k: old[k] for k in old if k != "threads"}
        assert _by_ts(new["threads"]) == _by_ts(old["threads"])
```

Create `tests/integration/test_discussions_sql_paging.py`:

```python
"""C-15: discussions are filtered, counted and paged in SQL; the HTML page refuses
`run_id=all` above DISCUSSIONS_ALL_RUNS_MAX while the export lists everything."""

import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN
from src.services import directory
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_KW = dict(channel_filter=None, status_filter=None, agent_filter=[])


async def _seed(db_session, n):
    run = await factories.make_simulation_run(db_session)
    for i in range(n):
        root = await factories.make_agent_message(
            db_session, run=run, phase="new_post", thread_ts=None,
            message_ts=f"500.{i}", channel_name="paging", agent_id="lab1",
        )
        await factories.make_thread_decision(
            db_session, run=run, thread_id=root.message_ts, outcome="timeout",
            agent_a="lab1", agent_b="hub", channel="paging",
        )
    return run


async def test_only_the_pages_decisions_are_loaded(db_session, engine, monkeypatch):
    monkeypatch.setattr(directory, "DISCUSSIONS_PAGE_SIZE", 2)
    run = await _seed(db_session, 5)
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        if "thread_decisions.summary_text" in statement:
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        view = await directory.build_discussions_view(db_session, run_id=str(run.id), page=2, **_KW)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert [t["message_ts"] for t in view["threads"]] == ["500.2", "500.3"]
    assert view["thread_total"] == 5 and view["page_count"] == 3
    assert seen and all(" IN (" in s for s in seen), "decision rows are loaded for the page only"


async def test_html_refuses_all_runs_above_the_cap_but_export_does_not(client, db_session, monkeypatch):
    monkeypatch.setattr(directory, "DISCUSSIONS_ALL_RUNS_MAX", 2)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _seed(db_session, 3)

    page = await directory.build_discussions_view(db_session, run_id="all", page=1, **_KW)
    assert page["all_runs_refused"] is True and page["threads"] == []
    export = await directory.build_discussions_view(db_session, run_id="all", page=None, **_KW)
    assert export["all_runs_refused"] is False and len(export["threads"]) >= 3
    one_run = await directory.build_discussions_view(db_session, run_id=str(run.id), page=1, **_KW)
    assert one_run["all_runs_refused"] is False and len(one_run["threads"]) == 3

    html = (await client.get("/admin/discussions?run_id=all", headers=auth_headers(admin.id))).text
    assert "too many to list on one page" in html
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/unit/test_discussions_split.py tests/integration/test_discussions_sql_paging.py -v`. Expected: the parity test passes against today's code (it is the oracle check); `test_only_the_pages_decisions_are_loaded` fails (`seen` statements carry no `IN (`: today every decision row is loaded); the cap test fails with `KeyError: 'all_runs_refused'`.

- [ ] **Step 3: Implement** — in `src/services/directory.py` change the sqlalchemy imports to:

```python
from sqlalchemy import String, and_, case, cast, func, literal_column, or_, select, union_all
from sqlalchemy import true as sa_true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload
```

  after `DISCUSSIONS_PAGE_SIZE = 200` add:

```python
#: C-15: the HTML discussions page lists `run_id=all` only while every run together
#: holds at most this many threads; above it the page asks for one run. The admin
#: export (`page=None`) is never capped.
DISCUSSIONS_ALL_RUNS_MAX = 2_000
```

  Delete `_load_thread_inputs`, `_thread_status`, `_build_threads`, `_status_counts`, `_collect_agents`
  and `_apply_filters`, and replace `build_discussions_view` with:

```python
def _run_scope(column, selected_run_id, show_all_runs: bool) -> list[Any]:
    return [] if show_all_runs else [column == selected_run_id]


def _thread_rows(selected_run_id, show_all_runs: bool):
    """One SQL row per thread: every root post (``new_post``, no ``thread_ts``), then
    every thread whose decisions have no root post. A thread's decision is its LAST by
    ``decided_at``; reply counts span every run under ``run_id=all``. ``grp`` is 0 for
    a root, 1 for an orphan; ``row_key`` is a unique tiebreak for stable paging."""
    roots = (
        select(
            AgentMessage.message_ts.label("ts"),
            AgentMessage.channel_name.label("channel"),
            AgentMessage.agent_id.label("poster"),
            AgentMessage.created_at.label("at"),
            AgentMessage.id.label("row_key"),
        )
        .where(
            AgentMessage.phase == "new_post",
            AgentMessage.thread_ts.is_(None),
            *_run_scope(AgentMessage.simulation_run_id, selected_run_id, show_all_runs),
        )
        .cte("roots")
    )
    replies = (
        select(AgentMessage.thread_ts.label("ts"), func.count(AgentMessage.id).label("n"))
        .where(
            AgentMessage.phase == "thread_reply",
            *_run_scope(AgentMessage.simulation_run_id, selected_run_id, show_all_runs),
        )
        .group_by(AgentMessage.thread_ts)
        .cte("replies")
    )
    last_dec = (
        select(
            ThreadDecision.thread_id.label("ts"),
            ThreadDecision.id.label("decision_id"),
            cast(ThreadDecision.outcome, String).label("outcome"),
            ThreadDecision.channel,
            ThreadDecision.agent_a,
            ThreadDecision.agent_b,
            ThreadDecision.decided_at,
        )
        .where(*_run_scope(ThreadDecision.simulation_run_id, selected_run_id, show_all_runs))
        .distinct(ThreadDecision.thread_id)
        .order_by(ThreadDecision.thread_id, ThreadDecision.decided_at.desc(), ThreadDecision.id.desc())
        .cte("last_dec")
    )
    root_replies = func.coalesce(replies.c.n, literal_column("0"))
    root_rows = select(
        roots.c.ts.label("message_ts"),
        roots.c.channel.label("channel_name"),
        roots.c.poster.label("agent_id"),
        roots.c.at.label("created_at"),
        root_replies.label("reply_count"),
        case(
            (last_dec.c.decision_id.is_not(None), last_dec.c.outcome),
            (root_replies > 0, literal_column("'active'")),
            else_=literal_column("'no_replies'"),
        ).label("status"),
        last_dec.c.decision_id,
        last_dec.c.agent_a,
        last_dec.c.agent_b,
        literal_column("0").label("grp"),
        roots.c.row_key,
    ).select_from(
        roots.outerjoin(replies, replies.c.ts == roots.c.ts).outerjoin(
            last_dec, last_dec.c.ts == roots.c.ts
        )
    )
    orphan_rows = (
        select(
            last_dec.c.ts,
            last_dec.c.channel,
            last_dec.c.agent_a,
            last_dec.c.decided_at,
            func.coalesce(replies.c.n, literal_column("0")),
            last_dec.c.outcome,
            last_dec.c.decision_id,
            last_dec.c.agent_a,
            last_dec.c.agent_b,
            literal_column("1"),
            last_dec.c.decision_id,
        )
        .select_from(last_dec.outerjoin(replies, replies.c.ts == last_dec.c.ts))
        .where(~select(roots.c.ts).where(roots.c.ts == last_dec.c.ts).exists())
    )
    return union_all(root_rows, orphan_rows).subquery("threads")


def _repliers_of(threads, scope_for_reply):
    """(thread message_ts, agent_id) of every non-poster replier of a root thread."""
    rep = aliased(AgentMessage)
    return (
        select(threads.c.message_ts, rep.agent_id)
        .select_from(threads)
        .join(rep, and_(
            rep.thread_ts == threads.c.message_ts, rep.phase == "thread_reply", *scope_for_reply(rep),
        ))
        .where(
            threads.c.grp == 0,
            rep.agent_id.is_not(None),
            rep.agent_id.is_distinct_from(threads.c.agent_id),
        )
        .distinct()
    )


def _thread_conditions(threads, scope_for_reply, channel_filter, status_filter, agent_filter) -> list[Any]:
    """The channel, status and agent filters as SQL. An agent matches as poster, as
    either side of the thread's decision, or (root threads) as any non-poster replier."""
    conds: list[Any] = []
    if channel_filter:
        conds.append(threads.c.channel_name == channel_filter)
    if status_filter:
        conds.append(threads.c.status == status_filter)
    if agent_filter:
        agents = list(agent_filter)
        rep = aliased(AgentMessage)
        replier_match = (
            select(rep.id)
            .where(
                rep.phase == "thread_reply",
                rep.thread_ts == threads.c.message_ts,
                rep.agent_id.in_(agents),
                rep.agent_id.is_distinct_from(threads.c.agent_id),
                *scope_for_reply(rep),
            )
            .exists()
        )
        conds.append(or_(
            threads.c.agent_id.in_(agents),
            threads.c.agent_a.in_(agents),
            threads.c.agent_b.in_(agents),
            and_(threads.c.grp == 0, replier_match),
        ))
    return conds


async def _thread_summary(db: AsyncSession, threads, scope_for_reply) -> tuple[dict[str, int], list[str], list[str]]:
    """Status counts, channels and agents over the whole selection, before any filter:
    each summary card links to a status filter alone, so its number must be what that
    link lists."""
    counts = {
        status: n
        for status, n in (
            await db.execute(select(threads.c.status, func.count()).group_by(threads.c.status))
        ).all()
    }
    channels = sorted(
        c for c in (await db.execute(select(threads.c.channel_name).distinct())).scalars() if c
    )
    agents: set[str] = set()
    for row in (
        await db.execute(select(threads.c.agent_id, threads.c.agent_a, threads.c.agent_b).distinct())
    ).all():
        agents.update(a for a in row if a)
    agents.update(a for _ts, a in (await db.execute(_repliers_of(threads, scope_for_reply))).all() if a)
    return counts, channels, sorted(agents)


async def _thread_page(db: AsyncSession, threads, conds: list[Any], page: int | None, scope: list[Any]) -> list[dict[str, Any]]:
    """The filtered threads of one page (every one when ``page`` is None), with each
    row's decision loaded for that page only."""
    query = select(threads).where(*conds).order_by(
        threads.c.grp, threads.c.created_at, threads.c.message_ts, threads.c.row_key
    )
    if page is not None:
        query = query.limit(DISCUSSIONS_PAGE_SIZE).offset((page - 1) * DISCUSSIONS_PAGE_SIZE)
    rows = (await db.execute(query)).all()
    decision_ids = [r.decision_id for r in rows if r.decision_id is not None]
    decisions = (
        {d.id: d for d in (await db.execute(select(ThreadDecision).where(ThreadDecision.id.in_(decision_ids)))).scalars()}
        if decision_ids else {}
    )
    page_ts = [r.message_ts for r in rows if r.grp == 0]
    repliers: dict[str, set[str]] = {}
    if page_ts:
        for ts, agent in (
            await db.execute(
                select(AgentMessage.thread_ts, AgentMessage.agent_id)
                .where(
                    AgentMessage.phase == "thread_reply",
                    AgentMessage.thread_ts.in_(page_ts),
                    AgentMessage.agent_id.is_not(None),
                    *scope,
                )
                .distinct()
            )
        ).all():
            repliers.setdefault(ts, set()).add(agent)
    return [
        {
            "message_ts": r.message_ts,
            "channel_name": r.channel_name,
            "agent_id": r.agent_id,
            "created_at": r.created_at,
            "reply_count": r.reply_count,
            "replier": (
                min(repliers.get(r.message_ts, set()) - {r.agent_id}, default=None)
                if r.grp == 0 else r.agent_b
            ),
            "status": r.status,
            "decision": decisions.get(r.decision_id),
        }
        for r in rows
    ]


async def build_discussions_view(
    db: AsyncSession,
    *,
    run_id: str | None,
    channel_filter: str | None,
    status_filter: str | None,
    agent_filter: list[str],
    page: int | None = 1,
) -> dict[str, Any]:
    """Discussion summary: threads grouped by status, filtered and paged in SQL (C-15).

    ``page=None`` returns every thread (the admin export); otherwise ``threads`` is
    one page of ``DISCUSSIONS_PAGE_SIZE`` and the result carries ``page``,
    ``page_count`` and ``thread_total`` (the filtered thread count). ``counts``,
    ``channels`` and ``agents`` cover the whole selection before any filter or paging.
    ``all_runs_refused`` is True when ``run_id="all"`` is asked for a page and every
    run together holds more than ``DISCUSSIONS_ALL_RUNS_MAX`` threads; ``threads`` is
    then empty and the page asks for one run.

    Thread semantics are those of the frozen oracle
    (tests/unit/_frozen_discussions.py): a root post or an orphaned decision; the last
    decision by ``decided_at``; replies across every run under ``run_id="all"``. Where
    that version depended on set order, this one is deterministic: the shown
    ``replier`` is the alphabetically first non-poster replier, the agent filter and
    the agent list consider every non-poster replier, and an orphan sorts by its last
    decision.

    Stops before the router's ``if export:`` branch, and both the no-runs early return
    and the normal return yield the same keys, so a template can rely on them.
    """
    runs = await runs_ordered(db)
    selected_run_id = _select_run(runs, run_id)

    if not selected_run_id:
        return {
            "runs": runs,
            "selected_run_id": None,
            "threads": [],
            "counts": {},
            "channels": [],
            "agents": [],
            "channel_filter": channel_filter,
            "status_filter": status_filter,
            "agent_filter": [],
            "page": 1,
            "page_count": 1,
            "thread_total": 0,
            "all_runs_refused": False,
        }

    show_all_runs = run_id == "all"
    scope = _run_scope(AgentMessage.simulation_run_id, selected_run_id, show_all_runs)

    def scope_for_reply(rep):
        return _run_scope(rep.simulation_run_id, selected_run_id, show_all_runs)

    threads = _thread_rows(selected_run_id, show_all_runs)
    counts, channels, agents = await _thread_summary(db, threads, scope_for_reply)
    conds = _thread_conditions(threads, scope_for_reply, channel_filter, status_filter, agent_filter)
    thread_total = (
        await db.execute(select(func.count()).select_from(threads).where(*conds))
    ).scalar_one()
    page_count = max(1, -(-thread_total // DISCUSSIONS_PAGE_SIZE))
    page_out = 1 if page is None else max(1, page)
    refused = show_all_runs and page is not None and sum(counts.values()) > DISCUSSIONS_ALL_RUNS_MAX
    listed = [] if refused else await _thread_page(
        db, threads, conds, None if page is None else page_out, scope
    )

    return {
        "runs": runs,
        "selected_run_id": selected_run_id,
        "threads": listed,
        "counts": counts,
        "channels": channels,
        "agents": agents,
        "channel_filter": channel_filter,
        "status_filter": status_filter,
        "agent_filter": agent_filter or [],
        "page": page_out,
        "page_count": page_count,
        "thread_total": thread_total,
        "all_runs_refused": refused,
    }
```

  In `src/routers/admin/discussions.py`, in the final `_template_context(...)` call add after
  `thread_total=view["thread_total"],`:

```python
            all_runs_refused=view["all_runs_refused"],
```

  (the manager route passes `**view`, so it gets the key unchanged). In
  `templates/admin/_discussions_threads.html` insert immediately before `<!-- Threads table -->`:

```html
{# C-15: `run_id=all` above DISCUSSIONS_ALL_RUNS_MAX (src/services/directory.py)
   lists nothing rather than paging the whole history. #}
{% if all_runs_refused %}
<div class="all-runs-refused rounded-lg border border-amber-300 bg-amber-50 p-3 mb-3 text-sm text-amber-900">
    All runs together hold too many threads, too many to list on one page. Choose a single run above.{% if admin_view %} Export still downloads every thread.{% endif %}
</div>
{% endif %}
```

  (The copy contains "too many to list on one page", which the test asserts.)

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/unit/test_discussions_split.py tests/integration/test_discussions_sql_paging.py tests/integration/test_discussions_filters.py tests/integration/test_discussions_panel_cards.py tests/integration/test_manager_views.py tests/unit/test_no_dead_src_symbols.py tests/unit/test_function_length_gate.py -v`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/directory.py src/routers/admin/discussions.py templates/admin/_discussions_threads.html tests/unit/test_discussions_split.py tests/integration/test_discussions_sql_paging.py
git commit -m "perf(webui-2): page discussions in SQL and cap run_id=all for HTML (C-15)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-5: PI directory paged in SQL (C-15)

**Files:**
- Modify: `src/services/directory.py:20-40` (imports: `Text`, `ResearcherProfile`), `:68-70` (constant), `:161-262` (`list_pi_directory`; add `_pi_directory_query`, `count_pi_directory`)
- Modify: `src/routers/admin/users.py:5-16`, `:21-52` (`admin_users`)
- Modify: `src/routers/manager.py:168-197` (`manager_pis`)
- Modify: `templates/admin/users.html:7` and after the users table; `templates/manager/pis.html:7` and after the PIs table
- Test: Create `tests/integration/test_pi_directory_paging.py`

**Interfaces:**
- Consumes: `MAX_PAGE`; manager `_PAGE` (`src/routers/manager.py`).
- Produces: `PI_DIRECTORY_PAGE_SIZE = 100`; `list_pi_directory(db, *, status_filter=None,
  institution_filter=None, claimed_filter=None, roles=None, page: int | None = None) -> list[dict]`
  (page None = every row, as today); `async def count_pi_directory(db, *, status_filter=None,
  institution_filter=None, claimed_filter=None, roles=None) -> int`. Template context keys `user_total`,
  `page`, `page_count` on `admin/users.html` and `manager/pis.html`.

- [ ] **Step 1: Write the failing test** — create `tests/integration/test_pi_directory_paging.py`:

```python
"""C-15: the users list and the PI directory filter, order and page in SQL."""

import pytest

from src.models import USER_ROLE_ADMIN, Job
from src.services import directory
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_pages_partition_the_filtered_directory_in_name_order(db_session, monkeypatch):
    monkeypatch.setattr(directory, "PI_DIRECTORY_PAGE_SIZE", 2)
    for i in (3, 0, 4, 1, 2):
        await factories.make_user(db_session, name=f"Pager {i}", institution="Paging University")
    kw = dict(institution_filter="paging university")
    assert await directory.count_pi_directory(db_session, **kw) == 5
    pages = [await directory.list_pi_directory(db_session, page=p, **kw) for p in (1, 2, 3)]
    assert [len(p) for p in pages] == [2, 2, 1]
    assert [r["user"].name for p in pages for r in p] == [f"Pager {i}" for i in range(5)]


async def test_profile_status_is_computed_and_filtered_in_sql(db_session):
    inst = dict(institution="Status University")
    generating = await factories.make_user(db_session, **inst)
    await factories.make_profile(db_session, user=generating, research_summary="")
    db_session.add(Job(type="generate_profile", user_id=generating.id, payload={}, status="pending"))
    pending = await factories.make_user(db_session, **inst)
    await factories.make_profile(db_session, user=pending, pending_profile={"research_summary": "draft"})
    complete = await factories.make_user(db_session, **inst)
    await factories.make_profile(db_session, user=complete)
    bare = await factories.make_user(db_session, **inst)
    await db_session.flush()
    kw = dict(institution_filter="status university")
    rows = {r["user"].id: r["profile_status"] for r in await directory.list_pi_directory(db_session, **kw)}
    assert rows == {
        generating.id: "generating",
        pending.id: "pending_update",
        complete.id: "complete",
        bare.id: "no_profile",
    }
    only = await directory.list_pi_directory(db_session, status_filter="generating", **kw)
    assert [r["user"].id for r in only] == [generating.id]
    assert await directory.count_pi_directory(db_session, status_filter="generating", **kw) == 1


async def test_the_users_page_shows_the_total_and_a_pager(client, db_session, monkeypatch):
    monkeypatch.setattr(directory, "PI_DIRECTORY_PAGE_SIZE", 1)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session)
    html = (await client.get("/admin/users?page=2", headers=auth_headers(admin.id))).text
    total = await directory.count_pi_directory(db_session)
    assert f"{total} users" in html
    assert f"Page 2 of {total}" in html
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_pi_directory_paging.py -v`. Expected: fails with `AttributeError: module 'src.services.directory' has no attribute 'PI_DIRECTORY_PAGE_SIZE'`.

- [ ] **Step 3: Implement the service** — in `src/services/directory.py`: extend the sqlalchemy import with
  `Text` (`from sqlalchemy import String, Text, and_, case, cast, func, literal_column, or_, select, union_all`)
  and add `ResearcherProfile,` to the `from src.models import (...)` list (alphabetically after
  `PiIndustryScore,`). After `JOBS_PAGE_SIZE = 100` add:

```python
#: Rows per page of the admin users list and the manager PI directory (C-15).
PI_DIRECTORY_PAGE_SIZE = 100
```

  Replace `list_pi_directory` with:

```python
def _pi_directory_query(*, status_filter, institution_filter, claimed_filter, roles):
    """One row per user, ``(User, profile_status)``, every filter applied in SQL.

    ``profile_status`` mirrors the Python rules it replaced: no profile row ->
    ``no_profile``; a non-empty ``pending_profile`` -> ``pending_update``; a non-empty
    ``research_summary`` -> ``complete``; else ``generating`` while a profile job is
    pending or processing, otherwise ``no_profile``. "Non-empty" for the JSON column
    excludes the JSON scalar ``null`` and an empty object or list, which Python read
    as falsy."""
    active_job = (
        select(Job.id)
        .where(Job.user_id == User.id, Job.status.in_(("pending", "processing")))
        .exists()
    )
    status_expr = case(
        (ResearcherProfile.id.is_(None), literal_column("'no_profile'")),
        (
            and_(
                ResearcherProfile.pending_profile.is_not(None),
                cast(ResearcherProfile.pending_profile, Text).not_in(("null", "{}", "[]")),
            ),
            literal_column("'pending_update'"),
        ),
        (func.coalesce(ResearcherProfile.research_summary, "") != "", literal_column("'complete'")),
        (active_job, literal_column("'generating'")),
        else_=literal_column("'no_profile'"),
    )
    query = select(User, status_expr.label("profile_status")).outerjoin(
        ResearcherProfile, ResearcherProfile.user_id == User.id
    )
    if roles is not None:
        query = query.where(User.user_role.in_(roles))
    if status_filter:
        query = query.where(status_expr == status_filter)
    if institution_filter:
        # strpos, not LIKE: a substring match with no wildcard escaping to get wrong.
        query = query.where(func.strpos(func.lower(User.institution), institution_filter.lower()) > 0)
    if claimed_filter == "claimed":
        query = query.where(User.claimed_at.is_not(None))
    elif claimed_filter == "unclaimed":
        query = query.where(User.claimed_at.is_(None))
    return query


async def count_pi_directory(
    db: AsyncSession,
    *,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    roles: tuple[str, ...] | None = None,
) -> int:
    """How many rows `list_pi_directory` returns over all pages for the same filters."""
    query = _pi_directory_query(
        status_filter=status_filter, institution_filter=institution_filter,
        claimed_filter=claimed_filter, roles=roles,
    )
    return (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()


async def list_pi_directory(
    db: AsyncSession,
    *,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    roles: tuple[str, ...] | None = None,
    page: int | None = None,
) -> list[dict[str, Any]]:
    """Admin users overview / manager PI directory, by name (C-15: filtered and paged
    in SQL). ``page=None`` returns every row; otherwise one page of
    ``PI_DIRECTORY_PAGE_SIZE``.

    ``roles=None`` means no role filter at all — the `/admin` behaviour.
    ``roles=(USER_ROLE_PI,)`` is what the manager directory passes to see PIs only.
    """
    query = (
        _pi_directory_query(
            status_filter=status_filter, institution_filter=institution_filter,
            claimed_filter=claimed_filter, roles=roles,
        )
        .options(selectinload(User.profile), selectinload(User.agent))
        .order_by(User.name, User.id)
    )
    if page is not None:
        query = query.limit(PI_DIRECTORY_PAGE_SIZE).offset((page - 1) * PI_DIRECTORY_PAGE_SIZE)
    pairs = (await db.execute(query)).unique().all()
    users = [u for u, _ in pairs]
    status_by_user = {u.id: status for u, status in pairs}

    # Publication counts, scoped to each PI's JHU tenure window (Task 13 and
    # D17 of docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md).
    # `user.agent` is already eager-loaded above, so the legacy
    # agent-keyed tenure fallback (`scoped_counts`'s `agent_ids` argument) can
    # be built with no extra query — omitting it would silently unscope the
    # 62 PIs who only have a legacy entry.
    agent_ids_by_user = {u.id: (u.agent.agent_id if u.agent else None) for u in users}
    pub_scope_by_user = await scoped_counts(db, [u.id for u in users], agent_ids_by_user)

    # Latest industry-interest score per listed user (Postgres DISTINCT ON).
    industry_result = await db.execute(
        select(PiIndustryScore)
        .where(PiIndustryScore.user_id.in_([u.id for u in users]))
        .distinct(PiIndustryScore.user_id)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc())
    )
    industry_by_user = {row.user_id: row for row in industry_result.scalars().all()}

    user_data = []
    for user in users:
        pub_scope = pub_scope_by_user.get(user.id)
        # `pub_count` is the SCOPED (in-tenure) count, not the full-career count;
        # `pub_scope` carries the split for the templates that explain the number.
        pub_count = pub_scope.in_tenure if pub_scope else 0
        if not user.agent:
            agent_status = "not_requested"
        elif user.agent.status == "pending":
            agent_status = "awaiting_token"
        else:
            agent_status = user.agent.status  # "active" or "suspended"
        industry_row = industry_by_user.get(user.id)
        user_data.append({
            "user": user,
            "profile": user.profile,
            "profile_status": status_by_user[user.id],
            "pub_count": pub_count,
            "pub_scope": pub_scope,
            "agent_status": agent_status,
            "industry_score": industry_row.score if industry_row else None,
            "industry_reason": industry_row.reason if industry_row else None,
        })
    return user_data
```

- [ ] **Step 4: Implement the routes and templates** — `src/routers/admin/users.py`: change
  `from fastapi import Depends, Form, HTTPException, Request` to
  `from fastapi import Depends, Form, HTTPException, Query, Request`, the directory import to
  `from src.services import directory` and `from src.services.directory import (MAX_PAGE, count_pi_directory, list_pi_directory, load_user_detail)` (the page size is read as `directory.PI_DIRECTORY_PAGE_SIZE` at call time, so the tests' monkeypatch reaches it; plan audit Q2-04)
  (one name per line), add after `logger = ...`:

```python
_PAGE = Query(1, ge=1, le=MAX_PAGE)
```

  and in `admin_users` add the parameter `page: int = _PAGE,` after `claimed_filter`, then replace the body
  up to `return` with:

```python
    """Admin users overview, one page of PI_DIRECTORY_PAGE_SIZE (C-15)."""
    filters = dict(
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
    )
    user_data = await list_pi_directory(db, page=page, **filters)
    user_total = await count_pi_directory(db, **filters)
```

  and add to its `_template_context(...)` kwargs:

```python
            user_total=user_total,
            page=page,
            page_count=max(1, -(-user_total // directory.PI_DIRECTORY_PAGE_SIZE)),
```

  `src/routers/manager.py`: add `from src.services import directory` (if absent) and `count_pi_directory,` to the existing
  `from src.services.directory import (...)` list; in `manager_pis` add `page: int = _PAGE,` after
  `claimed_filter`, replace the `user_data = await list_pi_directory(...)` call with:

```python
    filters = dict(
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
        roles=(USER_ROLE_PI,),
    )
    user_data = await list_pi_directory(db, page=page, **filters)
    user_total = await count_pi_directory(db, **filters)
```

  and add `user_total=user_total, page=page, page_count=max(1, -(-user_total // directory.PI_DIRECTORY_PAGE_SIZE)),`
  to its context. Templates: in `templates/admin/users.html` replace
  `<span class="text-sm text-gray-500">{{ user_data | length }} users</span>` with
  `<span class="text-sm text-gray-500">{{ user_total }} users</span>`, and insert after the users table's
  closing `</div>` (the one closing `<!-- Users table -->`'s wrapper):

```html
{% if page_count > 1 %}
{% set pager_qs %}{% if status_filter %}status_filter={{ status_filter | urlencode }}&amp;{% endif %}{% if claimed_filter %}claimed_filter={{ claimed_filter | urlencode }}&amp;{% endif %}{% if institution_filter %}institution_filter={{ institution_filter | urlencode }}&amp;{% endif %}{% endset %}
<nav aria-label="Pages" class="flex flex-wrap items-center justify-between gap-2 mt-4 text-sm text-gray-600">
    <span>Page {{ page }} of {{ page_count }} ({{ user_total }} users)</span>
    <span class="flex gap-3">
        {% if page > 1 %}<a href="/admin/users?{{ pager_qs }}page={{ page - 1 }}" class="text-indigo-700 underline">&larr; Prev</a>{% endif %}
        {% if page < page_count %}<a href="/admin/users?{{ pager_qs }}page={{ page + 1 }}" class="text-indigo-700 underline">Next &rarr;</a>{% endif %}
    </span>
</nav>
{% endif %}
```

  In `templates/manager/pis.html` make the same two edits with `PIs` for `users` and `/manager/pis?` for
  `/admin/users?`.

- [ ] **Step 5: Run** — `.venv-test/bin/python -m pytest tests/integration/test_pi_directory_paging.py tests/integration/test_directory_service.py tests/integration/test_bounded_lists.py tests/integration/test_manager_views.py tests/unit/test_reachability.py -v`. Expected: all pass.

- [ ] **Step 6: Rebuild CSS and commit** — run `scripts/build_css.sh`, then:

```bash
git add src/services/directory.py src/routers/admin/users.py src/routers/manager.py templates/admin/users.html templates/manager/pis.html static/css/app.css tests/integration/test_pi_directory_paging.py
git commit -m "perf(webui-2): page the users list and PI directory in SQL (C-15)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-6: LLM calls: deferred bodies and a fragment route; page bound; latency and labels (C-16, C-19, C-21, C-22)

**Files:**
- Modify: `src/routers/admin/runs.py:1-17` (imports, constants), `:85-242` (`admin_llm_calls`), new handler after it
- Create: `templates/admin/_llm_call_bodies.html`
- Modify: `templates/admin/llm_calls.html` (summary tiles lines 15-31, the "Showing" line, the per-row latency chip, the `<details>` body lines 174-190)
- Modify: `templates/admin/simulation.html:604-614` (C-22 copy)
- Modify: `static/js/ui.js` (append `data-lazy-fragment`)
- Modify: `tests/unit/test_admin_route_table.py`, `tests/integration/test_llm_calls_channel_filter.py`, `tests/integration/test_llm_call_stats_storage.py`
- Modify: `tests/integration/test_rendered_page_gate.py` (Part 1C, Task 1C-15): add
  `"/admin/activity/{run_id}/llm-calls/{call_id}/bodies": "HTML fragment for the llm_calls page; no layout",`
  to `SKIPPED`, or `test_every_get_route_is_walked_or_skipped` fails (assembly audit PX-08d)
- Test: Create `tests/integration/test_llm_call_bodies.py`, `tests/unit/test_ui_js_behaviours.py`

**Interfaces:**
- Consumes: `_DB`, `_ADMIN`, `templates`, `router` (`src/routers/admin/_common.py`); `MAX_PAGE`; `static/js/ui.js` (Part 1A, loaded by `base.html`).
- Produces: `GET /admin/activity/{run_id}/llm-calls/{call_id}/bodies` → `admin_llm_call_bodies(run_id: uuid.UUID, call_id: uuid.UUID, request, db=_DB, current_user=_ADMIN) -> HTMLResponse` (admin-only via `get_admin_user`; 404 unless the call belongs to the run); template `admin/_llm_call_bodies.html` (context `log`); `async def _avg_api_call_latency_ms(db, run_id) -> float | None`; `ui.js` behaviour: a `<details>` containing `[data-lazy-fragment]` with an `<a href>` fetches that href on first open and puts the HTML in place. Reachability: the fragment route is credited by that `href` in the reachable `admin/llm_calls.html` (`{{ run.id }}`/`{{ log.id }}` sit in the path-parameter slots); `_llm_call_bodies.html` is reachable because `runs.py` renders it by name.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_llm_call_bodies.py`:

```python
"""C-16: the LLM-calls list never carries prompt or response bodies; one call's
bodies load through an admin-only fragment. C-19, C-21, C-22 ride along."""

import uuid

import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _seed(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    log = await factories.make_llm_call_log(
        db_session, run=run, phase="thread_reply", system_prompt="SYSTEM-PROMPT-BODY",
        messages_json=[{"role": "user", "content": "USER-MESSAGE-BODY"}],
        response_text="RESPONSE-BODY", latency_ms=900.0,
        call_stats=[{"latency_ms": 100.0}, {"latency_ms": 300.0}],
    )
    return admin, run, log


async def test_the_list_defers_the_bodies(client, db_session, engine):
    admin, run, log = await _seed(db_session)
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        html = (await client.get(f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id))).text
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    for body in ("SYSTEM-PROMPT-BODY", "USER-MESSAGE-BODY", "RESPONSE-BODY"):
        assert body not in html
    assert f'href="/admin/activity/{run.id}/llm-calls/{log.id}/bodies"' in html
    assert "data-lazy-fragment" in html
    assert not any("llm_call_logs.system_prompt" in s for s in seen)
    assert not any("llm_call_logs.messages_json" in s for s in seen)


async def test_the_fragment_loads_one_calls_bodies(client, db_session):
    admin, run, log = await _seed(db_session)
    r = await client.get(f"/admin/activity/{run.id}/llm-calls/{log.id}/bodies", headers=auth_headers(admin.id))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    for body in ("SYSTEM-PROMPT-BODY", "USER-MESSAGE-BODY", "RESPONSE-BODY"):
        assert body in r.text
    assert "<html" not in r.text, "a fragment, not a page"


async def test_the_fragment_is_admin_only_and_scoped_to_its_run(client, db_session):
    admin, run, log = await _seed(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    denied = await client.get(f"/admin/activity/{run.id}/llm-calls/{log.id}/bodies", headers=auth_headers(manager.id))
    assert denied.status_code == 403 and "RESPONSE-BODY" not in denied.text
    other = await factories.make_simulation_run(db_session)
    wrong_run = await client.get(f"/admin/activity/{other.id}/llm-calls/{log.id}/bodies", headers=auth_headers(admin.id))
    assert wrong_run.status_code == 404
    unknown = await client.get(f"/admin/activity/{run.id}/llm-calls/{uuid.uuid4()}/bodies", headers=auth_headers(admin.id))
    assert unknown.status_code == 404


async def test_page_is_bounded_and_latency_and_counts_are_labelled(client, db_session):
    admin, run, _log = await _seed(db_session)
    over = await client.get(f"/admin/activity/{run.id}/llm-calls?page=100001", headers=auth_headers(admin.id))
    assert over.status_code == 422
    html = (await client.get(f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id))).text
    assert "Avg API-call latency" in html and "200.0ms" in html, "mean of call_stats latency_ms"
    assert "900ms last call" in html, "a row without wall_ms says its latency is the last call's"
    assert "Logged turns" in html and "Showing 1 of 1 logged turns" in html
```

Create `tests/unit/test_ui_js_behaviours.py`:

```python
"""Source pins for static/js/ui.js behaviours added in Phase 2 (no JS runner in CI)."""

from pathlib import Path

UI_JS = (Path(__file__).resolve().parents[2] / "static/js/ui.js").read_text(encoding="utf-8")


def test_ui_js_lazy_fragment_rejects_redirects_and_non_html():
    block = UI_JS[UI_JS.index("data-lazy-fragment"):]
    assert 'document.addEventListener("toggle"' in UI_JS, "toggle does not bubble: a capture listener"
    assert "resp.redirected" in block, "an expired session must not inject the login page"
    assert 'indexOf("text/html") !== 0' in block
    assert "slot.dataset.loaded" in block, "loads once per row"
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_llm_call_bodies.py tests/unit/test_ui_js_behaviours.py -v`. Expected: every test fails (bodies in the page, fragment route 404, `?page=100001` gives 200, `ValueError: substring not found` in the ui.js test).

- [ ] **Step 3: Implement the routes** — in `src/routers/admin/runs.py` replace the imports block with:

```python
import uuid

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import Float, column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from src.agent.specialists import parse_opinion
from src.database import get_db
from src.dependencies import get_admin_user
from src.models import LlmCallLog, SimulationRun, User
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services.directory import MAX_PAGE, build_run_detail, list_runs_overview
from src.services.headline_claims import held_headline_counts, list_in_doubt

_PAGE = Query(1, ge=1, le=MAX_PAGE)

#: C-16: the three body columns the list never renders. `raiseload` makes a template
#: or handler that reaches for one fail loudly instead of issuing one query per row.
_DEFERRED_BODIES = (
    defer(LlmCallLog.system_prompt, raiseload=True),
    defer(LlmCallLog.messages_json, raiseload=True),
    defer(LlmCallLog.response_text, raiseload=True),
)


async def _avg_api_call_latency_ms(db: AsyncSession, run_id: uuid.UUID) -> float | None:
    """Mean per-API-call latency from `call_stats` (C-21), the same source as the
    simulation page's latency percentiles. `latency_ms` on the row is NOT a turn
    figure (see LlmCallLog), so it is not averaged here. None when no row of the run
    has a `call_stats` array."""
    is_array = func.jsonb_typeof(LlmCallLog.call_stats) == "array"
    elem = func.jsonb_array_elements(LlmCallLog.call_stats).table_valued(column("value", JSONB))
    return (
        await db.execute(
            select(func.avg(elem.c.value["latency_ms"].astext.cast(Float)))
            .select_from(LlmCallLog)
            .join(elem, true())
            .where(LlmCallLog.simulation_run_id == run_id, is_array)
        )
    ).scalar_one_or_none()
```

  In `admin_llm_calls`: change `page: int = Query(1, ge=1),` to `page: int = _PAGE,`; change
  `query = select(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)` to
  `query = select(LlmCallLog).options(*_DEFERRED_BODIES).where(LlmCallLog.simulation_run_id == run_id)`;
  delete the inline `from sqlalchemy import func as sa_func` line and replace every `sa_func.` in the
  handler with `func.`; change the count query to
  `count_query = select(func.count()).select_from(query.with_only_columns(LlmCallLog.id).subquery())`
  (loader options such as `defer()` apply only to the top-level statement, not to a subquery —
  `sqlalchemy/orm/context.py`, `toplevel` — so the original subquery would still select the body
  columns; plan audit Q2-03); delete `sa_func.avg(LlmCallLog.latency_ms).label("avg_latency_ms"),` from the
  stats select; replace the `consult_signals = {...}` comprehension with:

```python
    consult_ids = [log.id for log in logs if log.phase.startswith("consult_")]
    consult_texts = (
        dict(
            (await db.execute(
                select(LlmCallLog.id, LlmCallLog.response_text).where(LlmCallLog.id.in_(consult_ids))
            )).all()
        )
        if consult_ids else {}
    )
    consult_signals = {
        str(log.id): parse_opinion(
            consult_texts[log.id],
            domain=log.phase.removeprefix("consult_"),
            allow_historical=True,
        ).verdict_signal
        for log in logs
        if log.id in consult_texts
    }
    avg_call_latency = await _avg_api_call_latency_ms(db, run_id)
```

  (the comment above the comprehension stays, with its first sentence extended by: "The bodies are
  deferred (C-16), so the consult rows' `response_text` is fetched for those rows only."). In the
  template context replace `avg_latency_ms=round(stats.avg_latency_ms or 0, 1),` with
  `avg_call_latency_display=f"{round(avg_call_latency, 1)}ms" if avg_call_latency is not None else "—",`.
  Append the new handler after `admin_llm_calls`:

```python
@router.get("/activity/{run_id}/llm-calls/{call_id}/bodies", response_class=HTMLResponse)
async def admin_llm_call_bodies(
    run_id: uuid.UUID,
    call_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """One logged turn's system prompt, messages and response (C-16): the fragment an
    expanded row of the LLM-calls page loads (static/js/ui.js `data-lazy-fragment`),
    and the page that row's link opens without JavaScript. 404 unless the call
    belongs to the run, so a call id cannot be read through another run's URL."""
    log = (
        await db.execute(
            select(LlmCallLog).where(LlmCallLog.id == call_id, LlmCallLog.simulation_run_id == run_id)
        )
    ).scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=404, detail="LLM call not found")
    return templates.TemplateResponse(request, "admin/_llm_call_bodies.html", {"request": request, "log": log})
```

- [ ] **Step 4: Implement the templates and ui.js** — create `templates/admin/_llm_call_bodies.html`:

```html
{# One logged turn's bodies (C-16). Loaded into an expanded row of
   admin/llm_calls.html by static/js/ui.js (`data-lazy-fragment`), and also the
   page that row's link opens without JavaScript. A fragment: no layout, no
   script, and h2 headings so it nests under the page's h1. #}
<div class="space-y-3">
    <div>
        <h2 class="text-xs font-semibold text-gray-600 uppercase mb-1">System Prompt</h2>
        <pre class="text-xs bg-gray-50 p-3 rounded-lg overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap">{{ log.system_prompt }}</pre>
    </div>
    <div>
        <h2 class="text-xs font-semibold text-gray-600 uppercase mb-1">User Message</h2>
        {% for msg in log.messages_json %}
        <pre class="text-xs bg-blue-50 p-3 rounded-lg overflow-x-auto max-h-48 overflow-y-auto whitespace-pre-wrap">{{ msg.content }}</pre>
        {% endfor %}
    </div>
    <div>
        <h2 class="text-xs font-semibold text-gray-600 uppercase mb-1">Response</h2>
        <pre class="text-xs bg-green-50 p-3 rounded-lg overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap">{{ log.response_text }}</pre>
    </div>
</div>
```

  In `templates/admin/llm_calls.html`: replace the tile label `<div class="text-xs text-gray-500">Total Calls</div>`
  with `<div class="text-xs text-gray-500">Logged turns</div>`; replace
  `<div class="text-2xl font-bold text-gray-800">{{ avg_latency_ms }}ms</div>` and its label
  `<div class="text-xs text-gray-500">Avg Latency</div>` with
  `<div class="text-2xl font-bold text-gray-800">{{ avg_call_latency_display }}</div>` and
  `<div class="text-xs text-gray-500">Avg API-call latency</div>`; replace
  `Showing {{ logs | length }} of {{ total_count }} calls` with
  `Showing {{ logs | length }} of {{ total_count }} logged turns`; replace the per-row latency chip
  `<span class="text-gray-600 text-xs">{{ "%.0f" | format(log.latency_ms) }}ms</span>` (Part 1C's X-01
  turned its `text-gray-400` into `text-gray-600`) with

```html
                <span class="text-gray-600 text-xs">{% if log.wall_ms is not none %}{{ "%.0f" | format(log.wall_ms) }}ms turn{% else %}{{ "%.0f" | format(log.latency_ms) }}ms last call{% endif %}</span>
```

  and replace the whole `<div class="px-4 pb-4 space-y-3">` … `</div>` body of each `<details>` (the
  System Prompt / User Message / Response block) with

```html
            <div class="px-4 pb-4" data-lazy-fragment>
                <a href="/admin/activity/{{ run.id }}/llm-calls/{{ log.id }}/bodies" class="text-sm text-indigo-700 underline">Open the prompt and response</a>
            </div>
```

  In `templates/admin/simulation.html` replace `(newest 20,000 calls at most)` with
  `(API calls of the newest 20,000 logged turns at most)`, both
  `Newest 20,000 calls only — this run has more.` with
  `Only the newest 20,000 logged turns are read — this run has more.`, and
  `(newest 20,000 rows at most)` with `(of the newest 20,000 logged turns at most)`.
  Append to `static/js/ui.js`:

```js
// C-16: a <details> row holding [data-lazy-fragment] loads its body on first open
// from the slot's link (also the no-JavaScript fallback). `toggle` does not bubble,
// hence the capture listener. A redirect (expired session -> /login) or a non-HTML
// answer leaves the link in place with a message rather than injecting that page.
(function () {
  "use strict";
  document.addEventListener("toggle", function (event) {
    const details = event.target;
    if (!(details instanceof HTMLDetailsElement) || !details.open) {
      return;
    }
    const slot = details.querySelector("[data-lazy-fragment]");
    const link = slot ? slot.querySelector("a[href]") : null;
    if (!link || slot.dataset.loaded) {
      return;
    }
    slot.dataset.loaded = "1";
    slot.setAttribute("aria-busy", "true");
    fetch(link.href, { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (resp) {
        const type = resp.headers.get("content-type") || "";
        if (!resp.ok || resp.redirected || type.indexOf("text/html") !== 0) {
          throw new Error("fragment");
        }
        return resp.text();
      })
      .then(function (html) {
        slot.innerHTML = html;
      })
      .catch(function () {
        delete slot.dataset.loaded;
        link.textContent = "Could not load here — open the prompt and response on their own page";
      })
      .finally(function () {
        slot.removeAttribute("aria-busy");
      });
  }, true);
})();
```

- [ ] **Step 5: Update the tests that read the old page** — `tests/unit/test_admin_route_table.py`: add
  `("GET", "/activity/{run_id}/llm-calls/{call_id}/bodies"),` to `EXPECTED_ROUTES` after
  `("GET", "/activity/{run_id}/llm-calls"),` and raise the `assert len(_routes()) == N` literal by one.
  `tests/integration/test_llm_call_stats_storage.py`: replace

```python
    assert "INSTRUMENTED-ROW" in html
    assert "PRE-0032-ROW" in html, "a NULL call_stats row must still render"
```

  with

```python
    assert html.count("data-lazy-fragment") == 2, "both rows render, a NULL call_stats row included"
```

  replace `assert "20000.0ms" in html, "Avg Latency tile"` with
  `assert "19658.3ms" in html, "Avg API-call latency tile: mean of the call_stats latencies"`, and in the
  docstring replace `AVG(latency_ms) into three stat tiles` with
  `the mean call_stats latency into three stat tiles`. `tests/integration/test_llm_calls_channel_filter.py`:
  add `from sqlalchemy import select` to the third-party import group (after `import pytest`), and add
  after `HUB = "blackbird"`:

```python
async def _row_links(db_session, run) -> dict[str, str]:
    """response_text -> the row's fragment link (C-16: bodies are no longer inline)."""
    rows = (
        await db_session.execute(
            select(LlmCallLog.id, LlmCallLog.response_text).where(LlmCallLog.simulation_run_id == run.id)
        )
    ).all()
    return {text: f"/admin/activity/{run.id}/llm-calls/{row_id}/bodies" for row_id, text in rows}
```

  and in `test_the_channel_filter_narrows_the_rows_to_one_interview` replace the four text assertions
  and the "Showing" assertion with

```python
    links = await _row_links(db_session, run)
    assert links["WANG-INTERVIEW-REPLY"] in html
    assert links["WANG-CONSULT-OPINION"] in html, "the interview's consults come with it"
    assert links["GORDY-INTERVIEW-REPLY"] not in html
    assert links["UNATTRIBUTED-CALL"] not in html
    assert "Showing 2 of 2 logged turns" in html
```

  in `test_the_unfiltered_page_still_shows_everything` replace its three assertions with

```python
    links = await _row_links(db_session, run)
    for text in ("WANG-INTERVIEW-REPLY", "GORDY-INTERVIEW-REPLY", "UNATTRIBUTED-CALL"):
        assert links[text] in html
```

  in `test_the_channel_filter_is_scoped_to_the_run` replace `assert "OTHER-RUN-REPLY" not in html` with
  `assert (await _row_links(db_session, other))["OTHER-RUN-REPLY"] not in html`,
  and replace `assert "Showing 1 of 51 calls" in page2` with `assert "Showing 1 of 51 logged turns" in page2`.

- [ ] **Step 6: Run** — `.venv-test/bin/python -m pytest tests/integration/test_llm_call_bodies.py tests/unit/test_ui_js_behaviours.py tests/integration/test_llm_calls_channel_filter.py tests/integration/test_llm_call_stats_storage.py tests/integration/test_assessment_detail_page.py tests/unit/test_admin_route_table.py tests/unit/test_reachability.py tests/characterization/test_auth_and_admin_routes.py -v`. Expected: all pass.

- [ ] **Step 7: Rebuild CSS and commit** — run `scripts/build_css.sh`, then:

```bash
git add src/routers/admin/runs.py templates/admin/_llm_call_bodies.html templates/admin/llm_calls.html templates/admin/simulation.html static/js/ui.js static/css/app.css tests/integration/test_llm_call_bodies.py tests/unit/test_ui_js_behaviours.py tests/unit/test_admin_route_table.py tests/integration/test_llm_calls_channel_filter.py tests/integration/test_llm_call_stats_storage.py tests/integration/test_rendered_page_gate.py
git commit -m "perf(webui-2): LLM calls load bodies on expand; bounded page; honest latency and turn labels (C-16, C-19, C-21, C-22)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-7: Detail page selects only the tool-use slice of `messages_json` (B-07)

**Files:**
- Modify: `src/services/assessment_detail.py` (imports; new constant before `_load_tool_turns` at `:1893`; its select list `:1920-1926` and loop `:1949-1952`)
- Test: Create `tests/integration/test_tool_slice.py`

**Interfaces:**
- Consumes: `tool_chips_from_conversation` (`src/services/assessment_detail.py:545`).
- Produces: `TOOL_BLOCKS_SLICE` — a labelled SQL column (`tool_slice`) yielding `[{"content": [tool_use|tool_result blocks]}, …]` or NULL, built from `llm_call_logs.messages_json` in the database.

- [ ] **Step 1: Write the failing test** — create `tests/integration/test_tool_slice.py`:

```python
"""B-07: the detail page reads only the tool_use/tool_result blocks of each hub turn;
the chips built from that slice equal the chips built from the whole conversation."""

import pytest
from sqlalchemy import select

from src.models import LlmCallLog
from src.services.assessment_detail import TOOL_BLOCKS_SLICE, tool_chips_from_conversation
from tests import factories

pytestmark = pytest.mark.integration

_CONVERSATION = [
    "the opening user string",
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "x" * 5000, "signature": "sig"},
        {"type": "text", "text": "Let me ask two specialists."},
        {"type": "tool_use", "id": "t1", "name": "consult_specialist", "input": {"domain": "clinical", "question": "Q1"}},
        {"type": "tool_use", "id": "t2", "name": "lookup", "input": {"query": "Q2"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "lookup result"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "{\"signal\": \"adequate\"}"},
    ]},
    {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
]


@pytest.mark.parametrize("messages_json", [_CONVERSATION, {"messages": []}, [], ["only a string"]])
async def test_chips_from_the_slice_equal_chips_from_the_whole(db_session, messages_json):
    log = await factories.make_llm_call_log(db_session, messages_json=messages_json)
    sliced = (
        await db_session.execute(select(TOOL_BLOCKS_SLICE).where(LlmCallLog.id == log.id))
    ).scalar_one()
    assert tool_chips_from_conversation(sliced) == tool_chips_from_conversation(messages_json)
    if messages_json is _CONVERSATION:
        assert "thinking" not in str(sliced) and "Let me ask" not in str(sliced)
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_tool_slice.py -v`. Expected: `ImportError: cannot import name 'TOOL_BLOCKS_SLICE'`.

- [ ] **Step 3: Implement** — in `src/services/assessment_detail.py` add `JSON` and `literal_column` to its
  `from sqlalchemy import ...` line, and insert before `async def _load_tool_turns`:

```python
#: B-07: the tool_use / tool_result blocks of a logged hub turn, extracted in SQL, as
#: `[{"content": [block, ...]}, ...]` in conversation order (NULL when there are
#: none). `tool_chips_from_conversation` reads nothing else, so the page no longer
#: transfers the thinking and text blocks, which are the bulk of the column's bytes.
#: `messages_json` is `json` (not `jsonb`); a non-array value or a non-array
#: `content` contributes nothing, as in the Python reader.
TOOL_BLOCKS_SLICE = literal_column(
    """(SELECT json_agg(json_build_object('content', kept.blocks) ORDER BY msg.ord)
        FROM json_array_elements(
               CASE WHEN json_typeof(llm_call_logs.messages_json) = 'array'
                    THEN llm_call_logs.messages_json ELSE '[]'::json END
             ) WITH ORDINALITY AS msg(value, ord)
        CROSS JOIN LATERAL (
          SELECT json_agg(blk.value ORDER BY blk.ord) AS blocks
          FROM json_array_elements(
                 CASE WHEN json_typeof(msg.value -> 'content') = 'array'
                      THEN msg.value -> 'content' ELSE '[]'::json END
               ) WITH ORDINALITY AS blk(value, ord)
          WHERE blk.value ->> 'type' IN ('tool_use', 'tool_result')
        ) AS kept
        WHERE kept.blocks IS NOT NULL)""",
    type_=JSON,
).label("tool_slice")
```

  In `_load_tool_turns` replace `LlmCallLog.messages_json,` in the `select(...)` with `TOOL_BLOCKS_SLICE,`,
  replace `chips = tool_chips_from_conversation(row.messages_json)` with
  `chips = tool_chips_from_conversation(row.tool_slice)`, and add to its docstring, after the
  `system_prompt` paragraph: "Nor is the whole ``messages_json``: only its tool blocks, extracted in SQL
  (``TOOL_BLOCKS_SLICE``, B-07)."

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/integration/test_tool_slice.py tests/integration/test_assessment_detail_page.py tests/unit/test_assessment_timeline.py -v`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/services/assessment_detail.py tests/integration/test_tool_slice.py
git commit -m "perf(webui-2): detail page reads only the tool blocks of hub turns (B-07)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-8: Chat polling skips the stale sweep (B-08)

**Files:**
- Modify: `src/services/assessment_chat.py:969-978` (`list_history`), new helper before it
- Modify: `src/routers/assessment_chat.py:112-134` (`assessment_chat_history`)
- Modify: `static/js/assessment_chat.js:546-575` (`schedulePoll`, `loadHistory`)
- Test: Create `tests/integration/test_assessment_chat_poll.py`, `tests/unit/test_chat_drawer_js.py`

**Interfaces:**
- Consumes: `sweep_stale`, `_since`, `STALE_AFTER_SECONDS`, `AssessmentChatTurn`, `CHAT_STATUS_STREAMING` (all in `src/services/assessment_chat.py`).
- Produces: `list_history(db, *, assessment_id, user, sweep: bool = True)`; `async def _own_turn_is_stale(db, *, assessment_id, user_id) -> bool`; the history GET accepts `?poll=1`; JS `loadHistory(isPoll)`.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_assessment_chat_poll.py`:

```python
"""B-08: the drawer's 5 s poll (`?poll=1`) does not run the every-user stale sweep,
unless the caller's own turn on this assessment is itself stale."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, AssessmentChatTurn
from src.services.assessment_chat import tier_for
from tests import factories
from tests.assessment_chat_support import history_url, seed_interview
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _streaming_turn(assessment_id, user, age):
    return AssessmentChatTurn(
        id=uuid.uuid4(), assessment_id=assessment_id, user_id=user.id, context_tier=tier_for(user),
        question="q", status="streaming", model="claude-opus-5-5", record_sha256_12="0" * 12,
        prompt_sha256_12="0" * 12, created_at=datetime.now(UTC) - age,
    )


async def _status(db_session, turn_id):
    return await db_session.scalar(select(AssessmentChatTurn.status).where(AssessmentChatTurn.id == turn_id))


async def test_a_poll_leaves_other_users_stale_turns_to_the_next_full_load(client, asgi_app, db_session):
    asgi_app.state.assessment_chat_enabled = True
    seeded = await seed_interview(db_session)
    viewer = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    other = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    stale = _streaming_turn(seeded.assessment_id, other, timedelta(seconds=301))
    db_session.add(stale)
    await db_session.flush()

    polled = await client.get(history_url(seeded.assessment_id) + "?poll=1", headers=auth_headers(viewer.id))
    assert polled.status_code == 200
    assert await _status(db_session, stale.id) == "streaming"

    opened = await client.get(history_url(seeded.assessment_id), headers=auth_headers(viewer.id))
    assert opened.status_code == 200
    assert await _status(db_session, stale.id) == "interrupted"


async def test_a_poll_still_sweeps_when_the_callers_own_turn_is_stale(client, asgi_app, db_session):
    asgi_app.state.assessment_chat_enabled = True
    seeded = await seed_interview(db_session)
    viewer = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    own = _streaming_turn(seeded.assessment_id, viewer, timedelta(seconds=301))
    db_session.add(own)
    await db_session.flush()

    polled = await client.get(history_url(seeded.assessment_id) + "?poll=1", headers=auth_headers(viewer.id))
    assert polled.status_code == 200
    assert await _status(db_session, own.id) == "interrupted"
    assert all(t["status"] != "streaming" for t in polled.json()["turns"])
```

Create `tests/unit/test_chat_drawer_js.py`:

```python
"""Source pins for static/js/assessment_chat.js Phase 2 behaviours (B-08, B-16, B-17,
B-20). Browser behaviour is exercised by tests/e2e/ui_audit/journeys_phase2_2b.py."""

from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / "static/js/assessment_chat.js").read_text(encoding="utf-8")


def _body(signature: str) -> str:
    start = JS.index(signature)
    return JS[start:JS.index("\n  }\n", start)]


def test_polls_ask_for_the_sweepless_history():
    assert 'cfg.historyUrl + "?poll=1"' in _body("async function loadHistory(isPoll)")
    assert "loadHistory(true)" in _body("function schedulePoll()")
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_assessment_chat_poll.py tests/unit/test_chat_drawer_js.py -v`. Expected: the first integration test fails (`assert 'interrupted' == 'streaming'` — today every GET sweeps); the JS test fails with `ValueError: substring not found`.

- [ ] **Step 3: Implement the server side** — in `src/services/assessment_chat.py` insert before `async def list_history`:

```python
async def _own_turn_is_stale(db: AsyncSession, *, assessment_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether the caller has a `streaming` turn on this assessment older than
    STALE_AFTER_SECONDS — the one case a poll must still sweep, or the drawer would
    poll a dead answer until it is reopened."""
    count = await db.scalar(
        select(func.count(AssessmentChatTurn.id)).where(
            AssessmentChatTurn.assessment_id == assessment_id,
            AssessmentChatTurn.user_id == user_id,
            AssessmentChatTurn.status == CHAT_STATUS_STREAMING,
            AssessmentChatTurn.created_at < _since(timedelta(seconds=STALE_AFTER_SECONDS)),
        )
    )
    return bool(count)
```

  and change `list_history` to:

```python
async def list_history(
    db: AsyncSession, *, assessment_id: uuid.UUID, user: Any, sweep: bool = True
) -> dict[str, Any] | None:
    """The GET shape (§6.5), or None for an unknown assessment. Sweeps first; builds
    the current record so each turn can say whether its record has changed.
    `sweep=False` is the drawer's 5 s poll (B-08): it skips the every-user sweep
    unless the caller's own turn here is already stale (`_own_turn_is_stale`)."""
    settings = get_settings()
    user_id = user.id
    tier = tier_for(user)
    if sweep or await _own_turn_is_stale(db, assessment_id=assessment_id, user_id=user_id):
        await sweep_stale(db)
```

  (the rest of the body, from `loaded = await load_chat_record(...)`, is unchanged). In
  `src/routers/assessment_chat.py` `assessment_chat_history`, replace
  `payload = await chat.list_history(db, assessment_id=parsed_id, user=current_user)` with:

```python
        # B-08: the drawer's poll asks with ?poll=1 and skips the every-user sweep.
        poll = request.query_params.get("poll") == "1"
        payload = await chat.list_history(
            db, assessment_id=parsed_id, user=current_user, sweep=not poll
        )
```

- [ ] **Step 4: Implement the client side** — in `static/js/assessment_chat.js` replace
  `state.poll = window.setTimeout(loadHistory, POLL_MS);` with
  `state.poll = window.setTimeout(function () { loadHistory(true); }, POLL_MS);`; replace
  `async function loadHistory() {` with `async function loadHistory(isPoll) {`; replace
  `resp = await fetch(cfg.historyUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });`
  with

```js
      // B-08: a poll asks for the sweepless history (?poll=1); an open or a reload does not.
      const url = isPoll ? cfg.historyUrl + "?poll=1" : cfg.historyUrl;
      resp = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
```

- [ ] **Step 5: Run** — `.venv-test/bin/python -m pytest tests/integration/test_assessment_chat_poll.py tests/unit/test_chat_drawer_js.py tests/integration/test_assessment_chat_flow.py tests/integration/test_assessment_chat_routes.py -v`. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/services/assessment_chat.py src/routers/assessment_chat.py static/js/assessment_chat.js tests/integration/test_assessment_chat_poll.py tests/unit/test_chat_drawer_js.py
git commit -m "perf(webui-2): the chat poll skips the every-user stale sweep (B-08)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-9: Jobs filters from the enums; non-negative start limits; copy; lab-filter encoding (C-18, C-20, B-09, D-28, B-18)

**Files:**
- Modify: `src/routers/admin/jobs.py:1-56`
- Modify: `templates/admin/jobs.html:29-51` (filter selects' options)
- Modify: `src/routers/admin/simulation.py:204-212` (`admin_simulation_start` signature; module constant beside `_RUN_ID_FORM = Form(...)` at `:280`, moved above the handler)
- Modify: `templates/manager/assessment_detail.html:110-114`, `templates/agent/request.html:15,27`
- Modify: `templates/admin/assessments.html:184`, `templates/manager/assessments.html:176`
- Test: Create `tests/integration/test_admin_correctness_low.py`, `tests/unit/test_copy_and_links.py`

**Interfaces:**
- Consumes: `Job` model enums (`src/models/job.py:50-61`).
- Produces: `JOB_STATUSES: tuple[str, ...]`, `JOB_TYPES: tuple[str, ...]` in `src/routers/admin/jobs.py`; template context `job_statuses`, `job_types`; `_NON_NEGATIVE_INT_FORM = Form(0, ge=0)` in `src/routers/admin/simulation.py`.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_admin_correctness_low.py`:

```python
"""C-18 (jobs filters from the enum, unknown value refused without a 500) and C-20
(start limits are >= 0 on the server)."""

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, Job, SimulationCommand
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_jobs_filters_offer_every_enum_value(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    html = (await client.get("/admin/jobs", headers=auth_headers(admin.id))).text
    for value in (*Job.__table__.c.type.type.enums, *Job.__table__.c.status.type.enums):
        assert f'value="{value}"' in html, value


@pytest.mark.parametrize("query", ["status_filter=nonsense", "type_filter=nonsense"])
async def test_an_unknown_jobs_filter_is_a_400(client, db_session, query):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(f"/admin/jobs?{query}", headers=auth_headers(admin.id))
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["max_runtime", "max_proposals"])
async def test_a_negative_start_limit_is_refused_server_side(client, db_session, field):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    data = {"max_runtime": "0", "max_proposals": "0", field: "-5"}
    r = await client.post("/admin/simulation/start", data=data, headers=auth_headers(admin.id))
    assert r.status_code == 422
    starts = await db_session.scalar(
        select(func.count()).select_from(SimulationCommand).where(SimulationCommand.command == "start")
    )
    assert starts == 0
```

Create `tests/unit/test_copy_and_links.py`:

```python
"""B-09 and D-28 (copy that no longer describes the page) and B-18 (encoded lab filter)."""

from pathlib import Path

T = Path(__file__).resolve().parents[2] / "templates"


def test_the_manager_detail_page_does_not_call_itself_read_only():
    assert "Read-only view." not in (T / "manager/assessment_detail.html").read_text()


def test_the_agent_request_page_has_no_stale_copy():
    text = (T / "agent/request.html").read_text()
    assert "Scripps" not in text
    assert "admin will review and provision" not in text


def test_review_tab_links_encode_the_lab_filter():
    for name in ("admin/assessments.html", "manager/assessments.html"):
        text = (T / name).read_text()
        assert "lab={{ (lab_filter or '') | urlencode }}" in text, name
        assert "lab={{ lab_filter or '' }}" not in text, name
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_admin_correctness_low.py tests/unit/test_copy_and_links.py -v`. Expected: `enrich_grants`/`industry_evidence` missing from the page; `?status_filter=nonsense` gives 500 (`DBAPIError` invalid enum input); negative start gives 302; all three copy/link tests fail.

- [ ] **Step 3: Implement** — `src/routers/admin/jobs.py`: change the fastapi import to
  `from fastapi import Depends, HTTPException, Query, Request`; after `_PAGE = ...` add:

```python
#: C-18: the filter values ARE the column enums, so the page can neither omit a
#: type nor send a value Postgres rejects as invalid enum input (a 500).
JOB_STATUSES: tuple[str, ...] = tuple(Job.__table__.c.status.type.enums)
JOB_TYPES: tuple[str, ...] = tuple(Job.__table__.c.type.type.enums)
```

  at the top of `admin_jobs`'s body (after the docstring) add:

```python
    if status_filter and status_filter not in JOB_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown job status filter")
    if type_filter and type_filter not in JOB_TYPES:
        raise HTTPException(status_code=400, detail="Unknown job type filter")
```

  and add `job_statuses=JOB_STATUSES, job_types=JOB_TYPES,` to its template context. In
  `templates/admin/jobs.html` replace the status select's
  `{% for s in ['pending', 'processing', 'completed', 'failed', 'dead'] %}` with
  `{% for s in job_statuses %}`, and replace the type select's three hard-coded `<option value="generate_profile" …>`,
  `<option value="monthly_refresh" …>`, `<option value="review_feedback_analysis" …>` lines with

```html
            {% for t in job_types %}
            <option value="{{ t }}" {% if type_filter == t %}selected{% endif %}>{{ t | replace('_', ' ') | title }}</option>
            {% endfor %}
```

  `src/routers/admin/simulation.py`: move `_RUN_ID_FORM = Form(...)` up to just above
  `@router.post("/simulation/start")` and add beside it:

```python
#: C-20: a run limit is a count of seconds or proposals; 0 means "no limit".
_NON_NEGATIVE_INT_FORM = Form(0, ge=0)
```

  and in `admin_simulation_start` replace `max_runtime: int = Form(0),` / `max_proposals: int = Form(0),`
  with `max_runtime: int = _NON_NEGATIVE_INT_FORM,` / `max_proposals: int = _NON_NEGATIVE_INT_FORM,`.
  `templates/manager/assessment_detail.html`: replace

```html
        Read-only view. The raw model output and the hub's per-call LLM record are
```

  with

```html
        The raw model output and the hub's per-call LLM record are
```

  `templates/agent/request.html`: replace `An admin will review and provision your agent shortly.` with
  `An administrator or manager will review the request and set the agent up.` and
  `to find collaboration opportunities with other Scripps labs.` with
  `to find collaboration opportunities with other labs on the platform.`. In
  `templates/admin/assessments.html` and `templates/manager/assessments.html` replace
  `&amp;lab={{ lab_filter or '' }}&amp;` with `&amp;lab={{ (lab_filter or '') | urlencode }}&amp;`.

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/integration/test_admin_correctness_low.py tests/unit/test_copy_and_links.py tests/integration/test_admin_jobs_page.py tests/integration/test_admin_simulation_page.py tests/integration/test_assessment_list_chrome.py -v`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/routers/admin/jobs.py templates/admin/jobs.html src/routers/admin/simulation.py templates/manager/assessment_detail.html templates/agent/request.html templates/admin/assessments.html templates/manager/assessments.html tests/integration/test_admin_correctness_low.py tests/unit/test_copy_and_links.py
git commit -m "fix(webui-2): jobs filters from the enums, non-negative start limits, copy, encoded lab filter (C-18, C-20, B-09, D-28, B-18)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-10: Drawer modality on resize; keep scroll and focus on re-render; clear the drawer for "Show in page" (B-16, B-17, B-20)

**Files:**
- Modify: `static/js/assessment_chat.js` (`showInPage` `:385-404`, `renderSources` `:447-452`, `render` `:519-536`, `openDrawer` `:1006-1012`, listeners after `els.close.addEventListener("click", closeDrawer);`)
- Modify: `tests/unit/test_chat_drawer_js.py`
- Create: `tests/e2e/ui_audit/journeys_phase2_2b.py`
- Modify: `tests/e2e/ui_audit/journeys_phase2.py` (created by Task 2A-34)

**Interfaces:**
- Consumes: Phase 0 harness (`Harness`: `h.base_url`, `h.ids`, `await h.page(role, width=1280)` → `(context, page, log)`); seed ids `admin`, `manager`, `reviewer`, `pi`, `run`, `assessments` (the keys of the 2026-10-01 audit seed; assumed kept by Phase 0); role `"anon"` meaning no session cookie (assumed); the assessment chat enabled in the harness app.
- Produces: JS `applyModality()`, `keepClearOfDrawer(target)`; button ids `chat-src-show-<turnKey>-<n>`; journeys `journey_drawer_modality_follows_resize`, `journey_poll_rerender_keeps_scroll_and_focus`, `journey_show_in_page_clears_the_drawer`; `journeys_phase2_2b.JOURNEYS`; `journeys_phase2.JOURNEYS` (Phase 2's list; other Phase 2 parts add their module's list to it).

- [ ] **Step 1: Write the failing source test** — append to `tests/unit/test_chat_drawer_js.py`:

```python
def test_modality_is_re_evaluated_on_resize():
    assert "function applyModality()" in JS
    assert 'WIDE.addEventListener("change"' in JS
    assert "applyModality();" in _body("function openDrawer(opener)")


def test_render_keeps_scroll_position_and_focus():
    body = _body("function render()")
    assert "const pinned = " in body
    assert "log.scrollTop = pinned ? log.scrollHeight : keptTop;" in body
    assert "again.focus({ preventScroll: true })" in body


def test_show_in_page_keeps_the_target_clear_of_the_drawer():
    assert "keepClearOfDrawer(target);" in _body("function showInPage(anchor)")
    clear = _body("function keepClearOfDrawer(target)")
    assert "WIDTH_MIN_PX" in clear and "closeDrawer();" in clear
    assert '"chat-src-show-" + turnKey' in JS
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/unit/test_chat_drawer_js.py -v`. Expected: the three new tests fail (`assert 'function applyModality()' in JS`, `ValueError: substring not found`).

- [ ] **Step 3: Implement** — in `static/js/assessment_chat.js`:
  1. In `showInPage`, replace

```js
    if (window.matchMedia("(max-width: 767px)").matches) {
      closeDrawer();
    }
  }
```

  with

```js
    if (!WIDE.matches) {
      closeDrawer();
    } else {
      keepClearOfDrawer(target);
    }
  }

  // B-20: from md up the drawer overlays the page's right side, so a target scrolled
  // into view can still sit under it. When the room right of the target fits the
  // drawer's minimum width the drawer narrows to it for this view (savedWidth is not
  // touched, so the next open or resize restores the chosen width); otherwise it
  // closes, as it already does below md.
  function keepClearOfDrawer(target) {
    if (!state.open) {
      return;
    }
    const room = Math.floor(window.innerWidth - target.getBoundingClientRect().right - 8);
    if (room >= drawer.getBoundingClientRect().width) {
      return;
    }
    if (room >= WIDTH_MIN_PX) {
      drawer.style.width = room + "px";
      return;
    }
    closeDrawer();
  }
```

  2. In `renderSources`, after `button.type = "button";` add `button.id = "chat-src-show-" + turnKey + "-" + c.n;`.
  3. Replace the whole `function render() { … }` with:

```js
  // B-17: the log is rebuilt on every streamed frame and every history load. A
  // reader scrolled up stays where they were (only a log already at the bottom
  // follows new content), and focus on a control with an id inside the log is put
  // back on the rebuilt control.
  function render() {
    const log = els.log;
    const pinned = log.scrollHeight - log.scrollTop - log.clientHeight <= 24;
    const keptTop = log.scrollTop;
    const active = document.activeElement;
    const focusId = active && log.contains(active) && active.id ? active.id : null;
    log.replaceChildren();
    const firstIn = state.turns.findIndex(function (t) { return t.in_window; });
    const olderLeftOut = firstIn > 0 && state.turns.slice(0, firstIn).some(function (t) {
      return (t.status === "complete" || t.status === "truncated") && !t.in_window;
    });
    state.turns.forEach(function (turn, i) {
      const prev = i > 0 ? state.turns[i - 1] : null;
      const revisionStart = prev !== null && (prev.verdict_revision || 1) !== (turn.verdict_revision || 1);
      log.appendChild(turnNode(turn, olderLeftOut && i === firstIn, revisionStart));
    });
    els.starters.hidden = state.turns.length > 0;
    els.notice.hidden = !(state.limits && state.limits.verdict_may_change);
    updateUsage();
    updateCounter();
    setBusy(state.busy);
    log.scrollTop = pinned ? log.scrollHeight : keptTop;
    if (focusId) {
      const again = document.getElementById(focusId);
      if (again) {
        again.focus({ preventScroll: true });
      }
    }
  }
```

  4. In `openDrawer`, replace

```js
    mobileModalActive = window.matchMedia("(max-width: 767px)").matches;
    if (mobileModalActive) {
      drawer.setAttribute("role", "dialog");
      drawer.setAttribute("aria-modal", "true");
      setInertForModal();
    }
```

  with `    applyModality();`, and insert before `function openDrawer(opener) {`:

```js
  // B-16: below md the open drawer is a modal (role=dialog, aria-modal, the page
  // inert); from md up it is not. Re-evaluated on every crossing of the breakpoint
  // while open, not only at open.
  function applyModality() {
    const narrow = !WIDE.matches;
    if (narrow && !mobileModalActive) {
      drawer.setAttribute("role", "dialog");
      drawer.setAttribute("aria-modal", "true");
      setInertForModal();
      mobileModalActive = true;
    } else if (!narrow && mobileModalActive) {
      drawer.removeAttribute("role");
      drawer.removeAttribute("aria-modal");
      clearInertForModal();
      mobileModalActive = false;
    }
  }
```

  5. After `els.close.addEventListener("click", closeDrawer);` add:

```js
  WIDE.addEventListener("change", function () {
    if (state.open) {
      applyModality();
    }
  });
```

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/unit/test_chat_drawer_js.py tests/integration/test_assessment_chat_templates.py -v`. Expected: all pass.

- [ ] **Step 5: Write the journeys** — create `tests/e2e/ui_audit/journeys_phase2_2b.py`:

```python
"""Web UI remediation Phase 2, Part 2B journeys (B-16, B-17, B-20; later tasks add
X-04, R-01/R-02 and the enforced CSP). Run through journeys_phase2.JOURNEYS by
`python -m tests.e2e.ui_audit.run journeys --phase 2`."""

from __future__ import annotations

from pathlib import Path

NARROW = 375
WIDE = 1280
BAD_UUID = "00000000-0000-0000-0000-000000000000"
_AXE = Path(__file__).with_name("axe.min.js")


def _detail_path(ids: dict) -> str:
    return f"/admin/assessments/{ids['assessments'][0]}"


def _history_payload(streaming: bool) -> dict:
    """A synthetic chat history: six long complete turns (the first cites the page's
    #brief card) and, when `streaming`, a seventh still being answered so the drawer
    keeps polling every 5 s."""
    def turn(i: int, status: str) -> dict:
        return {
            "id": f"turn-{i}", "question": f"Question {i} " + "about the proposal " * 20,
            "segments": [] if status == "streaming" else [{"text": "Answer paragraph. " * 60, "cites": [1] if i == 0 else []}],
            "citations": [{"n": 1, "label": "Brief", "anchor": "brief"}] if i == 0 else [],
            "allowed_links": [], "status": status, "stop_reason": None, "refusal_category": None,
            "error_code": None, "served_by_model": None, "fallback_used": False, "in_window": True,
            "verdict_revision": 1, "record_changed": False, "created_at": None, "completed_at": None,
        }

    turns = [turn(i, "complete") for i in range(6)]
    if streaming:
        turns.append(turn(6, "streaming"))
    return {
        "tier": "staff", "verdict_revision": 1, "turns": turns, "questions_used_24h": 6,
        "daily_limit": 50, "max_question_chars": 4000, "max_turns": 20, "verdict_may_change": False,
    }


async def _fake_history(page, ids: dict, streaming: bool) -> list[str]:
    """Answer the drawer's history GETs with `_history_payload`; returns the list the
    handler appends each answered URL to."""
    answered: list[str] = []

    async def handler(route):
        if route.request.method != "GET":
            await route.continue_()
            return
        answered.append(route.request.url)
        await route.fulfill(status=200, json=_history_payload(streaming))

    await page.route(f"**/assessment-chat/{ids['assessments'][0]}*", handler)
    return answered


async def _open_drawer(page) -> bool:
    if not await page.locator("[data-chat-bubble]").count():
        return False
    await page.locator("[data-chat-bubble]").click()
    await page.wait_for_selector("#assessment-chat:not(.hidden)")
    return True


async def _modality(page) -> dict:
    return await page.evaluate(
        """() => { const d = document.getElementById('assessment-chat');
                   return {role: d.getAttribute('role'), modal: d.getAttribute('aria-modal'),
                           inert: document.querySelectorAll('[inert]').length}; }"""
    )


async def journey_drawer_modality_follows_resize(h) -> dict:
    """B-16: opened wide (not modal), narrowed to 375 px (modal, page inert), widened
    again (not modal, nothing left inert)."""
    context, page, log = await h.page("admin", width=WIDE)
    try:
        await page.goto(h.base_url + _detail_path(h.ids), wait_until="networkidle")
        if not await _open_drawer(page):
            return {"ok": False, "reason": "no chat opener on the detail page", "log": log}
        wide = await _modality(page)
        await page.set_viewport_size({"width": NARROW, "height": 800})
        await page.wait_for_timeout(300)
        narrow = await _modality(page)
        await page.set_viewport_size({"width": WIDE, "height": 900})
        await page.wait_for_timeout(300)
        wide_again = await _modality(page)
    finally:
        await context.close()
    ok = (
        wide == {"role": None, "modal": None, "inert": 0}
        and narrow["role"] == "dialog" and narrow["modal"] == "true" and narrow["inert"] > 0
        and wide_again == wide
    )
    return {"ok": ok, "wide": wide, "narrow": narrow, "wide_again": wide_again, "log": log}


async def journey_poll_rerender_keeps_scroll_and_focus(h) -> dict:
    """B-17: with an answer streaming the drawer re-renders every poll; a reader
    scrolled to the top with focus on a Sources toggle keeps both."""
    context, page, log = await h.page("admin", width=WIDE)
    try:
        answered = await _fake_history(page, h.ids, streaming=True)
        await page.goto(h.base_url + _detail_path(h.ids), wait_until="networkidle")
        if not await _open_drawer(page):
            return {"ok": False, "reason": "no chat opener on the detail page", "log": log}
        await page.wait_for_selector("#chat-sources-toggle-turn-0")
        await page.evaluate("document.querySelector('[data-chat-log]').scrollTop = 0")
        await page.focus("#chat-sources-toggle-turn-0")
        before = len(answered)
        await page.wait_for_timeout(6500)
        state = await page.evaluate(
            """() => ({focus: document.activeElement && document.activeElement.id,
                       top: document.querySelector('[data-chat-log]').scrollTop})"""
        )
        polls = len(answered) - before
    finally:
        await context.close()
    ok = polls >= 1 and state["focus"] == "chat-sources-toggle-turn-0" and state["top"] < 50
    return {"ok": ok, "polls": polls, "after_poll": state,
            "poll_urls": answered[-2:], "log": log}


async def journey_show_in_page_clears_the_drawer(h) -> dict:
    """B-20: at 1280 px "Show in page" leaves the target visible: the drawer is
    either closed or entirely to the right of the target."""
    context, page, log = await h.page("admin", width=WIDE)
    try:
        await _fake_history(page, h.ids, streaming=False)
        await page.goto(h.base_url + _detail_path(h.ids), wait_until="networkidle")
        if not await _open_drawer(page):
            return {"ok": False, "reason": "no chat opener on the detail page", "log": log}
        await page.click("#chat-sources-toggle-turn-0")
        await page.click("#chat-src-show-turn-0-1")
        await page.wait_for_timeout(1200)
        geometry = await page.evaluate(
            """() => { const d = document.getElementById('assessment-chat');
                       const t = document.getElementById('brief').getBoundingClientRect();
                       return {drawer_hidden: d.classList.contains('hidden'),
                               drawer_left: d.getBoundingClientRect().left, target_right: t.right}; }"""
        )
    finally:
        await context.close()
    ok = geometry["drawer_hidden"] or geometry["target_right"] <= geometry["drawer_left"]
    return {"ok": ok, **geometry, "log": log}


JOURNEYS = [
    journey_drawer_modality_follows_resize,
    journey_poll_rerender_keeps_scroll_and_focus,
    journey_show_in_page_clears_the_drawer,
]
```

  Modify `tests/e2e/ui_audit/journeys_phase2.py`, which Task 2A-34 created with Part 2A's three
  journeys (it runs first). Add to its top import block, after `import time`:

```python
from tests.e2e.ui_audit.journeys_phase2_2b import JOURNEYS as _JOURNEYS_2B
```

  and append after its closing `JOURNEYS = [...]` list:

```python
# Part 2B's journeys (tests/e2e/ui_audit/journeys_phase2_2b.py).
JOURNEYS += _JOURNEYS_2B
```

- [ ] **Step 6: Lint the new test files** — `.venv-test/bin/python -m ruff check tests/e2e/ui_audit/journeys_phase2_2b.py tests/e2e/ui_audit/journeys_phase2.py tests/unit/test_chat_drawer_js.py`. Expected: `All checks passed!`.

- [ ] **Step 7: Rebuild CSS and commit** — run `scripts/build_css.sh` (no new class is expected; the drift check stays clean either way), then:

```bash
git add static/js/assessment_chat.js static/css/app.css tests/unit/test_chat_drawer_js.py tests/e2e/ui_audit/journeys_phase2_2b.py tests/e2e/ui_audit/journeys_phase2.py
git commit -m "fix(webui-2): drawer modality on resize, stable scroll/focus, show-in-page clear of the drawer (B-16, B-17, B-20)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-11: Apply buttons instead of submit-on-change (FN-04)

**Files:**
- Modify: `templates/admin/users.html` (`<!-- Filters -->` block, lines 22-44, and any `applyFilter` `<script>`), `templates/manager/pis.html` (lines 54-76 and any `applyFilter` script), `templates/admin/jobs.html` (lines 28-51 and any `applyFilter` script)
- Modify: `templates/admin/assessments.html:100-138`, `templates/manager/assessments.html:101-134`, `templates/admin/discussions.html:21-31`, `templates/manager/discussions.html:21-31`, `templates/manager/prompt_suggestions.html:8-17`, `templates/admin/simulation.html:408-420`
- Modify: `static/js/ui.js` (delete the `data-autosubmit` and `data-filter-nav` behaviours); `tests/unit/test_ui_behaviours.py` and `tests/e2e/ui_audit/journeys_phase1.py` (Task 1A-6's pins of those behaviours); any other test under `tests/` asserting `autosubmit` or `data-filter-nav`
- Test: Create `tests/unit/test_filter_forms_apply.py`

**Interfaces:**
- Consumes: Part 1A's `data-autosubmit` attribute and its `ui.js` listener (removed here).
- Produces: every GET filter form carries `<button type="submit">Apply</button>`; no `data-autosubmit` anywhere.

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_filter_forms_apply.py`:

```python
"""FN-04: a dropdown never submits on change; every GET filter form with a select
has a visible submit button."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_GET_FORM = re.compile(r'<form\b(?=[^>]*\bmethod="get")[^>]*>(.*?)</form>', re.I | re.S)


def _templates():
    return sorted((ROOT / "templates").rglob("*.html"))


def test_nothing_submits_on_change():
    offenders = []
    for path in [*_templates(), *(ROOT / "static/js").rglob("*.js")]:
        text = path.read_text(encoding="utf-8")
        for needle in ("autosubmit", "applyFilter", "this.form.submit()", "<noscript><button"):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == []


def test_every_get_form_with_a_select_has_a_submit_button():
    missing = []
    for path in _templates():
        for match in _GET_FORM.finditer(path.read_text(encoding="utf-8")):
            body = match.group(1)
            if "<select" in body and 'type="submit"' not in body:
                missing.append(f"{path.relative_to(ROOT)}: {match.group(0)[:90]}")
    assert missing == []
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/unit/test_filter_forms_apply.py -v`. Expected: both fail, listing the files above.

- [ ] **Step 3: Implement** — in `templates/admin/users.html` replace the whole `<!-- Filters -->` element
  (the comment and the element after it, through its closing tag) with:

```html
<!-- Filters -->
<form method="get" action="/admin/users" class="bg-white rounded-lg border border-gray-200 p-4 mb-4 flex flex-wrap items-end gap-4">
    <div>
        <label for="status-filter" class="text-xs font-medium text-gray-600 block mb-1">Profile Status</label>
        <select id="status-filter" name="status_filter" class="border border-gray-300 rounded px-2 py-1 text-sm">
            <option value="">All</option>
            <option value="no_profile" {% if status_filter == 'no_profile' %}selected{% endif %}>No Profile</option>
            <option value="generating" {% if status_filter == 'generating' %}selected{% endif %}>Generating</option>
            <option value="complete" {% if status_filter == 'complete' %}selected{% endif %}>Complete</option>
            <option value="pending_update" {% if status_filter == 'pending_update' %}selected{% endif %}>Pending Update</option>
        </select>
    </div>
    <div>
        <label for="claimed-filter" class="text-xs font-medium text-gray-600 block mb-1">Claimed</label>
        <select id="claimed-filter" name="claimed_filter" class="border border-gray-300 rounded px-2 py-1 text-sm">
            <option value="">All</option>
            <option value="claimed" {% if claimed_filter == 'claimed' %}selected{% endif %}>Claimed</option>
            <option value="unclaimed" {% if claimed_filter == 'unclaimed' %}selected{% endif %}>Unclaimed</option>
        </select>
    </div>
    <button type="submit" class="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded-md hover:bg-indigo-700">Apply</button>
</form>
```

  and delete the `function applyFilter()` `<script>` block at the end of the template if it is still there.
  `templates/manager/pis.html`: the same replacement with `action="/manager/pis"`, and delete its
  `applyFilter` script. `templates/admin/jobs.html`: replace its `<!-- Filters -->` element with:

```html
<!-- Filters -->
<form method="get" action="/admin/jobs" class="bg-white border border-gray-200 rounded-lg p-4 mb-4 flex flex-wrap items-end gap-4">
    <div>
        <label for="status-filter" class="text-xs font-medium text-gray-600 block mb-1">Status</label>
        <select id="status-filter" name="status_filter" class="border border-gray-300 rounded px-2 py-1 text-sm">
            <option value="">All</option>
            {% for s in job_statuses %}
            <option value="{{ s }}" {% if status_filter == s %}selected{% endif %}>{{ s | capitalize }}</option>
            {% endfor %}
        </select>
    </div>
    <div>
        <label for="type-filter" class="text-xs font-medium text-gray-600 block mb-1">Type</label>
        <select id="type-filter" name="type_filter" class="border border-gray-300 rounded px-2 py-1 text-sm">
            <option value="">All</option>
            {% for t in job_types %}
            <option value="{{ t }}" {% if type_filter == t %}selected{% endif %}>{{ t | replace('_', ' ') | title }}</option>
            {% endfor %}
        </select>
    </div>
    <button type="submit" class="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded-md hover:bg-indigo-700">Apply</button>
</form>
```

  and delete its `applyFilter` script. In each of `templates/admin/assessments.html`,
  `templates/manager/assessments.html`, `templates/admin/discussions.html` (run selector form),
  `templates/manager/discussions.html` (run selector form) and `templates/manager/prompt_suggestions.html`
  (status form): delete every ` data-autosubmit` attribute and every remaining `onchange="this.form.submit()"`,
  and insert immediately before that form's `</form>`:

```html
        <button type="submit" class="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded-md hover:bg-indigo-700">Apply</button>
```

  In `templates/admin/simulation.html`'s run selector delete ` data-autosubmit` from `<select name="run"`
  and replace `<noscript><button type="submit" class="px-3 py-1 rounded bg-gray-200 text-base">Switch</button></noscript>`
  with `<button type="submit" class="px-3 py-1 rounded bg-gray-200 text-base">Apply</button>`. In
  `static/js/ui.js` delete BOTH the `data-autosubmit` behaviour and the `data-filter-nav` /
  `data-filter-param` behaviour (the users, PI and jobs filters are now real GET forms), and delete
  the `change` listener if nothing else uses it. Then update Part 1A's tests and journey, which pin
  the deleted behaviours (assembly audit PX-03c, PX-08c):
  - `tests/unit/test_ui_behaviours.py` (Task 1A-6): delete `test_every_filter_nav_holds_its_params`
    and `test_autosubmit_replaces_every_form_submit_handler`; in
    `test_ui_js_delegates_every_behaviour_on_document` remove the needles `'"[data-filter-nav]"'`,
    `'"data-filter-param"'`, `'"data-autosubmit"'` and `"control.form.requestSubmit()"` (and the
    `document.addEventListener("change"` assertion if the listener is gone), and add
    `assert "data-autosubmit" not in UI_JS and "data-filter-nav" not in UI_JS`.
  - `tests/e2e/ui_audit/journeys_phase1.py` `journey_ui_behaviours` (Task 1A-6): replace the filter leg
    ```python
        await page.select_option("#status-filter", "complete")
        await page.wait_for_url(f"{base}/admin/users?status_filter=complete")
        out["filter_nav"] = page.url == f"{base}/admin/users?status_filter=complete"
    ```
    with
    ```python
        await page.select_option("#status-filter", "complete")
        await page.locator("form:has(#status-filter) button[type=submit]").click()
        await page.wait_for_url("**status_filter=complete**")
        out["filter_nav"] = "status_filter=complete" in page.url
    ```
    and in the sort leg insert, between `await sort.select_option(other)` and
    `await page.wait_for_url(f"**sort={other}**")`, the line
    `await page.locator("form:has(#assessments-sort-select) button[type=submit]").click()`.
  - Any other assertion under `tests/` naming `autosubmit` or `data-filter-nav`
    (`grep -rn "autosubmit\|data-filter-nav" tests/` lists them).

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/unit/test_filter_forms_apply.py tests/unit/test_reachability.py tests/integration/test_admin_jobs_page.py tests/integration/test_assessment_list_chrome.py tests/integration/test_assessment_queue_controls.py tests/integration/test_discussions_filters.py -v`, then `.venv-test/bin/python -m pytest tests/integration/test_rendered_page_gate.py -v`. Expected: all pass.

- [ ] **Step 5: Rebuild CSS and commit** — run `scripts/build_css.sh`, then:

```bash
git add templates static/js/ui.js static/css/app.css tests
git commit -m "fix(webui-2): Apply buttons replace submit-on-change dropdowns (FN-04)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-12: Links underlined in text; non-colour signals (X-03, X-06)

**Files:**
- Modify: `static/css/input.css` (append), `static/css/app.css` (rebuilt)
- Modify: `templates/admin/simulation.html:693-710` (gantt table), `templates/admin/_assessment_detail_body.html:287,315,779-785` (glyph spans), `:1120-1121` and `:1158-1159` (dimension fieldsets)
- Test: Create `tests/unit/test_low_a11y_markup.py`

**Interfaces:**
- Consumes: `gantt_links[].announced` (`src/services/simulation_view.py:412`).
- Produces: CSS rule `main p a:not([class*="bg-"]), main dd a:not([class*="bg-"]), main li a:not([class*="bg-"])` underlined.

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_low_a11y_markup.py`:

```python
"""X-03 (links in text are underlined, not colour-only) and X-06 (no colour-only
signals; glyph labels on role=img; grouped dimension selects)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = ROOT / "templates"


def test_links_in_text_are_underlined_in_the_compiled_css():
    assert 'main p a:not([class*="bg-"])' in (ROOT / "static/css/input.css").read_text()
    assert "main p a:not(" in (ROOT / "static/css/app.css").read_text()


def test_the_gantt_table_states_announced_in_text():
    text = (T / "admin/simulation.html").read_text()
    assert ">Announced</th>" in text
    assert "{{ 'yes' if link.announced else 'no' }}" in text


def test_glyph_labels_sit_on_role_img():
    text = (T / "admin/_assessment_detail_body.html").read_text()
    for span in re.findall(r"<span[^>]*\baria-label=[^>]*>", text):
        assert 'role="img"' in span, span


def test_dimension_selects_are_a_fieldset_with_a_legend():
    text = (T / "admin/_assessment_detail_body.html").read_text()
    assert text.count("<legend") >= 2
    assert '<label class="block text-sm font-medium text-gray-600 mb-1 mt-3">Rubric dimensions (optional)</label>' not in text
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/unit/test_low_a11y_markup.py -v`. Expected: all four fail.

- [ ] **Step 3: Implement** — append to `static/css/input.css` (after the `@tailwind` directives, outside any `@layer`, so it is always emitted):

```css
/* X-03 (web UI remediation Phase 2): a link inside running text is underlined, so
   it is not distinguished by colour alone. Button-styled links (a bg-* class) are
   left alone. */
main p a:not([class*="bg-"]),
main dd a:not([class*="bg-"]),
main li a:not([class*="bg-"]) {
  text-decoration-line: underline;
  text-underline-offset: 2px;
}
```

  `templates/admin/simulation.html`: in the gantt table header add, after the `Outcome` `<th>`,
  `<th class="px-4 py-2 text-left text-sm font-semibold text-gray-600">Announced</th>`, and in the row,
  after `<td class="px-4 py-1">{{ link.outcome }}</td>`, add
  `<td class="px-4 py-1">{{ 'yes' if link.announced else 'no' }}</td>`.
  `templates/admin/_assessment_detail_body.html`: add ` role="img"` to each `<span …>` carrying
  `aria-label=` (lines 287, 315, 779, 781, 783, 785: insert `role="img" ` before `title=`). Replace (both
  occurrences, the edit form and the add form)

```html
<label class="block text-sm font-medium text-gray-600 mb-1 mt-3">Rubric dimensions (optional)</label>
```

  followed by its `{{ dimension_score_rows(...) }}` line, with a fieldset around the same macro call —
  edit form:

```html
                            <fieldset class="mt-3">
                                <legend class="block text-sm font-medium text-gray-600 mb-1">Rubric dimensions (optional)</legend>
                                {{ dimension_score_rows(review_rubric, review.dimension_scores or {}, edit_prefix) }}
                            </fieldset>
```

  add form:

```html
            <fieldset class="mt-3">
                <legend class="block text-sm font-medium text-gray-600 mb-1">Rubric dimensions (optional)</legend>
                {{ dimension_score_rows(review_rubric, {}, 'add-') }}
            </fieldset>
```

  Run `scripts/build_css.sh`.

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/unit/test_low_a11y_markup.py tests/integration/test_assessment_review_ui.py tests/integration/test_assessment_detail_page.py tests/integration/test_admin_simulation_page.py -v`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add static/css/input.css static/css/app.css templates/admin/simulation.html templates/admin/_assessment_detail_body.html tests/unit/test_low_a11y_markup.py
git commit -m "fix(webui-2): underline links in text; announced as text, glyphs as images, grouped dimension selects (X-03, X-06)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-13: Landmarks and headings; the §6.10 gate checks them (X-04)

**Files:**
- Modify: `templates/base.html` (impersonation banner through manager sub-nav, lines 32-163; flash block 165-176)
- Modify: `templates/login.html:16`, `templates/admin/agent_detail.html:135`, `templates/admin/access_requests.html:86,121`, `templates/admin/_assessment_detail_body.html:68`, `templates/error.html` (Part 1C)
- Modify: `tests/integration/test_rendered_page_gate.py` (Part 1C file; append a self-contained section)
- Modify: `tests/e2e/ui_audit/journeys_phase2_2b.py`

**Interfaces:**
- Consumes: Part 1C's `tests/integration/test_rendered_page_gate.py` and `templates/error.html`; Phase 0 harness axe-core at `tests/e2e/ui_audit/axe.min.js` (`tests.e2e.ui_audit.harness.AXE_PATH`, downloaded and checksum-pinned by Phase 0's `run.py` before any journey runs).
- Produces: `_landmark_and_h1_problems(html: str) -> list[str]`; `async def _render_structure_pages(client, db_session) -> dict[str, str]` (path → HTML, reused by Task 2B-14); journey `journey_landmarks_headings_and_link_underlines`.

- [ ] **Step 1: Write the failing tests** — append to `tests/integration/test_rendered_page_gate.py`
  (imports are local to keep the Part 1C module's top-level imports untouched):

```python
# --- X-04 (Phase 2, Part 2B): landmarks and headings --------------------------------


def _landmark_and_h1_problems(html: str) -> list[str]:
    """X-04 on one full document: exactly one <main> and one <h1>, headings that never
    skip a level going down, every <nav> named (aria-label/aria-labelledby) with unique
    names, and no empty <th>. A fragment (no <html>) is not a page and passes."""
    from html.parser import HTMLParser

    if "<html" not in html:
        return []

    class _Scan(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.mains = 0
            self.headings: list[int] = []
            self.navs: list[str | None] = []
            self.empty_th = 0
            self._th: list[str] | None = None
            self._th_label = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "main":
                self.mains += 1
            elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self.headings.append(int(tag[1]))
            elif tag == "nav":
                self.navs.append(a.get("aria-label") or a.get("aria-labelledby"))
            elif tag == "th":
                self._th, self._th_label = [], a.get("aria-label")

        def handle_endtag(self, tag):
            if tag == "th" and self._th is not None:
                if not "".join(self._th).strip() and not self._th_label:
                    self.empty_th += 1
                self._th = None

        def handle_data(self, data):
            if self._th is not None:
                self._th.append(data)

    scan = _Scan()
    scan.feed(html)
    problems = []
    if scan.mains != 1:
        problems.append(f"{scan.mains} <main> elements")
    if scan.headings.count(1) != 1:
        problems.append(f"{scan.headings.count(1)} <h1> elements")
    previous = 0
    for level in scan.headings:
        if level > previous + 1:
            problems.append(f"heading skips from h{previous} to h{level}")
        previous = level
    if any(not n for n in scan.navs):
        problems.append("a <nav> without aria-label")
    named = [n for n in scan.navs if n]
    if len(named) != len(set(named)):
        problems.append(f"duplicate <nav> labels {named}")
    if scan.empty_th:
        problems.append(f"{scan.empty_th} empty <th>")
    return problems


def test_the_landmark_checker_has_teeth():
    page = '<html><body><nav aria-label="Main"></nav><main><h1>T</h1><h2>S</h2></main></body></html>'
    assert _landmark_and_h1_problems(page) == []
    assert _landmark_and_h1_problems("<div><h3>fragment</h3></div>") == []
    bad = '<html><body><nav></nav><nav aria-label="A"></nav><nav aria-label="A"></nav><h2>x</h2><h4>y</h4><table><tr><th></th></tr></table></body></html>'
    problems = _landmark_and_h1_problems(bad)
    assert "0 <main> elements" in problems and "0 <h1> elements" in problems
    assert "heading skips from h0 to h2" in problems and "heading skips from h2 to h4" in problems
    assert "a <nav> without aria-label" in problems and "1 empty <th>" in problems
    assert any(p.startswith("duplicate <nav> labels") for p in problems)


async def _render_structure_pages(client, db_session) -> dict[str, str]:
    """Path -> HTML for a fixed page set across roles, with factory data (shared with
    the R-01 table check). Includes the login page and an HTML 404 error page."""
    import uuid

    from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
    from tests import factories
    from tests.integration.test_manager_access import auth_headers

    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    agent = await factories.make_agent(db_session, user=pi)
    run = await factories.make_simulation_run(db_session, status="stopped")
    await factories.make_agent_message(db_session, run=run, phase="new_post", thread_ts=None, message_ts="900.1")
    await factories.make_llm_call_log(db_session, run=run)
    pages = [
        (None, "/login"),
        (admin, "/admin/users"), (admin, f"/admin/users/{pi.id}"), (admin, f"/admin/users/{uuid.uuid4()}"),
        (admin, "/admin/jobs"), (admin, "/admin/activity"), (admin, f"/admin/activity/{run.id}"),
        (admin, f"/admin/activity/{run.id}/llm-calls"), (admin, "/admin/discussions"),
        (admin, "/admin/agents"), (admin, f"/admin/agents/{agent.id}"), (admin, "/admin/cohorts"),
        (admin, "/admin/access-requests"), (admin, "/admin/simulation"),
        (manager, "/manager/pis"), (manager, f"/manager/pis/{pi.id}"), (manager, "/manager/discussions"),
        (manager, "/manager/activity"), (manager, "/manager/slack-bots"),
        (manager, "/manager/prompt-suggestions"), (manager, "/manager/assessments"),
        (pi, "/settings"), (pi, "/profile"),
    ]
    rendered: dict[str, str] = {}
    for user, path in pages:
        headers = {"Accept": "text/html"}
        if user is not None:
            headers.update(auth_headers(user.id))
        response = await client.get(path, headers=headers)
        assert response.status_code in (200, 404), (path, response.status_code)
        rendered[path] = response.text
    return rendered


async def test_full_pages_have_landmarks_one_h1_and_ordered_headings(client, db_session):
    pages = await _render_structure_pages(client, db_session)
    failures = {path: p for path, html in pages.items() if (p := _landmark_and_h1_problems(html))}
    assert failures == {}
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_rendered_page_gate.py -k "landmark or h1" -v`. Expected: the teeth test passes; the page test fails, reporting `a <nav> without aria-label` on every page, `0 <h1> elements` on `/login`, `heading skips from h1 to h3` on `/admin/agents/<id>`, and `2 empty <th>` on `/admin/access-requests` (when its allowlist/denied tables render).

- [ ] **Step 3: Implement** — `templates/base.html`:
  1. Insert `<header>` on the line before `{% if impersonation_banner %}`, and change the banner's
     `<div class="bg-amber-400 text-amber-900 px-4 py-2 text-sm flex items-center justify-between">` to
     `<div role="region" aria-label="Impersonation" class="bg-amber-400 text-amber-900 px-4 py-2 text-sm flex flex-wrap items-center justify-between gap-2">`.
  2. Replace `<nav class="bg-white shadow-sm border-b border-gray-200">` with
     `<nav aria-label="Main" class="bg-white shadow-sm border-b border-gray-200">`.
  3. Replace the admin sub-navigation's outer `<div class="bg-gray-50 border-b border-gray-200">` with
     `<nav aria-label="Admin sections" class="bg-gray-50 border-b border-gray-200">` and its matching
     closing `</div>` (the last `</div>` before that block's `{% endif %}`) with `</nav>`; do the same
     for the manager sub-navigation with `aria-label="Manager sections"`.
  4. Insert `</header>` on the line after the manager sub-navigation's `{% endif %}`.
  5. Move the whole `<!-- Flash messages -->` block (as Part 1C left it) to be the first content inside
     `<main id="main-content" …>`, before `{% block content %}`.
  `templates/login.html`: replace
  `<h2 class="text-xl font-semibold text-gray-800 mb-2">Sign in with ORCID</h2>` with
  `<h1 class="text-xl font-semibold text-gray-800 mb-2">Sign in with ORCID</h1>`.
  `templates/admin/agent_detail.html:135`: replace
  `<h3 class="font-medium text-gray-700 mb-2">What stands behind this lab</h3>` with
  `<h2 class="font-medium text-gray-700 mb-2">What stands behind this lab</h2>`.
  `templates/admin/access_requests.html:86` and `:121`: replace `<th class="px-4 py-3"></th>` with
  `<th class="px-4 py-3"><span class="sr-only">Actions</span></th>`.
  `templates/admin/_assessment_detail_body.html:68`: insert `aria-label="On this page" ` after `<nav `.
  `templates/error.html`: if its status title is not an `<h1>`, change that element's tag to `h1`
  (opening and closing tag, classes kept). If the test names any other page with
  `heading skips from hN to hM`, change that page's first skipped heading to level N+1 (tag only); if it
  names a page with `0 <h1> elements`, change that page's title heading to `<h1>`.

- [ ] **Step 3b: The every-route gate checks structure too (spec §7 "the §6.10 gate adds
  landmark and h1 checks"; assembly audit PX-18).** In `tests/integration/test_rendered_page_gate.py`
  (Task 1C-15), move `_landmark_and_h1_problems` above `page_problems` and make `page_problems` end
  with `problems += _landmark_and_h1_problems(html)` before `return problems`. Run
  `.venv-test/bin/python -m pytest tests/integration/test_rendered_page_gate.py -v`; every page the
  walk reports is a real X-04 defect: give it its `<h1>` / landmark fix in this step (same edit
  shapes as Step 3), never an exemption, then rerun until it passes.

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/integration/test_rendered_page_gate.py tests/integration/test_login_page.py tests/integration/test_impersonation_guards.py tests/integration/test_manager_views.py -v`. Expected: all pass.

- [ ] **Step 5: Add the axe journey** — in `tests/e2e/ui_audit/journeys_phase2_2b.py` add after `_modality`:

```python
def _routes(ids: dict) -> dict[str, list[str]]:
    """The crawl set per seeded role (the PI's /agent is expanded to its agent pages
    after its redirect is followed)."""
    run, first = ids["run"], ids["assessments"][0]
    return {
        "anon": ["/login"],
        "admin": [
            "/admin/users", f"/admin/users/{ids['pi']}", f"/admin/users/{BAD_UUID}", "/admin/jobs",
            "/admin/activity", f"/admin/activity/{run}", f"/admin/activity/{run}/llm-calls",
            "/admin/discussions", "/admin/agents", "/admin/assessments",
            *[f"/admin/assessments/{a}" for a in ids["assessments"]],
            "/admin/cohorts", "/admin/cohorts/topology", "/admin/access-requests",
            "/admin/simulation", "/manager/prompt-suggestions",
        ],
        "manager": [
            "/manager/pis", f"/manager/pis/{ids['pi']}", "/manager/assessments",
            f"/manager/assessments/{first}", "/manager/discussions", "/manager/activity",
            f"/manager/activity/{run}", "/manager/slack-bots", "/manager/prompt-suggestions",
        ],
        "reviewer": ["/manager/pis", "/manager/assessments", f"/manager/assessments/{first}"],
        "pi": ["/profile", "/profile/edit", "/settings", "/agent"],
    }


async def _visit_all(h, width: int, on_page, *, bypass_csp: bool = False) -> list[dict]:
    """Load every crawl route at `width`, calling `await on_page(page, role, url, response)`
    for each, and return the list of its non-None results. Journeys that inject axe-core
    pass `bypass_csp=True`: Playwright injects it as an inline script, which the enforced
    script-src (Task 2B-15) blocks; the CSP journey itself never bypasses."""
    results = []
    for role, paths in _routes(h.ids).items():
        context, page, _log = await h.page(role, width=width, bypass_csp=bypass_csp)
        try:
            queue = list(paths)
            while queue:
                path = queue.pop(0)
                response = await page.goto(h.base_url + path, wait_until="networkidle")
                if path == "/agent" and page.url.endswith("/dashboard"):
                    queue += [page.url.replace(h.base_url, "").replace("/dashboard", tail)
                              for tail in ("/conversations", "/public-profile")]
                result = await on_page(page, role, page.url.replace(h.base_url, ""), response)
                if result is not None:
                    results.append(result)
        finally:
            await context.close()
    return results


_X04_RULES = ["region", "landmark-unique", "landmark-one-main", "page-has-heading-one",
              "heading-order", "empty-table-header", "link-in-text-block"]


async def journey_landmarks_headings_and_link_underlines(h) -> dict:
    """X-04 and X-03 via axe-core at 1280 px: zero violations of the landmark, heading,
    empty-header and link-in-text-block rules on every crawled page."""
    async def on_page(page, role, url, response):
        if "html" not in (response.headers.get("content-type", "") if response else ""):
            return None
        await page.add_script_tag(path=str(_AXE))
        violations = await page.evaluate(
            """async (rules) => (await axe.run(document, {runOnly: {type: 'rule', values: rules}}))
                 .violations.map(v => ({id: v.id, n: v.nodes.length,
                                        sample: v.nodes.slice(0, 2).map(n => n.target.join(' '))}))""",
            _X04_RULES,
        )
        return {"role": role, "url": url, "violations": violations} if violations else None

    failing = await _visit_all(h, WIDE, on_page, bypass_csp=True)
    return {"ok": not failing, "failing_pages": failing}
```

  and append `journey_landmarks_headings_and_link_underlines,` to `JOURNEYS`.

- [ ] **Step 6: Lint, rebuild CSS, commit** — `.venv-test/bin/python -m ruff check tests/e2e/ui_audit/journeys_phase2_2b.py tests/integration/test_rendered_page_gate.py` (expected `All checks passed!`), run `scripts/build_css.sh`, then:

```bash
git add templates static/css/app.css tests/integration/test_rendered_page_gate.py tests/e2e/ui_audit/journeys_phase2_2b.py
git commit -m "fix(webui-2): header and labelled navs, one h1 per page, ordered headings; gate checks them (X-04)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-14: Narrow screens: wrapping filters, scrolling tables, responsive tiles, long strings (R-01, FN-03, R-02)

**Files:**
- Modify: `static/css/input.css` (append), `static/css/app.css` (rebuilt)
- Modify (table wrapper `overflow-hidden` → `overflow-x-auto`): `templates/manager/pis.html:79`, `templates/manager/activity.html:36`, `templates/manager/prompt_suggestions.html:72`, `templates/manager/slack_bots.html:30`, `templates/admin/cohorts.html:70`, `templates/admin/agents.html:29,60,121,153`, `templates/admin/access_requests.html:23,67,111`, `templates/admin/simulation.html:344,375,644`, `templates/admin/activity.html:36`, `templates/admin/_discussions_threads.html:34`, `templates/admin/jobs.html:54`, `templates/admin/users.html:47`, `templates/admin/cohort_detail.html:48,117,145`
- Modify (wrap an unwrapped `<table>`): `templates/manager/prompt_suggestion_detail.html:82`, `templates/manager/pi_detail.html:388`, `templates/admin/user_detail.html:194`, `templates/admin/_run_detail_body.html:35,60,85`, `templates/admin/simulation.html:550,616,693`, `templates/admin/_assessments_body.html:239`
- Modify (filter rows wrap): `templates/admin/assessments.html:93`, `templates/manager/assessments.html:93`, `templates/admin/discussions.html:18,61-62`, `templates/manager/discussions.html:18` and its filter form, `templates/manager/prompt_suggestions.html:5,7`
- Modify (tiles): `templates/admin/_assessments_body.html:177`, `templates/admin/agents.html:8`, `templates/admin/access_requests.html:7`, `templates/admin/_run_detail_body.html:12`
- Modify: `tests/integration/test_rendered_page_gate.py`, `tests/e2e/ui_audit/journeys_phase2_2b.py`
- Test: Create `tests/unit/test_responsive_markup.py`

**Interfaces:**
- Consumes: `_render_structure_pages` (Task 2B-13); `_routes`, `_visit_all` (Task 2B-13 journeys).
- Produces: `_unscrollable_tables(html: str) -> int`; journey `journey_narrow_crawl_has_no_overflow`.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/test_responsive_markup.py`:

```python
"""R-01/FN-03 (filters wrap, tables scroll, tiles stack) and R-02 (long strings wrap)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T = ROOT / "templates"


def test_no_table_sits_in_a_clipping_wrapper():
    offenders = []
    for path in sorted(T.rglob("*.html")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            if "overflow-hidden" in line and "<div" in line:
                following = "\n".join(lines[n + 1:n + 6])
                if "<table" in following:
                    offenders.append(f"{path.relative_to(ROOT)}:{n + 1}")
    assert offenders == []


def test_summary_tiles_stack_on_a_phone():
    assert '<div class="grid grid-cols-2 sm:grid-cols-5 gap-4 mb-8">' in (T / "admin/_assessments_body.html").read_text()
    for name in ("admin/agents.html", "admin/access_requests.html", "admin/_run_detail_body.html"):
        assert not re.search(r'class="grid grid-cols-[3-5] gap', (T / name).read_text()), name


def test_long_strings_and_selects_cannot_widen_the_page():
    css = (ROOT / "static/css/input.css").read_text()
    for rule in ("main {\n  overflow-wrap: anywhere;", "main table {\n  overflow-wrap: break-word;",
                 "select {\n  max-width: 100%;"):
        assert rule in css, rule
```

  Append to `tests/integration/test_rendered_page_gate.py`:

```python
# --- R-01/FN-03 (Phase 2, Part 2B): every table can scroll sideways ------------------


def _unscrollable_tables(html: str) -> int:
    """How many <table> elements have no `overflow-x-auto`/`overflow-auto` ancestor."""
    from html.parser import HTMLParser

    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    class _Scan(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack: list[tuple[str, list[str]]] = []
            self.bad = 0

        def handle_starttag(self, tag, attrs):
            if tag in void:
                return
            classes = (dict(attrs).get("class") or "").split()
            if tag == "table" and not any(
                c in ("overflow-x-auto", "overflow-auto") for _t, cs in self.stack for c in cs
            ):
                self.bad += 1
            self.stack.append((tag, classes))

        def handle_endtag(self, tag):
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break

    scan = _Scan()
    scan.feed(html)
    return scan.bad


def test_the_table_checker_has_teeth():
    assert _unscrollable_tables('<div class="overflow-x-auto"><table></table></div>') == 0
    assert _unscrollable_tables('<div class="overflow-hidden"><table></table></div>') == 1


async def test_every_rendered_table_can_scroll_sideways(client, db_session):
    pages = await _render_structure_pages(client, db_session)
    failures = {path: n for path, html in pages.items() if (n := _unscrollable_tables(html))}
    assert failures == {}
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/unit/test_responsive_markup.py tests/integration/test_rendered_page_gate.py -k "table or tiles or long_strings" -v`. Expected: the source tests fail listing the wrapper lines; `test_every_rendered_table_can_scroll_sideways` fails naming `/admin/users`, `/admin/jobs`, `/admin/activity`, `/admin/agents`, `/admin/simulation`, `/manager/pis` and others.

- [ ] **Step 3: Implement** — on each wrapper line listed in **Files**, replace the class token
  `overflow-hidden` with `overflow-x-auto` (keep every other class). Wrap each listed unwrapped table: insert
  `<div class="overflow-x-auto">` on the line before its `<table` and `</div>` on the line after its
  matching `</table>`. Filter rows: `templates/admin/assessments.html:93` and
  `templates/manager/assessments.html:93` and `templates/manager/prompt_suggestions.html:5`
  `<div class="flex items-center justify-between mb-2 gap-4">` → `<div class="flex flex-wrap items-center justify-between mb-2 gap-4">`;
  `templates/manager/prompt_suggestions.html:7` `<div class="flex items-center gap-4">` →
  `<div class="flex flex-wrap items-center gap-4">` and its status form's `class="flex items-center gap-2"` →
  `class="flex flex-wrap items-center gap-2"`; `templates/admin/discussions.html:18` and
  `templates/manager/discussions.html:18` `<div class="flex items-center justify-between mb-6">` →
  `<div class="flex flex-wrap items-center justify-between gap-4 mb-6">`, their run forms'
  `class="flex items-center gap-2"` → `class="flex flex-wrap items-center gap-2"`, their
  `<!-- Filters -->` row `<div class="flex items-center gap-4 mb-4">` → `<div class="flex flex-wrap items-center gap-4 mb-4">`
  and its form `class="flex items-center gap-3"` → `class="flex flex-wrap items-center gap-3"`. Tiles:
  `templates/admin/_assessments_body.html:177` `grid grid-cols-5 gap-4 mb-8` → `grid grid-cols-2 sm:grid-cols-5 gap-4 mb-8`;
  `templates/admin/agents.html:8` `grid grid-cols-4 gap-4 mb-8` → `grid grid-cols-2 sm:grid-cols-4 gap-4 mb-8`;
  `templates/admin/access_requests.html:7` `grid grid-cols-3 gap-4 mb-8` → `grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8`;
  `templates/admin/_run_detail_body.html:12` `grid grid-cols-3 gap-4 mb-6` → `grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6`.
  Append to `static/css/input.css`:

```css
/* R-02 (web UI remediation Phase 2): a long unbroken string (a URL, an id) wraps
   instead of widening the page. `anywhere`, not the break-words utility's
   `break-word`, because only `anywhere` lowers min-content width, which a flex item
   or grid cell (tag pills, dd values, chat bubbles) needs. Tables keep `break-word`
   so their columns are not squeezed; they scroll in their overflow-x-auto wrappers. */
main {
  overflow-wrap: anywhere;
}
main table {
  overflow-wrap: break-word;
}
[data-markdown] pre,
[data-markdown] table,
.md-content pre,
.md-content table {
  display: block;
  max-width: 100%;
  overflow-x: auto;
}
.tag-pill {
  max-width: 100%;
}
/* R-01: a select is never wider than its row (long run labels). */
select {
  max-width: 100%;
}
```

  Run `scripts/build_css.sh`.

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/unit/test_responsive_markup.py tests/integration/test_rendered_page_gate.py tests/integration/test_assessment_list_chrome.py tests/integration/test_admin_simulation_page.py -v`. Expected: all pass.

- [ ] **Step 5: Add the 375 px journey** — in `tests/e2e/ui_audit/journeys_phase2_2b.py` add:

```python
async def journey_narrow_crawl_has_no_overflow(h) -> dict:
    """R-01/FN-03/R-02 (spec §9): at 375 px no crawled page overflows the viewport
    horizontally on the seeded realistic data (long names, 300-character URLs)."""
    async def on_page(page, role, url, response):
        if "html" not in (response.headers.get("content-type", "") if response else ""):
            return None
        overflow = await page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        if overflow <= 0:
            return None
        widest = await page.evaluate(
            """() => [...document.querySelectorAll('body *')]
                 .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1
                              && getComputedStyle(e).position !== 'fixed')
                 .slice(0, 4).map(e => e.tagName + '.' + String(e.className).slice(0, 60))"""
        )
        return {"role": role, "url": url, "overflow_px": overflow, "widest": widest}

    failing = await _visit_all(h, NARROW, on_page)
    return {"ok": not failing, "failing_pages": failing}
```

  and append `journey_narrow_crawl_has_no_overflow,` to `JOURNEYS`.

- [ ] **Step 6: Lint and commit** — `.venv-test/bin/python -m ruff check tests/e2e/ui_audit/journeys_phase2_2b.py tests/unit/test_responsive_markup.py tests/integration/test_rendered_page_gate.py` (expected `All checks passed!`), then:

```bash
git add templates static/css/input.css static/css/app.css tests/unit/test_responsive_markup.py tests/integration/test_rendered_page_gate.py tests/e2e/ui_audit/journeys_phase2_2b.py
git commit -m "fix(webui-2): wrapping filters, scrolling tables, stacking tiles, wrapping long strings (R-01, FN-03, R-02)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2B-15: Enforce the CSP (last task of this part)

**Files:**
- Modify: `src/web/security_headers.py` (Part 1A, Task 1A-7: `SCRIPT_POLICY_ENFORCED` and the module and middleware docstrings)
- Modify: `tests/unit/test_security_headers.py`, `tests/integration/test_security_headers.py` and every other test that asserts the Phase 1 report-only header (`grep -rln "REPORT_ONLY_POLICY\|content-security-policy-report-only\|SCRIPT_POLICY_ENFORCED" tests/` lists them)
- Modify: `tests/e2e/ui_audit/journeys_phase2_2b.py`
- Test: Create `tests/integration/test_csp_enforced.py`

**Interfaces:**
- Consumes: `ENFORCED_POLICY`, `REPORT_ONLY_POLICY` (whose `form-action` is already `'self' https://slack.com` from Task 1A-7, amended at plan assembly), `SCRIPT_POLICY_ENFORCED`, `security_header_items`, `SecurityHeadersMiddleware`, `request.state.csp_nonce` (Part 1A); route `POST /api/csp-report` (Part 1A).
- Produces: `SCRIPT_POLICY_ENFORCED = True`. With it, `security_header_items` sends one `Content-Security-Policy: ENFORCED_POLICY + "; " + REPORT_ONLY_POLICY.format(nonce=...)` and no `Content-Security-Policy-Report-Only` (Task 1A-7's own code path). The constant keeps the name `REPORT_ONLY_POLICY` (its docstring says it is now enforced) so no other module changes. Journey `journey_enforced_csp_crawl_has_no_violations`.

- [ ] **Step 0: Precondition — a real Slack provisioning under the report-only policy (plan audit
  Q2-01).** The provisioning POSTs answer with a 302 to whatever OAuth URL Slack's API returns
  (`src/services/slack_provisioning.py`), so no local test follows the real redirect chain. On
  production, with Phase 1 deployed (report-only), provision one bot from `/admin/agents/<id>` or
  `/manager/pis/<id>` in a real browser, then check the web logs for CSP report lines
  (`docker logs copi-blackbird-app-1 --since 30m 2>&1 | grep "csp_report"`, the line Task 1A-8 logs per report). Proceed only if no
  `form-action` report appears; if one names a host other than `slack.com` or a `*.slack.com`
  subdomain, add that host to `form-action` in `REPORT_ONLY_POLICY` and to this task's test first.
  Record the result (date, host list) in the commit message.

- [ ] **Step 1: Write the failing test** — create `tests/integration/test_csp_enforced.py`:

```python
"""Spec §7 "CSP": the §6.3 script policy is enforced, `report-uri` kept, the
report-only header gone. `form-action` admits Slack, where both provisioning forms
redirect."""

import re

import pytest

from src.models import USER_ROLE_ADMIN
from src.web.security_headers import REPORT_ONLY_POLICY, SCRIPT_POLICY_ENFORCED
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_DIRECTIVES = (
    "default-src 'self'", "style-src 'self' 'unsafe-inline'", "img-src 'self' data:",
    "font-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'",
    "object-src 'none'", "report-uri /api/csp-report",
)


def _policy(response) -> str:
    assert "content-security-policy-report-only" not in response.headers
    return response.headers["content-security-policy"]


async def test_pages_carry_the_enforced_policy_with_a_fresh_nonce(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    first = _policy(await client.get("/admin/jobs", headers=auth_headers(admin.id)))
    second = _policy(await client.get("/login"))
    for policy in (first, second):
        for directive in _DIRECTIVES:
            assert directive in policy, directive
        assert re.search(r"script-src 'self' 'nonce-[A-Za-z0-9_-]+'", policy)
    nonce = re.compile(r"'nonce-([^']+)'")
    assert nonce.search(first).group(1) != nonce.search(second).group(1)


async def test_the_enforced_policy_lets_the_slack_provisioning_redirect_through(client):
    assert "form-action 'self' https://slack.com https://*.slack.com" in _policy(await client.get("/login"))


async def test_error_and_head_responses_are_covered_too(client):
    assert "script-src" in _policy(await client.get("/no-such-page", headers={"Accept": "text/html"}))
    assert "script-src" in _policy(await client.head("/login"))


def test_the_script_policy_is_enforced_and_takes_the_nonce():
    assert SCRIPT_POLICY_ENFORCED is True
    assert "'nonce-abc'" in REPORT_ONLY_POLICY.format(nonce="abc")
```

- [ ] **Step 2: Run** — `.venv-test/bin/python -m pytest tests/integration/test_csp_enforced.py -v`. Expected: every request test fails on `assert 'content-security-policy-report-only' not in ...`; `test_the_script_policy_is_enforced_and_takes_the_nonce` fails (`SCRIPT_POLICY_ENFORCED is False`).

- [ ] **Step 3: Implement** — in `src/web/security_headers.py` change

```python
#: False in Phase 1 (report only). Phase 2 sets it True and changes nothing else.
SCRIPT_POLICY_ENFORCED = False
```

to

```python
#: True since Phase 2 (spec §7): the script policy is enforced together with
#: ENFORCED_POLICY in one Content-Security-Policy header, `report-uri` kept.
SCRIPT_POLICY_ENFORCED = True
```

  and in the module docstring and `REPORT_ONLY_POLICY`'s comment replace "report-only" wording
  with "enforced since Phase 2". In `tests/unit/test_security_headers.py` (Task 1A-7):
  `test_phase_1_reports_the_script_policy_and_enforces_the_rest` becomes
  `test_phase_2_enforces_the_script_policy` asserting `SCRIPT_POLICY_ENFORCED is True`, and the
  header-items test expects one combined `Content-Security-Policy` and no report-only item; in
  `tests/integration/test_security_headers.py` every assertion on `content-security-policy-report-only` becomes
  `assert "content-security-policy-report-only" not in response.headers` plus the same directive
  assertion on `content-security-policy`. Apply the same rewrite to every other file the grep
  in **Files** lists.

- [ ] **Step 4: Run** — `.venv-test/bin/python -m pytest tests/integration/test_csp_enforced.py tests/integration/test_head_requests.py tests/integration/test_rendered_page_gate.py -v`, then every file the grep in Step 3 listed. Expected: all pass.

- [ ] **Step 5: Add the CSP journey** — in `tests/e2e/ui_audit/journeys_phase2_2b.py` add:

```python
_CSP_PROBE = """
  window.__cspViolations = [];
  document.addEventListener('securitypolicyviolation', function (e) {
    window.__cspViolations.push(e.violatedDirective + ' ' + (e.blockedURI || 'inline'));
  });
"""


async def journey_enforced_csp_crawl_has_no_violations(h) -> dict:
    """Spec §9 Phase 2: with the policy enforced, no crawled page logs a CSP violation,
    every HTML response carries the enforced header and no report-only header, and
    the two script-driven interactions (an LLM-call body loaded on expand, the chat
    drawer opened) run clean."""
    async def on_page(page, role, url, response):
        headers = response.headers if response else {}
        problems = []
        if "html" in headers.get("content-type", ""):
            if "script-src" not in headers.get("content-security-policy", ""):
                problems.append("no enforced script-src")
            if "content-security-policy-report-only" in headers:
                problems.append("report-only header still sent")
        if url.endswith("/llm-calls") and await page.locator("details summary").count():
            await page.locator("details summary").first.click()
            await page.wait_for_timeout(800)
        if "/admin/assessments/" in url and await page.locator("[data-chat-bubble]").count():
            await page.locator("[data-chat-bubble]").click()
            await page.wait_for_timeout(800)
        problems += await page.evaluate("window.__cspViolations || []")
        problems += [m for m in console if "Content Security Policy" in m]
        console.clear()
        return {"role": role, "url": url, "problems": problems} if problems else None

    console: list[str] = []
    failing = []
    for role, paths in _routes(h.ids).items():
        context, page, _log = await h.page(role, width=WIDE)
        page.on("console", lambda m: console.append(m.text))
        await page.add_init_script(_CSP_PROBE)
        try:
            for path in paths:
                response = await page.goto(h.base_url + path, wait_until="networkidle")
                result = await on_page(page, role, page.url.replace(h.base_url, ""), response)
                if result is not None:
                    failing.append(result)
        finally:
            await context.close()
    return {"ok": not failing, "failing_pages": failing}
```

  and append `journey_enforced_csp_crawl_has_no_violations,` to `JOURNEYS`.

- [ ] **Step 6: Lint and commit** — `.venv-test/bin/python -m ruff check tests/e2e/ui_audit/journeys_phase2_2b.py tests/integration/test_csp_enforced.py` (expected `All checks passed!`), then:

```bash
git add src/web/security_headers.py tests/integration/test_csp_enforced.py tests/e2e/ui_audit/journeys_phase2_2b.py tests
git commit -m "feat(webui-2): enforce the content security policy, keeping report-uri (spec §7 CSP)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Self-review

**Spec coverage**

| Spec §7 bullet / finding | Task |
|---|---|
| CSP: report-only → enforced, `report-uri` kept; §9 crawl shows zero console violations | 2B-15 |
| C-14 25 s cache of the live-run stats per run | 2B-3 |
| C-15 discussions paged in SQL; HTML refuses `run_id=all` above a cap | 2B-4 |
| C-15 PI list paged in SQL | 2B-5 |
| C-16 `llm_calls` defers bodies; fragment route on expand | 2B-6 |
| B-07 only the tool-use slice | 2B-7 |
| B-08 polling skips the stale sweep | 2B-8 |
| C-18 filters from the enum; unknown value refused without a 500 | 2B-9 |
| C-19 `le=MAX_PAGE` | 2B-6 |
| C-20 server-side `>= 0` | 2B-9 |
| C-21, C-22 labels and latency source consistent | 2B-6 |
| C-23 deterministic hub choice | 2B-3 |
| B-09, D-28 copy | 2B-9 |
| B-18 `urlencode` | 2B-9 |
| M-02 HEAD on every GET route, no body (incl. redirects) | 2B-1 |
| FN-07 one formatter that always shows the zone | 2B-2 |
| B-16, B-17, B-20 (+ journeys) | 2B-10 |
| X-03 underline links in text | 2B-12 (journey in 2B-13) |
| X-04 labelled navs, `h1` on login and error pages, heading order, empty header; gate adds landmark and `h1` checks (+ axe journey) | 2B-13 |
| X-06 announced as text, `role="img"`, `fieldset`/`legend` | 2B-12 |
| FN-04 Apply buttons | 2B-11 |
| R-01, FN-03 wrapping filters, `overflow-x-auto` wrappers, `grid-cols-2 sm:grid-cols-5` tiles | 2B-14 |
| R-02 wrapping long strings; markdown `pre`/`table` scroll | 2B-14 |
| §9 Phase 2: 375 px crawl, no overflow | 2B-14 |

**Placeholder scan:** no TBD/TODO. Three instructions are rules applied to the failing test's own
output rather than enumerated edits, each with an exact rule: the timestamp conversion table (2B-2, the
source test lists every line), the heading fix rule for pages other than the four named (2B-13), and the
Part 1A/1C test assertions renamed for the enforced policy and the retired `autosubmit` (2B-15, 2B-11;
the exact `grep` that lists them is given). These depend on Phase 1 text this part cannot read.

**Interface consistency:** `HeadAsGetMiddleware` (2B-1); `timestamp(dt, style)` / filter `ts` (2B-2, used
by 2B-11's jobs block via `job_statuses`/`job_types` from 2B-9); `LIVE_RUN_STATS_TTL_SECONDS`,
`_LIVE_RUN_STATS`, `_clock`, `_hub_agent_id`, `_run_aggregates`, `_cached_run_aggregates` (2B-3);
`DISCUSSIONS_ALL_RUNS_MAX`, `all_runs_refused` (2B-4); `PI_DIRECTORY_PAGE_SIZE`, `count_pi_directory`,
`list_pi_directory(page=)`, context `user_total`/`page`/`page_count` (2B-5, consumed by 2B-11's filter
blocks only through the unchanged `status_filter`/`claimed_filter` names); `admin_llm_call_bodies`,
`admin/_llm_call_bodies.html`, `_avg_api_call_latency_ms`, `data-lazy-fragment` (2B-6, exercised by the
2B-15 journey); `TOOL_BLOCKS_SLICE` (2B-7); `list_history(sweep=)`, `_own_turn_is_stale`, `?poll=1`,
`loadHistory(isPoll)` (2B-8); `JOB_STATUSES`, `JOB_TYPES`, `_NON_NEGATIVE_INT_FORM` (2B-9);
`applyModality`, `keepClearOfDrawer`, ids `chat-src-show-<turnKey>-<n>` (2B-10, used by its journeys);
`_landmark_and_h1_problems`, `_render_structure_pages` (2B-13, reused by 2B-14); `_unscrollable_tables`
(2B-14); `_routes`, `_visit_all` (2B-13, reused by 2B-14 and 2B-15); `ENFORCED_POLICY` (2B-15). External
names consumed: Phase 0 `Harness` (`h.base_url`, `h.ids`, `h.page`), Part 1A `SecurityHeadersMiddleware`,
`ENFORCED_POLICY`, `REPORT_ONLY_POLICY`, `static/js/ui.js`, `data-autosubmit`, `scripts/build_css.sh`,
Part 1C `tests/integration/test_rendered_page_gate.py`, `templates/error.html`, flash block. Assumptions
on the harness (seed id keys, `"anon"` role, `tests/e2e/ui_audit/axe.min.js`) are stated in 2B-10 and
2B-13 Interfaces. `journeys_phase2.py` is created here holding only 2B's list; other Phase 2 parts add
their module's `JOURNEYS` to it at merge.

---

### Task 2-Z: Phase gate, records, merge, deploy

**Files:**
- Modify: `docs/audits/open-findings.md` (the rows listed in Step 5)

- [ ] **Step 1: Rebuild the CSS and commit it if it changed (assembly audit PX-15).**
  `bash scripts/build_css.sh`; commit `static/css/app.css` if `git status --porcelain` lists it.
- [ ] **Step 2: Full gate.** Run `./scripts/ci.sh`. Expected: exit 0.
- [ ] **Step 3: Harness gate.** With the policy enforced, run
  `.venv-test/bin/python -m tests.e2e.ui_audit.run all --phase 2`, then `journeys --phase 1` and
  `journeys --phase 0`. Expected: exit 0 for each; `journey_enforced_csp_crawl_has_no_violations`
  reports zero violations; the crawl reports no enforced-CSP console error; the 375 px journey
  reports no document overflow on the seeded realistic data.
- [ ] **Step 4: Adversarial audit.** Dispatch `engineering:auditor` (model `opus`) with
  `git diff blackbird...webui/phase-2`, spec §7 and §12, this plan and the harness report. Fix
  confirmed defects first.
- [ ] **Step 5: Register (assembly audit PX-13).** Set `fixed`, with the fixing task's commit, on
  `2026-10-01/` + A-10, A-14, A-15, A-16, A-17, B-03, B-04, B-07, B-08, B-09, B-11, B-13, B-16,
  C-02, C-06, C-14, C-15, C-16, C-18, C-19, C-21, C-24, C-29, D-07, D-09, D-11, D-12, D-14, D-15,
  D-17, D-18, D-19, D-20, FN-02, FN-04, FN-07, FN-08, M-02, M-03, M-08, R-01, R-02, SN-01, SN-02,
  X-03, X-04, X-06; and the pre-existing `2026-09-30/W-admin-cross-delete` and
  `2026-09-30/W-admin-access-denial` (A-07). `2026-10-01/A-19` stays `deferred` (accepted);
  `2026-10-01/B-18` stays `refuted` (append "urlencode hygiene in `<hash>`"). `2026-10-01/C-03` was closed at Task 1-Z (amendment A11); Task 2A-21's regression test is its extra evidence (plan audit Q2-09). Run
  `.venv-test/bin/python -m pytest tests/unit/test_open_findings_register.py -v` (PASS); commit
  `docs(webui-2): register rows for Phase 2 fixes` with the attribution line.
- [ ] **Step 6: Merge.** `git switch blackbird && git merge --no-ff webui/phase-2`.
- [ ] **Step 7: Deploy (operator; assembly audit PX-12, PX-16).** Task 2B-7 edits
  `src/services/assessment_detail.py`, which the engine imports (`src/agent/engine/verdicts.py`),
  so the agent image is rebuilt too (`CLAUDE.md`: any change to what `src/agent/` imports).
  ```bash
  git tag rollback-pre-webui-2 HEAD^1            # the pre-merge blackbird commit (spec §10)
  git rev-parse rollback-pre-webui-2
  for s in blackbird-app worker agent; do
    docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-webui-2
  done
  git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini
  # must print nothing
  DC="docker compose -f docker-compose.prod.yml"
  $DC build blackbird-app worker && $DC --profile agent build agent
  $DC up -d blackbird-app worker
  curl -sI https://blackbird.copi.science/login | grep -iE '^HTTP|^content-security-policy:'
  ```
  Expected: `HTTP/2 200` (HEAD now answered) and an enforced `content-security-policy` containing
  `script-src 'self' 'nonce-`. No migration. Recreate the agent (`$DC up -d agent`) only when
  `/admin/simulation` shows no live run; tell the owner it changed.
- [ ] **Step 8: Before any push (D15; owner decision pending).** Exploit detail is already in
  local history (commit `e8f475f`: `docs/audits/2026-10-01-web-ui/raw/harness/*`, README payloads)
  and in `tests/e2e/ui_audit/seed.py`. Editing files does not remove it from history. The push
  procedure is the owner's decision recorded in spec §2 (see the open question raised with the
  plans); do not push until it is recorded.
