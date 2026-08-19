"""Layers 1-3: a new top-level post must use a post type its role and topology
allow, and may only tag an agent that type can address.

The failure being prevented, measured over one production run: 146 of 146
phase-5 posts that declared a tagged_agent named an agent the poster's cohort
gate forbade. The mention was stripped and the post published anyway, leaving
259 :bulb: posts with a 0.8% reply rate against 9.0% for :newspaper: papers.
"""
import types

from src.agent.agent import Agent
from src.agent.post_types import PostTypeSpec
from src.agent.simulation import SimulationEngine

# CANONICAL's one real broadcast-shaped (no `targets`) example
# (opportunity_assessment) stopped being a post type at all once the hub
# went reply-only (its assessment is now the sidecar carried inside its own
# Phase-4 CONCLUDE reply — see simulation.py's `_reply_to_thread`). A few
# tests below exercise `_post_type_rejection`'s broadcast-type branch
# directly; `_post_type_rejection` takes `available` as a plain argument
# with no coupling to CANONICAL/role.toml, so this synthetic stand-in
# exercises the same code path regardless of what CANONICAL contains.
_BROADCAST = PostTypeSpec(
    "broadcast_test_type", ":test_tube:", "Test-only broadcast type",
    "A synthetic broadcast (no targets) post type used only in this test file.",
)


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

def test_star_spoke_can_pitch_to_the_hub():
    eng, gill, _, _ = _star()
    names = {s.name for s in eng._available_post_types(gill)}
    assert "pitch" in names


def test_mesh_spoke_loses_pitch_with_no_reachable_hub():
    gill, pearce = _spoke("gill"), _spoke("pearce")
    eng = _engine(gill, pearce)  # gates stay None, neither is a scout_hub
    names = {s.name for s in eng._available_post_types(gill)}
    assert names == set()


def test_hub_menu_is_empty_it_has_no_top_level_post_type_left():
    """The hub went reply-only (Option A relocation): its former sole post
    type, :mag: Opportunity Assessment, is not a post type at all anymore —
    it is the sidecar carried inside the hub's own Phase-4 CONCLUDE reply
    (see simulation.py's `_reply_to_thread`). role.toml declares
    `post_types = []` and CANONICAL has no entry for it either, so this menu
    is empty for the hub on every topology, star included."""
    eng, _, hub, _ = _star()
    assert eng._available_post_types(hub) == ()


# --- rejection -------------------------------------------------------------

def test_layer1_rejects_a_type_the_role_never_declared():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    reason = eng._post_type_rejection(gill, "opportunity_assessment", None, avail)
    assert reason is not None
    assert "opportunity_assessment" in reason


def test_layer2_rejects_a_type_with_no_reachable_counterparty():
    """pitch IS declared for pi_lab, but this spoke's gate excludes the hub —
    so the type is dropped by topology (layer 2), not by role declaration
    (layer 1)."""
    gill = _spoke("gill")
    gill.allowed_sender_ids = {"gill"}
    eng = _engine(gill, _hub())
    avail = eng._available_post_types(gill)
    assert "pitch" not in {s.name for s in avail}
    reason = eng._post_type_rejection(gill, "pitch", None, avail)
    assert reason is not None


def test_layer3_rejects_a_tag_toward_an_unreachable_agent_on_an_allowed_type():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    reason = eng._post_type_rejection(gill, "pitch", "pearce", avail)
    assert reason is not None
    assert "pearce" in reason


def test_layer3_rejects_an_unreachable_tag_on_a_broadcast_type():
    """The dangling-ask bug does not stop being one because the type is a
    broadcast: the mention gets stripped and the sentence around it survives.

    CANONICAL's one real broadcast-shaped example (opportunity_assessment)
    stopped being a post type at all when the hub went reply-only, so this
    passes a synthetic broadcast spec directly to `_post_type_rejection` —
    it takes ``available`` as a plain argument and has no coupling to
    CANONICAL/role.toml, so the broadcast-rejection branch under test is
    exercised the same way regardless."""
    eng, _, hub, _ = _star()
    reason = eng._post_type_rejection(hub, _BROADCAST.name, "nobody", (_BROADCAST,))
    assert reason is not None
    assert "nobody" in reason


