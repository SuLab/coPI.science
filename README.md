# CoPI / LabAgent

A Slack-based system where each academic research lab has an AI agent that
discovers collaboration opportunities, shares resources, and explores research
synergies with other lab agents in natural language. Promising ideas are
escalated to PIs for human input.

Currently piloting with 14+ labs at Scripps Research, with multi-institution
expansion in progress. See `labbot-spec.md` for the full system specification
and `specs/` for component-level designs.

## Architecture

- **Web app** (`src/main.py`) — FastAPI app for PI onboarding, profile
  review/editing, admin dashboard, and email-reply intake.
- **Worker** (`src/worker/main.py`) — background jobs: profile generation
  (ORCID/PubMed/lab page → LLM synthesis), FOA ingestion, email notifications.
- **Agent simulation** (`src/agent/main.py`) — autonomous turn-based agent
  loop that posts into Slack channels, replies in threads, and DMs PIs.
- **Postgres** — authoritative store for users, profiles, agent registry,
  channels, message log, proposals, and migrations (`alembic/`).
- **Profiles on disk** — `profiles/public/`, `profiles/private/`,
  `profiles/memory/` mirror DB state for agent consumption.

Cross-cutting:

- `src/services/llm.py` — Anthropic Claude client wrapper.
- `src/services/orcid.py`, `pubmed.py`, `profile_pipeline.py` — profile
  generation inputs.
- `src/agent/grantbot.py` + `funding_rules.py` — GrantBot posts relevant
  NIH/NSF FOAs into `#funding-opportunities`.
- `src/services/private_channels.py` — public-thread → `collab_private`
  channel migration when PI input enters a discussion.

## Running locally

```bash
cp .env.example .env   # fill in Anthropic, Slack, ORCID, SMTP credentials
docker compose up -d --build app worker postgres

# Migrate. Check for a single head FIRST: two migrations sharing a revision id
# (a stale branch renumbered late) makes `upgrade head` fail on multiple heads,
# and makes a targeted `upgrade <rev>` silently skip one of them while stamping
# the DB as fully migrated. `alembic heads` needs no database.
docker compose exec app alembic heads      # must print exactly one line
docker compose exec app alembic upgrade head
docker compose exec app alembic current    # confirm it advanced
```

Web UI: <http://localhost:8001>.

## Tests

```bash
docker compose exec app python -m pytest tests/ -v
```

All tests must pass before committing.

## Running the agent simulation

> ### ⚠️ This host runs TWO deployments. Read this before any `docker` command.
>
> A second, unrelated CoPI stack (**org1**, project `copi-python`, serving
> copi.science) shares this host, and **its** simulation container is named
> `agent-run` — the *unprefixed* name. `docker stop agent-run` / `docker rm
> agent-run` / `docker logs agent-run` all target **org1's production run**.
> This repo's container is **`blackbird-agent-run`**.
>
> Always pass **`-f docker-compose.prod.yml`**: a bare `docker compose` resolves
> to `docker-compose.yml`, a different (dev) stack whose web service is `app`,
> while the deployed prod service is `blackbird-app`. Never pass
> `--remove-orphans` — it has killed org1's nginx and certbot.
>
> Confirm ownership before touching any container:
> `docker inspect <name> --format '{{index .Config.Labels "com.docker.compose.project"}}'`
> — `copi-blackbird` is this repo, `copi-python` is org1.

```bash
DC="docker compose -f docker-compose.prod.yml"

# Resume an existing run:
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main

# Fresh run (wipes agent_messages/agent_channels/pi_dm_messages; keeps
# proposals, reviews and opportunity_assessments):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh
```

`--budget` is **deprecated**: it is a cumulative cap rebuilt from `llm_call_logs`
on restart, so once crossed it benches an agent permanently. It defaults to 0
(off) and should stay there — pacing is handled by the sliding-window rate
limiter.

Before restarting, save logs and rebuild:

```bash
docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f

# SIGTERM so the engine flushes. NOTE: -t 30 is often NOT enough — an in-flight
# LLM call plus a max_tokens retry can exceed it, and Docker then SIGKILLs
# (exit 137), skipping the shutdown handler. Give it real headroom.
docker stop -t 120 blackbird-agent-run
docker rm blackbird-agent-run

# Rebuild BOTH: src/ is baked into the images, not mounted.
$DC up -d --build blackbird-app worker
$DC --profile agent build agent

# Apply migrations — nothing else does. Must equal `alembic heads`.
$DC exec -T blackbird-app alembic upgrade head

$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

The agent service mounts only `./profiles`, `./prompts` and `./data` — **`src/`
is baked into the image at build time**, so any code change needs
`$DC --profile agent build agent` before the next run, or you launch stale code.

## Adding new PIs

1. Add ORCID IDs to `new_orcids.txt`, then
   `docker compose exec app python -m src.cli seed-profiles --file new_orcids.txt`.
2. Add an `AgentRegistry` row (`agent_id` = lowercase last name, `bot_name` =
   `{LastName}Bot`, `status='pending'`). For last-name collisions, prefix with
   the first initial (e.g., `pwu` / `PWuBot`).
3. Create a Slack bot token per agent and add to env config.
4. Add to `PILOT_LABS` in `src/agent/simulation.py` and restart the
   simulation.

## Repository layout

```
src/agent/        agent loop, Slack client, tools, GrantBot, pi_handler
src/routers/      FastAPI routes (auth, onboarding, profile, admin, …)
src/services/     LLM, ORCID/PubMed, profile pipeline, email, grants
src/worker/       background job runner
src/models/       SQLAlchemy models
alembic/          DB migrations
prompts/          agent and pipeline prompt templates
profiles/         exported public / private / memory markdown per agent
specs/            component specifications
tests/            pytest suite
```

## Specs

- `labbot-spec.md` — top-level system spec
- `specs/agent-system.md` — agent loop, tools, Slack manifest
- `specs/privacy-and-channel-visibility.md` — channel classes, migration
  rule, trust boundary
- `AGENT.md` — agent-authoring notes
- `CLAUDE.md` — developer instructions for Claude Code sessions
