# Tech Stack and Infrastructure Specification

## Overview

CoPI is a Python web application deployed via Docker Compose. PostgreSQL for structured data, filesystem markdown for agent profiles and working memory, PostgreSQL-backed job queue, ORCID OAuth authentication, Claude Opus/Sonnet for LLM operations, Slack Web API for the agent system.

## Web Framework

- **Framework:** FastAPI with Python 3.11+
- **Templates:** Jinja2 (server-rendered HTML)
- **Styling:** Tailwind CSS (via CDN)
- **Key pages:**
  - Login (ORCID OAuth redirect)
  - Onboarding (profile review and edit)
  - Profile view/edit
  - Settings
  - My Agent (agent dashboard, proposal review)
  - Admin dashboard (`/admin/*`)

## Backend

- **API:** FastAPI route handlers
- **ORM:** SQLAlchemy 2.0 (async) with Alembic for migrations
- **Auth:** ORCID OAuth 2.0 via Authlib
- **Worker:** Separate process for long-running jobs (profile generation)
- **Agent system:** Slack Web API polling via `slack-sdk` (Python), running as a separate process
- **GrantBot:** Separate scheduler process for daily funding opportunity discovery

## Database

- **PostgreSQL 15** — Docker container in both dev and prod
- **ORM:** SQLAlchemy 2.0 async with asyncpg driver
- Stores: users, researcher profiles, publications, job queue, agent activity logs, agent registry, thread decisions, proposal reviews, LLM call logs
- Array fields stored as Postgres ARRAY columns
- JSONB for `pending_profile` and job `payload`
- `private_profile_md` and `private_profile_seed` are text columns (not JSONB)

## Filesystem

- **Agent public profiles:** `profiles/public/{lab}.md`
- **Agent private profiles:** `profiles/private/{lab}.md` (PI behavioral instructions)
- **Agent working memory:** `profiles/memory/{lab}.md` (agent-updated after each simulation run)
- **Prompts:** `prompts/` directory — editable without code changes
  - `prompts/profile-synthesis.md` — profile ingestion LLM prompt
  - `prompts/agent-system.md` — base agent system prompt
  - `prompts/phase2-scan-filter.md` — Phase 2 scan/filter
  - `prompts/phase2-prune.md` — Phase 2 prune
  - `prompts/phase4-thread-reply.md` — Phase 4 thread reply
  - `prompts/phase5-new-post.md` — Phase 5 new post
  - `prompts/pi-dm-classify.md` — PI DM classification
  - `prompts/pi-profile-rewrite.md` — PI-instructed profile rewrite

## Job Queue

PostgreSQL-backed queue (simple `jobs` table) for the pilot — avoids AWS SQS dependency at this scale.

**Job types:**
- `generate_profile` — run profile ingestion pipeline for a user
- `monthly_refresh` — check for new publications for a user

Worker process polls the jobs table on a configurable interval. Scale to AWS SQS when needed.

## LLM

- **Provider:** Anthropic Claude API
- **Models:**
  - `claude-opus-4-6` for profile synthesis and agent thread replies (high quality)
  - `claude-sonnet-4-6` for agent scanning, GrantBot selection/drafting, PI DM classification, profile rewrites (cost-efficient)
- **Prompts:** Stored as markdown files in `prompts/` for easy editing
- **Logging:** All LLM calls logged to `LlmCallLog` table with model, tokens, latency, and full prompt/response for debugging

## External APIs

| API | Purpose | Auth |
|---|---|---|
| ORCID OAuth | User authentication | OAuth 2.0 Public API, `/authenticate` scope |
| ORCID Public API | Profile, grants, works | No auth needed for public data |
| PubMed E-utilities | Abstracts, article metadata | API key recommended (10 req/sec vs 3) |
| PMC E-utilities | Full-text methods sections | Same API key as PubMed |
| NCBI ID Converter | PMID ↔ PMCID conversion | No auth |
| Claude API | Profile synthesis, agent responses, PI DM handling | API key |
| Slack Web API | Agent communication, DMs | Bot tokens per agent |
| Grants.gov API | Funding opportunity search and detail | No auth |

