"""Live integration tests for the agent page — all 19 endpoints of routers/agent_page.py.

Real ASGI requests, real Postgres, real Jinja templates, real invitation/reopen
flows. Task T8 of .notes/full-system-test-plan.md.

Nothing external is real: Slack (`slack_sdk.WebClient` and the copy bound inside
`src.agent.slack_client`), SES (`send_delegate_invitation`) and the whole httpx
transport layer are replaced by recorders that fail loudly if a test reaches for
the network. The database is NOT mocked — every assertion below is about rows
the routes actually wrote.

Discipline (see the plan's "The Discipline"):
  * every absence assertion carries a positive control in the same test;
  * state is produced by driving the real route, not hand-built, wherever the
    route that produces it is itself under test;
  * two known defects are pinned with ``xfail(strict=True)`` so the suite goes
    red the day they are fixed and the assertion has to be flipped, instead of
    quietly encoding a bug as expected behaviour.
"""

import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest
from fastapi import HTTPException
from itsdangerous import TimestampSigner
from sqlalchemy import select, text

from src.config import get_settings
from src.models import (
    VISIBILITY_COLLAB_PRIVATE,
    AgentChannel,
    AgentDelegate,
    AgentMessage,
    AgentRegistry,
    DelegateInvitation,
    PiDmMessage,
    PrivateChannelMember,
    ProfileRevision,
    ProposalReview,
    ResearcherProfile,
)
from src.routers.agent_page import derive_agent_identity
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


# ---------------------------------------------------------------------------
# External-world doubles. None of these may reach the network.
# ---------------------------------------------------------------------------


class _SlackRecorder:
    """Records every Slack API call the routes attempt.

    Unstubbed methods raise: a route that starts talking to Slack where these
    tests assert it must not will blow up rather than silently succeed against
    a permissive Mock. `calls` is asserted to be empty in the reopen tests —
    that is the "never make a live Slack call" constraint, enforced.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.handlers: dict[str, object] = {}

    def stub(self, method: str, response):
        self.handlers[method] = response

    @property
    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakeWebClient:
    def __init__(self, recorder: _SlackRecorder, **kwargs):
        self._rec = recorder
        self.token = kwargs.get("token")

    def __getattr__(self, name):
        def _call(**kwargs):
            self._rec.calls.append((name, kwargs))
            if name not in self._rec.handlers:
                raise AssertionError(
                    f"unstubbed Slack API call {name}({kwargs}) — tests must never "
                    "reach the real workspace"
                )
            resp = self._rec.handlers[name]
            return resp(**kwargs) if callable(resp) else resp

        return _call


@pytest.fixture(autouse=True)
def slack(monkeypatch) -> _SlackRecorder:
    rec = _SlackRecorder()
    factory = lambda *a, **kw: _FakeWebClient(rec, **kw)  # noqa: E731
    monkeypatch.setattr("slack_sdk.WebClient", factory)
    # AgentSlackClient bound WebClient at import time, so patch that name too —
    # it is the one the private-channel migration would use.
    monkeypatch.setattr("src.agent.slack_client.WebClient", factory)
    # services/slack_web.py is the web layer's Slack boundary and binds WebClient
    # at import time as well. Patching only `slack_sdk.WebClient` would leave the
    # routes' user lookups and channel listing talking to the real workspace.
    monkeypatch.setattr("src.services.slack_web.WebClient", factory)
    return rec


@pytest.fixture(autouse=True)
def _slack_enabled_auto_detect(monkeypatch):
    """Hermetic default for the Slack on/off tri-state (src/services/slack_tokens.py
    and src/services/private_channels.py's ``_slack_enabled_for_migration``): unset
    (auto-detect from token presence) rather than whatever ``SLACK_ENABLED`` the
    deployed .env on this host forces.

    Without this, a populated .env with SLACK_ENABLED=true forces the real-Slack
    branch of `reopen_proposal` even for `world`'s fictitious agents (`tstowner`,
    `tstother`), which have no token anywhere — `migrate_public_thread_to_private`
    then 500s on "No valid Slack bot token". Auto-detect is this suite's actual
    premise: a test that wants Slack ON gives its own agent a token (e.g.
    ``world.agent.slack_bot_token = "xoxb-fake-for-tests"``), which is what
    auto-detect keys on either way.
    """
    monkeypatch.setattr(get_settings(), "slack_enabled", None)


@pytest.fixture(autouse=True)
def sent_emails(monkeypatch) -> list[dict]:
    """Recording double for the SES leg (the plan's email seam: record, never send)."""
    sent: list[dict] = []

    def _send(to_email, pi_name, bot_name, invite_url):
        sent.append(
            {"to": to_email, "pi_name": pi_name, "bot_name": bot_name, "url": invite_url}
        )
        return True

    monkeypatch.setattr("src.services.email.send_delegate_invitation", _send)
    return sent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real outbound HTTP (Anthropic, ORCID, NCBI, grants.gov) is a test bug.

    Patched at the transport layer, which the ASGI test client does not use, so
    in-process requests still work.
    """

    def _boom(*args, **kwargs):
        raise AssertionError("a test attempted a real outbound HTTP request")

    monkeypatch.setattr("httpx.HTTPTransport.handle_request", _boom)
    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", _boom)


@pytest.fixture(autouse=True)
def profiles_dir(tmp_path, monkeypatch):
    """Keep the profile-save routes off the repo's real profiles/ directory."""
    monkeypatch.setattr("src.routers.agent_page.PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(
        "src.services.profile_export.PROFILES_DIR", tmp_path / "profiles" / "public"
    )
    monkeypatch.setattr(
        "src.services.profile_export.PRIVATE_PROFILES_DIR", tmp_path / "profiles" / "private"
    )
    return tmp_path / "profiles"


# ---------------------------------------------------------------------------
# World fixtures
# ---------------------------------------------------------------------------

# Deliberately not real roster slugs (`su`, `wu`, …): config.get_slack_tokens()
# is keyed by those, and a populated .env would hand the migration path a real
# bot token.
OWNER_AGENT = "tstowner"
OTHER_AGENT = "tstother"
THIRD_AGENT = "tstthird"


async def _agent_for(db, *, name, email, agent_id, bot_name, status="active"):
    user = await factories.make_user(db, name=name, email=email)
    agent = await factories.make_agent(
        db, user=user, agent_id=agent_id, bot_name=bot_name, pi_name=name, status=status
    )
    return user, agent


@pytest.fixture
async def world(db_session):
    """One owned active agent, a counterpart agent, a stranger, a run and a proposal."""
    pi, agent = await _agent_for(
        db_session, name="Pat Owner", email="pi@example.org",
        agent_id=OWNER_AGENT, bot_name="OwnerBot",
    )
    await factories.make_profile(db_session, user=pi)
    other_pi, other_agent = await _agent_for(
        db_session, name="Otto Other", email="otto@example.org",
        agent_id=OTHER_AGENT, bot_name="OtherBot",
    )
    stranger = await factories.make_user(
        db_session, name="Sam Stranger", email="stranger@example.org"
    )
    run = await factories.make_simulation_run(db_session)
    td = await factories.make_thread_decision(
        db_session, run=run, agent_a=OWNER_AGENT, agent_b=OTHER_AGENT,
        channel="general", outcome="proposal",
        summary_text=":memo: **Summary — A shared assay platform**\nBoth labs need it.",
    )
    await db_session.flush()
    return SimpleNamespace(
        pi=pi, agent=agent, other_pi=other_pi, other_agent=other_agent,
        stranger=stranger, run=run, td=td,
    )


async def _invite(client, world, email):
    """Drive the real invite route; return the DelegateInvitation token."""
    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/invite",
        data={"emails": email},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302, r.text
    assert "delegate_error" not in r.headers["location"], r.headers["location"]
    return r


async def _token_for(db, agent, email) -> str:
    row = (await db.execute(
        select(DelegateInvitation).where(
            DelegateInvitation.agent_registry_id == agent.id,
            DelegateInvitation.email == email,
        )
    )).scalar_one()
    return row.token


@pytest.fixture
async def delegated(client, db_session, world):
    """A delegate produced by the real invite → accept flow, plus a pending invite.

    Built by driving the routes rather than inserting an AgentDelegate row, so
    the fixture itself proves the add path works before anything asserts on it.
    """
    delegate = await factories.make_user(
        db_session, name="Dee Legate", email="dee@example.org"
    )
    await _invite(client, world, "dee@example.org")
    token = await _token_for(db_session, world.agent, "dee@example.org")
    r = await client.post(f"/invite/{token}/accept", headers=_auth(delegate.id))
    assert r.status_code == 302 and r.headers["location"].endswith(
        f"/agent/{OWNER_AGENT}/dashboard"
    )
    row = (await db_session.execute(
        select(AgentDelegate).where(AgentDelegate.agent_registry_id == world.agent.id)
    )).scalar_one()

    # A second, still-pending invitation so the revoke endpoint has a real target.
    await _invite(client, world, "pending@example.org")
    pending = (await db_session.execute(
        select(DelegateInvitation).where(
            DelegateInvitation.agent_registry_id == world.agent.id,
            DelegateInvitation.status == "pending",
        )
    )).scalar_one()
    return SimpleNamespace(user=delegate, row=row, pending_invitation=pending)


async def _private_channels(db) -> list[AgentChannel]:
    return list((await db.execute(
        select(AgentChannel)
        .where(AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE)
        .order_by(AgentChannel.channel_name)
    )).scalars().all())


async def _reviews(db, agent_id: str) -> list[ProposalReview]:
    return list((await db.execute(
        select(ProposalReview).where(ProposalReview.agent_id == agent_id)
    )).scalars().all())


# ===========================================================================
# 1. Self-service signup  (POST /agent/request)
# ===========================================================================


async def _signup(client, db_session, name, email):
    user = await factories.make_user(db_session, name=name, email=email)
    await factories.make_profile(db_session, user=user)
    await db_session.flush()
    r = await client.post("/agent/request", headers=_auth(user.id))
    return user, r


async def _agent_of(db, user) -> AgentRegistry | None:
    return (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )).scalar_one_or_none()


