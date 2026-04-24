"""GrantBot — searches for funding opportunities and posts to Slack.

Usage:
    python -m src.agent.grantbot [--dry-run] [--channel funding-opportunities]

GrantBot is independent of the simulation engine. It:
1. Loads all researcher profiles to extract search keywords
2. Searches Grants.gov for open opportunities matching those keywords
3. Uses an LLM to score relevance and draft Slack posts
4. Posts to a Slack channel, tagging relevant researchers
5. Tracks posted opportunities to avoid duplicates
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import GrantbotPostedFoa
from src.services.grants import fetch_opportunity_detail, list_posted_opportunities

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles/public")

app = typer.Typer(invoke_without_command=True)


def _load_researcher_profiles() -> dict[str, dict[str, Any]]:
    """Load all public profiles and extract searchable fields.

    Returns {agent_id: {name, keywords, disease_areas, techniques, ...}}
    """
    profiles = {}
    for md_file in sorted(PROFILES_DIR.glob("*.md")):
        agent_id = md_file.stem
        text = md_file.read_text(encoding="utf-8")

        profile: dict[str, Any] = {"agent_id": agent_id, "raw": text}

        # Extract PI name from first heading
        for line in text.splitlines():
            if line.startswith("# ") and "Lab" in line:
                profile["name"] = line.replace("# ", "").replace(" Lab — Public Profile", "").strip()
                break

        # Extract keywords section
        profile["keywords"] = _extract_list_section(text, "Keywords")
        profile["disease_areas"] = _extract_list_section(text, "Disease Areas")
        profile["techniques"] = _extract_list_section(text, "Key Methods and Technologies")
        profile["targets"] = _extract_list_section(text, "Key Molecular Targets")

        profiles[agent_id] = profile

    logger.info("Loaded %d researcher profiles", len(profiles))
    return profiles


def _extract_list_section(text: str, section_name: str) -> list[str]:
    """Extract bullet-pointed or comma-separated items from a markdown section."""
    items = []
    in_section = False
    for line in text.splitlines():
        if section_name.lower() in line.lower() and line.startswith("##"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            line = line.strip()
            if line.startswith("- "):
                items.append(line[2:].strip())
            elif line and not line.startswith("#"):
                # Comma-separated keywords
                items.extend(kw.strip() for kw in line.split(",") if kw.strip())
    return items


def _build_search_queries(profiles: dict[str, dict]) -> list[str]:
    """Build a deduplicated set of search queries from all profiles.

    Prioritizes disease areas (best match for grant language), then
    high-level keywords. Avoids overly specific technique names that
    won't match FOA descriptions.
    """
    # Priority 1: Disease areas (most grant-relevant)
    priority_queries: list[str] = []
    seen: set[str] = set()

    for profile in profiles.values():
        for da in profile.get("disease_areas", []):
            simplified = da.split("(")[0].strip()
            if len(simplified.split()) <= 5 and simplified.lower() not in seen:
                seen.add(simplified.lower())
                priority_queries.append(simplified.lower())

    # Priority 2: Keywords (broader research themes)
    keyword_queries: list[str] = []
    for profile in profiles.values():
        for kw in profile.get("keywords", []):
            if len(kw.split()) <= 4 and kw.lower() not in seen:
                seen.add(kw.lower())
                keyword_queries.append(kw.lower())

    # Interleave: disease areas first, then keywords
    queries = priority_queries + keyword_queries
    logger.info(
        "Built %d search queries (%d disease areas, %d keywords)",
        len(queries), len(priority_queries), len(keyword_queries),
    )
    return queries


async def _load_posted_numbers(session: AsyncSession) -> set[str]:
    """Return the set of already-posted FOA numbers from Postgres."""
    result = await session.execute(select(GrantbotPostedFoa.foa_number))
    return set(result.scalars().all())


async def _claim_foa(
    session: AsyncSession,
    foa_number: str,
    channel: str | None,
    title: str | None,
) -> bool:
    """Attempt to reserve an FOA for posting.

    Uses INSERT ... ON CONFLICT DO NOTHING so that concurrent GrantBot
    instances cannot both claim and post the same FOA — whoever's insert
    lands first wins. Commits so other instances see the claim immediately.

    Returns True if this caller owns the claim (proceed to post), else False.
    """
    stmt = (
        pg_insert(GrantbotPostedFoa)
        .values(foa_number=foa_number, channel=channel, title=title)
        .on_conflict_do_nothing(index_elements=["foa_number"])
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount == 1


async def _release_foa(session: AsyncSession, foa_number: str) -> None:
    """Undo a claim when the Slack post itself failed, so a later run can retry."""
    await session.execute(
        delete(GrantbotPostedFoa).where(GrantbotPostedFoa.foa_number == foa_number)
    )
    await session.commit()


async def _select_opportunities(
    opportunities: dict[str, dict],
    max_select: int = 30,
) -> list[str]:
    """Use LLM to select funding opportunities relevant to biomedical research at Scripps.

    Returns a list of FOA numbers. No numeric scoring — just include/exclude.
    """
    from src.services.llm import generate_agent_response

    opp_lines = []
    for num, opp in opportunities.items():
        title = opp.get("title", "")
        agency = opp.get("agency", "")
        close_date = opp.get("close_date", "")
        opp_lines.append(f"- {num} | {agency} | {title} | Closes: {close_date}")
    opp_list = "\n".join(opp_lines)

    system_prompt = f"""You are GrantBot, selecting funding opportunities to share with researchers at Scripps Research, a biomedical research institute.

