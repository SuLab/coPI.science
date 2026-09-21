# CLAUDE.md

## Testing

Run `./scripts/ci.sh` before committing — alembic sanity (single head, no
duplicate revision ids), an upgrade→downgrade→upgrade round trip against a
throwaway Postgres it creates and destroys itself, `ruff check` on the test
suite (zero findings) plus a ratcheted ceiling on `src/`, then the full pytest
run with a branch-coverage floor. This is exactly what the `pre-push` hook
runs, and it is the whole gate: there is no server-side CI.

**The supported way to run pytest alone is on the host, not inside a
container:**

```bash
.venv-test/bin/python -m pytest tests/ -v
```

(If `.venv-test` doesn't exist yet: `uv venv .venv-test && uv pip install
--python .venv-test/bin/python -e '.[dev]'`.) The host has a Docker socket, so
with `TEST_DATABASE_URL` unset, `tests/conftest.py` spins its own ephemeral
Postgres via testcontainers and migrates it with the real alembic chain — no
container, no manual database, no env var needed. This is exactly what
`scripts/ci.sh` runs.

> ### ⚠️ Two host/sshfs hazards that have each cost multiple sessions real time.
>
> 1. **Never run `pip install` against `.venv-test` from a client mounting this
>    repo over sshfs.** It corrupts the venv's console-script shebangs — they
>    get rewritten to the client's own interpreter path, which does not exist
>    on the host — and every DB-backed test then fails with a plain
>    `FileNotFoundError` that has nothing obviously to do with the real cause.
>    Run `pip install`/`uv pip install` against `.venv-test` on the host itself.
> 2. **Running pytest through an sshfs-mounted checkout can be dramatically
>    slower than on the host's local disk** — measured as much as 100-400x
>    slower in practice, from FUSE round-trips on every file read. If a run
>    that normally takes minutes appears to hang, check whether you're on an
>    sshfs mount before assuming a real regression.

> ### ⚠️ The suite does not necessarily run production's Anthropic SDK.
>
> `pyproject.toml` pins only `anthropic>=0.26.0`, and the two environments have
> resolved different versions: the deployed agent image has **1.0.0**, while
> `.venv-test` has **0.120.2** (both measured 2026-08-21). So a test that passes
> here says nothing certain about SDK behaviour in the container. **Do not
> "fix" this by tightening the pin** — dependency churn on a live deployment is
> the riskier move; just know the skew is there when a failure smells like the
> client library.
>
> The one SDK behaviour this has already cost us is the non-streaming
> `max_tokens` ceiling: `BaseClient._calculate_nonstreaming_timeout` refuses any
> non-streaming request with `max_tokens > 21_333`
> (`3600 * max_tokens / 128_000 > 600s`). **Both** versions carry that guard,
> verified in each — but no test could see it, because the suite drives
> `tests/fakes.py`'s `FakeAnthropic` and never reaches the real client. That is
> why the fake now enforces the same limit itself
> (`_MAX_NONSTREAMING_MAX_TOKENS`, re-derived from the SDK's arithmetic rather
> than imported from `src`), and why `src/services/llm.py` clamps every
> truncation retry to `NONSTREAMING_MAX_TOKENS` and raises on a call site above
> it. See `tests/unit/test_llm_nonstreaming_ceiling.py`.
>
> **The SDK no longer enforces that ceiling for us, and has not since the 300 s
> client timeout landed.** `Messages.create` applies
> `_calculate_nonstreaming_timeout` only `if not stream and not
> is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT`, and
> `_client_for_key` now constructs its client with
> `anthropic.Timeout(CLIENT_READ_TIMEOUT_SECONDS, connect=5.0)`
> (`src/services/llm.py:41`, `:121`) — so that condition is permanently false
> and the SDK will happily send a request the API rejects. `_acreate`'s own
> check, which raises `NonStreamingMaxTokensError` (`:207`, raised at `:373`),
> is now the ONLY enforcement in the process. Do not remove it on the grounds
> that the SDK checks too; it does not.

Running pytest **inside the container** (`docker compose exec blackbird-app
python -m pytest ...`) does not currently work: the image installs with
`pip install --no-cache-dir .` (`Dockerfile:14`), with no `[dev]` extra, so
pytest is not installed there (verified: `exec blackbird-app python -c "import
pytest"` → `ModuleNotFoundError`). Restoring that path would need the image (or
a test-targeted variant of it) to install `.[dev]` instead.

If that path is ever restored, `TEST_DATABASE_URL` becomes required again: the
web container has no Docker socket, so without it every test that needs a
database errors out (469 of them when that was measured on 2026-08-04 — treat
it as a floor, not a current count: the 2026-08-22 correctness branch alone
added 17 test files, many DB-backed, and the suite is now 183 `test_*.py`
files):

```bash
docker compose -f docker-compose.prod.yml exec -T \
  -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_a3 \
  blackbird-app python -m pytest tests/ -v
```

The service is **`blackbird-app`**, not `app` — see the two-stack warning under
"Running the Agent Simulation" for why the `-f docker-compose.prod.yml` is not
optional.

Whichever way you run it, a database named in `TEST_DATABASE_URL` must already
exist — the suite migrates it, it does not create it. Add a fresh scratch DB
with `docker compose -f docker-compose.prod.yml exec -T postgres createdb -U
copi copi_xN`, and give concurrent suites distinct names so they do not
migrate each other's schema mid-run. Never point `TEST_DATABASE_URL` at `copi`,
the dev database.

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
> ⚠️ **Every `blackbird-app` command in this file depends on an UNCOMMITTED edit
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
(`src/agent/main.py:99`) now only mints a new `SimulationRun` row: **the new
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
(`SimulationEngine._allowance_for`). A hub bot in a star topology will hit any
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
`dirty state unknown` in the announcement means `.build_info.json` was
missing and the `.git` fallback served (rebuild to restore the dirty count);
a pre-feature agent image never announces at all), the hub/PI prompt-set versions
(`version` keys in the two `prompts/roles/*/role.toml`, which must be bumped
on any prompt-set edit) and the rubric version. Both Slack-ingest paths drop
sentinel-prefixed messages, so markers never enter `agent_messages` — do not
reuse that prefix for anything else, and do not change it (old markers would
start re-ingesting on the next resume; see `src/agent/run_marker.py`). What
was announced is recorded under `run_start_announcement` in
`simulation_runs.config`.

> ⚠️ **As of 2026-08-22 every one of those numbers counts REAL API CALLS, where
> it used to count turns — and none of them has been re-tuned.**
> `Agent.record_api_call` booked six sites but never the extra TOOL ROUNDS
> inside `generate_with_tools`, so a turn that used three rounds before its
> terminating text call made four billed calls and was metered as one. 78.6% of
> stored `thread_reply` rows are 2+ calls. `_on_llm_call` now books the
> unbooked `kind == "round"` entries live, and the restart rebuild moved with it
> (`COALESCE(jsonb_array_length(call_stats), 1)`, steps 4 and 4b of
> `_rebuild_agent_state`, `simulation.py:6569`) — otherwise every restart would
> silently loosen the throttle by the calls-to-turns ratio. The COALESCE is
> load-bearing, not tidiness: 4,650 of 5,771 stored rows have `call_stats IS
> NULL` (the column arrived in `0032`) and NULL propagates through SUM.
>
> The practical effect: `llm_calls_per_load_per_window` is still **8**
> (`src/config.py:412`) and `hub_llm_calls_per_window` still **600**
> (`src/config.py:418`), but each now buys roughly 2-3x FEWER effective turns
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
#    `range(max_tool_rounds + 1)` (src/services/llm.py:1346), so the setting
#    UNDER-counts by one. A turn is 1..8 billed calls at the default
#    `max_tool_rounds=5` — up to max_tool_rounds + 1 tool-capable calls, then a
#    terminating or forced-final call, then at most one max_tokens retry. The
#    comment on `_call_log_callback` said "1..7" until then, taking the
#    setting's name at face value. Each specialist consult is 25-40s, and up to
#    8 of them run concurrently (`_API_MAX_CONCURRENCY=8` on a 12-thread pool of
#    llm.py's own, `_API_EXECUTOR_MAX_WORKERS`) rather than serially.
#
#    180 -> 420 for the 2026-08-21 thread_reply max_tokens raise (4000 ->
#    16000, src/agent/simulation.py): a single 16000-token final call can run
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
#    stopping..." is logged by `SimulationEngine.stop()` as its LAST statement,
#    after the bounded memory drain, after `_flush_tasks` are gathered, and
#    after all three `final=True` flushes. If you see it, the buffers are on
#    disk.
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
#    migrate-before-serve box below exists because that direction breaks the
#    live site. Splitting build from start is what makes step 4 possible at all.
$DC build blackbird-app worker
$DC --profile agent build agent

# 4. Apply migrations — NOTHING ELSE DOES. See the warning below.
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
> `0028`/`0030`/`0036` boxes). That asymmetry is why step 3 is build-only.
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
> database (preflight → apply → postflight), but it shells out to a bare
> `docker compose` and defaults `SVC=app`, neither of which matches this stack.
> Override both: `COMPOSE_FILE=docker-compose.prod.yml
> MIGRATE_SERVICE=blackbird-app ./scripts/migrate/run_migration.sh --apply`.

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

