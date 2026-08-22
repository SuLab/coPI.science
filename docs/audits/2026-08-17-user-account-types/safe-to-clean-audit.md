# Safe-to-reclaim audit — two-stack host, 2026-08-17

**Posture:** adversarial. Everything proposed as "safe" was treated as load-bearing until
disproved by measurement. **Nothing was deleted or modified.** The only writes performed
were this file and four throwaway `docker run --rm postgres:15 pg_restore -l` invocations
(volume count 8 before, 8 after — verified no leak).

**Scope note:** compose project `copi-python` (org1, `copi.science`) and its container
`agent-run` were treated as untouchable throughout. Ownership was confirmed by label on
every container before any inspection.

---

## 0. Headline

Two of the operator's three framing numbers are wrong, and one is wrong in a way that
matters:

| Operator's figure | Measured | Verdict |
|---|---|---|
| overlay2 ≈ 44.7 GB / 622 dirs | **36,163 MB / 623 dirs** | 44,766 MB **double-counts the `merged/` overlay mountpoints** of the 11 running containers (~8,603 MB of phantom bytes). `du --exclude=merged` gives the true figure. |
| 79 dirs (64-hex) = image layers ≈ 10,588 MB | 68 hex + 11 `-init` = **1,593 MB** real | Same `merged/` inflation: the 64-hex set contains the 11 live container r/w layers, whose `merged/` shows the whole container rootfs. |
| 543 short base32 dirs = BuildKit cache ≈ 34,569 MB | **Only 440 dirs / 18,505 MB is cache.** 103 base32 dirs (~16 GB) are live image layers. | **Wrong premise.** Directory-name format does *not* distinguish cache from image layers. |

The name-format heuristic fails because moby's BuildKit graphdriver worker names **image**
layers with the same 25-character `identity.NewID()` scheme it uses for cache snapshots.
Every one of the 543 base-32 dirs is registered in `/var/lib/docker/image/overlay2/layerdb`
(600 cache-ids total: 543 base32 + 57 hex, all resolving to an existing directory, zero
broken references). The correct discriminator is **reachability from an image's
`GraphDriver.Data.LowerDir`/`UpperDir` chain**, which was verified complete against
`len .RootFS.Layers` for a sample of images (11 = 11, 11 = 11, 14 = 14).

### Ground-truth overlay2 decomposition (measured, `du -sm --exclude=merged`)

| Category | dirs | MB |
|---|---:|---:|
| Layers reachable from a **tagged/kept** image | 92 | 7,173 |
| Layers reachable **only from the 50 dangling images** | 68 | **10,455** |
| Layers in layerdb but reachable from **no image** — true BuildKit cache | 440 | **18,505** |
| Container r/w + `-init` + the `l` symlink dir | 23 | 30 |
| **Total** | **623** | **36,163** |

Cross-check: 7,173 + 10,455 = 17,628 = exactly the measured "reachable from ≥1 image"
total. `/var` measured 40,443 MB with `du --one-file-system`, and 36,163 (overlay2) +
3,000 (volumes) + 128 (image db) + 65 (containers) + 65 (buildkit) + 481 (/var/log) +
385 (apt) ≈ 40,287 — consistent. `df` reports 49 G used of 61 G, 13 G free; the
per-directory sums add to ~49.8 G. The measurements are internally consistent.

### Where Docker's own numbers can and cannot be trusted

- **`docker system df` "Images 15.96 GB"** — *understates* disk. It sums deduplicated
  layer `size` metadata, not allocated blocks. Actual image-layer bytes on disk: 17,628 MB.
- **`docker system df` "Build Cache 30.56 GB / 15.69 GB reclaimable"** — *overstates* total
  and *understates* reclaimable. The 30.56 GB counts snapshots that are simultaneously
  image layers (they are literally the same directories), so most of it is double-counted
  against the Images row. Measured cache-only bytes: 18,505 MB.
- **`docker system df` "Local Volumes 3.143 GB / 534.5 MB reclaimable"** — this one is
  approximately right: measured 3,000 MB total, 514 MB in unreferenced volumes.
- **Trustworthy:** `docker ps`, `docker inspect` (labels, `GraphDriver.Data`,
  `RootFS.Layers`, `Mounts`), `docker volume ls/inspect`, `docker images -a`. Every
  conclusion below rests on these plus direct `du`, not on `system df`.

