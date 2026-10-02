"""The page-only gate keys `build_assessment_detail` adds (hub 1.10.0, spec §5.3):
`gating_definitions` follows the row's revision, `gating_reasons` its stored map, and
`gating_descriptions` (read by the assessment chat record) keeps its live-only meaning."""
import pytest

from src.models import OpportunityAssessment
from src.services.assessment_detail import build_assessment_detail
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, load_rubric
from tests import factories

pytestmark = pytest.mark.integration

GATING = {
    "life_sciences_domain": "met",
    "credible_science": "not_met",
    "translational_potential": "unconfirmed",
}
STAMPS = {
    "live": (RUBRIC_VERSION, RUBRIC_CONTENT_HASH),
    "3.4.0": ("3.4.0", "b7b0a1d6a4a5"),
    "3.2.0": ("3.2.0", "42aec0479ac6"),
    "unknown": ("9.9.9", "deadbeef0000"),
    "unstamped": (None, None),
}


async def _row(db_session, stamp, reasons):
    run = await factories.make_simulation_run(db_session)
    version, content_hash = stamp
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="gate-context-channel", company_or_project="Gate Context Co",
        gating=GATING, gating_rationales=reasons,
        rubric_version=version, rubric_content_hash=content_hash,
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.mark.parametrize("stamp_name, defined", [
    ("live", True), ("3.4.0", True), ("3.2.0", True), ("unknown", False), ("unstamped", False),
])
async def test_the_page_only_gate_keys_follow_the_rows_revision(db_session, stamp_name, defined):
    row = await _row(
        db_session, STAMPS[stamp_name], {"Credible_Science": " A reason. ", "not_a_gate": "x"}
    )
    detail = await build_assessment_detail(db_session, row.id, admin_view=False)
    assert detail["gating_definitions"] == (load_rubric().gating if defined else {})
    # Keyed by the row's own `gating` keys: a reason for a key the row has no gate
    # for attaches to nothing.
    assert detail["gating_reasons"] == {"credible_science": "A reason."}
    # The chat record's key keeps its live-only meaning (spec §5.3).
    assert detail["gating_descriptions"] == (
        load_rubric().gating if stamp_name == "live" else {}
    )


async def test_a_malformed_stored_reason_map_gives_the_page_no_reasons(db_session):
    row = await _row(db_session, STAMPS["live"], ["not", "a", "map"])
    detail = await build_assessment_detail(db_session, row.id, admin_view=False)
    assert detail["gating_reasons"] == {}
    gates = [
        entry
        for bucket in ("strengths", "risks", "unestablished")
        for entry in detail["verdict_signals"][bucket]
        if entry["source"] == "gating"
    ]
    assert len(gates) == 3
    assert all(entry["rationale"] is None for entry in gates)
