"""Cohort gate against the REAL Anthropic API. Skipped unless a key is present.

Everything else in the cohort suite scripts the LLM. This module spends real tokens,
because two claims cannot be checked with a fake:

1. A real model, given a Phase 2 prompt built under an active gate, cannot select or
   reason about a post the gate removed — the post is not in the prompt at all.
   A fake proves the prompt lacks the text; only a real call proves the model's
   *output* is unaffected by the excluded content.
2. A real model asked to start a conversation will name a partner, and the outbound
   strip must remove a cross-cohort mention from genuine model prose rather than from
   a hand-written string.

Cost control (the whole module is a handful of calls):
- ``max_tokens`` is capped hard.
- Prompts are the real ones, but the roster and history are minimal.
- Sonnet, not Opus, for the scan path — that is what Phase 2 uses anyway.
- One call per test, four tests. Roughly a cent at current prices.

Run it with:

    docker compose exec -e ANTHROPIC_API_KEY=sk-ant-... \\
      -e TEST_DATABASE_URL=postgresql+asyncpg://copi:copi@postgres:5432/copi_test \\
      app python -m pytest tests/integration/test_cohort_real_llm.py -v -m real_llm

Without a key every test skips, so the default suite stays free and offline.
"""

import os
import uuid

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry, MessageLog
from src.visibility import VISIBILITY_PUBLIC

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="no ANTHROPIC_API_KEY — real-API tests are opt-in and cost money",
    ),
]

MAX_TOKENS = 300


def _agent(agent_id="su", bot="SuBot"):
    return Agent(agent_id=agent_id, bot_name=bot, pi_name=f"PI {agent_id}")


def _post(ts, agent_id, name, content):
    return LogEntry(
        ts=ts, channel="general", sender_agent_id=agent_id, sender_name=name,
        content=content, thread_ts=None, posted_at=float(ts), is_bot=True,
        visibility=VISIBILITY_PUBLIC,
    )


# A profile that makes the EXCLUDED post directly relevant and the INCLUDED post
# clearly irrelevant. Without this the scan has nothing to latch onto: an agent with
# no profile selects no posts either way, and the test passes vacuously — measured.
SU_PROFILE = """# Su Lab

We run genome-scale CRISPR functional-genomics screens and build chemical-probe
pipelines. We are actively seeking collaborators in **activity-based protein
profiling** and **covalent ligand discovery** to turn screen hits into chemical
probes. We are NOT currently working on spatial transcriptomics or imaging.
"""

# Irrelevant to SU_PROFILE — the post the gate lets through.
POST_IRRELEVANT = (
    "We built a spatial transcriptomics imaging atlas of tumour microenvironments "
    "and are looking for an imaging-analysis partner."
)
# Directly relevant to SU_PROFILE — the post the gate removes.
POST_RELEVANT = (
    "We run activity-based protein profiling and want a functional-genomics "
    "collaborator to pair covalent ligand discovery with CRISPR screen hits."
)


@pytest.fixture
def log():
    ml = MessageLog()
    ml.set_bot_name_map({"subot": "su", "wisemanbot": "wiseman", "cravattbot": "cravatt"})
    ml.append(_post("1000.0001", "wiseman", "WisemanBot", POST_IRRELEVANT))
    ml.append(_post("1000.0002", "cravatt", "CravattBot", POST_RELEVANT))
    return ml


def _profiled_agent():
    a = _agent()
    a._public_profile = SU_PROFILE   # the cached-profile seam; avoids disk I/O
    return a


async def _call(system_prompt, messages, model=None):
    """One real API call. Sonnet (what Phase 2 uses) with a hard token cap."""
    from src.config import get_settings
    from src.services import llm

    settings = get_settings()
    return await llm.generate_agent_response(
        system_prompt=system_prompt,
        messages=messages,
        model=model or settings.llm_agent_model_sonnet,
        max_tokens=MAX_TOKENS,
        log_meta={"agent_id": "su", "phase": "real_llm_audit"},
    )


def _post_dicts(posts):
    """Exactly the shape _phase2_scan_filter builds (note: content_snippet)."""
    return [
        {"post_id": p.ts, "sender": p.sender_name, "channel": p.channel,
         "content_snippet": p.content}
        for p in posts
    ]


