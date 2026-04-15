# LabBot Podcast Specification

## Overview

LabBot Podcast is a daily personalized research briefing service for researchers. It surfaces the single most relevant and impactful recent publication from the scientific literature based on the researcher's profile, generates a structured text summary highlighting findings and tools useful to their ongoing work, and produces a short audio episode via Mistral AI TTS. Researchers can subscribe to a personal RSS podcast feed to listen to the audio.

The system runs once per day and requires no researcher interaction to be useful — but researchers can tune it through a web UI. There are two delivery paths:

- **Agent path** — pilot-lab PIs with an approved `AgentRegistry` entry additionally receive the text summary as a Slack DM from their lab bot.
- **User path** — any researcher who has completed ORCID onboarding and has a `ResearcherProfile` with a research summary receives the podcast automatically. No Slack bot, agent approval, or admin action required.

---

## Architecture

### Service Placement

LabBot Podcast runs as a separate Docker container (`podcast` service), mirroring the GrantBot pattern:
- Long-running scheduler process
- Executes once per calendar day at 9am UTC (1 hour after GrantBot)
- If the container was down at the scheduled time, runs immediately on startup (catch-up)
- State persisted in `data/podcast_state.json` (tracks which articles have been delivered per agent)

### Delivery Paths

| Path | Who | Profile source | Delivery | Audio/RSS key |
|---|---|---|---|---|
| **Agent** | Pilot-lab PIs with active `AgentRegistry` | `profiles/public/{agent_id}.md` (disk) | Slack DM + RSS | `agent_id` string |
| **User** | Any ORCID user with completed `ResearcherProfile` | `ResearcherProfile` DB row (structured fields) | RSS only | `user_id` UUID |

Both paths run in the same daily scheduler pass. A user who has both a `ResearcherProfile` and an active agent is handled only by the agent path (no duplicate episode).

### Dependencies on Existing Systems

| Existing component | How Podcast uses it |
|---|---|
| `ResearcherProfile` DB model | Source of research areas, keywords, techniques, disease areas for the user path |
| `profiles/public/{lab}.md` | Profile text for the agent path (LLM article selection and summary) |
| `src/services/pubmed.py` | Literature search (keyword + MeSH queries) |
| `src/services/llm.py` | Article selection ranking and summary generation (all calls logged to `LlmCallLog`) |
| `AgentRegistry` | Maps agent → PI → Slack bot token for DM delivery (agent path only) |
| `User.id` (UUID) | Stable, opaque RSS feed token for the user path |
| Slack bot DM | Text summary delivery (agent path only) |

### New External Dependency

**Mistral AI API** — text-to-speech generation.
- Configured via `MISTRAL_API_KEY` environment variable
- Voice selection per agent configured in `data/podcast_voices.json` (agent_id → voice_id); falls back to a default voice if not set
- Audio files stored at `data/podcast_audio/{agent_id}/{YYYY-MM-DD}.mp3`

---

## Daily Pipeline

Each day the scheduler runs two loops in sequence:

1. **Agent loop** — iterates over all active `AgentRegistry` entries and calls `run_pipeline_for_agent()` for each.
2. **User loop** — iterates over all `User` rows where `onboarding_complete=True` and `profile.research_summary IS NOT NULL`, skipping any whose `user_id` appeared in the agent loop, and calls `run_podcast_for_user()` for each.

For each recipient, the pipeline executes the following steps sequentially:

### Step 1: Load Profile

- **Agent path**: read `profiles/public/{agent_id}.md` from disk. If absent, skip.
- **User path**: construct profile text from structured `ResearcherProfile` DB fields (`research_summary`, `disease_areas`, `techniques`, `experimental_models`, `keywords`). If `research_summary` is empty, skip.

### Step 2: Build Search Queries

