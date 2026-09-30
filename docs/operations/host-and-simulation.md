# The host, deploys and the simulation

Reference detail behind the rules in the root `CLAUDE.md`, which keeps only what
every session needs. Dated statements hold as of the date they give; re-measure a
count or a line number before relying on it.

Normal operation now uses the supervisor service described under "Simulation
control plane" below: runs start and stop from `/admin/simulation`. The one-off
`run -d --name blackbird-agent-run` commands in the first half of this file are
the CLI emergency path.

## Running the Agent Simulation

> ### ⚠️ This host runs TWO stacks. Read this before any `docker` command.
>
> A second, unrelated CoPI deployment (**org1**, `/home/ubuntu/copi-python`,
> project `copi-python`, serving `copi.science`) shares this host. Its
> simulation container is named **`agent-run`** — the *unprefixed* name. This
> repo's is **`blackbird-agent-run`**.
>
> **`docker stop agent-run` / `docker rm agent-run` stops org1's PRODUCTION run.**
>
> Always confirm ownership before touching any container:
>
> ```bash
> docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project"}}'
> # copi-blackbird = this repo.  copi-python = org1, DO NOT TOUCH.
> ```
>
> Two more rules that follow from the shared host:
> - **Always pass `-f docker-compose.prod.yml`.** Bare `docker compose` resolves
>   to `docker-compose.yml`, a *different* (dev) stack whose web service is named
>   `app`, runs `--reload`, bind-mounts the whole repo, and binds host `:8001`.
>   The deployed stack is `docker-compose.prod.yml`, whose web service is
>   `blackbird-app`. `COMPOSE_PROJECT_NAME=copi-blackbird` in `.env` fixes the
>   project name but *not* the file.
> - **Never pass `--remove-orphans`** — it has killed org1's nginx + certbot.
>
> ⚠️ **Every `blackbird-app` command in these docs depends on an UNCOMMITTED edit
> to `docker-compose.prod.yml`.** The committed file still names the web service
> `app` (`git show HEAD:docker-compose.prod.yml`); the host's working tree
> renames it to `blackbird-app`, pins `container_name: copi-blackbird-app-1`,
> attaches it to the shared `copi-edge` network, swaps every `awslogs`
> driver for `json-file`, and (2026-08-28, for the review bot) adds a
> read-only `- ./prompts:/app/prompts:ro` mount to the **worker** service,
> which the committed file does not give the worker at all. It also
> (2026-08-30, for the simulation control panel) changes the **agent**
> service's `command` from `src.agent.main` to `src.agent.supervisor`, and
> adds `restart: unless-stopped`, `container_name:
> copi-blackbird-agent-1`, and `stop_grace_period: 420s` — none of which the
> committed file has either. The rename is not
> cosmetic — Compose adds the service name as a network alias on every
> attached network, so an `app` on `copi-edge` would collide with org1's `app`
> and org1's nginx upstream would resolve to **this** container, breaking
> copi.science. So: never `git checkout`, `git stash` or `git restore` that
> file, and on a fresh clone expect `service "blackbird-app" is not defined`
> AND a worker with no `prompts/` visibility at all until the edit is
> reapplied. The `prompts/` and `profiles/` bind mounts below are identical
> in both versions for `blackbird-app` and `agent`; the worker's `prompts/`
> mount is the one exception — it exists ONLY in the working tree, not in the
> committed file — so do not assume the worker row of any mount table matches
> `git show HEAD:docker-compose.prod.yml`. The agent service's
> `command`/`restart`/`container_name`/`stop_grace_period` lines are the same
> kind of exception — working-tree only, absent from the committed file.
> Keeping the file uncommitted was a deliberate decision on 2026-09-25 (D5 of
> `docs/plans/2026-09-25-rca-remediation-plan.md`), recorded as open in
> `docs/audits/open-findings.md`.

The simulation runs in a one-off container named `blackbird-agent-run`:

```bash
DC="docker compose -f docker-compose.prod.yml"

# Resume an existing run:
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main

# Fresh run (mints a new simulation_run_id; DELETES NOTHING):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh

# With a time limit (minutes):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --max-runtime 60
```

