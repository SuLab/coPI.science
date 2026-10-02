"""Engine soft checks on `key_points` under scout_hub 1.10.0 (prompt item 7;
spec 2026-10-02 §6.1). Each current group keeps exactly one main bullet, may add
one `Risk:` bullet, and `lab_background` may add one `Companies:` bullet. The
engine tells them apart with `classify_key_point`, the classifier the pages
render with, and warns on a missing main bullet, an unlabelled extra, a
repeated label, or a `Companies:` bullet outside `lab_background`. Every check
is log-only: the normalized value comes back, and is stored, unchanged."""
import logging

import pytest

from src.agent.simulation import SimulationEngine

MAIN = {
    "indication_audience": "Canavan disease, an inherited brain disease of infancy.",
    "lab_background": "Barbara Slusher runs the Johns Hopkins Drug Discovery unit.",
    "proposal": "A pill that blocks NAT8L, the enzyme that makes N-acetylaspartate.",
    "clinical_actionability": "Patients today get only supportive care.",
    "path_to_clinic": "Next steps run in cells, then in Canavan mice.",
    "commercial_opportunity": "A licence into a rare-disease company is the likely shape.",
}


def _groups(**extras: list[str]) -> dict[str, list[str]]:
    return {key: [main, *extras.get(key, [])] for key, main in MAIN.items()}


def _check(caplog, key_points):
    sim = SimulationEngine(agents=[], slack_clients={})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        stored = sim.verdicts._normalized_key_points("blackbird", key_points)
    warnings = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    return stored, warnings


def test_a_1_10_0_object_with_risk_and_companies_bullets_warns_nothing(caplog):
    key_points = _groups(
        lab_background=[
            "Companies: A 2024 paper of the lab discloses a research grant from Delfi Diagnostics.",
        ],
        proposal=["Risk: No cell-based potency has been shown yet."],
        commercial_opportunity=[
            "Risk: Johns Hopkins and Delfi Diagnostics may share this IP; the licence "
            "terms would settle it.",
        ],
    )
    stored, warnings = _check(caplog, key_points)
    assert stored == key_points
    assert "key_points" not in warnings


@pytest.mark.parametrize("label", ["Risk:", "risk:", "Risk :", "**Risk:**"])
def test_risk_label_variants_are_the_risk_bullet_not_an_unlabelled_extra(caplog, label):
    """Review Focus #3: case, spacing and bold around the label are the
    classifier's to absorb, so none of them is warned about."""
    key_points = _groups(commercial_opportunity=[f"{label} The licence terms are unknown."])
    stored, warnings = _check(caplog, key_points)
    assert stored == key_points
    assert "key_points" not in warnings


@pytest.mark.parametrize("label", ["Companies:", "companies:", "Companies :", "**Companies:**"])
def test_companies_label_variants_in_lab_background_warn_nothing(caplog, label):
    key_points = _groups(lab_background=[f"{label} Founded Acme Bio (2019 paper)."])
    _stored, warnings = _check(caplog, key_points)
    assert "key_points" not in warnings


def test_an_unlabelled_extra_bullet_is_warned(caplog):
    key_points = _groups(proposal=["It also works in a second disease."])
    stored, warnings = _check(caplog, key_points)
    assert stored == key_points
    assert "key_points.proposal carries 1 unlabelled extra bullet(s)" in warnings


def test_a_repeated_risk_label_is_warned_once_and_not_as_an_extra(caplog):
    key_points = _groups(commercial_opportunity=["Risk: one.", "Risk: two."])
    stored, warnings = _check(caplog, key_points)
    assert stored == key_points
    assert "key_points.commercial_opportunity repeats the Risk: label on 2 bullets" in warnings
    assert warnings.count("repeats the Risk: label") == 1
    assert "unlabelled extra" not in warnings


def test_a_repeated_companies_label_is_warned(caplog):
    key_points = _groups(lab_background=["Companies: Acme.", "Companies: Beta."])
    _stored, warnings = _check(caplog, key_points)
    assert "key_points.lab_background repeats the Companies: label on 2 bullets" in warnings


def test_a_companies_bullet_outside_lab_background_is_warned(caplog):
    key_points = _groups(proposal=["Companies: Acme Bio licensed it."])
    stored, warnings = _check(caplog, key_points)
    assert stored == key_points
    assert (
        "key_points.proposal carries a Companies: bullet (contract allows one only "
        "in lab_background)"
    ) in warnings
    assert "unlabelled extra" not in warnings


def test_a_group_with_only_a_risk_bullet_has_no_main_bullet(caplog):
    key_points = dict(_groups(), path_to_clinic=["Risk: No animal model exists."])
    _stored, warnings = _check(caplog, key_points)
    assert "key_points.path_to_clinic carries 0 main bullet(s) (contract asks for 1" in warnings


def test_an_overlong_labelled_bullet_is_still_warned(caplog):
    long_risk = "Risk: " + "x" * 300
    key_points = _groups(commercial_opportunity=[long_risk])
    stored, warnings = _check(caplog, key_points)
    assert stored == key_points
    assert "key_points.commercial_opportunity has a 306-char bullet" in warnings
