"""The one-line #assessments-summary headline of a hub decline without an assessment
(owner decision 2026-10-16)."""
from src.services.assessment_headline import (
    DECLINE_REASON_DISPLAY_CHARS,
    decline_reason,
    render_decline_headline,
)

HU_STYLE = (
    "⏸️ I'm going to decline this one, and I want to be specific about why.\n\n"
    "Your gating status on our criteria: credible science **met** — candidly characterised; "
    "life-sciences/biomedical **not met** as framed; translational potential "
    "**unconfirmed**. Recommendation: **decline**."
)


def test_named_gates_are_the_reason():
    assert decline_reason(HU_STYLE) == (
        "Not met: Life-sciences / biomedical; unconfirmed: Translational potential."
    )


def test_a_met_gate_is_not_listed():
    assert decline_reason("⏸️ Credible science: met. Translational potential: unconfirmed.") == (
        "Unconfirmed: Translational potential."
    )


def test_without_gates_the_first_sentence_is_the_reason():
    reply = "⏸️ Good work, but no distinct novelty angle beyond X. Come back with Y.\n\nMore."
    assert decline_reason(reply) == "Good work, but no distinct novelty angle beyond X."


def test_the_reason_is_clipped_and_non_strings_are_none():
    assert len(decline_reason("⏸️ " + "word " * 200)) <= DECLINE_REASON_DISPLAY_CHARS
    assert decline_reason(None) is None
    assert decline_reason({"x": 1}) is None


def test_render():
    assert render_decline_headline(pi_label="Ada Lab", reason="Not met: X.", permalink="https://p") == (
        "⏸️ Ada Lab — declined without an assessment. Not met: X. — <https://p|View interview>"
    )
    assert render_decline_headline(pi_label="Ada Lab", reason=None, permalink=None) == (
        "⏸️ Ada Lab — declined without an assessment (link unavailable)"
    )


def test_a_status_never_lends_itself_to_the_next_gate():
    reply = "⏸️ Not met: Credible science; unconfirmed: Translational potential."
    assert decline_reason(reply) == "Not met: Credible science; unconfirmed: Translational potential."


def test_negated_and_prefixed_statuses():
    assert decline_reason("Life-sciences / biomedical has not been met; credible science is met.") == (
        "Not met: Life-sciences / biomedical.")
    assert decline_reason("The life-sciences/biomedical gate is unmet.") == (
        "Not met: Life-sciences / biomedical.")
    assert decline_reason("Translational potential: not yet met; credible science: met.") == (
        "Not met: Translational potential.")


def test_an_unrelated_met_is_not_a_status():
    reply = "Credible science: we met with the PI twice. Translational potential remains unconfirmed."
    assert decline_reason(reply) == "Unconfirmed: Translational potential."
