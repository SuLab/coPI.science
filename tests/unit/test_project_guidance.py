"""The bands of a project thread's turns (spec §5.8, D55): `project_turn`."""

import pytest

from src.agent.project_guidance import (
    HUB_CLOSE_FROM,
    HUB_EARLY_CLOSE_FROM,
    LAB_FINAL_FROM,
    OPEN_UNTIL,
    THREAD_KIND_INTERVIEW,
    THREAD_KIND_PROJECT,
    project_turn,
)

HUB = "scout_hub"
LAB = "pi_lab"
EARLY_CLOSE = "The lab's latest reply starts with ✅: it agrees"
HOLD_CLOSE = "it is too early to close and you have turns left"

_HUB_BANDS = {
    2: ("open", "OPEN", "project_open"),
    4: ("open", "OPEN", "project_open"),
    5: ("refine", "REFINE", "project_refine"),
    8: ("refine", "REFINE", "project_refine"),
    9: ("refine", "REFINE", "project_refine"),
    10: ("refine", "REFINE", "project_refine"),
    11: ("close", "MUST CLOSE", "project_close"),
    12: ("close", "MUST CLOSE", "project_close"),
}
_LAB_BANDS = {
    2: ("open", "OPEN", "project_open"),
    4: ("open", "OPEN", "project_open"),
    5: ("refine", "REFINE", "project_refine"),
    8: ("refine", "REFINE", "project_refine"),
    9: ("final", "FINAL WORD", "project_final"),
    10: ("final", "FINAL WORD", "project_final"),
    11: ("final", "FINAL WORD", "project_final"),
    12: ("final", "FINAL WORD", "project_final"),
}


def test_constants():
    assert (THREAD_KIND_INTERVIEW, THREAD_KIND_PROJECT) == ("interview", "project")
    assert (OPEN_UNTIL, HUB_CLOSE_FROM, LAB_FINAL_FROM) == (4, 11, 9)
    assert HUB_EARLY_CLOSE_FROM == 8


@pytest.mark.parametrize("ordinal", sorted(_HUB_BANDS))
def test_hub_bands(ordinal):
    turn = project_turn(HUB, ordinal, lab_agrees=False)
    assert (turn.band, turn.label, turn.log_phase) == _HUB_BANDS[ordinal]
    assert turn.must_close is (ordinal >= HUB_CLOSE_FROM)
    assert turn.may_close_early is False


@pytest.mark.parametrize("ordinal", sorted(_LAB_BANDS))
@pytest.mark.parametrize("lab_agrees", [False, True])
def test_lab_bands_never_close(ordinal, lab_agrees):
    turn = project_turn(LAB, ordinal, lab_agrees=lab_agrees)
    assert (turn.band, turn.label, turn.log_phase) == _LAB_BANDS[ordinal]
    assert turn.must_close is False
    assert turn.may_close_early is False
    assert EARLY_CLOSE not in turn.guidance


def test_exact_strings():
    hub_close = project_turn(HUB, 11, lab_agrees=False)
    assert hub_close.guidance == "This is your last turn: you MUST close the project now."
    assert hub_close.instructions == (
        "Start the reply with ✅ if you agree the plan is feasible and complete, or ⚠️ if you "
        "do not; state the final plan; then add the <project_json> block with every field, "
        "the gaps and the disagreements."
    )
    assert project_turn(HUB, 5, lab_agrees=False).instructions == (
        "Write a reply that settles the next open field or disagreement."
    )
    assert project_turn(LAB, 9, lab_agrees=False).guidance == (
        "This may be your last turn before BlackbirdBot closes."
    )
    assert project_turn(LAB, 1, lab_agrees=False).guidance == (
        "Open: say what your lab would use for this project and what you would change in the "
        "opening plan."
    )


@pytest.mark.parametrize("ordinal", [8, 10])
def test_hub_may_close_early_from_the_gate_on_one_agreement(ordinal):
    agreed = project_turn(HUB, ordinal, lab_agrees=True)
    plain = project_turn(HUB, ordinal, lab_agrees=False)
    assert agreed.may_close_early is True
    assert plain.may_close_early is False
    assert EARLY_CLOSE in agreed.guidance
    assert EARLY_CLOSE not in plain.guidance
    assert agreed.guidance.startswith(plain.guidance)


@pytest.mark.parametrize("ordinal", [2, 4, 5, 7])
def test_before_the_gate_one_agreement_is_not_enough(ordinal):
    """D77: the lab's single ✅ holds the close, and the hub is told what to spend the turn
    on. Run b70d0cff closed at a median ordinal of 6 of 12 on exactly this signal."""
    once = project_turn(HUB, ordinal, lab_agrees=True)
    assert once.may_close_early is False
    assert EARLY_CLOSE not in once.guidance
    assert HOLD_CLOSE in once.guidance


@pytest.mark.parametrize("ordinal", [2, 4, 5, 7])
def test_before_the_gate_two_agreements_in_a_row_are(ordinal):
    twice = project_turn(HUB, ordinal, lab_agrees=True, lab_agreed_before=True)
    assert twice.may_close_early is True
    assert EARLY_CLOSE in twice.guidance
    assert HOLD_CLOSE not in twice.guidance


@pytest.mark.parametrize("ordinal", [2, 7, 8, 12])
def test_a_second_agreement_without_a_latest_one_closes_nothing(ordinal):
    """``lab_agreed_before`` is never the whole answer: the lab's latest post decides."""
    turn = project_turn(HUB, ordinal, lab_agrees=False, lab_agreed_before=True)
    assert turn.may_close_early is False
    assert EARLY_CLOSE not in turn.guidance
    assert HOLD_CLOSE not in turn.guidance


@pytest.mark.parametrize("ordinal", sorted(_LAB_BANDS))
def test_the_lab_never_closes_early_however_it_agreed(ordinal):
    turn = project_turn(LAB, ordinal, lab_agrees=True, lab_agreed_before=True)
    assert turn.may_close_early is False
    assert EARLY_CLOSE not in turn.guidance
    assert HOLD_CLOSE not in turn.guidance


@pytest.mark.parametrize("ordinal", [11, 12])
def test_close_band_ignores_the_lab_position(ordinal):
    turn = project_turn(HUB, ordinal, lab_agrees=True)
    assert turn.must_close is True
    assert turn.may_close_early is False
    assert EARLY_CLOSE not in turn.guidance


def test_every_log_phase_fits_the_column():
    """``llm_call_logs.thread_phase`` is String(20)."""
    phases = {
        project_turn(role, ordinal, lab_agrees=agrees).log_phase
        for role in (HUB, LAB) for ordinal in range(1, 14) for agrees in (False, True)
    }
    assert phases == {"project_open", "project_refine", "project_close", "project_final"}
    assert all(len(p) <= 20 for p in phases)
