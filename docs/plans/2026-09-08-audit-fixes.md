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

## Opus review of SEC3-1..SEC3-5 (same day, 2026-09-10) — five follow-ups, all landed

**Follow-up to SEC3-5 (MEDIUM) — the SEEDED_CHANNELS exemption still 403'd legitimate
non-seeded public channels.** `agent_channels` rows are only ever written for seeded
channels and `collab_private` channels (`record_channel_created`) -- an ordinary topic
channel an agent created on its own and posted public messages into had no
`agent_channels` row at all, even though it is exactly what `_visible_channels`
(`src/routers/agent_page.py`) already offers the PI in the conversations UI's channel
dropdown. Fixed by also allowing a channel with no row when at least one PUBLIC
(`visibility != collab_private`) `AgentMessage` row already exists for it in this run.
Tests (red first, `tests/integration/test_pi_inbox.py`):
`test_pi_may_post_to_channel_true_for_a_non_seeded_channel_with_a_public_message`,
`test_pi_may_post_to_channel_false_for_a_non_seeded_channel_with_no_rows_at_all`,
`test_pi_may_post_to_channel_false_when_only_a_private_message_exists`.

**Follow-up to SEC3-3 (MEDIUM) — `urlsplit`-based redaction crashed or leaked on a
malformed DSN.** `urlsplit(dsn).port` raises `ValueError` for a non-numeric port
(crashing a REFUSAL check instead of failing safe), and a password containing `/` or
`#` makes `urlsplit` attribute part of the password to `.path` instead of `.netloc`,
leaking it into the printed detail. Fixed by splitting the authority on the LAST `@` by
hand (`str.rpartition`, which never raises) and printing everything after it verbatim.
Tests: `test_check_five_never_echoes_a_dsn_with_a_non_numeric_port`,
`test_check_five_never_echoes_a_dsn_password_containing_a_slash`,
`test_check_five_never_echoes_a_dsn_password_containing_at_and_the_rest_intact`.

**Follow-up to SEC3-2 (MEDIUM) — the SPF/DKIM domain-tag regexes scanned the whole
header, ignoring quoting.** A quoted MAIL FROM local part
(`smtp.mailfrom="header.d=scripps.edu;"@evil.com`) contains a literal `;` that is only a
delimiter INSIDE the quotes, not a real segment boundary -- but
`_DKIM_D_RE.search(header)` had no notion of quoting, so it picked up the injected
`header.d=scripps.edu` tag ahead of the real `header.d=evil.com` in DKIM's own segment,
fabricating alignment with the PI's domain. Fixed with a new
`_split_auth_results_segments` that splits on `;` respecting quoted strings, plus an
anchored per-segment verdict match (`mech=verdict` must open the segment); the SPF/DKIM
domain tags are now searched only within that mechanism's own segment. Test (red first,
the reviewer's exact attack shape):
`test_quoted_mailfrom_local_part_cannot_inject_a_fake_domain_tag`, plus
`test_quoted_injection_in_a_non_topmost_header_is_still_ignored`.

**Follow-up to SEC3-2/SEC3-1 (LOW) — alignment was bidirectional; a docstring
overstated group-syntax rejection.** `_domains_aligned` accepted a PARENT domain of the
From domain as "aligned", which would let a broad platform domain (e.g. a shared email
provider) that legitimately passes SPF/DKIM for itself authenticate any of its tenants'
subdomains -- letting one tenant spoof another. Made alignment one-directional: the
authenticated domain must equal the From domain or be a SUBdomain of it, never the
reverse. Test (red first): `test_parent_domain_of_the_from_domain_is_not_aligned`
(`provider.com` authenticated vs `pi@pi.provider.com` From, now rejected). Also
corrected `_extract_email_address`'s docstring, which overstated that group syntax is
rejected outright -- a group naming exactly one member resolves unambiguously and IS
accepted; added `test_single_member_group_syntax_is_accepted` to pin the existing
(correct) behavior.

