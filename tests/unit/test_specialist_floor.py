"""Consults are recorded during the phase-4 interview and read during the
separate phase-5 assessment turn. That seam — two LLM calls apart — is what the
whole enforcement floor rests on.

See docs/specs/2026-08-07-nine-evaluator-panel-design.md §4.
"""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine(*agents):
    return SimulationEngine(agents=list(agents), slack_clients={})


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def test_the_consult_map_starts_empty():
    eng = _engine(_hub())
    assert eng._specialist_consults == {}


def test_recording_a_consult_is_keyed_by_thread():
    eng = _engine(_hub())
    eng._record_consult("t1", "chemistry")
    eng._record_consult("t1", "legal")
    eng._record_consult("t2", "scientific")
    assert eng._specialist_consults["t1"] == {"chemistry", "legal"}
    assert eng._specialist_consults["t2"] == {"scientific"}


def test_recording_the_same_domain_twice_is_idempotent():
    eng = _engine(_hub())
    eng._record_consult("t1", "chemistry")
    eng._record_consult("t1", "chemistry")
    assert eng._specialist_consults["t1"] == {"chemistry"}


def test_consults_for_an_unknown_thread_read_as_empty():
    """The floor reads this for a thread it may never have seen — after a
    restart, for instance. It must not KeyError inside _persist_assessment."""
    eng = _engine(_hub())
    assert eng._consulted_domains("never-seen") == frozenset()