async def test_signup_creates_a_pending_agent_row(client, db_session):
    """The documented self-service path (CLAUDE.md §Adding New PIs)."""
    user, r = await _signup(client, db_session, "Ada Zephyr", "ada@example.org")
    assert r.status_code == 302 and r.headers["location"] == "/agent"

    agent = await _agent_of(db_session, user)
    assert agent is not None, "POST /agent/request created no AgentRegistry row"
    assert agent.agent_id == "zephyr"
    assert agent.bot_name == "ZephyrBot"
    assert agent.pi_name == "Ada Zephyr"
    # Signup must not self-approve: an agent that came out 'active' would join
    # the simulation roster (_sync_roster_from_db) without an admin ever looking.
    assert agent.status == "pending"
    assert agent.slack_bot_token is None


async def test_signup_prefixes_the_first_initial_only_on_a_last_name_collision(
    client, db_session
):
    """CLAUDE.md: "Chunlei Wu = wu"; a second Wu becomes "pwu".

    Control (the second half): a *non*-colliding last name must come out
    unprefixed. Without it a request_agent() that always prefixed would pass.
    """
    first, r1 = await _signup(client, db_session, "Chunlei Wu", "chunlei@example.org")
    assert r1.status_code == 302
    assert (await _agent_of(db_session, first)).agent_id == "wu"

    second, r2 = await _signup(client, db_session, "Peng Wu", "peng@example.org")
    assert r2.status_code == 302
    assert (await _agent_of(db_session, second)).agent_id == "pwu"

    # Control: no collision → no prefix (not "azephyr").
    control, r3 = await _signup(client, db_session, "Ada Zephyr", "ada@example.org")
    assert r3.status_code == 302
    assert (await _agent_of(db_session, control)).agent_id == "zephyr"


async def test_signup_collision_also_disambiguates_the_bot_name(client, db_session):
    await _signup(client, db_session, "Chunlei Wu", "chunlei@example.org")
    second, _ = await _signup(client, db_session, "Peng Wu", "peng@example.org")
    assert (await _agent_of(db_session, second)).bot_name == "PWuBot"


async def test_signup_third_same_initial_collision_gets_a_numeric_suffix(
    client, db_session
):
    """A third same-initial namesake collides on the initial-prefixed id too
    ('pwu' is already taken by the second Wu). The fallback must append the
    numeric suffix to THAT prefixed candidate ('pwu2'), not restart from the
    bare stem ('wu2') — issue #26 C1/C2. Before the fix there was no numeric
    branch at all: this hit the agent_id unique constraint on commit -> 500.
    """
    first, _ = await _signup(client, db_session, "Chunlei Wu", "chunlei@example.org")
    second, _ = await _signup(client, db_session, "Peng Wu", "peng@example.org")
    third, r3 = await _signup(client, db_session, "Pei Wu", "pei@example.org")

    assert r3.status_code == 302, r3.text
    assert (await _agent_of(db_session, first)).agent_id == "wu"
    assert (await _agent_of(db_session, second)).agent_id == "pwu"
    third_agent = await _agent_of(db_session, third)
    assert third_agent.agent_id == "pwu2"
    assert third_agent.bot_name == "PWu2Bot"


async def test_signup_fourth_same_initial_collision_extends_the_numeric_suffix(
    client, db_session
):
    """A fourth same-initial namesake collides on 'pwu2' too; the suffix must
    keep extending to 'pwu3', not restart or repeat (#26 audit-issue-26.md
    Minor 4 — the fourth collision was previously unpinned)."""
    first, _ = await _signup(client, db_session, "Chunlei Wu", "chunlei@example.org")
    second, _ = await _signup(client, db_session, "Peng Wu", "peng@example.org")
    third, _ = await _signup(client, db_session, "Pei Wu", "pei@example.org")
    fourth, r4 = await _signup(client, db_session, "Ping Wu", "ping@example.org")

    assert r4.status_code == 302, r4.text
    assert (await _agent_of(db_session, first)).agent_id == "wu"
    assert (await _agent_of(db_session, second)).agent_id == "pwu"
    assert (await _agent_of(db_session, third)).agent_id == "pwu2"
    fourth_agent = await _agent_of(db_session, fourth)
    assert fourth_agent.agent_id == "pwu3"
    assert fourth_agent.bot_name == "PWu3Bot"


async def test_derive_agent_identity_raises_409_when_the_numeric_range_is_exhausted(
    db_session,
):
    """derive_agent_identity's last-resort loop is range(2, 20); once every
    candidate from 'wu' through 'pwu19' is taken it must raise a clean 409,
    not fall through and return a colliding id (#26 Minor 4)."""
    await factories.make_agent(db_session, agent_id="wu", bot_name="WuBot", pi_name="Wu")
    await factories.make_agent(db_session, agent_id="pwu", bot_name="PWuBot", pi_name="Wu")
    for i in range(2, 20):
        await factories.make_agent(
            db_session, agent_id=f"pwu{i}", bot_name=f"PWu{i}Bot", pi_name="Wu"
        )
    await db_session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await derive_agent_identity(db_session, "Ping Wu")
    assert exc_info.value.status_code == 409


async def test_signup_returns_409_on_a_lost_identity_race(client, db_session, monkeypatch):
    """Two concurrent signups can both pass derive_agent_identity's SELECT
    before either commits (TOCTOU on the agent_id unique constraint). Force
    the race by making derive_agent_identity return an id that's already
    taken, and assert the handler converts the resulting IntegrityError into
    a 409, not a raw 500 (issue #26 C1: no try/except existed on this commit
    at all)."""
    monkeypatch.setattr(
        "src.routers.agent_page.derive_agent_identity",
        AsyncMock(return_value=("racer", "RacerBot")),
    )
    other_user = await factories.make_user(
        db_session, name="Already There", email="already@example.org"
    )
    await factories.make_agent(
        db_session, user=other_user, agent_id="racer", bot_name="RacerBot",
        pi_name="Already There", status="pending",
    )

    user = await factories.make_user(db_session, name="Race Newcomer", email="race@example.org")
    await factories.make_profile(db_session, user=user)
    await db_session.flush()

    r = await client.post("/agent/request", headers=_auth(user.id))
    assert r.status_code == 409
    assert (await _agent_of(db_session, user)) is None


async def test_signup_needs_a_completed_profile(client, db_session):
    """Absence assertion + its control, in one test."""
    bare = await factories.make_user(
        db_session, name="Nora Newbie", email="nora@example.org",
        onboarding_complete=False,
    )
    await db_session.flush()
    r = await client.post("/agent/request", headers=_auth(bare.id))
    assert r.status_code == 400
    assert await _agent_of(db_session, bare) is None

    # Control: the same request from a user who *has* finished onboarding works,
    # so the 400 above is about the profile gate and not about the route.
    ready, r2 = await _signup(client, db_session, "Ready Researcher", "ready@example.org")
    assert r2.status_code == 302
    assert await _agent_of(db_session, ready) is not None