**Follow-up to SEC3-4 (LOW) — pruning silently weakened three lifetime caps into 24h
rolling caps; `_S3_FAILURE_COUNTS` was never pruned.** `_HELP_EMAILS_SENT`,
`_STALE_TOKEN_BOUNCES_SENT` and `_INSTRUCTION_FAILURE_EMAILS_SENT` are documented,
deliberate LIFETIME caps -- pruning them on the same 24h clock as the hourly
`_RECENT_REPLY_TIMES` limiter let a sender simply wait out the day for a fresh budget.
Separately, `_S3_FAILURE_COUNTS` (a poison-message counter) was never included in
pruning at all. Fixed with two windows: 24h for `_RECENT_REPLY_TIMES` and
`_S3_FAILURE_COUNTS` (both reset/bounded on their own terms already), 30 days for the
three lifetime caps. Added `_S3_FAILURE_TOUCHED`, stamped on every failure increment.
Tests (red first, `tests/unit/test_email_inbound_hardening.py`):
`test_prune_keeps_a_lifetime_cap_entry_older_than_24h_but_within_30_days`,
`test_prune_drops_a_lifetime_cap_entry_untouched_for_over_30_days`,
`test_prune_drops_a_short_window_entry_untouched_for_over_24_hours` (now covers
`_S3_FAILURE_COUNTS` too), `test_poll_inbound_emails_prunes_stale_entries` updated to
assert `_S3_FAILURE_COUNTS`/`_S3_FAILURE_TOUCHED` are pruned.

## K — final opus closure audit (2026-09-10)

**K-1 (MEDIUM) — a transient ownership-lookup DB error was fail-closed identically to
"nothing to authorize against."** `_agent_ids_owned_by_user` caught every exception from its DB
query (a NULL `sender_user_id` and a genuine DB outage) and returned the same empty set for
both, so `_handle_pi_inbound_entry` returned normally on a DB blip and `_poll_inbound_from_db`
stamped the row HANDLED, permanently discarding the PI's ownership-gated side effects. Added
`PiOwnershipLookupFailed`, raised on a genuine DB error so it propagates to the existing
per-row try/except (records an attempt, leaves the row `ingested` for retry). The empty-set
fail-closed return is now reserved for a NULL user id only. Test (red first, integration):
`test_a_transient_ownership_lookup_failure_retries_instead_of_discarding` in
`tests/integration/test_message_persistence.py`, using a session-factory wrapper that fails
only the ownership SELECT once. Also updated a pre-existing unit test
(`test_a_db_failure_fails_closed_to_the_empty_set` -> `test_a_db_failure_raises_instead_of_failing_closed`)
that pinned the old contract.

**K-2 (MEDIUM) — a retry sleep was not interruptible on shutdown.** `shutdown_slack_executor()`'s
`shutdown(wait=False, ...)` does not stop a worker thread already inside
`time.sleep(retry_after)` — `ThreadPoolExecutor` worker threads are ordinary non-daemon threads
still joined by the interpreter's own atexit machinery, so a call mid-throttle could hold up
exit for up to `RATE_LIMIT_WAIT_BUDGET_SECONDS` (180s). Added `slack_client.SHUTTING_DOWN`
(`threading.Event`); `_call_with_retry` now sleeps via `_sleep_interruptibly`, in <=1s slices,
aborting with `SlackApiError("shutting down")` as soon as the event is set.
`shutdown_slack_executor()` sets it before shutting the pool down. Docstrings corrected: only
the retry *sleep* is interruptible, not an in-flight HTTP request. Existing retry tests that
asserted an exact single `time.sleep()` call were updated to assert on the sum (and a <=1s
per-slice bound) instead. Test (red first): `test_a_retry_sleep_aborts_within_a_second_of_shutdown_being_set`
in `tests/unit/test_slack_client_contract.py` (a background thread sleeping through a 30s
Retry-After aborts within ~1s of `SHUTTING_DOWN.set()`).

**K-3 (MEDIUM) — a non-numeric migrate exit code was silently treated as success.**
`[ "$MIGRATE_EXIT" -ne 0 ]` on a non-numeric value prints "integer expression expected" and
returns exit status 2, which `if` treats identically to a clean "false" — the script fell
through and proceeded as though migrate had exited 0. Added a `case "$MIGRATE_EXIT" in
''|*[!0-9]*) die ...` guard before the numeric comparison. Test (red first):
`test_a_non_numeric_migrate_exit_code_aborts_the_deploy` in `tests/unit/test_redeploy_sh.py`
(a `docker wait` stub printing `abc`).

