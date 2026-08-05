# Cohort-Scoped Conversations Feed + Threads + Topology Payload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the agent conversations page showing bot traffic from outside the viewing agent's cohort, make threads readable by expanding replies on click, and fix the topology matrix save that 400s with "Too many fields".

**Architecture:** A new `src/services/conversation_feed.py` holds two functions: `resolve_agent_gate` (the viewing agent's `allowed_sender_ids`, computed with the engine's own `compute_gates`) and `gate_clause` (a SQLAlchemy `WHERE` fragment rendering the same truth table as the engine's in-memory `_entry_allowed`). The conversations route applies that clause **in SQL before `LIMIT`**, selects thread roots rather than raw messages, and attaches gated reply counts; a new endpoint returns a rendered replies partial on click. Separately, the topology form replaces 3,360 per-cell hidden inputs with 116 per-row/per-column markers whose cross product the handler reconstructs.

**Tech Stack:** Python 3.11 (async), FastAPI + Starlette 1.4.1, SQLAlchemy 2.0 (async), Jinja2, Tailwind-in-template, vanilla JS (no framework), pytest + pytest-asyncio, testcontainers (Postgres).

**Spec:** `docs/specs/2026-08-05-conversations-cohort-scope-and-threads-design.md`

## Global Constraints

- **No schema change, no alembic revision.** `message_ts`, `thread_ts`, `phase`, `visibility` all exist and are populated. `scripts/ci.sh` asserts a single alembic head — do not add one.
- **`gate_clause` must render the same truth table as `_entry_allowed`** (`src/agent/message_log.py:48-80`). Task 2 pins this with a parity test driven from the *existing* `DECISION_TABLE` in `tests/unit/test_cohort_isolation.py:194`. Do not copy that table — import it.
- **Order of clauses in `gate_clause` must match `_entry_allowed`'s order** (gate-off → human → private → NULL-fail-closed → cohort membership) so the correspondence is reviewable line by line.
- **The gate goes into SQL before `LIMIT`, never in Python after it.** `#general` carries traffic from 56 out-of-cohort labs; post-`LIMIT` filtering leaves a spoke PI with a near-empty page.
- **Never group threads on `slack_thread_ts`.** Use `thread_ts` / `message_ts`. The two diverge when a thread started Slack-off (`src/agent/message_log.py:35-40`).
- **Preserve the three-column ordering verbatim** — `posted_at DESC, created_at DESC, id DESC` — and its comment at `src/routers/agent_page.py:761-769`. Migration 0019 gave `posted_at` a `server_default '0'`, so pre-migration rows form one tie group and a 2-column sort makes row *selection* plan-dependent.
- **`VISIBILITY_COLLAB_PRIVATE` is imported from `src/visibility.py`**, never written as a string literal.
- **`collab_private` keeps its blanket pass.** The private-channel *read* leak is explicitly out of scope (spec §2.1). Do not add a `private_channel_members` check.
- **Run tests inside the container** per `CLAUDE.md`, with an explicit scratch DB:
  ```bash
  docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
    -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
    blackbird-app python -m pytest tests/ -v
  ```
  Create the DB first if absent: `docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T postgres createdb -U copi copi_a3`. Never point `TEST_DATABASE_URL` at `copi`.
- **The edge-facing service is `blackbird-app`, never `app`** — a service named `app` hijacks org1's nginx upstream. Never use `--remove-orphans`.
- **Full gate before any commit is considered done:** `./scripts/ci.sh` (alembic sanity → `ruff check` on the suite → pytest with a branch-coverage floor).

## File Structure

| File | Responsibility |
|---|---|
| `src/services/conversation_feed.py` (new) | The only place that answers "what may this PI see in the feed, and how do I ask Postgres for it". Two functions, no route or template knowledge. |
| `src/routers/agent_page.py` (modify) | Applies the gate; selects roots + gated reply counts; serves the replies partial. |
| `templates/agent/conversations.html` (modify) | Renders roots with a reply badge and an expand control. |
| `templates/agent/_thread_replies.html` (new) | The replies fragment returned by the expand endpoint. Rendered server-side; no client templating. |
| `templates/admin/cohort_topology.html` (modify) | Emits per-row/per-column markers instead of per-cell hidden inputs. |
| `src/routers/admin.py` (modify) | Reconstructs the rendered cell set as a cross product; raises `max_fields`. |
| `tests/integration/test_conversation_feed.py` (new) | Gate parity + feed scoping + thread endpoint authz. |
| `tests/integration/test_cohort_admin.py` (modify) | Topology payload round-trip and partial-form safety. |

---

### Task 1: Topology form payload — 3,360 fields → 116

Independent of Tasks 2-6. Land it first; it unblocks the admin UI immediately.

**Files:**
- Modify: `templates/admin/cohort_topology.html:40-88`
- Modify: `src/routers/admin.py:1544-1569`
- Test: `tests/integration/test_cohort_admin.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing other tasks rely on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_cohort_admin.py`. The existing helper at line 50 (`Cohort(name=name, created_by=admin.id)`) is the pattern for building cohorts; match whatever local fixture names that file already uses for `client`, `db_session`, and the admin auth header.

```python
async def test_topology_save_round_trips_with_marker_payload(
    client, db_session, admin, admin_headers
):
    """The new payload adds and removes exactly the ticked/unticked cells."""
    from src.models import Cohort, CohortMembership

    c1 = Cohort(name="alpha", created_by=admin.id)
    c2 = Cohort(name="beta", created_by=admin.id)
    db_session.add_all([c1, c2])
    await db_session.flush()
    a1 = await factories.make_agent(db_session, agent_id="ta1", bot_name="Ta1Bot")
    a2 = await factories.make_agent(db_session, agent_id="ta2", bot_name="Ta2Bot")
    # Pre-existing membership that the save must REMOVE (unticked but rendered).
    db_session.add(CohortMembership(cohort_id=c1.id, agent_id="ta2", added_by=admin.id))
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data=[
            ("present_agent", "ta1"), ("present_agent", "ta2"),
            ("present_cohort", str(c1.id)), ("present_cohort", str(c2.id)),
            ("cell", f"{c1.id}:ta1"),
        ],
        headers=admin_headers,
    )
    assert r.status_code == 302
    assert "1+added,+1+removed" in r.headers["location"], r.headers["location"]

    rows = {
        (str(cid), aid)
        for cid, aid in (await db_session.execute(
            select(CohortMembership.cohort_id, CohortMembership.agent_id)
        )).all()
    }
    assert rows == {(str(c1.id), "ta1")}


async def test_a_form_omitting_a_column_cannot_delete_that_columns_memberships(
    client, db_session, admin, admin_headers
):
    """The stale-form data-loss guard survives the cross-product reconstruction."""
    from src.models import Cohort, CohortMembership

    c1 = Cohort(name="shown", created_by=admin.id)
    c2 = Cohort(name="hidden", created_by=admin.id)
    db_session.add_all([c1, c2])
    await db_session.flush()
    await factories.make_agent(db_session, agent_id="tb1", bot_name="Tb1Bot")
    db_session.add(CohortMembership(cohort_id=c2.id, agent_id="tb1", added_by=admin.id))
    await db_session.commit()

    # c2 is NOT in present_cohort, so its cell was never rendered.
    r = await client.post(
        "/admin/cohorts/topology",
        data=[("present_agent", "tb1"), ("present_cohort", str(c1.id))],
        headers=admin_headers,
    )
    assert r.status_code == 302

    survivors = {
        (str(cid), aid)
        for cid, aid in (await db_session.execute(
            select(CohortMembership.cohort_id, CohortMembership.agent_id)
        )).all()
    }
    assert survivors == {(str(c2.id), "tb1")}, "a hidden column's membership was deleted"


async def test_a_form_omitting_a_row_cannot_delete_that_rows_memberships(
    client, db_session, admin, admin_headers
):
    from src.models import Cohort, CohortMembership

    c1 = Cohort(name="only", created_by=admin.id)
    db_session.add(c1)
    await db_session.flush()
    await factories.make_agent(db_session, agent_id="tc1", bot_name="Tc1Bot")
    await factories.make_agent(db_session, agent_id="tc2", bot_name="Tc2Bot")
    db_session.add(CohortMembership(cohort_id=c1.id, agent_id="tc2", added_by=admin.id))
    await db_session.commit()

    r = await client.post(
        "/admin/cohorts/topology",
        data=[("present_agent", "tc1"), ("present_cohort", str(c1.id))],
        headers=admin_headers,
    )
    assert r.status_code == 302

    survivors = {
        aid for (aid,) in (await db_session.execute(
            select(CohortMembership.agent_id)
        )).all()
    }
    assert survivors == {"tc2"}, "a hidden row's membership was deleted"


async def test_full_matrix_payload_stays_under_the_field_limit(
    client, db_session, admin, admin_headers
):
    """60x56 used to post 3,528 fields against Starlette's max_fields=1000."""
    from src.models import Cohort

    cohorts = []
    for i in range(56):
        c = Cohort(name=f"c{i:03d}", created_by=admin.id)
        db_session.add(c)
        cohorts.append(c)
    await db_session.flush()
    for i in range(60):
        await factories.make_agent(
            db_session, agent_id=f"td{i:03d}", bot_name=f"Td{i:03d}Bot"
        )
    await db_session.commit()

    payload = (
        [("present_agent", f"td{i:03d}") for i in range(60)]
        + [("present_cohort", str(c.id)) for c in cohorts]
        + [("cell", f"{cohorts[0].id}:td000")]
    )
    assert len(payload) == 117, f"expected 116 markers + 1 cell, got {len(payload)}"

    r = await client.post("/admin/cohorts/topology", data=payload, headers=admin_headers)
    assert r.status_code == 302, r.text
    assert "1+added" in r.headers["location"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_cohort_admin.py -k topology -v
```

Expected: FAIL. The round-trip and field-limit tests fail because the handler reads `present`, which the payload no longer sends, so it redirects to `?error=Nothing+to+save` instead of 302-ing to a `notice`.

- [ ] **Step 3: Change the template**

In `templates/admin/cohort_topology.html`, immediately after the `<form ...>` line (currently line 40), insert the markers:

```html
<form method="POST" action="/admin/cohorts/topology">
    {# Rendered rows and columns. The save reconstructs the rendered CELL set as
       the cross product of these two lists, which is exactly what the template
       renders below (an unconditional nested loop). Same stale-form guarantee as
       the old per-cell `present` inputs — a column or row that was not displayed
       cannot be diffed away — at 116 fields instead of 3,360. #}
    {% for a in agents %}<input type="hidden" name="present_agent" value="{{ a.agent_id }}">{% endfor %}
    {% for c in cohorts %}<input type="hidden" name="present_cohort" value="{{ c.id }}">{% endfor %}
```

Then delete the per-cell hidden input and its comment (currently lines 80-82), leaving the checkbox:

```html
                    <td class="px-3 py-3 text-center">
                        <input type="checkbox" name="cell" value="{{ cell }}"
                               data-col="{{ c.id }}"
                               class="h-4 w-4 rounded border-gray-300 text-indigo-600"
                               {% if cell in membership_set %}checked{% endif %}>
                    </td>
```

- [ ] **Step 4: Change the handler**

In `src/routers/admin.py`, add the constant next to `_COHORT_NAME_RE` (line 1369):

```python
# Starlette's request.form() defaults to max_fields=1000. The topology matrix
# posts one marker per rendered row and column plus one value per ticked cell,
# so the payload is agents + cohorts + ticked — but "ticked" alone will pass
# 1,000 on a large enough roster, so the limit is raised rather than relied on.
_TOPOLOGY_MAX_FIELDS = 50_000
```

Replace `admin.py:1559-1561` (the `form`/`ticked`/`rendered` block):

```python
    form = await request.form(max_fields=_TOPOLOGY_MAX_FIELDS)
    ticked = {v for v in form.getlist("cell") if isinstance(v, str)}
    present_agents = {v for v in form.getlist("present_agent") if isinstance(v, str)}
    present_cohorts = {v for v in form.getlist("present_cohort") if isinstance(v, str)}
    rendered = {f"{cid}:{aid}" for cid in present_cohorts for aid in present_agents}
```

Everything below is unchanged: the `if not rendered` guard, the `ticked - rendered` malformed check, the unknown-id skip, the per-cell diff, and the audit events.

Update the docstring's second sentence (line 1552) to describe the new payload:

```python
    """Apply a whole-matrix edit as a diff against the cells that were rendered.

    The form posts one ``cell`` value per ticked box (``{cohort_id}:{agent_id}``),
    one ``present_agent`` per rendered row and one ``present_cohort`` per rendered
    column; the rendered cell set is their cross product, which is what the
    template renders (an unconditional nested loop). Sending markers instead of one
    hidden input per cell keeps the payload at agents+cohorts fields rather than
    agents*cohorts — 60x56 posted 3,528 fields and hit Starlette's
    ``max_fields=1000``, which is why the matrix could not be saved at all.

    Diffing against ``rendered`` rather than against the whole table means a stale
    or partial form can never delete memberships for a cohort or agent it did not
    display — the usual checkbox-matrix data-loss bug. Unknown cohort/agent ids are
    ignored, never written. Every add and remove is audited individually.
    """
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_cohort_admin.py -v
```

Expected: PASS, including the pre-existing topology tests in that file.

- [ ] **Step 6: Verify by hand against the real matrix**

Load `/admin/cohorts/topology`, toggle one checkbox, save. Expected: a 302 to `?notice=1+added,+0+removed` (or `0+added,+1+removed`), not a 400 `detail`.

- [ ] **Step 7: Commit**

```bash
git add templates/admin/cohort_topology.html src/routers/admin.py tests/integration/test_cohort_admin.py
git commit -m "fix(admin): topology matrix payload 3,360 fields -> 116

60x56 cells emitted one hidden 'present' input each, so a save posted ~3,528
form fields against Starlette's max_fields=1000 default and 400'd with 'Too
many fields'. Post one marker per rendered row and column instead and
reconstruct the rendered cell set as their cross product — identical
stale-form guarantee, since the template renders a full matrix."
```

---

### Task 2: `gate_clause` + parity with the engine

**Files:**
- Create: `src/services/conversation_feed.py`
- Test: `tests/integration/test_conversation_feed.py`

**Interfaces:**
- Consumes: `_entry_allowed` and `DECISION_TABLE` (test-only, for parity).
- Produces: `gate_clause(gate: set[str] | None) -> ColumnElement[bool]` — a SQLAlchemy boolean expression over `AgentMessage`, safe to drop into any `.where()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_conversation_feed.py
"""The conversations feed's visibility gate, and its parity with the engine.

The page must show exactly what the viewing agent's bot is allowed to act on.
The engine decides that in memory (``_entry_allowed``); the page decides it in
SQL (``gate_clause``). Two implementations of one rule is a drift hazard, so the
parity test below drives BOTH from the engine's own ``DECISION_TABLE``.
"""

import pytest
from sqlalchemy import select

from src.agent.message_log import _entry_allowed
from src.models import AgentMessage
from src.services.conversation_feed import gate_clause
from tests import factories
from tests.unit.test_cohort_isolation import DECISION_TABLE, _post

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "name,kwargs,gate,expected", DECISION_TABLE, ids=[r[0] for r in DECISION_TABLE]
)
async def test_gate_clause_matches_entry_allowed(
    db_session, name, kwargs, gate, expected
):
    """Every row of the engine's §5.1 table, decided by SQL instead of Python."""
    run = await factories.make_simulation_run(db_session)
    row_kwargs = dict(agent_id="x", is_bot=True, visibility="public")
    row_kwargs.update(
        {k: v for k, v in kwargs.items() if k in ("agent_id", "is_bot", "visibility")}
    )
    msg = await factories.make_agent_message(
        db_session, run=run, message_ts="1.0001", content="body", **row_kwargs
    )
    await db_session.flush()

    found = (await db_session.execute(
        select(AgentMessage.id).where(
            AgentMessage.simulation_run_id == run.id,
            gate_clause(gate),
        )
    )).scalars().all()
    sql_visible = msg.id in found

    entry_kwargs = dict(ts="1", channel="c", agent_id="x", name="X", content="")
    entry_kwargs.update(kwargs)
    python_visible = _entry_allowed(_post(**entry_kwargs), gate)

    assert sql_visible == expected, f"SQL disagreed with the table on: {name}"
    assert sql_visible == python_visible, (
        f"gate_clause and _entry_allowed disagree on: {name}"
    )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -v
```

Expected: FAIL at collection — `ModuleNotFoundError: No module named 'src.services.conversation_feed'`.

- [ ] **Step 3: Write the implementation**

```python
# src/services/conversation_feed.py
"""What a PI may see in their agent's conversations feed.

The simulation engine gates what each agent may *act on* (``_entry_allowed`` in
``src/agent/message_log.py``); this module gates what that agent's PI may *read*
on the web page. They are the same rule, and they must never disagree — the same
constraint ``src/services/cohorts.py`` was written under, and for the same reason.

``_entry_allowed`` filters ``LogEntry`` objects already in memory. The page cannot
do that: the filter has to run in SQL, before ``LIMIT``, or ``#general`` traffic
from every other cohort consumes the window and the page comes back near-empty.
So the rule is expressed twice — once as a predicate, once as a WHERE fragment —
and ``tests/integration/test_conversation_feed.py`` asserts the two agree on
every row of the engine's own decision table.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, false, or_, true

from src.models import AgentMessage
from src.visibility import VISIBILITY_COLLAB_PRIVATE


def gate_clause(gate: set[str] | None) -> ColumnElement[bool]:
    """The cohort gate as a SQL predicate over ``AgentMessage``.

    Mirrors ``_entry_allowed`` clause for clause, in the same order, so the two
    can be diffed by eye:

    - ``gate is None`` — no filtering for this agent (isolation off, or policy
      "open" and the agent is uncohorted);
    - the author is a **human** — keyed on ``is_bot``, *not* on a NULL
      ``agent_id``. ``agent_messages.agent_id`` is nullable, so a bot-authored row
      with a NULL ``agent_id`` would otherwise pass through the human bypass;
    - the row is in a ``collab_private`` channel — a PI explicitly paired those
      agents, and an admin-level grouping must not veto an explicit human pairing;
    - a bot row with a NULL ``agent_id`` cannot be attributed to a cohort, so it
      fails closed;
    - otherwise the author must share a cohort with the viewing agent.

    ``gate`` is an EMPTY set for an uncohorted agent under
    ``cohort_default_policy="isolated"``. That is the one input where the
    membership branch must be dropped entirely rather than rendered as an empty
    ``IN`` — hence the ``if gate else false()``.
    """
    if gate is None:
        return true()
    return or_(
        AgentMessage.is_bot.is_(False),
        AgentMessage.visibility == VISIBILITY_COLLAB_PRIVATE,
        and_(
            AgentMessage.agent_id.is_not(None),
            AgentMessage.agent_id.in_(gate),
        ) if gate else false(),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -v
```

Expected: PASS — 10 parametrised cases, one per `DECISION_TABLE` row.

- [ ] **Step 5: Commit**

```bash
git add src/services/conversation_feed.py tests/integration/test_conversation_feed.py
git commit -m "feat(feed): gate_clause — the cohort gate as a SQL predicate

Mirrors the engine's _entry_allowed clause for clause so the web page can
filter before LIMIT. Parity is pinned against the engine's own DECISION_TABLE
rather than a copy of it."
```

---

### Task 3: `resolve_agent_gate`

**Files:**
- Modify: `src/services/conversation_feed.py`
- Test: `tests/integration/test_conversation_feed.py`

**Interfaces:**
- Consumes: `compute_gates` (`src/services/cohorts.py:74`).
- Produces: `async resolve_agent_gate(db: AsyncSession, agent_id: str) -> set[str] | None` — the viewing agent's `allowed_sender_ids`; `None` = gate off, `set()` = isolated.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_conversation_feed.py`:

```python
from src.models import Cohort, CohortMembership
from src.services.conversation_feed import resolve_agent_gate


async def _cohort(db, name, *agent_ids):
    c = Cohort(name=name)
    db.add(c)
    await db.flush()
    for aid in agent_ids:
        db.add(CohortMembership(cohort_id=c.id, agent_id=aid))
    await db.flush()
    return c


async def test_gate_is_the_union_of_co_members(db_session, monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(db_session, agent_id="spoke1", bot_name="Spoke1Bot")
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "pair1", "spoke1", "hub")
    await _cohort(db_session, "pair2", "spoke2", "hub")

    assert await resolve_agent_gate(db_session, "spoke1") == {"spoke1", "hub"}
    assert await resolve_agent_gate(db_session, "spoke2") == {"spoke2", "hub"}
    assert await resolve_agent_gate(db_session, "hub") == {"spoke1", "spoke2", "hub"}


async def test_uncohorted_agent_is_isolated_under_policy_isolated(
    db_session, monkeypatch
):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(db_session, agent_id="lonely", bot_name="LonelyBot")
    await factories.make_agent(db_session, agent_id="other", bot_name="OtherBot")
    await _cohort(db_session, "somepair", "other")

    assert await resolve_agent_gate(db_session, "lonely") == set()


async def test_gate_is_off_when_isolation_is_disabled(db_session, monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", False, raising=False)

    await factories.make_agent(db_session, agent_id="anyone", bot_name="AnyoneBot")

    assert await resolve_agent_gate(db_session, "anyone") is None


async def test_an_inactive_viewing_agent_still_resolves(db_session, monkeypatch):
    """compute_gates only keys the roster it is given, and the conversations route
    admits status 'inactive'. Without adding the viewer to the roster this raised
    KeyError instead of returning a gate."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(
        db_session, agent_id="sleeper", bot_name="SleeperBot", status="inactive"
    )
    await factories.make_agent(db_session, agent_id="awake", bot_name="AwakeBot")
    await _cohort(db_session, "mixed", "sleeper", "awake")

    assert await resolve_agent_gate(db_session, "sleeper") == {"sleeper", "awake"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -k resolve -v
```

Expected: FAIL — `ImportError: cannot import name 'resolve_agent_gate'`.

- [ ] **Step 3: Write the implementation**

Add to `src/services/conversation_feed.py` (imports first):

```python
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import AgentRegistry, Cohort, CohortMembership
from src.services.cohorts import compute_gates
```

```python
async def resolve_agent_gate(db: AsyncSession, agent_id: str) -> set[str] | None:
    """The viewing agent's ``allowed_sender_ids``, via the engine's own computation.

    Same call the admin preview makes (``_cohort_gate_context``), with one
    deliberate difference: the roster is the active agents **plus the viewing
    agent**. ``/agent/{id}/conversations`` admits ``status in ("active",
    "inactive")``, but ``compute_gates`` only returns keys for the roster it is
    handed, so an inactive viewer would KeyError. Adding it can only *raise*
    ``live_members``, which the preflight compares against zero — so it cannot
    turn a refusal into a silent roster-wide isolation.
    """
    settings = get_settings()
    roster = {
        r[0] for r in (await db.execute(
            select(AgentRegistry.agent_id).where(AgentRegistry.status == "active")
        )).all()
    }
    roster.add(agent_id)
    rows = (await db.execute(
        select(CohortMembership.cohort_id, CohortMembership.agent_id)
    )).all()
    cohort_count = (await db.execute(
        select(func.count()).select_from(Cohort)
    )).scalar() or 0

    gates, _preflight_error = compute_gates(
        membership_rows=[(r[0], r[1]) for r in rows],
        agent_ids=sorted(roster),
        isolation_enabled=settings.cohort_isolation_enabled,
        policy=settings.cohort_default_policy,
        cohort_count=cohort_count,
        has_db=True,
    )
    return gates.get(agent_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -v
```

Expected: PASS, all cases including Task 2's parity set.

- [ ] **Step 5: Commit**

```bash
git add src/services/conversation_feed.py tests/integration/test_conversation_feed.py
git commit -m "feat(feed): resolve_agent_gate via the engine's compute_gates

Roster is active agents plus the viewing agent, because the conversations
route admits inactive agents and compute_gates only keys its given roster."
```

---

### Task 4: Apply the gate to the feed, and select roots

**Files:**
- Modify: `src/routers/agent_page.py:747-784`
- Test: `tests/integration/test_conversation_feed.py`

**Interfaces:**
- Consumes: `resolve_agent_gate`, `gate_clause`.
- Produces: each entry in the template's `messages` list now carries `message_ts: str | None` and `reply_count: int` alongside the existing `channel`, `sender`, `is_bot`, `content`, `thread_ts`, `posted_at`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_conversation_feed.py`. `_auth` is defined in `tests/integration/test_agent_page.py:52` — import it rather than re-deriving the cookie.

```python
from tests.integration.test_agent_page import _auth


async def test_a_spoke_pi_does_not_see_another_spokes_bot(
    client, db_session, monkeypatch
):
    """The star topology: two spokes and a hub. Spoke 1's PI must not see
    Spoke 2's bot, and MUST still see the hub (the positive control)."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi1 = await factories.make_user(db_session, name="Spoke One", email="s1@example.org")
    await factories.make_agent(
        db_session, user=pi1, agent_id="spoke1", bot_name="Spoke1Bot", pi_name="Spoke One"
    )
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "pair1", "spoke1", "hub")
    await _cohort(db_session, "pair2", "spoke2", "hub")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    # Spoke 1's own post is what puts #general in its channel set.
    await factories.make_agent_message(
        db_session, agent_id="spoke1", message_ts="1.0001",
        content="MINE-own-post", sender_name="Spoke1Bot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="hub", message_ts="1.0002",
        content="HUB-visible-post", sender_name="HubBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="spoke2", message_ts="1.0003",
        content="LEAK-other-spoke-post", sender_name="Spoke2Bot", **common
    )
    await db_session.commit()

    page = await client.get("/agent/spoke1/conversations", headers=_auth(pi1.id))
    assert page.status_code == 200
    assert "MINE-own-post" in page.text
    assert "HUB-visible-post" in page.text, "positive control: the hub must be visible"
    assert "LEAK-other-spoke-post" not in page.text
    assert "Spoke2Bot" not in page.text


async def test_a_pi_message_still_renders_under_the_gate(
    client, db_session, monkeypatch
):
    """is_bot=False bypasses the gate — the human bypass must survive."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Solo PI", email="solo@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="solo", bot_name="SoloBot", pi_name="Solo PI"
    )
    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="solo", message_ts="2.0001",
        content="BOT-anchor", sender_name="SoloBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id=None, is_bot=False, message_ts="2.0002",
        content="HUMAN-said-this", sender_name="Solo PI (PI)", **common
    )
    await db_session.commit()

    page = await client.get("/agent/solo/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "HUMAN-said-this" in page.text


async def test_replies_are_not_listed_as_top_level_rows(
    client, db_session, monkeypatch
):
    """The feed selects ROOTS. A reply appears via its count, not as its own card."""
    from src.config import get_settings
    monkeypatch.setattr(
        get_settings(), "cohort_isolation_enabled", False, raising=False
    )

    pi = await factories.make_user(db_session, name="Root PI", email="root@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="rooter", bot_name="RooterBot", pi_name="Root PI"
    )
    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="rooter", message_ts="3.0001", phase="new_post",
        content="THE-ROOT", sender_name="RooterBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="rooter", message_ts="3.0002", thread_ts="3.0001",
        phase="thread_reply", content="THE-REPLY", sender_name="RooterBot", **common
    )
    await db_session.commit()

    page = await client.get("/agent/rooter/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "THE-ROOT" in page.text
    assert "THE-REPLY" not in page.text, "a reply must not render as a top-level card"


async def test_a_delegate_sees_exactly_what_the_owner_sees(
    client, db_session, monkeypatch
):
    """Access is owner-or-delegate; the gate is the AGENT's, not the viewer's, so
    both must get byte-identical feeds."""
    from src.config import get_settings
    from src.models import AgentDelegate
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Owner", email="own@example.org")
    agent = await factories.make_agent(
        db_session, user=pi, agent_id="deleg", bot_name="DelegBot", pi_name="Owner"
    )
    await factories.make_agent(db_session, agent_id="stranger", bot_name="StrangerBot")
    await _cohort(db_session, "solo", "deleg")

    dee = await factories.make_user(db_session, name="Dee", email="dee2@example.org")
    db_session.add(AgentDelegate(agent_registry_id=agent.id, user_id=dee.id))

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="deleg", message_ts="4.0001",
        content="OWN-POST", sender_name="DelegBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="stranger", message_ts="4.0002",
        content="OUTSIDER-POST", sender_name="StrangerBot", **common
    )
    await db_session.commit()

    owner_page = await client.get("/agent/deleg/conversations", headers=_auth(pi.id))
    dee_page = await client.get("/agent/deleg/conversations", headers=_auth(dee.id))
    assert owner_page.status_code == 200
    assert dee_page.status_code == 200
    assert "OWN-POST" in dee_page.text
    assert "OUTSIDER-POST" not in owner_page.text
    assert "OUTSIDER-POST" not in dee_page.text
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -k "spoke or human or top_level or delegate" -v
```

Expected: FAIL — `LEAK-other-spoke-post` and `OUTSIDER-POST` are present (no gate), and `THE-REPLY` renders as its own top-level card.

- [ ] **Step 3: Rewrite the query block**

In `src/routers/agent_page.py`, add near the top-of-function imports (the route already imports `get_latest_run_id` inline at line 715):

```python
    from src.services.conversation_feed import gate_clause, resolve_agent_gate
```

Replace lines 754-784 (from the `# Recent messages ...` comment through the `messages = [...]` comprehension) with:

```python
        # What this PI may read == what their bot may act on. Filtering happens in
        # SQL, before LIMIT: #general carries every other cohort's traffic, so
        # filtering in Python afterwards would leave the page nearly empty.
        gate = await resolve_agent_gate(db, aid)

        # Thread ROOTS, newest first. `phase` is belt-and-braces alongside
        # `thread_ts IS NULL`; the two agree on every row.
        #
        # The three-column ordering is load-bearing, not stylistic. Migration
        # 0019 adds posted_at with server_default '0', so EVERY row that
        # predates it shares one value. With `ORDER BY posted_at DESC LIMIT
        # 50` over a tie group larger than 50, Postgres is free to return any
        # 50 — measured on a 200-row tie group, the index-scan and seq-scan
        # plans returned two DISJOINT pages, so half the messages were
        # unreachable and which half flipped with the plan. Adding created_at
        # and the primary key makes the sort total.
        root_rows = await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.channel_name.in_(channels),
                AgentMessage.thread_ts.is_(None),
                AgentMessage.phase == "new_post",
                gate_clause(gate),
            )
            .order_by(AgentMessage.posted_at.desc(), AgentMessage.created_at.desc(),
                      AgentMessage.id.desc())
            .limit(_ROOT_LIMIT)
        )
        roots = list(reversed(root_rows.scalars().all()))

        # Reply counts, gated with the SAME clause so the badge can never promise
        # turns the expansion will not show.
        root_ts = [r.message_ts for r in roots if r.message_ts]
        counts: dict[str, int] = {}
        if root_ts:
            count_rows = await db.execute(
                select(AgentMessage.thread_ts, func.count(AgentMessage.id))
                .where(
                    AgentMessage.simulation_run_id == run_id,
                    AgentMessage.thread_ts.in_(root_ts),
                    gate_clause(gate),
                )
                .group_by(AgentMessage.thread_ts)
            )
            counts = {ts: n for ts, n in count_rows}

        messages = [
            {
                "channel": m.channel_name,
                "sender": m.sender_name or (m.agent_id or "PI"),
                "is_bot": m.is_bot,
                "content": m.content,
                "message_ts": m.message_ts,
                "thread_ts": m.thread_ts,
                "reply_count": counts.get(m.message_ts, 0),
                "posted_at": m.posted_at,
            }
            for m in roots
        ]
