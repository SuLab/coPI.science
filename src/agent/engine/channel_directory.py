"""Channel names, Slack ids and visibility for the run, and the seeded / assessments-summary channel bootstrap (spec §7.1)."""

from __future__ import annotations

import logging

from src.agent.engine import constants
from src.agent.engine.constants import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.engine.context import EngineContext, via
from src.agent.engine.helpers import hub_agent
from src.agent.slack_client import SlackListingIncomplete
from src.models import AgentChannel
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

logger = logging.getLogger("src.agent.simulation")


class ChannelDirectory:
    """The run's channel-name to Slack-id and visibility maps."""

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    slack_clients = via("ctx")

    OWNED_STATE: tuple[str, ...] = ("_channel_id_map", "_channel_visibility", "_assessments_summary_channel_id")

    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx
        # Channel ID map (populated during setup)
        self._channel_id_map: dict[str, str] = {}  # name -> id
        # Channel visibility map (populated from agent_channels.visibility
        # during setup; defaults to 'public' for any name not present). Used
        # by G1 prompt scoping and G3 dedup filtering.
        self._channel_visibility: dict[str, str] = {}  # name -> 'public' | 'collab_private'

        # Assessments-summary channel ID (hub-only, created separately from SEEDED_CHANNELS)
        self._assessments_summary_channel_id: str | None = None

    def channel_id_for(self, name: str) -> str | None:
        """The run's Slack channel id for ``name``, or None — ``EngineContext.channel_id_resolver``."""
        return self._channel_id_map.get(name)

    def _resolve_channel_visibility(self, channel_name: str) -> str:
        """Look up the visibility class of a channel by its name.

        Backed by an in-memory map (``self._channel_visibility``) populated
        alongside ``self._channel_id_map`` at rebuild/bootstrap time. Defaults
        to VISIBILITY_PUBLIC when the channel is not tracked (e.g., seeded
        channels before their AgentChannel row is created).
        """
        return self._channel_visibility.get(channel_name, VISIBILITY_PUBLIC)

    def _polled_channel_ids(self) -> dict[str, str]:
        """The channels the poller reads and the fresh-start seed parks cursors in:
        the seeded channels plus collab-private ones (S2-12: one definition)."""
        return {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in constants.SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }

    def _client_for_channel(self, channel_id: str, fallback):
        """Return the Slack client to poll ``channel_id`` with: always ``fallback``.

        Collab_private channels once routed through a member bot here. Nothing
        creates or tracks private channels any more, so every channel is read
        through the caller's round-robin client. Callers still treat ``None``
        as "skip this channel", which this never returns.
        """
        return fallback

    def _ensure_seeded_channels(self) -> None:
        """Create any missing seeded channels and join relevant bots."""
        client = next(iter(self.slack_clients.values()), None)
        if not client or not client.is_connected:
            # Slack off — channels are DB-native with stable local: ids that
            # can't collide with Slack C…/G… ids. See specs/local-db-conversations.md.
            self._channel_id_map = {ch: f"local:{ch}" for ch in constants.SEEDED_CHANNELS}
            # All seeded channels are public.
            self._channel_visibility = {ch: VISIBILITY_PUBLIC for ch in constants.SEEDED_CHANNELS}
            return

        # A *complete* listing, or none. list_channels raises rather than hand back a
        # subset that looks whole, because the subset is what made this method
        # re-create channels the workspace already had: conversations.create answers
        # name_taken, create_channel used to return None, and the channel ended up
        # with no id in _channel_id_map at all — after which every post to it was
        # addressed by name and Slack answered not_in_channel. Demonstrated on a real
        # workspace: #all-copi-test exists as C0BM57CG4HJ and the engine mapped it to
        # None. With an incomplete listing we adopt what we saw and create nothing,
        # since "absent from this listing" no longer means "absent from Slack".
        listing_complete = True
        try:
            existing = client.list_channels()
        except SlackListingIncomplete as exc:
            listing_complete = False
            existing = {ch["name"]: ch["id"] for ch in exc.partial}
            logger.error(
                "Channel discovery is incomplete (%s) — adopting the %d channel(s) "
                "seen and creating none, so a channel Slack already has is not "
                "duplicated", exc.reason, len(existing),
            )

        # Create missing seeded channels
        if listing_complete:
            for ch_name in constants.SEEDED_CHANNELS:
                if ch_name not in existing:
                    logger.info("Creating seeded channel #%s", ch_name)
                    ch_data = client.create_channel(ch_name)
                    if ch_data:
                        existing[ch_name] = ch_data.get("id", "")

        self._channel_id_map = dict(existing)
        # Seeded channels are always 'public'. Agent-created channels (including
        # future collab_private channels) populate their own entries when the
        # agent_channels rows are loaded during engine-state rebuild.
        for ch_name in existing:
            self._channel_visibility.setdefault(ch_name, VISIBILITY_PUBLIC)

        # Join the first (polling) client to ALL seeded channels so it can poll them
        for ch_name, ch_id in existing.items():
            if ch_name in constants.SEEDED_CHANNELS:
                client.join_channel(ch_id)

        # Share channel map across all clients
        for c in self.slack_clients.values():
            c.cache_channel_ids(existing)

    def _ensure_assessments_summary_channel(self) -> None:
        """Create (or adopt) the hub's one-way assessments-summary channel
        and join only the hub to it — never added to SEEDED_CHANNELS, so it
        never enters Phase-1 discovery or the poller's scope (design D11).
        """
        hub = hub_agent(self.agents)
        if hub is None:
            return
        client = self.slack_clients.get(hub.agent_id)
        if not client or not client.is_connected:
            self._assessments_summary_channel_id = f"local:{ASSESSMENTS_SUMMARY_CHANNEL}"
            self._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = self._assessments_summary_channel_id
            return

        try:
            existing = client.list_channels()
        except SlackListingIncomplete:
            # Same caution as _ensure_seeded_channels: an incomplete listing
            # must not risk creating a duplicate channel.
            return

        ch_id = existing.get(ASSESSMENTS_SUMMARY_CHANNEL)
        if ch_id is None:
            ch_data = client.create_channel(ASSESSMENTS_SUMMARY_CHANNEL)
            ch_id = ch_data.get("id") if ch_data else None
        if not ch_id:
            return

        self._assessments_summary_channel_id = ch_id
        self._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = ch_id
        client.join_channel(ch_id)

    async def _persist_seeded_channels(self) -> None:
        """Record seeded channels in agent_channels for this run (idempotent).

        Keeps channel existence in the DB so the workspace is reconstructable
        without Slack (and so the admin UI can count channels). Uses the current
        _channel_id_map (Slack ids when on, local: ids when off).
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select

        from src.agent.channels import record_channel_created
        try:
            async with self.session_factory() as db:
                existing_names = set(
                    (await db.execute(
                        sa_select(AgentChannel.channel_name).where(
                            AgentChannel.simulation_run_id == self.simulation_run_id
                        )
                    )).scalars().all()
                )
                created = 0
                for ch_name in constants.SEEDED_CHANNELS:
                    if ch_name in existing_names:
                        continue
                    await record_channel_created(
                        db,
                        simulation_run_id=self.simulation_run_id,
                        channel_id=self._channel_id_map.get(ch_name, f"local:{ch_name}"),
                        channel_name=ch_name,
                        channel_type="thematic",
                        created_by_agent="system",
                    )
                    created += 1
                if created:
                    await db.commit()
                    logger.info("Persisted %d seeded channels to agent_channels", created)
        except Exception as exc:
            logger.warning("Failed to persist seeded channels: %s", exc)
