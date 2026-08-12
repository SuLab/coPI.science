"""The post-type vocabulary and the role/topology filter.

Pure functions over plain data — no DB, no engine, no Agent. See
docs/specs/2026-08-06-role-topology-post-type-gating-design.md §2, §3.
"""
import logging
import re

from src.agent.post_types import (
    CANONICAL,
    DEFAULT_POST_TYPES,
    LEGACY_POST_TYPE_ALIASES,
    available_for,
    eligible_targets,
    parse_post_types,
    render_menu,
    resolve_post_type_name,
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
    assert set(CANONICAL) == {"pitch", "opportunity_assessment"}


def test_idea_is_not_a_type_anymore():
    """`idea` and `idea_crosslab` were both in the old enum with no documented
    difference and no code distinguishing them. Both are retired now."""
    assert "idea" not in CANONICAL
    assert "idea_crosslab" not in CANONICAL
    assert "idea" not in _by_name(DEFAULT_POST_TYPES)


def test_the_retired_idea_name_still_resolves():
    """Retired in the vocabulary, still accepted on input — the alias table
    itself does not care whether its destination is still canonical."""
    assert resolve_post_type_name("idea") == "idea_crosslab"


def test_resolve_passes_current_and_unknown_names_through():
    assert resolve_post_type_name("pitch") == "pitch"
    assert resolve_post_type_name("nonsense") == "nonsense"


def test_an_alias_is_never_offered_as_a_type():
    """Resolving on input must not put the retired name back in circulation."""
    for alias in LEGACY_POST_TYPE_ALIASES:
        assert alias not in CANONICAL
        out = render_menu(
            DEFAULT_POST_TYPES, gate=None, roles_by_agent=MESH_ROLES,
            self_id="gill", bot_names=BOT_NAMES,
        )
        assert f"**`{alias}`**" not in out


def test_default_post_types_is_the_pi_lab_set():
    assert _by_name(DEFAULT_POST_TYPES) == {"pitch"}
    assert "opportunity_assessment" not in _by_name(DEFAULT_POST_TYPES)


def test_broadcast_types_carry_no_targets():
    assert CANONICAL["opportunity_assessment"].targets == frozenset()


def test_addressed_types_declare_their_counterparty_role():
    assert CANONICAL["pitch"].targets == frozenset({"scout_hub"})


# --- eligible_targets -------------------------------------------------------

def test_eligible_targets_excludes_self():
    """An agent's own role is in its own gate; it must never be its own target."""
    spec = CANONICAL["pitch"]
    got = eligible_targets(
        spec, gate={"gill"}, roles_by_agent={"gill": "scout_hub"}, self_id="gill"
    )
    assert got == frozenset()


def test_eligible_targets_finds_the_hub_for_pitch():
    got = eligible_targets(
        CANONICAL["pitch"], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill"
    )
    assert got == frozenset({"blackbird"})


def test_eligible_targets_ignores_agents_with_no_known_role():
    """grantbot is in the gate but has no AgentRegistry row and is a separate
    process, never an entry in self.agents — so it never appears in
    roles_by_agent and matches no `targets`. It is a funding announcer, not a
    pitch recipient."""
    got = eligible_targets(
        CANONICAL["pitch"], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill"
    )
    assert "grantbot" not in got


def test_eligible_targets_is_empty_with_no_reachable_hub():
    got = eligible_targets(
        CANONICAL["pitch"], gate={"gill", "pearce"},
        roles_by_agent={"gill": "pi_lab", "pearce": "pi_lab"}, self_id="gill",
    )
    assert got == frozenset()


def test_eligible_targets_with_gate_off_returns_every_matching_role():
    roles = {"gill": "pi_lab", "blackbird": "scout_hub", "wu": "scout_hub"}
    got = eligible_targets(
        CANONICAL["pitch"], gate=None, roles_by_agent=roles, self_id="gill"
    )
    assert got == frozenset({"blackbird", "wu"})


# --- available_for ----------------------------------------------------------

def test_star_keeps_pitch_for_a_spoke():
    got = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", terminal_only=False,
    )
    assert _by_name(got) == {"pitch"}


def test_mesh_drops_pitch_for_a_spoke_with_no_reachable_hub():
    got = available_for(
        DEFAULT_POST_TYPES, gate=None, roles_by_agent=MESH_ROLES,
        self_id="gill", terminal_only=False,
    )
    assert _by_name(got) == set()


def test_gate_off_keeps_a_broadcast_type_even_with_no_known_roles():
    got = available_for(
        (CANONICAL["opportunity_assessment"],), gate=None, roles_by_agent={},
        self_id="gill", terminal_only=False,
    )
    assert _by_name(got) == {"opportunity_assessment"}


