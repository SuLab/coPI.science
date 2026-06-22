# Cohort System Specification

## Overview

A cohort is a named group of agents whose members are permitted to interact with each other during simulation. The purpose is purely practical: prevent agents from spending LLM turns scanning, activating threads with, or tagging agents they will never productively engage. Cohorts are orthogonal to Slack channels — channel subscriptions remain unchanged; cohort membership only gates whether one agent will *act on* another agent's activity.

Agents may belong to any number of cohorts. Cohort assignments are admin-managed and can change while a simulation is running. Interaction limits (thread count, proposal caps, budgets) remain per-agent and are shared across all cohorts an agent belongs to.

---

## Goals

- Skip Phase 2 scan evaluation of posts from non-cohort agents (save Sonnet calls)
- Skip Phase 3 thread activation from non-cohort agents (save CPU + state bloat)
- Skip Phase 5 tagging or replying to non-cohort agents (save Opus calls)
- Run N turns concurrently via a global semaphore for predictable API cost at any agent list size
- Ensure fair turn distribution across all agents via min-heap selection
- Allow membership to change mid-run without requiring a restart

---

## Data Model

### New Table: `cohorts`

```sql
CREATE TABLE cohorts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT now()
);
```

- `name`: short slug-style identifier (e.g. `"pilot-wave-1"`, `"structural-cohort"`). Unique, immutable after creation.
- `description`: optional free-text note for admin reference.
- `created_by`: FK to the admin user who created it; nullable (SET NULL on user delete).

### New Table: `cohort_memberships`

```sql
CREATE TABLE cohort_memberships (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cohort_id  UUID NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    added_by   UUID REFERENCES users(id) ON DELETE SET NULL,
    added_at   TIMESTAMP WITH TIME ZONE DEFAULT now(),
    UNIQUE (cohort_id, agent_id)
);
```

- `agent_id`: matches `AgentRegistry.agent_id` (string, e.g. `"su"`, `"wiseman"`). No FK enforced — agent records may not exist at table creation time; the application validates at join time.
- Composite unique constraint prevents duplicate membership.
- Cascade delete: removing a cohort removes all its memberships.

### Migration

File: `alembic/versions/0023_add_cohorts.py`

```python
def upgrade():
    op.create_table("cohorts", ...)
    op.create_table("cohort_memberships", ...)
    op.create_index("ix_cohort_memberships_cohort_id", "cohort_memberships", ["cohort_id"])
    op.create_index("ix_cohort_memberships_agent_id", "cohort_memberships", ["agent_id"])

def downgrade():
    op.drop_table("cohort_memberships")
    op.drop_table("cohorts")
```

### SQLAlchemy Models

`src/models/cohort.py`:

```python
class Cohort(Base):
    __tablename__ = "cohorts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    memberships: Mapped[list["CohortMembership"]] = relationship(back_populates="cohort", cascade="all, delete-orphan")

class CohortMembership(Base):
    __tablename__ = "cohort_memberships"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    cohort_id: Mapped[UUID] = mapped_column(ForeignKey("cohorts.id", ondelete="CASCADE"))
    agent_id: Mapped[str]
    added_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    added_at: Mapped[datetime] = mapped_column(default=func.now())
    cohort: Mapped["Cohort"] = relationship(back_populates="memberships")
```

Export from `src/models/__init__.py` alongside existing models.

---

## Agent Changes

### `src/agent/agent.py`

Add one field to `Agent.__init__`:

```python
self.cohort_ids: set[str] = set()   # populated by SimulationEngine at startup and on resync
```

Add one helper method:

```python
def can_interact(self, other: "Agent") -> bool:
    """True if the two agents share at least one cohort (or if either has no cohort assignments)."""
    if not self.cohort_ids or not other.cohort_ids:
        return True   # uncohorted agents interact with everyone — backward-compatible default
    return bool(self.cohort_ids & other.cohort_ids)
```

The fallback `return True` when either agent has no cohorts assigned preserves all-vs-all behaviour for agents not yet assigned to any cohort, preventing accidental silencing.

---

## Simulation Engine Changes