async def test_signup_twice_does_not_create_a_second_agent(client, db_session):
    user, r1 = await _signup(client, db_session, "Ada Zephyr", "ada@example.org")
    assert r1.status_code == 302
    r2 = await client.post("/agent/request", headers=_auth(user.id))
    assert r2.status_code == 302 and r2.headers["location"] == "/agent"

    rows = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )).scalars().all()
    assert len(rows) == 1

    # Control: a different user's request *does* add a row, so "still 1" above
    # is the dedup guard and not a route that stopped inserting.
    other, r3 = await _signup(client, db_session, "Bo Quill", "bo@example.org")
    assert r3.status_code == 302
    total = (await db_session.execute(select(AgentRegistry))).scalars().all()
    assert len(total) == 2
    assert (await _agent_of(db_session, other)).agent_id == "quill"


# ===========================================================================
# 2. The private-channel reopen route
# ===========================================================================


async def _reopen(client, world, td, user, guidance="Push on the shared assay."):
    return await client.post(
        f"/agent/{OWNER_AGENT}/proposals/{td.id}/reopen",
        data={"guidance": guidance},
        headers=_auth(user.id),
    )


async def test_reopening_the_same_proposal_twice_creates_one_channel(
    client, db_session, world, slack
):
    """The idempotency guard in reopen_proposal (stale page / Back-button replay).

    Control: a *different* proposal does create a second channel, so "still one"
    cannot be satisfied by a reopen that silently stopped working.
    """
    r1 = await _reopen(client, world, world.td, world.pi)
    assert r1.status_code == 302, r1.text
    channels = await _private_channels(db_session)
    assert len(channels) == 1
    first_name = channels[0].channel_name
    assert first_name.startswith("priv-")

    await db_session.refresh(world.td)
    assert world.td.refined_in_channel == channels[0].channel_id
    # Both bots + the triggering PI, per specs/privacy-and-channel-visibility.md.
    members = (await db_session.execute(
        select(PrivateChannelMember).where(
            PrivateChannelMember.agent_channel_id == channels[0].id
        )
    )).scalars().all()
    assert sorted(m.agent_id for m in members if m.agent_id) == sorted(
        [OWNER_AGENT, OTHER_AGENT]
    )
    assert [m.user_id for m in members if m.user_id] == [world.pi.id]

    # --- the replay -------------------------------------------------------
    r2 = await _reopen(client, world, world.td, world.pi, guidance="Second submit.")
    assert r2.status_code == 302
    after = await _private_channels(db_session)
    assert len(after) == 1, (
        "the reopen idempotency guard did not hold — the replay minted a second "
        f"private channel: {[c.channel_name for c in after]}"
    )
    assert after[0].channel_name == first_name
    assert len(await _reviews(db_session, OWNER_AGENT)) == 1

    await db_session.refresh(world.td)
    assert world.td.refined_in_channel == after[0].channel_id

    # --- control: a different proposal is not deduped ---------------------
    td2 = await factories.make_thread_decision(
        db_session, run=world.run, agent_a=OWNER_AGENT, agent_b=OTHER_AGENT,
        channel="proteomics", outcome="proposal", summary_text="Summary — A second idea",
    )
    await db_session.flush()
    r3 = await _reopen(client, world, td2, world.pi, guidance="Different proposal.")
    assert r3.status_code == 302
    assert len(await _private_channels(db_session)) == 2

    assert slack.calls == [], f"the reopen route called Slack: {slack.methods}"


