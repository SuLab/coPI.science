# CLAUDE.md

## Project Overview

Python implementation of the CoPI researcher collaboration platform combined with the LabAgent multi-agent Slack system. Includes ORCID OAuth, profile generation pipeline, profile editing UI, admin dashboard, Slack-based AI agent simulation, and LabBot Podcast (daily personalized research briefings).

**Target domain:** copi.science
**Pilot:** 14 labs at Scripps Research
**GitHub:** https://github.com/SuLab/coPI-python-opus
**Active branch:** `coPI-podcast`

## Tech Stack

| Component | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | FastAPI + Jinja2 templates |
| ORM | SQLAlchemy 2.0 async |
| Migrations | Alembic |
| Auth | Authlib (ORCID OAuth 2.0) |
| Database | PostgreSQL |
| Job queue | PostgreSQL-backed (jobs table) |
| LLM | Anthropic Claude (Opus for profiles/summaries, Sonnet for agents/selection) |
| Slack | slack-bolt (Socket Mode) |
| TTS | Mistral AI (voxtral-mini-tts-latest) or local vLLM-Omni server |
| Audio post-processing | ffmpeg loudnorm (EBU R128, -16 LUFS) |
| Styling | Tailwind CSS (CDN) |
| Deployment | Docker Compose (dev) / docker-compose.prod.yml (prod, AWS CloudWatch logging) |

## Project Structure

```
src/
├── main.py                 # FastAPI app factory
├── config.py               # Settings from env vars (pydantic-settings)
├── database.py             # SQLAlchemy async engine and session
├── models/                 # SQLAlchemy ORM models
├── routers/                # FastAPI routers (auth, profile, onboarding, admin, podcast)
├── services/               # Business logic (orcid, pubmed, llm, pipeline)
├── worker/                 # Job queue worker process
├── agent/                  # Slack simulation engine
└── podcast/                # LabBot Podcast pipeline
    ├── main.py             # Scheduler entry point (runs at 9am UTC daily)
    ├── pipeline.py         # Per-agent pipeline orchestration
    ├── pubmed_search.py    # PubMed query builder + candidate search
    ├── preprint_search.py  # bioRxiv, medRxiv, arXiv candidate search
    ├── rss.py              # RSS 2.0 feed builder (iTunes extensions)
    ├── state.py            # Delivered PMID tracking (data/podcast_state.json)
    ├── mistral_tts.py      # Mistral AI TTS backend
    ├── local_tts.py        # Local vLLM-Omni TTS backend
    └── tts_utils.py        # strip_markdown, normalize_audio (ffmpeg), get_audio_duration_seconds
profiles/
├── public/                 # Lab public profiles (markdown, read by podcast + agent)
└── private/                # Lab private profiles + working memory (markdown)
prompts/                    # LLM prompt files (podcast-select.md, podcast-summarize.md, etc.)
specs/                      # Feature specs (labbot-podcast.md, agent-system.md, etc.)
scripts/
└── test_podcast_su.py      # One-shot test: run podcast pipeline for 'su' agent only
```

## Testing

Run `python -m pytest tests/ -v` before committing. All tests must pass.
Tests run inside Docker: `docker compose exec app python -m pytest tests/ -v`

## Environment Setup

```bash
cp .env.example .env
# Required: ORCID_CLIENT_ID, ORCID_CLIENT_SECRET, ANTHROPIC_API_KEY, NCBI_API_KEY
# Required for podcast: MISTRAL_API_KEY (or PODCAST_TTS_BACKEND=local + LOCAL_TTS_HOST/PORT)
# Required for Slack: SLACK_BOT_TOKEN_<AGENT>, SLACK_APP_TOKEN_<AGENT> per agent
docker compose up --build
alembic upgrade head
```

## Key Commands

```bash
# Start core services (postgres + app)
docker compose up -d postgres app

# Run podcast test for 'su' agent only
docker compose exec app python scripts/test_podcast_su.py

# Run podcast scheduler once (all agents)
python -m src.podcast.main

# Run migrations
alembic upgrade head
```

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

## Podcast Pipeline

The LabBot Podcast pipeline (specs/labbot-podcast.md) runs daily at 9am UTC for each active agent:

1. Build PubMed queries from lab's public profile
2. Fetch candidates from PubMed + bioRxiv + medRxiv + arXiv (last 14 days, up to 50+10 candidates)
3. Claude Sonnet selects most relevant paper
4. Claude Opus writes a ~250-word structured brief
5. TTS audio generated (Mistral or local vLLM-Omni); ffmpeg loudnorm applied
6. Slack DM sent to PI with text summary + RSS link
7. RSS feed available at `/podcast/{agent_id}/feed.xml`
8. Audio served at `/podcast/{agent_id}/audio/{date}.mp3`

Preprint IDs use prefixed format: `biorxiv:...`, `medrxiv:...`, `arxiv:...`. The `paper_url` in summaries links to the correct server (not always PubMed).

## Pilot Lab Agents

| agent_id | PI | ORCID |
|---|---|---|
| su | Andrew Su | 0000-0002-9859-4104 |
| wiseman | Luke Wiseman | 0000-0001-9287-6840 |
| lotz | Martin Lotz | 0000-0002-6299-8799 |
| cravatt | Benjamin Cravatt | 0000-0001-5330-3492 |
| grotjahn | Danielle Grotjahn | 0000-0001-5908-7882 |
| petrascheck | Michael Petrascheck | 0000-0002-1010-145X |
| ken | Megan Ken | 0000-0001-8336-9935 |
| racki | Lisa Racki | 0000-0003-2209-7301 |
| saez | Enrique Saez | 0000-0001-5718-5542 |
| wu | Chunlei Wu | 0000-0002-2629-6124 |
| ward | (Scripps) | — |
| briney | (Scripps) | — |
| forli | (Scripps) | — |
| deniz | (Scripps) | — |

*ORCIDs verified via ORCID public API on 2026-03-21 (original 8), 2026-03-22 (Saez, Wu).*

## Key Architectural Decisions

- **Session storage:** `itsdangerous`-signed cookies via Starlette `SessionMiddleware` (no Redis). Rotating `SECRET_KEY` invalidates all sessions — acceptable for pilot.
- **Profile sync:** When a `ResearcherProfile` is saved/updated in DB, it is automatically exported to `profiles/public/{agent_id}.md` to keep the filesystem (agent input) in sync with the DB.
- **Job queue:** Worker polls every 5 seconds. Profile generation is slow (minutes), so polling overhead is negligible.
- **Admin impersonation:** Routes at `/api/admin/impersonate` so the stop button works from any page.
- **Tailwind via CDN:** Avoids Node.js build step. Switch to compiled Tailwind for production if performance matters.
- **LLM models:** Opus for profile synthesis and podcast summaries; Sonnet for agent responses and podcast article selection.
- **TTS backend switch:** `PODCAST_TTS_BACKEND=mistral` (default) or `local`. In Docker, the podcast container uses `LOCAL_TTS_HOST=host.docker.internal` to reach a vLLM-Omni server on the host.
- **Preprint IDs:** Prefixed (`biorxiv:`, `medrxiv:`, `arxiv:`) so deduplication, delivery tracking, and full-text skip logic all work without special-casing in state.py.
