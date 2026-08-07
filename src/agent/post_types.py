"""The post-type vocabulary, and the role + topology filter over it.

A "post type" is what an agent may emit as a NEW top-level post
(``action: "new_post"`` in the phase-5 response). ``action: "reply"`` is not
governed here.

Dependency-free on purpose (no src.models, no DB, no Agent import), like
src/agent/roles.py and src/agent/thread_guidance.py, so the filter is
unit-testable without a database, an engine, or a running loop.

Why the filter exists: the phase-5 prompt used to tell every agent to tag a peer
lab, while the cohort gate forbade every such tag. Measured over one run, 259
:bulb: Idea posts produced 2 replies (0.8%) and 146 of 146 tagged posts addressed
an agent the poster could not reach. Role alone is the wrong axis — ``pi_lab`` in
a mesh deployment *should* make cross-lab posts; the same role in a star must not.
So a post type declares the counterparty roles it addresses, and availability is
computed against the agent's live gate. See
docs/specs/2026-08-06-role-topology-post-type-gating-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostTypeSpec:
    """One post type. ``targets`` is the set of ``AgentRegistry.role`` values this
    type addresses; empty means it addresses no one (a broadcast)."""

    name: str
    emoji: str
    label: str
    when_to_use: str
    targets: frozenset[str] = frozenset()


# Every type the codebase knows. A role may only declare names from this table —
# an unknown name is dropped with a WARNING rather than silently inventing a type
# the prompt has no instructions for.
CANONICAL: dict[str, PostTypeSpec] = {
    s.name: s
    for s in (
        PostTypeSpec(
            "paper", ":newspaper:", "Paper",
            "Share a recent publication with a specific finding others could build on.",
        ),
        PostTypeSpec(
            "help_wanted", ":sos:", "Help Wanted",
            "Seek a specific capability, reagent, dataset, or expertise your lab "
            "genuinely needs and cannot produce in-house.",
        ),
        PostTypeSpec(
            "introduction", ":wave:", "Introduction",
            "Introduce your lab's interests and expertise. Use sparingly — only if "
            "you have not introduced yourself in this channel yet.",
        ),
        PostTypeSpec(
            "idea_crosslab", ":bulb:", "Idea (cross-lab)",
            "Propose an idea at the interface between your lab and another specific "
            "lab. Name a concrete first experiment or dataset exchange.",
            targets=frozenset({"pi_lab"}),
        ),
        PostTypeSpec(
            "pitch", ":bulb:", "Pitch to the scouting hub",
            "Offer one of your OWN lab's ideas for screening — something that might "
            "be patentable, fundable, or commercializable. Not a collaboration "
            "proposal, and never a suggestion that two other labs should talk.",
            targets=frozenset({"scout_hub"}),
        ),
        PostTypeSpec(
            "funding_collab", ":moneybag:", "Funding collaboration",
            "Start a funding-originated collaboration around a specific FOA. Must "
            "include the FOA number.",
            targets=frozenset({"pi_lab"}),
        ),
        PostTypeSpec(
            "opportunity_assessment", ":mag:", "Opportunity Assessment",
            "The completed screening artifact for Blackbird staff and the PI.",
        ),
    )
}

# ``pi_lab`` has no role.toml — "pi_lab is the absence of overrides" (roles.py).
# So this tuple IS pi_lab's declared list. Explicit rather than "everything in
# CANONICAL", for the same reason roles.DEFAULT_TOOLS is: adding a new type must
# never silently hand it to every role.
DEFAULT_POST_TYPES: tuple[PostTypeSpec, ...] = (
    CANONICAL["paper"],
    CANONICAL["help_wanted"],
    CANONICAL["introduction"],
    CANONICAL["idea_crosslab"],
    CANONICAL["pitch"],
    CANONICAL["funding_collab"],
)

# Types that count as funding actions. In funding_only mode (the agent is blocked
# for regular posts) the available set is narrowed to these.
FUNDING_POST_TYPES: frozenset[str] = frozenset({"funding_collab"})

# Retired names a running deployment may still emit. ``idea`` sat in the old
# phase-5 enum alongside ``idea_crosslab`` with no documented difference and no
# code distinguishing them (design §2), so collapsing them is right — but a mesh
# deployment whose bind-mounted prompts lag the baked-in code would otherwise
# have every ``idea`` post rejected by layer 1 and silently publish nothing.
# That is a regression in a deployment this change is not supposed to touch.
#
# Aliases resolve on INPUT only. They are deliberately absent from CANONICAL,
# from any role's declared list, and from every rendered menu, so nothing here
# re-offers a name the vocabulary retired.
LEGACY_POST_TYPE_ALIASES: dict[str, str] = {"idea": "idea_crosslab"}


def resolve_post_type_name(name: str) -> str:
    """Map a retired post-type name onto its current one; pass anything else through."""
    return LEGACY_POST_TYPE_ALIASES.get(name, name)


# Roles a `targets` entry may name. Kept here rather than imported from roles.py
# to avoid a cycle; roles.available_roles() is filesystem-derived and would make
# this module depend on the prompts directory.
_KNOWN_ROLES: frozenset[str] = frozenset({"pi_lab", "scout_hub"})


def parse_post_types(raw: object, *, role: str) -> tuple[PostTypeSpec, ...]:
    """Parse the ``post_types`` key of a role manifest. Never raises.

    ``None`` (key absent) yields ``DEFAULT_POST_TYPES``. Anything that is not a
    list yields the defaults with a WARNING. Individual malformed or unknown
    entries are dropped with a WARNING and the rest are kept — the same
    degradation roles.load_role uses for ``tools``.
    """
    if raw is None:
        return DEFAULT_POST_TYPES
    if not isinstance(raw, list):
        logger.warning(
            "[post_types] %s: post_types must be a list of tables, got %s — "
            "using defaults", role, type(raw).__name__,
        )
        return DEFAULT_POST_TYPES

    kept: list[PostTypeSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning(
                "[post_types] %s: post_types entry is not a table (%r) — dropped",
                role, entry,
            )
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            logger.warning(
                "[post_types] %s: post_types entry has no usable name (%r) — dropped",
                role, entry,
            )
            continue
        base = CANONICAL.get(name)
        if base is None:
            logger.warning(
                "[post_types] %s: unknown post type %r in role.toml — dropped",
                role, name,
            )
            continue
        targets = base.targets
        declared = entry.get("targets")
        if declared is not None:
            if not isinstance(declared, list) or not all(
                isinstance(x, str) for x in declared
            ):
                logger.warning(
                    "[post_types] %s: %s targets must be a list of role names, "
                    "got %r — keeping the canonical default %s",
                    role, name, declared, sorted(base.targets),
                )
            else:
                targets = frozenset(declared)
                unknown = targets - _KNOWN_ROLES
                if unknown:
                    logger.warning(
                        "[post_types] %s: %s targets name unknown role(s) %s — the "
                        "type will never be offered",
                        role, name, sorted(unknown),
                    )
        kept.append(
            PostTypeSpec(
                name=base.name, emoji=base.emoji, label=base.label,
                when_to_use=base.when_to_use, targets=targets,
            )
        )
    return tuple(kept)


def eligible_targets(
    spec: PostTypeSpec,
    *,
    gate: set[str] | None,
    roles_by_agent: dict[str, str],
    self_id: str,
) -> frozenset[str]:
    """Agents this post type may address, given the acting agent's gate.

    Self is always excluded — an agent's own role sits in its own gate, and an
    agent is never its own counterparty. An agent with no known role (e.g.
    ``grantbot``, which has cohort memberships but no ``AgentRegistry`` row)
    matches no ``targets``.

    ``gate is None`` means the cohort gate is off for this agent, so every agent
    with a matching role is reachable.
    """
    if not spec.targets:
        return frozenset()
    candidates = roles_by_agent if gate is None else {
        aid: r for aid, r in roles_by_agent.items() if aid in gate
    }
    return frozenset(
        aid for aid, r in candidates.items()
        if aid != self_id and r in spec.targets
    )


def available_for(
    declared: tuple[PostTypeSpec, ...],
    *,
    gate: set[str] | None,
    roles_by_agent: dict[str, str],
    self_id: str,
    funding_only: bool,
) -> tuple[PostTypeSpec, ...]:
    """The post types this agent may use as a new top-level post, right now.

    Declaration order is preserved so the rendered menu is stable between turns.
    A type with no ``targets`` is always available. A type with ``targets`` is
    available only when at least one reachable agent has a matching role.

    ``funding_only`` narrows the result to ``FUNDING_POST_TYPES``; the result may
    legitimately be empty in that mode, which must NOT be treated as "skip the
    turn" — a funding *reply* is still valid. See spec §5.
    """
    out = [
        s for s in declared
        if not s.targets
        or eligible_targets(
            s, gate=gate, roles_by_agent=roles_by_agent, self_id=self_id
        )
    ]
    if funding_only:
        out = [s for s in out if s.name in FUNDING_POST_TYPES]
    return tuple(out)


_EMPTY_MENU = (
    "**No new top-level post type is available to you this turn.** Do not use "
    "`action: \"new_post\"` — it will be rejected and nothing will be posted. "
    "Reply to an existing post (Option A) or skip (Option D)."
)


def render_menu(
    specs: list[PostTypeSpec] | tuple[PostTypeSpec, ...],
    *,
    gate: set[str] | None,
    roles_by_agent: dict[str, str],
    self_id: str,
    bot_names: dict[str, str],
) -> str:
    """Render the available set as the prompt's ``{post_type_menu}``.

    Never returns an empty string, and never prints an empty enumeration. An
    addressed type has two renderings:

    - **gate set** — enumerate the reachable agents, because the list is short
      and naming them is the whole point.
    - **gate None** — guidance only. A mesh has ~50 reachable labs; enumerating
      them into every phase-5 prompt would recreate the lab directory this
      design is shrinking. It is also the path a caller with no topology takes
      (``build_phase5_prompt`` with no menu), where the enumeration would come
      out as the literal ``one of: .`` and land in a snapshot.
    """
    if not specs:
        return _EMPTY_MENU
    lines: list[str] = []
    for s in specs:
        head = f"- **`{s.name}`** — {s.emoji} {s.label}. {s.when_to_use}"
        if not s.targets:
            lines.append(head + " Addresses no one — do not tag anyone; set "
                                "`tagged_agent` to `null`.")
            continue
        if gate is None:
            roles = " or ".join(sorted(s.targets))
            lines.append(
                head + f" Addresses one agent whose role is {roles} — set "
                "`tagged_agent` to that agent's `agent_id` and tag its @BotName "
                "in the message body."
            )
            continue
        reachable = sorted(
            eligible_targets(
                s, gate=gate, roles_by_agent=roles_by_agent, self_id=self_id
            )
        )
        if not reachable:
            # available_for already drops these, so reaching here means the
            # caller passed an unfiltered list. Drop it rather than printing
            # "Set tagged_agent to exactly one of: ." — offering a type with an
            # empty target list is worse than not offering it.
            continue
        named = ", ".join(f"`{aid}` (@{bot_names.get(aid, aid + 'Bot')})" for aid in reachable)
        lines.append(
            head + f" Set `tagged_agent` to exactly one of: {named}. "
            "Tagging anyone else gets the post rejected."
        )
    return "\n".join(lines) if lines else _EMPTY_MENU
