"""Gate explanations on the page (hub 1.10.0, spec §5.2): the Evidence summary's
visible second line (the hub's reason, else "Rubric definition: …"), rubric titles for
every state, no tooltip and no ⓘ, the gating card, and the list card — on a live row
with and without reasons, archived 3.4.0 and 3.2.0 rows, and an unrecognised revision."""
import re

import pytest
from markupsafe import escape

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, OpportunityAssessment
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, load_rubric
from src.services.rubric_revisions import resolve_revision
from tests import factories
from tests.integration.test_assessment_detail_page import _main, _signal_columns, _signals_card
from tests.integration.test_assessment_queue_controls import _details_slice
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_opportunity_assessment_persistence import _gating_state_for

pytestmark = pytest.mark.integration

GATING = {
    "life_sciences_domain": "met",
    "credible_science": "not_met",
    "translational_potential": "unconfirmed",
}
REASONS = {
    "life_sciences_domain": "GATEREASONLIFE a blood test for a cancer decision.",
    "credible_science": "GATEREASONSCIENCE the hits were never re-tested.",
    "translational_potential": "GATEREASONTRANSLATIONAL never asked.",
}
#: Which `_signal_columns` slice (strengths, risks, unestablished) each gate lands in.
COLUMN = {"life_sciences_domain": 0, "credible_science": 1, "translational_potential": 2}
LIVE = (RUBRIC_VERSION, RUBRIC_CONTENT_HASH)
V340 = ("3.4.0", "b7b0a1d6a4a5")
V320 = ("3.2.0", "42aec0479ac6")
UNKNOWN = ("9.9.9", "deadbeef0000")


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


async def _seed(db_session, stamp, reasons=None):
    run = await factories.make_simulation_run(db_session)
    version, content_hash = stamp
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="gate-lines-channel", company_or_project="Gate Lines Co",
        recommendation="conditional", weighted_score=3.2, band="conditional",
        gating=GATING, gating_rationales=reasons,
        rubric_version=version, rubric_content_hash=content_hash,
    )
    db_session.add(row)
    await db_session.flush()
    return run, row


async def _body(client, path, user):
    resp = await client.get(path, headers=auth_headers(user.id))
    assert resp.status_code == 200
    return _main(resp.text)


def _gating_card(body):
    return body[body.index('id="gating"'):body.index('id="red-flags"')]


def _definition_line(description):
    return (
        '<span class="signal-rationale signal-gate-definition block ml-6 text-sm text-gray-600">'
        f"Rubric definition: {escape(description)}</span>"
    )


def _reason_line(reason):
    return f'<span class="signal-rationale block ml-6 text-sm text-gray-600">{escape(reason)}</span>'


@pytest.mark.parametrize("surface", ["admin", "manager"])
async def test_a_live_row_with_reasons_shows_each_reason_under_its_rubric_title(
    client, db_session, admin, surface
):
    _run, row = await _seed(db_session, LIVE, REASONS)
    user = admin
    if surface == "manager":
        user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = await _body(client, f"/{surface}/assessments/{row.id}", user)
    columns = _signal_columns(body)
    live = load_rubric().gating
    for key, reason in REASONS.items():
        assert live[key]["title"] in columns[COLUMN[key]], key
        assert _reason_line(reason) in columns[COLUMN[key]], key
    # A stored reason takes the line; no definition competes with it.
    assert "Rubric definition:" not in _signals_card(body)
    gating = _gating_card(body)
    for key, reason in REASONS.items():
        assert live[key]["title"] in gating, key
        assert f"Rubric definition: {escape(live[key]['description'])}" in gating, key
        assert f"Hub's reason: {escape(reason)}" in gating, key


async def test_gate_rows_carry_no_tooltip_and_no_info_glyph(client, db_session, admin):
    _run, row = await _seed(db_session, LIVE)
    body = await _body(client, f"/admin/assessments/{row.id}", admin)
    tags = re.findall(r'<li class="[^"]*signal-source-gating[^"]*"([^>]*)>', _signals_card(body))
    assert len(tags) == 3
    assert all(extra.strip() == "" for extra in tags), tags
    assert "&#9432;" not in body and "ⓘ" not in body
    legend = re.search(r'<p class="signals-legend[^"]*"[^>]*>.*?</p>', body, re.DOTALL)
    assert legend is not None
    assert "hover" not in legend.group(0)


