"""Tests for the public-thread → collab_private channel migration service.

Covers the pure parts of src/services/private_channels.py and the new
slack_client helpers. Full end-to-end orchestration is exercised via the
mock-mode AgentSlackClient (no real Slack).

Most of the file is pure and DB-free; the transaction-boundary tests at the end
(``TestOfflineMigrationDurability``) need a real Postgres and carry the
``integration`` marker.
"""

import threading

import pytest
from sqlalchemy import func, select

from src.agent.slack_client import AgentSlackClient
from src.models import (
    VISIBILITY_COLLAB_PRIVATE,
    AgentChannel,
    AgentMessage,
    PrivateChannelMember,
    ThreadDecision,
)
from src.services.private_channels import (
    _build_handover_messages,
    _build_other_pi_dm,
    _build_slug,
    migrate_public_thread_to_private,
)
from tests import factories
from tests.fakes import FakeSlackClient


def _join_handover(
    creator_pi_name: str,
    proposal_summary: str | None,
    guidance_text: str,
    origin_channel_name: str,
) -> str:
    """Test helper: concatenate all handover posts for content assertions."""
    return "\n---\n".join(
        _build_handover_messages(
            creator_pi_name=creator_pi_name,
            proposal_summary=proposal_summary,
            guidance_text=guidance_text,
            origin_channel_name=origin_channel_name,
        )
    )


# ---------------------------------------------------------------------------
# Slug generation (G6 — descriptive names, with trade-off accepted)
# ---------------------------------------------------------------------------


class TestSlug:
    def test_sorts_agent_ids_alphabetically(self):
        """Slug is stable regardless of which agent creates the channel."""
        a = _build_slug("wiseman", "su", "drug-repurposing")
        b = _build_slug("su", "wiseman", "drug-repurposing")
        assert a == b
        assert a == "priv-su-wiseman-drug-repurposing"

    def test_includes_origin_channel_as_topic_hint(self):
        slug = _build_slug("cravatt", "wu", "chemical-biology")
        assert slug.startswith("priv-cravatt-wu-")
        assert "chemical-biology" in slug

    def test_respects_slack_80_char_cap(self):
        """Long origin names get truncated by normalize_channel_name."""
        slug = _build_slug("su", "wiseman", "x" * 200)
        assert len(slug) <= 80

    def test_lowercase_and_hyphenated(self):
        slug = _build_slug("Su", "Wiseman", "Drug_Repurposing")
        assert slug == slug.lower()
        assert "_" not in slug


# ---------------------------------------------------------------------------
# Handover message — must contain guidance verbatim (it's the migration's
# whole point) and must NOT appear in the origin thread by construction.
# ---------------------------------------------------------------------------


class TestHandoverMessages:
    def test_contains_guidance_verbatim(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="Joint cryo-ET study of mitochondrial remodeling.",
            guidance_text="Include the unpublished HRI activator structural data.",
            origin_channel_name="structural-biology",
        )
        assert "Include the unpublished HRI activator structural data." in joined

    def test_contains_proposal_summary(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="Joint cryo-ET study of mitochondrial remodeling.",
            guidance_text="x",
            origin_channel_name="structural-biology",
        )
        assert "Joint cryo-ET study of mitochondrial remodeling." in joined

    def test_tolerates_missing_summary(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary=None,
            guidance_text="x",
            origin_channel_name="general",
        )
        assert "(no summary recorded)" in joined

    def test_references_origin_channel(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="s",
            guidance_text="g",
            origin_channel_name="drug-repurposing",
        )
        assert "#drug-repurposing" in joined

    def test_names_creator_pi(self):
        joined = _join_handover(
            creator_pi_name="Andrew Su",
            proposal_summary="s",
            guidance_text="g",
            origin_channel_name="general",
        )
        assert "Andrew Su" in joined

    def test_short_handover_returns_three_posts(self):
        """Short content: [header, single guidance, closing] = 3 posts."""
        posts = _build_handover_messages(
            creator_pi_name="Andrew Su",
            proposal_summary="short summary",
            guidance_text="short guidance",
            origin_channel_name="general",
        )
        assert len(posts) == 3
        assert posts[0].startswith("*Private refinement channel*")
        assert "short guidance" in posts[1]
        assert posts[2] == "Continuing the conversation here — bots, please proceed with refinement."

    def test_long_guidance_splits_across_posts(self):
        """Long guidance exceeds per-post budget → split, with (N of M) labels."""
        long_guidance = "\n\n".join([f"Paragraph {i}: " + ("x" * 500) for i in range(10)])
        posts = _build_handover_messages(
            creator_pi_name="Andrew Su",
            proposal_summary="summary",
            guidance_text=long_guidance,
            origin_channel_name="general",
        )
        # At least 4 posts: header + ≥2 guidance chunks + closing
        assert len(posts) >= 4
        # Every post is under the length budget
        assert all(len(p) <= 3500 for p in posts)
        # Guidance chunks labeled (N of M)
        guidance_chunks = [p for p in posts if p.startswith("*Guidance from Andrew Su")]
        assert len(guidance_chunks) >= 2
        for i, chunk in enumerate(guidance_chunks, start=1):
            assert f"({i} of {len(guidance_chunks)})" in chunk

    def test_every_post_under_length_budget(self):
        """Even pathologically long inputs are clamped."""
        huge = "a" * 50000
        posts = _build_handover_messages(
            creator_pi_name="Andrew Su",
            proposal_summary=huge,
            guidance_text=huge,
            origin_channel_name="general",
        )
        assert all(len(p) <= 3500 for p in posts)