**Note:** The agent-run container loads Python modules only at startup, so **code**
changes require rebuilding the image (above) and restarting the container. **After any code change that affects the running agent process, flag this to the user so they can decide whether to restart.** (Roster changes — activating/inactivating agents or setting a new `slack_bot_token` in `AgentRegistry` — do NOT need a restart; they're picked up live by `_sync_roster_from_db`.)

**`.env` changes need a container *recreate*, not a restart.** `env_file` is
resolved when the container is created, so `docker restart` re-runs the old
environment. Step 2 + step 6 above (rm, then `run`) is what actually picks up an
edited `.env`. For the web tier the equivalent is `$DC up -d --force-recreate
blackbird-app` — and one `.env` key now fails the site closed if it is wrong,
see "The Origin guard" below.

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
  the same never-auto-start-the-simulation policy this file has always
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
(`CONTROL_POLL_INTERVAL`, `src/agent/simulation.py`), and `derive_panel_state`
treats it as `stale` once `updated_at` is more than
`HEARTBEAT_STALE_SECONDS` (120s) old — a stale row disables the Stop button
(nothing may be listening) and greys the per-agent live columns to "—"
rather than showing a frozen last-seen number. `not_deployed` is a distinct,
narrower state reserved for a `simulation_process_status` table with no row
at all — it is upserted once (id=1) and never deleted by any code path, so
once the supervisor has checked in even once in an environment, a later
`stop` degrades the page to `stale`, never back to `not_deployed`.

## Adding New PIs

**The `AgentRegistry` table is the single source of truth for the agent roster.**
There is no longer a `PILOT_LABS` list and no per-agent `config.py` token fields to
edit. A running `agent-run` re-syncs the roster from the DB every ~30s
(`_sync_roster_from_db`), so flipping an agent to `status='active'` (with a token on
its row) makes it go live **without a restart**.

**The one-step path (2026-08-24): the manager PIs tab.** `POST /manager/pis`
(the "Add a PI by ORCID iD" form on `/manager/pis`) now does, in ONE commit:
create the User, enqueue the `generate_profile` job, mint a **pending**
`AgentRegistry` row (inert — the roster sync loads only `status='active'`),
and record the ORCID-employment-derived JHU tenure start. The atomicity is
deliberate: the job and the agent row commit together, so the worker can never
run the pipeline before the row exists — the old seed-then-create-row order
lost the markdown export and revision every time (that is what
`scripts/backfill_agents.py` repairs). The profile job runs the **corpus
pipeline** (`src/services/corpus.py`: ORCID + OpenAlex + PubMed-by-ORCID +
name-affiliation search, identity-gated, consortium-excluded, year-ranked,
50-cap last) and the synthesis/export are tenure-filtered
(`src/services/jhu_rules.py`; per-user `app_settings` keys, legacy agent_id
map still read as fallback — `scripts/migrate_tenure_map.py` migrates the 62
curated entries). A wrong or missing tenure year is correctable on the manager
Edit Profile form ("JHU tenure start"). A corpus-stage failure FAILS the job
(retry ×3 → dead, visible on /admin/jobs and the PI detail page) instead of
storing a thin ORCID-only profile. **Activation is gated**: `admin_approve_agent`
refuses to flip a `pi_lab` agent to `active` — through the approve button OR
the status dropdown — when its profile is missing/ungrounded or its newest
generation job is dead, unless the logged "activate anyway" override is
checked (`src/services/agent_activation.py`). The CLI `seed-profiles` path
below still works but creates NO agent row and derives NO tenure entry.

The `generate_profile` job now enqueues two follow-on jobs of its own,
`enrich_grants` and `industry_evidence`, which run on the worker independently
of the corpus pipeline and never block or fail the profile it followed. The
first pulls NIH RePORTER grants for the PI, tenure-filters them, and
supplements (never replaces) the ORCID-fundings seed in `grant_titles`; the
second scores industry interest from OpenAlex/PubMed/USPTO/ClinicalTrials.gov
evidence. Neither writes anything a prompt or a profile export reads — the
grants panel and the industry-interest score are manager-only surfaces on
`/manager/pis/{id}`, each row individually vetoable ("not this PI's") via the
two veto routes below. `scripts/enqueue_enrichment.py` backfills both jobs for
PIs who predate this feature — it previews by default, needs `--apply` to
enqueue for real, and takes `--only grants` / `--only industry` and
`--orcid` to scope a run.

### 1. Create user records and generate profiles

Look up each PI's ORCID ID (search orcid.org or the ORCID public API). Add them to `orcids.txt` with a comment line, then seed:

```bash
docker compose -f docker-compose.prod.yml exec blackbird-app python -m src.cli seed-profiles --file new_orcids.txt
```

This creates `User` rows and enqueues profile generation jobs (processed by the worker).

### 2. Create agent registry entries

Each agent needs an `AgentRegistry` row with a unique `agent_id` (lowercase last name)
and `bot_name` (`{LastName}Bot`), created `status='pending'`. Self-service signups
(`src/routers/agent_page.py`) and the backfill scripts both create these automatically.

**Last-name collisions:** If a last name is already taken (e.g., Chunlei Wu = `wu`), prefix with the first initial (e.g., Peng Wu = `pwu` / `PWuBot`). The web UI applies this logic automatically.

### 3. Provision the Slack bot + activate (admin UI)

Go to **/admin/agents → the pending agent → Provision**. This creates the Slack app
via the Manifest API and sends you to Slack's install screen; on approval you return to
the page with the **bot token filled in and saved to `AgentRegistry.slack_bot_token`**.
Click **Approve & Activate** to set `status='active'`. The running simulation picks the
agent up on its next roster sync — no `.env` edit, no `config.py` edit, no restart.

Requires `SLACK_CONFIG_TOKEN` / `SLACK_CONFIG_REFRESH_TOKEN` in the environment (the
rotating pair is persisted in the `app_settings` KV table) and a public `base_url`.

**Bulk provisioning** (many agents at once) still uses the host script. First export the
roster from the container, then run the script on the host:

```bash
docker compose -f docker-compose.prod.yml exec blackbird-app python scripts/export_agent_roster.py   # writes data/agent_roster.json
python3 scripts/provision_slack_bots.py                               # host: creates apps, prints OAuth URLs
```

The host script writes tokens to `.env`; import them into the DB column with:

```bash
docker compose -f docker-compose.prod.yml exec blackbird-app python scripts/backfill_agent_tokens.py
```

(`.env` + `config.py get_slack_tokens()` remain a read fallback, but the DB column is
authoritative.)

## The Origin guard (every non-GET request, added 2026-08-22)

`OriginGuardMiddleware` (`src/main.py:107`) refuses any request whose method is
not GET/HEAD/OPTIONS unless it proves it came from our own origin. It is added
LAST in `create_app` and is therefore the OUTERMOST middleware — a forged POST
is refused before the session is even opened — and that position is pinned
structurally by
`tests/integration/test_origin_guard.py::test_the_guard_is_the_outermost_middleware`.
It exists because `same_site="lax"` was the only defence and is void here: one
nginx serves `blackbird.copi.science`, `copi.science` and `devel.copi.science`,
SameSite is computed on the registrable domain, so a page on either sibling
could auto-submit `POST /profile/delete-account` (cascades nine tables) or,
against a signed-in admin, `POST /admin/users/{id}/role`.

Three operator consequences, in order of how much they will cost you:

1. ⚠️ **A wrong or missing `BASE_URL` fails the site CLOSED, site-wide.** The
   expected origin is `normalized_origin(settings.base_url)`, and when that is
   `None` the guard sets `allowed = False` unconditionally rather than comparing
   equal to everything (`src/main.py:163-166`). Every login POST, every form,
   every admin action 403s with `Cross-site request refused.` while GETs keep
   rendering normally — so the site looks up. Production is
   `BASE_URL=https://blackbird.copi.science` (`.env:31`); the *default* is
   `http://localhost:8000` (`src/config.py:135`), which is a perfectly valid
   origin and will therefore silently refuse everything in production. Ports are
   normalised (`https://host:443` == `https://host`) and a trailing slash is
   tolerated, so those are not the failure mode; a scheme/host mismatch is.
2. **`curl -X POST` against the app now needs `-H "Origin: $BASE_URL"`.** Any
   script, health check or one-off `curl` that POSTs will 403 without it.
   `Sec-Fetch-Site: same-origin` works as an alternative (it is a forbidden
   header name, so a browser will not let a page forge one). The single
   exemption is `POST /settings/unsubscribe/{token}` (RFC 8058 one-click
   unsubscribe, issued server-side by Gmail/Apple/Yahoo), and only when the
   request carries **no** session cookie.
3. **`/docs`, `/redoc` and `/openapi.json` now 404**, not 401 — `create_app`
   passes `docs_url=None, redoc_url=None, openapi_url=None`, unregistering the
   routes. They were publishing the whole route inventory to anonymous callers.
   `application.openapi()` still builds the schema in-process, which is what
   `tests/unit/test_reachability.py`'s route walk needs.

Every refusal logs one WARNING naming the method, path, received origin,
`Sec-Fetch-Site` and the expected origin — grep for `Refused cross-site` first
when a form stops working after a deploy.

## Account Types (PI / manager / admin / reviewer)

**`users.user_role` is the single source of truth**, with values `pi`, `manager`,
`admin`, `reviewer`. `User.is_admin` is no longer a mapped column — it is a read-only
`hybrid_property` over `user_role`, so it still works in both SQL
(`select(User.is_admin)`) and Python, but **cannot be assigned**. Set the role
instead. The physical `users.is_admin` column stays in the database, unmapped and
defaulted. Dropping it is deferred to a separate later migration (`0042`+ — `0031`
through `0041` are all taken now: `0038` went to
`specialist_consults`'s `read_state`/`established`/rubric stamp instead, `0039`
to the reviewer-role/review-tables migration instead, `0040` went to
`prose_format`, and `0041` to `summary_posted_at`, see the
box below), which **has not been written, let alone applied** — see the design
doc's §8.

> **Deploy order for `0028_add_user_role` — migrate BEFORE the new code serves.**
> `0028` is additive and gives `is_admin` a server default, so *old code against the
> new schema* is safe: the running container keeps reading and inserting users. The
> reverse is not. The new code **maps `users.user_role`**, so it is named in the SELECT
> list of every `select(User)`, and against a pre-`0028` database each one raises
> `UndefinedColumn` — login included, for the whole gap. `up -d --build` builds and
> starts in one step, which is exactly that broken direction, and you cannot `exec`
> alembic in the *old* container because `0028` is only in the new image. Build, then
> migrate from a one-off container off that image, then start:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker

- **PI** — the original account: own profile, own lab agent, `/profile` and `/agent`.
- **Manager** — global, read-mostly: `/manager/pis`, `/manager/assessments`,
  `/manager/discussions`, `/manager/activity`. A scoped, deliberate reversal of the
  original all-GET guarantee (design D1) adds exactly eight write routes — `POST
  /manager/pis` (create a PI via ORCID), `/manager/pis/{id}/profile` (edit a PI's
  profile fields), `/manager/pis/{id}/mute` / `/unmute` (toggle a PI's agent),
  `/manager/pis/{id}/slack/provision` / `/activate` (install a pending PI's Slack
  bot and bring the agent live), and `/manager/pis/{id}/grants/{grant_id}/veto` /
  `/manager/pis/{id}/industry/{evidence_id}/veto` (mark a RePORTER-derived grant
  or a piece of industry evidence as not this PI's) — and nothing else;
  `tests/integration/test_manager_views.py`'s
  `test_manager_router_mutations_are_an_explicit_allowlist` fails loudly on a ninth.
  **Still cannot impersonate** or set roles (both stay admin-only), and there is
  deliberately no LLM-call drill-down and no export. A manager MAY provision a Slack
  bot and activate a pending PI's agent from `/manager/pis/{id}` (F2, 2026-09-10) —
  subject to the same `activate_agent` gate an admin faces but with **no** override,
  which stays admin-only; the Slack OAuth callback keeps its baked-in
  `/admin/agents/slack/callback` path and only widened its gate to staff, refusing
  any install a different account started (`slack_app_provisions.initiated_by_user_id`,
  migration `0046`).
  Managers *do* see private (`collab_private`) discussion threads — a policy
  decision, recorded in the design doc.
- **Reviewer** — read+review only, no write outside review: read-only PI directory
  and assessments (`/manager/pis`, `/manager/pis/{id}`, `/manager/assessments`,
  `/manager/assessments/{id}`), plus leaving review feedback and
  approve/disapprove via `/reviews`. Cannot assign reviewers, cannot see
  discussions/activity/prompt-suggestions, has no PI surface (`/profile`,
  `/agent`), and has no admin access. Provisioned the same way as manager/admin:
  the admin Account Type role-set on `/admin/users/{id}`, or the `role:set` CLI.
  `get_pi_user` denies it exactly as it denies a manager; `is_staff` (admin OR
  manager) deliberately **excludes** it, since the manager router's write
  handlers and the discussions/activity/prompt-suggestions pages must never
  admit a reviewer; the review-scoped predicate is a separate dependency,
  **`get_review_user`** (admin OR manager OR reviewer), gating the `/reviews`
  router and the manager router's reviewer-visible GETs.
  **Review WRITES are allowed while impersonating** (operator decision
  2026-09-10, reversing the earlier blanket refusal): feedback submit/edit and
  the approve/disapprove status writes go through, attributed to the
  *impersonated* user, with the real admin recorded in the new
  `recorded_by_user_id` second signature (`assessment_reviews` and
  `assessment_review_events`, migration `0044`; NULL means the named user acted
  in person). The three non-review actions on that router —
  reviewer assign, unassign, and prompt-suggestion status — still refuse an
  impersonated session outright (`_refuse_impersonation`,
  `src/routers/reviews.py`).
- **Admin** — everything, including `/admin/*` and impersonation.

`is_manager` means exactly `user_role == 'manager'`. The "may see the manager views"
predicate is **`is_staff`** (admin OR manager). **Never widen `is_admin`** — impersonation
(`src/dependencies.py`, and a duplicate check in `src/main.py`) is gated on it and
returns a fully substituted user, so a manager satisfying `is_admin` would be a full
privilege escalation.

**Exclude `manager`, never "non-PI".** An admin is not a `pi` either, and admins keep
the PI surfaces (`base.html` still offers them My Profile / My Agent), so a `!= 'pi'`
guard locks admins out of their own account — `/profile` bounces an admin whose
onboarding is incomplete to `/onboarding`, and only `POST /onboarding/save-profile` can
ever clear that flag. The **five** PI-write POSTs — `/onboarding/save-profile`
(`src/routers/onboarding.py:139`), `/onboarding/retry` (`:255`), `/profile/save`
(`src/routers/profile.py:113`), `/profile/refresh` (`:141`) and `/agent/request`
(`src/routers/agent_page.py:414`) — are gated on **`get_pi_user`** in
`src/dependencies.py:177`, which 403s a manager and lets an admin through. A
read-only redirect is not enough there: `save-profile` writes
`onboarding_complete` and creates the profile, which is the whole gate on
`/agent/request` — so an ungated pair is a manager with a lab bot.
`POST /profile/save` was the fifth and was left on `get_current_user` when the
other four were moved (fixed 2026-08-22, E1.3): it calls `apply_profile_edits`,
so a manager could create a `ResearcherProfile` on their own account and rewrite
`users.email` — the field delegate-invitation acceptance binds to. Managers keep
`POST /manager/pis/{user_id}/profile`, which calls the same service function
against a PI they name.

Appoint from **/admin/users/{id} → Account Type**. The last admin cannot be demoted
there (that guard counts only admins with `access_status='allowed'` — a denied admin
cannot log in, so counting one would just make demotion easier). If no admin can log in at all, recover from a container shell:

    docker compose -f docker-compose.prod.yml exec blackbird-app \
      python -m src.cli role:set --orcid 0000-0000-0000-0000 --role admin

New managers are provisioned in two steps: they sign in with ORCID (landing on
`/access-pending`), an admin approves them at `/admin/access-requests`, then sets their
role. Between approval and role-setting the account behaves as a PI.

**Revoking access now ends the session immediately** (`src/dependencies.py:72`,
fixed 2026-08-22 as E1.2). Sessions are unkeyed signed cookies with a 30-day
`max_age` and no server-side store, so `users.access_status` is the only
revocation signal there is — and nothing read it after login, so
`admin_deny_access` set the column and changed nothing the user could observe: a
denied user's `GET /profile` returned 200 for up to thirty more days. The check
now pops `user_id`, repopulates `pending_access` in the shape `auth.py` writes at
login, and 302s to `/access-pending`. Two consequences: `/access-pending` and
`POST /logout` must stay free of `get_current_user` or the bounce becomes a loop
with no way out; and the check runs on the **session holder**, deliberately
before the impersonation block, so **an admin can still impersonate a denied
account** — that is a support path, not an oversight, and it is commented at the
check.

## Deleting a PI

**All deletion goes through `src/services/user_deletion.py::delete_user_account`**
(both `POST /profile/delete-account` and `POST /admin/users/{id}/delete`).
Never `db.delete(user)` directly — before 2026-08-25 that was the whole
process, and it left the deleted PI's agent RUNNING: `agents.user_id` is SET
NULL, the roster sync loads by status alone, and the agent reads its persona
from `profiles/public/{agent_id}.md`, not from the users table. See
`docs/audits/2026-08-25-pi-deletion/README.md`.

What the teardown does: suspends the linked agent (`status='suspended'` — the
one state a manager unmute cannot undo), purges `profile_revisions` (full
profile snapshots), the agent's `pi_dm_messages`, the
`jhu_tenure_start:{user_id}` app_settings key, the on-disk
`profiles/public/{agent_id}.md` and `profiles/memory/{agent_id}` artifacts,
and revokes the Slack bot token (post-commit, best-effort — a failed
revocation is logged loudly and leaves the token in the DB column for manual
revocation; the agent is suspended either way). The agent ROW is kept: it is
the record behind old messages and assessments, and its `agent_id` slug stays
reserved. Deliberately retained: `agent_messages`, `llm_call_logs`,
assessments, and everything already posted to Slack — both confirmation pages
say so.

Guards: an impersonating admin cannot trigger the self-service delete (403);
the last loginable admin cannot self-delete; the admin form has a
default-checked "also remove from the access allowlist" checkbox — without it
a deleted allowlisted ORCID can sign straight back in as `allowed`. Related:
the allowlist promotes only `pending` users at login; a `denied` user stays
denied (`src/routers/auth.py`). The admin delete route refuses impersonated
sessions the same way. Known accepted residual: a deletion issued while that
user's generate_profile job is mid-run can block on the jobs-row lock until
the pipeline finishes; the delete still commits.

The roster criterion lives in `src/agent/roster_query.py` and excludes
`pi_lab` rows with `user_id IS NULL` (hub/specialists are exempt). Both the
startup load and `_sync_roster_from_db` use it; `--all-agents` bypasses it at
startup only — the ~30s live sync still applies the criterion and evicts
non-matching rows on its first tick.
One consequence to know before touching **/admin/agents → Link**: UNLINKING an
active `pi_lab` agent (submitting the link form with an empty user) now evicts
it from the running roster within ~30s — the same invariant, applied live.

## BlackbirdBot (the scout_hub role)

BlackbirdBot screens PI ideas against `data/Blackbird_initial_priorities-criteria_v1.pdf`.
**The rubric criteria live in one document, `prompts/rubric/blackbird-rubric.toml`** —
weights, band thresholds, the 1–5 scale, gating criteria, per-dimension evidence lists,
red flags, the heuristic. Since regime 3.0.0 (2026-08-27,
docs/plans/2026-08-27-rubric-v3-consolidation.md) that is SIX single-scale dimensions —
the 13-dimension dual-scale (investment/incubation) machinery, the standalone
target-level checklist, and the stage-selected scoring are all gone, and the operator
directed that no legacy-verdict compatibility be kept: pre-v3 rows are not carried over,
and the read paths render every row against the live document. `src/services/blackbird_rubric.py` loads it once at import (fail-fast on an
invalid document) and renders it into the `{rubric}` placeholder of
`prompts/roles/scout_hub/agent-system.md` at prompt-composition time, so the prompt the
hub reads and the score the code computes cannot drift apart. The `<assessment_json>`
skeleton stays in `prompts/roles/scout_hub/phase4-thread-reply.md` (it is the
authoritative contract for the sidecar's shape);
`tests/unit/test_rubric_prompt_sync.py` is the drift alarm between the two, plus
`specialists.py`'s `maps_to_dimension`. The per-phase behaviour otherwise lives in
`prompts/roles/scout_hub/` and `src/agent/thread_guidance.py`.

**The review bot is a separate consumer of `prompts/`, not part of BlackbirdBot's
live simulation.** A `review_feedback_analysis` job (`src/services/review_bot.py`)
turns human reviewer feedback with `feedback_mode == "learn"` into a distilled
prompt/rubric-change *suggestion*. Enqueueing is deduped
(`enqueue_analysis_if_absent`) so repeated "learn" feedback on one assessment
still fires at most one pending job, not one per feedback row. The job runs on
the **worker**, which now bind-mounts `./prompts` read-only for exactly this
purpose and reads the prompt files as plain data through the dependency-free
`src/services/interview_transcript.py` loader — it deliberately never imports
`blackbird_rubric.py` (see that module's comment, and the import probe in
`tests/`). The model is `settings.llm_review_model` (`claude-opus-5`), a
setting distinct from the simulation's own model config. A suggestion is never
auto-applied to any prompt file — it is stored as a `PromptChangeSuggestion`
row and surfaced at `/manager/prompt-suggestions` for a human (admin or
manager; a reviewer cannot see this page) to read and act on manually.

**Nothing enqueues a review-bot job automatically as of 2026-09-14.**
`submit_feedback` and `edit_feedback` no longer call
`enqueue_analysis_if_absent`; `POST /reviews/suggestions/generate` — the
"Generate suggestions from current reviews" button on
`/manager/prompt-suggestions`, staff-only and refused under impersonation — is
the ONLY trigger — it lives on the `/reviews` router (whose own explicit POST
allowlist is now eight) and NOT on the manager router, whose write allowlist
stays at the eight routes the Account Types section names. `feedback_mode ==
"learn"` now means "eligible for the next
manual generate", not "queued": the page states the eligible count
(`count_pending_analysis_candidates`) and disables the button at zero, and
because the dedupe counts PENDING jobs only, a second press is a visible no-op
(`generated=0`) rather than a duplicate spend. One consequence to know: an edit
made while a job is in flight still leaves its row unconsumed
(`consumed_at_predicates` is untouched and is what guarantees that), but nothing
re-queues it — it waits for the next press.

**One job can now write MORE than one suggestion row.** The reply's contract
gained an optional `additional_proposals` array (at most two entries, each with
its own `target`, `suggestion` and `rationale`), so the same feedback can
propose a hub-prompt change and the matching PI-prompt change together.
`target` stays the JSON object's FIRST key, which is load-bearing:
`_LEADING_TARGET_RE` recovers the declared target from an unparseable reply and
did so for 3 of 12 live replies in the 2026-09-03 evaluation. Each proposal
becomes its own row, all in one commit, all sharing one `feedback_snapshot` and
one `raw_response`. The bot is also now told which prompt SET each file belongs
to (`--- FILE: <path> [<role>] ... ---`) and is given both `role.toml`
manifests; it is told plainly that the per-phase interview guidance is Python in
`src/agent/thread_guidance.py` and has no quotable text. The
bot's LLM calls are, by design, unlogged (no `llm_call_logs` row — that emit
gate needs a callback only the simulation engine installs) and unthrottled (no
rate limiter in front of it): the suggestion row records only `model`,
`transcript_available` and `input_truncated` — no token counts anywhere — so
the Anthropic console is the only cost record; do not go looking for these
calls in `llm_call_logs` or in any per-window rate-limit accounting.

**Review-job lifecycle guarantees (2026-09-02 hardening,
`docs/audits/2026-09-02-review-pipeline/`).** `enqueue_analysis_if_absent`
dedupes against PENDING jobs only — a `processing` job has already
snapshotted its rows and cannot cover feedback written while it waits on the
model. The handler stamps `consumed_at` with a content-conditional UPDATE, so
a row edited or deleted mid-call is left for the job the edit already
enqueued (one WARNING names the count). `_retire_superseded_verdict`
re-points queued job payloads along with the four review tables. The worker
requeues every `processing` row at boot and any older than 30 minutes every
60 s (`requeue_stale_processing_jobs`, `src/worker/main.py`); exhausted ones
go `dead`. The worker's `stop_grace_period: 330s` (working-tree compose edit,
like the others in the two-stack box) exists so a deploy no longer SIGKILLs a
review call at 10 s. A reply that is not valid JSON keeps its declared
`target` via a leading-key regex (`_LEADING_TARGET_RE`,
`src/services/review_bot.py`) rather than defaulting to `out_of_scope`, and
logs one WARNING naming the recovered target — measured at 3 of 12 live
`claude-opus-5` replies in the 2026-09-02 evaluation. The worker's boot sweep
assumes a SINGLE worker instance: `older_than_seconds=0` at boot requeues
every `processing` row regardless of age, with no way to tell a genuinely
abandoned row from one a concurrently running second worker is still
handling — so an ad-hoc second worker would have its live job requeued out
from under it and processed twice.

**Editing the rubric takes effect on restart, not on rebuild.** `prompts/` is
bind-mounted into `blackbird-app` and `agent`, the two services that read it as a
*rendered* document, so a document edit needs no image build there. It is also now
bind-mounted read-only into `worker` (2026-08-28, for the review bot above), but that
mount needs no restart to pick up an edit: the worker reads prompt files fresh, as
plain data, on each `review_feedback_analysis` job rather than importing the rubric
module once at process start. For `blackbird-app` and `agent`, the document is read
ONCE at import, so a running process keeps the rubric it started with. Stop the run,
start it again (see "Before restarting" above), and
check the startup banner: it logs `Screening rubric: version X (content hash Y)`. X must
match `[meta].version` in the file; Y is the first 12 hex characters of the file's sha256
(not the full digest). New assessments are stamped with both, so pre-/post-change rows
stay comparable. A version bump also requires the outgoing document's entry in
`prompts/rubric/revisions.toml` — see the assessment-archive box.

> **Deploy order for `0030_specialist_consults_rubric_version` — migrate BEFORE the new
> code serves.** `0030` is additive (a new `specialist_consults` table, plus nullable
> `rubric_version`/`rubric_content_hash` columns on `opportunity_assessments`), so *old
> code against the new schema* is safe. The reverse is not: the new code **maps
> `opportunity_assessments.rubric_version`/`.rubric_content_hash`**, so EVERY
> `select(OpportunityAssessment)` — the assessments pages, the detail pages — and the
> discussions pages' `specialist_consults` query all raise
> `UndefinedColumn`/`UndefinedTable` against a pre-`0030` database. Build, migrate from a
> one-off container, then start — same ordering as `0028`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — see the "agent image does NOT mount `src/`"
> warning above. One caveat beyond the usual migrate-before-serve reasoning: an interview
> already in flight across the deploy has no `specialist_consults` rows yet (they only
> start being written once the new code is running), so a verdict that concludes without
> a fresh consult can be stamped `panel_incomplete` with a full `missing_domains` list — a
> false accusation, but only in that one-time window.

> **`0031_normalize_missing_domains_null` needs NO deploy ordering.** It is
> data-only — it rewrites `opportunity_assessments.missing_domains` from the JSONB
> scalar `null` to a real SQL NULL, and changes no DDL. The code side is
> `JSONB(none_as_null=True)` on the mapped column, which is a Python-side
> property, so old code against normalized data and new code against
> un-normalized data both read `None` exactly as before. Apply it in any order
> relative to the restart. Details:
> `docs/audits/2026-08-20-assessment-duplication/README.md`.

> **Deploy order for `0036_panel_owed_thread_id_truncated_and_repairs` — migrate
> BEFORE the new code serves.** `0036` is five additive nullable columns, one
> foreign-key rule corrected, and two data repairs, so *old code against the new
> schema* is safe (nothing is backfilled, nothing is NOT NULL). The reverse
> takes the live site down in pieces. The new code **maps
> `opportunity_assessments.panel_owed` and `.thread_id`,
> `specialist_consults.truncated`, and `llm_call_logs.cache_read_input_tokens` /
> `.cache_creation_input_tokens`**, so against a pre-`0036` database:
>
> * `/admin/assessments` and `/manager/assessments` raise `UndefinedColumn` —
>   both the `select(OpportunityAssessment)` at `src/services/directory.py:288`
>   and the unvetted-panel banner COUNT, which names `panel_owed` through
>   `unvetted_panel_filter()`;
> * both assessment DETAIL pages raise — `select(OpportunityAssessment)` at
>   `src/services/assessment_detail.py:503` and `select(SpecialistConsult)` at
>   `:731`;
> * `/admin/activity/{run_id}/llm-calls` raises — `select(LlmCallLog)` at
>   `src/routers/admin.py:379`;
> * on the engine side the LLM-log writer (`simulation.py:6987`) and the consult
>   writer (`:4293`) name the new columns in their INSERTs, so every
>   `llm_call_logs` flush and every `specialist_consults` row fails — the flush
>   path will say LOST with a row count, which is the loud half; the consult
>   write is best-effort and is the silent half.
>
> Build, migrate from a one-off container, then start — same ordering as `0028`
> and `0030`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`). Production was at `0035` and this branch
> is `0036`, so this box applies to the next deploy, not to some hypothetical one.

> **Deploy order for `0037_recommended_next_experiment` — migrate BEFORE the new
> code serves.** `0037` is one additive nullable Text column
> (`opportunity_assessments.recommended_next_experiment`, sidecar item 10 of
> rubric v2.1.0 — the single experiment Blackbird should fund next), so *old
> code against the new schema* is safe. The reverse is not: the new code **maps
> the column**, so against a pre-`0037` database every
> `select(OpportunityAssessment)` — both assessment list pages, both detail
> pages — raises `UndefinedColumn`, and on the engine side `_persist_assessment`
> names it in the INSERT, so every verdict write fails too. Build, migrate from
> a one-off container, then start — same ordering as `0028`/`0030`/`0036`. NULL
> on every pre-`0037` row, deliberately never backfilled: old verdicts were
> never asked to name one, and `raw_verdict` keeps what they did emit. The same
> 2026-08-24 change set RENAMED the third gating key
> (`fto_achievable` → `translational_potential`, rubric v2.1.0): needs no DDL —
> `gating` is JSONB and old rows keep their `fto_achievable` key, unrewritten —
> but the agent image must be rebuilt or the running hub keeps emitting the old
> contract while the rubric banner claims 2.1.0.
>
> Four things to expect afterwards, none of them a regression:
>
> 1. **The unvetted-panel banner on `/admin/assessments` JUMPS** to include every
>    row written before `0036` — 63 of them when the migration was written. Those
>    rows have `panel_owed IS NULL`, which is deliberately never backfilled
>    ("guessing would manufacture exactly the verification this column exists to
>    stop asserting"), and NULL is one of the three states
>    `unvetted_panel_filter()` counts. Staff read that page daily; the number
>    going up on deploy day is the fix working, not a new problem.
> 2. **`private_channel_members_user_id_fkey` is dropped and recreated ON DELETE
>    CASCADE**, taking a brief ACCESS EXCLUSIVE lock on `private_channel_members`
>    and `users`. It is free today (0 production rows) and only gets dearer. It
>    fixes a real 500: under the old `SET NULL`, deleting a user who was a
>    private-channel member drove both owner columns of the membership row to
>    NULL, violated `CHECK ((agent_id IS NULL) <> (user_id IS NULL))`, and made
>    **both** `POST /profile/delete-account` and the admin delete raise.
>    `added_by_user_id` deliberately stays `SET NULL`.
> 3. **Repair order inside the migration is load-bearing** and is already coded
>    that way: repair (a) recovers 17 de-risking milestones the backfill script
>    lost to a wrong sidecar key, and repair (b) is `0031`'s JSON-`null` → SQL
>    NULL normalization applied to eleven more columns. `derisking_milestones` is
>    one of the eleven, so running (b) first makes (a) match zero rows *while
>    reporting success*. Do not reorder them, and do not run the statements by
>    hand out of order.
> 4. **The `truncated` column reads NULL as "not truncated"**, so the three
>    known-truncated consults on run 8b64a0e0 keep crediting the specialist floor
>    exactly as they do today. The alternative retroactively invalidates history
>    on no evidence.

> **Deploy order for `0038_specialist_consult_read_state_and_stamp` — migrate
> BEFORE the new code serves.** `0038` is four additive nullable columns on
> `specialist_consults` (`read_state`, `established`, `rubric_version`,
> `rubric_content_hash`), so *old code against the new schema* is safe. The
> reverse is not: the new code **maps all four**, so against a pre-`0038`
> database `select(SpecialistConsult)` at `src/services/assessment_detail.py`
> — read by both assessment detail pages, admin's and manager's — raises
> `UndefinedColumn`, and on the engine side `_record_specialist_consult`'s
> INSERT (`src/agent/simulation.py:4366`) names all four, so every
> `specialist_consults` write fails too. (The discussions panel cards at
> `src/services/thread_panel.py` select an explicit column list that names
> none of the four, so that page is unaffected either way — the migration's
> own docstring originally overclaimed this and has been corrected.) Build,
> migrate from a one-off container, then start — same ordering as
> `0028`/`0030`/`0036`/`0037`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`). Production is stamped `0037`, so this
> box applies to the next deploy, not to some hypothetical one.
>
> **`established` IS written, as of the 2026-08-28 persona-contract change.**
> This box previously said it was "knowingly unwritten"; that stopped being
> true when all eight personas gained an `established` key and `tools.py`
> began forwarding it on every consult. Read the three states carefully,
> because two of them are easy to conflate:
>
> * **NULL** — never asked. The row predates the contract change, or the
>   consult was never recorded. Never read it as "the specialist established
>   nothing".
> * **`[]`** — asked, and nothing came back. This is genuinely ambiguous and
>   deliberately not disambiguated: it covers both "the specialist named no
>   positives" and "the specialist ignored the key", because
>   `_str_tuple(data.get("established"))` yields the empty tuple for a missing
>   key and for an empty list alike. Do not report `[]` as a specialist finding.
> * **a non-empty list** — the only case that carries evidence.
>
> `read_state` is written on every new consult too (`read_state_for`,
> `src/agent/specialists.py`).
>
> ⚠️ **After editing anything under `prompts/` or the `_PI_LAB`/`_SCOUT_HUB`
> guidance, run `.venv-test/bin/python scripts/sync_prompt_set_docs.py`.**
> `docs/specs/2026-08-07-{pi,hub}-bot-prompts.md` embed every prompt file
> verbatim and `tests/unit/test_doc_prompt_sync.py` asserts it, so skipping the
> sync turns one prompt edit into a fistful of CI failures on the next full run
> rather than an error at the point of the edit. `--check` reports drift without
> writing.

> **Deploy order for `0040_assessment_prose_format` — migrate BEFORE the new
> code serves.** `0040` is one additive nullable String(20) column
> (`opportunity_assessments.prose_format`), so *old code against the new
> schema* is safe. The reverse is not: the new code **maps the column**, so
> against a pre-`0040` database every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`, and on
> the engine side `_persist_assessment`'s INSERT names it, so every verdict
> write fails too. Build, migrate from a one-off container, then start — same
> ordering as `0028`/`0030`/`0036`/`0037`/`0038`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — this is also the change that ships the
> markdown phase4 prompt instruction the stamp gates, so a rebuild that skips
> the agent image leaves the hub writing markdown prose the running database
> stamps `NULL` (plain), the opposite of the intended pairing. NULL on every
> pre-`0040` row, deliberately never backfilled: those verdicts were never
> written under the markdown contract, so plain rendering is permanently
> correct for them, not a placeholder.

> **Deploy order for `0041_assessment_summary_posted_at` — migrate BEFORE the
> new code serves.** `0041` is one additive nullable `TIMESTAMPTZ` column
> (`opportunity_assessments.summary_posted_at`), so *old code against the new
> schema* is safe. The reverse is not, but only on the read side: the new code
> **maps the column**, so against a pre-`0041` database every
> `select(OpportunityAssessment)` — both assessment list pages, both detail
> pages — raises `UndefinedColumn`. The write side is genuinely safe:
> `_persist_assessment`'s INSERT never assigns `summary_posted_at` — that
> column is written later, by `_mark_summary_posted`, once a headline actually
> posts — and SQLAlchemy omits an unset, no-server-default nullable attribute
> from the generated INSERT's column list, so a fresh verdict write SUCCEEDS
> against a pre-`0041` schema either way. Build, migrate from a one-off
> container, then start — same ordering as
> `0028`/`0030`/`0036`/`0037`/`0038`/`0040`:
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>
> The agent image bakes `src/` in too and must be rebuilt separately
> (`$DC --profile agent build agent`) — and here that rebuild is the whole
> point: the announce-on-close path lives in the engine, so an app-only deploy
> migrates the column and keeps losing headlines.
>
> NULL on every pre-`0041` row, deliberately never backfilled: for those rows
> the only record of whether a headline posted is the Slack channel itself, and
> a guess would be indistinguishable from a measurement. Use
> `scripts/backfill_assessment_headlines.py --stamp-only` to record one you
> have verified by eye, and the same script without `--stamp-only` to post one
> that is genuinely missing.
>
> ⚠️ **Repair the existing rows BEFORE you start any simulation run.**
> `src/agent/main.py` RESUMES the latest run by default — that is the restart
> command in "Running the Agent Simulation" above — and the new shutdown sweep
> announces every verdict of the resumed run whose `summary_posted_at` is NULL.
> NULL means "not announced", every pre-`0041` row is NULL whatever actually
> happened in Slack, so **resuming a pre-`0041` run re-announces headlines that
> are already public**, one unretractable duplicate post each. No code change
> can close this: the column is the only durable record, and before `0041`
> there was none.
>
> Concretely, for production run `61ccad6d` as measured 2026-08-29: six
> assessment rows, all `summary_posted_at IS NULL`, **five of whose headlines
> are already in `#assessments-summary`** and one (rothstein — conditional,
> 2.85, the run's highest score) genuinely missing. Resuming it as-is posts
> five duplicates and one correct headline.
>
> So, after `alembic upgrade head` and before starting ANY run, for every run
> that already has headlines in Slack: check the five against the channel by
> eye and stamp them (`--stamp-only`), post the missing one (no `--stamp-only`),
> then verify there is nothing left owed —
>
>     $DC exec -T postgres psql -U copi -d copi -t -A -c \
>       "SELECT COUNT(*) FROM opportunity_assessments
>          WHERE simulation_run_id = '<run>' AND summary_posted_at IS NULL;"
>
> — which must read `0` before that run is resumed. Run the script without
> `--apply` first: it previews every post and stamp it would make. A run with
> NO headlines in Slack needs none of this; the sweep announcing its rows is
> the fix working.

> **Deploy order for `0043_assessment_narrative_and_review_dimension_scores` —
> migrate BEFORE the new code serves.** `0043` is six additive nullable columns
> across two tables (`opportunity_assessments.headline` / `.key_points` /
> `.elevator_pitch`; `assessment_reviews.dimension_scores` / `.rubric_version` /
> `.rubric_content_hash`), so *old code against the new schema* is safe. The
> reverse fails in both directions at once. READ side: the new code **maps all
> six**, so against a pre-`0043` database every `select(OpportunityAssessment)`
> — both assessment list pages, both detail pages — and every
> `select(AssessmentReview)` — the detail pages' feedback list and
> `review_bot`'s own load — raises `UndefinedColumn`. WRITE side:
> `_persist_assessment`'s INSERT names the three narrative columns, so **every
> verdict write of a running simulation fails** — and that write is
> best-effort, so the failure is swallowed and one ERROR line lands in a log
> nobody is tailing while the Slack replies keep looking completely normal.
> That is the silent half, and it is the same shape as the 2026-08-06 near-miss
> this file already records. `submit_feedback` and `edit_feedback`
> (`src/services/assessment_reviews.py:196-208`, `:257-259`) unconditionally
> assign `dimension_scores`/`rubric_version`/`rubric_content_hash` on every
> human review submission and edit too, so those ALSO fail against a
> pre-`0043` database — but LOUDLY, not silently: neither call site is wrapped
> in a best-effort try/except, so the `UndefinedColumn` propagates straight out
> of the route handler as a 500 rather than being swallowed into a log line
> nobody reads.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads`
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> The agent rebuild is **not optional and not interchangeable with the prompt
> mount**. `prompts/` is bind-mounted, `src/` is baked. Prompt without image
> means the hub emits `headline`/`key_points`/`elevator_pitch` and
> `_persist_assessment` discards them — they survive only inside `raw_verdict`.
> Image without prompt means every new row writes NULL.
>
> All three narrative columns are NULL on every pre-`0043` row and are
> **deliberately never backfilled**: those verdicts were never asked for a
> headline, and a generated one would be indistinguishable from one the hub
> wrote. Every read path degrades — `headline` falls back to
> `company_or_project`, absent bullets and pitch render nothing, and the
> `#assessments-summary` headline omits the pitch segment entirely. Expect the
> new card list and the new detail brief to look, for the 12 rows currently on
> record, almost exactly like the pages they replaced; the narrative half
> arrives with the first interview a rebuilt agent concludes.
>
> Production is stamped `0042`, so this box applies to the next deploy — as
> does the combined `0044`/`0045`/`0046` box immediately below it, which ships
> in the same deploy.

> **Deploy order for `0044` + `0045` + `0046` — migrate BEFORE the new code
> serves.** These three land together (2026-09-10, the seven-feature branch) and
> are all additive nullable columns, so *old code against the new schema* is
> safe in every case: nothing is backfilled, nothing is NOT NULL, and every
> read path treats NULL as the pre-migration answer. **Note the numbering: the
> plan documents assigned `0045` to F2 and `0046` to F1; execution swapped
> them.** What each one adds:
>
> * **`0044_review_recorded_by`** — `assessment_reviews.recorded_by_user_id`
>   and `assessment_review_events.recorded_by_user_id` (UUID FK `users`, ON
>   DELETE SET NULL). The second signature on a review written while an admin
>   was impersonating; NULL means the named reviewer/actor acted in person.
> * **`0045_llm_call_logs_thread_phase`** — `llm_call_logs.thread_phase`
>   (String(20): `explore`/`decide`/`conclude`) and `.message_ordinal`
>   (Integer). Stamped by the engine from the same `phase4_guidance()` call
>   that built the prompt. NULL on every pre-`0045` row and on
>   `new_post`/memory turns, deliberately never backfilled.
> * **`0046_slack_provision_initiated_by`** —
>   `slack_app_provisions.initiated_by_user_id` (UUID FK `users`, ON DELETE SET
>   NULL). Records the staff account that clicked "Install Slack bot", so
>   `complete_provisioning` can refuse to land a bot token for an install a
>   DIFFERENT account started — the Slack OAuth callback is a third-party
>   redirect and can carry no CSRF token, and its gate is now staff-wide rather
>   than admin-only. NULL (a pre-`0046` row, or a bulk
>   `scripts/make_install_links.py` row) is completable by an **admin only** —
>   allowing anyone staff would be a *widening* of the pre-`0046` admin-only
>   callback rather than a restoration of it.
>
> The reverse direction — new code against the old schema — breaks all three
> ways, and the `0046` failure is the one that will reach you first:
>
> * pre-`0044`: the new code **maps both `recorded_by_user_id` columns**, so
>   every `select(AssessmentReview)` and `select(AssessmentReviewEvent)` raises
>   `UndefinedColumn` — both assessment detail pages' feedback lists and
>   `review_bot`'s own load — and `submit_feedback` / `edit_feedback` /
>   the status write (`src/services/assessment_reviews.py`) assign the column
>   unconditionally, so every human review submission, edit and
>   approve/disapprove 500s out of the route handler. Loud, not silent.
> * pre-`0045`: the new code **maps `llm_call_logs.thread_phase` and
>   `.message_ordinal`**, so `/admin/activity/{run_id}/llm-calls`
>   (`select(LlmCallLog)`, `src/routers/admin.py`) and the
>   `src/services/simulation_stats.py` aggregates raise `UndefinedColumn`, and
>   on the engine side the `_llm_log_record` INSERT names both columns — so
>   **every `llm_call_logs` flush of a running simulation fails**, which the
>   flush path reports as LOST with a row count.
> * pre-`0046`: `start_provisioning`'s INSERT names
>   `initiated_by_user_id` and `complete_provisioning`'s select reads it
>   (`src/services/admin_provisioning.py`), so **ALL Slack bot provisioning
>   breaks** — both the start of an install and the callback that lands the
>   token.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0046)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is REQUIRED, not optional**, for two independent reasons.
> First, `0045`'s producers live in the engine (`src/agent/simulation.py`, which
> stamps the reply's own row, and `src/agent/tools.py`, which stamps a
> specialist consult's) and `src/` is BAKED into the
> agent image — an app-only deploy migrates the two columns and then writes
> NULL into them forever. Second, this deploy bumps the **scout_hub prompt set
> to `1.3.0`**, whose `key_points` is a three-group object rather than a flat
> list; `prompts/` is bind-mounted and `src/` is baked, so the prompt and the
> image must ship TOGETHER. Prompt without image means the hub emits the
> three-group object and the old parser mangles or discards it; image without
> prompt means the new parser is fed the flat 1.2.x shape, which is benign —
> `normalize_key_points` still accepts a flat list and passes it through. So
> the hazardous half of this pairing is prompt-without-image, not the reverse.
> Same pairing hazard the `0043` box describes, for the same reason.
>
> Per the control-plane section, `$DC up -d agent` brings the supervisor back
> **IDLE**: starting a run afterwards is a separate, explicit operator action
> from `/admin/simulation`.

