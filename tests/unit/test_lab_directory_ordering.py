"""The lab directory must be gate-scoped in the order production builds it.

src/agent/simulation.py's _build_lab_directories filters the directory by allowed_sender_ids, but
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


# --- the two that pin the actual bug ----------------------------------------
#
# Everything above calls refresh_lab_directories() by hand, which is what the
# PRE-EXISTING test did — and it is why the bug survived. The predicate was
# never broken; the ORDER was. These two guard the order.


def test_start_computes_the_gate_before_it_builds_the_directory():
    """A source assertion, deliberately.

    start() does too much I/O to drive in a unit test, and the failure mode is a
    reordering — exactly the edit a future refactor makes silently, and exactly
    what no behavioural test in this file would catch. Reading the source is
    crude but it is the thing that was actually wrong.
    """
    import inspect

    src = inspect.getsource(SimulationEngine.start)
    gate = src.index("_recompute_allowed_sender_ids")
    build = src.index("refresh_lab_directories")
    assert gate < build, (
        "start() builds the lab directory before computing the cohort gate; "
        "every agent's allowed_sender_ids is still None at that point, so the "
        "filter inside _build_lab_directories no-ops"
    )


async def test_recompute_refreshes_the_directory_when_it_disables_the_gate(monkeypatch):
    """The durable half of the fix, driven through the real method.

    _recompute_allowed_sender_ids owns the gate, so it must own the directory
    derived from it. The isolation-disabled path is the cheap way to prove that
    without a database: it sets every gate to None, and the directory must widen
    to match instead of staying scoped to a gate that no longer applies.
    """
    import types

    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: types.SimpleNamespace(cohort_isolation_enabled=False),
    )
    a, b = _agent("a", "paper A"), _agent("b", "paper B")
    a.allowed_sender_ids = {"a"}          # isolated under the old topology
    b.allowed_sender_ids = {"b"}
    eng = SimulationEngine(agents=[a, b], slack_clients={})
    eng.refresh_lab_directories()
    assert a._lab_directory is None       # correctly empty while isolated

    await eng._recompute_allowed_sender_ids()

    assert a.allowed_sender_ids is None
    assert "paper B" in (a._lab_directory or ""), (
        "the gate was disabled but the directory still reflects the old one"
    )
