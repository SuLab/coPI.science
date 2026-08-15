# tests/unit/test_cohort_tag_stripping.py
"""Whether THIS message had a tag stripped must not be inferred from a global
counter that any other agent's post can bump."""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(agents=[hub, lab], slack_clients={})
    eng.agents["wang"].allowed_sender_ids = {"wang", "blackbird"}
    return eng


def test_strip_reports_its_own_count():
    eng = _engine()
    text, n = eng._strip_disallowed_tags("hello @NobodyBot", eng.agents["wang"])
    assert isinstance(n, int)
    assert n >= 0


def test_a_clean_message_reports_zero_even_after_another_strip():
    """The global counter is shared; the per-call answer must not be."""
    eng = _engine()
    eng._strip_disallowed_tags("hi @NobodyBot", eng.agents["wang"])   # bumps global
    _, n = eng._strip_disallowed_tags("a perfectly clean message", eng.agents["wang"])
    assert n == 0, "a clean message must report 0 regardless of other agents' posts"