> **Deploy order for `0047_pi_grants_and_industry_evidence` — migrate BEFORE the
> new code serves.** `0047` adds three tables (`pi_grants`,
> `pi_industry_evidence`, `pi_industry_scores`) and two `job_type_enum` values
> (`enrich_grants`, `industry_evidence`). Additive, so *old code against the new
> schema* is safe. The reverse: `/manager/pis` and `/manager/pis/{id}` select the
> new tables (`UndefinedTable`), and the worker's two new handlers fail every
> job they're given. The engine is untouched by this change — the **agent**
> image needs no rebuild for it alone — but the **worker** image (the two new
> job handlers run there) and the **app** image (the new manager panels) both
> do.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0047)
>     $DC up -d blackbird-app worker
>     $DC exec -T blackbird-app python scripts/enqueue_enrichment.py          # preview
>     $DC exec -T blackbird-app python scripts/enqueue_enrichment.py --apply  # backfill existing PIs
>
> Two new manager POSTs — `/manager/pis/{user_id}/grants/{grant_id}/veto` and
> `/manager/pis/{user_id}/industry/{evidence_id}/veto` — bring the explicit
> write allowlist (see the Account Types section) to **eight**.
> `ResearcherProfile.grant_titles` is now RePORTER-derived and tenure-filtered
> once the `enrich_grants` job has run for a PI (the ORCID-fundings seed is
> never wiped by an empty RePORTER result — it is only supplemented); the
> industry-interest score is manager-only and has no import path into profiles
> or prompts (`tests/unit/test_enrichment_isolation.py` is the tripwire).
> RePORTER silently ignores unknown criteria keys and returns the whole
> database rather than erroring, so `src/services/nih_reporter.py` refuses any
> key outside `ALLOWED_CRITERIA` and aborts when `meta.total` exceeds a cap
> instead of paging through it blind. A PI with no recorded tenure start gets
> grants labelled `org_only` (JHU affiliation cannot be tenure-scoped) and an
> industry score of **Unscored** (`reason: no_tenure_start`); a field
> percentile additionally needs at least three other scored PIs in the same
> `primary_field` or it stays `reason: cohort_too_small`. Enum values cannot be
> dropped in Postgres; a downgrade drops the three tables and leaves the two
> `job_type_enum` values in place, exactly as `0039` does for
> `review_feedback_analysis`.

