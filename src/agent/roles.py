"""Per-role agent customization: prompt-path resolution and role manifests.

Dependency-free on purpose (no src.models, no DB) so the resolution rules are
unit-testable without a database, and so src/agent/agent.py can import it
without pulling the ORM into the Agent class. See
docs/specs/2026-08-05-hub-bot-customization-design.md.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path("prompts")
ROLES_DIR = PROMPTS_DIR / "roles"
DEFAULT_ROLE = "pi_lab"

# Explicit, NOT "every tool in TOOL_DEFINITIONS": if the default were "all tools",
# adding a new tool to that list would silently hand it to every agent. Explicit
# default keeps every new tool opt-in. See design §4.1.
DEFAULT_TOOLS: frozenset[str] = frozenset(
    {"retrieve_profile", "retrieve_abstract", "retrieve_full_text", "retrieve_foa"}
)


@dataclass(frozen=True)
class RoleSpec:
    name: str
    label: str
    tools: frozenset[str]
    # Optional per-role override for Settings.llm_calls_per_load_per_window.
    # None means "use the global setting". This exists to pin a specific agent;
    # it is NOT the mechanism — the load signal is (design §4.4). No role sets it.
    calls_per_load_per_window: int | None = None


def available_roles() -> list[str]:
    """Every role an agent can be assigned: ``pi_lab`` plus every directory under
    ``prompts/roles/``. ``pi_lab`` is listed even if its directory does not exist
    (it is the absence of overrides, see ``resolve_prompt_path``) and always comes
    first so callers (e.g. the admin `<select>`) get a stable, predictable order.
    """
    names = [DEFAULT_ROLE]
    if ROLES_DIR.is_dir():
        names += sorted(
            p.name for p in ROLES_DIR.iterdir() if p.is_dir() and p.name != DEFAULT_ROLE
        )
    return names


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


def _known_tool_names() -> set[str]:
    # Lazy import: avoids an import cycle (tools.py imports nothing from roles,
    # but keeping this lazy documents that roles.py must stay import-light).
    from src.agent.tools import TOOL_DEFINITIONS

    return {t["name"] for t in TOOL_DEFINITIONS}


def load_role(name: str) -> RoleSpec:
    """Load a role manifest. Never raises: a bad manifest degrades to defaults.

    - no role.toml            -> DEFAULT_TOOLS, label == name
    - malformed TOML          -> log ERROR, DEFAULT_TOOLS, label == name
    - tool not in the codebase -> log WARNING, drop it
    """
    manifest = ROLES_DIR / name / "role.toml"
    if not manifest.is_file():
        return RoleSpec(name=name, label=name, tools=DEFAULT_TOOLS)
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.error("[roles] %s: malformed role.toml (%s) — using defaults", name, exc)
        return RoleSpec(name=name, label=name, tools=DEFAULT_TOOLS)

    label = str(data.get("label", name))
    declared = data.get("tools")
    if declared is None:
        tools = DEFAULT_TOOLS
    else:
        known = _known_tool_names()
        kept = set()
        for t in declared:
            if t in known:
                kept.add(t)
            else:
                logger.warning("[roles] %s: unknown tool %r in role.toml — dropped", name, t)
        tools = frozenset(kept)
    rate = data.get("calls_per_load_per_window")
    if rate is not None and not (
        isinstance(rate, int) and not isinstance(rate, bool) and rate > 0
    ):
        logger.warning(
            "[roles] %s: calls_per_load_per_window must be a positive int, "
            "got %r — ignored", name, rate,
        )
        rate = None
    return RoleSpec(
        name=name, label=label, tools=tools, calls_per_load_per_window=rate,
    )