The operator's earlier experience (195 dangling images pruned → only 984 MB freed) is the
expected consequence of the same effect: those images' layers were shared with images that
survived. The 10,455 MB figure below is *exclusive* — computed as
`layers(dangling) \ layers(everything else)` — so it is not subject to that error.

---

## 1. BuildKit cache — **SAFE, 18,505 MB**

`docker builder prune -a` (a plain `builder prune` was already run and freed 2.89 GB).

**What is actually lost:** rebuild speed only. The 440 directories in this set are
referenced by *no* image (verified against the LowerDir/UpperDir chain of all 76 images)
and by *no* container (all 11 containers' layers are in the "image-reachable" or
"container r/w" sets). Removing them means the next `docker compose build` re-runs
`apt-get install gcc libpq-dev` and `pip install .` from scratch instead of hitting cache.
No image is invalidated, no container is disturbed, no data is touched.

**Does it drop cache "still associated with existing images"?** No — and this is the point
the framing gets backwards. Cache records that *are* associated with an existing image
point at a layer the image store also holds a reference to. Releasing the BuildKit
reference decrements a refcount; the image's reference keeps the directory alive. That is
precisely why the recoverable figure is 18,505 MB and not 34,569 MB.

**Effect on org1:** their next `docker compose build` is slower (measured base image
`python:3.11-slim` is 124 MB and is a *pulled* image, unaffected). No running container,
no volume, no image, and no rollback tag of theirs is touched. `docker builder prune` is
daemon-global, so it does affect org1 — but only in that one way.

Ancillary: `/var/lib/docker/buildkit` is 65 MB of metadata (`metadata_v2.db` 50 MB,
`containerdmeta.db` 8 MB, `history.db` 8 MB). Not counted in the figure above; these are
rewritten in place, not necessarily shrunk.

---

## 2. Remaining dangling images (50) — **SAFE, 10,455 MB**

All 50 carry `com.docker.compose.project` labels; 40 are `copi-blackbird`, 10 are
`copi-python`. None is used by any container (`docker image prune` would refuse anyway).

### The one that looked dangerous — and why it is not

`15582b083371` (created **2026-08-15T11:14:25.283391987Z**, label
`com.docker.compose.service=agent`, 1.19 GB) is the **pre-0028 agent build** — the third
member of the build round whose app and worker images carry
`copi-blackbird-blackbird-app:rollback-pre0028` and `copi-blackbird-worker:rollback-pre0028`
(both created at the identical timestamp). It is untagged. Rolling `0028` back requires
reverting the agent too: the agent maps `users.user_role` exactly as the web tier does, so
a post-0028 agent against a rolled-back schema raises `UndefinedColumn` on every
`select(User)`.

Measured, however:

```
15582b083371  RootFS.Layers md5 = dd83dd9b6efa429389cd79ffc9aae3df
0c1b4c707981  RootFS.Layers md5 = dd83dd9b6efa429389cd79ffc9aae3df   (:rollback-pre0028 app)
0574ca572380  RootFS.Layers md5 = dd83dd9b6efa429389cd79ffc9aae3df   (:rollback-pre0028 worker)
```

The three images are **byte-identical in content** — one compose build round emits one
layer set and three image configs differing only in the compose service label. Exclusive
layer size of `15582b083371`: **0 MB**. Rollback capability is fully preserved by the two
tagged images, and the agent is launched with an explicit command
(`python -m src.agent.main`), so the image's `CMD`/labels are irrelevant to it.

**Recommendation before pruning** (records the intent, costs 0 bytes):

```bash
docker tag copi-blackbird-blackbird-app:rollback-pre0028 copi-blackbird-agent:rollback-pre0028
```

### org1's rollback targets are all tag-protected

`/home/ubuntu/copi-backups/pre-deploy-20260814-150646/ROLLBACK.txt` names four rollback
image IDs. All four are tagged `:pre-35ce7ea`:

| ROLLBACK.txt entry | image ID | tag today |
|---|---|---|
| copi-python-grantbot | `7d3c0386278b` | `copi-python-grantbot:pre-35ce7ea` |
| copi-python-app | `9eb8a6f0a39c` | `copi-python-app:pre-35ce7ea` |
| copi-python-agent | `c1ea348def71` | `copi-python-agent:pre-35ce7ea` |
| copi-python-worker | `ab4a04c8f0ce` | `copi-python-worker:pre-35ce7ea` |