Construct PubMed search terms from the profile:
- Extract top research area keywords
- Extract technique and experimental model terms
- Combine into 2–3 PubMed query strings (e.g., `(proteostasis OR unfolded protein response) AND (neurodegeneration OR proteomics)`)
- Inject any `extra_keywords` from `PodcastPreferences` as additional quoted terms
- Limit to publications from the last 14 days (rolling window ensures coverage across weekend/holiday gaps)
- Cap at 50 candidate abstracts

### Step 3: Fetch Candidate Abstracts

Use `src/services/pubmed.py` to execute each query and retrieve PMIDs + abstracts. Deduplicate across queries. Skip any PMID already in `podcast_state.json` for this recipient (agent or user) to prevent re-delivering the same article.

### Step 4: LLM Article Selection (Sonnet)

Single LLM call (Sonnet) with:
- The researcher's full profile text (disk for agent path; constructed from DB for user path)
- The list of candidate abstracts (title + abstract text, numbered)
- Any journal preferences from `PodcastPreferences`
- Prompt: `prompts/podcast-select.md`

The LLM returns the index of the single best article, along with a one-sentence justification of why it is relevant to this researcher's ongoing work. If no article meets a minimum relevance threshold, it returns `null` and the pipeline skips delivery today.

### Step 5: Generate Text Summary (Opus)

One LLM call (Opus) with:
- The researcher's full profile text
- The selected article's title, abstract, and full text (fetched via `retrieve_full_text` if available in PMC, otherwise abstract only)
- Prompt: `prompts/podcast-summarize.md`

Output is a structured text summary (see format below). This is used as the TTS input and stored in `PodcastEpisode.text_summary`.

### Step 6: Generate Audio (Mistral AI)

Pass the text summary to the Mistral AI TTS API:
- Voice: from `PodcastPreferences.voice_id`, or `MISTRAL_TTS_DEFAULT_VOICE`
- Model: configurable via `MISTRAL_TTS_MODEL`
- Output: MP3 file saved to:
  - Agent path: `data/podcast_audio/{agent_id}/{YYYY-MM-DD}.mp3`
  - User path: `data/podcast_audio/users/{user_id}/{YYYY-MM-DD}.mp3`
- If TTS fails, the episode DB row is **not** written (see commit-last ordering); the run returns `False`.

### Step 7: Deliver via Slack DM _(agent path only)_

Send the text summary as a DM from the agent's Slack bot to its PI, appending the RSS feed URL. User-path episodes are delivered via RSS only — no Slack bot is required.

### Step 8: Persist Episode and Update State

1. Write the `PodcastEpisode` row to the DB:
   - Agent path: `agent_id` set, `user_id` NULL
   - User path: `user_id` set, `agent_id` NULL
2. Append the delivered PMID to `data/podcast_state.json` (keyed by `agent_id` or `user_id`) to prevent re-delivery.

---

## Text Summary Format

The Opus-generated summary follows a consistent structure. The prompt enforces this layout:

```
*Today's Research Brief — {Date}*

*{Paper Title}*
{Authors} · {Journal} · {Year}

*What they found:*
2–3 sentences on the core findings — specific results, effect sizes, or observations.

*Key output:*
1–2 sentences on any tool, method, dataset, or reagent released with the paper (if applicable). Omit this section if the paper has no distinct output.

*Why this matters for your lab:*
2–3 sentences connecting the paper's findings and outputs specifically to the PI's ongoing research areas, techniques, or open questions. Ground this in the PI's profile — name specific techniques, model systems, or questions from their work.

*PubMed:* https://pubmed.ncbi.nlm.nih.gov/{PMID}/
```

The Slack DM appends a line at the bottom:
> _Listen to the audio version: {rss_feed_url}_

---

## RSS Podcast Feed

### Endpoints

