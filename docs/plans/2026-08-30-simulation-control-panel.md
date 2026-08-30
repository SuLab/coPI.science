# Simulation Control Panel Implementation Plan (Phases 1–2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin page (`/admin/simulation`) that starts and stops the
simulation, controls the run-start Slack announcement (channels + message),
sets run settings (duration, fresh/resume), and shows live run statistics
including dollar cost — with the engine controlled through an explicit-command
DB control plane, never through Docker.

**Architecture:** The web tier cannot reach Docker (no socket is mounted
anywhere in this stack — verified), so control moves in-process: the `agent`
compose service becomes an always-on **supervisor** (`python -m
src.agent.supervisor`) that idles until an explicit `simulation_commands` row
tells it to start a run, and the running engine polls the same table on its
existing ~30s DB cadence to honor `stop` (via `request_stop()`, strictly
gentler than `docker stop -t 420`). The engine also upserts a heartbeat row
each poll, which is what makes the page's status and buttons honest. Stats and
cost are read-side aggregations over tables the engine already writes
(`llm_call_logs`, `opportunity_assessments`, `agent_messages`,
`specialist_consults`, `assessment_drops`), rendered server-side as inline
SVG/HTML — no new frontend stack.

**Tech Stack:** FastAPI + Jinja (existing admin patterns: `_DB`/`_ADMIN`
singletons, Origin guard, `templates/admin/`), SQLAlchemy async + Alembic
(`0042`), stdlib-only chart rendering, pytest with the existing
`client`/`db_session`/`engine` fixtures.

**Spec:** `docs/plans/2026-08-29-simulation-control-panel-requirements.md`
(the operator-approved requirements & design-inputs capture, including the
literature-review-derived metric sets, the verified price table, and the
standing pitfall rules). Conflicts in this plan resolve against that document.

## Architecture decision (approaches considered)

- **A. In-process supervisor + DB command table (CHOSEN).** No Docker socket
  anywhere; graceful stops through the engine's own `request_stop()`; commands
  and heartbeats are ordinary rows the web tier reads/writes with existing
  auth/audit patterns; the CLI (`docker compose run`) remains as the
  emergency path. Costs: a working-tree edit to `docker-compose.prod.yml`
  (the deliberately-uncommitted file — see Global Constraints), one container
  now hosts many runs' logs, and code deploys switch from `run` to
  `up -d agent`.
- **B. Host systemd unit polling the DB and shelling `docker compose run`.**
  Keeps one-container-per-run logs, but adds host-level machinery outside
  compose, is untestable from the suite, and couples the feature to root/host
  state (the host's one existing unit, `copi-backup.service`, sits in
  `failed` state today — not an encouraging precedent).
- **C. Docker-socket proxy container.** A socket reachable from anything
  web-adjacent is a container-escape surface; rejected outright.

**Never-auto-start is a hard invariant** (standing operator preference): the
supervisor marks every `pending` command **stale** at boot before entering its
loop, so no reboot, redeploy, or crash-restart can ever start a run that a
human did not explicitly request *after* the process came up. Commands are
one-shot rows, never desired-state.

## Verified facts this plan relies on (all re-checked 2026-08-29/30, HEAD `794dc3f`)

| Fact | Evidence |
|---|---|
| No service mounts the Docker socket; agent service is `profiles: [agent]`, no restart policy | working-tree `docker-compose.prod.yml` (read in full) |
| Engine polls the DB every ~30s already | `ROSTER_POLL_INTERVAL = 30.0`, `src/agent/simulation.py:245`; gate at `:8002-8004` |
| `request_stop()` is the graceful stop (sets `_running=False`/`_stop_event`; there is NO `_stop_requested` attribute); flush runs in the main loop's `finally` | `src/agent/simulation.py:1171`, CLAUDE.md restart runbook |
| `_run_simulation(max_runtime, budget, mock, no_db, fresh, reset_cursors, all_agents)` is an importable async function that builds engine + roster itself | `src/agent/main.py:154-435` |
| Worker's `claim_job` shows the FOR UPDATE SKIP LOCKED claim pattern and the module-level `_shutdown` signal pattern | `src/worker/main.py:28-52` |
| `app_settings` KV exists; read-through-to-Settings precedent | `src/models/provisioning.py:13-36`, `src/services/admin_provisioning.py:56-67` |
| Admin router uses `_DB = Depends(get_db)` / `_ADMIN = Depends(get_admin_user)` singletons (ruff B008 ratchet) | `src/routers/admin.py:87-92` |
| Admin sub-nav tabs render in `templates/base.html` under `active_admin` (~line 125-129) | read |
| Admin page tests: `client` fixture + `factories.make_user(db_session, user_role=USER_ROLE_ADMIN, ...)` + `auth_headers(user.id)` from `tests/integration/test_manager_access` | `tests/integration/test_admin_jobs_page.py` |
| Reachability gate: a new route is "reachable" once a reachable template links it (nav tab + page forms suffice; no allowlist entry needed) | `tests/unit/test_reachability.py` docstring |
| `llm_call_logs` has `model` (String(100), NOT NULL) and per-turn token sums documented "correct as billing totals"; cache columns since `0036`; `wall_ms` since `0035`; **no thread attribution column** | `src/models/agent_activity.py:195-215`, writer `_llm_log_record` |
| Per-call `stop_reason`/`max_tokens`/latency live only in `call_stats` (list, one object per real API call); row `latency_ms` is NOT a turn sum | `src/services/llm.py:100-118` |
| Distinct models in prod `llm_call_logs`: `claude-opus-4-6`, `claude-opus-5`, `claude-sonnet-4-6`, `claude-sonnet-5` | live query 2026-08-29 |
| The code uses the 5-minute cache TTL (`{"type": "ephemeral"}`, no `ttl`) | `src/services/llm.py:147-148,195,202` |
| No `inference_geo` anywhere in `src/` → standard pricing only | grep 2026-08-30 |
| Price table (LIVE-ONLY: fetched from `platform.claude.com/docs/en/about-claude/pricing`, 2026-08-29) | §Task 8 table |
| Announce feature (this repo, deployed 2026-08-30 at `794dc3f`): `Settings.run_start_announce_channels`, `run_marker.render_run_start_announcement` (file template + fallback, sentinel prepended by code), `_announce_run_start` (hub client, best-effort, records to `run.config`) | `src/agent/run_marker.py`, `src/agent/simulation.py:3806-3941`, `src/config.py:341` |
| Alembic head is `0041`; DB stamped `0041`; `0042` is free (the deferred `is_admin` drop note says "0042+", i.e. any later number, not a reservation) | `alembic heads` + live query; CLAUDE.md |
| ruff `line-length = 100` (E501 ignored); `select = [E,F,I,UP,B]`; tests lint zero-tolerance; src ratchet currently **226/231 — 5 findings of headroom**: new files must land at zero | `pyproject.toml:64`, `scripts/ci.sh`, measured on host 2026-08-30 |
| Dataviz categorical palette slots (light): 1 `#2a78d6`, 2 `#eb6834`, 3 `#1baf7a`, 4 `#eda100`, 5 `#e87ba4`, 6 `#008300`, 7 `#4a3aa7` | dataviz skill `references/palette.md` |

## Global Constraints

