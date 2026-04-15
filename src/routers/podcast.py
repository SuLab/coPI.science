"""Podcast RSS feed, audio serving, settings, and on-demand generation endpoints.

Two delivery paths:
  Agent path  — pilot-lab agents with an approved AgentRegistry entry.
                URLs are keyed by agent_id string.
                Endpoints: /podcast/{agent_id}/...

  User path   — any user who has completed ORCID onboarding and has a
                ResearcherProfile with a research_summary.
                URLs are keyed by user_id UUID (opaque, stable, subscribable).
                Endpoints: /podcast/users/{user_id}/...  (public RSS + audio)
                           /podcast/settings             (auth-gated settings UI)
                           /podcast/user/generate        (auth-gated on-demand trigger)
"""

import asyncio
import logging
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_db, get_session_factory
from src.dependencies import get_current_user
from src.models.agent_registry import AgentRegistry
from src.models.podcast import PodcastEpisode
from src.models.user import User
from src.podcast.rss import build_feed

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

AUDIO_DIR = Path("data/podcast_audio")

MISTRAL_VOICES = [
    ("alex", "Alex — US English, male, calm"),
    ("deedee", "DeeDee — US English, female, upbeat"),
    ("jessica", "Jessica — US English, female, expressive"),
    ("luna", "Luna — US English, female, soft"),
    ("rio", "Rio — US English, male, energetic"),
    ("stella", "Stella — US English, female, professional"),
    ("theo", "Theo — US English, male, measured"),
    ("tyler", "Tyler — US English, male, conversational"),
]


# ---------------------------------------------------------------------------
# Agent path — existing endpoints (unchanged behaviour)
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/feed.xml", response_class=Response)
async def podcast_feed(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """RSS 2.0 podcast feed for a pilot-lab agent's daily research briefings."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

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
        pi_name=agent.pi_name,
        episodes=episodes,
        base_url=base_url,
        agent_id=agent_id,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/{agent_id}/audio/{date}.mp3")
async def podcast_audio(agent_id: str, date: str):
    """Stream a podcast audio file for an agent."""
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


async def _run_pipeline_background(
    agent_id: str, bot_name: str, pi_name: str, bot_token: str, slack_user_id: str | None
) -> None:
    """Run the agent podcast pipeline in a background task with its own DB session."""
    from src.podcast.pipeline import run_pipeline_for_agent

    session_factory = get_session_factory()
    try:
        async with session_factory() as db:
            ok = await run_pipeline_for_agent(
                agent_id=agent_id,
                bot_name=bot_name,
                pi_name=pi_name,
                bot_token=bot_token,
                slack_user_id=slack_user_id,
                db_session=db,
            )
            await db.commit()
            logger.info("On-demand podcast pipeline for %s: %s", agent_id, "produced" if ok else "no episode")
    except Exception as exc:
        logger.error("On-demand podcast pipeline failed for %s: %s", agent_id, exc, exc_info=True)


@router.api_route("/{agent_id}/generate", methods=["GET", "POST"])
async def podcast_generate(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Trigger on-demand podcast generation for an agent (returns immediately)."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    settings = get_settings()
    slack_tokens = settings.get_slack_tokens()
    bot_token = agent.slack_bot_token or slack_tokens.get(agent_id, {}).get("bot", "")

    asyncio.create_task(
        _run_pipeline_background(
            agent_id=agent_id,
            bot_name=agent.bot_name,
            pi_name=agent.pi_name,
            bot_token=bot_token,
            slack_user_id=agent.slack_user_id,
        )
    )
    return {
        "status": "started",
        "agent_id": agent_id,
        "message": f"Podcast pipeline started for {agent.pi_name}. Check the RSS feed shortly.",
    }


# ---------------------------------------------------------------------------
# User path — plain ORCID users (no agent required)
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/feed.xml", response_class=Response)
async def podcast_feed_for_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Public RSS 2.0 feed for a plain ORCID user's daily research briefings.

    The user_id in the URL is the UUID primary key of the User record, which
    acts as an opaque, stable, subscribable token — no authentication required.
    """
    try:
        uid = _uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    user_result = await db.execute(select(User).where(User.id == uid))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    episodes_result = await db.execute(
        select(PodcastEpisode)
        .where(PodcastEpisode.user_id == uid)
        .order_by(PodcastEpisode.episode_date.desc())
        .limit(30)
    )
    episodes = episodes_result.scalars().all()

    settings = get_settings()
    base_url = settings.podcast_base_url or settings.base_url

    xml = build_feed(
        pi_name=user.name,
        episodes=episodes,
        base_url=base_url,
        user_id=user_id,
    )
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@router.get("/users/{user_id}/audio/{date}.mp3")
async def podcast_audio_for_user(user_id: str, date: str):
    """Stream a podcast audio file for a plain ORCID user."""
    if "/" in date or ".." in date or not date.replace("-", "").isdigit():
        raise HTTPException(status_code=400, detail="Invalid date format")
    try:
        _uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    audio_path = AUDIO_DIR / "users" / user_id / f"{date}.mp3"
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        filename=f"briefing-{date}.mp3",
    )


