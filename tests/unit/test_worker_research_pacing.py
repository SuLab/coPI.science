"""When the worker runs a project research round (src/worker/main.py ``_research_due``): at
once after a round that made a call, else at most once per PROJECT_SWEEP_SECONDS."""

from src.worker.main import PROJECT_SWEEP_SECONDS, _research_due


def test_a_busy_round_is_followed_at_once():
    assert _research_due(101.0, 100.0, True) is True


def test_an_idle_round_waits_the_sweep_interval():
    assert _research_due(101.0, 100.0, False) is False
    assert _research_due(100.0 + PROJECT_SWEEP_SECONDS, 100.0, False) is True


def test_the_first_pass_researches():
    assert _research_due(0.0, None, False) is True
