"""T7 — /admin/simulation: the control-plane page (routes + refusal semantics).

Covers the brief's test list (a)-(f):
  (a) not-deployed / stale rendering from derive_panel_state,
  (b) start enqueues a pending command + audit row, a second POST while one
      is pending is refused (no second row written),
  (c) stop is accepted only while the heartbeat reads "running",
  (d) the announce-channels KV round-trips (write, prefill, explicit disable,
      clear),
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

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    AdminAuditEvent,
    AppSetting,
    AssessmentDrop,
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
        data={"fresh": "true", "max_runtime": "30", "max_proposals": "4"},
        headers=auth_headers(admin.id),
    )

    assert resp.status_code in (302, 303)
    cmd = (await db_session.execute(select(SimulationCommand))).scalar_one()
    assert cmd.command == "start"
    assert cmd.status == "pending"
    assert cmd.payload == {"fresh": True, "max_runtime": 30, "max_proposals": 4}
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

    # Disable: an explicit `disable` checkbox writes the override as "" —
    # distinct from clearing it outright, and the engine already treats an
    # empty-string override as "skip the announcement"
    # (parse_announce_channels("") == []).
    resp_disable = await client.post(
        "/admin/simulation/announce-settings",
        data={"channels": "", "disable": "true"},
        headers=auth_headers(admin.id),
    )
    assert resp_disable.status_code in (302, 303)
    # `db_session` has `expire_on_commit=False` (tests/conftest.py), so the
    # `row` object loaded above is not auto-invalidated by the route's own
    # commit — an UPDATE-in-place (unlike the DELETE the "clear" step below
    # exercises) needs an explicit refresh or a plain `select()` would just
    # hand back the same stale in-memory object.
    await db_session.refresh(row)
    assert row.value == ""

    html_disabled = (await client.get("/admin/simulation", headers=auth_headers(admin.id))).text
    assert "Override: disabled" in html_disabled

    # Clear: an empty field with "disable" unchecked deletes the override
    # outright and falls back to the Settings default — unchanged behavior.
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

    html_default = (await client.get("/admin/simulation", headers=auth_headers(admin.id))).text
    assert "No override" in html_default
    assert get_settings().run_start_announce_channels in html_default


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
    assert 'data-sc-live="threads">3</td>' in fresh_resp.text
    assert 'data-sc-live="calls">5</td>' in fresh_resp.text

    row = (await db_session.execute(select(SimulationProcessStatus))).scalar_one()
    row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.commit()

    stale_resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
    assert stale_resp.status_code == 200
    assert 'data-sc-live="threads">3</td>' not in stale_resp.text
    assert 'data-sc-live="calls">5</td>' not in stale_resp.text


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
    # The None-ratio hour (11:00) is drawn as a hollow marker at the top of
    # the plot — at/above every finite hour (2.0 and 5.0) — not at 0.0.
    assert section.count('class="sc-none-marker"') == 1     # exactly one hollow ∞ marker
    assert "∞ — no lab tokens this hour" in section          # table twin row (unchanged wording)
    assert ">5.00</text>" in section                         # y-max tick reads the peak

    # …and the marker sits ON the y-max gridline, not below it. The y-axis tick
    # LABEL is drawn at the gridline's y + 4 (`line_chart`'s
    # `<text … y="{y + 4}">`, src/services/svg_charts.py), so the marker's own
    # `cy` must be that label's y minus 4. `class="sc-tick"` alone is the y-axis
    # ticks; the x-axis labels carry `sc-tick sc-tick--x` and the last point's
    # value carries `sc-point-label`, so neither can match here.
    marker_cy = re.search(r'class="sc-none-marker" cx="[0-9.]+" cy="([0-9.]+)"', section)
    y_max_tick = re.search(r'<text class="sc-tick" x="[0-9.-]+" y="([0-9.]+)"[^>]*>5\.00</text>', section)
    assert marker_cy is not None and y_max_tick is not None
    assert float(marker_cy.group(1)) + 4 == float(y_max_tick.group(1))


# ---------------------------------------------------------------------------
# Final-audit fixes (2026-08-30):
#   (i) F10 — the unattributed-cost gantt footnote only renders when the
#       unattributed bucket's cost is actually positive. `cost_per_interview`
#       always returns a `thread_ts=None` bucket (zero or not) whenever ANY
#       assessment has a thread_id, so gating on presence alone (the old
#       condition) rendered a "$0.00 ... could not be attributed" line on
#       every run with a scored interview and literally zero unattributed
#       spend — noise on the common case, not a caveat.
# ---------------------------------------------------------------------------


async def test_live_tab_unattributed_cost_footnote_gated_on_positive_cost(client, db_session):
    admin = await _admin(db_session, "sim-admin-t11i@example.org")
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        thread_id="T1", recommendation="advance", summary_posted_at=None,
    ))
    await db_session.commit()

    # No unattributed LLM calls yet — the footnote must not appear even
    # though the gantt has a row (the old, presence-only condition would
    # have shown "$0.00 ... could not be attributed" here).
    zero_resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
    assert zero_resp.status_code == 200
    assert "could not be attributed to a" not in zero_resp.text

    # Now add a genuinely unattributed call (no thread_ts) with real cost —
    # the footnote must appear.
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-sonnet-5",
        input_tokens=25_000, output_tokens=0,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    await db_session.commit()

    positive_resp = await client.get(
        f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id)
    )
    assert positive_resp.status_code == 200
    assert "could not be attributed to a" in positive_resp.text


# ---------------------------------------------------------------------------
# Task 7 — the admin waitlist views are removed.
# ---------------------------------------------------------------------------


async def test_admin_waitlist_route_is_gone(client, db_session):
    admin = await _admin(db_session, "no-waitlist@example.org")
    r = await client.get("/admin/waitlist", headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 404
    assert (await client.get("/admin/waitlist/export", headers=auth_headers(admin.id))).status_code == 404
    assert (
        await client.get(
            "/admin/waitlist/00000000-0000-0000-0000-000000000000/mark-contacted",
            headers=auth_headers(admin.id),
        )
    ).status_code == 404


# ---------------------------------------------------------------------------
# F1 — cost by interview stage / specialist consult / call kind
# ---------------------------------------------------------------------------


async def test_live_tab_renders_the_three_f1_cost_panels(client, db_session):
    admin = await _admin(db_session, "sim-admin-f1@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        thread_phase="decide", thread_ts="T1", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, **common,
        call_stats=[{"seq": 0, "kind": "round", "input_tokens": 1_000_000,
                     "output_tokens": 0, "thinking_tokens": 0}],
    )  # $5.00 — as a stage row, and again as a "round" call-kind row
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="consult_chemistry",
        thread_ts="T1", model="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=0, **common, call_stats=None,
    )  # $2.00 — unmatched consult; NULL call_stats makes the call-kind panel a floor
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "Cost by interview stage" in resp.text
    assert "Cost by specialist consult" in resp.text
    assert "Cost by call kind" in resp.text
    assert "scout_hub · decide" in resp.text
    assert "chemistry · unmatched" in resp.text
    assert "$2.00 (1 call)" in resp.text
    assert "≥ $5.00 (1 call)" in resp.text


async def test_live_tab_f1_panels_show_the_empty_state_on_a_run_with_no_calls(client, db_session):
    admin = await _admin(db_session, "sim-admin-f1e@example.org")
    run = await factories.make_simulation_run(db_session)
    await db_session.commit()

    resp = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))

    assert resp.status_code == 200
    assert "Cost by interview stage" in resp.text
    assert "No classified turns yet" in resp.text
    assert "No per-call breakdown yet" in resp.text
    assert "Internal Server Error" not in resp.text


# ---------------------------------------------------------------------------
# Task 6 — the new chart contract: captions, readability scale, labelled
# bars, pipeline funnel, UTC timestamps, keyed refresh.
# ---------------------------------------------------------------------------

_CAPTIONED_CARDS = [
    "Cumulative cost", "Tokens per hour", "Cost by agent", "Cost by model", "Cost by phase",
    "Cost by interview stage", "Cost by specialist consult", "Cost by call kind", "Funnel",
    "Drops by reason", "Specialist mix", "Panel fan-out", "Stop-reason taxonomy", "Latency",
    "Per-agent activity", "Interview timeline", "Hub : lab token burn ratio",
]


async def test_live_tab_every_chart_card_has_a_heading_and_a_caption(client, db_session):
    admin = await _admin(db_session, "sim-admin-cap@example.org")
    run = await factories.make_simulation_run(db_session)
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    for title in _CAPTIONED_CARDS:
        assert html.count(f">{title}</h2>") == 1, title
        i = html.index(f">{title}</h2>")
        assert 'class="sc-caption"' in html[i:i + 400], f"{title} has no caption"


async def test_the_page_uses_the_readability_scale_throughout(client, db_session):
    admin = await _admin(db_session, "sim-admin-type@example.org")
    run = await factories.make_simulation_run(db_session)
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    # Bounded to the #sim-body content itself: base.html's site-wide footer
    # (outside this div) legitimately carries text-gray-400 and is not part
    # of the chart panel's readability contract.
    page = html[html.index('id="sim-body"'):html.index("<!-- #sim-body -->")]
    for banned in ("text-gray-400", "text-gray-500", "bg-gray-400", "uppercase"):
        assert banned not in page, banned
    for m in re.finditer(r'class="([^"]*\btext-xs\b[^"]*)"', page):
        assert "rounded-full" in m.group(1), f"text-xs outside a chip: {m.group(1)}"
    assert ".sc-tile-value" in html and "font-size: 14px" in html   # stylesheet shipped, chart text ≥ 14px


async def test_live_tab_bars_carry_visible_labels_values_and_a_numeric_axis(client, db_session):
    admin = await _admin(db_session, "sim-admin-bars@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=0, model="claude-opus-5")
    await factories.make_llm_call_log(db_session, run=run, agent_id="blackbird", phase="thread_reply", input_tokens=1_000_000, **common)
    await factories.make_llm_call_log(db_session, run=run, agent_id="labbot", phase="new_post", input_tokens=500_000, **common)
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    card = html[html.index(">Cost by agent</h2>"):html.index(">Cost by model</h2>")]
    assert card.index("blackbird") < card.index("labbot")                       # sorted by cost, desc
    assert '<span class="sc-hbar-value">$5.00 (1 call)</span>' in card
    assert 'style="width:100.0%' in card and 'style="width:50.0%' in card
    assert '<span class="sc-tick sc-tick--0">$0.00</span>' in card
    assert '<span class="sc-tick sc-tick--mid">$2.50</span>' in card
    assert '<span class="sc-tick sc-tick--max">$5.00</span>' in card
    assert '<span class="sc-axis-unit">US$</span>' in card
    assert re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d\d:\d\d", html)  # hour labels


async def test_live_tab_funnel_is_in_pipeline_order_and_drops_are_visible(client, db_session):
    admin = await _admin(db_session, "sim-admin-funnel@example.org")
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
                                         thread_id="T1", recommendation="advance", summary_posted_at=None))
    db_session.add(AssessmentDrop(simulation_run_id=run.id, agent_id="blackbird", thread_id="T2",
                                  reason="empty_reply", detail="x"))
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    # Bounded to the chart itself (from its sc-chart wrapper), not the card's
    # caption paragraph — the caption's own prose ("Headlines owed should
    # read 0 on a healthy run.") names "Headlines owed" ahead of the bars.
    funnel_start = html.index('class="sc-chart sc-hbar"', html.index(">Funnel</h2>"))
    funnel = html[funnel_start:html.index(">Drops by reason</h2>")]
    assert funnel.index("Interviews opened") < funnel.index("Verdicts stored") < funnel.index("Headlines owed")
    drops = html[html.index(">Drops by reason</h2>"):html.index(">Specialist mix</h2>")]
    assert "<details" not in drops.split("</table>")[0]
    assert "empty_reply" in drops and '<td class="sc-num">1</td>' in drops


async def test_live_tab_timestamps_are_minute_precision_utc(client, db_session):
    admin = await _admin(db_session, "sim-admin-ts@example.org")
    run = await factories.make_simulation_run(db_session, started_at=datetime(2026, 9, 9, 18, 48, 1, 880209, tzinfo=UTC))
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    assert "2026-09-09 18:48 UTC" in html and "18:48:01.880209" not in html


async def test_live_tab_refresh_script_preserves_open_details_by_key(client, db_session):
    admin = await _admin(db_session, "sim-admin-js@example.org")
    html = (await client.get("/admin/simulation", headers=auth_headers(admin.id))).text
    assert "details[open][data-sc-key]" in html and "el.dataset.scKey" in html


async def test_live_tab_latency_and_progress_render_with_real_data(client, db_session):
    admin = await _admin(db_session, "sim-admin-lat@example.org")
    run = await factories.make_simulation_run(
        db_session, status="stopped", config={"max_runtime": 60},
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC), ended_at=datetime(2026, 1, 1, 11, 4, 9, tzinfo=UTC))
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-opus-5", phase="thread_reply",
        input_tokens=10, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        call_stats=[{"seq": 0, "kind": "final", "latency_ms": 133820, "stop_reason": "end_turn"},
                    {"seq": 1, "kind": "retry", "latency_ms": 500, "stop_reason": "end_turn"}])
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    assert "Internal Server Error" not in html
    assert "P50 (ms)" in html and '<td class="sc-num">2</td>' in html   # n, thousands-separated cells elsewhere
    assert '<div class="sc-tile-value">107%</div>' in html and "overran the limit" in html