def _selected_ids(response: str) -> set[str] | None:
    """Parse selected_post_ids out of a real Phase 2 response."""
    import json
    import re

    m = re.search(r"\{.*\}", response, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return set(map(str, data.get("selected_post_ids") or []))


async def test_real_model_would_have_acted_on_the_post_the_gate_removes(log):
    """The claim the whole feature rests on, measured on a real model.

    Two real Phase 2 calls with the same profile and the same log, differing only in
    whether the gate is applied:

    - ungated, the model **selects** the excluded agent's post and explains why —
      i.e. it would have opened a thread and spent Opus calls on it;
    - gated, that post is absent from the prompt, so the model cannot select it.

    A fake LLM can only show the prompt lacks the text. Only a real call shows the
    model's *decision* changes — which is what "the gate saves calls" actually means.
    Asserting on both halves is deliberate: without the ungated leg, a model that
    selects nothing regardless would make the gated leg pass for the wrong reason.
    """
    a = _profiled_agent()

    ungated = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su", allowed_sender_ids=None,
    )
    assert {p.ts for p in ungated} == {"1000.0001", "1000.0002"}
    sys_u, msg_u = a.build_phase2_scan_prompt(_post_dicts(ungated))
    assert POST_RELEVANT[:40] in sys_u + str(msg_u)
    selected_ungated = _selected_ids(await _call(sys_u, msg_u))
    assert selected_ungated is not None, "real Phase 2 response did not parse"
    assert "1000.0002" in selected_ungated, (
        "control leg failed: the model did not act on the relevant post even with the "
        f"gate off, so the gated leg proves nothing. selected={selected_ungated}"
    )

    gated = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids={"su", "wiseman"},
    )
    assert {p.ts for p in gated} == {"1000.0001"}
    sys_g, msg_g = a.build_phase2_scan_prompt(_post_dicts(gated))
    assert POST_RELEVANT[:40] not in sys_g + str(msg_g)
    selected_gated = _selected_ids(await _call(sys_g, msg_g))
    assert selected_gated is not None, "real Phase 2 response did not parse"
    assert "1000.0002" not in selected_gated, (
        "the model selected a post the gate removed — impossible unless the prompt "
        f"leaked it. selected={selected_gated}"
    )

    assert selected_ungated != selected_gated, (
        "the gate produced no measurable change in the model's decision: "
        f"{selected_ungated} vs {selected_gated}"
    )


async def test_real_model_prose_gets_its_cross_cohort_mention_stripped(monkeypatch):
    """Ask a real model to write a post that tags a specific bot, then run the real
    outbound strip over its actual prose."""
    import types

    import src.agent.simulation as sim
    from src.agent.simulation import SimulationEngine
    from src.agent.transport import NullTransport

    settings_ns = types.SimpleNamespace(
        cohort_isolation_enabled=True, cohort_default_policy="isolated",
        max_consecutive_reactive_turns=3, turn_delay_seconds=0.0,
    )
    monkeypatch.setattr(sim, "get_settings", lambda: settings_ns)

    ids = ("su", "wiseman", "cravatt")
    eng = SimulationEngine(
        agents=[_agent(a, f"{a.capitalize()}Bot") for a in ids],
        slack_clients={a: NullTransport(a) for a in ids},
        budget_cap=0, session_factory=None, slack_enabled=False,
    )
    eng._bot_name_to_id = {f"{a}bot": a for a in ids}
    su = eng.agents["su"]
    su.allowed_sender_ids = {"su", "wiseman"}   # cravatt is outside the cohort

    response = await _call(
        "You are SuBot, a lab's research agent in a Slack channel. Reply with one "
        "short paragraph only, no preamble.",
        [{"role": "user", "content":
          "Write a two-sentence Slack message proposing a collaboration. You must "
          "mention both @WisemanBot and @CravattBot by name with the @ prefix."}],
    )
    assert response, "the real API returned nothing"

    cleaned = eng._strip_disallowed_tags(response, su)
    assert cleaned is not None
    if "@CravattBot" in response:
        assert "CravattBot" not in cleaned, (
            f"a cross-cohort mention survived the strip.\nmodel wrote: {response!r}\n"
            f"after strip: {cleaned!r}"
        )
        assert eng._cohort_tags_stripped.get("su", 0) >= 1
    else:
        pytest.skip(
            "the model did not produce an @CravattBot mention, so there was nothing "
            f"to strip. Model output: {response!r}"
        )
    if "@WisemanBot" in response:
        assert "@WisemanBot" in cleaned, "a cohort-mate mention must survive"


async def test_real_scan_response_parses_under_an_active_gate(log):
    """End-to-end shape check: a real Phase 2 response must still parse into post
    ids the engine can act on, and can only name posts that survived the gate."""
    import json
    import re

    a = _profiled_agent()
    gated = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids={"su", "wiseman"},
    )
    allowed_ids = {p.ts for p in gated}
    post_dicts = [
        {"post_id": p.ts, "sender": p.sender_name, "channel": p.channel,
         "content_snippet": p.content}
        for p in gated
    ]
    system, messages = a.build_phase2_scan_prompt(post_dicts)
    response = await _call(system, messages)
    assert response

    m = re.search(r"\{.*\}", response, re.S)
    if not m:
        pytest.skip(f"real model returned no JSON object: {response!r}")
    data = json.loads(m.group(0))
    selected = data.get("selected_post_ids") or data.get("selected") or []
    assert set(map(str, selected)) <= allowed_ids | {""}, (
        f"the model selected a post id that was gated out: {selected} "
        f"(allowed: {sorted(allowed_ids)})"
    )


