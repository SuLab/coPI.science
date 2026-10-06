"""A persona file for tests that activate a lab: activation refuses a missing or
summary-less file (spec 2026-10-05 §6.4). Writes under profile_export.PROFILES_DIR, which
tests/conftest.py points at a per-test directory."""
from pathlib import Path

from src.services import profile_export


def write_persona(agent_id: str, *, summary: bool = True) -> Path:
    path = profile_export.PROFILES_DIR / f"{agent_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Test Lab — Public Profile\n\n**PI:** Test\n\n"
    if summary:
        body += "## Research Summary\n\nStudies the thing.\n"
    path.write_text(body, encoding="utf-8")
    return path
