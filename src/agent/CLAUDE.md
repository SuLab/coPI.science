# src/agent/ — the simulation engine

Loads when you read files here. Full detail: `docs/operations/blackbird-hub.md` (hub,
rubric, verdicts, specialists) and `docs/operations/host-and-simulation.md` (running,
stopping, restarting).

- `src/` is baked into the agent image. Any change here, or to a module the engine
  imports (`src/services/assessment_detail.py` is one), needs
  `$DC --profile agent build agent` before it runs. Tell the user when a change
  affects the running agent process.
- Engine methods live in `src/agent/engine/<unit>.py` (spec §7.1 of
  the 2026-09-29 audit-remediation spec, kept uncommitted); `SimulationEngine` in
  `src/agent/simulation.py` is the orchestrator and forwards `engine.<name>` to the
  owning unit. Units log as `src.agent.simulation`, and tests patch
  `src.agent.engine.deps.*` / `src.agent.engine.constants.*`. Many tests read engine
  source with `inspect.getsource(...)` and assert that a substring is present or
  absent, comments and docstrings included: before editing an engine method,
  `grep -rn getsource tests/` for it. `tests/unit/test_engine_import_graph.py` pins
  which unit may use which.
- `thread_guidance.py`'s `pi_lab` strings are pinned by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`. Do not reword
  them to make a test pass, and never run `pytest --snapshot-update`.
- Inside an interview thread the hub is reply-only. Its verdict travels as an
  `<assessment_json>` sidecar in the reply, stripped before posting, and `raw_verdict`
  and the per-dimension scores never reach Slack. The visible reply must still state
  gating, recommendation, red flags and confidence, but describes the idea only at the
  level the PI has already made public: an unpublished result, unfiled construct,
  undisclosed compound or volunteered limitation stays in the sidecar. No code or test
  checks that.
- The engine's `#assessments-summary` headline (`src/services/assessment_headline.py`)
  does publish sidecar data: the band/score and the first `PITCH_DISPLAY_CHARS` (600)
  characters of `elevator_pitch`, which can carry unpublished disclosures. Do not widen
  what it publishes without the operator's sign-off.
- One interview yields one assessment row: the last verdict-bearing reply wins
  (`_capture_hub_assessment`, `_retire_superseded_verdict`). A refusal is recorded in
  `assessment_drops` with its `raw_verdict`, never just logged. A headline cannot be
  retracted, so it posts at most once per interview (`summary_posted_at`), and every poster (the engine
  and `scripts/backfill_assessment_headlines.py`) claims the interview first
  (`summary_claimed_at`, `src/services/headline_claims.py`): for a
  terminal verdict when it is stored, or through `_announce_owed_headline` when the
  interview ends another way (timeout, abandoned thread, shutdown). A provisional
  verdict is never announced mid-interview.
- `weighted_score` and `band` are computed (`src/services/blackbird_rubric.py`), never
  taken from the model. `panel_owed` is decided once at write time (`panel_is_owed`)
  and replayed by the read path, never re-derived.
- `gating` values are the strings `met`, `not_met` and `unconfirmed`, never booleans.
- Specialist labels: `VERDICT_SIGNALS` (write) and `_READABLE_SIGNALS` (read,
  including the historical pair) in `specialists.py` deliberately differ in size.
- Rate limits count real API calls, including tool rounds. Do not raise
  `llm_calls_per_load_per_window` or `hub_llm_calls_per_window` on your own
  initiative.
- Never reuse or change `RUN_START_MARKER_PREFIX` (`run_marker.py`): the live Slack
  poller drops messages that carry it.
- A test that drives a concluding reply must seed the thread's history in the
  `MessageLog`: `_reply_to_thread` overwrites `ThreadState.message_count` from
  `get_thread_history`, so `message_count=11` over an empty log is an ordinal-1 turn.
