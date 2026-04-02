"""Per-agent podcast pipeline: search → select → summarize → TTS → Slack DM → DB."""

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles/public")
AUDIO_DIR = Path("data/podcast_audio")


def _load_public_profile(agent_id: str) -> str:
    """Load the public profile markdown for an agent."""
    path = PROFILES_DIR / f"{agent_id}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _format_candidates_for_prompt(records: list[dict[str, Any]]) -> str:
    """Format PubMed records as a numbered list for the selection prompt."""
    lines = []
    for i, rec in enumerate(records, 1):
        title = rec.get("title", "No title")
        abstract = rec.get("abstract", "No abstract")[:600]
        journal = rec.get("journal") or "Unknown journal"
        year = rec.get("year") or "Unknown year"
        lines.append(f"{i}. [{journal}, {year}] {title}\n   {abstract}")
    return "\n\n".join(lines)


async def _select_article(
    profile_text: str,
    candidates: list[dict[str, Any]],
    agent_id: str,
) -> tuple[dict[str, Any], str] | tuple[None, str]:
    """Use Sonnet to pick the most relevant article.

    Returns (selected_record, justification) or (None, reason).
    """
    from src.services.llm import generate_agent_response

    settings = get_settings()

    prompt_path = Path("prompts/podcast-select.md")
    template = prompt_path.read_text(encoding="utf-8")
    candidates_text = _format_candidates_for_prompt(candidates)
    prompt = template.replace("{profile}", profile_text).replace("{candidates}", candidates_text)

    try:
        response = await generate_agent_response(
            system_prompt=prompt,
            messages=[{"role": "user", "content": "Select the most relevant article."}],
            model=settings.llm_agent_model_sonnet,
            max_tokens=300,
            log_meta={"agent_id": agent_id, "phase": "podcast_select"},
        )

        # Extract JSON
        text = response.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
        else:
            raise ValueError("No JSON object found in response")

        idx = data.get("index")
        justification = data.get("justification", "")

        if idx is None:
            logger.info("Agent %s: no relevant article found (%s)", agent_id, justification)
            return None, justification

        idx = int(idx) - 1  # convert 1-based to 0-based
        if 0 <= idx < len(candidates):
            return candidates[idx], justification
        else:
            logger.warning("Agent %s: LLM returned out-of-range index %d", agent_id, idx + 1)
            return None, "Index out of range"

    except Exception as exc:
        logger.error("Article selection failed for agent %s: %s", agent_id, exc)
        return None, str(exc)


async def _generate_summary(
    profile_text: str,
    record: dict[str, Any],
    full_text: str | None,
    agent_id: str,
) -> str | None:
    """Use Opus to generate the structured text summary."""
    from src.services.llm import generate_agent_response

    settings = get_settings()

    prompt_path = Path("prompts/podcast-summarize.md")
    template = prompt_path.read_text(encoding="utf-8")

    # Build paper section
    authors_list = record.get("authors") or []
    if not authors_list:
        authors_str = "Authors not available"
    elif len(authors_list) > 3:
        authors_str = ", ".join(authors_list[:3]) + " et al."
    else:
        authors_str = ", ".join(authors_list)

    paper_section = f"""Title: {record.get('title', '')}
Authors: {authors_str}
Journal: {record.get('journal') or 'Unknown'}
Year: {record.get('year') or 'Unknown'}
PMID: {record.get('pmid', '')}

Abstract:
{record.get('abstract', '')}"""

    if full_text:
        paper_section += f"\n\nFull text excerpt:\n{full_text[:3000]}"

    today_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    prompt = (
        template
        .replace("{profile}", profile_text)
        .replace("{paper}", paper_section)
        .replace("{date}", today_str)
        .replace("{paper_title}", record.get("title", ""))
        .replace("{authors}", authors_str)
        .replace("{journal}", record.get("journal") or "Unknown")
        .replace("{year}", str(record.get("year") or ""))
        .replace("{pmid}", record.get("pmid", ""))
    )

    try:
        response = await generate_agent_response(
            system_prompt=prompt,
            messages=[{"role": "user", "content": "Write the research brief."}],
            model=settings.llm_agent_model_opus,
            max_tokens=600,
            log_meta={"agent_id": agent_id, "phase": "podcast_summarize"},
        )
        return response.strip()
    except Exception as exc:
        logger.error("Summary generation failed for agent %s: %s", agent_id, exc)
        return None


async def _try_fetch_full_text(pmid: str) -> str | None:
    """Attempt to fetch full text from PMC; return None on failure."""
    try:
        from src.services.pubmed import fetch_full_text
        result = await fetch_full_text(pmid)
        if "error" in result:
            return None
        return result.get("methods")
    except Exception:
        return None


async def _deliver_slack_dm(
    agent_id: str,
    bot_token: str,
    slack_user_id: str,
    summary_text: str,
    rss_url: str,
) -> bool:
    """Send the text summary as a Slack DM from the agent bot to the PI."""
    if not bot_token or bot_token.startswith("xoxb-placeholder"):
        logger.info("Agent %s: no valid Slack token, skipping DM delivery", agent_id)
        return False
    if not slack_user_id:
        logger.info("Agent %s: no slack_user_id configured, skipping DM delivery", agent_id)
        return False

    try:
        from slack_sdk import WebClient
        client = WebClient(token=bot_token)

        # Open DM channel
        dm_resp = client.conversations_open(users=[slack_user_id])
        channel_id = dm_resp["channel"]["id"]

        # Append RSS link
        full_message = summary_text
        if rss_url:
            full_message += f"\n\n_Listen to the audio version: {rss_url}_"

        client.chat_postMessage(channel=channel_id, text=full_message)
        logger.info("Agent %s: Slack DM delivered to %s", agent_id, slack_user_id)
        return True
    except Exception as exc:
        logger.error("Agent %s: Slack DM failed: %s", agent_id, exc)
        return False


