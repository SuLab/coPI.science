# Production migration runbook (0018 → 0024, and 0024 → the current alembic head)

**Audience: an operator or agent who has not done any of the analysis behind this.**
You do not need to understand the branch to run this. You do need to follow the order,
and you need to stop when something says STOP.

**The target is not a number you configure.** `run_migration.sh` derives that target from `alembic/versions/`
— every `revision` id minus every `down_revision` id, which must leave exactly one — and refuses
to run if it does not. That one id is the alembic tree's single head, **0030** at the time of writing.
Pass `--target <rev>` only when you mean to stop somewhere short of the head, deliberately; the
script's banner says which of the two happened. The pinned constant this replaced went stale
twice, and the second time a bare `--apply` would have migrated to 0028, stamped it, verified it
and reported success while the application code expected 0029.

Supported starting points: every revision in the chain from **0018** (`main` before PR19) up to
the revision below the head. **0018**, **0019**, **0020** and **0021** are tested end to end.
**0024** — where a deployment sits after completing this runbook
once — is also a supported starting point: the same tooling (`run_migration.sh`,
`preflight.py`, `postflight.py`) covers the later 0024 → head chain, and §10 below documents that path plus the routine, gate-free path for
deploys that add no new revision. `preflight.py` derives its own list the same way
(`SUPPORTED_START_REVISIONS`), so the revision *this* deploy leaves you stamped at is already an
accepted starting point for the next one — which the hand-maintained list it replaced was not,
three times running.

