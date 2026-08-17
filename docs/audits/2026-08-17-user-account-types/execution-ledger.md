# SDD ledger — plan: docs/plans/2026-08-17-user-account-types-plan.md

Spec: docs/specs/2026-08-17-user-account-types-design.md (read; binding authority)
Branch: feat/user-account-types, off blackbird @ b1de2ec
Plan+spec committed as 8ee3fed

Ruling: work in-place on a feature branch, not a git worktree — `.venv-test` and
`.env` are gitignored and do not travel, `scripts/ci.sh` hard-requires the venv at
the repo root, and docker-compose.prod.yml bind-mounts only ./profiles ./prompts
./data (verified), so in-place edits to src/ and templates/ cannot reach the running
production stack. Cost if wrong: a branch to delete; no production exposure.

## Pre-flight conflict scan

### Cross-task rows (tasks sharing a file or an interface)

| Tasks | Produced → consumed | Finding |
|---|---|---|
| T1→T2 | `User.is_staff` hybrid → `get_staff_user` | clean |
| T1→T3 | `USER_ROLE_PI` → `roles=` filter | clean |
| T1→T8 | `VALID_USER_ROLES`, `USER_ROLE_ADMIN` → role endpoint guards | clean |
| T2→T4,5,6 | `get_staff_user` → router-level dependency | clean |
| T2→T4,5,6,7,8 | `auth_headers` helper in test_manager_access.py → imported by 5 test files | clean; defined once |
| T3→T4 | `list_pi_directory(**kwargs)`, `load_user_detail`→dict\|None keys user/profile/publications/jobs | clean; T4 handler uses exactly those keys |
| T3→T5 | `list_assessments`→10 keys, splatted `**view` into `_template_context` | clean; no key collides with request/current_user/active_page, so no duplicate-kwarg TypeError |
| T3→T6 | `build_discussions_view`, `list_runs_overview`, `build_run_detail` → 3 templates | clean; every template variable is in the produced key list |
| T1,T8 | both edit src/cli.py | clean; T1 rewrites grant/revoke/list-users, T8 appends role:set — disjoint |
| T3,T8 | both edit src/routers/admin.py | clean; T3 moves 6 bodies, T8 appends a new handler — disjoint |
| T4,T5,T6,T7 | all edit templates/base.html | clean BY SEQUENCING: T4/5/6 append sub-nav tabs after line 109; T7 edits lines 52-68. Insertions are below 74, so T7's line refs stay valid. Sub-nav must grow one tab per task or the reachability gate rejects a link to a not-yet-existing route |
| T4,T8 | templates/admin/user_detail.html | clean; T4 only READS it as a copy source, T8 modifies it |
| T4,T5,T6 | all edit src/routers/manager.py and tests/integration/test_manager_views.py | clean; each appends |
| T1,T10 | alembic 0028 → 0029 down_revision | clean; single head maintained |

### Per-task self-consistency rows

| Task | Its tests vs its code / its files vs later touches | Finding |
|---|---|---|
| T1 | test tolerates `user_role is None` pre-flush (correct: `default=` is INSERT-time); `grep "is_admin="` check cannot false-match `sa.Column("is_admin"` or `WHERE is_admin = true` | clean |
| T2 | probe app wires SessionMiddleware with `session_cookie="copi-session"`, matching the forged cookie; no AgentBadgeMiddleware so no session-factory bypass needed | clean |
| T3 | `_ASSESSMENTS_LIMIT` → `ASSESSMENTS_LIMIT` moved; its only admin.py reference was inside the extracted body | clean |
| T4 | `checked >= 8` in the admin-denial sweep: measured 13 GET admin routes without path params | clean |
| T4 | `test_directory_excludes_staff_accounts` asserts `"Mgr Self" not in body` | **BROKEN — see R1.** base.html:80 renders `{{ current_user.name }}`, so the viewing manager's own name is always in the page. This assertion could never pass |
| T4 | `test_pi_detail_has_no_delete_or_impersonate_control`: `/delete` and `impersonate` appear in base.html only inside `{% if impersonation_banner %}`, which is falsy here | clean |
| T5 | `"/admin/" not in body` with a manager viewer: base.html:69 gates the Admin link on `is_admin` (false for a manager); T7 has not yet run so /profile,/settings,/agent still render — none match `/admin/` | clean |
| T6 | `"export" not in body.lower()`: measured — `export` occurs exactly twice in admin/discussions.html, both `name="export"` buttons at lines 91/93, inside the filter block the manager wrapper omits. Zero occurrences in the extracted partial | correct but fragile — see R2 |
| T7 | `/profile` → `/onboarding` → `/manager/pis` bounce terminates in 2 hops; `Settings` stays ungated so `"Settings" in body` holds | clean |
| T8 | direct-call test passes `request=None`; the handler never reads `request` (matches admin_delete_user, which also ignores it) | clean |
| T8 | `test_user_detail_shows_the_role_and_no_admin_yes_no_row` asserts `"manager" in body` | **WEAK — see R3.** The new Account Type help text contains the word "manager" for every user, so the assertion passes trivially and the "no admin row" half asserts nothing |
| T9,T10 | docs + separate destructive migration | clean |

### Rulings from the scan

Ruling R1: T4's `test_directory_excludes_staff_accounts` must assert on row links,
not display names — `f"/manager/pis/{admin.id}" not in body` and
`f"/manager/pis/{pi.id}" in body`, dropping the `"Mgr Self" not in body` line.
Why: base.html:80 renders the viewer's own name unconditionally, so the original
assertion tests the nav, not the query. Cost if wrong: the test proves slightly less
about display text; the filter itself is still directly asserted.

