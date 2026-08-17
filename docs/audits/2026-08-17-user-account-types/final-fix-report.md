# Final fix wave — `feat/user-account-types`

Branch `feat/user-account-types`, from `8ee827d`. Six findings from the final
whole-branch audit, all fixed. Five commits:

| SHA | Subject |
|---|---|
| `f9bb99f` | `fix(web): a manager can no longer obtain a lab bot (get_pi_user)` |
| `0020031` | `fix(web): the last-admin guard must count only admins who can log in` |
| `b94c3ed` | `fix(web): stop bouncing admins out of their own onboarding` |
| `55d01a5` | `test(web): close the third empty-branch gap and the untested duplicate gate` |
| `e2696cc` | `docs: fix the backwards deploy order and the false coverage claim` |

Nothing was staged from the three working-tree modifications that are not mine
(`.gitignore`, `docker-compose.prod.yml`, `new_orcids.txt`); no threshold in
`scripts/ci.sh` was touched; no `git stash`, no `git clean`, no `docker` command.

---

## F-A (Important, security) — a manager could obtain a lab bot

**What was wrong.** "Strictly read-only" was enforced only on the `/manager`
router. Four PI-write endpoints were still on plain `Depends(get_current_user)`:
`POST /onboarding/save-profile`, `POST /onboarding/retry`, `POST
/profile/refresh`, `POST /agent/request`. `save-profile` is the *only* writer of
`onboarding_complete = True` in `src/` and it also creates the
`ResearcherProfile`; `request_agent` gates on exactly those two fields. So a
manager was two form POSTs from an `AgentRegistry` row of its own — a lab, which
D7 forbids.

**What I changed.** New `get_pi_user` in `src/dependencies.py`, alongside
`get_admin_user` / `get_staff_user`, and the four handlers now declare it.

Two deliberate choices, both stated in the dependency's docstring:

* **Predicate: `is_manager`, not `user_role == 'pi'`.** An admin is not a `pi`
  either, and `templates/base.html` still shows admins the My Profile / My Agent
  links, so a `== 'pi'` gate would 403 every admin on their own navigation. The
  brief's requirement is only that a *manager* cannot reach these, and the
  brief states admins must keep working — so admins retain access, exactly as
  before the branch. Naming a predicate for a role it does not mean is the F7
  trap, so the docstring says the rule in its first paragraph and
  `test_an_admin_keeps_every_pi_write` pins it.
* **Status: 403, not a redirect.** All four are POSTs. Replaying a POST as a GET
  navigation is wrong for the same reason `_login_location` in the same module
  refuses to remember one, and a manager never sees these forms (the nav hides
  them), so this is not a wrong turn to be corrected — it is a request that must
  visibly fail. It also matches the two neighbouring gates, which 403.

**Tests** — `tests/integration/test_pi_only_writes.py` (13 cases):

* manager × 4 endpoints: 403 **and** a four-field state snapshot
  (`onboarding_complete`, generate_profile job count, `AgentRegistry` count,
  `research_summary`) unchanged. The manager fixture is deliberately the *most*
  privileged one possible — onboarding already complete, profile already
  present — so the refusal cannot be coming from `request_agent`'s readiness
  check (which would be 400).
* PI × 4 and admin × 4 controls: 302 **and** the snapshot must change.
* `test_a_manager_with_a_completed_profile_still_gets_no_agent` — the finding end
  to end, in attacker order, with a PI in the identical state as the control.

**Ruling out false passes.** Reverted all four `Depends(get_pi_user)` back to
`get_current_user` and reran: **5 of 13 fail** — the four manager denials plus
the end-to-end escalation. The PI and admin controls keep passing, which is what
proves the four endpoints are alive and the denials are about the gate.

**Residual, deliberately not expanded into.** `POST /profile/save` also creates a
`ResearcherProfile` for a caller that lacks one, and it is still on
`get_current_user`. It is not part of the escalation — it cannot set
`onboarding_complete`, and `/agent/request` is now gated regardless — so the D7
violation it permits is a stray profile row, not a bot. Flagged rather than
fixed, because the audit enumerated four endpoints and widening the blast radius
of a security commit past its audit is its own risk.

---

## F-B (Important, correctness) — the documented deploy sequence was backwards

**What was wrong.** §8 said `up -d --build blackbird-app worker` and *then*
`exec -T blackbird-app alembic upgrade head`. That starts the new code — which
maps `users.user_role`, so the column is named in the SELECT list of every
`select(User)` — against a database where `0028` has not run. Every one of those
raises `UndefinedColumn` for the length of the gap, login included. §8 also
claimed three lines earlier that there is "no window in which live code and
applied schema disagree", which is true only in the old-code/new-schema
direction.

**What I changed.** §8 now names both directions explicitly and says which is
safe and why (old code + new schema is safe *because* `0028` is additive and its
step 3 gives `is_admin` a server default; new code + old schema is a hard
outage). The command block is corrected to build → migrate from a one-off
container off the new image → confirm → start:

