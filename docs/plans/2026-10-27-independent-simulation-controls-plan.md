# Independent simulation controls — Implementation Plan

> **For agentic workers:** execute with `/lathe:plan-execution`. Task 0 is the shared contract: the lead
> writes it before fan-out. Tasks 1–9 are disjoint-file packages implemented in parallel with **no build and
> no test run**; each Interfaces block is the contract a neighbouring task codes against. Task 10 is the single
> integrated build-and-test pass after merge (the lead runs every "Run" step there). Task 11 is owner-gated
> release work. Steps use checkbox (`- [ ]`) syntax. Implementers do not run the "Run" steps.

**Goal:** Let an admin start and stop the interview system and the projects system separately from
`/admin/simulation`, each with a count and a runtime limit, inside one simulation run, with "Start both", no
spending limit on projects, and a projects session that ends on its own at a set number of finished projects.

**Architecture:** A pure module, `src/agent/sessions.py`, holds which systems are running in the live run
(one *session* per system start) and every rule about limits, wind-down and how the run ends; the engine
keeps one `Sessions` object on `EngineContext`, and every lane asks it before acting. Per-system starts and
stops travel as a new `system` command kind that the live engine claims in its control poll; a start with
no engine alive stays the supervisor's `start`. Migration `0074` adds the enum values `stopped` (a thread an
operator stopped) and `system` (the command kind). The panel gets an Interviews card and a Projects card; the
worker researches only while the live run has Projects running; the research ceiling, the 24-hour project
interval and `HUB_PROJECTS_ENABLED` go.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2 async, Alembic, Postgres 15, Jinja2, Tailwind 3.4.19
(compiled, `scripts/build_css.sh`), pytest + syrupy, Playwright UI harness (`tests/e2e/ui_audit`).

**Spec:** `docs/specs/2026-10-27-independent-simulation-controls-design.md` (approved by the owner
2026-10-27; owner decisions SC1–SC15, designer calls marked *my call*). Code facts below were read at
`blackbird@9439338b` on 2026-10-27.

## Global Constraints

- The interview prompt sets stay frozen: no edit under `prompts/roles/`, no `role.toml` bump, no change to
  `prompts/projects/`, `thread_guidance.py` strings or the rubric.
- `tests/characterization/__snapshots__/test_prompt_freeze_gm.ambr` and `test_agent_turn_gm.ambr` must pass
  **unchanged**: no snapshot regeneration in this plan. In particular `_run_start_announcement_values()` keeps
  reading `max_runtime_minutes` (Task 5 sets it to the planned duration), and a default engine
  (`sessions=None`) still pitches exactly as today.
- One engine at a time; the supervisor boots idle and stales every pending command, the new `system` kind
  included; nothing starts a run on its own; never start a run as part of a deploy.
- Headlines post only through the existing claims (`src/services/headline_claims.py`) and queue
  (`Headlines.enqueue` / `_drain_pending_headlines`); no new posting path.
- `0074` is additive: `ALTER TYPE … ADD VALUE IF NOT EXISTS` only; its downgrade leaves the values.
- Rate limits (`llm_calls_per_load_per_window`, `hub_llm_calls_per_window`) are not changed.
- Engine modules log as `"src.agent.simulation"` and use the `deps` seams
  (`tests/unit/test_engine_import_graph.py`); a unit reaches another unit only along its `ALLOWED` edges.
  `src/agent/sessions.py` sits outside `src/agent/engine/` and imports nothing from the engine.
- Help pages state no number that comes from code or settings (`docs/operations/help-content.md` rule 4): the
  failure stop is "several failures in a row" there, never "3".
- `templates/workspace/activity.html` is not edited (its help screenshot hashes it).
- Commits as `git -c user.name=alan -c user.email=alan@hueb.org commit`; never push.

## Review Focus

1. A per-system command that reaches the supervisor after the run ended (the race in spec §4.3) must be
   finished `stale`, never start an engine: Task 5,
   `test_a_system_command_with_no_live_engine_is_finished_stale`.
2. Projects "Stop now" while the worker holds a research claim: the engine's end wins and the worker's
   compare-and-set write is refused: Task 4, `test_end_open_projects_beats_a_research_write`.
3. A projects-only resume of a held run: the held interview threads get no turns and do not count against a
   lab's `active_thread_threshold` for its project reply: Task 3,
   `test_inactive_interview_threads_get_no_reply_turns`, and Task 4,
   `test_paused_interview_threads_do_not_block_a_project_reply`.
4. A projects session with a count and failing projects never overshoots, refills the failed slot, and winds
   down after 3 failures in a row: Task 4, `test_no_overshoot_and_a_failure_frees_its_slot` and
   `test_three_failures_in_a_row_wind_projects_down`.
5. An Interviews wind-down held open by a leaked thread says which threads it waits for, and "Stop now"
   still ends it: Task 3, `test_a_blocked_wind_down_names_its_threads_once`, and Task 2,
   `test_stop_now_closes_every_open_interview_as_stopped`.

## Files by task

| Task | Owns (create or modify) |
|---|---|
| 0 (lead) | `src/agent/sessions.py`, `src/agent/end_reasons.py`, `src/agent/engine/constants.py`, `src/models/agent_activity.py`, `src/models/simulation_control.py`, `src/models/hub_project.py`, `tests/unit/test_sessions.py`, `tests/unit/test_end_reasons.py` |
| 1 | `alembic/versions/0074_independent_controls.py`, `scripts/migrate/preflight.py`, `tests/integration/test_migration_0074.py`, `tests/unit/test_migration_checks.py`, `tests/integration/test_harness_smoke.py`, `docs/operations/migration-deploy-notes.md` |
| 2 | `src/agent/simulation.py`, `src/agent/engine/context.py`, `src/agent/engine/control.py`, `src/agent/engine/scheduler.py`, `tests/unit/test_engine_import_graph.py`, `tests/unit/test_engine_control_poll.py`, `tests/unit/test_engine_units.py`, `tests/integration/test_stop_end_reasons.py`, `tests/unit/test_resume_pitch_recovery.py`, `tests/integration/test_engine_sessions.py` (new) |
| 3 | `src/agent/engine/post_lane.py`, `src/agent/engine/reply_lane.py`, `src/agent/engine/threads.py`, `tests/unit/test_proposal_limit.py`, `tests/unit/test_post_lane_projects.py`, `tests/unit/test_hub_auto_activation.py`, `tests/unit/test_lane_sessions.py` (new) |
| 4 | `src/agent/engine/projects.py`, `scripts/projects_thread_replay.py`, `tests/integration/test_projects_engine.py`, `tests/unit/test_projects_unit.py`, `tests/integration/test_projects_thread_replay.py`, `tests/integration/test_rebuild_projects.py` |
| 5 | `src/agent/main.py`, `src/agent/supervisor.py`, `tests/unit/test_supervisor.py`, `tests/unit/test_resume_refusal.py`, `tests/unit/test_main_systems.py` (new) |
| 6 | `src/services/project_research.py`, `src/services/runs.py`, `src/config.py`, `.env.example`, `src/services/projects_report.py`, `scripts/projects_report.py`, `tests/integration/test_project_research.py`, `tests/unit/test_project_research.py`, `tests/unit/test_runs_helpers.py`, `tests/integration/test_projects_report.py` |
| 7 | `src/routers/admin/simulation.py`, `templates/admin/simulation.html`, `src/services/simulation_control.py`, `src/services/simulation_stats.py`, `tests/integration/test_admin_simulation_page.py`, `tests/integration/test_admin_simulation_liveness.py`, `tests/integration/test_simulation_confirms.py`, `tests/integration/test_admin_correctness_low.py`, `tests/integration/test_impersonated_writes.py`, `tests/integration/test_finalize_run_route.py`, `tests/unit/test_admin_route_table.py`, `tests/integration/test_spend_by_system.py` (new) |
| 8 | `src/services/directory.py`, `templates/workspace/activity_detail.html`, `src/services/projects_view.py`, `templates/help/pages/discussions-activity.html`, `templates/help/pages/projects.html`, `templates/help/pages/how-it-works.html`, `tests/integration/test_activity_sessions.py` (new), `tests/unit/test_hub_project_models.py` |
| 9 | `CLAUDE.md`, `src/agent/CLAUDE.md`, `docs/operations/host-and-simulation.md`, `docs/operations/blackbird-hub.md`, `docs/specs/2026-10-22-idea-pitching-prototype-design.md`, `docs/specs/2026-10-27-independent-simulation-controls-design.md` |