**`--fresh` deletes nothing.** It used to: three UNFILTERED deletes
(`AgentMessage`, `AgentChannel`, `PiDmMessage`, no `simulation_run_id`
predicate on any of them), so every previous run's conversation history went
with it. Measured 2026-08-22, before the fix: `llm_call_logs` held 10 runs and
`opportunity_assessments` 5, while `agent_messages` held **1** — run
8b64a0e0's 1,354 messages were gone, 57 of 64 assessments carried a `slack_ts`
that resolved to no message, and the assessment detail page's interview
timeline was empty for 90% of the corpus. `_open_fresh_run`
(`src/agent/main.py:121`) now only mints a new `SimulationRun` row: **the new
`simulation_run_id` IS the isolation**, every startup and main-loop read is
already run-scoped (true since 2026-08-28 — `thread_decisions`/`proposal_reviews`
were the unscoped exceptions until then, which fed prior runs' interview
summaries into fresh Phase-5 prompts), and pre-run Slack history is skipped
rather than re-imported (`_seed_slack_cursors_without_ingest` parks each polled channel's
cursor at the newest message it can see, or at the wall clock for a channel it
could not read at all — a "0" cursor made the live poller re-import the whole
back catalogue on the first tick). Consequences for an operator: rows now
**accumulate** across fresh runs, so pick the run you mean on the admin pages;
and `profiles/memory/*` is ARCHIVED, not kept: `--fresh` moves it to
`profiles/memory/archive/<UTC stamp>/` (2026-08-28) so a fresh run's prompts
carry no prior-run verdict ledger; plain resumes keep memory untouched, and
deleting a PI purges their archived copies too.

**`--budget` is deprecated.** It is a *cumulative* cap for the whole run, it is
rebuilt from `llm_call_logs` on restart, and it therefore benches an agent
permanently once crossed — a restart does not clear it. It defaults to 0 (off)
and should stay there. Pacing and runaway protection are now handled by the
sliding-window rate limiter: `llm_calls_per_load_per_window` x the agent's live
conversational load for a `pi_lab`, and the flat `hub_llm_calls_per_window`
brake for the `scout_hub`, which sits on an unpaced lane
(`SimulationEngine._allowance_for`, `src/agent/engine/scheduler.py:108`). A hub bot in a star topology will hit any
uniform cumulative cap long before any spoke does. See
`docs/specs/2026-08-06-hub-budget-scheduler-design.md`.

**Fresh runs announce themselves in Slack.** On `--fresh` (and only then), the
engine posts one `:checkered_flag: NEW EXPERIMENTAL RUN` marker per channel in
`RUN_START_ANNOUNCE_CHANNELS` (`src/config.py`, default: the six seeded
channels + `assessments-summary`; empty disables) right before the main loop —
after star-topology validation, so a run that fails startup is never
announced. The body is `prompts/run_start_announcement.md` (bind-mounted:
editable without a rebuild; the sentinel first line is prepended by code and
is NOT editable), carrying start time, planned duration, the image's git
commit/branch/dirty count (`.build_info.json`, baked by the Dockerfile —
`dirty state unknown` or an unknown commit in the announcement means an image
built before the two-stage Dockerfile: images no longer carry `.git`, and the
builder stage fails the build if `.build_info.json` cannot be written
(rebuild); a pre-feature agent image never announces at all), the hub/PI prompt-set versions
(`version` keys in the two `prompts/roles/*/role.toml`, which must be bumped
on any prompt-set edit) and the rubric version. The live Slack poller drops
sentinel-prefixed messages, so markers never enter `agent_messages` — do not
reuse that prefix for anything else, and do not change it (old markers would
start re-ingesting; see `src/agent/run_marker.py`). What
was announced is recorded under `run_start_announcement` in
`simulation_runs.config`.

### How a run ends, and what a resume restores (audit remediation, 2026-09-29)

Why a run ended decides what its shutdown sweep announces (`src/agent/end_reasons.py`):

| End | How | Shutdown sweep | Run row |
|---|---|---|---|
| Stop, SIGTERM | admin Stop; `docker stop -t 420` | every owed headline, open interviews included, capped at 25 | resumable |
| Stop — hold open interviews | the second admin button | only interviews with a `thread_decisions` row | `held_at` set |
| stall, escaped exception, failed start | automatic | as the hold | `held_at` set |
| lost engine lock | automatic | none: the owed headlines are left for the repair script | `held_at` set |
| natural end | `--max-runtime` reached, `--max-proposals` drained | every owed headline | `finalized_at` set; a resume is refused (`RunFinalized`), start fresh |