def _podcast_eligible(user: User) -> bool:
    """Return True if a plain user is eligible for the podcast feature."""
    return (
        user.onboarding_complete
        and getattr(user, "profile", None) is not None
        and bool(getattr(user.profile, "research_summary", None))
    )


@router.get("/settings", response_class=HTMLResponse)
async def get_podcast_settings_user(
    request: Request,
    saved: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Podcast settings page for a plain ORCID user (no agent required)."""
    from sqlalchemy.orm import selectinload

    from src.models.podcast_preferences import PodcastPreferences

    # Eagerly load profile relationship
    user_result = await db.execute(
        select(User)
        .options(selectinload(User.profile))
        .where(User.id == current_user.id)
    )
    user = user_result.scalar_one_or_none() or current_user

    if not _podcast_eligible(user):
        return RedirectResponse(url="/profile?podcast_incomplete=1", status_code=302)

    prefs_result = await db.execute(
        select(PodcastPreferences).where(PodcastPreferences.user_id == current_user.id)
    )
    prefs = prefs_result.scalar_one_or_none()

    settings = get_settings()
    base_url = settings.podcast_base_url or settings.base_url
    feed_url = f"{base_url}/podcast/users/{current_user.id}/feed.xml"

    return templates.TemplateResponse(
        request,
        "podcast_settings.html",
        {
            "request": request,
            "current_user": current_user,
            "active_page": "podcast",
            "prefs": prefs,
            "voices": MISTRAL_VOICES,
            "saved": saved,
            "feed_url": feed_url,
        },
    )


@router.post("/settings")
async def save_podcast_settings_user(
    request: Request,
    voice_id: str = Form(""),
    extra_keywords_raw: str = Form(""),
    preferred_journals_raw: str = Form(""),
    deprioritized_journals_raw: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save podcast preferences for a plain ORCID user."""
    from sqlalchemy.orm import selectinload

    from src.models.podcast_preferences import PodcastPreferences

    user_result = await db.execute(
        select(User)
        .options(selectinload(User.profile))
        .where(User.id == current_user.id)
    )
    user = user_result.scalar_one_or_none() or current_user

    if not _podcast_eligible(user):
        raise HTTPException(status_code=403, detail="Complete your profile before setting podcast preferences.")

    def _parse_keywords(raw: str) -> list[str]:
        return [v for line in raw.splitlines() if (v := line.strip())][:20]

    def _parse_journals(raw: str) -> list[str]:
        return [v for part in raw.replace(",", "\n").splitlines() if (v := part.strip())][:20]

    prefs_result = await db.execute(
        select(PodcastPreferences).where(PodcastPreferences.user_id == current_user.id)
    )
    prefs = prefs_result.scalar_one_or_none()

    if prefs is None:
        prefs = PodcastPreferences(user_id=current_user.id, agent_id=None)
        db.add(prefs)

    prefs.voice_id = voice_id.strip() or None
    prefs.extra_keywords = _parse_keywords(extra_keywords_raw)
    prefs.preferred_journals = _parse_journals(preferred_journals_raw)
    prefs.deprioritized_journals = _parse_journals(deprioritized_journals_raw)
    await db.commit()

    logger.info("Podcast preferences saved for user %s", current_user.id)
    return RedirectResponse(url="/podcast/settings?saved=1", status_code=302)


async def _run_user_pipeline_background(user_id) -> None:
    """Run the user podcast pipeline in a background task with its own DB session."""
    from src.podcast.pipeline import run_podcast_for_user

    session_factory = get_session_factory()
    try:
        async with session_factory() as db:
            ok = await run_podcast_for_user(user_id=user_id, db_session=db)
            await db.commit()
            logger.info("On-demand podcast pipeline for user %s: %s", user_id, "produced" if ok else "no episode")
    except Exception as exc:
        logger.error("On-demand podcast pipeline failed for user %s: %s", user_id, exc, exc_info=True)


@router.post("/user/generate")
async def podcast_generate_for_user(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger on-demand podcast generation for the current user (returns immediately)."""
    from sqlalchemy.orm import selectinload

    user_result = await db.execute(
        select(User)
        .options(selectinload(User.profile))
        .where(User.id == current_user.id)
    )
    user = user_result.scalar_one_or_none() or current_user

    if not _podcast_eligible(user):
        raise HTTPException(status_code=403, detail="Complete your profile before generating a podcast.")

    asyncio.create_task(_run_user_pipeline_background(current_user.id))

    return {
        "status": "started",
        "user_id": str(current_user.id),
        "message": "Podcast pipeline started. Check your feed URL shortly.",
    }
