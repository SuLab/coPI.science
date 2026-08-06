# Design — cohort-scoped conversations feed, threaded display, and topology payload restructure

**Status:** DESIGN, not implemented.
**Date:** 2026-08-05
**Target branch:** `blackbird`.
**Companions:** `specs/cohort-system-v2.md` (gate semantics, §5/§6/§12),
`specs/privacy-and-channel-visibility.md` (visibility classes),
`specs/local-db-conversations.md` (DB as the primary conversation store).

---

## 1. Problem

Three defects, two of them in the same page.

### 1.1 The conversations feed is not cohort-scoped

`GET /agent/{agent_id}/conversations` (`src/routers/agent_page.py:702`) selects
messages with **channel name as the only content filter**
(`agent_page.py:755-772`):

```python
select(AgentMessage).where(
    AgentMessage.simulation_run_id == run_id,
    AgentMessage.channel_name.in_(channels),
)
```

There is no `agent_id` filter, no `visibility` filter, and no cohort gate. The
channel set (`agent_page.py:747-753`) is "channels this agent authored in" unioned
with `{"general"}` unconditionally, so **every PI sees every other lab's bot traffic
in `#general`**, and sees all bot traffic in any channel their own bot has posted
in.

This directly contradicts the deployed topology. Runtime settings are
`cohort_isolation_enabled=True` and `cohort_default_policy="isolated"` (the
`src/config.py:330` default of `False` is overridden in this deployment), and the
membership table is a **star**: 56 agents in exactly one cohort each, 2 agents in
all 56. The engine therefore already prevents a spoke bot from *acting on* another
spoke's posts — but the PI-facing page shows them anyway. The page shows strictly
more than the bot it represents is allowed to see.

The dashboard, by contrast, does scope correctly: `AgentMessage.agent_id == aid` at
`agent_page.py:198-212`.

### 1.2 Threads are not viewable