# ---------------------------------------------------------------------------
# Other-PI DM — must NOT leak the guidance text to a PI who hasn't accepted
# the channel invite yet. Their visibility to guidance content is gated by
# whether they join the private channel.
# ---------------------------------------------------------------------------


class TestOtherPIDMContent:
    def test_does_not_include_guidance_text(self):
        """The DM pointer must not embed the guidance — that lives in the
        private channel, which the PI only sees after joining."""
        dm = _build_other_pi_dm(
            other_pi_name="Luke Wiseman",
            creator_pi_name="Andrew Su",
            origin_channel_name="drug-repurposing",
            new_channel_name="priv-su-wiseman-drug-repurposing",
        )
        # Sanity: ensure the specific guidance string we use in another test
        # would never leak via this DM.
        assert "Include the unpublished HRI activator structural data." not in dm

    def test_references_both_pis_and_channels(self):
        dm = _build_other_pi_dm(
            other_pi_name="Luke Wiseman",
            creator_pi_name="Andrew Su",
            origin_channel_name="drug-repurposing",
            new_channel_name="priv-su-wiseman-drug-repurposing",
        )
        assert "Luke" in dm  # first name form is fine
        assert "Andrew Su" in dm
        assert "drug-repurposing" in dm
        assert "priv-su-wiseman-drug-repurposing" in dm


# ---------------------------------------------------------------------------
# Slack client helpers — mock mode
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """AgentSlackClient in mock mode (no real Slack)."""
    return AgentSlackClient(agent_id="su", bot_token="xoxb-placeholder-abc")


class TestCreatePrivateChannel:
    def test_returns_mock_channel_with_is_private(self, mock_client):
        ch = mock_client.create_private_channel("priv-test")
        assert ch is not None
        # Mock mode applies the same timestamp suffix as the live path.
        assert ch["name"].startswith("priv-test-")
        assert ch["is_private"] is True
        # Slack-off channels use the DB-native 'local:' id scheme.
        assert ch["id"].startswith("local:")

    def test_public_create_channel_still_works(self, mock_client):
        """Don't regress the existing create_channel behavior."""
        ch = mock_client.create_channel("general")
        assert ch is not None
        assert ch["name"] == "general"
        # Slack-off channels use the DB-native 'local:' id scheme.
        assert ch["id"] == "local:general"


class _FakeSlack:
    """Minimal stand-in for slack_sdk.WebClient.conversations_create.

    Raises name_taken for the first ``fail_times`` calls, then succeeds. Using
    a call counter (rather than a set of taken names) keeps the tests robust to
    the timestamp suffix, whose exact value isn't predictable.
    """

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []

    def conversations_create(self, name, is_private=False):
        from slack_sdk.errors import SlackApiError

        self.calls.append(name)
        if len(self.calls) <= self.fail_times:
            raise SlackApiError("name_taken", response={"error": "name_taken"})
        return {"channel": {"id": f"C_{name}", "name": name, "is_private": is_private}}