A test outside its task's list that fails at integration is repaired by the lead in Task 10.

---

### Task 0 (lead, before fan-out): the sessions contract, enum values and end reasons

**Files:** create `src/agent/sessions.py`, `tests/unit/test_sessions.py`; modify `src/agent/end_reasons.py`,
`src/agent/engine/constants.py`, `src/models/agent_activity.py`, `src/models/simulation_control.py`,
`src/models/hub_project.py`, `tests/unit/test_end_reasons.py`.

**Interfaces (produced; every other task consumes these exactly):**

```python
# src/agent/sessions.py — pure: no database, no engine import, no logging needed.
INTERVIEWS = "interviews"; PROJECTS = "projects"; SYSTEMS = (INTERVIEWS, PROJECTS)
SYSTEM_LABELS = {INTERVIEWS: "Interviews", PROJECTS: "Projects"}
RUNNING = "running"; WINDING_DOWN = "winding_down"; ENDED = "ended"          # Session.state
LIMIT_COUNT = "limit_count"; LIMIT_TIME = "limit_time"; WIND_DOWN = "wind_down"
STOP_NOW = "stop_now"; FAILURE_STOP = "failure_stop"; STOP_EVERYTHING = "stop_everything"  # Session.reason
REASON_LABELS = {LIMIT_COUNT: "reached its count", LIMIT_TIME: "reached its runtime",
                 WIND_DOWN: "wound down by hand", STOP_NOW: "stopped by hand",
                 FAILURE_STOP: "stopped after 3 failures in a row", STOP_EVERYTHING: "the run stopped"}
FAILURES_IN_A_ROW_MAX = 3                       # *my call* in the spec (SC13)
SETTLE_CHECKS = {INTERVIEWS: 3, PROJECTS: 1}    # consecutive zero-open observations that end a wind-down

@dataclass(frozen=True)
class Limits:
    count: int = 0            # proposals (interviews) or counted projects (projects); 0 = none
    runtime_minutes: int = 0  # 0 = none
    @classmethod
    def from_mapping(cls, data: object) -> "Limits": ...   # non-mapping, missing, bool, non-int or negative -> 0
    def as_dict(self) -> dict[str, int]: ...               # {"count": …, "runtime_minutes": …}

@dataclass
class Session:
    system: str; limits: Limits; started_at: datetime
    state: str = RUNNING; reason: str | None = None; ended_at: datetime | None = None
    count: int = 0; open_work: int = 0; failures_in_a_row: int = 0; initial: bool = False
    waiting_on: tuple[str, ...] = ()            # lab agent ids holding the open work, as last observed
    def record(self) -> dict[str, object]: ...  # keys: system, state, reason, started_at (ISO), ended_at (ISO|None),
                                                # limits (as_dict), count, open_work, failures_in_a_row, initial,
                                                # waiting_on (list)

@dataclass(frozen=True)
class SessionEvent:
    system: str; kind: str        # "started" | "winding_down" | "ended"
    reason: str | None; initial: bool; cleared_hold: bool

class Sessions:
    hold_carried: bool            # the resumed run's interview hold, not yet taken up (spec §4.9)
    dirty: bool                   # a record changed since mark_clean()
    def __init__(self, *, prior_records: Sequence[Mapping] = (), hold_carried: bool = False) -> None: ...
    @classmethod
    def legacy(cls, *, count: int, runtime_minutes: int, now: datetime) -> "Sessions": ...  # interviews only, initial
    def start(self, system: str, limits: Limits, now: datetime, *, initial: bool = False) -> Session: ...
    def current(self, system: str) -> Session | None: ...   # the system's latest session in this engine
    def active(self, system: str) -> bool: ...              # current state RUNNING or WINDING_DOWN
    def accepting(self, system: str) -> bool: ...           # current state RUNNING
    def winding_down(self, system: str) -> bool: ...
    def any_active(self) -> bool: ...
    def add_count(self, system: str, now: datetime, n: int = 1) -> None: ...
    def observe(self, system: str, *, open_work: int, now: datetime, count: int | None = None,
                failures_in_a_row: int | None = None, waiting_on: Sequence[str] | None = None) -> None: ...
    def wind_down(self, system: str, reason: str, now: datetime) -> bool: ...
    def end(self, system: str, reason: str, now: datetime) -> bool: ...
    def stop_everything(self, now: datetime) -> None: ...
    def run_end_reason(self) -> str: ...
    def pop_events(self) -> list[SessionEvent]: ...
    def detail(self) -> dict[str, dict]: ...       # {system: current.record()} for each system with a session
    def records(self) -> list[dict]: ...           # prior records, then this engine's sessions in start order
    def mark_clean(self) -> None: ...

def systems_from_start_payload(payload: object) -> dict[str, Limits]: ...
def describe_records(records: Sequence[Mapping]) -> list[dict[str, object]]: ...
```

Rules the bodies implement (each pinned by a test in Step 1):

- `start`: `ValueError` for an unknown system or one already active. Starting `INTERVIEWS` while
  `hold_carried` sets it False and the "started" event carries `cleared_hold=True`. Emits ("started").
- `add_count` adds while the session is active; reaching `limits.count` (when > 0) while RUNNING winds it
  down at once with `LIMIT_COUNT`, so no pitch slips past the cap between observations.
- `observe` on a RUNNING session stores `count` / `failures_in_a_row` when given, then checks, in order:
  `limits.count > 0 and count >= limits.count` → `LIMIT_COUNT`; `limits.runtime_minutes > 0` and
  `now - started_at >= runtime` → `LIMIT_TIME`; `system == PROJECTS and failures_in_a_row >=
  FAILURES_IN_A_ROW_MAX` → `FAILURE_STOP` (`wind_down`). Then, on a WINDING_DOWN session (including one that
  just started winding down): `open_work == 0` adds one to its settle streak and ends the session with its
  wind-down reason at `SETTLE_CHECKS[system]`; `open_work > 0` resets the streak. A session absent or ENDED:
  no-op. Stores `open_work` and, when given, `waiting_on` (sorted, de-duplicated).
- `wind_down`: RUNNING → WINDING_DOWN with `reason`; False otherwise. `end`: active → ENDED with `reason` and
  `ended_at`; False otherwise. Both emit their event. `stop_everything` ends every active session with
  `STOP_EVERYTHING`.
