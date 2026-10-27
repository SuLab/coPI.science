# Independent simulation controls: start and stop interviews and projects separately — design

- **Status:** Approved by the owner 2026-10-27; plan `docs/plans/2026-10-27-independent-simulation-controls-plan.md`.
  Brainstormed with the owner the same day; every SC-numbered
  decision below is the owner's, and everything marked *my call* is the designer's, open to the owner's correction.
  A builder check the same day asked four questions (held interviews in a projects-only resume, finalizing after an
  earlier hand stop, the pitch catch-up, a start that races the run's end); all four are settled below as my calls
  (§4.3, §4.5, §4.8, §4.9). Uncommitted.
- **Requested by:** the owner, 2026-10-27 (verbatim): "we need to update the simulation control panel, i need the
  ability to independently control the two systems so i need to be able to start the existing interview style
  system where the pi bots post first and bblbot interviews and assesses and ability to start the system where
  bblbot develops projects, the new control should have the same abilities to run a simulation with a specific
  number of projects or run for a specific amount of time. there should also be an option to run both."
- **Measured:** code at `blackbird@9439338b`, read 2026-10-27.
- **Related:** the projects prototype, `docs/specs/2026-10-22-idea-pitching-prototype-design.md` (D41–D55); the
  panel, `docs/plans/2026-08-30-simulation-control-panel.md`; how runs end today,
  `docs/operations/host-and-simulation.md` ("How a run ends, and what a resume restores").

---

## 1. Why

The owner wants a set number of finished projects for reviewers to score, produced by a run that does nothing
else, and wants to run the interview system on its own, or both together, from `/admin/simulation`.

Today one run is the interview system. Projects ride along only when the server setting `HUB_PROJECTS_ENABLED` is
on (read once at engine start, so changing it needs an agent recreate), and they stop when the run reaches its max
proposals (`Projects._projects_tick(draining=…)`). Each live lab gets one project at the run's start and one more
per `PROJECT_INTERVAL_HOURS` (24) of run time, and research stops for the day at `PROJECT_RESEARCH_DAILY_USD_LIMIT`
($25 per rolling 24 hours). So there is no way to run projects without pitches and interviews, no way to ask for a
number of projects, and a 10-project run would take days.

## 2. Goals, non-goals, anti-goals

**Goals**

1. Start interviews, projects or both from the panel, each with a count limit and a time limit.
2. Start either system while the other runs, and stop either while the other carries on.
3. A projects session with a number ends on its own with exactly that many finished projects, unless it is stopped
   or projects keep failing.
4. No spending limit on projects, with spending visible on the panel.

**Non-goals**

- No change to the interview prompt sets (frozen, owner 2026-10-26), the project prompts, the rubric, verdict
  capture, headline content, or how an interview or a project conversation runs in Slack.
- No lab picking on the panel: the live labs take part, as today (on the roster, a spoke role, a public profile
  with a Research Summary).
- No change to the Projects page, reviews, or the rate limits (`llm_calls_per_load_per_window`,
  `hub_llm_calls_per_window` are the owner's to tune).
- Still one engine at a time.

**Anti-goals** (failure even if everything else works)

- Anything starts on its own: a deploy, reboot or crash-restart must still come back idle.
- A session with interviews off makes a pitch or interview call, or a session with projects off requests,
  researches or posts a project.
- A projects session overshoots its number, or keeps paying when every project fails.
- Stopping one system loses or doubles a headline, or leaves an interview open with nobody to finish it.

## 3. The owner's decisions (2026-10-27)

- **SC1 Two systems, one panel.** *Interviews* (labs pitch; BlackbirdBot interviews and assesses) and *Projects*
  (BlackbirdBot develops a project with each lab) each get their own Start, Stop and limits, and "Start both"
  starts them together.
- **SC2 Independent.** Either can start while the other runs; stopping one leaves the other going.
- **SC3 Same limits as today.** Each system has a count and a max runtime; whichever is reached first applies. For
  Projects the count is finished projects.
- **SC4 What counts.** A project counts when it closes in Slack as Ready or Not agreed. One that fails early shows
  on the Projects page but does not count.
- **SC5 No overshoot.** A projects session never has more projects going than it needs: with 10 asked for, counted
  plus in-progress never exceeds 10, and a failure frees a slot for another.
- **SC6 Pace.** A lab gets its next project as soon as its last one finishes, not a day of run time later.
- **SC7 Wind down at the limit.** A system that reaches a limit, count or time, winds down: nothing new starts,
  work under way finishes (interviews to their verdict, projects to their close), then it stops. For interviews this
  replaces today's max runtime, which ends open interviews at once.
- **SC8 Stopping by hand.** Each system has *Wind down* (as if its limit were reached) and *Stop now* (open
  interviews end and get their headlines; projects under way end as not finished). Today's two Stop buttons become
  *Stop everything* and *Stop everything, hold open interviews*, unchanged in behaviour.
- **SC9 One run.** Systems running at the same time share one simulation run. The first system started asks Fresh
  or resume, as today; a system started while the run is live joins it. The run ends when no system is running.
- **SC10 Labs without interviews.** While interviews are off, labs do not pitch; they only answer project postings
  and talk in project threads.
- **SC11 The panel is the switch.** `HUB_PROJECTS_ENABLED` goes away.
- **SC12 No spending limit on projects.** The research ceiling goes; every call is still recorded.
- **SC13 Stop after failures.** Projects stop themselves after a few failures in a row (*my call:* 3, below).
- **SC14 Counts start at Start.** A system's limits count from its own Start: "Start projects: 10" means 10 more,
  even in a resumed run. A resume no longer carries over an earlier max proposals.
- **SC15 Run end.** When the last running system reaches its limit, the run ends and is finalized, as a natural
  end is today. A run stopped by hand can still be resumed.

## 4. Behaviour

### 4.1 Terms

- **Session:** one system's span inside a run, from its Start to its end. A run can hold several sessions of each
  system, one at a time per system.
- **Open project:** status `requested`, `researching`, `ready_to_post`, `posted` or `refining`.
- **Counted project (SC4):** finished `ready` or `not_agreed` during this Projects session, whichever session
  started it.
- **Failed project (SC13):** finished `not_finished` with reason `no_idea`, `research_failed`, `choice_failed`,
  `reply_failed`, `thread_timeout`, `thread_abandoned` or `close_unreadable`. Not failures: `lab_not_live`,
  `run_ended_before_posting`, and the new reason for a hand stop (§4.6).
- **Under way:** an open interview thread; a project whose research has made at least one call. A `requested`
  project with no call yet is not under way.

### 4.2 The panel's Start/stop section

Replaces today's "Start a run" and "Stop the run" cards; nothing else on the page changes.

- **Run strip, nothing live:** "No run is live" and the Fresh run checkbox (checked by default; forced and disabled
  when the latest run is finalized, as today). It applies to whichever Start is pressed.
- **Run strip, run live:** the run's short id and start time, *Stop everything*, and *Stop everything, hold open
  interviews* (today's buttons, today's confirm dialog and behaviour).
- **Interviews card:** a state chip (Not running / Starting / Running since … / Winding down / Stopped: why).
  Not running: *Max proposals (0 = no limit)*, *Max runtime (minutes, 0 = indefinite)*, *Start interviews*.
  Running: proposals this session of N, open interviews, time used of the runtime, spending in this run, *Wind
  down*, *Stop now*. Winding down: what it is waiting on (the open interviews), *Stop now*.
- **Projects card:** the same, with *Finished projects (0 = no limit)*. Running: counted of N, open, failures in a
  row (stops at 3), spending in this run.
- **Start both:** shown when nothing is live; starts both systems in one fresh or resumed run with both cards'
  limits.
- Starting while the run is live: the card's Start joins the live run (no Fresh choice).
- Buttons that cannot act are disabled, and the routes refuse them too (a pending start, an engine stopping, the
  system already running or winding down).

### 4.3 Starting

- Nothing live: the supervisor starts the engine as today, with the chosen systems and their limits. A fresh run
  posts the run-start announcement, as today; a system joining later posts nothing.
- Run live: the engine starts the system within one control poll (`CONTROL_POLL_INTERVAL`, 30 s). Refused while that
  system is running or winding down, or while the engine is stopping.
- A start meant to join carries the id of the run it joins. If that run is no longer live when the start is claimed
  (it ended in between), the start is finished stale with "the run ended before this start could join it — start
  again"; it never starts a new engine, so no unchosen fresh run or announcement follows (*my call*; today's pattern
  for a start that raced a live run, audit V7).
- Every start and stop is a `simulation_commands` row and an admin audit event, as today. Boot still marks every
  pending command stale (never-auto-start).

### 4.4 Limits (SC3, SC14)

- Interviews: the count is lab pitches posted during the session (today's proposals).
- Projects: the count is counted projects (§4.1). No overshoot (SC5): a new project is requested only while
  counted + open < N. Projects left open by an earlier stop that continue in this session count toward N when they
  close counted, and occupy a slot while open.
- Max runtime counts from the session's Start. 0 means no limit for either field.

### 4.5 Winding down and ending (SC7, SC15)

- **Interviews winding down:** labs stop pitching; open interviews run to their end as they do today; when none has
  been open for `PROPOSAL_DRAIN_SETTLE_TICKS` checks, the session ends.
- **Projects winding down:** no new project is requested; open projects not under way end `not_finished` with
  reason `run_ended_before_posting` (the reason today's drain uses; unlike today's drain, a project whose research
  has begun is not ended); projects under way run to their close; when none is open, the session ends.
- **The run ends** when no session is running or winding down:

| How the last session ended | Run row |
|---|---|
| its count or runtime limit | natural end: every owed headline, `finalized_at` set (as today) |
| Wind down or Stop now by hand, or the failure stop (*my call*) | stopped, resumable |
| Stop everything (either button) | as today: stopped or held, resumable |

Only the last session decides (*my call*): an earlier Interviews Stop now leaves nothing waiting, since its
interviews ended with their headlines, so a later natural end still finalizes. The one exception is a run whose held
interviews no session resumed (§4.9): it ends stopped and still held, never finalized, because finalizing would
announce interviews the operator chose to hold.

### 4.6 Stopping by hand (SC8)

- **Interviews, Stop now:** labs stop pitching at once; every open interview ends now and its owed headline posts
  through today's claims (`headline_claims`, at most 25 per stop as today; beyond that, LOST as today, with its
  repair path). The run continues if Projects is running.
- **Projects, Stop now:** every open project ends `not_finished` with a new reason (*my call:* `stopped`; it is
  `String(40)` with no check constraint, so no migration). Its thread gets no more turns; nothing is posted to
  Slack about it.
- **Stop everything:** today's run stop, unchanged. Open projects stay open and show as paused, as today.

### 4.7 Projects: pace, spread, failures, spending

- **Pace (SC6):** a live lab with no open project gets a new one at the next tick, subject to the spread and SC5.
  `PROJECT_INTERVAL_HOURS` and the per-run interval rule go.
- **Spread (*my call*):** a lab is requested a project only when no live lab has started fewer in this session, so
  no lab gets more than one ahead and one failing lab cannot run up the bill. A lab that becomes live mid-session
  counts as having the session's current minimum.
- **Failure stop (SC13, *my call* on the details):** when the last 3 projects of the session to finish (by finish
  time) all failed (§4.1), the session winds down and the card says "Stopped after 3 failures in a row". A counted
  project resets the streak.
- **Spending (SC12):** `PROJECT_RESEARCH_DAILY_USD_LIMIT` and its check in `project_research._claim` go. The
  `hub_project_calls` ledger stays. Each card shows what its system has spent in this run (*my call*).

### 4.8 One system without the other (SC10)

- **Interviews off:** a lab's turn offers only a project reply and otherwise makes no model call; BlackbirdBot starts
  no interview, including the resume catch-up of lab pitches without a thread ("On a resume the hub … interviews any
  lab pitch from the last 14 days"), and takes up project threads only.
- **Projects off:** no project is requested, researched, posted or replied to. Open projects of the run stay paused
  until a Projects session starts. The worker researches only while the live run has Projects running.
- **The pitch catch-up (*my call*):** every Interviews start (a first start, a resume, a join, or a restart in the
  same run) runs today's resume catch-up: BlackbirdBot interviews this run's lab pitches from the last 14 days that
  have no thread yet. These do not count toward the session's max proposals. An Interviews Stop now ends interviews
  that have a thread; a pitch with no hub reply yet is not an open interview, owes no headline, and waits for the
  next Interviews start.

### 4.9 Resume

Start with Fresh cleared resumes the latest stopped, unfinalized run, as today. Only the systems started run. A
resume that starts Interviews clears a hold and continues the held interviews, as today. A resume that does not
start Interviews leaves the run's interview state alone, the hold included (`held_at` stays set, its headlines keep
waiting) (*my call*); Interviews can start later in the same run to finish them, or Finalize run releases them. Stop
everything treats them as today: plain Stop posts every owed headline, held ones included; the hold button keeps
them held.

### 4.10 What the run records

The run keeps each session: system, start time, limits, end time and why it ended (limit, wind down, stop now,
failure stop, stop everything). The run's Activity page shows them where it shows today's max runtime and max
proposals.

### 4.11 Settings and the CLI

- Removed: `HUB_PROJECTS_ENABLED`, `PROJECT_INTERVAL_HOURS`, `PROJECT_RESEARCH_DAILY_USD_LIMIT`.
- Kept: `PROJECT_RESEARCH_ENABLED` and a priced `LLM_PROJECT_RESEARCH_MODEL`. When either leaves research unable to
  run, the Projects card says so and Start projects is refused (*my call*): a session that cannot research would
  never finish or fail.
- The emergency CLI (`python -m src.agent.main`) can start either system or both with the same limits; with no
  system named it runs interviews only, today's production behaviour (*my call*).

## 5. Constraints

- One engine at a time (the engine advisory lock); the supervisor comes back idle on boot and exits after each run.
  A live engine must now take a `start` for a system; the supervisor still refuses to start a second engine.
- `simulation_commands` allows one pending row per command (`uq_simulation_commands_one_pending`); keep a
  double-click and a race refused, as today, and keep the C-24 row locks the start and finalize routes take.
- Headlines post at most once per interview through the claims in `src/services/headline_claims.py`; a per-system
  stop must use them, never a new path.
- New migrations are additive and nullable; never `DELETE FROM simulation_runs`.
- Engine changes need the agent image rebuilt; the worker and web images change too. Never start a run as part of a
  deploy.
- Many tests read engine source with `inspect.getsource`; sweep them (`grep -rn getsource tests/`) and the
  source-scanning tests for every changed method and setting.
- Panel pages follow the existing accessibility and contrast rules (`tests/unit/test_contrast_classes.py`,
  `tests/unit/test_low_a11y_markup.py`) and the Origin guard on every POST.

## 6. Left to the builder

- How a per-system start or stop reaches the engine (a `system` field in the existing `start`/`stop` payloads needs
  no enum migration; new enum values need one) and how "Start both" travels.
- Where sessions are stored (`simulation_runs.config` JSON or a new table) and the heartbeat `detail` shape the
  cards read.
- How the main loop and the drain gate (`_proposal_target_drained`, `Projects._projects_tick`) become per-session,
  and how an Interviews Stop now runs the headline sweep while the run continues.
- How spending is attributed to each system (project phases and `hub_project_calls` for Projects; the rest for
  Interviews).
- The tests, the browser-harness journeys for the panel (`tests/e2e/ui_audit/journeys_phase0.py`, `…phase1.py`,
  `…phase2_2b.py` touch it), and the markup.
- Docs to update: `CLAUDE.md` (the simulation section), `src/agent/CLAUDE.md`,
  `docs/operations/host-and-simulation.md` (the panel and the run-end table), `docs/operations/blackbird-hub.md`
  (projects settings and cadence), `.env.example`, the `src/config.py` comments, `scripts/projects_thread_replay.py`,
  and owner notes in the projects prototype spec (§5.2 cadence, the ceiling).

## 7. Known risks

- **A leaked open interview blocks a wind-down.** The drain gate today can be held open by a leaked thread
  (`docs/plans/2026-10-09-sim-drain-hang-rca-and-remediation.md`, not fixed). An Interviews wind-down uses the same
  gate, so the card must name what it is waiting on, and Stop now is the way out.
- **Spending is unbounded by design.** The failure stop and the spread are the only automatic brakes; a healthy
  session with no count and no runtime runs until someone stops it.
- **Both systems share the labs' call allowance.** Running both makes each slower; the rate limits are not changed
  here.
- **Interviews' max runtime changes meaning** (SC7): a timed interview session now finishes its open interviews,
  so it can run past its runtime; Stop now is the hard stop.
