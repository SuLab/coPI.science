# Production deploy log — PI/manager/admin account types (0028)

Host runs TWO stacks. Every claim below was verified live, not from memory.

## Ownership map (measured via docker inspect labels)

| Container | Project | Owner |
|---|---|---|
| copi-blackbird-app-1, copi-blackbird-worker-1, copi-blackbird-postgres-1 | copi-blackbird | THIS repo |
| copi-python-{app,worker,grantbot,nginx,certbot,postgres}-1 | copi-python | org1 — DO NOT TOUCH |
| **agent-run** | **copi-python** | **org1's PRODUCTION simulation — DO NOT TOUCH** |

This repo's simulation is NOT running. (Correction: a *stopped* container named
`blackbird-agent-run` does exist — Exited(137), from a run 2 days ago. An earlier
draft of this file wrongly said none existed. It still holds the name and is pinned
to a pre-merge image; see the agent trap below.)

## Confirmed hazards and the mitigations chosen

**H1 — bare `up -d` would collide with org1's ingress.** This repo's compose defines
`nginx` binding 80:80/443:443 (docker-compose.prod.yml:118-150), and org1's
copi-python-nginx-1 currently holds those ports. Mitigation: name services explicitly
(`up -d blackbird-app worker`); never bare `up -d`, never `--remove-orphans`.

**H2 — RETRACTED. This hazard is FALSE; the mitigation was harmless but the reasoning was wrong.**

ORIGINAL CLAIM (kept so the error is visible, not quietly deleted): blackbird-app joins the shared
`copi-edge` network and Compose adds the SERVICE NAME as a network alias. Verified:
org1's nginx has `upstream blackbird_app { server blackbird-app:8000; }` and proxies
blackbird.copi.science to it; copi-blackbird-app-1 holds alias `blackbird-app` on
copi-edge. A `docker compose run blackbird-app alembic ...` container would join
copi-edge under the SAME alias, so nginx DNS could round-robin production requests into
a container running alembic rather than uvicorn -> 502s.
Mitigation used: a plain `docker run` attached ONLY to `copi-blackbird_default`.

**CORRECTION, measured on Compose 2.37.1 (this host):** a `compose run` container is
given ONLY its own container name as a network alias — never the service name.
Verified empirically: a `compose run` probe showed aliases `[alias-probe]`, while the
`up`-created app container shows `[copi-blackbird-app-1 blackbird-app]`. So
`docker compose run blackbird-app alembic ...` could NOT have acquired the
`blackbird-app` alias on copi-edge and could NOT have taken production traffic.
The isolated `docker run` remains fine (it is still tighter isolation), but do not
propagate the stated hazard — it is not real.

**H3 — data.** Full `pg_dump -Fc` taken BEFORE any DDL and verified restorable.

## Pre-deploy measured state