> **Deploy order for `0048_assessment_score_rationale` — migrate BEFORE the new
> code serves, and rebuild the AGENT image in the same deploy.** `0048` is one
> additive nullable Text column (`opportunity_assessments.score_rationale`,
> sidecar item 10 of scout_hub prompt set 1.4.0 — the staff-only plain-language
> account of why the dimension scores add up to the score they do), so *old
> code against the new schema* is safe. The reverse breaks both ways. READ: the
> new code **maps the column**, so every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`. WRITE:
> `_persist_assessment` names it in the INSERT, and that write is best-effort,
> so **every verdict of a running simulation is lost** to one ERROR line in a
> log nobody is tailing while the Slack replies keep looking normal. Same shape
> as the `0043` box above.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0048)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.4.0**, whose `key_points` is a
> FIVE-group object (`significance`, `innovation`, `clinical_actionability`,
> `key_questions`, `commercial_potential`) and which adds `score_rationale`.
> Image-without-prompt is benign: `normalize_key_points` now accepts any SUBSET
> of the five known group keys, so a 1.3.0 three-group sidecar still stores
> (2026-09-14 — it used to demand exact set equality, which meant one omitted
> group stored `key_points = NULL` and lost the whole field to `raw_verdict`).
> Prompt-without-image writes NULL into `score_rationale` forever and hands the
> five-group object to a parser that rejects it.
>
> NULL on every pre-`0048` row and deliberately never backfilled: those
> verdicts were never asked for a score rationale, and a generated one would be
> indistinguishable from one the hub wrote. Both assessment surfaces render
> nothing when it is NULL.

> **Deploy order for `0049_assessment_strengths_risks` — migrate BEFORE the
> new code serves, and rebuild the AGENT image in the same deploy.** `0049` is
> two additive nullable JSONB columns (`opportunity_assessments.strengths` /
> `.risks`, sidecar items 11/12 of scout_hub prompt set 1.5.0 — the hub's own
> two-to-four-bullet strengths and risks lists for a verdict), so *old code
> against the new schema* is safe. The reverse breaks both ways. READ: the new
> code **maps both columns**, so every `select(OpportunityAssessment)` — both
> assessment list pages, both detail pages — raises `UndefinedColumn`. WRITE:
> `_persist_assessment` names both in the INSERT, and that write is
> best-effort, so **every verdict of a running simulation is lost** to one
> ERROR line in a log nobody is tailing while the Slack replies keep looking
> normal. Same shape as the `0043`/`0048` boxes above.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC run --rm blackbird-app alembic upgrade head
>     $DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0049)
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild is required, and the hazardous half of the pairing is
> prompt-without-image.** `prompts/` is bind-mounted and `src/` is baked. This
> deploy bumps the scout_hub prompt set to **1.5.0**, whose sidecar gains the
> `strengths`/`risks` keys. Prompt-without-image (an edited prompt served by
> an old image) writes NULL into both columns forever: the old image's parser
> does not know the two keys, so `normalize_bullets` is never called on them
> and `_persist_assessment` never assigns them — the hub's bullets survive
> only inside `raw_verdict`. Image-without-prompt (a rebuilt image serving an
> unbumped prompt) is benign: an old sidecar simply never emits `strengths` or
> `risks`, `verdict.get(...)` reads `None`, and `normalize_bullets(None)` is
> `None` — the columns store NULL exactly as they did before this migration.
>
> NULL on every pre-`0049` row and deliberately never backfilled: those
> verdicts were never asked for strengths/risks bullets, and generated ones
> would be indistinguishable from bullets the hub actually wrote. Both
> assessment detail pages render an "In the hub's words" section only when the
> column is non-NULL. Like `score_rationale`, both fields are app-only:
> `#assessments-summary` is untouched by this migration and still renders
> only its existing six fields — label, recommendation, band/score,
> permalink and the clipped elevator pitch.

