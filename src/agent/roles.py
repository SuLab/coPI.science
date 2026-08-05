"""Per-role agent customization: prompt-path resolution and role manifests.

Dependency-free on purpose (no src.models, no DB) so the resolution rules are
unit-testable without a database, and so src/agent/agent.py can import it
without pulling the ORM into the Agent class. See
docs/specs/2026-08-05-hub-bot-customization-design.md.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path("prompts")
ROLES_DIR = PROMPTS_DIR / "roles"
DEFAULT_ROLE = "pi_lab"


def resolve_prompt_path(role: str, filename: str) -> Path:
    """Return the role's override for ``filename`` if present, else the global file.

    ``pi_lab`` is the absence of overrides: ``prompts/roles/pi_lab/`` need never
    exist, and falling through to ``prompts/{filename}`` *is* pi_lab. That is what
    keeps existing agents byte-identical after this change lands.
    """
    override = ROLES_DIR / role / filename
    if override.is_file():
        return override
    return PROMPTS_DIR / filename