async def test_reopen_records_a_rating_zero_review_carrying_the_guidance(
    client, db_session, world
):
    """The row the idempotency guard keys on. If reopen stopped writing it, the
    guard would silently stop working — so pin its shape."""
    assert await _reviews(db_session, OWNER_AGENT) == []
    r = await _reopen(client, world, world.td, world.pi, guidance="Narrow the aims.")
    assert r.status_code == 302

    review = (await _reviews(db_session, OWNER_AGENT))[0]
    assert review.rating == 0
    assert review.comment == "[Reopened] Narrow the aims."
    assert review.user_id == world.pi.id
    assert review.delegate_user_id is None
    assert review.submitted_via == "web"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"guidance": "   "}, id="whitespace"),
        pytest.param({"guidance": ""}, id="empty-box"),
        pytest.param({}, id="field-omitted"),
    ],
)
async def test_reopen_rejects_empty_guidance(client, db_session, world, slack, body):
    """phase8 M2: the handler's own coded 400 has to be the answer for an EMPTY box too.

    `guidance` was `Form(...)`. Starlette's form parser hands an empty `guidance=` to
    FastAPI as a MISSING field, so the two non-whitespace cases below never reached the
    handler at all — they answered a raw
    `{"detail":[{"type":"missing","loc":["body","guidance"],"msg":"Field required",...}]}`
    422, which the reopen form has no rendering for, and the `Guidance text is required`
    branch was unreachable from a browser. Only the whitespace case (a box with a space
    in it) got that far. `Form("")` collapses all three onto the same coded rejection —
    the same fix `7cc2ea3` made on `profile/save`'s `content`.

    The other eight required-`str` Form fields the audit lists are follow-up F4; this
    task deliberately fixes only this site.
    """
    r = await client.post(
        f"/agent/{OWNER_AGENT}/proposals/{world.td.id}/reopen",
        data=body,
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 400, (
        f"empty guidance answered {r.status_code}, not the handler's coded 400: "
        f"{r.text[:200]}"
    )
    assert r.json()["detail"] == "Guidance text is required"
    assert await _private_channels(db_session) == []
    assert slack.calls == []

    # Control: real guidance on the same proposal does migrate.
    assert (await _reopen(client, world, world.td, world.pi)).status_code == 302
    assert len(await _private_channels(db_session)) == 1


async def test_reopen_is_blocked_while_the_agent_is_inactive(client, db_session, world):
    world.agent.status = "inactive"
    await db_session.flush()
    r = await _reopen(client, world, world.td, world.pi)
    assert r.status_code == 403
    assert "inactive" in r.json()["detail"].lower()
    assert await _private_channels(db_session) == []

    # Control: reactivating the same agent lets the same request through.
    world.agent.status = "active"
    await db_session.flush()
    assert (await _reopen(client, world, world.td, world.pi)).status_code == 302
    assert len(await _private_channels(db_session)) == 1


async def test_reopen_refuses_a_proposal_the_agent_is_not_part_of(
    client, db_session, world
):
    foreign = await factories.make_thread_decision(
        db_session, run=world.run, agent_a=OTHER_AGENT, agent_b=THIRD_AGENT,
        channel="metabolomics", outcome="proposal",
    )
    await db_session.flush()
    r = await _reopen(client, world, foreign, world.pi)
    assert r.status_code == 403
    assert await _private_channels(db_session) == []

    # Control: the same PI, same route, on a proposal that *is* theirs.
    assert (await _reopen(client, world, world.td, world.pi)).status_code == 302
    assert len(await _private_channels(db_session)) == 1


async def test_reopening_an_already_private_thread_reports_not_implemented(
    client, db_session, world, slack
):
    """The `origin_visibility != 'public'` branch: refuse loudly (501) rather than
    fall through to the legacy "post the PI's text in-channel" path."""
    already_private = await factories.make_thread_decision(
        db_session, run=world.run, agent_a=OWNER_AGENT, agent_b=OTHER_AGENT,
        channel="priv-existing", outcome="proposal",
        origin_visibility=VISIBILITY_COLLAB_PRIVATE,
    )
    await db_session.flush()
    r = await _reopen(client, world, already_private, world.pi)
    assert r.status_code == 501
    assert await _private_channels(db_session) == []
    assert await _reviews(db_session, OWNER_AGENT) == []
    assert slack.calls == []

    # Control: a public-origin proposal on the same route does migrate.
    assert (await _reopen(client, world, world.td, world.pi)).status_code == 302
    assert len(await _private_channels(db_session)) == 1


async def test_the_legacy_reopen_finds_a_channel_past_the_first_page(
    client, db_session, world, slack, monkeypatch
):
    """The legacy (``enable_private_refinement=False``) path resolves the channel
    id through the whole of conversations.list, not just page one.

    It used to call ``conversations_list(limit=200)`` once and scan that page, so
    a workspace with more channels than fit in one page answered "Channel #x not
    found" for a channel that exists — defect 11/12. The route now goes through
    ``slack_web.list_channel_ids``, which follows every cursor, so the target on
    page **two** below is the whole point of this test.
    """
    monkeypatch.setattr(get_settings(), "enable_private_refinement", False)
    world.agent.slack_bot_token = "xoxb-fake-for-tests"   # flips Slack on
    await db_session.flush()

    pages = [
        {"channels": [{"name": "decoy", "id": "C-DECOY"}],
         "response_metadata": {"next_cursor": "page2"}},
        {"channels": [{"name": world.td.channel, "id": "C-TARGET"}],
         "response_metadata": {"next_cursor": ""}},
    ]
    seen: list[dict] = []

    def _list(**kwargs):
        seen.append(kwargs)
        return pages[len(seen) - 1]

    slack.stub("conversations_list", _list)
    slack.stub("chat_postMessage", {"ok": True, "ts": "1700000000.000900"})

    assert (await _reopen(client, world, world.td, world.pi)).status_code == 302

    assert len(seen) == 2, "one page only — the pagination defect is back"
    assert seen[1]["cursor"] == "page2"
    posted = [kw for name, kw in slack.calls if name == "chat_postMessage"]
    assert len(posted) == 1
    assert posted[0]["channel"] == "C-TARGET", (
        "the channel on page two was not resolved"
    )
    assert posted[0]["thread_ts"] == world.td.thread_id, (
        "the guidance must stay in the proposal thread, not the channel root"
    )
    # Legacy path posts in place: no private refinement channel is minted.
    assert await _private_channels(db_session) == []


# ===========================================================================
# 3. Proposal review
# ===========================================================================


async def test_a_pi_review_is_recorded_and_cannot_be_submitted_twice(
    client, db_session, world
):
    url = f"/agent/{OWNER_AGENT}/proposals/{world.td.id}/review"
    r = await client.post(url, data={"rating": "3", "comment": " solid "},
                          headers=_auth(world.pi.id))
    assert r.status_code == 302
    review = (await _reviews(db_session, OWNER_AGENT))[0]
    assert review.rating == 3
    assert review.comment == "solid"
    assert review.user_id == world.pi.id
    assert review.delegate_user_id is None
    assert review.reviewed_by_user_id == world.pi.id

    r2 = await client.post(url, data={"rating": "4"}, headers=_auth(world.pi.id))
    assert r2.status_code == 400
    assert len(await _reviews(db_session, OWNER_AGENT)) == 1

    # Control: a second proposal is still reviewable, so the 400 is the
    # already-reviewed guard rather than a route that broke after one write.
    td2 = await factories.make_thread_decision(
        db_session, run=world.run, agent_a=OWNER_AGENT, agent_b=OTHER_AGENT,
        channel="general", outcome="proposal",
    )
    await db_session.flush()
    r3 = await client.post(
        f"/agent/{OWNER_AGENT}/proposals/{td2.id}/review",
        data={"rating": "2"}, headers=_auth(world.pi.id),
    )
    assert r3.status_code == 302
    assert len(await _reviews(db_session, OWNER_AGENT)) == 2


@pytest.mark.parametrize("rating", ["0", "5"])
async def test_review_rejects_out_of_range_ratings(client, db_session, world, rating):
    r = await client.post(
        f"/agent/{OWNER_AGENT}/proposals/{world.td.id}/review",
        data={"rating": rating}, headers=_auth(world.pi.id),
    )
    assert r.status_code == 400
    assert await _reviews(db_session, OWNER_AGENT) == []

    # Control: an in-range rating on the same proposal is accepted.
    ok = await client.post(
        f"/agent/{OWNER_AGENT}/proposals/{world.td.id}/review",
        data={"rating": "1"}, headers=_auth(world.pi.id),
    )
    assert ok.status_code == 302
    assert len(await _reviews(db_session, OWNER_AGENT)) == 1


# ===========================================================================
# 4. Delegates: add, act, remove
# ===========================================================================


async def test_inviting_a_delegate_creates_a_pending_invitation_and_one_email(
    client, db_session, world, sent_emails
):
    """One good address and one malformed one in the same submission.

    The malformed address is the absence assertion; the good one is its control.
    """
    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/invite",
        data={"emails": "good@example.org, not-an-email"},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    assert "delegate_error" in r.headers["location"]
    assert "not-an-email" in unquote(r.headers["location"])

    rows = (await db_session.execute(
        select(DelegateInvitation).where(
            DelegateInvitation.agent_registry_id == world.agent.id
        )
    )).scalars().all()
    assert [x.email for x in rows] == ["good@example.org"]
    assert rows[0].status == "pending"
    assert rows[0].invited_by_user_id == world.pi.id
    assert rows[0].expires_at is not None

    # The email leg is recorded, never sent (plan T11's seam, applied here).
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "good@example.org"
    assert sent_emails[0]["bot_name"] == "OwnerBot"
    assert sent_emails[0]["url"].endswith(f"/invite/{rows[0].token}")


async def test_a_duplicate_pending_invitation_is_refused(client, db_session, world):
    await _invite(client, world, "dee@example.org")
    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/invite",
        data={"emails": "dee@example.org"},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    assert "Invitation+already+pending" in r.headers["location"].replace("%20", "+")
    rows = (await db_session.execute(
        select(DelegateInvitation).where(DelegateInvitation.email == "dee@example.org")
    )).scalars().all()
    assert len(rows) == 1

    # Control: a different address still gets an invitation.
    await _invite(client, world, "eve@example.org")
    total = (await db_session.execute(
        select(DelegateInvitation).where(
            DelegateInvitation.agent_registry_id == world.agent.id
        )
    )).scalars().all()
    assert len(total) == 2


async def test_accepting_an_invitation_creates_the_delegation(
    client, db_session, world, delegated
):
    """`delegated` builds itself by driving invite → accept; assert the result."""
    assert delegated.row.user_id == delegated.user.id
    assert delegated.row.agent_registry_id == world.agent.id
    invitation = (await db_session.execute(
        select(DelegateInvitation).where(DelegateInvitation.email == "dee@example.org")
    )).scalar_one()
    assert invitation.status == "accepted"
    assert invitation.accepted_by_user_id == delegated.user.id
    assert delegated.row.invitation_id == invitation.id

    # And the landing page now routes them to the agent they were added to.
    r = await client.get("/agent", headers=_auth(delegated.user.id))
    assert r.status_code == 302
    assert r.headers["location"] == f"/agent/{OWNER_AGENT}/dashboard"


async def test_a_delegate_can_review_a_proposal_and_a_stranger_cannot(
    client, db_session, world, delegated
):
    url = f"/agent/{OWNER_AGENT}/proposals/{world.td.id}/review"

    denied = await client.post(url, data={"rating": "4"},
                               headers=_auth(world.stranger.id))
    assert denied.status_code == 403
    assert await _reviews(db_session, OWNER_AGENT) == []

    allowed = await client.post(url, data={"rating": "4", "comment": "go"},
                                headers=_auth(delegated.user.id))
    assert allowed.status_code == 302
    review = (await _reviews(db_session, OWNER_AGENT))[0]
    assert review.rating == 4
    # Attribution: the review belongs to the PI, the delegate is recorded
    # alongside (specs/web-delegates.md §Changes to ProposalReview).
    assert review.user_id == world.pi.id
    assert review.delegate_user_id == delegated.user.id
    assert review.reviewed_by_user_id == delegated.user.id


async def test_removing_a_delegate_revokes_their_access(
    client, db_session, world, delegated
):
    dash = f"/agent/{OWNER_AGENT}/dashboard"
    before = await client.get(dash, headers=_auth(delegated.user.id))
    assert before.status_code == 200, "control failed: the delegate could not read the dashboard"

    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/{delegated.row.id}/remove",
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    remaining = (await db_session.execute(
        select(AgentDelegate).where(AgentDelegate.agent_registry_id == world.agent.id)
    )).scalars().all()
    assert remaining == []

    after = await client.get(dash, headers=_auth(delegated.user.id))
    assert after.status_code == 403


async def test_revoking_an_invitation_kills_that_token_only(client, db_session, world):
    doomed = await factories.make_user(db_session, name="Dana Doomed", email="doomed@example.org")
    keeper = await factories.make_user(db_session, name="Kim Keeper", email="keeper@example.org")
    await db_session.flush()
    await _invite(client, world, "doomed@example.org")
    await _invite(client, world, "keeper@example.org")
    doomed_token = await _token_for(db_session, world.agent, "doomed@example.org")
    keeper_token = await _token_for(db_session, world.agent, "keeper@example.org")
    doomed_inv = (await db_session.execute(
        select(DelegateInvitation).where(DelegateInvitation.token == doomed_token)
    )).scalar_one()

    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/{doomed_inv.id}/revoke",
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    statuses = {
        row.email: row.status
        for row in (await db_session.execute(
            select(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == world.agent.id
            )
        )).scalars().all()
    }
    assert statuses == {"doomed@example.org": "revoked", "keeper@example.org": "pending"}

    # The revoked token must not grant access …
    dead = await client.post(f"/invite/{doomed_token}/accept", headers=_auth(doomed.id))
    assert dead.status_code == 200 and "no longer valid" in dead.text
    # … while the untouched one still does (control).
    alive = await client.post(f"/invite/{keeper_token}/accept", headers=_auth(keeper.id))
    assert alive.status_code == 302
    holders = {
        d.user_id
        for d in (await db_session.execute(
            select(AgentDelegate).where(AgentDelegate.agent_registry_id == world.agent.id)
        )).scalars().all()
    }
    assert holders == {keeper.id}


async def test_a_delegate_can_link_their_slack_account(client, db_session, world, delegated, slack):
    """POST /delegates/connect-slack, with the Slack lookup stubbed."""
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    await db_session.flush()
    slack.stub("users_lookupByEmail", {"user": {"id": "U-DELEGATE"}})

    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/connect-slack",
        headers=_auth(delegated.user.id),
    )
    assert r.status_code == 302 and "slack_error" not in r.headers["location"]
    assert ("users_lookupByEmail", {"email": "dee@example.org"}) in slack.calls

    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.delegate_slack_ids == ["U-DELEGATE"]


