# Independent post-deploy audit — user account types (0028)

**Auditor:** independent agent, read-only. **Date:** 2026-08-17.
**Subject:** production deploy of `feat/user-account-types` to `copi-blackbird` at
`/home/ubuntu/blackbird-copi-science`, as described in `deploy-log.md`.

Every number below was measured by me. Nothing in the deploy log was accepted as given.
No state was modified: no writes, no `up`/`down`/`build`/`stop`/`rm`, no git ref changes.
The only containers created were two `docker run --rm` read-only inspections of images this
project owns (`copi-blackbird-agent:latest`, `postgres:15` with a `:ro` bind mount).

---

## Verdict

**The deployment is sound and production is healthy.** The schema change is correct, the
data is intact for every row (not just the spot-checked ones), the running image is
byte-identical to the merged code, `0029` is genuinely absent, authorization holds, org1
was not touched, and the simulation is not running.

The defects I found are **not in what was deployed** — they are in **what happens next**:
the documented rollback is written in an order that would cause a total outage, the
rollback target image is untagged and one `docker image prune` from deletion on a 92%-full
disk, and the agent image was never rebuilt, leaving it as the one artifact that will
hard-fail when `0029` lands.

---

## 1. Data integrity — PASS, verified for all 65 rows

All five baseline counts reproduce exactly:

| Table | Deploy log baseline | Measured now |
|---|---|---|
| users | 65 | **65** |
| agents | 64 | **64** |
| researcher_profiles | 65 | **65** |
| agent_messages | 50 | **50** |
| opportunity_assessments | 0 | **0** |

Beyond the deployer's five spot-checks I also measured: publications 2759,
profile_revisions 533, llm_call_logs 3689, cohorts 62, cohort_memberships 186,
cohort_audit_events 8, simulation_runs 5, app_settings 1 — all populated and coherent.

**Backfill fidelity, every row (the deployer only checked the two admins):**

```sql
SELECT count(*) FROM users
WHERE (is_admin = true  AND user_role <> 'admin')
   OR (is_admin = false AND user_role NOT IN ('pi','manager'));
-- 0
```

Both columns still exist, so this is a complete, not sampled, cross-check. Result: **0
mismatches across all 65 rows.** Distribution is `admin/true = 2`, `pi/false = 63` —
no third state, no drift. **Zero NULLs** in either column.

Both admins survive with `access_status='allowed'`:
`0000-0001-8420-1325 Mohammad Alanjary`, `0000-0002-2416-7484 Alan Huebschen`.

**Referential integrity:** all 38 foreign-key constraints are `convalidated = true` — none
were left `NOT VALID`. Explicit anti-joins for orphans on `researcher_profiles.user_id`,
`agents.user_id`, `publications.user_id` and `cohort_memberships.added_by` all return 0.

**Note on measurement method:** `pg_stat_user_tables.n_live_tup` reports `users = 4` and
`agents = 4`. That is stale planner statistics, not damage — `count(*)` gives 65 and 64.
Anyone auditing this deployment via `pg_stat` will be misled (see F-L5).

---

## 2. Schema correctness — PASS; user creation does work

```
column      | nullable | default
------------+----------+---------------------------
user_role   | NO       | 'pi'::character varying
is_admin    | NO       | false
```

`ck_users_user_role CHECK (user_role IN ('pi','manager','admin'))` is present.
Alembic: single head, `current == heads == 0028`, confirmed from inside the running image.

**The load-bearing `is_admin DEFAULT false` is really there, and it is genuinely
load-bearing.** I confirmed the chain end to end:

- `User.__mapper__.columns` does **not** contain `is_admin` (verified in the running
  container) — the ORM's INSERT column list is 16 columns and omits it.
- `src/routers/auth.py:215` constructs `User(orcid=…, name=…, email=…, institution=…,
  department=…, access_status=…)` — no `is_admin`.
- Every other `NOT NULL`-without-DDL-default column on `users` (`id`, `name`, `orcid`,
  `email_notifications_enabled`, `onboarding_complete`, `access_status`) is mapped with a
  Python-side default the ORM supplies.

