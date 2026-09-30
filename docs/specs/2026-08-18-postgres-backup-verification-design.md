# Verified Postgres Backups — Design

**Date:** 2026-08-18
**Status:** IMPLEMENTED AND LIVE as of 2026-08-18. Timers armed for 01:00
America/Los_Angeles. Three verified backups per stack on disk; failure-injection harness
12/12; SES alert path confirmed received by a human. Three audit rounds; final
whole-branch audit found no Critical. Parked residuals are recorded in the commit
history and summarised at the end of this document.
**Scope:** Nightly, self-verifying logical backups for the two production Postgres
instances on the `copi` EC2 host (`copi-python`, `copi-blackbird`). Local storage
only, with a drop-in hook for offsite. Adds no long-lived process and does not
modify either application stack.

---

## 0. Why

There is no backup system on this host. The `.dump` files in
`blackbird-copi-science/backups/` are a side effect of
`scripts/migrate/run_migration.sh` — pre-deploy safety snapshots taken by hand
before a migration. Nothing runs on a schedule, nothing is retained on a policy,
and nothing has ever been restored to prove it can be.

Both databases live on `/dev/root`, the same EBS volume as everything else:

| Stack | Container | DB size | Volume |
|---|---|---|---|
| `copi-python` | `copi-python-postgres-1` | 1333 MB | `copi-python_pgdata` |
| `copi-blackbird` | `copi-blackbird-postgres-1` | 142 MB | `copi-blackbird_pgdata` |

`archive_mode=off`, `wal_level=replica` — point-in-time recovery is not possible
with the current configuration and is explicitly out of scope (§2).

The motivating failure is not hypothetical. On 2026-08-18 an audit of this host
found `certbot.service` had been failing every ~12 hours since May — roughly 200
consecutive failures — for a certificate that expired on 2026-05-07, with nobody
notified. A backup system with the same blind spot is worse than no backup
system, because it manufactures confidence. Every design decision below that
looks like paranoia traces to that finding.

## 1. Decisions taken

| # | Decision | Rationale |
|---|---|---|
| 1 | Nightly logical dumps (`pg_dump -Fc`) | ~1.5 GB total; a full dump is minutes. RPO of 24h accepted. |
| 2 | Local storage only, offsite via a hook | Offsite target does not exist yet. Hook keeps it a config change, not a rewrite. |
| 3 | Verify by restore + per-table row-count parity | Catches truncated/partial dumps, which a bare `pg_restore` exit code does not. |
| 4 | Retention: 3 most recent **verified** per stack, count-based | Day-based pruning empties the directory if the producer breaks. See §5. |
| 5 | Alerting: SES email on failure + weekly heartbeat | Recipients: `malanjary@`, `ahuebschen@`, `asu@scripps.edu`. SES v1 API, matching the app (§7.1). |
| 5b | Plus a local `status.json` second channel | Added by audit: a single alert channel cannot report its own silence. |
| 6 | Python 3, not bash | Snapshot-consistent counting needs a psql session held across `pg_dump`. See §4.2. |
| 7 | Verify stack is ephemeral, `--network none`, memory-capped | Host has a history of global OOM kills. See §4.4. |

## 2. Non-goals

- **PITR / WAL archiving.** Would require `archive_mode=on` and a restart of both
  production instances. Deliberately deferred.
- **Offsite replication.** Hook only (§6). No S3 bucket, no credentials, no upload.
- **Backing up anything but Postgres.** Not the certbot bind-mount, not
  `collab-platform_mongodb_data`, not application state on disk.
- **Rehearsing production recovery automatically.** Verification proves the dump
  restores into a throwaway. Recovering production is a different procedure and is
  documented as a human runbook (§11).
- **Replacing the pre-deploy dumps** in `scripts/migrate/run_migration.sh`. Those
  stay; this system never reads or deletes them.

## 3. Architecture

Host-level only. No container is added to either compose project, and neither
application stack is modified.

| Path | Purpose |
|---|---|
| `/usr/local/bin/copi-backup` | Python 3 entrypoint. Subcommands: `run`, `report`, `prune` |
| `/etc/copi-backup/backup.env` | All tunables |
| `/etc/systemd/system/copi-backup.{service,timer}` | Nightly dump + verify + prune |
| `/etc/systemd/system/copi-backup-report.{service,timer}` | Weekly heartbeat |
| `/var/backups/copi/<stack>/` | Dumps + sidecars, mode `0700`, owner `root` |
| `/var/backups/copi/status.json` | Machine-readable health, second channel (§7.2) |
| `/run/copi-backup.lock` | `flock` target, prevents overlapping runs |

### 3.1 Configuration

