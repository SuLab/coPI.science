# Role- and Topology-Aware Post-Type Gating — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `pi_lab` agents from emitting top-level posts addressed to agents their cohort gate forbids, by making the set of post types an agent may use a function of its role *and* its live topology, enforced in code rather than requested in prose.

**Architecture:** One new dependency-free module, `src/agent/post_types.py`, owns the post-type vocabulary and the filter. `src/agent/roles.py` gains a `post_types` field parsed from `role.toml`. `SimulationEngine` computes the available set once per phase-5 turn, renders it into the prompt as `{post_type_menu}`, and enforces the *same* set when the model answers — so the prompt and the gate can never disagree. A separate ordering fix makes the existing cohort filter on the lab directory actually run.

**Tech Stack:** Python 3.11, `tomllib`, pytest + syrupy (snapshot), SQLAlchemy async, Docker Compose.

**Spec:** `docs/specs/2026-08-06-role-topology-post-type-gating-design.md`
**Draft prompts:** `docs/specs/2026-08-06-post-type-gating-prompts-draft/`

## Global Constraints

- **Test command is on the host, never in the container:** `.venv-test/bin/python -m pytest tests/ -v`. The image has no `[dev]` extra, so pytest is not installed there.
- **Full gate before any commit is considered done:** `./scripts/ci.sh` (single alembic head, upgrade→downgrade→upgrade round trip, `ruff check` zero findings on `tests/`, ratcheted ceiling on `src/`, full pytest with a branch-coverage floor). There is no server-side CI.
- **No migration in this work.** `post_types` is config; `AgentRegistry.role` already exists. Do not add one.
- **`src/agent/post_types.py` and `src/agent/roles.py` must stay dependency-free** — no `src.models`, no DB, no `Agent` import. That is what makes them unit-testable without a database.
- **Never run `pytest --snapshot-update` as a blanket command.** Task 6 regenerates exactly eight named snapshots and requires a line-by-line diff review. The `pi_lab` EXPLORE/DECIDE/CONCLUDE strings in `src/agent/thread_guidance.py` must appear **unchanged** in that diff.
- **`prompts/identity.md` has no trailing newline, deliberately.** `_DEFAULT_IDENTITY` (`src/agent/agent.py:753`) must match it byte-for-byte; see the comment at `:750`.
- **Do not reword** `### Option C: Make a new top-level post`, `### Option D: Skip this turn`, `## Your subscribed channels`, `## Your recent posts`, `## Prior conversations with other labs`, or the two-line phase-5 intro paragraph. Four regexes in `src/agent/agent.py:599-633` key on them byte-exactly.
- **The allow-list governs `action: "new_post"` only.** `action: "reply"` is never gated by it.
- **Gate `None` means no filtering.** Layers 2 and 3 must be provably inert when `agent.allowed_sender_ids is None`, so org1's mesh deployment is unaffected.
- **Commit style:** end every commit message with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `src/agent/post_types.py` *(new)* | The canonical vocabulary, `DEFAULT_POST_TYPES`, `parse_post_types`, `available_for`, `eligible_targets`, `render_menu`. Pure functions over plain data. |
| `src/agent/roles.py` *(modify)* | Add `RoleSpec.post_types`; parse and degrade the `role.toml` key. |
| `prompts/roles/scout_hub/role.toml` *(modify)* | Declare the hub's two post types. |
| `src/agent/agent.py` *(modify)* | Substitute `{post_type_menu}` in `build_phase5_prompt`. |
| `src/agent/simulation.py` *(modify)* | Compute the set, pass the menu, enforce L1/L2/L3; fix the lab-directory ordering and add the gate-change rebuild. |
| `prompts/*.md`, `prompts/roles/scout_hub/*.md` *(modify)* | Install the reviewed drafts. |
| `tests/unit/test_post_types.py` *(new)* | The vocabulary, filter, and renderer. |
| `tests/unit/test_roles.py` *(modify)* | `post_types` parsing + degradation; add `{post_type_menu}` to the two token lists. |
| `tests/unit/test_lab_directory_ordering.py` *(new)* | The production-order regression and the gate-change rebuild. |
| `tests/unit/test_post_type_enforcement.py` *(new)* | L1/L2/L3 rejection, and that a rejection posts nothing. |

---

### Task 1: The `post_types` module

**Files:**
- Create: `src/agent/post_types.py`
- Test: `tests/unit/test_post_types.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `@dataclass(frozen=True) PostTypeSpec(name: str, emoji: str, label: str, when_to_use: str, targets: frozenset[str])`
  - `CANONICAL: dict[str, PostTypeSpec]` — every known type, keyed by name
  - `DEFAULT_POST_TYPES: tuple[PostTypeSpec, ...]` — the `pi_lab` set
  - `parse_post_types(raw: object, *, role: str) -> tuple[PostTypeSpec, ...]`
  - `eligible_targets(spec, *, gate: set[str] | None, roles_by_agent: dict[str, str], self_id: str) -> frozenset[str]`
  - `available_for(declared, *, gate, roles_by_agent, self_id, funding_only: bool) -> tuple[PostTypeSpec, ...]`
  - `render_menu(specs, *, gate, roles_by_agent, self_id, bot_names: dict[str, str]) -> str`
  - `FUNDING_POST_TYPES: frozenset[str]` — `{"funding_collab"}`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_post_types.py`:

```python
"""The post-type vocabulary and the role/topology filter.

Pure functions over plain data — no DB, no engine, no Agent. See
docs/specs/2026-08-06-role-topology-post-type-gating-design.md §2, §3.
"""
from src.agent.post_types import (
    CANONICAL,
    DEFAULT_POST_TYPES,
    FUNDING_POST_TYPES,
    available_for,
    eligible_targets,
    parse_post_types,
    render_menu,
)

# The star: a spoke may reach only itself, the hub, and grantbot (which has no
# AgentRegistry row, so no role).
STAR_GATE = {"gill", "blackbird", "grantbot"}
STAR_ROLES = {"gill": "pi_lab", "blackbird": "scout_hub"}
BOT_NAMES = {"gill": "GillBot", "blackbird": "BlackbirdBot", "pearce": "PearceBot"}

# The mesh: several pi_lab peers, no hub.
MESH_ROLES = {"gill": "pi_lab", "pearce": "pi_lab", "wu": "pi_lab"}


def _by_name(specs):
    return {s.name for s in specs}


def test_canonical_vocabulary_is_exactly_the_spec_table():
    assert set(CANONICAL) == {
        "paper", "help_wanted", "introduction",
        "idea_crosslab", "pitch", "funding_collab", "opportunity_assessment",
    }


def test_idea_is_not_a_type_anymore():
    """`idea` and `idea_crosslab` were both in the old enum with no documented
    difference and no code distinguishing them. Collapsed to one."""
    assert "idea" not in CANONICAL


def test_default_post_types_is_the_pi_lab_set():
    assert _by_name(DEFAULT_POST_TYPES) == {
        "paper", "help_wanted", "introduction",
        "idea_crosslab", "pitch", "funding_collab",
    }
    assert "opportunity_assessment" not in _by_name(DEFAULT_POST_TYPES)


def test_broadcast_types_carry_no_targets():
    for name in ("paper", "help_wanted", "introduction"):
        assert CANONICAL[name].targets == frozenset()


def test_addressed_types_declare_their_counterparty_role():
    assert CANONICAL["idea_crosslab"].targets == frozenset({"pi_lab"})
    assert CANONICAL["pitch"].targets == frozenset({"scout_hub"})
    assert CANONICAL["funding_collab"].targets == frozenset({"pi_lab"})


# --- eligible_targets -------------------------------------------------------

def test_eligible_targets_excludes_self():
    """An agent's own role is in its own gate; it must never be its own target."""
    spec = CANONICAL["idea_crosslab"]
    got = eligible_targets(spec, gate={"gill"}, roles_by_agent={"gill": "pi_lab"}, self_id="gill")
    assert got == frozenset()


def test_eligible_targets_finds_the_hub_for_pitch():
    got = eligible_targets(
        CANONICAL["pitch"], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill"
    )
    assert got == frozenset({"blackbird"})


def test_eligible_targets_ignores_agents_with_no_known_role():
    """grantbot has cohort memberships but no AgentRegistry row, so it matches
    no `targets` — it is a funding announcer, not a pitch recipient."""
    got = eligible_targets(
        CANONICAL["pitch"], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill"
    )
    assert "grantbot" not in got


def test_eligible_targets_is_empty_for_a_lab_peer_in_the_star():
    got = eligible_targets(
        CANONICAL["idea_crosslab"], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill"
    )
    assert got == frozenset()


def test_eligible_targets_with_gate_off_returns_every_matching_role():
    got = eligible_targets(
        CANONICAL["idea_crosslab"], gate=None, roles_by_agent=MESH_ROLES, self_id="gill"
    )
    assert got == frozenset({"pearce", "wu"})


# --- available_for ----------------------------------------------------------

def test_star_drops_lab_peer_types_and_keeps_pitch():
    got = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", funding_only=False,
    )
    assert _by_name(got) == {"paper", "help_wanted", "introduction", "pitch"}


def test_mesh_keeps_lab_peer_types_and_drops_pitch():
    got = available_for(
        DEFAULT_POST_TYPES, gate=None, roles_by_agent=MESH_ROLES,
        self_id="gill", funding_only=False,
    )
    assert _by_name(got) == {
        "paper", "help_wanted", "introduction", "idea_crosslab", "funding_collab",
    }


def test_gate_off_never_filters_a_broadcast_type():
    got = available_for(
        DEFAULT_POST_TYPES, gate=None, roles_by_agent={}, self_id="gill", funding_only=False,
    )
    assert {"paper", "help_wanted", "introduction"} <= _by_name(got)


def test_funding_only_restricts_to_funding_types():
    got = available_for(
        DEFAULT_POST_TYPES, gate=None, roles_by_agent=MESH_ROLES,
        self_id="gill", funding_only=True,
    )
    assert _by_name(got) == {"funding_collab"}
    assert _by_name(got) <= FUNDING_POST_TYPES


def test_funding_only_in_the_star_is_empty():
    """The case that must NOT skip the turn — Option A (a funding reply) is still
    legitimate. See spec §5."""
    got = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", funding_only=True,
    )
    assert got == ()


def test_available_for_preserves_declaration_order():
    got = available_for(
        DEFAULT_POST_TYPES, gate=None, roles_by_agent=MESH_ROLES,
        self_id="gill", funding_only=False,
    )
    declared = [s.name for s in DEFAULT_POST_TYPES if s.name in _by_name(got)]
    assert [s.name for s in got] == declared


# --- parse_post_types -------------------------------------------------------

def test_parse_none_yields_the_defaults():
    assert parse_post_types(None, role="pi_lab") == DEFAULT_POST_TYPES


def test_parse_reads_name_and_targets():
    got = parse_post_types(
        [{"name": "opportunity_assessment"},
         {"name": "funding_collab", "targets": ["pi_lab"]}],
        role="scout_hub",
    )
    assert _by_name(got) == {"opportunity_assessment", "funding_collab"}
    assert dict((s.name, s.targets) for s in got)["funding_collab"] == frozenset({"pi_lab"})


def test_parse_drops_an_unknown_name_and_keeps_the_rest(caplog):
    got = parse_post_types(
        [{"name": "paper"}, {"name": "not_a_real_type"}], role="pi_lab"
    )
    assert _by_name(got) == {"paper"}
    assert "not_a_real_type" in caplog.text


def test_parse_drops_a_malformed_entry_and_keeps_the_rest(caplog):
    got = parse_post_types(["paper", {"name": "help_wanted"}, {}], role="pi_lab")
    assert _by_name(got) == {"help_wanted"}
    assert caplog.text


def test_parse_warns_when_targets_names_a_role_that_cannot_exist(caplog):
    """A typo'd role means the type is silently never offered — say so at load."""
    got = parse_post_types(
        [{"name": "pitch", "targets": ["scout_hubb"]}], role="pi_lab"
    )
    assert _by_name(got) == {"pitch"}
    assert "scout_hubb" in caplog.text


def test_parse_of_a_non_list_yields_the_defaults(caplog):
    assert parse_post_types("paper", role="pi_lab") == DEFAULT_POST_TYPES
    assert caplog.text


def test_parse_targets_override_replaces_the_canonical_default():
    got = parse_post_types([{"name": "pitch", "targets": []}], role="pi_lab")
    assert got[0].targets == frozenset()


# --- render_menu ------------------------------------------------------------

def test_render_menu_names_every_available_type_with_its_emoji():
    specs = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", funding_only=False,
    )
    out = render_menu(
        specs, gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill", bot_names=BOT_NAMES,
    )
    for name in ("paper", "help_wanted", "introduction", "pitch"):
        assert CANONICAL[name].emoji in out
        assert name in out
    assert "idea_crosslab" not in out


def test_render_menu_names_the_reachable_agent_for_an_addressed_type():
    specs = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", funding_only=False,
    )
    out = render_menu(
        specs, gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill", bot_names=BOT_NAMES,
    )
    assert "BlackbirdBot" in out
    assert "blackbird" in out


def test_render_menu_marks_a_broadcast_type_as_addressing_no_one():
    out = render_menu(
        [CANONICAL["paper"]], gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "no one" in out.lower() or "broadcast" in out.lower()


def test_render_menu_of_an_empty_set_says_so_and_points_at_reply_or_skip():
    out = render_menu(
        [], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill", bot_names=BOT_NAMES,
    )
    assert out.strip()
    low = out.lower()
    assert "no new top-level post type" in low
    assert "reply" in low and "skip" in low


def test_render_menu_never_returns_an_empty_string():
    """A blank menu would leave the prompt claiming a list exists with nothing in
    it, which reads as a rendering bug to the model."""
    for specs in ([], list(DEFAULT_POST_TYPES)):
        out = render_menu(
            specs, gate=None, roles_by_agent=MESH_ROLES, self_id="gill", bot_names=BOT_NAMES,
        )
        assert out.strip()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_post_types.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'src.agent.post_types'`

- [ ] **Step 3: Write the implementation**

Create `src/agent/post_types.py`:

```python
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

    Never returns an empty string: an empty set renders an explicit statement
    plus the two actions that remain valid, because a blank menu under a heading
    promising a list reads to the model as a rendering bug.
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
        reachable = sorted(
            eligible_targets(
                s, gate=gate, roles_by_agent=roles_by_agent, self_id=self_id
            )
        )
        named = ", ".join(f"`{aid}` (@{bot_names.get(aid, aid + 'Bot')})" for aid in reachable)
        lines.append(
            head + f" Set `tagged_agent` to exactly one of: {named}. "
            "Tagging anyone else gets the post rejected."
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_post_types.py -q`
Expected: PASS, all tests.

- [ ] **Step 5: Lint**

Run: `.venv-test/bin/python -m ruff check tests/unit/test_post_types.py src/agent/post_types.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/agent/post_types.py tests/unit/test_post_types.py
git commit -m "feat(post_types): the canonical vocabulary and the role+topology filter

A post type declares the counterparty roles it addresses; availability is
computed against the acting agent's live cohort gate. Gate None means no
filtering, which is what keeps a mesh deployment unaffected.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire `post_types` into `role.toml`

**Files:**
- Modify: `src/agent/roles.py` (RoleSpec at `:30-38`, `load_role` at `:76-116`)
- Modify: `prompts/roles/scout_hub/role.toml`
- Test: `tests/unit/test_roles.py` (append)

**Interfaces:**
- Consumes: `src.agent.post_types.parse_post_types`, `DEFAULT_POST_TYPES`, `PostTypeSpec` (Task 1).
- Produces: `RoleSpec.post_types: tuple[PostTypeSpec, ...]`, populated for every role.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_roles.py`:

