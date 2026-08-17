# Final adversarial review — security and authorization half

**Branch:** `feat/user-account-types` (`b1de2ec..06e9325`, 18 commits, 45 files)
**Scope:** authorization model and data exposure only. Correctness, refactor fidelity,
migration/deploy safety and test quality are a second reviewer's.
**Method:** read-only. HEAD in the working tree is `8ee827d`; `06e9325` touches only
`alembic/versions/0029_drop_is_admin.py`, `scripts/migrate/preflight.py` and two tests,
so every `src/` and `templates/` file read below is identical at both revisions
(verified via `git show --stat`). No `pytest` was run — `tests/conftest.py` starts a
testcontainers Postgres, which is a `docker` command, and this host runs two live
production deployments. Every claim below is therefore from reading the code, from a
live FastAPI dependency-graph walk (imports the app, resolves no database), or from
`git show` / `diff` against the base revision.

---

## Verdict

**No Critical findings. Two Important, five Minor. Ship with fixes.**

The five authorization invariants that matter most all hold, and three of them hold
*structurally* rather than by convention:

- `is_admin` is false for a manager by construction, in Python and in SQL, with only two
  write sites for `user_role` in the entire tree.
- Every `/admin` route kept its gate — proven by byte-diff, not by inspection.
- `/manager` is deny-by-default, GET-only, with no export and no `llm-calls`.

The strongest thing I looked for and failed to find was a manager→admin escalation path.
I attacked it five ways (hybrid expression form, migration backfill, role endpoint, CLI,
impersonation cookie in both of its duplicated implementations) and found nothing. The
two Important findings are both about the *edges* of the new role — a manager's residual
write access to PI-shaped endpoints outside `/manager`, and an untested duplicate of the
impersonation gate — not about the boundary itself.

---

## Invariant-by-invariant

### 1. A manager can never become an admin — **HOLDS**

Checked all four surfaces the brief names, plus two it did not.

**Hybrid expression form.** `src/models/user.py:88-115`. `is_admin` compiles to
`users.user_role = 'admin'` and `is_staff` to `user_role IN ('manager','admin')`; the two
are distinct symbols with no call site conflating them. `grep -rn "is_admin" src/
templates/` returns exactly four live readers — `src/main.py:53`, `src/dependencies.py:74`,
`src/dependencies.py:132`, and `templates/base.html:52,62,73,103` — and none of them is
spelled `is_staff`. The hybrid has no setter, so `user.is_admin = True` raises
`AttributeError` (pinned at `tests/unit/test_user_roles.py:61`).

**Impersonation, both copies.** `src/dependencies.py:74` gates the unsigned
`copi-impersonate` cookie on `session_user.is_admin`; `src/main.py:52-55` re-implements the
same check as SQL. Both resolve through the hybrid, so a manager fails both. The Python
side is pinned by `tests/integration/test_manager_views.py:224`. The SQL side is **not**
pinned — see Finding I2.

**Migration backfill.** `alembic/versions/0028_add_user_role.py:28-41`. The column lands
`NOT NULL DEFAULT 'pi'`, the backfill is `UPDATE users SET user_role = 'admin' WHERE
is_admin = true`, and a CHECK constraint pins the domain to the three literals. The
migration can produce `pi` or `admin` and nothing else — it cannot mint a manager, and it
cannot mint a value outside the enum.

**Role endpoint.** `src/routers/admin.py:164-214`. Membership check against
`VALID_USER_ROLES` precedes the SELECT; exact string match, so `'admin '`, `'Admin'` and
`'superuser'` all 400. Gate is `Depends(get_admin_user)`.

