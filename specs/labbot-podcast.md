# LabBot Podcast Specification

## Overview

LabBot Podcast is a daily personalized research briefing service for each PI. It surfaces the single most relevant and impactful recent publication from the scientific literature based on the PI's profile, generates a structured text summary highlighting findings and tools useful to the PI's ongoing work, and produces a short audio episode via ElevenLabs TTS. PIs receive the text summary via Slack DM from their lab bot and can subscribe to a per-PI RSS podcast feed to listen to the audio.

The system runs once per day (alongside GrantBot) and requires no PI interaction to be useful — but PIs can tune it through the same standing-instruction DM mechanism used by the agent system.

---

## Architecture

### Service Placement

LabBot Podcast runs as a separate Docker container (`podcast` service), mirroring the GrantBot pattern:
- Long-running scheduler process
- Executes once per calendar day at 9am UTC (1 hour after GrantBot)
- If the container was down at the scheduled time, runs immediately on startup (catch-up)
- State persisted in `data/podcast_state.json` (tracks which articles have been delivered per agent)

### Dependencies on Existing Systems

| Existing component | How Podcast uses it |
|---|---|
| `ResearcherProfile` DB model | Source of PI research areas, keywords, techniques, disease areas |
| `profiles/public/{lab}.md` | Supplementary profile text for LLM article selection and summary |
| `src/services/pubmed.py` | Literature search (keyword + MeSH queries) |
| `src/services/llm.py` | Article selection ranking and summary generation (all calls logged to `LlmCallLog`) |
| `AgentRegistry` | Maps agent → PI → Slack bot token for DM delivery |
| Slack bot DM | Text summary delivery to PI |

### New External Dependency

**ElevenLabs API** — text-to-speech generation.
- Configured via `ELEVENLABS_API_KEY` environment variable
- Voice selection per agent configured in `data/podcast_voices.json` (agent_id → voice_id); falls back to a default voice if not set
- Audio files stored at `data/podcast_audio/{agent_id}/{YYYY-MM-DD}.mp3`

---

## Daily Pipeline

Each day, for each active agent in `AgentRegistry`, the pipeline executes the following steps sequentially:

### Step 1: Build Search Queries

Construct PubMed search terms from the PI's `ResearcherProfile`:
- Extract top research area keywords
- Extract technique and experimental model terms
- Combine into 2–3 PubMed query strings (e.g., `(proteostasis OR unfolded protein response) AND (neurodegeneration OR proteomics)`)
- Limit to publications from the last 14 days (rolling window ensures coverage across weekend/holiday gaps)
- Cap at 50 candidate abstracts per agent

### Step 2: Fetch Candidate Abstracts

Use `src/services/pubmed.py` to execute each query and retrieve PMIDs + abstracts. Deduplicate across queries. Skip any PMID already in `podcast_state.json` for this agent (prevents re-delivering the same article).

### Step 3: LLM Article Selection (Sonnet)

Single LLM call (Sonnet) with:
- The PI's full public profile (from `profiles/public/{lab}.md`)
- The list of candidate abstracts (title + abstract text, numbered)
- Prompt: `prompts/podcast-select.md`

The LLM returns the index of the single best article, along with a one-sentence justification of why it is relevant to this PI's ongoing work. If no article meets a minimum relevance threshold (as instructed in the prompt), it returns `null` and the pipeline skips delivery for that agent today.

### Step 4: Generate Text Summary (Opus)

One LLM call (Opus) with:
- The PI's full public profile
- The selected article's title, abstract, and full text (fetched via `retrieve_full_text` if available in PMC, otherwise abstract only)
- Prompt: `prompts/podcast-summarize.md`

Output is a structured text summary (see format below). This is the content delivered to the PI via Slack and used as the TTS input.

### Step 5: Generate Audio (ElevenLabs)

Pass the text summary to the ElevenLabs TTS API:
- Voice: agent-specific or default
- Model: `eleven_multilingual_v2` (configurable via `ELEVENLABS_MODEL`)
- Output: MP3 file saved to `data/podcast_audio/{agent_id}/{YYYY-MM-DD}.mp3`
- If ElevenLabs call fails, continue — Slack text delivery still proceeds

### Step 6: Serve Audio via RSS

The podcast RSS feed for each agent is served by the FastAPI web app. New episodes are registered in `data/podcast_state.json` with the audio file path, episode title, pub date, and duration (parsed from the MP3 file using `mutagen`).

### Step 7: Deliver via Slack DM

Send the text summary as a DM from the agent's Slack bot to its PI, using the same `AgentRegistry.slack_bot_token` used by the agent simulation. Format described below.

### Step 8: Update State

Append the delivered PMID and episode metadata to `data/podcast_state.json` for this agent. This prevents re-delivery and powers the RSS feed.

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

### Endpoint

`GET /podcast/{agent_id}/feed.xml`

Served by FastAPI from `src/routers/podcast.py`. No authentication required — the URL is obscure-by-default (agent_id is a UUID), not secret.

