"""Podcast state persistence — tracks delivered PMIDs and last run timestamp.

State is keyed separately for agents (by agent_id string) and for plain ORCID
users (by user_id UUID string, stored under "users" in the JSON).

JSON structure:
{
  "agents": {
    "<agent_id>": {"delivered_pmids": ["12345", ...]},
    ...
  },
  "users": {
    "<user_id UUID string>": {"delivered_pmids": ["12345", ...]},
    ...
  },
  "last_run_date": "2026-04-14"
}
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/podcast_state.json")
_LOCK = threading.Lock()


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load podcast state: %s", exc)
    return {}


def _save(data: dict) -> None:
    """Write state atomically via temp-file + rename."""
    import os
    import tempfile

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, STATE_FILE)
    except Exception:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Agent-keyed helpers (existing behaviour, unchanged interface)
# ---------------------------------------------------------------------------

def get_delivered_pmids(agent_id: str) -> set[str]:
    """Return the set of PMIDs already delivered to this agent."""
    data = _load()
    return set(data.get("agents", {}).get(agent_id, {}).get("delivered_pmids", []))


def record_delivery(agent_id: str, pmid: str) -> None:
    """Record that a PMID was delivered to this agent."""
    with _LOCK:
        data = _load()
        agents = data.setdefault("agents", {})
        agent_data = agents.setdefault(agent_id, {"delivered_pmids": []})
        pmids = agent_data.setdefault("delivered_pmids", [])
        if pmid not in pmids:
            pmids.append(pmid)
        _save(data)


# ---------------------------------------------------------------------------
# User-keyed helpers (new — for plain ORCID users)
# ---------------------------------------------------------------------------

def get_delivered_pmids_for_user(user_id: str) -> set[str]:
    """Return the set of PMIDs already delivered to this user (no agent)."""
    data = _load()
    return set(data.get("users", {}).get(str(user_id), {}).get("delivered_pmids", []))


def record_delivery_for_user(user_id: str, pmid: str) -> None:
    """Record that a PMID was delivered to this user."""
    with _LOCK:
        data = _load()
        users = data.setdefault("users", {})
        user_data = users.setdefault(str(user_id), {"delivered_pmids": []})
        pmids = user_data.setdefault("delivered_pmids", [])
        if pmid not in pmids:
            pmids.append(pmid)
        _save(data)


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------

def get_last_run_date() -> str | None:
    """Return ISO date string of the last completed podcast run, or None."""
    data = _load()
    return data.get("last_run_date")


def mark_run_complete() -> None:
    """Record that the podcast pipeline ran today (UTC)."""
    with _LOCK:
        data = _load()
        data["last_run_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _save(data)


def should_run_today() -> bool:
    """Return True if the podcast pipeline has not run today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return get_last_run_date() != today
