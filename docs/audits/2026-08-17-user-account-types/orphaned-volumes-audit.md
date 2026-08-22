# Adversarial audit: all 86 orphaned Docker volumes

**Date:** 2026-08-17 · **Posture:** read-only. No volume was deleted, pruned, mounted,
started against a container, or written to. Every content determination below was made by
reading files on disk under `/var/lib/docker/volumes/*/_data` with `ls`/`du`/`stat`/
`strings`. No `pytest`, no `ci.sh`, no `docker run`.

**Bottom line:** the preliminary classification is right about the bulk of the disk
(the ~3.7 GB of anonymous Postgres volumes are throwaway CI artifacts) but **wrong about
its mechanism**, and **wrong about `copi_pgdata`, which holds real, unbacked-up, single-copy
application data.** It also missed two orphans entirely and mis-stated several dates.

---

## 1. Inventory reconciliation

| | Count | Size |
|---|---|---|
| Volumes on host | 89 | 7.069 GB (docker) |
| Attached to a running container | 3 | — |
| **Orphaned (`docker volume ls -f dangling=true`)** | **86** | **4,373,660 KB = 4,271.2 MiB = 4.48 GB** |

The 3 attached volumes are `copi-blackbird_pgdata` (202 MB, this stack's prod Postgres),
`copi-python_pgdata` (2,284 MB, org1's prod Postgres), and anonymous
`4a675ea246bc…c64e` mounted at `/var/lib/letsencrypt` in `copi-python-certbot-1`.

**There are zero stopped/created containers on this host** (`docker ps -a --filter
status=exited --filter status=created` → 0), so no stopped container definition holds a
claim on any orphan.

The 86 orphans break down as:

| Class | Count | MiB | Verdict |
|---|---|---|---|
| Anonymous `copi_migcheck` Postgres 15 clusters (CI leak) | 80 | 3,760.8 | **SAFE** |
| Anonymous empty volume `7c56304a…e3d` | 1 | 0.004 | **SAFE** |
| `copi-prod_pgdata` — empty Postgres 15 cluster | 1 | 45.6 | **SAFE** |
| `copi_pgdata` — **live data of `/home/ubuntu/coPI`** | 1 | 47.7 | **DO NOT DELETE** |
| `collab-platform_mongodb_data` — real MongoDB data | 1 | 417.1 | **DO NOT DELETE** |
| `collab-platform_redis_data` — Celery broker crumbs | 1 | 0.008 | safe, but 0 gain; third-party |
| `copi-python_grantbot_data` — **org1's state files** | 1 | 0.012 | **DO NOT TOUCH (org1)** |

---

## 2. Claim 1 — "81 anonymous volumes are testcontainers pytest artifacts"

**Verdict: the disposability conclusion holds. The stated mechanism, count, and date range
are all wrong.**

### 2a. They are not testcontainers. They are `scripts/ci.sh`.

Every one of the 80 Postgres-shaped anonymous volumes contains exactly one non-template
database, and its name is **`copi_migcheck`** (read out of the `pg_database` heap,
`global/1262`). That name appears in exactly one place in either repo:

- `/home/ubuntu/blackbird-copi-science/scripts/ci.sh:173`
- `/home/ubuntu/copi-python/scripts/ci.sh:171`

```bash
docker run -d --name "$MIGCHECK_CONTAINER" \
  -e POSTGRES_USER=copi -e POSTGRES_PASSWORD=copi -e POSTGRES_DB=copi_migcheck \
  -p "127.0.0.1:${MIGCHECK_PORT}:5432" postgres:15
```

This is the alembic upgrade→downgrade→upgrade round trip, **not** the pytest session
database. The pytest path is `tests/conftest.py:37`:

```python
with PostgresContainer("postgres:15", dbname="copi_test") as pg:
```

**There is not a single orphaned volume whose database is named `copi_test`.** Testcontainers'
Ryuk reaper is cleaning up after itself correctly; pytest is not leaking anything. The leak is
one missing flag in `ci.sh`:

```bash
migcheck_cleanup() { docker rm -f "$MIGCHECK_CONTAINER" >/dev/null 2>&1 || true; }
```

`docker rm -f` without `-v` destroys the container and orphans its anonymous
`/var/lib/postgresql/data` volume. `ci.sh` calls `migcheck_cleanup` **twice** per run (once
up-front at line 171, once after the round trip at line 197) plus an `EXIT/INT/TERM` trap —
so one leaked ~47 MB volume per `./scripts/ci.sh` invocation. 80 leaked volumes ≈ 80 CI runs.
**Adding `-v` to that one line stops the bleeding permanently.**

### 2b. Count and dates

- **80**, not 81, are `copi_migcheck` Postgres clusters. The 81st anonymous orphan is
  `7c56304ac2491b…e3d`, created 2026-03-27 22:38, and it is **completely empty** (0 entries,
  4 KB) — almost certainly org1's first certbot `/var/lib/letsencrypt` volume, superseded by
  `4a675ea…` on 2026-04-03.
- The dates are **Aug 6 (16), Aug 7 (10), Aug 14 (14), Aug 15 (31), Aug 17 (9)**. The
  preliminary classification omitted **Aug 14 entirely (14 volumes, 658 MB)**.

### 2c. Falsification attempt: is real data hiding in any of them?

I checked **all 80**, not a sample. Every one is identical in structure and none holds rows:

| Probe | Result across all 80 |
|---|---|
| `PG_VERSION` | `15` — uniform. No stray version. |
| `base/` subdirectories | exactly `1, 4, 5, 16384` — no extra database anywhere |
| Database name | `copi_migcheck` — uniform. No default `postgres`-only, no custom name |
| `base/16384` total | 8,924–9,036 KB (5 discrete values, tracking alembic head at that date) |
| **Largest relation with relfilenode > 16384** | **16,384 bytes — in every single volume** |
| WAL segments in `pg_wal/` | **1** — uniform |
| `postmaster.pid` present | yes, all 80 (killed by `docker rm -f`, never shut down cleanly) |
| File mtimes | all within the same minute as volume creation |

The last row of that table is the decisive one. A relation capped at 16 KB is **two 8 KB
heap pages** — that is `alembic_version` holding one row, and nothing else. The largest file
in each `base/16384` is `1255` (`pg_proc`, a system catalog) at 786,432 bytes. **These are
schemas with zero application rows.** The 8,924→9,036 KB spread is not data; it is the
catalog growing as migrations `0025`→`0028` added tables between Aug 6 and Aug 17.

### 2d. Could they be org1's rather than this repo's?

**Yes, some of them almost certainly are, and it does not matter.** Both repos ship a
byte-equivalent `ci.sh` using the same container name (`copi-ci-migcheck`) and the same
database name (`copi_migcheck`), and both are checked out on this host. Attribution per
volume is not recoverable from the volume contents — the schema is the shared CoPI schema.
But the classification is identical either way: for **both** repos this is a throwaway
migration-round-trip cluster that the script itself intends to destroy on exit. Neither
repo's runbooks, deploy scripts, or docs reference these volumes. Deleting them cannot
affect org1 beyond making its next `ci.sh` run start from a fresh `initdb`, which is what it
does anyway.

**Claim 1 verdict: SAFE — 3,760.8 MiB across 80 volumes, plus 0.004 MiB for the empty
`7c56304a…`.**

---

## 3. Claim 2 — "`copi_pgdata` is an empty early-deployment cluster, disposable"

### **FALSIFIED. This volume holds real, single-copy application data. Do not delete it.**

Nearly every factual premise of the claim is wrong:

| Claim | Reality |
|---|---|
| "48 MB, Postgres, early CoPI deployment" | Postgres **16** (`PG_VERSION` = 16). Both current stacks run Postgres 15. |
| "last written 2026-02-23" | `base/16384` files carry mtimes up to **2026-03-27 22:33**; `pg_wal` to 22:36. Feb 23 is the *initdb* date. |
| "project `copi` is neither current project name" | True, and that is the point: it is **`/home/ubuntu/coPI`**, the original TypeScript/Prisma CoPI prototype. Compose normalises the directory `coPI` → project `copi`. |
| "empty/near-empty, disposable" | **353 files in `base/16384` vs 299 in a genuinely empty cluster.** Real user relations with real content. |

### Ownership proof

`/home/ubuntu/coPI/docker-compose.yml`:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: copi
      POSTGRES_DB: copi
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
```

`postgres:16-alpine` matches `PG_VERSION` = 16; `POSTGRES_DB: copi` matches the single
non-template database found in `global/1262`; the volume `pgdata` under project `copi`
resolves to exactly `copi_pgdata`. **A plain `docker compose up` in `/home/ubuntu/coPI`
would reattach this volume today.** That satisfies the "referenced on disk such that someone
intends to reattach it" test outright.

### Content proof

`pg_class` (`base/16384/1259`) yields a **Prisma** schema, not SQLAlchemy/Alembic:
`_prisma_migrations`, `affiliation_selections`, `collaboration_proposals`,
`match_pool_entries`, `matches`, `matching_results`, `jobs` — matching the eleven models in
`/home/ubuntu/coPI/prisma/schema.prisma` (`User`, `ResearcherProfile`, `Publication`,
`MatchPoolEntry`, `AffiliationSelection`, `CollaborationProposal`, `Swipe`, `Match`,
`MatchingResult`, `Job`, `SurveyResponse`).

User relations, with mtimes spread across **Feb 23 → Mar 27** (a month of live use, not a
single initdb instant):

```
475136 bytes  16468  Mar 27 22:33
 81920 bytes  16491  Feb 24 06:04
 40960 bytes  16474  Feb 27 21:40
 32768 bytes  16496  Feb 24 06:04
 … plus free-space maps on five separate relations
```

`strings` on relation `16468` returns a **PubMed literature corpus** — DOIs, PMIDs, PMCIDs,
journal names and full abstracts (CIViC, *Molecular Cell*, *Journal of Immunology*,
*Bioinformatics Advances*, the Biomedical Data Translator release paper).

`strings` on relation `16491` returns **LLM-generated collaboration proposals** naming real
PIs, tagged `claude-opus-4-20250514`, e.g. *"Validating Knowledge Graph-Predicted Covalent
Inhibitor Mechanisms via ABPP"* (Su × Cravatt), *"Striatal Ensemble Dynamics Knowledge Mining
for Antipsychotic Mechanism Discovery"* (Su × Kennedy), each with complementarity rationale,
proposed first experiments, and success metrics.

### No backup exists

Every `.dump` on this host is a **`pg_dump` of the SQLAlchemy CoPI schema** (copi-python /
blackbird):
`/home/ubuntu/copi-backups/{20260814T134641Z/copi_pre0024.dump, pre-deploy-20260814-150646/copi-db.dump}`,
`/home/ubuntu/blackbird-copi-science/backups/copi_pre{0027,0028}_*.dump`,
`/home/ubuntu/blackbird-backups/*/blackbird-db.dump`.
**Not one is a dump of the Prisma database.** No `.sql`, `.bson`, or `.archive` of it exists
anywhere under `/home/ubuntu`. And it cannot be reconstructed from copi-python's database —
that is a different schema for a different application generation.

**Claim 2 verdict: UNSAFE. 47.7 MiB. This is the only copy of a month of the original coPI
prototype's output. Recommended action: `pg_dump` it (offline, from a read-only copy of the
directory) before anyone even considers reclaiming 47 MB.**

---

## 4. Claim 3 — "`collab-platform_mongodb_data` must not be deleted without owner consent"

**Verdict: CONFIRMED, and the reasoning is sound — but the loss profile is different from what
the 418 MB figure suggests.**

Content determination (`/home/ubuntu/collab-platform/docker-compose.yml` sets
`MONGO_INITDB_DATABASE=collab_platform`; `_mdb_catalog.wt` confirms databases
`collab_platform` and `orcid`):

- `collection-26-…wt` (2.9 MB, the largest) holds **generated collaboration proposals** with
  fields `from_orcid`, `source_document_id`, `collaboration_type`, `complementarity_*` — real
  narrative text naming real labs (Miller, Diercks) and their techniques.
- `collection-7-…wt` (446 KB) holds **researcher profiles**: real ORCID iDs
  (`0000-0001-9116-5465`, `0000-0003-3569-6231`), affiliation strings
  ("Assistant professor, Immunology and Microbiology, Scripps Research Institute"),
  `disease_areas` (hereditary breast and ovarian cancer, cancer prevention, healthcare
  disparities, immunotherapy), `deep_mode_pmids`, `custom_data_*`.

This is **personal data about identifiable researchers**, not test fixtures.

**Size nuance the raw 418 MB hides:**

| Component | MiB |
|---|---|
| `journal/` (WiredTiger write-ahead log) | 301 |
| `diagnostic.data/` (FTDC telemetry ring buffer) | 113 |
| **`*.wt` collection + index files — the actual payload** | **~5** |

So the irreplaceable content is roughly **5 MiB**; the other ~414 MiB is journal and
telemetry. That does **not** make the volume disposable — you cannot delete the volume
without deleting the payload — but it does mean *anyone hoping to recover 418 MB here is
recovering 414 MB of log files and destroying a scientific dataset to do it.* Dumping the
5 MB and then deleting is the sane path, and requires the owner.

`docker compose up` in `/home/ubuntu/collab-platform` reattaches this volume
(`mongodb_data:/data/db`). No dump of it exists anywhere on the host.

**Claim 3 verdict: UNSAFE. 417.1 MiB. Requires the owner's explicit consent.
`collab-platform_redis_data` (8 KB) is the sibling orphan: `dump.rdb` holds only
`_kombu.binding.celeryev` / `worker.#` Celery broker keys — content-wise disposable, but it
frees 0.008 MiB and belongs to the same third party, so leave it with the Mongo volume.**

---

## 5. Findings beyond the three claims

### 5.1 `copi-prod_pgdata` (45.6 MiB) — missed by the preliminary classification, and SAFE

Postgres 15, project label `copi-prod`, created 2026-04-03 02:33, database `copi`. This one
*is* what claim 2 described:

- **Zero relations with relfilenode > 16384.** `base/16384` has 299 files vs `copi_pgdata`'s 353.
- **Every file carries the same mtime, `Apr 3`** — the cluster was created and never written to again.
- `pg_class` yields only `pg_*` catalogs and `information_schema` views. No application schema at all.
- Clean shutdown (no `postmaster.pid`), 1 WAL segment.

An `initdb` + `createdb copi` that never served a query. `copi-prod` today survives only as a
**git branch name** in both repos (`origin/copi-prod`); no compose file on this host declares
that project. **SAFE to delete: 45.6 MiB.**

### 5.2 `copi-python_grantbot_data` (12 KB) — org1's, DO NOT TOUCH

Labelled `com.docker.compose.project=copi-python`. Contains org1's grantbot dedup state:
`grantbot_last_run.txt` and `grantbot_posted.json` (479 bytes, last written Apr 2). It is
orphaned only because `copi-python-grantbot-1` was later reconfigured to use bind mounts
(`docker inspect` shows it now mounts only `./prompts`, `./data`, `./profiles`). This is the
single orphan bearing org1's project label. It frees 0.012 MiB. **Do not touch it** — the
rule is ownership, not size, and a careless `docker volume prune` takes it.

### 5.3 TLS material: the prior audit's concern is real but is *not* a volume concern

A prior audit noted blackbird's certificates live in org1's certbot volume. Checked directly:

- The Docker volume attached to `copi-python-certbot-1` is `4a675ea246bc…c64e`, mounted at
  `/var/lib/letsencrypt`, and it is **completely empty** — 0 entries, 4 KB.
- Both certificates live on a **host bind mount**: `/home/ubuntu/copi-python/certbot/conf`,
  containing `live/copi.science`, `live/blackbird.copi.science`, matching `archive/` trees,
  and `renewal/{copi.science,blackbird.copi.science}.conf`.

**No Docker volume on this host holds TLS material.** `docker volume prune` cannot affect
certificates for either domain. The load-bearing coupling is at the *filesystem* level:
blackbird's certificate chain lives inside org1's repo directory, so deleting or moving
`/home/ubuntu/copi-python/certbot/` would break TLS for `blackbird.copi.science`. That is a
real hazard and it should stay documented — but it is out of scope for volume reclamation,
and the certbot volume is attached anyway, so `prune` would skip it regardless.

### 5.4 Backups, secrets, certificates, uploads in volumes?

**None.** Across all 86 orphans there is no `.dump`/`.sql`/`.tar.gz`, no PEM/key material, no
`.env`, no uploaded-file tree. The only non-database orphan content on the entire host is
org1's two grantbot state files (§5.2). All real backups live on the host filesystem
(`/home/ubuntu/{copi-backups,blackbird-backups}`, `blackbird-copi-science/backups/`), not in
volumes.

### 5.5 Cross-repo reference sweep

Grepped both repos (`*.yml`, `*.yaml`, `*.sh`, `*.md`, `*.py`) for `copi_pgdata`,
`copi-prod`, `collab-platform`, `mongodb_data`, `redis_data`, `grantbot_data`, and
`external: true`:

- No hit declares any orphaned volume. The `external: true` in both `docker-compose.prod.yml`
  files is the **`copi-edge` network**, not a volume.
- `copi-prod` hits are the git branch, not the compose project.
- The only on-disk reattachment paths are the two **third-party** compose files:
  `/home/ubuntu/coPI/docker-compose.yml` → `copi_pgdata`, and
  `/home/ubuntu/collab-platform/docker-compose.yml` → `collab-platform_{mongodb,redis}_data`.
  Both are exactly the volumes flagged unsafe.

---

## 6. How much space is actually freed

| Bucket | KB | MiB | MB (decimal) |
|---|---:|---:|---:|
| 80 × anonymous `copi_migcheck` clusters | 3,851,040 | 3,760.8 | 3,943.5 |
| Anonymous empty `7c56304a…e3d` | 4 | 0.004 | 0.004 |
| `copi-prod_pgdata` | 46,684 | 45.6 | 47.8 |
| **TOTAL UNAMBIGUOUSLY SAFE (82 volumes)** | **3,897,728** | **3,806.4** | **3,991.3** |
| — held: `copi_pgdata` | 48,848 | 47.7 | 50.0 |
| — held: `collab-platform_mongodb_data` | 427,064 | 417.1 | 437.3 |
| — held: `collab-platform_redis_data` | 8 | 0.008 | 0.008 |
| — held: `copi-python_grantbot_data` (org1) | 12 | 0.012 | 0.012 |
| All 86 orphans | 4,373,660 | 4,271.2 | 4,478.6 |

Disk is currently `53G used / 8.9G avail / 86%` on `/dev/root`. Reclaiming the safe set moves
that to roughly **~49G used / ~12.6G avail / ~80%**.

### Do I trust the number?

**Yes — more than the image figure, and for the reason given.** Docker's own
`Local Volumes … 4.463GB` reclaimable is within **0.4%** of my independently measured
4.4786 GB, which is the agreement you expect when the accounting is honest. Image pruning
mis-estimated by ~25× because layers are shared between images and Docker's `RECLAIMABLE`
counts each layer against every image that references it. **Volumes share nothing** — each
volume is a distinct directory under `/var/lib/docker/volumes/<name>/_data` with no
copy-on-write, no hardlinking between volumes, and no reference counting. `du -sk` on that
directory is the exact number of blocks that `docker volume rm` returns to the filesystem.
Two caveats worth stating: `du` reports allocated blocks (so sparse or reflinked files could
in principle read high — Postgres does not create either), and deleting the *unsafe* set
would free far less usable value than its 417 MiB suggests, since ~414 MiB of that is
MongoDB journal and telemetry (§4).

---

## 7. Recommendations

1. **Fix the leak first, or this recurs at ~47 MB per CI run.** In **both**
   `/home/ubuntu/blackbird-copi-science/scripts/ci.sh:152` and
   `/home/ubuntu/copi-python/scripts/ci.sh:150`, change
   `docker rm -f "$MIGCHECK_CONTAINER"` to `docker rm -f -v "$MIGCHECK_CONTAINER"`.
   This is org1's file too — coordinate before editing theirs.
2. **Never run bare `docker volume prune` on this host.** It takes `copi_pgdata`,
   `collab-platform_mongodb_data`, and org1's `copi-python_grantbot_data` along with the
   safe set. Delete by explicit name only.
3. **Before touching `copi_pgdata` or `collab-platform_mongodb_data`, get the owner's
   consent and take a dump.** Neither has a backup anywhere on this host.
4. Leave `collab-platform_redis_data` alone — deleting it frees 8 KB and touches a
   third party's project.

---

## 8. Complete table — all 86 orphaned volumes

Sorted by creation date. Sizes are `du -sm` MiB of `_data`. "Last modified" is the `_data`
directory mtime. Content type was determined by reading files on disk; the Postgres database
name is read from the `pg_database` heap (`global/1262`), template databases excluded.

| Volume | MiB | Compose project | Created | Last modified | Content |
|---|---:|---|---|---|---|
| `collab-platform_mongodb_data` | 417.1 | collab-platform | 2026-02-06 22:57 | 2026-02-23 07:10 | MongoDB (WiredTiger) |
| `collab-platform_redis_data` | 0.0 | collab-platform | 2026-02-06 22:57 | 2026-02-23 07:10 | Redis RDB |
| `copi_pgdata` | 47.7 | copi | 2026-02-23 06:59 | 2026-03-27 22:36 | Postgres 16 (db: copi ) |
| `7c56304ac2491b74287bfe2f2d179366dcef28caa954904de9a342565e400e3d` | 0.0 | _(anonymous)_ | 2026-03-27 22:38 | 2026-03-27 22:38 | empty |
| `copi-python_grantbot_data` | 0.0 | copi-python | 2026-03-28 13:31 | 2026-03-29 12:49 | other: grantbot_last_run.txt grantbot_posted.json  |
| `copi-prod_pgdata` | 45.6 | copi-prod | 2026-04-03 02:33 | 2026-04-03 02:33 | Postgres 15 (db: copi ) |
| `951126c0e55dfbec498684c0d151b10c2446633112d5e217be0e937e29848f35` | 46.9 | _(anonymous)_ | 2026-08-06 03:17 | 2026-08-06 03:17 | Postgres 15 (db: copi_migcheck ) |
| `8744eae3c614daadb83a2c841193d307892140ba145b9f8c54d7b8ffe8f2f1f6` | 46.9 | _(anonymous)_ | 2026-08-06 04:38 | 2026-08-06 04:38 | Postgres 15 (db: copi_migcheck ) |
| `71440ccdd904f88a6655eb2a3bfce61e7d0bdcfb944e0f1d526cab162dcdb7b7` | 46.9 | _(anonymous)_ | 2026-08-06 05:02 | 2026-08-06 05:02 | Postgres 15 (db: copi_migcheck ) |
| `cbe7bf18bbaa842627593c6f6256fe79c6affe0dd6c2ef04aa651147c2a144eb` | 46.9 | _(anonymous)_ | 2026-08-06 05:11 | 2026-08-06 05:11 | Postgres 15 (db: copi_migcheck ) |
| `122104d68b0c401f21a6f721cb7c03a023650d6de95c88da4bcf2adad93cc5bb` | 46.9 | _(anonymous)_ | 2026-08-06 05:30 | 2026-08-06 05:30 | Postgres 15 (db: copi_migcheck ) |
| `3c49e7d40d532c466844a925f2776f2ef742362bf1822b7b2bbb2057d110cfb5` | 46.9 | _(anonymous)_ | 2026-08-06 05:38 | 2026-08-06 05:38 | Postgres 15 (db: copi_migcheck ) |
| `b05ba56b7a1b0a71b7806a29b3f50a80e8089265dfb38ab07a01caecd315ce2e` | 47.0 | _(anonymous)_ | 2026-08-06 16:05 | 2026-08-06 16:05 | Postgres 15 (db: copi_migcheck ) |
| `a7e3525bdd341649a53e0f21f5dfac50f0955679f1a5a5adc5178b0966fb2e2e` | 47.0 | _(anonymous)_ | 2026-08-06 19:28 | 2026-08-06 19:28 | Postgres 15 (db: copi_migcheck ) |
| `3af92dbc92ba2a5abb02ca7530ecc92801c3a860e7eec1d6185235293854d006` | 47.0 | _(anonymous)_ | 2026-08-06 19:34 | 2026-08-06 19:34 | Postgres 15 (db: copi_migcheck ) |
| `1b7b6f01f17abb05b9536394dc48bea06d477ce4b6c1aae4bb2e96399ca5dbf3` | 47.0 | _(anonymous)_ | 2026-08-06 21:39 | 2026-08-06 21:39 | Postgres 15 (db: copi_migcheck ) |
| `f0b41cd2a35d6cce9e821a871cc8901fa07a5b49a799b4ab88cc1b42f89c891a` | 47.0 | _(anonymous)_ | 2026-08-06 22:08 | 2026-08-06 22:08 | Postgres 15 (db: copi_migcheck ) |
| `64430b1edaaf5666391421bda5a6fcd5fd71d12f490f5e6d723ee70fe761d163` | 47.0 | _(anonymous)_ | 2026-08-06 22:09 | 2026-08-06 22:09 | Postgres 15 (db: copi_migcheck ) |
| `4f7df144cd647ac8e88d9b137ece802be7675343cd508d8843a793ee671d41e7` | 47.0 | _(anonymous)_ | 2026-08-06 22:43 | 2026-08-06 22:43 | Postgres 15 (db: copi_migcheck ) |
| `e16dec192d07d85f1b64ce9dd78748f262df7c67fcc37787742b50dbc2b3fff2` | 47.0 | _(anonymous)_ | 2026-08-06 23:08 | 2026-08-06 23:08 | Postgres 15 (db: copi_migcheck ) |
| `ccfb5db6f029b2a7e7f8c7a42370305b17c5a90aae8306b0c453b1fff53933d0` | 47.0 | _(anonymous)_ | 2026-08-06 23:27 | 2026-08-06 23:27 | Postgres 15 (db: copi_migcheck ) |
| `ff356c30ea0b44109b79e29cece07368ee614624f20f6306affd4b2aba7559de` | 47.0 | _(anonymous)_ | 2026-08-06 23:42 | 2026-08-06 23:42 | Postgres 15 (db: copi_migcheck ) |
| `720819bbe0f673fcb84197368a380482327eaddeccdebf0da814760038427bc9` | 47.0 | _(anonymous)_ | 2026-08-07 01:55 | 2026-08-07 01:55 | Postgres 15 (db: copi_migcheck ) |
| `d245c7554739e3094735b9a61f028fda9787396c098f42a9e796c24cae115a7f` | 47.0 | _(anonymous)_ | 2026-08-07 02:34 | 2026-08-07 02:34 | Postgres 15 (db: copi_migcheck ) |
| `03a28c478a80c1442bbb867317c7d8114bc781c2044b9b7d208cc50af52cb329` | 47.0 | _(anonymous)_ | 2026-08-07 02:40 | 2026-08-07 02:40 | Postgres 15 (db: copi_migcheck ) |
| `04be627b7af96897fe9b7bfa85252c5ab3bbf3bb9205ea1db9da8b09daf09e1e` | 47.0 | _(anonymous)_ | 2026-08-07 12:28 | 2026-08-07 12:28 | Postgres 15 (db: copi_migcheck ) |
| `4acde6654eb482db8d26a435658d55db60f287b8addab5897f7c7403d60fdc71` | 47.0 | _(anonymous)_ | 2026-08-07 12:32 | 2026-08-07 12:32 | Postgres 15 (db: copi_migcheck ) |
| `963e3c6ad24ce6e8c5bb03e62f2209a6ea1b9c228b8af835e4818c35791a7911` | 47.0 | _(anonymous)_ | 2026-08-07 12:42 | 2026-08-07 12:42 | Postgres 15 (db: copi_migcheck ) |
| `dd395e77a8204b026e996b54278bbb4154ba489c6b316f336a576a40e797590d` | 47.0 | _(anonymous)_ | 2026-08-07 12:47 | 2026-08-07 12:47 | Postgres 15 (db: copi_migcheck ) |
| `4c05f7011b67da1c4636c65599060887c4e2ec666acdaf12e9817cd2a62e67d4` | 47.0 | _(anonymous)_ | 2026-08-07 13:43 | 2026-08-07 13:43 | Postgres 15 (db: copi_migcheck ) |
| `ad3568a6e83cd9535b8b05c575878eb6b2968d6b5d7c6bbdc24b4206dcbc0c31` | 47.0 | _(anonymous)_ | 2026-08-07 13:48 | 2026-08-07 13:48 | Postgres 15 (db: copi_migcheck ) |
| `30fb01de3e242aed4ed91666c117562448cc15cd6caf5d7755533fbbdac9336a` | 47.0 | _(anonymous)_ | 2026-08-07 15:30 | 2026-08-07 15:30 | Postgres 15 (db: copi_migcheck ) |
| `b4b38978e58e3aa1d692f509bf1c3e723873c6d5dd5875aa877fb4365ef303b7` | 46.9 | _(anonymous)_ | 2026-08-14 15:18 | 2026-08-14 15:18 | Postgres 15 (db: copi_migcheck ) |
| `fdfd0ee9c7c0f0a8f74f35a17e9700623de2ec925dde3e47dc8a8a20b6bd9ea3` | 46.9 | _(anonymous)_ | 2026-08-14 15:19 | 2026-08-14 15:19 | Postgres 15 (db: copi_migcheck ) |
| `a434be874f986f979726d604e93a9eecceb8d0b0aa5dc25c3312862fb0b5de38` | 47.0 | _(anonymous)_ | 2026-08-14 20:36 | 2026-08-14 20:36 | Postgres 15 (db: copi_migcheck ) |
| `41031fc594714767c279fed4c2e7fb8a52ca267dd364f6af8bf841c1c6509d70` | 47.0 | _(anonymous)_ | 2026-08-14 21:15 | 2026-08-14 21:15 | Postgres 15 (db: copi_migcheck ) |
| `f34cf964292ff65078df525a3b68b69e30aa52a490081be3c7d026b132654ac7` | 47.0 | _(anonymous)_ | 2026-08-14 21:17 | 2026-08-14 21:17 | Postgres 15 (db: copi_migcheck ) |
| `1562fe3292ffcf6d09264f29f13cdc8149ee0b114b7806ab7b0e8dffd3af2bce` | 47.0 | _(anonymous)_ | 2026-08-14 21:18 | 2026-08-14 21:18 | Postgres 15 (db: copi_migcheck ) |
| `60e8bbf63a060a59d245482e2213e37bac663ac7e784a14a45f9c65182ab94c2` | 47.0 | _(anonymous)_ | 2026-08-14 21:25 | 2026-08-14 21:25 | Postgres 15 (db: copi_migcheck ) |
| `9efe05af7c3f8ced236dd84261437f8e0034476df06d3d260c78d4aedd795b3c` | 47.0 | _(anonymous)_ | 2026-08-14 21:49 | 2026-08-14 21:49 | Postgres 15 (db: copi_migcheck ) |
| `6c445a3bdc97f4afa24e00b02f35708839650dfe082ed8c55fe5b8dd91447345` | 47.0 | _(anonymous)_ | 2026-08-14 22:24 | 2026-08-14 22:24 | Postgres 15 (db: copi_migcheck ) |
| `90891864f373dd609db079307bc259bc6373760bc4eedb5adaf66555313e4180` | 47.0 | _(anonymous)_ | 2026-08-14 23:01 | 2026-08-14 23:01 | Postgres 15 (db: copi_migcheck ) |
| `668aa78ac2992d355d9c6770add2db7a5c533a8b7243ae722e7b52daaa0fab97` | 47.0 | _(anonymous)_ | 2026-08-14 23:33 | 2026-08-14 23:33 | Postgres 15 (db: copi_migcheck ) |
| `5844a069185fc57098fefc03fc86f7d757b69d149108711f7552964f37c7033a` | 47.0 | _(anonymous)_ | 2026-08-14 23:42 | 2026-08-14 23:42 | Postgres 15 (db: copi_migcheck ) |
| `4c207f506c4003be37ccd96b1a948b4ecbc1c36100cb4e24712fccc1634fcf02` | 47.0 | _(anonymous)_ | 2026-08-14 23:44 | 2026-08-14 23:44 | Postgres 15 (db: copi_migcheck ) |
| `7abde71770192618bcb05c3612bcd520480da5a0ada05aaed9ce3df035e4179c` | 47.0 | _(anonymous)_ | 2026-08-14 23:58 | 2026-08-14 23:58 | Postgres 15 (db: copi_migcheck ) |
| `ed3475030fb0019ffb0086e3cb47e43f39fc464aa2ea8b22beee23c87b759b6d` | 47.0 | _(anonymous)_ | 2026-08-15 00:12 | 2026-08-15 00:12 | Postgres 15 (db: copi_migcheck ) |
| `ec109744dfa145af3a4bc733359d8599a232229b1a352c979670ce61437ed988` | 47.0 | _(anonymous)_ | 2026-08-15 00:26 | 2026-08-15 00:26 | Postgres 15 (db: copi_migcheck ) |
| `2184a02d9d7686b01ade10c3ad755f91851de59558369020a94d627fad520c29` | 47.0 | _(anonymous)_ | 2026-08-15 00:33 | 2026-08-15 00:33 | Postgres 15 (db: copi_migcheck ) |
| `09e6df7d4fd30093295b1205c9474a8b303bfd6377559ad00b1ab1d3f7e1df7d` | 46.9 | _(anonymous)_ | 2026-08-15 00:34 | 2026-08-15 00:34 | Postgres 15 (db: copi_migcheck ) |
| `0cc7f9204dbbcb385ab5b07f470beb0f362ab97cd5e26ce0c70b263bab35c268` | 46.9 | _(anonymous)_ | 2026-08-15 00:36 | 2026-08-15 00:36 | Postgres 15 (db: copi_migcheck ) |
| `f093085a448f441ffb89adaad3512f8f517a228e7cb70a880017132243164ad7` | 47.0 | _(anonymous)_ | 2026-08-15 00:46 | 2026-08-15 00:46 | Postgres 15 (db: copi_migcheck ) |
| `c32a750f65c047db02a88b930fde38460ae4bb5ca33431e07413dbb06cd6b86d` | 47.0 | _(anonymous)_ | 2026-08-15 00:53 | 2026-08-15 00:53 | Postgres 15 (db: copi_migcheck ) |
| `83acfb16317a47084f0a3875dabc1a3b50a92e8b38a508165978663b8fe4e85a` | 46.9 | _(anonymous)_ | 2026-08-15 00:57 | 2026-08-15 00:57 | Postgres 15 (db: copi_migcheck ) |
| `5bcf2020a8640d902dcdf40672230e3346bc468308f207dc849cd4f7b131764b` | 47.0 | _(anonymous)_ | 2026-08-15 01:12 | 2026-08-15 01:12 | Postgres 15 (db: copi_migcheck ) |
| `8ccc08d0e2cfe5c5939af2a292ff47710b8c697e757c0bd09d17df0cb790041e` | 47.0 | _(anonymous)_ | 2026-08-15 01:17 | 2026-08-15 01:17 | Postgres 15 (db: copi_migcheck ) |
| `e6bf6857ed6c17410d1ca136c3dc63bf2b6c53c47ecec1f7798fd5cece3a57a2` | 47.0 | _(anonymous)_ | 2026-08-15 01:28 | 2026-08-15 01:28 | Postgres 15 (db: copi_migcheck ) |
| `fa68b76e62d022029ae75d4e90d7a4a6924d2a45f7b71a8ef8c72c920897dd8d` | 47.0 | _(anonymous)_ | 2026-08-15 01:39 | 2026-08-15 01:39 | Postgres 15 (db: copi_migcheck ) |
| `24bd163ba84ae9d0e8db173eb6485b425d0239dedd26a0c50d97eea1827009fb` | 47.0 | _(anonymous)_ | 2026-08-15 01:55 | 2026-08-15 01:55 | Postgres 15 (db: copi_migcheck ) |
| `40e7b39649767caee9fae88b58f54e02c982f0b1fc776281b447fea047a311d2` | 47.0 | _(anonymous)_ | 2026-08-15 02:05 | 2026-08-15 02:05 | Postgres 15 (db: copi_migcheck ) |
| `20c734f5b428adedc69a1cd393129bd75fd23aecb9e067c795478f5e798d5918` | 47.0 | _(anonymous)_ | 2026-08-15 02:22 | 2026-08-15 02:22 | Postgres 15 (db: copi_migcheck ) |
| `c529235b8da816259088c6a7d7e5ce819baac86262d5789ce093445a409bcf48` | 47.0 | _(anonymous)_ | 2026-08-15 02:41 | 2026-08-15 02:41 | Postgres 15 (db: copi_migcheck ) |
| `2b75c72a4dbf189d0bb6ed3db173c8ab003beedb8afdcfaaaee9e1b5af42d52c` | 47.0 | _(anonymous)_ | 2026-08-15 02:54 | 2026-08-15 02:54 | Postgres 15 (db: copi_migcheck ) |
| `5e8234d9d7a6478f11f34a1288c9ff5b1df4d66ccbda4a63d34e380239dc879f` | 47.0 | _(anonymous)_ | 2026-08-15 03:30 | 2026-08-15 03:30 | Postgres 15 (db: copi_migcheck ) |
| `1e40559417d1103674594bfe74d7efcdce50ad233f59b156d1c18c9086a8f265` | 47.0 | _(anonymous)_ | 2026-08-15 03:39 | 2026-08-15 03:39 | Postgres 15 (db: copi_migcheck ) |
| `a6186a1ba6cc220c6a3e287670b01d5803fcb3ea92ca1f7f2161c8dc8a5d37d4` | 47.0 | _(anonymous)_ | 2026-08-15 03:51 | 2026-08-15 03:51 | Postgres 15 (db: copi_migcheck ) |
| `092af538b31c46f7ef7368dd63708c76327495a769019a0a8207d1e0a7abd0a2` | 47.0 | _(anonymous)_ | 2026-08-15 04:37 | 2026-08-15 04:37 | Postgres 15 (db: copi_migcheck ) |
| `529e66cec2bbeb0d986c83d7c5729ed6872300b07e258842b6bdb6f9af3baf39` | 47.0 | _(anonymous)_ | 2026-08-15 05:16 | 2026-08-15 05:16 | Postgres 15 (db: copi_migcheck ) |
| `5ba6d85c4ffba21016c6cf3ed05393bf1d7a6a9de33aeb9ed64b93d0995caa99` | 47.0 | _(anonymous)_ | 2026-08-15 05:47 | 2026-08-15 05:47 | Postgres 15 (db: copi_migcheck ) |
| `fba52a6c1d1c2f856c19a1516ebc48c557ebf4fffdef3a170a7b288019380e2e` | 47.0 | _(anonymous)_ | 2026-08-15 05:53 | 2026-08-15 05:53 | Postgres 15 (db: copi_migcheck ) |
| `2176d58f03e27aaad0fedf82d6aa9e7bf537449f5a7e0dc7b32fa319072c1027` | 47.0 | _(anonymous)_ | 2026-08-15 06:03 | 2026-08-15 06:03 | Postgres 15 (db: copi_migcheck ) |
| `6f4d8012796940dda972e186247f1447bf3deebeefb7b7f02bc3488088ac5471` | 47.0 | _(anonymous)_ | 2026-08-15 06:15 | 2026-08-15 06:15 | Postgres 15 (db: copi_migcheck ) |
| `fc3fc77136fb52b1288adbe93d2af5f1b3365659da82aa23e1d9314d66a5d46f` | 47.0 | _(anonymous)_ | 2026-08-15 06:51 | 2026-08-15 06:51 | Postgres 15 (db: copi_migcheck ) |
| `b3094846f8f2fa415cb591a438e37c00aa01ef4da0edb0592de7952eccd5d27a` | 47.0 | _(anonymous)_ | 2026-08-15 07:15 | 2026-08-15 07:15 | Postgres 15 (db: copi_migcheck ) |
| `59e9aca2a920cea21b0be34f8bbeae47c499a856fd494514f2a50dfadff193c3` | 47.0 | _(anonymous)_ | 2026-08-15 08:06 | 2026-08-15 08:06 | Postgres 15 (db: copi_migcheck ) |
| `ac9ae5494e0bd78576dca776b0d9bb00601ad75eebb2c9dbf5aa7d01827bfd73` | 47.0 | _(anonymous)_ | 2026-08-15 08:50 | 2026-08-15 08:50 | Postgres 15 (db: copi_migcheck ) |
| `12eee05f5fdfe17ccd2341a3cd4614ec6d2d1b3b8fe8ce55da4c9fef37f48377` | 47.0 | _(anonymous)_ | 2026-08-15 10:16 | 2026-08-15 10:16 | Postgres 15 (db: copi_migcheck ) |
| `a411bf1c916c9b04aeecebfb359a89785fec7f523497d71de5557be23ad5b593` | 47.0 | _(anonymous)_ | 2026-08-17 14:36 | 2026-08-17 14:36 | Postgres 15 (db: copi_migcheck ) |
| `264befbdce91bcb86c08d51f6896aa1dd8cf2231b3bbf96cdc1e1f4cc31c9158` | 47.0 | _(anonymous)_ | 2026-08-17 14:52 | 2026-08-17 14:52 | Postgres 15 (db: copi_migcheck ) |
| `1cb2fd90286edecd5bb312eace19b2f5872d1f09676354de5b49b2dc4835b34e` | 47.0 | _(anonymous)_ | 2026-08-17 16:35 | 2026-08-17 16:35 | Postgres 15 (db: copi_migcheck ) |
| `6d62887c99e19a89350b0e9ccffa39742a3f715760b556407334e3073ea52ed0` | 47.0 | _(anonymous)_ | 2026-08-17 17:09 | 2026-08-17 17:09 | Postgres 15 (db: copi_migcheck ) |
| `382b2426851d371a9987e61388998298b17b0a9c76614ffee4c456bde583cb44` | 47.0 | _(anonymous)_ | 2026-08-17 18:32 | 2026-08-17 18:33 | Postgres 15 (db: copi_migcheck ) |
| `9839ea57bfff3e240eb2e7c44450ac82e4d10e34b6fe9991df0b33795676b902` | 47.0 | _(anonymous)_ | 2026-08-17 18:51 | 2026-08-17 18:51 | Postgres 15 (db: copi_migcheck ) |
| `c7bb9058a6cfe6eeb509f2aacff2b1001ce0365b0ba90e39f89e0db9bfc3827e` | 47.0 | _(anonymous)_ | 2026-08-17 19:00 | 2026-08-17 19:00 | Postgres 15 (db: copi_migcheck ) |
| `10741bc2435fc7ae549359e9ae716e7b031eda42651a5081ee62f51e6026c91f` | 47.0 | _(anonymous)_ | 2026-08-17 19:48 | 2026-08-17 19:48 | Postgres 15 (db: copi_migcheck ) |
| `f823e4e8f5662f521d457625bca700325b3d0ed964b2acc915add74ae0038fd1` | 47.0 | _(anonymous)_ | 2026-08-17 19:54 | 2026-08-17 19:54 | Postgres 15 (db: copi_migcheck ) |

**Row count check:** 86 rows = 86 orphans, matching `docker volume ls -qf dangling=true | wc -l`.

### The 3 attached volumes (NOT orphans, listed for completeness)

| Volume | MiB | Compose project | Mounted in | Content |
|---|---:|---|---|---|
| `copi-blackbird_pgdata` | 202 | copi-blackbird | `copi-blackbird-postgres-1` | Postgres 15 — **this stack's production database** (dbs: `copi`, plus scratch DBs `copi_base`, `copi_mig`, `copi_review11`, `copi_review_task10`, `copi_sdd`, `copi_task10_roundtrip`, `copi_task12review`, `copi_test`) |
| `copi-python_pgdata` | 2,284 | copi-python | `copi-python-postgres-1` | Postgres 15 — **org1's production database** (dbs: `copi`, plus `copi_at0018`, `copi_it_test`, `copi_m1a_test`, `copi_rca`, `copi_rehearsal`, `copi_restore_verify`, `copi_t_upgrade`, `copi_tdd`, `copi_verify`) |
| `4a675ea246bc0c354c9a89765d773a02a583b0d89d5a9f9e87dce5ba571ac64e` | 0.004 | _(anonymous)_ | `copi-python-certbot-1` at `/var/lib/letsencrypt` | **empty** — no TLS material (see §5.3) |

Incidental observation: both production clusters carry a pile of scratch databases created
inside the *production* volume by test and rehearsal runs. That is a separate hygiene issue
from this audit, but it is where `TEST_DATABASE_URL` scratch databases have been accumulating —
and it is why CLAUDE.md's warning never to point `TEST_DATABASE_URL` at `copi` matters.