So `INSERT INTO users` omits `is_admin`, Postgres fills `false`, and registration works.
Without 0028's `ALTER COLUMN is_admin SET DEFAULT false` it would fail outright. The
deployer's F14 reasoning is correct and the fix is live.

Caveat: this is proven by construction, **not empirically** — see F-L4, no user has been
created in production since 2026-08-11.

---

## 3. Deployed image matches deployed code — PASS

- Running `copi-blackbird-app-1` image ID `sha256:77ecf8d080bb…` **is** the current
  `copi-blackbird-blackbird-app:latest` (built 2026-08-17 20:37:09). Worker likewise
  (`8c97a76dab3f`). Neither is stale.
- **Byte-identical code.** I sha256'd all 148 `.py`/`.html`/`.js`/`.css` files under
  `src/`, `templates/`, `static/`, `alembic/` inside the container and in the working tree
  (clean at HEAD `42c2d66` for those paths). `diff` → **IDENTICAL**, 148/148.
- **`0029` is absent from the image.** `/app/alembic/versions/` ends at `0028_add_user_role.py`.
  `0029_drop_is_admin.py` exists nowhere on `blackbird` — only on branch
  `feat/user-account-types-0029` (commit `06e9325`). The two-phase split holds: the deployed
  image physically cannot drop the column.
- **`templates/` and `static/` are baked in, not bind-mounted.** The app container's only
  mounts are `./prompts → /app/prompts` and `./profiles → /app/profiles`; the worker's only
  mount is `./profiles`. There is no stale-template channel. All six manager templates
  (`pis`, `pi_detail`, `assessments`, `discussions`, `activity`, `activity_detail`) are in
  the image.

---

## 4. Authorization — PASS on substance; the deployer's *evidence* was partly wrong

**The deployer's `_IncludedRouter` trap is real, and worse than they described.** FastAPI
0.141.1 wraps included routers as `_IncludedRouter`, and I confirmed that object exposes
`routes == []` and no `prefix` attribute — so even a *recursive* walk of `app.routes` finds
0 manager endpoints (I wrote one; total endpoint count came back as 15). Introspection via
`app.routes` is genuinely blind here.

**But the sound evidence is not HTTP probing either — it is the router object.** I
enumerated `src.routers.manager.router.routes` directly:

```
GET /manager                  deps: get_staff_user
GET /manager/pis              deps: get_staff_user
GET /manager/pis/{user_id}    deps: get_staff_user
GET /manager/assessments      deps: get_staff_user
GET /manager/discussions      deps: get_staff_user
GET /manager/activity         deps: get_staff_user
GET /manager/activity/{run_id} deps: get_staff_user
router-level deps: get_staff_user
```

Seven routes, all `GET`, all gated at both router and route level. **No `llm-calls` route
exists anywhere under `/manager`**; the only one in the app is
`GET /admin/activity/{run_id}/llm-calls`. No export route under `/manager`. The design
intent holds. **The conclusion in the deploy log is correct.**

**The deploy log's stated evidence for it is not.** It claims
`/manager/.../llm-calls -> **404**`. The app's own access log — which I read — records the
deployer's own probe returning the opposite:

```
"GET /manager/pis/llm-calls HTTP/1.1" 302 Found
```

I reproduced this through real production ingress: `/manager/pis/llm-calls` → **302**,
`/manager/activity/llm-calls` → **302**, `/manager/pis/export` → **302** (and there is no
export route). The reason is that `{user_id}` and `{run_id}` swallow an arbitrary segment.
Only `/manager/activity/{uuid}/llm-calls`, `/manager/assessments/llm-calls`,
`/manager/discussions/llm-calls`, `/manager/llm-calls` and `/manager/export` return 404.

So the "302 = registered-and-gated, 404 = unregistered" discriminator **is not sound in
general**: under a path-parameter prefix, an unregistered path also returns 302. It
happened to reach the right conclusion, but it cannot be relied on and the log overstates
what was observed. See F-L1 and F-L2.