Ruling R2: T6 keeps `"export" not in body.lower()` and ADDS the precise
`'name="export"' not in body`. Why: the loose form is factually correct today
(measured: 2 occurrences, both in the omitted block) but would break on unrelated
markup; the precise form is the real invariant. Cost if wrong: one redundant
assertion.

Ruling R3: T8's role-row test must assert the Role value renders in a `<dd>` and
that the literal `>Admin</dt>` row is gone, instead of a bare `"manager" in body`.
Why: the Account Type help text names all three roles, so the original passes
regardless of the fix. Cost if wrong: none — strictly stronger.

## Measured partial boundaries (prep for T5/T6, verified by reading the files)

- `templates/admin/assessments.html` (282 lines): last absolute link is line 36;
  intro `</p>` closes at 42. **Partial = lines 43-281.** Wrapper keeps 1-42 + include
  + line 282 `{% endblock %}`. Only blocks: title, content.
- `templates/admin/discussions.html` (196 lines): last absolute link (Clear) is line
  96; `</form>` 98, `</div>` 99, `<!-- Threads table -->` 101. Content block ends at
  191. **Partial = lines 101-190.** Wrapper keeps 1-100 + include + 191.
  Export buttons are lines 91-94 — the manager wrapper omits exactly those.
- `templates/admin/activity_detail.html` (139 lines): `<div class="max-w-5xl mx-auto">`
  opens line 5, closes 138. Header 6-16. **Actions block with the llm-calls link is
  lines 18-24.** Run summary starts 26. **Partial = lines 26-137.** Manager wrapper =
  1-16 (back link → /manager/activity) + include + 138 + 139, OMITTING 18-24.
- `templates/admin/activity.html` (73 lines): single link at line 47 (row onclick).
  Duplicated whole, no partial.

Ruling R4: T6's `templates/manager/discussions.html` MUST copy
`{% block extra_head %}` (admin/discussions.html lines 4-21) and
`{% block scripts %}` (lines 193-196) verbatim. Why: the shared threads partial
contains `data-markdown="..."` at line 155, and that attribute is rendered ONLY by
`/static/js/markdown.js`, which is loaded in extra_head along with marked@12.0.2 and
dompurify@3.1.6 (both SRI-pinned) and the `.proposal-md` styles. Omitting the block
silently renders every proposal summary as an empty, unstyled div. The plan did not
mention this. Cost if wrong: none — the copy is strictly required for correctness.

## Task 1

Ruling R5: scripts/migrate/postflight.py `VERIFIED_REVISIONS` stays at ("0019".."0023").
The implementer left it alone and asked. Correct: that file's own comment (lines 131-136)
records that its pinned EXPECTED_* expectations were never extended past 0023, so
0024/0025/0026/0027 are ALREADY outside it. Extending it for 0028 alone would assert
verification coverage that does not exist for the four intervening revisions. preflight.py
WAS correctly extended (PLANNED_OBJECTS gains the user_role column + ck_users_user_role
constraint, REVISION_ORDER and DEFAULT_TARGET bumped to 0028). Cost if wrong: the guarded
production-migration path does not post-verify users.user_role; mitigated by ci.sh's
upgrade/downgrade/upgrade round trip and by preflight's collision check covering 0028.

Note: implementer touched 3 files beyond the brief (scripts/migrate/preflight.py,
tests/integration/test_harness_smoke.py, tests/unit/test_migration_checks.py) plus one
extra test_cli.py assertion. All are hardcoded "0027"/"Admin"-column pins that a new
alembic head necessarily breaks. Legitimate, not scope creep; the plan should have
listed them.

Task 1 review: spec ✅, quality NEEDS FIXES — 3 Important. All three are real; all
three trace to plan text, so each gets a ruling before the fix dispatch.

Ruling R6 (I1, plan-mandated — FIX): the plan's SQL assertions
`assert "user_role" in str(select(User.is_admin))` are vacuous. The reviewer compiled
the escalation formulation `User.user_role.in_(("manager","admin"))` and showed it
contains "user_role" too, so it passes both tests. The F7 guard is therefore pinned in
Python only, while src/main.py:52-55 executes the SQL path — exactly the path that
matters. Spec is binding authority and F7 is its central invariant, so the finding beats
the plan text. Fix: compile with literal_binds=True, assert "users.user_role = 'admin'"
is present AND "manager" is absent. Cost if wrong: none, strictly stronger.

Ruling R7 (I2 — FIX, slightly wider than the finding): add BOTH "0026" and "0027" to
preflight.SUPPORTED_START_REVISIONS, not just 0027. Measured: the tuple is
("0018","0019","0020","0021","0023","0024","0025") and its own comment states the rule —
"Each stays supported afterward; nothing here narrows." The earlier bump to DEFAULT_TARGET
0027 already violated that for 0026; ours violates it for 0027. Leaving 0026 blocked
means run_migration.sh refuses a 0026-stamped database, and production's true stamp is
unknown (CLAUDE.md measured 0024 on 2026-08-06, before 0025-0027 existed). Also correct
the now-false comment at preflight.py:75 claiming 0026 is covered by the current==target
branch. Cost if wrong: two extra supported start states on a guarded operator script —
strictly more permissive, and every intervening migration is additive.

Ruling R8 (I3, plan-mandated — FIX): the plan said `admin:revoke` sets user_role=pi
unconditionally. That silently strips manager status and prints "Revoked admin from…"
about someone who was never an admin. Fix: demote only when user_role == admin;
otherwise report no change. MUST stay exit-0 for a non-admin target —
tests/integration/test_cli.py:396-399 invokes revoke twice through `_ok()` and requires
idempotence. Cost if wrong: a manager could still be demoted by `role:set`, which is
explicitly the unguarded escape hatch.