async def test_a_live_row_without_reasons_shows_every_rubric_definition(client, db_session, admin):
    _run, row = await _seed(db_session, LIVE)
    body = await _body(client, f"/admin/assessments/{row.id}", admin)
    columns = _signal_columns(body)
    live = load_rubric().gating
    for key in GATING:
        assert live[key]["title"] in columns[COLUMN[key]], key
        assert _definition_line(live[key]["description"]) in columns[COLUMN[key]], key
    assert "Hub's reason:" not in _gating_card(body)


@pytest.mark.parametrize("stamp", [V340, V320], ids=["3.4.0", "3.2.0"])
async def test_an_archived_row_shows_its_registry_definitions(client, db_session, admin, stamp):
    _run, row = await _seed(db_session, stamp)
    body = await _body(client, f"/admin/assessments/{row.id}", admin)
    registry = resolve_revision(*stamp)[0].gating
    assert registry, "the registry entry must carry gate tables (Task B1)"
    columns = _signal_columns(body)
    gating = _gating_card(body)
    for key in GATING:
        assert registry[key]["title"] in columns[COLUMN[key]], key
        assert _definition_line(registry[key]["description"]) in columns[COLUMN[key]], key
        assert f"Rubric definition: {escape(registry[key]['description'])}" in gating, key


async def test_an_unrecognised_revision_keeps_bare_labels_and_still_shows_reasons(
    client, db_session, admin
):
    _run, row = await _seed(db_session, UNKNOWN, {"credible_science": REASONS["credible_science"]})
    body = await _body(client, f"/admin/assessments/{row.id}", admin)
    assert "Rubric definition:" not in body
    _strengths, risks, unestablished = _signal_columns(body)
    assert "credible science" in risks
    assert _reason_line(REASONS["credible_science"]) in risks
    assert "translational potential" in unestablished
    gating = _gating_card(body)
    assert "life sciences domain" in gating
    assert f"Hub's reason: {escape(REASONS['credible_science'])}" in gating


async def test_a_malformed_stored_reason_map_falls_back_to_the_definitions(
    client, db_session, admin
):
    _run, row = await _seed(db_session, LIVE, {"credible_science": 5, "life_sciences_domain": "  "})
    body = await _body(client, f"/admin/assessments/{row.id}", admin)
    strengths, risks, _unestablished = _signal_columns(body)
    live = load_rubric().gating
    assert _definition_line(live["credible_science"]["description"]) in risks
    assert _definition_line(live["life_sciences_domain"]["description"]) in strengths


async def test_the_list_card_gives_each_gate_its_own_line(client, db_session, admin):
    run, _row = await _seed(db_session, V340, {"credible_science": REASONS["credible_science"]})
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    scores = _details_slice(html, "assessment-card-scores")
    registry = resolve_revision(*V340)[0].gating
    assert '<span class="gating-row gating-met block text-gray-600">' in scores
    assert registry["life_sciences_domain"]["title"] in scores
    assert (
        '<span class="gating-definition block ml-6">Rubric definition: '
        f'{escape(registry["life_sciences_domain"]["description"])}</span>'
    ) in scores
    # A stored reason replaces the definition on the card's one line per gate.
    assert (
        f'<span class="gating-reason block ml-6">{escape(REASONS["credible_science"])}</span>'
    ) in scores
    assert f'Rubric definition: {escape(registry["credible_science"]["description"])}' not in scores
    assert _gating_state_for(html, registry["credible_science"]["title"]) == "not_met"
    assert _gating_state_for(html, registry["translational_potential"]["title"]) == "unconfirmed"
    assert 'title="Met"' in scores and "&#10060;" in scores and "&#10067;" in scores


async def test_an_unstamped_list_card_keeps_bare_labels_and_no_definition(
    client, db_session, admin
):
    run, _row = await _seed(db_session, (None, None))
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    scores = _details_slice(html, "assessment-card-scores")
    assert _gating_state_for(html, "life sciences domain") == "met"
    assert "Rubric definition:" not in scores