**Anonymous access through real production ingress** (`https://blackbird.copi.science`,
no redirect following):

- `302 → /login?next=…`: `/manager`, `/manager/pis`, `/manager/assessments`,
  `/manager/discussions`, `/manager/activity`, `/admin/users`, `/admin/agents`,
  `/admin/assessments`, `/profile`, `/agent`, `/onboarding`.
- `200`: only `/api/health` and `/login`.

**Static analysis of the gates:**

- `get_staff_user` 403s anything that is not `is_staff`; it is used *only* by `/manager`.
- Every route on `admin.router` carries an admin dependency except
  `POST /admin/impersonate/stop`, which is `get_current_user` — it only clears the
  impersonation cookie. Benign.
- **Impersonation is still gated on `is_admin`**, in both `src/dependencies.py:74` and the
  duplicate check at `src/main.py:52`. `is_admin` was not widened; `is_staff` is a separate
  predicate. No escalation path for a manager.
- The hybrids compile correctly under SQLAlchemy 2.0.52 — I compiled them in the running
  container: `select(User.is_admin)` → `SELECT users.user_role = :user_role_1`,
  `where(User.is_staff)` → `users.user_role IN (…)`. `src/main.py:53`'s SQL-level use works.
- `POST /admin/users/{user_id}/role` (the admin-granting endpoint): gated on
  `get_admin_user`, validates against `VALID_USER_ROLES` (defence in depth alongside the
  CHECK constraint), refuses self-change, and the last-admin guard counts only
  `access_status='allowed'` admins — which is the correct direction, as its comment argues.

---

## 5. org1 impact — PASS, and the residual risk is narrower than feared

**Not impacted.** Every org1 container has `RestartCount = 0` and a start time that
predates the 20:37/20:39 deploy window:

| Container | Started | Restarts |
|---|---|---|
| copi-python-nginx-1 | 2026-08-06 21:55 | 0 |
| copi-python-postgres-1 | 2026-08-06 21:55 | 0 |
| copi-python-certbot-1 | 2026-08-06 21:55 | 0 |
| copi-python-grantbot-1 | 2026-08-14 15:27 | 0 |
| **agent-run** (org1's live sim) | 2026-08-14 15:28 | 0 |
| copi-python-app-1 | 2026-08-15 01:07 | 0 |
| copi-python-worker-1 | 2026-08-15 01:07 | 0 |

`https://copi.science/` → 200. org1's alembic is still **0024** (unchanged; blackbird's
migration ran against a different database in a different container). `agent-run`'s label
is `com.docker.compose.project=copi-python`, and its host process (PID 286057,
`python -m src.agent.main --budget 0`) maps to that container's cgroup.

**Could it have been impacted?**

- **copi-edge:** membership is exactly `copi-python-nginx-1` + `copi-blackbird-app-1`. The
  H2 hazard is real — `docker compose run blackbird-app` would join copi-edge under the
  alias `blackbird-app`, which org1's nginx resolves, so DNS round-robin could route
  production traffic into an alembic container. The deployer's mitigation (plain
  `docker run` attached only to `copi-blackbird_default`, where postgres holds the alias
  `postgres`) was correct and I confirmed both aliases.