**If your deployment tracks `main`, you are at 0021** — that is `origin/main`'s own alembic
head, since PR19 merged 0019, 0020 and 0021. Starting there is the *easiest* case: the
expensive 0019 index build and the duplicate-row risk are both already behind you. What remains
is 0022 (three empty tables), 0023 (three columns on a small table) — about ~2 s at any size —
and then the 0024 → head tail that §10 covers, whose measured lock windows are tabulated in R.6
of `docs/plans/2026-09-02-close-issues-20-27.md`. Starting from 0018 is the one that needs a
real window.

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
| `0023 -> 0024` | One `VARCHAR(20) NOT NULL DEFAULT 'pi_lab'` column on `agents`. Postgres 11+ fills a non-volatile default without a table rewrite, and `agents` is small, so this is seconds at any size. |

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
export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml
./scripts/redeploy.sh
```

See §10.1 for what this script does and why a raw `docker compose up -d --build app
worker` is unsafe against an already-running stack (it can find `migrate`'s exited
container from the last deploy and skip re-running it).

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

## 10. Routine deploys after 0024

Everything above this point covers the 0018 → 0024 chain and the gated tooling it was
built for. Once a deployment is at 0024, most future deploys need no operator ceremony at
all — a `migrate` one-shot compose service runs `alembic upgrade head` before `app`,
`worker` or `grantbot` start, on every `docker compose up`. That service lands in the same
pull request as this section, alongside the 0025–0029 revisions it exists to apply. Only a
migration big enough to need a window (ACCESS EXCLUSIVE locks, a long-running backfill,
anything that would block writers for more than a few seconds) still goes through the
gated path below.

### 10.1 The routine path: `scripts/redeploy.sh`, not a raw `up -d --build`

`app`, `worker` and `grantbot` each declare `depends_on.migrate.condition:
service_completed_successfully`, and `migrate`'s command is exactly `python -m alembic
upgrade head` — if the database is already at head (the common case for most deploys,
which change application code but no schema), alembic prints that there is nothing to do
and the container still exits 0, so this step never blocks a code-only deploy.

That `depends_on` condition is enough on a **cold start**: `migrate` has to be created
(and therefore run) before `app`/`worker`/`grantbot` can be created at all. It is **not**
enough on an **already-running** stack, which is what a redeploy actually is: `docker
compose $C up -d --build app worker grantbot` can find `migrate`'s existing container
from the LAST deploy already `Exited (0)` and treat that as satisfying the condition
without re-running it against the newly built image, so the OLD app/worker/grantbot
containers (running old code) keep serving requests (and, for grantbot, posting to
Slack) for however long the new migration takes to apply — exactly the window this
section exists to close (audit 2026-09-08 RC-6, #27 I2; audit 2026-09-10 R-4 folded
`grantbot` into the script, which has the identical `depends_on` shape and was
previously left out of it).

Use `scripts/redeploy.sh` for `app`/`worker`/`grantbot` instead of a raw compose
invocation. It enforces the ordering explicitly rather than relying on `depends_on`:
build `migrate`+`app`+`worker`+`grantbot`, **stop** `app`/`worker`/`grantbot` first (so
old code cannot serve during the window), start `migrate` and check its exit code, only
then start the new `app`/`worker`/`grantbot`, then `nginx -s reload` (the recreated
`app` container gets a new IP — see the nginx-stale-upstream-ip memory note). It refuses
to run unless both prod compose files are visible (via `$COMPOSE_FILE` or `-f`) and
never asks for orphan removal:

```bash
export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml
./scripts/redeploy.sh
# or: ./scripts/redeploy.sh -f docker-compose.prod.yml -f docker-compose.override.yml
```

`docker compose $C ps -a` shows `migrate: Exited (0)` and `docker compose $C logs migrate`
should never contain a traceback or `AccessDeniedException` (that error means
`docker-compose.override.yml` is missing the `migrate` service's `json-file` logging
entry — see the "Compose file set" section of `CLAUDE.md`).

`agent` is deliberately NOT part of `redeploy.sh`'s scope: it is a one-off
(`docker compose --profile agent run`), not a long-running service `up -d` would ever
recreate, and it is started later — well after `redeploy.sh` has already driven
`migrate` to completion — via its own restart runbook in `CLAUDE.md`.

### 10.2 The gated path: `run_migration.sh --via-run`

Use this path instead of the routine one whenever the migration itself needs a window —
i.e. whenever it is not safe to let `app`/`worker`/`grantbot` keep running (and taking
writes) while it applies. The full runbook for this is Part R of the deploy guide; the
load-bearing commands are:

Stop every writer first, in this order — `agent-run` (gracefully, so the in-flight turn
flushes to Postgres), then `grantbot`/`worker` (worker inserts into `publications`), then
`app` last so the no-web-service window is as short as possible:

```bash
cd /home/ubuntu/copi-python && . /tmp/deploy.env && [ -n "$C" ] || { echo "deploy.env missing — redo R.1"; exit 1; }
mkdir -p logs
docker logs agent-run > logs/run_$(date +%s).log 2>&1 || true
ls -t logs/run_*.log | tail -n +11 | xargs -r rm -f
docker stop -t 30 agent-run || true ; docker rm agent-run || true   # SIGTERM + 30 s flushes the in-flight turn to Postgres
docker compose $C stop grantbot worker                              # worker inserts into publications
# app goes LAST so the no-web-service window is as short as possible — but it MUST go:
#  - preflight check 7 BLOCKs on any idle-in-transaction session (preflight.py:474-494) and its own
#    remediation list says `docker compose stop app worker grantbot`;
#  - the alembic chain is ONE transaction, so 0025's ACCESS EXCLUSIVE on publications, 0026's ACCESS
#    EXCLUSIVE on private_channel_members + SHARE ROW EXCLUSIVE on users, and 0027's SHARE on 13 tables
#    are all held until the last statement commits.
docker compose $C stop app
# nginx now returns 502 (static `upstream app { server app:8000; }`) and goes unhealthy. EXPECTED for the
# window; do not restart nginx here.
docker compose $C exec -T postgres psql -U copi -d copi -c \
  "select pid, state, now()-xact_start as age, left(query,60) from pg_stat_activity
    where datname=current_database() and pid<>pg_backend_pid() and backend_type='client backend'"