```python
def test_missing_manifest_yields_default_post_types():
    from src.agent.post_types import DEFAULT_POST_TYPES

    spec = load_role("definitely_not_a_role_dir")
    assert spec.post_types == DEFAULT_POST_TYPES


def test_manifest_post_types_are_parsed(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "widget",
        'label = "Widget"\n'
        '[[post_types]]\nname = "paper"\n'
        '[[post_types]]\nname = "pitch"\ntargets = ["scout_hub"]\n',
    )
    spec = load_role("widget")
    assert [s.name for s in spec.post_types] == ["paper", "pitch"]
    assert dict((s.name, s.targets) for s in spec.post_types)["pitch"] == frozenset(
        {"scout_hub"}
    )


def test_manifest_unknown_post_type_is_dropped(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _write_role(
        tmp_path, monkeypatch, "widget",
        'label = "Widget"\n'
        '[[post_types]]\nname = "paper"\n'
        '[[post_types]]\nname = "nonsense"\n',
    )
    spec = load_role("widget")
    assert [s.name for s in spec.post_types] == ["paper"]
    assert "nonsense" in caplog.text


def test_malformed_toml_still_yields_default_post_types(tmp_path, monkeypatch):
    from src.agent.post_types import DEFAULT_POST_TYPES

    _write_role(tmp_path, monkeypatch, "broken", "label = = =\n")
    assert load_role("broken").post_types == DEFAULT_POST_TYPES


def test_scout_hub_declares_its_two_post_types():
    spec = load_role("scout_hub")
    assert {s.name for s in spec.post_types} == {
        "opportunity_assessment", "funding_collab",
    }
    assert dict((s.name, s.targets) for s in spec.post_types)[
        "funding_collab"
    ] == frozenset({"pi_lab"})
    assert dict((s.name, s.targets) for s in spec.post_types)[
        "opportunity_assessment"
    ] == frozenset()


def test_scout_hub_cannot_post_a_cross_lab_idea():
    """The hub is not a party to the science — brokering is explicitly not its
    job (prompts/roles/scout_hub/agent-system.md)."""
    assert "idea_crosslab" not in {s.name for s in load_role("scout_hub").post_types}
    assert "pitch" not in {s.name for s in load_role("scout_hub").post_types}
```

These use the file's existing helper, `_write_role(tmp_path, monkeypatch, name, toml_text)` (`tests/unit/test_roles.py:7`), which patches `roles.ROLES_DIR` to `tmp_path / "roles"` itself — do **not** add a second `monkeypatch.setattr` for `ROLES_DIR`, and do not re-import `load_role` or `logging`; both are already imported at the top of the file.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -q -k post_type`
Expected: FAIL — `AttributeError: 'RoleSpec' object has no attribute 'post_types'`

- [ ] **Step 3: Add the field and the parsing**

In `src/agent/roles.py`, add the import near the top (after `from pathlib import Path`):

```python
from src.agent.post_types import DEFAULT_POST_TYPES, PostTypeSpec, parse_post_types
```

Add the field to `RoleSpec` (after `calls_per_load_per_window`):

```python
    # Layer 1 of post-type gating: what this role may emit as a NEW top-level
    # post. Defaults to DEFAULT_POST_TYPES, which IS the pi_lab set (pi_lab has
    # no role.toml — the absence of overrides is pi_lab).
    post_types: tuple[PostTypeSpec, ...] = DEFAULT_POST_TYPES
```

`RoleSpec` is `@dataclass(frozen=True)` and `post_types` has a default, so it must come after every other defaulted field. `calls_per_load_per_window` already has a default, so appending is correct.

In `load_role`, both early returns must now name it explicitly (they already pass `tools=DEFAULT_TOOLS`; the `post_types` default applies automatically, so **no change is needed to the two early returns**). Add the parse before the final `return RoleSpec(`:

```python
    post_types = parse_post_types(data.get("post_types"), role=name)
```

and extend the final return:

```python
    return RoleSpec(
        name=name, label=label, tools=tools,
        calls_per_load_per_window=rate, post_types=post_types,
    )
```

- [ ] **Step 4: Declare the hub's types**

Replace `prompts/roles/scout_hub/role.toml` with the reviewed draft:

```bash
cp docs/specs/2026-08-06-post-type-gating-prompts-draft/roles/scout_hub/role.toml \
   prompts/roles/scout_hub/role.toml
```

- [ ] **Step 5: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py tests/unit/test_post_types.py tests/unit/test_tool_gating.py -q`
Expected: PASS. `test_tool_gating.py` is included because it also calls `load_role`; a broken `RoleSpec` signature would show up there.

- [ ] **Step 6: Commit**

```bash
git add src/agent/roles.py prompts/roles/scout_hub/role.toml tests/unit/test_roles.py
git commit -m "feat(roles): parse a post_types allow-list from role.toml

Mirrors the existing tools key, with the same never-raises degradation: an
unknown type or malformed entry is dropped with a WARNING and the rest kept.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Fix the lab-directory ordering

This task is independent of Tasks 1, 2, 4, 5 and can be done in any order relative to them. It is the change that makes the *existing* cohort filter run at all.

**Files:**
- Modify: `src/agent/simulation.py` (`start()` `:508`/`:533`; `_sync_roster_from_db` `:4500`/`:4502` and `:4545`/`:4555`; `_recompute_allowed_sender_ids` tail `:4641-4667`)
- Test: `tests/unit/test_lab_directory_ordering.py` *(new)*

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_lab_directory_ordering.py`:

```python
"""The lab directory must be gate-scoped in the order production builds it.

src/agent/simulation.py:3615 filters the directory by allowed_sender_ids, but
start() built it at :508 and only computed the gate at :533 — so every gate was
still None and the filter no-opped. On a stable roster it was never rebuilt.

Measured in production: gill's phase-5 system prompt named 51 labs it could not
reach, "Blackbird" (its one reachable partner) appeared nowhere, and the
directory was 69% of a 67 KB prompt.

The pre-existing test (tests/unit/test_simulation_logic.py) sets the gates by
hand BEFORE calling the builder, which is why it passed throughout.
"""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _agent(aid: str, pub: str, role: str = "pi_lab") -> Agent:
    a = Agent(aid, f"{aid.capitalize()}Bot", f"{aid.upper()} PI", role=role)
    a._public_profile = f"# {aid} Lab\n\n## Recent Publications\n- {pub}\n"
    return a


def test_a_fresh_agents_gate_is_none():
    """The precondition that made the ordering matter."""
    assert _agent("a", "paper A").allowed_sender_ids is None


def test_directory_is_gate_scoped_after_the_gate_is_applied():
    """Whatever the internal ordering, once gates exist the directory must agree
    with them. This is the invariant; it does not care how it is achieved."""
    a, b, c = _agent("a", "paper A"), _agent("b", "paper B"), _agent("c", "paper C")
    eng = SimulationEngine(agents=[a, b, c], slack_clients={})

    a.allowed_sender_ids = {"a", "b"}
    b.allowed_sender_ids = {"a", "b"}
    c.allowed_sender_ids = {"c"}
    eng.refresh_lab_directories()

    assert "paper B" in (a._lab_directory or "")
    assert "paper C" not in (a._lab_directory or "")
    assert c._lab_directory is None


def test_refresh_is_idempotent():
    a, b = _agent("a", "paper A"), _agent("b", "paper B")
    a.allowed_sender_ids = {"a", "b"}
    b.allowed_sender_ids = {"a", "b"}
    eng = SimulationEngine(agents=[a, b], slack_clients={})
    eng.refresh_lab_directories()
    first = a._lab_directory
    eng.refresh_lab_directories()
    assert a._lab_directory == first


def test_tightening_a_gate_then_refreshing_removes_the_stale_lab():
    """The gate-change rebuild: a topology edit mid-run must not leave an agent
    primed with a lab it can no longer reach."""
    a, b = _agent("a", "paper A"), _agent("b", "paper B")
    a.allowed_sender_ids = {"a", "b"}
    b.allowed_sender_ids = {"a", "b"}
    eng = SimulationEngine(agents=[a, b], slack_clients={})
    eng.refresh_lab_directories()
    assert "paper B" in (a._lab_directory or "")

    a.allowed_sender_ids = {"a"}
    eng.refresh_lab_directories()
    assert a._lab_directory is None


