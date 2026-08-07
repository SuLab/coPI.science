"""Layers 1-3: a new top-level post must use a post type its role and topology
allow, and may only tag an agent that type can address.

The failure being prevented, measured over one production run: 146 of 146
phase-5 posts that declared a tagged_agent named an agent the poster's cohort
gate forbade. The mention was stripped and the post published anyway, leaving
259 :bulb: posts with a 0.8% reply rate against 9.0% for :newspaper: papers.
"""
import types

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
    names = {s.name for s in eng._available_post_types(gill, funding_restricted=False)}
    assert "idea_crosslab" not in names
    assert "funding_collab" not in names


def test_star_spoke_can_pitch_to_the_hub():
    eng, gill, _, _ = _star()
    names = {s.name for s in eng._available_post_types(gill, funding_restricted=False)}
    assert "pitch" in names


def test_star_spoke_keeps_every_broadcast_type():
    eng, gill, _, _ = _star()
    names = {s.name for s in eng._available_post_types(gill, funding_restricted=False)}
    assert {"paper", "help_wanted", "introduction"} <= names


def test_mesh_spoke_keeps_idea_crosslab_and_loses_pitch():
    gill, pearce = _spoke("gill"), _spoke("pearce")
    eng = _engine(gill, pearce)  # gates stay None
    names = {s.name for s in eng._available_post_types(gill, funding_restricted=False)}
    assert "idea_crosslab" in names
    assert "pitch" not in names


def test_hub_may_only_post_its_assessment():
    eng, _, hub, _ = _star()
    names = {s.name for s in eng._available_post_types(hub, funding_restricted=False)}
    assert "opportunity_assessment" in names
    assert "idea_crosslab" not in names
    assert "paper" not in names


def test_funding_only_in_the_star_is_empty_but_that_is_not_a_skip():
    eng, gill, _, _ = _star()
    assert eng._available_post_types(gill, funding_restricted=True) == ()


# --- rejection -------------------------------------------------------------

def test_layer1_rejects_a_type_the_role_never_declared():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    reason = eng._post_type_rejection(gill, "opportunity_assessment", None, avail)
    assert reason is not None
    assert "opportunity_assessment" in reason


def test_layer2_rejects_a_type_with_no_reachable_counterparty():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    reason = eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail)
    assert reason is not None


def test_layer3_rejects_the_exact_production_case():
    """{"post_type": "idea_crosslab", "tagged_agent": "pearce"} from markham."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail) is not None


def test_layer3_rejects_a_tag_toward_an_unreachable_agent_on_an_allowed_type():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    reason = eng._post_type_rejection(gill, "pitch", "pearce", avail)
    assert reason is not None
    assert "pearce" in reason


def test_layer3_tolerates_a_reachable_tag_on_a_broadcast_type():
    """Redundant is not wrong. The hub posts its :mag: assessment into the PI's
    own channel; naming that PI is the natural thing for the model to do, and
    rejecting it would destroy the artifact and the interview behind it over a
    field nothing routes on."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "paper", "blackbird", avail) is None


def test_layer3_rejects_an_unreachable_tag_on_a_broadcast_type():
    """The dangling-ask bug does not stop being one because the type is a
    broadcast: the mention gets stripped and the sentence around it survives."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    reason = eng._post_type_rejection(gill, "paper", "pearce", avail)
    assert reason is not None
    assert "pearce" in reason


def test_the_hubs_assessment_is_accepted_tagged_or_not():
    """Both shapes must publish. The prompt asks for tagged_agent=null, but a
    model that names the PI anyway must not lose the assessment."""
    eng, _, hub, _ = _star()
    avail = eng._available_post_types(hub, funding_restricted=False)
    assert eng._post_type_rejection(hub, "opportunity_assessment", None, avail) is None
    assert eng._post_type_rejection(hub, "opportunity_assessment", "gill", avail) is None


def test_the_hubs_funding_note_must_address_a_reachable_pi():
    eng, _, hub, _ = _star()
    avail = eng._available_post_types(hub, funding_restricted=False)
    assert eng._post_type_rejection(hub, "funding_collab", "gill", avail) is None
    assert eng._post_type_rejection(hub, "funding_collab", None, avail) is not None
    assert eng._post_type_rejection(hub, "funding_collab", "nobody", avail) is not None


def test_layer3_rejects_an_unknown_agent_id():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "pitch", "nobody", avail) is not None


def test_a_valid_pitch_at_the_hub_is_accepted():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "pitch", "blackbird", avail) is None


def test_a_valid_broadcast_with_no_tag_is_accepted():
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "paper", None, avail) is None


def test_an_empty_post_type_is_rejected_for_a_new_post():
    """post_type defaults to "" when the model omits it."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "", None, avail) is not None


def test_gate_off_accepts_everything_the_role_declared():
    """Layers 2 and 3 must be inert in a mesh so org1 is unaffected.

    Inert means *skipped*, not "happens to pass": a tag toward an agent that is
    not on the roster at all, and an addressed type with no tag, both still
    publish, exactly as they do today. Anything else is a behaviour change to a
    deployment this work is not supposed to touch.
    """
    gill, pearce = _spoke("gill"), _spoke("pearce")
    eng = _engine(gill, pearce)
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "idea_crosslab", "pearce", avail) is None
    assert eng._post_type_rejection(gill, "paper", None, avail) is None
    assert eng._post_type_rejection(gill, "idea_crosslab", "ghost", avail) is None
    assert eng._post_type_rejection(gill, "idea_crosslab", None, avail) is None


