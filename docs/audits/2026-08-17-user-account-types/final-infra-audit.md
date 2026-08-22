# Final infrastructure & provenance audit — 0028 account-types deploy

Read-only adversarial audit of what is *actually deployed* on this host, whether it
matches source, and what is left dangerous. Scope: infrastructure and provenance only
(application security is covered by a separate auditor). Every claim below was measured,
not recalled; where a measurement contradicts an earlier document it is called out.

**Audit window: 2026-08-17 ~21:25–21:50 UTC.** Production was rebuilt and redeployed
*twice* during this window by another party (see F3). All statements are timestamped
because the target moved.

---

## Verdict

**The deployment is functionally correct and recoverable, but it is not in a clean or
safe steady state.** The schema, data and running code are right; both sites serve 200;
org1 is untouched; backups are genuinely restorable. Against that: the root filesystem is
at **96% (2.8 GB free)** and holds *both* production databases, the deployed commit exists
on **no remote**, and every production image contains the live `.env` and 208 MB of full
production database dumps.

None of these is currently causing an outage. Two of them (disk, provenance) will cause
one if nothing changes.

---

## Findings

### C1 — Root filesystem at 96%; both production Postgres volumes live on it — CRITICAL

```
/dev/root  61G  59G  2.8G  96% /
/var/lib/docker  48G   (overlay2 41G, volumes 6.6G)
```

Free space fell from 3.5 GB to 2.8 GB **during this audit**, consumed by two image
rebuilds. Each rebuild of this repo writes ~1.26 GB of new layers (see C2 for why the
images are twice the size they need to be). At the observed hotfix cadence that is a
handful of rebuilds from zero.

`copi-blackbird_pgdata` and org1's pgdata are both under `/var/lib/docker/volumes`. When
the volume fills, **both** copi.science and blackbird.copi.science lose write capability
at the same moment, and Postgres may refuse to start again until space is freed.

Reclaimable right now, measured:

| Category | Total | Reclaimable |
|---|---|---|
| Images (274, 10 active) | 34.42 GB | **25.67 GB** |
| Build cache (592 entries, **0 active**) | 33.45 GB | 1.48 GB conservative; effectively all of it via `builder prune -af` |
| Local volumes (89, 3 active) | 7.07 GB | **4.46 GB** (86 dangling testcontainers volumes, ~49 MB each) |

Safely reclaimable without touching either production stack: dangling testcontainers
volumes, the build cache (0 active), and untagged images **not referenced by a running or
stopped container**. Note the two exceptions that must be preserved deliberately:
`742f40d006f1` (held by the stopped `blackbird-agent-run`, see M6) and the untagged images
of any prior production build you still want as a rollback target (see H4). A blanket
`docker image prune -a` or `docker system prune -a` is **not** safe here — it would also
delete org1's `:pre-35ce7ea` rollback tags' underlying layers and any untagged build you
have not tagged.

### C2 — Live `.env` (28 secrets) and 208 MB of production DB dumps are baked into every image — CRITICAL

`Dockerfile:18` is `COPY . .` and there is **no `.dockerignore`**. Measured inside
`copi-blackbird-blackbird-app`: **20,523 files under `/app`**, against 397 files tracked at
HEAD.

What ships in every production image:

| Path in image | Size | Contents |
|---|---|---|
| `/app/.env` | 6.3 KB | 28 populated secrets |
| `/app/backups/` | **208 MB** | 3 full `pg_dump -Fc` of production + `profiles_predeploy*.tgz` + 34 per-agent memory files |
| `/app/.git/` | 24 MB | full history, all branches |
| `/app/.venv-test/` | **477 MB** | host test virtualenv |
| `/app/logs/` | 6.2 MB | agent run transcripts |
| `/app/.superpowers/`, `/app/.ruff_cache/`, `/app/build/`, `/app/.coverage` | — | scratch |

Secret names present and populated in the baked `.env`: `ANTHROPIC_API_KEY`, `SECRET_KEY`,
`POSTGRES_PASSWORD`, `ORCID_CLIENT_SECRET`, `ORCID_CLIENT_ID`, `SLACK_CONFIG_TOKEN`,
`SLACK_CONFIG_REFRESH_TOKEN`, `NCBI_API_KEY`, `PATENTSVIEW_API_KEY`, `USPTO_API_KEY`,
`POSTHOG_API_KEY`, `DATABASE_URL`.

