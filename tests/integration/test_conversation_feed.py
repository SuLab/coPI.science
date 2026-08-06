"""The conversations feed's visibility gate, and its parity with the engine.

The page must show exactly what the viewing agent's bot is allowed to act on.
The engine decides that in memory (``_entry_allowed``); the page decides it in
SQL (``gate_clause``). Two implementations of one rule is a drift hazard, so the
parity test below drives BOTH from the engine's own ``DECISION_TABLE``.
"""

import pytest
from sqlalchemy import select

from src.agent.message_log import _entry_allowed
from src.models import AgentMessage, Cohort, CohortMembership
from src.services.conversation_feed import gate_clause, resolve_agent_gate
from tests import factories
from tests.integration.test_agent_page import _auth
from tests.unit.test_cohort_isolation import DECISION_TABLE, _post

pytestmark = pytest.mark.integration


async def _cohort(db, name, *agent_ids):
    c = Cohort(name=name)
    db.add(c)
    await db.flush()
    for aid in agent_ids:
        db.add(CohortMembership(cohort_id=c.id, agent_id=aid))
    await db.flush()
    return c


@pytest.mark.parametrize(
    "name,kwargs,gate,expected", DECISION_TABLE, ids=[r[0] for r in DECISION_TABLE]
)
async def test_gate_clause_matches_entry_allowed(
    db_session, name, kwargs, gate, expected
):
    """Every row of the engine's §5.1 table, decided by SQL instead of Python."""
    run = await factories.make_simulation_run(db_session)
    row_kwargs = dict(agent_id="x", is_bot=True, visibility="public")
    row_kwargs.update(
        {k: v for k, v in kwargs.items() if k in ("agent_id", "is_bot", "visibility")}
    )
    msg = await factories.make_agent_message(
        db_session, run=run, message_ts="1.0001", content="body", **row_kwargs
    )
    await db_session.flush()

    found = (await db_session.execute(
        select(AgentMessage.id).where(
            AgentMessage.simulation_run_id == run.id,
            gate_clause(gate),
        )
    )).scalars().all()
    sql_visible = msg.id in found

    entry_kwargs = dict(ts="1", channel="c", agent_id="x", name="X", content="")
    entry_kwargs.update(kwargs)
    python_visible = _entry_allowed(_post(**entry_kwargs), gate)

    assert sql_visible == expected, f"SQL disagreed with the table on: {name}"
    assert sql_visible == python_visible, (
        f"gate_clause and _entry_allowed disagree on: {name}"
    )


async def test_gate_is_the_union_of_co_members(db_session, monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(db_session, agent_id="spoke1", bot_name="Spoke1Bot")
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "pair1", "spoke1", "hub")
    await _cohort(db_session, "pair2", "spoke2", "hub")

    assert await resolve_agent_gate(db_session, "spoke1") == {"spoke1", "hub"}
    assert await resolve_agent_gate(db_session, "spoke2") == {"spoke2", "hub"}
    assert await resolve_agent_gate(db_session, "hub") == {"spoke1", "spoke2", "hub"}


async def test_uncohorted_agent_is_isolated_under_policy_isolated(
    db_session, monkeypatch
):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(db_session, agent_id="lonely", bot_name="LonelyBot")
    await factories.make_agent(db_session, agent_id="other", bot_name="OtherBot")
    await _cohort(db_session, "somepair", "other")

    assert await resolve_agent_gate(db_session, "lonely") == set()


async def test_gate_is_off_when_isolation_is_disabled(db_session, monkeypatch):
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", False, raising=False)

    await factories.make_agent(db_session, agent_id="anyone", bot_name="AnyoneBot")

    assert await resolve_agent_gate(db_session, "anyone") is None


