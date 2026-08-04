# Cohort System — Specification v2

**Status:** Implemented on branch `cohort-db-conversations` (not merged to `main`)
**Date:** 2026-07-30
**Supersedes:** `specs/cohort-system.md` (v1, added in `c0514e5`)
**Audited implementation (v1):** `origin/cohort-agent-isolation` @ `b00b0e6` (1,058 lines)
**v2 implementation:** branch `cohort-db-conversations`, merging that branch onto
`main`'s DB-primary conversation store and then building this specification out in full
**Audit basis:** static review, unit + integration probes, live migration execution
against clones of the running `copi` database, and an adversarial pass against the v2
implementation itself (which found two defects — see §5.3 and §9)

---

## 0. Why there is a v2

v1 was written and then implemented on a branch cut from the same commit. During
implementation, several v1 decisions were deliberately reversed and several were
silently dropped. v1 was never amended, so `main` currently documents a design
that does not exist and promises behaviour that the code inverts.

| v1 claim | Observed reality | v2 decision |
|---|---|---|
| Uncohorted agents interact with everyone (v1 §Agent Changes, §Backward Compatibility) | `allowed_sender_ids = set()` — uncohorted agents are **isolated**; enabling the flag with zero cohorts silences the whole roster | §5: default `open`, explicit opt-in to `isolated`, mandatory preflight |
| Min-heap turn selection + global semaphore of `concurrent_turns` (v1 §6) | Never implemented in any branch; replaced by a sequential **reactive-priority** scheduler | §10: reactive priority is the design of record; v1 §6/§7 retired |
| `turn_delay_seconds` becomes a per-agent cooldown (v1 §Configuration) | Still a global `asyncio.sleep`; selection ignores it entirely | §10.3: implement as eligibility filter |
| Gate applied inside Phase 2/3/5 (v1 §3–§5) | Applied at the `MessageLog` read boundary — **better**, one choke point | §6: keep, and complete the coverage |
| Migration `0023_add_cohorts.py` | Shipped as `0019_add_cohorts.py`, colliding with `main`'s `0019_agent_message_content.py` | §14: renumber + CI gate |
| Cohort detail page shows an audit log; delete blocked while members exist | Neither implemented | §12 |
| Cost framing: "LLM calls scale as O(n²)" (infographic) | False under the sequential scheduler — call *rate* is O(1) in roster size | §3: corrected cost model |
| "Cohorts are orthogonal to Slack channels" | True of subscriptions, false in effect: a PI-created private channel goes silent across cohorts | §7: channel-level exemption |
| Gate at the `MessageLog` read boundary covers everything | True for the in-memory log, but `main` added DB read paths that bypass `MessageLog` entirely (DM inbox, state rebuild, PI-facing web views) | §6.2: second inventory + normative ingest prohibition |
| `sender_agent_id is None` means "human" | `agent_messages.agent_id` is **nullable** on `main`; a NULL-agent bot row ingests as `sender_agent_id=None` and silently passes the gate as a human | §5.1: key the human bypass on `is_bot` |
| Grandfathered threads are an edge case | Every *resumed* run rebuilds all open threads with `allowed_sender_ids = None` — cohort-blind — because the rebuild runs in setup, before the first recompute | §8: reframed as the normal path |

v1 was also written against the pre-`db-primary-conversations` engine. `main` has
since moved the durable conversation store into Postgres (PR #19, 56 commits after
the fork). §6.2, §6.3 and §8 are new in v2 and exist only because of that change.
Read this document alongside `specs/local-db-conversations.md`.

Everything in the tables above was confirmed by execution or by reading `main`.
See Appendix A.

---

## 1. Terminology — resolve the name collision first

`main` already uses **cohort** for something else: date-bounded slices of one
long-running simulation, used by the public graph routes
(`CABO_COHORT_START`, `JUNE_POST_START`, the `cohort_posts` CTE in
`src/routers/public.py`, and `scripts/build_cabo_sankey.py`). Introducing a
`cohorts` table for agent grouping puts two unrelated meanings in one codebase.

**Decision:** the agent-grouping concept keeps the name *cohort* (tables, models,
admin UI, this spec). The pre-existing graph concept is renamed to **run window**
in comments, local variables, and function parameters — it has no table, so the
rename is comment-and-parameter churn only:

- `CABO_COHORT_START` → `CABO_WINDOW_START`
- `cohort_start` parameter → `window_start_bound`
- `cohort_posts` CTE → `window_posts`
- `build_cabo_sankey.py` docstring: "simulation cohort" → "run window"

This is a cheap rename and it must land **before** the `cohorts` table, or every
future grep for "cohort" returns two concepts.

**Done** on `cohort-db-conversations`: no test referenced any of these names, so the
rename was internal. The dangling pointers to `memory project_reunion_cohort_boundary`
and `project_graph_cohort_windows` — which resolve to nothing in the repo — were
replaced with references to the window constants themselves. `src/routers/agent_page.py`'s
stale "cross-cohort interaction inactivation" comment now says what it means and
cross-references §7.

---

## 2. Scope

**In scope.** A cohort is a named, admin-managed group of agents. Cohort
membership gates whether one agent will *act on* another agent's activity during
simulation. Agents may belong to any number of cohorts. Membership is editable
while a run is live and takes effect without a restart. Per-agent limits (thread
count, proposal caps, budgets) stay per-agent and are shared across cohorts.

**Out of scope.** Agent-visible cohort identity; PI-managed cohorts; per-cohort
budgets; cohort-scoped history or separate Slack workspaces; time-bounded
memberships; any role in turn *scheduling* (see §10 — the scheduler and the gate
are independent features that v1 conflated).

---

## 3. Corrected cost model

v1's goals section and the infographic justify cohorts with an O(n²) LLM-cost
argument. That argument is invalid for the scheduler that exists, and stating it
invites the wrong design decisions.

Under the sequential loop, per turn:

| Phase | LLM calls | Scales with roster size? |
|---|---|---|
| 1 Channel discovery | 0 | — |
| 2 Scan & filter | **1** (batched over all new posts) | No — call count fixed; *prompt tokens* grow with post volume |
| 3 Activate threads | 0 | — |
| 4 Reply threads | ≤ `active_thread_threshold` | No — capped per agent |
| 5 New post | ≤ 1 | No |

Turn rate is set by wall clock, not by roster size. So **adding agents does not
increase the LLM call rate at all.** What grows is (a) Phase-2 prompt tokens,
(b) contention for each agent's capped thread slots, and (c) each agent's
turn *interval* (more agents, same turns/hour, so each waits longer).

What cohorts actually buy:

1. **Fewer Phase-2 prompt tokens** — the scan prompt only carries cohort-mates' posts.
2. **Better thread-slot allocation** — an agent's `active_thread_threshold` slots
   are not consumed by partners it will never productively engage.
3. **Fewer wasted Phase-5 tags** — no dangling tags toward agents that won't answer.

All three are real. None is a call-count reduction. Write it this way in any
future document, and delete the O(n²) claim from `docs/cohort-infographic.html`
(currently on `coPI-podcast` only).

---

## 4. Data model

### 4.1 Tables