```ini
# /etc/copi-backup/backup.env
# stack_name:container:db:user
STACKS="copi-python:copi-python-postgres-1:copi:copi
        copi-blackbird:copi-blackbird-postgres-1:copi:copi"

BACKUP_ROOT=/var/backups/copi
RETENTION_COUNT=3          # verified dumps kept per stack
RETENTION_UNVERIFIED=2     # failed-verify dumps kept for diagnosis
VERIFY_IMAGE=postgres:15   # MUST match the source server image
VERIFY_MEM=768m            # measured: peak 195.4MiB restoring the 1333MB db
VERIFY_TIMEOUT_SEC=1800
FREE_SPACE_FACTOR=7        # require 7x that stack's last dump before starting
MIN_FREE_BYTES=2147483648  # absolute floor, checked alongside the factor
WARN_SPACE_FACTOR=14       # soft warning threshold; 0 disables
OFFSITE_CMD=""             # empty = local only

AWS_REGION=us-east-2
SES_SENDER_EMAIL=<sender>@copi.science
MAIL_TO="malanjary@scripps.edu ahuebschen@scripps.edu asu@scripps.edu"
```

Stacks are data. Adding a third is one line; no code change.

SES settings are duplicated here rather than read from either app's `.env`, so an
application config change cannot break the backup path.

## 4. Backup and verify flow

Stacks are processed **sequentially**, never in parallel — the host has 3.7 GB RAM
and is already ~1.2 GB into swap.

### 4.1 Preflight

1. Acquire `flock` on `/run/copi-backup.lock`; exit 0 if already held (systemd
   will not overlap runs, but manual invocation can).
2. Sweep leftovers from a previously crashed run:
   - containers: `docker ps -aq --filter label=copi.backup.ephemeral=true` → `docker rm -f -v`
   - volumes: `docker volume ls -q --filter label=copi.backup.ephemeral=true` → `docker volume rm`
   - stale `*.dump.partial` files older than 24h under `BACKUP_ROOT`
   - stale `/tmp/copi_backup_*.dump` inside each configured postgres container
   - orphaned `*.json` sidecars whose `.dump` no longer exists

   The volume sweep must be **label-filtered**, never `docker volume prune`. A bare
   prune on this host would destroy `copi-prod_pgdata`, `copi_pgdata`,
   `copi-python_grantbot_data` and `collab-platform_mongodb_data` — 512 MB of
   unreferenced but un-backed-up data.
3. Free-space check, **per stack**, re-measured at the top of each stack's turn:
   require `free_bytes >= max(FREE_SPACE_FACTOR * that_stack_last_dump_size,
   MIN_FREE_BYTES)` (falling back to that stack's live DB size when it has no dump
   yet). **Abort that stack and mail if short; continue with the others.** A backup
   job that fills `/dev/root` takes production down with it — which is what
   `MIN_FREE_BYTES` is for, because a per-stack factor alone scales to nothing for a
   small stack and would clear it to start a restore on a nearly-full filesystem.

   Amended 2026-09-16. This was a whole-run abort until then, and the cost was
   measured: on 2026-09-15 and 2026-09-16 copi-blackbird needed 884 MB, had ~4 GB
   free, and was skipped anyway because the guard charged it copi-python's 847 MB
   demand. Stacks are processed in ascending demand so a large stack cannot starve a
   small one of the remaining headroom; a stack whose demand cannot be measured is
   processed last and, per the F3 invariant, is refused rather than passed.

### 4.2 Dump with a consistent snapshot

Row-count parity against a *live* source is not well-defined: production keeps
writing between the dump and the count. Comparing "restored dump" against "source
now" produces false mismatches on any active table, and a verifier that cries wolf
gets ignored. Counts must come from the dump's own snapshot.

```
psql session S (held open for the duration of the dump):
    BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
    SET LOCAL idle_in_transaction_session_timeout = 0;   -- see below
    SELECT pg_export_snapshot();          -- → e.g. 00000009-00008D07-1

    pg_dump -Fc --snapshot=<id> -U <user> -d <db>   (separate connection)

    <count query, §4.3, in session S — same snapshot as the dump>
    COMMIT;
```

Constraints this relies on, **all verified against the live servers on 2026-08-18**:

- The exporting transaction must stay open for as long as `pg_dump` uses the
  snapshot. This defers vacuum cleanup for the dump duration plus the count query
  (~2–4 min for `copi-python`); it blocks no writer and takes no user-visible lock.
- **Session S sits idle-in-transaction while `pg_dump` runs.** If
  `idle_in_transaction_session_timeout` were non-zero, the server would kill S,
  invalidating the snapshot and failing the dump. Both servers currently have it at
  `0`, but the script sets `SET LOCAL ... = 0` explicitly so a future server-config
  change cannot silently break backups. This is the single most fragile dependency
  in the design; it is neutralised rather than assumed.
- `pg_export_snapshot()` requires the exporting transaction to have performed no
  writes. Session S only reads.
- The snapshot is only valid for the same database and cluster. Both are.
- Verified empirically: `pg_dump --snapshot` is supported by the `postgres:15`
  image (server 15.17), and `pg_export_snapshot()` returned a valid ID
  (`00000009-00008D07-1`) in this exact `docker exec psql` configuration.

**Dump to a container-side temp file, verify its TOC there, then `docker cp` it out.**
Not `docker exec pg_dump > host_file`. This follows the pattern already proven in
`scripts/migrate/run_migration.sh:176`, which documents the failure it avoids: a
custom-format archive needs *random access* to read its table of contents, so any
verification that reads the archive from a non-seekable stream fails on a perfectly
good dump. Keeping the archive on a real filesystem path at every step — first inside
the container, then on the host — sidesteps the whole class, and avoids pushing
~600 MB of binary through Docker's exec stream.