No org1 rollback target is dangling. `docker image prune` (dangling-only, **not** `-a`)
cannot touch them.

**Verdict: SAFE — 10,455 MB.** Do the `docker tag` above first.

---

## 3. Tagged-but-old images — **NEEDS-OWNER-DECISION, 1,224 MB incremental**

Removing all four *in addition to* the dangling set frees 11,679 MB total, i.e. only
**1,224 MB more** than the dangling set alone. Per-image exclusive sizes:

| Image | Age | Exclusive MB | Live reference? |
|---|---|---:|---|
| `copi-prod-app:latest` | 4 mo | **429** | None. No compose file on this host declares project `copi-prod`; the only other trace is the unreferenced 46 MB `copi-prod_pgdata` volume. Dead intent — but org1-lineage, so their call. |
| `copi-blackbird-app:latest` | 3 wk | **278** | Name is still live: `docker-compose.yml:18` (the *dev* stack) declares service `app` under project `copi-blackbird`, which produces exactly this tag. The image is stale; `docker compose up --build` rebuilds it, and a bare `up` builds it if missing. Safe to drop if nobody runs the dev stack. |
| `blackbird-test:latest` | 12 d | **214** | Referenced only in `.superpowers/sdd/2026-08-05-*/task-*-report.md` (completed workspace records) and `.superpowers/sdd/2026-08-05-conversations-cohort-scope-and-threads/run-tests.sh:64`. `CLAUDE.md` documents that in-container pytest does not work and that the supported gate is `.venv-test`. Dead intent. |
| `copi-blackbird-grantbot:latest` | 12 d | **0** | `grantbot` is **not** a service in `docker-compose.prod.yml` (services: postgres, blackbird-app, worker, agent, nginx, certbot). No grantbot container exists. Dead intent, but removing it frees nothing — all its layers are shared with the Aug-5 image set. |

A tag implies intent; here three of four intents are demonstrably dead. But `copi-prod-app`
belongs to the other operator's lineage, so this whole block is **NEEDS-DECISION** rather
than SAFE.

---

## 4. Backups — mostly **UNSAFE / KEEP**. Only 0 MB is unambiguously redundant.

All dumps were validated with `pg_restore -l` inside a throwaway `postgres:15` container
(not assumed from the `PGDMP` magic bytes, though those check out too).

| File | Size | `pg_restore -l` | Verdict |
|---|---:|---|---|
| `backups/copi_pre0028_20260817_203346.dump` | 69 MB | valid, 189 TOC, **30 TABLE DATA**, PG 15.17 | **KEEP** — live rollback point for the deployed 0028. |
| `backups/copi_pre0027_20260815_111249.dump` | 69 MB | valid, 183 TOC, **29 TABLE DATA** | **KEEP** — see below. |
| `backups/copi_predeploy_20260814_203331.dump` | 69 MB | valid, 186 TOC, **30 TABLE DATA** | **KEEP** — sole full copy of `grantbot_posted_foas`. |
| `blackbird-backups/20260811-.../copi.dump` | 69 MB | valid, 186 TOC, 30 TABLE DATA | KEEP. |
| `blackbird-backups/2026080*` (3 dumps) | 1.4 MB | valid | KEEP (negligible). |
| `/home/ubuntu/copi-backups/**` | 1,387 MB | not ours | **UNSAFE for us** — org1's live rollback set. |

### Why the older dumps are *not* redundant — the decisive measurement

Live production database, read-only query just now:

```
alembic                 = 0028
agent_messages          = 161 rows,  min(created_at) = 2026-08-17 22:48:06+00
llm_call_logs           = 3,946 rows, min(created_at) = 2026-07-24 15:08:57+00
users                   = 65
opportunity_assessments = 18
public tables           = 30
```

`agent_messages` starts at **22:48 today** while `llm_call_logs` reaches back to July 24.
That is the signature of a `--fresh` run: `--fresh` wipes `agent_messages`/`channels` and
keeps everything else. **Every conversation generation before 2026-08-17 22:48 exists only
inside these dumps** — and `CLAUDE.md` is explicit that "the DB, not Slack, is the durable
store". A later dump is therefore *not* a superset of an earlier one.