def test_terminal_only_keeps_only_terminal_types():
    """The subject is a lab (`gill`), with a reachable hub in the fixture, so
    `pitch` is otherwise available — the assertions below must actually
    discriminate on `terminal_only`, not just observe a set that was already
    going to exclude `pitch` for an unrelated (topology) reason."""
    declared = (CANONICAL["pitch"], CANONICAL["opportunity_assessment"])

    kept = available_for(
        declared, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", terminal_only=False,
    )
    assert _by_name(kept) == {"pitch", "opportunity_assessment"}

    narrowed = available_for(
        declared, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", terminal_only=True,
    )
    assert _by_name(narrowed) == {"opportunity_assessment"}


def test_terminal_only_in_the_star_can_be_empty():
    """Empty is the correct answer for a role with nothing terminal to
    report, and the engine must NOT read it as "skip the turn" on its own —
    that half is enforced in test_post_type_enforcement.py, not here; this
    only pins that the set really is empty. See spec §5."""
    got = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", terminal_only=True,
    )
    assert got == ()


def test_available_for_preserves_declaration_order():
    declared = (CANONICAL["opportunity_assessment"], CANONICAL["pitch"])
    roles = dict(MESH_ROLES, blackbird="scout_hub")
    got = available_for(
        declared, gate=None, roles_by_agent=roles, self_id="gill", terminal_only=False,
    )
    declared_names = [s.name for s in declared if s.name in _by_name(got)]
    assert [s.name for s in got] == declared_names


# --- parse_post_types -------------------------------------------------------

def test_parse_none_yields_the_defaults(caplog):
    """Spec §5 row 1 says "DEFAULT_POST_TYPES, WARNING once". The defaults are
    the correct answer for pi_lab, which HAS no manifest by design, so warning
    on every load would be noise on the common path — the warning belongs to a
    role that has a manifest and forgot the key. Pinned here so the divergence
    from §5 is a decision on record, not a silent omission."""
    caplog.set_level(logging.WARNING)
    assert parse_post_types(None, role="pi_lab") == DEFAULT_POST_TYPES
    assert caplog.text == ""


def test_parse_reads_name_and_targets():
    got = parse_post_types(
        [{"name": "opportunity_assessment"},
         {"name": "pitch", "targets": ["scout_hub"]}],
        role="scout_hub",
    )
    assert _by_name(got) == {"opportunity_assessment", "pitch"}
    assert dict((s.name, s.targets) for s in got)["pitch"] == frozenset({"scout_hub"})


def test_parse_drops_an_unknown_name_and_keeps_the_rest(caplog):
    got = parse_post_types(
        [{"name": "pitch"}, {"name": "not_a_real_type"}], role="pi_lab"
    )
    assert _by_name(got) == {"pitch"}
    assert "not_a_real_type" in caplog.text


def test_parse_drops_a_malformed_entry_and_keeps_the_rest(caplog):
    got = parse_post_types(
        ["not_a_table", {"name": "opportunity_assessment"}, {}], role="pi_lab"
    )
    assert _by_name(got) == {"opportunity_assessment"}
    assert caplog.text


def test_parse_warns_when_targets_names_a_role_that_cannot_exist(caplog):
    """A typo'd role means the type is silently never offered — say so at load."""
    caplog.set_level(logging.WARNING)
    got = parse_post_types(
        [{"name": "pitch", "targets": ["scout_hubb"]}], role="pi_lab"
    )
    assert _by_name(got) == {"pitch"}
    assert "scout_hubb" in caplog.text


def test_a_typod_target_role_really_is_never_offered(caplog):
    """The other half of that §5 row. The WARNING is only useful if the
    behaviour it predicts is real: no agent can ever satisfy `scout_hubb`, so
    the type is filtered out of every menu on every topology."""
    caplog.set_level(logging.WARNING)
    declared = parse_post_types(
        [{"name": "opportunity_assessment"}, {"name": "pitch", "targets": ["scout_hubb"]}],
        role="pi_lab",
    )
    for gate, roles in ((STAR_GATE, STAR_ROLES), (None, MESH_ROLES)):
        got = available_for(
            declared, gate=gate, roles_by_agent=roles, self_id="gill",
            terminal_only=False,
        )
        assert _by_name(got) == {"opportunity_assessment"}


def test_parse_of_a_non_list_yields_the_defaults(caplog):
    assert parse_post_types("paper", role="pi_lab") == DEFAULT_POST_TYPES
    assert caplog.text


def test_parse_targets_override_replaces_the_canonical_default():
    got = parse_post_types([{"name": "pitch", "targets": []}], role="pi_lab")
    assert got[0].targets == frozenset()


def test_parse_targets_absent_inherits_the_canonical_default():
    """The other half of the pair above: an ABSENT `targets` key is not the same
    as an explicit `targets = []`. Absent must inherit CANONICAL's default
    (non-empty, for `pitch`); only an explicit empty list means "broadcast"."""
    got = parse_post_types([{"name": "pitch"}], role="pi_lab")
    assert got[0].targets == CANONICAL["pitch"].targets
    assert got[0].targets == frozenset({"scout_hub"})