async def test_connecting_slack_twice_is_idempotent(client, db_session, world, delegated, slack):
    """POST /delegates/connect-slack twice with the same stubbed Slack id must not append
    a second entry — exercises append_delegate_slack_id_stmt's NOT ... ANY() guard at the
    route level, not just in the unit-level compiled-SQL test (issue #22 C1)."""
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    await db_session.flush()
    slack.stub("users_lookupByEmail", {"user": {"id": "U-DELEGATE"}})

    for _ in range(2):
        r = await client.post(
            f"/agent/{OWNER_AGENT}/delegates/connect-slack",
            headers=_auth(delegated.user.id),
        )
        assert r.status_code == 302 and "slack_error" not in r.headers["location"]

    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.delegate_slack_ids == ["U-DELEGATE"]


async def test_accepting_an_invitation_syncs_the_delegates_slack_id(
    client, db_session, world, slack
):
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    await db_session.flush()
    slack.stub("users_lookupByEmail", {"user": {"id": "U-DELEGATE"}})
    delegate = await factories.make_user(db_session, name="Dee Legate", email="dee@example.org")
    await _invite(client, world, "dee@example.org")
    token = await _token_for(db_session, world.agent, "dee@example.org")

    assert (await client.post(f"/invite/{token}/accept",
                              headers=_auth(delegate.id))).status_code == 302
    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.delegate_slack_ids == ["U-DELEGATE"]