**CLI.** `src/cli.py:107-207`. `admin:grant` → `USER_ROLE_ADMIN`, `admin:revoke` →
`USER_ROLE_PI` only when the row is currently admin (so it can no longer silently strip a
manager — R8's fix, and the comment at `cli.py:155-161` explains why it still exits 0),
`role:set` unguarded by design. All three require a container shell.

**Two surfaces the brief did not name, checked anyway:**

- *Self-service.* `src/routers/auth.py:215-221`, `src/routers/invite.py` and
  `src/routers/agent_page.py` all create `User` rows; none passes `user_role`, so all take
  the `'pi'` default. `grep -rn "user_role" src/` shows exactly **two** write sites in the
  whole tree: `admin.py:208` and `cli.py:124/167/199`. There is no third.
- *Route shadowing.* `public.router` is included first (`main.py:141`) and has no
  single-segment catch-all that could match `/manager` or `/admin` ahead of the gated
  routers. Enumerated all 88 `APIRoute`s to confirm.

### 2. `/manager` is deny-by-default and read-only — **HOLDS**

Walked the live dependency graph rather than trusting the source. All seven routes,
including the bare prefix:

```
['GET'] /manager                       manager_root             gate=[get_staff_user, get_current_user]
['GET'] /manager/activity              manager_activity         gate=[get_staff_user, get_current_user]
['GET'] /manager/activity/{run_id}     manager_activity_detail  gate=[get_staff_user, get_current_user]
['GET'] /manager/assessments           manager_assessments      gate=[get_staff_user, get_current_user]
['GET'] /manager/discussions           manager_discussions      gate=[get_staff_user, get_current_user]
['GET'] /manager/pis                   manager_pis              gate=[get_staff_user, get_current_user]
['GET'] /manager/pis/{user_id}         manager_pi_detail        gate=[get_staff_user, get_current_user]
```

`{m for r in ... for m in r.methods} == {"GET"}` on FastAPI 0.141.1 — confirmed the
router-level `dependencies=[Depends(get_staff_user)]` (`manager.py:39`) really does
propagate onto the `""` route, which is the one a per-handler convention most often
misses. FastAPI's `APIRoute` does not add `HEAD`, so the set is genuinely `{GET}` and not
an artefact.

**No `llm-calls`.** No such route exists under `/manager`; the only link to it lives in
`templates/admin/activity_detail.html:20`, which `templates/manager/activity_detail.html`
does not copy. Both halves pinned at `test_manager_views.py:351`.

**No export.** `templates/manager/discussions.html` is byte-identical to
`templates/admin/discussions.html` apart from the `<title>` and the two removed
`name="export"` buttons (verified by normalised diff — a 4-line delta and nothing else).
`manager_discussions` does not declare an `export` parameter, and FastAPI silently drops
unknown query params, so `?export=true` is inert. `build_discussions_view`
(`src/services/directory.py:240`) deliberately stops before the export branch.

**The three shared partials are clean.** `templates/manager/*` includes
`admin/_run_detail_body.html`, `admin/_discussions_threads.html` and
`admin/_assessments_body.html`. `grep -E 'href|action|form|button|/admin/'` across all
three returns **zero** matches — they are pure table markup with no navigation and no
controls. That is the load-bearing fact for invariant 4, because those three files are the
only route by which an admin control could reach a manager page without appearing in a
`templates/manager/*` file.

### 3. A manager cannot enumerate staff accounts — **HOLDS**

`src/routers/manager.py:107-109`:

```python
detail = await load_user_detail(db, user_id)
if detail is None or detail["user"].user_role != USER_ROLE_PI:
    raise HTTPException(status_code=404, detail="PI not found")
```

Missing row and wrong-role row raise the *same* exception object with the *same* detail
string, so the two are indistinguishable in status, body and headers. Not an existence
oracle. (`load_user_detail` runs one query on the miss and two on the wrong-role hit, so
there is a sub-millisecond timing delta; over a network, against a page that also renders
a template on the 200 path, this is not exploitable and I am not counting it.)

The directory itself passes `roles=(USER_ROLE_PI,)` (`manager.py:82`), which becomes
`WHERE users.user_role IN ('pi')` at `directory.py:61-62`. `/admin/users` keeps
`roles=None` and is unchanged. Pinned at `test_manager_views.py:113`.

### 4. No manager control 403s on click — **HOLDS, with one inversion (Finding M2)**

`grep -E 'form|action=|button|<a ' templates/manager/*.html`: the only `<form>`s are three
`method="GET"` filter forms posting to their own manager routes; the only `<button>` is
that filter's submit. No impersonation widget, no Delete User, no Danger Zone, no
`/admin/` href anywhere. Pinned three ways at `test_manager_views.py:129,189,252` with
body-content assertions (the reachability sweeps cannot catch this, and the test comments
correctly say so).

The admin side got the same treatment: `templates/admin/user_detail.html:64-78` wraps the
new Role selector in `{% if target_user.id != current_user.id %}` and renders explanatory
text otherwise, so the self-change guard has no dead control in front of it either.

The inversion: the manager surface is *missing* a control it should have — the
impersonation banner. See M2.

### 5. The role endpoint — **HOLDS**

Gate `Depends(get_admin_user)` (verified in the live graph). Validation before the SELECT.
Self-change guard compares two `uuid.UUID` objects, so every string spelling of the same
UUID normalises. Last-admin guard fires on exactly the admin→non-admin transition and
counts before the write. Redirect on success; 400/404 otherwise.

I confirmed the guard-3-is-unreachable claim independently and it is correct, and I
confirmed the corollary the ledger did not state: **no single actor can reach zero
admins.** Demoting yourself is blocked by guard 1; demoting anyone else always leaves you
admin. Zero admins requires two distinct admins demoting each other concurrently. That is
the whole of the R24(b) race, and it is correctly characterised.

One escalation-adjacent case I checked and cleared: admin A impersonating admin B. `B` is
returned by `get_current_user`, so `current_user.id == B.id` and A can change *A's own*
role, sidestepping guard 1. That does not reach zero admins (B is still an admin, so the
count is ≥2 and B survives), and it requires admin rights to begin with. It does corrupt
the audit trail — see M1.

### 6. No admin route lost its gate — **HOLDS, proven mechanically**

Two independent proofs.

*Live graph:* all 37 `/admin` routes resolve `get_admin_user`, except
`POST /admin/impersonate/stop`, which resolves `get_current_user` — still the only one.

*Byte-diff of the gate declarations:* extracted every `@router.` decorator line and every
`Depends(get_admin_user)` / `Depends(get_current_user)` line from `admin.py` at `b1de2ec`
and at HEAD, in order, and diffed:

```
7a8,9
> @router.post("/users/{user_id}/role")
>     current_user: User = Depends(get_admin_user),
```

71 lines → 73 lines, one insertion, zero modifications, zero deletions. The six-handler
body extraction perturbed no decorator and no gate. This is the strongest single piece of
evidence in the review, because it rules out a silent gate change without depending on my
reading 1700 lines correctly.

### 7. Data exposure matches the spec — **HOLDS**

Confirmed present, per D3/D5: PI email (`templates/manager/pi_detail.html:19`), account and
engagement metadata (onboarding, claimed, joined, last login, job history with truncated
`last_error`), research profile, publications, assessments, discussion threads, run
activity. Confirmed **no** visibility filter anywhere in `build_discussions_view` — D5's
private-channel exposure is live and intentional, exactly as the spec records it.

Confirmed *absent*, i.e. nothing beyond the policy:
- No `llm_call_logs` reachable from `/manager` at all.
- `OpportunityAssessment.raw_verdict` is **not** rendered by
  `templates/admin/_assessments_body.html`; the fields it renders are
  band/company/confidence/created_at/derisking_milestones/funnel_stage/gating/rationale/
  recommendation/red_flags/scores/simulation_run_id/subject_agent_id/weighted_score.
- `build_run_detail`'s message list renders `message_length`, not message content
  (`_run_detail_body.html:117`).
- `get_agent_with_access` (`dependencies.py:92-125`) grants nothing to staff, so
  `/agent/{id}/conversations` and `/agent/{id}/thread/{ts}` stay closed to a manager. The
  manager sees the thread *index*, not the per-lab dashboards.

One nuance worth recording rather than fixing: `_assessments_body.html` renders
`a.rationale`, which *is* verbatim model prose, and the template renders `RUBRIC_WEIGHTS`
(dimension names and weights). D10's stated reason for excluding `llm-calls` — "full
system prompts (including the Blackbird rubric) and raw model output" — is therefore
slightly overstated, since a subset of both already reaches the manager through the
assessments view that §3 explicitly grants. The implementation matches §3; it is the
*rationale* in D10 that is imprecise, not the code.

---

## Findings

### Important

**I1 — A manager is read-only only inside `/manager`. Four PI-write endpoints remain open
to them, and one of them is F8 all over again.**

- `src/routers/onboarding.py:220-240` (`POST /onboarding/retry`) — gate is
  `get_current_user`, no role check. Enqueues `generate_profile` for the caller.
- `src/routers/profile.py:198-212` (`POST /profile/refresh`) — same, same job.
- `src/routers/onboarding.py:112` (`POST /onboarding/save-profile`) — writes a
  `ResearcherProfile` and sets `onboarding_complete = True` for a manager.
- `src/routers/agent_page.py:409-436` (`POST /agent/request`) — gated only on
  `onboarding_complete and profile`, both of which the previous bullet supplies. Creates an
  `AgentRegistry` row with `status='pending'` and a `{LastName}Bot` name, which then
  surfaces in `/admin/agents` awaiting approval.

F8 is the finding that "a manager account is dragged into PI onboarding, and it fires a
pipeline job". The fix landed on `onboarding.py:82-90`, the **GET** self-heal, and its test
(`test_manager_onboarding.py:26`) drives the **GET**. The POST twin that enqueues the
identical job was never gated and is never tested. `src/routers/agent_page.py:119`
(`GET /agent`) likewise still renders `agent/request.html` with a live request form for a
manager — F9's fix was nav-only (`base.html:62`), which hides the link but not the route.

Chained: this is how D7 ("no bot for a manager") is violated. And it is reachable without
crafting anything in the D9 bootstrap window the spec itself documents — a user who
completes onboarding as a PI in step 1-2 and is promoted in step 3 keeps
`onboarding_complete=True`, a profile, and any `AgentRegistry` row they already created;
`admin_set_user_role` does nothing to clean that up.

Impact is *not* privilege escalation — every one of these writes lands on the manager's own
row. It is (a) unbudgeted ORCID/PubMed/LLM work fired against a non-PI, (b) a manager
holding a lab bot, which D7 forbids, and (c) the spec's "cannot mutate anything" being
false as written.

This is a gap in the **spec**, not a coding slip: §5 enumerates `auth.py:295`,
`auth.py:300`, `onboarding.py:55`, `onboarding.py:75` and `base.html`, and the
implementation matches that list exactly. The list is incomplete.

*Fix:* add a `user_role != USER_ROLE_PI` bounce to `POST /onboarding/retry`,
`POST /onboarding/save-profile`, `POST /profile/refresh` and `POST /agent/request` (four
lines), or lift the check into a small `require_pi` dependency. Then pin each with a test
in `test_manager_onboarding.py` shaped like the existing
`test_manager_visiting_onboarding_enqueues_no_profile_job`. If you would rather ship as-is,
amend §5 and D7 to say the guarantee is scoped to the `/manager` router — but do not leave
the spec claiming "cannot mutate anything".

**I2 — The duplicated impersonation check in `src/main.py:50-55` has no test, and the
spec claims it does.**

`test_manager_views.py:224` (`test_a_hand_set_impersonate_cookie_is_ignored_for_a_manager`)
asserts two status codes: 200 on `/manager/pis`, 403 on `/admin/users`. Both are decided
entirely by `get_current_user` in `src/dependencies.py:74`. Delete `if is_admin:` at
`main.py:55` and both assertions still pass — the only observable difference is
`request.state.agent_badge_count`, which no test in the tree reads (`grep -rn
"agent_badge_count" tests/` → only `conftest.py` plumbing).

Spec §7 says that test exercises "`dependencies.py:74` **and** `main.py:50-59` from the
manager side". It exercises the first only.

`scripts/mutate_system.sh:226` carries mutant M8 for the `dependencies.py` copy of this
gate ("copi-impersonate is honoured for non-admins") and has no counterpart for the
`main.py` copy, so the mutation harness does not cover it either. A duplicated auth check
with a mutant on one copy and no test on the other is precisely the shape that rots.

Impact if it regresses is low — an integer count of another user's unreviewed proposals —
which is why this is Important and not Critical. But "the protection could be deleted and
every test stays green" is exactly the property I was asked to hunt for, and this is the
one place I found it.

*Fix:* one test that sets `copi-impersonate` for a non-admin and asserts the rendered badge
is the caller's, not the target's; and an M-number in `mutate_system.sh` for `main.py:55`.

### Minor

**M1 — `POST /admin/users/{user_id}/role` logs the impersonated identity, not the actor.**
`src/routers/admin.py:210-213` logs `current_user.name`, and under impersonation
`current_user` *is* the substituted user. An admin impersonating another admin can grant or
revoke admin rights, and the only record of it names someone who did not do it. This is the
privilege-granting endpoint and that log line is the entire audit trail (§10 notes there is
no audit log at all). `_real_admin` is already attached to the object at
`dependencies.py:84`. Same pre-existing pattern at `admin.py:160` for delete-user; fix both
or neither.

**M2 — The whole `/manager` surface renders with no impersonation banner and no exit.**
`src/routers/manager.py:47-57` does not set `impersonation_banner`, unlike
`profile.py:28`, `agent_page.py:107`, `onboarding.py:40` and `settings.py:45`. So an admin
impersonating a manager reads PI contact details and private-channel discussions with no
indicator that they are impersonating and no "Stop Impersonating" button — and because the
flag is undefined (falsy), `base.html:79` still renders the Manager nav link, so the
session looks entirely normal. `/manager` is the first staff surface reachable while
impersonating a non-admin, so this is newly reachable even though `admin.py:68-78` has the
same omission. Three lines, copied from `profile.py:22-28`.

**M3 — The ledger's justification for deferring the `access_status` gap is backwards.**
See triage below. The deferral is fine; the reason recorded for it is wrong, and a future
reader relying on it would conclude the guard is safer than it is.

**M4 — No mutants registered for any of the new gates.** `scripts/mutate_system.sh` is
untouched by this branch. The repo's own idiom for a gate like this is an M-number (M8
covers `dependencies.py:74`). Missing: `get_staff_user`'s `if not current_user.is_staff`
(`dependencies.py:150`), the router-level dependency (`manager.py:39`), the
`!= USER_ROLE_PI` 404 (`manager.py:108`), and the `roles=(USER_ROLE_PI,)` directory filter
(`manager.py:82`). I traced each by hand and each **would** be killed by an existing test —
`test_get_staff_user_gates_by_role`, `test_pi_is_denied_the_manager_surface`,
`test_pi_detail_404s_for_a_staff_account`, `test_directory_excludes_staff_accounts`
respectively — so this is enrolment, not exposure.

