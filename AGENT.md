# AGENT.md — CoPI Python / LabAgent Implementation

## Project Overview

Python implementation of the CoPI researcher collaboration platform combined with the LabAgent multi-agent Slack system. ORCID OAuth, profile generation pipeline, profile editing UI, admin dashboard, and Slack-based AI agent simulation.

**GitHub:** https://github.com/andrewsu/coPI-python-opus
**Target domain:** copi.science
**Pilot:** 10 labs at Scripps Research

## What's In Scope

- ORCID OAuth authentication
- Profile ingestion pipeline (ORCID → PubMed → PMC → Claude Opus synthesis)
- Profile review/editing web UI (FastAPI + Jinja2)
- Admin dashboard (users, profiles, jobs, agent activity)
- Slack agent system (8 bots, simulation engine)

## What's Out of Scope

- Matching engine (pairwise proposal generation)
- Swipe interface
- Notifications (email)
- Daily digest

## Key Specs

All specs are in `/specs/`:
- `tech-stack.md` — FastAPI, SQLAlchemy 2.0 async, Postgres, Slack Bolt, Docker Compose
- `data-model.md` — User, ResearcherProfile, Publication, Job, SimulationRun, AgentMessage, AgentChannel
- `auth-and-user-management.md` — ORCID OAuth via Authlib, session cookies
- `profile-ingestion.md` — 9-step pipeline, ORCID → PubMed → PMC → LLM
- `admin-dashboard.md` — read-only, server-rendered, impersonation
- `agent-system.md` — Slack Bolt, Socket Mode, two-phase LLM calls, simulation engine
- `labbot-podcast.md` — daily personalized research briefing: PubMed search, LLM selection/summarization, Local or API TTS, Slack DM delivery, per-PI RSS podcast feed

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
| LLM | Anthropic Claude (Opus for profiles, Sonnet for agents) |
| Slack | slack-bolt (Socket Mode) |
| Styling | Tailwind CSS (CDN) |
| Deployment | Docker Compose |

## Project Structure

```
src/
├── main.py                 # FastAPI app factory
├── config.py               # Settings from env vars
├── database.py             # SQLAlchemy engine and session
├── models/                 # SQLAlchemy models
├── routers/                # FastAPI routers (auth, profile, onboarding, admin)
├── services/               # Business logic (orcid, pubmed, llm, pipeline)
├── worker/                 # Job queue worker process
└── agent/                  # Slack simulation engine
profiles/
├── public/                 # Lab public profiles (markdown)
└── private/               # Lab private profiles + working memory (markdown)
prompts/                    # LLM prompt files
templates/                  # Jinja2 HTML templates
```

## Decisions Log

Decisions made autonomously during implementation are recorded here for human review.

### 2026-03-20: Admin impersonation endpoint location
**Decision:** Admin impersonation routes placed at `/api/admin/impersonate` (POST) and `/api/admin/impersonate/stop` (POST) rather than inside the `/admin` router prefix.
**Reason:** The impersonate stop button posts from any page (including non-admin pages when impersonating), so a clean `/api/admin/` prefix was clearer. Both routes still require is_admin verification.

### 2026-03-20: Login page GET /login serves both redirect and HTML
**Decision:** `/login` GET route redirects directly to ORCID OAuth if not already logged in. The login.html page has its sign-in button also pointing to `/login` (which re-triggers the redirect).
**Reason:** Simplifies the OAuth flow — one endpoint handles both "show the login page" and "start the OAuth flow". The page renders as a landing/marketing page with an ORCID button; clicking it calls /login again which starts OAuth.

### 2026-03-20: Agent __init__.py files
**Decision:** Created empty __init__.py files for src/agent/, src/worker/, src/routers/, src/models/ packages.
**Reason:** Required for Python package imports to work correctly.

### 2026-03-20: Pilot lab ORCID verification
**Decision:** ORCIDs in AGENT.md are marked as placeholders. They should be verified against orcid.org before running the seeding pipeline.
**Reason:** ORCID IDs were provided in the spec but not independently verified. Wrong ORCIDs would fetch the wrong researcher's publications.

### 2026-03-20: Session storage
**Decision:** Use `itsdangerous`-signed cookies via Starlette's `SessionMiddleware` (in-memory, cookie-based) rather than server-side sessions.
**Reason:** Simplest approach that is production-safe at pilot scale. No Redis dependency. Session data is minimal (user_id only).
**Risk:** Rotating SECRET_KEY invalidates all sessions. Acceptable for pilot.

### 2026-03-20: Job queue polling interval
**Decision:** Worker polls every 5 seconds.
**Reason:** Profile generation is slow (minutes), so 5s polling overhead is negligible. Simple sleep loop without external scheduler dependency.

### 2026-03-20: Tailwind via CDN
**Decision:** Load Tailwind CSS from CDN (`cdn.tailwindcss.com`) rather than building locally.
**Reason:** Avoids Node.js build step in a Python project. Acceptable for pilot; switch to compiled Tailwind for production if performance matters.

### 2026-03-20: Profile markdown export
**Decision:** When a ResearcherProfile is saved/updated in the DB, automatically export it to `profiles/public/{lab}.md` if the user is one of the 8 pilot labs (matched by ORCID).
**Reason:** Keeps the DB (source of truth) and filesystem (agent input) in sync without a separate sync step.

## Implementation Status

- [x] Project scaffolding (pyproject.toml, Docker, Alembic)
- [x] Database models
- [x] ORCID OAuth
- [x] Profile ingestion pipeline
- [x] Web UI (login, onboarding, profile view/edit)
- [x] Admin dashboard
- [x] Worker process
- [x] Agent system (Slack bots, simulation engine)
- [x] Agent profiles (8 pilot labs, auto-generated structure)
- [x] Prompt files

## Pilot Lab ORCIDs

| PI | ORCID |
|---|---|
| Andrew Su | 0000-0002-9859-4104 |
| Luke Wiseman | 0000-0001-9287-6840 |
| Martin Lotz | 0000-0002-6299-8799 |
| Benjamin Cravatt | 0000-0001-5330-3492 |
| Danielle Grotjahn | 0000-0001-5908-7882 |
| Michael Petrascheck | 0000-0002-1010-145X |
| Megan Ken | 0000-0001-8336-9935 |
| Lisa Racki | 0000-0003-2209-7301 |
| Enrique Saez | 0000-0001-5718-5542 |
| Chunlei Wu | 0000-0002-2629-6124 |

*ORCIDs verified via ORCID public API on 2026-03-21 (original 8), 2026-03-22 (Saez, Wu).*

## Environment Setup

```bash
cp .env.example .env
# Fill in: ORCID_CLIENT_ID, ORCID_CLIENT_SECRET, ANTHROPIC_API_KEY, NCBI_API_KEY
# Fill in Slack tokens after creating apps (see agent-system.md)
docker compose up --build
alembic upgrade head
python -m src.cli seed-profiles --file orcids.txt
```

## Running the Agent Simulation

```bash
python -m src.agent.main --max-runtime 60 --budget 50
```