async def test_accepting_an_invitation_with_no_bot_token_skips_the_sync_and_logs(
    client, db_session, world, slack, caplog,
):
    """V10b: a delegate invite must still succeed with no bot token configured. The old code
    silently skipped the whole Slack-sync block (`if bot_token:` with no `else`), which hid an
    unconfigured agent token indefinitely. `world.agent.slack_bot_token` is None by default and
    "tstowner" has no `.env` fallback slug, so this is the fixture's default path — no stub
    needed, and the `slack` recorder (autouse) fails the test if invite.py reaches for a client
    anyway.
    """
    delegate = await factories.make_user(db_session, name="Dee Legate", email="dee@example.org")
    await _invite(client, world, "dee@example.org")
    token = await _token_for(db_session, world.agent, "dee@example.org")

    with caplog.at_level("INFO"):
        r = await client.post(f"/invite/{token}/accept", headers=_auth(delegate.id))
    assert r.status_code == 302

    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.delegate_slack_ids is None
    assert slack.calls == [], f"invite.py reached Slack with no usable token: {slack.calls}"
    assert any(
        "no slack bot token" in r.getMessage().lower() and OWNER_AGENT in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


async def test_removing_a_delegate_also_removes_their_slack_id(
    client, db_session, world, delegated, slack
):
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    world.agent.delegate_slack_ids = ["U-OTHER", "U-DELEGATE"]
    await db_session.flush()
    slack.stub("users_lookupByEmail", {"user": {"id": "U-DELEGATE"}})

    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/{delegated.row.id}/remove",
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302

    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.delegate_slack_ids == ["U-OTHER"]


async def test_removing_a_delegate_removes_authority_even_if_the_in_memory_gate_is_stale(
    client, db_session, world, delegated, slack
):
    """Minor 1 (fix round 1): the route's gate used to read
    `if delegate_email and agent.delegate_slack_ids:` — when this session's in-memory `agent`
    was loaded before a concurrent append (e.g. a connect-slack landing in another
    session/transaction), `agent.delegate_slack_ids` reads stale (None/empty) even though the
    DB row already holds the id. The old gate then skipped the Slack lookup and the
    array_remove entirely, so a removed delegate's Slack id stayed in the array and kept
    agent-command authority (src/agent/simulation.py ~4011-4021).

    Simulated here with a raw UPDATE that bypasses the ORM session (so the already-loaded
    `world.agent` instance is never touched/expired) — leaving the in-memory attribute at its
    original None while the DB row is seeded with the delegate's Slack id out from under it.
    """
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    await db_session.flush()
    assert world.agent.delegate_slack_ids is None  # baseline the stale in-memory value

    await db_session.execute(
        text("UPDATE agents SET delegate_slack_ids = ARRAY[:sid] WHERE id = :id"),
        {"sid": "U-DELEGATE", "id": world.agent.id},
    )
    assert world.agent.delegate_slack_ids is None  # still stale after the out-of-band write

    slack.stub("users_lookupByEmail", {"user": {"id": "U-DELEGATE"}})

    r = await client.post(
        f"/agent/{OWNER_AGENT}/delegates/{delegated.row.id}/remove",
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302

    # populate_existing=True: without it this select would just return the SAME
    # already-loaded, never-expired identity-mapped object with its stale cached
    # value — which would make this assertion measure the ORM cache instead of the
    # actual on-disk row the bug (and the fix) are about.
    agent = (await db_session.execute(
        select(AgentRegistry)
        .where(AgentRegistry.agent_id == OWNER_AGENT)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert agent.delegate_slack_ids == []


# ===========================================================================
# 5. The remaining read/write routes — enough behaviour to make the auth matrix
#    mean something (an endpoint that 403s everyone would satisfy authorization
#    tests alone).
# ===========================================================================


async def test_the_dashboard_counts_only_this_agents_activity_and_titles_the_proposal(
    client, db_session, world
):
    """agent_dashboard's three queries and _extract_proposal_title, through the
    real template."""
    await factories.make_agent_message(
        db_session, run=world.run, agent_id=OWNER_AGENT, phase="new_post"
    )
    await factories.make_agent_message(
        db_session, run=world.run, agent_id=OWNER_AGENT, phase="thread_reply",
        thread_ts="1700000000.000100",
    )
    # Control for the agent_id filter: another agent's post must not be counted.
    await factories.make_agent_message(
        db_session, run=world.run, agent_id=OTHER_AGENT, phase="new_post"
    )
    await db_session.flush()

    page = await client.get(f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(world.pi.id))
    assert page.status_code == 200
    assert re.search(r'text-indigo-600">\s*1\s*<', page.text), "posts_count was not 1"
    assert re.search(r'text-blue-600">\s*1\s*<', page.text), "threads_count was not 1"

    # The proposal's *title* is the subject, not the ":memo: **Summary — …**"
    # boilerplate (the raw summary is still rendered inside the panel).
    titles = re.findall(r'text-gray-800 truncate">\s*([^<]*?)\s*<', page.text)
    assert titles == ["A shared assay platform"], titles
    # Unreviewed → the "agent is paused" banner is shown.
    assert "paused from initiating new posts" in page.text

    # Reviewing moves it out of the unreviewed list (the other half).
    r = await client.post(
        f"/agent/{OWNER_AGENT}/proposals/{world.td.id}/review",
        data={"rating": "4"}, headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    page2 = await client.get(f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(world.pi.id))
    assert "paused from initiating new posts" not in page2.text
    assert "A shared assay platform" in page2.text


async def test_dashboard_still_shows_review_form_for_implicit_minus_one_review(
    client, db_session, world
):
    """Issue #20 COR-5/COR-13 residual (Task 20.9c): the engine's implicit
    `rating=-1` marker (written when a PI merely engages a proposal thread,
    without giving an explicit verdict) must not move the proposal into the
    "reviewed" bucket — the PI would then never see the review form again.
    """
    db_session.add(
        ProposalReview(
            thread_decision_id=world.td.id,
            agent_id=OWNER_AGENT,
            user_id=world.pi.id,
            rating=-1,
        )
    )
    await db_session.flush()

    page = await client.get(f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(world.pi.id))
    assert page.status_code == 200
    # The unreviewed-bucket banner and the review form's POST target for this
    # exact proposal must both still be present.
    assert "paused from initiating new posts" in page.text
    assert f'/agent/{OWNER_AGENT}/proposals/{world.td.id}/review' in page.text


async def test_a_legacy_rating_zero_marker_is_not_labelled_as_a_score_or_a_reopen(
    client, db_session, world
):
    """#20 blocker 5 / audit D1. rating=0 is a MARKER with two populations.

    Measured on the production copy: of 233 rating=0 rows only **6** carry a
    '[Reopened]' comment; the other **227** are a 2026-04-30/05-01 bulk backfill with no
    comment and no `reviewed_by_user_id`. Task 16 rendered every one of them "Reopened
    with guidance", asserting an event that never happened for 227 rows; before that they
    rendered as "Rating: 0/4", asserting a score the PI never gave (89 times on one PI's
    own dashboard).

    A marker stays HANDLED -- the PI engaged and the proposal is deliberately not
    rateable again (pinned in test_proposal_review.py) -- so this test is about the LABEL
    only: say what is known and nothing more.
    """
    db_session.add(
        ProposalReview(
            thread_decision_id=world.td.id,
            agent_id=OWNER_AGENT,
            user_id=world.pi.id,
            rating=0,
            comment=None,          # the 227-row backfill shape
        )
    )
    await db_session.flush()

    page = (await client.get(f"/agent/{OWNER_AGENT}/dashboard",
                             headers=_auth(world.pi.id))).text
    assert "Rating: 0/4" not in page, "a marker was rendered as a score the PI never gave"
    assert "Reopened with guidance" not in page, (
        "a backfill row with no comment and no reviewer was labelled as a reopen"
    )
    assert "No score recorded" in page, "the marker should be described, not mislabelled"


async def test_a_genuine_reopen_marker_is_labelled_as_a_reopen(
    client, db_session, world
):
    """The 6-row twin of the test above: a real reopen DOES carry the label, so the
    discriminator is not just suppressing every label."""
    db_session.add(
        ProposalReview(
            thread_decision_id=world.td.id,
            agent_id=OWNER_AGENT,
            user_id=world.pi.id,
            rating=0,
            comment="[Reopened] please broaden the target list",
            reviewed_by_user_id=world.pi.id,
        )
    )
    await db_session.flush()

    page = (await client.get(f"/agent/{OWNER_AGENT}/dashboard",
                             headers=_auth(world.pi.id))).text
    assert "Reopened with guidance" in page
    assert "Rating: 0/4" not in page
    assert "No score recorded" not in page


async def test_posting_a_message_writes_a_pi_row_into_the_named_channel(
    client, db_session, world
):
    r = await client.post(
        f"/agent/{OWNER_AGENT}/message",
        data={"channel_name": "general", "content": "  Let's aim at the assay.  ",
              "tag_bot": "1"},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/agent/{OWNER_AGENT}/conversations?posted=1"

    msg = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.channel_name == "general")
    )).scalar_one()
    assert msg.is_bot is False
    assert msg.agent_id is None
    assert msg.sender_name == "Pat Owner (PI)"
    assert msg.content == "@OwnerBot Let's aim at the assay."  # tag_bot prepends
    assert msg.visibility == "public"
    # RC-1 (#20 COR-5): the ownership carrier _agent_ids_owned_by_user
    # resolves against — must be the acting PI's own id, not NULL.
    assert msg.sender_user_id == world.pi.id
    # RC-2: stamped 'pending' at insert time so a down agent-run's cursor
    # jump can never make this row permanently invisible to the poller.
    assert msg.pi_inbound_state == "pending"

    # …and it is visible on the read view (control that the write is reachable).
    page = await client.get(f"/agent/{OWNER_AGENT}/conversations",
                            headers=_auth(world.pi.id))
    assert page.status_code == 200
    assert "Let&#39;s aim at the assay." in page.text or "aim at the assay" in page.text


async def test_posting_an_empty_message_is_rejected(client, db_session, world):
    r = await client.post(
        f"/agent/{OWNER_AGENT}/message",
        data={"channel_name": "general", "content": "   "},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 400
    assert (await db_session.execute(select(AgentMessage))).scalars().all() == []

    # Control: non-empty content on the same route does write.
    ok = await client.post(
        f"/agent/{OWNER_AGENT}/message",
        data={"channel_name": "general", "content": "real"},
        headers=_auth(world.pi.id),
    )
    assert ok.status_code == 302
    assert len((await db_session.execute(select(AgentMessage))).scalars().all()) == 1


async def test_a_pi_cannot_post_into_another_pairs_private_channel(
    client, db_session, world
):
    third_user, _ = await _agent_for(
        db_session, name="Thea Third", email="thea@example.org",
        agent_id=THIRD_AGENT, bot_name="ThirdBot",
    )
    foreign_td = await factories.make_thread_decision(
        db_session, run=world.run, agent_a=OTHER_AGENT, agent_b=THIRD_AGENT,
        channel="metabolomics", outcome="proposal", summary_text="Summary — theirs",
    )
    await db_session.flush()
    # Produced by the real route, by a PI who is entitled to it.
    r = await client.post(
        f"/agent/{OTHER_AGENT}/proposals/{foreign_td.id}/reopen",
        data={"guidance": "Ours alone."},
        headers=_auth(world.other_pi.id),
    )
    assert r.status_code == 302
    private = (await _private_channels(db_session))[0]
    assert private.visibility == VISIBILITY_COLLAB_PRIVATE

    await client.post(
        f"/agent/{OWNER_AGENT}/message",
        data={"channel_name": private.channel_name, "content": "eavesdropping"},
        headers=_auth(world.pi.id),
    )
    intruder = (await db_session.execute(
        select(AgentMessage).where(
            AgentMessage.channel_name == private.channel_name,
            AgentMessage.is_bot.is_(False),
        )
    )).scalars().all()
    assert intruder == [], (
        "a PI with no membership in this collab_private channel wrote into it: "
        f"{[m.content for m in intruder]}"
    )
    assert third_user is not None


async def test_pi_cannot_reply_into_a_thread_their_agent_never_joined(client, db_session, world):
    """COR-5 web residual: a free-form thread_ts must belong to a thread the posting
    PI's own agent participates in; otherwise the engine would clear another lab's
    proposal-review block on the PI's behalf."""
    root = await factories.make_agent_message(
        db_session, run=world.run, agent_id="alpha", channel_name="general",
        message_ts="1700000000.000100", thread_ts=None, phase="new_post", content="root",
    )
    await factories.make_agent_message(
        db_session, run=world.run, agent_id="beta", channel_name="general",
        message_ts="1700000000.000200", thread_ts=root.message_ts, phase="thread_reply", content="r",
    )
    resp = await client.post(
        f"/agent/{world.agent.agent_id}/message",
        data={"channel_name": "general", "content": "looks good", "thread_ts": "1700000000.000100"},
        headers=_auth(world.pi.id),
    )
    assert resp.status_code == 403
    assert "thread" in resp.json()["detail"].lower()
    written = (await db_session.execute(
        select(AgentMessage).where(
            AgentMessage.thread_ts == "1700000000.000100",
            AgentMessage.is_bot.is_(False),
        )
    )).scalars().all()
    assert written == [], (
        "a PI whose agent never joined this thread wrote into it: "
        f"{[m.content for m in written]}"
    )


async def test_pi_cannot_reply_into_an_unknown_thread(client, db_session, world):
    """The fail-closed branch: a thread_ts naming no root at all is refused, not
    just one the PI's agent didn't join."""
    resp = await client.post(
        f"/agent/{world.agent.agent_id}/message",
        data={"channel_name": "general", "content": "looks good", "thread_ts": "9999999999.999999"},
        headers=_auth(world.pi.id),
    )
    assert resp.status_code == 403
    assert "thread" in resp.json()["detail"].lower()
    written = (await db_session.execute(
        select(AgentMessage).where(
            AgentMessage.thread_ts == "9999999999.999999",
            AgentMessage.is_bot.is_(False),
        )
    )).scalars().all()
    assert written == [], (
        "a PI addressed a nonexistent thread and it was written anyway: "
        f"{[m.content for m in written]}"
    )


async def test_pi_can_reply_into_their_own_agents_thread(client, db_session, world):
    await factories.make_agent_message(
        db_session, run=world.run, agent_id=world.agent.agent_id, channel_name="general",
        message_ts="1700000000.000300", thread_ts=None, phase="new_post", content="root",
    )
    resp = await client.post(
        f"/agent/{world.agent.agent_id}/message",
        data={"channel_name": "general", "content": "thanks", "thread_ts": "1700000000.000300"},
        headers=_auth(world.pi.id),
    )
    assert resp.status_code == 302
    saved = (await db_session.execute(
        select(AgentMessage).where(
            AgentMessage.channel_name == "general",
            AgentMessage.is_bot.is_(False),
        )
    )).scalar_one()
    assert saved.thread_ts == "1700000000.000300"


async def test_sending_a_dm_records_an_inbound_pi_dm(client, db_session, world):
    r = await client.post(
        f"/agent/{OWNER_AGENT}/dm",
        data={"content": "Always cite the 2019 paper."},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302
    dm = (await db_session.execute(select(PiDmMessage))).scalar_one()
    assert dm.agent_id == OWNER_AGENT
    assert dm.direction == "inbound"
    assert dm.content == "Always cite the 2019 paper."
    assert dm.pi_user_id == f"local:{world.pi.id}"


async def test_saving_the_private_profile_persists_to_db_disk_and_a_revision(
    client, db_session, world, profiles_dir
):
    r = await client.post(
        f"/agent/{OWNER_AGENT}/profile/save",
        data={"content": "# Private\nUnpublished compound series X."},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    assert "compound series X" in profile.private_profile_md
    assert (profiles_dir / "private" / f"{OWNER_AGENT}.md").exists()
    revisions = (await db_session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == world.agent.id)
    )).scalars().all()
    assert [x.profile_type for x in revisions] == ["private"]
    assert revisions[0].changed_by_user_id == world.pi.id
    assert revisions[0].mechanism == "web"


async def test_first_private_profile_save_creates_the_missing_profile_row(
    client, db_session
):
    """Before this fix, `if profile:` made a first-ever private save (no
    ResearcherProfile row yet) a silent, permanent disk-only write."""
    pi, _agent = await _agent_for(
        db_session, name="No Profile", email="noprof@example.org",
        agent_id="tstnoprof", bot_name="NoProfBot",
    )
    await db_session.flush()

    r = await client.post(
        "/agent/tstnoprof/profile/save",
        headers=_auth(pi.id),
        data={"content": "first ever private instructions"},
    )
    assert r.status_code == 302
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == pi.id)
    )).scalar_one()
    assert profile.private_profile_md == "first ever private instructions"


async def test_saving_the_public_profile_updates_the_pis_profile_not_the_editors(
    client, db_session, world, delegated
):
    """A delegate edit must land on the PI's ResearcherProfile row."""
    await factories.make_profile(db_session, user=delegated.user, research_summary="Delegate's own")
    await db_session.flush()

    r = await client.post(
        f"/agent/{OWNER_AGENT}/public-profile/save",
        data={
            "research_summary": "Chemical biology of proteostasis.",
            "techniques": "cryo-EM, mass spec",
            "keywords": "proteostasis",
        },
        headers=_auth(delegated.user.id),
    )
    assert r.status_code == 302 and "saved=1" in r.headers["location"]

    pi_profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    assert pi_profile.research_summary == "Chemical biology of proteostasis."
    assert pi_profile.techniques == ["cryo-EM", "mass spec"]

    # Control: the delegate's own profile is untouched — a route that wrote to
    # current_user's profile would have clobbered this instead.
    delegate_profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == delegated.user.id)
    )).scalar_one()
    assert delegate_profile.research_summary == "Delegate's own"