### `src/agent/main.py`

#### 1. Cohort Loading at Startup

After agents are loaded and before the main loop, query cohort memberships:

```python
async def _load_cohort_memberships(self):
    async with self._session_factory() as db:
        rows = await db.execute(
            select(CohortMembership.agent_id, CohortMembership.cohort_id)
        )
        # Clear and rebuild
        for agent in self.agents.values():
            agent.cohort_ids = set()
        for agent_id, cohort_id in rows:
            if agent_id in self.agents:
                self.agents[agent_id].cohort_ids.add(cohort_id)

    self._last_cohort_sync = time.time()
    logger.info("Cohort memberships loaded for %d agents", sum(1 for a in self.agents.values() if a.cohort_ids))
```

No index structure is needed — the interaction gate operates purely via `agent.cohort_ids` set intersection at the point of interaction. Turn dispatch is global and cohort-unaware (see Section 6).

#### 2. Dynamic Membership Resync

Every 60 seconds (checked at the top of each main-loop round), re-run `_load_cohort_memberships()` and rebuild `_cohort_members`. This is a full replace, not a diff — simple and correct.

```python
COHORT_RESYNC_INTERVAL = 60  # seconds

if time.time() - self._last_cohort_sync >= COHORT_RESYNC_INTERVAL:
    await _load_cohort_memberships()
    _rebuild_cohort_index()
```

Resync only updates `agent.cohort_ids` and `_cohort_members`. It does not touch `AgentState` or close any active threads — existing open threads between agents who have since been removed from a shared cohort are allowed to conclude naturally.

#### 3. Interaction Gate — Phase 2

In `_phase2_scan_filter()`, filter incoming posts before building the LLM prompt:

```python
new_posts = [
    p for p in new_posts
    if self._sender_can_interact(agent, p.sender_agent_id)
]
```

Where:

```python
def _sender_can_interact(self, agent: Agent, sender_id: str | None) -> bool:
    if sender_id is None:
        return True   # PI/human message — always show
    sender = self.agents.get(sender_id)
    if sender is None:
        return True   # unknown sender — don't filter
    return agent.can_interact(sender)
```

#### 4. Interaction Gate — Phase 3

In `_phase3_activate_threads()`, tag-based and reply-based activation both check:

```python
sender = self.agents.get(entry.sender_agent_id)
if sender and not agent.can_interact(sender):
    continue   # skip activation — not a cohort-mate
```

This applies before any other checks (thread cap, thread participation rules, etc.) to fail fast.

#### 5. Interaction Gate — Phase 5

In `_phase5_new_post()`, when filtering `available_posts`:

```python
sender = self.agents.get(post.sender_agent_id)
if sender and not agent.can_interact(sender):
    agent.state.interesting_posts = [
        p for p in agent.state.interesting_posts if p.post_id != post.post_id
    ]
    continue   # prune stale post — sender is no longer a cohort-mate
```

When the LLM response names a `tagged_agent` for a new top-level post:

```python
if tagged_agent:
    target = self.agents.get(tagged_agent)
    if target and not agent.can_interact(target):
        logger.debug("%s: cohort gate blocked tag of %s in phase5", agent.agent_id, tagged_agent)
        return
```

#### 6. Turn Selection: Min-Heap + Global Semaphore

Replace the current O(n) weighted-random `_select_agent()` with a **min-heap keyed by `last_selected`** and a **global semaphore of width `concurrent_turns`**.

**Why min-heap over weighted random:**
The current weighted-random gives probabilistic fairness but can starve agents at large list sizes, particularly when `phase5_skip_probability` is non-zero (fast no-op turns let an agent re-enter the lottery immediately). A min-heap guarantees the longest-waiting eligible agent always gets the next slot — O(log n) selection, deterministic fairness.

**Selection and dispatch:**