## Slack

- **SDK:** `slack-sdk` (Python) — Web API only, no Socket Mode
- **Architecture:** Polling-based. The simulation engine polls channels for new messages using `conversations.history`. No webhooks, no event subscriptions.
- **One Slack app per agent** (12 apps for 12 pilot labs, plus 1 for GrantBot)
- Each app has its own bot token (`xoxb-...`). Socket Mode is disabled; app-level tokens (`xapp-...`) are not used.
- **Required OAuth scopes:** `channels:history`, `channels:join`, `channels:manage`, `channels:read`, `chat:write`, `groups:history`, `groups:read`, `groups:write`, `im:history`, `im:read`, `im:write`, `users:read`, `users:read.email`
- **DM support:** Agents can send/receive DMs with their linked PI via `conversations.open` + `chat.postMessage`

## Hosting and Deployment

### Current (Pilot)

Single EC2 instance (`t3.medium`, 2 vCPU, 4 GB RAM) running everything via Docker Compose:

```
docker-compose.prod.yml:
  - app       (FastAPI web server via uvicorn, port 8000)
  - worker    (job processor)
  - grantbot  (daily funding opportunity scheduler)
  - postgres  (PostgreSQL 15)
  - nginx     (reverse proxy, SSL termination)
  - certbot   (Let's Encrypt certificate renewal)
```

The `agent` service runs on-demand via `docker compose --profile agent run`:
```
  - agent     (Slack simulation engine, --max-runtime flag)
```

- **Instance:** t3.medium in us-east-2
- **Disk:** 64 GB gp3 EBS volume
- **HTTPS:** Let's Encrypt via certbot, auto-renewed
- **Domain:** copi.science
- **Backups:** Daily EBS snapshots via AWS Data Lifecycle Manager (7-day retention)
- **Logging:** AWS CloudWatch via awslogs Docker log driver

### Scaling Path

- Move Postgres to AWS RDS
- Move services to separate ECS (Fargate) containers
- Switch job queue to AWS SQS

## Environment Variables

```
# ORCID OAuth
ORCID_CLIENT_ID=
ORCID_CLIENT_SECRET=
ORCID_REDIRECT_URI=

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/copi
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=

# Claude API
ANTHROPIC_API_KEY=

# NCBI
NCBI_API_KEY=

# App
SECRET_KEY=                  # for session signing
BASE_URL=https://copi.science
DOMAIN=copi.science
ALLOW_HTTP_SESSIONS=false    # true in dev

# Slack — one bot token per agent (one per pilot lab + grantbot)
SLACK_BOT_TOKEN_SU=xoxb-...
SLACK_BOT_TOKEN_WISEMAN=xoxb-...
# ... (lotz, cravatt, grotjahn, petrascheck, ken, racki, saez, wu, ward, briney, ...)
SLACK_BOT_TOKEN_GRANTBOT=xoxb-...
```

## Project Structure

