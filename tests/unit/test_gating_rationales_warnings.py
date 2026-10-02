"""The engine's write-time checks on `gating_rationales` (sidecar item 1's
companion, scout_hub 1.10.0, migration 0058), driven through
`Verdicts._normalized_gating_rationales` with no database.

Warnings only, the policy `_normalized_dimension_rationales` applies to the
dimensions (A4): the value is normalized by `normalize_gating_rationales` and
returned for storage; nothing here drops a verdict."""
import logging

from src.agent.simulation import SimulationEngine

GATES = {
    "life_sciences_domain": "met",
    "credible_science": "met",
    "translational_potential": "unconfirmed",
}
REASONS = {
    "life_sciences_domain": "A small-molecule therapeutic for an inherited brain disease.",
    "credible_science": "A published 36,000-compound screen and a knockout-mouse rescue.",
    "translational_potential": "Never asked whether the lab would license or found a company.",
}


def _check(caplog, verdict, gating):
    sim = SimulationEngine(agents=[], slack_clients={})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        stored = sim.verdicts._normalized_gating_rationales("blackbird", verdict, gating)
    warnings = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    return stored, warnings


def test_a_reason_for_every_gate_is_returned_and_warns_nothing(caplog):
    stored, warnings = _check(caplog, {"gating_rationales": REASONS}, GATES)
    assert stored == REASONS
    assert warnings == ""


def test_a_gate_with_no_reason_is_named(caplog):
    partial = {k: v for k, v in REASONS.items() if k != "translational_potential"}
    stored, warnings = _check(caplog, {"gating_rationales": partial}, GATES)
    assert stored == partial
    assert "has 1 gate(s) with no reason: translational_potential" in warnings
    assert "DROPPED" not in warnings


def test_a_1_9_0_sidecar_names_every_gate_and_is_not_a_drop(caplog):
    """A sidecar from a stale prompt carries no `gating_rationales` at all:
    NULL is stored, and the warning says which gates went unexplained."""
    stored, warnings = _check(caplog, {}, GATES)
    assert stored is None
    assert (
        "has 3 gate(s) with no reason: credible_science, life_sciences_domain, "
        "translational_potential"
    ) in warnings
    assert "DROPPED" not in warnings


def test_an_unfilled_skeleton_is_named_per_gate_not_dropped(caplog):
    blank = {k: "" for k in GATES}
    stored, warnings = _check(caplog, {"gating_rationales": blank}, GATES)
    assert stored is None
    assert "has 3 gate(s) with no reason" in warnings
    assert "gating_rationales was DROPPED" not in warnings


def test_a_malformed_map_is_a_named_drop(caplog):
    stored, warnings = _check(caplog, {"gating_rationales": {"credible_science": 3}}, GATES)
    assert stored is None
    assert "gating_rationales was DROPPED" in warnings


def test_the_200_character_bound_warns_only_past_it(caplog):
    reasons = dict(REASONS, credible_science="x" * 201, life_sciences_domain="y" * 200)
    stored, warnings = _check(caplog, {"gating_rationales": reasons}, GATES)
    assert stored == reasons, "an over-long reason is stored, never clipped"
    assert "gating_rationales.credible_science is 201 chars (contract asks for <=200)" in warnings
    assert "gating_rationales.life_sciences_domain is" not in warnings


def test_a_differently_cased_key_still_explains_its_gate(caplog):
    """The normalizer lower-cases keys, so `Credible_Science` explains the
    `credible_science` gate and must not be reported as missing."""
    reasons = {"Credible_Science": "Two cohorts.", "life_sciences_domain": "A drug.",
               "translational_potential": "Never asked."}
    _stored, warnings = _check(caplog, {"gating_rationales": reasons}, GATES)
    assert "with no reason" not in warnings


def test_colliding_keys_are_warned(caplog):
    reasons = {"Credible_Science": "first", "credible_science": "second"}
    _stored, warnings = _check(
        caplog, {"gating_rationales": reasons}, {"credible_science": "met"},
    )
    assert "collide after lower-casing (credible_science)" in warnings


def test_no_gates_and_no_reasons_warn_nothing(caplog):
    stored, warnings = _check(caplog, {}, None)
    assert stored is None
    assert warnings == ""
