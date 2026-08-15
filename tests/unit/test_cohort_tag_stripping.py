# tests/unit/test_cohort_tag_stripping.py
"""Whether THIS message had a tag stripped must not be inferred from a global
counter that any other agent's post can bump."""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    # Registered (resolves via _bot_name_to_id) but outside wang's cohort gate,
    # so a message from wang mentioning it is a GENUINE strip, not a no-op on
    # an unresolvable name — see test_a_clean_message_reports_zero_even_after_
    # another_strip below, which depends on this actually moving the shared
    # counter.
    cravatt = Agent("cravatt", "CravattBot", "Cravatt", role="pi_lab")
    eng = SimulationEngine(agents=[hub, lab, cravatt], slack_clients={})
    eng.agents["wang"].allowed_sender_ids = {"wang", "blackbird"}
    eng.agents["blackbird"].allowed_sender_ids = set()
    return eng


def test_strip_reports_its_own_count():
    eng = _engine()
    text, n = eng._strip_disallowed_tags("hello @NobodyBot", eng.agents["wang"])
    assert isinstance(n, int)
    assert n >= 0


def test_a_clean_message_reports_zero_even_after_another_strip():
    """The engine-wide counter is shared across every agent; the per-call
    answer for one agent's clean message must not be inferred from a
    before/after delta on it.

    Fix round 1: the original version of this test used "@NobodyBot", an
    unregistered name that never resolves via _bot_name_to_id and so never
    actually strips anything or bumps ``_cohort_tags_stripped`` — the
    "even after another strip" premise was never established, only assumed.
    "@CravattBot" here is a real, registered agent outside wang's cohort gate,
    so the first call below is asserted to genuinely strip one mention and
    genuinely bump the shared counter before the real assertion runs.
    """
    eng = _engine()
    before = eng._cohort_tags_stripped.get("wang", 0)
    _, first_n = eng._strip_disallowed_tags("hi @CravattBot", eng.agents["wang"])
    assert first_n == 1
    assert eng._cohort_tags_stripped.get("wang", 0) == before + 1, (
        "setup is broken: this call must genuinely bump the shared counter, "
        "or the premise below ('even after another strip') is unestablished"
    )

    # A completely different agent's perfectly clean message must report 0,
    # regardless of the bump above.
    _, n = eng._strip_disallowed_tags(
        "a perfectly clean message", eng.agents["blackbird"]
    )
    assert n == 0, "a clean message must report 0 regardless of another agent's strip"
