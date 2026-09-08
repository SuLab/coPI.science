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