```python
import heapq

def _build_heap(self) -> list[tuple[float, Agent]]:
    now = time.time()
    return [
        (a.state.last_selected, a)
        for a in self.agents.values()
        if not a.is_paused
        and self._agent_within_budget(a)
        and (now - a.state.last_selected) >= settings.turn_delay_seconds
    ]

async def _run_concurrent_turns(self) -> bool:
    heap = self._build_heap()
    if not heap:
        return False

    heapq.heapify(heap)
    n = min(settings.concurrent_turns, len(heap))
    selected = [heapq.heappop(heap)[1] for _ in range(n)]

    results = await asyncio.gather(
        *[self._run_turn(agent) for agent in selected],
        return_exceptions=True,
    )

    did_any_work = False
    for agent, result in zip(selected, results):
        agent.state.last_selected = time.time()
        if isinstance(result, Exception):
            logger.exception("Turn error for %s", agent.agent_id)
        elif result:
            did_any_work = True

    return did_any_work
```

The main loop calls `_run_concurrent_turns()` each iteration and uses `did_any_work` to drive the existing idle-backoff logic unchanged.

**Slack polling** continues once per round, before `_run_concurrent_turns()`, as a single sequential operation.

**`_last_llm_caller` guard:** This guard exists to prevent the same agent from making back-to-back LLM calls in the sequential model. It is superseded by the min-heap + per-agent cooldown (`turn_delay_seconds` eligibility check) and should be removed from the concurrent path. The min-heap naturally pushes a just-selected agent to the bottom of the queue; the cooldown makes them ineligible until the delay has elapsed.

#### 7. Phase 5 Concurrent Initiation Guard

With N turns running concurrently, two agents can independently decide to start a new thread with each other in the same round (both see `has_pending_reply=False` and neither has an active thread with the other yet). Track in-flight pair initiations to prevent duplicate thread creation:

```python
self._initiating_pairs: set[frozenset[str]] = set()
```

In `_phase5_new_post()`, before posting a reply that opens a new thread toward `target_agent_id`:

```python
pair = frozenset([agent.agent_id, target_agent_id])
if pair in self._initiating_pairs:
    logger.debug("%s: concurrent initiation guard blocked duplicate thread with %s", agent.agent_id, target_agent_id)
    return

self._initiating_pairs.add(pair)
try:
    await self._post_message(...)
    # activate thread ...
finally:
    self._initiating_pairs.discard(pair)
```

The pair is removed once the thread is activated (or on failure). Note: Phase 4 back-and-forth replies are safe without this guard — `has_pending_reply` is a logical baton held by only one side at a time, so two agents cannot both have a pending reply to each other simultaneously.

---

## Admin Interface

### Routes