def test_mesh_still_accepts_the_retired_idea_post_type():
    """A mesh deployment's bind-mounted prompts may still say `idea` while the
    baked-in code has moved on. Layer 1 must not silently delete those posts —
    that is a regression in a deployment this change is not supposed to touch."""
    gill, pearce = _spoke("gill"), _spoke("pearce")
    eng = _engine(gill, pearce)
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "idea", "pearce", avail) is None


def test_the_star_still_rejects_the_retired_idea_post_type():
    """Resolving the alias must not smuggle the type past the topology filter:
    `idea` resolves to `idea_crosslab`, which a star spoke still cannot use."""
    eng, gill, _, _ = _star()
    avail = eng._available_post_types(gill, funding_restricted=False)
    assert eng._post_type_rejection(gill, "idea", "pearce", avail) is not None


def test_gate_off_still_rejects_a_type_the_role_never_declared():
    gill = _spoke("gill")
    eng = _engine(gill)
    avail = eng._available_post_types(gill, funding_restricted=False)
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
# available set at all, so this never reaches the tag check — the reason names
# the TYPE, not the tag.
_REJECTED_L1 = _response(
    "idea_crosslab", "pearce", ":bulb: Idea — @PearceBot, your recent finding…"
)
# Layer 3: an AVAILABLE type aimed at an unreachable agent. This is the branch
# whose reason names the tag.
_REJECTED_L3 = _response(
    "pitch", "pearce", ":bulb: @PearceBot — our unpublished assay…"
)
_ACCEPTED = _response("paper", None, ":newspaper: Paper — we published a thing.")


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
            daily_post_cap=50, active_thread_threshold=12,
            unreviewed_proposal_block_count=3, phase5_skip_probability=0.0,
            llm_agent_model_opus="test-model",
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


async def test_an_allowed_post_still_goes_out(monkeypatch):
    """The other half: enforcement that rejects everything would also pass the
    tests above."""
    eng, gill, client = await _drive(monkeypatch, _ACCEPTED)
    assert len(client.posted) == 1
    assert client.posted[0]["text"].startswith(":newspaper:")
    assert gill.message_count == 1


async def test_the_menu_handed_to_the_prompt_is_the_set_that_is_enforced(monkeypatch):
    """Step 5's ONLY test. Every other end-to-end test stubs build_phase5_prompt
    away, so without this one `post_type_menu=` could be deleted from the call
    and the whole suite would still pass.

    Spec §6 test 7: the rendered menu names exactly the post-layer-2 set.
    """
    from src.agent.post_types import CANONICAL

    capture = {}
    eng, gill, _ = await _drive(monkeypatch, _ACCEPTED, capture=capture)

    menu = capture["post_type_menu"]
    available = {
        s.name for s in eng._available_post_types(gill, funding_restricted=False)
    }
    assert available == {"paper", "help_wanted", "introduction", "pitch"}
    for name in available:
        assert f"**`{name}`**" in menu
    for name in set(CANONICAL) - available:
        assert f"**`{name}`**" not in menu
    # The hub is the one reachable counterparty, so the addressed type names it.
    assert "blackbird" in menu


# --- step 6a: the reply-path bypass -----------------------------------------

async def test_a_blocked_agent_cannot_self_declare_funding_collab_on_a_reply(
    monkeypatch, caplog
):
    """The bypass (`is_funding_post`) read post_type regardless of action, so
    {"action": "reply", "post_type": "funding_collab"} to a NON-funding thread
    walked past the unreviewed-proposal block. Layers 1-3 do not catch it —
    they govern new_post only."""
    from src.agent.message_log import LogEntry
    from src.agent.state import ProposalRef, ThreadState
    from tests.fakes import FakeSlackClient

    caplog.set_level("INFO")
    gill = _spoke("gill")
    gill.allowed_sender_ids = {"gill", "blackbird"}
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(agents=[gill, _hub()], slack_clients={"gill": client})

    # Blocked: one unreviewed non-funding proposal.
    gill.state.pending_proposals.append(
        ProposalRef(
            thread_id="t1", channel="general", other_agent_id="blackbird",
            summary_text=":memo: Summary — a proposal", proposed_at=0.0,
        )
    )
    # A thread carrying an FOA, so the "blocked and nothing to do" early
    # return (`if not available_posts and blocked_for_regular ...`, :2054)
    # does not fire before we reach the bypass.
    gill.state.active_threads["t9"] = ThreadState(
        thread_id="t9", channel="funding", other_agent_id="blackbird",
        message_count=1, foa_number="RFA-AI-27-019",
    )
    # A plain, non-funding thread to aim the reply at.
    eng.message_log.load_entry(LogEntry(
        ts="t1", channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="not a funding post", posted_at=0.0,
        slack_ts="t1",
    ))

    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: types.SimpleNamespace(
            daily_post_cap=50, active_thread_threshold=12,
            unreviewed_proposal_block_count=1, phase5_skip_probability=0.0,
            llm_agent_model_opus="test-model",
        ),
    )
    monkeypatch.setattr(gill, "build_phase5_prompt", lambda **kw: ("sys", []))

    async def _fake_generate(**kwargs):
        return (
            '```json\n'
            '{"action": "reply", "target_post_id": "t1", "channel": "general", '
            '"post_type": "funding_collab", "tagged_agent": null}\n'
            '```\n\n'
            '<slack_message>:moneybag: RFA-AI-27-019 — unrelated.</slack_message>'
        )

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )

    await eng._phase5_new_post(gill)

    assert client.posted == []
    assert "Blocked non-funding action" in caplog.text