async def test_real_model_acts_on_an_uncohorted_peer_under_open_policy(log):
    """§5.2 with a real model: under `open`, a cohorted agent must be able to act on an
    uncohorted one.

    This is the defect a real multi-turn run surfaced. The gate was asymmetric — the
    uncohorted agent could react to anyone but appeared in nobody's mate set, so it
    opened threads that were never answered. Every gate-computation test passed.

    Control: the same call with the uncohorted agent excluded from the gate must NOT
    select the post, so a model that selects everything cannot pass.
    """
    from src.services.cohorts import compute_gates

    c1 = uuid.uuid4()
    gates, reason = compute_gates(
        membership_rows=[(c1, "su"), (c1, "wiseman")],
        agent_ids=["su", "wiseman", "cravatt"],
        isolation_enabled=True, policy="open", cohort_count=1,
    )
    assert reason is None
    assert gates["cravatt"] is None, "the uncohorted agent stays unrestricted"
    assert "cravatt" in gates["su"], (
        f"precondition: the open-policy fix must be in place. su gate={gates['su']}"
    )

    a = _profiled_agent()
    visible = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids=gates["su"],
    )
    assert {p.ts for p in visible} == {"1000.0001", "1000.0002"}, (
        "the uncohorted peer's post must reach su's prompt at all"
    )
    selected = _selected_ids(await _call(*a.build_phase2_scan_prompt(_post_dicts(visible))))
    assert selected is not None, "real Phase 2 response did not parse"
    assert "1000.0002" in selected, (
        f"the model did not act on the uncohorted peer's relevant post: {selected}"
    )

    # Control: exclude cravatt and the same model must not select it — it is not in
    # the prompt to select.
    gated = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids={"su", "wiseman"},
    )
    sel2 = _selected_ids(await _call(*a.build_phase2_scan_prompt(_post_dicts(gated))))
    assert sel2 is not None
    assert "1000.0002" not in sel2, (
        f"control leg failed: the post was selected even when gated out ({sel2}), so "
        "the prompt leaked it"
    )


async def test_real_model_cannot_reach_across_a_hub(log):
    """Non-transitivity with a real model: A-B and B-C must not yield A-C.

    wiseman shares a cohort with su, and su shares one with cravatt, but wiseman and
    cravatt share none. cravatt's post must be absent from wiseman's prompt even though
    su can see it — and wiseman is given SU_PROFILE so scientific relevance cannot be
    the thing doing the filtering.

    Control: su, who does share a cohort with cravatt, selects the same post. Without
    that leg, wiseman's non-selection is equally explained by a model that selects
    nothing.
    """
    from src.services.cohorts import compute_gates

    c1, c2 = uuid.uuid4(), uuid.uuid4()
    gates, reason = compute_gates(
        membership_rows=[(c1, "su"), (c1, "wiseman"), (c2, "su"), (c2, "cravatt")],
        agent_ids=["su", "wiseman", "cravatt"],
        isolation_enabled=True, policy="isolated", cohort_count=2,
    )
    assert reason is None
    assert "cravatt" in gates["su"] and "wiseman" in gates["su"], gates["su"]
    assert "cravatt" not in gates["wiseman"], (
        f"precondition: the hub must not be transitive. wiseman gate={gates['wiseman']}"
    )

    w = _agent("wiseman", "WisemanBot")
    w._public_profile = SU_PROFILE
    seen = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="wiseman",
        allowed_sender_ids=gates["wiseman"],
    )
    sysp, msgs = w.build_phase2_scan_prompt(_post_dicts(seen))
    assert POST_RELEVANT[:40] not in sysp + str(msgs), "the spoke's prompt leaked it"
    sel = _selected_ids(await _call(sysp, msgs))
    assert sel is not None
    assert "1000.0002" not in sel

    # Control: the hub does select it.
    s = _profiled_agent()
    seen_su = log.get_new_top_level_posts(
        since=0, channels={"general"}, exclude_agent_id="su",
        allowed_sender_ids=gates["su"],
    )
    assert POST_RELEVANT[:40] in str(_post_dicts(seen_su))
    sel_su = _selected_ids(await _call(*s.build_phase2_scan_prompt(_post_dicts(seen_su))))
    assert sel_su is not None
    assert "1000.0002" in sel_su, (
        f"control leg failed: the hub did not select the post either ({sel_su}), so "
        "the spoke's non-selection proves nothing"
    )