Second, non-obvious dependency: diffing the dumps' TABLE DATA entries,

- `grantbot_posted_foas` is present in the **Aug-14** dump and **absent** from Aug-15,
  Aug-17, and the live DB. The Aug-14 dump is its only full-fidelity copy (the 2,644-byte
  `backups/grantbot_posted_foas_20260814T203857.sql` is the only other trace).
- `assessment_drops` appears first in the Aug-17 dump (added by 0028).

**Recoverable here: 0 MB unambiguously.** If the owner explicitly accepts losing all
pre-2026-08-17 conversation history *and* `grantbot_posted_foas`, the two superseded dumps
are 144 MB — that is a data-retention decision, not a disk decision.

`blackbird-backups/20260811-.../env.bak` (6,329 B, mode 600) and org1's
`copi-python-repo.tar.gz` (containing `.env` and TLS keys, mode 600) are secret-bearing;
neither should be moved to less-protected storage as part of any cleanup.

---

## 5. `.venv-test` (477 MB) — **NEEDS-DECISION**, and it exposed a much bigger problem

**Is it needed by the gate?** Yes, directly. `scripts/ci.sh:34` hard-codes
`VENV_PY="${VENV_PY:-$REPO_ROOT/.venv-test/bin/python}"`, and lines 84-87 abort the entire
gate if that interpreter is missing. Every step — `alembic heads`, the
upgrade→downgrade→upgrade round trip (191-193), both `ruff` invocations (205, 223) and
`pytest` (255) — runs through it. It is regenerable in one command
(`uv venv .venv-test && uv pip install --python .venv-test/bin/python -e '.[dev]'`), but
deleting it disables the pre-push gate until that command is re-run.

**Does removing it free space given it is baked into images?** Deleting the host directory
frees 477 MB on disk immediately; it does **not** shrink existing images. But the premise
of the question turned out to be the most important finding in this audit:

### There is no `.dockerignore`, and `Dockerfile:20` is `COPY . .`

Verified **inside the running production web container** (`copi-blackbird-app-1`, image
`30030f052d81` = `:5961bc5`):

```
477 MB  /app/.venv-test
208 MB  /app/backups        <- three complete production DB dumps
 24 MB  /app/.git
  7 MB  /app/logs
-rw------- 1 root root 6329  /app/.env    <- every production secret
```

Consequences:

1. **Space.** ~716 MB of dead weight is baked into *every* image this repo builds, times
   three service images per round (they share layers, so ~716 MB per *round*). This fully
   explains the image-size progression measured across build rounds: 582 MB (Aug 5, before
   `.venv-test` existed) → 1.02 GB (Aug 6) → 1.11 GB (Aug 14) → 1.19 GB (Aug 15) → 1.26 GB
   (Aug 17, after the third dump landed in `backups/`). Adding a `.dockerignore`
   (`.venv-test`, `backups`, `.git`, `logs`, `.ruff_cache`, `.superpowers`, `.env`) is the
   single highest-leverage space change available here — it removes ~716 MB from every
   future image and shrinks the build context, which also shrinks future BuildKit cache.
2. **Security.** Three complete copies of the production database, and the production
   `.env`, sit in a distributable image layer. `docker save`/registry push of any of these
   images ships them. This is out of scope for a disk audit but should not wait for one.

**Verdict on `.venv-test` itself: NEEDS-DECISION, 477 MB** — safe to delete only if the
operator accepts re-creating it before the next `git push`. The `.dockerignore` fix is the
recommendation that actually matters.

---

## 6. Everything else found