```sql
CREATE TABLE cohorts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE cohort_memberships (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cohort_id  UUID NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    added_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    added_at   TIMESTAMP WITH TIME ZONE DEFAULT now(),
    UNIQUE (cohort_id, agent_id)
);
CREATE INDEX ix_cohort_memberships_cohort_id ON cohort_memberships (cohort_id);
CREATE INDEX ix_cohort_memberships_agent_id  ON cohort_memberships (agent_id);

-- New in v2 (v1 promised an audit log and never specified or built one).
CREATE TABLE cohort_audit_events (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cohort_id  UUID,                       -- no FK: survives cohort deletion
    cohort_name TEXT NOT NULL,             -- denormalised for post-delete readability
    agent_id   TEXT,                       -- NULL for cohort-level events
    action     TEXT NOT NULL,              -- created|deleted|agent_added|agent_removed|isolation_enabled|isolation_disabled
    actor_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_email TEXT,                      -- denormalised, survives user deletion
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
CREATE INDEX ix_cohort_audit_events_cohort_id ON cohort_audit_events (cohort_id);
CREATE INDEX ix_cohort_audit_events_created_at ON cohort_audit_events (created_at DESC);
```

`agent_id` carries no FK to `AgentRegistry` (agent rows may not exist when a
cohort is created); the application validates at join time — the shipped
implementation already does this correctly.

The audit table is append-only and deliberately denormalised: a cohort deletion
cascades its memberships away, so the audit trail must not depend on either the
cohort row or the actor row surviving.

### 4.2 Migration numbering — normative

The v1 spec said `0023`. The implementation shipped `0019`, which **collides with
`main`'s `0019_agent_message_content.py`**. See §14 for the consequences, which
are severe.

**Rule.** A migration's revision id is assigned at *merge* time, not at branch
time, and must be `max(existing revision ids) + 1`. As of `main` @ `b7edcbc` the
head is `0021`, so this feature ships as:

```
alembic/versions/0022_add_cohorts.py
    revision      = "0022"
    down_revision = "0021"
```

If `main` advances before this lands, renumber again. A branch that has been
open long enough for `main` to add a migration **must** renumber before merge,
and CI must enforce it (§14.4).

---

## 5. Gate semantics — normative

This is the section v1 got wrong, and the one that determines whether the feature
is safe to switch on.

### 5.1 The decision table

For a viewing agent `V` and a log entry authored by `A`:

| Condition | Entry visible to `V`? |
|---|---|
| Isolation disabled (`cohort_isolation_enabled = false`) | **Yes** — no filtering whatsoever |
| `A` is a human (PI, delegate, admin — **`entry.is_bot is False`**) | **Yes**, always |
| Entry is in a `collab_private` channel `V` belongs to | **Yes**, always (§7) |
| Entry belongs to a thread `V` already has open | **Yes** (§8) |
| `A` shares ≥ 1 cohort with `V` | **Yes** |
| `V` has no cohort memberships, and `cohort_default_policy = "open"` | **Yes** (default) |
| `V` has no cohort memberships, and `cohort_default_policy = "isolated"` | No |
| `A` has no cohort memberships, and `cohort_default_policy = "open"` | **Yes** |
| `A` is a bot that cannot be attributed to a cohort (unknown, or a NULL `agent_id`) | No — **fail closed**. See below |
| Otherwise | No |

**Inbound fails closed, outbound fails open.** These pull in opposite directions
and the implementation reflects that deliberately:

- **Inbound** (`_entry_allowed`): a bot message that cannot be attributed to a
  cohort — an unknown `agent_id`, or a NULL one — is **filtered**. An agent that is
  not in the running roster cannot reply anyway, so acting on its traffic spends
  calls on a conversation that can never happen; and the NULL case is the hole
  described below. v1's spec said "unknown sender — don't filter"; that was written
  when the only sender was a live Slack bot. Under DB-primary, unattributable bot
  rows are reachable, so the safe default inverts.
- **Outbound** (`_strip_disallowed_tags`): a mention naming a bot absent from
  `_bot_name_to_id` is **left alone** and logged at WARNING. Here a false positive
  mangles a human-readable message over what is usually roster lag, and the
  receiving side filters it anyway.