```
docker exec <pg>  pg_dump -U <user> -Fc --snapshot=<id> -f /tmp/copi_backup_<pid>.dump <db>
docker exec <pg>  pg_restore -l /tmp/copi_backup_<pid>.dump   # TOC readable? (§4.4 precheck)
docker cp         <pg>:/tmp/copi_backup_<pid>.dump  <BACKUP_ROOT>/<stack>/<name>.dump.partial
docker exec <pg>  rm -f /tmp/copi_backup_<pid>.dump           # always, even on failure
```

The host-side `.partial` is then `fsync`'d and **atomically renamed** to `<name>.dump`.
A truncated file can never occupy a valid backup name. `pg_dump` runs under
`nice -n 10` / `ionice -c 3` so it yields to production I/O.

The container-side temp costs transient space inside the postgres container's
writable layer — which is on `/dev/root`, the same filesystem as `BACKUP_ROOT`. Peak
transient usage is therefore ~2× the dump size, which is what `FREE_SPACE_FACTOR=3`
(§4.1) budgets for. The temp file is removed in a `finally`, and any survivor is
swept on the next run (§4.1).

### 4.3 Count query

Exact counts, not `pg_stat_user_tables.n_live_tup` and not `reltuples`. This matches
the reasoning already recorded in `scripts/migrate/preflight.py:942`
(`snapshot_row_counts`): "Exact, not reltuples: reltuples is an estimate that a fresh
table reports as -1, which would make the postflight comparison a coin toss." 

```sql
SELECT n.nspname || '.' || c.relname AS tbl,
       (xpath('/row/cnt/text()',
              query_to_xml(format('SELECT count(*) AS cnt FROM %I.%I',
                                  n.nspname, c.relname),
                           false, true, '')))[1]::text::bigint AS n_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
```

`relkind = 'r'` is correct for both databases today: 30 ordinary tables, zero
partitioned tables and zero materialised views. **If a partitioned table is ever
added, this query must change** — a parent (`relkind='p'`) plus its leaf partitions
(`relkind='r'`) would otherwise double-count. Both sides run the identical query,
so the comparison stays symmetric either way; the risk is only in interpreting the
absolute numbers.

### 4.4 Verify

**Cheap integrity precheck first — and be precise about what it can see.**
`pg_restore -l` is run twice against a real seekable path: once inside the source
container immediately after the dump (§4.2), and once on the copied `.dump` before the
verify container starts. **Both runs happen inside a throwaway container of
`VERIFY_IMAGE`, never on the host** — the host has no postgres client tools installed at
all (verified 2026-08-18). The second run also proves the `docker cp` did not truncate.

It reads only the archive's table of contents, which on a 75,571,751-byte custom-format
archive occupies roughly the **first 755 KB**. Measured on this host:

| Damage | `pg_restore -l` |
|---|---|
| corrupt bytes in the header | fails (exit 1) |
| truncated to 64 KB / 16 KB / 4 KB | fails (exit 1) |
| truncated to 50% / 25% / 10% / 5% / **1%** | **passes (exit 0)** |
| corrupt data block at offset 40,000,000 | **passes (exit 0)** |

So the precheck catches header-region damage cheaply and is **blind to everything
past the TOC**. A dump missing half its data blocks, or corrupted deep inside one, sails
through it. Those cases are caught only by the full restore in §4.4 — verified: a dump
corrupted at offset 40,000,000 passes the precheck and is then rejected by
`pg_restore --exit-on-error` during the real restore.

Calling this check "catches a corrupt file" — as an earlier draft of this document did —
overstates it, and overstating a cheap check is the same false-confidence failure this
whole design exists to prevent. It is a fast filter that saves a container start on
grossly damaged archives, not a substitute for restoring. `failure_injection.sh` tests
both halves of that layering (2a/3a and 2b/3b). Failure at either point short-circuits
to `.unverified`.

This mirrors `scripts/migrate/run_migration.sh:189`, which already gates its
pre-migration dump the same way: "A dump whose table of contents cannot be read
cannot be restored. This is the difference between having a backup and having a
file." 

```
docker volume create --label copi.backup.ephemeral=true copi-verify-<stack>-<pid>

docker run -d \
  --name copi-verify-<stack>-<pid> \
  --label copi.backup.ephemeral=true \
  --network none \
  --memory ${VERIFY_MEM} --memory-swap ${VERIFY_MEM} \
  -e POSTGRES_PASSWORD=<random> \
  -e POSTGRES_USER=<user-from-STACKS> -e POSTGRES_DB=<db-from-STACKS> \
  -v copi-verify-<stack>-<pid>:/var/lib/postgresql/data \
  -v <dump>:/dump.bin:ro \
  ${VERIFY_IMAGE}
```

- **`--network none`** — every command runs via `docker exec` over the container's
  unix socket. The verify database is physically incapable of reaching production
  or the network. This is stronger than a firewall rule and cannot be misconfigured.
- **`--memory-swap` equal to `--memory`** disables swap for the container, so a
  runaway restore fails fast instead of thrashing a box that is already swapping.
