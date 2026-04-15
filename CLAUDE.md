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

**Note:** The agent-run container uses mounted source code but the Python process only loads modules at startup. Code changes require a container restart to take effect.

## Podcast Pipeline

The LabBot Podcast pipeline (specs/labbot-podcast.md) runs daily at 9am UTC for each active agent:

1. Build PubMed queries from lab's public profile
2. Fetch candidates from PubMed + bioRxiv + medRxiv + arXiv (last 14 days, up to 50+10 candidates)
3. Claude Sonnet selects most relevant paper (applying PI's podcast preferences from their private ProfileRevision)
4. Claude Opus writes a ~250-word structured brief
5. TTS audio generated (Mistral or local vLLM-Omni); ffmpeg loudnorm applied if PODCAST_NORMALIZE_AUDIO=true
6. Slack DM sent to PI with text summary + RSS link
7. RSS feed available at `/podcast/{agent_id}/feed.xml`
8. Audio served at `/podcast/{agent_id}/audio/{date}.mp3`

Preprint IDs use prefixed format: `biorxiv:...`, `medrxiv:...`, `arxiv:...`. The `paper_url` in summaries links to the correct server (not always PubMed).

```bash
# Run podcast pipeline once for all active agents
docker compose --profile podcast run --rm podcast python -m src.podcast.main

# Test pipeline for 'su' agent only
docker compose exec app python scripts/test_podcast_su.py
```

## Database Migration Caveat

If the DB was initialized from the `main` branch schema and then this branch is checked out, `alembic upgrade head` will stamp the version without re-running migrations that share a revision ID with ones already applied on `main`. Any columns added by branch-specific migrations may be silently missing.

**Symptom:** `UndefinedColumnError` at runtime despite `alembic current` showing `head`.

**Fix:** Check for missing columns and apply them manually:
```bash
docker compose exec app python -c "
import asyncio
from src.database import get_engine
from sqlalchemy import text

async def check():
    eng = get_engine()
    async with eng.connect() as conn:
        result = await conn.execute(text(\"SELECT column_name FROM information_schema.columns WHERE table_name='researcher_profiles' ORDER BY ordinal_position\"))
        print([r[0] for r in result])

asyncio.run(check())
"
```
Then add any missing columns with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...`.
