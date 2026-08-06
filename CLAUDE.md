# CLAUDE.md

## Testing

Run `python -m pytest tests/ -v` before committing. All tests must pass.
Tests run inside Docker: `docker compose exec app python -m pytest tests/ -v`
(may need `pip install pytest pytest-asyncio` first if the container was rebuilt).

## Compose file set (read this before any `docker compose` command)

**Production is `docker-compose.prod.yml` + `docker-compose.override.yml`. Always pass
both `-f` flags.** A bare `docker compose up` reads only `docker-compose.yml`, which is
the **dev** file: it has no `restart:` policy, runs `uvicorn --reload`, and publishes
port 8001. Recreating prod services from it leaves them with `restart: no`, so they do
not come back after a reboot. (This is exactly what happened on 2026-08-06: a host
freeze rebooted the box and app/worker/postgres/grantbot stayed dead while nginx
crash-looped on `host not found in upstream "app:8000"`.)

`docker-compose.override.yml` forces the `json-file` log driver. It is required because
`docker-compose.prod.yml` sets `logging.driver: awslogs` on every service and the EC2
instance role (`copi-ec2-ses-role`) lacks `logs:CreateLogStream` — without the override,
every container dies at start with `AccessDeniedException`.

To avoid repeating the flags, export this once per shell:

```bash
export COMPOSE_FILE=docker-compose.prod.yml:docker-compose.override.yml
```

Verify you used the right file set — all six services must report `unless-stopped`:

```bash
docker inspect copi-python-app-1 -f '{{.HostConfig.RestartPolicy.Name}}'
```

## Running the Agent Simulation

The simulation runs in a one-off container named `agent-run`:

```bash
C="-f docker-compose.prod.yml -f docker-compose.override.yml"

# Resume an existing run (no budget limit):
docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0

# Resume with a budget cap (e.g. 50 LLM calls per agent):
docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --budget 50

# Fresh run (wipes agent_messages/channels, keeps proposals):
docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --fresh --budget 0

# With a time limit (minutes):
docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --max-runtime 60 --budget 0
```

On resume the sim fetches Slack history for each bot in roster order before reaching
turn 1. Slack throttles this hard — expect ~10 minutes of
`[<first-agent>] Rate limited, retrying in 10s (attempt 1/3)` before the first
`=== Turn 1 ===`. Repeated `attempt 1/3` (never `2/3`) means each call 429s once then
succeeds on retry — that is forward progress, not a hang.

**Before restarting**, always save logs and rebuild containers:

```bash
C="-f docker-compose.prod.yml -f docker-compose.override.yml"

# 1. Save logs
docker logs agent-run > logs/run_$(date +%s).log 2>&1
ls -t logs/run_*.log | tail -n +11 | xargs rm -f

# 2. Stop the old container
docker rm -f agent-run

# 3. Rebuild app + worker (picks up code changes)
docker compose $C up -d --build app worker

# 4. Rebuild the agent image too — prod bakes code into the image, so skipping
#    this silently runs whatever source was current at the last build.
docker compose $C --profile agent build agent

# 5. Start the new run
docker compose $C --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0
```

Never pass `--remove-orphans`: it deletes the prod nginx/certbot containers.

**Note:** Under prod (`docker-compose.prod.yml`) the agent image **bakes** the source in
— only `profiles/`, `prompts/`, and `data/` are bind-mounted, so **code changes require
`build agent`, not just a container restart.** (The dev `docker-compose.yml` bind-mounts
the whole repo at `/app`, which is why this used to be a restart-only step.)

**After any code change that affects the running agent process, flag this to the user so
they can decide whether to restart.** Roster changes — activating/inactivating agents or
setting a new `slack_bot_token` in `AgentRegistry` — do NOT need a restart; they're
picked up live by `_sync_roster_from_db`.

## Adding New PIs

**The `AgentRegistry` table is the single source of truth for the agent roster.**
There is no longer a `PILOT_LABS` list and no per-agent `config.py` token fields to
edit. A running `agent-run` re-syncs the roster from the DB every ~30s
(`_sync_roster_from_db`), so flipping an agent to `status='active'` (with a token on
its row) makes it go live **without a restart**.

### 1. Create user records and generate profiles

Look up each PI's ORCID ID (search orcid.org or the ORCID public API). Add them to `orcids.txt` with a comment line, then seed:

```bash
docker compose exec app python -m src.cli seed-profiles --file new_orcids.txt
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
docker exec copi-python-app-1 python scripts/export_agent_roster.py   # writes data/agent_roster.json
python3 scripts/provision_slack_bots.py                               # host: creates apps, prints OAuth URLs
```

The host script writes tokens to `.env`; import them into the DB column with:

```bash
docker exec copi-python-app-1 python scripts/backfill_agent_tokens.py
```

(`.env` + `config.py get_slack_tokens()` remain a read fallback, but the DB column is
authoritative.)
