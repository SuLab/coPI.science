# Audit fixes for `close-issues-20-27` — root causes and fixes (2026-09-08)

Source: the adversarial audit of this branch (HEAD bc03917) on 2026-09-05/08: `scripts/ci.sh`
green, live Slack tier 53/53 non-LLM and 7/8 real-LLM against copi-test, five sonnet audits
confirmed by an opus pass. Every item below was traced to its root cause in the code before a fix
was chosen. Each fix ships a test that is red on the pre-fix tree.

Global rules for implementers: read the cited code before editing; one commit per RC item (subject
`fix(<area>): <what> (audit 2026-09-08 RC-N)`); tests first, confirm red, then green; run the
affected unit/integration files, then `ruff check` on what you touched; never edit `prompts/`; never
print `.env` values; never run anything that reaches Slack or the network; do not run
`scripts/ci.sh` (the coordinator runs the gate). Use `.venv-test/bin/python -m pytest <files>
-p no:cacheprovider -q`. Integration tests need Docker (testcontainers) and no
`TEST_DATABASE_URL`.

---

## RC-1 — HIGH — DB/web-path PI side effects are authorized by thread membership, not sender ownership (#20 COR-5)

**Root cause.** `agent_messages` carries no sender identity. `_handle_pi_inbound_entry`
(`src/agent/simulation.py`, ~3495-3535) derives `authorized_agent_ids` from
`get_thread_history(entry.thread_ts)` (every agent that ever posted), passes it to
`_check_pi_proposal_review` (flips `reviewed` and persists a rating=-1 `ProposalReview` or
`pi_engaged_at` for each), sets `pi_context`/`has_pending_reply`/`has_pi_directive` on every agent
holding the thread, and reopens via `participants[0]`. `post_agent_message`
(`src/routers/agent_page.py` ~1422) only checks that the caller's own agent participates
(`pi_may_reply_in_thread`), so PI A acting in a shared A/B thread drives B's agent. The e-mail path
(`src/services/email_inbound.py:1067`) writes the same row shape with `thread_ts=td.thread_id`.
The Slack path is correct: it scopes everything to `_pi_slack_id_to_agent_ids[user_id]`
(registry owner + `delegate_slack_ids`).

**Fix.**
1. Migration `0030` (down_revision `0029`): `agent_messages.sender_user_id UUID NULL`
   FK `users.id ON DELETE SET NULL`, indexed; `pi_dm_messages.handled_at TIMESTAMPTZ NULL`
   (RC-2). Backfill in the same migration: `UPDATE pi_dm_messages SET handled_at = created_at
   WHERE direction='inbound' AND handled_at IS NULL` (every pre-existing inbound DM has already
   been through the handler or is unrecoverable; without the backfill a deploy re-handles all
   history). Downgrade drops both with `if_exists`. Update `scripts/migrate/preflight.py`
   `REVISION_ORDER`/`DEFAULT_TARGET` and its tests (`tests/unit/test_run_migration_sh.py`,
   `tests/unit/test_migrate_preflight*.py` — find them), and `docs/production-migration.md`'s
   revision mentions.
2. `record_pi_message(..., sender_user_id: uuid.UUID | None)` stamps the column (and
   `pi_inbound_state='pending'`, RC-2). All four callers pass the acting user: the three in
   `agent_page.py` (`current_user.id`) and `email_inbound.py:1067` (`user.id`).
