# Production migration to alembic 0023 (`cohort-db-conversations`)

**Audience: an operator or agent who has not done any of the analysis behind this.**
You do not need to understand the branch to run this. You do need to follow the order,
and you need to stop when something says STOP.

Supported starting points: **0018** (`main` before PR19) and **0019**. Both are tested.

The executable half of this runbook is `scripts/migrate/run_migration.sh`. This document
explains *why* each step is where it is, which is what you need when a step fails.

---

## 0. The five hard rules

1. **Take the backup. Verify the backup.** Migration 0019 is a one-way door (§9).
   `run_migration.sh --apply` does both for you and refuses to continue if either fails.
2. **Migrate the database BEFORE deploying the application code.** Not the other way
   round. §8 explains what breaks in each direction.
3. **`alembic downgrade` is not a rollback.** It either destroys data silently or refuses
   to run. Your rollback is a restore from the dump. See §9 — read it before you start,
   not after.
4. **Never let the DSN default, and always run these tools inside the container.**
   `alembic.ini` falls back to `postgresql+asyncpg://copi:copi@localhost:5432/copi`, and
   `env.py` only overrides it when `DATABASE_URL` is set — so a migration run with no DSN
   targets whatever answers on `localhost:5432`. Two measured facts on this machine:
   the compose file does **not** publish Postgres to the host (nothing listens on
   127.0.0.1:5432, so a host-side run fails closed), but the bare hostname `postgres`
   resolves from the host to **195.35.25.84 — a public IP** via a LAN search domain. A DSN
   copied out of this runbook and run on the *host* therefore points at a stranger's
   server, not your database. Inside the container `postgres` is the compose service and is
   correct. Always pass `--database-url` or export `DATABASE_URL`;
   `run_migration.sh` refuses to run without one and does the `docker compose exec` for you.
5. **Alembic's own output is not evidence.** "Running upgrade 0018 -> 0019" is printed
   before the transaction commits. A bad `env.py` once made all 18 migrations log success
   and then silently roll the entire chain back, leaving no `alembic_version` row at all.
   Always read the revision back out of the database. Step 6 of the script does this.

---

## 1. What this migration actually does

Five revisions, applied as one chain:

| Revision | Change |
|---|---|
| `0018 -> 0019` | 7 new columns on `agent_messages` (the DB becomes the primary message store), 3 new indexes, the unique constraint `uq_agent_messages_run_ts` (itself backed by a 4th index), and `agent_id` becomes nullable. This is the expensive one. |
| `0019 -> 0020` | Creates `pi_dm_messages` (+2 indexes) and the `pi_dm_direction_enum` type. |
| `0020 -> 0021` | 2 indexes for the DB inbox pollers' `created_at` cursor. |
| `0021 -> 0022` | Creates `cohorts`, `cohort_memberships`, `cohort_audit_events` (+4 indexes). |
| `0022 -> 0023` | 3 synthesis-provenance columns on `researcher_profiles`. |

Verified properties of the chain, measured rather than assumed:

- **No migration in the chain issues a data-mutating statement against existing rows.**
  Counted directly from the five migration files: 0 `op.execute`, and 0 `UPDATE` /
  `INSERT INTO` / `DELETE FROM` in any `upgrade()`. Confirmed against live data too — an md5
  fingerprint over all 11 pre-0019 columns of `agent_messages` is byte-identical at every
  revision from 0018 to 0023.
- **No table rewrite.** `pg_class.relfilenode` for `agent_messages` is unchanged across the
  chain. The new columns are added with non-volatile defaults, which Postgres 11+ applies
  as metadata only.
- **Partial application is impossible.** `alembic/env.py` deliberately does *not* pass
  `transaction_per_migration`, so all five revisions run in a single transaction. Verified
  by `pg_terminate_backend`-ing the backend mid-chain twice: both times the database came
  back at the original revision with nothing applied.
