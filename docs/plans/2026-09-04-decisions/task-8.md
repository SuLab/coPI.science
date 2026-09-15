# Task 8 — #20 COR-5: the carrier for the implicit PI review

**Status: RULED BY BRANCH OWNER 2026-09-04.** Escalated by
`docs/plans/2026-09-04-close-remaining-gaps.md` Task 8; ruled before the task started. The
implementer implements this ruling and fills in `## Evidence`. It does **not** re-decide it.

## Ruling

**Chosen: option (a) — the carrier is `thread_decisions.pi_engaged_at`, a nullable `timestamptz`.**
This is `audit-over-implementation.md`'s own recommendation (*Candidate A point 2*), which v1 adopted
against without engaging.

**`0029` carries exactly two things:** this column and Task 7's handled-marker. Nothing else.
Nullable, no server default, no backfill — absent means "unknown", which every reader must treat as
today's behaviour.

**Explicitly rejected: (b) making `proposal_reviews.user_id` nullable.** That column is
`ForeignKey("users.id", ondelete="CASCADE")` (`src/models/agent_registry.py:82-84`), so deleting a PI
would destroy the engine's block-clearing markers for every proposal that PI engaged with, and those
proposals re-block after the next restart. It also breaks
`tests/integration/test_proposal_review.py:621-666`, which manufactures its `IntegrityError` from
that very `NOT NULL` constraint. Matching the issue's literal wording is not worth a destructive
carrier.

**Explicitly rejected: (c) carve COR-5's second half out of `Closes #20`.** The reassurance it rests
on — "0 of 53 active agents with `AgentRegistry.user_id IS NULL`" — is one button press away from
false (`audit-phase8-functional.md` I3: `POST /profile/delete-account`, `src/routers/profile.py:259`,
leaves `status=active, user_id IS NULL`), and `0026` on this branch removed the `CheckViolation` that
had been an accidental guardrail. Task 25 closes that path, but the carrier must not depend on it.

Correct the two v1 errors before writing anything: `src/models/thread_decision.py` **does not
exist** — `ThreadDecision` is `src/models/agent_activity.py:213` and `ProposalReview` is
`src/models/agent_registry.py:70`. v1 swapped them and invented a filename.

## Evidence

Filled in by Task 8's implementer, 2026-09-04.

### What 0029 landed

`alembic/versions/0029_pi_engagement_and_inbound_state.py`. Two nullable `ADD COLUMN`s, no server
default, no backfill, no constraint, no index — and nothing else:

| Table | Column | Type | Null | NULL means | Written by |
|---|---|---|---|---|---|
| `thread_decisions` | `pi_engaged_at` | `timestamptz` | yes | no PI engagement recorded — today's behaviour, i.e. the rebuild re-blocks | Task 9, in `_persist_implicit_proposal_review` |
| `agent_messages` | `pi_inbound_state` | `varchar(16)` | yes | unknown — no DB inbound poller has claimed this row | Task 7, in `_poll_inbound_from_db` only |