- `run_end_reason` (the `end_reasons` vocabulary): `"held_over"` when `hold_carried`; else by the reason of the
  session that ended last: `LIMIT_COUNT` → `"target_drained"`, `LIMIT_TIME` → `"time_limit"`,
  `FAILURE_STOP` → `"failure_stop"`, anything else or none → `"operator"`.
- `__init__` normalises prior records: a record whose `state` is not `ended` (a process that died before
  persisting) is kept with `state="ended"`, `reason=STOP_EVERYTHING`.
- Every transition and every `observe`/`add_count` that changes a record sets `dirty`.
- `systems_from_start_payload`: `payload["systems"]` → `{name: Limits.from_mapping(v)}` for known names only;
  with no `"systems"` key, the legacy shape `{"max_runtime", "max_proposals"}` →
  `{INTERVIEWS: Limits(max_proposals, max_runtime)}`; anything else → `{}`.
- `describe_records` → one row per record: `system` (`SYSTEM_LABELS`), `started_at`/`ended_at` (aware
  `datetime` or None, parsed from ISO), `limits` (`"20 proposals, 240 minutes"`, `"10 finished projects"`,
  `"no limit"`), `outcome` (`REASON_LABELS[reason]` when ended, `"running"`, or `"winding down"`).

Other files:

- `src/agent/end_reasons.py`: `END_REASON_CLASS` gains `"failure_stop": TODAY` and `"held_over": HOLD`;
  the module docstring names both (a projects failure stop announces like a Stop; a run whose interview hold
  no session took up ends held).