def test_parse_dedupes_a_repeated_name_and_the_last_entry_wins(caplog):
    """Two [[post_types]] tables for the same name used to produce two
    contradictory lines in the rendered menu while enforcement (`by_name` in
    `_post_type_rejection`) silently kept only the last — so the model could
    read a permission the gate had already revoked. Last-wins, with a WARNING,
    matches `by_name`'s own last-wins-by-construction behaviour."""
    caplog.set_level("WARNING")
    got = parse_post_types(
        [
            {"name": "pitch", "targets": ["scout_hub"]},
            {"name": "pitch", "targets": []},
        ],
        role="pi_lab",
    )
    assert _by_name(got) == {"pitch"}
    assert len(got) == 1
    assert got[0].targets == frozenset()
    assert "duplicate" in caplog.text.lower()
    assert "pitch" in caplog.text


def test_parse_dedupe_preserves_first_occurrence_position():
    """Declaration order is the menu's rendering order and must stay stable
    between turns even when a later duplicate wins on content."""
    got = parse_post_types(
        [
            {"name": "opportunity_assessment"},
            {"name": "pitch", "targets": ["scout_hub"]},
            {"name": "opportunity_assessment"},  # duplicate, later — content wins, position doesn't move
        ],
        role="pi_lab",
    )
    assert [s.name for s in got] == ["opportunity_assessment", "pitch"]


# --- render_menu ------------------------------------------------------------

def test_render_menu_names_every_available_type_with_its_emoji():
    specs = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", terminal_only=False,
    )
    out = render_menu(
        specs, gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill", bot_names=BOT_NAMES,
    )
    assert CANONICAL["pitch"].emoji in out
    # The stronger form: `name in out` alone would also pass for a menu
    # that merely echoes the name in prose somewhere, without actually
    # naming it as a selectable `post_type` value.
    assert "**`pitch`**" in out
    assert "idea_crosslab" not in out


def test_render_menu_never_prints_an_empty_enumeration():
    """The bug this exists to stop: `Set tagged_agent to exactly one of: .`

    build_phase5_prompt renders a default menu when no caller supplies one, and
    test_phase5_prompt_gm goes down that path — so an empty enumeration would be
    committed into a characterization snapshot and shipped to a live model.

    Checks a regex rather than the two exact literals ("one of: ." and
    "one of: \\n") so a spacing variant — an extra space before the period, a
    trailing space before the newline — cannot slip the same bug past this test.
    """
    for gate, roles in ((None, {}), (None, MESH_ROLES), ({"gill"}, {"gill": "pi_lab"})):
        out = render_menu(
            DEFAULT_POST_TYPES, gate=gate, roles_by_agent=roles,
            self_id="gill", bot_names={},
        )
        assert not re.search(r"one of:\s*\.", out)
        assert not re.search(r"one of:\s*$", out, re.MULTILINE)
        assert out.strip()


def test_render_menu_does_not_enumerate_when_the_gate_is_off():
    """A mesh has ~50 reachable labs. Enumerating them in every phase-5 prompt
    would recreate the 46 KB lab directory this design is shrinking, so gate
    None renders guidance instead of a list."""
    out = render_menu(
        [CANONICAL["pitch"]], gate=None, roles_by_agent=MESH_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "pearce" not in out and "wu" not in out
    assert "scout_hub" in out
    assert "agent_id" in out


def test_render_menu_enumerates_when_the_gate_is_on():
    out = render_menu(
        [CANONICAL["pitch"]], gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "one of:" in out
    assert "blackbird" in out


def test_render_menu_enumerated_branch_also_requires_the_body_mention():
    """tagged_agent alone routes nothing (phase-3 activation and thread
    participation both scan the message BODY for an @-mention — see
    src/agent/message_log.py). The gate-set branch is the production path (a
    star topology), so it must say so, the same as the gate=None branch
    already does."""
    out = render_menu(
        [CANONICAL["pitch"]], gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "@BotName" in out or "message body" in out.lower()


def test_render_menu_names_the_reachable_agent_for_an_addressed_type():
    specs = available_for(
        DEFAULT_POST_TYPES, gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", terminal_only=False,
    )
    out = render_menu(
        specs, gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill", bot_names=BOT_NAMES,
    )
    assert "BlackbirdBot" in out
    assert "blackbird" in out


def test_render_menu_marks_a_broadcast_type_as_addressing_no_one():
    out = render_menu(
        [CANONICAL["opportunity_assessment"]], gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "no one" in out.lower() or "broadcast" in out.lower()


def test_render_menu_of_an_empty_set_says_so_and_points_at_skip():
    out = render_menu(
        [], gate=STAR_GATE, roles_by_agent=STAR_ROLES, self_id="gill", bot_names=BOT_NAMES,
    )
    assert out.strip()
    low = out.lower()
    assert "no new top-level post type" in low
    assert "skip" in low
    assert "new_post" in low


def test_render_menu_never_returns_an_empty_string():
    """A blank menu would leave the prompt claiming a list exists with nothing in
    it, which reads as a rendering bug to the model."""
    for specs in ([], list(DEFAULT_POST_TYPES)):
        out = render_menu(
            specs, gate=None, roles_by_agent=MESH_ROLES, self_id="gill", bot_names=BOT_NAMES,
        )
        assert out.strip()