async def test_an_inactive_viewing_agent_still_resolves(db_session, monkeypatch):
    """compute_gates only keys the roster it is given, and the conversations route
    admits status 'inactive'. Without adding the viewer to the roster, 'sleeper'
    would be absent from compute_gates' agent_ids, so gates.get('sleeper') would
    silently return None (gate off / unrestricted) instead of the viewer's real
    cohort gate {'sleeper', 'awake'} — the opposite of the intended isolation,
    and worse than a KeyError because it fails open rather than loud."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    await factories.make_agent(
        db_session, agent_id="sleeper", bot_name="SleeperBot", status="inactive"
    )
    await factories.make_agent(db_session, agent_id="awake", bot_name="AwakeBot")
    await _cohort(db_session, "mixed", "sleeper", "awake")

    assert await resolve_agent_gate(db_session, "sleeper") == {"sleeper", "awake"}


async def test_a_spoke_pi_does_not_see_another_spokes_bot(
    client, db_session, monkeypatch
):
    """The star topology: two spokes and a hub. Spoke 1's PI must not see
    Spoke 2's bot, and MUST still see the hub (the positive control)."""
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi1 = await factories.make_user(db_session, name="Spoke One", email="s1@example.org")
    await factories.make_agent(
        db_session, user=pi1, agent_id="spoke1", bot_name="Spoke1Bot", pi_name="Spoke One"
    )
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "pair1", "spoke1", "hub")
    await _cohort(db_session, "pair2", "spoke2", "hub")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    # Spoke 1's own post is what puts #general in its channel set.
    await factories.make_agent_message(
        db_session, agent_id="spoke1", message_ts="1.0001",
        content="MINE-own-post", sender_name="Spoke1Bot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="hub", message_ts="1.0002",
        content="HUB-visible-post", sender_name="HubBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="spoke2", message_ts="1.0003",
        content="LEAK-other-spoke-post", sender_name="Spoke2Bot", **common
    )
    await db_session.commit()

    page = await client.get("/agent/spoke1/conversations", headers=_auth(pi1.id))
    assert page.status_code == 200
    assert "MINE-own-post" in page.text
    assert "HUB-visible-post" in page.text, "positive control: the hub must be visible"
    assert "LEAK-other-spoke-post" not in page.text
    assert "Spoke2Bot" not in page.text


async def test_a_pi_message_still_renders_under_the_gate(
    client, db_session, monkeypatch
):
    """is_bot=False bypasses the gate — the human bypass must survive.

    The gate must be genuinely ON here, or this proves nothing: with zero
    cohorts defined, compute_gates' preflight refuses under
    policy="isolated" (roster-wide-silence guard) and returns gate=None for
    every agent, which makes gate_clause a no-op regardless of is_bot. Putting
    "solo" in a cohort of its own makes resolve_agent_gate return a real,
    non-None set, so the human row can only pass through the is_bot bypass
    branch of gate_clause, not through the gate being off — asserted below.
    """
    from src.config import get_settings
    from src.services.conversation_feed import resolve_agent_gate
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Solo PI", email="solo@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="solo", bot_name="SoloBot", pi_name="Solo PI"
    )
    await _cohort(db_session, "solo-cohort", "solo")
    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="solo", message_ts="2.0001",
        content="BOT-anchor", sender_name="SoloBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id=None, is_bot=False, message_ts="2.0002",
        content="HUMAN-said-this", sender_name="Solo PI (PI)", **common
    )
    await db_session.commit()

    assert await resolve_agent_gate(db_session, "solo") == {"solo"}, (
        "the gate must be a real, non-None set here, or the bypass this test "
        "targets is never actually exercised"
    )

    page = await client.get("/agent/solo/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "HUMAN-said-this" in page.text


async def test_an_uncohorted_agent_still_sees_its_own_posts(
    client, db_session, monkeypatch
):
    """Under policy="isolated" an active-but-uncohorted agent's gate is the
    EMPTY set (not None) — deliberately, so it cannot read any other lab's
    traffic. But activation and cohort assignment are separate admin steps
    (see CLAUDE.md's onboarding order: Provision -> Approve & Activate happens
    before any admin adds the agent to a cohort), so a PI must still see their
    OWN bot's posts in that gap, or their page goes blank the moment their bot
    goes live. This is the safe, deliberate divergence from `_entry_allowed`
    documented at the `own_or_gated` clause in agent_page.py: it can only ever
    admit this agent's own rows, never another agent's."""
    from src.config import get_settings
    from src.services.conversation_feed import resolve_agent_gate
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Lonely PI", email="lonely@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="lonely", bot_name="LonelyBot", pi_name="Lonely PI"
    )
    await factories.make_agent(db_session, agent_id="other", bot_name="OtherBot")
    await _cohort(db_session, "other-only", "other")  # "lonely" is deliberately left out

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="lonely", message_ts="8.0001",
        content="LONELY-OWN-POST", sender_name="LonelyBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="other", message_ts="8.0002",
        content="OTHER-LAB-POST", sender_name="OtherBot", **common
    )
    await db_session.commit()

    assert await resolve_agent_gate(db_session, "lonely") == set(), (
        "this test targets the isolated-empty-set case specifically"
    )

    page = await client.get("/agent/lonely/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "LONELY-OWN-POST" in page.text, (
        "an uncohorted agent's PI must still see their own bot's posts"
    )
    assert "OTHER-LAB-POST" not in page.text