- `src/agent/engine/constants.py`: `STOPPED_OUTCOME = "stopped"` (a thread an operator's Stop now ended),
  added to `__all__`.
- `src/models/agent_activity.py`: `thread_outcome_enum` gains `"stopped"`; the comment above it says what it
  means (migration 0074).
- `src/models/simulation_control.py`: `sim_command_enum` gains `"system"`; the payload comment documents
  `start` `{"fresh": bool, "systems": {"interviews"|"projects": {"count": int, "runtime_minutes": int}}}`
  (the legacy `{"fresh", "max_runtime", "max_proposals"}` still read) and `system`
  `{"action": "start"|"wind_down"|"stop_now", "system": …, "run_id": "<uuid>", "limits": {…}}` (`limits` on
  `start` only).
- `src/models/hub_project.py`: `REASON_STOPPED = "stopped"` in `REASONS`.

- [ ] **Step 1: Write `tests/unit/test_sessions.py`** — one test per rule above, at least:
  `test_start_twice_or_unknown_raises`, `test_add_count_winds_down_at_the_cap`,
  `test_runtime_winds_down`, `test_three_failures_wind_projects_down_and_interviews_ignore_failures`,
  `test_interviews_end_after_three_quiet_observations_and_a_busy_one_resets`,
  `test_projects_end_on_the_first_quiet_observation`, `test_hand_wind_down_and_stop_now`,
  `test_stop_everything_ends_every_active_session`,
  `test_run_end_reason_follows_the_last_session_and_a_carried_hold`,
  `test_events_pop_once_and_carry_initial_and_cleared_hold`,
  `test_records_append_to_prior_and_normalise_unfinished_ones`,
  `test_limits_from_mapping_rejects_garbage`, `test_start_payload_new_and_legacy_shapes`,
  `test_describe_records_rows`. Example:

```python
def test_run_end_reason_follows_the_last_session_and_a_carried_hold():
    s = Sessions()
    s.start(INTERVIEWS, Limits(), T0, initial=True)
    s.start(PROJECTS, Limits(count=2), T0, initial=True)
    s.end(INTERVIEWS, STOP_NOW, T0 + MIN)
    s.observe(PROJECTS, open_work=0, now=T0 + 2 * MIN, count=2)
    assert not s.any_active() and s.run_end_reason() == "target_drained"
    held = Sessions(hold_carried=True)
    assert held.run_end_reason() == "held_over"
```

- [ ] **Step 2: Update `tests/unit/test_end_reasons.py`** for the two new reasons and their classes.
- [ ] **Step 3: Implement** the module and the five small edits above.

---

### Task 1: migration `0074` and the migration tooling

**Files:** listed in the table. **Consumes:** the enum values of Task 0.

- [ ] **Step 1:** `alembic/versions/0074_independent_controls.py` (revision `"0074"`, down `"0073"`):
  `upgrade()` runs `ALTER TYPE thread_outcome_enum ADD VALUE IF NOT EXISTS 'stopped'` and
  `ALTER TYPE sim_command_enum ADD VALUE IF NOT EXISTS 'system'`; `downgrade()` does nothing (Postgres cannot
  drop an enum value). The docstring, in the shape of `0073`'s: old code on the new schema is safe, except a
  rollback after either value was written (a pre-0074 ORM cannot map a `stopped` decision or a `system`
  command: the old engine's rebuild swallows the error into an empty closed set, and the old panel's command
  list 500s); new code on the old schema fails (`InvalidTextRepresentation` on a `stopped` decision, which
  `Threads._record_decision` re-queues; the panel's per-system routes 500). Migrate before the new images; the
  agent image imports `src.models`.
- [ ] **Step 2:** `scripts/migrate/preflight.py`: `DEFAULT_TARGET = "0074"`, `"0073"` joins
  `SUPPORTED_START_REVISIONS`, `REVISION_ORDER` ends `"0074"`, and the comment above
  `SUPPORTED_START_REVISIONS` adds that 0074's two ADD VALUEs enumerate nothing (as 0063's and 0073's).
- [ ] **Step 3:** tests: `tests/unit/test_migration_checks.py` pins `DEFAULT_TARGET == "0074"` and adds
  `test_0074_plans_no_objects` (`pf.planned_objects_between("0073", "0074")` is empty);
  `tests/integration/test_harness_smoke.py` expects head `"0074"`;
  `tests/integration/test_migration_0074.py` (copy `test_migration_0073.py`'s loader): both values exist at
  head, `downgrade()` then `upgrade()` again succeeds and keeps them, and a `ThreadDecision(outcome="stopped")`
  and a `SimulationCommand(command="system")` round-trip through the ORM.
- [ ] **Step 4:** `docs/operations/migration-deploy-notes.md`: a box opening "**Deploy order for
  `0074_independent_controls`**" after the `0073` box: what it adds, the two compatibility directions from
  Step 1, rebuild all three images, migrate before `$DC up -d blackbird-app worker`, recreate `agent` only with
  no live run, rollback tags `rollback-pre-0074`, before/after checks (`SELECT version_num FROM
  alembic_version;` reads `0073`, then `0074`; `SELECT enum_range(NULL::sim_command_enum);` lists `system`).

---

### Task 2: the orchestrator, the control poll, the context and the scheduler

**Files:** listed in the table. **Consumes:** Task 0; Task 3's `PostLane._observe_interview_session()` and
`PostLane._open_interview_ids()` (project threads excluded); Task 4's `Projects._projects_tick()` (no
arguments) and `Projects.end_open_projects(reason: str) -> set[str]`; `Threads._close_thread` as today.
**Produces:** `SimulationEngine(…, sessions: Sessions | None = None)`, `EngineContext(…, sessions=None)` with
`ctx.sessions`, `Control.persist_sessions()`.

- [ ] **Step 1: Tests.** In the new `tests/integration/test_engine_sessions.py` (engine built as the existing
  engine tests build one, with a database):
  - `test_the_loop_ends_target_drained_when_the_only_session_reaches_its_count` (interviews,
    `Limits(count=1)`: one booked pitch, no open threads → after three ticks `request_stop` reason is
    `"target_drained"`);
  - `test_the_loop_ends_operator_after_a_hand_stop` and `test_a_carried_hold_ends_held_over`;
  - `test_stop_now_closes_every_open_interview_as_stopped` (two open interview threads, one holding an
    unannounced verdict → both get a `thread_decisions` row with outcome `stopped`, both leave
    `active_threads`, the held verdict's thread is in `_pending_headlines`, and no working-memory event is
    queued);
  - `test_projects_stop_now_closes_the_threads_end_open_projects_returns`;
  - `test_an_interviews_join_runs_the_pitch_catch_up_and_clears_the_hold` (`_recover_reply_less_pitches`
    awaited once; `simulation_runs.held_at` NULL);
  - `test_stop_marks_active_sessions_and_persists_them` (`config["sessions"]` holds a `stop_everything`
    record).
  In `tests/unit/test_engine_control_poll.py`: a pending `system` command for this run is claimed and applied
  (`start` → session active, `wind_down`, `stop_now`) and finished `done`; one for another run is finished
  `stale`; a start of an active system and a stop of an inactive one are finished `failed` with the reason;
  the upserted `detail` carries `"systems"`; a dirty `Sessions` is written to `simulation_runs.config`
  `["sessions"]` and marked clean. Update `tests/unit/test_engine_units.py`,
  `tests/integration/test_stop_end_reasons.py` and `tests/unit/test_resume_pitch_recovery.py` for the removed
  `is_within_time_limit`, the session-driven loop end and the catch-up gate (resume runs it only with
  Interviews active). In `tests/unit/test_engine_import_graph.py`: `EXPECTED_METHOD_OWNER` gains
  `_apply_session_events`/`_close_threads` (orchestrator), `_observe_interview_session` (post_lane),
  `end_open_projects`/`_wind_down_unstarted`/`_observe_session` (projects),
  `persist_sessions`/`_persist_sessions`/`_apply_system_command` (control) and loses `is_within_time_limit`
  and `_proposal_target_drained`; `EXPECTED_STATE_OWNER` loses `max_proposals` and `_proposal_drain_streak`
  (post_lane) and `_enabled` (projects) and gains `_wind_down_warned` (post_lane) and `_lab_offsets`,
  `_offsets_started_at` (projects); `ORCHESTRATOR` gains `_apply_session_events` and `_close_threads`.
- [ ] **Step 2: `src/agent/engine/context.py`:** `EngineContext.__init__` takes `sessions: Sessions | None =
  None` and stores `self.sessions = sessions if sessions is not None else Sessions.legacy(count=0,
  runtime_minutes=0, now=deps.datetime.now(UTC))` (a bare context behaves as today: interviews on, projects
  off; `deps`, never the bare `datetime`, per `test_patch_seam_is_used_everywhere`).
- [ ] **Step 3: `src/agent/engine/scheduler.py`:** delete `is_within_time_limit` (sessions own runtime
  limits); keep `max_runtime_minutes` and `_start_time` (the run-start announcement reads them).
- [ ] **Step 4: `src/agent/engine/control.py`:** in `_poll_control_plane`, after the stop branch, claim at
  most one pending `system` command and finish it with `_apply_system_command(cmd) -> tuple[str, str]`
  (status, result): `run_id` ≠ this run → `("stale", "for run <id>, not this one")`; `action == "start"` →
  `ctx.sessions.start(system, Limits.from_mapping(payload["limits"]), now)` or
  `("failed", "<System> is already running")`; `wind_down` → `wind_down(system, WIND_DOWN, now)`;
  `stop_now` → `end(system, STOP_NOW, now)`, each `("failed", "<System> is not running")` when it returns
  False; an unknown action or system → `("failed", …)`. `detail` gains `"systems": ctx.sessions.detail()`.
  When `ctx.sessions.dirty`, `_persist_sessions(db)` writes `config = {**config, "sessions":
  ctx.sessions.records()}` on this run's `simulation_runs` row (reassign, never mutate in place) and calls
  `mark_clean()`. `persist_sessions()` opens its own session and does the same; both never raise (one
  warning).
- [ ] **Step 5: `src/agent/simulation.py`:**
  - `__init__(…, sessions: Sessions | None = None)`: None → `Sessions.legacy(count=max_proposals,
    runtime_minutes=max_runtime_minutes, now=deps.datetime.now(UTC))`; pass it to `EngineContext`; stop
    passing `max_proposals` to `PostLane`.
  - `start()`: `_recover_reply_less_pitches()` only when `not self._fresh_start and
    self.ctx.sessions.active(INTERVIEWS)`.
  - `_run_main_loop()`: `while self._running and self.ctx.sessions.any_active():`; right after
    `_poll_control_plane`: `await self._apply_session_events()`, then `await self._projects_tick()`, then
    `self._observe_interview_session()`. At the end: `if self._running: self.request_stop("stall" if stalled
    else self.ctx.sessions.run_end_reason())`.
  - `async def _apply_session_events(self) -> None` handles `ctx.sessions.pop_events()`: an `INTERVIEWS`
    "started" that is not `initial` → when `cleared_hold`, set this run's `held_at` to NULL, then
    `await self._recover_reply_less_pitches()`; an "ended" with reason `STOP_NOW` → `INTERVIEWS`:
    `await self._close_threads(<every active, non-project thread id in any agent's active_threads>,
    STOPPED_OUTCOME)`; `PROJECTS`: `await self._close_threads(await self.projects.end_open_projects(
    REASON_STOPPED), STOPPED_OUTCOME)`. Never raises (logs).
  - `async def _close_threads(self, thread_ids: set[str], outcome: str) -> None`: for each id held by some
    agent, under `self._thread_locks.acquire_all(thread_id)`, `await self._close_thread(agent, thread,
    outcome)` once (it closes both sides).
  - `stop()`: first `self.ctx.sessions.stop_everything(deps.datetime.now(UTC))`; after
    `_record_run_end_state`, `await self.control.persist_sessions()`.
  - `_record_run_end_state`: a TODAY end of a run whose hold was carried (`ctx.sessions.hold_carried`) also
    sets `held_at` to NULL, since that sweep has just announced the held interviews (a resume used to clear it
    at start).
  - Headlines of interviews a Stop now closes post through the per-tick drain, all of them: the
    `HEADLINES_MAX_AT_SHUTDOWN` bound exists for a stop's grace period, and here the run goes on. (The spec's
    §4.6 said "at most 25 per stop as today"; this plan posts all, flagged to the owner.)

---

### Task 3: the post lane, the reply lane and closes

**Files:** listed in the table. **Consumes:** Task 0 (`ctx.sessions`, `STOPPED_OUTCOME`); Task 4's
`Projects._offer_project_reply(agent) -> bool` (no `draining`). **Produces:**
`PostLane._observe_interview_session() -> None`; `_open_interview_ids()` without project threads.

- [ ] **Step 1: Tests.** Rewrite `tests/unit/test_proposal_limit.py` for sessions: a lab's Phase 5 makes no
  model call once the interviews session is not accepting (cap reached, wound down, ended, or absent), and
  pitches as today while accepting; `_book_pitch` counts against the session and winds it down at the cap;
  a zero count never stops pitches; `_observe_interview_session` ends a winding-down session after three
  quiet observations; an orphaned thread does not block it; a project thread is not an open interview;
  `test_a_blocked_wind_down_names_its_threads_once` (one WARNING with the thread ids per blocked
  wind-down, again only after it clears). Update `tests/unit/test_post_lane_projects.py` (no `draining`
  argument, no proposal-cap drain). In `tests/unit/test_hub_auto_activation.py`: a lab pitch is not
  activated while Interviews is inactive, a project-reply post is not activated while Projects is inactive,
  each is activated while its system is active. New `tests/unit/test_lane_sessions.py`:
  `test_inactive_interview_threads_get_no_reply_turns`, the same for project threads, and
  `test_a_stopped_close_queues_no_memory_event` (`_close_thread(…, STOPPED_OUTCOME)` writes the decision,
  queues the owed headline, queues no working-memory event).
- [ ] **Step 2: `src/agent/engine/post_lane.py`:**
  - `__init__` loses `max_proposals` and the state `max_proposals`, `_proposal_drain_streak`,
    `_drain_block_warned`; gains `_wind_down_warned: bool` (in `OWNED_STATE`).
  - `_phase5_new_post`: `self._projects._offer_project_reply(agent)`; then `if not
    self.ctx.sessions.accepting(INTERVIEWS): return` (debug log, no model call) before the daily cap; the
    proposal-cap block goes.
  - `_book_pitch`: when it counts the pitch, also `self.ctx.sessions.add_count(INTERVIEWS,
    deps.datetime.now(UTC))`.
  - `_rehydrate_proposal_count`: always counts (drop the `max_proposals` early return); the docstring says
    the count is now for logs and tests only.
  - `_open_interview_ids`: also skips `self.message_log.is_project_thread(tid)`.
  - Replace `_proposal_target_drained` with `_observe_interview_session(self) -> None`:
    `ctx.sessions.observe(INTERVIEWS, open_work=self._open_interview_count(), now=deps.datetime.now(UTC),
    waiting_on=<agent ids of the spoke-role participants of the open interview threads>)`;
    while the session is winding down with open work, one WARNING per episode naming the open thread ids
    (today's drain-block wording, "the interviews wind-down cannot finish until they do; Stop now ends
    them").
  - `_auto_activate_lab_posts`: per entry, `project = self.message_log.is_project_thread(thread_id)`; skip it
    when its system (`PROJECTS` if `project` else `INTERVIEWS`) is not `ctx.sessions.active(…)`.
- [ ] **Step 3: `src/agent/engine/reply_lane.py`:** `_pending_reply_pairs` skips a thread whose system
  (project thread → `PROJECTS`, else `INTERVIEWS`) is not active.
- [ ] **Step 4: `src/agent/engine/threads.py`:** `_close_thread` queues no working-memory event when
  `outcome == STOPPED_OUTCOME` (docstring: an operator's stop is not something the agents lived through); the
  decision row, the closed set, the prior-thread summary and the headline queue are unchanged.

---

### Task 4: the projects unit

**Files:** listed in the table. **Consumes:** Task 0. **Produces:** `Projects._projects_tick(self) -> None`,
`Projects._offer_project_reply(self, agent) -> bool`, `Projects.end_open_projects(self, reason: str) ->
set[str]`, module constant `FAILURE_REASONS`.

- [ ] **Step 1: Tests** (`tests/integration/test_projects_engine.py`; its `_settings` helper drops
  `hub_projects_enabled` and `project_interval_hours`, and engines get a projects session through
  `sessions=`):
  - `test_every_live_lab_is_requested_when_projects_start` and
    `test_a_labs_next_project_is_requested_when_its_last_finishes` (no interval);
  - `test_a_lab_one_ahead_waits_for_the_others` and `test_a_lab_live_mid_session_starts_at_the_minimum`;
  - `test_no_overshoot_and_a_failure_frees_its_slot` (count 3, four live labs → 3 requested; one ends
    `choice_failed` → one more requested; counted + open never above 3);
  - `test_three_failures_in_a_row_wind_projects_down` (`FAILURE_STOP`), and
    `test_a_counted_project_resets_the_streak_and_excluded_reasons_are_skipped`;
  - `test_count_reached_ends_the_session` (`LIMIT_COUNT`, ended on the same tick when nothing is open);
  - `test_wind_down_ends_unstarted_projects_only` (`requested` → `run_ended_before_posting`; `researching`
    and later carry on; the session ends when none is open);
  - `test_end_open_projects_ends_every_open_status_as_stopped_and_returns_their_threads`;
  - `test_end_open_projects_beats_a_research_write` (after it, `project_research._move(db, id,
    "researching", …)` returns False);
  - `test_inactive_projects_request_post_and_offer_nothing` and `awaiting replies count 0`;
  - `test_paused_interview_threads_do_not_block_a_project_reply` (a lab holding
    `active_thread_threshold` open interview threads while Interviews is inactive still gets its reply
    offered).
  Update `tests/unit/test_projects_unit.py`, `tests/integration/test_projects_thread_replay.py` and
  `tests/integration/test_rebuild_projects.py` for the removed switch, interval and `draining` arguments.
- [ ] **Step 2: `src/agent/engine/projects.py`:**
  - Drop `_enabled` and every read of `hub_projects_enabled`; the module docstring says the Projects session
    (`ctx.sessions`) gates requests, postings, replies and the reply count, and never how a project thread is
    recognised or ended.
  - `__init__` gains `_lab_offsets: dict[str, int]` and `_offsets_started_at: datetime | None`
    (`OWNED_STATE`).
  - `_projects_tick(self)`: cadence and the clock as today; steps in order: advance the clock, resolve
    claims, `_wind_down_unstarted` (only while winding down), `_end_stale_projects(db)` (its `draining`
    branch goes), `_request_due_labs` (only while `accepting(PROJECTS)`), `_post_ready_projects` (only while
    `active(PROJECTS)`), `_count_awaiting_replies`, `_observe_session` (only while active). One warning per
    failed step, as today.
  - `_request_due_labs(db)` — the session's request rule (SC5, SC6, the spread):

    ```text
    s = sessions.current(PROJECTS); since = s.started_at
    if self._offsets_started_at != since: self._lab_offsets = {}; self._offsets_started_at = since
    live = sorted live labs; started[lab] = the run's projects of lab with requested_at >= since
    for lab in live without an offset:
        known = [started[l] + self._lab_offsets[l] for l in live if l in self._lab_offsets]
        self._lab_offsets[lab] = max(0, min(known) - started[lab]) if known else 0
    eff[lab] = started[lab] + self._lab_offsets[lab]; floor = min(eff over live)
    counted = the run's projects finished ready/not_agreed with finished_at >= since
    budget = None if s.limits.count == 0 else s.limits.count - counted - (the run's open projects)
    for lab in live: stop when budget is not None and budget <= 0; skip a lab with an open project
        or eff[lab] > floor; insert slot max+1 as today (on conflict nothing); budget -= 1
    ```
  - `_wind_down_unstarted(db)`: compare-and-set every `requested` project of the run to `not_finished` /
    `run_ended_before_posting`.
  - `_end_refining_projects`: a `stopped` decision maps to `REASON_STOPPED`.
  - `_count_awaiting_replies`: 0 while Projects is not active.
  - `_offer_project_reply(self, agent)`: False with no model call while Projects is not active;
    `draining = ctx.sessions.winding_down(PROJECTS)` replaces the argument.
  - `_open_partnered_thread_count(agent)`: counts only threads whose system is active.
  - `FAILURE_REASONS = (no_idea, research_failed, choice_failed, reply_failed, thread_timeout,
    thread_abandoned, close_unreadable)`; `_observe_session(db)`: `counted` as above, `open_work` = the run's
    open projects, `failures_in_a_row` = walking the session's finished projects newest `finished_at` first,
    count reasons in `FAILURE_REASONS` until the first counted one, skipping any other reason; then
    `ctx.sessions.observe(PROJECTS, count=…, open_work=…, failures_in_a_row=…,
    waiting_on=<agent ids of the open projects>, now=deps.datetime.now(UTC))`.
  - `async def end_open_projects(self, reason: str) -> set[str]`: compare-and-set every open project of the
    run, status by status, to `not_finished` with `reason`; return the `reply_ts` of the ended ones that had
    a project thread. Never raises.
- [ ] **Step 3: `scripts/projects_thread_replay.py`:** drop the `hub_projects_enabled` override;
  `build_replay_engine` passes `sessions=` holding one initial `PROJECTS` session with
  `Limits(runtime_minutes=max_minutes)`.

---

### Task 5: the entry point and the supervisor

**Files:** listed in the table. **Consumes:** Task 0; `SimulationEngine(…, sessions=…)` (Task 2).
**Produces:** `_run_simulation(max_runtime, budget, mock, no_db, fresh, reset_cursors=False,
all_agents=False, max_proposals=0, *, systems: Mapping[str, Limits] | None = None)`.

- [ ] **Step 1: Tests.** `tests/unit/test_supervisor.py`: a `start` with `{"fresh": True, "systems":
  {"projects": {"count": 5}}}` calls `run_fn` with positional interview limits 0/0 and `systems=` holding
  projects; a legacy payload still passes `max_runtime`/`max_proposals` positionally and `systems=` with
  interviews; a `start` whose payload yields no system is finished `failed`;
  `test_a_system_command_with_no_live_engine_is_finished_stale` (result "no run is live — start it again",
  `run_fn` never called); a `system` command beside a live engine (seeded lock) is left pending; boot stales
  a pending `system` command. `tests/unit/test_resume_refusal.py`: `_reopen_run_for_resume(run,
  starts_interviews=…)` refuses a finalized run, keeps `held_at` and returns True when Interviews does not
  start on a held run, clears it and returns False when it does, and no longer inherits `max_proposals`.
  New `tests/unit/test_main_systems.py`: `--systems interviews,projects` with the four limit options builds
  the right `Limits`; an unknown system name exits with an error; `_run_simulation` raises `ValueError` for
  no system; the planned duration is 0 when any started system has no runtime, else the largest.
- [ ] **Step 2: `src/agent/main.py`:**
  - CLI: `--systems` (default `"interviews"`, comma-separated subset of `interviews,projects`),
    `--max-projects` (default 0) and `--projects-runtime` (minutes, default 0); `--max-runtime` and
    `--max-proposals` keep their names and become the interviews limits (help texts say so).
  - `_run_simulation(…, *, systems=None)`: None → `{INTERVIEWS: Limits(max_proposals, max_runtime)}`; empty
    → `ValueError("no system to start")`; the heartbeat's first `tick_detail` is `{"fresh": fresh,
    "systems": {name: {"state": "starting", "limits": l.as_dict()}}}`.
  - `_open_or_resume_run(…, systems)` returns `(session_factory, run_id, hold_carried, prior_records)`;
    `run_config` keeps `max_runtime` (the planned duration) and `max_proposals` (the interviews count or 0)
    and adds `"systems": {name: l.as_dict()}`; a resume calls `_reopen_run_for_resume(run,
    starts_interviews=INTERVIEWS in systems)` and reads `prior_records = run.config.get("sessions") or []`.
  - `_reopen_run_for_resume(run, *, starts_interviews: bool) -> bool`: `RunFinalized` as today; the
    rubric warning as today; `status="running"`, `ended_at=None`; `held_at=None` only when
    `starts_interviews`; returns `run.held_at was set and not starts_interviews`; no `max_proposals`
    inheritance.
  - `_run_simulation_locked`: build `Sessions(prior_records=…, hold_carried=…)`, start each system with
    `initial=True` at one `now`, and pass `sessions=` and `max_runtime_minutes=<planned duration>` to the
    engine (no `max_proposals`); the runtime label lists each system's limits. `--no-db`: no prior records,
    no hold.
- [ ] **Step 3: `src/agent/supervisor.py`:** read the start with `systems_from_start_payload`; none →
  finish `failed` "no system to start"; the `starting` detail is `{"fresh": …, "systems": …}` as above; call
  `run_fn(<interviews runtime or 0>, 0, False, False, fresh, False, False, <interviews count or 0>,
  systems=systems)`. Each loop also claims a pending `system` command: with an engine alive,
  `await db.rollback()` (the engine owns it); with none, finish it `stale` with "no run is live — start it
  again". The module docstring names the `system` kind.

---

### Task 6: the worker gate, the settings and the report

**Files:** listed in the table. **Consumes:** Task 0 (`PROJECTS`, `RUNNING`, `WINDING_DOWN`).
**Produces:** `live_projects_run_id(db, *, now=None) -> uuid.UUID | None` (`src/services/runs.py`),
`research_unavailable_reason() -> str | None` (`src/services/project_research.py`).

- [ ] **Step 1: Tests.** `tests/unit/test_runs_helpers.py`: `live_projects_run_id` is the live run only
  while its heartbeat `detail["systems"]["projects"]["state"]` is `running` or `winding_down`, and None for a
  stale heartbeat, no `systems`, or projects `ended`/`starting`. `tests/integration/test_project_research.py`:
  the ceiling tests go; new: research runs past $25 of ledger spend in 24 hours; nothing is claimed while
  the live run's projects are not running. `tests/unit/test_project_research.py`:
  `research_unavailable_reason()` names a switched-off research, an unpriced model and a missing prompt, and
  is None otherwise. `tests/integration/test_projects_report.py`: the missed-interval assertions go.
- [ ] **Step 2: `src/services/runs.py`:** `live_projects_run_id` as above (reuse `live_run_id`'s staleness
  rule).
- [ ] **Step 3: `src/services/project_research.py`:** `_claim` uses `live_projects_run_id` and no spend
  check; `_Config` loses `limit`; `spend_24h` and `_WINDOW` go if nothing else uses them; the module
  docstring says there is no dollar ceiling (owner, 2026-10-27) and every call is still claimed in the
  ledger; `research_unavailable_reason()` returns the same three conditions `_config()` checks, worded for
  an admin, and `_config()` uses it.
- [ ] **Step 4: `src/config.py` and `.env.example`:** remove `hub_projects_enabled`,
  `project_research_daily_usd_limit` and `project_interval_hours`, their entries in the finite-positive
  validation tuple, and their comment lines; rewrite the projects comment (projects start from
  `/admin/simulation`; no ceiling; `PROJECT_RESEARCH_ENABLED` stops research alone). Settings ignore unknown
  environment variables (`extra="ignore"`), so an old `.env` line is harmless.
- [ ] **Step 5: `src/services/projects_report.py` and `scripts/projects_report.py`:** drop
  `_missed_intervals`, `MISSED_*`, the interval and the report keys `interval_hours`, `missed_intervals`,
  `missed_by_reason`; the script's lab table drops its "Missed intervals" column and the header its
  interval.

---

### Task 7: the admin panel

**Files:** listed in the table. **Consumes:** Task 0; Task 6's `research_unavailable_reason()`; the
heartbeat `detail["systems"]` shape (Task 0 `Session.record()`, Task 5's `starting` form).
**Produces:** `POST /admin/simulation/start` (new fields), `POST /admin/simulation/system`,
`systems_view(status_row, *, engine_is_alive: bool) -> dict[str, dict]`, `spend_by_system(db, run_id) ->
dict[str, Decimal]`.

- [ ] **Step 1: Tests.** `tests/integration/test_admin_simulation_page.py` (existing tests updated to the
  new form, plus): idle → both cards with their inputs, Start both, Fresh; idle start payloads for
  `system=interviews|projects|both` (`{"fresh", "systems": {…}}`, `count`/`runtime_minutes` from the
  fields); live (engine lock held, heartbeat `running` with `systems`) → Start of an inactive system
  enqueues `system` `{"action": "start", "system", "run_id": <live run>, "limits"}`; refusals: both while
  live, a running or winding-down system, a stopping engine, a pending `system` command, projects while
  `research_unavailable_reason()` is set, an unknown system; `/admin/simulation/system` enqueues `wind_down`
  and `stop_now` for an active system and refuses with nothing running, an inactive system, wind-down of a
  winding-down one, an unknown action; `test_stop_everything_is_not_blocked_by_a_pending_system_command`;
  each request writes its audit row (`simulation_start_requested`, `simulation_system_start_requested`,
  `simulation_system_stop_requested`). `tests/unit/test_admin_route_table.py` lists the new route. Adjust
  `test_admin_simulation_liveness.py`, `test_simulation_confirms.py`, `test_admin_correctness_low.py`,
  `test_impersonated_writes.py` and `test_finalize_run_route.py` where they post the old start form. New
  `tests/integration/test_spend_by_system.py`: research ledger dollars and project-thread, project-reply and
  project-thread consult calls count as Projects; every other call of the run as Interviews.
- [ ] **Step 2: `src/services/simulation_control.py`:** `systems_view` → `{system: {"state", "since",
  "limits", "count", "open_work", "failures_in_a_row", "reason_label"}}` with `state` one of
  `not_running`, `starting`, `running`, `winding_down`, `stopped` (an `ended` session of the live run;
  `reason_label` from `REASON_LABELS`); no engine alive → every system `not_running`; a live heartbeat with
  no `systems` (an older engine) → interviews `running`, projects `not_running`.
- [ ] **Step 3: `src/services/simulation_stats.py`:** `spend_by_system`: Projects = the run's
  `hub_project_calls` priced with `project_research.ledger_row_cost` plus its `llm_call_logs` rows with
  `phase` in (`project_reply`, `project_thread`) or `thread_ts` among the run's `hub_projects.reply_ts`;
  Interviews = every other priced `llm_call_logs` row of the run.
- [ ] **Step 4: `src/routers/admin/simulation.py`:**
  - `_simulation_context` adds `systems` (`systems_view`), `system_spend` (live run only),
    `research_unavailable` and `live_run_id`.
  - `admin_simulation_start(request, system: str = Form("interviews"), fresh, max_runtime, max_proposals,
    max_projects: int = Form(0, ge=0), projects_runtime: int = Form(0, ge=0))`: interviews use
    `max_proposals`/`max_runtime`, projects `max_projects`/`projects_runtime`. With no engine alive: today's
    checks, locks and audit, payload `{"fresh": fresh, "systems": {…}}`, flash "Start requested.". With an
    engine alive: the `system` command above; flash "Start interviews requested." / "Start projects
    requested."; refusal texts: "Start both applies only when no run is live.", "The run is stopping.",
    "Interviews are already running." / "Projects are already running.", "Another interviews or projects
    request is pending; try again in a moment.", the research reason.
  - New `admin_simulation_system(request, system: str = Form(...), action: str = Form(...))` at
    `POST /admin/simulation/system`, `action` in (`wind_down`, `stop_now`): refusals "Nothing is running.",
    "Interviews are not running." / "Projects are not running.", "Projects are already winding down.", the
    pending text; flash "Wind down projects requested." / "Stop projects now requested." (and interviews).
  - `admin_simulation_stop` is unchanged.
- [ ] **Step 5: `templates/admin/simulation.html`:** `#sec-controls` per the spec §4.2: one form posting to
  `/admin/simulation/start` holds the run strip's Fresh checkbox (shown only when nothing is live; today's
  finalized-run variant kept) and both cards' inputs, with submit buttons `name="system"` valued
  `interviews`, `projects` and `both` ("Start interviews", "Start projects", "Start both"; Start both only
  when nothing is live). The Interviews card's `max_runtime` input comes before its `max_proposals` input
  (`tests/e2e/ui_audit/journeys_phase0.py` reads them in DOM order); the Projects card's inputs are
  `projects_runtime` then `max_projects`, labelled "Max runtime (minutes, 0 = indefinite)" and "Finished
  projects (0 = no limit)". With a run live, the run strip shows its short id and start time. A card shows
  its state chip, its progress (Interviews: proposals this session of N, open interviews, time used;
  Projects: finished of N, open, failures in a row and the stop threshold, from `systems`; spending from
  `system_spend`), "Waiting on: <labs>" while winding down (`waiting_on`), and, while active, its own forms
  posting to
  `/admin/simulation/system` (hidden `system` and `action`): "Wind down" and "Stop now" (Stop now asks first:
  `data-confirm` naming what ends). The run strip's two `/admin/simulation/stop` forms are always rendered,
  disabled with no engine alive, with today's markup, `hold_open` input and `data-confirm` text; their
  labels become "Stop everything" and "Stop everything, hold open interviews". The engine status card's
  Max runtime / Max proposals lines become one line per system from `status_row.detail.systems`. Keep the
  section a refresh unit with id `sec-controls`; labels on every control; no class from
  `tests/unit/test_contrast_classes.py::FORBIDDEN`.

---

### Task 8: the run's sessions on the Activity page, the new reason, and help

**Files:** listed in the table. **Consumes:** Task 0 (`describe_records`, `REASON_STOPPED`).

- [ ] **Step 1: Tests.** New `tests/integration/test_activity_sessions.py`: a run whose `config["sessions"]`
  holds two records shows a "Sessions" table on `/workspace/activity/<id>` with each system, its limits and
  its outcome; a run without sessions shows none. `tests/unit/test_hub_project_models.py`: every code in
  `REASONS` has a `projects_view.REASON_LABELS` entry.
- [ ] **Step 2:** `src/services/directory.py`: `build_run_detail` adds `"sessions":
  describe_records(run.config.get("sessions") or [])`. `templates/workspace/activity_detail.html`: a
  "Sessions" card after the summary cards when `sessions` is non-empty (System, Started, Ended, Limits,
  Outcome; times through `| ts`). `src/services/projects_view.py`: `REASON_LABELS["stopped"] = "Projects were
  stopped"`.
- [ ] **Step 3: Help** (`docs/operations/help-content.md` rules; no numbers):
  `discussions-activity.html#run-detail` lists the Sessions card (each time interviews or projects ran in
  the run: start, end, limits, why it ended); `projects.html` adds "**Projects were stopped**: an admin
  stopped projects while it was open", rewords "The run ended before posting" to "projects were winding down
  before its research began", and says a project is also Paused while projects are not running in a live
  run; `how-it-works.html` says a run can hold interviews, projects or both, which only an admin starts or
  stops, and that while projects are running each live lab gets a project, one at a time, the next when its
  last one finishes.

---

### Task 9: operator documentation

**Files:** listed in the table.

- [ ] `CLAUDE.md`: "head is `0074`"; the simulation section: interviews and projects start and stop
  separately from `/admin/simulation` inside one run (the `system` command kind, claimed by the live engine;
  the supervisor stales one when no engine is alive); a natural end is the last running system reaching its
  count or runtime limit (finalizes) unless an interview hold was carried (`held_over`); a resume clears a
  hold only when Interviews starts; Stop is "Stop everything" with today's two variants.
- [ ] `src/agent/CLAUDE.md`: the project-thread bullet says threads are recognised from the root's phase
  whether or not Projects is running (replaces the `HUB_PROJECTS_ENABLED` wording); a bullet that every lane
  asks `ctx.sessions` (`src/agent/sessions.py`) before acting, and that an operator's Stop now closes
  threads with outcome `stopped`.
- [ ] `docs/operations/host-and-simulation.md`: the panel (cards, Start both, Wind down / Stop now, Stop
  everything), the run-end table rows (`failure_stop` TODAY, `held_over` HOLD, the session-driven natural
  end), the CLI options `--systems`, `--max-projects`, `--projects-runtime`.
- [ ] `docs/operations/blackbird-hub.md`: the projects settings and cadence paragraphs (no switch, no
  ceiling, no interval; the request rule, spread, failure stop; the worker researches only while Projects
  runs).
- [ ] `docs/specs/2026-10-22-idea-pitching-prototype-design.md`: owner notes (2026-10-27) at §5.2 "When",
  at the ceiling and at the switch, pointing to the independent-controls design.
- [ ] `docs/specs/2026-10-27-independent-simulation-controls-design.md`: status "Approved by the owner
  2026-10-27; plan `docs/plans/2026-10-27-independent-simulation-controls-plan.md`".

---

### Task 10 (lead): integrated verification and audit

- [ ] **Step 1: Tripwire sweep** (memory "plan tripwire sweep"): on the host,
  `grep -rn "getsource\|read_text" tests/ | grep -E "simulation|post_lane|reply_lane|projects|control|supervisor|main\.py|config"`
  and `grep -rn "max_proposals\|hub_projects_enabled\|project_interval_hours\|project_research_daily_usd_limit\|is_within_time_limit\|_proposal_target_drained\|draining=" src tests scripts`;
  every hit is either intended or fixed.
- [ ] **Step 2:** `scripts/build_css.sh` then `scripts/build_css.sh --check`;
  `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` (expect "in sync": no prompt changed).
- [ ] **Step 3:** `ENGINE_REFUSAL_FALLBACK_ENABLED=false HUB_PROJECTS_ENABLED=false ./scripts/ci.sh` on the
  host (nohup into `/tmp/ci_controls.log`, ends `CI_RC=`). Expected: CI passed, both golden masters unchanged.
  Repair through `lathe:test-triage`, then the failing check, then the full gate.
- [ ] **Step 4:** the browser harness on the workstation (`tests.e2e.ui_audit.run all --phase 6`, then
  `journeys --phase 0..5` with `--baseline`), `tests.e2e.help_shots.capture --check` ("shots current"), and
  one exploratory check of `/admin/simulation` with `lathe:browser-tester`: idle shows both cards and Start
  both; a Start posts the expected form (refused or enqueued as the seeded state dictates).
- [ ] **Step 5:** adversarial audit in one batch: `lathe:plan-auditor` (this plan + diff),
  `lathe:semantic-reviewer` (diff), `lathe:security-reviewer` (the routes, the command payloads, the
  supervisor and engine claims). Fix, re-run the failing checks, re-audit fixed areas only.
- [ ] **Step 6:** commit (no push), with the plan's "As built" notes if anything differs.

### Task 11 (owner-gated): release

- [ ] Only on the owner's instruction: porcelain check empty; tag `rollback-pre-0074` images and git tag;
  `pg_dump` the database; build `blackbird-app worker`, then `--profile agent build agent`; verify each
  image's `.build_info.json` commit; read-only checks (alembic `0073`, engine lock free, supervisor idle,
  no live run); `./scripts/migrate/run_migration.sh` rehearsal, then `--apply` to `0074`;
  `$DC up -d blackbird-app worker`; `$DC --profile agent up -d agent` only with no live run; verify stamps,
  the panel, `/api/health` and the logs. Never start a run.