The production defaults (`max_runtime = 0`, `max_proposals = 0`) never reach a natural end.

- **A resume restores from the database only.** The Slack reconcile is gone; the
  engine's own posts are written to `agent_messages` before they count; the poll cursors
  are parked past the history already on Slack. Slack-native messages posted while the
  engine was down are not recovered. On a resume the hub subscribes to its channels and
  interviews any lab pitch from the last 14 days that has no thread yet. A resume clears
  `held_at`.
- **Headline claims.** Every headline (engine or
  `scripts/backfill_assessment_headlines.py`) claims its interview
  (`opportunity_assessments.summary_claimed_at`) immediately before posting. A post that
  raised with no Slack response keeps its claim and is logged IN DOUBT; nothing re-posts
  it. Check `#assessments-summary`, then run the script with `--list-in-doubt` and, for
  one that did not post, `--release-in-doubt <id>`.
- **LOST and HELD lines at shutdown.** `LOST n` names headlines that were never posted
  (past the 25 cap, no connected hub, Slack off) with the exact repair command; `HELD n`
  names the open interviews a hold kept back. Release held ones with a resume, or with
  `--finalize --apply`, which also finalizes the run.
- **A second sweep, failure path only.** A verdict or thread decision that could not be
  written before the sweep (its message rows would not flush) is written by `stop()`'s
  final flush, after the memory drain; the sweep then runs again for those interviews
  only, so their headlines still post before the process exits. A healthy stop has
  nothing queued at that point and sweeps once.
- **The script refuses to write** (exit 2) while an engine holds the engine advisory lock,
  and holds that lock itself for the whole write. The status row is not consulted, so a
  clean CLI exit needs nothing extra and a dead engine frees the lock at once.
  `--run-crashed` is a deprecated no-op. The dry run and `--list-in-doubt` take no lock.
- **Slack-off runs work end to end** (NEW-1): `NullTransport.ajoin_channel` exists, so
  labs reach Phase 5 in DB-only runs, which the local rehearsal before a deploy needs.

### Engine lock, heartbeat and Finalize run

- **One engine at a time.** The engine takes a Postgres advisory lock
  (`ENGINE_LOCK_KEY`, `src/services/advisory_locks.py`) on a dedicated connection before it
  writes anything. A second engine, from the CLI or the supervisor, raises
  `EngineAlreadyRunning` and exits without touching the run. The lock frees the instant its
  holder dies, so the panel trusts the lock, not the status row, for "is an engine up".
- **Panel states:** `not_deployed` (no status row), `idle`, `starting`, `running`,
  `stopping` and `unresponsive`. `unresponsive` means the lock is held but the heartbeat
  (every 30 s, on its own connection) is older than 120 s; Stop still reaches it. A fresh
  `running` row with no lock holder (an old lock-less engine, or one that crashed within
  120 s) reads `idle`: Stop answers "Nothing is running." and Start is refused only while a
  start or a Finalize run stop is pending.
- **Stops are claimed only by the engine's control poll** (every `CONTROL_POLL_INTERVAL`,
  30 s), so a stop lands on the same tick as before; the heartbeat task never claims a
  command. A stop still pending after a run, and a Finalize run stop pending at boot, is
  settled by the supervisor (`_settle_pending_stops`): a Finalize run runs
  `HeadlineAnnouncer.finalize` under the engine lock, any other stop finishes "run already
  stopped" if no engine holds the lock.