- alembic_version = **0027** (so 0028 applies cleanly; 0028's down_revision is 0027)
- `users.is_admin`: NOT NULL, **column_default EMPTY** — confirms audit finding F14 against
  real production. Without 0028's `ALTER COLUMN is_admin SET DEFAULT false`, the new code
  (which no longer maps that column) could not INSERT a user at all.
- `users.user_role` does not exist yet.
- Row baseline: users 65, is_admin=true 2, agents 64, researcher_profiles 65,
  agent_messages 50, opportunity_assessments 0.
- Admins that MUST survive as user_role='admin':
  0000-0001-8420-1325 Mohammad Alanjary (allowed), 0000-0002-2416-7484 Alan Huebschen (allowed).

Note: prod being at 0027 is exactly the state that the preflight bug found in Task 1 would
have BLOCKED (`SUPPORTED_START_REVISIONS` lacked "0027" while DEFAULT_TARGET moved to 0028).
That fix was load-bearing for this deploy.

## Sequence

1. Backup (done) 2. Merge to blackbird (done, ff to e9c6c26) 3. Build images
4. Migrate via isolated `docker run` 5. Verify schema + data 6. `up -d blackbird-app worker`
7. Verify health/endpoints 8. Adversarial audit. Simulation NOT started (per instruction).

## Executed — results (2026-08-17)

- **Backup**: `backups/copi_pre0028_20260817_203346.dump` (72378144 bytes), pg_restore -l verified.
- **Merge**: fast-forward, blackbird @ e9c6c26, alembic head 0028. 13 untracked user docs intact.
- **Build**: copi-blackbird-blackbird-app + worker rebuilt (1.26GB). Verified INSIDE the image:
  manager router, services/directory, 0028 present; **0029 ABSENT** (two-phase split holds —
  the deployed image physically cannot drop users.is_admin); all 6 manager templates present.
- **Migration**: applied from an ISOLATED `docker run` on `copi-blackbird_default` only.
  Verified copi-edge membership was unchanged during it (still just nginx + the live app), so
  no production request could be routed into the migration container. 0027 -> 0028;
  `alembic current` == `heads` == 0028.
- **Post-migration data** (vs pre-deploy baseline): users 65/65, agents 64/64,
  researcher_profiles 65/65, agent_messages 50/50, assessments 0/0 — **no row changed**.
  user_role: admin 2, pi 63. Both admins preserved by ORCID
  (0000-0001-8420-1325, 0000-0002-2416-7484). `is_admin` default now `false`
  (F14 fix live), `user_role` NOT NULL default 'pi', CHECK ck_users_user_role present.
- **Old code against new schema**: verified healthy + HTTP 200 BEFORE restarting — the
  additive-migration safety property demonstrated in production, not just argued.
- **Deploy**: `up -d blackbird-app worker` (named services only; NO bare up, NO
  --remove-orphans). postgres reported "Running", not recreated. Healthy in ~3s.
  Zero ERROR/Traceback lines in app or worker logs.
- **Behavioural route proof**: all six /manager/* -> 302 to /login (registered + gated);
  /manager/.../llm-calls -> **404**; /manager/nonexistent -> 404 (so 404 is the
  unregistered response, making those 302s meaningful). /admin/* and /profile -> 302.
  Only /api/health and /login are anonymously 200.
  NOTE: a flat `app.routes` scan shows 0 manager routes — this FastAPI version wraps
  included routers as `_IncludedRouter`, so nested paths are invisible to that scan.
  Introspection artifact, not a deployment defect; the HTTP evidence is authoritative.
- **org1 untouched**: copi-python-* uptimes still 2/3/10/10/10 days (nothing restarted),
  agent-run (org1's PRODUCTION sim) still Up 3 days, copi.science HTTP 200, org1 alembic
  still 0024.
- **Simulation NOT started**: no running blackbird-agent-run; the stopped one
  (Exited 137, 2 days ago) deliberately left in place — `--remove-orphans` was refused
  even though compose suggested it.

## Rollback — SUPERSEDED, WAS WRONG (kept for the record)

> The procedure below inverts the safe order and was corrected after an independent
> audit. Do not follow it. See "Rollback — CORRECTED" at the end of this file.

`docker run --rm --network copi-blackbird_default --env-file .env \
  copi-blackbird-blackbird-app:latest alembic downgrade 0027` then redeploy the prior
image. Data-preserving: 0028's downgrade restores is_admin from user_role. Full dump above.

---

## Independent post-deploy audit — response

An independent auditor re-verified this deployment read-only. It reproduced every data and
schema claim exactly (counts 65/64/65/50/0, **0 backfill mismatches across all 65 rows**,
both defaults + CHECK, current==heads==0028, 0029 absent from the image, image byte-identical
to HEAD across 148 files, org1 untouched at 0024, backup valid with 189 TOC entries).

**It falsified two claims I made. Both are corrected here.**

1. **"`/manager/**/llm-calls` returns 404" was stated too broadly.** `/manager/pis/llm-calls`
   actually returns **302**, because `/manager/pis/{user_id}` swallows `llm-calls` as a path
   parameter. My specific probe (`/manager/activity/<uuid>/llm-calls` -> 404) was valid, but
   the general claim was not.
2. **"404 is the unregistered response, so those 302s are meaningful" was unsound reasoning.**
   `/manager/pis/export` is unregistered yet returns 302, for the same path-param reason.
   The conclusion still holds — the auditor proved it properly by enumerating
   `manager.router.routes`: exactly **7 GET routes, all gated by `get_staff_user`**, and no
   llm-calls route among them. Route enumeration, not status-code probing, is the sound test.

**Actions taken in response**
- Rollback images **tagged** so a `docker image prune` cannot destroy the rollback path
  (they were dangling/untagged): `copi-blackbird-blackbird-app:rollback-pre0028`
  (sha256:0c1b4c7079819bef0913a90bfbd6c94f1e425928ce8349fa3427f46c4d22532b) and
  `copi-blackbird-worker:rollback-pre0028` (sha256:0574ca5723807e6ea22f37c9dffeacd5510d80b18b7c8a5bba4b9dc21a01f588).
  New images also tagged `:post0028` so re-tagging `:latest` cannot orphan them.
- **Agent image rebuilt.** It was stale (no manager router, chain ended at 0027, still mapped
  `is_admin` as a column) and would have hard-broken once 0029 drops that column. Now current:
  `is_admin` unmapped, `user_role` mapped, 0029 absent. **Not started** — `agent` sits behind a
  compose profile, so it cannot start without `--profile agent`.

## Rollback — CORRECTED

The original order was backwards in exactly the way the deploy order had been: it downgraded
the schema while the running image still mapped `user_role`, which would raise
`UndefinedColumn` on every request, login included.

There is also a non-obvious constraint: **the pre-0028 image's migration chain stops at 0027**
(verified), so it cannot execute the 0028->0027 downgrade. The downgrade script exists only in
the new image. So old code must SERVE while alembic runs FROM the new image.

    DC="docker compose -f docker-compose.prod.yml"
    # 1. Put the OLD code in front first (it does not map user_role, so the extra
    #    column is harmless while it serves).
    docker tag copi-blackbird-blackbird-app:rollback-pre0028 copi-blackbird-blackbird-app:latest
    docker tag copi-blackbird-worker:rollback-pre0028 copi-blackbird-worker:latest
    $DC up -d --no-build blackbird-app worker

    # 2. THEN downgrade, running alembic from the POST image (the only one with 0028).
    #    Isolated network so it never joins copi-edge and never takes production traffic.
    docker run --rm --network copi-blackbird_default --env-file .env \
      copi-blackbird-blackbird-app:post0028 alembic downgrade 0027

    # 3. Verify: revision back to 0027, and is_admin repopulated from user_role.
    docker exec -e PGPASSWORD=<POSTGRES_PASSWORD> copi-blackbird-postgres-1 \
      psql -U copi -d copi -t -A -c \
      "SELECT version_num FROM alembic_version; SELECT count(*) FROM users WHERE is_admin;"

Last resort, if the schema is damaged rather than merely ahead: restore the pre-0028 dump in
`backups/` (pg_restore into a clean database). That dump predates all DDL from this deploy.

## Rollback — correction to the corrected procedure

The step-1 `docker tag ...:rollback-pre0028 ...:latest` retag would orphan the only
reference to the CURRENTLY RUNNING build. Tag the running build FIRST. As of the latest
deploy this is already done: `copi-blackbird-{blackbird-app,worker,agent}:5961bc5` point at
the live images. Note `:post0028` predates BOTH hotfixes (c6cca1e impersonation banner,
5961bc5 manager nav), so it is NOT a valid forward target — use `:5961bc5`.
