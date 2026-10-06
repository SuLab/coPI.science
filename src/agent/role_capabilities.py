"""What each agent role may do — the one code registry (spec §8.5, B11 as
amended by B22).

The flags live in code, not in ``role.toml``: a manifest is a prompt-set file,
and B22 forbids changing one (C27, C7). ``roles.load_role`` validates each
manifest against this registry, and a role with no entry here, or whose entry
contradicts its manifest, is unavailable (fail closed): the engine skips its
agents rather than running them as ``pi_lab``.

Imports nothing from ``src.agent``, so ``roles`` and the dependency-free
``post_types`` can both import it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Blackbird's brief for labs (spec 2026-10-05 §6.5, D26): part of the hub's prompt set,
#: so the engine snapshots it at start and ``prompt_set_stamp`` hashes it, but never
#: composed into the hub's own prompt; ``retrieve_profile`` serves it, fenced, when a
#: lab asks for a hub agent.
LAB_BRIEF_FILE = "lab-brief.md"


@dataclass(frozen=True)
class RoleCapabilities:
    #: May open a new top-level thread (Phase 5 pitch).
    posts_new_threads: bool
    #: Activates labs' top-level posts in its subscribed channels — for a hub role every
    #: ``SEEDED_CHANNELS`` entry (Phase 3, P0-03; spec 2026-10-05 D28).
    auto_activates_on_lab_posts: bool
    #: Its replies carry the ``<assessment_json>`` verdict sidecar.
    captures_verdicts: bool
    #: ``per_load``: calls_per_load x load; ``hub_ceiling``: hub_llm_calls_per_window.
    rate_model: Literal["per_load", "hub_ceiling"]
    #: Which phase-4 guidance dict ``thread_guidance.GUIDANCE_SETS`` renders.
    guidance_set: Literal["pi_lab", "scout_hub"]
    #: The live roster admits this role only with a linked user.
    requires_linked_user: bool
    #: Its place in the star topology.
    star_topology_role: Literal["spoke", "hub", "none"]
    #: The role's prompt-set files, in stamp order (moved verbatim from
    #: ``roles.ROLE_PROMPT_FILES``; ``prompt_set_stamp`` hashes them in this order,
    #: ``PromptSnapshot`` loads them). The composer reads its files by name, so a file
    #: listed here is not composed unless the composer asks for it (``LAB_BRIEF_FILE``
    #: never is).
    prompt_files: tuple[str, ...]


ROLE_CAPABILITIES: dict[str, RoleCapabilities] = {
    "pi_lab": RoleCapabilities(
        posts_new_threads=True,
        auto_activates_on_lab_posts=False,
        captures_verdicts=False,
        rate_model="per_load",
        guidance_set="pi_lab",
        requires_linked_user=True,
        star_topology_role="spoke",
        prompt_files=(
            "agent-system.md", "identity.md",
            "phase4-thread-reply.md", "phase5-new-post.md",
        ),
    ),
    # Reply-only: scout_hub omits phase5-new-post.md, so a pi-side edit to that
    # file never moves the hub's prompt-set hash, and holds the brief it serves to labs.
    "scout_hub": RoleCapabilities(
        posts_new_threads=False,
        auto_activates_on_lab_posts=True,
        captures_verdicts=True,
        rate_model="hub_ceiling",
        guidance_set="scout_hub",
        requires_linked_user=False,
        star_topology_role="hub",
        prompt_files=(
            "agent-system.md", "identity.md", "phase4-thread-reply.md", LAB_BRIEF_FILE,
        ),
    ),
}


def capabilities_for(role: str | None) -> RoleCapabilities | None:
    """The role's flags, or None for a role with no registry entry."""
    return ROLE_CAPABILITIES.get(role) if role else None


def hub_role_names() -> tuple[str, ...]:
    return tuple(sorted(n for n, c in ROLE_CAPABILITIES.items() if c.star_topology_role == "hub"))


def spoke_role_names() -> tuple[str, ...]:
    return tuple(sorted(n for n, c in ROLE_CAPABILITIES.items() if c.star_topology_role == "spoke"))


def roles_requiring_user() -> tuple[str, ...]:
    return tuple(sorted(n for n, c in ROLE_CAPABILITIES.items() if c.requires_linked_user))


def star_role(role: str | None) -> str:
    """The role's star-topology place: ``spoke``, ``hub`` or ``none`` (also for
    a role with no registry entry)."""
    caps = capabilities_for(role)
    return caps.star_topology_role if caps else "none"


def requires_linked_user(role: str | None) -> bool:
    caps = capabilities_for(role)
    return caps is not None and caps.requires_linked_user