def test_a_broadcast_type_is_accepted_tagged_or_not():
    """Both shapes must publish. A broadcast type addresses no one by
    declaration, so a model naming a reachable agent anyway (redundant, not
    wrong) must not lose the post over it."""
    eng, _, hub, _ = _star()
    avail = (_BROADCAST,)
    assert eng._post_type_rejection(hub, _BROADCAST.name, None, avail) is None
    assert eng._post_type_rejection(hub, _BROADCAST.name, "gill", avail) is None


def test_layer3_rejects_an_unknown_agent_id():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "pitch", "nobody", avail) is not None


# --- FIX B: tagged_agent normalisation --------------------------------------
#
# The menu line a model reads offers both forms adjacent — `` `blackbird`
# (@BlackbirdBot) `` — so any of these near-miss spellings is one slip away
# from the exact agent_id the gate compares against. Before normalisation all
# four rejected and published nothing.


def test_a_leading_at_sign_on_the_tagged_agent_still_resolves():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "pitch", "@blackbird", avail) is None


def test_a_bot_name_instead_of_an_agent_id_still_resolves():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "pitch", "BlackbirdBot", avail) is None


def test_a_capitalized_agent_id_still_resolves():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "pitch", "Blackbird", avail) is None


def test_stray_whitespace_around_the_tagged_agent_still_resolves():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "pitch", " blackbird", avail) is None


def test_normalisation_does_not_launder_a_genuinely_unreachable_agent():
    """Conservative on purpose: resolving the spelling must not resolve the
    reachability question too. pearce is a real agent_id, just not one gill's
    gate permits for `pitch` — every near-miss spelling of it must still be
    rejected, and the reason must still quote what the model actually sent."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    for spelling in ("pearce", "@pearce", "PearceBot", " pearce"):
        reason = eng._post_type_rejection(gill, "pitch", spelling, avail)
        assert reason is not None, f"{spelling!r} must still be rejected"
        assert spelling in reason, "the reason must quote what was actually sent"


def test_a_rejection_increments_the_per_agent_counter():
    """Mirrors _cohort_tags_stripped: a deployment where every pitch is
    rejected on a format slip must be visible without grepping logs."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejections.get(gill.agent_id, 0) == 0
    assert eng._post_type_rejection(gill, "pitch", "nobody", avail) is not None
    assert eng._post_type_rejections[gill.agent_id] == 1
    assert eng._post_type_rejection(gill, "pitch", "pearce", avail) is not None
    assert eng._post_type_rejections[gill.agent_id] == 2
    # An accepted post must not move the counter.
    assert eng._post_type_rejection(gill, "pitch", "blackbird", avail) is None
    assert eng._post_type_rejections[gill.agent_id] == 2


def test_a_valid_pitch_at_the_hub_is_accepted():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "pitch", "blackbird", avail) is None


def test_a_valid_broadcast_with_no_tag_is_accepted():
    eng, _, hub, _ = _star()
    avail = (_BROADCAST,)
    assert eng._post_type_rejection(hub, _BROADCAST.name, None, avail) is None