| Item | MB | Verdict |
|---|---:|---|
| `/home/ubuntu/.local/share/claude/versions/` — 4 CLI versions; `~/.local/bin/claude` symlinks to `2.1.234` (314 MB), which `claude --version` confirms is current. The other three (2.1.233, 2.1.232, 2.1.227) are superseded. | **910** | **SAFE** |
| systemd journal — 438.8 MB, and `/etc/systemd/journald.conf:27` leaves `SystemMaxUse` **commented out**, so it grows unbounded. `journalctl --vacuum-size=200M` reclaims ~239 MB and setting a cap prevents recurrence. | **239** | **SAFE** |
| `/var/cache/apt` — `apt-get clean` | **192** | **SAFE** |
| `~/.cache/pip` | **173** | **SAFE** |
| Unreferenced Docker volumes: `collab-platform_mongodb_data` 418, `copi_pgdata` 48, `copi-prod_pgdata` 46, `copi-python_grantbot_data` <1, `collab-platform_redis_data` <1 | 514 | **NEEDS-DECISION** — none is ours. `collab-platform` is a separate Feb-2026 project with a live git repo and compose file at `/home/ubuntu/collab-platform`; the other four are org1/predecessor lineage. |
| `/var/lib/snapd/snaps` — two revisions each of `amazon-ssm-agent`, `core22`, `snapd` | ~160 | **NEEDS-DECISION** — old revisions are the system's own rollback. |
| `/tmp` 63 MB, repo `logs/` 7 MB, `/boot` second kernel | ~250 | Leave. Trivial or system rollback. |
| `/swapfile` | 2,048 | **UNSAFE** — do not touch. |
| Core dumps: `/var/crash` and `/var/lib/systemd/coredump` both **empty**. No orphaned tarballs found by `find / -xdev -size +80M`. | 0 | Nothing to reclaim. |

### The 8 remaining volumes — every one accounted for

