"""The discussions view under a channel or status filter.

Real Postgres, through ``build_discussions_view`` and the admin route. Pins two
rules: the status counts (the summary cards and "Total threads") are
run-wide and never move with a filter, and a thread with several
``thread_decisions`` rows is listed, counted and exported once, under its LAST
decision, whether or not it has a root post.

Fixture, one run:

    Thread  Channel  Messages          Decisions
    A       chan-a   root + reply      no_proposal @T0 ("A-FIRST"),
                                       then timeout @T0+60 ("A-LAST")
    B       chan-b   root + reply      none (active)
    C       chan-b   root, no replies  none (no_replies)
    D       chan-b   no root (orphan)  proposal @T0 ("D-FIRST"),
                                       then no_proposal @T0+60 ("D-LAST")
"""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import USER_ROLE_ADMIN
from src.services.directory import build_discussions_view
from tests import factories
from tests.session_support import session_cookie_name

pytestmark = pytest.mark.integration

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

RUN_WIDE_COUNTS = {"timeout": 1, "active": 1, "no_replies": 1, "no_proposal": 1}


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"{session_cookie_name()}={signer.sign(data).decode()}"}


@pytest.fixture
async def run(db_session):
    run = await factories.make_simulation_run(db_session)

    async def root(ts, channel):
        await factories.make_agent_message(
            db_session, run=run, agent_id="su", channel_name=channel,
            phase="new_post", message_ts=ts, content=f"root {ts}",
        )

    async def reply(ts, channel):
        await factories.make_agent_message(
            db_session, run=run, agent_id="cravatt", channel_name=channel,
            phase="thread_reply", message_ts=f"{ts}.r1", thread_ts=ts,
            content=f"reply in {ts}",
        )

    async def decision(ts, channel, outcome, at, summary):
        await factories.make_thread_decision(
            db_session, run=run, thread_id=ts, channel=channel,
            agent_a="su", agent_b="cravatt", outcome=outcome,
            decided_at=at, summary_text=summary,
        )

    await root("A", "chan-a")
    await reply("A", "chan-a")
    await decision("A", "chan-a", "no_proposal", T0, "A-FIRST")
    await decision("A", "chan-a", "timeout", T0 + timedelta(seconds=60), "A-LAST")

    await root("B", "chan-b")
    await reply("B", "chan-b")

    await root("C", "chan-b")

    await decision("D", "chan-b", "proposal", T0, "D-FIRST")
    await decision("D", "chan-b", "no_proposal", T0 + timedelta(seconds=60), "D-LAST")

    await db_session.flush()
    return run


async def _view(db_session, run, *, channel_filter=None, status_filter=None):
    return await build_discussions_view(
        db_session,
        run_id=str(run.id),
        channel_filter=channel_filter,
        status_filter=status_filter,
        agent_filter=[],
    )


async def test_a_status_filter_never_lists_a_thread_under_its_first_decision(
    db_session, run
):
    view = await _view(db_session, run, status_filter="no_proposal")
    # A's first decision was no_proposal, but its last is timeout.
    assert [t["message_ts"] for t in view["threads"]] == ["D"]
    assert view["counts"] == RUN_WIDE_COUNTS


async def test_a_channel_filter_does_not_change_the_status_counts(db_session, run):
    unfiltered = await _view(db_session, run)
    filtered = await _view(db_session, run, channel_filter="chan-b")

    assert unfiltered["counts"] == RUN_WIDE_COUNTS
    assert filtered["counts"] == unfiltered["counts"]
    assert sorted(t["message_ts"] for t in filtered["threads"]) == ["B", "C", "D"]


async def test_a_real_orphan_takes_its_last_decision(db_session, run):
    view = await _view(db_session, run)
    by_ts: dict[str, list[dict]] = {}
    for t in view["threads"]:
        by_ts.setdefault(t["message_ts"], []).append(t)

    assert sorted(by_ts) == ["A", "B", "C", "D"]
    assert all(len(rows) == 1 for rows in by_ts.values())

    orphan = by_ts["D"][0]
    assert orphan["status"] == "no_proposal"
    assert orphan["decision"].summary_text == "D-LAST"
    assert orphan["channel_name"] == "chan-b"
    # A has a root post; it too is shown under its last decision.
    assert by_ts["A"][0]["status"] == "timeout"
    assert by_ts["A"][0]["decision"].summary_text == "A-LAST"


async def test_export_under_a_status_filter_excludes_the_mislisted_thread(
    client, db_session, run
):
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="admin@example.org"
    )
    await db_session.flush()

    r = await client.get(
        f"/workspace/discussions?run_id={run.id}&status_filter=no_proposal&export=true",
        headers=_auth(admin.id),
    )
    assert r.status_code == 200
    assert "A-FIRST" not in r.text
    assert "A-LAST" not in r.text
    assert "D-LAST" in r.text
    assert "D-FIRST" not in r.text


async def _admin_page(client, db_session, run, query: str) -> str:
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="admin@example.org"
    )
    await db_session.flush()
    r = await client.get(
        f"/workspace/discussions?run_id={run.id}{query}", headers=_auth(admin.id)
    )
    assert r.status_code == 200
    return r.text


async def test_the_footer_total_counts_the_orphan_as_a_thread(
    client, db_session, run
):
    # Three root posts (A, B, C) plus one orphan decision (D): the footer is
    # the sum of the run-wide counts, so it says 4 and calls them threads.
    text = await _admin_page(client, db_session, run, "")
    assert "Total threads: 4" in text
    assert "Total root posts" not in text
    assert "(filtered)" not in text


async def test_an_agent_filter_alone_marks_the_list_filtered(
    client, db_session, run
):
    # A and B have cravatt replies and D's decision names cravatt as agent_b,
    # so the list is A, B, D; C (su's root, no reply, no decision) drops out.
    view = await build_discussions_view(
        db_session,
        run_id=str(run.id),
        channel_filter=None,
        status_filter=None,
        agent_filter=["cravatt"],
    )
    assert sorted(t["message_ts"] for t in view["threads"]) == ["A", "B", "D"]
    assert view["counts"] == RUN_WIDE_COUNTS

    text = await _admin_page(client, db_session, run, "&agent_filter=cravatt")
    assert "Showing 3 threads" in text
    assert "(filtered)" in text
    assert "Total threads: 4" in text