The feed is a flat list of the 100 newest messages. `AgentMessage` carries
everything needed to thread them — `message_ts` (canonical id, unique per run) and
`thread_ts` (NULL on roots, equal to the root's `message_ts` on replies), plus
`phase ∈ {new_post, thread_reply}` (`src/models/agent_activity.py:88-93`) — but the
row dict built at `agent_page.py:774-784` passes `thread_ts` and **omits
`message_ts`**, so a reply has no root to attach to. The template can only render a
`· thread` badge (`templates/agent/conversations.html:82`). A PI cannot read
responses to their bot's posts.

### 1.3 The topology matrix cannot be saved

`POST /admin/cohorts/topology` fails with
`Too many fields. Maximum number of fields is 1000.`

`templates/admin/cohort_topology.html:82` emits one hidden `present` input **per
rendered cell**. At 60 agents × 56 cohorts that is 3,360 hidden fields plus 168
ticked checkboxes ≈ **3,528 form fields**. `admin.py:1559` calls
`await request.form()` with no arguments, and Starlette 1.4.1 defaults
`max_fields=1000`, raising at `starlette/formparsers.py:96`. FastAPI surfaces it as
a 400 `detail`. The page renders fine; only the POST fails. It broke silently once
`agents × cohorts` crossed ~1,000 cells. This is the only `request.form()` call in
the codebase.

## 2. Requirements (settled during brainstorming)

- **Gate rule:** the page mirrors the engine's `_entry_allowed`
  (`src/agent/message_log.py:48-80`) **exactly**, including both documented
  bypasses — humans always pass, `collab_private` always passes. One narrow,
  deliberate exception ships on top of this mirror — see §4.3.
- **Thread fetch:** roots only on first paint, replies loaded **on click** from a
  new endpoint.
- **Thread gating:** replies **are** gated, and the reply count is computed with the
  same gate so the badge never promises turns the expansion will not show. This is a
  deliberate divergence from the engine, which classifies `get_thread_history` as
  UNGATED.
- **Topology:** restructure the payload to 116 markers; do not weaken the diff
  safety property.

### 2.1 Explicitly out of scope

The **private-channel read leak** is real but latent and is *not* fixed here. The
write path is gated by `pi_may_post_to_channel` (`src/services/pi_inbox.py:52-100`,
which honours `removed_at`); the read path has no equivalent, so a PI can read a
`collab_private` channel's history from before their bot joined, and read access
survives membership revocation. There are currently **zero** `collab_private`
channels in the database. Mirroring `_entry_allowed` (requirement above) means
`collab_private` keeps its blanket pass. Track separately.

## 3. Approach

The gate becomes one set of semantics expressed in two places that are provably in
sync. `_entry_allowed` remains the in-memory predicate for the engine; a new
`gate_clause()` renders the same truth table as a SQLAlchemy `WHERE` fragment for
the web page. `src/services/cohorts.py:1-8` already establishes this pattern — "The
simulation engine applies the gate; the admin UI previews it. They must never
disagree" — so the new module follows that precedent instead of inventing a parallel
one, and §7 pins the equivalence with a parity test.

Three deliverables:

| # | Change | Files |
|---|---|---|
| A | Cohort-scoped visibility | new `src/services/conversation_feed.py`, `src/routers/agent_page.py` |
| B | Threaded display with lazy expand | `agent_page.py` (+1 route), `conversations.html`, new `_thread_replies.html` |
| C | Topology payload 3,360 → 116 | `templates/admin/cohort_topology.html`, `src/routers/admin.py` |

C is independent and lands first. A and B share the gate primitive, so A precedes B.

## 4. Component: `src/services/conversation_feed.py` (new)

Owns one question: *what may this PI see in the conversation feed, and how do I ask
Postgres for it.*

```python
async def resolve_agent_gate(db, agent_id: str) -> set[str] | None
def gate_clause(gate: set[str] | None) -> ColumnElement[bool]
```

### 4.1 `resolve_agent_gate`

Calls `compute_gates` (`src/services/cohorts.py:74`) exactly as
`_cohort_gate_context` does (`src/routers/admin.py:1372-1401`), with **one
deliberate difference**: the roster is `active agents ∪ {viewing agent}`.

The route admits `status in ("active", "inactive")` (`agent_page.py:718`), but
`compute_gates` only returns keys for the roster it is given, so an inactive viewing
agent would raise `KeyError`. Including it can only *lower* the chance of a
preflight refusal — it raises `live_members`, which the refusal test at
`cohorts.py:59-70` compares against zero — so it cannot cause a silent
roster-wide-silence regression.

Returns the viewing agent's `allowed_sender_ids`: `None` (gate off), or a set
(possibly empty).

### 4.2 `gate_clause`

The SQL mirror of `_entry_allowed`, ordered clause for clause to make the
correspondence reviewable:

```python
if gate is None:                       # gate off for this agent
    return true()
return or_(
    AgentMessage.is_bot.is_(False),                        # humans pass
    AgentMessage.visibility == VISIBILITY_COLLAB_PRIVATE,  # explicit pairing passes
    and_(AgentMessage.agent_id.is_not(None),               # fail closed on NULL
         AgentMessage.agent_id.in_(gate)) if gate else false(),
)
```

The `if gate else false()` is load-bearing: an uncohorted agent under
`policy="isolated"` gets `set()`, and an empty `IN` is the one input whose rendering
would otherwise be ambiguous.

`VISIBILITY_COLLAB_PRIVATE` is imported from `src/visibility.py:18` — the same
constant `_entry_allowed` uses, not a string literal, so the two cannot drift apart
on a rename.

The `is_bot` keying (rather than `agent_id is None`) and the NULL-`agent_id`
fail-closed branch are both carried over from `_entry_allowed`'s docstring, which
records why each exists: `agent_messages.agent_id` is nullable, so a bot-authored
row with a NULL `agent_id` would otherwise pass through the human bypass.

### 4.3 `own_or_gated` — the one deliberate divergence from `_entry_allowed`

Shipped alongside `gate_clause` in `src/services/conversation_feed.py` but not
enumerated in §4.2's mirror above (an audit gap — this subsection is the fix):

```python
def own_or_gated(gate: set[str] | None, agent_id: str) -> ColumnElement[bool]:
    return or_(gate_clause(gate), AgentMessage.agent_id == agent_id)
```

`gate_clause` alone is not quite what every call site needs. Under
`policy="isolated"`, an agent that is active but not yet placed in any cohort
gets `gate == set()` (§4.1, `resolve_agent_gate`/`compute_gates`), and
`gate_clause(set())` — correctly, per `_entry_allowed` — admits nothing from the
membership branch. That is exactly right for the *engine*: an uncohorted agent
should not act on anyone. It is wrong for the *PI's own page*: onboarding
activates an agent before an admin has assigned it to a cohort (see
`CLAUDE.md`'s Provision → Approve & Activate order), so a strict `gate_clause`
mirror would blank a PI's conversations feed the moment their bot goes live,
before anyone had a chance to misconfigure anything.

`own_or_gated` widens `gate_clause` with an explicit own-post carve-out: the
viewing agent's own rows always render, regardless of gate. This is safe
because the OR's second arm is keyed on `agent_id == agent_id` — the *viewing*
agent's own id, fixed by the route's own authorization
(`get_agent_with_access`), not attacker input — so it can only ever admit rows
this exact agent authored, never another agent's. It cannot be used to read
anyone else's traffic.

Three call sites share one `own_or_gated(gate, aid)` expression rather than
three independently-written clauses, specifically so they cannot drift apart:
the conversations feed's roots query, its reply-count query, and the
thread-expand endpoint's root re-resolution and reply fetch (§5.1, §5.2).

Covered by `test_an_uncohorted_agent_still_sees_its_own_posts` and
`test_expanding_an_uncohorted_own_thread_is_200_not_404`
(`tests/integration/test_conversation_feed.py`).

## 5. Data flow

### 5.1 Feed — `GET /agent/{agent_id}/conversations`

The channel set is **unchanged and needs no gate**: it derives from the agent's own
authored messages (`agent_id == aid`). The unconditional `#general` union stays —
`#general` is the lobby, and it is now cohort-filtered like every other channel,
which is precisely what made it a leak before.

The single flat query is replaced by two:

1. **Roots** — `thread_ts IS NULL AND phase == "new_post" AND gate_clause(gate)`,
   `ORDER BY posted_at DESC, created_at DESC, id DESC LIMIT 50`, then reversed for
   oldest-first render.

   The three-column ordering must survive **verbatim**, along with its comment at
   `agent_page.py:761-769`: migration 0019 added `posted_at` with `server_default
   '0'`, so every pre-migration row shares one value, and `ORDER BY posted_at DESC
   LIMIT n` over a tie group larger than `n` lets Postgres return any `n` — measured
   on a 200-row tie group, the index-scan and seq-scan plans returned two **disjoint**
   pages.

   `phase == "new_post"` is belt-and-braces alongside `thread_ts IS NULL`; the two
   agree on every current row (snapshot 2026-08-05: 304 messages, 273 roots, 31
   replies; `phase` partitions them identically).

   Note this changes the window's **unit**: today it is the newest 100 *messages*,
   and it becomes the newest 50 *threads*. Threads are the thing a PI reads, and
   replies no longer consume window slots, so 50 roots surfaces strictly more
   distinct conversations than 100 mixed rows did.

2. **Reply counts** — `thread_ts IN (root message_ts) AND gate_clause(gate)`,
   `GROUP BY thread_ts`. Gated, so the badge equals what expansion renders.
   Precedent: `src/routers/admin.py:537-545`.

**The gate goes into SQL before `LIMIT`, never in Python after it.** `#general`
carries traffic from 56 out-of-cohort labs; post-`LIMIT` filtering would let it
consume the window and leave a spoke PI with a near-empty page.

The row dict gains `message_ts` and `reply_count`.

### 5.2 Expand — `GET /agent/{agent_id}/thread/{message_ts}` (new)

Returns a **server-rendered HTML fragment**, not JSON: no `fetch()` exists anywhere
in this codebase yet, and a Jinja partial matches house style and keeps rendering
logic server-side.

Authorization is four server-side checks:

1. `get_agent_with_access(agent_id, db, current_user)` — owner or delegate, else
   403 (`src/dependencies.py:94-127`).
2. Root exists in the **current run** and has `thread_ts IS NULL`.
3. Root's `channel_name` is in **this agent's** channel set.
4. Root passes `gate_clause(gate)`.

Any failure returns **404**. Checks 2–4 are the IDOR defense: `message_ts` is
otherwise a guessable identifier that would read out any thread in the run.

Replies are then fetched with the same gate, `ORDER BY posted_at ASC` (with
`created_at`, `id` tiebreakers for the same reason as §5.1).

### 5.3 Client

Vanilla JS, matching `templates/agent/public_profile.html:245-307`: a click handler
per root that fetches the fragment, injects it once, caches it, and toggles
thereafter. No framework, no client-side templating.

### 5.4 Known limitation (accepted)

Roots order by their **own** `posted_at`, so a new reply does not bump an old thread
back into the newest 50. At 273 roots / 31 replies this is not observable. Fixing it
requires a correlated `max(reply.posted_at)` in the `ORDER BY`; deferred as not worth
the cost now. Reviewed and accepted during brainstorming.

## 6. Component: topology payload

### 6.1 Template

Replace the per-cell hidden input (`cohort_topology.html:82`) with per-row and
per-column markers emitted once each:

```html
{% for a in agents %}<input type="hidden" name="present_agent" value="{{ a.agent_id }}">{% endfor %}
{% for c in cohorts %}<input type="hidden" name="present_cohort" value="{{ c.id }}">{% endfor %}
```

The checkbox at `cohort_topology.html:83` is unchanged.

### 6.2 Handler

`admin.py:1559-1569` rebuilds `rendered` as the cross product:

```python
form = await request.form(max_fields=_TOPOLOGY_MAX_FIELDS)
present_agents = {v for v in form.getlist("present_agent") if isinstance(v, str)}
present_cohorts = {v for v in form.getlist("present_cohort") if isinstance(v, str)}
rendered = {f"{cid}:{aid}" for cid in present_cohorts for aid in present_agents}
```

Everything downstream (`admin.py:1571-1626`) is untouched: the `ticked - rendered`
malformed-submission check, the unknown-id skip, the per-cell add/remove diff, and
the per-change audit events.

**60 + 56 = 116 markers** (+168 ticked = 284 fields, down from 3,528).

### 6.3 Why the safety property is preserved

The existing guarantee is that a stale or partial form cannot delete memberships for
a cohort or agent it did not display. That holds because `rendered` is exactly the
set of displayed cells — and the displayed cells **are** the cross product of
displayed rows and displayed columns, since the template renders a full matrix
(`cohort_topology.html:61-88`, an unconditional nested loop). Reconstructing the
product server-side yields an identical set, so the property is unchanged rather
than merely approximated.

`max_fields` is raised alongside as a guard, because ticked cells alone will
eventually pass 1,000 even with the restructure.

## 7. Testing

Gate is `./scripts/ci.sh`: alembic sanity → `ruff check` on the suite → full pytest
with a branch-coverage floor.

### 7.1 Parity test (the important one)

Table-driven unit test over representative rows — human; bot in-cohort; bot
out-of-cohort; bot with NULL `agent_id`; `collab_private`; gate `None`; gate `set()`
— asserting that `_entry_allowed` and `gate_clause` return **the same verdict on
every row**. This is what stops the two implementations drifting, which is the exact
failure mode `src/services/cohorts.py:1-8` was written to prevent.

### 7.2 Integration — feed

Against the star topology: a spoke PI cannot see another spoke's bot in `#general`;
hub bots remain visible to every spoke; the PI's own messages still render
(preserving `tests/integration/test_agent_page.py:906-911`); an uncohorted agent
under `policy="isolated"` sees humans only; a delegate sees exactly what the owner
sees.

This closes a real coverage gap: `test_agent_page.py` currently has **no** test
asserting which *other* agents' messages appear, unlike the dashboard test at
`test_agent_page.py:876-879`, which explicitly controls for its `agent_id` filter.

### 7.3 Integration — threads

Reply badge count equals the number of rows the expansion renders; expanding a
`message_ts` from a channel the agent does not participate in returns 404;
expanding an out-of-cohort root returns 404; expanding a reply's `message_ts`
(not a root) returns 404; a non-owner non-delegate gets 403.

### 7.4 Integration — topology

Round-trip save with the new payload adds and removes the expected memberships; a
form that omits a column cannot delete that column's memberships; a form that omits
a row cannot delete that row's memberships; an explicit assertion that a full-matrix
POST stays under `max_fields`.

## 8. Migration / operational notes

No schema change; no alembic revision. `message_ts`, `thread_ts`, `phase`, and
`visibility` all already exist and are populated.

No agent-run restart is required — this is web-tier only, and touches no module the
`agent-run` process loads. Per `CLAUDE.md`, `docker compose up -d --build app` is
sufficient to pick the change up.

Grouping must key on `thread_ts` / `message_ts` and **never** on `slack_thread_ts`:
the two diverge whenever a thread started with Slack off, because the canonical root
id is then locally minted and is not a valid Slack ts
(`src/agent/message_log.py:35-40`, `specs/local-db-conversations.md:37-38`).
