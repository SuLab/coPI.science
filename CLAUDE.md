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
> attaches it to the shared `copi-edge` network, and swaps every `awslogs`
> driver for `json-file`. The rename is not cosmetic — Compose adds the service
> name as a network alias on every attached network, so an `app` on `copi-edge`
> would collide with org1's `app` and org1's nginx upstream would resolve to
> **this** container, breaking copi.science. So: never `git checkout`,
> `git stash` or `git restore` that file, and on a fresh clone expect `service
> "blackbird-app" is not defined` until the edit is reapplied. The `prompts/`
> and `profiles/` bind mounts below are identical in both versions; only the
> name, network, container_name and logging differ.

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

## Account Types (PI / manager / admin)

**`users.user_role` is the single source of truth**, with values `pi`, `manager`,
`admin`. `User.is_admin` is no longer a mapped column — it is a read-only
`hybrid_property` over `user_role`, so it still works in both SQL
(`select(User.is_admin)`) and Python, but **cannot be assigned**. Set the role
instead. The physical `users.is_admin` column stays in the database, unmapped and
defaulted. Dropping it is deferred to a separate later migration (`0039`+ — `0031`
through `0038` are all taken now: `0038` went to
`specialist_consults`'s `read_state`/`established`/rubric stamp instead, see the
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
  original all-GET guarantee (design D1) adds exactly four write routes — `POST
  /manager/pis` (create a PI via ORCID), `/manager/pis/{id}/profile` (edit a PI's
  profile fields), and `/manager/pis/{id}/mute` / `/unmute` (toggle a PI's agent) —
  and nothing else; `tests/integration/test_manager_views.py`'s
  `test_manager_router_mutations_are_an_explicit_allowlist` fails loudly on a fifth.
  **Still cannot impersonate**, set roles, or provision Slack bots (all three stay
  admin-only), and there is deliberately no LLM-call drill-down and no export.
  Managers *do* see private (`collab_private`) discussion threads — a policy
  decision, recorded in the design doc.
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

**Editing the rubric takes effect on restart, not on rebuild.** `prompts/` is
bind-mounted into exactly the two services that read it, `blackbird-app` and `agent`, so
a document edit needs no image build (`worker` mounts only `./profiles` — it never
imports the rubric). But the document is read ONCE at import, so a running process keeps
the rubric it started with. Stop the run, start it again (see "Before restarting" above), and
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
> **`established` is knowingly unwritten.** The column exists from this
> migration on, but nothing populates it yet — that is Phase B of
> `docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md` (§3.2, §8
> step 5/6), not this deploy. A NULL there means "never asked", not "the
> specialist established nothing" — do not read it as the latter on any row,
> including rows written well after this migration. `read_state`, by contrast,
> IS written on every new consult from this deploy forward
> (`read_state_for`, `src/agent/specialists.py`).

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
`ordinal=0`, `announced=False` and a `final` DERIVED from `_closed_thread_ids`;
each of those three is a deliberate choice about which way to fail, documented at
the function.

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
  and never run `pytest --snapshot-update` to make a mismatch go away. (One reviewed
  regeneration has occurred: 2026-08-27, funnel→instrument rewording across the pi_lab
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
  headline-only line (PI/lab name, `company_or_project`, `recommendation`, band/score, and a
  permalink or `(link unavailable)`) to `#assessments-summary`
  (`ASSESSMENTS_SUMMARY_CHANNEL`, `src/agent/channels.py`) — deliberately with **no**
  rationale, red flags, gating, or `raw_verdict` (design D12). Band/score are omitted
  entirely when the verdict carried no dimension scores, for the same reason
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
