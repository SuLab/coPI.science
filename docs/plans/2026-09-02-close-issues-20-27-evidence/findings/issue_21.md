# Issue #21 verification — Worker & background jobs (V11, V2, V3, V4)

Verified against `copi-prod` @ `18ba52c` (clean tree), 2026-09-02. All line numbers below are CURRENT.
Method: symbol grep + reading the actual function bodies, `git log -S` for the "fixed in #31" rows, and python
snippets against the real `src.services.email_inbound` module for the two mechanically testable COR-19 rows.
Nothing was executed against a database, Docker, Slack or AWS.

## 1. Summary table

| id | claim (one line) | verdict | key evidence | conf |
|---|---|---|---|---|
| V11-a | `ids.py` defines four slots; module default is `TsMinter(WRITER_WEB)` | ACCURATE / STILL PRESENT | `src/agent/ids.py:47-50`, `:111` | high |
| V11-b | `set_default_writer_id` called by web, engine, grantbot only | ACCURATE (lines drifted) | `src/main.py:113`, `src/agent/main.py:54`, `src/agent/grantbot.py:731,764` | high |
| V11-c | `src/worker/main.py` never imports `src.agent.ids` / never claims a slot | STILL PRESENT | `src/worker/main.py:6-18` (imports), whole file has no `ids` reference | high |
| V11-d | worker mints via `record_pi_message` and `migrate_public_thread_to_private` | STILL PRESENT (one stale ref) | `email_inbound.py:674`→`pi_inbox.py:120`; `email_inbound.py:638`→`private_channels.py:269` | high |
| V11-e | both paths gated on `enable_inbound_email` (default False) | ACCURATE | `src/config.py:152`, `src/worker/main.py:162` | high |
| V11-f | runbook step 4 mentions no writer slot | STILL PRESENT | `docs/inbound-email.md:84-90`; `grep -i writer` → no hits | high |
| V11-g | backfill scripts don't mint | ACCURATE | `grep mint_local_ts\|TsMinter scripts/` → no hits | high |
| V11-h | spec says "Three processes"; counts now wrong (4 slots / 5 with worker / 6 with slot 99) | STILL PRESENT | `specs/local-db-conversations.md:66-67`; `scripts/migrate/remediate_duplicates.py:131` | high |
| COR-17 | worker except block commits without rollback; DB error strands job in `processing` | STILL PRESENT (test *characterizes* the bug) | `src/worker/main.py:100-111`; `tests/integration/test_worker.py:760-827` | high |
| COR-18a | `started_at` written, never read | STILL PRESENT | only writer `worker/main.py:49`; no reader anywhere in `src/` or `scripts/` | high |
| COR-18b | no reaper for orphaned `processing` rows | STILL PRESENT | only other `"processing"` ref is display-only `src/routers/admin.py:126` | high |
| COR-18c | failed jobs re-queue with no backoff ("~15 s for 3 attempts") | STILL PRESENT — issue understates it: retry is **immediate** (no sleep) | `worker/main.py:127-137` sleeps only when `claim_job` returns None | high |
| COR-18d | `completed_at` set on failure | STILL PRESENT | `worker/main.py:110` | high |
| COR-18e | enum `'failed'` never written | STILL PRESENT | `src/models/job.py:23`; worker writes only processing/completed/dead/pending; also documented in `tests/unit/test_reachability.py:39-44` | high |
| COR-18f | onboarding self-heals only when `job is None` → wedged job = permanent spinner, no retry | STILL PRESENT | `src/routers/onboarding.py:79`; `templates/onboarding/profile_review.html:28` (spinner for none/pending/processing), `:47-55` (retry only under `'failed'`) | high |
| COR-19.1 | poison objects retried forever | FIXED (8f96f86, PR #31) | `email_inbound.py:33`, `:186-209`; test `tests/unit/test_email_inbound_hardening.py:259-292` | high |
| COR-19.2 | `MAX_REPLIES_PER_TOKEN_PER_HOUR` never enforced | FIXED (8f96f86, PR #31) | `email_inbound.py:52-65`, `:241-245`; tests `hardening.py:195-211` | high |
| COR-19.3 | `charset=unknown-8bit` → `LookupError` (codec lookup precedes `errors="replace"`) | STILL PRESENT — reproduced on the real helper | `email_inbound.py:386-389`; snippet output below | high |
| COR-19.4 | string rating → `TypeError`; `classify_reply` returns raw `json.loads` | STILL PRESENT — reproduced | `email_inbound.py:320-322`, `:516` | high |
| COR-19.5 | `list_objects_v2(MaxKeys=50)` unpaginated | STILL PRESENT | `email_inbound.py:165`; no `ContinuationToken`/`IsTruncated` anywhere in file | high |
| COR-19.6 | side effects (SES confirm, Slack post, migration) happen before `db.commit()` | STILL PRESENT | side effects `:335`, `:359`, `:605`, `:638`, `:655`, `:706`; commit is in the poller at `:179`, S3 delete `:182` | high |
| COR-32 | `_handle_instruction` returning False still marks notification responded + S3 delete; PI not told on token/channel failures | STILL PRESENT | caller `:338-353` (`mark_notification_responded` at `:348` unconditional); silent `return False` at `:682,:699,:704,:719`; emailing paths `:605-612`, `:655-662` | high |
| V4-1 | zero `rollback` in `email_notifications.py`; per-item excepts fall through to sweep commit | STILL PRESENT | `grep -c rollback` = 0; excepts `:208-214`, `:721-725`, `:912-916`; commits `:216`, `:726`, `:917` | high |
| V4-2 | phantom-sent: row created `status="sent"` + flushed before SES send, never cleared on failure | STILL PRESENT | `:331-340` vs send `:481-485`/fail `:493-495`; `_send_new_proposal_email` `:995-1004` vs `:1066`; bails `:240-250`, `:968-976` | high |
| V4-3 | `"expired"` never written | STILL PRESENT | `grep -c expired src/services/email_notifications.py` = 0; only `src/models/email_notification.py:42` comment | high |
| V4-4a | downgrade ladder dead: one increment, unreachable past 1, `MISSED_THRESHOLD=3` unreachable | STILL PRESENT | increment `:295`; bail `:247-250`; resets `:517`, `:523`, `:594`, `src/routers/settings.py:133`; dead code `:505-529` | high |
| V4-4b | delegate reply marks only the delegate's rows; PI's `sent` row is immortal | STILL PRESENT | `email_inbound.py:334,348` and `src/routers/agent_page.py:511,714` pass the *replier's* id; filter `email_notifications.py:608` | high |
| T-1 | `enable_inbound_email` defaults False | ACCURATE | `src/config.py:152` | high |
| T-2 | …and is unset in prod | NOT VERIFIABLE here (no prod access allowed). Local dev `.env` has no `ENABLE_INBOUND_EMAIL` key; `.env.example` doesn't list it either | — | low |
| T-3 | `proposal_review` sweep gates on `User` columns, deliberate per model comment | ACCURATE | `email_notifications.py:192-199`; `src/models/email_notification.py:99-105` | high |
| T-4 | all three sweeps run every 300 s | ACCURATE | `src/config.py:158`; `worker/main.py:141-157` | high |
| T-5 | sweeps "still write — `get_or_create_pref` inserts on a composite-PK table each cycle" | PARTIALLY ACCURATE — inserts only when the row is missing; SELECT-only afterwards. Other writes per cycle do exist | `email_notifications.py:56-77`, `:230-233`, `:267` | high |
| T-6 | V11 / V3-remainder / COR-32 all arm at runbook step 4 | ACCURATE | `docs/inbound-email.md:84-90` | high |
| DoD | "takes `worker/main.py` from 0 % coverage" | STALE — `tests/integration/test_worker.py` (15 tests, commit d732804, 2026-07-30) already exercises `claim_job`/`process_job`/`run_worker` | `tests/integration/test_worker.py:1-29` | high |

## 2. Per-item detail

### PR V11 — writer slot

**ids.py** (`src/agent/ids.py:47-50`):
```
WRITER_ENGINE = 0        # SimulationEngine._ts_minter (agent_messages)
WRITER_WEB = 1           # web app process (PI messages + DMs)
WRITER_GRANTBOT = 2      # grantbot process (funding posts)
WRITER_ENGINE_AUX = 3    # module default inside the engine process (PI DMs)
```
`:111 _default = TsMinter(WRITER_WEB)`. Four slots defined. Claimers: `src/main.py:113 set_default_writer_id(WRITER_WEB)`,
`src/agent/main.py:54 set_default_writer_id(WRITER_ENGINE_AUX)`, `src/agent/grantbot.py:731` and `:764` (`WRITER_GRANTBOT`).
Issue cited grantbot `:724/757` — drifted by 7 lines, same calls.

**Worker**: `src/worker/main.py` imports (lines 6-18) are asyncio/logging/signal/sys/uuid/datetime/sqlalchemy/`src.config`/
`src.models`/`src.services.profile_pipeline`. No `src.agent.ids`. The worker therefore mints in residue class 1 — the same as
the web app.

**Mint paths reached from the worker** (both inside `_handle_instruction`, only when `enable_inbound_email`):
- `email_inbound.py:674 await record_pi_message(...)` → `src/services/pi_inbox.py:120 ts = mint_local_ts()`.
  (Issue also cites `pi_inbox.py:151`; that is `record_pi_dm`, which nothing in `email_inbound.py` calls — stale/over-inclusive
  reference, does not change the verdict.)
- `email_inbound.py:638 await migrate_public_thread_to_private(...)` → `src/services/private_channels.py:269 ts = slack_ts or mint_local_ts()`
  (inside `_add_handover_message`, used by both online and offline migration paths).

**Gate**: `src/config.py:152 enable_inbound_email: bool = False`; `src/worker/main.py:162 if settings.enable_inbound_email and ...`.

**Runbook**: `docs/inbound-email.md:84-90` step 4 = flip flag + recreate app/worker. `grep -n -i "writer\|slot\|ids.py"` on the doc → no hits.

**Spec count**: `specs/local-db-conversations.md:66-67`: "Three processes mint into the same run — the engine, the web app and
GrantBot". Slots defined: 4. Processes that would mint once inbound is on: engine (0 and 3), web (1), grantbot (2), worker
(unclaimed → 1). `scripts/migrate/remediate_duplicates.py:131 REMEDIATION_WRITER_SLOT = 99`. Issue's "wrong three ways over" is
fair.

**Mitigation the issue missed**: `remediate_duplicates.py:189-215` introspects `ids.py` for every `WRITER_*` int and raises if
`REMEDIATION_WRITER_SLOT` is ever claimed, and `tests/unit/test_remediate_duplicates.py:726-727` asserts 99 is free — so adding
`WRITER_WORKER = 4` is safe against that script. No test asserts the worker claims a slot.

**Backfill scripts**: `grep -rl "mint_local_ts\|TsMinter" scripts/` → nothing. Claim accurate.

### PR V2 — COR-17 (no rollback)

`src/worker/main.py:100-111` (unchanged since 1342cc0; the issue's line numbers still match exactly):
```
        except Exception as exc:
            logger.error("Job %s failed: %s", job.id, exc, exc_info=True)
            job.last_error = str(exc)[:2000]

            if job.attempts >= job.max_attempts:
                job.status = "dead"
                ...
            else:
                job.status = "pending"  # Will be retried

            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
```
No `rollback` anywhere in the file (`git log -S rollback -- src/worker/main.py` → empty). `run_profile_pipeline` only
`flush()`es (`src/services/profile_pipeline.py:234, 293, 466, 497`; no `commit`). A flush-time DB error leaves the session in
needs-rollback state; the failure-path `commit()` raises `PendingRollbackError`, escapes `process_job`, is caught by
`run_worker`'s outer handler at `:170-172`, and the row stays `processing` (committed by `claim_job` `:48-51`) with
`last_error=None`.

**Test found**: `tests/integration/test_worker.py:760-827 test_a_database_error_in_the_pipeline_orphans_the_job_in_processing`
reproduces exactly this and **asserts the buggy behaviour** (`status == "processing"`, `last_error is None`,
`PendingRollbackError` escapes), with a message saying to flip the assertion when fixed. It is a characterization test, not a
fail-against-pre-fix test, so the issue's Definition-of-done test is still owed.

### PR V2 — COR-18 (reaper / backoff / status)

- `started_at`: `grep -rn started_at src/ scripts/` → written `worker/main.py:49`, declared `models/job.py:37`, and an unrelated
  `agent_activity.py:43`. Never read. STILL PRESENT.
- Reaper: `grep "processing" src/ scripts/` → only `src/routers/admin.py:126` (counts active jobs for display). None. STILL PRESENT.
- Backoff: `worker/main.py:127-137` — `asyncio.sleep(worker_poll_interval)` is in the `else:` branch (no job). After
  `process_job` returns the loop immediately re-enters `claim_job`, so a job re-queued as `pending` is re-claimed on the very
  next iteration with **no delay**. The issue's "~15 s" is an over-estimate; all 3 attempts burn as fast as the pipeline can
  fail. `tests/integration/test_worker.py:381-419` (T5.2) asserts the 3-attempts-then-`dead` behaviour but not timing.
- `completed_at` on failure: `:110`. STILL PRESENT.
- `'failed'`: `src/models/job.py:23` enum has it; the worker writes only `processing` (`:48`), `completed` (`:95`), `dead`
  (`:105`), `pending` (`:108`). `tests/unit/test_reachability.py:39-44` records the same fact as a known false negative
  ("the retry button is unreachable at runtime").
- Onboarding: `src/routers/onboarding.py:79 if job is None and profile is None and current_user.access_status == "allowed":`.
  Template `templates/onboarding/profile_review.html:28 {% if job_status == 'pending' or job_status == 'processing' or job_status == 'none' %}`
  → spinner; `:47 {% elif job_status == 'failed' %}` → the only "Try Again" form (`:53-55`, posts `/onboarding/retry`,
  `onboarding.py:318-337`). Since `'failed'` is never written, a `processing`-wedged job (COR-17) or a `dead` job leaves the PI
  on the spinner with no self-service retry. STILL PRESENT.
  **Manual mitigation only**: an admin can enqueue a fresh `generate_profile` job (`src/routers/admin.py:1108-1114`), which
  becomes the "latest job" the onboarding page reads (`onboarding.py:63-68` orders by `enqueued_at desc`).

### PR V3 — COR-19 table

1. **Poison retried forever — FIXED.** `email_inbound.py:33 MAX_S3_PROCESS_ATTEMPTS = 3`; `:186-209` copies to `failed/` and
   deletes after the 3rd consecutive failure. Introduced in `8f96f86 fix(email): harden inbound reply processing…` (PR #31).
   Tests: `tests/unit/test_email_inbound_hardening.py:259` (`test_poison_email_is_quarantined_after_repeated_failures`) and
   `:279` (transient failure not quarantined).
2. **Rate limit — FIXED.** `_reply_rate_ok` `:52-65`, enforced at `:241-245`. Same commit. Tests `hardening.py:195-211`.
3. **Charset — STILL PRESENT.** `email_inbound.py:386-389`:
   ```
   def _decode_part(part):
       charset = part.get_content_charset() or "utf-8"
       payload = part.get_payload(decode=True) or b""
       return payload.decode(charset, errors="replace")
   ```
   Ran against the real module (`.venv-test/bin/python`, message with `charset="unknown-8bit"`):
   `_decode_part -> LookupError: unknown encoding: unknown-8bit`; `_extract_reply_body -> LookupError` (same). Expression
   sweep: `unknown-8bit`, `x-unknown`, `UNKNOWN`, `default`, `x-user-defined` all raise `LookupError`; `utf-8`, `iso-8859-1`,
   `windows-1252`, `gb2312`, `ks_c_5601-1987` decode fine. The exception propagates out of `process_inbound_email` → poller
   `except` `:186` → counted → quarantined on the 3rd poll (3 × 60 s). Net effect as the issue says: legitimate reply silently
   lost to `failed/`. No test mentions `unknown-8bit`/`LookupError`.
4. **String rating — STILL PRESENT.** `:320-322 rating = classification.get("rating") … if not rating or rating < 1 or rating > 4:`
   with `classify_reply` returning `json.loads(response_text)` unmodified at `:516`. Snippet on the exact guard expression:
   `rating='3' -> TypeError: '<' not supported between instances of 'str' and 'int'`; `'abc'` same; `3`, `None`, `0`, `5`
   behave; `2.5` and `True` **pass** the guard (minor extra). Same quarantine fate as row 3. All tests feed integer ratings
   (`tests/integration/test_email_inbound_reply_paths.py:105,126,184,215,238`); none feed a string.
5. **Pagination — STILL PRESENT.** `:165 response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=50)`; no
   `ContinuationToken`/`IsTruncated` in the file. Each cycle processes-and-deletes whatever 50 it sees, so backlog drains
   50/min; starvation is bounded by the quarantine as the issue says.
6. **Side effects before commit — STILL PRESENT.** In `process_inbound_email` (no commit of its own): `_send_review_confirmation`
   `:335` (SES), `_send_help_email` `:359` (SES), and inside `_handle_instruction`: inactive-agent email `:605`, migration
   `:638`, private-origin email `:655`, Slack post `:706`. The only commit is the poller's `await db.commit()` at `:179`, then
   `s3.delete_object` at `:182`. A commit failure keeps the S3 object, re-runs everything next poll, and re-sends. Issue's
   cited lines (`:301/:658/:590`) map to `:335/:706/:638`.

### PR V3 — COR-32 remainder

Caller `email_inbound.py:338-353`:
```
    if category == "instruction":
        instruction = classification.get("instruction", body)
        reopened = await _handle_instruction(...)
        await record_engagement(user.id, db)
        await mark_notification_responded(user.id, td.id, "instruction", db)
        if reopened:
            await _send_instruction_confirmation(user, notification, td, db)
        return
```
`mark_notification_responded` is unconditional; the poller then commits and deletes the object (`:179-182`). Inside
`_handle_instruction`, `return False` **with** a PI email: inactive agent `:605-612`, already-private origin `:655-662`.
`return False` **without** any email: no simulation run `:681-682`, no bot token `:697-699`, channel not found `:702-704`,
blanket `except` `:717-719`. The already-acted-on guard `:623-628` also returns False silently (arguably correct). A resend
of the same notification hits `:256-258 if notification.status != "sent": … return`. STILL PRESENT. No test exercises the
silent-False paths (reply_paths tests cover the confirmation copy only).

### PR V4 — email-notification transactional safety

1. **No rollback**: `grep -c rollback src/services/email_notifications.py` → `0`. Per-item excepts:
   `:208-214` (proposal_review, per user), `:721-725` (status_overview, per user), `:912-916` (new_proposal, per proposal/agent);
   each logs and continues to the sweep-level `await db.commit()` at `:216`, `:726`, `:917`. A flush error in one item poisons
   the session; later items raise `PendingRollbackError` inside the try (logged), and the final commit raises out to
   `worker/main.py:158-159`. Rows for earlier users whose SES send already returned success are discarded; next cycle they
   have no `sent` row and get re-emailed with a fresh token. Mechanism confirmed by reading; not executed (needs DB).
2. **Phantom-sent**: `send_proposal_notification` `:331-340` builds `EmailNotification(status="sent")`, `db.add`, `await
   db.flush()`; the SES call is `:481-485`, failure returns False at `:493-495` leaving the row. `_process_user_notifications`
   then skips `last_notification_sent_at`/`consecutive_missed` (`:293-295`) but the row remains; next cycle
   `:240-250` finds an outstanding `sent` row and bails forever. Same shape in `_send_new_proposal_email` `:995-1004` vs
   `_send_html_email` `:1066`; dedup `:968-976` checks existence regardless of status → permanently lost.
   (Positive note: the allowlist check `:320-326` runs *before* row creation, so suppressed recipients don't get phantom rows.)
3. **`"expired"`**: zero occurrences in `email_notifications.py`; in `src/` only the model comment
   `src/models/email_notification.py:42`. STILL PRESENT.
4. **Ladder dead**: sole increment `:295` (only after a successful send). Outstanding-row bail `:247-250` precedes it and is
   only cleared by `mark_notification_responded`, which is always paired with `record_engagement` (`:594` resets to 0) in every
   caller (`email_inbound.py:333-334, 347-348`; `agent_page.py:510-511, 713-714`). Other resets: `:517`, `:523`,
   `src/routers/settings.py:133`. So the counter oscillates 0→1→0; `:502 if tracker.consecutive_missed < MISSED_THRESHOLD: return`
   always returns; `:505-529` unreachable. STILL PRESENT.
   **Delegate reply**: `mark_notification_responded(user_id, …)` filters `EmailNotification.user_id == user_id` (`:606-611`);
   both email (`email_inbound.py:334/:348`, `user` = sender matched to the notification's `user_id`) and web
   (`agent_page.py:511/:714`, `current_user.id`) pass the *replier's* id. If a delegate answers, the PI's own `sent` row for
   that proposal stays `sent`; the PI can't clear it by reviewing (proposal already reviewed → `_handle_review` `:544` and
   `_get_unreviewed_proposals_for_user` `:169-177` treat it as done), so the PI is blocked at `:240-250` indefinitely. STILL
   PRESENT. `tests/integration/test_proposal_review.py:673-731` covers the PI-reviews-own-row case only.

### Triage claims

- `enable_inbound_email` default False — `src/config.py:152`. Accurate. Prod `.env` not inspectable from here.
- `proposal_review` gated on `User.email_notification_frequency != "off"`, `User.email_notifications_paused_by_system.is_(False)`,
  `User.email.isnot(None)` — `email_notifications.py:192-199`; rationale `src/models/email_notification.py:99-105`. Accurate.
- 300 s cadence — `src/config.py:158 notification_check_interval: int = 300`; loop `worker/main.py:141`. Accurate.
- "`get_or_create_pref` inserts … each cycle" — `:60-77` SELECTs first and only INSERTs+flushes when no row exists. After the
  first pass every existing user is read-only. The sweeps do still write each cycle in other ways: tracker creation for new
  users `:230-233`, `last_notification_sent_at` bump for allowlist-suppressed recipients `:267`. Partially accurate.
- Runbook step 4 arms V11/V3/COR-32 — `docs/inbound-email.md:84-90`. Accurate; no prerequisite list there.

### Definition of done / coverage

`tests/integration/test_worker.py` exists (commit `d732804 2026-07-30 "Full-system T5: the worker, 15 passed — and a silent
job-loss path"`), drives `claim_job`, `process_job`, `execute_generate_profile`, `execute_monthly_refresh`, `run_worker`
against a real Postgres. The "from 0 % coverage" wording is stale on `copi-prod`. It does not, however, provide a
fail-against-pre-fix test for COR-17/COR-18 — the DB-error test asserts the *current* broken behaviour by design.

### Where the issue text is stale/wrong (separate from the defects)

- All `email_inbound.py` / `email_notifications.py` line refs drifted (see mapping in rows above); `worker/main.py` refs still exact.
- `pi_inbox.py:151` (`record_pi_dm`) is not on the worker's path; only `:120` is.
- `grantbot.py:724/757` → `:731/:764`.
- "all 3 attempts burn in ~15 s" — actually immediate; there is no inter-attempt sleep at all.
- "worker/main.py 0 % coverage" — a 15-test integration suite exists.
- "`get_or_create_pref` inserts … each cycle" — inserts only on first sight of a user.

## 3. Counts

Sub-claims assessed: 33. **26 still present / accurate**, **2 fixed** (COR-19.1, COR-19.2), **1 partially accurate** (T-5),
**1 stale** (DoD 0 % coverage), **0 changed-mechanism**, **1 not verifiable** (T-2 prod env), plus 2 minor stale references
(pi_inbox.py:151, "~15 s") that do not alter any verdict.

## 4. What I could not verify and why

- Whether `ENABLE_INBOUND_EMAIL` is unset on prod: prohibited from ssh/containers. Only the local dev `.env` was checked
  (key absent), which says nothing about prod.
- V4-1's "duplicate email next cycle" end-to-end consequence and COR-17's `PendingRollbackError` path were confirmed by code
  reading and by the existing T5 characterization test's assertions, not by executing against a database (no DB permitted).
- Prod user `email_notification_frequency` values (whether the `proposal_review` sweep is actually sending today): needs prod DB.
- Whether SES/Slack side effects actually duplicate on a commit failure in practice: mechanism confirmed, not exercised.
