# Production deploy log — PI/manager/admin account types (0028)

Host runs TWO stacks. Every claim below was verified live, not from memory.

## Ownership map (measured via docker inspect labels)

| Container | Project | Owner |
|---|---|---|
| copi-blackbird-app-1, copi-blackbird-worker-1, copi-blackbird-postgres-1 | copi-blackbird | THIS repo |
| copi-python-{app,worker,grantbot,nginx,certbot,postgres}-1 | copi-python | org1 — DO NOT TOUCH |
| **agent-run** | **copi-python** | **org1's PRODUCTION simulation — DO NOT TOUCH** |

No `blackbird-agent-run` exists; this repo's simulation is not running.

## Confirmed hazards and the mitigations chosen

**H1 — bare `up -d` would collide with org1's ingress.** This repo's compose defines
`nginx` binding 80:80/443:443 (docker-compose.prod.yml:118-150), and org1's
copi-python-nginx-1 currently holds those ports. Mitigation: name services explicitly
(`up -d blackbird-app worker`); never bare `up -d`, never `--remove-orphans`.

**H2 — `compose run` would steal production traffic.** blackbird-app joins the shared
`copi-edge` network and Compose adds the SERVICE NAME as a network alias. Verified:
org1's nginx has `upstream blackbird_app { server blackbird-app:8000; }` and proxies
blackbird.copi.science to it; copi-blackbird-app-1 holds alias `blackbird-app` on
copi-edge. A `docker compose run blackbird-app alembic ...` container would join
copi-edge under the SAME alias, so nginx DNS could round-robin production requests into
a container running alembic rather than uvicorn -> 502s.
Mitigation: run the migration with a plain `docker run` attached ONLY to
`copi-blackbird_default` (where postgres lives, alias `postgres`), never copi-edge.

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

## Rollback
`docker run --rm --network copi-blackbird_default --env-file .env \
  copi-blackbird-blackbird-app:latest alembic downgrade 0027` then redeploy the prior
image. Data-preserving: 0028's downgrade restores is_admin from user_role. Full dump above.