> **The 2026-09-21 assessment-queue change ships NO migration, and both
> images still have to be rebuilt.** Schema head stays at `0049`; there is no
> migrate-before-serve ordering to observe. What it changes:
>
> * **scout_hub prompt set 1.5.0 → 1.6.0.** Sidecar item 6 now makes two
>   things mandatory in a `headline` — the specific disease/condition/patient
>   population, and what the intervention physically is *and does* — worded
>   for any modality, because most of the corpus is diagnostics rather than
>   drugs. No new sidecar key, so the `<assessment_json>` contract and
>   `_persist_assessment` are untouched, and the 10 existing headlines are
>   deliberately not regenerated. `prompts/` is bind-mounted, so this half
>   reaches the hub without an image build.
> * **The approve/disapprove/clear buttons are gone** from the queue card and
>   the detail page's Human review section. `POST
>   /reviews/assessments/{id}/status`, `set_review_status` and
>   `assessment_review_events` all survive, deliberately caller-less, behind a
>   `ROUTE_ALLOWLIST` entry in `tests/unit/test_reachability.py` — a new
>   category on that list, since every other entry names a real external
>   caller. The read-only Status line, Status history and the card's
>   Approved/Disapproved chip still render whatever history the database
>   holds, so a restored backup shows a chip no UI can now change.
> * **The queue has reviewed/unreviewed sub-tabs**, `?review=`, defaulting to
>   **unreviewed**. "Reviewed" means at least one `assessment_reviews` row —
>   written feedback, NOT an assignment and NOT a status event, which is
>   narrower than the card's own "Reviewed by" column
>   (`review_columns_for` unions status-event actors). The filter narrows
>   `total_count`, the five recommendation cards and `dimension_stats`; it
>   deliberately does not narrow the dropped-verdict or unvetted-panel
>   warnings, `lab_options`, or the run menu's per-run counts.
> * **The card's weighted score and band label moved into a collapsed "Why
>   this score" disclosure**, which now renders unconditionally so a pre-`0048`
>   row still carries its score. This is an accepted accessibility regression:
>   `band-label` exists because band-as-colour-alone was invisible to a
>   colour-blind reader, and it is now one click down. Only the recommendation
>   chip stays on the card face, so a recommendation/band disagreement is no
>   longer visible at a glance.
> * **URLs in assessment prose render as a blue underlined "cited paper"
>   link** (`src/services/prose_citations.py`, registered as the Jinja globals
>   `md_citations`/`plain_citations` on BOTH routers — each owns its own
>   `Jinja2Templates`, so one registration would 500 the other surface). The
>   anchor carries `href`, `title`, `class` and — on the plain path only —
>   `rel="noreferrer"`. It carries no `target`: DOMPurify 3.1.6's default
>   `ALLOWED_ATTR` has no `target`, so one would be stripped on the markdown
>   path and survive on the plain one, and the two renderings would disagree
>   visibly. `rel` is the opposite case: DOMPurify keeps it and it has no
>   visible behaviour, so it is emitted where it can be. Markdown has no
>   syntax for `rel`, so a `prose_format='markdown'` row's links carry none
>   and fall back to the browser's referrer policy. `#assessments-summary` is
>   untouched — `render_assessment_headline` still clips the RAW pitch, and
>   moving that would move the 600-character sentence boundary.
>
>     DC="docker compose -f docker-compose.prod.yml"
>     $DC build blackbird-app worker
>     $DC --profile agent build agent
>     $DC up -d blackbird-app worker
>     $DC up -d agent                                 # supervisor returns IDLE
>
> **The agent rebuild in that list changed the agent image's contents but
> changed no agent behaviour — and the distinction is the whole point.** The
> `src/` edits are six files: `src/routers/{admin,manager,reviews}.py` and
> `src/services/{directory,assessment_detail,prose_citations}.py`. Five of
> those the engine never imports. The sixth does matter:
> **`src/services/assessment_detail.py` IS on the engine's import graph** —
> `src/agent/simulation.py` pulls `KEY_POINT_GROUPS`, `normalize_bullets` and
> `normalize_key_points` from it, which is the sidecar parser. What this
> change added to that module is one new function, `has_review_filter()`,
> called only from `src/services/directory.py`; nothing the engine reaches
> was touched. So the rebuild was REQUIRED for image/tree parity and was
> behaviourally inert — not, as an earlier draft of this box said, a rebuild
> the agent did not need.
>
> Do not generalise this into "the agent never needs rebuilding". The rule is
> the one the "agent image does NOT mount `src/`" box states: **`src/` is
> baked, `prompts/` is mounted.** Any change under `src/agent/` — or under
> anything it imports, `assessment_detail.py` included — still requires
> `$DC --profile agent build agent` before the new code runs.
>
> The converse, which this file had never said outright: an edit under
> **`prompts/roles/**`** reaches a RUNNING agent with no build and no
> restart, version bump included. `Agent._load_prompt` → `_load_file` does a
> `read_text()` **per use**, and `prompt_set_stamp` re-reads `role.toml` the
> same way. **`prompts/rubric/blackbird-rubric.toml` is the exception** and
> still needs a process restart — `src/services/blackbird_rubric.py` parses
> it ONCE at import (`_RUBRIC = parse_rubric(RUBRIC_PATH)` at module level),
> which is what the "Editing the rubric takes effect on restart, not on
> rebuild" box above already says. Both statements are true; they are about
> different subtrees of `prompts/`.
>
> No sidecar key changed, so image-without-prompt and prompt-without-image are
> both benign for this change.