- **Existing rows keep their row count.** Confirmed on a 463-row production-like fixture:
  463 before, 463 after.

The cost is that every lock the chain takes is held until the final commit, and 0019 takes
`ACCESS EXCLUSIVE` on `agent_messages`. That is why §3 and §4 exist.

---

## 2. Measure production first (read-only, safe to run any time)

Run these against production **before** you plan the window. They are pure `SELECT`s.
They work at 0018 and at 0023, so you can also run them afterwards to compare.

Open a psql shell in the container (no `-T` — you want a terminal):

```bash
docker compose exec postgres psql -U copi -d copi
```

```sql
-- Q1. Scale. Drives how long the lock is held (§3).
SELECT (SELECT count(*) FROM agent_messages)                     AS agent_messages_rows,
       pg_size_pretty(pg_table_size('agent_messages'))           AS heap,
       pg_size_pretty(pg_indexes_size('agent_messages'))         AS indexes,
       pg_size_pretty(pg_total_relation_size('agent_messages'))  AS total,
       (SELECT count(*) FROM simulation_runs)                    AS runs;

-- Q2. Will migration 0019 abort? Anything other than 0 means STOP and read §5.
SELECT count(*) AS duplicate_groups,
       coalesce(sum(n) - count(*), 0) AS rows_above_one_per_group
FROM (SELECT simulation_run_id, message_ts, count(*) AS n
        FROM agent_messages WHERE message_ts IS NOT NULL
       GROUP BY 1, 2 HAVING count(*) > 1) d;

-- Q3. Legacy inventory and rollback blockers (§9).
SELECT count(*)                                                   AS total,
       count(*) FILTER (WHERE agent_id IS NULL)                    AS blocks_downgrade_past_0019,
       count(*) FILTER (WHERE message_ts IS NULL)                  AS null_message_ts,
       count(*) FILTER (WHERE message_ts LIKE 'local:%')           AS locally_minted,
       count(*) FILTER (WHERE message_ts IS NOT NULL
                          AND message_ts NOT LIKE 'local:%')       AS slack_shaped
FROM agent_messages;

-- Q4. Anything that would block (or be blocked by) the ACCESS EXCLUSIVE lock.
-- An `idle in transaction` row here is the dangerous one: it will never finish on its own.
SELECT pid, state, now() - xact_start AS xact_age, left(query, 60) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid()
  AND xact_start IS NOT NULL
ORDER BY xact_start LIMIT 10;
```