```bash
$DC build blackbird-app worker
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current
$DC up -d blackbird-app worker
```

with a note on why `run --rm` (throwaway container off the just-built image, no
published ports, does not replace the running service) and why `exec` in the old
container is not an option (`0028` is baked into the new image only). §2's step-3
paragraph now cross-references §8 instead of asserting the safety in isolation.
The same error was present in `CLAUDE.md`'s Account Types section and is fixed
there as a call-out block.

---

## F-C (Important) — third empty-branch test gap

**What was wrong.** Every table in `templates/admin/_run_detail_body.html` sits
behind an `{% if %}` (`agent_stats`, `channel_stats`, `channels`, `messages`).
The only manager request reaching it seeded a bare `SimulationRun` and asserted
nothing about the body, so a manager route that dropped a key from its `**view`
splat — or a wrapper that lost the `{% include %}` — would still return 200.

**What I changed.** `test_manager_activity_detail_renders_the_populated_shared_partial`
in `tests/integration/test_manager_views.py` seeds one `AgentChannel` and one
`AgentMessage` on the run so all four guards open, and asserts on markup only the
populated path can emit: the fixture-unique channel name (the wrapper prints no
channel names at all), `PartialbotBot` (the partial's own `{{ msg.agent_id }}Bot`
concatenation — the bare agent_id would not discriminate), `Message Timeline (1)`
(the count comes from `messages | length`, so it separates "rendered with a row"
from "rendered empty" in a way a bare `Message Timeline` substring cannot), and
`140 chars`. Wrapper-only assertions are kept as the control.

**Ruling out false passes.** Two mutations, both run:

1. Remove the message/channel seeding → fails (`partial-proof-channel` absent;
   the page is the three summary cards alone, since all four guards close).
2. Delete `{% include "admin/_run_detail_body.html" %}` from
   `manager/activity_detail.html` → fails identically, while the wrapper's own
   assertions still pass.

Both files were restored and re-verified clean (`git diff --stat templates/`
empty).

---

## F-D (Important, security) — untested duplicate gate, and a false claim about it

**What was wrong.** `AgentBadgeMiddleware` in `src/main.py` re-implements the
impersonation check independently (it re-reads `select(User.is_admin)` and only
then swaps the uid). No test covered it: the existing manager-impersonation test
asserts status codes only, and both of those (200 on `/manager/pis`, 403 on
`/admin/users`) come from `get_current_user`. Deleting `if is_admin:` in
`main.py` left the whole suite green. Spec §7 claimed that test exercises
`main.py:50-59`.

**What I changed.** `tests/integration/test_badge_impersonation_gate.py`, which
observes the middleware's only real output: whose badge count it computed.

A note on why it needs its own probe app rather than the shared `client` fixture:
`tests/conftest.py` deliberately repoints the middleware's session factory at a
*separate committed connection*, which by construction cannot see rows written
inside the test's rolled-back transaction — so through the shared client every
badge count is 0 and the difference under test does not exist. The probe app
mounts `AgentBadgeMiddleware` + `SessionMiddleware` in `create_app()`'s order and
returns `request.state.agent_badge_count` directly (the nav pill that normally
renders it is role-gated and hidden from managers, so markup would not do).

The fixture makes three values distinguishable: the manager's own count is **1**
(via a delegated lab), the impersonation target admin's is **3**, and **0** is
what the middleware yields when it fails outright and swallows the exception.
Giving the manager a non-zero count is what separates "saw their own" from "the
middleware crashed" — with 0 those are the same observation. Two controls: an
admin impersonating the same target must see 3 (the cookie really does swap the
uid, so deleting the whole block would not pass vacuously), and the three
baseline counts must differ at all.

**Ruling out false passes.** Neutralised the gate (`if True or is_admin:`) and
reran: the manager and PI cases fail, both controls pass. `src/main.py` restored
and verified unmodified.

Spec §7's claim is rewritten to say what the manager test actually covers, why
status codes cannot reveal this gate, and where the real coverage now lives.

---

## F-E — last-admin guard reasoning was inverted

**What was wrong.** The guard counted every `user_role='admin'` row regardless of
`access_status`. An earlier note called that "more conservative"; it is the
opposite — a larger count makes `<= 1` fire *less* often, so demotion gets
*easier*. Counterexample: admins X (`denied`) and Y (`allowed`) count as 2, Y is
demotable, and zero loginable admins remain (`auth.py` hands no session to anyone
whose `access_status != 'allowed'` — verified at the redirect to
`/access-pending`).

**What I changed.** The count is filtered on `access_status == "allowed"`, with
the inversion spelled out in a comment so the "more conservative" reading cannot
recur. Spec §6's guard-3 bullet records the same.