> ### ⚠️ The assessment archive: never purge, never delete a run row.
>
> `opportunity_assessments` rows are the cross-version comparison corpus —
> each is stamped (`rubric_version`, `rubric_content_hash`) and the read
> paths render it against that revision via `prompts/rubric/revisions.toml`
> + `src/services/rubric_revisions.py`. Three standing rules:
>
> 1. **A rubric regime change is "stamp and keep", never a purge.** The one
>    purge on record (2026-08-27, rubric v3) deleted all 82 pre-3.2.0 rows;
>    they survive only in
>    `backups/opportunity_assessments_pre_purge_1787862739.dump`
>    (restore runbook: docs/plans/2026-08-28-run-isolation-and-assessment-
>    archive-plan.md, Task 9).
> 2. **On every `[meta].version` bump** of `blackbird-rubric.toml`, append
>    the OUTGOING document's entry (version, sha256[:12] of the old bytes,
>    scale, band lines, dimension table) to `prompts/rubric/revisions.toml`
>    in the same commit — otherwise the rows it stamped render as "unknown
>    revision".
> 3. **Never DELETE from `simulation_runs`.** Every run-produced table
>    (`agent_messages`, `opportunity_assessments`, `assessment_drops`,
>    `llm_call_logs`, `specialist_consults`, `thread_decisions`,
>    `pi_dm_messages`, `agent_channels`) is ON DELETE CASCADE from it — one
>    row's delete silently destroys that run's entire archive. No code path
>    does this; the exposure is manual SQL.
> 4. **Deleting an `opportunity_assessments` row now destroys human work, not
>    just engine output.** `assessment_reviews`, `assessment_review_events`
>    and `assessment_review_assignments` are all ON DELETE CASCADE from
>    `opportunity_assessments.id` (migration `0039`), so a run-row delete under
>    rule 3 — or any other delete of an assessment — takes every reviewer
>    comment, score, approve/disapprove event and assignment on it with it.
>    `prompt_change_suggestions` is the deliberate exception: its
>    `assessment_id` FK is SET NULL, because a suggestion is a distilled
>    artifact of the source assessment, not a record ABOUT it, and is worth
>    keeping even once its source row is gone.