- **Same image as the source** (`postgres:15`, server 15.17) guarantees no
  `pg_restore` version skew and identical extension availability. Both DBs use only
  `plpgsql`, so there is no extension to install.
- **Named, labelled volume for `PGDATA`** — not an anonymous one. Anonymous volumes
  do not inherit container labels, so a script killed between container removal and
  volume cleanup would orphan a volume that could only be reclaimed by
  `docker volume prune` — which on this host would also destroy `copi_pgdata`,
  `copi-prod_pgdata` and `collab-platform_mongodb_data`. A labelled volume is
  sweepable by filter (§4.1) and never requires a blanket prune. Disk-backed, not
  `tmpfs` — a 1.3 GB restore into RAM would defeat the memory cap.
- **`VERIFY_MEM` is a starting value, not a proven one.** 768 MB comfortably holds
  `postgres:15` defaults (`shared_buffers=128MB`, `maintenance_work_mem=64MB`), but
  index builds during a 1333 MB restore have not been measured. If it is set too
  low the container OOMs and the run reports a *backup* failure that is really a
  *harness* failure — a false alarm, which is the fastest way to train people to
  ignore this mail. Test 1 (§10) must record peak usage via `docker stats` and set
  `VERIFY_MEM` to roughly 1.5× the observed peak before the system goes live.

Then:

1. `pg_isready` poll until ready, or fail at `VERIFY_TIMEOUT_SEC`.
2. `pg_restore --no-owner --no-privileges --exit-on-error -U copi -d <db> /dump.bin`.
   `--no-owner/--no-privileges` are safe: the only non-system role is `copi`, which
   the container recreates as its superuser. `--exit-on-error` makes partial
   restores loud.
3. Re-run the §4.3 count query inside the container.
4. Compare against the snapshot counts as ordered `(table, count)` pairs. Any
   difference in table set or any count — fail, with a per-table diff.

### 4.5 Teardown

`docker rm -f -v <name>` in a `finally` block, unconditional: success, restore
failure, timeout, or unhandled exception. The startup sweep (§4.1) is the backstop
for a hard kill of the script itself.

## 5. Retention

Count-based, not day-based.

```
keep the RETENTION_COUNT (3) most recent VERIFIED dumps per stack
keep the RETENTION_UNVERIFIED (2) most recent .unverified dumps per stack
delete nothing if it would leave zero dumps of ANY kind for a stack
deleting a dump also deletes its .json sidecar (never the reverse)
```

Two edge cases the floor must cover, not one:

- Zero verified dumps exist because verification has been failing. The
  `RETENTION_UNVERIFIED=2` cap would ordinarily trim a backlog of 5 unverified
  dumps down to 2 — but those may be the only copies in existence. The floor is
  therefore expressed over *all* dumps, not just verified ones: if a stack has no
  verified dump, unverified ones are never pruned.
- A sidecar must never outlive or predecease its dump. Pruning is dump-driven; the
  `.json` goes with it. Orphaned sidecars from an interrupted run are cleared by the
  §4.1 sweep.

Day-based pruning (`find -mtime +5 -delete`) has a failure mode that this design
rejects: the pruner outlives the producer. If dumping breaks for a week, day-based
retention deletes every backup and leaves an empty directory. Count-based retention
degrades to "stale but present". In steady state with nightly runs the two are
identical — 3 nightly dumps is 3 days.

The count was 5 at go-live and is 3 as of 2026-09-16. The reason is disk, not policy
preference: rebuilds on this host add Docker image layers and BuildKit cache faster than
anything reclaims them (§8.1), and `/var/backups/copi` shares `/dev/root` with both
pgdata volumes, so every retained copy is headroom that churn cannot use. 3 costs
~1.95 GB less than 5. The price is a **3-day recovery window**, which matters more than
it looks because there is still no offsite copy (§12.1) and no PITR (§12.3): logical
corruption not noticed within 3 days is unrecoverable. Revisit this the moment
`OFFSITE_CMD` is real.

Pruning is deliberately narrow and only ever deletes paths that:

1. are directly inside `<BACKUP_ROOT>/<stack>/`,
2. match `^<stack>_<db>_\d{8}T\d{6}Z\.dump(\.unverified)?(\.json)?$`,
3. are regular files, not symlinks (no `follow_symlinks`).

The hand-made dumps in `blackbird-copi-science/backups/` are structurally
unreachable by this pruner.

## 6. Offsite hook

```
OFFSITE_CMD=""     # today
```

If non-empty, invoked after a **successful verify** as:

```
$OFFSITE_CMD <path-to-dump> <path-to-sidecar>
```

Contract: exit `0` = durably stored; non-zero = failure, which is reported in the
run's mail and recorded as `offsite: false`. Offsite failure does **not** invalidate
a verified local backup.

Each dump gets a JSON sidecar:

```json
{
  "stack": "copi-python",
  "database": "copi",
  "started_utc": "2026-08-19T08:00:00Z",
  "duration_sec": 74,
  "dump_bytes": 612344320,
  "dump_sha256": "9f2b...",
  "pg_version": "15.17",
  "snapshot_id": "00000003-0000001B-1",
  "row_counts": {"public.users": 412, "public.proposals": 1180},
  "verified": true,
  "verify_duration_sec": 96,
  "offsite": false
}
```