- **Never `git checkout`/`git stash`/commit `docker-compose.prod.yml`.** Task 5
  EDITS it (working-tree only, like the operator's standing edits) — the one
  deliberate exception, operator-flagged in that task; it still must never be
  staged or committed.
- **Run every test/ruff/git/docker command ON THE HOST via ssh**
  (`ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com "cd /home/ubuntu/blackbird-copi-science && <cmd>"`),
  never through the sshfs mount. Never `pip install` anywhere (no new
  dependencies in this plan — charts and statistics are stdlib).
- **Never start a simulation run** during implementation. The supervisor may
  be exercised only by tests. Deploying the supervisor container is a
  separate, operator-gated step (Deploy notes).
- **Never touch `agent-run`** (org1's production container). All compose
  commands carry `-f docker-compose.prod.yml`; never `--remove-orphans`.
- **Never-auto-start invariant** (see Architecture): boot-time staling of
  pending commands is load-bearing and pinned by test in Task 4.
- Migration `0042` is **migrate-before-serve** (it adds a column the new code
  maps on `llm_call_logs` — see Task 1's deploy box).
- Prompt files, `thread_guidance.py`, and `.ambr` snapshots are untouched by
  this plan. No prompt-set doc sync is needed.
- New routes must be linked from templates (reachability gate) and POSTs go
  through the Origin guard (tests copy the existing admin POST patterns).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`;
  `git add` only named files; `./scripts/ci.sh` ON-HOST must pass before the
  final commit of each phase.

## File structure

- Create: `alembic/versions/0042_simulation_control_plane.py`
- Create: `src/models/simulation_control.py` (SimulationCommand,
  SimulationProcessStatus, AdminAuditEvent) + export in `src/models/__init__.py`
- Modify: `src/models/agent_activity.py` (LlmCallLog.thread_ts)
- Create: `src/services/simulation_control.py` (commands, heartbeat, status)
- Create: `src/agent/supervisor.py` (idle loop; consumes `start`)
- Modify: `src/agent/simulation.py` (control poll + heartbeat in main loop;
  announce DB overrides; thread_ts in the LLM-log payload)
- Modify: `src/agent/run_marker.py` (optional `template_body` override +
  `validate_template`)
- Create: `src/services/llm_pricing.py` (versioned price table + cost math)
- Create: `src/services/simulation_stats.py` (all read-side aggregates)
- Create: `src/services/svg_charts.py` (tile/meter/bars/stacked/diverging/
  sparkline/gantt renderers)
- Modify: `src/routers/admin.py` (+5 routes) and `templates/base.html` (nav tab)
- Create: `templates/admin/simulation.html`
- Modify: `docker-compose.prod.yml` (WORKING TREE ONLY — Task 5)
- Modify: `CLAUDE.md` (operator docs)
- Tests: `tests/unit/test_simulation_control_service.py`,
  `tests/unit/test_supervisor.py`, `tests/unit/test_engine_control_poll.py`,
  `tests/unit/test_llm_pricing.py`, `tests/unit/test_simulation_stats.py`,
  `tests/unit/test_svg_charts.py`, `tests/unit/test_announce_overrides.py`,
  `tests/integration/test_admin_simulation_page.py`

---

# PHASE 1 — Control plane

### Task 1: Migration `0042` + models

**Files:**
- Create: `alembic/versions/0042_simulation_control_plane.py`
- Create: `src/models/simulation_control.py`
- Modify: `src/models/__init__.py` (exports), `src/models/agent_activity.py`
- Test: `tests/unit/test_simulation_control_service.py` (model round-trip half)

**Interfaces:**
- Consumes: `Base`, existing naming conventions from `src/models/`.
- Produces (later tasks import these exact names from `src.models`):
  `SimulationCommand` (`id: UUID`, `command: str` in {"start","stop"},
  `payload: dict | None`, `status: str` in {"pending","done","failed","stale"},
  `requested_by_user_id: UUID | None`, `created_at`, `consumed_at`,
  `result: str | None`); `SimulationProcessStatus` (`id: int` — always the
  single row `id=1`, `state: str` in {"idle","starting","running","stopping"},
  `simulation_run_id: UUID | None`, `detail: dict | None`, `updated_at`);
  `AdminAuditEvent` (`id: UUID`, `action: str`, `actor_user_id: UUID | None`,
  `payload: dict | None`, `created_at`); `LlmCallLog.thread_ts: str | None`.

- [ ] **Step 1: Write the failing model round-trip test**

```python
# tests/unit/test_simulation_control_service.py  (first half; Task 2 appends)
"""Control-plane rows: commands are one-shot and claimable exactly once;
the status row is a single upserted heartbeat; audit events are append-only."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AdminAuditEvent, SimulationCommand, SimulationProcessStatus

pytestmark = pytest.mark.asyncio


async def test_command_and_status_rows_round_trip(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        cmd = SimulationCommand(command="start", payload={"fresh": True, "max_runtime": 60})
        status = SimulationProcessStatus(id=1, state="idle")
        audit = AdminAuditEvent(action="simulation_start_requested", payload={"fresh": True})
        db.add_all([cmd, status, audit])
        await db.commit()
        cmd_id = cmd.id
    async with factory() as db:
        row = (await db.execute(
            select(SimulationCommand).where(SimulationCommand.id == cmd_id)
        )).scalar_one()
        assert row.status == "pending"          # server default
        assert row.payload["max_runtime"] == 60
        st = (await db.execute(select(SimulationProcessStatus))).scalar_one()
        assert st.state == "idle"
        # cleanup (shared session DB)
        await db.delete(row); await db.delete(st)
        for a in (await db.execute(select(AdminAuditEvent))).scalars():
            await db.delete(a)
        await db.commit()
```

- [ ] **Step 2: Run it to verify it fails**

ON-HOST: `.venv-test/bin/python -m pytest tests/unit/test_simulation_control_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'SimulationCommand'`

- [ ] **Step 3: Write the models**

```python
# src/models/simulation_control.py
"""Control plane for the simulation: explicit one-shot commands, a single
heartbeat/status row, and a generic admin audit trail.

Commands are NEVER desired-state: a `pending` row is a request that exactly
one consumer (the supervisor for `start`, the running engine for `stop`)
claims once and marks done/failed. The supervisor marks all pending rows
`stale` at boot — the never-auto-start invariant — so a reboot can never
replay a request from before the process came up.
"""
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from src.database import Base


class SimulationCommand(Base):
    __tablename__ = "simulation_commands"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    command: Mapped[str] = mapped_column(
        Enum("start", "stop", name="sim_command_enum"), nullable=False
    )
    #: start: {"fresh": bool, "max_runtime": int}. stop: none.
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("pending", "done", "failed", "stale", name="sim_command_status_enum"),
        nullable=False, server_default="pending",
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Free-text outcome: run id started, error tail, or the stale reason.
    result: Mapped[str | None] = mapped_column(Text, nullable=True)


class SimulationProcessStatus(Base):
    """One row (id=1), upserted: the supervisor writes state transitions, the
    engine overwrites `detail` + `simulation_run_id` on its ~30s poll. The
    page derives 'engine not responding' from `updated_at` staleness."""

    __tablename__ = "simulation_process_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="idle")
    simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    detail: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdminAuditEvent(Base):
    """Append-only audit of admin control actions (start/stop requests,
    announce-setting changes). Deliberately generic — cohort_audit_events is
    cohort-shaped; this is the everything-else trail."""

    __tablename__ = "admin_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

Add to `src/models/agent_activity.py`, on `LlmCallLog` directly under the
`channel` column (`:200`):

```python
    #: The interview thread this turn served, when the call site knows it
    #: (thread replies and specialist consults). Nullable and never
    #: backfilled: pre-0042 rows cannot be attributed retroactively, and
    #: cost-per-interview treats NULL as "unattributed". Added 0042.
    thread_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
```

Export the three new models from `src/models/__init__.py` following its
existing import/`__all__` pattern.

- [ ] **Step 4: Write migration `0042`**

```python
# alembic/versions/0042_simulation_control_plane.py
"""simulation control plane: commands, process status, admin audit,
llm_call_logs.thread_ts

Revision ID: 0042
Revises: 0041
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "simulation_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("command", sa.Enum("start", "stop", name="sim_command_enum"), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "done", "failed", "stale", name="sim_command_status_enum"),
            nullable=False, server_default="pending",
        ),
        sa.Column(
            "requested_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_simulation_commands_pending", "simulation_commands", ["status", "created_at"]
    )
    # At most ONE pending row per command kind — closes the two-admin
    # double-start race at the database (audit V7): the second concurrent
    # enqueue raises IntegrityError and the route renders a refusal.
    op.create_index(
        "uq_simulation_commands_one_pending",
        "simulation_commands", ["command"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "simulation_process_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="idle"),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "admin_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("llm_call_logs", sa.Column("thread_ts", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_call_logs", "thread_ts")
    op.drop_table("admin_audit_events")
    op.drop_table("simulation_process_status")
    op.drop_index("uq_simulation_commands_one_pending", table_name="simulation_commands")
    op.drop_index("ix_simulation_commands_pending", table_name="simulation_commands")
    op.drop_table("simulation_commands")
    sa.Enum(name="sim_command_status_enum").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sim_command_enum").drop(op.get_bind(), checkfirst=True)
```

> **Deploy order for `0042` — migrate BEFORE the new code serves** (same
> pattern as `0028`-`0041`): `0042` is three additive tables plus one additive
> nullable column, so *old code against the new schema* is safe. The reverse
> is not: the new code **maps `llm_call_logs.thread_ts`**, so against a
> pre-`0042` database `/admin/activity/{run}/llm-calls`' `select(LlmCallLog)`
> raises `UndefinedColumn` and the engine's LLM-log flush INSERT names the
> column and fails (the flush path reports LOST rows loudly). Additionally
> the new admin page and the engine's control poll read the three new tables.
> Build → `run --rm blackbird-app alembic upgrade head` → confirm `alembic
> current` = `0042` → `up -d blackbird-app worker`. The agent image must be
> rebuilt too (engine + supervisor code live there).

- [ ] **Step 5: Run the alembic round trip + the test**

ON-HOST: `./scripts/ci.sh` runs the round trip; for the inner loop use:
`.venv-test/bin/python -m pytest tests/unit/test_simulation_control_service.py -v`
Expected: PASS (testcontainers migrates through `0042`).

- [ ] **Step 6: Commit**

ON-HOST:
```bash
git add alembic/versions/0042_simulation_control_plane.py src/models/simulation_control.py src/models/__init__.py src/models/agent_activity.py tests/unit/test_simulation_control_service.py
git commit -m "feat(sim-control): 0042 control-plane tables + llm_call_logs.thread_ts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Control service

**Files:**
- Create: `src/services/simulation_control.py`
- Test: append to `tests/unit/test_simulation_control_service.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces (exact signatures; Tasks 3/4/6 call these):

```python
async def enqueue_command(db, *, command: str, payload: dict | None,
                          requested_by_user_id) -> SimulationCommand
    # May raise IntegrityError at flush/commit: the 0042 partial unique index
    # allows at most one pending row per command kind. Callers (Task 7 routes)
    # catch it and render a refusal — that IS the double-click guard.
async def claim_pending(db, *, command: str) -> SimulationCommand | None
    # SELECT ... WHERE status='pending' AND command=:c ORDER BY created_at
    # LIMIT 1 FOR UPDATE SKIP LOCKED; marks status via finish/fail by caller.
async def finish_command(db, cmd_id, *, status: str, result: str | None) -> None
async def mark_pending_stale(db, *, reason: str, command: str | None = None) -> int
    # UPDATE pending -> stale with result=reason (optionally one command kind
    # only); returns count. command=None at supervisor BOOT (stales
    # everything); command="start" after each run (audit V7).
async def upsert_status(db, *, state: str, simulation_run_id=None,
                        detail: dict | None = None) -> None
    # INSERT ... ON CONFLICT (id=1) DO UPDATE; always bumps updated_at
    # explicitly (datetime.now(UTC)) — onupdate does not fire on
    # do_update_stmt, so set it in the upsert's values.
async def read_status(db) -> SimulationProcessStatus | None
async def record_audit(db, *, action: str, actor_user_id, payload: dict | None) -> None

HEARTBEAT_STALE_SECONDS = 120

def derive_panel_state(status_row, now) -> str
    # "not_deployed" (row absent) | "stale" (updated_at older than
    # HEARTBEAT_STALE_SECONDS) | status_row.state otherwise.
```

- [ ] **Step 1: Write the failing tests** (append to the Task 1 file): (a)
  `claim_pending` claims the OLDEST pending row of that command and a second
  concurrent claim (two sessions) gets None while the first holds the lock —
  use two `async_sessionmaker` sessions, claim in the first without commit,
  attempt in the second, assert None (SKIP LOCKED), then finish in the first;
  (b) `mark_pending_stale` flips every pending row of both commands and
  returns the count, leaving done/failed rows untouched, and with
  `command="start"` stales only the start; (b2) a second `enqueue_command`
  of the same kind while one is pending raises IntegrityError (the 0042
  partial unique index), while a pending start + pending stop coexist fine; (c) `upsert_status`
  twice leaves exactly one row with the second state and a strictly later
  `updated_at`; (d) `derive_panel_state` returns "not_deployed" for None,
  "stale" for an old `updated_at`, and passes through `state` otherwise
  (pure function, no DB). Clean up all rows (shared session DB).
- [ ] **Step 2: Run to verify the new tests fail** (ImportError).
- [ ] **Step 3: Implement** exactly the signatures above; the claim uses
  `.with_for_update(skip_locked=True)` (worker precedent
  `src/worker/main.py:36-44`); `finish_command` sets `consumed_at`.
- [ ] **Step 4: Run the file green.**
- [ ] **Step 5: Ruff + commit**
  (`feat(sim-control): command/heartbeat/audit service`, files:
  `src/services/simulation_control.py`, the test file).

---

### Task 3: Engine hooks — stop poll, heartbeat, thread_ts

**Files:**
- Modify: `src/agent/simulation.py` (three surgical edits)
- Modify: `src/agent/tools.py` (consult thread attribution — audit V6)
- Test: `tests/unit/test_engine_control_poll.py`

**Interfaces:**
- Consumes: Task 2 service.
- Produces: `SimulationEngine._poll_control_plane(now: float) -> None`
  (async), called once per main-loop iteration; `CONTROL_POLL_INTERVAL = 30.0`
  module constant; heartbeat `detail` schema (page reads it in Task 9/11):
  `{"tick_at": iso, "agents": {agent_id: {"active_threads": int,
  "calls_in_window": int, "api_calls": int, "messages": int}},
  "roster_size": int}`.

- [ ] **Step 1: Write the failing tests.** Using the
  `test_assessments_summary_post.py::_engine` construction pattern
  (FakeSlackClient, no session_factory) plus a real `engine`-fixture
  session_factory where DB is needed:
  (a) with a pending `stop` command in the DB and
  `eng.session_factory`/`simulation_run_id` set, `await
  eng._poll_control_plane(now=1e9)` marks the command done, calls
  `request_stop()` (monkeypatch-record `request_stop` on the instance — there
  is NO `_stop_requested` attribute; `request_stop` sets
  `_running`/`_stop_event`), and upserts state `"stopping"`;
  (b) with no command, it upserts a heartbeat whose `detail["agents"]` has one
  entry per roster agent and state `"running"`;
  (c) called twice inside the interval, the second call is a no-op (one
  `updated_at`);
  (d) with `session_factory=None` it returns without raising;
  (e) a DB error (monkeypatch the service to raise) is swallowed with a
  WARNING — the control plane must never take down a run.
  Clean up rows.
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement.**
  1. Constant next to `ROSTER_POLL_INTERVAL` (`:245`):
     `CONTROL_POLL_INTERVAL = 30.0`, plus `self._last_control_poll = 0.0` in
     `__init__` near `_last_roster_poll` (`:572`).
  2. New method (place after `request_stop`, before `_sleep`): guard on the
     interval; whole body in `try/except Exception: logger.warning(...)`;
     open one session; `claim_pending(db, command="stop")` → if found:
     `finish_command(..., status="done", result=f"run {self.simulation_run_id}")`,
     `self.request_stop()`, upsert state `"stopping"`; else upsert
     `"running"` — both upserts pass
     `simulation_run_id=self.simulation_run_id` — with the detail dict built
     from `self.agents`
     (`len(a.state.active_threads)`, `len(a.state.call_times)`,
     `a.api_call_count`, `a.message_count`).
  3. Call it from `_run_main_loop`'s loop body, immediately beside the
     existing `await self._sync_roster_from_db()` call (NOT inside that
     method — the `_last_roster_poll` gate lives inside `_sync_roster_from_db`
     at :8002-8004; this method carries its own `_last_control_poll` gate the
     same way). Mint `now = time.time()` at the call site if the loop does
     not already hold a current value there.
  4. `thread_ts` passthrough: in `_llm_log_record` map
     `entry.get("thread_ts")` into the `LlmCallLog(...)` constructor next to
     `channel`. Two attribution sites (audit V6 corrected the second):
     (i) the thread-reply path — `_reply_to_thread`'s `log_meta` dict
     (~simulation.py:2369) gains `"thread_ts": thread.thread_id`;
     (ii) the specialist-consult path lives in **`src/agent/tools.py`**, not
     simulation.py: `_execute_consult_specialist` builds its own `log_meta`
     (`tools.py:672-676`) and its signature (`tools.py:543-553`) has no
     thread parameter — add `thread_ts: str | None = None` to it, include it
     in that `log_meta`, and pass `thread.thread_id` down from
     `execute_tool`'s consult branch (which already receives `thread`;
     called from simulation.py:2258-2261). Leave every other `log_meta`
     site (`new_post` ~:3190, `memory` :8550) alone — NULL = unattributed,
     by design.
- [ ] **Step 4: GREEN** + neighbor guard:
  `pytest tests/unit/test_engine_control_poll.py tests/unit/test_run_start_announcement.py tests/unit/test_roster_sync.py -v`
- [ ] **Step 5: Ruff + commit** (`feat(sim-control): engine stop-poll, heartbeat, llm-log thread attribution`).

---

### Task 4: Supervisor

**Files:**
- Create: `src/agent/supervisor.py`
- Test: `tests/unit/test_supervisor.py`

**Interfaces:**
- Consumes: Task 2 service; `src.agent.main._run_simulation`.
- Produces: `python -m src.agent.supervisor` entrypoint;
  `run_supervisor(session_factory=None, run_fn=None, poll_seconds=5.0,
  max_loops=None) -> None` (async; injectable for tests: `run_fn` defaults to
  `_run_simulation`, `max_loops` bounds the loop for tests, None = forever).

- [ ] **Step 1: Write the failing tests** (all with a stub `run_fn` recorder
  and a real DB factory from the `engine` fixture):
  (a) **boot staling (the never-auto-start pin):** seed a pending `start`
  command, run `run_supervisor(max_loops=1)`, assert the command is `stale`
  with the boot reason and `run_fn` was **never called**;
  (b) a `start` command enqueued AFTER boot: launch the supervisor as a
  background task — `task = asyncio.create_task(run_supervisor(
  session_factory=factory, run_fn=stub, poll_seconds=0.05))` — poll the DB
  until the status row reads `idle` (boot staling has committed), THEN
  enqueue the start (`{"fresh": True, "max_runtime": 60}`), and
  `await asyncio.wait_for(task, 10)`: the supervisor claims it, awaits the
  stub, and — because a completed run EXITS the loop — the task finishes on
  its own. The stub records `*args` (the call is positional); assert
  `stub.calls == [(60, 0, False, False, True, False, False)]`, the command
  ended `done`, and the final status row is `idle`. (Do NOT try to seed the
  command before `run_supervisor` starts, and do NOT call `run_supervisor` a
  second time after enqueueing — boot staling makes both recipes
  structurally impossible.)
  (c) same background-task shape with a `run_fn` that raises: the command
  ends `failed` with the exception text in `result`, and the final status is
  `idle` (the task still exits cleanly);
  (d) a pending `stop` with nothing running (no status row, or a stale one)
  is finished `done` with result "nothing running" under
  `run_supervisor(max_loops=1)`;
  (e) a pending `stop` alongside a status row with state `running` and a
  FRESH `updated_at` (a live CLI-run engine) is **left pending** by
  `run_supervisor(max_loops=1)` — the live engine's own poll owns it;
  (f) a `start` enqueued while a run is live is STALED at run end: use the
  background-task shape with a `run_fn` that itself enqueues a second start
  before returning; after the task exits, the second command is `stale` with
  the "requested while a run was live" reason and the stub ran exactly once.
  Clean up rows.
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement**

```python
# src/agent/supervisor.py
"""Always-on control-plane consumer for the agent container.

Replaces the operator's `docker compose run ... python -m src.agent.main` with
an idle loop: the container stays up, and a run starts ONLY when an explicit
`simulation_commands` row (written by /admin/simulation) is claimed. Boot
marks every pending command stale — the never-auto-start invariant: a reboot,
redeploy, or crash-restart must never replay a pre-boot request.

The running engine handles `stop` itself (SimulationEngine._poll_control_plane)
— while idle, this loop clears orphaned `stop`s ONLY when no fresh `running`
heartbeat exists (a live heartbeat means a CLI-emergency engine owns that
stop; audit V4). The loop EXITS after each completed run (audit V5):
`restart: unless-stopped` brings the process back up idle through the
boot-staling path, which is what keeps `docker stop -t 420`'s documented
semantics — mid-run, _run_simulation's own SIGTERM handler stops the engine
gracefully, the run returns, the process exits, and `docker stop` returns
well inside the grace period instead of idling into a SIGKILL.
"""
import asyncio
import logging
import signal
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.ids import WRITER_ENGINE_AUX, set_default_writer_id
from src.agent.main import _run_simulation
from src.config import get_settings
from src.services.simulation_control import (
    claim_pending, derive_panel_state, finish_command, mark_pending_stale,
    read_status, upsert_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
_shutdown = False


def _request_shutdown() -> None:
    global _shutdown
    logger.info("Supervisor received shutdown signal")
    _shutdown = True


def _install_signal_handlers(loop) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown)


async def run_supervisor(session_factory=None, run_fn=None, poll_seconds=POLL_SECONDS,
                         max_loops=None) -> None:
    global _shutdown
    _shutdown = False
    run_fn = run_fn or _run_simulation
    own_engine = None
    if session_factory is None:
        own_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        session_factory = async_sessionmaker(own_engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    try:
        _install_signal_handlers(loop)
    except (NotImplementedError, RuntimeError):
        pass  # test event loops without signal support
    try:
        async with session_factory() as db:
            staled = await mark_pending_stale(db, reason="supervisor boot — request again")
            await upsert_status(db, state="idle")
            await db.commit()
        if staled:
            logger.warning("Boot: marked %d pre-boot pending command(s) stale", staled)
        loops = 0
        while not _shutdown and (max_loops is None or loops < max_loops):
            loops += 1
            async with session_factory() as db:
                # Stops FIRST, so a stale stop can never survive into a run
                # this iteration is about to start; and a stop aimed at a
                # live foreign engine (CLI emergency path) is left pending
                # for that engine's own control poll — a fresh `running`
                # heartbeat is the tell (audit V4).
                stop_cmd = await claim_pending(db, command="stop")
                if stop_cmd is not None:
                    if derive_panel_state(await read_status(db), datetime.now(UTC)) == "running":
                        logger.info("Leaving stop %s for the live engine", stop_cmd.id)
                    else:
                        await finish_command(db, stop_cmd.id, status="done",
                                             result="nothing running")
                cmd = await claim_pending(db, command="start")
                if cmd is None:
                    # Idle heartbeat: without this the page reads a healthy
                    # idle supervisor as "stale" two minutes after boot
                    # (audit V3).
                    await upsert_status(db, state="idle")
                    await db.commit()
                else:
                    payload = cmd.payload or {}
                    fresh = bool(payload.get("fresh", False))
                    max_runtime = int(payload.get("max_runtime", 0))
                    await upsert_status(db, state="starting",
                                        detail={"fresh": fresh, "max_runtime": max_runtime})
                    await db.commit()
                    cmd_id = cmd.id
            if cmd is not None:
                try:
                    await run_fn(max_runtime, 0, False, False, fresh, False, False)
                    outcome, result = "done", "run completed"
                except Exception as exc:  # noqa: BLE001 — the loop must survive a run
                    logger.exception("Simulation run raised")
                    outcome, result = "failed", f"{type(exc).__name__}: {exc}"[:500]
                async with session_factory() as db:
                    await finish_command(db, cmd_id, status=outcome, result=result)
                    # A start that slipped in while the run was live raced the
                    # page's is-running refusal — stale it, never run it
                    # (audit V7).
                    await mark_pending_stale(
                        db, reason="requested while a run was live — request again",
                        command="start",
                    )
                    await upsert_status(db, state="idle")
                    await db.commit()
                # EXIT after each completed run (audit V5): _run_simulation
                # replaced our signal handlers with the engine's, so idling on
                # would leave `docker stop` burning the full grace period into
                # a SIGKILL. Exiting restores the documented semantics;
                # `restart: unless-stopped` brings us back idle via boot
                # staling — never-auto-start holds.
                logger.info("Run finished (%s) — exiting for a clean restart", outcome)
                break
            await asyncio.sleep(poll_seconds)
    finally:
        if own_engine is not None:
            await own_engine.dispose()


def main() -> None:
    set_default_writer_id(WRITER_ENGINE_AUX)
    asyncio.run(run_supervisor())


if __name__ == "__main__":
    main()
```

Note: `set_default_writer_id` mirrors `main.main()` (`src/agent/main.py:65`).
`_run_simulation` does NOT call it itself, so the supervisor's `main()` must —
and one call per process suffices: verified against `src/agent/ids.py:114-128`
(safe to call once, carries the high-water mark; no per-run re-invocation
needed).

- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Ruff + commit** (`feat(sim-control): supervisor loop with boot-time command staling`).

---

### Task 5: Compose working-tree edit + operator docs ⚠️

**Files:**
- Modify: `docker-compose.prod.yml` (WORKING TREE ONLY — never staged)
- Modify: `CLAUDE.md`

⚠️ **This edits the deliberately-uncommitted production compose file.** The
change is four lines on the `agent` service and must be applied exactly, with
nothing else in the file touched, and the file must never be `git add`ed:

```yaml
  agent:
    build:
      context: .
    command: ["python", "-m", "src.agent.supervisor"]   # was src.agent.main
    restart: unless-stopped                              # NEW
    container_name: copi-blackbird-agent-1               # NEW (stable exec/log target)
    stop_grace_period: 420s                              # NEW — see below
```

`stop_grace_period: 420s` is load-bearing (audit V5): under `up -d`, compose
recreate/stop uses a DEFAULT 10-SECOND grace, so without this line a deploy or
`docker compose stop` during a live run SIGKILLs mid-turn and loses the flush
— exactly what the CLAUDE.md runbook's `docker stop -t 420` exists to prevent.
With it, plus the supervisor's exit-after-each-run behavior, `docker stop`
returns as soon as the engine's SIGTERM handler finishes the flush. Until this
line is in the file, treat `up -d agent` / `compose stop agent` while a run is
live as a data-loss operation. (Compose v2 `run` one-offs — the emergency CLI
path — clear the restart policy automatically, verified against compose
v2.37.1's own source, so `restart: unless-stopped` cannot resurrect a CLI
run; never-auto-start holds on that path too.)

(`profiles: [agent]` stays: the supervisor still only starts via an explicit
`$DC --profile agent up -d agent`, and `docker compose run` with an explicit
`python -m src.agent.main ...` command remains the documented emergency path.)

- [ ] **Step 1: Apply the compose edit** (verify with
  `git diff -- docker-compose.prod.yml | grep -c supervisor` → 1; still never stage it).
- [ ] **Step 2: CLAUDE.md** — two edits: (a) extend the two-stack warning's
  enumeration of working-tree compose deltas with the agent-service
  supervisor/restart/container_name lines; (b) add a "Simulation control
  plane" subsection under "Running the Agent Simulation" documenting: the
  supervisor replaces `run -d --name blackbird-agent-run` for normal
  operation (`$DC --profile agent up -d agent`); starts happen ONLY from
  /admin/simulation (or the CLI emergency path); `stop` from the page is
  gentler than `docker stop` (engine flushes in-process); boot stales pending
  commands (never-auto-start); code deploys = build agent + `up -d agent`
  (the supervisor comes back IDLE); `docker stop -t 420` semantics unchanged.
- [ ] **Step 3: Run the CLAUDE.md guards**
  ON-HOST: `pytest tests/unit/test_claude_md_disclosure_sync.py tests/unit/test_doc_prompt_sync.py -v` → green.
- [ ] **Step 4: Commit CLAUDE.md only**
  (`docs(sim-control): supervisor operations; compose delta documented` —
  `git add CLAUDE.md` and nothing else).

---

### Task 6: Announce overrides (DB-backed channels + template)

**Files:**
- Modify: `src/agent/run_marker.py` (override param + validator)
- Modify: `src/agent/simulation.py` (`_announce_run_start` reads app_settings)
- Test: `tests/unit/test_announce_overrides.py`

**Interfaces:**
- Produces: `render_run_start_announcement(values, template_body: str | None = None)`
  (None → file/default exactly as today); `validate_template(body: str) -> str | None`
  (None = ok, else a human-readable error — formats `body` against a
  sample values dict of all `ANNOUNCEMENT_VALUE_KEYS`); app_settings keys
  **`run_start_announce_channels`** and **`run_start_announcement_template`**
  (absent row/None value = fall back to `Settings` / template file). Task 7's
  routes write these keys.

- [ ] **Step 1: Failing tests:** (a) `render_run_start_announcement(values,
  template_body="custom {run_id}")` renders the custom body, still
  sentinel-prefixed, and a broken override falls back to the default with a
  WARNING (never raises); (b) `validate_template("ok {run_id}")` is None and
  `validate_template("{nope}")` names `nope`; (c) engine: with an app_settings
  row `run_start_announce_channels = "general"`, `_announce_run_start` posts
  only to #general (DB overrides the Settings default); with a
  `run_start_announcement_template` row, the posted text contains the
  override body; with neither row, behavior is byte-identical to today
  (pin against the existing `test_run_start_announcement.py` expectations);
  (d) DB read failure (monkeypatch service to raise) falls back to Settings
  with a WARNING — announce must not die on a KV hiccup.
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement.** run_marker: thread `template_body` through
  (`body = template_body if template_body is not None else _template_body()`),
  same broadened guard; `validate_template` formats against
  `{k: "sample" for k in ANNOUNCEMENT_VALUE_KEYS}` catching `Exception` →
  message. Engine: in `_announce_run_start`, after the channels parse, if
  `self.session_factory`: read both keys in one session (a small
  `_announce_overrides()` helper, `try/except` → `(None, None)` + WARNING);
  channels string from KV wins when non-None; pass template override into the
  render call.
- [ ] **Step 4: GREEN** + `pytest tests/unit/test_run_marker.py tests/unit/test_run_start_announcement.py` still green.
- [ ] **Step 5: Ruff + commit** (`feat(sim-control): DB-overridable announce channels and template`).

---

### Task 7: Admin routes + page (control half) + nav

**Files:**
- Modify: `src/routers/admin.py`, `templates/base.html`
- Create: `templates/admin/simulation.html` (control sections; Task 11 appends stats)
- Test: `tests/integration/test_admin_simulation_page.py`

**Interfaces:**
- Produces routes (all `_ADMIN`-gated, POSTs behind the Origin guard):
  - `GET  /admin/simulation` — status card (derive_panel_state + heartbeat
    detail + latest run row), pending/recent commands list, start form
    (fresh checkbox default ON, max_runtime number input default 0 =
    indefinite), stop button, announce settings form (channels text input
    prefilled from KV-else-Settings), template editor (textarea prefilled
    from KV-else-file, "Validate & save" + "Reset to file default"), recent
    `admin_audit_events`.
  - `POST /admin/simulation/start` (Form: `fresh: bool = Form(False)`,
    `max_runtime: int = Form(0)`) → refuse (redirect + flash-style query
    param) when `derive_panel_state` reads running/starting (this covers a
    live CLI-run engine too — its heartbeat is the tell) or a start is
    pending; else `enqueue_command` + `record_audit(...)`, catching
    IntegrityError from the unique-pending index as the same refusal (the
    double-click guard's second layer). DECLARED v1 decision: the two-field
    form + these refusals ARE the confirmation step (no JS confirm dialog);
    and the sharp CLI flags `--all-agents`/`--reset-cursors` are deliberately
    NOT exposed — CLI-only, with the page linking no substitute (spec §8.7
    resolved).
  - `POST /admin/simulation/stop` → refuse when nothing is running; else
    enqueue stop + audit (same IntegrityError-as-refusal handling).
  - `POST /admin/simulation/announce-settings` (Form `channels: str`) —
    validate each name against `^[a-z0-9_-]{1,80}$` (Slack allows
    underscores) after `parse_announce_channels`;
    write/clear the KV row (empty input clears → Settings default); audit
    with old+new values in payload.
  - `POST /admin/simulation/announce-template` (Form `body: str`,
    `reset: bool = Form(False)`) — `reset` deletes the KV row; else
    `validate_template` and refuse with the error rendered on the page;
    audit (payload records a sha256[:12] of old/new bodies, not full text).
- Nav: `base.html` admin sub-nav gains
  `<a href="/admin/simulation" class="{% if active_admin == 'simulation' %}…">Simulation</a>`
  copying the Users tab's exact class pattern (~line 129); handlers pass
  `active_admin="simulation"`.

- [ ] **Step 1: Failing tests** (pattern: `client` + `factories.make_user` +
  `auth_headers`, POSTs copying an existing admin POST test's headers):
  (a) GET renders "not deployed" with no status row, and "STALE"/warning when
  the row's `updated_at` is 10 minutes old;
  (b) POST start creates a pending `start` command with the payload and an
  audit row; a second POST while one is pending is refused (no second row);
  (c) POST stop with state `running` (seed the status row) creates the stop
  command; with state idle it refuses;
  (d) announce-settings round-trip: POST writes the KV; GET prefills it;
  empty POST clears it;
  (e) template POST with `{nope}` re-renders with the validator's error and
  writes no KV; valid body writes the KV; reset deletes it;
  (f) a non-admin (manager role) gets 403/redirect on every route.
  Clean up rows.
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement** routes (use the `_DB`/`_ADMIN` singletons for the
  new handlers, matching `admin.py:814-815`), template, and nav. Keep the
  template to the existing admin pages' plain-Tailwind idiom; each form is a
  plain POST that redirects back to `/admin/simulation?msg=…`.
- [ ] **Step 4: GREEN** + reachability:
  `pytest tests/integration/test_admin_simulation_page.py tests/unit/test_reachability.py -v`
- [ ] **Step 5: Ruff + commit** (`feat(sim-control): /admin/simulation control page`).
  **Phase-1 gate:** run full `./scripts/ci.sh` ON-HOST before this commit.

---

# PHASE 2 — Cost + Live statistics

### Task 8: Price table + cost service

**Files:**
- Create: `src/services/llm_pricing.py`
- Test: `tests/unit/test_llm_pricing.py`

The price table (LIVE-ONLY fact: fetched from
`platform.claude.com/docs/en/about-claude/pricing` on **2026-08-29**; the
implementer re-fetches and updates `AS_OF` if values moved):

```python
# src/services/llm_pricing.py
"""Versioned Anthropic price table + cost math for llm_call_logs rows.

Prices are $/MTok from platform.claude.com/docs/en/about-claude/pricing.
The simulation uses the 5-MINUTE cache TTL exclusively (src/services/llm.py
:147-148 — deliberate), so cache_creation tokens bill at the 1.25x write rate.
No batch/fast-mode/inference_geo modifiers apply (none are used in src/ —
verified 2026-08-30). Unknown models are None-priced: the caller renders
"unpriced" and surfaces the model name; a silent $0 is the one forbidden
failure mode. Rows written before migration 0036 have NULL cache columns —
treat NULL as 0 and label those aggregates as floors ("≥"), which
the read side (Task 9's CostSummary.is_floor) carries; cost_for_tokens itself
takes plain token counts and has no flag parameter.
"""
from dataclasses import dataclass
from decimal import Decimal

AS_OF = "2026-08-29"

@dataclass(frozen=True)
class ModelPrice:
    input: Decimal          # $/MTok
    output: Decimal
    cache_write_5m: Decimal
    cache_read: Decimal

PRICES: dict[str, ModelPrice] = {
    "claude-opus-5":      ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-opus-4-6":    ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-sonnet-5":    ModelPrice(Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("0.20")),
    "claude-sonnet-4-6":  ModelPrice(Decimal("3"), Decimal("15"), Decimal("3.75"), Decimal("0.30")),
    "claude-haiku-4-5":   ModelPrice(Decimal("1"), Decimal("5"),  Decimal("1.25"), Decimal("0.10")),
    "claude-fable-5":     ModelPrice(Decimal("10"), Decimal("50"), Decimal("12.50"), Decimal("1")),
}

_MTOK = Decimal(1_000_000)

def cost_for_tokens(model: str, *, input_tokens: int, output_tokens: int,
                    cache_read: int, cache_creation: int) -> Decimal | None:
    """Dollar cost of one aggregate; None when the model is unpriced."""
    p = PRICES.get(model)
    if p is None:
        return None
    return (
        Decimal(input_tokens) * p.input
        + Decimal(output_tokens) * p.output
        + Decimal(cache_read) * p.cache_read
        + Decimal(cache_creation) * p.cache_write_5m
    ) / _MTOK
```

- [ ] **Step 1: Failing tests:** exact arithmetic for a hand-computed case —
  opus-5 with input 1,000,000 / output 100,000 / cache_read 500,000 /
  cache_creation 200,000 → 5.00 + 2.50 + 0.25 + 1.25 = **Decimal("9.00")**
  (Task 11's cost-hero test reuses this same seeded case and figure); unknown
  model → None; the
  four production model ids are all priced (drift alarm naming them); zero
  tokens → Decimal 0; AS_OF matches `^\d{4}-\d{2}-\d{2}$`.
- [ ] **Step 2-4: RED → implement → GREEN.**
- [ ] **Step 5: Commit** (`feat(sim-stats): versioned price table + cost math`).

---

### Task 9: Stats service (read-side aggregates)

**Files:**
- Create: `src/services/simulation_stats.py`
- Test: `tests/unit/test_simulation_stats.py`

**Interfaces (all async, take `db` + `simulation_run_id`; return frozen
dataclasses defined in the module — Task 11 renders them):**

- `run_overview(db, run_id) -> RunOverview` — run row fields, rubric stamp
  from config, `run_start_announcement` record, elapsed/planned seconds,
  `get_build_info()` fields, `prompt_set_stamp` for both roles.
- `cost_summary(db, run_id) -> CostSummary` — one grouped query
  (`GROUP BY model`): token sums via `func.coalesce(func.sum(...), 0)`,
  per-model `cost_for_tokens`, `total: Decimal`, `unpriced_models: list[str]`,
  `is_floor: bool` (True when any row in the run predates the cache columns —
  detect via `func.count()` filtered on `cache_read_input_tokens.is_(None)`),
  plus per-agent and per-phase cost breakdowns (two more grouped queries).
- `hourly_activity(db, run_id) -> list[HourBucket]` — `date_trunc('hour',
  created_at)` over `llm_call_logs`: calls, cost, and the four token classes
  per bucket.
- `funnel(db, run_id) -> Funnel` — interviews opened (distinct
  `opportunity_assessments.thread_id` union hub-thread count from
  `thread_decisions`), verdicts stored, terminal vs provisional, announced
  (`summary_posted_at IS NOT NULL`), headlines owed, drops by reason
  (`assessment_drops` grouped), unvetted panel count (reuse
  `unvetted_panel_filter()` from `src/services/assessment_detail.py:435` —
  reuse, never re-derive).
- `specialist_mix(db, run_id) -> list[DomainMix]` — `specialist_consults`
  grouped by domain × signal (live three; historical labels folded into an
  `"historical"` bucket, never dropped), plus the per-interview consult
  fan-out distribution (consults per thread — the spec §4 "panel fan-out").
- `per_agent(db, run_id) -> list[AgentRow]` — role, registry status, muted
  flag (join `agents`), messages + calls + cost + last activity per agent
  (joins the heartbeat detail in the ROUTE, not here — this function is
  DB-only).
- `stop_reason_taxonomy(db, run_id) -> dict[str, int]` and
  `latency_percentiles(db, run_id) -> dict[str, LatencyPcts]` — both from
  `call_stats`: fetch `(phase, call_stats)` for the run (cap
  `LIMIT 20000`, newest first, and say so on the page when capped), explode
  in Python, P50/P95/P99 via `statistics.quantiles(data, n=100)` — NOTE:
  that returns 99 cut points (P50/P95/P99 = indices 49/94/98), and it
  **raises StatisticsError below 2 samples** — return an empty percentile set
  for n<2 (young runs), with a covering test; taxonomy buckets:
  `end_turn`/`tool_use` → "normal", `max_tokens` → "truncated",
  `refusal` → "refused", anything else verbatim.
- `interview_timeline(db, run_id) -> list[InterviewSpan]` — per assessment
  thread: first/last message ts from `agent_messages` (run-scoped, by
  `thread_ts`), outcome (recommendation or "in flight"), announced flag.
- `hub_lab_burn(db, run_id) -> list[BurnPoint]` — hourly hub-vs-lab token
  ratio (hub = `agent_id` of the roster's scout_hub — parameter, passed in).
- `cost_per_interview(db, run_id) -> list[InterviewCost]` — the literature
  review's cost-per-OUTCOME attribution: `llm_call_logs` grouped by
  `thread_ts` (non-NULL only — attribution exists for rows written after
  `0042`), joined to `opportunity_assessments.thread_id` for the outcome;
  includes an `unattributed: Decimal` bucket (NULL thread_ts) so the page's
  per-interview costs visibly do not sum to the run total on runs that
  predate or straddle `0042`.

- [ ] **Step 1: Failing tests** — seed a minimal run (one `SimulationRun`,
  a handful of `LlmCallLog`/`OpportunityAssessment`/`AssessmentDrop`/
  `SpecialistConsult`/`AgentMessage` rows via the existing `tests/factories`
  where factories exist, else direct model constructions) and assert each
  function's numbers by hand; include: unpriced-model surfacing, the
  pre-0036 floor flag (a row with NULL cache columns), an empty run returning
  zeroed structures (never raising), and run-scoping (a second run's rows
  never leak in). Clean up.
- [ ] **Step 2-4: RED → implement → GREEN.** All queries strictly
  run-scoped; every function tolerates an empty run.
- [ ] **Step 5: Ruff + commit** (`feat(sim-stats): read-side aggregates for the control panel`).

---

### Task 10: SVG/HTML chart primitives

**Files:**
- Create: `src/services/svg_charts.py`
- Test: `tests/unit/test_svg_charts.py`

Dependency-free renderers returning HTML/SVG strings (escaped via
`markupsafe.escape` — already a Jinja dependency). Constants: categorical
slots (light) `#2a78d6 #eb6834 #1baf7a #eda100 #e87ba4 #008300 #4a3aa7`;
sequential = tints of slot 1; diverging pair (pre-validated, from the dataviz
reference palette): red `#e34948` (blocking) ↔ blue `#2a78d6` (adequate) with
neutral midpoint gray `#f0efec` (gap) on the light surface — do not invent
other hexes. Rules baked in (from the dataviz
skill): one axis; thin marks; 2px gaps between stacked segments; text in
text-tokens never series colors; a `<title>` element per mark (native hover
tooltip — the no-JS floor); every chart paired with a `<details>` table
fallback.

Functions (exact signatures Task 11 calls):

```python
def stat_tile(label: str, value: str, note: str | None = None, *, warn: bool = False) -> str
def meter(label: str, fraction: float, detail: str) -> str            # clamps [0,1]
def hbar_list(rows: list[tuple[str, float, str]], *, color: str = "#2a78d6") -> str
    # (label, value, display) — widths normalized to max value
def stacked_hbar(label: str, segments: list[tuple[str, float, str]]) -> str
    # (segment_label, value, hex) — 2px gaps, per-segment <title>
def diverging_hbar(label: str, neg: float, mid: float, pos: float,
                   labels: tuple[str, str, str]) -> str                # blocking|gap|adequate
def sparkline(points: list[float], *, width: int = 240, height: int = 40) -> str
def gantt(rows: list[tuple[str, float, float, str, str]],
          t0: float, t1: float) -> str
    # (label, start, end, hex, title) — one thin bar per row on a shared scale
```

- [ ] **Step 1: Failing tests:** each renderer returns well-formed markup
  (parse with `xml.etree.ElementTree` for the SVG ones), escapes a
  `<script>` label, normalizes widths (two bars 10/20 → second exactly twice
  the first's width attr), meter clamps 1.5 → full, sparkline of one point
  doesn't divide by zero, gantt clamps spans to [t0,t1].
- [ ] **Step 2-4: RED → implement → GREEN.**
- [ ] **Step 5: Ruff + commit** (`feat(sim-stats): dependency-free chart primitives`).

---

### Task 11: The Live tab — wiring stats into `/admin/simulation`

**Files:**
- Modify: `src/routers/admin.py` (GET handler gains the stats context; add
  `?run=<uuid>` selector defaulting to the latest run), `templates/admin/simulation.html`
- Test: extend `tests/integration/test_admin_simulation_page.py`

Page sections in order (each renders its `<details>` table fallback and
degrades to an em-dash when its data is empty): **KPI row** (cost hero with
`is_floor` "≥" and unpriced-model warning; burn $/h; cache hit-rate meter;
progress meter or "indefinite"; headlines-owed tile with `warn=True` when >0;
interviews concluded) → **status/lifecycle card** (already present from Task
7; gains build-info/prompt-stamp/rubric lines from `run_overview`) →
**cumulative cost sparkline + token-class stacked bars per hour** → **cost by
agent / model / phase `hbar_list`s** → **funnel** (`hbar_list` descending +
drops table) → **specialist `diverging_hbar` per domain** → **stop-reason
taxonomy + latency P50/P95/P99 table (with the "newest 20k calls" cap note
when hit)** → **per-agent table** (stats + heartbeat columns merged;
heartbeat columns show "—" when the heartbeat is stale/absent) →
**interview gantt** (rows link to `/admin/assessments/{id}` when an
assessment exists, each row's `<title>` carrying its `cost_per_interview`
figure, with the unattributed bucket footnoted) → **hub:lab burn
sparkline**. Auto-refresh: a 12-line
inline script `setInterval(() => fetch(location.href).then(r=>r.text()).then(h=>{
const d=new DOMParser().parseFromString(h,'text/html');
document.getElementById('sim-body').innerHTML=d.getElementById('sim-body').innerHTML;}), 30000)`
guarded by a visible pause checkbox; everything dynamic sits inside
`<div id="sim-body">`, and the script + pause checkbox live OUTSIDE it (or
the swap resets the checkbox), with a null guard: when the fetched document
has no `#sim-body` (expired session → login page), stop the interval instead
of throwing.

Mandatory inline caveats (standing rules from the requirements doc): the
`total_api_calls` units note wherever that column shows; single-run-draw
labeling on the KPI row; regime segmentation — the run selector shows each
run's rubric stamp and the page NEVER aggregates across runs.

- [ ] **Step 1: Failing tests:** (a) GET with a seeded run renders the cost
  hero with the hand-computed dollar figure and the cache hit-rate; (b) an
  unpriced model name appears in a visible warning; (c) headlines-owed
  renders the count from a seeded un-stamped assessment; (d) `?run=` switches
  runs (seed two, assert the selected one's figure); (e) empty run renders
  every section without a 500 (the strongest regression net for the SQL);
  (f) the api-call units caveat string is present.
- [ ] **Step 2-4: RED → implement → GREEN.**
- [ ] **Step 5: Ruff + commit** (`feat(sim-stats): Live tab on /admin/simulation`).

---

### Task 12: Docs + full gate

- [ ] **Step 1:** CLAUDE.md: extend the Task-5 subsection with the stats/cost
  page (one short paragraph: where the price table lives + AS_OF discipline,
  the pre-0036 floor semantics, that the heartbeat drives status honesty).
- [ ] **Step 2:** Deploy-notes block at the bottom of THIS plan (already
  written — verify still accurate).
- [ ] **Step 3:** Full `./scripts/ci.sh` ON-HOST → green.
- [ ] **Step 4:** Commit (`docs(sim-control): operator docs for the control panel`,
  files: CLAUDE.md, this plan file, the requirements doc if amended).

---

# PHASE 3 (deferred — separate future plan)

The Analysis tab (agreement statistics, disagreement entropy, calibration
diagram, Kaplan-Meier survival, rater drift, weight sensitivity, cross-run
distributions) is specified in the requirements doc §6 and deliberately NOT
planned here: it is research-grade, dependency-decision-heavy (pure-Python
alpha/kappa/KM implementations), and independent — Phases 1-2 ship working
software without it.

## Deploy notes (operator)

- **Migration `0042`: migrate-before-serve** (Task 1's box). Build all three
  images, migrate from a one-off container, `up -d blackbird-app worker`,
  then — the new step — **`$DC --profile agent up -d agent`** brings up the
  supervisor, which comes up IDLE (boot stales anything pending). Nothing
  starts a run until an admin clicks Start (or runs the CLI emergency path).
- The compose edit (Task 5) is working-tree-only, like the operator's
  standing edits; a fresh clone needs it reapplied (CLAUDE.md documents it).
- Rollback: `$DC --profile agent stop agent` (the `--profile` flag is
  required for compose to resolve the service) returns to the pre-panel
  world; the CLI path is unchanged. Corrected from an earlier draft of this
  note: the page does NOT degrade to "Not deployed" — `derive_panel_state`
  only returns `not_deployed` when `simulation_process_status` has no row at
  all (`status_row is None`), and that row is upserted once and never
  deleted (no code path drops it; only a schema downgrade of `0042` does).
  So once the supervisor has checked in at least once in an environment, a
  plain `stop` instead surfaces as **STALE** once `HEARTBEAT_STALE_SECONDS`
  (120s) passes with no further heartbeat — "Not deployed" is reserved for an
  environment where the supervisor has never run at all. Do not stop it
  while a run is live unless `stop_grace_period: 420s` is in the compose
  file (Task 5).
- Deviations from the spec, declared: non-DB error counters (flush failures,
  poll errors, Slack refusals) are deferred to Phase 3 with the rest of the
  heartbeat-counter work; the v1 confirmation step is the form + refusal
  design (Task 7), not a dialog.
- The price table's `AS_OF` is part of the page; when Anthropic reprices,
  edit `PRICES`/`AS_OF` in `src/services/llm_pricing.py` (web-tier rebuild
  only — the agent image doesn't render costs).