**M5 — Two stale security docs now assert things that are false.**
`specs/admin-dashboard.md:11` — "`is_admin` is set via CLI only — no self-service admin
promotion" — is contradicted by the new `POST /admin/users/{user_id}/role`.
`specs/data-model.md:21` still lists `is_admin` as a boolean column, which `0029` drops.
Both are security-describing documents; a false statement in one is worse than no
statement.

**M6 — The `ck_users_user_role` CHECK constraint has no test.**
`alembic/versions/0028_add_user_role.py:37-41` is the database-level last line of defence
on the role domain, and `tests/integration/test_db_contract.py` is the established home for
exactly this assertion (it already has ten `pytest.raises(IntegrityError)` blocks). A
`user_role='superuser'` insert should raise there. Today, if the constraint were dropped
from the migration, only the HTTP-level `VALID_USER_ROLES` check would remain and nothing
would fail. (The ledger already flags the related point that the constraint's SQL string
is untied to `VALID_USER_ROLES`; a contract test is the cheap way to bind them.)

### Informational — verified, no action

- `access_status` is checked only at login (`auth.py:267-273`). Nothing re-checks it per
  request, so denying a manager's access does not end their 30-day session; only a
  `user_role` change takes effect immediately (the role is re-read from the DB on every
  request through `get_current_user`). Pre-existing and app-wide, not introduced here.