```

Add the constant near the top of `src/routers/agent_page.py`, after the imports:

```python
# Thread roots per page. The window's unit is threads, not messages: replies no
# longer consume slots, so this surfaces more distinct conversations than the
# previous flat 100-message window did.
_ROOT_LIMIT = 50
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py tests/integration/test_agent_page.py -v
```

Expected: PASS. `test_agent_page.py` must stay green — in particular
`test_posting_a_message_writes_a_pi_row_into_the_named_channel` (line ~887), whose
control asserts a PI message is visible on the read view.

No assertion in this task depends on markup that Task 5 or 6 introduces; the
reply *badge* is asserted in Task 6, once the template that renders it exists.

- [ ] **Step 5: Commit**

```bash
git add src/routers/agent_page.py tests/integration/test_conversation_feed.py
git commit -m "fix(feed): cohort-scope the conversations page and select thread roots

The feed filtered on channel name only, so every PI saw every other lab's bot
traffic in #general — contradicting the deployed star topology, where the
engine already forbids those agents from interacting. Filter with the engine's
gate in SQL before LIMIT, and select roots with gated reply counts."
```

---

### Task 5: Thread expand endpoint + replies partial

**Files:**
- Modify: `src/routers/agent_page.py` (new route, after `agent_conversations`)
- Create: `templates/agent/_thread_replies.html`
- Test: `tests/integration/test_conversation_feed.py`

**Interfaces:**
- Consumes: `resolve_agent_gate`, `gate_clause`, `get_agent_with_access`.
- Produces: `GET /agent/{agent_id}/thread/{message_ts}` → an HTML fragment (`200`), or `404` for any unauthorised/absent/gated-out root, or `403` from `get_agent_with_access` for a non-owner non-delegate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_conversation_feed.py`:

```python
async def _threaded_world(db_session, monkeypatch):
    """Spoke1 (owned) + Spoke2 (not owned), each with a root and one reply."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi1 = await factories.make_user(db_session, name="S One", email="t1@example.org")
    await factories.make_agent(
        db_session, user=pi1, agent_id="spoke1", bot_name="Spoke1Bot", pi_name="S One"
    )
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "p1", "spoke1", "hub")
    await _cohort(db_session, "p2", "spoke2", "hub")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="spoke1", message_ts="9.0001", phase="new_post",
        content="MY-ROOT", sender_name="Spoke1Bot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="hub", message_ts="9.0002", thread_ts="9.0001",
        phase="thread_reply", content="HUB-REPLY", sender_name="HubBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="spoke2", message_ts="9.0003", phase="new_post",
        content="FOREIGN-ROOT", sender_name="Spoke2Bot", **common
    )
    await db_session.commit()
    return pi1


async def test_expanding_own_thread_returns_the_gated_replies(
    client, db_session, monkeypatch
):
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/9.0001", headers=_auth(pi1.id))
    assert r.status_code == 200
    assert "HUB-REPLY" in r.text


async def test_expanding_an_out_of_cohort_root_is_404(client, db_session, monkeypatch):
    """The IDOR guard: message_ts is guessable, so the root must re-pass the gate."""
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/9.0003", headers=_auth(pi1.id))
    assert r.status_code == 404
    assert "FOREIGN-ROOT" not in r.text


async def test_expanding_a_reply_ts_rather_than_a_root_is_404(
    client, db_session, monkeypatch
):
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/9.0002", headers=_auth(pi1.id))
    assert r.status_code == 404


async def test_expanding_an_unknown_ts_is_404(client, db_session, monkeypatch):
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/0.0000", headers=_auth(pi1.id))
    assert r.status_code == 404


async def test_a_stranger_cannot_expand_someone_elses_thread(
    client, db_session, monkeypatch
):
    await _threaded_world(db_session, monkeypatch)
    stranger = await factories.make_user(
        db_session, name="Nosy", email="nosy@example.org"
    )
    await db_session.commit()
    r = await client.get("/agent/spoke1/thread/9.0001", headers=_auth(stranger.id))
    assert r.status_code == 403
    assert "HUB-REPLY" not in r.text
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -k "expand or stranger" -v
```