def test_an_empty_post_type_is_rejected_for_a_new_post():
    """post_type defaults to "" when the model omits it."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "", None, avail) is not None


def test_gate_off_accepts_everything_the_role_declared():
    """Layers 2 and 3 must be inert in a mesh so org1 is unaffected.

    Inert means *skipped*, not "happens to pass": a tag toward an agent that is
    not on the roster at all, and a null tag on an addressed type, both still
    publish, exactly as they do today. Anything else is a behaviour change to a
    deployment this work is not supposed to touch.
    """
    gill = _spoke("gill")
    wu = Agent("wu", "WuBot", "Wu Lab", role="scout_hub")
    eng = _engine(gill, wu)
    avail = eng._available_post_types(gill)
    assert "pitch" in {s.name for s in avail}
    assert eng._post_type_rejection(gill, "pitch", "wu", avail) is None
    assert eng._post_type_rejection(gill, "pitch", "ghost", avail) is None
    assert eng._post_type_rejection(gill, "pitch", None, avail) is None


def test_the_star_still_rejects_the_retired_idea_post_type():
    """Resolving the alias must not smuggle the type past the topology filter:
    `idea` resolves to `idea_crosslab`, which no longer exists in the
    vocabulary at all — a star spoke still cannot use it."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "idea", "pearce", avail) is not None


def test_gate_off_still_rejects_a_type_the_role_never_declared():
    gill = _spoke("gill")
    eng = _engine(gill)
    avail = eng._available_post_types(gill)
    assert eng._post_type_rejection(gill, "opportunity_assessment", None, avail) is not None


def _response(post_type, tagged_agent, body):
    tag = "null" if tagged_agent is None else f'"{tagged_agent}"'
    return (
        '```json\n'
        '{"action": "new_post", "channel": "general", '
        f'"post_type": "{post_type}", "tagged_agent": {tag}'
        '}\n```\n\n'
        f'<slack_message>{body}</slack_message>'
    )


# Layer 1: the exact production JSON. `idea_crosslab` is not in a star spoke's
# available set at all (it no longer exists in the vocabulary), so this never
# reaches the tag check — the reason names the TYPE, not the tag.
_REJECTED_L1 = _response(
    "idea_crosslab", "pearce", ":bulb: Idea — @PearceBot, your recent finding…"
)
# Layer 3: an AVAILABLE type aimed at an unreachable agent. This is the branch
# whose reason names the tag.
_REJECTED_L3 = _response(
    "pitch", "pearce", ":bulb: @PearceBot — our unpublished assay…"
)
_ACCEPTED = _response(
    "pitch", "blackbird", ":bulb: @BlackbirdBot — our unpublished assay on X."
)


async def _drive(monkeypatch, response, *, capture=None):
    """One spoke with a real fake Slack client, driven through the real handler.

    ``capture``, if given, is a dict the build_phase5_prompt stub fills with the
    kwargs it was called with — the only way to observe step 5, since stubbing
    that method is what makes driving the handler cheap in the first place.
    """
    from tests.fakes import FakeSlackClient

    gill = _spoke("gill")
    gill.allowed_sender_ids = {"gill", "blackbird"}
    hub, pearce = _hub(), _spoke("pearce")
    hub.allowed_sender_ids = {"gill", "blackbird", "pearce"}
    pearce.allowed_sender_ids = {"pearce", "blackbird"}
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[gill, hub, pearce], slack_clients={"gill": client},
    )

    async def _fake_generate(**kwargs):
        return response

    def _stub_prompt(**kw):
        if capture is not None:
            capture.update(kw)
        return ("sys", [])

    # Pin the settings this handler reads. Without this, a future
    # PHASE5_SKIP_PROBABILITY in the environment turns every rejection
    # assertion below into a silent skip that passes for the wrong reason.
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: types.SimpleNamespace(
            lab_daily_post_cap=50, active_thread_threshold=12,
            phase5_skip_probability=0.0,
            llm_agent_model_opus="test-model",
            # Task 9: _phase5_new_post now reserves a rate-limiter window slot
            # before the LLM call, which reads both of these.
            llm_calls_per_load_per_window=8, llm_rate_window_seconds=600,
        ),
    )
    monkeypatch.setattr(gill, "build_phase5_prompt", _stub_prompt)
    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )
    await eng._phase5_new_post(gill)
    return eng, gill, client