async def test_gate_is_applied_before_limit_not_after(
    client, db_session, monkeypatch
):
    """`_ROOT_LIMIT` must select the top-N GATE-PASSING roots, not the top-N
    roots with the gate applied afterward in Python. Flood the channel with 60
    out-of-cohort roots, all newer (higher posted_at) than a single in-cohort
    root belonging to a cohort-mate. If the gate ran after `.limit(_ROOT_LIMIT)`
    instead of in the SQL WHERE, the initial fetch would already be full of the
    50 newest out-of-cohort rows and the in-cohort root — older than all 60 —
    would never be fetched at all, gate or no gate."""
    from src.config import get_settings
    from src.routers.agent_page import _ROOT_LIMIT
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    assert _ROOT_LIMIT < 60, "the flood must exceed the window for this test to prove anything"

    pi = await factories.make_user(db_session, name="Flooded PI", email="flooded@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="victim", bot_name="VictimBot", pi_name="Flooded PI"
    )
    await factories.make_agent(db_session, agent_id="mate", bot_name="MateBot")
    await factories.make_agent(db_session, agent_id="flooder", bot_name="FlooderBot")
    await _cohort(db_session, "victim-mate", "victim", "mate")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    for i in range(60):
        await factories.make_agent_message(
            db_session, agent_id="flooder", message_ts=f"7.{i:04d}",
            phase="new_post", content=f"FLOOD-{i}", sender_name="FlooderBot",
            posted_at=1000.0 + i, **common
        )
    await factories.make_agent_message(
        db_session, agent_id="mate", message_ts="7.9999", phase="new_post",
        content="SURVIVOR-ROOT", sender_name="MateBot", posted_at=1.0, **common
    )
    await db_session.commit()

    page = await client.get("/agent/victim/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "SURVIVOR-ROOT" in page.text, (
        "the in-cohort root, though older than all 60 out-of-cohort floods, must "
        "still render — proving the gate ran in SQL before LIMIT, not in Python after"
    )


async def test_reply_count_excludes_out_of_cohort_replies(
    client, db_session, monkeypatch
):
    """`reply_count` must be computed with the SAME gate as the roots query, so
    the badge (Task 6) can never promise a reply the thread-expand endpoint
    (Task 5) will not show. A root with one in-cohort reply and one
    out-of-cohort reply must report reply_count == 1, not 2.

    The template does not render reply_count yet (Task 6 owns that), so this
    intercepts the context handed to templates.TemplateResponse rather than
    reading it out of rendered HTML.
    """
    import src.routers.agent_page as agent_page_module
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Counter PI", email="counter@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="counter", bot_name="CounterBot", pi_name="Counter PI"
    )
    await factories.make_agent(db_session, agent_id="mate", bot_name="MateBot")
    await factories.make_agent(db_session, agent_id="outsider", bot_name="OutsiderBot")
    await _cohort(db_session, "counter-mate", "counter", "mate")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="counter", message_ts="6.0001", phase="new_post",
        content="COUNT-ROOT", sender_name="CounterBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="mate", message_ts="6.0002", thread_ts="6.0001",
        phase="thread_reply", content="IN-COHORT-REPLY", sender_name="MateBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="outsider", message_ts="6.0003", thread_ts="6.0001",
        phase="thread_reply", content="OUT-OF-COHORT-REPLY", sender_name="OutsiderBot",
        **common
    )
    await db_session.commit()

    captured: dict = {}
    original_response = agent_page_module.templates.TemplateResponse

    def _capture(request, name, context, *args, **kwargs):
        captured["messages"] = context.get("messages")
        return original_response(request, name, context, *args, **kwargs)

    monkeypatch.setattr(agent_page_module.templates, "TemplateResponse", _capture)

    page = await client.get("/agent/counter/conversations", headers=_auth(pi.id))
    assert page.status_code == 200

    roots_by_content = {m["content"]: m for m in captured["messages"]}
    assert "COUNT-ROOT" in roots_by_content
    assert roots_by_content["COUNT-ROOT"]["reply_count"] == 1, (
        "reply_count must be gated the same as the roots query — it should count "
        "only the in-cohort reply, not the out-of-cohort one"
    )