Expected: FAIL with 404 on every case — the route does not exist yet. (The two
tests that *expect* 404 will fail too, on the `403` and `HUB-REPLY` assertions.)

- [ ] **Step 3: Create the partial**

```html
{# templates/agent/_thread_replies.html
   Fragment returned by GET /agent/{agent_id}/thread/{message_ts}. Rendered
   server-side so the gate and the markup stay in one place; the page injects it
   verbatim. #}
{% if replies %}
<div class="mt-2 space-y-2 border-l-2 border-gray-200 pl-3">
    {% for m in replies %}
    <div data-reply-row class="rounded-lg border {% if not m.is_bot %}border-indigo-200 bg-indigo-50{% else %}border-gray-200 bg-white{% endif %} p-2">
        <div class="flex items-center justify-between text-xs text-gray-500 mb-1">
            <span class="font-medium text-gray-700">{{ m.sender }}{% if not m.is_bot %} · PI{% endif %}</span>
        </div>
        <div class="text-sm text-gray-800 whitespace-pre-wrap">{{ m.content }}</div>
    </div>
    {% endfor %}
</div>
{% else %}
<p class="mt-2 pl-3 text-xs text-gray-400">No replies you can see in this thread.</p>
{% endif %}
```

- [ ] **Step 4: Add the route**