async def test_save_public_profile_partial_post_does_not_blank_omitted_fields(
    client, db_session, world
):
    """The `world` fixture already gives `world.pi` a ResearcherProfile
    (`factories.make_profile(db_session, user=pi)` in the fixture itself) — and
    `researcher_profiles.user_id` is UNIQUE, so a second `factories.make_profile(...,
    user=world.pi, ...)` here would raise IntegrityError at flush. Mutate the row
    the fixture already created instead."""
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    profile.research_summary = "old summary"
    profile.techniques = ["old-t"]
    await db_session.flush()

    r = await client.post(
        f"/agent/{OWNER_AGENT}/public-profile/save",
        headers=_auth(world.pi.id),
        data={"keywords": "new-kw"},
    )
    assert r.status_code == 302

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    assert profile.research_summary == "old summary"
    assert profile.techniques == ["old-t"]
    assert profile.keywords == ["new-kw"]


async def test_save_public_profile_still_clears_a_field_the_user_emptied(client, db_session, world):
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    profile.research_summary = "old"
    profile.techniques = ["t"]
    await db_session.flush()

    r = await client.post(
        f"/agent/{OWNER_AGENT}/public-profile/save",
        headers=_auth(world.pi.id),
        data={"research_summary": "", "techniques": ""},
    )
    assert r.status_code == 302

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    assert profile.research_summary == ""
    assert profile.techniques == []


async def test_save_public_profile_resets_synthesis_validated(client, db_session, world):
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    profile.research_summary = "old summary"
    profile.techniques = ["old-t"]
    profile.synthesis_validated = False
    await db_session.flush()

    r = await client.post(
        f"/agent/{OWNER_AGENT}/public-profile/save",
        headers=_auth(world.pi.id),
        data={"research_summary": "hand-edited", "techniques": "t1"},
    )
    assert r.status_code == 302

    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == world.pi.id)
    )).scalar_one()
    assert profile.synthesis_validated is None


async def test_connect_slack_stores_the_pis_slack_user_id(client, db_session, world, slack):
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    await db_session.flush()
    slack.stub("users_lookupByEmail", {"user": {"id": "U-PI"}})

    r = await client.post(
        f"/agent/{OWNER_AGENT}/slack",
        data={"email": "pi@example.org"},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302 and "slack_error" not in r.headers["location"]
    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.slack_user_id == "U-PI"


async def test_connect_slack_reports_a_lookup_failure_without_writing(
    client, db_session, world, slack
):
    world.agent.slack_bot_token = "xoxb-fake-for-tests"
    await db_session.flush()

    def _not_found(**kwargs):
        raise RuntimeError("users_not_found")

    slack.stub("users_lookupByEmail", _not_found)
    r = await client.post(
        f"/agent/{OWNER_AGENT}/slack",
        data={"email": "nobody@example.org"},
        headers=_auth(world.pi.id),
    )
    assert r.status_code == 302 and "slack_error" in r.headers["location"]
    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.slack_user_id is None

    # Control: the same route with a resolving lookup does write.
    slack.stub("users_lookupByEmail", {"user": {"id": "U-PI"}})
    assert (await client.post(
        f"/agent/{OWNER_AGENT}/slack", data={"email": "pi@example.org"},
        headers=_auth(world.pi.id),
    )).status_code == 302
    agent = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == OWNER_AGENT)
    )).scalar_one()
    assert agent.slack_user_id == "U-PI"


# ===========================================================================
# 6. Authorization, all 19 endpoints
# ===========================================================================


@dataclass(frozen=True)
class Ep:
    method: str
    route: str                       # exactly as registered, for the inventory check
    template: str                    # concrete path, .format(**ctx)
    data: dict = field(default_factory=dict)
    agent_scoped: bool = True        # goes through get_agent_with_access
    owner_only: bool = False         # rejects delegates (specs/web-delegates.md)

    @property
    def id(self) -> str:
        return f"{self.method} {self.route}"