**One interview yields exactly one assessment, and the row you end up with comes
from the LAST verdict-bearing reply.** **A sidecar is now trusted on its own**
(`_sidecar_refusal`, `src/agent/simulation.py:3765`): emitting one IS the hub
saying "this is my verdict", so `_capture_hub_assessment` stores it whether or
not the reply ends the interview. The only refusal left is a re-capture —
`duplicate_thread_verdict`, for a turn already stored, for anything after a
verdict whose reply CLOSED the interview, or for the same ordinal captured twice
— and every refusal is recorded in `assessment_drops`, carrying the model's
`raw_verdict` with it, rather than logged and forgotten. A non-terminal sidecar
is stored as PROVISIONAL and superseded by any later one: last write wins, and
`_retire_superseded_verdict` removes the earlier row (leaving a
`duplicate_thread_verdict` drop as its trace) so the one-row invariant still holds.

`premature_sidecar` is therefore **HISTORICAL ONLY as of 2026-08-22** — no new
rows carry it. It used to mean "a sidecar arrived on a turn that neither
concluded the interview nor closed the thread, so a later turn is still owed the
verdict", and that promise was unbacked: nothing scheduled the later turn,
nothing tracked the debt, and nothing kept the discarded JSON. Two rounds of
evidence killed it. Gating on the ordinal alone destroyed every `pass`: a `pass`
is delivered as a ⏸️ decline, which closes the thread 3-8 ms later in the same
code path, so no ordinal-12 turn ever arrives (run 076e80b6: 4 of 5 refusals were
the thread's terminal message; only 1 of 62 threads reached 12; all 23 `pass`
sidecars ever emitted carried ⏸️). Adding `or closes_thread` rescued the declines
and left the positives exposed, because `phase4-thread-reply.md` binds the two to
MUTUALLY EXCLUSIVE outcomes — Outcome 1 is verdict + sidecar and NO ⏸️, Outcome 2
is ⏸️ and "emit no sidecar" — so the only sidecar the code reliably accepted was
one the prompt forbids. Run 8b64a0e0 measured it: the CONCLUDE door was offered
once in 140 hub reply turns, 0 of 15 sidecars used it, all 13 stored verdicts came
through the ⏸️ door, and the two refused at ordinal 10 included the run's
highest-scoring idea (markham, 3.04, its only `route-to-incubation`) — refused six
minutes before the run's timer ended the interview that was supposedly still owed
a verdict. Gating on *neither* is not the answer either: that wrote three rows for
a single pearce interview (ordinals 8, 10 and 12), because the `<assessment_json>`
contract sits in the STATIC body of `phase4-thread-reply.md` and is therefore in
front of the model on every phase-4 turn — which is what supersession, not
refusal, now handles.

`closes_thread` (the ⏸️ decline, decided by `_reply_closes_thread`, hoisted once
in `_reply_to_thread` and passed down) still matters, just not for admission: with
"is this the CONCLUDE ordinal" it decides whether a verdict is TERMINAL
(`_verdict_is_terminal`), which marks the held record `final` so nothing later can
re-capture it, and which is the only thing that releases the public
`#assessments-summary` headline.

**`_assessed_threads` survives a restart now**, via
`opportunity_assessments.thread_id` (migration `0036`) and
`_rehydrate_assessed_threads`. The map is process-local, so before that a restart
left the engine blind to every verdict it had already written: the interview's own
later turn looked like a FIRST verdict and landed a second row, and a lab bot
⏸️-closing a thread that already held one produced a spurious
`closed_before_verdict` drop. Rows with a NULL `thread_id` — every row written
before `0036` — are skipped rather than guessed at, and the restored record uses
`ordinal=0`, a `final` DERIVED from `_closed_thread_ids`, and an `announced`
READ from `summary_posted_at is not None` (migration `0041`) rather than
defaulted. `ordinal` and `final` are still deliberate choices about which way to
fail if the answer is unknown; `announced` no longer needs one, because the
column now carries the real answer — it used to be hardcoded `False`, which
was the safer of two guesses (a hardcoded `True` would have suppressed the
`#assessments-summary` headline for a verdict stored provisionally before the
restart, and a headline cannot be retracted), but a guess either way traded one
breach for another: reading the column instead means a verdict whose headline
was already public does not get a second one. A pre-`0041` row reads NULL and
therefore `False`, which is exactly the old hardcoded behaviour. All three
fields are documented at the function.

When writing a
test that drives a concluding reply, seed the thread's history in the
`MessageLog`: `_reply_to_thread` overwrites `ThreadState.message_count` from
`get_thread_history`, so `message_count=11` over an empty log is an ordinal-1
EXPLORE turn, not the CONCLUDE turn it looks like.

As of the 2026-08-12 removal cycle (private instructions + reply-only hub), there is no
runtime "private profile" mechanism — `Agent._compose_system_prompt` injects the rendered
rubric but no `## Your Private Instructions` header, and nothing reads
`profiles/private/{agent_id}.md` per-agent. The stale hub copy has now been diffed against
the extracted rubric and archived as
`profiles/private/blackbird.archived-2026-08-20.md` (untracked, git-ignored, unread — no
longer a per-deploy chore) — the diff is recorded in
`docs/audits/2026-08-20-rubric-extraction/blackbird-private-diff.md`. Its one substantive
delta was a fourth **`baltimore_commitment`** gating criterion, deliberately absent from
the tracked rubric: the three gating keys are structural (the sidecar's JSON keys and the
`opportunity_assessments.gating` keys), and `blackbird_rubric.py`'s validator rejects a
fourth outright. (Since 2026-08-24 / rubric v2.1.0 the third key is
`translational_potential`, not `fto_achievable` — FTO was demoted from gate to diligence —
and since v3.0.0 / 2026-08-27 the second key is `credible_science`, not
`credible_tech_source`; pre-rename rows kept their old keys and are not carried over.)

