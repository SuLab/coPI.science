# Scoped re-review — final fix wave (`8ee827d..e2696cc`), `feat/user-account-types`

Read-only review. No working-tree mutation, no `docker`, no subagents, suite not re-run
(only `ruff check tests/` and `ruff check src`, both cheap and named in the constraints).

## Finding Verdicts

### F-A — a manager could obtain a lab bot — **ADDRESSED**

- `src/dependencies.py:136-158` — new `get_pi_user`, 403 on `current_user.is_manager`.
- All four endpoints verified in source, not just the diff:
  `src/routers/onboarding.py:139` (`save_profile`), `src/routers/onboarding.py:247`
  (`retry_pipeline`), `src/routers/profile.py:202` (`profile_refresh`),
  `src/routers/agent_page.py:413` (`request_agent`).
- Escalation chain broken end to end, verified independently of the report:
  - `grep -rn "onboarding_complete" src/ --include=*.py` → the **only** assignment of
    `True` in all of `src/` is `src/routers/onboarding.py:225`, inside the now-gated
    handler. `src/cli.py:235` only reads it.
  - `grep -rn "AgentRegistry(" src/` → the **only** constructor in `src/` is
    `src/routers/agent_page.py:434`, inside the now-gated `request_agent`.
  - So both preconditions for a bot are behind `get_pi_user`. The remaining
    `/agent/request` body check (`agent_page.py:423`) is correctly demoted to a
    readiness check in its own docstring.
- Test evidence is real, not status-code theatre: `tests/integration/test_pi_only_writes.py`
  pairs every 403 with a four-field state snapshot (`onboarding_complete`, job count,
  `AgentRegistry` count, `research_summary`) *and* with PI/admin controls that must move
  that same snapshot — so a dead endpoint cannot score as a protected one. `factories.make_user`
  defaults (`onboarding_complete=True`, an email, `access_status='allowed'`,
  `tests/factories.py:29-42`) are what make the PI/admin controls actually reach 302 on all
  four paths, so the controls are not vacuous.

**Judgement on the `is_manager` predicate (adversarial).** Behaviourally correct today and
the reasoning for not using `== 'pi'` is right: `templates/base.html:52,62` gate My Profile
and My Agent on `user_role == 'pi' or is_admin`, so a `== 'pi'` dependency would 403 every
admin on links the app itself renders for them. `VALID_USER_ROLES`
(`src/models/user.py:19`) is exactly `(pi, manager, admin)` and migration `0028` adds a
CHECK constraint pinning that set, so "not a manager" == "pi or admin" *today*.

The defect is that it is a **denylist in a file whose two neighbours are allowlists**.
`get_admin_user` and `get_staff_user` both fail closed; `get_pi_user` fails **open**. Add a
fourth role later — `reviewer`, `observer`, an SSO-provisioned `guest` — and it silently
inherits every PI write surface, including the sole `AgentRegistry` constructor. That is the
identical shape of the bug just fixed ("a role that is not a PI reached PI writes"), and
nothing in the change process forces a revisit: adding a role touches
`src/models/user.py` and a migration, neither of which references `get_pi_user`.

The name compounds it: `get_pi_user` returns a user who is **not guaranteed to be a PI**, so
a caller reading the signature may assume `current_user.profile` / lab semantics that only
hold for two of the three roles. The docstring states the real rule in its second paragraph
and `test_an_admin_keeps_every_pi_write` pins it, which is why this is not a blocker.

Recommended (non-blocking, one line, behaviour-identical today):

```python
if current_user.user_role not in (USER_ROLE_PI, USER_ROLE_ADMIN):
```

plus either renaming to `get_non_manager_user` / `deny_manager`, or keeping the name and
adding a `VALID_USER_ROLES`-length assertion in `tests/unit/test_user_roles.py` so a fourth
role trips a test. Severity: **Minor** (latent, not exploitable on the current role set).

One more predicate check: impersonation composes correctly. `get_pi_user` depends on
`get_current_user`, which returns the *substituted* user, so an admin impersonating a
manager is correctly refused these writes.

### F-B — documented deploy order was backwards — **ADDRESSED, with two caveats**