async def test_replies_are_not_listed_as_top_level_rows(
    client, db_session, monkeypatch
):
    """The feed selects ROOTS. A reply appears via its count, not as its own card."""
    from src.config import get_settings
    monkeypatch.setattr(
        get_settings(), "cohort_isolation_enabled", False, raising=False
    )

    pi = await factories.make_user(db_session, name="Root PI", email="root@example.org")
    await factories.make_agent(
        db_session, user=pi, agent_id="rooter", bot_name="RooterBot", pi_name="Root PI"
    )
    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="rooter", message_ts="3.0001", phase="new_post",
        content="THE-ROOT", sender_name="RooterBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="rooter", message_ts="3.0002", thread_ts="3.0001",
        phase="thread_reply", content="THE-REPLY", sender_name="RooterBot", **common
    )
    await db_session.commit()

    page = await client.get("/agent/rooter/conversations", headers=_auth(pi.id))
    assert page.status_code == 200
    assert "THE-ROOT" in page.text
    assert "THE-REPLY" not in page.text, "a reply must not render as a top-level card"


async def test_a_delegate_sees_exactly_what_the_owner_sees(
    client, db_session, monkeypatch
):
    """Access is owner-or-delegate; the gate is the AGENT's, not the viewer's, so
    both must get byte-identical feeds."""
    from src.config import get_settings
    from src.models import AgentDelegate
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(db_session, name="Owner", email="own@example.org")
    agent = await factories.make_agent(
        db_session, user=pi, agent_id="deleg", bot_name="DelegBot", pi_name="Owner"
    )
    await factories.make_agent(db_session, agent_id="stranger", bot_name="StrangerBot")
    await _cohort(db_session, "solo", "deleg")

    dee = await factories.make_user(db_session, name="Dee", email="dee2@example.org")
    db_session.add(AgentDelegate(agent_registry_id=agent.id, user_id=dee.id))

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="deleg", message_ts="4.0001",
        content="OWN-POST", sender_name="DelegBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="stranger", message_ts="4.0002",
        content="OUTSIDER-POST", sender_name="StrangerBot", **common
    )
    await db_session.commit()

    owner_page = await client.get("/agent/deleg/conversations", headers=_auth(pi.id))
    dee_page = await client.get("/agent/deleg/conversations", headers=_auth(dee.id))
    assert owner_page.status_code == 200
    assert dee_page.status_code == 200
    assert "OWN-POST" in dee_page.text
    assert "OUTSIDER-POST" not in owner_page.text
    assert "OUTSIDER-POST" not in dee_page.text


# ---------------------------------------------------------------------------
# Task 5: thread expand endpoint (GET /agent/{agent_id}/thread/{message_ts})
# ---------------------------------------------------------------------------


async def _threaded_world(db_session, monkeypatch):
    """Spoke1 (owned) + Spoke2 (not owned).

    Spoke1's root (9.0001) has TWO replies: one from cohort-mate `hub`
    (HUB-REPLY, in spoke1's gate {spoke1, hub}) and one from `spoke2`
    (OUT-OF-COHORT-REPLY, NOT in spoke1's gate — spoke1 and spoke2 are each
    paired with `hub` but not with each other). That pairing is deliberate:
    it is the only way to prove replies are gated at all, rather than merely
    admitted through the root owner's own-post carve-out. Spoke2 additionally
    has its own root (9.0003, FOREIGN-ROOT) with no reply, used by the
    out-of-cohort-root/IDOR tests below.
    """
    from src.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi1 = await factories.make_user(db_session, name="S One", email="t1@example.org")
    await factories.make_agent(
        db_session, user=pi1, agent_id="spoke1", bot_name="Spoke1Bot", pi_name="S One"
    )
    await factories.make_agent(db_session, agent_id="spoke2", bot_name="Spoke2Bot")
    await factories.make_agent(db_session, agent_id="hub", bot_name="HubBot")
    await _cohort(db_session, "p1", "spoke1", "hub")
    await _cohort(db_session, "p2", "spoke2", "hub")

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="spoke1", message_ts="9.0001", phase="new_post",
        content="MY-ROOT", sender_name="Spoke1Bot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="hub", message_ts="9.0002", thread_ts="9.0001",
        phase="thread_reply", content="HUB-REPLY", sender_name="HubBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="spoke2", message_ts="9.0003", phase="new_post",
        content="FOREIGN-ROOT", sender_name="Spoke2Bot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="spoke2", message_ts="9.0004", thread_ts="9.0001",
        phase="thread_reply", content="OUT-OF-COHORT-REPLY", sender_name="Spoke2Bot",
        **common
    )
    await db_session.commit()
    return pi1


