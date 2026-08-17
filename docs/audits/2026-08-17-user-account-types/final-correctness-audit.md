# Final correctness / refactor-fidelity / deploy-safety audit

**Range:** `b1de2ec..06e9325` (18 commits, 45 files, +6099/−966)
**Scope:** refactor fidelity, migration safety, deploy sequencing, test quality,
the route-enumeration guarantee, and plan/ledger defects. **Not** the
authorization boundary or data exposure — a second reviewer owns those.
**Method:** whole-diff read plus mechanical fidelity proofs (below). No
subagents. No `docker`, no `git stash`/`clean`, no `ci.sh` re-run.

---

## Verdict

**Ship with two fixes.** The refactor is clean — I could not find a single
behavioural delta in the moved code, and I looked hard (see "What I failed to
find"). The migrations are correct in both directions. The two real problems are
outside the moved code: the documented deploy order is backwards for an
additive-first migration, and one instance of the empty-branch test gap that was
found and closed twice was left open a third time.

---

## Strengths (specific, verified)

- **The Python extraction is exact.** I normalised (strip, drop blank/comment
  lines) the six removed bodies from `b1de2ec:src/routers/admin.py` and diffed
  them against `src/services/directory.py`. Every difference is an intended one:
  the `roles=` filter, `raise HTTPException` → `return None`, and the
  `TemplateResponse` → `dict` returns. **No dropped clause, no changed sort, no
  lost guard.** Both production 500 regressions are intact and unmoved:
  `directory.py:432-441` (poster-included `None` guard before `sorted()`),
  `directory.py:561-562` (channel-stats guard), `directory.py:194-197`
  (`.desc().nullslast()` with the comment explaining why a bare `.desc()` would
  float unscored rows to the top of the triage queue).
- **The markup extraction is provably exact.** I textually inlined each
  `{% include %}` back into its wrapper and diffed against the pre-branch
  template with whitespace and Jinja comments normalised out:
  `admin/assessments.html`, `admin/discussions.html` and
  `admin/activity_detail.html` all come back **IDENTICAL**. The uniform 4-space
  dedent in `_run_detail_body.html` changed no `{% if %}` scope.
- `manager/activity.html` is byte-identical to `admin/activity.html` modulo the
  `/admin/`→`/manager/` link and the title; `manager/assessments.html` differs
  from its twin only by dropping the `--fresh` sentence. No accidental divergence.
- Zero absolute `/admin/` or `/manager/` URLs in all three shared partials
  (verified by grep), which is what makes the reuse safe.
- The `is_admin` hybrid is genuinely read-only and genuinely SQL-compilable, and
  `tests/unit/test_user_roles.py:68-80` pins the *literal* predicate
  (`users.user_role = 'admin'` present, `manager` absent) rather than the
  substring form the plan originally specified — R6 was the right call.
- `scripts/migrate/postflight.py` is unaffected by either migration:
  `users.is_admin` is not in `EXPECTED_COLUMNS`, and `remove_column` is in
  `DRIFT_IGNORED_OPS` (postflight.py:216-218), so the orphaned column between
  0028 and 0029 cannot fail the guarded path. R5 was correct.
- The `tests/unit/test_reachability.py` gate covers the new router and templates
  automatically, and no allowlist entry was added for any `/manager` route — so
  all seven are genuinely linked from rendered markup.

---

## Issues

### Important

**I1. The documented deploy sequence is backwards, and the spec's zero-window
claim is false in the direction the sequence actually uses.**
`docs/specs/2026-08-17-user-account-types-design.md:436-443`

```bash
$DC up -d --build blackbird-app worker             # new code goes live
$DC exec -T blackbird-app alembic upgrade head     # THEN the column is created
```

`up -d --build` recreates and starts `blackbird-app` and `worker` on the new
image. That code maps `users.user_role`; the column does not exist until the
next command runs. In the interval **every `select(User)` raises
`UndefinedColumn`** — ORCID login (`auth.py:210`), `/profile`, `/admin/*`,
`/manager/*`, the badge middleware (`main.py:53`) and the worker's job loop.
The spec asserts the opposite three lines earlier (design:429-432, echoed in
`CLAUDE.md:238-241`): "there is no window in which live code and applied schema
disagree." That is true for *old code against new schema* — which is the
direction the two-phase split was designed for, and the direction this command
order does not use.

You cannot fix it with `exec` on the old container: `alembic/versions/` is baked
into the image, so `0028_add_user_role.py` does not exist there. The fix is to
build without recreating and migrate from a one-off container of the new image:

```bash
$DC build blackbird-app worker
$DC run --rm --name blackbird-migrate blackbird-app alembic upgrade head
$DC up -d blackbird-app worker
$DC exec -T blackbird-app alembic current
```

(`blackbird-app` pins `container_name`, hence the explicit `--name`, exactly as
`CLAUDE.md` already does for `blackbird-agent-run`.) Note this generalises: the
same inversion sits in `CLAUDE.md`'s house sequence, so it is inherited, not
invented here — but this is the first branch whose spec explicitly promises the
property the order breaks.

**I2. The empty-branch test gap was closed for assessments and discussions and
left open for run detail — the third instance of the same class.**
`tests/integration/test_manager_views.py:369-374`, `templates/admin/_run_detail_body.html`

R18 (Task 5) and R20 (Task 6) were both opened because a manager wrapper that
dropped a key from its `**view` splat would render a clean empty branch and pass
every test. `manager_activity_detail` (`src/routers/manager.py:190-206`) splats
identically, and `_run_detail_body.html`'s four tables all sit behind
`{% if agent_stats %}` / `{% if channel_stats %}` / `{% if channels %}` /
`{% if messages %}` (lines 15, 39, 63, 92). The **only** manager-side request
that reaches that template is the reachability sweep at
`test_manager_views.py:78-94`, which seeds a bare `SimulationRun` with no
messages and no channels — so every one of those four branches renders empty,
and nothing asserts on the response body at all. There is also no
`"/admin/" not in body` assertion for a *populated* manager run-detail page,
which is the assertion that caught the shared-partial link risk on the other two
surfaces.

Fix (~10 lines, mirrors the two existing ones): seed the run + two
`factories.make_agent_message` rows (one with `agent_id=None`, to reuse the
production NULL case), request `/manager/activity/{run.id}`, and assert the
agent row, the channel row and `"/admin/" not in body`.

### Minor

**M1. An admin with `onboarding_complete=False` is now permanently locked out of
`/profile`, while the nav still offers it.** `src/routers/onboarding.py:57-58`,
`templates/base.html:52`
`/profile` → `profile.py:47` → `/onboarding` → the new non-PI bounce →
`/manager/pis`; and the self-heal at `onboarding.py:79-84` is now gated on
`user_role == 'pi'`, so no `generate_profile` job ever fires for them either.
Meanwhile `base.html:52` still renders **My Profile** for them (`user_role ==
'pi' or is_admin`). The ledger recorded this as "bites nobody today", which was
true of *migrated* admins — but it is reachable through an ordinary bootstrap:
promote a seeded, never-logged-in PI stub (`onboarding_complete=False`) to admin
via `role:set` or `/admin/users/{id}`. Before this branch that admin could
complete onboarding normally.
This is a **spec defect, not an implementation error**: §5 says "gate the
`/onboarding` redirect on `user_role == 'pi'` … non-PI staff skip onboarding
entirely" *and* "gate My Profile on `user_role == 'pi' or is_admin`" — the two
lines contradict each other. Cleanest fix: bounce on `is_manager` rather than
`!= USER_ROLE_PI` in both places, restoring the admin path unchanged.

**M2. D7 ("no bot for a manager") is enforced only by hiding a nav link.**
`src/routers/agent_page.py:409` — `POST /agent/request` carries
`Depends(get_current_user)` only, so a manager can self-request a PI bot by URL;
`POST /onboarding/save-profile` is likewise ungated on role. Spec §5 asks only
for nav gating, so the code matches the plan — the plan is what's incomplete.
Recoverable (the request lands as a `pending` `AgentRegistry` row an admin sees),
so Minor. May overlap the other reviewer's scope.

**M3. `0028`'s downgrade silently demotes managers to PI on a down/up cycle, and
nothing says so.** `alembic/versions/0028_add_user_role.py:47`
`UPDATE users SET is_admin = (user_role = 'admin')` preserves admins and loses
managers; a later re-upgrade restores them at `server_default 'pi'`. Combined
with M1's neighbourhood, such a user then has `onboarding_complete=False` and a
`pi` role, so the next login sends them to `/onboarding` and fires a
`generate_profile` job against an account with no relevant publications — which
is exactly F8, the thing the design set out to prevent. Not reachable via
`ci.sh`'s round trip (no manager rows exist there); real only on a production
downgrade. One sentence in §8 and in 0028's docstring is enough.

**M4. `CLAUDE.md:238-241` describes an undeployed migration in the past tense** —
"the already-running container **kept** working against the new schema with no
window where live code and applied schema disagreed". 0028 has not been applied
to production. R25 correctly fixed the "no `is_admin` column" claim; this
adjacent tense is the residue.

**M5. Vacuous assertion: `tests/unit/test_user_roles.py:94-96`.**
`assert u.user_role is None or u.user_role == USER_ROLE_PI` cannot fail —
SQLAlchemy's `default=` is INSERT-time, so a pre-flush instance's attribute is
always `None`. Already logged as a Task 1 deferred minor; the genuine coverage
is `test_manager_access.py:32-36`, which flushes. Delete the unit one.

**M6. Weak assertion: `tests/unit/test_user_roles.py:83-91`.** It asserts
`'manager'` and `'admin'` appear in the compiled `IN` clause but never that
`'pi'` is absent, so a widened `is_staff` covering all three roles would pass.
(The DB-level `test_is_staff_filters_admin_and_manager_only` does catch it, so
this is redundancy, not a hole.) Add `assert "'pi'" not in sql` to match the
`is_admin` test's discipline.

**M7. Static-markup-satisfiable assertion:
`tests/integration/test_manager_onboarding.py:79`.** `assert "Manager" in body`
against `/manager/pis` is satisfied by `<title>Manager — PIs — CoPI</title>`
alone and does not prove the nav link rendered. Harmless — the paired
`test_pi_nav_is_unchanged` negative at line 87 is the one with teeth — but it is
the pattern the branch has been purging. Scope it to `href="/manager"`.

**M8. `institution_filter` is accepted and applied but never surfaced.**
`src/routers/manager.py:69,81` passes it to `list_pi_directory`, but
`manager/pis.html` renders no input for it and `applyFilter()` (line 125-132)
rebuilds the query string from only `status_filter` and `claimed_filter` — so a
URL-supplied institution filter is silently dropped on the next filter change.
Cosmetic; matches the deferred Task 4 minor.

---

## Migration safety — both directions, verified

| Check | Result |
|---|---|
| `0028.upgrade` order (add → backfill → default → constraint) | correct; the backfill reads `is_admin` while it is still present |
| `0028` fixes F14 | yes — `alter_column(server_default=sa.text("false"))` leaves the unmapped `NOT NULL` column insertable |
| `0028.downgrade` data-preserving | yes for admins; **not** for managers (M3) |
| `alter_column(..., server_default=None)` really drops the default | yes — confirmed against the installed alembic 1.19.0: the parameter's sentinel is `False`, so `None` means DROP DEFAULT, restoring `0001_initial`'s exact shape |
| `0029.upgrade` / `.downgrade` ordering | correct in both directions: `0029.downgrade` re-adds and backfills `is_admin` while `user_role` still exists, and only then does `0028.downgrade` run |
| `0029`'s re-added column survives a populated table | yes — `nullable=False` paired with `server_default=text("false")` |
| Old code (pre-branch) against schema 0028 | works — `is_admin` present and defaulted, `user_role` ignored, INSERTs fill it from the server default |
| New code against schema 0027 | **broken** — this is I1 |
| Old code against schema 0029 | broken by design; `0029`'s docstring says so and names `alembic downgrade 0028` as the way back. Correct. |
| Single head / round trip | one head per branch (0028 / 0029); `ci.sh` exercises both downgrades on every push (`MIGRATION_FLOOR=0018`) |
| `preflight.SUPPORTED_START_REVISIONS` | R7's widening to include 0026/0027/0028 is right and is pinned by a new regression test at `test_migration_checks.py:236-248` |
| `PLANNED_OBJECTS` | correct: two entries for 0028, none for 0029 (a pure DROP cannot collide), matching 0026's precedent |

**Does the two-phase split achieve its stated purpose?** Yes for the direction it
was designed for — old code keeps running against 0028 — and that property is
real, not asserted. R27 (isolating 0029 on its own branch) is the correct call
for the same reason. The purpose is defeated only by the §8 command order (I1),
which is a documentation fix, not a design one.

---

## The route-enumeration guarantee — it holds

`_manager_get_paths()` (`test_manager_views.py:30-50`) enumerates
`manager_router.router.routes` live, prefixes `/manager`, and substitutes each
`{param}` from a caller-supplied map (fresh UUID otherwise). I traced every way a
future route could slip through:

- **Un-gated new route?** Impossible. The gate is
  `APIRouter(dependencies=[Depends(get_staff_user)])` (`manager.py:39`), which
  resolves before any handler body, so the deny sweep's `assert == 403` fires
  regardless of what the handler would have returned.
- **404-slip?** No. A route that exists always 403s for a PI before routing into
  the body. A route whose param the test cannot fill 404s in the *allowed* sweep,
  where `assert status in (200, 302)` turns that into a **red test** — the safe
  direction.
- **Typed converter (`{id:int}`)?** `_PARAM_RE = r"\{(\w+)\}"` will not match it,
  the literal stays in the URL, Starlette 404s, and the deny sweep fails red.
- **Mutating route?** `test_manager_router_exposes_no_mutating_routes` asserts
  `methods == {"GET"}` over the whole router.
- **The one real hole:** the guarantee is scoped to objects on
  `manager.router` that expose `.methods` (both sweeps use
  `getattr(route, "methods", ())`). A second `APIRouter` mounted at `/manager` in
  `main.py`, or a `WebSocketRoute`, is invisible to all three tests. Worth one
  line of docstring; not worth code.

The `/admin` denial sweep is weaker by construction — it skips every non-GET
route and every path containing `{` (`test_manager_views.py:205`), with
`checked >= 8` as the floor. That is stated honestly in the test and is not a
regression.

---

## Test quality — guilty-until-proven-useful pass

**Genuinely load-bearing** (I checked each could fail): the compiled-SQL
predicate tests (`test_user_roles.py:68-91`); the populated-assessments row
(`test_manager_views.py:259-341`) — `_band_label` and `_gating_state_for` *raise*
on no match and `_score_cell` returns `""` against a non-empty expectation, and
all three key on classes (`band-label`, `gating-row`, `score-<key>`) that exist
nowhere outside the partial's row markup; the populated-thread test
(`:377-459`) — `data-markdown="A **markdown** summary of the proposal."` is a
fixture string unique to that test and can only reach the page through the
partial; the `?export=true`-with-zero-runs regression
(`test_auth_and_admin_routes.py:205-224`); the `admin:revoke`-does-not-demote-a-
manager regression (`test_cli.py:417-437`); the `list-users` cell test, which now
splits on the box character and asserts the last two cells positionally rather
than counting Yes/No.

**Found weak:** M2 (empty-branch gap for run detail — the only one I rate
Important), M5, M6, M7. Also noted but not filed: `test_pi_directory_has_no_admin_controls`'s
`"/delete" not in body` is trivially true (no `/delete` link exists in
`admin/users.html` either) — its sibling `"impersonate" not in body.lower()` is
the assertion doing the work.

**"Would still pass if the feature were deleted"** — I found none beyond M5.

---

## Ledger review: rulings I disagree with

Of the 27 rulings, **26 hold on inspection.** Spot-verified independently: R6
(the escalation formulation really does contain the substring `user_role`), R9
(Jinja's default `Undefined` really does render nothing when iterated, so the
9-key normalisation is byte-identical output), R12 (the export reordering is a
real, if narrow, behaviour delta and the regression test has teeth), R5
(postflight's `EXPECTED_COLUMNS` contains no `users` entry, so extending
`VERIFIED_REVISIONS` would have asserted coverage that does not exist), R14 (the
B008 arithmetic — the singleton idiom is what the rule's own message recommends),
R19 (line 190's `{% endif %}` closes an `{% if %}` opened in the wrapper at line
42; moving it would be a `TemplateSyntaxError`), R24 (both deferrals are in the
safe direction).

**The one I'd re-grade: Task 7's "KNOWN EDGE CASE".** It was recorded as "bites
nobody today" and left. That is understated — the stub-promotion path in M1 makes
it reachable through an ordinary bootstrap, and it is a *regression* (the admin
could complete onboarding before this branch), not merely a missing feature. The
underlying cause is an internal contradiction in spec §5, so it is a plan defect
the ledger absorbed as an implementation edge case. Still Minor; still shippable.

---

## Triage of the in-scope deferred Minors

**Fix before merge (2):**
- **Task 6 — the empty-branch gap, third instance** (I2). Not in the ledger's
  deferred list, because it was never noticed; it is the same defect R18 and R20
  each ruled must-fix.
- **Task 1 — `test_default_role_is_pi` passes either way** (M5). One-line
  deletion. This branch has explicitly purged vacuous tests twice; leaving a
  known one is inconsistent.

**Ship (all others), with reasons:**
- *0028's CHECK list is a SQL literal untied to `VALID_USER_ROLES`* — adding a
  fourth role requires a migration anyway, so the drift window is the migration
  itself.
- *`user.py` comment restates its own definition; 3 lines >100 chars* — E501 is
  not gated.
- *No test pins `User(is_admin=...)`* — the declarative constructor `setattr`s,
  so it raises via the read-only hybrid, which `test_is_admin_is_read_only`
  already covers.
- *Unused `monkeypatch` param (`test_manager_access.py:59`)* — cosmetic.
- *No unauthenticated-302 test through `get_staff_user`* — covered on a real
  route by `test_unauthenticated_manager_root_redirects_to_login`.
- *`directory.py:286` hardcodes `agent_filter=[]` on the empty path* — the filter
  form is not rendered on that branch, so the value is unreachable.
- *In-function `ProposalReview` import* — moved verbatim; changing it is a
  behaviour risk for zero gain.
- *`test_directory_service.py:22` assumes no other `User` rows* — `db_session` is
  per-test and rolled back.
- *`manager.py:88,101` unused logger; `:122` `response_class` on a redirect* —
  cosmetic / OpenAPI-only.
- *`manager.py:132` `institution_filter` unused by the template* — see M8;
  cosmetic, and fixing it means adding UI, not deleting code.
- *`test_manager_views.py:693` 200-alone does not prove non-elevation* — the
  paired `/admin/users` 403 does.
- *Task 6: bare `SimulationRun()`; partials living under `templates/admin/`;
  module-level `Query(default=[])`* — all verified harmless; the partial location
  is entangled with the reachability gate's template rules and is not worth
  churning.
- *`manager/activity.html` duplicates its admin twin* — I confirmed they are
  byte-identical apart from the title, so drift would be invisible. **Add the
  "keep in sync" comment the ledger proposes**; that is the whole fix.
- *`base.html` duplicate `user_role == 'pi' or is_admin` guard; brief metadata
  mismatch* — cosmetic.
- *Task 8: last-admin count ignores `access_status`* — makes the guard strictly
  more conservative.
- *Task 8: count-then-write race* — requires two admins cross-demoting
  simultaneously (the self-change guard blocks the single-actor path), and
  `role:set` is the documented recovery. Fixing it properly needs row locking,
  which is out of scope.
- *Task 10: `preflight.py` comment stops at 0028; `test_migration_checks.py`
  docstring narrates only the 0028 bump* — comment staleness on assertions that
  are themselves correct.

---

## What I looked for hardest and failed to find

**A behavioural delta anywhere in the ~530 moved Python lines or the ~440 moved
markup lines.** That was the highest-value finding available — this branch's one
hard constraint was "no behaviour change", and two of the affected pages have
500'd in production before. I did not settle for reading: I extracted
`b1de2ec:src/routers/admin.py`, `06e9325:src/services/directory.py` and the three
pre/post template pairs to disk, normalised whitespace and comments, textually
inlined every `{% include %}`, and diffed. **All three templates came back
identical; every Python difference is an intended signature or return-type
change.** The `set.add(None)` guards and the `.desc().nullslast()` survived with
their explanatory comments attached. I also checked the two specific hazards you
named — a re-indentation changing a conditional's scope (the run-detail dedent is
uniform and the if/endif nesting is preserved) and a sort silently changing
(`ORDER BY` clauses are character-identical). There is no finding there.

The second thing I hunted and did not find: a way for a newly added `/manager`
route to reach production un-gated and untested. The router-level dependency plus
the two live-router sweeps genuinely close it, and every failure mode I could
construct turns the suite red rather than green.
