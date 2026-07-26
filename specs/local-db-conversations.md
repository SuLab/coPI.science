# Local-DB-Backed Conversations (Slack as an optional mirror)

Status: in progress. Companion to `specs/agent-system.md` and
`specs/privacy-and-channel-visibility.md`.

## Motivation

Historically the agent simulation treated **Slack as the primary durable store
of conversation content**. The in-memory `MessageLog` (`src/agent/message_log.py`)
held message text only for the life of the process and was **rebuilt from Slack
history on every startup** (`SimulationEngine._rebuild_state_from_slack`). The
database was a metadata-only sibling: `agent_messages` stored `message_length`
but no content, and only the agent's *own* posts were recorded. With Slack off,
the system could not reconstruct any conversation and a restart lost all history.

This spec makes the local PostgreSQL database the **single source of truth** for
conversations. The simulation must run identically with **all Slack API access
disabled**. When Slack is enabled it is a redundant, bidirectional mirror/view:

- **Outbound:** DB-origin messages are posted to Slack for human viewing.
- **Inbound:** human/PI Slack messages are written into the DB.

The DB reproduces the Slack-provided semantics the engine relies on — channels,
threads, membership/permissions (`public` vs `collab_private`), and PI↔bot DMs —
by **reusing the existing schema** wherever possible.

## Core design rules

1. **Canonical id = the id a message is born with.** A DB-origin message
   (Slack-off, or an agent post in mirror mode) gets a locally-minted, ts-shaped
   id from `mint_ts()`. A Slack-origin message keeps its Slack `ts` as canonical.
   In pure Slack-on mode `message_ts == slack_ts`, so behavior is identical to the
   pre-change system. Every structure keyed by ts (`PostRef.post_id`,
   `ThreadState.thread_id`, `_poll_cursors`, `ThreadDecision.thread_id`,
   `MessageLog._by_ts`) is unchanged.

   **Corollary: never hand a canonical id to Slack.** Slack threads on the
   *root's* `ts`, which equals the canonical `thread_ts` only when the root was
   born on Slack. `_slack_parent_ts()` translates canonical → Slack via the root
   entry's `slack_ts`, and `_post_message` skips the mirror entirely (DB-only,
   with a warning) when the root has no Slack presence, rather than posting
   against an id Slack has never seen. This is what makes enabling Slack
   mid-conversation degrade safely instead of detaching or erroring. The mapping
   is restored on rebuild, so it survives a restart. The web process has no
   `MessageLog`, so `private_channels._slack_parent_ts_from_db()` performs the
   same translation from `agent_messages` for the private-channel migration's
   origin-thread close marker.

   **`slack_ts` is the only evidence of Slack presence; a NULL means "not on
   Slack".** It is never inferred from the channel id. Inferring ("a row in a
   real Slack channel was born on Slack, so its canonical id is its Slack ts")
   looks reasonable for pre-Stage-6 history but is unsound: a DB-origin message
   can carry a real Slack `channel_id` too — a PI message from the web inbox
   resolves it from the `agent_channels` row, and so does an agent post whose
   mirror failed. Both mint a *local* id, and inferring promotes it to a Slack ts
   Slack never issued, which then goes out as a `chat.postMessage` `thread_ts`
   and orphans the reply. Legacy rows are repaired once by
   `scripts/backfill_slack_ts.py`, which asks Slack which timestamps exist rather
   than assuming; run it before deploying on a workspace with pre-Stage-6 history.

2. **`mint_ts()` is monotonic and unique — across processes, not just within
   one.** Ids are carried as integer microseconds (never round-tripped through a
   float, which cannot hold microsecond precision at current epoch magnitudes)
   and strictly advance; the high-water mark is seeded at rebuild from
   `max(posted_at)` so minted ids sort after restored history. This preserves
   `posted_at = float(ts)` ordering. Three processes mint into the same run — the
   engine, the web app and GrantBot — so each minter also owns a **writer slot**:
   ids are quantized to `WRITER_SLOT_MODULUS` (100 µs) and the writer id occupies
   the low microsecond digits, giving every writer its own residue class. Without
   this, two processes minting in the same microsecond produce the identical id,
   and the `uq_agent_messages_run_ts` conflict handler resolves it by *dropping*
   one message — unrecoverable now that the DB is the only durable store. Writer
   ids are claimed at process entry with `set_default_writer_id()`
   (`src/agent/ids.py`). The DB constraint remains the backstop.

3. **Persist at the single chokepoint `MessageLog.append`.** Peer, human/PI, and
   reopen messages reach state only via `append`. A persist callback there
   (mirroring `set_bot_name_map`) captures all senders and keeps `message_log.py`
   DB-agnostic. DMs never enter `MessageLog`, so they persist separately.

4. **`append` is idempotent** (skip in-memory add and callback when `ts` is
   already present) — safe only because `mint_ts()` guarantees uniqueness.

