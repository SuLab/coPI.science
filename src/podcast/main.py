"""LabBot Podcast — daily personalized research briefings for each PI.

Usage:
    python -m src.podcast.main            # run once immediately
    python -m src.podcast.main scheduler  # long-running daily scheduler

The scheduler runs at 9am UTC daily (1 hour after GrantBot).
"""

import asyncio
import logging
from datetime import datetime, timezone

import typer

from src.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer(invoke_without_command=True)

RUN_HOUR_UTC = 9  # run at 9am UTC


async def run_podcast(dry_run: bool = False) -> list[str]:
    """Run the podcast pipeline for all active agents AND eligible plain users.

    Returns list of identifiers (agent_ids + "user:<uuid>") that produced episodes.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from src.database import get_session_factory
    from src.models.agent_registry import AgentRegistry
    from src.models.profile import ResearcherProfile
    from src.models.user import User
    from src.podcast.pipeline import run_pipeline_for_agent, run_podcast_for_user

    settings = get_settings()
    slack_tokens = settings.get_slack_tokens()

    session_factory = get_session_factory()
    produced: list[str] = []

    async with session_factory() as db:
        # ----------------------------------------------------------------
        # Agent path — pilot-lab agents with active AgentRegistry entries
        # ----------------------------------------------------------------
        result = await db.execute(
            select(AgentRegistry).where(AgentRegistry.status == "active")
        )
        agents = result.scalars().all()

        if not agents:
            logger.warning("No active agents found in registry — trying all known agents")
            for agent_id, tokens in slack_tokens.items():
                bot_token = tokens.get("bot", "")
                if not bot_token or bot_token.startswith("xoxb-placeholder"):
                    continue
                if dry_run:
                    logger.info("DRY RUN — would run pipeline for agent: %s", agent_id)
                    continue
                try:
                    ok = await run_pipeline_for_agent(
                        agent_id=agent_id,
                        bot_name=f"{agent_id.capitalize()}Bot",
                        pi_name=agent_id.capitalize(),
                        bot_token=bot_token,
                        slack_user_id=None,
                        db_session=db,
                    )
                    if ok:
                        produced.append(agent_id)
                except Exception as exc:
                    logger.error("Pipeline failed for agent %s: %s", agent_id, exc, exc_info=True)
        else:
            for agent in agents:
                agent_id = agent.agent_id
                tokens = slack_tokens.get(agent_id, {})
                bot_token = agent.slack_bot_token or tokens.get("bot", "")

                if dry_run:
                    logger.info(
                        "DRY RUN — would run pipeline for agent: %s (%s)", agent_id, agent.pi_name
                    )
                    continue

                try:
                    ok = await run_pipeline_for_agent(
                        agent_id=agent_id,
                        bot_name=agent.bot_name,
                        pi_name=agent.pi_name,
                        bot_token=bot_token,
                        slack_user_id=agent.slack_user_id,
                        db_session=db,
                    )
                    if ok:
                        produced.append(agent_id)
                except Exception as exc:
                    logger.error(
                        "Pipeline failed for agent %s: %s", agent_id, exc, exc_info=True
                    )

        await db.commit()

        # ----------------------------------------------------------------
        # User path — plain ORCID users without an active agent
        # ----------------------------------------------------------------
        # Collect user_ids of users who already have an active agent so we
        # can skip them (they're already covered by the agent path above).
        agent_user_ids_result = await db.execute(
            select(AgentRegistry.user_id).where(
                AgentRegistry.status == "active",
                AgentRegistry.user_id.is_not(None),
            )
        )
        agent_user_ids = {row[0] for row in agent_user_ids_result}

        # Fetch onboarded users who have a completed ResearcherProfile
        users_result = await db.execute(
            select(User)
            .options(selectinload(User.profile))
            .where(User.onboarding_complete.is_(True))
        )
        all_users = users_result.scalars().all()

        eligible_users = [
            u for u in all_users
            if u.id not in agent_user_ids
            and u.profile is not None
            and u.profile.research_summary
        ]

        if eligible_users:
            logger.info("Running user podcast pipeline for %d eligible users", len(eligible_users))
        else:
            logger.info("No eligible plain users for podcast pipeline")

        for user in eligible_users:
            if dry_run:
                logger.info("DRY RUN — would run pipeline for user: %s (%s)", user.id, user.name)
                continue
            try:
                ok = await run_podcast_for_user(user_id=user.id, db_session=db)
                if ok:
                    produced.append(f"user:{user.id}")
            except Exception as exc:
                logger.error(
                    "Pipeline failed for user %s: %s", user.id, exc, exc_info=True
                )

        await db.commit()

    logger.info("Podcast run complete: %d episodes produced", len(produced))
    return produced


@app.command()
def main(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without posting or generating audio"),
):
    """Run the podcast pipeline once for all active agents."""
    from src.podcast.state import mark_run_complete

    results = asyncio.run(run_podcast(dry_run=dry_run))
    if results:
        typer.echo(f"\nProduced {len(results)} episodes:")
        for aid in results:
            typer.echo(f"  {aid}")
    else:
        typer.echo("No episodes produced.")
    if not dry_run:
        mark_run_complete()


@app.command("scheduler")
def scheduler(
    run_hour: int = typer.Option(RUN_HOUR_UTC, "--run-hour", help="UTC hour to run daily (0-23)"),
    check_interval: int = typer.Option(900, "--check-interval", help="Seconds between schedule checks"),
):
    """Long-running scheduler: runs podcast pipeline once per calendar day.

    If the container starts after the scheduled hour, runs immediately to catch up.
    """
    asyncio.run(_scheduler_loop(run_hour, check_interval))


async def _scheduler_loop(run_hour: int, check_interval: int) -> None:
    """Single long-lived event loop for the daily scheduler.

    Using a single asyncio.run() call keeps the SQLAlchemy asyncpg connection
    pool bound to one event loop for the lifetime of the process.  Calling
    asyncio.run() in a while-loop would create a fresh event loop each
    iteration, leaving the cached engine attached to the closed previous loop
    and causing "Future attached to a different loop" errors on the second run.
    """
    from src.podcast.state import mark_run_complete, should_run_today

    logger.info(
        "Podcast scheduler started (run_hour=%d UTC, check every %ds)", run_hour, check_interval
    )

    while True:
        now = datetime.now(timezone.utc)
        if should_run_today() and now.hour >= run_hour:
            logger.info("Running daily podcast pipeline...")
            try:
                results = await run_podcast()
                mark_run_complete()
                logger.info("Daily run complete: %d episodes", len(results))
            except Exception as exc:
                logger.error("Daily run failed: %s", exc, exc_info=True)
        else:
            logger.debug("No run needed (last run: %s, hour: %d)", "?", now.hour)

        await asyncio.sleep(check_interval)


if __name__ == "__main__":
    app()
