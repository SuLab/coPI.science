"""Podcast RSS feed and audio serving endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_db
from src.models.agent_registry import AgentRegistry
from src.models.podcast import PodcastEpisode
from src.podcast.rss import build_feed

logger = logging.getLogger(__name__)
router = APIRouter()

AUDIO_DIR = Path("data/podcast_audio")


@router.get("/{agent_id}/feed.xml", response_class=Response)
async def podcast_feed(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """RSS 2.0 podcast feed for a PI's daily research briefings."""
    # Verify agent exists
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Fetch episodes newest-first
    episodes_result = await db.execute(
        select(PodcastEpisode)
        .where(PodcastEpisode.agent_id == agent_id)
        .order_by(PodcastEpisode.episode_date.desc())
        .limit(30)
    )
    episodes = episodes_result.scalars().all()

    settings = get_settings()
    base_url = settings.podcast_base_url or settings.base_url

    xml = build_feed(
        agent_id=agent_id,
        pi_name=agent.pi_name,
        episodes=episodes,
        base_url=base_url,
    )

    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/{agent_id}/audio/{date}.mp3")
async def podcast_audio(agent_id: str, date: str):
    """Stream a podcast audio file."""
    # Basic validation to prevent path traversal
    if "/" in date or ".." in date or not date.replace("-", "").isdigit():
        raise HTTPException(status_code=400, detail="Invalid date format")

    audio_path = AUDIO_DIR / agent_id / f"{date}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        filename=f"{agent_id}-{date}.mp3",
    )