Below is a list of {len(opportunities)} open funding opportunities (title and agency only).

Select up to {max_select} opportunities that are relevant to biomedical research at a place like Scripps Research. Scripps Research focuses on basic and translational biomedical science including: drug discovery, structural biology, chemical biology, immunology, virology, neuroscience, aging, genomics, proteomics, computational biology, and related fields.

INCLUDE:
- NIH research grants (R01, R21, R33, U01, P01, U54, etc.) in biomedical areas
- NSF grants at the biology/chemistry/computation interface
- Multi-PI or collaborative mechanisms
- Grants for methods development, tool building, or infrastructure relevant to biomedical research

EXCLUDE:
- Training grants (T32, F31, F32, K awards) unless unusually relevant
- Clinical trials, health services research, or public health implementation
- Administrative supplements, conference grants, or planning grants
- Opportunities clearly outside biomedical research (agriculture, education, policy, etc.)
- Opportunities with past close dates

Respond with ONLY a JSON array of FOA numbers:
["FOA-NUMBER-1", "FOA-NUMBER-2", ...]"""

    user_msg = f"""## Funding Opportunities\n\n{opp_list}"""

    try:
        settings = get_settings()
        response = await generate_agent_response(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            model=settings.llm_agent_model_sonnet,
            max_tokens=1500,
            log_meta={"agent_id": "grantbot", "phase": "select"},
        )

        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if "```" in cleaned:
                cleaned = cleaned[:cleaned.index("```")]
            cleaned = cleaned.strip()
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]

        selected = json.loads(cleaned)
        logger.info("Selected %d of %d opportunities", len(selected), len(opportunities))
        return selected[:max_select]
    except Exception as exc:
        logger.warning("Selection failed: %s — falling back to all", exc)
        return list(opportunities.keys())[:max_select]