async def test_a_rejected_post_reaches_neither_slack_nor_the_counter(monkeypatch, caplog):
    """The exact production case, end to end:
    {"post_type": "idea_crosslab", "tagged_agent": "pearce"} from a spoke that
    cannot reach pearce. Before this change the mention was stripped and
    ":bulb: Idea —, your recent finding…" was published anyway — 113 times.

    The reason names the TYPE, not the tag: `idea_crosslab` is absent from the
    available set, so layer 1 rejects before the tag is ever examined.
    """
    caplog.set_level("WARNING")
    eng, gill, client = await _drive(monkeypatch, _REJECTED_L1)

    assert client.posted == []
    assert gill.message_count == 0
    assert "rejected new post" in caplog.text
    assert "idea_crosslab" in caplog.text


async def test_layer3_rejection_names_the_unreachable_tag_end_to_end(monkeypatch, caplog):
    """`pitch` IS available to a star spoke, so this reaches layer 3 and the
    reason must name the agent that could not be reached. Without this test the
    layer-3 branch is never exercised through the handler at all — every other
    star-topology rejection short-circuits at layer 1."""
    caplog.set_level("WARNING")
    eng, gill, client = await _drive(monkeypatch, _REJECTED_L3)

    assert client.posted == []
    assert gill.message_count == 0
    assert "rejected new post" in caplog.text
    assert "pearce" in caplog.text


async def test_a_rejected_post_re_increments_the_skip_backoff(monkeypatch):
    """consecutive_phase5_skips is zeroed before the branch, so a rejection that
    forgets to re-increment silently disables the backoff for that agent."""
    eng, gill, _ = await _drive(monkeypatch, _REJECTED_L1)
    assert gill.state.consecutive_phase5_skips == 1


async def test_repeated_rejections_accumulate_instead_of_pinning_at_one(monkeypatch):
    """The reset (`consecutive_phase5_skips = 0`, applied to every "real
    action" before the post-type gate runs) used to erase the streak on every
    single turn, so the rejection right after it always landed on a bare `1`
    no matter how many times in a row this agent got rejected. That pins the
    proactive-selection damping (`skips >= 3` in _select_next_agent) off
    forever for an agent that keeps reaching for an unavailable type — it gets
    rejected at full weight and full cadence, burning a max_tokens=4000 Opus
    call every time. Three consecutive rejections of the same agent must climb
    1, 2, 3 — not sit at 1."""
    gill = _spoke("gill")
    gill.allowed_sender_ids = {"gill", "blackbird"}
    hub, pearce = _hub(), _spoke("pearce")
    hub.allowed_sender_ids = {"gill", "blackbird", "pearce"}
    pearce.allowed_sender_ids = {"pearce", "blackbird"}
    from tests.fakes import FakeSlackClient

    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(agents=[gill, hub, pearce], slack_clients={"gill": client})

    async def _fake_generate(**kwargs):
        return _REJECTED_L1

    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: types.SimpleNamespace(
            lab_daily_post_cap=50, active_thread_threshold=12,
            phase5_skip_probability=0.0,
            llm_agent_model_opus="test-model",
            # Task 9: _phase5_new_post now reserves a rate-limiter window slot
            # before the LLM call, which reads both of these.
            llm_calls_per_load_per_window=8, llm_rate_window_seconds=600,
        ),
    )
    monkeypatch.setattr(gill, "build_phase5_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    streak = []
    for _ in range(3):
        await eng._phase5_new_post(gill)
        streak.append(gill.state.consecutive_phase5_skips)

    assert streak == [1, 2, 3]


# Layer 1-3 all pass this one: `pitch` is declared and reachable, and
# tagged_agent matches. But the BODY names an agent gill's cohort gate
# forbids — the exact scenario the JSON-only gate does not catch.
# `_strip_disallowed_tags` would silently delete " @PearceBot" and publish
# ":bulb: @BlackbirdBot — ..., your recent finding..." if nothing rejected it
# first.
_MUTILATED_BODY = _response(
    "pitch", "blackbird",
    ":bulb: @BlackbirdBot — @PearceBot, your recent finding on X was great.",
)


async def test_an_unreachable_mention_in_the_body_publishes_nothing(monkeypatch, caplog):
    """The gate validates the JSON declaration, but the mutilation it exists to
    prevent is driven by the message BODY, which the JSON-only checks above
    never look at. Production evidence: 42 of 259 posts named a lab in prose
    with no tag set. This must reject outright rather than strip-and-publish."""
    caplog.set_level("WARNING")
    eng, gill, client = await _drive(monkeypatch, _MUTILATED_BODY)

    assert client.posted == []
    assert gill.message_count == 0
    assert gill.state.consecutive_phase5_skips == 1
    assert "rejected new post" in caplog.text
    assert "mention" in caplog.text.lower()


async def test_an_allowed_post_still_goes_out(monkeypatch):
    """The other half: enforcement that rejects everything would also pass the
    tests above."""
    eng, gill, client = await _drive(monkeypatch, _ACCEPTED)
    assert len(client.posted) == 1
    assert client.posted[0]["text"].startswith(":bulb:")
    assert gill.message_count == 1


# --- a non-string post_type/tagged_agent must not publish -------------------
#
# Both currently raise into _phase5_new_post's blanket `except Exception` —
# fail-closed, but unpinned before these two tests. A future refactor that
# narrows that except clause needs to notice these cases rather than silently
# starting to publish a malformed action.

_NON_STRING_POST_TYPE = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": ["idea_crosslab"], "tagged_agent": null}\n```\n\n'
    '<slack_message>:bulb: Idea — something specific.</slack_message>'
)
_NON_STRING_TAGGED_AGENT = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": "pitch", "tagged_agent": ["pearce"]}\n```\n\n'
    '<slack_message>:bulb: Pitch — something specific.</slack_message>'
)