- **Ports 80/443:** held solely by `copi-python-nginx-1`. `docker compose config` confirms
  a bare `up -d` would try to start blackbird's `nginx` (published 80/443) and `certbot`.
  **Correction to the received wisdom:** this could not *evict* org1's nginx — Docker
  cannot steal an already-bound port; blackbird's nginx would simply fail to bind and
  crash-loop under `restart: unless-stopped`. The real residual risks of a bare `up -d` are
  a crash-looping container and a `certbot` service pointed at blackbird's own
  `certbot/conf` (Let's Encrypt rate-limit exposure), not an org1 outage. Still worth
  avoiding; just not for the reason usually given.
- **`--remove-orphans`:** with `COMPOSE_PROJECT_NAME=copi-blackbird` pinned in `.env`,
  orphan removal is **project-scoped** and cannot reach `copi-python` containers. The only
  thing it would delete today is `blackbird-agent-run` (agent is a profile service). The
  deployer refused it anyway, which is right, but the org1 danger is historical — it
  predates the pinned project name.

---

## 6. Simulation not running — PASS

`blackbird-agent-run` is `Exited (137)`, 2 days ago, label
`com.docker.compose.project=copi-blackbird`. It was correctly left in place rather than
removed. The only `src.agent.main` process on the host belongs to org1's `agent-run`.

---

## 7. Rollback — the backup is good; the *procedure* is not

**The backup is genuinely usable.**
`backups/copi_pre0028_20260817_203346.dump`, 72,378,144 bytes, created **2026-08-17
20:33:46 UTC** — before the 20:37 build and the migration. It begins with the `PGDMP`
magic header, and `pg_restore -l` (run in a throwaway `postgres:15` container with a `:ro`
mount) parses it cleanly: **189 TOC entries, 30 `TABLE DATA` sections**, including
`users`, `agents`, `researcher_profiles`, `agent_messages`, `publications`,
`llm_call_logs`. It contains no `user_role`, confirming it is genuinely pre-0028.
`pg_restore` 15.17 matches the server's 15.17.

Minor friction: `backups/` is **not** mounted into `copi-blackbird-postgres-1` (its only
mount is the `pgdata` volume), so a restore needs a `docker cp` or an ad-hoc mount. Not a
blocker, but the runbook does not say so.

**The `0028` downgrade itself is correct and data-preserving.** It restores
`is_admin = (user_role = 'admin')` *before* dropping the column, then drops the CHECK
constraint, drops `user_role`, and removes the `is_admin` default (Alembic's
`server_default=None` does emit `DROP DEFAULT`, restoring the original pre-0028 shape).
`docker run --network copi-blackbird_default --env-file .env` would resolve `postgres`
correctly (alias confirmed) and would *not* join copi-edge, so it cannot steal traffic.
`.env` contains no quoted values, so `--env-file` is safe.

**But the documented order is backwards — see F-H1.** And the image to roll back *to* is
untagged — see F-H2.

---

## 8. What the deployer did not check

### F-H1 — HIGH — The documented rollback order would cause a total outage

The deploy log says: run `alembic downgrade 0027` "**then** redeploy the prior image."
That is the wrong way round, and it is the exact mirror of the hazard the team correctly
identified and correctly avoided in the forward direction.

`0028`'s downgrade **drops `users.user_role`**. The currently-running image **maps**
`user_role`, so it is named in the SELECT list of every `select(User)` — including
`get_current_user`, i.e. login. Downgrade first and the live container starts raising
`UndefinedColumn` on every authenticated request, for the whole interval until the old
image is up. Total, immediate outage.

The correct order is the mirror of the deploy: **stop/replace with the prior image first,
then downgrade.** The deploy log's own "Deploy order for 0028" reasoning (preserved in
CLAUDE.md) gets the forward direction exactly right and then states the reverse direction
backwards. Nobody re-derived it for the rollback.

### F-H2 — HIGH — The rollback target image is untagged and one prune from deletion

"Redeploy the prior image" names no image ID or digest. The build overwrote the
`copi-blackbird-blackbird-app:latest` tag, so the previous app image is now **dangling**
(most likely `0c1b4c707981` / `0574ca572380`, both 2026-08-15 11:14). Combined with F-M2
(host at 92%), a routine `docker image prune` — the obvious response to disk pressure —
would silently destroy the rollback target, and 24 GB of the reclaimable space is
untagged images. The digest should have been recorded in the deploy log before the build.

### F-M1 — MEDIUM — `copi-blackbird-agent:latest` was never rebuilt and is the one artifact `0029` will break

Built **2026-08-15 11:14**, before this change. I inspected it: its
`src/models/user.py:24` still declares