| Path | Auth | Key |
|---|---|---|
| `GET /podcast/{agent_id}/feed.xml` | None | Pilot-lab agent |
| `GET /podcast/{agent_id}/audio/{date}.mp3` | None | Pilot-lab agent |
| `GET /podcast/users/{user_id}/feed.xml` | None | Plain ORCID user |
| `GET /podcast/users/{user_id}/audio/{date}.mp3` | None | Plain ORCID user |

All four endpoints are public and unauthenticated. The `user_id` UUID is opaque and acts as a stable, subscribable feed token — equivalent to a private podcast URL. Users retrieve their feed URL from the `/podcast/settings` page.

### Feed Structure

Standard RSS 2.0 with iTunes podcast extensions (identical structure for both paths):

```xml
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{Name} — LabBot Research Briefings</title>
    <description>Daily personalized research summaries for {Name}.</description>
    <link>{feed_url}</link>
    <itunes:author>{Name}</itunes:author>
    <itunes:category text="Science"/>
    <item>
      <title>{Paper Title} — {Date}</title>
      <description>{text summary}</description>
      <enclosure url="{audio_url}" type="audio/mpeg" length="{file_size}"/>
      <pubDate>{RFC 822 date}</pubDate>
      <guid>{agent_id|user-{user_id}}-{YYYY-MM-DD}</guid>
      <itunes:duration>{duration}</itunes:duration>
    </item>
    ...
  </channel>
</rss>
```

### Audio File Storage

| Path | Audio directory |
|---|---|
| Agent path | `data/podcast_audio/{agent_id}/{YYYY-MM-DD}.mp3` |
| User path | `data/podcast_audio/users/{user_id}/{YYYY-MM-DD}.mp3` |

Files are streamed with `Content-Type: audio/mpeg`.

---

## LLM Prompt Files

Two new prompt files in `prompts/`:

### `prompts/podcast-select.md`

Instructs the LLM to act as a literature triage assistant for a specific PI. It receives:
- The PI's public profile (research areas, techniques, open questions, unique capabilities)
- Numbered list of candidate abstracts (title + abstract)

It must return:
- The number of the most relevant article, or `null` if none clears the relevance bar
- A one-sentence justification referencing a specific aspect of the PI's profile

Key instructions in the prompt:
- Relevance is defined as: the paper's findings or outputs could plausibly accelerate or inform a specific aspect of the PI's ongoing work
- Recency alone is not sufficient — the connection must be specific
- Prefer papers that release a tool, method, dataset, or reagent alongside findings
- Do not pick review articles or editorials

### `prompts/podcast-summarize.md`

Instructs the LLM to act as a science communicator writing for a specific PI. It receives:
- The PI's public profile
- Full paper text (or abstract if full text unavailable)

It must produce the structured summary described above. Key instructions:
- The "Why this matters for your lab" section must name specific techniques, model systems, or open questions from the PI's profile — no generic connections
- Tone is like a knowledgeable postdoc briefing their PI: specific, direct, no filler
- The "Key output" section is only included if the paper releases a concrete artifact (tool, code, dataset, method, reagent); skip it otherwise
- Target length: ~250 words total

---

## Data Model

### `PodcastEpisode`

Rows are keyed by either `agent_id` (string) or `user_id` (UUID FK to `users.id`). Exactly one should be set per row.

```python
class PodcastEpisode(Base):
    __tablename__ = "podcast_episodes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("users.id"), nullable=True, index=True)
    episode_date: Mapped[date] = mapped_column(Date, nullable=False)
    pmid: Mapped[str] = mapped_column(String(100), nullable=False)
    paper_title: Mapped[str] = mapped_column(String(500), nullable=False)
    paper_authors: Mapped[str] = mapped_column(String(500), nullable=False)
    paper_journal: Mapped[str] = mapped_column(String(255), nullable=False)
    paper_year: Mapped[int] = mapped_column(Integer, nullable=False)
    paper_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    text_summary: Mapped[str] = mapped_column(Text, nullable=False)
    audio_file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slack_delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    selection_justification: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Agent-path: one episode per agent per day
        UniqueConstraint("agent_id", "episode_date", name="uq_podcast_agent_date"),
        # User-path: enforced by partial unique index (migration 0013):
        # CREATE UNIQUE INDEX ix_podcast_episodes_user_date
        #   ON podcast_episodes (user_id, episode_date) WHERE user_id IS NOT NULL
    )
```