def test_gate_off_still_lists_every_other_lab():
    """Mesh behaviour is unchanged: gate None means no filtering."""
    a, b = _agent("a", "paper A"), _agent("b", "paper B")
    eng = SimulationEngine(agents=[a, b], slack_clients={})
    eng.refresh_lab_directories()
    assert "paper B" in (a._lab_directory or "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_lab_directory_ordering.py -q`
Expected: FAIL — `AttributeError: 'SimulationEngine' object has no attribute 'refresh_lab_directories'`

- [ ] **Step 3: Add the public alias and fix the three call sites**

In `src/agent/simulation.py`, immediately after the `_build_lab_directories` definition, add:

```python
    # Public alias. `_build_lab_directories` is called from three places whose
    # ordering relative to the cohort gate is the whole bug this name documents:
    # it must run AFTER _recompute_allowed_sender_ids, never before.
    def refresh_lab_directories(self) -> None:
        """Rebuild every agent's lab directory against its CURRENT gate."""
        self._build_lab_directories()
```

In `start()`, delete the call at `:508` and add one after `:533`:

```python
        await self._recompute_allowed_sender_ids()
        # AFTER the gate, never before: the filter inside reads
        # agent.allowed_sender_ids, which is None until the line above runs.
        self.refresh_lab_directories()
```

In `_sync_roster_from_db`, the no-membership-change branch currently reads:

```python
            if not to_remove and not to_add:
                if role_changed:
                    self._build_lab_directories()
                await self._recompute_allowed_sender_ids()
                return
```

Replace with:

```python
            if not to_remove and not to_add:
                # Recompute the gate FIRST; the directory rebuild below reads it.
                # _recompute_allowed_sender_ids refreshes the directory itself
                # whenever the gate signature moves, so only a role change needs
                # an unconditional rebuild here.
                await self._recompute_allowed_sender_ids()
                if role_changed:
                    self.refresh_lab_directories()
                return
```

In the membership-change path, delete the `self._build_lab_directories()` at `:4545` and let the existing `await self._recompute_allowed_sender_ids()` at `:4555` handle it — then add one line after it:

```python
            await self._recompute_allowed_sender_ids()
            self.refresh_lab_directories()
```

- [ ] **Step 4: Rebuild the directory whenever the gate moves**

In `_recompute_allowed_sender_ids`, the early returns set every gate to `None` and must refresh too, or an agent keeps a directory scoped to a gate that no longer applies. Add `self.refresh_lab_directories()` immediately after each `self._disable_all_gates()` call, and once more at the end of the method. The tail becomes:

```python
        self._apply_cohort_gate_to_state()
        # The directory is derived from the gate, so it is refreshed on the same
        # cadence. Cheap: it re-reads in-memory profiles, no I/O.
        self.refresh_lab_directories()
        if topology_changed:
            await self._record_topology_snapshot()
```

- [ ] **Step 5: Run the new test and the two that guard the old behaviour**

Run: `.venv-test/bin/python -m pytest tests/unit/test_lab_directory_ordering.py tests/unit/test_simulation_logic.py tests/unit/test_roster_sync.py -q`
Expected: PASS. `test_roster_sync.py:108` stubs `_build_lab_directories` out with a lambda; because `refresh_lab_directories` delegates to it, that stub still works. If it fails, update the stub to patch `refresh_lab_directories` instead.

- [ ] **Step 6: Verify the production-order bug is actually gone**

Run:

```bash
.venv-test/bin/python - <<'PY'
import asyncio, inspect
from src.agent.simulation import SimulationEngine
src = inspect.getsource(SimulationEngine.start)
build = src.index("refresh_lab_directories")
gate  = src.index("_recompute_allowed_sender_ids")
print("start(): gate before directory =", gate < build)
assert gate < build, "start() still builds the directory before the gate exists"
PY
```

Expected: `start(): gate before directory = True`

- [ ] **Step 7: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_lab_directory_ordering.py
git commit -m "fix(cohort): build the lab directory after the gate, not before

The filter at _build_lab_directories has always been correct, but start() ran
it before _recompute_allowed_sender_ids, when every gate is still None — so it
no-opped, and on a stable roster it never ran again. Production evidence: a
spoke's phase-5 prompt named 51 unreachable labs, omitted its one reachable
partner entirely, and the directory was 69% of the prompt.

The directory is derived from the gate, so it is now refreshed on the gate's
own cadence, including the paths that disable gating.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Substitute `{post_type_menu}` in the phase-5 prompt

**Files:**
- Modify: `src/agent/agent.py` (`build_phase5_prompt` signature `:511-521`; token block `:636-639`)
- Test: `tests/unit/test_agent_prompts.py` (append)

**Interfaces:**
- Consumes: `src.agent.post_types.DEFAULT_POST_TYPES`, `render_menu` (Task 1).
- Produces: `Agent.build_phase5_prompt(..., post_type_menu: str | None = None)`. When `None`, a default menu is rendered from `DEFAULT_POST_TYPES` with no filtering, so the token is **always** consumed and no caller can leak it.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_agent_prompts.py`:

```python
def test_phase5_menu_token_is_always_substituted():
    """No caller may leak the raw token into a prompt. prompts/ is bind-mounted
    and re-read per call while src/ is baked into the agent image, so a template
    that ships ahead of its renderer would put `{post_type_menu}` in front of a
    live model."""
    from src.agent.agent import Agent

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt()
    assert "{post_type_menu}" not in messages[0]["content"]


def test_phase5_menu_defaults_to_the_unfiltered_pi_lab_set():
    from src.agent.agent import Agent
    from src.agent.post_types import DEFAULT_POST_TYPES

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt()
    content = messages[0]["content"]
    for spec in DEFAULT_POST_TYPES:
        assert spec.name in content


def test_phase5_menu_uses_the_caller_supplied_text_when_given():
    from src.agent.agent import Agent

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt(post_type_menu="- ONLY THIS ONE")
    content = messages[0]["content"]
    assert "- ONLY THIS ONE" in content
    assert "idea_crosslab" not in content


def test_phase5_menu_survives_funding_only_surgery():
    """funding_only strips Option C but the menu section sits above ## Instructions
    and must still render — the engine narrows its contents instead."""
    from src.agent.agent import Agent

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt(
        funding_only=True, post_type_menu="- **`funding_collab`** — only this"
    )
    content = messages[0]["content"]
    assert "- **`funding_collab`** — only this" in content
    assert "### Option C: Make a new top-level post" not in content
    assert "### Option D: Skip this turn" in content
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_prompts.py -q -k menu`
Expected: FAIL — `TypeError: build_phase5_prompt() got an unexpected keyword argument 'post_type_menu'`

(The first test, `test_phase5_menu_token_is_always_substituted`, will pass before the change and after — the token does not exist in the template yet. It starts guarding once Task 6 installs the template. Keep it.)

- [ ] **Step 3: Implement**

In `src/agent/agent.py`, add to `build_phase5_prompt`'s signature after `channel_id`:

```python
        post_type_menu: str | None = None,
```

Add to the docstring:

```
        post_type_menu: pre-rendered {post_type_menu} block. The engine computes
            it from the role's allow-list filtered by the live cohort gate, and
            enforces the SAME set when the response comes back. None renders the
            unfiltered pi_lab defaults — used by direct callers and tests that
            have no topology to apply.
```

Add the import at the top of the module:

```python
from src.agent.post_types import DEFAULT_POST_TYPES, render_menu
```

Extend the token block at `:636-639`:

```python
        prompt_text = prompt_text.replace("{prior_conversations}", prior_text)
        if post_type_menu is None:
            # No topology supplied — render the defaults unfiltered, matching the
            # "gate is None means no filtering" rule. Never leave the token raw.
            post_type_menu = render_menu(
                DEFAULT_POST_TYPES, gate=None, roles_by_agent={},
                self_id=self.agent_id, bot_names={},
            )
        prompt_text = prompt_text.replace("{post_type_menu}", post_type_menu)
```

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_prompts.py tests/unit/test_post_types.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/agent.py tests/unit/test_agent_prompts.py
git commit -m "feat(agent): substitute {post_type_menu} in the phase-5 prompt

Defaults to the unfiltered pi_lab set so the token is always consumed — a raw
token reaching a live prompt is the specific failure this guards, since prompts/
is bind-mounted while src/ is baked into the agent image.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Compute the menu and enforce it

**Files:**
- Modify: `src/agent/simulation.py` (phase-5 handler: `build_phase5_prompt` call at `:2111`; new-post branch at `:2323-2380`)
- Test: `tests/unit/test_post_type_enforcement.py` *(new)*

**Interfaces:**
- Consumes: `available_for`, `render_menu`, `eligible_targets` (Task 1); `load_role(...).post_types` (Task 2); `build_phase5_prompt(post_type_menu=...)` (Task 4).
- Produces: `SimulationEngine._available_post_types(agent, funding_only)` returning `tuple[PostTypeSpec, ...]`, and `SimulationEngine._post_type_rejection(agent, post_type, tagged_agent, available)` returning `str | None` — the reason, or `None` when the post is allowed.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_post_type_enforcement.py`:

```python
"""Layers 1-3: a new top-level post must use a post type its role and topology
allow, and may only tag an agent that type can address.

The failure being prevented, measured over one production run: 146 of 146
phase-5 posts that declared a tagged_agent named an agent the poster's cohort
gate forbade. The mention was stripped and the post published anyway, leaving
259 :bulb: posts with a 0.8% reply rate against 9.0% for :newspaper: papers.
"""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine(*agents):
    return SimulationEngine(agents=list(agents), slack_clients={})


def _spoke(aid="gill"):
    return Agent(aid, f"{aid.capitalize()}Bot", f"{aid.upper()} PI", role="pi_lab")


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _star():
    """One spoke, the hub, and a second spoke the first cannot reach."""
    gill, hub, pearce = _spoke("gill"), _hub(), _spoke("pearce")
    gill.allowed_sender_ids = {"gill", "blackbird"}
    hub.allowed_sender_ids = {"gill", "blackbird", "pearce"}
    pearce.allowed_sender_ids = {"pearce", "blackbird"}
    return _engine(gill, hub, pearce), gill, hub, pearce


# --- the available set ------------------------------------------------------

def test_star_spoke_cannot_use_idea_crosslab():
    eng, gill, _, _ = _star()
    names = {s.name for s in eng._available_post_types(gill, funding_only=False)}
    assert "idea_crosslab" not in names
    assert "funding_collab" not in names


def test_star_spoke_can_pitch_to_the_hub():
    eng, gill, _, _ = _star()
    names = {s.name for s in eng._available_post_types(gill, funding_only=False)}
    assert "pitch" in names


def test_star_spoke_keeps_every_broadcast_type():
    eng, gill, _, _ = _star()
    names = {s.name for s in eng._available_post_types(gill, funding_only=False)}
    assert {"paper", "help_wanted", "introduction"} <= names


def test_mesh_spoke_keeps_idea_crosslab_and_loses_pitch():
    gill, pearce = _spoke("gill"), _spoke("pearce")
    eng = _engine(gill, pearce)  # gates stay None
    names = {s.name for s in eng._available_post_types(gill, funding_only=False)}
    assert "idea_crosslab" in names
    assert "pitch" not in names


def test_hub_may_only_post_its_assessment():
    eng, _, hub, _ = _star()
    names = {s.name for s in eng._available_post_types(hub, funding_only=False)}
    assert "opportunity_assessment" in names
    assert "idea_crosslab" not in names
    assert "paper" not in names


def test_funding_only_in_the_star_is_empty_but_that_is_not_a_skip():
    eng, gill, _, _ = _star()
    assert eng._available_post_types(gill, funding_only=True) == ()


# --- rejection -------------------------------------------------------------

def test_layer1_rejects_a_type_the_role_never_declared():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    reason = eng._post_type_rejection(gill, "opportunity_assessment", None, avail)
    assert reason is not None
    assert "opportunity_assessment" in reason


def test_layer2_rejects_a_type_with_no_reachable_counterparty():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    reason = eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail)
    assert reason is not None


def test_layer3_rejects_the_exact_production_case():
    """{"post_type": "idea_crosslab", "tagged_agent": "pearce"} from markham."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail) is not None


def test_layer3_rejects_a_tag_toward_an_unreachable_agent_on_an_allowed_type():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    reason = eng._post_type_rejection(gill, "pitch", "pearce", avail)
    assert reason is not None
    assert "pearce" in reason


def test_layer3_rejects_a_tag_on_a_broadcast_type():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    reason = eng._post_type_rejection(gill, "paper", "blackbird", avail)
    assert reason is not None


def test_layer3_rejects_an_unknown_agent_id():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "pitch", "nobody", avail) is not None


def test_a_valid_pitch_at_the_hub_is_accepted():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "pitch", "blackbird", avail) is None


def test_a_valid_broadcast_with_no_tag_is_accepted():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "paper", None, avail) is None


def test_an_empty_post_type_is_rejected_for_a_new_post():
    """post_type defaults to "" when the model omits it."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "", None, avail) is not None


def test_gate_off_accepts_everything_the_role_declared():
    """Layers 2 and 3 must be inert in a mesh so org1 is unaffected."""
    gill, pearce = _spoke("gill"), _spoke("pearce")
    eng = _engine(gill, pearce)
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail) is None
    assert eng._post_type_rejection(gill, "paper", None, avail) is None


def test_gate_off_still_rejects_a_type_the_role_never_declared():
    gill = _spoke("gill")
    eng = _engine(gill)
    avail = eng._available_post_types(gill, funding_only=False)
    assert eng._post_type_rejection(gill, "opportunity_assessment", None, avail) is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_post_type_enforcement.py -q`
Expected: FAIL — `AttributeError: 'SimulationEngine' object has no attribute '_available_post_types'`

- [ ] **Step 3: Add the two helpers**

In `src/agent/simulation.py`, add these methods next to `_strip_disallowed_tags`. Add the import at the top: `from src.agent.post_types import available_for, eligible_targets, render_menu`.

```python
    def _roles_by_agent(self) -> dict[str, str]:
        """Live roster agent_id -> role. Agents absent from this map (e.g.
        ``grantbot``, which has cohort memberships but no AgentRegistry row)
        match no post type's ``targets``."""
        return {aid: a.role for aid, a in self.agents.items()}

    def _available_post_types(self, agent: "Agent", funding_only: bool) -> tuple:
        """Layer 1 ∩ layer 2: what this agent may post as a NEW top-level post.

        The SAME tuple is rendered into the prompt and used to judge the
        response, so the menu and the gate cannot disagree.
        """
        return available_for(
            load_role(agent.role).post_types,
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
            funding_only=funding_only,
        )

    def _post_type_rejection(
        self,
        agent: "Agent",
        post_type: str,
        tagged_agent: str | None,
        available: tuple,
    ) -> str | None:
        """Why this new top-level post must not be published, or None.

        Applies only to ``action: "new_post"`` — a reply is never gated here.
        """
        by_name = {s.name: s for s in available}
        spec = by_name.get(post_type)
        if spec is None:
            return (
                f"post_type {post_type!r} is not available to role "
                f"{agent.role!r} with this topology "
                f"(available: {sorted(by_name) or 'none'})"
            )
        if not spec.targets:
            if tagged_agent:
                return (
                    f"post_type {post_type!r} addresses no one, but "
                    f"tagged_agent={tagged_agent!r} was set"
                )
            return None
        allowed = eligible_targets(
            spec,
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
        )
        if not tagged_agent:
            return (
                f"post_type {post_type!r} must address one of "
                f"{sorted(allowed)}, but tagged_agent was null"
            )
        if tagged_agent not in allowed:
            return (
                f"tagged_agent={tagged_agent!r} is not reachable for post_type "
                f"{post_type!r} (allowed: {sorted(allowed)})"
            )
        return None
```

- [ ] **Step 4: Run the helper tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_post_type_enforcement.py -q`
Expected: PASS

- [ ] **Step 5: Pass the menu into the prompt**

At the `build_phase5_prompt` call (`:2111`), compute the set first and pass the rendered menu. Insert directly above the call:

```python
        available_types = self._available_post_types(agent, funding_only)
        post_type_menu = render_menu(
            available_types,
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
            bot_names={aid: a.bot_name for aid, a in self.agents.items()},
        )
```

and add `post_type_menu=post_type_menu,` to the call's keyword arguments.

- [ ] **Step 6: Enforce in the new-post branch**

In the `else:` branch at `:2323`, insert before `posted = await self._post_message(...)`:

```python
            else:
                # New top-level post. Layers 1-3, against the SAME set that was
                # rendered into the prompt above. Reject rather than strip-and-
                # publish: a mention stripped out of an addressed post leaves a
                # dangling ask no one can answer (259 such posts, 0.8% reply
                # rate). WARNING, not DEBUG — the cohort strip was logged at
                # DEBUG and 200 of them produced no operator-visible signal.
                rejection = self._post_type_rejection(
                    agent,
                    post_type,
                    action_data.get("tagged_agent"),
                    available_types,
                )
                if rejection is not None:
                    logger.warning(
                        "[%s] Phase 5: rejected new post in #%s — %s",
                        agent.agent_id, channel, rejection,
                    )
                    agent.state.consecutive_phase5_skips += 1
                    return
                # New top-level post
                posted = await self._post_message(agent.agent_id, channel, message_text)
```

`consecutive_phase5_skips` is zeroed at `:2180`, before this branch, so a rejection must re-increment it to keep the existing backoff working.

- [ ] **Step 7: Write the integration test that nothing is posted**

Add `import pytest` as the first import in `tests/unit/test_post_type_enforcement.py`
(it is unused until now, and `ruff` must report zero findings on `tests/`), then append:

```python
@pytest.mark.asyncio
async def test_a_rejected_post_reaches_neither_slack_nor_the_log(monkeypatch):
    """The assertion that makes "reject" honest rather than cosmetic."""
    eng, gill, _, _ = _star()

    calls = []

    async def _spy(agent_id, channel, text, thread_ts=None):
        calls.append((agent_id, channel, text))
        return "1234.5678"

    monkeypatch.setattr(eng, "_post_message", _spy)

    before = gill.message_count
    avail = eng._available_post_types(gill, funding_only=False)
    rejection = eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail)

    assert rejection is not None
    assert calls == []
    assert gill.message_count == before
