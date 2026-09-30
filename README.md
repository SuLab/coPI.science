# CoPI / Blackbird

BlackbirdBot (the `scout_hub` agent) interviews one PI's lab agent at a time, in
Slack threads, to assess ideas for Blackbird Laboratories' incubation-grant or equity
funding. Each verdict is scored against `prompts/rubric/blackbird-rubric.toml`; the
hub's role prompt is `prompts/roles/scout_hub/agent-system.md`. Lab agents
(`pi_lab`) pitch their lab's work from a profile built from ORCID and PubMed.

`labbot-spec.md` describes the earlier lab-to-lab collaboration product and is kept
for history only; none of that product runs on this branch.

## Architecture

- **Web app** (`src/main.py`) — FastAPI: PI onboarding and profile editing, the agent
  page, staff pages (`/admin`, `/manager`, `/reviews`), assessment detail and chat,
  and the read-only public collaboration graph.
- **Worker** (`src/worker/main.py`) — background jobs: profile generation (ORCID +
  corpus resolution + LLM synthesis), grant and industry enrichment, review-bot
  analysis.
- **Agent simulation** (`src/agent/supervisor.py` → `src/agent/main.py` →
  `src/agent/simulation.py`) — the turn-based engine: lab pitches, hub interviews,
  specialist consults, verdict capture and `#assessments-summary` headlines. Started
  and stopped from `/admin/simulation`.
- **Postgres** — the authoritative store (users, profiles, agent registry, message
  log, assessments, reviews); migrations in `alembic/`.
- **Profiles on disk** — `profiles/public/` and `profiles/memory/` hold the exported
  profile each lab agent reads and its working memory.

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

# Fresh run: a new simulation_run_id isolates it; nothing is deleted (rows accumulate across runs).
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
# (exit 137), skipping the shutdown handler. Give it real headroom: since the
# thread_reply ceiling went to 16000 tokens, one uninterruptible final call can
# run ~4-5 minutes. A larger -t costs nothing — `docker stop` returns as soon as
# the container exits. See the restart procedure in docs/operations/host-and-simulation.md.
docker stop -t 420 blackbird-agent-run
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

See `docs/operations/pis-and-access.md` (manager Add-PI, activation, Slack tokens).

## Repository layout

```
src/agent/        simulation engine, roles, tools, specialists, Slack transport
src/routers/      FastAPI routes (auth, onboarding, profile, agent, admin, manager, reviews)
src/services/     LLM, ORCID/PubMed/corpus, profile pipeline, rubric, assessments
src/worker/       background job runner
src/models/       SQLAlchemy models
alembic/          DB migrations
prompts/          role prompts, specialist personas, rubric
profiles/         exported public profiles and working memory per agent
docs/             operations runbook, plans, specs, audits
tests/            pytest suite
```

## Specs

- `docs/specs/` — current designs (for example `docs/specs/2026-08-07-hub-lab-flow.md`)
- `docs/operations/` — the operator runbook indexed from `CLAUDE.md`
- `labbot-spec.md` — historical: the retired collaboration product
- `CLAUDE.md` — developer instructions for Claude Code sessions