In `src/routers/agent_page.py`, directly after `agent_conversations` (which ends at line 797):

```python
@router.get("/{agent_id}/thread/{message_ts}", response_class=HTMLResponse)
async def agent_thread_replies(
    agent_id: str,
    message_ts: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replies for one thread, as an HTML fragment for the conversations page.

    ``message_ts`` is a guessable identifier, so authorisation cannot stop at the
    agent: the ROOT is re-resolved under this agent's channel set and cohort gate
    before any reply is read. Anything that does not resolve is a 404 — absent,
    not-a-root, another channel, and out-of-cohort are deliberately
    indistinguishable to the caller.

    Replies are gated too, with the same clause that produced the count on the
    page, so the badge and the expansion can never disagree. This diverges from
    the engine, which classifies ``get_thread_history`` as UNGATED because it is
    thread-internal; here the whole point is that out-of-cohort traffic must not
    be reachable by clicking.
    """
    from src.services.conversation_feed import gate_clause, resolve_agent_gate
    from src.services.pi_inbox import get_latest_run_id

    agent, _is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status not in ("active", "inactive"):
        raise HTTPException(status_code=404)
    aid = agent.agent_id

    run_id = await get_latest_run_id(db)
    if not run_id:
        raise HTTPException(status_code=404)

    ch_rows = await db.execute(
        select(distinct(AgentMessage.channel_name)).where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.agent_id == aid,
        )
    )
    channels = sorted({r[0] for r in ch_rows} | {"general"})

    gate = await resolve_agent_gate(db, aid)
    root = (await db.execute(
        select(AgentMessage)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.message_ts == message_ts,
            AgentMessage.thread_ts.is_(None),
            AgentMessage.channel_name.in_(channels),
            gate_clause(gate),
        )
        .limit(1)
    )).scalar_one_or_none()
    if root is None:
        raise HTTPException(status_code=404)

    reply_rows = await db.execute(
        select(AgentMessage)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.thread_ts == message_ts,
            gate_clause(gate),
        )
        .order_by(AgentMessage.posted_at.asc(), AgentMessage.created_at.asc(),
                  AgentMessage.id.asc())
    )
    replies = [
        {
            "sender": m.sender_name or (m.agent_id or "PI"),
            "is_bot": m.is_bot,
            "content": m.content,
        }
        for m in reply_rows.scalars().all()
    ]

    return templates.TemplateResponse(
        request, "agent/_thread_replies.html", {"replies": replies}
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -v
```

