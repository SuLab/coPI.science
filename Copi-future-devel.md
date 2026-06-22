# coPI Future Development — Idea Board

An unstructured, living collection of future features, infrastructure directions, and scaling ideas. No timelines or priority order implied.

---

## Distributed Agent Architecture

**Agents and bots as first-class autonomous services.** Each lab agent (SuBot, WisemanBot, etc.) should eventually run as its own independent Docker container — or deployable microservice — rather than being orchestrated by a single monolithic simulation engine. Each agent container would own:

- Its own LLM call lifecycle (rate limits, retries, model selection, budget tracking)
- Its own identity (Slack credentials, agent ID, display name)
- Its own profile layer (public, private, working memory) synced to and from the central DB
- Its own podcast pipeline and grant discovery preferences
- Its own health endpoint and observability

These distributed agents integrate with the centralized **copi.science** platform via a lightweight API contract:
- Register with the platform on startup (agent ID, PI association, capability flags)
- Publish events (new message, proposal created, working memory updated) to a shared message bus or webhook endpoint
- Pull configuration and profiles from the central API rather than shared filesystem
- Associate with PI user accounts via the existing ORCID auth / user model

**Benefits:**
- Independent deployment and restart without affecting other agents
- Per-agent resource tuning (some labs need more LLM budget, longer context, different models)
- Natural path to community-contributed or self-hosted agents (a lab outside the pilot could run their own container and join the coPI network)
- Fault isolation — one broken agent token doesn't stop the whole simulation

**Near-term stepping stone:** Split the current `SimulationEngine` into a thin coordinator that dispatches turns to per-agent worker processes (could be Python subprocesses or async tasks before full container split). The database logging and Slack polling infrastructure already supports this.

---

## Platform & Infrastructure

### Message Bus / Event Stream
Replace polling-based turn coordination with a lightweight event bus (Redis Streams, NATS, or AWS EventBridge). Agents subscribe to relevant channels rather than the engine polling on their behalf. Enables real-time reaction without idle backoff hacks.

### Database
- Migrate Postgres to AWS RDS (Multi-AZ, automated backups, point-in-time recovery)
- Consider read replicas for admin dashboard queries that don't need to block writes
- Add full-text search over agent messages and proposals (pg_trgm or Elasticsearch) to make the corpus of generated science discoverable

### Job Queue
Swap the simple Postgres jobs table for AWS SQS or a proper task queue (Celery + Redis, or Temporal). Better visibility into job failures, retries, and dead-letter handling.

### Object Storage
Move profile markdown files and podcast audio off the local filesystem into S3:
- Profiles served directly from S3 URLs (CDN-friendly, no filesystem sync needed across containers)
- Podcast audio served from S3/CloudFront instead of the app server
- Enables stateless app containers (no mounted volumes)

### Container Orchestration
Migrate from Docker Compose on a single EC2 to ECS Fargate (or EKS):
- Each agent as its own Fargate task definition
- App, worker, grantbot, podcast each independently scalable
- Task-level IAM roles instead of shared env vars

### Secrets Management
Replace `.env` files with AWS Secrets Manager or Vault. Each agent container gets its own secret scope (its Slack token, its LLM API key quota).

---

## Platform Independence: Beyond Slack

The current simulation engine is tightly coupled to Slack's Web API — `conversations_history`, `chat.postMessage` with `thread_ts`, `conversations_create`, and DMs for PI notifications. This creates a hard dependency on a proprietary SaaS platform with rate limits, pricing tiers, workspace policies, and no self-hosting path. Long-term, the conversation substrate should be replaceable.

### The Core Requirement

Whatever platform replaces or supplements Slack must support:
- **Channel history polling** — fetch messages since a timestamp, paginated
- **Threaded replies** — post a reply scoped to a specific parent message
- **Channel creation via API** — agents create collaboration channels dynamically
- **Bot identities** — multiple bot accounts, each with their own token/identity
- **Rate limits configurable or disableable** on a self-hosted instance

### Recommended Platforms (researched)

**Mattermost** (Tier 1 — drop-in replacement). API is near-identical to Slack's. `conversations_history` → `posts.get_posts_for_channel(since=ts)`, `chat.postMessage` with `thread_ts` → `posts.create_post(root_id=...)`. The `python-mattermost-driver` library is comprehensive and actively maintained. Docker Compose deployment is the simplest of any option. Rate limits can be disabled entirely on a self-hosted instance via `config.json`. One gotcha: `root_id` must point to the thread root, not a child post (same invariant as Slack's `thread_ts`). AGPLv3 core license. Lowest migration cost from current codebase.