class TestCreatePrivateChannelNameTaken:
    """Regression: a second proposal between the same agent pair in the same
    origin channel yields an identical base slug; Slack rejects it with
    name_taken. create_private_channel disambiguates with a UTC timestamp
    suffix (plus random entropy on collision) rather than failing the reopen."""

    _BASE = "priv-lairson-su-drug-repurposing"

    def _live_client(self, fail_times=0):
        client = AgentSlackClient(agent_id="su", bot_token="xoxb-real-token")
        client._client = _FakeSlack(fail_times)  # force out of mock mode
        return client

    def test_appends_timestamp_suffix(self):
        client = self._live_client()
        ch = client.create_private_channel(self._BASE)
        assert ch is not None
        # Base preserved, with a -YYYYMMDD-HHMMSS suffix appended.
        assert ch["name"].startswith(self._BASE + "-")
        assert ch["name"] != self._BASE
        # One API call in the common case — no probe-and-increment loop.
        assert len(client._client.calls) == 1

    def test_retries_with_entropy_on_name_taken(self):
        client = self._live_client(fail_times=1)
        ch = client.create_private_channel(self._BASE)
        assert ch is not None
        assert ch["name"].startswith(self._BASE + "-")
        # Two attempts: timestamp, then timestamp + entropy.
        calls = client._client.calls
        assert len(calls) == 2
        assert len(calls[1]) > len(calls[0])  # entropy makes the 2nd longer

    def test_returns_none_when_all_attempts_exhausted(self):
        client = self._live_client(fail_times=99)
        assert client.create_private_channel(self._BASE) is None

    def test_respects_slack_80_char_cap(self):
        client = self._live_client()
        long_base = "priv-" + ("x" * 100)
        ch = client.create_private_channel(long_base)
        assert ch is not None
        assert len(ch["name"]) <= 80


class TestInviteToChannel:
    def test_empty_invite_list_is_noop_true(self, mock_client):
        assert mock_client.invite_to_channel("C123", []) is True

    def test_mock_mode_returns_true(self, mock_client):
        assert mock_client.invite_to_channel("C123", ["U1", "U2", "BOT3"]) is True


# ---------------------------------------------------------------------------
# Sanity: service + endpoint modules import cleanly. Catches syntax errors
# and missing deps that would otherwise only surface at request time.
# ---------------------------------------------------------------------------


class TestImports:
    def test_service_module_imports(self):
        import src.services.private_channels as svc  # noqa: F401
        assert hasattr(svc, "migrate_public_thread_to_private")
        assert hasattr(svc, "MigrationResult")

    def test_reopen_endpoint_imports(self):
        from src.routers.agent_page import reopen_proposal  # noqa: F401

    def test_config_flag_available(self):
        from src.config import get_settings
        settings = get_settings()
        assert hasattr(settings, "enable_private_refinement")
        assert isinstance(settings.enable_private_refinement, bool)