Expected: PASS. Every assertion in this task is against the endpoint's own
response, so nothing here waits on Task 6's template.

- [ ] **Step 6: Commit**

```bash
git add src/routers/agent_page.py templates/agent/_thread_replies.html tests/integration/test_conversation_feed.py
git commit -m "feat(feed): thread expand endpoint returning a gated replies partial

The root is re-resolved under the agent's channel set and cohort gate before
any reply is read — message_ts is guessable, so agent-level authz alone would
be an IDOR. Replies are gated with the same clause that produced the badge."
```

---

### Task 6: Render roots with a reply badge and expand-on-click

**Files:**
- Modify: `templates/agent/conversations.html:74-90`
- Test: `tests/integration/test_conversation_feed.py`

**Interfaces:**
- Consumes: `messages[].message_ts`, `messages[].reply_count` (Task 4); `GET /agent/{id}/thread/{ts}` (Task 5); `data-reply-row` on each rendered reply (Task 5's partial).
- Produces: nothing later tasks rely on.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_conversation_feed.py`. `_threaded_world` is defined in Task 5.

```python
async def test_the_badge_count_equals_the_rendered_reply_count(
    client, db_session, monkeypatch
):
    """The badge is computed with the same gate as the expansion, so it can never
    promise turns the expansion will not show."""
    pi1 = await _threaded_world(db_session, monkeypatch)

    page = await client.get("/agent/spoke1/conversations", headers=_auth(pi1.id))
    assert page.status_code == 200
    assert "1 reply" in page.text
    assert "1 replies" not in page.text, "singular/plural must agree with the count"

    r = await client.get("/agent/spoke1/thread/9.0001", headers=_auth(pi1.id))
    assert r.status_code == 200
    assert r.text.count("data-reply-row") == 1
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py -k badge -v
```

Expected: FAIL on `assert "1 reply" in page.text` — the template renders no badge yet.

- [ ] **Step 3: Replace the Recent activity block**

Replace `templates/agent/conversations.html` lines 74-90:

```html
    <!-- Recent activity -->
    <h2 class="text-lg font-semibold text-gray-900 mb-3">Recent activity</h2>
    {% if messages %}
    <div class="space-y-3">
        {% for m in messages %}
        <div class="rounded-lg border {% if not m.is_bot %}border-indigo-200 bg-indigo-50{% else %}border-gray-200 bg-white{% endif %} p-3 shadow-sm">
            <div class="flex items-center justify-between text-xs text-gray-500 mb-1">
                <span class="font-medium text-gray-700">{{ m.sender }}{% if not m.is_bot %} · PI{% endif %}</span>
                <span>#{{ m.channel }}</span>
            </div>
            <div class="text-sm text-gray-800 whitespace-pre-wrap">{{ m.content }}</div>
            {% if m.reply_count and m.message_ts %}
            <button type="button"
                    class="mt-2 text-xs font-medium text-indigo-600 hover:text-indigo-800"
                    data-thread-ts="{{ m.message_ts }}"
                    data-thread-url="/agent/{{ agent.agent_id }}/thread/{{ m.message_ts }}">
                Show {{ m.reply_count }} {% if m.reply_count == 1 %}reply{% else %}replies{% endif %}
            </button>
            <div class="thread-replies hidden" data-thread-for="{{ m.message_ts }}"></div>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    {% else %}
    <p class="text-sm text-gray-500">No messages yet in your agent's channels.</p>
    {% endif %}
</div>

<script>
// Threads load on demand: the page ships roots plus a gated reply count, and the
// replies fragment is fetched once per thread and cached in the DOM thereafter.
// Server-rendered HTML, so there is no client-side templating to keep in sync.
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('[data-thread-url]').forEach(function(btn) {
        var ts = btn.getAttribute('data-thread-ts');
        var panel = document.querySelector('[data-thread-for="' + CSS.escape(ts) + '"]');
        if (!panel) { return; }
        var labelShown = btn.textContent.trim().replace(/^Show/, 'Hide');
        var labelHidden = btn.textContent.trim();
        btn.addEventListener('click', function() {
            if (panel.dataset.loaded === '1') {
                panel.classList.toggle('hidden');
                btn.textContent = panel.classList.contains('hidden') ? labelHidden : labelShown;
                return;
            }
            btn.disabled = true;
            fetch(btn.getAttribute('data-thread-url'), { credentials: 'same-origin' })
                .then(function(r) {
                    if (!r.ok) { throw new Error('HTTP ' + r.status); }
                    return r.text();
                })
                .then(function(html) {
                    panel.innerHTML = html;
                    panel.dataset.loaded = '1';
                    panel.classList.remove('hidden');
                    btn.textContent = labelShown;
                })
                .catch(function() {
                    panel.innerHTML = '<p class="mt-2 pl-3 text-xs text-red-600">Could not load replies.</p>';
                    panel.classList.remove('hidden');
                })
                .finally(function() { btn.disabled = false; });
        });
    });
});
</script>
{% endblock %}
```

Note the `· thread` badge is gone from the channel line: a reply no longer renders
as a top-level card, so the marker has nothing left to mark.

- [ ] **Step 4: Run the full feed suite**

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/integration/test_conversation_feed.py tests/integration/test_agent_page.py -v
```