async def test_a_non_string_post_type_does_not_publish(monkeypatch, caplog):
    """resolve_post_type_name's dict.get on an unhashable key (a list, here)
    raises TypeError before layer 1 ever runs."""
    caplog.set_level("ERROR")
    eng, gill, client = await _drive(monkeypatch, _NON_STRING_POST_TYPE)
    assert client.posted == []
    assert gill.message_count == 0


async def test_a_non_string_tagged_agent_does_not_publish(monkeypatch, caplog):
    """`pitch` is declared and reachable, so layer 1 passes and this reaches
    the tag check — an unhashable tagged_agent (a list, here) raises TypeError
    out of the `in`/`not in` set-membership checks in _post_type_rejection."""
    caplog.set_level("ERROR")
    eng, gill, client = await _drive(monkeypatch, _NON_STRING_TAGGED_AGENT)
    assert client.posted == []
    assert gill.message_count == 0


async def test_the_menu_handed_to_the_prompt_is_the_set_that_is_enforced(monkeypatch):
    """Step 5's ONLY test. Every other end-to-end test stubs build_phase5_prompt
    away, so without this one `post_type_menu=` could be deleted from the call
    and the whole suite would still pass.

    Spec §6 test 7: the rendered menu names exactly the post-layer-2 set.
    """
    capture = {}
    eng, gill, _ = await _drive(monkeypatch, _ACCEPTED, capture=capture)

    menu = capture["post_type_menu"]
    available = {
        s.name for s in eng._available_post_types(gill)
    }
    assert available == {"pitch"}
    for name in available:
        assert f"**`{name}`**" in menu
    # A type gill's role never declared must not appear. CANONICAL is
    # exactly {pitch} now (the hub's assessment stopped being a post type
    # when it went reply-only), so there is no second real canonical name
    # left to demonstrate exclusion with — the synthetic broadcast stand-in
    # used elsewhere in this file exercises the same "excluded name is
    # absent from the rendered menu" contract just as well.
    assert f"**`{_BROADCAST.name}`**" not in menu
    # The hub is the one reachable counterparty, so the addressed type names it.
    assert "blackbird" in menu