| Volume | MB | Links | Verdict |
|---|---:|---:|---|
| `copi-python_pgdata` | 2,284 | `copi-python-postgres-1` | **UNSAFE — org1's live production database.** |
| `copi-blackbird_pgdata` | 207 | `copi-blackbird-postgres-1` | **UNSAFE — our live production database.** |
| `4a675ea246bc…` (anonymous) | <1 | `copi-python-certbot-1` | **UNSAFE — in use by org1's live certbot.** |
| `collab-platform_mongodb_data` | 418 | 0 | NEEDS-DECISION (third project). |
| `copi_pgdata` | 48 | 0 | NEEDS-DECISION (predecessor). |
| `copi-prod_pgdata` | 46 | 0 | NEEDS-DECISION (predecessor). |
| `copi-python_grantbot_data` | <1 | 0 | NEEDS-DECISION (org1's project label). |
| `collab-platform_redis_data` | <1 | 0 | NEEDS-DECISION (third project). |

**Was anything of value lost in the earlier removal of 81 anonymous volumes?** I cannot
prove a negative about deleted data, and I will not claim to. What I *can* establish:
every `Mounts` entry of all 11 containers was enumerated and every named-volume reference
resolves to an existing volume (3 OK, 0 MISS); all remaining compose-labelled volumes are
intact; both production databases are up and answering queries. The strongest available
evidence says nothing referenced was lost — but the contents of the 81 are unrecoverable
and unverifiable. **This is the one item in this audit I could not determine.**

---

## 7. Verification of the two fixes

### Fix 1 — `scripts/ci.sh:152`, `docker rm -f` → `docker rm -fv`

**CORRECT, and complete for `ci.sh`.** The container is created at line 172 with
`docker run -d --name "$MIGCHECK_CONTAINER" ... postgres:15` — no `--rm`. `postgres:15`
declares `VOLUME /var/lib/postgresql/data`, so each run created a ~48 MB anonymous volume
that `docker rm -f` (no `-v`) orphaned. `-v` removes it. Committed as `9e70522`; working
tree clean. The trap covers `EXIT INT TERM` and `migcheck_cleanup` is also called up
front, so a killed run cannot leave one behind either.

**Empirical confirmation of the underlying semantics:** four throwaway
`docker run --rm postgres:15 pg_restore -l` invocations during this audit left the volume
count unchanged at 8 — `--rm` does remove anonymous volumes, `docker rm` without `-v` does
not. That is the exact asymmetry the fix addresses.

**Other volume-leak paths in this repo — none found.**
- `tests/conftest.py` / testcontainers: `DockerContainer.stop()` defaults to
  `delete_volume=True` and calls `container.remove(force=force, v=delete_volume)`
  (`.venv-test/lib/python3.12/site-packages/testcontainers/core/container.py:292-294`).
  No leak on clean shutdown; the ryuk reaper covers the killed-process case.
- `scripts/export_agent_roster.py:13` uses `docker run --rm` — no leak. **But** it
  hard-codes `--network copi-python_default`, i.e. **org1's network**, inside this repo's
  script. Separate cross-stack hazard; flagged below.
- `tests/e2e/README.md:43,50` — `docker compose run -d --name app-8002 …` with no `--rm`
  and no documented cleanup. Leaks containers (and any anonymous volumes) *and* resolves
  to the dev compose file. Low severity (test doc), but it is the same class of bug.
- `scripts/migrate/run_migration.sh:184,191,196` and `scripts/mutate_slack_mirror.sh:83,92`
  are `docker compose exec … rm -f <path>` — in-container file deletes, not container
  removal. Not a leak.

### Fix 2 — `scripts/provision_slack_bots.py`

**CORRECT.** Lines 499-510 now print `docker rm blackbird-agent-run` and
`docker compose -f docker-compose.prod.yml --profile agent run -d …`, with a comment
explaining that the unprefixed name belongs to project `copi-python`. Committed as
`9e70522`; working tree clean.

### Three of your four remaining hazards are false positives

I read all of them:

- **`scripts/migrate/preflight.py:1566`** — already emits
  `"  docker stop -t 30 blackbird-agent-run"`, preceded by a three-line comment stating
  that the unprefixed name belongs to the other deployment. **Not a hazard.**
- **`scripts/migrate/preflight.py:1714`** — same; emits
  `docker stop -t 30 blackbird-agent-run && docker compose -f docker-compose.prod.yml stop …`
  with the comment "the unprefixed `agent-run` is org1's container". **Not a hazard.**
- **`scripts/migrate/run_migration.sh:264`** — emits
  `"Stop the writers (docker stop -t 30 blackbird-agent-run) and re-run."` **Not a hazard.**
- **`scripts/migrate/run_migration.sh:325`** — emits
  `"10. Start blackbird-agent-run last (NOT agent-run — that is org1's)."` This is a
  *warning*, not an instruction to run the wrong thing. **Not a hazard.**

A `grep -F 'agent-run'` matches these because `blackbird-agent-run` contains the substring.
A negative-lookbehind grep (`(?<!blackbird-)agent-run`) is what separates real hits.

- **`docs/production-migration.md`** — this one *is* a live hazard, but your line numbers
  are off by ~18. The actual bare-`agent-run` commands are at **210, 213, 477, and 542**
  (not 192/459/524). Mitigating: lines 3-18 carry a 16-line `⚠️ STOP` banner that names
  every one of the three dangers by name (unprefixed `agent-run` is org1's, bare
  `docker compose` is the dev file, never `--remove-orphans`).

### The instance you missed — and it is the worst one

**`README.md:64-88`.** It has **no warning of any kind**: `grep -iE 'org1|two stack|copi-python|unprefixed|blackbird-agent-run|docker-compose.prod'` over `README.md` returns
**zero** matches. It prescribes, as a complete copy-pasteable block:

```bash
docker compose --profile agent run -d --name agent-run agent \
  python -m src.agent.main --budget 0
...
docker logs agent-run > logs/run_$(date +%s).log 2>&1
docker stop -t 30 agent-run
docker rm agent-run
docker compose up -d --build app worker
```

Every line is wrong on this host: bare `docker compose` resolves to the dev
`docker-compose.yml`; `agent-run` is org1's live production simulation; `app` is the dev
service name, not `blackbird-app`. `docker rm agent-run` on line 82 is the exact command
`CLAUDE.md` exists to prevent. `README.md:55` additionally still instructs
`docker compose exec app python -m pytest tests/ -v`, which `CLAUDE.md` documents as
non-functional (no `[dev]` extra in the image). The block also advertises `--budget`,
which `CLAUDE.md` marks deprecated.

### Ranking by how likely an operator is to actually follow it during an incident

| Rank | Location | Why |
|---|---|---|
| **1** | `README.md:64-88` (+ `:55`) | First file anyone reads. **No warning anywhere in the file.** Self-contained copy-paste block. `docker rm agent-run` stops org1's production run, and `docker compose up -d --build app worker` operates the wrong stack. Highest probability × highest blast radius. |
| **2** | `docs/production-migration.md:542` | The "if you need to get back to where you started" rollback block. An operator in a failed-migration panic jumps straight here and will not scroll 530 lines back to the banner. Contains `docker stop -t 30 agent-run` **and** `docker compose stop app worker`. |
| **3** | `docs/production-migration.md:210, 213, 477` | Same file, but reached by reading forward from the banner, so the warning is likely to have been seen. |
| **4** | `tests/e2e/README.md:43,50` | Bare `docker compose run -d --name app-8002/app-8003` — wrong stack and leaks containers. Test-only doc, low incident traffic. |
| **5** | `scripts/export_agent_roster.py:13` | Hard-codes `--network copi-python_default` (org1's network) in this repo's own script. Read-only in effect today, but it is this repo reaching into org1's namespace by default. |

`docker-compose.yml:52`, `CLAUDE.md:60-63`, `docs/blackbird-star-topology-runbook.md:51,294`,
`docs/plans/*`, `docs/audits/*` and `templates/admin/_cohort_gate_banner.html` also contain
the string but are warnings, prose, or UI copy — not instructions. 128 total occurrences
were reviewed.

---

## 8. Totals

### Unambiguously safe to reclaim: **30,474 MB (≈ 29.8 GB)**

| Item | MB |
|---|---:|
| BuildKit cache-only layers (`docker builder prune -a`) | 18,505 |
| Layers exclusive to the 50 dangling images (`docker image prune`, after the one `docker tag`) | 10,455 |
| Superseded Claude CLI versions (2.1.233 / 2.1.232 / 2.1.227) | 910 |
| systemd journal above a 200 MB cap | 239 |
| apt cache | 192 |
| pip cache | 173 |
| **Total** | **30,474** |

That takes the root filesystem from 13 G free / 80% used to roughly **42.8 G free / ~30%
used**, without deleting a single byte of database, backup, secret, or rollback artefact.

### Requires an owner decision: **~3,762 MB**

| Item | MB | Whose call |
|---|---:|---|
| `/home/ubuntu/copi-backups` (org1's rollback set: 677 MB `copi-db.dump`, 672 MB `copi_pre0024.dump`, git bundle, repo tarball with secrets) | 1,387 | **org1 only** |
| 4 stale tagged images (incremental over the dangling set) | 1,224 | ours, except `copi-prod-app` |
| `.venv-test` (regenerable, but `ci.sh` aborts without it) | 477 | ours |
| Unreferenced volumes (`collab-platform_mongodb_data` 418 + 4 others) | 514 | third-party / org1 |
| Old snap revisions | ~160 | system rollback |

### Must be kept (measured, for completeness): ~12,026 MB

Live volumes 2,491 · kept-image layers 7,173 · container r/w layers 30 · repo `backups/`
208 · `blackbird-backups/` 76 · swapfile 2,048.

### Things I could not determine

1. **Whether the 81 already-deleted anonymous volumes contained anything of value.** They
   are gone; no ledger of their contents survives. All *surviving* references resolve
   cleanly and both databases are healthy, but that is circumstantial, not proof.
2. **Whether the Aug-14 and Aug-15 dumps' `agent_messages` generations are genuinely
   distinct from each other.** I established that both differ from the live DB (which was
   reset at 22:48 today) and that Aug-14 uniquely contains `grantbot_posted_foas`.
   Determining whether Aug-15 is a strict superset of Aug-14 would require restoring both
   into scratch databases — out of scope for a read-only audit, and the answer only
   matters if the owner wants to delete one.
3. **Whether `/home/ubuntu/collab-platform`'s 418 MB Mongo volume is wanted.** The project
   has a live git repo and compose file but no containers and no activity since Feb 2026.
   Its owner is not identified in anything on this host.

### Recommended order of operations

```bash
# 0. Preserve the pre-0028 agent rollback name (0 bytes, prevents a real regret)
docker tag copi-blackbird-blackbird-app:rollback-pre0028 copi-blackbird-agent:rollback-pre0028

# 1. Dangling images only — NEVER `-a`, which would take the :pre-35ce7ea and
#    :rollback-pre0028 tags org1 and we depend on.
docker image prune            # ~10,455 MB

# 2. Build cache — daemon-global, slows org1's next build and nothing else.
docker builder prune -a       # ~18,505 MB

# 3. Host-level, no Docker involvement
rm -rf ~/.local/share/claude/versions/2.1.{227,232,233}   # ~910 MB
sudo journalctl --vacuum-size=200M                        # ~239 MB
sudo apt-get clean                                        # ~192 MB
rm -rf ~/.cache/pip                                       # ~173 MB
```

Then, independent of disk pressure, add a `.dockerignore` — it is worth more than any
single line above, because it stops the 716 MB/round regrowth *and* removes three copies
of the production database and the production `.env` from every image.