```

- [ ] **Step 8: Run the full affected suite**

Run:

```bash
.venv-test/bin/python -m pytest tests/unit/test_post_type_enforcement.py \
  tests/unit/test_post_types.py tests/unit/test_roles.py \
  tests/unit/test_simulation_logic.py tests/unit/test_agent_prompts.py -q
```

Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/agent/simulation.py tests/unit/test_post_type_enforcement.py
git commit -m "feat(phase5): enforce role+topology post-type gating on new posts

The menu rendered into the prompt and the set used to judge the response are
one value, so they cannot drift. A disallowed type or an unreachable
tagged_agent is rejected at WARNING and publishes nothing, instead of having
its mention stripped and the mutilated post published anyway.

Applies to action:\"new_post\" only; replies are untouched. Inert when the
cohort gate is off, so a mesh deployment is unaffected.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Install the prompts and regenerate the snapshots

**Files:**
- Modify: `prompts/identity.md`, `prompts/agent-system.md`, `prompts/phase5-new-post.md`, `prompts/roles/scout_hub/phase5-new-post.md`, `prompts/roles/scout_hub/agent-system.md`
- Modify: `src/agent/agent.py:753` (`_DEFAULT_IDENTITY`)
- Modify: `tests/unit/test_roles.py:178` and `:370` (token lists)
- Modify: `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` (8 of 9 snapshots)

**Interfaces:**
- Consumes: `{post_type_menu}` substitution (Task 4).
- Produces: nothing other tasks depend on.

> **Ordering constraint.** This task must land in the same change as Task 4, or `prompts/` (bind-mounted, re-read per call) would ship a template carrying `{post_type_menu}` to a renderer (baked into the image) that cannot substitute it.

- [ ] **Step 1: Add the token to both pins first, so a miss fails loudly**

In `tests/unit/test_roles.py`, add to the `leftover_tokens` list at `:177-182`:

```python
        "{post_type_menu}",