The container runs as **root** (no `USER` directive), and `/app/.env` is mode 600 root-owned
— which is no protection when the application process *is* root.

Mitigating: these images are local-only build artifacts (`copi-blackbird-*`, no registry
prefix) and have never been pushed. The exposure is latent, not realised. It becomes real
the moment anyone runs `docker save`/`docker push`, shares the image, or achieves code
execution in the container — at which point it is simultaneously a full credential
disclosure *and* a full database disclosure, because the dumps are right there.

This is also the direct cause of C1's burn rate: the pre-bloat image was 561 MB
(`copi-blackbird-app:latest`, 3 weeks old); it is now 1.26 GB.

Fix is a one-line `.dockerignore` (`.git`, `.venv-test`, `backups`, `logs`, `.env`,
`.superpowers`, caches). Note that removing `.env` from the image is safe — the running
services get their environment from compose `env_file:`, not from the baked copy.

### H3 — The deployed commit exists on no remote — HIGH

| | |
|---|---|
| Local `blackbird` HEAD | **`5961bc5`** "fix(web): show the Manager nav link while impersonating a manager" |
| `origin/blackbird` | `c6cca1e` |
| Running app image | `30030f052d81` = build of `5961bc5` |

`git branch -r --contains HEAD` → **empty**. Production is running a commit that is on this
host and nowhere else. If the host is lost, the running code cannot be reconstructed from
origin.

Timeline measured during the audit:

- 21:21:33 — containers started on `a44b0bd3cf8b` (build of `c6cca1e`)
- ~21:35 — `5961bc5` committed; images rebuilt, `:latest` moved
- 21:40:54 — containers recreated on `30030f052d81` (build of `5961bc5`)

Byte-verification against the *current* running image (`30030f052d81`), all 397 tracked
files at HEAD: **394 identical**. The 3 that differ — `.gitignore`,
`docker-compose.prod.yml`, `new_orcids.txt` — are exactly the three uncommitted
working-tree files, which `COPY . .` copies in their working-tree form. Same result held
for the previous image `a44b0bd3cf8b` against `c6cca1e` (394/397).

### H4 — The rollback ladder has a hole, and it silently reverts both hotfixes — HIGH

Tagged rollback targets:

| Tag | Image | Code |
|---|---|---|
| `:rollback-pre0028` | `0c1b4c707981` | pre-0028 (chain ends 0027) |
| `:post0028` | `77ecf8d080bb` | 0028, **before both impersonation hotfixes** |
| `:latest` | `30030f052d81` | current (`5961bc5`) |

There is **no tag for the `c6cca1e` build**. It is now dangling (`RepoTags: []`) and is only
protected from `docker image prune` by nothing at all — no container references it any more.

Consequence: the documented rollback tags `:rollback-pre0028` onto `:latest`, which orphans
the *current* image the same way. A later roll-forward via `:post0028` lands on code that
predates `c6cca1e` **and** `5961bc5` — silently undoing both impersonation fixes with no
error. Recommend immutable per-commit tags (`:c6cca1e`, `:5961bc5`) rather than moving
`:latest`, and tagging the current build before any rollback is attempted.

### H5 — No automated backups; dumps live only on the 96%-full disk — HIGH

`crontab -l` is empty of jobs; `systemctl list-timers` shows only OS timers (`certbot`,
`logrotate`, `sysstat`, `fwupd`, `dpkg-db-backup`) — **no database backup timer**. Every
dump in `backups/` was taken by hand as part of a deploy. There is no rotation policy and
no offsite copy: the 208 MB of dumps sit on the same filesystem as the database they back
up, at 96% utilisation. A disk-full or volume-loss event takes the database and its
backups together.

`/home/ubuntu/blackbird-backups/` holds four dated dirs, newest **2026-08-11** — already
stale relative to the deploy.

### M6 — The stale `blackbird-agent-run` trap: both halves confirmed — MEDIUM

Confirmed exactly as suspected.

```
blackbird-agent-run   Exited (137) 2 days ago   image=742f40d006f1
```