### `PodcastPreferences`

Rows are keyed by either `agent_id` or `user_id`. Both columns are nullable and uniquely indexed.

```python
class PodcastPreferences(Base):
    __tablename__ = "podcast_preferences"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("users.id"), nullable=True, unique=True, index=True)
    voice_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), server_default="{}")
    preferred_journals: Mapped[list[str]] = mapped_column(ARRAY(String), server_default="{}")
    deprioritized_journals: Mapped[list[str]] = mapped_column(ARRAY(String), server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

### State File (`data/podcast_state.json`)

Keyed separately for agents and users:

```json
{
  "agents": {
    "<agent_id>": { "delivered_pmids": ["12345", "67890"] }
  },
  "users": {
    "<user_id UUID string>": { "delivered_pmids": ["11111"] }
  },
  "last_run_date": "2026-04-14"
}
```

The state file is a lightweight deduplication cache. The DB is the authoritative record for RSS generation and admin visibility.

### Alembic Migrations

| Migration | Creates / alters |
|---|---|
| `0010_add_podcast_episodes.py` | `podcast_episodes` table (agent path) |
| `0011_add_podcast_paper_url.py` | `paper_url` column |
| `0012_add_podcast_preferences.py` | `podcast_preferences` table (agent path) |
| `0013_podcast_user_support.py` | `user_id` FK on both tables; make `agent_id` nullable; partial unique index for user-path episodes |

---

## Configuration

New environment variables:

| Variable | Required | Description |
|---|---|---|
| `MISTRAL_API_KEY` | Yes (for audio) | Mistral AI API key |
| `MISTRAL_TTS_MODEL` | No | TTS model ID (default: `mistral-tts-latest`) |
| `MISTRAL_TTS_DEFAULT_VOICE` | No | Default voice when no per-agent override exists |
| `PODCAST_BASE_URL` | Yes | Public base URL for RSS enclosure links (e.g., `https://copi.science`) |
| `PODCAST_SEARCH_WINDOW_DAYS` | No | Rolling search window in days (default: `14`) |
| `PODCAST_MAX_CANDIDATES` | No | Max PubMed abstracts per agent per day (default: `50`) |

Per-agent voice overrides (Phase 2/3): `data/podcast_voices.json`
```json
{
  "su": "alex",
  "wiseman": "stella"
}
```
**Deprecated in Phase 4** — voice preferences move to the `podcast_preferences` DB table. The JSON file is still read as a fallback while the migration is in progress.

---

## Docker Service

Add `podcast` service to `docker-compose.yml` and `docker-compose.prod.yml`:

```yaml
podcast:
  build: .
  command: python -m src.podcast.main
  env_file: .env
  volumes:
    - ./data:/app/data
  depends_on:
    - postgres
  profiles:
    - podcast
```

Run with: `docker compose --profile podcast up -d podcast`

---

## Module Structure

```
src/podcast/
├── main.py          # Scheduler entry point (APScheduler, same pattern as grantbot.py)
├── pipeline.py      # Per-agent pipeline (steps 1–8 above)
├── pubmed_search.py # Query builder from ResearcherProfile
├── mistral_tts.py   # Mistral AI TTS client wrapper
├── rss.py           # RSS feed builder (reads from DB)
└── state.py         # podcast_state.json read/write helpers

src/routers/podcast.py   # FastAPI routes: /podcast/{agent_id}/feed.xml, /podcast/{agent_id}/audio/{date}.mp3
```