# expect: no rows (or only this psql). Anything else must be understood before applying the migration.
```

If the container runs as a non-root user, fix bind-mount ownership before continuing (the
image's UID owns `profiles`/`data`; the bind mounts shadow that):

```bash
cd /home/ubuntu/copi-python && . /tmp/deploy.env && [ -n "$C" ] || { echo "deploy.env missing — redo R.1"; exit 1; }
sudo chown -R 10001:10001 profiles data      # UID fixed in Dockerfile Task 27.8; bind mounts shadow the image dirs
ls -ld profiles data                          # 10001 10001
```

With every writer stopped, apply the migration with `--via-run` — it runs the same
preflight/alembic/postflight steps as always, but through `docker compose run --rm
--no-deps -T` one-off containers built from the *new* image, since `app` is stopped and
there is no running container left to `exec` into:

```bash
cd /home/ubuntu/copi-python && . /tmp/deploy.env && [ -n "$C" ] || { echo "deploy.env missing — redo R.1"; exit 1; }
PW="$(grep -m1 '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)"
PW_ENC="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PW")"
export DATABASE_URL="postgresql+asyncpg://copi:${PW_ENC}@postgres:5432/copi"   # in-network DSN, password percent-encoded
unset PW PW_ENC
# Rehearsal first (writes nothing; every step runs in a one-off container from the NEW image):
./scripts/migrate/run_migration.sh --via-run --backup-verified-elsewhere "copi-backup run $(date -u +%FT%TZ) -> $PRE_DEPLOY_DUMP"
# Expect: the masked DSN line shows host `postgres`, db `copi`; exit 0 (or 2 with only WARN lines you can explain).
# Exit 1 = BLOCKED: read the check name, fix, re-run. Check 7 (blocking sessions) => something is still connected: stop the writers again.
ls -l data/preflight_snapshot.json             # written by the rehearsal on the HOST (bind-mounted by --via-run)
./scripts/migrate/run_migration.sh --via-run --apply --backup-verified-elsewhere "copi-backup run $(date -u +%FT%TZ) -> $PRE_DEPLOY_DUMP"
# Expect: "alembic_version = 0030" read back (the head the script derived and printed in its banner), postflight 0 FAIL, exit 0.
ls -l data/preflight_snapshot.json             # newer than the rehearsal's
unset DATABASE_URL
docker compose $C exec -T postgres psql -U copi -d copi -c 'select * from alembic_version'   # 0030
```

If alembic reports `LockNotAvailableError`, something is still connected: `docker compose $C ps -a`,
confirm `agent-run` is gone and **app, worker and grantbot are stopped**, re-run the `pg_stat_activity`
query, then re-run `--apply`. If it still times out,
`ALEMBIC_LOCK_TIMEOUT_MS=60000 ./scripts/migrate/run_migration.sh --via-run --apply ...` (the knob
bounds lock *wait*, not statement duration). If postflight prints any FAIL: **do not deploy code**;
the chain is one transaction and nothing is half-applied.

Before recreating `app`, `worker` and `grantbot` on the new image, confirm the routine
path's `migrate` service agrees the database is now at head:

```bash
cd /home/ubuntu/copi-python && . /tmp/deploy.env && [ -n "$C" ] || { echo "deploy.env missing — redo R.1"; exit 1; }
docker compose $C run --rm --no-deps -T migrate python -m alembic current    # prints 0030 (head)
```

### 10.3 Both paths are idempotent

Neither path re-applies a migration that has already run. `run_migration.sh` treats "already
at head" as success (exit 0), not a no-op error, and so does the `migrate` service's plain
`alembic upgrade head` — running either one twice in a row, or running the gated path after
the routine path already brought the database to head, is safe and does nothing the second
time. This is what makes the routine path safe to run unconditionally on every
`docker compose up`: a deploy that ships no new revision just pays the cost of alembic
checking the stamped revision against head and finding nothing to do.

### 10.4 Where the preflight snapshot lands

`--via-run` bind-mounts a host directory into each one-off container so that the ephemeral
preflight, alembic and postflight containers — which do not share filesystem state with
each other the way a single long-running container would — can still hand off the
preflight row-count snapshot. Point `MIGRATE_SNAPSHOT` at a directory the container's UID 10001 can write —
`data/preflight_snapshot.json` — because the one-off container runs as 10001 while `backups/` stays owned by the
invoking host user. With the default it lands in `backups/preflight_snapshot.json`
on the host (the default `MIGRATE_BACKUP_DIR`, gitignored); postflight reads it back from
the same path to verify counts after the migration applies. A fresh, newer timestamp on
that file after `--apply` (as in the commands above) is confirmation postflight had the
rehearsal's snapshot to compare against.

### 10.5 Host-reboot semantics: a reboot does not re-run `migrate`

`app`, `worker` and `grantbot` are `restart: unless-stopped` in `docker-compose.prod.yml`,
so a host reboot brings the Docker daemon back up and it restarts each of those containers
individually. `migrate` is `restart: "no"` — it is a one-shot container that already ran to
completion (`Exited (0)`) before the reboot, and the daemon does not restart exited
one-shots, so a reboot never re-runs `alembic upgrade head`. This is the expected, safe
behaviour: the schema was already at head before the reboot, so there is nothing for
`migrate` to apply, and app/worker/grantbot come back on the same image and the same schema
they were running before. It does mean a reboot is not a substitute for `docker compose $C
up -d --build app worker` after a real code-plus-migration deploy — if you deployed new code
with a new revision and the host rebooted before anyone ran `up` again, the reboot restarts
the OLD containers (pre-deploy image), not the new ones; `migrate` only runs as part of an
explicit `up`, never as part of a restart.

### 10.6 A non-zero `migrate` aborts the deploy, not the running services

`app`, `worker` and `grantbot` each declare `depends_on: migrate: condition:
service_completed_successfully`. If `migrate` exits non-zero — a bad revision file, an
unresolved lock, a schema conflict the deploy introduced — `docker compose up` does not
start or recreate any of the three: whatever was already running (the previous deploy's
containers, on the previous image and the previous schema) keeps serving traffic. There is
no partial-start window and no window where the new, unmigrated code is live. Fix the
underlying schema problem first (correct or roll back the bad revision file, or apply the
fix by hand with `docker compose $C run --rm --no-deps migrate python -m alembic ...`),
confirm `docker compose $C run --rm --no-deps -T migrate python -m alembic current` prints
`head`, and only then bring the new code up while bypassing the dependency gate:

```bash
docker compose $C up -d --no-deps app worker grantbot
```

`--no-deps` skips the `migrate` dependency check entirely — it does not re-verify the schema
for you, so only use it once you have independently confirmed the database is at head.

### 10.7 `certbot`/`nginx` OOM detection: `OOMKilled` stays `false`

`certbot`'s and `nginx`'s entrypoints are shell loops (`certbot`: `trap exit TERM; while :;
do certbot renew --quiet; sleep 12h & wait $!; done`; `nginx`: an `envsubst` + periodic
`nginx -s reload` loop) — PID 1 inside each container is `/bin/sh`, not the process that
actually does the work. When the kernel OOM-killer takes the child process (a `certbot`
renewal, or an nginx worker), PID 1 survives and the container keeps reporting
`Up`/`running`, so `docker inspect <container> -f '{{.State.OOMKilled}}'` stays `false` —
the container itself was never killed, only a process inside it. Do not rely on `OOMKilled`
for these two services; check the kernel and the daemon's own event log instead:

```bash
dmesg | grep -i oom
docker events --since 1h --filter event=oom
```

A hit in either output for `certbot` or `nginx` means their `mem_limit` (128m each, per
`docker-compose.prod.yml`) is being exceeded and the effective process — a renewal, or a
burst of TLS connections — is silently dying; raise the limit rather than trusting the
container's own health/restart signal to notice.

### 10.8 UID 10001 precondition: `profiles/` and `data/`, never `prompts/`

The image's runtime user is a fixed UID 10001 (Task 27.8). `profiles/` is bind-mounted from
the host into all four app-image services that write to it (`app`, `worker`, `agent`,
`grantbot`); `data/` is bind-mounted only into `agent` and `grantbot` — `app`/`worker` never
mount it (see `docker-compose.prod.yml`). Before recreating any of those services on a host
whose bind mounts are not already owned by 10001:

```bash
sudo mkdir -p data
sudo chown -R 10001:10001 profiles/ data/
```

Never chown `prompts/` — it is git-tracked (13 files) and read-only at container runtime;
chowning it to 10001 makes the next `git pull` on the host fail with "unable to unlink old
'prompts/…'" because the host user (typically `ubuntu`) no longer owns those files. `migrate`
has no bind mounts and writes nothing, so it needs no chown; `logs/` and `static/` are not
bind-mounted in prod either. After recreating, confirm the UID actually took:

```bash
docker compose $C exec app id                                                  # uid=10001(copi) gid=10001(copi)
docker compose $C exec app sh -c 'touch profiles/public/.w && rm profiles/public/.w'
```

See Part R.5/R.7 of `docs/plans/2026-09-02-close-issues-20-27.md` for this precondition in
the context of a full deploy, and `CLAUDE.md`'s "Before restarting" step for the agent-only
restart path.

---

## 11. Quick reference

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

## 12. What has been tested, and what has not

Tested end to end on seeded production-like databases:

- **From 0018**, with 463 rows (300 Slack-born, 120 `local:`, 40 NULL-`message_ts`) and 3
  planted duplicate groups: rehearsal blocked with all 3 groups listed → remediation dry
  run inert (checksum identical) → `--apply` cleared them → migration applied → revision
  read back as 0023 → postflight 13 checks, 0 FAIL → 463 rows preserved.
- **From 0019**, with 151 rows including a PI row (`agent_id IS NULL`): preflight warned
  (correctly) that downgrade is blocked, migration applied, revision 0023, postflight 13
  checks 0 FAIL, 151 rows preserved.
- **From 0020 and 0021**, with 120 rows each: check 1 passes, check 9 correctly reports
  that 0019's index build is already behind you rather than quoting a row-scaled window,
  migration applied, revision 0023, postflight 13 checks 0 FAIL, 120 rows preserved.
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
