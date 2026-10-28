# Projects thread time-outs and research throughput — Implementation Plan

> **For agentic workers:** to be executed through `/lathe:plan-execution` once the owner approves:
> packages A (Tasks 1–3, engine), B (Tasks 4–5, worker) and C (Task 6, docs) are implemented in
> parallel without building or testing, then verified together on the host, adversarially audited,
> fixed and committed; Task 7 (release) is the main session's. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop project threads ending before BlackbirdBot's close, read a close block that ends one
bracket short, and research up to three projects at once with no idle padding between steps.

**Architecture:** Engine: a project thread counts posts (consecutive log entries by one sender), not
Slack chunks, for its turn ordinal and its 12-message cap, and the cap never closes it on
BlackbirdBot's turn; the `<project_json>` reader repairs a block that ends early. Worker: research
runs in rounds of up to `PROJECT_RESEARCH_CONCURRENCY` lanes that take steps back to back, and a
claimed step leases its project through `next_step_after`. No migration, no prompt text change.

**Tech Stack:** Python 3.12 asyncio, SQLAlchemy 2 (async), pytest (+ testcontainers Postgres).

**Spec:** none separate. This plan carries its evidence and decisions, and amends
`docs/specs/2026-10-22-idea-pitching-prototype-design.md` §5.3, §5.8, PR6 and PR12 (Task 6).

## Evidence (run 1abf809c, read-only, 2026-10-28)

- Every project thread stopped at 12 log entries holding only 6–7 posts. The chunks of one post
  were logged at most 0.41 s apart, and turns at least 72 s apart. `_reply_preflight` counts
  entries (`src/agent/engine/reply_lane.py:750`), so the count jumped past BlackbirdBot's close
  band (ordinal 11 and later), and the cap closed four threads as time-outs before its close turn.
- casadevall's early close (ordinal 10) and coyne's last-turn close (ordinal 12) each carried a
  `<project_json>` block one `}` short at its end ("Expecting ',' delimiter" at the last character):
  `plan` was never closed, so `gaps` and `disagreements` sat inside it. Appending `}` and moving
  those two keys up recovers all ten plan fields and the four top-level keys of both blocks.
  - casadevall: an early close with an unreadable block is not a close (`reply_lane.py:1333`), so
    the thread ran on and timed out.
  - coyne ended `close_unreadable`.
  - antiochos's block parsed; it was the run's one ready project.
- Three time-outs in a row (bergles, casadevall, coller) reached the failure stop at 23:10:48. It
  ended the 17 projects still `requested`.
- Research took 39–45 calls per project: 8.9–11.2 minutes of model time spread over 22–25 minutes.
  The worker researches one project at a time, at most once per `SWEEP_INTERVAL_SECONDS` (30 s,
  `src/worker/main.py:582-587`). It spent $17.49 on 6 projects; every call succeeded.

## Decisions

Owner, 2026-10-28:

- **D66** The worker researches up to 3 projects at once (`PROJECT_RESEARCH_CONCURRENCY`, default
  3), with no wait between steps while research has work.
- **D67** The failure stop is unchanged: 3 failed projects in a row, thread-stage reasons included.

Mine (the owner may overrule them at review):

- **M1** Only project threads count posts. Interviews keep counting log entries; their guidance and
  golden master stay as they are.
- **M2** In a project thread the cap never closes the thread on BlackbirdBot's turn: from ordinal 11
  that turn is its must-close turn (`project_turn`). So a project thread always ends with
  BlackbirdBot's close, or on an existing abandon path (two empty or suppressed replies, two
  non-transient model errors). A lab turn at the cap still ends the thread as a time-out.
- **M3** The close reader:
  - tolerates raw control characters inside strings (`strict=False`);
  - repairs only a block that ends early, by appending its missing closing brackets;
  - moves `position`, `gaps` and `disagreements` out of `plan` when the top level lacks them.

  There is no second model call. A block that is still unreadable is treated as today:
  `close_unreadable` at the last turn, and no close before it. With posts counted, BlackbirdBot has
  later turns to close again.
- **M4** Research lanes:
  - a round's lanes take steps back to back for up to 60 s;
  - a claimed step leases its project for 10 minutes and stores only while it still holds the lease;
  - the worker starts the next round at once after a round that made a call, and after an idle round
    waits the 30 s sweep interval, as today.
