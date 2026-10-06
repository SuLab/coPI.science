"""Per-role agent customization: prompt-path resolution and role manifests.

Dependency-free on purpose (no src.models, no DB) so the resolution rules are
unit-testable without a database, and so src/agent/agent.py can import it
without pulling the ORM into the Agent class. See
docs/specs/2026-08-05-hub-bot-customization-design.md.
"""

from __future__ import annotations

import hashlib
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.agent.post_types import DEFAULT_POST_TYPES, PostTypeSpec, parse_post_types
from src.agent.role_capabilities import ROLE_CAPABILITIES
from src.agent.tool_definitions import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path("prompts")
ROLES_DIR = PROMPTS_DIR / "roles"
DEFAULT_ROLE = "pi_lab"

# Explicit, NOT "every tool in TOOL_DEFINITIONS": if the default were "all tools",
# adding a new tool to that list would silently hand it to every agent. Explicit
# default keeps every new tool opt-in. See design §4.1.
DEFAULT_TOOLS: frozenset[str] = frozenset(
    {"retrieve_profile", "retrieve_abstract", "retrieve_full_text"}
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
    # Layer 1 of post-type gating: what this role may emit as a NEW top-level
    # post. Defaults to DEFAULT_POST_TYPES, the fallback for a role with no
    # manifest or a manifest without post_types; pi_lab's role.toml declares its
    # own list explicitly.
    post_types: tuple[PostTypeSpec, ...] = DEFAULT_POST_TYPES


def available_roles() -> list[str]:
    """Every role an agent can be assigned: ``pi_lab`` plus every directory under
    ``prompts/roles/``. ``pi_lab`` is listed even if its directory does not exist
    (it is the absence of overrides, see ``resolve_prompt_path``) and always comes
    first so callers (e.g. the admin `<select>`) get a stable, predictable order.
    Only roles valid on both counts (registry entry and manifest, spec §8.5) are
    returned.
    """
    names = [DEFAULT_ROLE]
    if ROLES_DIR.is_dir():
        names += sorted(
            p.name for p in ROLES_DIR.iterdir() if p.is_dir() and p.name != DEFAULT_ROLE
        )
    valid: list[str] = []
    for name in names:
        problem = role_problem(name)
        if problem is None:
            valid.append(name)
        else:
            logger.error("[roles] %s is not an available role: %s", name, problem)
    return valid


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
    return {t["name"] for t in TOOL_DEFINITIONS}


class RoleManifestError(ValueError):
    """A ``role.toml`` that does not match the manifest schema (spec §8.5)."""


_MANIFEST_KEYS = frozenset({"version", "label", "tools", "post_types", "calls_per_load_per_window"})
_POST_TYPE_KEYS = frozenset({"name", "targets"})


def _validate_manifest(name: str, data: dict) -> None:
    """The EXISTING schema, strictly (no new keys are asked for)."""
    unknown = set(data) - _MANIFEST_KEYS
    if unknown:
        raise RoleManifestError(f"{name}: unknown key(s) {sorted(unknown)} in role.toml")
    for key in ("version", "label"):
        if key in data and not isinstance(data[key], str):
            raise RoleManifestError(f"{name}: {key} must be a string")
    tools = data.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise RoleManifestError(f"{name}: tools must be a list of tool names")
        unknown_tools = set(tools) - _known_tool_names()
        if unknown_tools:
            raise RoleManifestError(f"{name}: unknown tool(s) {sorted(unknown_tools)}")
    rate = data.get("calls_per_load_per_window")
    if rate is not None and not (isinstance(rate, int) and not isinstance(rate, bool) and rate > 0):
        raise RoleManifestError(f"{name}: calls_per_load_per_window must be a positive int")
    post_types = data.get("post_types")
    if post_types is not None:
        if not isinstance(post_types, list):
            raise RoleManifestError(f"{name}: post_types must be a list of tables")
        for entry in post_types:
            if not isinstance(entry, dict) or set(entry) - _POST_TYPE_KEYS:
                raise RoleManifestError(f"{name}: post_types entries take only name and targets")
            if not isinstance(entry.get("name"), str) or not entry["name"]:
                raise RoleManifestError(f"{name}: a post_types entry has no name")
            targets = entry.get("targets")
            if targets is not None and (
                not isinstance(targets, list) or not all(isinstance(x, str) for x in targets)
            ):
                raise RoleManifestError(f"{name}: post_types targets must be a list of role names")


def load_role(name: str) -> RoleSpec:
    """Load and strictly validate a role manifest (spec §8.5).

    A missing ``role.toml`` still yields the defaults; malformed TOML, an unknown
    key, a bad type or an unknown tool raise ``RoleManifestError``. Both shipped
    manifests validate unchanged and load to the same ``RoleSpec`` as before
    (``tests/unit/test_role_capabilities.py``). Inside the engine, reads go
    through the start-time snapshot (``prompt_snapshot.role_spec``), so a
    mid-run manifest edit cannot raise here or change tools and post types.
    """
    manifest = ROLES_DIR / name / "role.toml"
    if not manifest.is_file():
        return RoleSpec(name=name, label=name, tools=DEFAULT_TOOLS)
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RoleManifestError(f"{name}: malformed role.toml ({exc})") from exc
    _validate_manifest(name, data)
    declared = data.get("tools")
    return RoleSpec(
        name=name,
        label=str(data.get("label", name)),
        tools=DEFAULT_TOOLS if declared is None else frozenset(declared),
        calls_per_load_per_window=data.get("calls_per_load_per_window"),
        post_types=parse_post_types(data.get("post_types"), role=name),
    )


def role_problem(name: str) -> str | None:
    """Why ``name`` is not a usable role, or None (spec §8.5, fail closed): no
    ``ROLE_CAPABILITIES`` entry, a manifest that fails validation, or an entry
    that contradicts its manifest."""
    caps = ROLE_CAPABILITIES.get(name)
    if caps is None:
        return f"role {name!r} has no ROLE_CAPABILITIES entry"
    try:
        spec = load_role(name)
    except RoleManifestError as exc:
        return str(exc)
    if not caps.posts_new_threads and spec.post_types:
        return f"{name}: posts_new_threads is false but role.toml declares post types"
    if caps.posts_new_threads and not spec.post_types:
        return f"{name}: posts_new_threads is true but role.toml declares no post types"
    return None


#: The prompt-set files of each role — derived from the capability registry
#: (spec §8.5), where the tuples moved verbatim; kept under this name for its
#: readers. ``prompt_set_stamp`` hashes the same files in the same order; not every
#: file is composed (``role_capabilities.LAB_BRIEF_FILE``).
ROLE_PROMPT_FILES: dict[str, tuple[str, ...]] = {
    name: caps.prompt_files for name, caps in ROLE_CAPABILITIES.items()
}


@dataclass(frozen=True)
class PromptSetStamp:
    """Declared version + computed content hash of one role's prompt set.

    Same pattern as the rubric (src/services/blackbird_rubric.py): the version
    is what a human declared in role.toml, the sha256[:12] hash is what the
    files actually contain — a hash change without a version bump means an
    edit nobody recorded.
    """

    role: str
    version: str
    content_hash: str


def prompt_set_stamp(role: str) -> PromptSetStamp:
    """Stamp for a role in ROLE_PROMPT_FILES. Raises KeyError for any other
    role — the two announced roles are a closed set, and a silent default
    would stamp a role with files it does not use.

    The hash covers the role.toml manifest (when present) plus each resolved
    prompt file, keyed by FILENAME (not path) so the value is stable across
    checkouts. A missing file hashes as the literal b"<missing>" rather than
    raising: the announcement must never take down a run start.
    """
    filenames = ROLE_PROMPT_FILES[role]
    h = hashlib.sha256()
    manifest = ROLES_DIR / role / "role.toml"
    parts: list[tuple[str, Path]] = []
    if manifest.is_file():
        parts.append(("role.toml", manifest))
    parts += [(name, resolve_prompt_path(role, name)) for name in filenames]
    for name, path in parts:
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")

    version = "unversioned"
    if manifest.is_file():
        try:
            declared = tomllib.loads(manifest.read_text(encoding="utf-8")).get("version")
            if declared:
                version = str(declared)
        # UnicodeDecodeError (from read_text on a corrupt manifest) is a
        # ValueError subclass, unrelated to the other two caught here — but a
        # non-UTF-8 role.toml must degrade the same way a malformed one does.
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            pass  # load_role already logs malformed manifests
    return PromptSetStamp(role=role, version=version, content_hash=h.hexdigest()[:12])
