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

Running pytest **inside the container** (`docker compose exec blackbird-app
python -m pytest ...`) does not currently work: the image installs with
`pip install --no-cache-dir .` (`Dockerfile:14`), with no `[dev]` extra, so
pytest is not installed there (verified: `exec blackbird-app python -c "import
pytest"` → `ModuleNotFoundError`). Restoring that path would need the image (or
a test-targeted variant of it) to install `.[dev]` instead.

If that path is ever restored, `TEST_DATABASE_URL` becomes required again: the
web container has no Docker socket, so without it every test that needs a
database errors out (469 of them, measured 2026-08-04):

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

The simulation runs in a one-off container named `blackbird-agent-run`:

```bash
DC="docker compose -f docker-compose.prod.yml"

# Resume an existing run:
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main

# Fresh run (wipes agent_messages/channels, keeps proposals):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh

# With a time limit (minutes):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --max-runtime 60
```

**`--budget` is deprecated.** It is a *cumulative* cap for the whole run, it is
rebuilt from `llm_call_logs` on restart, and it therefore benches an agent
permanently once crossed — a restart does not clear it. It defaults to 0 (off)
and should stay there. Pacing and runaway protection are now handled by the
sliding-window rate limiter, whose allowance scales with each agent's live
conversational load (`llm_calls_per_load_per_window`, `llm_rate_window_seconds`).
A hub bot in a star topology will hit any uniform cumulative cap long before any
spoke does. See `docs/specs/2026-08-06-hub-budget-scheduler-design.md`.

**Before restarting**, always save logs and rebuild containers:

```bash
DC="docker compose -f docker-compose.prod.yml"

# 1. Save logs
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f

# 2. Stop the old container — GRACEFULLY. `docker rm -f` sends SIGKILL, which
#    skips the shutdown flush and permanently loses the in-flight turn's
#    messages (the DB, not Slack, is the durable store). `docker stop` sends
#    SIGTERM; -t 30 leaves room for an in-flight LLM call to finish.
docker stop -t 30 blackbird-agent-run
docker rm blackbird-agent-run

# 3. Rebuild the web tier AND the agent image (both bake src/ into the image)
$DC up -d --build blackbird-app worker
$DC --profile agent build agent

# 4. Apply migrations — NOTHING ELSE DOES. See the warning below.
$DC exec -T blackbird-app alembic upgrade head
$DC exec -T blackbird-app alembic current   # confirm it matches `alembic heads`

# 5. Start the new run
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

> ### ⚠️ Nothing migrates the database for you. Step 4 is not optional.
>
> The prod web command is a bare `uvicorn` (`docker-compose.prod.yml`), there is
> no `alembic upgrade` at startup, and no `create_all` anywhere in `src/main.py`
> or `src/database.py`. So a rebuild + restart runs the **new code against the
> old schema**, and the failure is silent rather than loud: writes that hit a
> missing table or column raise, get swallowed by a best-effort `except`, and
> leave one ERROR line in a log nobody is tailing.
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
> `$DC up -d --build blackbird-app worker` does *not* update the simulation — it
> rebuilds the web tier only, and `docker compose run agent` then starts the
> **previous** image. Measured 2026-08-06: a rebuild that skipped step 3's second
> line launched a run on hours-old code, silently, with no error — the startup
> banner (`Budget: N calls/agent`) was the only tell.
>
> After any `src/` change, always run `$DC --profile agent build agent` before
> starting a new run, and check the startup banner matches what you expect.

**Note:** The agent-run container loads Python modules only at startup, so **code**
changes require rebuilding the image (above) and restarting the container. **After any code change that affects the running agent process, flag this to the user so they can decide whether to restart.** (Roster changes — activating/inactivating agents or setting a new `slack_bot_token` in `AgentRegistry` — do NOT need a restart; they're picked up live by `_sync_roster_from_db`.)

**`.env` changes need a container *recreate*, not a restart.** `env_file` is
resolved when the container is created, so `docker restart` re-runs the old
environment. Step 2 + step 4 above (rm, then `run`) is what actually picks up an
edited `.env`.

## Adding New PIs

**The `AgentRegistry` table is the single source of truth for the agent roster.**
There is no longer a `PILOT_LABS` list and no per-agent `config.py` token fields to
edit. A running `agent-run` re-syncs the roster from the DB every ~30s
(`_sync_roster_from_db`), so flipping an agent to `status='active'` (with a token on
its row) makes it go live **without a restart**.

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

## BlackbirdBot (the scout_hub role)

BlackbirdBot screens PI ideas against `data/Blackbird_initial_priorities-criteria_v1.pdf`.
The rubric lives in **`profiles/private/blackbird.md`** (loaded per-agent from
`profiles/private/{agent_id}.md` and injected under the `## Your Private Instructions`
header that `Agent._compose_system_prompt` builds into every phase's system prompt); the
per-phase behaviour lives in `prompts/roles/scout_hub/` and `src/agent/thread_guidance.py`.

- **Interview guidance is per-role Python**, not a prompt: `src/agent/thread_guidance.py`.
  The `pi_lab` strings there are byte-identical to the pre-refactor literals and are pinned
  by `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` — do not reword them,
  and never run `pytest --snapshot-update` to make a mismatch go away.
- **Assessments are durable.** A `:mag:` Opportunity Assessment must carry an
  `<assessment_json>` sidecar (bare JSON, *no* ``` fence — a fenced block would be parsed
  as the phase-5 action and silently no-op the post). It is stripped from the Slack body
  and written to `opportunity_assessments`, visible at `/admin/assessments`.
- **`weighted_score` is computed**, never taken from the model:
  `src/services/blackbird_rubric.py`. `recommendation` (which may be
  `route-to-incubation`) comes straight from the model's verdict and the computed `band`
  comes straight from `weighted_score` — they are separate columns on
  `opportunity_assessments` and neither is derived from the other.
- **`gating` values are the tri-state strings** `"met"` / `"not_met"` / `"unconfirmed"`,
  never booleans — "the PI declined" and "we never asked" are different answers, and only
  the former can license discounting an idea.
- **`search_prior_art` is a TITLE-only search** on the USPTO Open Data Portal (PatentsView
  was decommissioned in its 2026-03-20 migration to api.uspto.gov). It backs off to the
  most specific terms when the full phrase misses — before that backoff existed, every
  production search ANDed in domain-generic words like "inhibitor" and returned zero hits,
  reported to PIs as clean novelty. An empty title search is never FTO.