`742f40d006f1` is **untagged**, built `2026-08-14T21:57:33Z` — before the merge. Measured
inside it: alembic chain ends at **`0026`** (two migrations behind), `src/routers/manager.py`
**ABSENT**, no `user_role` in the models. The current agent image is `b67f1e40dea8`
(`copi-blackbird-agent:latest`) — an entirely different image.

Both halves hold:

1. `docker compose --profile agent run -d --name blackbird-agent-run agent ...` — the name
   is held by the exited container, so Docker refuses with a name conflict. The documented
   start command **fails as written**.
2. `docker start blackbird-agent-run` — succeeds, and resurrects pre-merge code *silently*.
   It would not even crash: the old code does not map `user_role`, and 0028 is additive, so
   it runs happily against the new schema while lacking every account-types change.

Loaded-gun context: **63 agents are `status='active'` with a `slack_bot_token`** (1 inactive).
Anything that starts an agent container puts 63 bots live immediately.

**Correct start procedure now** — remove the stale container first, and only then start,
having rebuilt the agent image:

```bash
DC="docker compose -f docker-compose.prod.yml"
docker inspect blackbird-agent-run \
  --format '{{index .Config.Labels "com.docker.compose.project"}}'   # MUST print copi-blackbird
docker rm blackbird-agent-run          # exited already; no docker stop needed. NEVER `docker rm agent-run`.
$DC --profile agent build agent        # src/ is baked in; skipping this deploys stale code
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

Do **not** use `--remove-orphans` to clear the name; it has previously killed org1's nginx
and certbot. `docker rm` of that one specific, verified-owned container is the safe path.

### M7 — The deployed compose file is uncommitted; the committed one does not work — MEDIUM

`docker-compose.prod.yml` is modified in the working tree and has never been committed. The
*committed* version, which is what anyone cloning `origin/blackbird` receives:

- names the web service **`app`**, not `blackbird-app`. The deployed file's own comment
  explains why this matters: Compose adds the service name as a network alias on every
  attached network, so an `app` alias on `copi-edge` collides with org1's `app` and org1's
  nginx upstream would resolve to the wrong container.
- uses the **awslogs** logging driver, which per the operator's own notes fails on this
  host for lack of IAM permissions.
- **lacks the `copi-edge` network block entirely**, so the container never joins the network
  org1's nginx proxies over — blackbird.copi.science would simply not resolve.
- lacks `container_name: copi-blackbird-app-1`, breaking every documented `docker exec`.

Real severity: **for disaster recovery this is the blocking finding.** A rebuild from the
pushed branch alone reproduces neither the topology nor a serving stack, and the first
failure mode it *would* hit (`app` alias on copi-edge) is the one that takes down the other
production tenant. The mitigation is trivial — commit the file — and its absence is the
single largest gap between "the repo" and "the system".

`.gitignore` (adds `.superpowers/`) and `new_orcids.txt` (six new ORCIDs replacing the old
Moore entry) are also uncommitted; both are benign, but `new_orcids.txt` is deploy input
that is now baked into the image in a state no commit records.

### M8 — blackbird's TLS depends on org1's stack — MEDIUM

org1's nginx (`/home/ubuntu/copi-python/nginx/nginx.conf:246-291`) terminates TLS for
blackbird.copi.science:

```
upstream blackbird_app { server blackbird-app:8000; }
ssl_certificate     /etc/letsencrypt/live/blackbird.copi.science/fullchain.pem;
```

The certificate lives in **org1's** certbot volume and is renewed by **org1's** certbot
container. blackbird's own compose defines `nginx` and `certbot` services that are not
running and have no `./certbot` directory on disk. So: tearing down, rebuilding or
re-volumising the `copi-python` stack silently breaks blackbird's HTTPS, and blackbird's
repo contains no means to re-issue its own certificate.

Current cert is valid `notBefore=Jul 24 2026`, `notAfter=Oct 22 2026`.

Related, and it lowers the severity of the recorded H1 hazard: a bare
`docker compose -f docker-compose.prod.yml up -d` on blackbird would try to start its own
`nginx` on 80/443, which org1 already holds — so it **fails to bind rather than stealing
the ports**. It does not take org1 down. It would, however, create root-owned empty
`./certbot/conf` and `./certbot/www` directories. `--remove-orphans` remains the genuinely
destructive flag.

### L9 — Public-repo information disclosure — LOW

`SuLab/coPI.science` is **PUBLIC** (`isPrivate: false`). The pushed
`docs/audits/2026-08-17-user-account-types/deploy-log.md` publishes: both production admins
by full name and ORCID, the complete admin/manager URL surface, the database user and name
(`copi`/`copi`), container names, the pgdata volume path, image digests, and the rollback
procedure.

No credentials are exposed — scanning the pushed range `b1de2ec..c6cca1e` found only a
`<POSTGRES_PASSWORD>` placeholder and `example.org`/`example.edu` test fixtures. `.env`,
`*.dump`, `*.pem` and `backups/*` have **never** been committed on any branch, and
`.gitignore` covers `.env`, `backups/`, `data/`, `.venv-test/`. This is reconnaissance
material, not a leak.

### L10 — `worker` has no healthcheck — LOW

`blackbird-app` and `postgres` both define healthchecks; `worker` defines none. Its entire
log output since the 21:40 redeploy is one line (`Worker started, polling every 5s`). A
wedged or crash-looping worker is therefore invisible to `docker ps` — it reports "Up"
regardless. `restart: unless-stopped` is set, so a hard exit recovers, but a hung poll loop
does not.

### L11 — Test database on the production Postgres instance — LOW

`copi_sdd` (11 MB) sits alongside `copi` (136 MB) on `copi-blackbird-postgres-1`. Harmless
today; it is exactly the footgun `CLAUDE.md` warns about when `TEST_DATABASE_URL` is
pointed at the wrong name.

### L12 — `profiles/` is root-owned, untracked and unbacked — LOW

`./profiles` is bind-mounted **read-write** into both `blackbird-app` and `worker`, and
`./profiles`, `./prompts`, `./data` into `agent`. The tree is root-owned (written by
containers running as root), contains 64 public profiles, is neither tracked nor
gitignored, and its only backup is a manual `profiles_predeploy_20260814T204701.tgz`. Host
tooling run as `ubuntu` cannot write to it. `profiles/private/blackbird.md` is present,
untracked, and — per `CLAUDE.md` — unread at runtime; it still has not been archived and
diffed against the tracked rubric text.

---

## Verification of the deploy log's specific claims

### Backups — VALID (an earlier negative result of mine was a bad method)

My first attempt piped the dumps into `pg_restore --list` via `/dev/stdin` and got
`did not find magic string in file header` for all three. **That was my invocation, not
corruption** — recorded here so nobody repeats it. `pg_restore` 15 also requires `-f -` to
write a schema to stdout; without it, `--schema-only` silently produces zero lines and
exits 1, which briefly looked like an empty dump.

Verified correctly, by read-only mount:

| Dump | Format | Tables | `user_role` present | alembic |
|---|---|---|---|---|
| `copi_pre0028_20260817_203346.dump` | CUSTOM, 189 TOC entries, 30 TABLE DATA | 30 | **no** | **0027** |
| `copi_pre0027_20260815_111249.dump` | CUSTOM | 29 | no | — |
| `copi_predeploy_20260814_203331.dump` | CUSTOM | 30 | no | — |

All three carry the `PGDMP` magic, restore a full schema *and* data, and the pre-0028 dump
is genuinely pre-migration. The rollback data path is sound.

### The pre-0028 image cannot run the 0028 downgrade — CONFIRMED

`copi-blackbird-blackbird-app:rollback-pre0028` (`0c1b4c707981`): `alembic/versions/` ends at
`0027_add_assessment_drops.py`. `0028` is physically absent, so it cannot execute
`downgrade 0027`. The corrected procedure's insistence on running alembic from
`:post0028` while the old image serves is correct and load-bearing.

### The corrected rollback procedure — sound except for the tagging step

Checked mechanically:

- `--env-file .env` works. `.env` line 53 is
  `DATABASE_URL=postgresql+asyncpg://copi:***@postgres:5432/copi`, which resolves on
  `copi-blackbird_default` where postgres holds the `postgres` alias. There are **no**
  quoted values and **no** values containing spaces in `.env`, so Docker's `--env-file`
  (which, unlike compose, does not strip quotes or interpolate) will not mangle anything.
  Verified by inspection of all 28 assignments.
- 0028's `downgrade()` does `UPDATE users SET is_admin = (user_role = 'admin')` *before*
  dropping the column, so it is data-preserving as claimed. Current data would survive:
  `is_admin` is already perfectly consistent with `user_role` (see below).
- Step 3's verification `psql -c "SELECT ...; SELECT ...;"` is valid — psql executes
  multiple statements in one `-c`.
- **The error is step 1**: `docker tag ...:rollback-pre0028 ...:latest` orphans the current
  image, which after F4 is the only copy of the running build. Tag the current build first.

### Schema and data state — CORRECT

```
alembic_version                = 0028
users.user_role   varchar NOT NULL DEFAULT 'pi'
users.is_admin    boolean NOT NULL DEFAULT false     (the F14 fix is live)
```

Role distribution, and legacy-column consistency:

| user_role | access_status | count | | is_admin | user_role | count |
|---|---|---|---|---|---|---|
| admin | allowed | 2 | | t | admin | 2 |
| manager | allowed | 1 | | f | manager | 1 |
| pi | allowed | 62 | | f | pi | 62 |

Zero mismatches across all 65 rows. Two `allowed` admins exist, so the last-admin demotion
guard is not at its floor. `0029_drop_is_admin` is **absent** from this branch's
`alembic/versions/` and present only on `feat/user-account-types-0029` — the two-phase split
holds.

---

## Claims falsified

### deploy-log.md line 13: "No `blackbird-agent-run` exists" — WRONG

It exists, exited, holding the name and pinned to a pre-merge image (M6). The same document
contradicts itself at lines 87-89, which correctly describe the stopped container as
"deliberately left in place". Line 13 should be struck.

### deploy-log.md H2: "`compose run` would steal production traffic" — NOT SUPPORTED

H2 asserts that a `docker compose run blackbird-app alembic ...` container "would join
copi-edge under the SAME alias", letting nginx round-robin production requests into it.

Measured on the two one-off containers actually present, on Compose **2.37.1**:

```
blackbird-agent-run   net=copi-blackbird_default  aliases=[blackbird-agent-run]
agent-run             net=copi-python_default     aliases=[agent-run]
```

One-off (`oneoff=True`) containers receive **only their container name** as a network alias
— never the service name. By contrast the long-running service container does get both:

```
copi-blackbird-app-1  copi-edge  aliases=[copi-blackbird-app-1 blackbird-app]
```

So a one-off `blackbird-app` container would join `copi-edge` (the service declares that
network) but would **not** hold the `blackbird-app` alias, and nginx's
`upstream blackbird_app { server blackbird-app:8000; }` could not resolve to it. The
traffic-steal scenario does not occur.

Caveat on the evidence: both observed one-offs were started with an explicit `--name`, which
sets the alias to that name. Without `--name` the alias would be the generated
`<project>-<service>-run-<hash>` — still not the service name.

The mitigation chosen (plain `docker run` on `copi-blackbird_default` only) was harmless and
remains good practice for an unrelated reason: it keeps migration containers off the shared
network entirely. But the hazard as written is false, and it is now published in a public
runbook where it will be cited as fact.

---

## Claims reproduced

**"org1 was not impacted" — REPRODUCED, and residual risk is low but real.**
All seven `copi-python` containers show `RestartCount=0` with uptimes (2, 2, 3, 3, 11, 11,
11 days) that straddle both of today's blackbird deploys — nothing was recreated or
restarted. `agent-run` (org1's production simulation, project `copi-python`) is still up 3
days. copi.science and copi.science/login both return 200. `copi-edge` contains exactly two
members, `copi-python-nginx-1` and `copi-blackbird-app-1`, and blackbird's alias set
(`copi-blackbird-app-1`, `blackbird-app`) does not intersect org1's `app`.

Residual coupling that remains: the shared `copi-edge` network (org1's nginx is reachable
from blackbird's app container and vice versa); org1's sole ownership of ports 80/443 and of
blackbird's TLS material (M8); `--remove-orphans` on either project; and above all the
shared Docker daemon and its **single 96%-full disk** (C1), which is the one failure that
takes down both tenants simultaneously.

**"The simulation is not running" — REPRODUCED.**
No running agent container in project `copi-blackbird`. Corroborated from the database
rather than from `docker ps` alone: `agent_messages` holds 50 rows with
`max(created_at) = 2026-08-14 22:30:38+00`, matching `blackbird-agent-run`'s
`FinishedAt = 2026-08-14T22:31:33Z`. Nothing has been written in three days.

**"The push was safe" — REPRODUCED.**
`origin/blackbird = c6cca1e`, `origin/copi-prod = 364bee3`, `origin/main = b7edcbc`.
`git merge-base --is-ancestor c6cca1e origin/copi-prod` → false: org1's deploy branch does
not contain the push. There is **no `.github/workflows/` directory** on any branch, so no
push-triggered CI or deploy exists to act on it (consistent with `CLAUDE.md`: "there is no
server-side CI"). Secret and PII scans of the pushed range are clean (L9).

**"All running containers run code byte-identical to branch HEAD" — REPRODUCED, with
caveats about what the method could not see.**
Independently confirmed 394/397 tracked files identical, twice (against `c6cca1e` for the
21:21 image, against `5961bc5` for the 21:40 image). The 171-file figure is exactly the
count of tracked files under `src/ templates/ static/ alembic/ prompts/` at that commit —
the arithmetic checks out.

Everything the five-directory comparison omitted, I checked, and **none of it differs**:
`pyproject.toml`, `alembic.ini`, `Dockerfile`, `docker-compose.yml`, `nginx/nginx.conf`,
`orcids.txt`, and all 18 `scripts/*` files are byte-identical to HEAD. App, worker and agent
images are byte-identical to each other across all runtime paths. No container is running a
stale image — the *only* stale artifact on the host is the stopped `blackbird-agent-run`
(M6). The worker does run the image believed: `copi-blackbird-worker` `ce945f038bfe`, same
build as the app.

Three things the method structurally could not have caught, offered as what to check next
time rather than as defects:

1. **A shadow copy of `src/` exists in site-packages.** `Dockerfile:12-14` does
   `COPY src/ src/` then `pip install .`, installing `/usr/local/lib/python3.11/site-packages/src`
   *in addition to* `/app/src`. Verified byte-identical here, and `python -c "import src.main"`
   from `/app` resolves to `/app/src/main.py` because cwd precedes site-packages on
   `sys.path`. It is latent, not active: any invocation from a different working directory,
   or with `-I`/`-P`, would import the installed copy instead — which would be silently
   stale after any future edit-without-rebuild.
2. **`prompts/` and `profiles/` are bind-mounted at runtime, so the image copy is not what
   runs.** Comparing them inside the image proves nothing about production. Checked
   separately: host `prompts/` is clean against HEAD, so the two agree today; host
   `profiles/` is untracked entirely (L12) and has no source of truth to compare against.
3. **The comparison was scoped to tracked files**, so it could not surface the 20,126
   untracked files that `COPY . .` also ships — including `.env` and the database dumps (C2).

Finally, the claim is inherently perishable: it was true of `c6cca1e` when made, was false
for roughly five minutes around 21:35, and is true of `5961bc5` now — but only against a
local commit that no remote has (H3).

---

## Recommended order of action

1. **Free disk** (C1) — `docker builder prune -af`, then dangling testcontainers volumes.
   Do *not* use blanket `system prune -a`; it would take org1's rollback layers and the
   image pinned by `blackbird-agent-run`.
2. **Push `5961bc5`** (H3), and tag the running images immutably per commit (H4).
3. **Add `.dockerignore`** and rebuild (C2) — this also roughly halves image size, which
   directly relieves C1.
4. **Commit `docker-compose.prod.yml`** (M7) — the DR blocker.
5. Rotate anything in `.env` if any image was ever exported or shared (C2).
6. Automate a `pg_dump` with rotation and an offsite copy (H5).
7. Strike deploy-log.md line 13 and correct H2 (falsified above).
8. `docker rm blackbird-agent-run` before the next simulation start (M6).