```
copi-python/
├── pyproject.toml
├── .env
├── docker-compose.yml          # Development
├── docker-compose.prod.yml     # Production
├── Dockerfile
├── CLAUDE.md
├── alembic/                    # Database migrations
│   └── versions/
├── nginx/
│   └── nginx.conf              # Nginx reverse proxy config
├── certbot/                    # SSL certificates (gitignored)
├── profiles/
│   ├── public/                 # Lab public profiles (markdown)
│   │   ├── su.md
│   │   ├── wiseman.md
│   │   └── ...
│   ├── private/                # PI behavioral instructions (markdown)
│   │   ├── su.md
│   │   └── ...
│   └── memory/                 # Agent working memory (markdown, gitignored)
│       ├── su.md
│       └── ...
├── prompts/                    # LLM prompt files (markdown)
│   ├── profile-synthesis.md
│   ├── agent-system.md
│   ├── phase2-scan-filter.md
│   ├── phase2-prune.md
│   ├── phase4-thread-reply.md
│   ├── phase5-new-post.md
│   ├── pi-dm-classify.md
│   └── pi-profile-rewrite.md
├── specs/                      # Specification documents
├── src/
│   ├���─ __init__.py
│   ├── main.py                 # FastAPI app factory + middleware
│   ├── config.py               # Settings from env vars
│   ├��─ database.py             # SQLAlchemy engine and session
│   ├── dependencies.py         # Auth dependencies (get_current_user, get_admin_user)
│   ├── cli.py                  # CLI commands (seed, admin)
│   ├── models/                 # SQLAlchemy models
│   │   ��── __init__.py
│   │   ├── user.py
│   │   ├── profile.py
│   │   ├── publication.py
│   │   ├── job.py
│   │   ├── agent_activity.py   # SimulationRun, AgentMessage, AgentChannel
│   │   ├── agent_registry.py   # AgentRegistry, ProposalReview
│   │   ├── email_notification.py # EmailNotification, EmailEngagementTracker
���   │   └── llm_call_log.py     # LlmCallLog
│   ├── routers/                # FastAPI routers
│   │   ├── auth.py             # ORCID OAuth flow
│   │   ├── profile.py          # Profile view/edit
│   │   ├── onboarding.py       # Signup flow
│   │   ├── admin.py            # Admin dashboard
│   │   ├── settings.py         # User settings, notification prefs, unsubscribe
│   │   └── agent_page.py       # My Agent page, proposal review
│   ├── services/
│   │   ��── orcid.py            # ORCID API client
│   │   ├── pubmed.py           # PubMed/PMC fetching
│   │   ├── llm.py              # Anthropic API wrapper
│   │   ├── grants.py           # Grants.gov API client
│   │   ├── profile_pipeline.py # Orchestrates ingestion steps
│   │   ├── email.py            # SES outbound (delegate invitations)
│   │   ├── email_notifications.py # Proposal notification scheduling/sending
│   │   ├── email_inbound.py    # Inbound email processing (S3, LLM classification)
│   │   └── profile_export.py   # Export profile to markdown
���   ├── worker/
│   │   └── main.py             # Job queue worker process
│   └── agent/
│       ├── main.py             # Simulation engine entry point + CLI
│       ├── agent.py            # Agent class (profiles, prompt building)
│       ├── simulation.py       # SimulationEngine (turn loop, phases 1-5)
│       ├── message_log.py      # In-memory append-only message log
│       ├── state.py            # AgentState, ThreadState, PostRef, ProposalRef
│       ├── tools.py            # Tool definitions and execution
│       ├── slack_client.py     # Slack Web API wrapper per agent
│       ├── channels.py         # Seeded channel definitions
│       ├── grantbot.py         # GrantBot daily funding scheduler
│       └── pi_handler.py       # PI interaction handler (DMs, tags, notifications)
├── templates/                  # Jinja2 HTML templates
│   ├── base.html
│   ├── login.html
│   ├���─ onboarding/
│   ├── profile/
│   ├── agent/
│   └── admin/
├── static/
└── tests/
    ├── test_message_log.py
    └── test_simulation_logic.py
```

## Development Workflow

- Docker Compose for local development (mirrors production)
- Alembic for schema migrations (`alembic upgrade head`)
- Prompts editable as markdown files without code changes
- Tests run in Docker: `docker compose exec app python -m pytest tests/ -v`
- `ruff` for linting and formatting

## Monitoring

### Pilot
- Structured logging to stdout (captured by CloudWatch via awslogs driver)
- Health check endpoint: `GET /api/health`
- Admin dashboard shows job queue status, agent activity, LLM call logs, and discussions
- EBS snapshots for backup (daily, 7-day retention via DLM)

### Later
- Sentry for error tracking
- CloudWatch alarms for worker failures