async def test_expanding_own_thread_returns_the_gated_replies(
    client, db_session, monkeypatch
):
    """Positive control (HUB-REPLY, in-cohort) and negative control
    (OUT-OF-COHORT-REPLY, from an agent NOT in spoke1's gate) in the same
    thread the viewer legitimately owns. This is the exact clause the brief
    calls out as the deliberate engine divergence: replies must be gated with
    own_or_gated, not merely admitted because the root belongs to the viewer.
    Deleting `gated` from the reply_rows query in agent_thread_replies turns
    this red (verified by hand — see task-5-report.md).
    """
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/9.0001", headers=_auth(pi1.id))
    assert r.status_code == 200
    assert "HUB-REPLY" in r.text
    assert "OUT-OF-COHORT-REPLY" not in r.text, (
        "a reply must be gated even inside a thread the viewer owns the root of"
    )


async def test_expanding_an_out_of_cohort_root_is_404(client, db_session, monkeypatch):
    """The IDOR guard: message_ts is guessable, so the root must re-pass the gate."""
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/9.0003", headers=_auth(pi1.id))
    assert r.status_code == 404
    assert "FOREIGN-ROOT" not in r.text


async def test_expanding_a_reply_ts_rather_than_a_root_is_404(
    client, db_session, monkeypatch
):
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/9.0002", headers=_auth(pi1.id))
    assert r.status_code == 404


async def test_expanding_an_unknown_ts_is_404(client, db_session, monkeypatch):
    pi1 = await _threaded_world(db_session, monkeypatch)
    r = await client.get("/agent/spoke1/thread/0.0000", headers=_auth(pi1.id))
    assert r.status_code == 404


async def test_a_stranger_cannot_expand_someone_elses_thread(
    client, db_session, monkeypatch
):
    await _threaded_world(db_session, monkeypatch)
    stranger = await factories.make_user(
        db_session, name="Nosy", email="nosy@example.org"
    )
    await db_session.commit()
    r = await client.get("/agent/spoke1/thread/9.0001", headers=_auth(stranger.id))
    assert r.status_code == 403
    assert "HUB-REPLY" not in r.text


async def test_expanding_an_uncohorted_own_thread_is_200_not_404(
    client, db_session, monkeypatch
):
    """CONTROLLER AMENDMENT case: a PI whose agent is active but uncohorted
    (gate == empty set under policy="isolated") must still be able to expand
    their OWN thread. A bare `gate_clause(gate)` would resolve `root is None`
    here and 404 the PI's own thread — the leaking-inverse of the feed's own
    carve-out, which already renders this root and counts this reply in the
    badge. `own_or_gated(gate, aid)` must admit both the root and the reply.
    """
    from src.config import get_settings
    from src.services.conversation_feed import resolve_agent_gate
    s = get_settings()
    monkeypatch.setattr(s, "cohort_isolation_enabled", True, raising=False)
    monkeypatch.setattr(s, "cohort_default_policy", "isolated", raising=False)

    pi = await factories.make_user(
        db_session, name="Lonely PI", email="lonelyexpand@example.org"
    )
    await factories.make_agent(
        db_session, user=pi, agent_id="lonely", bot_name="LonelyBot", pi_name="Lonely PI"
    )
    await factories.make_agent(db_session, agent_id="other", bot_name="OtherBot")
    await _cohort(db_session, "other-only", "other")  # "lonely" is deliberately left out

    run = await factories.make_simulation_run(db_session)
    common = dict(run=run, channel_name="general", channel_id="C1", visibility="public")
    await factories.make_agent_message(
        db_session, agent_id="lonely", message_ts="11.0001", phase="new_post",
        content="LONELY-ROOT", sender_name="LonelyBot", **common
    )
    await factories.make_agent_message(
        db_session, agent_id="lonely", message_ts="11.0002", thread_ts="11.0001",
        phase="thread_reply", content="LONELY-OWN-REPLY", sender_name="LonelyBot",
        **common
    )
    await db_session.commit()

    assert await resolve_agent_gate(db_session, "lonely") == set(), (
        "this test targets the isolated-empty-set case specifically"
    )

    r = await client.get("/agent/lonely/thread/11.0001", headers=_auth(pi.id))
    assert r.status_code == 200
    assert "LONELY-OWN-REPLY" in r.text