# ---------------------------------------------------------------------------
# Transaction boundary — the Slack-off migration owns its own commit (#24 N1-a)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOfflineMigrationDurability:
    """`_migrate_offline`'s rows must outlive a rollback by the caller.

    The Slack-on path has committed its own rows since `34d3c15`; the Slack-off
    path only flushed them, so it kept the whole of N1: `reopen_proposal`'s
    `except IntegrityError: await db.rollback()` discarded the AgentChannel, its
    three members and the handover messages, while its recovery arm re-bound
    `refined_in_channel` to the `local:` id of a channel whose rows no longer
    existed — a dangling pointer no retry could repair.
    """

    async def _seed(self, db):
        run = await factories.make_simulation_run(db)
        pi = await factories.make_user(db, name="Andrew Su")
        await factories.make_agent(
            db, user=pi, agent_id="alpha", bot_name="AlphaBot", pi_name="Andrew Su",
        )
        other_pi = await factories.make_user(db, name="Luke Wiseman")
        await factories.make_agent(
            db, user=other_pi, agent_id="beta", bot_name="BetaBot", pi_name="Luke Wiseman",
        )
        td = await factories.make_thread_decision(
            db, run=run, agent_a="alpha", agent_b="beta",
            channel="drug-repurposing", origin_visibility="public",
            summary_text="Joint repurposing screen of the HRI activator series.",
        )
        return run, pi, td

    @pytest.fixture
    def slack_off(self, monkeypatch):
        """Force the DB-only migration path (mirrors test_proposal_review.py's fixture)."""
        async def _off(*args, **kwargs):
            return False

        monkeypatch.setattr(
            "src.services.private_channels._slack_enabled_for_migration", _off,
        )

    async def test_offline_migration_rows_survive_the_callers_rollback(
        self, db_session, slack_off,
    ):
        run, pi, td = await self._seed(db_session)
        # Read every PK out of the ORM now: expire_all() below detaches these
        # instances from a usable connection, and a lazy re-load of `run.id` from a
        # plain assertion raises MissingGreenlet rather than reporting the count.
        run_id, td_id = run.id, td.id

        result = await migrate_public_thread_to_private(
            db_session,
            thread_decision=td,
            creator_agent_id="alpha",
            creator_pi_user=pi,
            guidance_text="Nail down the ternary-complex geometry first.",
        )
        assert result.channel_id.startswith("local:")

        # Control: every row exists BEFORE the rollback, so "absent afterwards"
        # cannot be an artefact of a seed that never ran.
        assert await self._channels(db_session, run_id) == 1
        assert await self._members(db_session, result.agent_channel_id) == 3
        assert await self._messages(db_session, result.channel_id) >= 1

        # Second control, in the other direction: a row the CALLER adds after the
        # migration returns is still the caller's to lose. If this survived too, the
        # harness would not be rolling anything back and the assertions below would
        # be vacuous.
        db_session.add(AgentMessage(
            simulation_run_id=run_id, agent_id="alpha",
            channel_id=result.channel_id, channel_name=result.channel_name,
            message_ts="9999999999.000001", message_length=7, phase="new_post",
            visibility=VISIBILITY_COLLAB_PRIVATE, content="caller", sender_name="alphaBot",
            is_bot=True, posted_at=9999999999.000001,
        ))
        await db_session.flush()

        # What reopen_proposal's `except IntegrityError` arm does.
        await db_session.rollback()
        db_session.expire_all()

        assert await self._channels(db_session, run_id) == 1, (
            "the Slack-off migration's AgentChannel row was rolled back with the "
            "caller's losing write — the engine discovers private channels only from "
            "agent_channels, so the refinement channel would exist for nobody"
        )
        assert await self._members(db_session, result.agent_channel_id) == 3, (
            "both bots and the triggering PI lost their membership rows"
        )
        assert await self._messages(db_session, result.channel_id) >= 1, (
            "the handover — which carries the PI's guidance verbatim — was discarded"
        )
        reloaded = (await db_session.execute(
            select(ThreadDecision).where(ThreadDecision.id == td_id)
        )).scalar_one()
        assert reloaded.refined_in_channel == result.channel_id, (
            "refined_in_channel must point at a channel whose rows still exist"
        )
        assert await db_session.scalar(
            select(func.count(AgentMessage.id)).where(
                AgentMessage.message_ts == "9999999999.000001"
            )
        ) == 0, (
            "the caller's own post-migration row survived, so this test proves "
            "nothing about the migration's commit"
        )

    @staticmethod
    async def _channels(db, run_id) -> int:
        return await db.scalar(
            select(func.count(AgentChannel.id)).where(
                AgentChannel.simulation_run_id == run_id,
                AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE,
            )
        )

    @staticmethod
    async def _members(db, agent_channel_id) -> int:
        return await db.scalar(
            select(func.count(PrivateChannelMember.id)).where(
                PrivateChannelMember.agent_channel_id == agent_channel_id
            )
        )

    @staticmethod
    async def _messages(db, channel_id) -> int:
        return await db.scalar(
            select(func.count(AgentMessage.id)).where(
                AgentMessage.channel_id == channel_id
            )
        )


# ---------------------------------------------------------------------------
# RC-14 (audit 2026-09-08 follow-up) — every Slack call runs off the event loop
# ---------------------------------------------------------------------------