ENDPOINTS: list[Ep] = [
    Ep("GET", "/agent", "/agent", agent_scoped=False),
    Ep("POST", "/agent/request", "/agent/request", agent_scoped=False),
    Ep("GET", "/agent/{agent_id}/dashboard", "/agent/{agent}/dashboard"),
    Ep("GET", "/agent/{agent_id}/conversations", "/agent/{agent}/conversations"),
    Ep("GET", "/agent/{agent_id}/thread/{message_ts}", "/agent/{agent}/thread/{ts}"),
    Ep("POST", "/agent/{agent_id}/message", "/agent/{agent}/message",
       {"channel_name": "general", "content": "hello"}),
    Ep("POST", "/agent/{agent_id}/dm", "/agent/{agent}/dm", {"content": "directive"}),
    Ep("GET", "/agent/{agent_id}/profile", "/agent/{agent}/profile"),
    Ep("GET", "/agent/{agent_id}/profile/edit", "/agent/{agent}/profile/edit"),
    Ep("POST", "/agent/{agent_id}/profile/save", "/agent/{agent}/profile/save",
       {"content": "# Private"}),
    Ep("GET", "/agent/{agent_id}/public-profile", "/agent/{agent}/public-profile"),
    Ep("GET", "/agent/{agent_id}/public-profile/edit", "/agent/{agent}/public-profile/edit"),
    Ep("POST", "/agent/{agent_id}/public-profile/save", "/agent/{agent}/public-profile/save",
       {"research_summary": "s", "techniques": "a,b", "keywords": "k"}),
    Ep("POST", "/agent/{agent_id}/proposals/{thread_decision_id}/review",
       "/agent/{agent}/proposals/{td}/review", {"rating": "3"}),
    Ep("POST", "/agent/{agent_id}/proposals/{thread_decision_id}/reopen",
       "/agent/{agent}/proposals/{td}/reopen", {"guidance": "refine the aims"}),
    Ep("POST", "/agent/{agent_id}/slack", "/agent/{agent}/slack",
       {"email": "pi@example.org"}, owner_only=True),
    Ep("POST", "/agent/{agent_id}/delegates/connect-slack",
       "/agent/{agent}/delegates/connect-slack"),
    Ep("POST", "/agent/{agent_id}/delegates/invite", "/agent/{agent}/delegates/invite",
       {"emails": "fresh@example.org"}, owner_only=True),
    Ep("POST", "/agent/{agent_id}/delegates/{invitation_id}/revoke",
       "/agent/{agent}/delegates/{inv}/revoke", owner_only=True),
    Ep("POST", "/agent/{agent_id}/delegates/{delegate_id}/remove",
       "/agent/{agent}/delegates/{dele}/remove", owner_only=True),
]

AGENT_SCOPED = [e for e in ENDPOINTS if e.agent_scoped]


def test_the_endpoint_table_matches_the_registered_routes():
    """A new endpoint on agent_page.py must show up here as a missing entry.

    Without this the parametrised authorization tests silently keep passing
    while an unprotected route ships.
    """
    from src.routers import agent_page

    registered = {
        (method, "/agent" + route.path)
        for route in agent_page.router.routes
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    }
    listed = {(e.method, e.route) for e in ENDPOINTS}
    assert registered == listed, (
        f"missing from ENDPOINTS: {sorted(registered - listed)}; "
        f"stale entries: {sorted(listed - registered)}"
    )
    assert len(ENDPOINTS) == 20


def _path(ep: Ep, world, delegated=None, ts: str = "0.0000") -> str:
    return ep.template.format(
        agent=OWNER_AGENT,
        td=world.td.id,
        inv=delegated.pending_invitation.id if delegated else uuid.uuid4(),
        dele=delegated.row.id if delegated else uuid.uuid4(),
        ts=ts,
    )


@pytest.fixture
async def thread_root(db_session, world) -> str:
    """A real thread ROOT belonging to OWNER_AGENT, for the
    /agent/{agent_id}/thread/{message_ts} authorization tests.

    Not folded into `world` itself: several tests assert an exact,
    unfiltered count of `AgentMessage` rows (e.g.
    test_posting_an_empty_message_is_rejected), so a message seeded into the
    shared fixture would silently change what they are counting. Without a
    ts that actually resolves, the owner's positive-control request would 404
    — which is not in the {200, 302} the authorization tests accept — and
    would mask a stranger's 403 test passing for the wrong reason.
    """
    msg = await factories.make_agent_message(
        db_session, run=world.run, agent_id=OWNER_AGENT, channel_name="general",
        channel_id="C-THREAD-ROOT", phase="new_post", message_ts="50.0001",
        sender_name="OwnerBot", content="root", visibility="public",
    )
    await db_session.flush()
    return msg.message_ts


@pytest.mark.parametrize("ep", ENDPOINTS, ids=[e.id for e in ENDPOINTS])
async def test_every_endpoint_redirects_a_logged_out_visitor(client, world, delegated, ep):
    # The `delegated` fixture authenticated as the PI and the delegate on this
    # same httpx client; empty the jar so "logged out" means exactly that and
    # cannot be satisfied (or broken) by a leftover Set-Cookie.
    client.cookies.clear()
    r = await client.request(ep.method, _path(ep, world, delegated), data=ep.data)
    assert r.status_code == 302, f"{ep.id} returned {r.status_code} to an anonymous caller"
    assert r.headers["location"].startswith("/login"), r.headers["location"]


@pytest.mark.parametrize("ep", AGENT_SCOPED, ids=[e.id for e in AGENT_SCOPED])
async def test_a_stranger_cannot_touch_an_agent_they_do_not_own(
    client, world, delegated, ep, slack, thread_root
):
    """The half worth most: a logged-in user with no relationship to the agent.

    Positive control in the same test — the *owner* making the identical request
    is not rejected, so a route that 403s everyone (or one that 404s because the
    fixture URL is wrong) cannot pass this.
    """
    slack.stub("users_lookupByEmail", {"user": {"id": "U-PI"}})
    path = _path(ep, world, delegated, ts=thread_root)

    denied = await client.request(ep.method, path, data=ep.data,
                                  headers=_auth(world.stranger.id))
    assert denied.status_code == 403, (
        f"{ep.id} let a stranger through with {denied.status_code}"
    )
    assert denied.json()["detail"] == "Access denied"

    allowed = await client.request(ep.method, path, data=ep.data,
                                   headers=_auth(world.pi.id))
    assert allowed.status_code in (200, 302), f"{ep.id} owner control got {allowed.status_code}"
    if allowed.status_code == 302:
        assert not allowed.headers["location"].startswith("/login")


@pytest.mark.parametrize("ep", AGENT_SCOPED, ids=[e.id for e in AGENT_SCOPED])
async def test_delegate_write_access_matches_the_spec(
    client, world, delegated, ep, slack, thread_root
):
    """Delegates get everything except delegate management and Slack linking of
    the PI's own account (specs/web-delegates.md §Write access differentiation).

    Both halves are in this one parametrisation: the owner-only endpoints must
    reject, and every other endpoint must accept.
    """
    slack.stub("users_lookupByEmail", {"user": {"id": "U-DELEGATE"}})
    r = await client.request(ep.method, _path(ep, world, delegated, ts=thread_root),
                             data=ep.data, headers=_auth(delegated.user.id))
    if ep.owner_only:
        assert r.status_code == 403, f"{ep.id} should be PI-only, got {r.status_code}"
        assert "Only the PI" in r.json()["detail"]
    else:
        assert r.status_code in (200, 302), f"{ep.id} refused a delegate ({r.status_code})"
        if r.status_code == 302:
            assert not r.headers["location"].startswith("/login")


async def test_an_unknown_agent_id_is_a_404_not_a_403(client, world):
    """Distinguishes "no such agent" from "not yours" — a 403 here would leak
    nothing, but a 200 would mean the lookup was skipped entirely."""
    r = await client.get("/agent/nosuchagent/dashboard", headers=_auth(world.pi.id))
    assert r.status_code == 404
    # Control: the real slug is reachable for the same user.
    ok = await client.get(f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(world.pi.id))
    assert ok.status_code == 200


async def test_a_pending_agent_cannot_reach_the_dashboard(client, db_session, world):
    world.agent.status = "pending"
    await db_session.flush()
    r = await client.get(f"/agent/{OWNER_AGENT}/dashboard", headers=_auth(world.pi.id))
    assert r.status_code == 302 and r.headers["location"] == "/agent"

    # Control: active reaches it. (inactive is allowed in too — the dashboard
    # gates the reopen action separately, see agent_dashboard's docstring.)
    world.agent.status = "inactive"
    await db_session.flush()
    assert (await client.get(f"/agent/{OWNER_AGENT}/dashboard",
                             headers=_auth(world.pi.id))).status_code == 200