Deferred minors (for final-review triage): CHECK list in 0028 is a SQL string literal
untied to VALID_USER_ROLES; test_default_role_is_pi passes either way; comment at
user.py:32 restates its own definition; 3 touched lines >100 chars (E501 is ignored, not
a gate failure); no test pins the User(is_admin=...) constructor form. "VALID_USER_ROLES
has no src/ consumer" is NOT a defect — Task 8 adds one.

Ruling R9 (prep for T3, not a review finding): `admin_discussions` contains an EARLY
`return templates.TemplateResponse(...)` at src/routers/admin.py:522 for the
"no simulation runs at all" case, and that context omits `agents` and `agent_filter`
while the normal return at :756 supplies both. `build_discussions_view` must therefore
return the SAME 9 keys on both paths, with `agents=[]` and `agent_filter=[]` on the
empty path — the plan's interface listed one key set and did not mention the early
return.

Measured, so the record is accurate: this is NOT currently a 500. Jinja2's default
Undefined yields nothing when iterated (verified: `{% for a in agents %}` over an
undefined name renders ''), and no custom `undefined=` is configured anywhere in src/.
So today the no-runs page silently renders an empty agent dropdown. Normalizing the keys
renders byte-identically while removing the reliance on lenient-Undefined, which matters
because T6's manager wrapper would otherwise inherit the same dependency. Cost if wrong:
none — same output, one less implicit contract.

Task 1: fix round 1/5 (3 addressed, 0 open; commits 855bec4..e75cacb)
Task 1: complete (commits 8ee3fed..e75cacb, review clean)
  Verified independently by the controller: select(User.is_admin) compiles to
  "SELECT users.user_role = 'admin'", 'manager' absent; is_admin no longer in the mapped
  column list; manager.is_admin is False; assignment raises AttributeError.

## Task 2

Ruling R10: accept the `# noqa: B008` at tests/integration/test_manager_access.py:70.
Independently verified that ruff genuinely emits B008 for `Depends()` in an argument
default, and the test tree is held to zero findings, so a suppression is the only way to
keep FastAPI's own idiom in a test. First such suppression in tests/. Cost if wrong: one
suppressed lint on a throwaway probe route that exists only inside one test.

Task 2 review: spec ✅, quality APPROVED, 0 Critical/Important.
Task 2: minor (deferred): unused `monkeypatch` parameter at test_manager_access.py:74.
Task 2: minor (deferred): no test pins the unauthenticated (no-cookie) 302 through
  get_staff_user — inherited from get_current_user, and Task 4's
  test_unauthenticated_manager_root_redirects_to_login covers it on a real route.
Task 2: complete (commits e75cacb..ce532db, review clean)

## Task 3

Ruling R11: accept the one out-of-scope test edit
(tests/integration/test_opportunity_assessment_persistence.py:1513, repointing
`monkeypatch.setattr(admin_router, "_ASSESSMENTS_LIMIT", 1)` to
`monkeypatch.setattr(directory_service, "ASSESSMENTS_LIMIT", 1)`). Unavoidable: the brief
mandated moving that constant, which deleted the old patch target. Verified the test still
has teeth rather than silently going vacuous — ASSESSMENTS_LIMIT is defined only at
src/services/directory.py:43 and read at CALL time by both the query bound (:197) and the
reported value (:234), and admin.py holds no imported copy, so patching the service module
really does bound the query. Cost if wrong: a bounding test could pass without bounding;
checked, it does not.

Task 3 review: spec ✅, quality APPROVED, 0 Critical/Important, 6 Minor + 1 ⚠️.
Reviewer confirmed R9 (9-key early return) implemented at directory.py:276-287, confirmed
R11's reasoning, and confirmed the double filter application is PRE-EXISTING (present in
the removed half at old admin.py:503-507 and 574-578) so behaviour-preserving.
Reviewer also corrected the controller: my "consecutive same-agent LLM-log dedup" concern
was misfiled — that code is in src/routers/agent_page.py:273-282 and was never in admin.py.

Ruling R12: FIX the one Minor that is actually a constraint violation —
src/routers/admin.py:382. The old no-runs early return sat BEFORE the `if export:` branch;
after extraction it sits after, so on an instance with zero SimulationRuns
`/admin/discussions?export=true` now returns a PlainTextResponse attachment where it used
to return the HTML page. Graded Minor by the reviewer, and the blast radius is tiny
(admin-only, requires zero runs AND an explicit ?export=), but "no behaviour may have
changed" was this task's explicit hard constraint and this is its single violation. A
3-line reordering restores it. Cost if wrong: a zero-run instance's ?export= returns an
empty text file instead of HTML — which is arguably the better behaviour, so the downside
of fixing is nil either way.

Ruling R13: resolve the reviewer's ⚠️ (ci.sh not run for Task 3) without running it here.
Alembic is untouched since Task 1's full green ci.sh (single head 0028, round trip clean),
so those steps cannot have regressed. Coverage can only have risen: the 570 new src lines
are moved code already exercised by the 1961-test suite, plus 4 new tests. The plan makes
`./scripts/ci.sh` a mandatory step of Task 9, which is the authoritative gate. Cost if
wrong: a coverage/alembic regression surfaces at Task 9 instead of now.

Task 3: minor (deferred): directory.py:286 empty path hardcodes agent_filter=[] (per R9).
Task 3: minor (deferred): directory.py:379 in-function ProposalReview import (moved verbatim).
Task 3: minor (deferred): test_directory_service.py:44-47 in-function imports (from brief).
Task 3: minor (deferred): test_directory_service.py:22 bare-list assert assumes no other User rows.

Task 3: fix round 1/5 (1 addressed, 0 open; commits ff920ca..9c27fe9)
  Re-reviewer traced the revert case to prove the regression test has teeth: with the
  early return moved back after `if export:`, threads==[] -> empty proposals -> plain-text
  branch -> PlainTextResponse + Content-Disposition, failing both assertions.