**K-4 (MEDIUM) — RC-7 residual: a restart re-loaded a stale on-disk private profile.** A
DB-succeeded/disk-failed standing-instruction write left the DB row (the one that survives a
restart) ahead of the stale on-disk file, and `Agent.private_profile` only reads disk on a
cache miss — every restart re-loaded the stale file via `_rebuild_agent_state`. Added
`_sync_private_profiles_from_db()`, called at the top of `_rebuild_agent_state`: for every
agent with a linked user, if `ResearcherProfile.private_profile_md` differs from the agent's
current private profile, rewrite disk (best effort, via `update_private_profile`) and set the
in-memory cache to the DB content regardless of whether the disk write succeeds. Tests (red
first, integration, DB-backed): `test_rebuild_resyncs_a_stale_disk_private_profile_from_the_db`
and `test_rebuild_keeps_the_db_content_cached_even_if_disk_is_unwritable` in
`tests/integration/test_state_rebuild.py`.

**K-5 (LOW) — the stale-token bounce refund overwrote the counter instead of decrementing
it.** `_maybe_send_stale_token_bounce`'s refund wrote back `sent_so_far` (the pre-reservation
snapshot) rather than decrementing the counter's current value — two reservations interleaved
around the reserve-then-send window let the first call's refund clobber the second call's
legitimate charge back to zero. Fixed to `max(0, current - 1)`, reading `current` fresh at
refund time. Test (red first): `test_refunding_one_of_two_interleaved_reservations_leaves_the_other_charged`
in `tests/unit/test_stale_token_bounce_budget.py`.

**K-6 (LOW) — RC-15 residual: the known-channel check trusted another pair's private
channel.** `_channel_visibility` is a single process-wide map holding every collab_private
channel discovered for every agent pair this run, not just ones the checking agent belongs to
— bare membership let an agent name another pair's private channel in a `new_post` and pass the
gate. Fixed: a channel only counts as known via `_channel_visibility` when its entry is
`VISIBILITY_PUBLIC`; a private channel this agent actually belongs to is covered separately by
`agent.state.subscribed_channels`. Tests (red first):
`test_a_new_post_naming_another_pairs_private_channel_is_refused` and
`test_a_new_post_naming_the_agents_own_private_channel_is_allowed` in
`tests/unit/test_simulation_logic.py::TestPhase5NewPostNeverDefaultsToGeneral`.

**K-7 (LOW) — a PI-inbound attempt count was forgotten even when the terminal HANDLED write
failed.** Both give-up branches in `_poll_inbound_from_db` popped `_pi_inbound_attempts`
unconditionally, regardless of whether `_mark_pi_inbound_row_handled`'s write actually
committed — a failed write left the row retryable but with its attempt count forgotten, so
SEC2-1's cap restarted from zero on the next poll. Fixed: only pop the attempt entry when the
write returns `True`. Tests (red first):
`test_a_failed_handled_write_keeps_the_attempt_count_for_a_tombstoned_row` and
`test_a_failed_handled_write_keeps_the_attempt_count_after_giving_up` in
`tests/unit/test_simulation_logic.py`.

**K-8 (LOW) — a persistently failing INGESTED-marker write had no attempt accounting at
all.** Unlike every other give-up path in this poller, a row whose INGESTED marker write kept
failing `continue`d with no attempt recorded, retrying silently and unboundedly, never engaging
SEC2-1's cap. Fixed: record an attempt on each failed write, and once
`PI_INBOUND_MAX_ATTEMPTS` is reached, stamp the row HANDLED via the same terminal path (K-7's
fix applies here too). Test (red first): `test_a_persistently_failing_ingested_marker_write_is_capped`
in `tests/unit/test_simulation_logic.py`.