The `offsite` field is recorded now so retention can later be taught "never prune a
dump that has not been uploaded" without a redesign. That enforcement is **not**
built today — it would be speculative until a target exists.

`dump_sha256` is computed once at write time. Verification proves a dump was good
*when taken*; the checksum is what lets you prove a retained dump has not rotted
since, and gives the future offsite step an end-to-end integrity check for free.

## 7. Alerting

**One mail per run, not per stack** — a bad night produces one message, not three.

*Failure mail* — subject `[copi-backup] FAILED <stack>[,<stack>] YYYY-MM-DD`, body
gives the stage that broke (`preflight` / `dump` / `restore` / `count-mismatch` /
`offsite`), the stderr tail, and for a mismatch a per-table expected-vs-actual diff.

*Weekly heartbeat* — Mondays, `[copi-backup] weekly summary`. Last 7 runs per stack:
timestamp, size, duration, verify result, retained count, oldest and newest. Size
drift is visible here.

The heartbeat exists because failure-only alerting cannot distinguish "healthy" from
"not running". A dead timer, a revoked instance role, or an SES misconfiguration all
produce the same empty inbox as a perfect week. If the Monday mail stops arriving,
the system itself is broken. This is precisely the gap that let `certbot.service`
fail 200 times unnoticed.

Delivery is `boto3` (already present on the host, 1.34.46) via the instance role
`copi-ec2-ses-role`. **If SES delivery fails, the script still exits non-zero** so
`systemctl --failed` catches it. Mail is the primary channel, not the only one.

### 7.1 SES status — RESOLVED

Probing the instance role on 2026-08-18 established:

| Check | Result |
|---|---|
| `ses:SendEmail` | **Granted** — probe failed on address validation, not authorization |
| `ses:GetAccount` | AccessDenied — cannot read `ProductionAccessEnabled` from this host |
| `ses:ListEmailIdentities` | AccessDenied — cannot enumerate verified identities |

Sandbox status is not readable from the instance, but it is settled by evidence:
`copi-python` already sends invitation and notification mail to **arbitrary user
addresses** (`src/services/email.py`, `src/services/email_notifications.py`), and
inbound email is configured. Sending to unverified third-party recipients is
impossible in the SES sandbox, so the account has production access. Confirmed by
the operator 2026-08-18.

**Implementation note:** the application uses the **SES v1** client
(`boto3.client("ses", region_name=...)` with `send_email(Source=, Destination=,
Message=)`). The backup script uses the same API and the same
`SES_SENDER_EMAIL` / `AWS_REGION` values so there is one sending pattern on this
host, not two.

Test 9 (§10) still stands — the channel must be observed working end to end before
go-live. The account being capable of sending is not the same as this script's mail
arriving in three specific inboxes.

### 7.2 Second channel (added by audit)

Mail is a push channel and cannot report its own silence — a dead timer, a revoked
role, or an SES change all look like a quiet week. Every run therefore also writes
`/var/backups/copi/status.json`:

```json
{"last_run_utc": "...", "last_success_utc": "...", "ok": false,
 "stacks": {"copi-python": {"verified": true, "dump_bytes": 0, "problems": [], "error": null}},
 "regressed": {}, "warnings": [], "config": {"retention_count": 5, "...": "..."},
 "reason": "only when the run aborted before producing per-stack results"}
```

This costs a few lines, depends on nothing external, and is insurance against mail
being silently undeliverable.

`regressed` is carried forward across runs that produce no results at all (preflight
abort, unexpected exception, signal): a suspected data loss is evidence that must
outlive the run that spotted it, and a flag erased by an aborted night would be gone
after two bad ones. A stack drops its flag only by producing a verified dump that does
not regress. `prune` excludes every stack listed here, not just ones flagged this run.

`config` records the tunables that decide what is kept and what is deleted, so that a
change to them is visible without reading `/etc`. The nightly run also logs them, and
logs a WARNING naming any value that differs from the shipped default.

**This file has no automated consumer.** An earlier version of this section claimed the
07:00 `daily_audit.md` cron reads it; it does not, and never did. Treat status.json as
a channel a human must be pointed at, not one that raises anything by itself.

## 8. Scheduling and resource guards

| Unit | Schedule | Action |
|---|---|---|
| `copi-backup.timer` | `*-*-* 01:00:00 America/Los_Angeles`, `Persistent=true` | dump → verify → prune |
| `copi-backup-report.timer` | `Mon *-*-* 08:00:00 America/Los_Angeles`, `Persistent=true` | heartbeat only |

01:00 Pacific is requested by the operator. A **named timezone, not a fixed UTC
offset**: 1am Pacific is 08:00 UTC under PDT and 09:00 UTC under PST, so a hardcoded
offset would silently shift by an hour twice a year. systemd 255 on this host accepts
`America/Los_Angeles` in `OnCalendar` (verified 2026-08-18).

