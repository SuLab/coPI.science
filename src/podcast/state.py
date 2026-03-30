"""Podcast state persistence — tracks delivered PMIDs and last run timestamp."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/podcast_state.json")


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load podcast state: %s", exc)
    return {}


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_delivered_pmids(agent_id: str) -> set[str]:
    """Return the set of PMIDs already delivered to this agent."""
    data = _load()
    return set(data.get("agents", {}).get(agent_id, {}).get("delivered_pmids", []))


def record_delivery(agent_id: str, pmid: str) -> None:
    """Record that a PMID was delivered to this agent."""
    data = _load()
    agents = data.setdefault("agents", {})
    agent_data = agents.setdefault(agent_id, {"delivered_pmids": []})
    pmids = agent_data.setdefault("delivered_pmids", [])
    if pmid not in pmids:
        pmids.append(pmid)
    _save(data)


def get_last_run_date() -> str | None:
    """Return ISO date string of the last completed podcast run, or None."""
    data = _load()
    return data.get("last_run_date")


def mark_run_complete() -> None:
    """Record that the podcast pipeline ran today (UTC)."""
    data = _load()
    data["last_run_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _save(data)


def should_run_today() -> bool:
    """Return True if the podcast pipeline has not run today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return get_last_run_date() != today