Task 3: complete (commits ce532db..9c27fe9, review clean)

## Lint-ceiling defect in the plan (found before T4, quantified)

MEASURED: ruff src = 225, ceiling SRC_LINT_MAX = 231, so 6 findings of headroom. But 137
of those 225 are B008 ("Do not perform function call `Depends` in argument defaults") —
i.e. FastAPI's own idiom, at ~2 per handler (admin.py: 70 B008 across 35 handlers).
The plan adds roughly 15 handlers' worth of Depends defaults across T4/T5/T6/T8, so the
conventional style would reach ~240 and BREACH the ceiling at Task 6. The plan never
noticed this, and ci.sh + CLAUDE.md both forbid raising SRC_LINT_MAX to accommodate code.

Ruling R14: new handlers in the NEW file src/routers/manager.py read their dependencies
from module-level singletons (`_DB = Depends(get_db)`, `_STAFF = Depends(get_staff_user)`,
`_AGENT_FILTER = Query(default=[])`), which is precisely the remedy B008's own message
recommends ("read the default from a module-level singleton variable"). Verified
empirically: conventional style = 2 findings per handler, singleton style = 0. T8's single
new handler is appended to admin.py and should match that file's surrounding style
instead (+3 findings -> 228), because consistency inside a 2000-line file beats a lone
divergent idiom. Net: the whole plan lands at ~228 <= 231 with ZERO noqa suppressions and
no ceiling change.
Rejected: (a) `# noqa: B008` on ~15 lines — has src/ precedent (E731/E402/BLE001) but is
noisy and inconsistent beside 137 un-suppressed identical patterns; (b) ruff
`extend-immutable-calls = ["fastapi.Depends", ...]`, the officially documented FastAPI fix
— technically the best answer, but it silently reclassifies 137 existing findings, taking
the measurement from 225 to ~88 and breaking comparability with the historical 249/231
baselines that CLAUDE.md documents. Re-baselining a repo-wide quality gate is the user's
call, not mine, so it is surfaced as a recommendation rather than applied.
Cost if wrong: manager.py reads slightly differently from the other routers.

## Task 4

Task 4 review: spec ✅ (Rulings A/B/C all followed correctly), quality NEEDS FIXES —
1 Important (test-only), 0 Critical. Reviewer independently confirmed the security
boundary: is_admin is strictly user_role=='admin' in both Python and SQL so a manager is
excluded at dependencies.py:74 AND at the duplicate main.py:52-55; manager.py:170 tests
the LOADED row's role and returns an identical 404 for missing-row and wrong-role (no
existence oracle); directory.py:61-62 excludes staff in SQL while keeping claimed_at IS
NULL stubs. Ruling B confirmed safe: Depends is immutable and FastAPI caches on the
callable, so the shared _STAFF singleton and the router-level gate resolve once.

Ruling R15 (Important — FIX): tests/integration/test_manager_views.py:568-577.
`_manager_get_paths()` is dead code and its docstring falsely claims the sweeps enumerate
the router "with path params filled by name"; the actual deny sweep at :595 hand-lists two
paths. This is precisely the property the plan was built around — "routes enumerated from
the router so a route added later is automatically covered" — and it is the mechanism that
keeps the deny-by-default guarantee honest rather than aspirational. Tasks 5 and 6 add four
more routes that the hand-list would silently skip. Fix: parametrize the PI-denied and
staff-allowed sweeps over the live `manager.router.routes`, substituting concrete values
for path params. Cost if wrong: a future un-gated manager route ships untested.

Ruling R16 (two Minors folded into the same round, both genuine coverage gaps, ~6 lines):
(a) test_manager_views.py has no impersonate/delete assertion against `pis.html` — the
template derived from the file that actually carried the impersonation widget, and the
reachability gate cannot catch a regression there because /admin/impersonate is a real
route; (b) no test renders a CLAIMED PI with a profile, so pis.html's status and version
columns only ever exercise their empty branches. Both are cheap and both close holes in
the exact guarantees this task exists to provide.

Task 4: minor (deferred): manager.py:88,101 unused logging import + logger.
Task 4: minor (deferred): manager.py:132 institution_filter accepted but not passed to the
  template — verified harmless, neither pis.html nor admin/users.html reads it (0 hits).
Task 4: minor (deferred): manager.py:122 response_class=HTMLResponse on a handler that
  returns RedirectResponse — affects only the OpenAPI schema.
Task 4: minor (deferred): test_manager_views.py:693 the 200 assertion alone does not prove
  non-elevation; the paired /admin/users 403 is what proves it.

Task 4: fix round 1/5 (3 addressed, 0 open; commits eb1b924..b601f34)
  Re-reviewer confirmed the red-on-new-route invariant HOLDS by tracing code: a new GET
  route is auto-swept by both sweeps; a new POST/PUT/DELETE trips
  test_manager_router_exposes_no_mutating_routes; and a missing gate cannot hide behind a
  coincidental 404 because the router-level dependency raises 403 before any handler body
  and the deny sweep asserts the specific 403. Also verified Finding 3 is not a false pass:
  pis.html has one guaranteed "Complete" dropdown option, so the test's >=2 count genuinely
  requires a populated row.
Task 4: complete (commits 9c27fe9..b601f34, review clean)

## Task 5

Ruling R17: accept the implementer's rewording of the partial's leading comment. The
brief prescribed a verbatim comment whose own prose and embedded grep example contained
literal "/admin/" and "/manager/" substrings — which would have failed the same task's
hard-gated check that `grep -n '/admin/\|/manager/' templates/admin/_assessments_body.html`
prints nothing. My brief was self-contradictory; the implementer preserved the comment's
meaning without the substrings. Cost if wrong: the comment's wording differs from the plan
text while conveying the same rule.