```

and to the renderer-anchor tuple at `:366-372`:

```python
        "{post_type_menu}",
```

- [ ] **Step 2: Add the pi_lab pin that does not exist yet**

Measured: `test_roles.py:160-227` pins the **scout_hub** override only. The global `pi_lab` template's tokens and `funding_only` surgeries are pinned nowhere. Append to `tests/unit/test_roles.py`:

```python
def test_pi_lab_phase5_template_renders_in_both_modes():
    """The global template's tokens and funding_only surgeries were pinned
    nowhere — only the scout_hub override was. This rewrite is exactly the kind
    of change that needs the pin."""
    from src.agent.agent import Agent

    agent = Agent("gill", "GillBot", "Gill PI")  # role defaults to pi_lab

    for funding_only in (False, True):
        _, messages = agent.build_phase5_prompt(
            recent_posts=[{"channel": "general", "content_snippet": "an old post"}],
            foa_contexts={},
            thread_foa_contexts={"RFA-AI-27-019": "Example FOA text"},
            prior_threads={
                "pearce": [
                    {"channel": "general", "outcome": "no_proposal", "summary": "n/a"}
                ]
            },
            funding_only=funding_only,
            funding_thread_summaries={},
        )
        content = messages[0]["content"]
        for token in (
            "{interesting_posts}", "{subscribed_channels}", "{your_recent_posts}",
            "{prior_conversations}", "{post_type_menu}",
        ):
            assert token not in content, (
                f"leftover token {token!r} (funding_only={funding_only})"
            )

    _, fo = agent.build_phase5_prompt(funding_only=True)
    fo_content = fo[0]["content"]
    assert "### Option C: Make a new top-level post" not in fo_content
    assert "### Option D: Skip this turn" in fo_content
    assert "## Your subscribed channels" not in fo_content
    assert "## Your recent posts" not in fo_content
    assert "## Prior conversations with other labs" not in fo_content

    _, normal = agent.build_phase5_prompt()
    assert "### Option C: Make a new top-level post" in normal[0]["content"]
```

- [ ] **Step 3: Install the reviewed drafts**

```bash
DRAFT=docs/specs/2026-08-06-post-type-gating-prompts-draft
cp $DRAFT/identity.md                          prompts/identity.md
cp $DRAFT/agent-system.md                      prompts/agent-system.md
cp $DRAFT/phase5-new-post.md                   prompts/phase5-new-post.md
cp $DRAFT/roles/scout_hub/phase5-new-post.md   prompts/roles/scout_hub/phase5-new-post.md
cp $DRAFT/roles/scout_hub/agent-system.md      prompts/roles/scout_hub/agent-system.md
```

- [ ] **Step 4: Verify `identity.md` kept its missing trailing newline**

```bash
tail -c 1 prompts/identity.md | xxd | grep -q '0a' \
  && echo "BROKEN: trailing newline present" \
  || echo "OK: no trailing newline"
```

Expected: `OK: no trailing newline`

- [ ] **Step 5: Update the code fallback to match byte-for-byte**

In `src/agent/agent.py`, change `_DEFAULT_IDENTITY` (`:753`) to drop `at Scripps Research`:

```python
_DEFAULT_IDENTITY = """## Your Identity
You are **{bot_name}**, the AI agent representing the {pi_name} lab.
Your agent ID is "{agent_id}". When communicating, represent your lab professionally."""
```

Verify it matches the file exactly:

```bash
.venv-test/bin/python - <<'PY'
from pathlib import Path
from src.agent.agent import _DEFAULT_IDENTITY
disk = Path("prompts/identity.md").read_text(encoding="utf-8")
assert disk == _DEFAULT_IDENTITY, "fallback and prompts/identity.md have diverged"
print("identity fallback matches the file byte-for-byte")
PY
```

Expected: `identity fallback matches the file byte-for-byte`

- [ ] **Step 6: Confirm the surgeries and anchors still match the installed files**

```bash
.venv-test/bin/python - <<'PY'
import re
from pathlib import Path
pats = [r"## Your subscribed channels\n.*?\n\{subscribed_channels\}\n",
        r"## Your recent posts\n.*?\{your_recent_posts\}\n",
        r"## Prior conversations with other labs\n.*?\{prior_conversations\}\n",
        r"### Option C: Make a new top-level post\n.*?(?=### Option D:)"]