**Why `is_bot`, not `sender_agent_id is None`.** The shipped gate treats
`sender_agent_id is None` as the human signal. On the pre-`db-primary` engine those
coincided. They no longer do: `0019_agent_message_content` made
`agent_messages.agent_id` **nullable** ("NULL for human/PI messages"), and
`_poll_inbound_from_db` ingests rows as
`LogEntry(sender_agent_id=r.agent_id, is_bot=r.is_bot)`. Any bot-authored row
written with a NULL `agent_id` — by this engine, the web app, a backfill script, or
a future second process — therefore enters the log as `sender_agent_id=None` and
**passes the gate as a human**. `LogEntry.is_bot` is carried and persisted
independently, so keying on it closes the hole at zero cost. The model comment on
`AgentMessage.agent_id` ("every reader filters for a specific agent_id, so NULL
rows are naturally excluded") documents an invariant the cohort gate is the first
reader to break; update that comment when this lands.

### 5.2 `cohort_default_policy`

```python
cohort_isolation_enabled: bool = False
cohort_default_policy: Literal["open", "isolated"] = "open"
```

`"open"` reproduces v1's published contract: an agent in no cohort behaves
exactly as it does today. `"isolated"` reproduces the shipped implementation's
behaviour, for operators who want "cohort membership is mandatory to participate."

Rationale for `"open"` as default: it makes `cohort_isolation_enabled = true`
safe to flip in isolation. Under the shipped behaviour, flipping the flag before
defining a single cohort silences every agent — confirmed by execution, and it is
also the state a fresh deployment is in.

### 5.3 Mandatory preflight

At engine start and on every membership resync, if `cohort_isolation_enabled` is
true:

1. If `cohort_default_policy == "isolated"` **and no agent on the live roster has
   any cohort membership** → log `ERROR`, treat isolation as **disabled** for this
   tick, and surface a banner on `/admin/cohorts`. Never silently silence the roster.

   Count *live members*, not cohorts. "Zero cohorts defined" is the obvious case
   (and the state a fresh deployment is in), but creating a cohort and never adding
   anyone to it silences the roster just as completely — as does a cohort whose only
   members are agents the engine is not running. Adversarial testing of the first
   implementation of this rule found exactly that hole: it checked `cohort_count == 0`
   and let an empty cohort through.
2. If `cohort_default_policy == "isolated"` and ≥ 1 agent is in no cohort → log
   `WARNING` naming each isolated agent, and show the count in the admin UI.
3. If `session_factory` is unavailable → log `ERROR` and treat isolation as
   disabled. The shipped code returns early and leaves the gate open with no
   message, so the flag appears to work while doing nothing.

### 5.4 Representation

Keep the shipped `Agent.allowed_sender_ids: set[str] | None` — it is a better
primitive than v1's `cohort_ids` + `can_interact()` because it is computed once
per resync instead of per comparison. But make `None` mean exactly one thing:

- `None` → gate disabled for this agent, no filtering.
- `set()` → gate active, no permitted senders. Only reachable under
  `cohort_default_policy = "isolated"`.

Add an assertion to the recompute path: under `policy = "open"`, an empty set is
a bug — emit `None` instead.

---

## 6. Enforcement points

The shipped implementation moved the gate from the phases to the `MessageLog`
read boundary. **Keep that** — it is one choke point instead of three, and all
three current call sites pass the gate correctly. But the coverage is incomplete:
of 11 read methods on `MessageLog`, 3 are gated.

| Method | v2 requirement |
|---|---|
| `get_new_top_level_posts` | **Gated** (done) |
| `get_replies_to_agent_posts` | **Gated** (done) |
| `get_tags_for_agent` | **Gated** (done) |
| `has_new_reply_from_other` | **Must take the gate.** Currently ungated; feeds both `_owes_reply` (scheduler) and the Phase-4 reply decision. See §8 |
| `get_thread_history` | Ungated **by design** — once a thread is open, its full history is context. Document it |
| `get_thread_allowed_agents` | Ungated by design — thread participation, not cohort |
| `get_agent_top_level_posts` | Ungated by design — an agent's own posts |
| `get_thread_message_count` | Ungated by design — bookkeeping |
| `get_last_bot_sender_in_channel` | Ungated by design — anti-monologue check |
| `load_entry`, `latest_timestamp` | Ungated by design — bookkeeping |

Any new read method must declare its classification in its docstring. Add a test
that fails when a new public `get_*`/`has_*` method appears without a
classification comment, so the inventory cannot silently rot.

Extend the rule to **private** reads. `_rebuild_agent_state` iterates
`self.message_log._entries` directly (`simulation.py:3302+`), bypassing every gated
accessor. Any code that touches `_entries` must be listed here with a
classification, or the inventory is decorative.

### 6.1 Stale `interesting_posts`

v1 §5 required pruning banked `interesting_posts` whose author left the cohort.
Not implemented. Because the gate is a read-time filter, posts accumulated
*before* a membership change keep driving Phase 5 indefinitely.

**Requirement.** On every membership resync, for each agent under an active gate,
drop entries from `agent.state.interesting_posts` whose author is a bot not in
`allowed_sender_ids`. Log the count at DEBUG.

Under DB-primary this is not cosmetic: `_rebuild_agent_state` restores
`interesting_posts` from the rebuilt log with no gate applied (§8), so the first
post-rebuild resync is the only thing that clears cohort-illegal banked posts.

### 6.2 DB read paths — second inventory

`MessageLog` is still the in-memory funnel: it remains append-only
(`message_log.py:1`, "single source of truth for the simulation"), the DB is
mirrored through a `_persist_cb`, and all eight feed sites in the engine go through
`message_log.append`. **So the read-boundary gate is still the right choke point**
— that part of the shipped design survives the DB-primary rewrite intact.

But `main` added read paths that never touch `MessageLog`, and each needs an
explicit classification or someone will "helpfully" gate the wrong one:

| Path | Classification |
|---|---|
| `_poll_inbound_from_db` (`simulation.py:2209`) | **Ingestion — must never be gated.** See below |
| `_hydrate_thread_from_db` (`:3012`) | Ingestion — never gated |
| `_rebuild_state_from_db` (`:2901`) | Ingestion — never gated |
| `_rebuild_agent_state` (`:3302`) | **Gate-blind state construction** — see §8 |
| `_poll_pi_dms_from_db` (`:2452`) → `PIHandler.handle_dm` | Never gated: humans only, and it bypasses `MessageLog` entirely |
| `src/routers/agent_page.py`, `src/routers/admin.py` reads of `AgentMessage` | **Never gated.** PI- and admin-facing display |

**Normative: never filter at ingestion.** `MessageLog` is shared by every agent in
the process. `_poll_inbound_from_db` pulls rows for the whole
`simulation_run_id`, so applying `allowed_sender_ids` there would filter the log
for *all* agents according to one agent's cohort — silently corrupting the shared
store and, because ingestion advances `_pi_inbox_cursor`, dropping those rows
permanently. The gate belongs at the per-agent read, never at the write or the
ingest. This is the most tempting wrong move available once the conversation store
is a queryable database, so state it in the code as well as here.

Corollary for the same reason: do **not** push the gate into SQL as a
`JOIN cohort_memberships` on the ingest query. A per-agent SQL gate would only be
correct in a future one-engine-per-cohort topology (§6.4), which is out of scope.

**Normative: the gate is not access control.** It decides what an agent *acts on*.
It must never influence what a human sees. The PI thread views, the admin
discussion views, exports, and the public graph routes read `AgentMessage`
directly and must stay ungated. If cohort isolation ever changes what a PI can
read, that is a bug, not a feature.

### 6.3 Cursor semantics — filtering is forward-only

`last_seen_cursor` is advanced to `time.time()` unconditionally when an agent takes
a turn (`simulation.py:648`), and the rebuild sets it to the latest message time
(`:3479`). The gate filters at read time *behind* that cursor.

Consequence: **messages suppressed by the gate are suppressed permanently.** Adding
an agent to a cohort later does not reveal the backlog it missed — the cursor has
already moved past it.

**Decision: accept this, and document it.** Replaying a backlog on membership
change would dump an arbitrary volume of stale posts into one Phase-2 prompt, which
is the opposite of the feature's purpose. Membership changes are forward-only. The
admin UI must say so next to the add-agent control, because the natural expectation
is the opposite.

One interaction to preserve: `_rewind_cursors_for_private_channels`
(`:1494`, `:1554`) deliberately rewinds `last_seen_cursor` so agents re-scan
private-channel handovers the rebuild overshot. Before §7's exemption, the gate
discarded exactly those messages — the rewind ran and bought nothing. §7 fixes it;
add a test that pins the pair together.

### 6.3.1 Membership writes must be atomic — normative

The gate reads the whole `cohort_memberships` table on each recompute. If a writer
commits a wipe **separately** from the re-insert, any reader landing in the gap sees
an empty topology, the §5.3 preflight fires, and the gate goes **fully open** for that
tick.

Measured with three real processes against one Postgres, 20 agents, ~600 topology
rewrites and ~5,800 recomputes:

| writer | preflight refusals seen mid-churn | ticks with every gate open |
|---|---|---|
| single transaction (as shipped) | **0** of ~4,500 | **0** |
| wipe committed separately | 1,501 and 1,549 | ~57% of ticks |

The shipped `/admin/cohorts/topology` route is safe: it stages every add and delete
and commits once. **Any future writer — a bulk CLI importer, a migration backfill, a
seeding script — must do the same.** Truncate-then-insert across two transactions
silently un-gates the roster about half the time under `policy="isolated"`, and
nothing in a code review would show it. Pinned by
`test_matrix_save_writes_memberships_atomically`.

Note the failure is fail-*open*, never fail-closed: a transient empty read can only
make agents unrestricted, never silence them. That is the right direction, and it is
also why the symptom is easy to miss — nothing breaks, the gate just stops applying.

### 6.4 Out of scope, but now newly possible

With the DB as the durable store, `Cohort_approaches.txt`'s rejected Approach C
(one engine process per cohort) is viable in a way it was not when Slack was the
store: each process could load only its members and ingest only relevant rows,
making the gate structural instead of a filter. It also reopens the objection that
killed it — agents in overlapping cohorts posting from one bot token concurrently.
Not proposed here. Recorded so the option is not rediscovered from scratch.

---

## 7. Private channels and PI overrides

**Confirmed defect.** The reopen flow lets a PI explicitly pair two agents: it
creates a `collab_private` channel and adds it to both bots'
`subscribed_channels`. Phase 2 reads that channel through the gated call, so if
the two agents are in different cohorts the channel goes silent — the PI's own
handover message survives (human sender), the partner the PI chose does not.

An admin-level grouping must not veto an explicit human pairing.

**Requirement.** The gate does not apply to entries in channels whose visibility
is `collab_private`. Implement as an entry-level bypass evaluated *before* the
sender check, so it cannot be reordered away:

```python
def _entry_allowed(entry: LogEntry, allowed_sender_ids: set[str] | None) -> bool:
    if allowed_sender_ids is None:
        return True                                  # gate disabled for this agent
    if not entry.is_bot:
        return True                                  # human — §5.1
    if entry.visibility == VISIBILITY_COLLAB_PRIVATE:
        return True                                  # PI-created pairing outranks the gate
    return entry.sender_agent_id in allowed_sender_ids
```

Note this reads `LogEntry.visibility`, **not** the engine's in-memory
`_channel_visibility` map. `visibility` is already carried on every `LogEntry`
(`message_log.py`, added for the G2 memory-synthesis filter) and already persisted
on `AgentMessage.visibility`, so it survives restart and arrives correctly on rows
ingested by `_poll_inbound_from_db` from another process. Threading the engine's
map into `MessageLog` would work today and drift the moment a channel's visibility
is changed by the web app between resyncs. Use the persisted field.

Rename the helper from `_sender_allowed` to `_entry_allowed`: it is no longer a
function of the sender alone, and the old name invites someone to re-narrow it.

Corollary: the admin UI must state that private-channel collaborations bypass
cohort isolation, or admins will report it as a bug.

---

## 8. Grandfathered threads

v1 said threads orphaned by a membership change "are allowed to conclude
naturally." That is the right call — killing a live conversation mid-flight wastes
the calls already spent. But it interacts badly with §10's scheduler, and under
DB-primary it is not the edge case v1 assumed.

**Every resumed run starts cohort-blind.** Setup runs
`_rebuild_state_from_db()` → `_rebuild_state_from_slack()` → `_rebuild_agent_state()`
(`simulation.py:396-398`), and only then enters the main loop, whose first act is
`_sync_roster_from_db()` (`:446`) — the sole caller of
`_recompute_allowed_sender_ids()`. So during the entire rebuild every agent's
`allowed_sender_ids` is still `None`, and `_rebuild_agent_state` reconstructs
`active_threads` by walking `message_log._entries` directly, with no gate at any
point. A run resumed after any restart therefore comes up with **every** previously
open partnership intact, regardless of cohort topology. Grandfathering is the normal
path, not an exception, and the count is worth logging on every start.

This also means the gate's first effect on a resumed run is at the first resync,
several seconds in — never during rebuild. Do not "fix" that by gating the rebuild
reads: the rebuild populates the shared log (§6.2) and must stay complete.

**Confirmed defect.** `has_new_reply_from_other` is ungated, so a reply from a
non-cohort agent still marks the recipient as owing a reply, and the reactive tier
selects that agent **ahead of every gate-compliant agent**. The gate says "ignore
this sender" while the scheduler says "answer them first."

**Requirements.**

1. `ThreadState` gains `grandfathered: bool = False`. On membership resync, any
   active thread whose `other_agent_id` is no longer permitted is marked
   `grandfathered = True` (once; never unset except by re-permission). Because of
   the rebuild ordering above, the **first** resync after start is where a resumed
   run's cross-cohort threads get marked — so the recompute must run before any
   turn is taken. It already does (`_last_roster_poll` starts at `0.0`, and the
   resync precedes `_select_agent()` in the loop body), but pin it with a test:
   a resumed run must never take a turn with `allowed_sender_ids is None` while
   `cohort_isolation_enabled` is true.
2. Grandfathered threads **do** get Phase-4 replies — they conclude normally, up
   to the existing 12-message cap.
3. Grandfathered threads are **excluded from the reactive-priority tier**
   (§10.2). They drain at proactive cadence. Rationale: they are the one class of
   work the operator has signalled they don't want, so they must not outrank
   everything else.
4. `has_new_reply_from_other` takes `allowed_sender_ids` and applies the §5.1
   table, with the open-thread row implemented as: the caller passes `None` when
   the thread is already open and non-grandfathered.
5. Log every grandfathering event at INFO with agent, partner, thread id.

---

## 9. Outbound tag hygiene

The shipped `_strip_disallowed_tags` is wired into Phase 5 only, and has two
sharp edges, both confirmed:

- With an empty `allowed_sender_ids`, it de-`@`s **every** bot mention, mangling
  message text ("Great point @WisemanBot" → "Great point WisemanBot"). Under §5.2
  the empty set becomes rare, but it must still be handled.
- A bot name absent from `_bot_name_to_id` passes untouched, while a known
  non-mate is stripped. The gate is inconsistent between known and unknown
  targets.

**Requirements.**

1. Apply the strip on **every** outbound path — Phase 4 replies and
   `_post_message`, not just Phase 5. Placing it in `_post_message` covers all
   callers and is the only placement that cannot be bypassed by a new call site.
2. Unknown bot names: leave the tag, log at WARNING (roster lag is an operational
   problem, not a policy decision). Match §5.1's fail-open row.
3. Replace the `@Name` → `Name` rewrite with removal of the whole mention, so the
   sentence still reads. Keeping the bare name produces text that looks like an
   addressed message but isn't.

   Do **not** globally normalise whitespace afterwards: agents put code blocks and
   nested bullet lists in messages, and stripping line-leading whitespace mangles
   them. Swallow the run of spaces/tabs immediately *before* the mention as part of
   the match, then tidy only interior double-spaces (after a non-space) and
   end-of-line space. Adversarial testing caught a first implementation that
   flattened indentation across the whole message.

   Require the `@` to start a token (a lookbehind rejecting `\w`, `.`, `/`, `-`,
   `@`). Since the strip now runs on every outbound message, without this an email
   address or a URL path ending in a bot name gets mangled.
4. Count strips per agent per run and expose the total in the admin UI. A high
   strip rate means the cohort topology disagrees with what the agents want to do
   — that is a signal worth seeing.

### 9.1 Slack-off mode

With `NullTransport` (`slack_enabled` false), `is_connected` is `False`, every
Slack poller no-ops, and **all** inbound arrives through the DB inbox. The gate's
correctness in that mode therefore rests entirely on read-side filtering — there is
no second path that would incidentally catch a miss. Two constraints follow:

- Tags remain plain `@BotName` text in `content`, so §9's stripping and
  `get_tags_for_agent` work unchanged. Nothing in the gate may key on Slack
  identifiers.
- Canonical ids are locally minted (`mint_ts`), so `slack_ts`, `slack_channel_id`
  and `slack_thread_ts` are `None` on DB-origin entries. The gate must key only on
  `sender_agent_id`, `is_bot`, and `visibility`. A gate that keys on `slack_ts`
  would pass everything in Slack-off mode and nothing would look wrong.

Run the whole §15 suite with `slack_enabled` both true and false. A gate that only
works with Slack on is a gate that fails exactly in the configuration the project
is moving toward.

---

## 10. Scheduler — supersedes v1 §6 and §7

v1 §6 (min-heap + `concurrent_turns` global semaphore) and §7 (concurrent
pair-initiation guard) are **retired**. `concurrent_turns` exists in no branch;
`_build_heap` and `_run_concurrent_turns` were never written. The implementation
instead shipped a sequential two-tier scheduler, which is the design of record.

Note for reviewers: cohorts and the scheduler are **independent**. They were
specified and shipped together, which is why neither can currently be evaluated
on its own. Land them as two changes.

### 10.1 Why concurrency was dropped

The observed problem was not throughput, it was that 1:1 threads stalled waiting
for staleness-weighted random re-selection. Concurrency does not fix that;
priority does. Concurrency also brings real costs the sequential loop avoids:
duplicate pair initiation (v1 §7 existed only to patch this), interleaved Slack
posting from one bot token, and non-deterministic `AgentState` mutation. Dropping
it was correct. It should have been written down.

### 10.2 Reactive-priority selection

Two tiers, sequential, one agent per turn:

1. **Reactive** — agents that owe a thread reply, oldest-waiting first, excluding
   `_last_llm_caller` (so an A→B→A baton alternates without a wasted skip tick)
   and excluding grandfathered threads (§8). Bounded by
   `max_consecutive_reactive_turns`.
2. **Proactive** — the existing staleness-weighted random selection, with the
   Phase-5-skip penalty (`weight /= 2^(skips-2)` once `skips >= 3`).

### 10.3 Fairness — the valve default must change

`max_consecutive_reactive_turns = 8` was measured: with two agents in a live
thread and three idle, **24 of 27 selections went to the pair**. That is 8:1, and
v1's stated goal was the opposite ("ensure fair turn distribution across all
agents").

**Requirements.**

1. Default `max_consecutive_reactive_turns = 3`, matching `active_thread_threshold`
   so the two levers stay in proportion. Document the 8:1 → 3:1 change.
2. Add the two eligibility filters v1 specified and the implementation omitted —
   the candidate pool is currently `_agent_within_budget` only:
   - `(now - a.state.last_selected) >= turn_delay_seconds` (per-agent cooldown)
   - agent is not administratively paused, if/when a pause flag lands on `main`
     (`is_paused` exists only on `coPI-podcast` today — do not reference it until
     it does)
3. Once the cooldown is enforced at selection time, remove the global
   `asyncio.sleep(turn_delay_seconds)` from the main loop. Not before: removing it
   first raises the turn rate with no throttle.
4. Emit a per-100-turn ratio of reactive:proactive selections to the run log, so
   starvation is observable rather than inferred.

---

## 11. Configuration

```python
# Cohort interaction gate
cohort_isolation_enabled: bool = False
cohort_default_policy: Literal["open", "isolated"] = "open"

# Scheduler (independent of cohorts)
max_consecutive_reactive_turns: int = 3
```

Membership resync rides the existing `ROSTER_POLL_INTERVAL = 30.0` tick. v1
specified a separate 60 s timer; reusing the roster tick is simpler, already
implemented, and correct — `_last_roster_poll` initialises to `0.0`, so the first
recompute happens before the first turn (verified). Do not add a second timer.

**Membership is live; the settings are not.** `get_settings()` is `@lru_cache`d, so
`cohort_isolation_enabled`, `cohort_default_policy` and
`max_consecutive_reactive_turns` are read once per process. Editing the topology in
the admin UI takes effect within ~30 s with no restart; **changing the flag or the
policy requires restarting `agent-run`.** Verified by execution: setting the env var
in a live process leaves `get_settings()` returning the cached value. Say so in the
admin banner, or an operator will flip the flag and conclude the feature is broken.

Retired settings: `concurrent_turns` (never existed), `COHORT_RESYNC_INTERVAL`
(subsumed by the roster tick).

---

## 12. Admin interface

Routes as shipped, all under `get_admin_user`:

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/cohorts` | list + inline create form |
| POST | `/admin/cohorts/create` | name validated `^[a-z0-9-]{1,48}$`, uniqueness checked |
| GET | `/admin/cohorts/{id}` | members, add-agent picker, agent→cohorts map, **audit log** |
| POST | `/admin/cohorts/{id}/delete` | **must refuse while members exist** |
| POST | `/admin/cohorts/{id}/add-agent` | validates agent exists in `AgentRegistry`; rejects duplicates |
| POST | `/admin/cohorts/{id}/remove-agent` | — |

The shipped implementation already does name validation, the unknown-agent guard,
and the duplicate guard correctly, and the agent→cohorts map exists. Two gaps and
three additions:

**Gaps to close.**

1. **Delete guard.** The route currently deletes unconditionally and cascades;
   only a JS `confirm()` mentions the member count. Enforce server-side: if
   `memberships` is non-empty, redirect back with
   `?error=Remove+all+members+first`. Disable the button in the template too — but
   the server check is the one that counts.
2. **Audit log.** Render `cohort_audit_events` for the cohort, newest first, on
   the detail page. Write an event from every mutating route (§4.1).

**Additions required by §5.**

3. Banner on `/admin/cohorts` when `cohort_isolation_enabled` is true, stating the
   active `cohort_default_policy` and the number of agents currently isolated.
4. Red banner when the §5.3 preflight has forced isolation off, with the reason.
5. A note that `collab_private` channels bypass the gate (§7).

---

## 13. Observability

Nothing in the shipped implementation makes the gate's effect visible, so an
operator cannot tell whether it is working, over-filtering, or silently disabled.
Minimum:

- Per resync, at INFO: cohort count, membership count, number of agents with an
  active gate, number isolated.
- Per turn, at DEBUG: entries filtered by the gate, per agent.
- Per run, in the admin UI: total gate-filtered entries, total tags stripped,
  reactive:proactive selection ratio, grandfathered-thread count.
- Every grandfathering event and every preflight override at INFO/ERROR (§5.3, §8).

### 13.1 Run-topology provenance

Cohort memberships are global; conversations are scoped to a `simulation_run_id`
(`_poll_inbound_from_db` filters on it). So an admin can reshape the topology
mid-run and nothing records that it happened, which makes a completed run's output
un-attributable to the configuration that produced it. For a research system that
is the more expensive failure than any of the bugs in §5–§9.

**Requirement.** At run start, and on every membership change during a run, write a
`cohort_audit_events` row carrying the full topology snapshot (cohort names →
member `agent_id`s) plus the active `cohort_isolation_enabled` /
`cohort_default_policy` values, tagged with `simulation_run_id`. One row per
change, not per tick. This is the record that lets someone later ask "which cohort
configuration produced these proposals?" and get an answer.

---

## 14. Migration and deployment — the collision

This section is the reason v2 exists at all. It was verified by running real
migrations against clones of the live database.

### 14.1 The collision

`0019_add_cohorts.py` (`revision = "0019"`, `down_revision = "0018"`) collides
with `main`'s `0019_agent_message_content.py` (identical revision and
down_revision). Git merges the two branches **cleanly** — zero conflicts, and the
full test suite passes (398 tests) — so nothing in code review or CI flags it.
Alembic emits only `UserWarning: Revision 0019 is present more than once`.

The live `copi` database is at revision **`0018`** — the exact fork point. Both
`0019`s claim to be the next migration.

### 14.2 What happens, by command

| Command | Result | DB after |
|---|---|---|
| `alembic upgrade head` (the command in `README.md:40`) | `FAILED: Multiple head revisions are present` | unchanged — fails closed |
| `alembic upgrade heads` (what the error message suggests) | `FAILED: Requested revision 0021 overlaps with other requested revisions 0019` | unchanged — fails closed |
| **`alembic upgrade 0021`** (the natural next attempt) | **Succeeds, exit 0** | stamped `0021`, **one of the two `0019` migrations silently never ran** |
| `alembic current` afterwards | prints `0021 (head)` | reports healthy |
| `alembic heads` | prints `0019` and `0021 (head)` — two heads | — |
| `alembic downgrade 0018` (incident rollback) | **Crashes**: `UndefinedObjectError: index "ix_cohort_memberships_agent_id" does not exist` | unchanged — Postgres transactional DDL rolls the whole chain back |
| `alembic upgrade 0022` (any future migration) | `FAILED: Multiple head revisions are present` | unchanged, forever |

### 14.3 The damage

Alembic loads version files in `sorted(filename)` order and the **last** duplicate
wins the revision map. With the two files as they exist,
`0019_agent_message_content.py` sorts after `0019_add_cohorts.py`, so:

- `alembic upgrade 0021` applies `agent_message_content`, `pi_dm_messages`, and the
  inbox indexes, and **never creates `cohorts` / `cohort_memberships`.** The
  database reports `0021 (head)`. The cohort feature is silently absent; the admin
  UI 500s on first use.
- Rename either file — or add a third `0019_*` — and the winner flips. Verified by
  execution: with `add_cohorts` sorting last, the identical command on the
  identical starting database produced `cohorts` present and
  **`agent_messages.content` missing**, still stamped `0021`. Runtime result:
  `asyncpg.exceptions.UndefinedColumnError: column agent_messages.content does not
  exist` — the entire DB-primary conversation store is broken while Alembic
  reports the database fully migrated.

So the outcome is deterministic given the filenames, but which schema change is
silently dropped depends on filenames alone, and neither the migration output nor
`alembic current` reveals that anything was skipped.

**And the box is then wedged permanently.** Two heads means every future
`alembic upgrade head` fails; `downgrade` crashes in the wrong `0019`'s
`downgrade()` and rolls back. There is no forward and no backward path without
hand-editing revision ids. `alembic current` says `0021 (head)` throughout.

Blast radius on a production database with live data: no destructive DDL runs
(every failure path rolled back atomically — `pi_dm_messages` and its rows
survived the failed downgrade). The damage is a **silently incomplete schema plus
a permanently unmigratable database**, not data loss. That is still an incident,
and the "reports healthy" property makes it one that gets discovered late.

### 14.4 Required fixes

1. **Renumber** to `0022_add_cohorts.py` / `revision = "0022"` /
   `down_revision = "0021"`.
2. **Preflight gate in CI** — this is the durable fix; renumbering one file only
   fixes one file. Add to `scripts/ci.sh`:

   ```bash
   # Exactly one Alembic head, and no duplicate revision ids.
   heads=$(alembic heads 2>/dev/null | grep -c .)
   if [ "$heads" -ne 1 ]; then
     echo "FAIL: expected 1 alembic head, found $heads"; alembic heads; exit 1
   fi
   dupes=$(grep -h '^revision' alembic/versions/*.py | sort | uniq -d)
   if [ -n "$dupes" ]; then
     echo "FAIL: duplicate alembic revision ids:"; echo "$dupes"; exit 1
   fi
   ```

   `alembic check` is **not** sufficient — it reported `Target database is not up
   to date`, which is the wrong diagnosis, and it needs a live database.
3. **Idempotent downgrades.** Both `0019`s crash on a partially applied schema.
   Use `op.drop_index(..., if_exists=True)` / `DROP TABLE IF EXISTS` so a rollback
   cannot wedge on an object that was never created.
4. **Deploy runbook.** Before any migration: record `alembic current` and
   `alembic heads`; abort if `heads` returns more than one line. After: assert the
   expected tables/columns exist, not just that `alembic current` advanced. Update
   `README.md:40`, which currently documents the failing command.

### 14.5 Separately: this box is three migrations behind its code

The live `copi` database is at `0018`. `main`'s code expects `0021`.
`agent_messages` is missing `content`, `slack_ts`, `slack_channel_id`, and
`slack_thread_ts` — every column `0019_agent_message_content` adds. The
DB-primary conversation store on `main` cannot work against this schema. Run
`alembic upgrade head` on the **clean** `main` tree (single head, succeeds) before
any cohort work lands, and verify the four columns exist afterwards.

---

## 14.6 Measured scale headroom

Gate recompute against a real Postgres with 20 agents (the recompute runs every 30 s):

| cohorts | memberships | p50 | p99 |
|---|---|---|---|
| 4 | 20 | 2.3 ms | 8.3 ms |
| 20 | 100 | 3.1 ms | 11.9 ms |
| 100 | 500 | 4.9 ms | 99.8 ms |
| 400 | 2000 | 11.0 ms | 109.3 ms |

Free at any plausible roster size, and still affordable three orders of magnitude out.
No indexing or caching work is warranted; do not add a cache and reintroduce staleness
to solve a 3 ms problem.

**Engine-level figure.** The table above times the gate computation. The full
`_recompute_allowed_sender_ids()` — session checkout, three queries, `compute_gates`,
then `_apply_cohort_gate_to_state` over every agent (grandfathering plus
`interesting_posts` pruning) — measures **p50 61 ms** at 20 agents / 100 cohorts / 500
memberships, in-container against a networked Postgres. That is the number that matters
operationally, and it is the one pinned by
`test_gate_is_correct_and_affordable_at_20_agents` with a 500 ms bound (~8x headroom).
The bound is deliberately loose: it exists to catch an order-of-magnitude regression,
not to police jitter on a shared test box. That test also asserts the gates are
non-empty, self-inclusive and symmetric, because a recompute that returned empty gates
instantly would satisfy a timing bound alone — and an empty gate silences an agent.

---

## 15. Test plan

The shipped 20 tests pass and cover the filter, the recompute, `_owes_reply`, and
the reactive tier honestly. They are also the mechanism by which the inverted
semantics became load-bearing: `test_enabled_computes_cohort_mates` asserts
`allowed_sender_ids == set()` for an uncohorted agent, locking in the behaviour
v1 forbade. Rewrite that assertion against §5.1.

Relocate to `tests/unit/test_cohort_isolation.py` — `main` moved to a four-tier
layout (`tests/unit`, `characterization`, `contract`, `integration`) in PR #17 and
the branch predates it, so the tests currently sit outside the CI gate.

Required new coverage, one test per normative claim:

**Gate semantics (§5)**
- `policy="open"` + isolation on + zero cohorts → every agent's gate is `None`; a
  post from any agent is visible.
- `policy="open"` + partial cohorting → uncohorted agent sees the cohorted roster.
- `policy="isolated"` + uncohorted agent → sees only humans.
- `policy="isolated"` + zero cohorts → preflight forces isolation off, logs ERROR.
- No `session_factory` + isolation on → forced off, logs ERROR.
- Human sender always passes, under both policies.
- Unknown sender passes, logs WARNING.

**Enforcement (§6)**
- Each gated method filters; each intentionally ungated method is asserted
  ungated, so a future change is deliberate.
- New-method guard: every public `get_*`/`has_*` on `MessageLog` carries a
  classification comment.
- Stale `interesting_posts` are pruned on resync.

**Private channels (§7)**
- Two agents in different cohorts, isolation on, `collab_private` channel → both
  the PI message *and* the partner's message are visible.

**Grandfathered threads (§8)**
- Membership removal marks an open thread grandfathered; Phase 4 still replies.
- A grandfathered thread does **not** win the reactive tier.
- `has_new_reply_from_other` respects the gate for non-open threads.

**Tag hygiene (§9)**
- Phase 4 and `_post_message` strip cross-cohort tags.
- Unknown bot name survives; WARNING logged.
- Stripped text reads cleanly (no bare dangling name).

**Scheduler (§10)**
- With `max_consecutive_reactive_turns = 3`, a live pair takes ≤ 3 of every 4
  turns (the current default gives 24/27).
- An agent within `turn_delay_seconds` of its last turn is not selected.

**DB-primary paths (§6.2, §6.3, §8)**
- A bot-authored row with `agent_id = NULL` ingested via `_poll_inbound_from_db`
  does **not** pass the gate (keys on `is_bot`).
- `_poll_inbound_from_db` ingests every row for the run regardless of any agent's
  cohort — assert the shared log is complete while a gated agent's read is filtered.
- A resumed run reconstructs a cross-cohort thread, and the first resync marks it
  grandfathered; no turn is taken with `allowed_sender_ids is None` while isolation
  is enabled.
- PI-facing reads (`agent_page`, `admin`) return unfiltered history for a
  cross-cohort thread while isolation is on.
- PI DM handling is unaffected by any cohort configuration.
- Private-channel cursor rewind + §7 exemption together: the rewound agent actually
  sees its partner's private-channel messages.
- Whole suite green with `slack_enabled` true **and** false (§9.1).

**Real API (§15.1)** — opt-in, `real_llm` marker, skipped without a key
- Two real Phase 2 calls with the same profile and log, differing only in the gate:
  ungated the model **selects** the excluded partner's post; gated it cannot. Assert
  both legs — the ungated control is what makes the gated leg mean anything.
- The outbound strip removes a cross-cohort mention from real model prose.
- A real scan response parses, and can only name surviving post ids.

**Migration (§14)**
- `alembic heads` returns exactly one line.
- No duplicate revision ids across `alembic/versions/`.
- Round-trip `upgrade` → `downgrade` → `upgrade` on a scratch database leaves the
  expected schema, and each downgrade is idempotent.

---

### 15.1 Real-API tests — the vacuity trap

`tests/integration/test_cohort_real_llm.py` spends real tokens and is skipped unless
`ANTHROPIC_API_KEY` is set. It exists for one claim a fake cannot check: that the gate
changes the model's **decision**, not merely its prompt.

The first version of it passed while proving nothing, and only capturing the raw model
output revealed why. With no profile loaded on the agent, the scan selected **no posts
in either leg** — so the gated assertion ("the model did not select the excluded post")
held for the wrong reason, and the ungated "control" demonstrated no difference at all.

The fix is structural, and any future test here needs the same shape: give the agent a
profile that makes the *excluded* post directly relevant and the *surviving* post
clearly irrelevant, then assert on **both** legs. Measured:

| | posts in prompt | model selected |
|---|---|---|
| gate off | both | `["1000.0002"]` — with reasoning citing the match |
| gate on | one | `[]` — considered only the survivor, rejected as out of scope |

A real-API test whose passing condition is an *absence* (no leak, no mention, no
selection) is vacuous unless a paired positive leg shows the thing would otherwise be
present. Do not add one without it.

Model IDs are read from settings (`llm_agent_model_sonnet`), not hardcoded, so the
suite follows the configured model. Both configured ids — `claude-opus-4-6` and
`claude-sonnet-4-6` — were verified live; pricing at the time of writing was
$5/$25 and $3/$15 per MTok respectively.

---

### 15.2 Coverage as shipped

The plan that closed the remaining gaps is `.notes/cohort-thorough-test-plan.md`. Its
organising principle is the failure mode above, generalised into two rules that every
new cohort test must follow:

- **Rule A** — a test whose passing condition is an absence needs a positive control in
  the same test. If the permitted leg does not fire, the result is *inconclusive*, not a
  pass. Assertion messages say `INCONCLUSIVE` where that distinction matters.
- **Rule B** — let the system produce the state you assert on. Do not construct a row,
  flag or field by hand and then assert the reader honours it.

Rule B exists because the original §7 test wrote the `AgentMessage` row with
`visibility` already set and so never exercised the writer — which is how
`_post_message` shipped without stamping the field at all, leaving this section's
exemption dead code and letting private content into the public memory segment. Rule A
exists because the §5.2 symmetry test skipped the `None`-vs-set case, which is how the
`open`-policy asymmetry shipped: an uncohorted agent could act on anyone but appeared in
nobody's mate set, so it opened threads that were never answered.

Both defects were found by a real multi-turn run, not by the suite. Both are now
mutants in `scripts/mutate_cohorts.sh`, which applies nine one-line edits to
`src/services/cohorts.py`, `src/agent/message_log.py` and `src/agent/simulation.py` and
requires each to make at least one test fail. A surviving mutant means the behaviour is
untested regardless of what the test names say. Run it after adding a cohort test; it is
offline and needs no API key.

Sections whose normative claims had been written down but never exercised, now covered:

| § | Claim | Where |
|---|---|---|
| 5.1 | the whole decision table, incl. empty-string agent_id and unknown visibility | `test_decision_table_row` |
| 5.3 | all four preflight inputs, 12 combinations | `test_preflight_matrix` |
| 5.4 | `open` never emits an empty gate, over six topology shapes | `test_open_policy_never_emits_an_empty_gate` |
| 6.3 | forward-only cursor: no backlog replay, and the filter is per-read not stamped at ingest | `test_filtering_is_forward_only`, `test_a_rewound_cursor_does_replay_and_the_gate_still_applies` |
| 6.3.1 | the matrix save commits exactly once, after the diff loop | `test_matrix_save_is_one_transaction` |
| 7 | the exemption through all three writers, plus every channel class stamped | `test_private_exemption_holds_for_every_write_path`, `test_every_outbound_channel_class_is_stamped` |
| 8 | grandfathered thread loses priority **and** still concludes | `test_grandfathered_thread_concludes_but_loses_priority` |
| 9 | 14 mention surroundings; indentation never reflowed | `test_strip_cases`, `test_strip_indentation_is_preserved` |
| 10.3 | the valve at 20 agents over 200 picks | `test_valve_holds_over_sustained_load` |
| 11 | membership is live, settings are cached | `test_membership_is_live_but_settings_are_cached` |
| 13.1 | the snapshot is written by `start()`, before the first turn, gate on **or** off | `test_start_computes_the_gate_and_records_a_snapshot`, `..._even_when_the_gate_is_off` |
| 14 | the CI gate itself, ordered before pytest | `test_ci_script_gates_on_alembic_before_running_tests` |
| — | 20-agent scale, mid-run activate/deactivate | `test_gate_is_correct_and_affordable_at_20_agents`, `test_{de,}activating_an_agent_mid_run_*` |

**Measured, §10.3.** With `valve=3`, 20 agents, and two locked in a perpetual exchange,
the pair takes **150 of 200** selections and the valve forces **50** proactive picks — a
clean 3:1. With the valve effectively disabled the pair takes 200/200, which is the
check that the test has teeth. The lower bound (`pair >= 100`) is the control: a
scheduler with no reactive tier would give the pair ~2/20 of the turns and would satisfy
an upper bound on its own.

**Measured, §14.** The `0022` round trip was run against a throwaway database:
`0022 → 0021` drops all three cohort tables, `→ 0022` re-applies clean. Then, with the
tables dropped by hand but the stamp left at `0022` — a partial upgrade — the downgrade
still succeeds and re-upgrades cleanly. That is precisely what the `if_exists=True`
guards buy, and it is the state a failed deploy leaves behind. `scripts/ci.sh` will run
the round trip when `CI_MIGRATION_DB` is set; it is off by default so the gate stays
offline.

**Emergent behaviour, real API.** `tests/integration/test_cohort_scenarios.py` drives
real multi-turn runs and asserts on who ends up conversing with whom — the only claims
in this document that a deterministic test cannot settle. Three things make it
falsifiable, and all three come from a run that proved nothing:

1. Every lab profile is complementary to every other, so any pair is a plausible
   collaboration and the gate is the only thing that can prevent one. The earlier
   version made the cohorts mutually irrelevant, so the gate-OFF baseline also produced
   zero cross-cohort threads.
2. The roster is trimmed to the agents under test. Four agents over a dozen turns is not
   enough for a *specific* pair to form a thread.
3. Messages the harness posts itself are recorded and excluded from every pair
   measurement. Counting them would make "these two conversed" true by construction.

And a fourth, found by executing the plan: **the scenario workspace is collapsed to a
single `#general` channel.** Phase 1 joins channels by keyword-matching the profile and
Phase 5 posts into whichever subscribed channel the model names, so across the real
seven-channel workspace two agents never landed in the same room often enough to open a
thread — measured, with su in three channels and cravatt in all seven, posting into
`#chemical-biology` and `#drug-repurposing` where su does not read. Every outcome claim
came back `INCONCLUSIVE` and *every thread had exactly one participant*. This is a
general hazard for any scenario test here: agents that cannot see each other produce the
same observation as a gate that works perfectly. Only the positive control tells the two
apart, and only per-thread diagnostics say which one you are looking at.

`test_harness_produces_conversation_at_all` is the module's positive control: if a
permissive single-cohort run produces no conversation, every absence assertion built on
the harness is worthless, and that test is what tells you so.

**Out of scope by instruction: Slack mirroring under an active gate.** Also not
currently possible here — no agent carries a bot token and no `SLACK_*` value is set.
Every engine test runs `slack_enabled=False` with `NullTransport`, which is the
configuration where the DB is the sole conversation store and the gate's correctness
rests entirely on read-side filtering (§9.1).

---

## 16. Rollout

1. Rename the graph "cohort" concept to "run window" (§1). Comment-only.
2. Bring the live database to `0021` on the clean `main` tree, verify the four
   `agent_messages` columns (§14.5).
3. Land the CI preflight gate (§14.4) — **before** the feature, so it can catch it.
4. Land the scheduler changes alone: valve default 3, cooldown eligibility,
   ratio logging, `asyncio.sleep` removal. Independently reviewable, no new
   tables, immediately useful.
5. Land the cohort gate: migration `0022`, models, gated read paths (`is_bot`
   keying, `_entry_allowed`), the §6.2 DB-path classifications, private-channel
   exemption, grandfathering, tag hygiene, topology provenance, admin UI with audit
   log and delete guard. Flag off.
6. Verify with `slack_enabled` **false** as well as true (§9.1), and on a
   *resumed* run, not just a fresh one — the rebuild path (§8) is only exercised on
   resume and is where the gate is blind.
7. Enable on one run with `policy="open"` and two cohorts covering the whole
   roster. Watch the filter/strip/ratio counters and the grandfathered count for
   one full run.
8. Only then consider `policy="isolated"`, and only with the preflight and the
   isolated-agent banner in place.

Do not enable the flag on a roster where any agent is uncohorted until step 7.

---

## Appendix A — audit evidence

All figures below were produced by execution on 2026-07-30 against
`origin/cohort-agent-isolation` @ `b00b0e6` and `main` @ `b7edcbc`, with
migrations run against throwaway clones of the live `copi` database. The live
database was not modified (verified at `0018` before and after).

| Check | Result |
|---|---|
| Branch's own suite | 282 passed — commit message's claim reproduces exactly |
| `tests/test_cohort_isolation.py` | 20 passed |
| Merge into `main` | **clean, 0 conflicts** |
| `tests/unit` on `main` | 378 passed |
| `tests/unit` + cohort tests on merged tree | 398 passed — no regressions |
| Probes asserting v1's promises | **11 of 11 failed** |
| Probes asserting the defects | **9 of 9 passed** (both on the branch and merged) |
| `alembic heads` (merged) | `0019`, `0021` — two heads + duplicate-revision warning |
| `alembic upgrade head` at `0018` | FAILED, DB unchanged |
| `alembic upgrade heads` at `0018` | FAILED, DB unchanged |
| `alembic upgrade 0021` at `0018`, files as-shipped | **exit 0**, stamped `0021`, `cohorts` **never created** |
| same command, `add_cohorts` sorted last | **exit 0**, stamped `0021`, `agent_messages.content` **never created** |
| runtime read of `content` in that state | `UndefinedColumnError` while `alembic current` = `0021 (head)` |
| `alembic downgrade 0018` on the wedged box | crash in the wrong `0019.downgrade()`; whole chain rolled back atomically |
| `alembic check` as a guard | `Target database is not up to date` — wrong diagnosis, needs a live DB |
| Live `copi` schema | at `0018`; `content`, `slack_ts`, `slack_channel_id`, `slack_thread_ts` all MISSING |

DB-primary interface observations (read from `main` @ `b7edcbc`):

| Observation | Location |
|---|---|
| `MessageLog` is still in-memory append-only; DB mirrored via `_persist_cb`; all 8 engine feed sites go through `message_log.append` — the read-boundary gate remains the correct choke point | `message_log.py:1`, `:89-104` |
| `agent_messages.agent_id` is **nullable**; ingestion maps it straight to `sender_agent_id` | `0019_agent_message_content.py:49`, `agent_activity.py:82`, `simulation.py:2253` |
| `_poll_inbound_from_db` ingests all rows for the run, advancing `_pi_inbox_cursor` — filtering here would drop rows for every agent | `simulation.py:2209-2264` |
| PI DMs bypass `MessageLog` entirely, going to `PIHandler.handle_dm` | `simulation.py:2452-2498` |
| Setup order is rebuild → rebuild → rebuild, *then* loop → first `_sync_roster_from_db` | `simulation.py:396-398`, `:424`, `:446` |
| `_rebuild_agent_state` walks `message_log._entries` directly, bypassing gated accessors | `simulation.py:3302+` |
| `last_seen_cursor` advances to `time.time()` per turn; rebuild sets it to the latest message time | `simulation.py:648`, `:3479` |
| Private-channel cursor rewind exists specifically to re-scan handovers | `simulation.py:1494`, `:1554-1559` |
| `LogEntry.visibility` is carried in memory and persisted as `AgentMessage.visibility` | `message_log.py` dataclass, `agent_activity.py` |
| `NullTransport.is_connected` is `False`, so Slack pollers no-op and DB inbox is the only inbound path | `transport.py:67-99` |
| PI/admin web views read `AgentMessage` directly for display | `routers/agent_page.py`, `routers/admin.py` |

Hypotheses tested and **refuted** — recorded so they are not re-litigated:

- *"The merge will conflict heavily."* No. `main` rewrote 862 lines of
  `simulation.py` and 111 of `message_log.py` since the fork and the merge is
  still clean with all tests green. The damage is at the Alembic layer only.
- *"The gate is open for the first 30 s of a run."* No. `_last_roster_poll`
  starts at `0.0` and `_sync_roster_from_db()` runs before the first
  `_select_agent()`.
- *"Membership is re-queried every loop iteration."* No. The 30 s
  `ROSTER_POLL_INTERVAL` early-return precedes the recompute.
- *"Some gated call sites forget to pass the gate."* No. 3 of 3 pass it.
- *"Alembic file order is filesystem-dependent, so the outcome is
  non-deterministic per host."* No. `ScriptDirectory._list_py_dir` uses
  `sorted(files)`; the last duplicate wins, deterministically. The hazard is that
  the winner depends on filenames, and nothing in the output reveals the loser was
  skipped.
- *"A failed migration could leave a half-applied schema."* No. Postgres
  transactional DDL plus a single `begin_transaction()` around the whole chain
  meant every failure path rolled back completely, data intact.

## Appendix B — retired v1 decisions

Kept so the reasoning is not rediscovered:

- **Min-heap selection** (v1 §6) — retired. The problem was thread latency, not
  starvation; priority tiers address it directly. Revisit only if measurement
  shows proactive starvation under §10.3's 3:1 valve.
- **Global semaphore / `concurrent_turns`** (v1 §6) — retired. Brings duplicate
  pair initiation, single-token posting contention, and non-deterministic state
  mutation. v1 §7 existed solely to patch the first of those.
- **Concurrent pair-initiation guard** (v1 §7) — retired with concurrency.
- **`Agent.cohort_ids` + `can_interact()`** (v1 §Agent Changes) — retired in
  favour of a precomputed `allowed_sender_ids` set, which is cheaper and has one
  evaluation point. The *semantics* v1 attached to `can_interact` are restored in
  §5.
- **Separate 60 s `COHORT_RESYNC_INTERVAL`** — retired; the 30 s roster tick
  already covers it.
- **O(n²) cost framing** — retired as factually wrong under the sequential
  scheduler; replaced by §3.