**K-9 (HIGH, coordinator-added) — the process-global Slack executor was shut down by any
lifespan/atexit/test, permanently, for the rest of the interpreter.** The Slack I/O
`ThreadPoolExecutor` was a single module-level singleton created once at import time;
`shutdown_slack_executor()` (called from every real shutdown path AND from any test that drives
`create_app()`'s ASGI lifespan, unrelated to Slack) shut it down with no way to come back. Any
one test doing this broke every LATER `run_slack_call` in the same pytest process with
`RuntimeError: cannot schedule new futures after shutdown` — 16 tests across
`test_slack_executor.py`, `test_slack_provisioning.py`, `test_slack_web.py`,
`test_admin_provisioning.py`, `test_private_channel_migration.py`. Fixed: the pool is now
created lazily by `_get_executor()` (lock-guarded); `shutdown_slack_executor()` drops the
reference (sets it to `None`) instead of leaving callers pointed at a dead singleton;
`run_slack_call` transparently creates a fresh pool on its next call. `SHUTTING_DOWN` (K-2)
stays set across the shutdown itself (so an old pool's in-flight retry sleep still aborts) but
is cleared only when a NEW pool is actually created, not immediately — clearing it right away
would give an old pool's still-sleeping thread no realistic chance to observe it. Tests (red
first): `test_run_slack_call_after_shutdown_lazily_creates_a_fresh_pool` and
`test_a_fresh_pool_clears_the_shutting_down_event` in `tests/unit/test_slack_executor.py`
(replacing the old `test_run_slack_call_after_shutdown_raises_a_clear_runtimeerror`, whose
"never re-create" contract this deliberately reverses); a full-app-lifespan regression test,
`test_a_real_lifespan_shutdown_does_not_permanently_kill_run_slack_call`, in
`tests/unit/test_main_lifespan.py`.

**K-10 (MEDIUM, coordinator-added) — three tests still monkeypatched the retired
`_send_html_email`.** R-3 switched `_maybe_send_stale_token_bounce` to
`send_html_email_outcome` (a `SendOutcome`-returning function), but
`test_a_reply_to_an_unknown_token_from_a_registered_user_gets_a_bounce`,
`..._from_an_unknown_address_gets_no_bounce`, and `test_stale_token_bounces_are_capped_per_address`
in `tests/integration/test_email_inbound_reply_paths.py` still monkeypatched the old name,
failing with `AttributeError`. Fixed by monkeypatching `inbound.send_html_email_outcome`
instead (matching its 4-positional-arg call signature) and returning `SendOutcome.SENT`.
Verified against a real migrated Postgres via testcontainers (offline, cached images) — 38/38
tests in the file pass.

**K-11 (MEDIUM, coordinator-added) — root cause identical to K-9, no separate fix needed.**
`test_the_retry_split_follows_the_slack_mutation_not_the_exception_type[after-False]` failed
with `InstructionApplyFailed: ... failed before any irreversible side effect` on the merged
tree. Reproduced by running `test_main_lifespan.py`'s real (unmocked) lifespan-shutdown test
ahead of it in one process: pre-K-9, the dead singleton makes `run_slack_call(_make_client, ...)`
raise `RuntimeError: cannot schedule new futures after shutdown` at the very first Slack call in
`migrate_public_thread_to_private` — before `progress.safe_to_retry` is ever set to `False` —
so the "after channel creation" failure the test intends (`invite_to_channel` raising once a
channel already exists) never happens; the migration fails at the client-creation stage
instead, which the code correctly classifies as retryable (no Slack side effect occurred),
but the test expects terminal for that parametrization. `private_channels.py`'s
retry/terminal split (`progress.safe_to_retry`) is correct as written — this is purely K-9's
executor-singleton bug surfacing through a different call site. Confirmed the K-9 fix alone
makes both parametrizations pass in the same combined run; no change to
`test_email_inbound_hardening.py` or `private_channels.py` was needed or made.

**Full-suite verification (this worktree).** `tests/unit` (excluding `test_ci_gate.py` /
`test_dependencies_lock.py`, which need `.venv-test` at the repo root and are not present in
this worktree): 2137 passed, 5 known-environmental failures. Combined with
`tests/integration/test_email_inbound_reply_paths.py`, `tests/unit/test_private_channel_migration.py`,
`tests/integration/test_state_rebuild.py`, and `tests/integration/test_message_persistence.py`:
113 passed. `ruff check` on every file touched: no new findings (three pre-existing,
unrelated `simulation.py` findings noted and left alone, outside scope).

### K follow-ups (same day)

A further opus review of the K fixes above landed several same-day commits (all on this
branch, ahead of section L below) that supersede parts of the K-2 and K-4 text as written:

- **K-2 is superseded by a per-pool shutdown signal.** The K-2 text above describes
  `SHUTTING_DOWN` as a single shared `threading.Event`. `1c18003` ("make the
  shutdown-abort signal per-pool, not one shared event") replaced that with one
  `threading.Event` per Slack executor pool generation, bound into each worker thread's
  thread-local at thread start — because a single shared Event could not survive a
  shutdown/re-create cycle (K-9) correctly: clearing it the moment a new pool is created
  would also silence the signal for an OLD pool's worker thread still sleeping through a
  backoff right now. `slack_client.SHUTTING_DOWN` remains as the fallback for a caller
  never bound to a pool (see L-1 below). `a570521` and `b46ac2b` are further K-2
  follow-ups (a real Slack-error-shaped response on the shutdown exception; only
  forgetting a PI-inbound attempt count once the HANDLED write commits).
- **K-4 is superseded by strip-compare + empty-is-authoritative semantics.** The K-4 text
  above describes `_sync_private_profiles_from_db` as rewriting disk "if
  `ResearcherProfile.private_profile_md` differs from the agent's current private
  profile" — an exact comparison. `3bc8715` ("normalize whitespace and honor a cleared
  DB profile in the private-profile sync") changed this to a `.strip()`ped comparison
  (avoiding a spurious rewrite-every-startup from `update_private_profile`'s own
  trailing-newline-on-disk-only convention) and added the empty/NULL-DB-value branch:
  a PI clearing their standing instruction is now itself treated as authoritative,
  removing a stale non-default disk file and invalidating the cache. Section L below
  (L-2/L-3/L-4) fixes three residual defects in that same empty/NULL branch.
- **K-6 is superseded by a subscribed_channels restore.** `b8bca06` ("restore
  subscribed_channels on a roster re-add") added the `_rebuild_one_agent_state` query
  the K-6 text above does not mention; L-6 below fixes a gap in that query.
- **K-9 gained a test-fixture follow-up.** `47fc012` shuts down test-created Slack pools
  in fixture teardown, closing a thread leak the K-9 fix's lazy pool re-creation
  introduced for any test that forces a fresh pool into existence.

## L — opus review of same-day follow-ups (2026-09-10)

A further opus review of the K-2/K-4/K-6/K-9 follow-ups above found seven residual
defects, all fixed on this branch.

**L-1 (MEDIUM) — the shutdown-abort signal never reached a caller outside the Slack
executor pool.** The K-2 follow-up above made the abort signal per-pool
(`bind_shutdown_event`/`_current_shutdown_event`), with `slack_client.SHUTTING_DOWN`
kept only as a fallback for a thread never bound to a pool's own event —
but `shutdown_slack_executor()` only ever set the CURRENT pool's event, so
`SHUTTING_DOWN` was never set anywhere in production. `AgentSlackClient` calls made
directly on the agent-run process's event-loop thread (`src/agent/main.py`'s
`_run_simulation`, and `src/agent/simulation.py`'s roster sync/turn-taking — none of
which go through `src.services.slack_executor`'s pool) are bound to exactly that
fallback, so a retry sleep blocked on a sustained Slack throttle could no longer be
aborted at shutdown at all: it would run out its full (up to 180s) wait budget and hold
up process exit until SIGKILL. Fixed: `shutdown_slack_executor()` also calls the new
`slack_client.signal_shutdown()`, which sets `SHUTTING_DOWN`; and the agent-run
process's own SIGTERM/SIGINT handler (`_run_simulation`'s nested `shutdown()`) calls it
directly too, since that process never calls `shutdown_slack_executor()` at all. Tests
(red first): `test_shutdown_slack_executor_also_sets_the_fallback_shutdown_event` in
`tests/unit/test_slack_executor.py` (a main-thread retry sleep aborts promptly with NO
monkeypatching of the event), and a source-level pin,
`test_shutdown_signals_the_slack_client_fallback_event`, in the new
`tests/unit/test_agent_main_shutdown.py`.

**L-2 (MEDIUM) — a missing `ResearcherProfile` row was treated as a PI-cleared
instruction.** The K-4 follow-up's empty/NULL branch computed
`db_content = (profile.private_profile_md or "").strip() if profile else ""` — a
genuinely MISSING row and a REAL row with empty content both collapsed to
`db_content == ""`, so a user who simply never had a profile row created had their
on-disk private profile file unrecoverably unlinked. Fixed: return immediately when
`profile is None`, before computing `db_content` at all. Test (red first):
`test_a_missing_researcher_profile_row_leaves_a_stale_disk_file_alone` in
`tests/integration/test_state_rebuild.py`.

**L-3 (LOW) — an `unlink()` failure on a genuine clear silently kept serving the stale
content.** If `profile_path.unlink()` raised in the cleared branch, the exception
escaped to the method's outer bare `except Exception` (log-and-swallow), skipping
`reload_private_profile()` entirely — and even a bare `reload_private_profile()` call
would not have been enough, since the next read would just re-load the SAME file
`unlink()` failed to remove. Fixed: wrap the unlink in its own try/except; on failure,
call the new `Agent.force_clear_private_profile()`, which sets the cache directly to
the same default text `Agent.private_profile`'s own fallback uses for a missing file,
bypassing disk entirely. Test (red first):
`test_an_unlink_failure_on_a_real_clear_still_invalidates_the_cache` in
`tests/integration/test_state_rebuild.py` (an unwritable dir).

**L-4 (LOW) — the roster re-add path never ran the private-profile DB/disk
reconciliation.** `_rebuild_one_agent_state` (the K-6-follow-up rebuild path, an
inactive->active roster flip) built a fresh `Agent()` and reconstructed
pending_proposals/active_threads/cursors/call-counts from the DB, but never ran the
K-4-follow-up reconciliation `_rebuild_agent_state` runs once at startup — a re-added
agent's `private_profile` cache was whatever a fresh `Agent()` happened to read off a
possibly-stale disk file. Fixed: extracted the per-agent body of
`_sync_private_profiles_from_db` into `_sync_one_agent_private_profile_from_db`, called
from both the startup loop (unchanged behavior) and `_rebuild_one_agent_state`. Test
(red first): `test_a_roster_re_add_also_resyncs_the_private_profile` in
`tests/integration/test_state_rebuild.py`.

**L-5 (LOW) — the handler and its HANDLED-write retry shared one attempt budget.**
`_poll_inbound_from_db`'s handler-retry loop and its HANDLED-write retry loop share one
counter keyed on `message_ts`; the K-2-follow-up fix that made the counter survive from
the handler phase into the write phase never reset it at the point it should: a handler
that failed a few times before succeeding left the counter part-spent, so an unrelated
write failure could hit `PI_INBOUND_MAX_ATTEMPTS` on its very first attempt. Fixed: pop
the counter the moment the handler succeeds, before the HANDLED write is even
attempted, giving the write its own full budget; also downgraded the write's give-up log
from ERROR to WARNING (the side effects already ran; only the marker write is
exhausted). Test (red first):
`test_a_write_that_starts_failing_only_after_the_handler_retried_gets_its_own_full_budget`
in `tests/unit/test_simulation_logic.py` (handler fails `PI_INBOUND_MAX_ATTEMPTS - 1`
times then succeeds; the write got exactly 1 attempt before the fix, the full budget
after).

**L-6 (LOW) — the roster re-add's subscribed_channels restore trusted any channel
name.** The K-6-follow-up query had no visibility predicate and did not intersect with
`self._channel_id_map`, unlike `_sync_private_channels_from_db`'s own gate — a channel
this engine process had never discovered could end up named in `subscribed_channels`
with no corresponding `_channel_id_map`/`_channel_visibility` entry, breaking every
lookup keyed on those maps for that name. Fixed: added the
`AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE` predicate and intersected the
query's result with `self._channel_id_map`. Test (red first):
`test_a_roster_flip_never_subscribes_an_undiscovered_channel` in
`tests/integration/test_state_rebuild.py`.

**L-7 — this doc.**