async def _draft_post(
    opportunity: dict[str, Any],
) -> dict[str, Any] | None:
    """Draft a Slack post for a funding opportunity.

    Returns {channel, post_text} or None if drafting fails.
    Lab-specific relevance is left to the lab agents — GrantBot just summarizes the FOA.
    """
    from src.services.llm import generate_agent_response

    opp_text = f"""Title: {opportunity.get('title', '')}
Number: {opportunity.get('number', '')}
Agency: {opportunity.get('agency', '')}
Close Date: {opportunity.get('close_date', 'Not specified')}
Description: {opportunity.get('description', '')[:2000]}
Synopsis: {opportunity.get('synopsis', '')[:2000]}"""

    system_prompt = """You are GrantBot, posting funding opportunities for researchers at Scripps Research.

Draft a concise Slack post summarizing this funding opportunity. The post should help researchers quickly decide if this FOA is worth reading in detail.

RULES:
- Summarize the scientific scope and goals in 2-3 sentences
- Note the mechanism type (R01, U01, etc.), budget range if available, and key eligibility details
- Do NOT tag specific researchers or labs — lab agents will decide relevance themselves
- Use Slack mrkdwn formatting: *bold* (single asterisks), _italic_ (underscores). Do NOT use **double asterisks** or emoji.

Choose the best Slack channel for the post:
- "drug-repurposing" — drug repurposing, therapeutic development, pharmacology
- "structural-biology" — structural methods, cryo-EM, crystallography, molecular visualization
- "aging-and-longevity" — aging, longevity, neurodegeneration, age-related disease
- "single-cell-omics" — single-cell sequencing, transcriptomics, genomics, multiomics
- "chemical-biology" — chemical probes, proteomics, covalent ligands, ABPP
- "funding-opportunities" — broad/cross-cutting opportunities that don't fit a specific topic

Respond in JSON format:
{
  "channel": "funding-opportunities",
  "post_text": "the Slack post text"
}"""

    user_msg = opp_text

    try:
        settings = get_settings()
        response = await generate_agent_response(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            model=settings.llm_agent_model_sonnet,
            max_tokens=500,
            log_meta={"agent_id": "grantbot", "phase": "draft"},
        )

        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if "```" in cleaned:
                cleaned = cleaned[:cleaned.index("```")]
            cleaned = cleaned.strip()
        start = cleaned.find("{")
        if start >= 0:
            depth = 0
            for i, ch in enumerate(cleaned[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        cleaned = cleaned[start:i + 1]
                        break

        return json.loads(cleaned)
    except Exception as exc:
        logger.warning("Draft failed for %s: %s", opportunity.get("number"), exc)
        return None


def _ensure_channel_membership(slack_client, channel_names: set[str]) -> None:
    """Join any public channels the bot isn't already a member of."""
    try:
        # Build a map of channel name -> id for all public channels
        channel_map: dict[str, str] = {}
        cursor = None
        while True:
            resp = slack_client.conversations_list(
                types="public_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for ch in resp.get("channels", []):
                channel_map[ch["name"]] = ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        for name in channel_names:
            clean_name = name.lstrip("#")
            ch_id = channel_map.get(clean_name)
            if not ch_id:
                logger.warning("Channel #%s not found in workspace", clean_name)
                continue
            try:
                slack_client.conversations_join(channel=ch_id)
                logger.info("Joined #%s", clean_name)
            except Exception as exc:
                logger.warning("Could not join #%s: %s", clean_name, exc)
    except Exception as exc:
        logger.warning("Failed to list channels for auto-join: %s", exc)


async def run_grantbot(
    channel: str = "funding-opportunities",
    dry_run: bool = False,
    max_posts: int = 10,
    max_per_channel: int = 1,
) -> list[dict]:
    """Main GrantBot pipeline.

    Returns list of posted opportunities.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with session_factory() as session:
            return await _run_grantbot_with_session(
                session,
                channel=channel,
                dry_run=dry_run,
                max_posts=max_posts,
                max_per_channel=max_per_channel,
            )
    finally:
        await engine.dispose()


async def _run_grantbot_with_session(
    session: AsyncSession,
    channel: str,
    dry_run: bool,
    max_posts: int,
    max_per_channel: int,
) -> list[dict]:
    settings = get_settings()

    # 1. Load already-posted set (cheap pre-filter to save LLM cost).
    #    Final duplicate check is the DB claim below, which is race-safe.
    posted = await _load_posted_numbers(session)
    logger.info("Already posted %d opportunities", len(posted))

    # 2. Fetch all posted NIH/NSF opportunities from Grants.gov
    raw_opps = await list_posted_opportunities()
    all_opps: dict[str, dict] = {}
    for opp in raw_opps:
        num = opp.get("number", "")
        if num and num not in posted:
            all_opps[num] = opp

    logger.info("Found %d new opportunities (after filtering posted)", len(all_opps))

    if not all_opps:
        logger.info("No new opportunities to process")
        return []

    # 3. Select: LLM reviews titles to pick broadly relevant biomedical opportunities
    selected_nums = await _select_opportunities(all_opps)
    selected_opps = {num: all_opps[num] for num in selected_nums if num in all_opps}
    logger.info("Selected %d opportunities for posting", len(selected_opps))

    # 4. Fetch details for selected opportunities
    detailed_opps = []
    for num, opp in selected_opps.items():
        if opp.get("id"):
            try:
                detail = await fetch_opportunity_detail(str(opp["id"]))
                if detail:
                    detailed_opps.append(detail)
                    continue
            except Exception as exc:
                logger.debug("Detail fetch failed for %s: %s", num, exc)
        detailed_opps.append(opp)

    # 4b. Cache FOA details locally for agent access
    from src.agent.foa_cache import cache_foa
    for opp in detailed_opps:
        opp_num = opp.get("number", "")
        if opp_num:
            cache_foa(opp_num, opp)

    # 5. Draft posts using LLM
    drafted: list[dict] = []
    for opp in detailed_opps:
        result = await _draft_post(opp)
        if result:
            result["opportunity"] = opp
            drafted.append(result)

    # Pick posts respecting per-channel limit
    channel_counts: dict[str, int] = {}
    to_post: list[dict] = []
    for item in drafted:
        ch = item.get("channel", channel)
        if channel_counts.get(ch, 0) >= max_per_channel:
            continue
        channel_counts[ch] = channel_counts.get(ch, 0) + 1
        to_post.append(item)
        if len(to_post) >= max_posts:
            break

    logger.info("Drafted %d posts, posting %d (max %d per channel)", len(drafted), len(to_post), max_per_channel)

    # 6. Post to Slack (or dry-run)
    posted_list: list[dict] = []
    slack_client = None

    if not dry_run:
        from slack_sdk import WebClient
        bot_token = getattr(settings, "slack_bot_token_grantbot", "")
        if not bot_token or bot_token.startswith("xoxb-placeholder"):
            bot_token = settings.slack_bot_token_su
            logger.info("No grantbot Slack token — using SuBot's token as fallback")
        if bot_token and not bot_token.startswith("xoxb-placeholder"):
            slack_client = WebClient(token=bot_token)
            _ensure_channel_membership(slack_client, {item.get("channel", channel) for item in to_post})

    for item in to_post:
        opp = item["opportunity"]
        opp_num = opp.get("number", "unknown")
        post_text = item.get("post_text", "")
        target_channel = item.get("channel", "funding-opportunities")
        title = opp.get("title")

        # Build the full post
        close_date = opp.get("close_date", "Not specified")
        grants_url = f"https://www.grants.gov/search-results-detail/{opp.get('id', '')}"
        header = f":moneybag: *Funding Opportunity*\n*{opp.get('title', '')}*\n{opp_num} | Closes: {close_date}\n{grants_url}\n\n"
        full_post = header + post_text

        if dry_run:
            logger.info("DRY RUN — would post to #%s:\n%s\n", target_channel, full_post)
            posted_list.append({"number": opp_num, "title": title, "channel": target_channel})
            continue

        # Race-safe claim: only one GrantBot instance (or run) can win the insert.
        # If another instance already claimed this FOA, we skip without posting.
        claimed = await _claim_foa(session, opp_num, target_channel, title)
        if not claimed:
            logger.info("Skipping FOA %s — already claimed by another run", opp_num)
            continue

        if not slack_client:
            # No Slack client available (no token configured). Release the claim
            # so a future run with credentials can post this FOA.
            await _release_foa(session, opp_num)
            continue

        try:
            slack_client.chat_postMessage(channel=f"#{target_channel}", text=full_post)
            logger.info("Posted opportunity %s to #%s", opp_num, target_channel)
        except Exception as exc:
            logger.error("Failed to post %s to #%s: %s", opp_num, target_channel, exc)
            await _release_foa(session, opp_num)
            continue

        posted_list.append({
            "number": opp_num,
            "title": title,
            "channel": target_channel,
        })

    logger.info("GrantBot run complete: %d opportunities posted", len(posted_list))
    return posted_list


LAST_RUN_FILE = Path("data/grantbot_last_run.txt")


def _should_run_today() -> bool:
    """Return True if grantbot hasn't completed a run today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if LAST_RUN_FILE.exists():
        last_date = LAST_RUN_FILE.read_text(encoding="utf-8").strip()
        return last_date != today
    return True


def _mark_run_complete() -> None:
    """Record that grantbot ran today."""
    LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    LAST_RUN_FILE.write_text(today, encoding="utf-8")


@app.command()
def main(
    channel: str = typer.Option("funding-opportunities", "--channel", help="Slack channel to post to"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview posts without sending to Slack"),
    max_posts: int = typer.Option(10, "--max-posts", help="Max opportunities to post per run"),
    max_per_channel: int = typer.Option(1, "--max-per-channel", help="Max opportunities to post per channel per run"),
):
    """Search for funding opportunities and post relevant ones to Slack."""
    results = asyncio.run(run_grantbot(
        channel=channel,
        dry_run=dry_run,
        max_posts=max_posts,
        max_per_channel=max_per_channel,
    ))
    if results:
        typer.echo(f"\nPosted {len(results)} opportunities:")
        for r in results:
            typer.echo(f"  #{r['channel']}: {r['number']} — {r['title']}")
    else:
        typer.echo("No new relevant opportunities found.")
    if not dry_run:
        _mark_run_complete()


@app.command("scheduler")
def scheduler(
    channel: str = typer.Option("funding-opportunities", "--channel", help="Slack channel to post to"),
    max_posts: int = typer.Option(10, "--max-posts", help="Max opportunities to post per run"),
    max_per_channel: int = typer.Option(1, "--max-per-channel", help="Max opportunities to post per channel per run"),
    run_hour: int = typer.Option(8, "--run-hour", help="UTC hour to run daily (0-23)"),
    check_interval: int = typer.Option(900, "--check-interval", help="Seconds between schedule checks"),
):
    """Long-running scheduler that executes grantbot once per calendar day.

    If the container starts after the scheduled hour, it runs immediately
    to catch up on the missed execution.
    """
    import time

    logger.info("GrantBot scheduler started (run_hour=%d UTC, check every %ds)", run_hour, check_interval)

    while True:
        now = datetime.now(timezone.utc)
        if _should_run_today() and now.hour >= run_hour:
            logger.info("Running daily grant search...")
            try:
                results = asyncio.run(run_grantbot(
                    channel=channel,
                    max_posts=max_posts,
                    max_per_channel=max_per_channel,
                ))
                _mark_run_complete()
                logger.info("Daily run complete: %d opportunities posted", len(results))
            except Exception as exc:
                logger.error("Daily run failed: %s", exc, exc_info=True)
        else:
            logger.debug("No run needed (last run: %s, hour: %d)",
                         LAST_RUN_FILE.read_text().strip() if LAST_RUN_FILE.exists() else "never",
                         now.hour)

        time.sleep(check_interval)


if __name__ == "__main__":
    app()