All routes are added to `src/routers/admin.py` under the `/admin/cohorts` prefix, protected by the existing `get_admin_user` dependency.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/cohorts` | List all cohorts with member counts |
| POST | `/admin/cohorts/create` | Create a new cohort |
| GET | `/admin/cohorts/{cohort_id}` | Cohort detail: members, audit log |
| POST | `/admin/cohorts/{cohort_id}/delete` | Delete cohort (cascades memberships) |
| POST | `/admin/cohorts/{cohort_id}/add-agent` | Add an agent to the cohort |
| POST | `/admin/cohorts/{cohort_id}/remove-agent` | Remove an agent from the cohort |

POST routes redirect back to the referring page on success and render an inline error on failure (same pattern as existing admin routes).

### Cohort List Page — `GET /admin/cohorts`

Template: `templates/admin/cohorts.html`

**Header:** "Cohorts" with a "New Cohort" button (opens inline form or modal).

**Create form** (inline, collapsed by default):
- `name` (text input, required) — validated: lowercase, alphanumeric + hyphens only, max 48 chars
- `description` (textarea, optional)
- Submit → `POST /admin/cohorts/create`

**Table: All Cohorts**

| Column | Notes |
|--------|-------|
| Name | Link to detail page |
| Description | Truncated at 80 chars |
| Members | Count of current memberships |
| Created by | Admin user name |
| Created at | Date |
| Actions | Delete button (with confirmation; disabled if cohort has active members) |

If no cohorts exist: empty state with "No cohorts yet. Create one above."

### Cohort Detail Page — `GET /admin/cohorts/{cohort_id}`

Template: `templates/admin/cohort_detail.html`

**Header:** Cohort name + description. Delete button (top right, requires confirmation prompt via `data-confirm` attribute; only shown if member count is 0, otherwise disabled with tooltip "Remove all members first").

**Section: Members**

Table of current members:

| Column | Notes |
|--------|-------|
| Agent ID | e.g. `su`, `wiseman` |
| Bot Name | e.g. `SuBot` |
| PI Name | e.g. `Andrew Su` |
| Agent Status | `active` / `suspended` / `pending` (from AgentRegistry) |
| Added by | Admin user name |
| Added at | Date |
| Actions | "Remove" button → `POST /admin/cohorts/{cohort_id}/remove-agent` with `agent_id` |

**Section: Add Agent**

Dropdown of all agents *not already in this cohort*, populated from AgentRegistry. Only active agents are shown by default; a checkbox toggle shows suspended/pending agents as well.

```
[ Select agent ▼ ]  [ Add to Cohort ]
```

`POST /admin/cohorts/{cohort_id}/add-agent` body: `{ agent_id: "su" }`

If the selected agent already belongs to this cohort, return a 400 with inline error "Agent is already a member."

**Section: Agent Cohort Map (read-only)**

Summary table showing all active agents and which cohorts they currently belong to, for cross-reference:

| Agent | Cohorts |
|-------|---------|
| SuBot | pilot-wave-1, structural |
| WisemanBot | pilot-wave-1 |
| LotzBot | *(none)* |

This section is static (no editing — use individual cohort pages to manage membership).

### Navigation

Add "Cohorts" to the existing admin sidebar nav alongside Agents, Users, Activity, etc.

---

## Configuration

### New settings (`src/config.py`)

```python
concurrent_turns: int = 3   # max simultaneous agent turns; overridden by active_thread_threshold at runtime
```

At engine startup, `concurrent_turns` is clamped to `max(concurrent_turns, active_thread_threshold)`. This keeps the two levers in proportion: if an admin raises the thread threshold to allow more simultaneous conversations, the concurrent turn capacity rises with it automatically. The `concurrent_turns` setting therefore acts as a floor, not a ceiling.

The cohort resync interval is hardcoded as `COHORT_RESYNC_INTERVAL = 60` seconds in the engine. It can be promoted to `Settings` if operational tuning is needed.

### `turn_delay_seconds` — Behavior Change

**Current behavior (to be removed):** `simulation.py:360-361` applies `asyncio.sleep(turn_delay_seconds)` at the end of every productive main-loop iteration. This is a **global pause** — no Slack polling, no other agents, nothing runs during the sleep. It is 0.0 by default and has no per-agent targeting.

**New behavior:** `turn_delay_seconds` becomes a **per-agent cooldown** enforced at selection time inside `_build_heap()`:

```python
and (now - a.state.last_selected) >= settings.turn_delay_seconds
```

An agent that just completed a turn is ineligible until the cooldown has elapsed. All other agents are unaffected. The `asyncio.sleep(settings.turn_delay_seconds)` call in `simulation.py` is removed.

This preserves the original intent (throttle individual agent tempo) while composing correctly with concurrent dispatch — N slots can stay busy while a recently-active agent sits out its cooldown.

---

## Backward Compatibility

- Agents with no cohort memberships are grouped into `"__uncohorted__"` and continue to interact with all other uncohorted agents. This means a simulation with zero cohorts defined behaves identically to the current all-vs-all system.
- `Agent.can_interact()` returns `True` when either agent has an empty `cohort_ids` set, so partially-cohorted simulations (some agents assigned, some not) do not silently break.
- No existing tables, models, or routes are modified.

---

## Out of Scope

- Agent-visible cohort concept: agents do not know which cohort a conversation was initiated from; threads are indistinguishable.
- PI-managed cohorts: only admins create and delete cohorts. PIs cannot request cohort changes.
- Per-cohort budgets or limits: all limits remain per-agent and are shared across cohorts.
- Cohort-scoped message history or separate Slack workspaces per cohort.
- Time-bounded cohort memberships (automatic expiry).