```python
is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

its alembic chain ends at `0027`, and it has **no manager router**. Consequences:

1. Starting the simulation today would deploy **pre-account-types code**. It would work
   (0028 is additive, `is_admin` still exists) — silently, with no error, which is exactly
   the failure mode CLAUDE.md warns about for this image.
2. When `0029` drops `users.is_admin`, this image's every `select(User)` raises
   `UndefinedColumn`. It is the single artifact in the system that is *not* 0029-safe, and
   the deploy log does not mention it at all. CLAUDE.md explicitly requires
   `$DC --profile agent build agent` after any `src/` change.

(`copi-blackbird-grantbot:latest`, 2026-08-05, is in the same category but is not running.)

### F-M2 — MEDIUM — Disk headroom got worse, and was not re-checked

`/dev/root`: **56G used of 61G, 92%, 5.3 GB available** — and this filesystem also carries
`/var/lib/docker/volumes/copi-blackbird_pgdata`, i.e. production Postgres. `docker system df`:
267 images / 32.06 GB (24.02 GB reclaimable) and **31.1 GB of build cache**. The build
consumed headroom rather than freeing it, and the deploy log's checklist never revisits
disk. 5.3 GB is enough for now (the DB is 136 MB) but leaves no room for another image
build plus WAL growth.

### F-M3 — MEDIUM — The manager PI directory hides real PIs, on live data

`src/routers/manager.py:82` passes `roles=(USER_ROLE_PI,)` to `list_pi_directory`. That is
a "PI only" filter, not an "exclude managers" filter — the precise inversion CLAUDE.md
warns about, applied to the directory instead of to the guards. On current production data
that means the two admin accounts are **invisible at `/manager/pis`**, including:

```
0000-0001-8420-1325  Mohammad Alanjary  admin  profiles=1  publications=25  agents=1
```

A real PI with a real profile, 25 publications and a lab agent, absent from the read-only
oversight surface the feature exists to provide. `/manager/pis/{user_id}` applies **no**
such filter, so the detail page is still reachable by direct URL — an internal
inconsistency. Latent today (0 managers exist), which is presumably why it was never
noticed; it becomes real the moment the first manager is appointed. The docstring records
`roles=(USER_ROLE_PI,)` as intentional, but nobody measured its effect on real rows.

### F-M4 — MEDIUM — The email/notification path is role-blind and was not examined

`src/services/email_notifications.py:671` is
`select(User).options(selectinload(User.agent)).where(User.email.isnot(None))` — **no role
filter**; the same is true at :193 and :901. A newly appointed manager would be enrolled in
PI-oriented digests, and `src/services/email_inbound.py:164` resolves inbound mail to a
user with no role awareness either. `src/services/directory.py:62` is the *only* place in
`src/services/` that knows `user_role` exists. The deploy log's coverage of background and
notification paths is limited to "zero ERROR lines in worker logs."

### F-L1 — LOW — A stated result in the deploy log is contradicted by the app's own log

"`/manager/.../llm-calls` -> **404**" is wrong as written; the deployer's own probe of
`/manager/pis/llm-calls` returned **302**, visible in `docker logs copi-blackbird-app-1`.
Only the `/manager/activity/{uuid}/llm-calls` variant returned 404, and only that variant
was reported. The conclusion is correct; the evidence cited for it is not.

### F-L2 — LOW — The 302-vs-404 discriminator is not sound

Under a path-parameter prefix, an *unregistered* path also returns 302
(`/manager/pis/export` → 302, and no export route exists). The log presents the 404 as
what makes the 302s meaningful; it does not. Router enumeration is the sound method.

### F-L3 — LOW — Worker health is unproven

`copi-blackbird-worker-1` has **no healthcheck** at all (`docker inspect` → no `Health`),
has emitted exactly one log line since deploy (`Worker started, polling every 5s`), and
`jobs` is empty. "Zero ERROR lines" therefore means *zero work done*, not *works*. Nothing
has exercised `src/worker/main.py:65`'s `select(User)` against the new schema.

### F-L4 — LOW — The load-bearing default has never been exercised in production

`max(users.created_at) = 2026-08-11`, `max(last_login_at) = 2026-07-29`. No user has
registered and nobody has logged in since the deploy, so neither the `is_admin DEFAULT
false` fix nor the login path has empirical confirmation — only the (solid) structural
proof in §2. A single ORCID sign-in would convert this to certainty.

### F-L5 — LOW — No `ANALYZE` after the backfill; planner stats are stale

`users` shows `n_dead_tup = 8` from 0028's `UPDATE`, with `last_autovacuum` and
`last_analyze` both **NULL**, and `n_live_tup = 4` against an actual 65. Harmless at this
size, but it means anyone auditing row counts via `pg_stat_user_tables` gets wrong answers.

### F-L6 — LOW — `src/cli.py admin:revoke` has no last-admin guard

The HTTP endpoint has one; the CLI does not. It is a container-shell escape hatch and
`role:set` recovers from it, so the exposure is small — but the invariant is enforced in
only one of the two places that can break it.

### F-L7 — LOW — The deployed compose topology is not in git

`docker-compose.prod.yml` is modified and **uncommitted** (awslogs → json-file, `app` →
`blackbird-app` + pinned `container_name`, copi-edge attachment). Pre-existing, not
introduced by this deploy, but the running production topology cannot be reconstructed from
any commit — and the deploy was performed with a dirty working tree for that file.

### F-L8 — LOW — `profiles/` contains root-owned paths

`profiles/`, `profiles/public/`, `profiles/memory.pre-pitchonly/` and files within are not
owned by `ubuntu` (a known pre-existing condition). Host and container views agree
(0 top-level `.md` in both), so this deploy introduced no divergence in bind-mounted state.

### Forward-compatibility with `0029` — mostly good

I grepped all of `src/`, `templates/`, `scripts/` and `alembic/` for `is_admin`. **Every
remaining read goes through the hybrid over `user_role`** — `src/main.py:53`
(`select(User.is_admin)`, which compiles to `users.user_role = 'admin'`),
`src/dependencies.py:74` and `:132`, and `templates/base.html` (lines 52, 62, 73, 103).
There are **no raw SQL references** to the physical column outside `0028` itself, and no
assignment to `is_admin` anywhere (the CLI correctly uses `USER_ROLE_ADMIN`). So the
deployed `src/` is 0029-safe. The exception is F-M1's stale agent image.

---

## Claims in the deploy log I could not reproduce, or found wrong

1. **"`/manager/.../llm-calls` -> 404"** — **WRONG as stated.** `/manager/pis/llm-calls` and
   `/manager/activity/llm-calls` return **302**, and the deployer's own request appears in
   the app access log returning 302. Only the `{uuid}` variant is 404. The underlying
   conclusion (no such route) is nonetheless correct.
2. **"404 is the unregistered response, making those 302s meaningful"** — **UNSOUND.**
   `/manager/pis/export` is unregistered and returns 302.
3. **"Rollback … `alembic downgrade 0027` then redeploy the prior image"** —
   **WRONG ORDER**, would cause a total outage (F-H1).
4. **"pg_restore -l verified"** — reproduced, but only after mounting the file into a
   container; the naive in-container path fails because a custom-format dump is not
   seekable over stdin. Not a defect in the backup.
5. **Everything else reproduced exactly**: all row counts, both admin ORCIDs, the schema
   defaults and CHECK constraint, `0027 → 0028`, `current == heads`, `0029` absent from the
   image, all six manager templates present, org1 uptimes and revision 0024, the
   `_IncludedRouter` introspection artifact (which is if anything understated), no running
   `blackbird-agent-run`, and the backup's size, timestamp and restorability.

## Recommended actions, in priority order

1. Correct the rollback runbook: **replace the image first, then downgrade.**
2. Record the prior image digest in the deploy log; ideally tag it
   (`copi-blackbird-blackbird-app:pre-0028`) so a prune cannot destroy it.
3. Rebuild `copi-blackbird-agent` before any simulation run, and **before `0029`**.
4. Reclaim disk (`docker builder prune` — 31 GB of build cache — *after* step 2).
5. Decide whether `/manager/pis` should be `roles=(pi, admin)`; today it hides a real PI.
6. Exercise one ORCID login to convert §2's structural proof into an empirical one.