**Test.** `test_the_last_admin_guard_counts_only_admins_who_can_log_in`,
parametrized: `denied` → the demotion must raise 400, `allowed` → the identical
call must succeed. The second case is the false-pass guard — a guard hard-wired
to refuse every demotion would pass the first alone. It calls the handler
directly, following the existing last-admin test, because over HTTP the actor
would itself be an allowed admin and would change the count.

**Ruling out false passes.** Reverted the filter and reran: only
`[other-admin-cannot-log-in]` fails.

The separate concurrency concern (two demotions racing between the count and the
commit) is untouched and stays deferred, as instructed.

---

## F-F — four cheap items

1. **Vacuous default-role test deleted.** `tests/unit/test_user_roles.py`'s
   `test_default_role_is_pi` asserted `is None or == 'pi'`, which accepts both
   answers. Replaced with a comment explaining why a pre-flush `User()` cannot
   answer this and pointing at the DB-backed coverage in
   `test_manager_access.py::test_default_role_is_pi_in_the_database`.

2. **Admins no longer locked out of `/profile`.** Root cause was a contradiction
   inside spec §5: it gated the nav on `user_role == 'pi' or is_admin` while
   telling the onboarding guards to test `!= 'pi'`. Implemented literally, an
   admin with `onboarding_complete=False` went `/profile` → `/onboarding` →
   `/manager/pis` forever, and since `POST /onboarding/save-profile` is the only
   writer of the flag, it could never be cleared. All three guards now read
   `is_manager`: the `/onboarding` bounce, the `generate_profile` self-heal
   (narrowing that to `== 'pi'` would leave an admin on "Building Your Profile"
   with no job, no profile and no retry control — the exact spin it exists to
   prevent), and `auth.py`'s post-login onboarding redirect. §5 now states the
   rule once — *exclude `manager`, never "non-PI"* — and records why.

   Tests: the self-heal test is parametrized over `pi`/`admin`, and
   `test_an_admin_with_incomplete_onboarding_is_not_locked_out` pins both hops
   (`/onboarding` renders 200 with "Building Your Profile"; `/profile` followed
   through redirects lands on `/onboarding`). Verified by reverting both guards
   to `!= 'pi'` / `== 'pi'`: exactly those two fail, and every manager case keeps
   passing, so the narrowing was not simply deleted.

   One sub-change is not directly covered: `auth.py`'s post-login branch is only
   reachable through the ORCID OAuth callback, which the suite cannot drive (no
   test in `tests/` exercises a successful callback). It is behaviour-equivalent
   for admins either way — with the old predicate they reach `/onboarding` one
   redirect later, via `/profile` — so it is a consistency change, not the fix.

3. **`CLAUDE.md` tense corrected.** It described `0028`'s deploy in the past
   tense ("the already-running container kept working…") and implied `0029`
   exists. `ls alembic/versions` shows no `0029`; the text now says it "has not
   been written, let alone applied", and the `0028` claim is restated as the
   forward-looking deploy rule (see F-B).

4. **Twin markers.** `templates/manager/activity.html` and
   `templates/admin/activity.html` each carry a `{# TWIN — keep in sync with … #}`
   comment noting they are byte-identical apart from title and links, and that
   unlike the run-*detail* pages (which share `admin/_run_detail_body.html`) this
   list page was copied rather than extracted. Confirmed the twin claim by
   diffing the two files with the admin/manager tokens normalised: the only
   remaining differences are the comments themselves.

---

## Gate

`./scripts/ci.sh`, foreground, twice (once after the last commit, once to
capture the summary). Both green.

```
==> alembic (single head, no duplicate revision ids)
==> alembic round trip against a throwaway postgres:15 on 127.0.0.1:55432
    round trip clean (upgrade head -> downgrade 0018 -> upgrade head)
==> ruff (test-suite lint)          -> All checks passed!  (0 findings)
==> ruff (src/ ratchet, ceiling 231)
    228 findings (ceiling 231)
==> pytest (full suite + branch coverage, fail-under=60%)
Required test coverage of 60% reached. Total coverage: 75.95%
16 snapshots passed.
2026 passed, 93 skipped, 1 warning in 370.96s (0:06:10)
==> CI passed.
```

`src/` lint went 227 → **228**: the single added finding is B008 on
`get_pi_user`'s `Depends(get_current_user)` default, matching the two
dependencies immediately above it in the same file. Swapping the four handlers'
`Depends(get_current_user)` for `Depends(get_pi_user)` is finding-neutral. No
threshold was moved — `SRC_LINT_MAX` is still 231, `COV_MIN` 60,
`MIGRATION_FLOOR` 0018.

Net test movement: **+22 cases** (13 in the parametrized PI-write sweep, 4 in the
badge-gate file, 2 in the parametrized last-admin case, 1 run-detail partial, 1
admin-lockout, 1 added `admin` parametrization on the self-heal) and **−1**
(the deleted vacuous default-role test).
