# CLAUDE.md

## Testing

Run `python -m pytest tests/ -v` before committing. All tests must pass.
Tests run inside Docker: `docker compose exec app python -m pytest tests/ -v`
(may need `pip install pytest pytest-asyncio` first if the container was rebuilt).

## Running the Agent Simulation

The simulation runs in a one-off container named `agent-run`:

```bash
# Resume an existing run (no budget limit):
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0

# Resume with a budget cap (e.g. 50 LLM calls per agent):
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 50

# Fresh run (wipes agent_messages/channels, keeps proposals):
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --fresh --budget 0

# With a time limit (minutes):
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --max-runtime 60 --budget 0
```

**Before restarting**, always save logs and rebuild containers:

```bash
# 1. Save logs
docker logs agent-run > logs/run_$(date +%s).log 2>&1
ls -t logs/run_*.log | tail -n +11 | xargs rm -f

# 2. Stop the old container — GRACEFULLY. `docker rm -f` sends SIGKILL, which
#    skips the shutdown flush and permanently loses the in-flight turn's
#    messages (the DB, not Slack, is the durable store). `docker stop` sends
#    SIGTERM; -t 30 leaves room for an in-flight LLM call to finish.
docker stop -t 30 agent-run
docker rm agent-run

# 3. Rebuild app + worker (picks up code changes)
docker compose up -d --build app worker

# 4. Start the new run
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0
```

**Note:** The agent-run container uses mounted source code but the Python process only loads modules at startup. **Code** changes require a container restart to take effect. **After any code change that affects the running agent process, flag this to the user so they can decide whether to restart.** (Roster changes — activating/inactivating agents or setting a new `slack_bot_token` in `AgentRegistry` — do NOT need a restart; they're picked up live by `_sync_roster_from_db`.)

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