## As built (2026-10-27)

- **The panel moved (owner, after this plan was approved).** The page is `/workspace/simulation`
  (Workspace → Simulation; admins only for now, managers later), served by
  `src/routers/workspace/simulation.py` from `templates/workspace/simulation.html`. Every write stays
  at `/admin/simulation/*` and redirects there; `GET /admin/simulation` redirects too, keeping
  `?run=`. The page's context skips the workspace projection because the page is admin-only.
- **Task 2.** `start()` runs `_recover_reply_less_pitches()` on every resume, not only one that starts
  Interviews: a projects-only resume needs the hub's Phase 1 subscription, and
  `_auto_activate_lab_posts` takes up no pitch while Interviews is not accepting
  (`test_a_projects_only_resume_subscribes_the_hub_but_takes_up_no_pitch` replaces the planned
  test). `_close_threads(thread_ids)` takes no outcome: it always writes `STOPPED_OUTCOME`, a named
  constant `tests/unit/test_repo_hygiene.py` can enumerate. An Interviews Stop now posts every owed
  headline with no cap, even when it ends the run: the main loop's per-tick drain posts them in
  the same tick, before the shutdown sweep and its cap of 25.
- **Task 1.** `scripts/migrate/postflight.py` still expected `sim_command_enum` to be
  `('start', 'stop')`, so it blocked the correct production migration to `0074`; it now
  expects `system` from `0074` on (`_LABEL_REVISION`), and
  `tests/integration/test_postflight_schema_pins.py` checks every enum at the default target.
- **Task 4.** `_count_awaiting_replies`, `_awaiting_reply` and `_awaiting_project_reply_count` are
  removed with the drain gate that was their only reader, so there is no "awaiting replies" tick step
  or test.
- **Task 8.** The Sessions card sits before the run's summary cards.
- **The Projects page.** An open project of a live run shows "In progress" whether or not Projects is
  running (the spec leaves that page unchanged), and "Paused" once the run stops.
- **Review fixes.** The worker researches no project while the engine stops (`live_projects_run_id`);
  project threads paused with Projects no longer hold back a lab's pitch; the panel disables what
  the routes refuse (a stopping engine, a pending Finalize run, a start the supervisor has claimed,
  an engine that predates per-system controls) and the routes refuse the last two, while the
  start a live run was launched by (pending until that run ends) no longer blocks a per-system
  start; a limit above
  1,000,000 is refused; the run page shows a session its process never ended as "the run
  stopped" once the run is not live.
