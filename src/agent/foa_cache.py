"""Local cache for FOA (Funding Opportunity Announcement) details.

GrantBot caches full FOA details to disk when posting. Agents access
the cache via prompts (Phase 5) or tool calls (Phase 4) instead of
hitting the Grants.gov API every time.
"""

import json
import logging
from pathlib import Path
from typing import Any

from src.agent.foa_pattern import FOA_NUMBER_RE
from src.agent.foa_pattern import extract_foa_number as extract_foa_number

logger = logging.getLogger(__name__)

# Kept as a module attribute for backward compatibility:
# foa_cache.FOA_PATTERN used to be this module's own compiled pattern; it is
# now just the shared one, same object, so identity checks against
# foa_pattern.FOA_NUMBER_RE still hold.
FOA_PATTERN = FOA_NUMBER_RE

CACHE_DIR = Path("data/foa_cache")


def cache_foa(foa_number: str, opportunity: dict[str, Any]) -> None:
    """Write full opportunity dict to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{foa_number}.json"
    try:
        path.write_text(json.dumps(opportunity, default=str), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to cache FOA %s: %s", foa_number, exc)


def load_cached_foa(foa_number: str) -> dict[str, Any] | None:
    """Read cached opportunity dict, or None if not found."""
    path = CACHE_DIR / f"{foa_number}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.error("Failed to read FOA cache for %s: %s", foa_number, exc)
        return None


def format_foa_for_prompt(foa_number: str) -> str | None:
    """Load cached FOA and format as readable text for prompt injection.

    Returns the same format as tools.py _execute_retrieve_foa so agents
    see consistent FOA text regardless of source.
    """
    result = load_cached_foa(foa_number)
    if not result:
        return None

    parts = [
        f"Title: {result.get('title', 'Unknown')}",
        f"Number: {result.get('number', foa_number)}",
        f"Agency: {result.get('agency', 'Unknown')}",
        f"Open Date: {result.get('open_date', 'Not specified')}",
        f"Close Date: {result.get('close_date', 'Not specified')}",
    ]
    if result.get("award_ceiling") or result.get("award_floor"):
        parts.append(
            f"Award Range: ${result.get('award_floor', '?')} – ${result.get('award_ceiling', '?')}"
        )
    if result.get("eligibility"):
        parts.append(f"Eligibility: {result['eligibility']}")
    if result.get("category"):
        parts.append(f"Category: {result['category']}")
    parts.append("")
    if result.get("description"):
        parts.append(f"Description:\n{result['description']}")
    if result.get("synopsis"):
        parts.append(f"\nSynopsis:\n{result['synopsis']}")
    if result.get("additional_info_url"):
        parts.append(f"\nMore info: {result['additional_info_url']}")
    return "\n".join(parts)


async def backfill_cache(posted_numbers: list[str]) -> int:
    """Fetch and cache any posted FOAs not already in the cache."""
    from src.services.grants import fetch_opportunity_by_number

    count = 0
    for num in posted_numbers:
        if not load_cached_foa(num):
            try:
                result = await fetch_opportunity_by_number(num)
                if result:
                    cache_foa(num, result)
                    count += 1
            except Exception as exc:
                logger.warning("Backfill failed for %s: %s", num, exc)
    return count