Controller-verified: partial is 249 lines with ZERO absolute URLs; both wrappers are 44
lines and include it (282 -> 44+249 shared); manager wrapper's only links are the two
/manager/assessments ones; base.html sub-nav has exactly /manager/pis + /manager/assessments
with no premature Discussions/Activity tab; router is 4 routes, all GET; full ./scripts/ci.sh
green with ruff src at exactly 225 and coverage 75.48%.

Task 5 review: spec ✅ (reviewer extracted the 239 removed lines and the 239 partial lines
to files and diff'd them — ZERO differences; all tri-state gating branches, all nine rubric
dimensions, the confidence bracket-strip guard and every {% if %}/{% endif %} pairing moved
verbatim), quality APPROVED with 1 Important, 0 Critical, 0 Minor.

⚠️ RESOLVED by the controller (reviewer could not see it — the sweep tests are unchanged so
absent from the diff): `_manager_get_paths()` now returns 4 paths including
/manager/assessments, so both the PI-denied and staff-can-reach sweeps cover the new route
automatically. Confirmed by calling the helper directly.

Ruling R18 (Important, plan-mandated — FIX): all three new manager-assessments tests run
against an EMPTY assessments table, so they prove only "200 with the wrapper title" and
"403 for a PI" — never that the shared verdict table renders through the manager route.
Verified the hole is real, not theoretical: the table lives inside `{% if assessments %}`
at _assessments_body.html:89 with the empty branch at :245-247, and it is the ONLY consumer
of `rubric_weights` and `runs_by_id`. So a context key missing from the manager route's
splat would render a clean "No assessments recorded yet." and pass every current test.
Table-rendering coverage today comes from test_opportunity_assessment_persistence.py, which
exercises /admin/assessments only. This is a gap in my brief, not implementer error.
Cost if wrong: the manager assessments page could ship silently blank-bodied.

Task 5: fix round 1/5 (1 addressed, 0 open; commits 9108862..5c9576a)
  Re-reviewer independently verified false-pass resistance per assertion by grepping all of
  templates/: the classes the helpers key on (band-label, gating-row, score-) appear nowhere
  outside this partial's row/detail-row markup, so no assertion can be satisfied by the
  static header, gating legend or run-selector dropdown. Also confirmed the unscored-vs-low
  distinction (val is none -> em-dash at body.html:228-238).
Task 5: complete (commits b601f34..5c9576a, review clean)

## Task 6

Note: the implementer's first turn ended without a commit or a report (it stalled after
completing the edits). Resumed it to run the suites, write the report and commit; no work
was lost and the controller made no edits.

Ruling R19: accept the implementer's one-line correction to my measured boundary. I
specified the discussions partial as lines 101-190; the correct end is **189**, because line
190's `{% endif %}` closes an `{% if not selected_run_id %}` opened at line 42 — inside the
WRAPPER. Moving that tag into an {% include %}d partial is a Jinja2 TemplateSyntaxError
(the implementer verified empirically; I confirmed line 42 is `{% if not selected_run_id %}`
and that all nine templates now parse under a real Jinja environment). Both wrappers keep
their own trailing {% endif %} after the include. Cost if wrong: none — verified by parse.

