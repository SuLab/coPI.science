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

# 2. Stop the old container
docker rm -f agent-run

# 3. Rebuild app + worker (picks up code changes)
docker compose up -d --build app worker

# 4. Start the new run
docker compose --profile agent run -d --name agent-run agent python -m src.agent.main --budget 0
```

**Note:** The agent-run container uses mounted source code but the Python process only loads modules at startup. Code changes require a container restart to take effect. **After any code change that affects the running agent process, flag this to the user so they can decide whether to restart.**

## Adding New PIs

Follow these steps in order. Steps 1-2 can be done immediately; steps 3-4 when ready to activate the agent.

### 1. Create user records and generate profiles

Look up each PI's ORCID ID (search orcid.org or the ORCID public API). Add them to `orcids.txt` with a comment line, then seed:

```bash
docker compose exec app python -m src.cli seed-profiles --file new_orcids.txt
```

This creates `User` rows and enqueues profile generation jobs (processed by the worker).

### 2. Create agent registry entries

Each agent needs an `AgentRegistry` row with a unique `agent_id` (lowercase last name) and `bot_name` (`{LastName}Bot`).

**Last-name collisions:** If a last name is already taken (e.g., Chunlei Wu = `wu`), prefix with the first initial (e.g., Peng Wu = `pwu` / `PWuBot`). The web UI (`src/routers/agent_page.py`) applies this same logic automatically for self-service signups.

New entries should have `status='pending'` until Slack tokens are configured.

### 3. Create Slack bot tokens

Create a Slack bot token for each agent and add to the settings/env config. Each agent needs its own bot token keyed by `agent_id`.

### 4. Add to PILOT_LABS and restart simulation

Add entries to `PILOT_LABS` in `src/agent/simulation.py`:

```python
{"id": "lastname", "name": "LastNameBot", "pi": "First Last"},
```

Then restart the simulation (see "Running the Agent Simulation" above).
