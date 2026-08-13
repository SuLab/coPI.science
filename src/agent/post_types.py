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
            "pitch", ":bulb:", "Pitch to the scouting hub",
            "Offer one of your OWN lab's ideas for screening — something that might "
            "be patentable, fundable, or commercializable. Not a collaboration "
            "proposal, and never a suggestion that two other labs should talk.",
            targets=frozenset({"scout_hub"}),
        ),
    )
}
# ``opportunity_assessment`` used to live here as scout_hub's one top-level post
# type — the :mag: screening artifact. The hub is reply-only now (hard phase-5
# gate, decision 9): the artifact is the `<assessment_json>` sidecar carried
# inside its Phase-4 CONCLUDE reply (see simulation.py's `_reply_to_thread`),
# which is not a post type at all — nothing about it involves "posting a new
# top-level type" anymore, so it has no CANONICAL entry, no role.toml
# declaration (scout_hub's is `post_types = []`), and no menu row.
# ``OpportunityAssessment`` the DB model/table/admin page are unaffected —
# see src/models/opportunity.py.

# ``pi_lab`` has no role.toml — "pi_lab is the absence of overrides" (roles.py).
# So this tuple IS pi_lab's declared list. Explicit rather than "everything in
# CANONICAL", for the same reason roles.DEFAULT_TOOLS is: adding a new type must
# never silently hand it to every role.
DEFAULT_POST_TYPES: tuple[PostTypeSpec, ...] = (
    CANONICAL["pitch"],
)

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


# Roles a `targets` entry may name. Hardcoded rather than sourced from
# roles.available_roles() — not to dodge an import cycle (available_roles()
# only lists a directory, so importing it would not actually create one), but
# because this module is dependency-free by design (no src.models, no DB, no
# Agent import — see the module docstring) so it stays unit-testable with no
# filesystem or database at all. The cost: this constant does not know about a
# role directory added after this list was last updated, which is exactly the
# case the WARNING below is worded to describe.
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

    # A dict, not a list: a later `[[post_types]]` entry for a name already
    # seen replaces the earlier one (last wins) rather than appending a second,
    # contradictory line to the rendered menu while enforcement (`by_name` in
    # `_post_type_rejection`, also last-wins by construction) silently keeps
    # only the last. Re-assigning an existing key does not move it, so
    # declaration order is still the position of the FIRST occurrence of each
    # name — stable between turns, per this function's docstring.
    kept: dict[str, PostTypeSpec] = {}
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
                        "[post_types] %s: %s targets role(s) %s with no role "
                        "directory known to this module — if that role directory "
                        "does not actually exist the type will never be offered, "
                        "but if it does (e.g. it was added after _KNOWN_ROLES was "
                        "last updated) it WILL be offered whenever a live agent "
                        "holds that role; this warning cannot tell the two apart",
                        role, name, sorted(unknown),
                    )
        if base.name in kept:
            logger.warning(
                "[post_types] %s: duplicate post_types entry for %r — the "
                "later one wins",
                role, base.name,
            )
        kept[base.name] = PostTypeSpec(
            name=base.name, emoji=base.emoji, label=base.label,
            when_to_use=base.when_to_use, targets=targets,
        )
    return tuple(kept.values())


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
) -> tuple[PostTypeSpec, ...]:
    """The post types this agent may use as a new top-level post, right now.

    Declaration order is preserved so the rendered menu is stable between turns.
    A type with no ``targets`` is always available. A type with ``targets`` is
    available only when at least one reachable agent has a matching role.

    This used to also take a ``terminal_only`` flag that narrowed the result to
    a "reports finished work" subset (``TERMINAL_POST_TYPES``), so a blocked
    agent could still file its terminal artifact past the regular-work
    backpressure. That artifact (the hub's :mag: Opportunity Assessment) is not
    a post type anymore (see CANONICAL's comment) — nothing satisfies
    ``terminal_only`` post-reconciliation, for any role, ever — so the
    parameter and its narrowing were removed rather than kept as permanently
    dead code. A blocked agent's caller now skips Phase 5 outright instead of
    calling in here at all (see simulation.py's ``_phase5_new_post``).
    """
    return tuple(
        s for s in declared
        if not s.targets
        or eligible_targets(
            s, gate=gate, roles_by_agent=roles_by_agent, self_id=self_id
        )
    )


_EMPTY_MENU = (
    "**No new top-level post type is available to you this turn.** Do not use "
    '`action: "new_post"` — it will be rejected and nothing will be posted. '
    'Return `{"action": "skip"}`.'
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
            head + f" Set `tagged_agent` to exactly one of: {named}, AND tag "
            "that same agent's @BotName in the message body — tagged_agent "
            "alone does not deliver the post; the @-mention is what routes "
            "it. Tagging anyone else gets the post rejected."
        )
    return "\n".join(lines) if lines else _EMPTY_MENU