This still clears the 07:00 UTC `daily_audit.md` Claude cron — the largest memory
consumer on this host — which runs for roughly 8 minutes (measured: log last written
07:07:46). The backup starts ~52 minutes after that finishes in summer, and ~112 in
winter.

Service hardening: `Nice=10`, `IOSchedulingClass=idle`, `TimeoutStartSec=3600`,
`ProtectSystem=strict` with `ReadWritePaths=/var/backups /run` (the shipped unit
grants `/var/backups`, not `/var/backups/copi`).

### 8.1 Build-cache reclaim (added 2026-09-16)

`docker-builder-prune.timer` runs `docker builder prune -f --keep-storage 10GiB` every
Sunday at 06:00 UTC, two hours before the backup. It is not part of the backup system
and it is installed by the same `install.sh` anyway, because the backup system cannot
run without it: the 2026-09-15/16 outage was BuildKit cache filling `/dev/root` until
the free-space guard could no longer be satisfied, and nothing on this host reclaimed
it. It regrows with every rebuild, so a one-off cleanup is not a fix.

`--keep-storage`, not a bare prune: recent cache is what makes a redeploy fast. The cap
is set just above the ~8.3 GiB still referenced after the 2026-09-16 reclaim, making
this a ceiling on growth rather than a weekly cold start.

Note for anyone reclaiming by hand: deleting **images** is close to useless here.
On 2026-09-16, removing 41 images (32 dangling plus 9 stale rollback tags) reclaimed
**~1.2 MB**, because BuildKit still referenced every layer — `docker image prune`
reported `Total reclaimed space: 0B` while build-cache reclaimable rose from 6.098 GB
to 30.25 GB. Prune the build cache first; it is the gate, not the cheap first step.

`MemoryMax=` is deliberately **not** set on the unit. `docker run` children are
parented into `docker.service`'s cgroup, not the unit's, so a unit-level memory cap
would constrain the Python orchestrator (a few MB) and not the verify container.
The cap that matters is `--memory` in §4.4.

## 9. Failure semantics

| Failure | Behaviour | Backup kept? | Mail | Exit |
|---|---|---|---|---|
| Insufficient free space for a stack | Abort **that stack** before dumping, continue others | n/a | yes | ≠0 |
| Insufficient free space for every stack | Nothing dumped; top-level `reason` in status.json | n/a | yes | ≠0 |
| `pg_dump` fails / killed | `.partial` deleted | no | yes | ≠0 |
| Snapshot export fails | Abort that stack, continue others | no | yes | ≠0 |
| Session S killed mid-dump | `pg_dump` fails on invalid snapshot; `.partial` deleted | no | yes | ≠0 |
| `pg_restore --list` precheck fails | No container started; dump `.unverified` | yes | yes | ≠0 |
| Verify container OOMs (`VERIFY_MEM` too low) | Dump `.unverified`; mail flags OOM explicitly | yes | yes | ≠0 |
| Docker daemon unavailable | Abort run before dumping | n/a | attempted | ≠0 |
| Verify container won't start | Dump renamed `.unverified` | yes | yes | ≠0 |
| `pg_restore` fails | Dump renamed `.unverified` | yes | yes | ≠0 |
| Verify timeout | Container killed, dump `.unverified` | yes | yes | ≠0 |
| Row-count mismatch | Dump renamed `.unverified`, diff in mail | yes | yes | ≠0 |
| `OFFSITE_CMD` non-zero | `offsite: false` in sidecar | yes (verified) | yes | ≠0 |
| SES send fails | Logged to journal | unchanged | n/a | ≠0 |
| One stack fails | Other stack still runs to completion | — | one mail | ≠0 |

A failed verify never deletes the dump. A suspect backup beats no backup, and
deleting it destroys the evidence needed to diagnose the failure.

## 10. Testing

Happy path proves nothing. Each of these must be demonstrated before the system is
considered live:

| # | Injection | Expected |
|---|---|---|
| 1 | `--dry-run` first pass | Full flow, zero deletions |
| 2 | Corrupt bytes mid-dump-file | Verify fails, `.unverified`, mail sent |
| 3 | Truncate dump to 50% | `pg_restore` fails, teardown still runs |
| 4 | `docker kill` verify container mid-restore | No stray container, no leaked volume |
| 5 | `INSERT` into restored copy pre-count | Mismatch detected, correct table named |
| 6 | Run with zero verified dumps present | Prune deletes nothing (floor holds) |
| 7a | Simulate low disk (sparse dump, both stacks) | Dumps nothing, non-zero exit, `reason` in status.json |
| 7b | Simulate low disk for ONE stack | That stack is skipped; the other is backed up |
| 7c | `MIN_FREE_BYTES` above real free space | Stack refused even though the factor is satisfied |
| 8 | Stray container left from prior run | Startup sweep removes it |
| 9 | **Real SES send to all three recipients** | Mail arrives in all three inboxes; confirm out-of-band |
| 10 | Concurrent manual + timer invocation | `flock` serialises; no double run |
| 11 | `SIGKILL` script mid-dump | Stale `.partial` cleared on next run's sweep |
| 12 | Orphan a labelled verify volume | Sweep removes it; the 4 unreferenced production volumes survive |
| 13 | Set `VERIFY_MEM=64m` deliberately | Reported as OOM, distinguished from a restore failure |
| 14 | Delete a dump, leave its sidecar | Sweep removes the orphaned `.json` |
| 15 | Flip one byte in a retained dump | `dump_sha256` mismatch detected |
| 16 | Measure peak verify memory (`docker stats`) | Sets the real `VERIFY_MEM` before go-live |