Models: `src/models/agent_activity.py` — `ThreadDecision.pi_engaged_at` (`:266`) and
`AgentMessage.pi_inbound_state` (`:118`). Both
are in *that* file; `src/models/thread_decision.py` does not exist (v1's error, corrected above).

### The handled-marker's design — Task 7 codes against this

**`agent_messages.pi_inbound_state`, `varchar(16)`, nullable, three states.**

| Value | Means | The poller's response |
|---|---|---|
| `NULL` | unknown: no DB inbound poller has claimed this row | fall back to **today's** behaviour — dedup on `message_log.get_entry(r.message_ts)` |
| `'ingested'` | the poller appended this row's text to the `MessageLog` and has **not** confirmed `_handle_pi_inbound_entry` succeeded | re-run the handler, regardless of log presence; do not advance the cursor past it |
| `'handled'` | `_handle_pi_inbound_entry` returned without raising | skip, advance the cursor |

Set **only** by `SimulationEngine._poll_inbound_from_db` (`src/agent/simulation.py:3300-3398`), and
only on `is_bot = False` rows: `'ingested'` committed immediately after
`self.message_log.append(entry)` and **before** `await self._handle_pi_inbound_entry(entry)`;
`'handled'` committed immediately after it returns, alongside the cursor advance. No other writer,
in the engine or the web app. Nothing backfills it.

**Why three states and not a plain `handled_at` timestamp.** A two-valued marker cannot tell *"no
poller has claimed this row"* from *"the poller claimed it and its handler failed"*, and on this
codebase that difference is load-bearing, not theoretical:

- `_poll_channels` (`simulation.py:3225-3290`) appends a **Slack-origin PI message** to the log
  itself and applies the same side effects inline (`_check_pi_proposal_review`, `_reopen_thread`,
  `pi_context`, `handle_channel_tag`). `MessageLog.append` fires `_enqueue_persist`
  (`:4673`), so an `agent_messages` row with `is_bot = false` and a fresh `created_at` appears a
  moment later.
- `_poll_inbound_from_db` then re-reads that row: it filters on `simulation_run_id` and
  `created_at` **only** — there is no origin predicate — and today it skips the row *solely*
  because `message_log.get_entry(...)` is truthy.
- So under "dedup reads a two-valued marker; NULL ⇒ process", every tagged Slack PI message would
  get `_handle_pi_inbound_entry` run on it a second time, including the `handle_channel_tag` route,
  which produces an LLM reply. A duplicate bot reply per tagged Slack message, recurring — not a
  one-off.
- Keying the retry on the cursor instead (`r.created_at > self._pi_inbox_cursor`) does not separate
  them either: the Slack-poller row is also newer than the cursor.

`NULL` therefore has to keep meaning *"decide the way you decide today"*, which is exactly the
ruling's "absent means unknown, which every reader must treat as today's behaviour". `'ingested'` is
what makes `'handled'`'s absence readable, and it is durable, so the retry survives a process
restart and a cursor jump, not just the next tick.

**Why `MessageLog.append` not being idempotent stops mattering.** Once dedup reads
`pi_inbound_state`, `append`'s behaviour is no longer the dedup mechanism at all: the poller appends
first (so the PI's text is in the log even if the handler dies — COR-10(3)'s first half) and then
asks the *column* whether the side effects ran. `append` is free to be a no-op on a repeat, because
nothing reads its return value to decide whether to run the handler.

**Deliberately not done here.** The marker does not force any change to `src/agent/simulation.py`,
and this task made none — Group A tasks 9 and 10 still have to edit that file. Two things Task 7
will have to decide inside it, which the column supports but does not dictate: (i) the cursor is
advanced per row inside the loop, so a later row that succeeds can advance `_pi_inbox_cursor` past
an earlier row that failed; stopping the advance at the first failure in a batch keeps the retry
inside `PI_INBOX_LOOKBACK_S`; (ii) `'ingested'` must be **committed** before the handler runs or its
durability is fictional.

### Red first

`tests/integration/test_migration_0029.py` written before `0029` existed, run at `d9f0797`:

```
FAILED test_0029_adds_two_nullable_markers_and_downgrades_back_out
  E  AssertionError: assert '0028' == '0029'      # alembic_version after `upgrade head`
FAILED test_0029_downgrade_is_idempotent_when_the_columns_are_already_gone
  E  UndefinedColumnError: column "pi_engaged_at" of relation "thread_decisions" does not exist
FAILED test_0029_is_the_only_head
  E  AssertionError: 0028 (head)  /  assert ['0028'] == ['0029']
3 failed in 9.93s
```

After: `tests/unit/test_migration_checks.py tests/integration/test_migration_0029.py
tests/integration/test_migration_tooling_chain.py` → **187 passed in 44.51s**;
`tests/integration/test_harness_smoke.py` → 3 passed. `python -m alembic heads` → `0029 (head)`,
exactly one head, exit 0.

The round trip is asserted, not assumed: the test snapshots
`information_schema.columns` for both tables at 0028, upgrades, asserts the post-state (type,
nullability, **no** server default, and that pre-existing rows read NULL), asserts the delta is
*exactly* those two columns in each direction, then `downgrade 0028` and asserts the two column maps
are equal to the pre-state maps and both seeded rows survive.

### The chain against the production copy (plan Step 10)

**What I started from, exactly.** `v1's pristine_0024.dump does not exist in this repo` — confirmed;
the record that names it names a dump on the production host, and `backups/` is forbidden. Instead:
`CREATE DATABASE copi_m29a TEMPLATE copi_verify` on `copi-prodtest-db` (`127.0.0.1:55434`, user/pw
`copi`/`copi`), i.e. **an exact clone of `copi_verify` at revision 0028** — 8,460 `agent_messages`
(19 MB total relation), 1,017 `thread_decisions` (1,720 kB), 4,508 `publications`, 30 tables /
19,633 rows. That clone was then `alembic downgrade 0024`'d, because **0024 is where org1 production
actually sits** and 0028 is not in `SUPPORTED_START_REVISIONS`, so 0024→0029 is the chain the deploy
will really run. `copi_verify` itself was never written to: re-checked afterwards at `0028`, 8,460 /
1,017 rows, 0 columns named `pi_engaged_at`/`pi_inbound_state`. All three clones
(`copi_m29a/b/c`) were dropped.

| Step | Command | Exit | Wall |
|---|---|---|---|
| preflight | `preflight.py --database-url … --target 0029 --snapshot … --backup-verified-elsewhere …` | **2** (warnings only; 14 checks, 0 BLOCK, 3 WARN) | 0.85 s |
| upgrade | `alembic upgrade head` (0024 → 0029) | **0** | 1.32 s |
| postflight | `postflight.py --database-url … --target 0029 --snapshot …` | **0** (13 checks, 0 FAIL, 1 WARN) | 1.84 s |

The three preflight WARNs are pre-existing and unrelated to 0029: 2,210 `agent_messages` rows with
`agent_id IS NULL` (blocks a downgrade past 0019), 14 Slack-recoverable legacy empty-content rows,
and the `--backup-verified-elsewhere` override. Preflight check 5 counted **24** objects the pending
revisions create, including both of 0029's columns — proof that `REVISION_ORDER` and
`PLANNED_OBJECTS` really did learn 0029; before that edit `check_name_collisions` would have
reported green while checking nothing, and `revision_status` would have BLOCKed the run outright.
Postflight check 4 rose from 24 to **26 columns checked**. The downgrade was also exercised on the
copy: `alembic downgrade 0028` (1.12 s, exit 0) left 8,460 / 1,017 rows and 0 of the two columns.

**What postflight did NOT check — `postflight` exiting 0 is not sufficient evidence.**

- **C1**: its row-count check (check 11) is *systematically* blind to a concurrent deleter operating
  inside a `publications` duplicate group — every row a third party deletes from a group reduces
  0025's own delete count by exactly one, so the net always lands on the prediction. This branch
  added an identity check (`expected_deleted_ids` / `expected_kept_ids`) that catches the deleted-
  keeper case, but **it verified nothing in my run**: the copy has no duplicate `(user_id, pmid)`
  pairs, so the snapshot recorded `expected_deletions {}`. That guarantee is untested by this run.
- **M9**: with no `--snapshot`, postflight performs no row-loss verification at all and still prints
  "VERIFIED WITH WARNINGS" and exits 0. I passed `--snapshot` on every invocation; the figures above
  are only meaningful because of that.
- **Semantics.** Postflight checks that the two columns exist with the right type, nullability and
  absent default, and that the ORM can `SELECT` every model. It asserts **nothing** about values:
  not that pre-existing rows are NULL, not that `pi_inbound_state` only ever holds
  `'ingested'`/`'handled'` (there is deliberately no CHECK constraint), and not that NULL is read as
  "unknown". Those are `test_migration_0029.py`'s job and Task 7's/Task 9's.
- **Contention.** Preflight check 7 reported "no other client sessions on this database", so nothing
  here exercised lock contention, a blocked writer, or the `lock_timeout` path.
- **Its own WARN.** Check 12 (ORM drift) was a WARN — one operator artefact table
  (`email_notifications_expired_bak_20260814`) present in the copy that no model declares — and the
  run still exited 0. An `exit 0` here means "0 FAIL", not "0 findings".

### 0029's lock row (plan Step 11) — for Task 33 to apply to Part R.6

Measured on `copi-prodtest-db` (PostgreSQL 15, local container, warm cache, **no concurrent
sessions**), against `copi_m29b` — the second exact clone of `copi_verify` at 0028, so
`agent_messages` = 8,460 rows / 19 MB total relation and `thread_decisions` = 1,017 rows / 1,720 kB.
Statement times from `psql \timing`, three runs, executed inside one transaction and rolled back:

| Revision | Statement | Lock taken | Measured |
|---|---|---|---|
| 0029 | `ALTER TABLE thread_decisions ADD COLUMN pi_engaged_at timestamptz` | ACCESS EXCLUSIVE on `thread_decisions` | 0.77 – 1.27 ms |
| 0029 | `ALTER TABLE agent_messages ADD COLUMN pi_inbound_state varchar(16)` | ACCESS EXCLUSIVE on `agent_messages` | 0.28 – 0.42 ms |
| 0029 | whole revision (both `ALTER`s + the `alembic_version` update), locks held to COMMIT | both of the above | 1.6 – 3.1 ms |

`alembic upgrade` wall clock, same clone: 0028 → 0029 = **0.99 s** against a measured no-op baseline
(interpreter start + connect + version probe, the same command re-run at 0029) of **1.03 – 1.05 s**
— i.e. 0029's own work is *below the harness's measurement floor*. For the same data, the chain
0024 → 0028 took 1.21 s and 0024 → 0029 took 1.32 s; that 0.11 s delta is inside the run-to-run
noise of the baseline and should not be read as 0029's cost.

**What this figure is a measurement of, and what it is not.** It is a measurement of **0029's own
two statements** on a 0028 clone of the production copy, on this developer's machine, warm cache,
idle server, no concurrent sessions, local disk. Both are `ADD COLUMN … NULL` with no default, which
Postgres 11+ applies as a catalogue-only change — no rewrite and no per-row work — so the figure is
independent of row count and will not grow with `agent_messages`. It is **not** a measurement of the
0024→0029 chain's lock window: alembic runs the whole chain in ONE transaction, so every lock is
held until the final COMMIT, and that window is dominated by 0025's ACCESS EXCLUSIVE unique-index
build on `publications` and 0027's 20 non-concurrent `CREATE INDEX`es, not by 0029. And per
`audit-phase8-migration.md` **N4**, the "worst-case lock window 0.3 s" preflight prints is
`estimate_lock_window_ms(publications_rows)` — a linear model calibrated on 0019's `agent_messages`
index build, applied to a different table — so it is not a measurement of this chain either, and
none of the figures above inherit it. Nothing here is a prediction for EC2/EBS under load.

### Tooling ripple — nine files, not six

Beyond the six the plan named: `alembic/versions/0029_pi_engagement_and_inbound_state.py` (new),
`src/models/agent_activity.py`, `scripts/migrate/preflight.py` (`DEFAULT_TARGET`, `PLANNED_OBJECTS`,
`REVISION_ORDER`, and `check_sizing`'s operator-facing chain description, which still said the chain
ended at 0028), `scripts/migrate/postflight.py` (`EXPECTED_COLUMNS`; `DEFAULT_TARGET` is derived at
`:77` — verified, no separate edit), `tests/unit/test_migration_checks.py` (`:232`, `:908`, `:1225`,
`:1266`, plus a 0029 case in `test_new_chain_objects_are_planned`), `tests/integration/test_migration_0029.py` (new):

- `tests/integration/test_migration_tooling_chain.py:225,240` — `_preflight_snapshot` and
  `_postflight` defaulted `target="0028"`, so after `upgrade head` reaches 0029 postflight's
  `check_revision` fails on `0029 != 0028` and three tests go red. Changed to `pre.DEFAULT_TARGET`,
  which cannot drift again.
- `tests/integration/test_harness_smoke.py:17` — the deliberate head-revision pin, whose own comment
  says "bump it deliberately with each new migration". Bumped to `0029`.

Neither file is named by any task in the plan, and both were unmodified in the shared checkout when
edited. **Not touched, and needing a follow-up:** `scripts/migrate/run_migration.sh:73` still has
`TARGET="0028"`, so a bare `run_migration.sh --apply` would migrate production to 0028 and stop —
0029 unapplied, while the app code expects it. Bumping it also requires the two fake-`psql` shims in
`tests/unit/test_run_migration_sh.py:73,195` (which echo `0028` for the `alembic_version` read that
`run_migration.sh:365` compares against `TARGET`), and the title of `docs/production-migration.md`.
Those four belong to the deploy-runbook surface Task 33 owns. Until then the deploy must pass
`--target 0029` explicitly. Related, and also not mine to fix: `SUPPORTED_START_REVISIONS` does not
contain 0028 or 0029, so preflight will BLOCK the *next* migration off a database this one leaves at
0029 — the same gap that had to be closed for 0024.

## Consequence a closing comment must state

`#20`'s closing comment must state the **substitution** against the issue's literal `Fix:` wording.
The clause reads "require the sender be the owning PI; insert a `ProposalReview`". The first half
shipped as written. The second half is implemented as a nullable `thread_decisions.pi_engaged_at`
marker **instead of** a `ProposalReview` row, because `proposal_reviews.user_id` is
`ondelete="CASCADE"` to `users` and a PI deletion would therefore erase the engine's block-clearing
markers — the issue's literal instruction would have built a self-erasing record. The behaviour the
issue asks for (`_rebuild_agent_state` does not re-block the proposal after a restart) is delivered;
the row shape is not the one the issue names.