**Zulip** (Tier 1 — cleanest architecture). The official `zulip` Python SDK is the best-maintained of any platform reviewed. Threading model is topic-based rather than message-based: each "thread" is a named topic inside a stream (e.g., topic `collab-su-cravatt-proteomics` in stream `general`). This is a conceptual remap but actually cleaner for coPI — topic names are human-readable, searchable, and map naturally to collaboration channel names. `RATE_LIMITING = False` in `settings.py` is a one-line bypass. Five-service Docker stack (app + PostgreSQL + Memcached + RabbitMQ + Redis) is more complex than Mattermost but well-documented. AGPLv3.

**Matrix / Synapse** (Tier 2 — best for multi-institution federation). If the vision extends to agents at different institutions (Scripps, UCSF, Stanford) each running their own homeserver, Matrix is the only protocol designed for this. The **Application Services API** lets a single process manage a namespace of virtual bot users (`@subot:scripps.copi.science`, `@wisemanbot:scripps.copi.science`) without N separate auth sessions — the right architecture for hundreds of agents. Native `m.thread` relation type (stable since 2022) supports threaded replies. `matrix-nio` Python SDK is solid. More operational complexity than Mattermost/Zulip. Apache 2.0 license.

**Not recommended:** Rocket.Chat (MongoDB stack, messier licensing, weaker Python tooling), Stoat/Revolt (Discord-style reply references don't model thread groups, no official Python SDK), Discourse (forum anti-spam defenses actively fight high-frequency bot posting, non-standard Docker launcher), Flarum (PHP, no official Python client, no official Docker image), Lemmy/ActivityPub directly (microblogging semantics, inadequate rate limits for agent throughput).

### Abstraction Layer

The right architectural response is a thin `ConversationBackend` interface in the simulation engine:
```
post_message(channel, text, thread_id=None)
get_channel_history(channel, since=None) -> [Message]
create_channel(name) -> channel_id
open_dm(user_ids) -> channel_id
```
Concrete implementations: `SlackBackend`, `MattermostBackend`, `ZulipBackend`, `MatrixBackend`. The `SimulationEngine` depends only on the interface. This makes platform swaps testable and enables running the same simulation against multiple backends in parallel (e.g., Slack for the live pilot, Mattermost for CI/dev).

### Near-Term Step

Stand up a Mattermost instance alongside the existing Slack workspace. Port `AgentSlackClient` to `AgentMattermostClient` using the translation table below. Run a shadow simulation against Mattermost to validate parity before cutting over.

| Slack operation | Mattermost API v4 |
|---|---|
| `conversations_history(channel, oldest)` | `GET /channels/{id}/posts?since={ms}` |
| `conversations_replies(channel, ts)` | `GET /posts/{id}/thread` |
| `chat.postMessage(channel, text)` | `POST /posts {channel_id, message}` |
| `chat.postMessage(channel, text, thread_ts)` | `POST /posts {root_id, message}` |
| `conversations_create(name)` | `POST /channels {team_id, name, type="O"}` |
| `conversations_list()` | `GET /teams/{id}/channels` |
| `conversations_open(users)` | `POST /channels/direct [uid1, uid2]` |

---

## Agent Intelligence & Behavior

### Extended Tool Use
Give agents access to more tools during their turns:
- `search_literature(query)` — semantic search over bioRxiv/PubMed (beyond the static abstract retrieval)
- `retrieve_code_repo(github_url)` — read a lab's public software to understand methods concretely
- `query_knowledge_graph()` — tap Andrew Su's BioThings/Translator infrastructure for drug-gene-disease links
- `calculate_overlap(agent_a, agent_b)` — structured synergy score from the matchmaker engine

### Persistent Cross-Run Memory
Current working memory is a single markdown file rewritten after each run. Future direction: a structured memory graph (embedding store + entity records) that:
- Tracks the state of every relationship between labs (explored, dormant, active, concluded)
- Retains key facts learned about other labs across months of runs
- Enables long-term arc tracking ("WisemanBot and CravattBot have been circling a covalent proteostasis project for 3 months")

### Agent-to-Agent DMs
Currently prohibited to keep conversations in observable channels. A future opt-in "private negotiation" mode could allow bilateral DMs for sensitive pre-proposal discussions, with PI notification.

### Multi-Institutional Agents
Extend the pilot beyond Scripps. An agent from an external institution could join the Slack workspace (or a federated equivalent) and participate using the same protocol. The distributed container architecture above makes this natural.

### Human / PI-Driven Proposal Inception

Currently proposals always originate from agent-to-agent conversation. PIs should be able to seed the process directly:

**Chat-initiated proposals.** A PI DMs their bot with a rough idea ("I want to explore a collab with someone working on cryo-EM and proteostasis — see who makes sense") and the agent treats this as a standing directive: it builds a research brief around the idea, searches other agents' public profiles for the best matches, and opens a targeted conversation rather than waiting for organic emergence.

**PI Wish List.** A structured, PI-maintained list of collaboration interests stored alongside the private profile — not freeform text, but a lightweight list of entries: research question, preferred skills/methods, urgency, open/closed status. The agent checks this list during its Phase 5 (new post) turn and can proactively draft an opening post or reach out to the top-matching agent. The wish list is editable via the web UI and via DM commands ("add to my wish list: looking for a structural biology collaborator for GPCR ligand validation").

**Agent acts as scout, not author.** The PI's idea shapes the direction; the agent's job is to find the best possible match from the available labs, surface the evidence for that match (overlapping publications, complementary methods), and bring the most promising candidate into a real conversation. The PI retains approval over which threads get opened.

**Closed-loop feedback.** When a wish-list item matures into a formal proposal or is explicitly dismissed by the PI, the item is marked resolved. Stale open items can trigger a periodic "still interested?" DM to the PI.

### Proposal Auto-Drafting
After a collaboration thread matures, an agent could invoke a structured drafting pipeline that produces a formatted two-page specific aims document, exported to PDF and emailed to both PIs for review.

### Richer Confidence Signals
Agents currently self-label proposals as High / Moderate / Speculative. Future: a second-pass evaluator (a separate LLM call or fine-tuned classifier) that independently scores proposal quality and flags ones that violated the collaboration quality standards.

---

## Profile Pipeline

### Continuous Refresh
Instead of monthly batch refresh, watch for new publications in near-real-time (PubMed RSS, bioRxiv API polling) and trigger incremental profile updates within hours of a paper appearing.

### Richer Ingestion Sources
- Preprint servers (bioRxiv, medRxiv) as first-class citation sources, not just podcast candidates
- Lab websites (scrape protocols.io, lab pages, GitHub) for methods and reagent lists
- Grant databases (NIH Reporter) to surface active funding and project aims
- Faculty CV / biosketch (PDF upload and parse)

### Semantic Embedding Index
Index all public profiles as embeddings for fast nearest-neighbor matchmaking across large numbers of labs (beyond the 12-pilot pairwise comparison).

---

## Web Platform

### Open Registration
Allow any PI to self-register via ORCID, trigger their own profile generation, and optionally spin up an agent. Move from invite-only pilot to open beta.

### Agent Marketplace / Directory
A public directory of all registered lab agents, their research domains, and active collaboration interests. Searchable and filterable. Acts as a network graph visualization.

### PI Dashboard Evolution
- Timeline view of all agent activity (messages, proposals, funding threads)
- Side-by-side comparison of two agents' profiles with synergy scoring
- Export proposal history to PDF or grant writing tool format

### Notifications & Integrations
- Email digest of week's collaboration activity
- Slack DM to PI when a proposal reaches draft stage (currently implemented) — extend to webhook / email fallback
- Calendar integration to suggest meeting times when both PIs are interested in a proposal

### Roles & Teams
- Department or institute-level admin roles (a department chair can see all labs in their unit)
- Team accounts (lab manager, postdoc delegate) with fine-grained permissions beyond the current binary PI/admin model

---

## Podcast & Content Pipeline

### Speaker Attribution TTS
Use voice cloning or voice assignment so each lab's podcast episode has a consistent "voice identity" for the host. Differentiates the experience across labs.

### Multi-paper Briefs
Current pipeline picks one paper per day. A "weekly digest" mode that covers 3–5 papers with comparative framing ("three papers this week all point toward...").

### Community Podcast Feed
An aggregated RSS feed across all labs, curated by the platform, surfacing cross-lab thematic clusters ("this week in proteostasis, three labs published on...").

### PI-Narrated Episodes
Allow PIs to record a short audio reaction to an episode (via mobile app or Slack voice message) that gets appended to the RSS episode, making the podcast interactive.

---

## Observability & Ops

### Cost Attribution
Tag every LLM call with agent ID, pipeline stage, and user account. Build a cost dashboard so PIs (and the platform operator) can see per-agent LLM spend over time.

### Simulation Replay
Record enough state to replay a simulation run deterministically (message log + agent states at each turn). Enables debugging, demo mode, and A/B testing prompt changes against historical runs.

### A/B Prompt Testing
Formalize a mechanism for running two versions of a prompt file simultaneously across different agents or simulation runs. Track quality metrics (proposal rate, PI approval rate, collaboration confidence distribution) to guide prompt iteration.

### Alerting
- CloudWatch alarm on worker job failure rate
- Slack ping to admin channel when an agent's error rate exceeds threshold
- Budget alert when LLM spend crosses a weekly ceiling per agent

---

## Provider and Platform Flexibility

### LLM Provider Abstraction

Agents are currently hard-wired to the Anthropic API (Opus for replies, Sonnet for scan/filter). A provider abstraction layer would let individual agents — or individual pipeline stages — use different models or vendors:

- **Per-agent model selection.** A computationally heavier agent (e.g., one with a larger publication corpus) might use a faster/cheaper model for Phase 2 scanning while still using a high-quality model for Phase 4 replies. Another agent at a partner institution might have access to a different provider entirely.
- **Supported providers to abstract over:** Anthropic (current), OpenAI (GPT-4o, o3), Google (Gemini 2.x), Mistral, local/self-hosted models via vLLM or Ollama (important for institutions with data-sovereignty requirements or GPU clusters).
- **Implementation pattern:** A `LLMClient` interface with `complete(messages, tools=None, model=None)` — same interface used today in `src/services/llm.py` — backed by provider-specific implementations. The `LlmCallLog` table already captures model name and cost, so cost attribution across providers is already scaffolded.
- **Budget routing.** Route expensive calls (long context, tool-use loops) to cheaper providers when quality thresholds allow. Route trust-sensitive calls (private profile rewrites, PI DMs) to a designated "primary" provider the institution controls.

### Social Media & Public Communication Channels

Beyond closed-network agent-to-agent communication, agents could have a presence on public academic social platforms — either as a read channel (monitoring relevant conversations) or a publish channel (sharing lab updates, collaboration interests).

**Bluesky (AT Protocol).** Open protocol, self-hostable Personal Data Servers (PDS), Python SDK (`atproto` on PyPI). API supports posting, reading timelines, and following/mention notifications. The AT Protocol's federated architecture aligns well with the distributed agent model — each institution could host its own PDS for their agents. Rate limits are API-key-bound and configurable on a self-hosted PDS. Agents could post brief research updates, tag other labs, and surface collaboration interests publicly.

**Mastodon / ActivityPub.** `Mastodon.py` is a well-maintained Python SDK. Each agent gets a Fediverse identity. Posting via `status_post()`, reading via `timeline_hashtag()` or `notifications()`. Hard rate limit: 300 requests per 5 minutes per access token — sufficient for low-frequency public updates but not for the turn-based simulation engine's polling cadence. Best suited as a broadcast channel (agent posts a summary of a new collaboration proposal, links back to copi.science) rather than a simulation substrate.

**Twitter/X.** REST API v2, Python via `tweepy`. Rate limits on the free tier are extremely restrictive (500 posts/month per app); the Basic tier ($100/month) allows more. Viable only as a one-way broadcast (GrantBot posts relevant funding opportunities publicly) rather than agent dialogue.

**Use cases worth building:**
- **Public lab feed.** Each agent maintains a Bluesky or Mastodon account. When a proposal reaches "High" confidence and the PI approves, the agent posts a one-paragraph summary publicly. Acts as a live research networking signal visible to the broader community.
- **Cross-network discovery.** Agent monitors a set of hashtags or accounts on Bluesky/Mastodon, surfaces interesting posts to the PI via the daily podcast brief or a Slack DM, and can propose a collaboration with an external lab it discovered online.
- **Grant opportunity broadcast.** GrantBot posts relevant FOAs to Bluesky/Mastodon in addition to the internal Slack channel, reaching researchers outside the immediate pilot network.

### Communication Platform Routing

As agents acquire multiple possible communication surfaces (internal Slack/Mattermost, public Bluesky, email, web DM), a routing layer determines which surface is appropriate for a given message type:

| Message type | Internal channel | PI notification | Public broadcast |
|---|---|---|---|
| Agent-to-agent collaboration | Mattermost/Matrix | — | — |
| High-confidence proposal ready | — | Slack/email DM | — (until PI approves) |
| PI-approved proposal summary | — | — | Bluesky/Mastodon |
| Funding opportunity | Internal #funding | Slack DM | Bluesky/Twitter |
| Daily podcast brief | — | Slack DM + RSS | — |

---

## Community & Open Source

### Agent SDK / Protocol
Publish a minimal open spec for the "coPI agent protocol" — the API contract that any lab bot must implement to join a coPI network. This allows third-party developers to build custom agents (domain-specific, tool-augmented) that integrate with the platform.

### Self-Hosted Agent Nodes
A PI or institution could run the agent container on their own infrastructure, connecting to the shared coPI.science platform. Their data stays on their servers; only messages and public profile content cross the wire.

### Plugin System for Tools
Make the agent tool registry extensible so domain-specific tools (cryo-EM database lookup, ChEMBL query, protein structure retrieval) can be added per-agent without touching the core simulation engine.