Test 9 is not optional. An untested alert channel is how
`certbot.service` failed silently for three months. Test 16 is not optional either:
shipping with an unmeasured `VERIFY_MEM` risks nightly false alarms, which destroy
trust in the alert faster than no alert at all.

## 11. Restore runbook (human procedure)

Automated verification proves a dump *restores*. It does not rehearse recovering
production. This procedure should be walked through by hand once, on a copy, before
it is ever needed in anger.

```bash
STACK=copi-python; DUMP=/var/backups/copi/copi-python/<file>.dump
cd /home/ubuntu/copi-python

# 1. Stop ALL writers. Leave postgres up.
docker compose -f docker-compose.prod.yml -f docker-compose.override.yml \
  stop app worker grantbot
#    The agent is NOT part of that compose invocation: it is a one-off container
#    (com.docker.compose.oneoff=True) behind `profiles: [agent]`. It holds live
#    DB connections and MUST be stopped explicitly, or step 3 will terminate it
#    mid-transaction:
docker stop agent-run            # if running; blackbird uses blackbird-agent-run
#    Do NOT `docker compose up` this project to bring things back, and never pass
#    --profile agent. The agent is operator-controlled and restart=no by design;
#    restart it by hand at the end if it was running.

# 2. Snapshot the current state BEFORE overwriting it.
docker exec copi-python-postgres-1 pg_dump -Fc -U copi -d copi \
  > /var/backups/copi/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump

# 3. Recreate the database.
docker exec copi-python-postgres-1 psql -U copi -d postgres \
  -c "DROP DATABASE copi WITH (FORCE);" -c "CREATE DATABASE copi OWNER copi;"

# 4. Restore.
docker exec -i copi-python-postgres-1 \
  pg_restore --no-owner --no-privileges --exit-on-error -U copi -d copi < "$DUMP"

# 5. Verify, then restart writers.
docker exec copi-python-postgres-1 psql -U copi -d copi -c "\dt"
docker compose -f docker-compose.prod.yml -f docker-compose.override.yml \
  start app worker grantbot
```

Step 2 is mandatory. Restoring over a live database without first capturing it
converts a recoverable incident into an unrecoverable one.

**Schema-version mismatch.** A dump carries the schema as of the night it was taken.
If the deployed code has advanced past it, the app will fail against the restored
database until migrations are re-applied. After step 5, compare the restored
`alembic_version` against what the running image expects and apply any pending
migrations before restarting writers. Restoring a dump older than the last
migration and starting the app unmigrated is the most likely way this runbook goes
wrong in practice.

**Full-cluster loss.** This procedure assumes the postgres container and its role
still exist. If the cluster itself is gone, roles must be recreated first —
`pg_dump -Fc` does not carry globals. The only non-system role in either database
is `copi`, which the `postgres:15` entrypoint creates from `POSTGRES_USER`, so a
fresh container plus this restore is sufficient. No `pg_dumpall --globals-only` is
required today; that changes the moment a second role is added.

## 12. Known limitations

1. **Same-volume storage.** `/var/backups/copi` is on `/dev/root`, alongside
   `copi-python_pgdata`. EBS volume loss destroys both. This is accepted for now;
   §6 is the remedy.
2. **24h RPO.** Up to a day of agent runs, proposals and mirrored Slack activity is
   lost in a full-restore scenario.
3. **No PITR.** Cannot recover to a point mid-day, e.g. immediately before a bad
   migration. The pre-deploy dumps in `run_migration.sh` partially cover this case.
4. **Verify proves restorability, not application correctness.** A schema the app
   can no longer read would still pass.
5. **Single host.** The backup system runs on the machine it protects.
6. **Row counts do not verify sequences.** `pg_dump -Fc` carries sequence positions
   and `pg_restore` applies them, but nothing here checks it. A restore with
   rewound sequences would pass verification and then throw duplicate-key errors on
   the app's next insert. Accepted: checking it means comparing `last_value` across
   every sequence, and the failure mode has never been observed with `-Fc`.
7. **Dumps are not encrypted at rest.** `/var/backups/copi` is mode `0700` root, but
   the files are full plaintext copies of production. Anyone with root on this host,
   or a snapshot of the EBS volume, has the entire database. This is no worse than
   the `pgdata` volume sitting beside it, but it becomes a real decision the moment
   `OFFSITE_CMD` starts shipping these somewhere — encrypt before upload, not after.
8. **`relkind='r'` counting is correct only while no partitioned tables exist.**
   True today (30 ordinary tables, zero partitioned, zero matviews). §4.3 must be
   revisited if that changes.