Also check free disk, because 0019 and 0021 add six indexes to `agent_messages` (three plus
the unique constraint's, then two more). Ask the container about its
own data directory rather than guessing the volume name on the host:

```bash
docker compose exec -T postgres df -h /var/lib/postgresql/data
docker compose exec -T postgres psql -U copi -d copi \
  -c "select pg_size_pretty(pg_database_size(current_database()))"
```

If `Use%` is in the high 90s, stop and reclaim space first. A full data volume during index
creation is a much worse failure than a postponed window. (`docker system prune` and
`docker builder prune` are usually where the space went on a dev box; do not run either
against a production host without knowing what is on it.)

Preflight (§6) runs stricter versions of all of these and blocks on them. Q1–Q4 exist so
you can size the window *before* touching anything.

---

## 3. How long the window needs to be

Measured on seeded copies of this schema, on the machine this tooling was built on. Treat
them as order-of-magnitude for your own hardware, and re-measure on a restored copy if the
window is tight:

| `agent_messages` rows | `0018 -> 0021` | `0021 -> 0023` |
|---|---|---|
| 10,000 | ~0.11 s | ~2 s |
| 100,000 | ~0.75 s | ~2 s |
| 500,000 | ~5.1 s | ~2 s |
| 1,000,000 | ~7.9 s | ~2 s |
| 2,000,000 | ~30.6 s | ~2 s |

The second hop is effectively constant — it creates empty tables and adds columns to a
small table. All the cost is index-building in 0019/0021, which scales with row count.
Preflight check 9 makes the same estimate from your actual row count and prints it, so you
do not have to interpolate this table by hand.

Index storage for `agent_messages` grew from **96 MB to 565 MB at 2.5 M rows** in testing.
Size your headroom from Q1, not from that number.

**Writes to `agent_messages` are blocked for the whole window.** This was verified, not
inferred: a concurrent writer blocks until the chain commits. Reads that start *before*
the migration continue; reads that arrive *after* the `ACCESS EXCLUSIVE` request queues
behind it and also block. Treat the window as a full outage on that table.

---

## 4. The lock timeout, and why you want it

`ALEMBIC_LOCK_TIMEOUT_MS` defaults to **10000** (10 s). It bounds only how long the
migration *waits to acquire* a lock. It is not `statement_timeout` and will not cancel a
legitimately long index build partway through.

Without it, one forgotten `BEGIN; SELECT …` parks the migration forever, and because a
pending `ACCESS EXCLUSIVE` request queues ahead of new readers, every subsequent query on
`agent_messages` stalls behind it — an unbounded outage with nothing to end it.

Verified behaviour with a real blocker holding `AccessShareLock`: the migration failed
after ~12 s with `LockNotAvailableError`, the transaction rolled back cleanly, and
`alembic_version` was still `0018`. **A lock timeout costs you nothing but the attempt.**

If you hit it: stop the writers and re-run.

```bash
docker stop -t 30 agent-run        # SIGTERM; -t 30 lets an in-flight LLM call finish
```

Do **not** use `docker rm -f` / `kill -9` on `agent-run`: SIGKILL skips the shutdown flush
and permanently loses the in-flight turn's messages. The DB, not Slack, is the durable
store.

It is an environment variable, not a flag. Raise it only if you have a specific reason:

```bash
ALEMBIC_LOCK_TIMEOUT_MS=30000 ./scripts/migrate/run_migration.sh --apply
```

`0` means wait forever. Don't.

---

## 5. Duplicate `(simulation_run_id, message_ts)` rows

Migration 0019 creates `uq_agent_messages_run_ts`. If duplicates exist, the migration
aborts — and **Postgres names only ONE conflicting key per failed index build**, so
fixing them by reading the error message means one migration attempt per duplicate group.

Preflight check 4 lists **all** groups with their row ids in one pass (up to
`--max-duplicate-groups`, default 200; the *count* is never truncated, and the output tells
you when it has truncated the listing).

`NULL` `message_ts` rows are excluded on purpose: Postgres `UNIQUE` treats NULLs as
distinct, so they cannot violate the constraint. Verified — three NULL-ts rows in one run
coexist with the constraint.

### Fixing them

```bash
DSN=postgresql+asyncpg://copi:copi@postgres:5432/copi

# Dry run. Runs in a READ ONLY transaction — it cannot write. Verified inert by
# checksumming the table before and after.
docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" app \
  python scripts/migrate/remediate_duplicates.py

# Apply. Takes SHARE ROW EXCLUSIVE on agent_messages, re-checks inside the same
# transaction, and rolls back if any group would remain.
docker compose exec -T -e PYTHONPATH=/app -e DATABASE_URL="$DSN" app \
  python scripts/migrate/remediate_duplicates.py --apply
```

Strategies:

- **`renumber`** (default, non-destructive): never deletes a row. Gives duplicates new
  locally-minted ids where that is safe. **Use this.**
- `keep-earliest` / `keep-latest` (destructive, opt-in): additionally `DELETE` the
  redundant copies of byte-identical groups.

Divergent groups — two rows sharing a key but with *different* content — are renumbered,
never deleted, under every strategy. Two rows that both carry a real Slack timestamp and
disagree are refused entirely and reported as `needs_human`: that combination means
something upstream is wrong and a script guessing which one is canonical would be worse
than stopping.

Exit codes: `0` clean/applied · `1` duplicates remain or would remain · `2` found in a dry
run, all resolvable · `3` operational failure · `64` usage error. (`64`, not argparse's
`2`, because `2` already means "duplicates found".)

---

## 6. Run the migration

### 6a. Rehearse. This writes nothing.

```bash
export DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi
./scripts/migrate/run_migration.sh
```

Every tool in `scripts/migrate/` is dry-run by default, deliberately: an operator who
learns the convention from one must not be caught out by another.

Exit `0` = clear · `1` = **STOP**, a check failed · `2` = warnings, your judgement ·
`3` = operational failure · `64` = usage error.

Exit 2 is the normal outcome of a first rehearsal on real data: checks 11 and 12 warn (see
below). It means "read these, then decide", not "something is broken". In `--apply` mode
warnings do not stop the run, because choosing to apply *is* the decision — so a successful
apply is `0` even if preflight warned.

The 13 preflight checks:

```
 1. Stamped alembic revision is a supported starting point
 2. Exactly one alembic head, no duplicate revision ids
 3. The 0019 stamp is the content 0019, not one of the other 0019s
 4. No duplicate (simulation_run_id, message_ts) in agent_messages
 5. Objects the pending revisions create do not already exist
 6. Rows that would block a downgrade past 0019 (agent_messages.agent_id IS NULL)
 7. No sessions that would block (or be blocked by) the ACCESS EXCLUSIVE lock
 8. Migration harness commits what it applies (alembic/env.py)
 9. Sizing and expected lock window
10. Disk headroom for the indexes 0019/0021 add
11. Legacy-row inventory (rows that will have content = '')
12. Recent, non-trivial backup exists
13. Row-count snapshot written for postflight
```

Check 3 deserves a note. **Three** different files in this repository's history declared
`revision = "0019"`, all revising 0018 — enumerated by parsing every historical blob under
`alembic/versions/`, not from memory:

| File | Branch | Signature it leaves |
|---|---|---|
| `0019_agent_message_content.py` | this chain — the one you want | `agent_messages.content` |
| `0019_add_cohorts.py` | `cohort-agent-isolation` | `cohorts` table |
| `0019_add_hidden_to_proposals.py` | `coPI-podcast` | `thread_decisions.hidden` |

A database stamped `0019` by either of the other two is missing the content columns the
application requires, and **`alembic upgrade` does not notice**. Both outcomes were measured
on fixtures in exactly those states:

- **Cohort 0019** — `alembic upgrade 0023` applies 0020 and 0021, then dies at 0022 with
  `DuplicateTableError: relation "cohorts" already exists`. The revision stays `0019` and
  nothing is applied (one transaction). Loud, and safe.
- **Podcast 0019** — `alembic upgrade 0023` **exits 0 and stamps `0023`**, having run 0020,
  0021, 0022 and 0023 without complaint, while `agent_messages.content` does not exist and
  neither does `uq_agent_messages_run_ts`. Alembic reports complete success on a database
  the application cannot run against. This is the silent one, and it is why check 3 exists.

Check 3 probes for each signature separately and names the one it actually found, because
the remediation differs: the cohort tables must be dropped so 0022 can create them properly,
whereas the two `hidden` columns are orphaned but harmless and are better left in place. If
it finds a `0019` stamp matching none of the three, it refuses and tells you to inspect by
hand rather than guess.

If preflight is somehow bypassed, **postflight is the backstop**: run against the silently
"successful" podcast-0019 database it reports **6 FAIL, exit 1**. That is the whole reason
step 7 checks the schema instead of trusting the revision stamp.

Checks 11 and 12 normally `WARN`. Check 11 warns because legacy rows genuinely will have
`content = ''` (§7). Check 12 warns in rehearsal mode because no dump was taken. Read
both; neither blocks.

### 6b. Apply.

```bash
./scripts/migrate/run_migration.sh --apply
```

Seven steps, in this order and for these reasons:

1. **Container runs current code.** The `Dockerfile` does `pip install .`, baking a copy of
   `src/` into site-packages. For `python scripts/X.py`, CPython sets `sys.path[0]` to the
   script's directory, so `/app` is *not* on the path and `import src` resolves to the
   baked — possibly days-old — copy. Every step passes `PYTHONPATH=/app`; this step proves
   it worked by asserting `src.__file__ == /app/src/__init__.py` and that `Cohort` imports.
2. **Resolve the DSN and print it** (password masked). Refuses to run without one.
3. **Backup**, before preflight, so a *blocked* preflight still leaves you with a dump.
   Dumps `-Fc` inside the container, verifies the archive is readable with `pg_restore -l`
   there, then copies it to the host and re-checks the size. A dump whose table of contents
   cannot be read is a file, not a backup.
4. **Preflight.** Exit 1 stops here.
5. **`alembic upgrade`** — one command, so the whole chain is one transaction.
6. **Read `alembic_version` back out of the database.** See rule 5 in §0.
7. **Postflight.**

If you have a verified backup the script cannot see (managed snapshots, base backup + WAL):

```bash
./scripts/migrate/run_migration.sh --apply \
  --backup-verified-elsewhere "nightly base backup + WAL, restore tested 2026-08-04"
```

That flag makes you *write down* what you are asserting, and the reason is echoed into the
run's output. It is not a way to skip having a backup, and it is the **only** way to stop
this script taking its own dump — there is deliberately no bare "skip the backup check"
flag, so nobody can turn the check off without stating a reason.

### 6c. What postflight proves

13 checks. Check 1 is the revision stamp, and its own output says the stamp proves nothing
on its own — the other 12 check the schema:

```
 1. alembic_version is exactly the target revision
 2. Exactly one alembic head, no duplicate revision ids
 3. Every table 0020/0022 creates exists
 4. Every column 0019/0020/0023 adds exists, with the right type and nullability
 5. Every index 0019/0020/0021/0022 creates exists, on the right columns
 6. Constraints 0019/0022 add exist with the right definition
 7. pi_dm_direction_enum has exactly the expected values
 8. No invalid indexes (pg_index.indisvalid / indisready / indislive)
 9. No unintended NULLs in the columns the migrations declare NOT NULL
10. No foreign-key orphans, and every FK is convalidated
11. Row counts match the preflight snapshot
12. No ORM drift (nothing the models require is absent from the database)
13. The ORM at HEAD can query every model
```

Check 11 compares against the snapshot preflight wrote, which is why the two must be run
as a pair — `run_migration.sh` handles that. Checks 12 and 13 are the ones that catch
"schema applied but the application still can't run".

**Postflight must be 0 FAIL before you deploy code.**

---

## 7. Legacy rows: what the migration cannot give back

0019 adds `content` with a default of `''` and `posted_at` with a default of `0`. Rows that
existed before the migration therefore end up claiming *"this message had an empty body and
was posted at the Unix epoch"*. That is a semantically false statement about real data, and
no migration can fix it, because the bodies were never in the database — they were only
ever in Slack.

Consequences you should expect, and which are already handled in the code:

- Legacy rows all share `posted_at = 0`. Any `ORDER BY posted_at DESC … LIMIT n` therefore
  has ties, and Postgres may return a *different* page each time. `src/routers/agent_page.py`
  orders by `posted_at DESC, created_at DESC, id DESC` — a total ordering — for exactly this
  reason. If you add a paged query over `agent_messages`, do the same.
- Preflight check 11 splits legacy rows into **Slack-recoverable** and **permanently
  unrecoverable**. Read that number before the window so nobody is surprised by it after.

Step 8 recovers what Slack still has.

---

## 8. After the migration, in this order

### Step 8 — repair the Slack mirror mapping

```bash
docker compose exec -T -e PYTHONPATH=/app app python scripts/backfill_slack_ts.py          # report
docker compose exec -T -e PYTHONPATH=/app app python scripts/backfill_slack_ts.py --apply  # write
```

This asks Slack which timestamps actually exist and writes only confirmed ones. Rows Slack
does not recognise are left `NULL`, which is now the truthful value — the code no longer
*infers* the mapping, because inferring fabricated timestamps that were then handed to
`chat.postMessage` as a `thread_ts`.

It needs a valid bot token in every affected channel. It is read-only against Slack, only
ever writes `slack_ts`, and is safe to re-run.

**Exit 2 means some rows were UNVERIFIED — not that they were absent.** Unverified means
Slack did not answer for them (rate limit, token missing from that channel, channel
archived). Re-run once the cause is fixed. Do not read exit 2 as "done".

### Step 9 — deploy the application code, then restart

```bash
docker compose up -d --build app worker
```

**Order matters, and only one order is safe.** The new code requires columns that only
exist at 0023, so code-before-migration fails immediately and obviously. Migration-before-code
is the safe direction: the old code does not reference the new columns, and the new columns
all have defaults, so old code keeps working against the new schema during the gap.

One real gap exists in that window: `_slack_parent_ts_from_db` has no content filter, so a
private-channel close marker can resolve its parent to `None` and not be mirrored to Slack.
Messages themselves keep mirroring correctly — `_slack_parent_ts` returns `thread_ts` when
the root row is missing. (PR19's own deploy-order warning claims replies stop being
mirrored. That claim is wrong; this is what actually breaks.) Keep the gap short and this
costs you one marker.

### Step 10 — start the simulation last

```bash
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0
```

Last, because it is the heaviest writer to `agent_messages`. Starting it before app+worker
are up on the new code means it writes through code paths the rest of the deployment does
not yet agree with.

Roster changes do **not** need a restart (`_sync_roster_from_db` re-reads every ~30 s), but
**code** changes do: the process only loads modules at startup.

---

## 9. Rollback: read this before you start

### `alembic downgrade` is not a rollback. Both of its outcomes are bad.

Verified on live databases, twice, just now:

**If no PI messages exist** (`agent_id IS NULL` count is 0 in Q3):

```
$ alembic downgrade 0018
exit=0
rows=463          <- unchanged
content column:   GONE
pi_dm_messages:   GONE
cohorts:          GONE
```

It **exits 0**, reports success, preserves the row count exactly — and destroys every
message body, every PI direct message, and every cohort. A row-count check will not notice.
This is the single most dangerous command in this runbook.

**If any PI message exists:**

```
$ alembic downgrade 0018
sqlalchemy.exc.IntegrityError: NotNullViolationError:
  column "agent_id" of relation "agent_messages" contains null values
  [SQL: ALTER TABLE agent_messages ALTER COLUMN agent_id SET NOT NULL]
exit=1
revision now: 0023   <- unchanged, and the PI row survived
```

It refuses. The single transaction rolls back cleanly and nothing is lost — but you have no
downgrade path. Note the shape of this: the downgrade is blocked *exactly when* there is
real PI data to protect, and succeeds destructively *exactly when* the bodies it deletes are
the only copy.

**Therefore: your rollback is a restore from the dump.**

### If postflight fails

Do **not** deploy application code. Nothing is half-applied — the chain is one transaction,
so either it all committed or none of it did. Postflight failing after a committed chain
means the schema is not what 0023 should produce, which is a bug to investigate, not a
partial state to repair.

1. Read which checks failed. Checks 3–7 name the exact missing object.
2. Confirm the revision independently:
   ```bash
   docker compose exec -T postgres psql -U copi -d copi -c 'select * from alembic_version'
   ```
3. If you need to get back to where you started, restore the dump:
   ```bash
   docker stop -t 30 agent-run || true
   docker compose stop app worker

   docker compose cp backups/copi_pre0023_<timestamp>.dump postgres:/tmp/restore.dump
   docker compose exec -T postgres psql -U copi -d postgres \
     -c 'ALTER DATABASE copi RENAME TO copi_failed_migration'
   docker compose exec -T postgres psql -U copi -d postgres -c 'CREATE DATABASE copi'
   docker compose exec -T postgres pg_restore -U copi -d copi --exit-on-error /tmp/restore.dump

   docker compose exec -T postgres psql -U copi -d copi -c 'select * from alembic_version'
   ```
   Rename rather than drop: keep the failed database until you have confirmed the restore
   is good. `--exit-on-error` is not optional — without it `pg_restore` reports success
   after partially restoring.
4. Verify the restore before starting anything: row counts against Q1/Q3, and the revision
   should read `0018` or `0019` again.

---

## 10. Quick reference

| | |
|---|---|
| Orchestrator | `scripts/migrate/run_migration.sh` — `0` clear/applied · `1` blocked · `2` rehearsal raised warnings · `3` operational · `64` usage |
| Preflight | `scripts/migrate/preflight.py` — `0` ok · `1` blocked · `2` warnings |
| Postflight | `scripts/migrate/postflight.py` — `0` verified · non-zero: do not deploy |
| Duplicates | `scripts/migrate/remediate_duplicates.py` — `0` clean · `1` remain · `2` found (dry run) · `3` operational · `64` usage |
| Slack mapping | `scripts/backfill_slack_ts.py` — `0` all verified · `2` some UNVERIFIED |
| Lock wait | `ALEMBIC_LOCK_TIMEOUT_MS`, default `10000` ms |
| Backup dir | `MIGRATE_BACKUP_DIR`, default `backups/` (gitignored) |
| Services | `MIGRATE_SERVICE` (default `app`), `MIGRATE_PG_SERVICE` (default `postgres`) |

Every Python tool here takes `--database-url` and defaults to `$DATABASE_URL`; all are
dry-run unless given `--apply`; all must be run with `PYTHONPATH=/app` inside the container.

---

## 11. What has been tested, and what has not

Tested end to end on seeded production-like databases:

- **From 0018**, with 463 rows (300 Slack-born, 120 `local:`, 40 NULL-`message_ts`) and 3
  planted duplicate groups: rehearsal blocked with all 3 groups listed → remediation dry
  run inert (checksum identical) → `--apply` cleared them → migration applied → revision
  read back as 0023 → postflight 13 checks, 0 FAIL → 463 rows preserved.
- **From 0019**, with 151 rows including a PI row (`agent_id IS NULL`): preflight warned
  (correctly) that downgrade is blocked, migration applied, revision 0023, postflight 13
  checks 0 FAIL, 151 rows preserved.
- Lock timeout against a real blocker: failed fast at ~12 s, revision unchanged.
- Both downgrade outcomes in §9, on live databases.
- Mid-chain `pg_terminate_backend`, twice: no partial application.
- **The restore path in §9, as a full drill.** Dump a seeded 0018 database → migrate to
  0023 → destroy half the rows → run the §9 commands verbatim → 463 rows back, revision
  back to `0018`, and an md5 over `(id, message_ts, agent_id, channel_id)` for all 463 rows
  **byte-identical to the pre-migration source**. Then re-ran the whole migration on the
  restored database: 0023, postflight 0 FAIL, 463 rows. So the dump restores, and what it
  restores can be migrated again — you get a second attempt, not just your data back.

**Not tested, and you should know it:**

- Any database larger than ~2.5 M `agent_messages` rows. The §3 timings are extrapolation
  beyond that.
- The restore drill above was run against a scratch database in this same cluster, with the
  `copi` role and extensions already present. It did not include starting the application
  against the restored database. **Do the drill on a copy of your own production data before
  your window**, not during it.
- A managed/hosted Postgres. Everything here assumes the `postgres` compose service and
  `docker compose exec`. On RDS or similar, the SQL and the alembic steps carry over; the
  backup and `docker compose exec` plumbing does not.
- Replication. Nobody checked what a standby does with a 30-second `ACCESS EXCLUSIVE` hold.