intro = ("You have the opportunity to either reply to an interesting post or make a new top-level\n"
         "post in one of your subscribed channels.")
for p in ("prompts/phase5-new-post.md", "prompts/roles/scout_hub/phase5-new-post.md"):
    t = Path(p).read_text()
    assert all(re.search(x, t, re.DOTALL) for x in pats), f"{p}: a funding_only surgery broke"
    assert intro in t, f"{p}: the intro replacement target broke"
    assert "{post_type_menu}" in t, f"{p}: menu token missing"
print("all surgeries, the intro target, and the menu token are present in both templates")
PY
```

Expected: the success line.

- [ ] **Step 7: Run the non-snapshot tests first**

Run: `.venv-test/bin/python -m pytest tests/unit -q`
Expected: PASS. Fix any failure here **before** touching a snapshot — a snapshot is the last thing to move, never the first.

- [ ] **Step 8: Confirm exactly which snapshots fail, and that it is the expected eight**

Run: `.venv-test/bin/python -m pytest tests/characterization/test_agent_turn_gm.py -q`

Expected: 8 failures, in `test_scan_system_prompt_gm`, `test_system_prompt_public_vs_private_gm`, `test_thread_reply_system_prompt_gm`, `test_phase2_scan_prompt_flags_self_authored_gm`, `test_phase4_prompt_phase_progression_gm`, `test_phase4_prompt_pi_context_and_funding_gm`, `test_reply_turn_composes_prompt_and_posts_gm`, `test_phase5_prompt_gm`. `test_decide_phase_parses_scripted_json_gm` must still pass.

**If any other test fails, or if `test_decide_phase_parses_scripted_json_gm` fails, STOP** — something beyond the prompt text moved.

- [ ] **Step 9: Regenerate those eight, then review the diff line by line**

```bash
.venv-test/bin/python -m pytest tests/characterization/test_agent_turn_gm.py --snapshot-update -q
git diff tests/characterization/__snapshots__/test_agent_turn_gm.ambr > /tmp/snap.diff
wc -l /tmp/snap.diff
```

Now read `/tmp/snap.diff` in full and confirm **every** hunk is text originating in the five edited prompt files. Then prove the guidance strings did not move:

```bash
grep -E "^[+-]" /tmp/snap.diff | grep -E "EXPLORE phase|DECIDE phase|MUST CONCLUDE|message 12" \
  && echo "STOP: thread_guidance strings moved — investigate" \
  || echo "OK: EXPLORE/DECIDE/CONCLUDE strings unchanged"
```

Expected: `OK: EXPLORE/DECIDE/CONCLUDE strings unchanged`

Also confirm the intended removals actually happened:

```bash
A=tests/characterization/__snapshots__/test_agent_turn_gm.ambr
for s in "at Scripps Research" ":test_tube: Experiment" ":package: Resource" "@WisemanBot"; do
  printf "%-28s %s remaining\n" "$s" "$(grep -c "$s" $A)"
done
```

Expected: `0 remaining` for each.

- [ ] **Step 10: Full suite**

Run: `.venv-test/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add prompts/ src/agent/agent.py tests/unit/test_roles.py \
        tests/characterization/__snapshots__/test_agent_turn_gm.ambr
git commit -m "feat(prompts): render the post-type menu; tell agents who they can reach

Installs the reviewed drafts. Option C now defers to {post_type_menu} instead of
hardcoding four types; a new 'Who You Can Reach' section says that knowing a lab
exists is not evidence you can reach it, and introduces the scouting hub, which
appeared nowhere in a spoke's prompt before. Drops :test_tube:/:package: (offered
as labels, defined nowhere) and marks :question: reply-only. Collapses idea into
idea_crosslab.

Identity goes institution-neutral: 57 of 60 public profiles say Johns Hopkins,
the one saying Scripps is the test bot being retired, and two name no
institution — so the profile carries the fact and the shared line does not.
_DEFAULT_IDENTITY updated to match byte-for-byte, missing trailing newline
included.

Eight of nine characterization snapshots move because agent-system.md and
identity.md are injected into every phase's system prompt. Diff reviewed hunk by
hunk; the thread_guidance EXPLORE/DECIDE/CONCLUDE strings are unchanged.

Adds the pi_lab phase-5 token/surgery pin that never existed — only the
scout_hub override was pinned.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Full gate, then the operational cutover

**Files:** none — this task changes deployment state, not the repo.

**Interfaces:**
- Consumes: Tasks 1-6, all committed.
- Produces: a running simulation on the new code with clean memory.

> **The Slack deletion is irreversible.** Do not run step 5 without explicit sign-off on the count from the operator.

- [ ] **Step 1: The whole gate**

Run: `./scripts/ci.sh`
Expected: pass. This is the only gate — there is no server-side CI.

- [ ] **Step 2: Confirm the schema is already at head (no migration in this work)**

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC exec -T postgres psql -U copi -d copi -t -A -c "SELECT version_num FROM alembic_version;"
$DC exec -T blackbird-app alembic heads
```

Expected: the two values match. If they do not, stop and run
`$DC exec -T blackbird-app alembic upgrade head` **before** starting a run — nothing else migrates the database, and the failure mode is silent.

- [ ] **Step 3: Retire the `alanjary` test bot**

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC exec -T postgres psql -U copi -d copi -c \
  "UPDATE agents SET status='inactive' WHERE agent_id='alanjary';"
$DC exec -T postgres psql -U copi -d copi -c \
  "DELETE FROM cohort_memberships WHERE cohort_id =
     (SELECT id FROM cohorts WHERE name='hub-alanjary');"
$DC exec -T postgres psql -U copi -d copi -c \
  "DELETE FROM cohorts WHERE name='hub-alanjary';"
$DC exec -T postgres psql -U copi -d copi -c \
  "SELECT status FROM agents WHERE agent_id='alanjary';
   SELECT count(*) FROM cohorts;"
```

Expected: `inactive`, and 55 cohorts. Its profile still says "Scripps Research Institute", which is why it was the only such file.

- [ ] **Step 4: Back up and clear working memory**

`profiles/memory/` is not cohort-filtered and names out-of-cohort labs written under the previous topology (e.g. `profiles/memory/epearce/public.md` lists six unreachable partners). `--fresh` does not touch it.

```bash
STAMP=$(date +%s)
mkdir -p backups/memory_$STAMP
cp -a profiles/memory/. backups/memory_$STAMP/
find profiles/memory -name '*.md' -delete
find backups/memory_$STAMP -name '*.md' | wc -l   # non-zero
find profiles/memory -name '*.md' | wc -l         # 0
```

Never `rm -rf profiles/memory` — the per-agent directories are expected to exist.

- [ ] **Step 5: Delete the mutilated Slack posts — needs sign-off**

Measured: 200 top-level `:bulb:` posts carry the strip artifact; **113 reached Slack**, across 43 authors and 6 channels. Only those 113 exist to delete.

First re-measure and print the set without deleting anything:

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC exec -T postgres psql -U copi -d copi -c \
  "SELECT agent_id, channel_name, slack_ts, left(content, 60) AS preview
     FROM agent_messages
    WHERE thread_ts IS NULL AND is_bot
      AND content LIKE ':bulb:%' AND content ~ '—,|--,'
      AND slack_ts IS NOT NULL
    ORDER BY agent_id, posted_at;"