class _ThreadRecordingSlackClient(FakeSlackClient):
    """Records which OS thread each Slack-calling method actually ran on.

    A synchronous ``AgentSlackClient`` call can block for up to
    ``slack_client.RATE_LIMIT_WAIT_BUDGET_SECONDS`` (180s, RC-3) under a
    sustained 429. Called straight from async code, that freezes the single
    ASGI worker or the worker process for the whole retry loop. This fake
    proves the fix: every method the migration calls records
    ``threading.get_ident()`` before delegating to the real (mock-mode)
    behaviour, so a test can assert none of them ran on the event loop's own
    thread.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.threads_seen: set[int] = set()

    def _record(self) -> None:
        self.threads_seen.add(threading.get_ident())

    def create_private_channel(self, name):
        self._record()
        return super().create_private_channel(name)

    def invite_to_channel(self, channel_id, user_ids):
        self._record()
        return super().invite_to_channel(channel_id, user_ids)

    def post_message(self, channel, text, thread_ts=None):
        self._record()
        return super().post_message(channel, text, thread_ts)

    def send_dm(self, user_id, text):
        self._record()
        return super().send_dm(user_id, text)

    def _resolve_channel_id(self, channel):
        self._record()
        return super()._resolve_channel_id(channel)


@pytest.mark.integration
class TestSlackCallsRunOffTheEventLoop:
    """RC-14: src/services/private_channels.py calls straight into
    AgentSlackClient's synchronous, blocking methods from async code (the web
    reopen route, the e-mail inbound worker). Every one of those calls must go
    through ``asyncio.to_thread`` so a Slack throttle cannot freeze the
    process.
    """

    async def _seed(self, db):
        run = await factories.make_simulation_run(db)
        pi = await factories.make_user(db, name="Andrew Su")
        await factories.make_agent(
            db, user=pi, agent_id="alpha", bot_name="AlphaBot", pi_name="Andrew Su",
            slack_user_id="U_ALPHA_PI",
        )
        other_pi = await factories.make_user(db, name="Luke Wiseman")
        await factories.make_agent(
            db, user=other_pi, agent_id="beta", bot_name="BetaBot", pi_name="Luke Wiseman",
            slack_user_id="U_BETA_PI",
        )
        td = await factories.make_thread_decision(
            db, run=run, agent_a="alpha", agent_b="beta",
            channel="drug-repurposing", origin_visibility="public",
            summary_text="Joint repurposing screen of the HRI activator series.",
        )
        return run, pi, td

    async def test_migration_slack_calls_execute_off_the_event_loop_thread(
        self, db_session, monkeypatch,
    ):
        run, pi, td = await self._seed(db_session)
        loop_thread = threading.get_ident()

        made: list[_ThreadRecordingSlackClient] = []
        make_client_threads: list[int] = []

        async def _on(*a, **k):
            return True

        async def _token(db, agent_id):
            return f"xoxb-fake-{agent_id}"

        def _client(agent_id, bot_token):
            make_client_threads.append(threading.get_ident())
            c = _ThreadRecordingSlackClient(agent_id=agent_id, bot_token=bot_token)
            made.append(c)
            return c

        monkeypatch.setattr(
            "src.services.private_channels._slack_enabled_for_migration", _on)
        monkeypatch.setattr(
            "src.services.private_channels._get_or_fail_bot_token", _token)
        monkeypatch.setattr("src.services.private_channels._make_client", _client)

        result = await migrate_public_thread_to_private(
            db_session,
            thread_decision=td,
            creator_agent_id="alpha",
            creator_pi_user=pi,
            guidance_text="Nail down the ternary-complex geometry first.",
        )

        # Sanity: this exercised the Slack-on path (a real channel-shaped id),
        # not the DB-only fallback — otherwise the assertions below would be
        # vacuously true because no Slack call was ever made.
        assert result.channel_id.startswith("G_")
        assert len(made) == 2, "expected one client each for the creator and other bot"

        assert make_client_threads, "_make_client was never called"
        assert loop_thread not in make_client_threads, (
            "_make_client (which calls AgentSlackClient.connect()) ran on the "
            "event loop's own thread"
        )
        for client in made:
            assert client.threads_seen, (
                f"no Slack calls were recorded on the {client.agent_id} client"
            )
            assert loop_thread not in client.threads_seen, (
                f"a Slack call on the {client.agent_id} client ran on the event "
                "loop's own thread — a sustained throttle would freeze the "
                "whole process for up to RATE_LIMIT_WAIT_BUDGET_SECONDS"
            )