- `SameSite=lax` on `copi-session` (`main.py:131`) is what stands in for CSRF tokens on the
  new role POST. Cross-site forged POSTs will not carry the session cookie. Same posture as
  the pre-existing delete-user endpoint; adequate, and worth not regressing.
- `/openapi.json` and `/docs` are unauthenticated and now advertise
  `POST /admin/users/{user_id}/role` and its `user_role` form field. Route metadata only,
  and every `/admin` route was already listed. Pre-existing posture.
- `get_staff_user` 403s an admin who is currently impersonating a PI. Correct, and the
  docstring says so.

---

## Triage of the in-scope deferred Minors

**R24(a) — the last-admin count ignores `access_status`. Ships, but the recorded reason is
wrong and should be corrected.**

The ledger says it "makes the guard MORE conservative (harder to demote), which is the safe
direction". That is inverted. Counting *every* admin row yields a *larger* count, so
`admin_count <= 1` is satisfied *less* often, so demotion is permitted *more* often.
Concretely: admins X (`access_status='denied'`) and Y (`allowed`). Y is the only admin who
can log in. The count is 2, the guard stands down, Y is demoted, and there are now zero
admins who can reach `/admin` — the exact lockout guard 3 exists to prevent. Filtering to
`allowed` shrinks the count and makes the guard fire *more* often, which is the safe
direction.