Expected: PASS, including `test_the_badge_count_equals_the_rendered_reply_count`
and `test_replies_are_not_listed_as_top_level_rows`.

- [ ] **Step 5: Run the whole gate**

```bash
./scripts/ci.sh
```

Expected: alembic single head, `ruff check` clean, full pytest green above the
branch-coverage floor.

- [ ] **Step 6: Verify by hand**

Rebuild the web tier and load a spoke PI's page:

```bash
docker compose -f docker-compose.prod.yml -p copi-blackbird up -d --build blackbird-app
```

Check: another spoke's bot does not appear in `#general`; the hub does; a root
with replies shows "Show N replies" and expands in place; clicking again
collapses without a second request (Network tab shows one call per thread).

- [ ] **Step 7: Commit**

```bash
git add templates/agent/conversations.html tests/integration/test_conversation_feed.py
git commit -m "feat(feed): render roots with a reply badge and expand-on-click

Replies load once per thread from the gated fragment endpoint and are cached in
the DOM. The '· thread' badge is dropped: replies no longer render as
top-level cards, so it had nothing left to mark."
```

---

## Deployment note

Web tier only — no schema change, and nothing the `agent-run` process imports at
startup. `docker compose -f docker-compose.prod.yml -p copi-blackbird up -d --build blackbird-app`
is sufficient; the simulation does **not** need restarting for this plan.

⚠️ **This host runs two stacks.** The container named `agent-run` belongs to
**org1 production** (`/home/ubuntu/copi-python`); this instance's is
`blackbird-agent-run`. Never `docker stop`/`rm` the unprefixed name, and never
pass `--remove-orphans`.
