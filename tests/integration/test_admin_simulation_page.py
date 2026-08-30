"""T7 — /admin/simulation: the control-plane page (routes + refusal semantics).

Covers the brief's test list (a)-(f):
  (a) not-deployed / stale rendering from derive_panel_state,
  (b) start enqueues a pending command + audit row, a second POST while one
      is pending is refused (no second row written),
  (c) stop is accepted only while the heartbeat reads "running",
  (d) the announce-channels KV round-trips (write, prefill, clear),
  (e) the announce-template editor validates before writing, and reset
      deletes the KV row,
  (f) a manager (non-admin) is refused on every route.

Uses the `client` fixture (get_db routed to the test's own rolled-back
`db_session` — see tests/conftest.py's `asgi_app`), so a route's own
`db.commit()` is visible to a `db_session.execute(...)` read straight after,
and everything rolls back at teardown with no manual cleanup needed (the
same pattern tests/integration/test_star_topology.py uses).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    AdminAuditEvent,
    AppSetting,
    OpportunityAssessment,
    SimulationCommand,
    SimulationProcessStatus,
    ThreadDecision,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _admin(db_session, email):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email=email)


# ---------------------------------------------------------------------------
# (a) status card rendering
# ---------------------------------------------------------------------------


async def test_get_renders_not_deployed_with_no_status_row(client, db_session):
    admin = await _admin(db_session, "sim-admin-a1@example.org")

    resp = await client.get("/admin/simulation", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "not deployed" in resp.text.lower()
    # Nav tab is wired and marked active on this page.
    assert '/admin/simulation' in resp.text
    assert "text-indigo-600 font-semibold" in resp.text


async def test_get_renders_stale_when_heartbeat_is_ten_minutes_old(client, db_session):
    admin = await _admin(db_session, "sim-admin-a2@example.org")
    old = datetime.now(UTC) - timedelta(minutes=10)
    db_session.add(SimulationProcessStatus(id=1, state="running", updated_at=old))
    await db_session.commit()

    resp = await client.get("/admin/simulation", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "STALE" in resp.text


# ---------------------------------------------------------------------------
# (b) start
# ---------------------------------------------------------------------------


async def test_post_start_creates_pending_command_with_payload_and_audit_row(
    client, db_session
):
    admin = await _admin(db_session, "sim-admin-b1@example.org")

    resp = await client.post(
        "/admin/simulation/start",
        data={"fresh": "true", "max_runtime": "30"},
        headers=auth_headers(admin.id),
    )

    assert resp.status_code in (302, 303)
    cmd = (await db_session.execute(select(SimulationCommand))).scalar_one()
    assert cmd.command == "start"
    assert cmd.status == "pending"
    assert cmd.payload == {"fresh": True, "max_runtime": 30}
    assert cmd.requested_by_user_id == admin.id

    audit = (await db_session.execute(select(AdminAuditEvent))).scalar_one()
    assert "start" in audit.action

    # Second POST while the first is still pending is refused; no 2nd row.
    resp2 = await client.post(
        "/admin/simulation/start",
        data={"fresh": "false", "max_runtime": "0"},
        headers=auth_headers(admin.id),
    )
    assert resp2.status_code in (302, 303)
    rows = (await db_session.execute(select(SimulationCommand))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# (c) stop
# ---------------------------------------------------------------------------


async def test_post_stop_with_running_state_creates_command(client, db_session):
    admin = await _admin(db_session, "sim-admin-c1@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="running"))
    await db_session.commit()

    resp = await client.post("/admin/simulation/stop", headers=auth_headers(admin.id))

    assert resp.status_code in (302, 303)
    cmd = (await db_session.execute(select(SimulationCommand))).scalar_one()
    assert cmd.command == "stop"
    assert cmd.status == "pending"


async def test_post_stop_with_idle_state_is_refused(client, db_session):
    admin = await _admin(db_session, "sim-admin-c2@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="idle"))
    await db_session.commit()

    resp = await client.post("/admin/simulation/stop", headers=auth_headers(admin.id))

    assert resp.status_code in (302, 303)
    rows = (await db_session.execute(select(SimulationCommand))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# (d) announce-settings (channels KV)
# ---------------------------------------------------------------------------


async def test_announce_settings_roundtrip(client, db_session):
    admin = await _admin(db_session, "sim-admin-d1@example.org")

    resp = await client.post(
        "/admin/simulation/announce-settings",
        data={"channels": "general,drug-repurposing"},
        headers=auth_headers(admin.id),
    )
    assert resp.status_code in (302, 303)

    row = (
        await db_session.execute(
            select(AppSetting).where(AppSetting.key == "run_start_announce_channels")
        )
    ).scalar_one()
    assert row.value == "general,drug-repurposing"

    html = (await client.get("/admin/simulation", headers=auth_headers(admin.id))).text
    assert "general,drug-repurposing" in html

    resp2 = await client.post(
        "/admin/simulation/announce-settings",
        data={"channels": ""},
        headers=auth_headers(admin.id),
    )
    assert resp2.status_code in (302, 303)
    row2 = (
        await db_session.execute(
            select(AppSetting).where(AppSetting.key == "run_start_announce_channels")
        )
    ).scalar_one_or_none()
    assert row2 is None


async def test_announce_settings_rejects_a_bad_channel_name(client, db_session):
    admin = await _admin(db_session, "sim-admin-d2@example.org")

    resp = await client.post(
        "/admin/simulation/announce-settings",
        data={"channels": "Not Valid!"},
        headers=auth_headers(admin.id),
    )
    assert resp.status_code in (302, 303)
    row = (
        await db_session.execute(
            select(AppSetting).where(AppSetting.key == "run_start_announce_channels")
        )
    ).scalar_one_or_none()
    assert row is None


# ---------------------------------------------------------------------------
# (e) announce-template
# ---------------------------------------------------------------------------


async def test_template_post_bad_placeholder_rerenders_with_error_and_writes_no_kv(
    client, db_session
):
    admin = await _admin(db_session, "sim-admin-e1@example.org")

    resp = await client.post(
        "/admin/simulation/announce-template",
        data={"body": "Bad {nope} template", "reset": "false"},
        headers=auth_headers(admin.id),
    )

    assert resp.status_code == 200  # re-rendered inline, not a redirect
    assert "Bad {nope} template" in resp.text
    assert "KeyError" in resp.text

    row = (
        await db_session.execute(
            select(AppSetting).where(AppSetting.key == "run_start_announcement_template")
        )
    ).scalar_one_or_none()
    assert row is None


async def test_template_post_valid_body_writes_kv_then_reset_deletes_it(client, db_session):
    admin = await _admin(db_session, "sim-admin-e2@example.org")

    resp = await client.post(
        "/admin/simulation/announce-template",
        data={"body": "Custom template {run_id}", "reset": "false"},
        headers=auth_headers(admin.id),
    )
    assert resp.status_code in (302, 303)

    row = (
        await db_session.execute(
            select(AppSetting).where(AppSetting.key == "run_start_announcement_template")
        )
    ).scalar_one()
    assert row.value == "Custom template {run_id}"

    audits = (await db_session.execute(select(AdminAuditEvent))).scalars().all()
    assert any("template" in a.action for a in audits)
    for a in audits:
        # Never the full template text, only a fingerprint.
        assert "Custom template" not in str(a.payload)

    resp2 = await client.post(
        "/admin/simulation/announce-template",
        data={"body": "", "reset": "true"},
        headers=auth_headers(admin.id),
    )
    assert resp2.status_code in (302, 303)
    row2 = (
        await db_session.execute(
            select(AppSetting).where(AppSetting.key == "run_start_announcement_template")
        )
    ).scalar_one_or_none()
    assert row2 is None


# ---------------------------------------------------------------------------
# (f) non-admin refusal
# ---------------------------------------------------------------------------


async def test_non_admin_manager_is_refused_on_every_route(client, db_session):
    manager = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="sim-manager-f1@example.org"
    )
    headers = auth_headers(manager.id)

    assert (await client.get("/admin/simulation", headers=headers)).status_code == 403
    assert (
        await client.post(
            "/admin/simulation/start",
            data={"fresh": "false", "max_runtime": "0"},
            headers=headers,
        )
    ).status_code == 403
    assert (await client.post("/admin/simulation/stop", headers=headers)).status_code == 403
    assert (
        await client.post(
            "/admin/simulation/announce-settings", data={"channels": ""}, headers=headers
        )
    ).status_code == 403
    assert (
        await client.post(
            "/admin/simulation/announce-template",
            data={"body": "", "reset": "false"},
            headers=headers,
        )
    ).status_code == 403


# ---------------------------------------------------------------------------
# Task 11 — the Live tab: stats wired into /admin/simulation.
#
#   (a) a seeded run renders the cost hero with the hand-computed figure
#       (Task 9/8's canonical case: 1M input + 100k output + 500k cache-read
#       + 200k cache-creation on claude-opus-5 = $9.00) and the cache hit-rate
#       meter's detail text (500,000 of 1,500,000 input tokens cached),
#   (b) an unpriced model name appears in a visible warning,
#   (c) headlines-owed renders the count from a seeded un-stamped assessment,
#   (d) `?run=` switches runs — the default (latest) shows one figure, the
#       explicit older run shows the other,
#   (e) an EMPTY run (no calls/messages/assessments at all) renders every
#       section without a 500,
#   (f) the api-call-units caveat string is present wherever total_api_calls
#       shows.
# ---------------------------------------------------------------------------


async def test_live_tab_cost_hero_and_cache_hit_rate_hand_computed(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11a@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_input_tokens=500_000, cache_creation_input_tokens=200_000,
    )
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "$9.00" in resp.text
    assert "500,000 of 1,500,000 input tokens served from cache" in resp.text
    assert "33%" in resp.text


async def test_live_tab_surfaces_unpriced_model_in_a_visible_warning(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11b@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-unknown-42",
        input_tokens=100, output_tokens=10,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "claude-unknown-42" in resp.text
    assert "sc-tile--warn" in resp.text
    assert "Unpriced model" in resp.text


async def test_live_tab_headlines_owed_from_a_terminal_unannounced_assessment(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11c@example.org")
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T1", recommendation="advance", summary_posted_at=None,
    ))
    db_session.add(ThreadDecision(
        simulation_run_id=run.id, thread_id="T1", channel="c",
        agent_a="blackbird", agent_b="labbot", outcome="no_proposal",
    ))
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "Headlines owed" in resp.text
    assert "sc-tile--warn" in resp.text
    assert '<div class="sc-tile-value">1</div>' in resp.text


async def test_live_tab_run_selector_switches_between_runs(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11d@example.org")
    older = await factories.make_simulation_run(
        db_session, started_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    await factories.make_llm_call_log(
        db_session, run=older, model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_input_tokens=500_000, cache_creation_input_tokens=200_000,
    )  # $9.00
    # No OpportunityAssessment rows on either run — a config-stamped run must
    # still show its rubric version in the selector (Finding 2): the stamp is
    # recorded at run-open, not derived from what it happened to score.
    newer = await factories.make_simulation_run(
        db_session, started_at=datetime(2026, 1, 2, tzinfo=UTC),
        config={"rubric_version": "v9.9.9", "rubric_content_hash": "deadbeefcafe"},
    )
    await factories.make_llm_call_log(
        db_session, run=newer, model="claude-sonnet-5",
        input_tokens=25_000, output_tokens=0,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )  # 25_000 * 2 / 1e6 = 0.05
    await db_session.commit()

    default_resp = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    assert default_resp.status_code == 200
    assert "$0.05" in default_resp.text
    assert "$9.00" not in default_resp.text
    assert f'value="{newer.id}"' in default_resp.text
    assert "selected" in default_resp.text
    # The zero-assessment `newer` run's config stamp, not "no verdicts yet".
    assert "v9.9.9 (deadbeefcafe)" in default_resp.text

    older_resp = await client.get(
        f"/admin/simulation?run={older.id}", headers=auth_headers(admin.id)
    )
    assert older_resp.status_code == 200
    assert "$9.00" in older_resp.text


async def test_live_tab_empty_run_renders_every_section_without_a_500(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11e@example.org")
    run = await factories.make_simulation_run(db_session)
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "Interview timeline" in resp.text
    assert "Stop-reason taxonomy" in resp.text
    assert "Hub : lab token burn ratio" in resp.text
    assert "Internal Server Error" not in resp.text


async def test_live_tab_api_call_units_caveat_is_present(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11f@example.org")
    run = await factories.make_simulation_run(db_session, total_api_calls=42)
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "REAL API CALLS" in resp.text
    assert "not turns" in resp.text


# ---------------------------------------------------------------------------
# Review fixes (2026-08-30 re-review):
#   (g) the per-agent table's live columns come from the REAL heartbeat
#       snapshot (`SimulationProcessStatus.detail["agents"]`), reading "—"
#       once that heartbeat goes stale — never a fabricated liveness guess,
#   (h) the hub:lab burn sparkline plots a None ratio (zero lab tokens — the
#       most alarming state, pure hub burn) as a SPIKE at the series' peak,
#       never as a calm 0.0 floor.
# ---------------------------------------------------------------------------


async def test_live_tab_per_agent_table_uses_the_real_heartbeat_snapshot(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11g@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", model="claude-sonnet-5",
        input_tokens=10, output_tokens=1,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    db_session.add(SimulationProcessStatus(
        id=1, state="running", updated_at=datetime.now(UTC),
        detail={"agents": {"labbot": {"active_threads": 3, "calls_in_window": 5}}},
    ))
    await db_session.commit()

    fresh_resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
    assert fresh_resp.status_code == 200
    assert "Active threads" in fresh_resp.text
    assert "Calls in window" in fresh_resp.text
    assert '<td class="px-4 py-2 text-sm text-gray-500">3</td>' in fresh_resp.text
    assert '<td class="px-4 py-2 text-sm text-gray-500">5</td>' in fresh_resp.text

    row = (await db_session.execute(select(SimulationProcessStatus))).scalar_one()
    row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.commit()

    stale_resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
    assert stale_resp.status_code == 200
    assert '<td class="px-4 py-2 text-sm text-gray-500">3</td>' not in stale_resp.text
    assert '<td class="px-4 py-2 text-sm text-gray-500">5</td>' not in stale_resp.text


async def test_live_tab_hub_lab_burn_none_ratio_plots_as_a_spike_not_a_floor(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11h@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub", status="active")

    # Hour 10: hub=1000, lab=500 -> ratio 2.0 (finite).
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-sonnet-5",
        input_tokens=1000, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", model="claude-sonnet-5",
        input_tokens=500, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 10, 15, 0, tzinfo=UTC),
    )
    # Hour 11: hub=700, lab=0 -> ratio None. The alarming hour: pure hub burn.
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-sonnet-5",
        input_tokens=700, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC),
    )
    # Hour 12: hub=2500, lab=500 -> ratio 5.0 (the series' peak finite ratio).
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", model="claude-sonnet-5",
        input_tokens=2500, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="labbot", model="claude-sonnet-5",
        input_tokens=500, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        created_at=datetime(2026, 1, 1, 12, 30, 0, tzinfo=UTC),
    )
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
    assert resp.status_code == 200

    section = resp.text[resp.text.index("Hub : lab token burn ratio"):]
    # The None-ratio hour (11:00) is plotted at 5.0 too — tied for the peak,
    # at/above every finite hour (2.0 and 5.0) — not at 0.0.
    assert section.count("<title>5.0</title>") == 2
    assert "<title>0.0</title>" not in section
    assert "∞ — no lab tokens this hour" in section
