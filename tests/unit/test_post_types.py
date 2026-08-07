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


def test_render_menu_never_prints_an_empty_enumeration():
    """The bug this exists to stop: `Set tagged_agent to exactly one of: .`

    build_phase5_prompt renders a default menu when no caller supplies one, and
    test_phase5_prompt_gm goes down that path — so an empty enumeration would be
    committed into a characterization snapshot and shipped to a live model.
    """
    for gate, roles in ((None, {}), (None, MESH_ROLES), ({"gill"}, {"gill": "pi_lab"})):
        out = render_menu(
            DEFAULT_POST_TYPES, gate=gate, roles_by_agent=roles,
            self_id="gill", bot_names={},
        )
        assert "one of: ." not in out
        assert "one of: \n" not in out
        assert out.strip()


def test_render_menu_does_not_enumerate_when_the_gate_is_off():
    """A mesh has ~50 reachable labs. Enumerating them in every phase-5 prompt
    would recreate the 46 KB lab directory this design is shrinking, so gate
    None renders guidance instead of a list."""
    out = render_menu(
        [CANONICAL["idea_crosslab"]], gate=None, roles_by_agent=MESH_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "pearce" not in out and "wu" not in out
    assert "pi_lab" in out
    assert "agent_id" in out


def test_render_menu_enumerates_when_the_gate_is_on():
    out = render_menu(
        [CANONICAL["pitch"]], gate=STAR_GATE, roles_by_agent=STAR_ROLES,
        self_id="gill", bot_names=BOT_NAMES,
    )
    assert "one of:" in out
    assert "blackbird" in out


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