async def run_pipeline_for_agent(
    agent_id: str,
    bot_name: str,
    pi_name: str,
    bot_token: str,
    slack_user_id: str | None,
    db_session,
) -> bool:
    """Run the full podcast pipeline for one agent.

    Returns True if an episode was produced and recorded.
    """
    from src.models.podcast import PodcastEpisode
    from src.podcast.mistral_tts import generate_audio, get_audio_duration_seconds
    from src.podcast.pubmed_search import build_queries, fetch_candidates
    from src.podcast.state import get_delivered_pmids, record_delivery

    settings = get_settings()
    today = date.today()

    logger.info("Starting podcast pipeline for agent: %s (%s)", agent_id, pi_name)

    # Step 1: Load profile
    profile_text = _load_public_profile(agent_id)
    if not profile_text:
        logger.warning("Agent %s: no public profile found, skipping", agent_id)
        return False

    # Build a minimal profile dict from markdown for query building
    profile_dict = _parse_profile_markdown(profile_text)

    # Step 2: Build queries and fetch candidates
    queries = build_queries(profile_dict)
    if not queries:
        logger.warning("Agent %s: could not build search queries", agent_id)
        return False

    already_delivered = get_delivered_pmids(agent_id)
    candidates = await fetch_candidates(
        queries,
        already_delivered=already_delivered,
        days=settings.podcast_search_window_days,
        max_total=settings.podcast_max_candidates,
    )

    if not candidates:
        logger.info("Agent %s: no new candidate articles found", agent_id)
        return False

    # Step 3: LLM article selection
    selected, justification = await _select_article(profile_text, candidates, agent_id)
    if selected is None:
        logger.info("Agent %s: no article selected", agent_id)
        return False

    pmid = selected.get("pmid", "")
    logger.info("Agent %s: selected PMID %s", agent_id, pmid)

    # Step 4: Try to fetch full text
    full_text = await _try_fetch_full_text(pmid)

    # Step 5: Generate text summary
    summary = await _generate_summary(profile_text, selected, full_text, agent_id)
    if not summary:
        logger.error("Agent %s: summary generation failed", agent_id)
        return False

    # Step 6: Generate audio
    audio_path = AUDIO_DIR / agent_id / f"{today.isoformat()}.mp3"
    audio_ok = await generate_audio(summary, agent_id, audio_path)
    audio_file_path = str(audio_path) if audio_ok else None
    audio_duration = None
    if audio_ok:
        audio_duration = get_audio_duration_seconds(audio_path)

    # Step 7: Build RSS URL for DM
    base_url = settings.podcast_base_url or settings.base_url
    rss_url = f"{base_url}/podcast/{agent_id}/feed.xml"

    # Step 8: Deliver Slack DM
    slack_ok = await _deliver_slack_dm(
        agent_id=agent_id,
        bot_token=bot_token,
        slack_user_id=slack_user_id or "",
        summary_text=summary,
        rss_url=rss_url,
    )

    # Extract metadata from selected record
    authors_list = selected.get("authors") or []
    if len(authors_list) > 3:
        authors_str = ", ".join(authors_list[:3]) + " et al."
    else:
        authors_str = ", ".join(authors_list) if authors_list else "Unknown"

    # Step 9: Persist to DB
    episode = PodcastEpisode(
        agent_id=agent_id,
        episode_date=today,
        pmid=pmid,
        paper_title=selected.get("title") or "",
        paper_authors=authors_str,
        paper_journal=selected.get("journal") or "",
        paper_year=selected.get("year") or 0,
        text_summary=summary,
        audio_file_path=audio_file_path,
        audio_duration_seconds=audio_duration,
        slack_delivered=slack_ok,
        selection_justification=justification,
    )
    db_session.add(episode)
    await db_session.flush()

    # Step 10: Update state
    record_delivery(agent_id, pmid)

    logger.info(
        "Agent %s: episode complete (audio=%s, slack=%s)", agent_id, audio_ok, slack_ok
    )
    return True


def _parse_profile_markdown(text: str) -> dict[str, Any]:
    """Extract structured fields from public profile markdown for query building."""
    from src.agent.grantbot import _extract_list_section
    return {
        "disease_areas": _extract_list_section(text, "Disease Areas"),
        "techniques": _extract_list_section(text, "Key Methods and Technologies"),
        "experimental_models": _extract_list_section(text, "Model Systems"),
        "keywords": _extract_list_section(text, "Keywords"),
        "research_summary": _extract_section_text(text, "Research Summary"),
    }


def _extract_section_text(text: str, section_name: str) -> str:
    """Extract free-form text from a markdown section."""
    lines = []
    in_section = False
    for line in text.splitlines():
        if section_name.lower() in line.lower() and line.startswith("##"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            lines.append(line)
    return " ".join(l.strip() for l in lines if l.strip())