9. **The weekly heartbeat renderer is load-bearing as an alert path.** `OnFailure=`
   runs `copi-backup report`, so `render_heartbeat_mail` is not only the Monday
   summary — it is the independent path of §14.4. Its staleness check is therefore
   evaluated **per stack** (any configured stack past `HEARTBEAT_STALE_HOURS`, plus
   any stack with no sidecar at all). It must not be reverted to a single `max()`
   over all entries: with a per-stack free-space guard, one healthy stack would
   silence the alarm for a stack that had not been backed up in weeks, while its old
   sidecars kept rendering as clean `verified` lines.
10. **Backups still cover Postgres only, and still only locally.** `OFFSITE_CMD` is
   empty, so every copy of both databases lives on the same EBS volume as the
   databases. This remains the largest single risk in the design and is untouched by
   the 2026-09-16 work.

## 13. Follow-ups (not in scope)

- Offsite target (S3 + lifecycle policy), then enable `OFFSITE_CMD` and consider
  gating retention on upload.
- Revisit PITR if the 24h RPO proves too loose.
- The host has no memory headroom (3.7 GB, ~1.2 GB swapped, prior global OOM kills
  traced to Claude Code sessions). Adding a nightly 768 MB verify container is
  budgeted for, but the underlying pressure is a separate issue.

## 14. Known residuals at go-live (2026-08-18)

Recorded deliberately rather than left implicit. None blocks operation; all were
adjudicated during the final audit.

1. **A random verify password can reach the logs.** If `docker run` for the verify
   container fails, `CommandError` embeds the full argv, including
   `-e POSTGRES_PASSWORD=<token>`, which then appears in journald and in the failure
   mail. Unexploitable — the token is `secrets.token_urlsafe(24)`, generated per verify,
   for a `--network none` container that is unreachable and destroyed in the `finally`
   block — but worth redacting when this code is next touched.
2. **If `BACKUP_ROOT` itself cannot be created, `status.json` cannot be written.** The
   run still logs an ERROR, attempts mail, and exits non-zero. Strictly better than the
   pre-audit behaviour (an unhandled crash with no trace), but the second channel is
   unavailable in exactly that case.
3. **Sidecars are `chmod 0600` immediately after write**, leaving a sub-millisecond
   window at the default mode. The `0700` parent contains it throughout.
4. ~~**No `OnFailure=` on the units.**~~ **Resolved** — `copi-backup.service` carries
   `OnFailure=copi-backup-failure@%n.service` (commit c7c3427), the fifth independent
   path. Note the consequence: that unit runs `copi-backup report`, so the weekly
   heartbeat renderer IS the independent alert path and its staleness check must stay
   per stack (see 12.9).
5. **`failure_injection.sh` is expensive.** Its sweep check invokes a real
   `copi-backup run`, dumping both stacks (~800 MB, ~4 minutes). Retention bounds the
   accumulation at RETENTION_COUNT per stack, but do not run it casually.
6. **The offsite hook has never been exercised.** `OFFSITE_CMD` ships empty. Its failure
   path now mails and exits non-zero (audit F7), but no real hook has ever run.

### Change log after go-live

**2026-08-30 — `RETENTION_COUNT` 5 -> 3, on the live host only.** A response to disk
pressure: `cp -a backup.env backup.env.bak-20260830` then
`sed -i s/^RETENTION_COUNT=5$/RETENTION_COUNT=3/`, verified with `prune --dry-run` and a
manual run. Defensible in the moment, but it left the spec, `backup.env.example` and the
`DEFAULTS` comment all still saying 5, and **no channel reported the divergence** — the
value appeared in no log, no mail and no status document.

Resolved 2026-09-16 by **adopting 3 as the documented value** in the spec, the template
and the code default, rather than by reverting the host. 3 was briefly set back to 5
during that day's work and then returned to 3 by operator decision; the rationale is in
§5. The durable fix was never the number: `config_drift()` now names any value that
differs from the shipped default, every night, in the journal.

**2026-09-15/16 — two nights with no backups at all.** `insufficient free space:
4,048,744,448 available, need >= 5,932,680,516`. Root cause was outside this system: a
rebuild and redeploy of the blackbird stack on the evening of 09-14 added image layers
and BuildKit cache. Recovery reclaimed 30.25 GB from `docker builder prune` alone, which
took the volume from 94% to 43%. Two properties of this design worked exactly as
intended and are worth keeping: the guard failed **closed** (three good dumps per stack
survived untouched, where day-based retention would have deleted them), and all four
alert channels fired. Two did not:

- the guard was global, so copi-blackbird was skipped on both nights despite needing
  884 MB against ~4 GB free (fixed: §4.1.3, per-stack);
- deleting Docker images reclaimed **0 bytes** while BuildKit still referenced their
  layers — 41 images removed for ~1.2 MB. Build-cache reclaim is the gate, not a
  cheap first step. Anything scheduling disk reclaim on this host must prune the
  build cache, and must do so **recurringly**: it regrows with every rebuild.

### Not yet confirmed

The first **scheduled** run had not occurred when this was written. Every piece of
evidence above comes from manual invocations, including two full runs under systemd
(`Result=success`). Confirm the 01:00 run with:

```bash
systemctl status copi-backup.service
journalctl -u copi-backup.service --since yesterday
sudo jq '{last_run_utc,last_success_utc,ok}' /var/backups/copi/status.json
```