The scheduler in `src/podcast/main.py` follows the same catch-up-on-startup pattern as `src/agent/grantbot.py`:
1. On startup, check `data/podcast_state.json` for last run timestamp
2. If last run was before today's 9am UTC, run immediately
3. Schedule next run at 9am UTC

---

## Admin Dashboard Integration

Add a **Podcast** tab to the existing admin dashboard (`src/routers/admin.py` + `templates/admin.html`) showing:
- Table of recent episodes: agent, date, paper title, PMID, Slack delivered (yes/no), audio generated (yes/no)
- Link to each agent's RSS feed
- LLM call counts and token usage for the podcast pipeline (pulled from `LlmCallLog` filtered by `source = "podcast"`)

The LLM calls from the podcast pipeline should set a `source` tag in `LlmCallLog` (add a `source` column via migration if not already present, or use the existing `extra_metadata` JSONB field).

---

## PI Customization

### Via Standing Instructions (Current)

PIs can adjust podcast behavior through standing instructions to their lab bot (same DM mechanism as the agent system — see `pi-interaction.md`). The podcast pipeline reads the private profile when building the selection prompt.

Examples of effective standing instructions:
- "For my daily podcast, focus only on papers that release a new tool or dataset — I don't need summaries of pure wet-lab findings"
- "Prioritize papers from computational biology journals for the podcast"
- "Skip anything about C. elegans — we're not pursuing that direction anymore"

The bot's private profile rewrite (via `prompts/pi-profile-rewrite.md`) should include a `## Podcast Preferences` section that the podcast pipeline reads when constructing the selection and summarization prompts.

### Via Preferences UI (Phase 4)

A structured preferences page at `/agent/{agent_id}/podcast-settings` replaces the `data/podcast_voices.json` file and augments the standing-instructions mechanism with three explicit controls:

1. **Voice** — select the TTS voice used for audio generation
2. **Extra search keywords** — additional terms appended to PubMed/preprint queries beyond the auto-extracted profile keywords
3. **Source preferences** — journals or preprint servers to prioritize (boosted in the selection prompt) or deprioritize

See the **Podcast Preferences UI** section below for the full design.

---

## Podcast Preferences UI

### Route and Access Control

| Route | Method | Handler | Access | Notes |
|---|---|---|---|---|
| `/agent/{agent_id}/podcast-settings` | `GET` | Render agent preferences form | Agent owner or admin | Agent path |
| `/agent/{agent_id}/podcast-settings` | `POST` | Save agent preferences | Agent owner or admin | Agent path |
| `/podcast/settings` | `GET` | Render user preferences form | Any authenticated user with completed profile | User path |
| `/podcast/settings` | `POST` | Save user preferences | Any authenticated user with completed profile | User path |
| `/podcast/user/generate` | `POST` | Trigger on-demand episode | Any authenticated user with completed profile | User path |

The agent-path routes remain in `src/routers/agent_page.py` with the same `get_agent_with_access()` ownership check. The user-path routes live in `src/routers/podcast.py` and use `get_current_user()` + a profile-completeness check (`onboarding_complete=True` and `profile.research_summary IS NOT NULL`).

### User Feed URL

After saving preferences or visiting `/podcast/settings`, the user sees their personal feed URL:

```
{PODCAST_BASE_URL}/podcast/users/{user.id}/feed.xml
```

This URL:
- Requires no authentication to read (subscribe in any podcast app)
- Is stable for the lifetime of the user account
- Acts as an opaque token — not guessable, not secret, but not publicly listed
- Is displayed with a one-click copy button on the settings page

### Form Fields

#### 1. Voice Selection

A `<select>` dropdown pre-populated with valid Mistral Voxtral voices. The current TTS model is `voxtral-mini-tts-latest`.