3. Engine: new helper `async _agent_ids_owned_by_user(user_id) -> set[str]` = agents where
   `AgentRegistry.user_id == user_id` ∪ agents where an `AgentDelegate` row links the user
   (`src/models/delegate.py`), mirroring the Slack map. `LogEntry` gains `sender_user_id`
   (populate in `load_entry`/rebuild and in `_poll_inbound_from_db`). In
   `_handle_pi_inbound_entry`: `owned = await self._agent_ids_owned_by_user(entry.sender_user_id)`;
   review clearing uses `owned ∩ thread_participants`; `pi_context`/`has_pending_reply`/
   `has_pi_directive` and the reopen target are restricted to `owned`; the `@Bot` tag route only
   fires for a tagged agent in `owned` (keeps F1's forged-injection concern closed). A row with
   `sender_user_id IS NULL` gets NO ownership-gated side effect and one WARNING naming the row
   (it is still appended to the log as context). Document this in the method docstring: the only
   NULL rows after 0030 are pre-deploy rows already marked HANDLED or in the log.
4. Tests (red first): unit test in `tests/unit/test_simulation_logic.py` with two agents sharing a
   thread and a PI row from A's owner: only A's proposal clears, only A's `pi_context` is set, B
   untouched; a NULL-sender row clears nothing; a delegate of A clears A's. Integration test in
   `tests/integration/test_agent_page.py` (or the pi_inbox tests): `POST /agent/{a}/messages`
   persists `sender_user_id == current_user.id`. Migration test: `pi_dm_messages.handled_at`
   backfilled for pre-existing inbound rows (pattern: `tests/integration/test_migration_0025.py`).

## RC-2 — HIGH — PI messages written while `agent-run` is down lose every side effect

**Root cause.** Three cooperating mechanisms, all "handled is inferred from presence":
(a) `record_pi_message` never sets `pi_inbound_state`; (b) `_rebuild_state_from_db` loads PI rows
into the MessageLog, so `_poll_inbound_from_db`'s NULL fallback `state is None and in_log` skips
them (`simulation.py` ~3360-3366); (c) `_seed_pi_inbox_cursor` jumps the cursor to
`max(created_at)` of all rows, so rows older than `PI_INBOX_LOOKBACK` are never scanned. The DM
channel has the same shape: `_seed_pi_dm_cursor` seeds `_pi_dm_seen` from the DB and
`_poll_pi_dms_from_db` skips seen ts; `pi_dm_messages` has no handled marker at all.

**Fix.**
1. Writers stamp `pi_inbound_state='pending'` (`PI_INBOUND_PENDING` constant next to
   INGESTED/HANDLED). `_poll_inbound_from_db`'s query becomes
   `created_at > cursor - lookback OR (is_bot = false AND pi_inbound_state = 'pending')`, and the
   skip predicate never skips a `pending` row regardless of `in_log`. Legacy NULL keeps today's
   fallback. `_seed_pi_inbox_cursor` is unchanged (pending rows are cursor-independent).
2. DMs: `_poll_pi_dms_from_db` selects `direction='inbound' AND handled_at IS NULL` (plus the
   existing window for safety) and sets `handled_at = now()` after `handle_dm` returns (also on a
   handler exception, after logging, to avoid a poison DM re-running forever — one attempt, like
   the channel path's INGESTED→HANDLED). `_seed_pi_dm_cursor` no longer needs to seed the seen set
   from the DB for dedup (keep in-process dedup by ts).
3. Tests (red first): a PI row inserted with `pending`, then `_rebuild_state_from_db` (which loads
   it into the log), then one `_poll_inbound_from_db` tick → handler ran once, state HANDLED; a
   second tick does nothing. Same for a DM row with `handled_at IS NULL` → `handle_dm` called once,
   `handled_at` set. Use the existing DB-backed patterns in `tests/integration/test_state_rebuild.py`
   / the inbox tests.

## RC-9 — LOW — parked threads never exit parking; parking resets on restart

**Root cause.** `_is_parked_thread` (`simulation.py:537-560`) is exited only when the counterpart
posts; `post_failure_count` lives only in memory (`src/agent/state.py:55`).

**Fix.** (a) `PARKED_THREAD_MAX_TURNS = 20`: a thread parked for that many of its agent's turns
is dropped from `active_threads` with one INFO line (no decision, no DM — task-5's ruling stands);
Phase 3 can re-activate it later. (b) On rebuild (`_rebuild_agent_state` and
`_rebuild_one_agent_state`), derive `post_failure_count` as the number of trailing rows in the
thread authored by this agent with `slack_ts IS NULL` while the engine has a connected client for
it (DB-only rows are exactly what a failed post writes since COR-1b); cap at 2. Tests red first
for both.

---

## RC-3 — MEDIUM — Retry-After cap converts a long Slack throttle into a dropped post (#23 V7)

**Root cause.** `parse_retry_after(cap=30)` with unchanged `MAX_RETRIES = 3`
(`src/agent/slack_client.py:121-122, :332-343`): a legitimate `Retry-After: 60` yields three 30 s
sleeps then `SlackApiError("Rate limit retries exhausted")`; `_post_one` turns that into `None`
and `_post_message` records a DB-only row the PIs never see on Slack.

**Fix.** Replace the fixed attempt count with a wait budget: `RATE_LIMIT_WAIT_BUDGET_SECONDS =
180.0`, per-sleep cap stays 30 s; loop while `cumulative + next_sleep <= budget` (so ≥6 attempts
at 30 s, and a 60 s header is honoured as 30+30 before the next try). Keep `MAX_RETRIES` as a
hard ceiling on attempts (raise to 8) so a 1 s header cannot spin. Log the budget in the warning.
Tests red first in `tests/unit/test_slack_client_contract.py`: `Retry-After: 60` three times then
success → returns the result (fails today with exhaustion); a header of 1 s forever → exhausts at
the attempt ceiling, total sleep ≤ budget.

## RC-7 — MEDIUM — a failed private-profile disk write silently clobbers the DB copy

**Root cause.** `Agent.update_private_profile` (`src/agent/agent.py:731-742`) is disk-first and
swallows the exception; `persist_private_profile_to_db` (`:744-767`) then writes
`self.private_profile`, which re-reads the (stale) disk file. Seen live on copi-test: permission
denied on the temp file → DB overwritten with the old file and the instruction lost, PI told it
was recorded. Pre-existing at 18ba52c; the new tempfile write needs directory write permission,
which widened the trigger.

**Fix.** `update_private_profile` returns `bool` and on failure logs ERROR and sets
`self._private_profile = new_profile` (memory reflects the accepted instruction). New
`persist_private_profile_to_db(db, content: str)` takes the content explicitly (never re-reads
disk). `PIHandler` (`src/agent/pi_handler.py:138-160`) persists to DB FIRST, then writes disk;
if the DB write fails, the acknowledgement DM says the instruction could not be saved and asks the
PI to retry (do not claim success); if only the disk write fails, log and continue (DB is
primary; the next `_sync_profiles_from_disk` must not resurrect the stale file — check that path
and make it prefer the newer of DB/disk or skip when the in-memory copy is newer). Also apply the
same explicit-content rule to any other caller of `persist_private_profile_to_db`. Tests red
first: disk write raising → DB still holds the new content and the ack says so; DB raising → ack
reports failure.

---

## RC-4 — MEDIUM — review reply tokens never expire at the consumer (#21 V4-3)

**Root cause.** `expired` is written only when a replacement for a different proposal is sent
(`src/services/email_notifications.py:355-372`); the inbound side
(`src/services/email_inbound.py:327`) checks only `status == "sent"`, so `review+<token>@` is a
bearer credential with unbounded lifetime.

**Fix.** Inbound: reject a reply whose `notification.sent_at` is older than
`settings.email_notification_expiry_days` (already exists, default 14): log, mark the row
`expired`, send the PI the existing "couldn't apply" explanation (or a short "this link has
expired, use the dashboard" via `_send_html_email`), and do not apply the rating/instruction.
Sweep: on the `if not proposals` and allowlist-suppressed paths, if the outstanding row is past
the window, mark it `expired` (safe now that inbound refuses it anyway). Keep the same-proposal
resend reconcile (it refreshes `sent_at`, which is the intended token renewal). Tests red first:
a reply against a row with `sent_at = now - 15 days` is refused and the row becomes `expired`;
a 13-day-old one is applied.

## RC-8 — LOW — admin user delete can orphan an active agent (#25 D1 asymmetry)

**Root cause.** The guard lives in `src/routers/profile.py:254-273` and is not used by
`admin_delete_user` (`src/routers/admin.py:213-241`); `agents.user_id` is SET NULL.

**Fix.** Move `AGENT_STATUSES_BLOCKING_ACCOUNT_DELETE` + `_agent_blocking_account_delete` to
`src/services/account_deletion.py` (public name), use it in both routes; admin gets a 409 whose
detail names the agent and the remedy (deactivate or re-link first). Test red first: admin delete
of a user owning an active agent → 409 and the user still exists; owning only an inactive agent →
deleted.

## RC-11 — LOW — ORCID present-but-null containers still iterate `None`

**Root cause.** `src/services/orcid.py:66-67, :110, :131-132` use plain `.get(key, [])`, which
returns `None` for an explicit null.

**Fix.** Use the existing `_get(..., default=[])` for `group`, `summaries`, `funding-summary`,
`work-summary`, `affiliation-group`. Contract test red first with `"group": None` and
`"summaries": None`.

---

## RC-10 — LOW — `/api/health`'s retry can run past the documented bound

**Root cause.** `probe_once` bounds only `conn.execute`; `engine.connect()` and the second attempt
sit outside (`src/main.py:255-292`), and the comment claims the response still lands inside
`HEALTH_PROBE_TIMEOUT_SECONDS`.

**Fix.** Track `deadline = loop.time() + HEALTH_PROBE_TIMEOUT_SECONDS`; the retry runs only if
time remains and is wrapped in `asyncio.wait_for(..., remaining)`; correct the comment. Keep the
probe engine kwargs unchanged (task-29's pause experiment stands). Unit test red first: first
probe raises `DBAPIError(connection_invalidated=True)` after consuming most of the budget, the
retry is bounded and the route answers 503 within `HEALTH_PROBE_TIMEOUT_SECONDS + 0.5`.

## RC-5 — MEDIUM — CSP is inert (#27 I5)

**Root cause.** `nginx/nginx.conf:144, 288, 375` send `Content-Security-Policy-Report-Only` with
no `report-uri`/`report-to`, so nothing is enforced or collected.

**Fix.** (a) Add an enforcing `Content-Security-Policy` header carrying only the directives that
cannot break rendering: `frame-ancestors 'none'; base-uri 'self'; form-action 'self';
object-src 'none'`. (b) Keep the Report-Only header for `script-src`/`style-src`/… and append
`report-uri /api/csp-report`. (c) Add `POST /api/csp-report` in `src/main.py` (public, no auth,
accepts `application/csp-report` and `application/reports+json`, body capped at 8 KB, logs one
WARNING line with `document-uri`, `violated-directive`, `blocked-uri`, returns 204). Tests red
first: nginx structural test (both headers on all three vhosts, report-uri present), route test.

## RC-6 — MEDIUM — live redeploy serves old code against the migrated schema (#27 I2)

**Root cause.** `depends_on: service_completed_successfully` orders creation on a cold start;
`up -d --build app worker` on a running stack lets the old containers serve during migrate.

**Fix.** `scripts/redeploy.sh`: `COMPOSE_FILE` check (both prod files), `build migrate app worker`,
`stop app worker`, `up -d migrate` and wait for exit 0 (`docker compose wait migrate` or inspect),
then `up -d app worker`, then `nginx -s reload` (the stale-upstream note in the memory/runbook),
printing each step; refuse to run against the dev compose file. CLAUDE.md restart runbook step 3
and `docs/production-migration.md` point at it and explain the window. Unit test: the script is
executable, refuses without the prod compose files, and its step order is as above (text test in
the existing `tests/unit/test_runbook_docs.py` family).

## RC-13 — LOW — the live tier needs a writable `profiles/` and nothing says so

**Root cause.** `PROFILES_DIR = Path("profiles")` (`src/agent/agent.py:18`) and
`profiles/public|private` in `src/services/profile_export.py:12-13` (and any other
`Path("profiles…")` in `src/` — grep) are CWD-relative literals; on a host where `profiles/` is
root-owned the DM path fails (RC-7's trigger) and the tier's preflight says nothing.

**Fix.** One setting `profiles_dir` in `src/config.py` (env `COPI_PROFILES_DIR`, default
`profiles`) consumed by every path builder (keep the module constants as functions or resolve at
call time; default behaviour unchanged). `scripts/live_slack_preflight.py` check 6: the resolved
`profiles/{public,private,memory}` are writable by the current uid (create if missing) — refuse
otherwise with the exact `chown` or `COPI_PROFILES_DIR=` remedy. `scripts/run_live_slack.sh`
docstring documents it. Tests red first for the preflight check and for the setting.

## RC-14 — MEDIUM — synchronous Slack calls in `private_channels.py` block the event loop (Opus review, 2026-09-08)

**Root cause.** `src/services/private_channels.py`'s `migrate_public_thread_to_private`
(and its `_make_client` helper) is `async` but calls straight into `AgentSlackClient`'s
synchronous, blocking methods — `connect()` (via `_make_client`), `create_private_channel`,
`invite_to_channel`, `_resolve_channel_id`, `post_message`, `send_dm` — from the web reopen
route and the e-mail inbound worker. Since RC-3 raised the rate-limit wait budget to
`RATE_LIMIT_WAIT_BUDGET_SECONDS = 180.0`, a single sustained Slack throttle during this flow
can now block for up to 180s on the calling thread. Because both callers are async
(single-worker ASGI app / worker event loop), that blocks every other request or job in the
process for the duration, not just the one PI's reopen.

**Fix.** Wrap every `AgentSlackClient` call site in `private_channels.py` in
`asyncio.to_thread(...)`: the two `_make_client(...)` calls, `create_private_channel`,
`invite_to_channel` (both bots), `_resolve_channel_id`, the handover `post_message` loop, the
close-marker `post_message`, and `send_dm`. `other_client.bot_user_id` is a plain in-memory
property read (no I/O) and is left as-is. The engine's own `_post_message`
(`src/agent/simulation.py`) is deliberately NOT touched — the simulation main loop is
synchronous by design and runs in its own process, so it was never in scope here.

**Cancellation caveat (pre-existing class, not introduced here).** `asyncio.to_thread`
schedules the call on a worker thread via an executor and is not itself cancellable — an
`await asyncio.to_thread(...)` that is cancelled (caller timeout, task group failure, process
shutdown) does not stop the underlying thread; the Slack call keeps running to completion (or
its own timeout) in the background, detached from whatever awaited it. This widens, slightly,
the same "unknown whether Slack acted on the request" window `migrate_public_thread_to_private`
already documents at its point of no return: a cancellation during `create_private_channel`
now has a running background thread as well as an ambiguous Slack response. It does not weaken
the e-mail retry guard, though — `progress.safe_to_retry` is set to `False` *before* the
`create_private_channel` call (not after), specifically because the outcome is unknowable the
moment the call is made, threaded or not. Pre-existing risk class (any blocking call under an
`await` was already subject to caller-side cancellation not stopping in-flight Slack I/O);
`asyncio.to_thread` does not add a new failure mode, it just means the in-flight work now
outlives cancellation in a background thread rather than an already-blocked coroutine.

**Tests (red first).** `tests/unit/test_private_channel_migration.py`,
`TestSlackCallsRunOffTheEventLoop`: drives `migrate_public_thread_to_private` through its
Slack-on path with `_ThreadRecordingSlackClient` (records `threading.get_ident()` on every
call) standing in for both bots' clients, and asserts none of those calls — nor the
`_make_client` dispatch itself — ran on the event loop's own thread. Failed pre-fix with
`_make_client` recorded on the loop thread; passes post-fix. Style follows
`tests/unit/test_slack_provisioning.py`'s `test_create_app_async_runs_off_the_event_loop`.

## RC-12 — closure paperwork

- `docs/plans/2026-09-02-close-issues-20-27-pr-body.md:179`: `Closes #22, #23, #24, #25` (add
  `#20` only in the same commit that lands RC-1 and states the ownership-carrier in its carve-out).
- `docs/plans/2026-09-04-decisions/README.md`: ack threshold is **10** words, NCBI pacing is
  **0.112 s ≈ 8.93 req/s** (burst 9); add the RC-1/RC-2/RC-7 carve-outs to #20/#21; add RC-4 to
  #21; add RC-5/RC-6 to #27.
- `docs/plans/2026-09-04-decisions/task-35.md`: the live-tier run record (coordinator writes it).
- Handoff gate figures: replaced by the coordinator after the final gate.

---

## RC-15 — MEDIUM — a new post with no channel was defaulted to `#general` (found live)

**Root cause.** `_phase5_new_post` used `action_data.get("channel", "general")`, so a model response that omitted
`channel` (observed in the real-LLM copi-test run of 2026-09-10) posted into a channel the model never named and that
need not exist in the workspace.

**Fix.** A `new_post` whose channel is missing or not in `_channel_visibility ∪ SEEDED_CHANNELS ∪
agent.state.subscribed_channels` is refused as unparseable (skip streak incremented); replies keep COR-9b's
target-derived channel. Tests: `TestPhase5NewPostNeverDefaultsToGeneral`.

## Post-implementation review rounds (all landed on this branch)

Each fix group was reviewed by an opus pass before merge; the findings and their fixes are the commits tagged
`SEC-F1..F5`, `A1..A6`, `SEC2-1..5`, `REV3-1..7`, `REV4-1..6` in `git log 4f0e284..HEAD`. The material ones:
reply-token rotation on resend (SEC-F1); `ingested` rows recovered after a restart with a bounded, lookback-aware
attempt counter (SEC-F2/A1/SEC2-1/REV4-5); INGESTED marked for `pending` rows before the handler (A1); no
false-positive parking for DB-only-rooted threads (A3); DMs for an agent that merely left the live roster are not
discarded (REV3-1); the lazy profiles-dir accessor's two missed consumers (REV4-1, a release blocker caught in
review); label-based migrate-container selection in `redeploy.sh` (REV4-2); owner- and users-row locks in the delete
guard (SEC2-2/REV4-3).

A superseded reply token's bounce requires the sender to be a known user, by design (recorded,
not fixed). The other three items previously recorded here — the Slack client's per-page wait
budget × `MAX_PAGES`, the default executor shared by the `to_thread` Slack calls, and the
pre-dispatch/post-dispatch conflation in the stale-token bounce budget — are fixed below
(R-1, R-2, R-3), along with two independent findings (R-4, R-5) from the same 2026-09-10 pass.

## R-1 — MEDIUM — Slack pagination's wait budget was per-page, not per-listing

**Root cause.** `RATE_LIMIT_WAIT_BUDGET_SECONDS` (`src/agent/slack_client.py`) bounds how long
`_call_with_retry` will sleep on **one** Slack call, but `_paginate` can issue up to
`MAX_PAGES = 200` of them to walk a single cursor-paginated listing. Each page got the full
180s budget independently, so a listing that stayed throttled across many pages could block the
synchronous engine loop for up to `MAX_PAGES * RATE_LIMIT_WAIT_BUDGET_SECONDS` — 200 × 180s —
even though the per-call budget was sized (RC-3) around a single call's worst case, not a whole
listing's.

**Fix.** `_call_with_retry`/`_api` accept an optional private `_wait_budget: float | None`,
consumed as a keyword-only parameter before the slack_sdk call is made — it is therefore never
present in the kwargs the slack_sdk method itself receives. `_paginate` now owns a
listing-level wall-clock deadline, `PAGINATION_WAIT_BUDGET_SECONDS = 600.0`, tracked via
`time.monotonic()` from the start of the listing; each page's `_api` call is given only
whatever remains of that deadline. A page whose remaining budget is already exhausted is asked
to wait none at all, so it either succeeds immediately or fails immediately — surfacing through
the existing "a page after the first failed" path as `SlackListingIncomplete`. A single-call
caller that never touches `_wait_budget` is unaffected: `_call_with_retry` falls back to
`RATE_LIMIT_WAIT_BUDGET_SECONDS` exactly as before.

**Tests (red first).** `tests/unit/test_slack_client_contract.py`:
`test_the_private_wait_budget_kwarg_never_reaches_the_slack_method` asserts the kwarg is absent
from what a fake slack_sdk method receives; `test_a_single_call_with_no_wait_budget_override_is_unchanged`
pins the pre-existing single-call behaviour; `test_pagination_total_wait_is_bounded_by_the_listing_budget_not_per_page`
drives an endlessly-throttled fake cursor listing with a fake monotonic clock (advanced only by
`time.sleep`, since the file's `_no_real_sleep` fixture makes real `time.sleep` free) and asserts
the listing gives up within roughly the listing budget rather than walking towards `MAX_PAGES`.
All three failed pre-fix (`ImportError`/`AttributeError` for the not-yet-existing seam and
constant, then `fake.page_n == MAX_PAGES` for the budget test) and pass post-fix.

## R-2 — MEDIUM — every Slack `to_thread` call shared the process-wide default executor

**Root cause.** Every `asyncio.to_thread` wrapper around a synchronous `AgentSlackClient`/httpx
Slack call (`src/services/private_channels.py`, `slack_web.py`, `slack_provisioning.py`,
`src/agent/grantbot.py`, `src/routers/agent_page.py`) ran on the event loop's *default*
executor — `ThreadPoolExecutor(max_workers=min(32, os.cpu_count() + 4))`, shared by every other
`to_thread` caller in the process. Each Slack call can block for up to
`RATE_LIMIT_WAIT_BUDGET_SECONDS` (180s) under a sustained throttle, so enough concurrent Slack
calls could occupy the shared pool and starve unrelated `to_thread` work that has nothing to do
with Slack.

**Fix.** New `src/services/slack_executor.py` exposes `run_slack_call(fn, *args, **kwargs)`,
which runs on a module-level `ThreadPoolExecutor(max_workers=8, thread_name_prefix="slack-io")`
dedicated to Slack I/O. Replaced every Slack `to_thread` call site (9 in `private_channels.py`,
5 in `slack_web.py`, 4 in `slack_provisioning.py`, 1 each in `grantbot.py` and `agent_page.py`)
with `run_slack_call`; removed the now-unused `import asyncio` from the four files where it had
no other use. Non-Slack `to_thread` sites are unaffected — there were none of Slack's shape in
these files to begin with; `grantbot.py`'s other `asyncio.run(...)` calls and `agent_page.py`'s
FastAPI route bodies are untouched.

**Tests (red first).** New `tests/unit/test_slack_executor.py`: the pool is a bounded
`ThreadPoolExecutor` with `max_workers == 8` and the `"slack-io"` thread-name prefix;
`run_slack_call` actually executes off the event-loop thread on a `slack-io`-prefixed thread;
args/kwargs are forwarded and the result is returned; an exception raised inside the call
propagates by identity (`exc_info.value is marker`), not merely by type — reusing the
exception-identity pattern from `tests/unit/test_private_channel_migration.py`'s
`TestSlackCallsRunOffTheEventLoop`. All four tests failed with `ModuleNotFoundError` before
`slack_executor.py` existed and pass post-fix. `tests/unit/test_private_channel_migration.py`'s
own `TestSlackCallsRunOffTheEventLoop` (DB-backed, requires Docker) and the rest of that file's
offline classes (25 tests) were re-run to confirm the swap didn't change observable behaviour.

## R-3 — MEDIUM — the stale-token bounce budget charged suppressed/never-dispatched sends

**Root cause.** `_send_html_email` (`src/services/email_notifications.py`) returned one `bool`
for three different outcomes: suppressed before any SES call was attempted (no recipient, or
blocked by the outbound allowlist), a dispatch attempted but never reaching `send_raw_email`
(a `boto3.client(...)` or MIME-construction error), and `send_raw_email` itself raising.
`_maybe_send_stale_token_bounce` (`src/services/email_inbound.py`) charged its per-address
budget for anything that got past its own allowlist pre-check, which meant a `NOT_DISPATCHED`-
shaped failure (nothing ever reached SES) was charged identically to a real send — burning the
cap without ever having sent, or risked sending, a second bounce.

**Fix.** Added `send_html_email_outcome(...) -> SendOutcome`, an `Enum` of `SUPPRESSED`,
`NOT_DISPATCHED`, `FAILED`, `SENT`. `_send_html_email` is now a thin bool wrapper
(`is SendOutcome.SENT`) so its four existing callers are unchanged. `_maybe_send_stale_token_bounce`
now calls `send_html_email_outcome` directly and charges the budget only when the outcome is
`FAILED` or `SENT` — both mean `send_raw_email` was actually invoked, so a post-dispatch failure
(e.g. a read timeout) still counts, since mail may have left regardless.

**Tests (red first).** New `tests/unit/test_send_html_email_outcome.py` exercises all four
outcomes of `send_html_email_outcome` directly (no recipient, allowlist-blocked, a `boto3.client`
construction failure, `send_raw_email` raising, and a successful send) plus
`_send_html_email`'s thin-wrapper behaviour over each. Rewrote
`tests/unit/test_stale_token_bounce_budget.py` to monkeypatch `send_html_email_outcome` (not
`_send_html_email`) and assert the budget is charged for `FAILED`/`SENT` and not for
`SUPPRESSED`/`NOT_DISPATCHED`. Both files failed with `ImportError: cannot import name
'SendOutcome'` before the fix and pass post-fix (11 tests total).

## R-4 — MEDIUM — `redeploy.sh` left the old `grantbot` image serving during a redeploy

**Root cause.** `grantbot` declares the identical `depends_on: migrate: condition:
service_completed_successfully` shape as `app`/`worker` in `docker-compose.prod.yml` — the
exact condition RC-6 already established is insufficient on an already-running stack — but
`scripts/redeploy.sh` only ever built, stopped and started `app` and `worker`. A redeploy left
the OLD `grantbot` container running (and posting to Slack) against the newly migrated schema
for as long as the process kept running, unaffected by the very ordering fix RC-6 built for
exactly this race. `docs/production-migration.md` §10.1 had already recorded this as a known
gap ("`redeploy.sh`'s current scope is `app`/`worker`; `grantbot` still relies on `depends_on`
ordering... by hand until it is folded into the script").

**Fix.** Added `grantbot` to all three of `redeploy.sh`'s steps: `compose build migrate app
worker grantbot`, `compose stop -t 30 app worker grantbot`, `compose up -d app worker grantbot`.
`agent` stays excluded — it is a one-off (`docker compose --profile agent run`), not a
long-running service `up -d` would ever recreate, with its own restart runbook in `CLAUDE.md`.
Updated `CLAUDE.md`'s step-3 comment and `docs/production-migration.md` §10.1 (which now states
the fold-in instead of describing it as an open gap) to match.

**Tests (red first).** Extended `tests/unit/test_redeploy_sh.py`'s existing
`test_step_order_builds_stops_migrates_then_starts_then_reloads` predicates to also require
`grantbot` in the build/stop/start lines, and added
`test_grantbot_is_built_stopped_and_started_alongside_app_worker` (also asserts `agent` never
appears in any of these steps) and `test_aborts_and_does_not_start_grantbot_when_migrate_fails`.
Both new/strengthened assertions failed against the pre-fix script (`grantbot` absent from
every logged `docker compose` invocation) and pass post-fix (19 tests total in the file).

## R-5 — LOW — the preflight `chown` remedy wasn't shell-quoted

**Root cause.** `check_profiles_dir_writable` (`scripts/live_slack_preflight.py`) interpolated
the raw `profiles_dir` path into a copy-paste `sudo chown -R $(id -u):$(id -g) <dir>`
suggestion. A path containing a space or shell metacharacter would render a command that either
chowns the wrong (truncated) path or does something else entirely if pasted as-is.

**Fix.** Wrapped the interpolated path in `shlex.quote(str(profiles_dir))`.

**Tests (red first).** Added `test_check_six_quotes_a_profiles_dir_containing_a_space_in_the_remedy`
to `tests/unit/test_live_slack_preflight.py`, using a `tmp_path` subdirectory with a literal
space in its name; asserts the exact quoted `chown` command line is present and the unquoted
form is not (while still permitting the unquoted path to appear in the unrelated "does not
exist" prefix). Failed pre-fix on the missing quoting, passes post-fix (34 tests total in the
file).

## Opus review of R-1..R-5 (same day, 2026-09-10) — five follow-ups, all landed

**R-1 follow-up (MEDIUM) — page 0's budget was the full listing budget, not the per-call
default.** `_paginate` passed `max(remaining, 0.0)` as each page's `_wait_budget`, and page 0
starts with the *entire* `PAGINATION_WAIT_BUDGET_SECONDS` (600s) remaining — so a single early
page could be handed ~600s of retry patience instead of the 180s any other caller gets. Fixed
by capping every page's budget at `min(RATE_LIMIT_WAIT_BUDGET_SECONDS, max(remaining, 0.0))`.
Test (red first): a one-page listing 429ing with `Retry-After: 200` (capped to `MAX_RETRY_AFTER`
30s per attempt by `parse_retry_after`, as always) slept 240s pre-fix and 180s post-fix —
`tests/unit/test_slack_client_contract.py::test_a_single_pages_wait_budget_is_capped_at_the_per_call_default`.

**R-2 follow-up #1 (MEDIUM) — the Slack executor was never shut down.** A bare module-level
`ThreadPoolExecutor` blocks interpreter shutdown until every worker thread finishes; an
in-flight Slack call sleeping through a sustained throttle (up to 180s) could hold the process
open past `docker stop -t 30`'s grace period, which then SIGKILLs it. Added
`shutdown_slack_executor()` (`.shutdown(wait=False, cancel_futures=True)`), wired into a new
FastAPI `lifespan` in `src/main.py` and a new `_shutdown_worker()` helper in
`src/worker/main.py` (called before `engine.dispose()`), plus an `atexit` backstop. Documented
contract: `run_slack_call` after shutdown raises `RuntimeError` (not a silently re-created
pool) — this is `ThreadPoolExecutor`'s own native behaviour, needing no extra code. Tests (red
first): `tests/unit/test_main_lifespan.py`, `tests/unit/test_worker_shutdown.py`,
`tests/unit/test_slack_executor.py`'s two new shutdown tests.

**R-2 follow-up #2 (LOW) — executor sizing was undocumented and tight.** A single
`migrate_public_thread_to_private` reopen makes ~8 sequential `run_slack_call`s by itself;
`max_workers=8` left zero headroom for a second concurrent flow, which would simply queue
behind the first — indistinguishable from a Slack throttle. Made the size a named constant,
`SLACK_IO_MAX_WORKERS = 16` (~2x one flow's call count), and documented the reasoning in the
module docstring. Test: `test_slack_io_max_workers_exceeds_one_reopen_flows_sequential_call_count`.

**R-3 follow-up #1 (LOW) — the bounce budget was charged after dispatch, not reserved before
it.** `_maybe_send_stale_token_bounce` read `sent_so_far`, dispatched, and only then charged —
a check-then-act race that (for a future threaded/concurrent send) could let two replies from
one address both pass the cap check before either charged the budget. Now reserves the slot
before dispatch and refunds it only for `SUPPRESSED`/`NOT_DISPATCHED`. Tests (red first):
`test_the_slot_is_reserved_before_the_send_is_dispatched` (asserts the counter is already
incremented *while* the send is in flight) and
`test_a_refunded_reservation_does_not_leak_across_repeated_suppressions`.

**R-3 follow-up #2 (LOW) — `NOT_DISPATCHED` conflated two failure shapes.** `boto3.client(...)`
raising (a persistent misconfiguration — bad/missing AWS credentials, a bad region — that will
keep failing every retry) and MIME/message construction raising (a property of THIS message,
e.g. an unencodable header) both returned `NOT_DISPATCHED`, so a broken SES client and a one-off
message error were treated identically by the bounce budget. Added `SendOutcome.CLIENT_UNAVAILABLE`
for the former (`boto3.client(...)` itself); `NOT_DISPATCHED` now means only the latter (MIME
construction). `_maybe_send_stale_token_bounce` charges the budget for `CLIENT_UNAVAILABLE` too
(a persistently broken client should be capped like a real failure), while `NOT_DISPATCHED`
stays refunded (the next message can still succeed). Tests (red first):
`test_a_client_construction_failure_is_client_unavailable`,
`test_a_mime_construction_failure_is_not_dispatched`,
`test_a_client_unavailable_outcome_consumes_the_budget`.

## SEC3: opus review, audit 2026-09-10 — five findings, all landed

**SEC3-1 (MEDIUM) — `_extract_email_address` trusted the FIRST angle-bracketed token.**
`re.search(r"<([^>]+)>", from_header)` returns the first bracketed token in a From header, so a
display name that itself contains an address literal —
`"Alice <pi@univ.edu>" <attacker@evil.com>` — yielded the PI's address while the real envelope
sender (and the domain that legitimately passed SPF/DKIM) was `attacker@evil.com`. The
downstream sender-identity check then matched a known PI even though authentication aligned to
the attacker's domain. Fixed by parsing with
`email.utils.getaddresses(msg.get_all("From", []))`, which resolves RFC 5322 address syntax
correctly, and requiring exactly one resolved address (multiple From headers or group syntax are
now refused as ambiguous rather than picked arbitrarily). The function's signature changed from
`(from_header: str)` to `(msg: email.message.Message)` so it can see every From header, not just
the first; both call sites in `process_inbound_email` updated. Tests (red first, all in
`tests/unit/test_email_inbound_security.py`): `test_display_name_with_embedded_bracketed_address_is_not_fooled`,
`test_two_from_headers_rejected`, `test_group_syntax_rejected`, plus the pre-existing bracketed/bare/
unparseable cases updated to pass a `Message` instead of a bare string.

**SEC3-2 (MEDIUM-LOW) — `dmarc=none` plus a lone `spf=pass` on an unrelated domain satisfied
the "one strong pass" rule.** `_authentication_results_ok` treated any single passing
spf/dkim/dmarc verdict as sufficient once explicit failures were ruled out. But SES reports
`spf=pass` for whatever domain the envelope sender actually used, with no relation to the From
header — so for a PI domain that publishes no DMARC policy (`dmarc=none`), an attacker sending
from their own domain (which legitimately passes SPF/DKIM for itself) with a forged
`From: pi@that-domain` satisfied the rule outright. Fixed by requiring, when `dmarc=pass` is
absent, that the domain which actually passed (`smtp.mailfrom=` for SPF, `header.d=`/`header.i=`
for DKIM) align with the From address's domain — equal, or one a subdomain of the other (relaxed
alignment via a new `_domains_aligned` helper). `dmarc=pass` remains sufficient on its own since
it already encodes alignment. Tests (red first, `tests/unit/test_email_inbound_security.py`):
`test_unaligned_spf_pass_on_attacker_domain_rejected`, `test_aligned_spf_pass_accepted`,
`test_aligned_dkim_header_d_accepted`, `test_unaligned_dkim_header_d_rejected`,
`test_dmarc_pass_accepted_regardless_of_alignment`, `test_subdomain_alignment_accepted`; the
pre-existing `test_dmarc_none_with_spf_pass_accepted` renamed/updated to carry an aligned
`smtp.mailfrom=`/From pair.

**SEC3-3 (LOW) — the DSN redaction regex leaked a password containing `@`.**
`scripts/live_slack_preflight.py`'s `check_no_operator_supplied_database` redacted
`TEST_DATABASE_URL` with `re.sub(r"//[^@/]*@", "//<redacted>@", dsn)`, which matches up to the
FIRST `@` after the scheme — a password containing a literal `@` (`user:hun@ter2@host`) only
redacted the prefix up to that first `@`, leaking the rest of the password (and the real host)
into the printed detail. Fixed by parsing with `urllib.parse.urlsplit`, which resolves
hostname/port by splitting the authority section on the LAST `@`, and printing only
`scheme://<redacted>@hostname[:port]/path`. Removed the now-unused `re` import. Test (red first):
`test_check_five_never_echoes_a_dsn_password_containing_an_at_sign` in
`tests/unit/test_live_slack_preflight.py`.

**SEC3-4 (LOW) — four in-memory rate-limit/dedup maps in `email_inbound.py` grew without
bound.** `_RECENT_REPLY_TIMES`, `_HELP_EMAILS_SENT`, `_STALE_TOKEN_BOUNCES_SENT` and
`_INSTRUCTION_FAILURE_EMAILS_SENT` are keyed by notification id or sender address and never drop
a key — not even when a per-notification list empties out to `[]` — so a long-lived worker
process accumulates one entry per notification/address ever seen for as long as it runs. Fixed
by adding a companion "last touched" timestamp dict for each map (`_RECENT_REPLY_TOUCHED`,
`_HELP_EMAILS_TOUCHED`, `_STALE_BOUNCES_TOUCHED`, `_INSTRUCTION_FAILURE_TOUCHED`), stamped on
every write, and a new `_prune_stale_entries()` that drops any key not touched in the last 24h
from both the data dict and its touched dict — called at the top of every `poll_inbound_emails`
run. Tests (red first, `tests/unit/test_email_inbound_hardening.py`):
`test_prune_drops_an_entry_untouched_for_over_24_hours`, `test_prune_keeps_a_fresh_entry`,
`test_poll_inbound_emails_prunes_stale_entries`.

**SEC3-5 (LOW) — `pi_may_post_to_channel` let a PI write into any invented channel name.**
An unknown `(run_id, channel_name)` pair returned `True`, matching `_resolve_channel`'s
documented fallback of minting a `local:<name>` public row for any name at all — but that
fallback exists so `record_pi_message` never chokes on an unrecognized name, and using the same
"unknown means public" rule for AUTHORIZATION let an authenticated PI write into any channel
name they invented, including the real name of another run's private collab channel
(`channel_name` is only unique per run, so it carries no row in THIS run). Fixed by refusing
when there is no `agent_channels` row for this run, UNLESS the name is one of the app's own
`SEEDED_CHANNELS` (`src/agent/channels.py`: `general`, `funding-opportunities`, and the seeded
topic channels) — that exemption is required because those channels' `agent_channels` row is
only materialized lazily on first join/post within a run, and the web message form's own
`"general"` default would otherwise be refused on a brand-new run (caught immediately by
`tests/integration/test_agent_page.py`'s existing message-route tests). Any other unknown name
is now refused; known public channels and the private-membership check are unchanged. Tests (red
first, `tests/integration/test_pi_inbox.py`):
`test_pi_may_post_to_channel_false_for_a_channel_with_no_agent_channels_row`,
`test_pi_may_post_to_channel_true_for_a_known_public_channel`,
`test_pi_may_post_to_channel_true_for_a_private_member`,
`test_pi_may_post_to_channel_false_for_a_private_non_member`; the full
`tests/integration/test_pi_inbox.py` (11) and `tests/integration/test_agent_page.py` (111)
suites re-run green as a regression control given the SEEDED_CHANNELS carve-out.