Controller-verified in the committed tree: manager/discussions.html carries both
{% block extra_head %} (line 4, with markdown.js and both SRI integrity hashes) and
{% block scripts %} (line 102), satisfying R4 — without them every proposal summary would
render as an empty unstyled div; both partials contain zero absolute URLs; zero
name="export" in the manager wrapper; zero llm-calls in manager/activity_detail.html; no
stray /admin/ link in any templates/manager/*.html; base.html sub-nav complete with all four
tabs; router at 7 routes all GET; the staff sweep backs {run_id} with a real SimulationRun;
ruff src 225; full ./scripts/ci.sh green with coverage 75.61%.

## INCIDENT: 13 untracked files deleted and recovered (2026-08-17 15:36:10 UTC)

Surfaced by the Task 6 reviewer as an aside, not by any test. 12 untracked files present at
session start (SECOND_INSTANCE_SETUP.md, slack_install_links.md, scripts/make_install_links.py,
docs/blackbird-star-topology-runbook.md, 4 docs/plans/*, 4 docs/specs/*) plus
logs/profiles_public_pre_sync_1786656614.tgz had vanished from disk.

CAUSE (established, not guessed): a `git stash` with untracked files included, run during
Task 3's implementer turn and then DROPPED. Evidence: dangling 3-parent commit 4cf440a dated
exactly 2026-08-17 15:36:10 reading "WIP on feat/user-account-types: ce532db", matching the
15:36:11 mtime shared by docs/, docs/specs/, docs/plans/ and scripts/. A 3-parent stash is
the -u/--include-untracked form; its THIRD parent (3a5140c) holds the untracked-file tree.
Ruled out first: zero `git clean`, zero `git checkout --`, zero `rm -rf` across all 30
subagent transcripts (fixed-string search). Task 3's transcript mentions `git stash` 5 times.

RECOVERY: all 13 files restored from 3a5140c via `git cat-file`, verified BYTE-IDENTICAL by
comparing `git hash-object` against `git rev-parse $U:$path` — 13/13 match, 0 mismatched.
Sizes cross-check the session-start `ls -la` (14581 / 2883 / 71066 bytes). Deliberately NOT
restored: src/services/directory.py and tests/integration/test_directory_service.py, which
were also in that stash as WIP but whose committed, reviewed versions are current.
Objects pinned against gc as refs/recovered/untracked-20260817-1536 and
refs/recovered/dropped-stash-20260817-1536.

PREVENTION: every remaining dispatch must be told explicitly never to run `git stash -u`,
`git stash --include-untracked`, or `git clean`. This repo's working tree carries the user's
untracked, never-committed documents, so a stash-and-drop is silent data loss.

Task 6 review: spec ✅, quality APPROVED, 1 Important + 4 Minor, 0 Critical.
Reviewer verified mechanically: threads extraction 88/88 lines byte-identical; run-detail
body 112/112 identical modulo a uniform 4-space dedent; all three "(unknown sender)" NULL
agent_id guards intact and the documented sorted() guard untouched in
src/services/directory.py:425-430,548-554; both extra_head blocks byte-identical with SRI
hashes copied not retyped; zero absolute URLs in partials; sweep assertion unweakened;
if/endif balance 10/10 in BOTH wrappers.

Ruling R20 (Important, plan-mandated — FIX): the export-absence test is vacuous, and so are
both new happy-path tests. With no SimulationRun seeded, build_discussions_view returns
selected_run_id=None, so the manager wrapper renders its empty branch and the filter form —
where the admin export buttons live — is never emitted at all. So "no export control" is
asserted against markup that could not have contained one either way, and NO test ever
renders the manager threads partial or its data-markdown attribute. That last point matters
most: R4 exists because omitting extra_head silently blanks every proposal summary, and
there is currently no test that would catch it. Exactly parallel to Task 5's finding, and
again a gap in my brief. Cost if wrong: the R4 protection is unverified by any test.

Task 6: minor (deferred): test_manager_views.py:90-93 bare SimulationRun() instead of factories.
Task 6: minor (deferred): templates/manager/activity.html duplicates 71/73 lines of the admin
  twin — forced by the link-locality rule; a "keep in sync" comment is the right fix.
Task 6: minor (deferred): src/routers/manager.py:60 module-level Query(default=[]) mutable
  default is safe (pydantic deep-copies) but undocumented.
Task 6: minor (deferred): shared partials live under templates/admin/ despite being shared.

Task 6: fix round 1/5 (1 addressed, 0 open; commits ba3bd0a..d28d667)
  Re-reviewer confirmed the export re-assertion is no longer vacuous (filter form now
  actually renders) and that the two R4 probes are jointly necessary: dropping extra_head
  kills the <script> tag but leaves data-markdown intact, while deleting the {% include %}
  does the reverse — so neither assertion alone pins R4 and both are present. Also confirmed
  the literal-tag form dodges the decoy HTML comment that mentions the bare path.
Task 6: complete (commits 5c9576a..d28d667, review clean)

## Task 7

CORRECTION to a controller-supplied fact: I told the implementer that both "Manager"
occurrences in templates/base.html sat inside is_staff guards. That was WRONG. The
`<!-- Manager sub-navigation -->` HTML comment was OUTSIDE its {% if ... is_staff %} guard
(introduced by my own Task 4 snippet), so it rendered on every page for every role. The
implementer found it and moved it inside — verified: base.html:121 is the {% if %}, :122 the
comment. No behaviour change for staff; it is what makes "Manager" genuinely absent for a PI.

Ruling R21: accept the implementer's rewrite of test_manager_profile_url_bounce_terminates.
The brief's verbatim version cannot pass under the installed httpx 0.28.1, which strips
manually-set Cookie headers on each redirect hop and rebuilds them from the client cookie
jar — so a header-only `auth_headers()` loses authentication on hop two and lands at /login.
Verified independently: httpx 0.28.1 confirmed, and its Client._redirect_headers does
manipulate cookies. The implementer seeded client.cookies instead of relaxing the assertion,
which is the correct direction. Also scanned the whole suite: this is the ONLY test combining
header-based auth with follow_redirects=True, so no other test is silently unauthenticated.
Cost if wrong: none — the server-side chain was independently replayed hop by hop.

Task 7 review: spec ✅, quality APPROVED, 0 Critical/Important, 2 Minor.
⚠️ resolved by the reviewer's hand-trace: an admin with onboarding_complete=False goes
/profile -> profile.py:48 -> /onboarding -> new non-PI bounce -> /manager/pis (get_staff_user
accepts admin). Terminates in 3 hops, no loop, no 403. Reviewer also independently confirmed
the cookie-jar rewrite is not a weakening (_session_cookie produces a signed value
independent of any Set-Cookie exchange, so seeding client.cookies is byte-equivalent to a
real jar and the two-hop chain is still exercised; the client fixture is function-scoped so
no cross-test leak).

KNOWN EDGE CASE for the final audit (not a fix, recorded deliberately): an ADMIN whose
onboarding_complete is False can no longer complete PI onboarding — the new non-PI bounce
sends them to /manager/pis, while base.html still shows them a "My Profile" link (gated on
`user_role == 'pi' or is_admin`, and is_admin is true for them). So that link is a dead end
for such an admin. It bites nobody today: every existing admin was migrated from
is_admin=True and already has onboarding_complete=True, and the spec explicitly says admins
keep /profile unchanged while roles are mutually exclusive (an admin is not a PI, so having
no research profile is coherent). Left as-is rather than churning the login flow; flagged so
the final review can triage it.

Task 7: minor (deferred): templates/base.html:174,184 duplicate the
  `user_role == 'pi' or is_admin` guard verbatim — plan-mandated.
Task 7: minor (deferred): the brief's "Consumes: User.is_staff" metadata does not match the
  implemented `is_manager` / `!= USER_ROLE_PI` checks — cosmetic brief inconsistency,
  semantically equivalent given only three roles exist.
Task 7: complete (commits d28d667..fde8491, review clean)

## Task 8

Task 8 review: spec ✅, quality APPROVED, 1 Important + 4 Minor, 0 Critical.
Reviewer independently confirmed: get_admin_user 403s on `not is_admin` and managers are
is_staff only; role validation precedes the SELECT with the ck_users_user_role CHECK behind
it; the self-change guard compares two uuid.UUID objects so casing/whitespace/brace variants
all normalise and trip it; the last-admin guard fires on exactly admin->non-admin and counts
before the write so <=1 correctly means "target is the only admin"; an impersonating admin
also gets 403 (a good property); and R3's two assertions genuinely discriminate (">Admin</dt>"
now appears nowhere in templates/, and the Role <dd> is the only place a bare "manager" can
render). scripts/ci.sh untouched.

Ruling R22 (Important — FIX): src/cli.py:178-209 `role:set` has ZERO tests, while
admin:grant/admin:revoke have a dedicated block at tests/integration/test_cli.py:373-474
that includes a regression test for this exact command family silently clobbering a role.
An untested CLI command that can set any role — including admin — on any account is not
acceptable on the privilege-granting surface, even though the code reads correctly.
Cost if wrong: a future edit to role:set breaks the recovery path with nothing to catch it.

Ruling R23 (three small items folded into the same round): (a) the 404 branch of the new
handler is untested; (b) only a manager is tested as a forbidden actor, never a `pi` —
worth pinning explicitly on a privilege endpoint; (c) src/models/user.py:82 still cites
templates/admin/user_detail.html:38 as a live is_admin consumer, but this very diff deleted
that row — a comment stating something factually untrue about the code, and cheap to correct.

Ruling R24 (two Minors deliberately DEFERRED, with reasons rather than silence):
- src/routers/admin.py:178-180 counts admins without filtering access_status, which gates
  login (auth.py:267-273), so an admin row that cannot log in still counts toward the
  last-admin guard. Real, but it makes the guard MORE conservative (harder to demote), which
  is the safe direction, and access_status is orthogonal to role by design.
- src/routers/admin.py:166-176 is count-then-write with no serialization, so two admins
  concurrently demoting each other could both observe 2 and both proceed, reaching zero
  admins. Genuine, but it needs two simultaneous cross-demotions by two different admins
  (the self-change guard blocks the single-actor path), and `role:set` from a container shell
  is the documented recovery. Fixing it properly means a locking or constraint-level change
  that is out of this plan's scope; surfaced for the final review to triage.

Task 8: fix round 1/5 (4 addressed, 0 open; commits 90edae4..19d855e)
  Re-reviewer confirmed the new CLI tests are not self-satisfying (round-trip and
  invalid-role both verify against the DB rather than stdout; unknown-ORCID checks the
  specific message, not just the exit code), and verified each surviving claim in the
  corrected user.py comment individually (main.py:53 really is select(User.is_admin);
  base.html 52/62/73 all reference is_admin; the CLI test really reads .is_admin;
  user_detail.html now has zero is_admin occurrences). user.py diff is comment-only with the
  hybrid bodies byte-identical, so is_admin-false-for-manager is preserved.
Task 8: complete (commits fde8491..19d855e, review clean)

## Task 9

Gate: ./scripts/ci.sh PASSED — single head 0028, clean round trip to 0018, ruff tests 0,
ruff src 227/231, coverage 75.82% (floor 60), 2005 passed / 93 skipped. scripts/ci.sh
untouched; neither SRC_LINT_MAX nor COV_MIN moved.

Task 9 review: spec ✅, quality NEEDS FIXES — 1 CRITICAL + 1 Important.
Reviewer fact-checked every other claim in the new CLAUDE.md section against code and all
held: manager route list (manager.py:66,125,142,175), impersonation gated on is_admin
(dependencies.py:74,132 and the duplicate main.py:52-55) with no impersonation path in
manager.py, is_manager/is_staff semantics (user.py:97-115), the appointment path and
last-admin guard (admin.py:164-214, user_detail.html:57), the role:set recovery command
(cli.py:178-181), the two-step provisioning flow (auth.py:213-308, admin.py:921-995), and
the collab_private disclosure matching D5.

Ruling R25 (CRITICAL — FIX): CLAUDE.md's new section states "There is no `is_admin` column
any more". That is FALSE of the database. 0028 is deliberately additive — it stops the ORM
mapping the column and gives it a server default, but never drops it; no 0029 file exists.
Worse, the claim contradicts the status line this SAME commit wrote into the design doc
three lines above ("0029 (the is_admin column drop) is NOT yet applied"). CLAUDE.md is the
first file every future agent and operator reads, and a confident wrong claim about live
schema state can misdirect a deploy — the whole reason 0028 was split from 0029 was to keep
old code working against the new schema. Must be scoped to the ORM, with the physical
column's continued existence stated explicitly. Cost if wrong: an operator or agent believes
the column is gone and reasons incorrectly about migration ordering or rollback.

Ruling R26 (Important — FIX): docs/specs/2026-08-17-user-account-types-design.md:5-6 still
reads "Companion plan: ... (the *how* — not yet written)". That plan is 91KB, exists, and is
literally the document this task was executed from. The same commit edited the adjacent
status line and left this one stale.

Task 9: fix round 1/5 (2 addressed, 0 open; commits 4001cb6..8ee827d)
  Re-reviewer independently confirmed 0028 never drops is_admin, no 0029 file exists, the
  corrected wording separates ORM-mapping from physical presence, and all THREE descriptions
  of the two-phase split (CLAUDE.md, design §2, design §8) now agree with each other and with
  0028's real contents. Also verified "Nothing in src/agent/ changes" by diffing the actual
  Task 1-9 commit range against src/agent/ — empty.
Task 9: complete (commits 19d855e..8ee827d, review clean)

## Task 10 — ruling on WHERE it lands

Ruling R27: implement Task 10 on a SEPARATE branch (feat/user-account-types-0029) branched
from the Task 1-9 head, leaving feat/user-account-types at alembic head 0028.

Why: the user asked for the entire plan implemented, and Task 10's work is writing +
gate-verifying a migration, which is not a deploy. But the plan's own Task 10 opens with
"Do not include this in the same deploy as Tasks 1-9" and the whole reason 0028 was split
from 0029 is to avoid a window where live code and applied schema disagree. If 0029 sat on
the feature branch, an operator running the documented `alembic upgrade head` would apply
BOTH migrations at once — dropping users.is_admin while the previous container is still
running code that does `select(User.is_admin)` (src/main.py) against it. That is precisely
the breakage the two-phase split exists to prevent, and CLAUDE.md warns that nothing
migrates this database automatically.

So the artifact gets built and verified, and the deploy ordering stays a deliberate act.
Merging feat/user-account-types-0029 is the user's call once /admin and /manager are
confirmed working in production on the 0028 code.
Cost if wrong: an extra branch to merge; nothing is left unimplemented.

Task 10 review: spec ✅, quality APPROVED, 0 Critical/Important, 2 Minor (comment staleness).
Reviewer independently confirmed: no raw-column reader anywhere in src/, templates/ or tests/
(all are hybrid accesses; no INSERT names the column); the round trip is genuinely ordered in
BOTH directions (0029.downgrade re-adds and backfills while user_role still exists, then
0028's downgrade runs; upward, 0028 reads/defaults is_admin before 0029 drops it);
nullable=False is paired with server_default false so ADD COLUMN survives a populated table;
0028's server_default=None restores 0001_initial's exact shape; the downgrade repopulates
from a NOT NULL user_role so admins survive rollback; ci.sh untouched (COV_MIN 60,
SRC_LINT_MAX 231, MIGRATION_FLOOR 0018); PLANNED_OBJECTS correctly gets no entries for a pure
DROP, matching 0026's precedent; postflight derives DEFAULT_TARGET from preflight so needed
no edit.
Task 10: minor (deferred): preflight.py:100-103 "All that remains is..." enumeration stops at
  0028 and omits 0029.
Task 10: minor (deferred): test_migration_checks.py:237-248 docstring/name narrate only the
  0028 bump though the assertion covers 0026/0027/0028.
Task 10: complete (commit 06e9325 on feat/user-account-types-0029, review clean)

ALL 10 TASKS COMPLETE.

## FINAL WHOLE-BRANCH AUDIT (two independent reviewers, disjoint scopes)

Security audit: no Critical; all 5 authorization invariants hold, 3 structurally.
Correctness audit: refactor proved clean MECHANICALLY (extracted bodies diffed, every
{% include %} textually re-inlined — all three templates identical; both None guards and
.desc().nullslast() intact). Migrations correct in both directions. "Strongest thing looked
for and not found: any behavioural change in the moved code."

Findings entering ONE fix wave:

F-A (Important, security) — CONTROLLER-VERIFIED. "Strictly read-only" was scoped to the
/manager router only. Four PI-write endpoints remain gated on plain get_current_user:
onboarding.py:112 (POST /onboarding/save-profile), onboarding.py:220 (POST /onboarding/retry
— F8's POST twin; Task 7 fixed only the GET self-heal), profile.py:198 (POST /profile/refresh)
and agent_page.py:409 (POST /agent/request). I confirmed each dependency myself, and that
/agent/request gates only on `onboarding_complete and profile`. So a manager can POST
save-profile (setting onboarding_complete=True and creating a ResearcherProfile) and then
POST /agent/request to obtain an AgentRegistry row — their own lab bot. That directly
violates D7 (roles mutually exclusive, "no lab of their own"). Spec §5's list was incomplete;
no test covers any of it.

F-B (Important, correctness) — CONTROLLER-VERIFIED. Spec §8's deploy sequence is backwards:
`up -d --build` starts the new code (which maps users.user_role) BEFORE
`alembic upgrade head` creates the column, so every select(User) raises UndefinedColumn in
between. My "no window in which live code and applied schema disagree" claim holds only for
old-code/new-schema. Also, alembic cannot be exec'd in the OLD container — 0028 is baked into
the new image only — so the fix is build, then a one-off `run --rm` migration from the new
image, then `up -d`.

F-C (Important, correctness) — the empty-branch trap that R18 and R20 already closed twice is
open a third time: _run_detail_body.html's four tables sit behind {% if %}, and the only
manager request reaching it seeds a bare SimulationRun with no messages and asserts nothing
on the body.

F-D (Important, security) — src/main.py's DUPLICATE impersonation gate is untested;
test_manager_views.py:224 asserts only status codes, so deleting `if is_admin:` there leaves
every test green. Worse, spec §7 explicitly claims that test exercises main.py:50-59. It does
not. A false coverage claim in the spec is worse than a missing test.

F-E (was R24(a) — MY RULING WAS WRONG, now fixing). I recorded that counting admins without
filtering access_status "makes the guard MORE conservative, which is the safe direction."
That is inverted. A larger count makes `<= 1` fire LESS often, so demotion is EASIER.
Concrete counterexample from the auditor: admins X(denied) + Y(allowed) -> count 2 -> Y is
demotable -> zero loginable admins remain. Fixing the count rather than the note.
R24(b) (the unserialized count-then-write) was re-examined and STANDS: the single-actor path
provably cannot reach zero, so it needs two admins cross-demoting concurrently.

F-F (cheap minors folded in): vacuous test_user_roles.py:94; admin-with-incomplete-onboarding
locked out of /profile while the nav still offers it (both auditors flagged; root cause is a
contradiction inside my spec §5); CLAUDE.md past tense for an unapplied migration;
manager/activity.html needs a "keep in sync" comment (verified byte-identical to its twin, so
drift would be invisible).

Not fixed, deliberately: 21 other deferred minors triaged as ship by both auditors; two stale
claims in older specs/ docs predating this work.