- **Interview guidance is per-role Python**, not a prompt: `src/agent/thread_guidance.py`.
  The `pi_lab` strings there are pinned by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` — do not reword them,
  and never run `pytest --snapshot-update` to make a mismatch go away. (THREE reviewed
  regenerations have occurred, each operator-directed with the diff audited: 2026-08-28
  the PI-bot redline integration (181 hunks, +1144/-614, every changed line machine-traced
  to the four edited files); 2026-08-28 the pi-doc funnel-replacement combine (7 hunks);
  and 2026-08-27, funnel→instrument rewording across the pi_lab
  prompts and `_PI_LAB[EXPLORE]` for rubric v3.x, executed at the operator's direction
  with the `.ambr` diff audited hunk-by-hunk — every changed line belonged to that one
  rewrite. Any future pi_lab change takes the same reviewed-diff path.)
- **Inside an interview thread the hub is reply-only — it never makes a top-level post
  there.** An Opportunity Assessment is not a post type: it is an `<assessment_json>`
  sidecar carried inside the hub's CONCLUDING reply in the interview thread (bare JSON, *no*
  ``` fence). It is stripped from the Slack body before anything is posted and written to
  `opportunity_assessments`, visible at `/admin/assessments`. To the MODEL, `:mag:` names
  the sidecar and is never a post label it may write
  (`prompts/roles/scout_hub/agent-system.md`); it never appears on anything a PI or another
  lab sees. **What is confidential is the sidecar, not the verdict.** The hub's concluding
  reply is *required* to state its verdict inline in the visible `<slack_message>` —
  gating status, recommendation, red flags, confidence label — by
  `src/agent/thread_guidance.py`'s `_SCOUT_HUB[CONCLUDE]` (both strings), by
  `prompts/roles/scout_hub/agent-system.md` and by `phase4-thread-reply.md`: four places,
  all naming those same four things. (The funnel-stage classification was removed in
  rubric v3.1.0 — zero measured entropy at this system's pipeline position; the
  `opportunity_assessments.funnel_stage` column survives, unwritten by new verdicts.) An interview that ended saying nothing would be the
  defect, and when a sidecar is never stored the visible prose is the only surviving record
  of the verdict. What never reaches Slack is the sidecar and what only it carries —
  `raw_verdict`, the computed `weighted_score`, the `band`, and the per-dimension rubric
  scores — measured at **0 leaks across all 1,354 messages** of run 8b64a0e0. The protected
  class in the *visible* half is the PI's own **unpublished** disclosures:
  `phase4-thread-reply.md` binds the visible reply to describe the idea and its evidence
  "only at the level the PI has already made public", confining an unpublished result, an
  unfiled construct, an undisclosed compound or a volunteered limitation to the sidecar — an
  invariant no code and no test currently checks. (Until 2026-08-22 this bullet claimed the
  whole verdict was hidden: `5d67e92` grafted the `#assessments-summary` D12 field list onto
  an unrelated claim about the `:mag:` label, which sent an audit chasing a leak that was
  in fact prompt compliance. See
  `docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md` §1;
  `tests/unit/test_claude_md_disclosure_sync.py` is now the drift alarm.)
  As of the 2026-08-21 manager-PI-controls cycle
  (`SimulationEngine._post_assessment_summary`, `src/agent/simulation.py`), a HELD
  verdict — pass or fail alike — does additionally trigger one genuinely top-level post,
  written by the ENGINE rather than the model and prefixed with that same `:mag:`: a
  headline line (PI/lab name, `company_or_project`, `recommendation`,
  band/score, a permalink or `(link unavailable)`, and — since 2026-09-09 — the
  sidecar's `elevator_pitch` on a second line, clipped at a SENTENCE
  boundary — see below) to `#assessments-summary`
  (`ASSESSMENTS_SUMMARY_CHANNEL`, `src/agent/channels.py`) — deliberately with **no**
  rationale, red flags, gating, or `raw_verdict` (design D12, widened once). The pitch is
  a SIDECAR field and may carry the PI's unpublished disclosures; publishing it rests on
  the operator's assertion (2026-09-09) that PIs cannot join the workspace, which no code
  enforces — `SLACK_INVITE_URL` (`src/routers/agent_page.py:37`) still renders a join link
  on every PI's own `/agent` page. The pitch segment is omitted entirely when
  `elevator_pitch` is NULL, which is every row written before migration `0043`.
  **It is clipped at a SENTENCE boundary, not at an offset (2026-09-14).**
  `PITCH_DISPLAY_CHARS` is still 600 and deliberately was NOT raised — raising it
  would publish more sidecar prose to a channel whose content policy needed
  sign-off — but all 8 pitches on record measured 1173-1406 characters, so the
  old `value[:600]` cut every published headline mid-word (`...picked by univ`,
  `...(as oppose`, `...built on a handfu`). `_clip_at_sentence`
  (`src/services/assessment_headline.py`) now cuts after the last sentence
  terminator leaving at least half the budget, falls back to the last space with
  a `" ..."` marker, and returns a short pitch byte-identically unchanged.
  **`score_rationale` (sidecar item 10, migration `0048`) is deliberately NOT
  published here** — it reasons about the score, which is exactly the widening
  D12 bounds; it is app-only, on both assessment surfaces. Band/score
  are omitted entirely when the verdict carried no dimension scores, for the same reason
  `_persist_assessment` leaves those columns NULL: an empty `scores` map is "we don't know",
  and `weighted_score({})` is a 0.00 that bands as a decline nobody made. That channel is
  human-joinable/workspace-visible ("public" in the Slack sense — design D11) but is never
  added to `SEEDED_CHANNELS` or any per-agent subscription, so no PI-lab bot is ever joined
  to it or polls it — it still never reaches a PI/lab **agent**'s own view of the
  simulation, only human staff who join the channel directly. The post fires synchronously
  right after `_persist_assessment` returns HELD inside `_capture_hub_assessment` — but
  only for a verdict that is **TERMINAL and not already announced** for that interview
  (`announce = terminal and not already_announced`, `simulation.py:3236`). That
  condition is not the same as "held", and the difference arrived with provisional
  storage: since a non-terminal sidecar is now STORED rather than refused, one interview
  can hold several verdicts in turn, and a headline is a public Slack post that cannot be
  retracted when the row it described is superseded moments later. So a provisional verdict
  is stored, visible to staff, and logged as `Provisional verdict stored ... no
  #assessments-summary headline until the interview concludes` — announced only when a
  terminal reply arrives. `announced` carries forward across supersession for the same
  reason. A dropped
  or refused sidecar (an `AssessmentDrop` row, never an `opportunity_assessments` row) never
  posts (design D14), and a Slack failure in the post/permalink step is caught and logged,
  never raised into the calling turn (design D16) — see
  `docs/specs/2026-08-21-manager-pi-controls-design.md`. With `SLACK_ENABLED=false` the
  headline is skipped outright (the hub's transport is a `NullTransport`, which has no
  async post/permalink methods at all) — the assessment row is still written, so nothing is
  lost but the Slack copy.

> As of 2026-08-29 that is no longer the ONLY path. A verdict whose interview
> ends without a terminal reply — the `max_thread_messages` timeout, an
> abandoned thread, or the run's own shutdown — is announced by
> `_announce_owed_headline`, queued by `_close_thread` and drained by
> `_drain_and_flush` / `stop()`. Announcement is now a property of the
> INTERVIEW ENDING, not of one particular reply, and `at-most-once` is enforced
> by the `opportunity_assessments.summary_posted_at` column rather than by
> in-memory state alone. Each rescue logs one **WARNING** naming the trigger —
> a run with several means the hub is being locked out of its own CONCLUDE
> ordinal (RCA §2.2), which this path makes non-destructive but does not fix.
> See `docs/audits/2026-08-29-lost-assessment-headlines/README.md`.

- **`weighted_score` is computed**, never taken from the model:
  `src/services/blackbird_rubric.py`. `recommendation` (which may be
  `route-to-incubation`) comes straight from the model's verdict and the computed `band`
  comes straight from `weighted_score` — they are separate columns on
  `opportunity_assessments` and neither is derived from the other.
  A fourth write-time fact joined them in `0036`: **`panel_owed`**, the specialist
  floor's own answer to "was a panel owed here", computed once by `panel_is_owed` in
  `_persist_assessment` (`simulation.py:3631`) and **replayed** by the read path rather
  than recomputed. That is the point of the column. `assessment_detail.panel_state`
  used to ask `panel_is_owed(recommendation, band)` at RENDER time, which answers a
  different question — "would a panel be owed under TODAY's rules" — so every widening
  of the predicate silently re-labelled every older row. It widened twice in 2026-08
  alone, and 12 production rows written by the recommendation-only floor were re-read by
  the band-aware page as completed audits; at least five had a demonstrable gap.
  `panel_state` now returns **five** states — `gap`, `unverified`, `unrecorded`,
  `not_owed`, `verified` — and reaches `verified` (the green box) ONLY via
  `panel_owed is True`. `unrecorded` is `panel_owed IS NULL`: the row predates `0036`,
  or was backfilled, or was hand-built by a test, and no claim is available for it.
  `tests/unit/test_panel_state.py::test_the_read_path_never_re_derives_the_floor_s_decision`
  fails if anyone puts `panel_is_owed` back in front of the column test.
- **The specialist vocabulary is `blocking` / `gap` / `adequate`** (renamed from
  `blocking` / `caution` / `clear` on 2026-08-28; only `blocking` survived). The write set
  and the read set are deliberately DIFFERENT sizes, and that asymmetry is load-bearing:
  `VERDICT_SIGNALS` (the three live labels) is what a persona may offer and what the
  calibration ladder admits, while `_READABLE_SIGNALS = VERDICT_SIGNALS |
  HISTORICAL_VERDICT_SIGNALS` is what `parse_opinion` will READ. Two paths re-parse
  *stored* reply text — `/admin/activity/{run}/llm-calls` and the retro assessment-detail
  parse — so reading against the live three alone would re-render all ~1,192 pre-rename
  consults as a defaulted `gap` and log a WARNING per row per page view. This is not
  "legacy-verdict compatibility" in the sense the rubric section rules out: nothing writes
  a retired label, no persona offers one, and no verdict is carried over — only historical
  text stays readable. The admin templates keep colour/glyph branches for the retired pair
  for the same reason, and `adequate` takes ☑ where `clear` took ✓ so the two are
  distinguishable on sight.
- **Panel notes and consult truncation:** Panel notes clip the hub's question
  at `PANEL_NOTE_QUESTION_CHARS` (src/agent/specialists.py, recalibrated 850
  on 2026-08-26), and `clip_rate_warning` logs once per run when >10% of >=20
  posted notes clip — that WARNING means the calibration has decayed again;
  remeasure from `specialist_consults` question lengths. Truncated-consult
  cause (refusal vs max_tokens ceiling) is derivable per call from
  `llm_call_logs.call_stats[].stop_reason` — see
  docs/audits/2026-08-26-specialist-truncation-rca/README.md.
- **`gating` values are the tri-state strings** `"met"` / `"not_met"` / `"unconfirmed"`,
  never booleans — "the PI declined" and "we never asked" are different answers, and only
  the former can license discounting an idea.
- **`AssessmentDrop.reason` gained `unwritable_row`** on 2026-08-22, and it is the one
  reason that is not a GATE decision: the engine WANTED the row and the database refused
  it, even alone, during `_recover_rows_individually`'s per-row retry after its batch
  failed (`_flush_persisted`). The verdict was already concluded, parsed and assembled
  into a row before it was lost, so the drop is its only surviving trace — `raw_verdict`
  carries the verdict exactly as the row would have stored it and `detail` names the
  database's own exception plus the channel/thread. Not retried (it already failed twice,
  batch then alone), and recording it is itself best-effort, since a malformed row must
  not take its batch's surviving verdicts down with it. `premature_sidecar` and
  `specialist_floor` are the two HISTORICAL-ONLY reasons; the full list, with what each
  one costs, is on the model (`src/models/opportunity.py`).
- **`search_prior_art` is a TITLE-only search** on the USPTO Open Data Portal (PatentsView
  was decommissioned in its 2026-03-20 migration to api.uspto.gov). It backs off to the
  most specific terms when the full phrase misses — before that backoff existed, every
  production search ANDed in domain-generic words like "inhibitor" and returned zero hits,
  reported to PIs as clean novelty. An empty title search is never FTO.
  **The query the model asks for is not always the query that is sent**, and as of
  2026-08-22 every difference is disclosed to it. `_prepare` (`src/services/patents.py:169`)
  NFKD-normalises and transliterates each whitespace chunk before tokenising: Greek is
  spelled out (`Qβ` → `Qbeta`), Unicode dashes and combining marks are folded away, and a
  chunk with no ASCII equivalent at all is DROPPED. `AND`/`OR`/`NOT` are dropped too —
  they are query syntax, not title words, and the terms are ANDed for the caller anyway;
  the tool description the model sees now says so outright. Each of those lands in
  `PriorArtResult.dropped_or_rewritten` and is rendered into the tool result by
  `tools.py::_rewrite_note`, alongside `.broadened` (the backoff fired, so hits may be
  adjacent) and `.truncation_note` (`.hits` is one page, not the whole match set). The
  disclosure has to be TOTAL: the first version fired only when the fold CHANGED a chunk,
  so `π-π stacking` reached the model as "SCOPE: searched titles for stacking." with
  `broadened` False — a term silently deleted and the note saying nothing had happened,
  which is the same class of damage the transliteration exists to prevent.
