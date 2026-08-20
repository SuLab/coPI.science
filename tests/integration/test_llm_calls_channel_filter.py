"""The interview filter on /admin/activity/{run}/llm-calls.

A run's log is thousands of rows with nothing to group them by. `channel` is the
closest thing the table has to a conversation id — the hub's `thread_reply` rows
have always carried it, and consults carry it as of the `log_meta` change — so
filtering by it is what turns "go read the log" into "read THIS interview".
"""

from __future__ import annotations

import pytest

from src.models import USER_ROLE_ADMIN, LlmCallLog
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

HUB = "blackbird"


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="llm-channel-admin@example.org"
    )


async def _seed(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id=HUB, phase="thread_reply",
        channel="scout-wang", response_text="WANG-INTERVIEW-REPLY",
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id=HUB, phase="consult_chemistry",
        channel="scout-wang", response_text="WANG-CONSULT-OPINION",
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id=HUB, phase="thread_reply",
        channel="scout-gordy", response_text="GORDY-INTERVIEW-REPLY",
    )
    # A call with no channel at all — memory/decide turns and pre-log_meta
    # consults look like this. It must not become a dropdown option.
    await factories.make_llm_call_log(
        db_session, run=run, agent_id=HUB, phase="memory",
        channel=None, response_text="UNATTRIBUTED-CALL",
    )
    await db_session.flush()
    return run


async def test_the_channel_filter_narrows_the_rows_to_one_interview(
    client, db_session, admin
):
    run = await _seed(db_session)

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls?channel=scout-wang",
            headers=auth_headers(admin.id),
        )
    ).text

    assert "WANG-INTERVIEW-REPLY" in html
    assert "WANG-CONSULT-OPINION" in html, "the interview's consults come with it"
    assert "GORDY-INTERVIEW-REPLY" not in html
    assert "UNATTRIBUTED-CALL" not in html
    assert "Showing 2 of 2 calls" in html


async def test_the_unfiltered_page_still_shows_everything(client, db_session, admin):
    """Non-vacuity control: the four rows above are all reachable, so the
    assertions in the test above are about the filter, not about the fixture."""
    run = await _seed(db_session)

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id)
        )
    ).text

    assert "WANG-INTERVIEW-REPLY" in html
    assert "GORDY-INTERVIEW-REPLY" in html
    assert "UNATTRIBUTED-CALL" in html


async def test_the_dropdown_lists_the_runs_channels_and_never_a_null(
    client, db_session, admin
):
    run = await _seed(db_session)

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls?channel=scout-wang",
            headers=auth_headers(admin.id),
        )
    ).text

    assert 'name="channel"' in html
    assert '<option value="scout-wang" selected' in html
    assert '<option value="scout-gordy"' in html
    # The NULL-channel row contributed no option. "All" is the only empty value.
    assert html.count('<option value=""') == 4, (
        "one empty option per filter select (agent, phase, model, channel)"
    )
    # The Clear control appears for a channel-only filter too.
    assert f'href="/admin/activity/{run.id}/llm-calls"' in html


async def test_the_channel_filter_is_scoped_to_the_run(client, db_session, admin):
    """A channel name is reused across runs — `general` exists in every one — so
    an unscoped dropdown or an unscoped filter would mix two runs' calls."""
    run = await _seed(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=other, agent_id=HUB, phase="thread_reply",
        channel="other-run-channel", response_text="OTHER-RUN-REPLY",
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id)
        )
    ).text

    assert "OTHER-RUN-REPLY" not in html
    assert "other-run-channel" not in html


async def test_pagination_links_carry_the_channel(client, db_session, admin):
    """Page 2 of a filtered interview must still be the filtered interview. The
    page size is 50, so this seeds 51 rows in one channel rather than
    monkeypatching a local."""
    run = await factories.make_simulation_run(db_session)
    for i in range(51):
        db_session.add(
            LlmCallLog(
                simulation_run_id=run.id,
                agent_id=HUB,
                phase="thread_reply",
                channel="scout-paged",
                model="claude-test",
                system_prompt="sys",
                messages_json=[],
                response_text=f"PAGED-REPLY-{i}",
            )
        )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls?channel=scout-paged",
            headers=auth_headers(admin.id),
        )
    ).text

    assert "Page 1 of 2" in html
    assert "?page=2&channel=scout-paged" in html

    page2 = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls?channel=scout-paged&page=2",
            headers=auth_headers(admin.id),
        )
    ).text
    assert "Page 2 of 2" in page2
    assert "?page=1&channel=scout-paged" in page2
    assert "Showing 1 of 51 calls" in page2