- **Finalize run** is the button on a stopped run's `/admin/activity/<id>` page
  (`POST /admin/simulation/finalize-run`). It announces the owed headlines of that run,
  then sets `finalized_at`. A live engine fails the command ("Finalize run applies to a
  stopped run"). Start is refused while a Finalize run stop is pending ("A Finalize run is
  pending; start after it finishes."). After a finalized run the Start form forces Fresh.
- **A lost lock connection** ends the run with the HOLD reason `lock_lost` and the run is
  marked held. The shutdown sweep then posts NO headline, because another engine or the
  repair script may hold the lock by then; the ERROR line names the repair command
  (`scripts/backfill_assessment_headlines.py --run <id> --apply` for ended interviews, or
  `--finalize --apply` to release them all).
- **Schema check at start.** The engine refuses to start (before any write or memory
  archive) on a database without `uq_opportunity_assessments_run_thread` (migration
  `0055`), since every threaded verdict write would fail there. The supervisor records the
  start `failed` with the message; apply the migrations and start again.
- **Circuit breaker.** Repeated model-call failures across interviews open an engine-wide
  breaker; `/admin/simulation` shows "LLM calls paused" and calls resume after ten minutes.
  A single interview whose replies keep failing with non-transient errors while other calls
  succeed is abandoned with an `assessment_drops` row of reason `reply_failed`.
- **`MAX_THREAD_MESSAGES` other than 12** makes the engine refuse to start
  (`EngineConfigError`): the phase guidance's CONCLUDE turn is written for 12.
- **Prompt snapshot.** `PromptSnapshot` (`src/agent/prompt_snapshot.py`) loads prompts,
  personas, manifests and the rubric once per start; the heartbeat checks the disk every
  minute and the panel shows "Prompt set changed on disk — restart to apply". Each start
  appends a `loaded_stamps` entry to the run's config.

> ⚠️ **As of 2026-08-22 every one of those numbers counts REAL API CALLS, where
> it used to count turns — and none of them has been re-tuned.**
> `Agent.record_api_call` booked six unreserved sites (besides the two reserved
> turns) but never the extra TOOL ROUNDS
> inside `generate_with_tools`, so a turn that used three rounds before its
> terminating text call made four billed calls and was metered as one. 78.6% of
> stored `thread_reply` rows are 2+ calls. `_on_llm_call` now books the
> unbooked `kind == "round"` entries live, and the restart rebuild moved with it
> (`COALESCE(jsonb_array_length(call_stats), 1)`, steps 4 and 4b of
> `_rebuild_agent_state`, `src/agent/engine/rebuild.py:172`) — otherwise every restart would
> silently loosen the throttle by the calls-to-turns ratio. The COALESCE is
> load-bearing, not tidiness: 4,650 of 5,771 stored rows have `call_stats IS
> NULL` (the column arrived in `0032`) and NULL propagates through SUM.
>
> The practical effect: `llm_calls_per_load_per_window` is still **8**
> (`src/config.py:453`) and `hub_llm_calls_per_window` still **600**
> (`src/config.py:459`), but each now buys roughly 2-3x FEWER effective turns
> for any agent whose turns use tool rounds — which is the hub, on essentially
> every `thread_reply`. Expect the hub to be throttled sooner and to take fewer
> turns per window than the pre-2026-08-22 calibration notes in `config.py`
> describe. **Do not "fix" this by raising the setting on your own initiative**
> — it is a tuning decision, and the numbers stand as measured until the owner
> re-tunes them.
>
> `SimulationRun.total_api_calls` changes units with them and is **not
> comparable with any run recorded before 2026-08-22**. The old per-turn figure
> is recoverable for any run as `SELECT COUNT(*) FROM llm_call_logs WHERE
> simulation_run_id = <run>`. The startup banner declares this (see step 6
> below).

**Before restarting**, always save logs and rebuild containers:

```bash
DC="docker compose -f docker-compose.prod.yml"

# 1. Stop the old container FIRST — GRACEFULLY — then save logs. `docker rm -f`
#    sends SIGKILL, which skips the shutdown flush and permanently loses the
#    in-flight turn's messages (the DB, not Slack, is the durable store).
#
#    -t 420, NOT -t 30 (and not -t 180 or -t 300 either, as of the
#    thread_reply max_tokens raise below). Measured 2026-08-19: a single
#    `thread_reply` turn runs up to 134s, and `request_stop()` is cooperative — it
#    flips a flag, and the flush in src/agent/main.py's finally-block only
#    runs once the main loop RETURNS. At -t 30 and even -t 90, SIGKILL landed
#    mid-turn and the flush never ran; 6 buffered llm_call_logs rows were
#    lost. `generate_with_tools` now polls the flag and stops opening new
#    tool rounds (`should_continue`), which bounds a stopping turn to the
#    round already underway plus one final call — but the grace period must
#    still exceed that.
#
#    How many real API calls a turn can be, corrected 2026-08-22: the loop is
#    `range(max_tool_rounds + 1)` (src/services/llm.py:1360), so the setting
#    UNDER-counts by one. A turn is 1..8 billed calls at the default
#    `max_tool_rounds=5` — up to max_tool_rounds + 1 tool-capable calls, then a
#    terminating or forced-final call, then at most one max_tokens retry. The
#    comment on `_call_log_callback` said "1..7" until then, taking the
#    setting's name at face value. Each specialist consult is 25-40s, and up to
#    8 of them run concurrently (`_API_MAX_CONCURRENCY=8` on a 12-thread pool of
#    llm.py's own, `_API_EXECUTOR_MAX_WORKERS`) rather than serially.
#
#    180 -> 420 for the 2026-08-21 thread_reply max_tokens raise (4000 ->
#    16000, src/agent/engine/reply_lane.py): a single 16000-token final call can run
#    ~4-5 minutes at Opus output rates, and it is the round `should_continue`
#    already lets finish — so it cannot be interrupted, only awaited out.
#    Total turn time is not much worse (this one call replaces what used to be
#    a call-then-retry pair), but it now lands in a single uninterruptible
#    call instead of two shorter ones, so the grace period has to cover that
#    call outright. Sized generously rather than to the minute: `docker stop`
#    returns as soon as the container actually exits, so a larger `-t` costs
#    nothing on the common path and is free insurance against the tail.
#
#    Verify it worked by the LOG LINE, not the exit code: "Simulation
#    stopping..." is logged by `SimulationEngine.stop()` after the headline
#    sweep, the bounded memory drain, the gathered `_flush_tasks` and every
#    `final=True` flush (messages, LLM logs, thread decisions, verdicts); only
#    the heartbeat cancel follows it. If you see it, the buffers are on disk.
#
#    Exit 137 no longer implies a lost flush (corrected 2026-08-22). Two other
#    changes moved that: `_drain_and_flush` now runs in the main loop's
#    `finally`, so EVERY exit from an iteration flushes, and `_get_api_executor`
#    returns a pool of llm.py's OWN that is deliberately never shut down — so
#    `asyncio.run` can return and the flush can complete while an API request is
#    still blocked in a thread, with the process then hanging at interpreter
#    exit for up to CLIENT_READ_TIMEOUT_SECONDS (300s). So:
#      * 137 WITHOUT "Simulation stopping..."  -> SIGKILL mid-turn; assume loss.
#      * 137 WITH "Simulation stopping..."     -> an orphaned API thread held the
#        process past the grace period. Data is safe; nothing to redo.
#    Either way 137 is SIGKILL, NOT necessarily an OOM.
docker stop -t 420 blackbird-agent-run
docker inspect blackbird-agent-run --format 'exit={{.State.ExitCode}}'

# 2. Save logs — AFTER the stop, so the shutdown lines are captured.
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f
docker rm blackbird-agent-run

# 3. BUILD the web tier AND the agent image (both bake src/ into the image).
#    BUILD ONLY — do NOT start anything yet. `up -d --build` builds and starts
#    in one step, which serves the new code against the old schema; every
#    migrate-before-serve box in docs/operations/migration-deploy-notes.md exists because that direction breaks the
#    live site. Splitting build from start is what makes step 4 possible at all.
$DC build blackbird-app worker
$DC --profile agent build agent

# 4. Apply migrations — NOTHING ELSE DOES. See the warning below.
#    Prefer the guarded path, `./scripts/migrate/run_migration.sh` (rehearse it
#    without --apply first; see the box below). The two commands here are the
#    unguarded alternative: no dump, no preflight, no postflight.
#    From a ONE-OFF container off the image you just built, not `exec`: a new
#    revision only exists in the new image, so `exec` into the old running
#    container cannot see it.
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current   # confirm it matches `alembic heads`

# 5. Now start the web tier on the migrated schema
$DC up -d blackbird-app worker

# 6. Start the new run. Check the three-line startup banner:
#      Starting simulation: N agents, ... max runtime, 0 budget/agent (resuming)
#      Screening rubric: version X (content hash Y)
#      API-call accounting: ... counts REAL API CALLS ... not turns, as of 2026-08-22
#    All three come from src/agent/main.py; the third is `_log_api_call_units`
#    and is the tell that you are on 2026-08-22-or-later code.
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

> ### ⚠️ Nothing migrates the database for you. Step 4 is not optional.
>
> The prod web command is a bare `uvicorn` (`docker-compose.prod.yml`), there is
> no `alembic upgrade` at startup, and no `create_all` anywhere in `src/main.py`
> or `src/database.py`. So a rebuild + restart runs the **new code against the
> old schema**, and the failure is either silent or total depending on where it
> lands: an engine WRITE that hits a missing column raises, gets swallowed by a
> best-effort `except`, and leaves one ERROR line in a log nobody is tailing,
> while a web-tier READ of a newly-mapped column 500s the page outright (see the
> `0028`/`0030`/`0036` boxes in `docs/operations/migration-deploy-notes.md`). That asymmetry is why step 3 is build-only.
>
> Measured 2026-08-06: production sat at `0024` while the branch head was `0025`.
> Restarting without step 4 would have made every `_persist_assessment` fail on a
> missing `opportunity_assessments` and lost **every** screening verdict, while
> Slack posts continued to look completely normal.
>
> Check before you start a run, not after:
>
> ```bash
> $DC exec -T postgres psql -U copi -d copi -t -A -c \
>   "SELECT version_num FROM alembic_version;"   # must equal `alembic heads`
> ```
>
> `scripts/migrate/run_migration.sh` is the guarded path for a populated
> database: dump → preflight → apply → read-back → postflight. Every in-image
> step runs in a one-off container off the image you just built (`run --rm
> --no-deps`, never `exec`), and it refuses an image whose `.build_info.json`
> names a commit other than the host's HEAD. Its default target is the image's
> own `preflight.DEFAULT_TARGET`, and it mounts the host `backups/` at
> `/app/backups` for the dump. Use it as step 4 in place of `alembic upgrade
> head`: rehearse first with `./scripts/migrate/run_migration.sh` (no
> `--apply`; writes nothing), then run it again with `--apply`. Its defaults
> are `docker-compose.prod.yml` and `blackbird-app`.

> ### ⚠️ The agent image does NOT mount `src/`. Rebuild it, or you deploy stale code.
>
> The `agent` service in `docker-compose.prod.yml` mounts only `./profiles`,
> `./prompts` and `./data`. **`src/` is baked into the image at build time.** So
> `$DC build blackbird-app worker` does *not* update the simulation — it builds
> the web tier only, and `docker compose run agent` then starts the **previous**
> image. Measured 2026-08-06: a rebuild that skipped step 3's second line
> launched a run on hours-old code, silently, with no error — the startup banner
> (`Starting simulation: N agents, ... budget/agent`) was the only tell. It now
> has a third line (`API-call accounting: ...`, `_log_api_call_units`), which is
> a sharper tell for the same mistake: its absence means the image predates
> 2026-08-22.
>
> After any `src/` change, always run `$DC --profile agent build agent` before
> starting a new run, and check the startup banner matches what you expect.
>
> What the image does carry is the tracked tree only: `.dockerignore` keeps
> `.env`, `backups/`, `data/`, `logs/`, `profiles/` and `.venv-test` out of the
> build context, and the builder stage's `git clean -ffdx` drops any other
> untracked file, so services get their environment from compose's `env_file:`,
> never from a baked `.env`. The flip side: a NEW template, prompt, script or
> alembic revision that is still untracked is silently left out of the image, so
> `git add` it before building. Before `$DC build`, `git status --porcelain
> --untracked-files=all -- src templates static prompts alembic scripts
> pyproject.toml alembic.ini` must print nothing. Never add a tracked path or `.git` to
> `.dockerignore`: the builder needs `.git` to write `.build_info.json`, and an
> excluded tracked path would count as dirty in every image
> (`tests/unit/test_docker_build_context.py`).

**Note:** The agent container loads Python modules only at startup, so **code**
changes require rebuilding the image (above) and restarting the container. **After any code change that affects the running agent process, flag this to the user so they can decide whether to restart.** (Roster changes — activating/inactivating agents or setting a new `slack_bot_token` in `AgentRegistry` — do NOT need a restart; they're picked up live by `_sync_roster_from_db`.)

**`.env` changes need a container *recreate*, not a restart.** `env_file` is
resolved when the container is created, so `docker restart` re-runs the old
environment. Step 2 + step 6 above (rm, then `run`) is what actually picks up an
edited `.env`. For the web tier the equivalent is `$DC up -d --force-recreate
blackbird-app` — and one `.env` key now fails the site closed if it is wrong,
see "The Origin guard" in `docs/operations/pis-and-access.md`.

### Simulation control plane (2026-08-30)

`src.agent.supervisor` is now the `agent` service's `command` (see the
two-stack warning above — that line, like `restart: unless-stopped`,
`container_name: copi-blackbird-agent-1` and `stop_grace_period: 420s`, lives
only in the uncommitted compose working tree). It replaces the one-off `$DC
--profile agent run -d --name blackbird-agent-run agent python -m
src.agent.main` invocation above for NORMAL operation: with `restart:
unless-stopped` and a stable `container_name` in place, the supervisor is
started like any other long-lived service, `$DC --profile agent up -d agent`,
and stays up across host reboots and crashes rather than living and dying
with a single `docker run`.

- **Starts happen ONLY from `/admin/simulation`**, or the CLI emergency path
  documented above (`docker compose run` with an explicit `python -m
  src.agent.main ...` command) — compose v2 clears the restart policy on a
  `run` one-off, so that emergency path can never be auto-resurrected by
  `restart: unless-stopped`. The supervisor process itself never starts a
  simulation run on its own.
- **`stop` issued from the admin page is gentler than `docker stop`**: it
  asks the already-running engine to stop in-process, which drains and
  flushes exactly like `SimulationEngine.stop()` always has, with no
  container-level SIGTERM/grace-period dance needed at all.
- **On boot the supervisor stales any pending start command** rather than
  acting on it — it never auto-starts a run on container (re)start, which is
  the same never-auto-start-the-simulation policy these docs have always
  followed, now enforced by the supervisor itself rather than by operator
  discipline alone. A host reboot or a plain `$DC up -d agent` brings the
  supervisor back IDLE, waiting for an operator to start a run from
  `/admin/simulation`.
- **A code deploy is now**: `$DC --profile agent build agent` then `$DC up -d
  agent` — there is no `run -d --name blackbird-agent-run` step anymore. Per
  the previous bullet, the supervisor container comes back up IDLE; starting
  a run after a deploy is a separate, explicit operator action from
  `/admin/simulation`.
- **`docker stop -t 420` semantics are unchanged.** Everything the "Before
  restarting" section above says about the 420s grace period still applies
  verbatim to the supervisor container. `stop_grace_period: 420s` in the
  compose file gives that same 420s as compose's own default (compose's
  built-in default is 10s), so a bare `docker compose stop agent` no longer
  needs an explicit `-t 420` to avoid a premature SIGKILL — but running
  `docker stop -t 420 copi-blackbird-agent-1` by hand remains correct and
  loses nothing.

**`/admin/simulation`'s Live tab renders dollar cost from a versioned,
hand-maintained price table, not from anything Anthropic returns at call
time.** `PRICES`/`AS_OF` in `src/services/llm_pricing.py` are the whole
table — repricing is an edit to that one file plus a **web-tier rebuild
only** (`$DC build blackbird-app`; the agent image never renders costs, so
it does not need rebuilding for a price change). An unpriced model name
renders as "unpriced" rather than a silent $0. Rows written before migration
`0036` have NULL cache-token columns (`llm_call_logs.cache_read_input_tokens`
/ `.cache_creation_input_tokens` did not exist yet), which the cost service
reads as zero — the page labels any aggregate touching one of those rows
with "≥" (`CostSummary.is_floor` / `InterviewCost.is_floor`): the true cost
of a pre-`0036` run is that number or higher, never less, and the read path
never re-derives this from anything except the presence of NULLs. Both the
page's engine-status badge and its Start/Stop buttons are only as honest as
the heartbeat: the running engine upserts the single
`simulation_process_status` row roughly every 30s
(`CONTROL_POLL_INTERVAL`, `src/agent/engine/constants.py:112`), and `derive_panel_state`
treats it as `stale` once `updated_at` is more than
`HEARTBEAT_STALE_SECONDS` (120s) old — a stale row disables the Stop button
(nothing may be listening) and greys the per-agent live columns to "—"
rather than showing a frozen last-seen number. `not_deployed` is a distinct,
narrower state reserved for a `simulation_process_status` table with no row
at all — it is upserted once (id=1) and never deleted by any code path, so
once the supervisor has checked in even once in an environment, a later
`stop` degrades the page to `stale`, never back to `not_deployed`.