- **M5** Unchanged:
  - thread reply length (no limit; owner, 2026-10-27);
  - `PROJECT_RESEARCH_MAX_TOOL_ROUNDS` (8);
  - the request rule, and the wind-down of `requested` projects;
  - BlackbirdBot's rate allowances;
  - every prompt and every `project_guidance` string.

## Global Constraints

- Interviews unchanged: no edit to `src/agent/thread_guidance.py`, `phase4_guidance`, the interview
  path of `_reply_preflight` or the `_PI_LAB` strings. Never `pytest --snapshot-update` on
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr`.
- No text change under `prompts/`, and no string change in `src/agent/project_guidance.py` or
  `RESEARCH_ANGLES`. `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` must stay
  clean.
- No migration (head stays `0076`). `max_thread_messages` stays 12 (`REQUIRED_MAX_THREAD_MESSAGES`).
  `HUB_CLOSE_FROM`, `LAB_FINAL_FROM` and `OPEN_UNTIL` are unchanged.
- `hub_llm_calls_per_window` and `llm_calls_per_load_per_window` are not raised.
- Engine code calls through `src.agent.engine.deps` and logs as `src.agent.simulation`. Before editing
  an engine method, `grep -rn getsource tests/` for it (`src/agent/CLAUDE.md`).
- `tests/unit/test_single_json_extractor.py`: a function that calls `json.loads` must carry no
  ```` ``` ```` literal and no `find`/`index` on `{` or `}`. `src/services/json_extract.py` stays as
  it is: by design it never repairs.
- Host commands as in the 2026-10-27 plan: git, pytest and scripts on the host. Run pytest as
  `.venv-test/bin/python -m pytest …` and CI as `ENGINE_REFUSAL_FALLBACK_ENABLED=false ./scripts/ci.sh`.
- Never start a simulation. The agent is recreated only when `/workspace/simulation` shows no live run.
- Commit as `git -c user.name=alan -c user.email=alan@hueb.org commit`, with the session's two
  trailers. Never push, and never stage `docker-compose.prod.yml`.

## Review Focus

1. **A human message inside a project thread** (`sender_agent_id` None, `is_bot` False) is its own
   post and shifts parity. BlackbirdBot must still get its close (M2). Pinned by a Task 2 test.
2. **A close block with an error mid-text** (an unescaped quote) or a misnested bracket must read as
   unreadable, never as a wrongly repaired object. Pinned by Task 3 tests.
3. **A step that outlives its lease** (another lane took the project) must not overwrite the
   project's research state, and its paid call is still recorded. Pinned by a Task 4 test.
4. **A round with fewer due projects than lanes:** the idle lanes return at once, and no lane takes
   a leased project. Pinned by Task 4 tests.
5. **After an idle round** the worker waits the sweep interval, never spins. Pinned by a Task 5 test.

---

## Package A — engine

### Task 1: Posts in the message log

**Files:**
- Modify: `src/agent/message_log.py` (new module function), `src/agent/engine/projects.py`
  (`_latest_lab_position` only)
- Test: `tests/unit/test_message_log_projects.py`

**Interfaces:**
- Produces `group_posts(entries: Iterable[LogEntry]) -> list[list[LogEntry]]`, a module function,
  not a `MessageLog` method, so the cohort read-path inventory is untouched. Consecutive entries with
  the same `sender_agent_id` form one post, in order. A message over Slack's 4,000-character limit is
  logged as several chunks a second or less apart, while turns are a minute or more apart.