- Spec §8 now states both mismatch directions and which is safe, and the safety claim is
  verified against the migration itself: `alembic/versions/0028_add_user_role.py:28-38` is
  `add_column` + `UPDATE` + `alter_column ... server_default=false` + CHECK — additive, and
  the `is_admin` server default is really there, so old-code/new-schema is genuinely safe.
- §2's overclaim is now a cross-reference to §8, and `CLAUDE.md`'s Account Types section
  carries the same corrected block. They agree, command for command.
- The corrected command form works against this compose file:
  - `Dockerfile` has **no `ENTRYPOINT`**, only `CMD`, so `run --rm blackbird-app alembic
    upgrade head` replaces the command cleanly.
  - `alembic>=1.13.0` is a **runtime** dependency (`pyproject.toml:21`) and there is no
    `.dockerignore`, so `alembic/` + `alembic.ini` are in the image.
  - `run` inherits `env_file: .env` and the service's `DATABASE_URL`/`SECRET_KEY`, starts
    `depends_on: postgres (service_healthy)`, publishes no ports, and does not replace the
    running service — all as the note claims.

Caveat 1 (**Minor**, worth one line in the doc): the deployed compose file pins
`container_name: copi-blackbird-app-1` on `blackbird-app`. Compose v2 overrides
`ContainerName` for one-off containers (the `%[1]s%[4]s%[2]s%[4]srun%[4]s%[3]s` run-name
format is present in the installed plugin binary), so this should generate
`copi-blackbird-blackbird-app-run-<slug>` and not collide — but I could not confirm the
ordering of that override without running docker, which is forbidden here. Passing an
explicit `--name blackbird-migrate` costs nothing and removes the question. If it *did*
collide, the failure is loud and lands in the safe state (old code + old schema).