It ships because guard 1 makes the whole branch unreachable over HTTP today, and because
`role:set` is the documented recovery. But the one-line fix
(`.where(User.access_status == "allowed")` at `admin.py:200`) is strictly safer and costs
nothing, and if you keep the deferral you must fix the justification — a future engineer
relaxing guard 1 would read that note and conclude guard 3 has them covered.

**R24(b) — count-then-write is not serialized. Ruling stands. Ships.**

I verified the claim independently and it is correct, including the part the ledger left
implicit: the single-actor path cannot reach zero, because guard 1 blocks self-demotion and
demoting anyone else always leaves the actor admin. Zero admins genuinely requires two
distinct admins cross-demoting inside the same READ COMMITTED window. Combined with an
unguarded `role:set` recovery, the residual risk does not justify a `SELECT ... FOR UPDATE`
or a partial unique index in this branch. Leave it; the ledger's reasoning is sound as
written.

**Other deferrals falling in my scope:**

- *Task 2, "no test pins the unauthenticated 302 through `get_staff_user`"* — **ships.**
  `test_manager_views.py:60` covers it on a real route, as the ledger says.
- *Task 4, `manager.py` minors* (unused logger; `institution_filter` accepted but absent
  from the template context; `response_class=HTMLResponse` on the redirect handler;
  module-level `Query(default=[])`) — **all ship.** None has authorization or exposure
  bearing. The `institution_filter` one fails *open* in the harmless direction: it shows
  more PI rows than asked for, and every row is a PI either way.
- *Task 1, "CHECK list in 0028 is a SQL string literal untied to `VALID_USER_ROLES`"* —
  **ships**, but see M6: pin it with a `test_db_contract.py` case rather than leaving the
  DB-level defence entirely unexercised.
- *Task 1, "no test pins the `User(is_admin=...)` constructor form"* — **ships.**
  `test_user_roles.py:61` pins the assignment form, which is the one that matters
  (`AttributeError` on write is what makes `is_admin` derived rather than settable).