```

Confirm the row count is 113 and that every preview shows the `Idea —,` artifact. Get sign-off, then delete via each authoring bot's own token (a bot may only delete its own messages):

```bash
$DC exec -T blackbird-app python - <<'PY'
import asyncio
from sqlalchemy import text
from src.database import get_session_factory
from src.services.slack_tokens import env_token, is_valid_token
from src.agent.slack_client import AgentSlackClient

SQL = text("""
    SELECT agent_id, slack_channel_id, slack_ts FROM agent_messages
     WHERE thread_ts IS NULL AND is_bot AND content LIKE ':bulb:%'
       AND content ~ '—,|--,' AND slack_ts IS NOT NULL
""")

async def main():
    sf = get_session_factory()
    async with sf() as db:
        rows = (await db.execute(SQL)).all()
        tokens = dict((await db.execute(text(
            "SELECT agent_id, slack_bot_token FROM agents"))).all())
    print(f"{len(rows)} messages to delete")
    clients, ok, fail = {}, 0, 0
    for aid, ch, ts in rows:
        if aid not in clients:
            tok = tokens.get(aid)
            tok = tok if is_valid_token(tok) else env_token(aid)
            if not is_valid_token(tok):
                print(f"  {aid}: no usable token — skipping its messages"); fail += 1; continue
            c = AgentSlackClient(agent_id=aid, bot_token=tok)
            if not c.connect():
                print(f"  {aid}: connect failed"); fail += 1; continue
            clients[aid] = c
        if aid not in clients:
            fail += 1; continue
        try:
            # _api is this class's enforced chokepoint for every Slack call
            # (slack_client.py:294) and carries the retry/backoff path. The
            # class already deletes messages this way at :725.
            clients[aid]._api("chat_delete", channel=ch, ts=ts)
            ok += 1
        except Exception as exc:
            print(f"  {aid} {ts}: {exc}"); fail += 1
    print(f"deleted={ok} failed={fail}")

asyncio.run(main())
PY
```

Two verified details behind that script: `AgentSlackClient` keeps its SDK handle
private as `self._client` and routes **every** Slack call through
`self._api(method_name, **kwargs)` (`src/agent/slack_client.py:294`) — a
source-level test, `tests/unit/test_slack_client_contract.py`, fails if a second
`self._client.` appears anywhere in that module, so `_api` is the sanctioned path
rather than a shortcut. The class already deletes messages through it at `:725`.
And `get_session_factory()` is in `src/database.py:36`; the token lives in
`agents.slack_bot_token` (`src/models/agent_registry.py:34`).

- [ ] **Step 6: Rebuild both images and restart the run**

```bash
DC="docker compose -f docker-compose.prod.yml"
docker inspect blackbird-agent-run --format '{{index .Config.Labels "com.docker.compose.project"}}'
# MUST print copi-blackbird. If it prints copi-python, STOP — that is org1's production run.

docker logs blackbird-agent-run > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f

docker stop -t 30 blackbird-agent-run     # SIGTERM, not SIGKILL — flushes the in-flight turn
docker rm blackbird-agent-run

$DC up -d --build blackbird-app worker
$DC --profile agent build agent           # src/ is baked in; without this you deploy stale code

$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh
```

`--fresh` wipes `agent_messages`, so the DB rows behind the deleted Slack posts go with them and the two sides stay consistent. Never pass `--remove-orphans` — it has killed org1's nginx and certbot.

- [ ] **Step 7: Verify the fix is live, in the logs**

```bash
sleep 120
# 1. The gate is on and the roster no longer isolates anyone.
docker logs blackbird-agent-run 2>&1 | grep -E "\[cohort\] gate:" | tail -2
# Expect: 55 cohorts, 0 isolated. NOT "1 isolated (alanjary)".

# 2. No raw token reached a prompt.
docker logs blackbird-agent-run 2>&1 | grep -c "{post_type_menu}"   # must be 0

# 3. Rejections, if any, are visible at WARNING.
docker logs blackbird-agent-run 2>&1 | grep "rejected new post" | head

# 4. No new artifact-bearing posts.
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -c \
  "WITH run AS (SELECT id FROM simulation_runs ORDER BY started_at DESC LIMIT 1)
   SELECT count(*) FILTER (WHERE content ~ '—,|--,') AS artifacts,
          count(*) FILTER (WHERE content LIKE ':bulb:%') AS bulb
     FROM agent_messages WHERE simulation_run_id=(SELECT id FROM run)
       AND thread_ts IS NULL AND is_bot;"
```

Expected: `artifacts = 0`. Any `:bulb:` posts should now be `pitch` posts tagging `@BlackbirdBot`.

- [ ] **Step 8: Confirm the prompt no longer names unreachable labs**

```bash
docker compose -f docker-compose.prod.yml exec -T postgres psql -U copi -d copi -c \
  "WITH run AS (SELECT id FROM simulation_runs ORDER BY started_at DESC LIMIT 1),
        s AS (SELECT system_prompt sp FROM llm_call_logs
               WHERE simulation_run_id=(SELECT id FROM run) AND phase='new_post'
               ORDER BY created_at DESC LIMIT 1)
   SELECT (SELECT count(*) FROM regexp_matches(sp, '### ([A-Za-z .''-]+) Lab', 'g')) AS labs_listed,
          sp ILIKE '%Blackbird%' AS mentions_the_hub,
          sp ILIKE '%Scripps%'   AS says_scripps
     FROM s;"
```

Expected: `labs_listed` near 0 for a spoke (its only cohort-mates are the hub, which has no publications section, and grantbot), `mentions_the_hub = t`, `says_scripps = f`. Before this work the same query returned 52 labs, `f`, and `t`.

- [ ] **Step 9: Report**

State plainly: the artifact count, whether any rejections fired and for what, and whether the prompt audit in step 8 came back as expected. If `artifacts` is non-zero, the enforcement is not working — say so rather than reporting success.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 canonical vocabulary | 1 (`CANONICAL`, `DEFAULT_POST_TYPES`) |
| §3 layer 1 (`role.toml`) | 2 |
| §3 layer 2 (topology filter) | 1 (`available_for`), 5 (`_available_post_types`) |
| §3 layer 3 (`tagged_agent`) | 5 (`_post_type_rejection`) |
| §3 enabling fix (ordering) | 3 |
| §3 prompt changes + institution + `_DEFAULT_IDENTITY` | 6 |
| §4 data flow / placement / skips re-increment | 5 step 6 |
| §5 degradation table | 1 (`parse_post_types`), 2 |
| §5 empty menu is not a skip | 1 (`_EMPTY_MENU`, `test_funding_only_in_the_star_is_empty`) |
| §6 tests 1-2 | 3 |
| §6 tests 3-7 | 1, 5 |
| §6 test 8 (mesh inert) | 5 (`test_gate_off_*`) |
| §6 tests 9-10 (token pins) | 6 steps 1-2 |
| §6 snapshot rule | 6 steps 8-9 |
| §6 atomic landing | 6 header note, Task 4 test 1 |
| §7 operational items | 7 |

§8 is explicitly out of scope and has no task, by design.

**Placeholder scan:** none — every code step carries the actual code, every verification step carries the actual command and its expected output.

**Type consistency:** `PostTypeSpec` fields (`name`, `emoji`, `label`, `when_to_use`, `targets`) are used identically in Tasks 1, 2, 4, 5. `available_for` / `eligible_targets` / `render_menu` keep the same keyword-only signatures at every call site. `_available_post_types(agent, funding_only)` and `_post_type_rejection(agent, post_type, tagged_agent, available)` match between their definitions in Task 5 step 3 and their uses in steps 5-7 and the tests. `refresh_lab_directories()` is defined in Task 3 step 3 and used only there.

**Two details resolved against the source rather than left as caveats:**
`_write_role(tmp_path, monkeypatch, name, toml_text)` patches `roles.ROLES_DIR`
itself (`tests/unit/test_roles.py:7`), so Task 2's tests use its real arity and add
no second `setattr`; and `AgentSlackClient` routes every Slack call through
`self._api(method, **kwargs)` (`src/agent/slack_client.py:294`), enforced by
`tests/unit/test_slack_client_contract.py`, so Task 7's cleanup uses `_api` rather
than reaching for a public handle that does not exist.