- [ ] **Step 1: Write the failing tests** (the file's `_entry`/`_log` helpers; `HUMAN = (None, "Wang (PI)")`)
  - `test_group_posts_merges_consecutive_chunks_by_sender`: the root (lab), 2 hub chunks, 1 lab
    entry and 3 hub chunks → `[[root], [h1, h2], [l1], [h3, h4, h5]]` by ts. `get_thread_message_count(root)`
    is still 7.
  - `test_group_posts_keeps_alternating_senders_apart`: lab, hub, lab → three posts.
  - `test_a_human_message_is_its_own_post`: hub, human, hub → three posts.
  - `test_group_posts_of_nothing_is_empty`: `group_posts([]) == []`.
- [ ] **Step 2: Run them**: `.venv-test/bin/python -m pytest tests/unit/test_message_log_projects.py -q` → FAIL (ImportError).
- [ ] **Step 3: Implement `group_posts`.** Rewrite `Projects._latest_lab_position` over
  `group_posts(self.message_log.get_thread_history(thread_id))`, with the same behaviour. Its
  docstring points to `group_posts`.
- [ ] **Step 4: Run** `tests/unit/test_message_log_projects.py` and `tests/unit/test_projects_unit.py`
  (unchanged) → PASS.

### Task 2: Project threads count posts; BlackbirdBot's close is never pre-empted

**Files:**
- Modify: `src/agent/engine/reply_lane.py` (`_reply_preflight` only)
- Test: `tests/integration/test_project_threads.py`

**Interfaces:** Consumes `group_posts` (Task 1). No new names.

- [ ] **Step 1: Write the failing tests.**
  - **Harness:**
    - `_drive_project` seeds a `None` sender as a human message: `sender_agent_id=None`,
      `sender_name="Gordy (PI)"`, `is_bot=False`.
    - A new helper, `_chunked(history, chunks)`, repeats every entry after the root `chunks` times.
      Every chunk after the first gets the suffix " (part k)".
  - **Replace** `test_close_band_reached_at_ordinal_11_after_a_split` with
    `test_a_split_post_counts_once`, run on `_history(10, lab_latest="✅ Agreed.", split_at=5)`:
    - `message_ordinal == 10` and `thread_phase == "project_refine"`;
    - with the lab's ✅ and a readable block, the thread still closes `project_closed` and the
      project is `ready`.

    The old test pinned chunk counting, which caused the time-outs. This is the intended change; do
    not restore it.
  - `test_posts_split_three_ways_still_reach_the_close_at_12`:
    `_chunked(_history(11, lab_latest="✅ Agreed, we can run it."), 3)` (31 entries):
    - ordinal 12 and `project_close`;
    - the project is `ready`, and the decisions are `[(ROOT, "project_closed")]`.
  - `test_a_hub_turn_past_the_cap_closes_instead_of_timing_out`: `_history(13, lab_latest="✅ Agreed.")`:
    - ordinal 14 and `project_close`;
    - the project is `ready`, and the decision is `project_closed`.
  - `test_a_human_message_counts_and_the_hub_still_closes` (Review Focus 1):
    `_history(11, lab_latest="✅ Agreed.")` with `(None, "Looks good from here.")` inserted at index
    6, making 12 posts:
    - ordinal 13 and `project_close`;
    - the project is `ready`.
  - `test_a_lab_turn_at_the_cap_still_times_out`: `_history(12)` with `as_lab=True` → no model
    call, decision `timeout`, thread closed.
- [ ] **Step 2: Run them** → the new tests FAIL (time-out instead of close; ordinals counted in chunks).
- [ ] **Step 3: Implement in `_reply_preflight`.**
  - Keep `thread.message_count = len(history_entries)`. For a project thread
    (`self.message_log.is_project_thread`), overwrite it with `len(group_posts(history_entries))`.
  - The cap check skips a project thread's hub-role turn (`agent.role in hub_role_names()`).
  - Comments say why (run 1abf809c), and that the hub's turn there is its must-close turn
    (`project_turn`).
  - The interview path stays byte-identical.
- [ ] **Step 4: Run** `tests/integration/test_project_threads.py`,
  `tests/integration/test_hub_assessment_capture_gate.py` and `tests/unit/test_reply_lane.py` → PASS.

### Task 3: Read a close block that ends early

**Files:**
- Modify: `src/agent/engine/sidecar.py`
- Test: `tests/unit/test_project_sidecar.py`, `tests/integration/test_project_threads.py` (one
  end-to-end test; same package as Task 2)

**Interfaces:**
- `_extract_project_json(raw: str | None) -> dict | None` keeps its signature, and still never raises.
- New `_PROJECT_TOP_KEYS = ("position", "gaps", "disagreements")`.
- New `_missing_closers(text: str) -> str | None` returns the closing brackets that complete `text`,
  innermost first. It scans characters, skipping string contents and escapes, and uses no
  `str.find`. It returns "" when `text` is balanced, and None when a bracket closes out of order or
  `text` ends inside a string.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/test_project_sidecar.py`, `PLAN` is the
  ten template fields.
  - `test_a_block_one_brace_short_with_its_tail_inside_plan_is_repaired` (run 1abf809c's shape): the
    block
    `'{"position": "agree", "plan": {' + json.dumps(PLAN)[1:-1] + ', "gaps": ["No budget yet"], "disagreements": []}'`
    reads as `{"position": "agree", "plan": PLAN, "gaps": ["No budget yet"], "disagreements": []}`.
  - `test_raw_line_breaks_inside_strings_are_read`: `{"plan": {"readout": "qPCR` + a literal newline
    + `and ELISA"}}` is read.
  - `test_top_level_keys_left_in_a_balanced_plan_move_up`: `{"position": "agree", "plan":
    {"readout": "qPCR", "gaps": [], "disagreements": ["x"]}}` → plan `{"readout": "qPCR"}`, with the
    two keys at the top level.
  - `test_a_top_level_key_already_present_is_not_overwritten`: a plan holding `"gaps": ["inner"]` and
    a top level holding `"gaps": ["outer"]` come back unchanged.
  - `test_a_mid_text_error_is_not_repaired` (Review Focus 2): `{"plan": {"readout": "the "best" assay"}}` → None.
  - `test_a_misnested_bracket_is_not_repaired`: `{"plan": {"lab_tools": ["a"}}` → None.
  - `test_missing_closers`:
    - `'{"a": [1, {"b": "}"'` → `"}]}"`;
    - `'{"a": "x'` → None;
    - `'{"a": 1}'` → `""`;
    - `'{"a": [1}'` → None.

  End to end, in `tests/integration/test_project_threads.py`:
  `test_an_early_close_one_brace_short_is_recorded`. It runs `_history(9, lab_latest="✅ This plan works for us.")`
  with the raw `<slack_message>✅ Then we are done: final plan below.</slack_message>` plus the
  one-brace-short block:
  - the thread closes `project_closed`;
  - the project is `ready`, with `final["plan"] == PLAN`.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement in `_extract_project_json`.**
  - Parse with `json.loads(body, strict=False)`.
  - On a `JSONDecodeError` whose `pos` is at the end of the stripped body, append
    `_missing_closers(body)` (when it is neither None nor "") and parse once more.
  - Move each `_PROJECT_TOP_KEYS` key that the top level lacks out of `plan`.
  - Each repair logs one warning ("[project] close block repaired: …") naming what changed, never the
    block's text.
  - Keep the shape check: an object with a `plan` object.
- [ ] **Step 4: Run** `tests/unit/test_project_sidecar.py`, `tests/integration/test_project_threads.py`
  and `tests/unit/test_single_json_extractor.py` → PASS.

## Package B — worker

### Task 4: Research lanes and the step lease

**Files:**
- Modify: `src/services/project_research.py`, `src/config.py`, `.env.example`
- Test: `tests/integration/test_project_research.py`; create `tests/unit/test_project_research_round.py`

**Interfaces:**
- **Settings:**
  - `Settings.project_research_concurrency: int = 3` (D66), env `PROJECT_RESEARCH_CONCURRENCY`;
  - it sits next to the other `project_research_*` settings, its comment naming D66.
- **Constants:**
  - `RESEARCH_ROUND_SECONDS = 60.0`: a job waits behind at most one round plus one step;
  - `STEP_LEASE = timedelta(minutes=10)`.
- **The claim and its lease:**
  - `_Claim` gains `lease_until: datetime` as its last field.
  - `_move(db, project_id, expected, *, lease: datetime | None = None, **values) -> bool`: given
    `lease`, the compare-and-set also requires `HubProject.next_step_after == lease`.
  - `_claim` moves the project to `researching` with `next_step_after = datetime.now(UTC) + STEP_LEASE`,
    and puts that value in the claim.
  - `_store` passes `lease=claim.lease_until` to each of its three moves. The call row's update is
    not lease-guarded, so a paid call is always recorded.
- **Entry points:**
  - `research_projects(session_factory)` is unchanged: one step. Its existing tests keep their
    meaning.
  - New `async def research_round(session_factory: async_sessionmaker, slots: int | None = None) -> bool`:
    - `slots` defaults to `get_settings().project_research_concurrency`, at least 1;
    - it runs that many lanes through `asyncio.gather(..., return_exceptions=True)`;
    - each lane calls the module's `research_projects` until it returns False, or until
      `RESEARCH_ROUND_SECONDS` have passed since the round began (checked after each step);
    - it returns True when any lane made a call, and never raises.

- [ ] **Step 1: Write the failing tests.**

  In `tests/integration/test_project_research.py`:
  - `test_a_claimed_step_holds_its_project_while_its_call_runs` (Review Focus 4):
    - through `tools.during`, mid-step, `next_step_after` is 590–605 s out, and
      `pr._due_project(db_session, world.run.id)` is None;
    - after the step, `next_step_after` is None.
  - `test_a_step_that_lost_its_lease_does_not_store` (Review Focus 3): `tools.during` sets
    `next_step_after` to now + 30 minutes, as another lane's lease would:
    - `research_projects` returns True;
    - the research state is still the claim's (`sessions[0]["rounds"] == 0`);
    - the call row is `done`.

  In `tests/unit/test_project_research_round.py`, with `pr.research_projects` monkeypatched:
  - `test_lanes_run_at_once_and_take_steps_back_to_back`: a fake with 6 steps of work and `slots=3`
    → True, all 6 steps taken, peak in-flight 3.
  - `test_a_round_ends_at_its_deadline`: with `RESEARCH_ROUND_SECONDS = 0`, a fake that always makes
    a call and `slots=2` → True, exactly 2 calls.
  - `test_nothing_due_is_one_look_per_lane`: always False, `slots=3` → False, 3 calls.
  - `test_a_lane_that_raises_ends_alone`: one call raises and the others make calls → True.
  - `test_slots_default_to_the_setting`: with `get_settings` stubbed to
    `project_research_concurrency=2` and always False → 2 calls.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
  - The module docstring says: one model call per step, steps taken in lanes, and the lease.
  - `.env.example` gains `# PROJECT_RESEARCH_CONCURRENCY=3`, with one comment line.
- [ ] **Step 4: Run** the two files above and `tests/unit/test_project_research.py` → PASS.

### Task 5: The worker runs research rounds back to back

**Files:**
- Modify: `src/worker/main.py`
- Test: `tests/integration/test_worker.py`; create `tests/unit/test_worker_research_pacing.py`

**Interfaces:**
- Consumes `research_round` (Task 4), imported in place of `research_projects`.
- `PROJECT_SWEEP_SECONDS` stays.
- New `_research_due(now: float, last_check: float | None, busy: bool) -> bool` returns
  `busy or last_check is None or now - last_check >= PROJECT_SWEEP_SECONDS`.

- [ ] **Step 1: Write the failing tests.**
  - The unit tests are a truth table for `_research_due`:
    - busy → True, even 1 s after the last check;
    - not busy → False 1 s after the last check, and True at `PROJECT_SWEEP_SECONDS`;
    - the first pass (`None`) → True.
  - The integration test is `test_research_rounds_run_back_to_back_while_they_make_calls`
    (Review Focus 5), modelled on `test_the_stale_sweep_still_runs_without_the_notification_block`:
    - `claim_job` returns None, suggestions and trajectories return False, `run_persona_sweep`
      returns None;
    - a `research_round` fake returns True for three rounds, then False;
    - with `PROJECT_SWEEP_SECONDS = 3600`, the rounds are `[True, True, True, False]`, and there is
      no fifth round within 0.2 s.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.**
  - The loop keeps `research_busy` across passes, and calls `research_round(session_factory)` when
    `_research_due(...)`.
  - The module docstring's idle-chain sentence says research runs in rounds of up to
    `PROJECT_RESEARCH_CONCURRENCY` lanes, so a job waits behind at most one round.
- [ ] **Step 4: Run** both files, all of `tests/integration/test_worker.py` → PASS.

## Package C — documentation

### Task 6: Documentation

**Files:**
- Modify: `docs/operations/blackbird-hub.md` (the projects section)
- Modify: `src/agent/CLAUDE.md` (one sentence in the project-thread bullet)
- Modify: `docs/specs/2026-10-22-idea-pitching-prototype-design.md` (dated amendment notes under
  §5.3, §5.8, PR6 and PR12)

- [ ] **Step 1:** In `blackbird-hub.md`:
  - The "Research (worker)" bullet covers rounds of lanes, steps back to back, the 10-minute lease
    and what a worker dying mid-step leaves. Its settings list gains `PROJECT_RESEARCH_CONCURRENCY`.
  - The "What the engine posts" bullet says:
    - project threads count posts, not Slack messages;
    - BlackbirdBot always gets its close turn, and a lab turn at the cap still times out;
    - the close reader's repairs (M3).
- [ ] **Step 2:** In `src/agent/CLAUDE.md`: "Its turns and its 12-message cap count posts, not Slack
  chunks (`group_posts`), and the cap never closes it on BlackbirdBot's turn."
- [ ] **Step 3:** Each spec amendment is one line: "Amended 2026-10-28 (D66, M1–M4;
  `docs/plans/2026-10-28-projects-thread-timeouts-and-research-throughput-plan.md`): …".
- [ ] **Step 4: Run** `tests/unit/test_claude_md_disclosure_sync.py` and `tests/unit/test_doc_prompt_sync.py` → PASS.

## Task 7: Verify, audit, commit, release (main session)

- [ ] **Tripwire sweep:** `grep -rn "getsource\|read_text" tests/ | grep -E "reply_lane|message_log|engine/projects|sidecar|project_research|worker/main|config"`.
  Read every hit.
- [ ] **Checks:**
  - the focused tests of Tasks 1–6;
  - `scripts/sync_prompt_set_docs.py --check` and `ruff check`;
  - then the full CI.
- [ ] **Adversarial audit:** dispatch `lathe:plan-auditor` and `lathe:semantic-reviewer` in one
  batch. The reviewer covers lane concurrency, the lease, and the cap exemption against the abandon
  paths. Fix what they find, then rerun the focused tests and CI.
- [ ] **Commit:** stage an explicit file list, never `docker-compose.prod.yml`.
- [ ] **Release.** No migration; the web app is not rebuilt, since nothing it serves changes.
  1. Check that `git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts help_assets pyproject.toml alembic.ini`
     prints nothing.
  2. Rollback tags:
     - `copi-blackbird-worker:rollback-pre-projfix` and `copi-blackbird-agent:rollback-pre-projfix`
       on the running images;
     - git tag `rollback-pre-projfix-git` at the pre-change HEAD.
  3. Build with `$DC build worker` and `$DC --profile agent build agent`. Check each image's
     `.build_info.json` commit.
  4. With no `processing` job, run `$DC up -d --no-deps worker`. Its log shows "Worker started".
  5. Only when `/workspace/simulation` shows no live run, run
     `$DC --profile agent up -d --no-deps agent`. The supervisor's startup banner names the new
     commit, and it comes back idle.
  6. Do not start a run. The owner chooses a fresh run or a resume of 1abf809c (its projects have all
     finished). To roll back, retag the two images and run `up -d` on the same two services.
- [ ] **On the owner's next projects run** (read-only):
  - posts per thread: 12, or an early agreed close;
  - no `thread_timeout`;
  - the research span per project;
  - no two overlapping steps on one project (`hub_project_calls`).

## Residual risks

- Threads now run their designed 12 posts. Refinement will cost more per project than run 1abf809c's
  cut-short threads, and take longer.
- With research about 6× faster, BlackbirdBot's unchanged rate allowance may set the pace of
  refinement.
- The repair covers a block that ends early. Any other malformation still ends `close_unreadable`
  at the last turn.
- Lane concurrency is proven only by the lease logic and by sequential tests on one test session.
  The first live run is the real concurrent test.
- A worker killed mid-step leaves its lease behind. That project's step reruns up to 10 minutes
  later, instead of at once.

## Execution amendments (audit, 2026-10-28)

The plan audit found Tasks 1–6 complete. The semantic review's findings were fixed:

- `research_round` gained a `should_continue` predicate, checked before each step; the worker
  passes `lambda: not _shutdown`, so a round takes no new step once the worker is stopping.
- Lanes are clamped to `RESEARCH_MAX_LANES = 8` (the worker's database pool and the per-process
  API concurrency), whatever `PROJECT_RESEARCH_CONCURRENCY` says.
- `group_posts` keys on `(sender_agent_id, sender_name)`, so two humans in a row, or a human and a
  bot row restored without an agent id, are separate posts.
- The §5.8 amendment of the prototype design also states the close reader's repairs (M3).

Declined: a lane stops for the rest of its round when its step ends a project without a model
call (no persona, no idea). That is the lane rule this plan specifies; such endings come at most
once per project, and the worst case is the 30 s sweep wait when every lane hits one in the same
round.