Available voices for `voxtral-mini-tts-latest` (verify current list at [Mistral docs](https://docs.mistral.ai/capabilities/audio/#text-to-speech)):

| Voice ID | Description |
|---|---|
| `alex` | US English, male, neutral |
| `deedee` | US English, female, bright |
| `jasmine` | US English, female, warm |
| `laurel` | US English, female, clear |
| `luna` | US English, female, soft |
| `rio` | US English, male, energetic |
| `stella` | US English, female, professional |
| `theo` | US English, male, measured |
| `tyler` | US English, male, conversational |

> **Note:** This list should be refreshed from the Mistral API at deploy time. If Mistral exposes a `GET /v1/audio/voices` endpoint, the admin UI should call it to populate the dropdown dynamically. If not available, hardcode from the table above and update as the API evolves.

The form shows a short audio preview label next to each voice name if available. The current agent's voice is pre-selected; if no voice is set, the first voice in the list is shown as the default.

#### 2. Extra Search Keywords

A plain `<textarea>` accepting one keyword or phrase per line. These are appended as additional quoted terms to the PubMed/preprint query in Step 1 of the pipeline.

```
insulin receptor substrate
adipose tissue browning
mitochondrial fission
```

Stored as `extra_keywords: list[str]` (each non-blank line becomes one entry). Max 20 entries, each up to 100 characters.

#### 3. Source Preferences

Two separate tag-input fields (or textareas with comma-separation):

**Preferred sources** — journals or preprint servers to actively surface. Shown first in the selection-prompt candidate list and referenced explicitly in the prompt:
> "Prefer papers from: {preferred_journals}. Give these extra weight when relevance is comparable."

**Deprioritized sources** — journals or preprint servers to down-rank. Added as a negative signal in the selection prompt:
> "Deprioritize papers from: {deprioritized_journals} unless exceptionally relevant."

Examples:
- Preferred: `Nature Methods`, `Cell Systems`, `bioRxiv`, `eLife`
- Deprioritized: `Frontiers in ...`, `PLOS ONE`

Stored as `preferred_journals: list[str]` and `deprioritized_journals: list[str]`.

### Template

`templates/agent/podcast_settings.html` — extends `base.html`, matches the visual style of `templates/agent/profile_edit.html`.

Sections:
1. **Voice** — `<select>` with voice options
2. **Extra Keywords** — `<textarea>` with instructions
3. **Source Preferences** — two `<textarea>` fields (preferred / deprioritized), comma or newline separated
4. **Save button** — POSTs to the same URL, redirects back on success with a flash message

### Pipeline Integration

In `run_pipeline_for_agent()` (`src/podcast/pipeline.py`), after loading profile and preferences text:

```python
# Load structured preferences from DB
prefs = await _load_podcast_preferences_structured(agent_id)  # returns PodcastPreferences | None

# Step 2 (query building): inject extra_keywords
if prefs and prefs.extra_keywords:
    queries.extend(
        f'"{kw}"' for kw in prefs.extra_keywords[:20]
    )

# Step 3 (article selection): inject journal preferences into selection prompt
journal_context = ""
if prefs and prefs.preferred_journals:
    journal_context += f"\nPreferred sources: {', '.join(prefs.preferred_journals)}."
if prefs and prefs.deprioritized_journals:
    journal_context += f"\nDeprioritized sources: {', '.join(prefs.deprioritized_journals)}."
# journal_context is appended to the {preferences} block in the selection prompt

# Step 5 (TTS): use voice from preferences
voice_override = prefs.voice_id if prefs else None
# mistral_tts.get_voice() checks PodcastPreferences first, then podcast_voices.json, then env default
```

Add `_load_podcast_preferences_structured(agent_id)` as an async helper that queries `PodcastPreferences` and returns the ORM row or `None`.

Update `mistral_tts.get_voice()` and `local_tts.get_voice()` to accept an optional `voice_override` parameter passed from the pipeline instead of reading from `podcast_voices.json` directly.

### Admin Visibility

The existing `/admin/podcast` page gets a **Preferences** column in the agent filter section: when an agent is selected, show a summary of its preferences (voice, keyword count, journal counts) with a link to the preferences page.

---

## Module Structure

```
src/podcast/
├── main.py            # Scheduler: agent loop then user loop
├── pipeline.py        # run_pipeline_for_agent() + run_podcast_for_user()
├── pubmed_search.py   # Query builder from profile dict
├── preprint_search.py # bioRxiv / medRxiv / arXiv search
├── mistral_tts.py     # Mistral AI TTS client
├── local_tts.py       # Local vLLM-Omni TTS client (optional)
├── tts_utils.py       # ffmpeg loudnorm, duration extraction
├── rss.py             # RSS feed builder (agent_id or user_id keyed)
└── state.py           # podcast_state.json helpers (agent + user variants)

src/routers/podcast.py     # All podcast HTTP endpoints
templates/
├── agent/podcast_settings.html   # Agent-path preferences UI
└── podcast_settings.html          # User-path preferences UI (+ feed URL card)
```

---

## Rollout Phases

### Phase 1: Text-only delivery _(complete)_
- PubMed search, LLM selection, Opus summarization
- Slack DM delivery
- `PodcastEpisode` DB table and admin visibility
- No audio, no RSS

### Phase 2: Audio + RSS _(complete)_
- Mistral AI TTS integration
- Audio file storage and streaming endpoint
- RSS feed generation and `/podcast/{agent_id}/feed.xml` endpoint
- Per-agent voice configuration

### Phase 3: PI customization surface _(complete)_
- Podcast preferences section in private profile
- Pipeline reads preferences when building prompts
- Admin dashboard podcast tab with LLM usage metrics

### Phase 4: Structured Preferences UI _(complete)_
- `PodcastPreferences` DB table (migration `0012`)
- `GET/POST /agent/{agent_id}/podcast-settings` route and form
- Voice picker, extra keywords, source preferences
- Deprecate `data/podcast_voices.json` in favour of DB-stored voice preference

### Phase 5: Open Access for Plain ORCID Users _(implemented in migration 0013)_
- **Goal**: any researcher who signs in with ORCID and completes their profile receives daily podcast briefings automatically — no agent approval, no Slack bot required.
- **Schema**: migration `0013` adds `user_id` FK to `podcast_preferences` and `podcast_episodes`; makes `agent_id` nullable in both tables; adds partial unique index for user-path episodes.
- **Pipeline**: `run_podcast_for_user(user_id, db_session)` in `src/podcast/pipeline.py` — loads profile from `ResearcherProfile` DB row (no disk file), queries PubMed/preprints, selects article, generates audio, and persists a `PodcastEpisode` keyed by `user_id`.
- **Scheduler**: `src/podcast/main.py` runs the user loop after the agent loop; users whose `user_id` appears in an active `AgentRegistry` row are skipped (covered by agent path).
- **Endpoints** (all in `src/routers/podcast.py`):
  - `GET /podcast/users/{user_id}/feed.xml` — public RSS feed
  - `GET /podcast/users/{user_id}/audio/{date}.mp3` — audio streaming
  - `GET /podcast/settings` — preferences UI (auth-gated)
  - `POST /podcast/settings` — save preferences (auth-gated)
  - `POST /podcast/user/generate` — on-demand episode trigger (auth-gated)
- **State**: `data/podcast_state.json` gains a `"users"` section keyed by user_id UUID strings.
- **Eligibility gate**: `user.onboarding_complete == True` and `profile.research_summary IS NOT NULL`. Users who have not yet built their profile are silently skipped.

---

## Out of Scope

- Real-time or on-demand article requests from non-authenticated callers
- Multi-article episodes (one article per day, selected by the LLM as the single most relevant)
- Full-text audio of the paper itself (summary only)
- Publicly listed or shared RSS feeds (each feed URL is personal and opaque)
- Push notifications or mobile app integration
- Email delivery of the text summary (RSS + audio only for the user path)