5. **Reuse, don't replace.** Extend `agent_messages` rather than add a parallel
   table. The `visibility` model, `private_channel_members`, `_visibility_permits`,
   thread participant rules, and `_sync_private_channels_from_db` all port directly.

## Schema

### `agent_messages` (extended — migration 0019)

Adds, all NOT NULL with server defaults so existing rows survive:
`content Text ''`, `sender_name String(100) ''`, `is_bot Boolean true`,
`posted_at Float 0` (ordering key), and nullable mirror columns
`slack_ts String(50)`, `slack_channel_id String(100)`, `slack_thread_ts String(50)`.
`agent_id` is relaxed to nullable and read as `sender_agent_id` (NULL = human/PI).
`message_ts` is the canonical id.

Indexes: `UNIQUE(simulation_run_id, message_ts)`,
`INDEX(simulation_run_id, posted_at)`,
`INDEX(simulation_run_id, channel_name, posted_at)`,
partial `INDEX(simulation_run_id, slack_ts) WHERE slack_ts IS NOT NULL`.

### `pi_dm_messages` (new — migration 0020)

`id`, `simulation_run_id` (FK cascade), `agent_id`, `pi_user_id` (Slack user id
Slack-on; `local:<users.id>` off), `direction` enum(`inbound`,`outbound`),
`content`, `sender_name`, `ts` (canonical), `slack_ts` (nullable), `posted_at`,
`created_at`. Indexes `(simulation_run_id, agent_id, posted_at)` and
`(simulation_run_id, direction, posted_at)`.

### Channels

No schema change. Slack-off stores `local:<name>` in `agent_channels.channel_id`.
Public subscriptions stay re-derived from profile keywords at seeding; a
`channel_subscriptions` table is deferred until hand-edited/agent-created public
subscriptions must survive a restart independent of re-derivation.

## Transport abstraction

`src/agent/transport.py` defines a `Transport` `Protocol` covering the exact
method set the engine calls on `AgentSlackClient`:

- outbound: `post_message`, `send_dm`, `create_channel`, `create_private_channel`,
  `invite_to_channel`, `join_channel`, `list_channels`, `open_dm_channel`
- inbound: `poll_channel_messages`, `get_thread_replies`, `get_all_thread_replies`,
  `get_full_channel_history`, `poll_dm_messages`
- identity: `connect`, `is_connected`, `bot_user_id`, `resolve_user_name`,
  `is_bot_user`

`SlackTransport` is today's `AgentSlackClient` conformed. `NullTransport` reports
`is_connected=False` / `bot_user_id=None` (so existing
`if client and client.is_connected` branches take the no-op path), returns a
minted-id dict from outbound calls, and `[]` from inbound calls.

`slack_enabled` (config + CLI) auto-detects from the presence of ≥1 valid token
with an explicit override; `--mock`/no-token ⇒ Slack-off.

## PI interaction without Slack

When Slack is off, PIs interact through a web interface that **writes inbound
rows** (`agent_messages` with `is_bot=false`/`sender_agent_id=null`, or
`pi_dm_messages` `direction='inbound'`). The engine's `_poll_pi_inbox_from_db()`
reads those new rows each tick and routes them through the existing PI-handling
logic (`_check_pi_proposal_review`, `has_pi_directive`, thread reopen, `@bot` tag
→ `handle_channel_tag`/`handle_dm`). This is the convergence point: the Slack
mirror's inbound side and the Slack-off PI path are the same DB reader. Identity
is `AgentRegistry.user_id` rather than `slack_user_id`.

## Rollout (staged, each independently shippable)

1. Content persistence + DB rebuild (`_rebuild_state_from_db`; demote
   `_rebuild_state_from_slack` to a Slack-gated reconcile).
2. Local id minting (`mint_ts`, remove `mock_ts` constant, `local:` channel ids).
3. Transport abstraction + `slack_enabled` + `_poll_pi_inbox_from_db`.
4. Slack-less private-channel migration branch. Both branches persist the handover
   (posts + origin-thread close marker) through one helper,
   `private_channels._add_handover_message`, so the refinement channel is
   reconstructable from the DB alone whether or not Slack was involved.
5. PI web interface.
6. Secondary Slack posters guarded by `slack_enabled`; outbound mirror write-back
   (records `slack_ts`; reconcile dedups on `slack_ts`).
7. One-time Slack→DB backfill (`scripts/backfill_slack_history_to_db.py`).

## Verification

- Slack-off boot: `python -m src.agent.main --mock --fresh --max-runtime 1`
  starts, runs `_rebuild_state_from_db`, seeds `local:` channels, persists content.
- Continuity: two Slack-off runs — resume rebuilds the log with content and
  active threads intact.
- Parity: Slack-on produces identical DB/log state and `message_ts == slack_ts`.
- Existing suites stay green (`tests/test_message_log.py`,
  `test_simulation_logic.py`, `test_private_channel_migration.py`,
  `test_roster_sync.py`, `test_privacy_scoping.py`, `test_thread_not_found.py`).