Caveat 2 (pre-existing, out of scope): the **committed** `docker-compose.prod.yml` has no
`blackbird-app` service at all — it is `app`. The `blackbird-app` rename lives only in the
uncommitted working-tree modification (not the implementer's). The documented commands
therefore match the deployed host and `CLAUDE.md`'s existing convention, but not a fresh
clone of this branch.

Caveat 3 (**Minor**): a *third* copy of the retired claim survives, in the migration's own
docstring — `alembic/versions/0028_add_user_role.py:8-10` still says "there is no window
where live code and applied schema disagree". The file is outside the fix diff; the two
places the finding named (spec §8, `CLAUDE.md`) are both fixed.

### F-C — third empty-branch test gap — **ADDRESSED**

`tests/integration/test_manager_views.py::test_manager_activity_detail_renders_the_populated_shared_partial`
seeds an `AgentChannel` + `AgentMessage` on the run, so all four `{% if %}` guards in
`templates/admin/_run_detail_body.html` open (lines 28, 53, 78, 105). Checked each assertion
against the template:

- `"partial-proof-channel"` — data-only, printed by the guarded channel rows. Discriminating.
- `"PartialbotBot"` — emitted by `_run_detail_body.html:42`
  (`{{ agent_id | capitalize }}Bot`, inside `{% if agent_stats %}`). Discriminating.
  (Nit: the test docstring attributes it to line 112's `{{ msg.agent_id }}Bot`, which would
  render `partialbotBot` — the assertion is right, the comment names the wrong line.)
- `"Message Timeline (1)"` — `_run_detail_body.html:107`, count from `messages | length`.
  Discriminating, and the parenthesised count is what separates "rendered with a row" from
  "rendered empty".
- `"140 chars"` — `:44` (`stats.avg_length`) / `:117`. Discriminating.
- `"Messages by Agent"` — `:30`, inside `{% if agent_stats %}`. Discriminating.
- `"Channels Created"` — **not** discriminating: `:23` is an unguarded summary-card label
  that renders with zero data. Harmless (it still fails the dropped-`{% include %}`
  mutation), but it is not the guard the docstring implies. **Minor**.

`{% include "admin/_run_detail_body.html" %}` confirmed at
`templates/manager/activity_detail.html:18`, so the assertions really do traverse the shared
partial from the manager side.

### F-D — untested duplicate impersonation gate in `src/main.py` — **ADDRESSED**

This was the assertion to scrutinise. Traced concretely through
`src/main.py:28-96` and the fixture in `tests/integration/test_badge_impersonation_gate.py`:

Fixture state: `admin` owns active agent `hublab` with **3** `ThreadDecision` rows
(`outcome='proposal'`); `pi` owns active `delegatedlab` with **1**, and `mgr` reaches it
through an `AgentDelegate` row; `bystander` (admin) owns nothing. No `ProposalReview` rows
exist, so `badge = max(0, total - 0) = total` (`main.py:88-94`).

- Gate **present**: `uid` = manager. `select(User.is_admin).where(User.id == uid)` compiles
  to `users.user_role = 'admin'` (`src/models/user.py:92-95`) → `False` → **no swap** → own
  agents empty, delegated `['delegatedlab']` → count **1**. Test asserts 1. Passes.
- Gate **removed** (`if is_admin:` deleted, keeping the body): `uid = UUID(impersonate_id)`
  = the admin → own agents `['hublab']` → 3 decisions, 0 reviews → count **3**. Test asserts
  1 → **fails**. The two values genuinely differ, so the assertion distinguishes gate
  present from gate absent. Not a number that would be the same either way.
- Whole block deleted (cookie ignored entirely): the manager case would still pass, which is
  exactly why `test_an_admin_impersonating_gets_the_targets_badge_count` exists — bystander
  (own count 0) impersonating admin must read 3. That control fails if the swap is dead, so
  the pair pins both directions.
- Crash-vs-correct is separated too: the middleware swallows exceptions and leaves 0
  (`main.py:96-103`), and the manager's own value is deliberately **1**, not 0, so "saw
  their own count" cannot be confused with "the middleware died".
- Wiring checks: `main.py:14` imports `get_session_factory` at module scope, so
  `monkeypatch.setattr("src.main.get_session_factory", ...)` really replaces what
  `main.py:41` calls; the probe adds `AgentBadgeMiddleware` before `SessionMiddleware`, which
  under Starlette's prepend semantics reproduces `create_app()`'s order (session outermost),
  so `request.session` is populated. The rationale for not using the shared `client` fixture
  (conftest repoints the factory at a separate committed connection) is consistent with the
  fixture reading rows flushed inside the test transaction.

The PI variant asserts 0, which coincides with the crash value — weak in isolation, but it
is a supplement to the manager case, not the load-bearing assertion.

Spec §7's false claim is rewritten (design doc §7, "Manager cannot impersonate" bullet): it
now says the manager test asserts status codes only, that both codes come from
`get_current_user`, and where the real coverage lives. Accurate.

### F-E — last-admin guard counted admins who cannot log in — **ADDRESSED**

`src/routers/admin.py:206-213` now filters `User.access_status == "allowed"` alongside
`User.user_role == USER_ROLE_ADMIN`, with the inversion spelled out in the comment above it.
The premise is verified: `src/routers/auth.py:267,273` hands a session only to
`access_status == 'allowed'`. `tests/integration/test_role_appointment.py::
test_the_last_admin_guard_counts_only_admins_who_can_log_in` encodes exactly the
counterexample (denied X + allowed Y ⇒ demotion of Y must raise 400) and parametrizes the
`allowed` variant as the false-pass guard against a guard hard-wired to refuse.

### F-F — four cheap items — **ADDRESSED (4/4)**

1. `tests/unit/test_user_roles.py` — the `is None or == 'pi'` test is gone, replaced by a
   comment pointing at `test_manager_access.py::test_default_role_is_pi_in_the_database`
   (which exists and flushes: `tests/integration/test_manager_access.py:32-36`). The
   `USER_ROLE_PI` import is still used at lines 32/38/65, so no dead import.
2. Admin lockout — all three guards now read `is_manager`: `src/routers/onboarding.py:70`
   (bounce), `:101` (self-heal), `src/routers/auth.py:304` (post-login). With
   `templates/base.html:52` offering admins My Profile and `src/routers/profile.py:48`
   bouncing incomplete onboarding, the loop is broken. Pinned by
   `test_an_admin_with_incomplete_onboarding_is_not_locked_out` (both hops) and the
   `pi`/`admin` parametrized self-heal. The `USER_ROLE_PI` import was dropped from both
   modules and no usage remains (grep: only a comment mention).
3. `CLAUDE.md` tense — verified true: `ls alembic/versions/` tops out at `0028_add_user_role.py`,
   no `0029`.
4. Twin markers — verified the twin claim myself: diffing the two files with admin/manager
   tokens normalised leaves only the title and the new comments.

## Residual item — `POST /profile/save` still on `get_current_user`

**Judgement: ship. Not an escalation; file as a follow-up, do not block merge.**

Verified the implementer's argument independently rather than taking it:

- `profile_save` (`src/routers/profile.py:113-195`) writes `User.name/email/institution/
  department` and a `ResearcherProfile`, and **never touches `onboarding_complete`** — the
  grep over all of `src/` shows the sole `= True` assignment is `onboarding.py:225`, which
  is now `get_pi_user`-gated. There is no admin route, CLI command, worker job or invite
  path that sets it.
- The sole `AgentRegistry` constructor in `src/` is `agent_page.py:434`, gated.
- So even a manager who mints a stray profile row cannot satisfy the bot precondition; the
  D7 violation is one orphan row, not a lab.
- Side effects are contained: `export_profile_to_markdown` returns `None` when the caller
  has no `AgentRegistry` (`src/services/profile_export.py:24-25`), so no `profiles/public/`
  file is written and `create_revision` is skipped.

Two things to note for the follow-up: `GET /profile/edit` (`profile.py:81`/`:113` region) is
likewise ungated, so the form is reachable by direct URL, not only by a hand-crafted POST;
and both are trivially fixed by the same dependency once someone decides whether a manager
should be able to edit their own `name`/`email` there (they can already via `/settings`).

## New Breakage in the Fix Diff

**None Critical or Important.**

- The normal PI path is intact and proven, not assumed: `test_a_pi_can_still_use_every_pi_write`
  asserts 302 **and** a changed state snapshot on all four newly-gated POSTs, and
  `test_an_admin_keeps_every_pi_write` does the same for admins — so neither role lost
  access. `factories.make_user`'s defaults are what make those controls non-vacuous.
- Impersonated PIs are unaffected (`get_pi_user` sits on top of the substituted user).
- Behaviour changes that are intended, not breakage: an admin with
  `onboarding_complete=False` is now sent to `/onboarding` at login and the self-heal will
  enqueue `generate_profile` for them. Both are the F-F#2 fix.
- Minor items already listed above: the fail-open predicate shape in `get_pi_user`
  (`src/dependencies.py:136`), the vacuous `"Channels Created"` assertion
  (`test_manager_views.py`, template `:23`), the mis-attributed `PartialbotBot` docstring
  line, the `container_name` question on `run --rm`, and the third copy of the retired
  no-window claim in `alembic/versions/0028_add_user_role.py:8-10`.

Constraints: `ruff check tests/` → *All checks passed!* (0). `ruff check src` → **228**
(ceiling 231). `scripts/ci.sh` is not in the diff's file list — no threshold moved.

## Out-of-Scope Observations

- Promoting a PI who already owns an `AgentRegistry` row to `manager` leaves that manager
  owning a lab — `admin_set_user_role` changes only `user_role`. D7's "no lab of its own" is
  enforced at acquisition time but not at role-change time. Outside this diff.
- Managers can still act on labs delegated to them (`/agent/{agent_id}/...` via
  `get_agent_with_access`). That is the pre-existing delegate feature, distinct from owning a
  lab, but worth a deliberate decision in the whole-branch review.
- `docker-compose.prod.yml`'s `blackbird-app` rename is an uncommitted working-tree
  modification, so the branch's documented deploy commands do not resolve against the
  committed file.

## Verdict

**Fix round: all six findings addressed, no new Critical or Important breakage.** Five
Minor follow-ups recorded (fail-open `get_pi_user` predicate + name, one vacuous assertion
and one mis-attributed docstring line in the run-detail test, `--name` on the migrate
`run --rm`, and the third copy of the retired no-window claim in `0028`'s docstring). The
residual `POST /profile/save` gap can ship.
