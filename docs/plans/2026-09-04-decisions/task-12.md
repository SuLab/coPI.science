# Task 12 — the residual orphan-channel window (#21 CL21-2)

Plan: `docs/plans/2026-09-04-close-remaining-gaps.md`, Task 12 Step 6 (DECIDE).
Scope of this ruling: **only** the window that survives Task 11 (`391e545`) and Task 12's
`refined_in_channel` guard — the migration's own `db.commit()` failing *after*
`create_private_channel` has already succeeded on Slack.

## Ruling

**Option (b): leave the window, and state it in #21's closing comment with an orphan-sweep
procedure.** Option (a) — drop the per-call timestamp suffix at
`src/agent/slack_client.py:933-936` so Slack's own `name_taken` becomes the idempotency
guard — is **rejected**, for two independent reasons.

**1. The base slug is not per-proposal, so a deterministic name collides for a real reason.**
`_build_slug` (`src/services/private_channels.py:70-84`) is
`priv-{sorted agent ids}-{origin channel}`. It carries no proposal identity at all. Two
different proposals between the same pair in the same channel are ordinary — and
production already has exactly that pair of channels live (see Evidence). Under (a) the
second refinement would resolve to the *first* refinement's channel name. Slack would
either refuse the reopen or, with the "adopt the existing channel by name" design v1
rejected as too expensive, silently drop the second proposal's PI guidance into the first
proposal's conversation. That is a worse failure than the one being fixed: the whole point
of the private channel is that one PI's guidance stays with one proposal.

**2. As literally scoped, (a) does not even produce idempotency.** `create_private_channel`
does not adopt on `name_taken`: it retries with random entropy
(`slack_client.py:946-952`) and returns `None` when the attempts are exhausted. Its sibling
`create_channel` adopts (`:875-901`); this one deliberately does not. So dropping the
suffix converts the retry from "mints a second channel" into "the reopen fails with no
channel", and turning it into an adopt is the rejected design, not a suffix deletion.

**What the residual window actually is, stated precisely.** With both fixes in, a second
channel requires `migrate_public_thread_to_private`'s own `await db.commit()`
(`private_channels.py:644`, and `:415` on the Slack-off path) to fail. Everything durable
rides that one commit — the `AgentChannel` row, its `PrivateChannelMember` rows, the
handover `agent_messages`, and `thread_decisions.refined_in_channel`. So the failure leaves
**no DB trace whatsoever** and a fully-formed Slack channel: both bots invited, handover
posted, the other PI DM'd. The new guard cannot see it, because the guard reads
`refined_in_channel` and `refined_in_channel` was in the commit that failed. A retry
therefore migrates again and the first channel is orphaned — real, populated, and invisible
to the engine, which discovers private channels only from `agent_channels`
(`src/agent/simulation.py:2113-2126`).

Closing it needs a durable *intent* record written and committed **before** the Slack call
(a two-phase claim), then reconciled after. That is a new DB-and-Slack design, not a
one-line change, and it is out of this task's scope. Filing it as a follow-up rather than
improvising it here is the ruling.

**Exposure today.** The Slack-off path (`_migrate_offline`) is not exposed at all: it makes
no Slack call, so its commit failing loses nothing that exists outside the DB. The Slack-on
path is exposed on two callers — the web reopen route, live today; and the e-mail twin,
which is unreachable while `enable_inbound_email` is `False` (unset in prod) and arms at
step 4 of `docs/inbound-email.md`. The trigger is a local-Postgres commit failing in the
instant after a multi-second Slack round trip returns.

## Evidence

Measured 2026-09-04 against the disposable production copy `copi-prodtest-db`
(`127.0.0.1:55434`, db `copi`) — never production itself.

```bash
docker exec -e PGPASSWORD=copi copi-prodtest-db psql -U copi -d copi -c "
SELECT id, channel_id, channel_name, created_by_agent, migrated_from_channel_id, created_at
FROM agent_channels WHERE visibility='collab_private' ORDER BY created_at;
SELECT id, thread_id, agent_a, agent_b, channel, refined_in_channel
FROM thread_decisions WHERE refined_in_channel IS NOT NULL;"
```

Three private refinement channels exist, and **two of them are the same agent pair in the
same origin channel**:

| channel_id | channel_name | created_at |
|---|---|---|
| `C0AUHSASJLU` | `priv-lotz-su-single-cell-omics` | 2026-04-21 21:47 |
| `C0AUCMNCEFQ` | `priv-lairson-su-drug-repurposing` | 2026-04-21 23:34 |
| `C0BB48ETLQL` | `priv-lairson-su-drug-repurposing-20260616-180113` | 2026-06-16 18:01 |

The last two are distinct Slack channels, two months apart, refining **different**
proposals (`thread_decisions.thread_id` `1776150307.616969` and `1781124831.657319`,
`refined_in_channel` `C0AUCMNCEFQ` and `C0BB48ETLQL`). The earlier one predates the suffix,
which is why its name is the bare base — i.e. the two rows are the before/after of the very
change (a) proposes to revert, and they coexist legitimately. Under (a) the June reopen
would have resolved to the April channel's name.

The population that can reach this collision is not marginal:

```bash
docker exec -e PGPASSWORD=copi copi-prodtest-db psql -U copi -d copi -Atc "
SELECT count(*) FROM (SELECT least(agent_a,agent_b), greatest(agent_a,agent_b), channel
  FROM thread_decisions GROUP BY 1,2,3 HAVING count(*) > 1) t;"
```

**111** (pair, origin-channel) groups hold more than one proposal, covering **407 of 1017**
`thread_decisions`; the largest group holds **39**. Any two reopens inside one group would
share a name under (a).

The suffix's stated purpose is pinned by tests that would have to be deleted, not adapted:
`tests/unit/test_private_channel_migration.py:259-300`
(`TestCreatePrivateChannelNameTaken`), whose class docstring is the same claim — "a second
proposal between the same agent pair in the same origin channel yields an identical base
slug".

That the suffix is what lets Slack accept the *second* create (the defect Task 12 fixes) was
read directly at `src/agent/slack_client.py:933-936`: `ts = time.strftime(...)` is evaluated
per call, inside `create_private_channel`, so a retry seconds later asks for a name Slack has
never seen. `_migrate_offline` builds its `local:` id the same way
(`private_channels.py:344-346`), which is why the Slack-off retry in the new test produces
two `AgentChannel` rows carrying the *identical* `local:` id — `agent_channels` has no
uniqueness constraint on `(simulation_run_id, channel_id)` (`src/models/agent_activity.py:140-176`).

## Consequence a closing comment must state

For **#21** (Task 32/33), and for the deploy-note accumulator that Task 35 owns:

1. **The window is knowingly left open.** After this branch, a duplicate private refinement
   channel requires `migrate_public_thread_to_private`'s own commit to fail in the instant
   after Slack has created the channel and accepted the handover. It leaves no DB row at
   all, so no code-side guard can detect it; a retry mints a second channel and orphans the
   first. Both the web reopen route and the e-mail reopen path can reach it; the e-mail one
   only once `ENABLE_INBOUND_EMAIL` is on.
2. **The suffix must not be removed** to close it. `priv-{a}-{b}-{origin}` is not unique per
   proposal — production already runs two legitimate `priv-lairson-su-drug-repurposing`
   channels — so a deterministic name would merge two proposals' private conversations.
3. **Orphan-sweep procedure** (run after any reopen that returned a 500 or a terminal
   "couldn't reopen" e-mail, and once before enabling inbound e-mail):
   - DB side, the set of channels that are *supposed* to exist:
     `SELECT channel_id FROM agent_channels WHERE visibility='collab_private';`
   - Slack side, per bot token, `conversations.list(types="private_channel")`, keeping names
     matching `priv-*`.
   - An orphan is a Slack `priv-*` channel whose id is **not** in the DB set. It will have
     the handover posted and both bots as members, and no bot will ever read it again.
   - Remediate by archiving it (`conversations.archive`) after confirming the same proposal
     has a live channel — compare `thread_decisions.refined_in_channel` for the pair. Do not
     delete DB rows; there are none.
4. **Follow-up, not fixed here:** closing the window properly needs a durable claim row
   committed *before* the Slack `conversations.create`, reconciled after it returns. New
   design work; deliberately not improvised in this task.