### Feed Structure

Standard RSS 2.0 with iTunes podcast extensions:

```xml
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{PI Name} — LabBot Research Briefings</title>
    <description>Daily personalized research summaries for {PI Name} at Scripps Research</description>
    <link>{base_url}/podcast/{agent_id}/feed.xml</link>
    <itunes:author>{PI Name}</itunes:author>
    <itunes:category text="Science"/>
    <item>
      <title>{Paper Title} — {Date}</title>
      <description>{text summary}</description>
      <enclosure url="{audio_url}" type="audio/mpeg" length="{file_size}"/>
      <pubDate>{RFC 822 date}</pubDate>
      <guid>{agent_id}-{YYYY-MM-DD}</guid>
      <itunes:duration>{duration}</itunes:duration>
    </item>
    ...
  </channel>
</rss>
```

### Audio File Serving

`GET /podcast/{agent_id}/audio/{date}.mp3`

Served directly by FastAPI from `data/podcast_audio/{agent_id}/`. Files are read from disk and streamed with `Content-Type: audio/mpeg`.

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

### New Table: `PodcastEpisode`

```python
class PodcastEpisode(Base):
    __tablename__ = "podcast_episodes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    episode_date: Mapped[date] = mapped_column(Date, nullable=False)
    pmid: Mapped[str] = mapped_column(String, nullable=False)
    paper_title: Mapped[str] = mapped_column(String, nullable=False)
    paper_authors: Mapped[str] = mapped_column(String, nullable=False)
    paper_journal: Mapped[str] = mapped_column(String, nullable=False)
    paper_year: Mapped[int] = mapped_column(Integer, nullable=False)
    text_summary: Mapped[str] = mapped_column(Text, nullable=False)
    audio_file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # null if TTS failed
    audio_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slack_delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    selection_justification: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("agent_id", "episode_date", name="uq_podcast_agent_date"),
    )
```

The `data/podcast_state.json` file serves as a lightweight startup cache (to avoid a DB query to get delivered PMIDs during query construction), but the DB is the authoritative record for RSS feed generation and admin visibility.

### Alembic Migration

Add migration `0005_add_podcast_episodes.py` creating the `podcast_episodes` table.

---

## Configuration

New environment variables:

| Variable | Required | Description |
|---|---|---|
| `ELEVENLABS_API_KEY` | Yes (for audio) | ElevenLabs API key |
| `ELEVENLABS_MODEL` | No | TTS model ID (default: `eleven_multilingual_v2`) |
| `ELEVENLABS_DEFAULT_VOICE_ID` | No | Default voice when no per-agent override exists |
| `PODCAST_BASE_URL` | Yes | Public base URL for RSS enclosure links (e.g., `https://copi.science`) |
| `PODCAST_SEARCH_WINDOW_DAYS` | No | Rolling search window in days (default: `14`) |
| `PODCAST_MAX_CANDIDATES` | No | Max PubMed abstracts per agent per day (default: `50`) |

Per-agent voice overrides: `data/podcast_voices.json`
```json
{
  "su": "voice_id_abc123",
  "wiseman": "voice_id_def456"
}
```

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
├── elevenlabs.py    # ElevenLabs TTS client wrapper
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

PIs can adjust podcast behavior through standing instructions to their lab bot (same DM mechanism as the agent system — see `pi-interaction.md`). The podcast pipeline reads the private profile when building the selection prompt.

Examples of effective standing instructions:
- "For my daily podcast, focus only on papers that release a new tool or dataset — I don't need summaries of pure wet-lab findings"
- "Prioritize papers from computational biology journals for the podcast"
- "Skip anything about C. elegans — we're not pursuing that direction anymore"

The bot's private profile rewrite (via `prompts/pi-profile-rewrite.md`) should include a `## Podcast Preferences` section that the podcast pipeline reads when constructing the selection and summarization prompts.

---

## Rollout Phases

### Phase 1: Text-only delivery
- PubMed search, LLM selection, Opus summarization
- Slack DM delivery
- `PodcastEpisode` DB table and admin visibility
- No audio, no RSS

### Phase 2: Audio + RSS
- ElevenLabs TTS integration
- Audio file storage and streaming endpoint
- RSS feed generation and `/podcast/{agent_id}/feed.xml` endpoint
- Per-agent voice configuration

### Phase 3: PI customization surface
- Podcast preferences section in private profile
- Pipeline reads preferences when building prompts
- Admin dashboard podcast tab with LLM usage metrics

---

## Out of Scope

- Real-time or on-demand article requests (this is a daily scheduled briefing only)
- Multi-article episodes (one article per day, selected by the LLM as the single most relevant)
- Full-text audio of the paper itself (summary only)
- Public or shared RSS feeds (each feed is per-PI, addressed by UUID)
- Push notifications or mobile app integration
- Preprint servers (bioRxiv, medRxiv) — PubMed only for Phase 1; preprints are a Phase 2+ addition
