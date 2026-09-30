"""The running roster: profiles from disk, the DB roster sync, cohort gates, star-topology validation, lab directories and the outbound tag filter (spec §7.1)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from src.agent.agent import Agent
from src.agent.engine import constants, deps
from src.agent.engine.constants import ROSTER_POLL_INTERVAL
from src.agent.engine.context import EngineContext, RejectionCounts, via
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE
from src.services.cohorts import compute_gates, summarise_gates

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory

logger = logging.getLogger("src.agent.simulation")


class Roster:
    """Who is on the roster and who may talk to whom: profiles, cohort gates, topology."""

    agents = via("ctx")
    message_log = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    slack_clients = via("ctx")
    slack_enabled = via("ctx")
    _channel_visibility = via("_channel_directory")

    OWNED_STATE: tuple[str, ...] = (
        "_bot_name_to_id",
        "_profile_mtimes",
        "_last_roster_poll",
        "_cohort_gate_active",
        "_cohort_preflight_error",
        "_cohort_log_signature",
        "_cohort_tags_stripped",
    )

    def __init__(
        self,
        ctx: EngineContext,
        *,
        agents: list[Agent],
        channel_directory: ChannelDirectory,
        rejection_counts: RejectionCounts,
    ) -> None:
        self.ctx = ctx
        self._channel_directory = channel_directory
        self._rejection_counts = rejection_counts
        # Agent name lookups
        self._bot_name_to_id: dict[str, str] = {
            a.bot_name.lower(): a.agent_id for a in agents
        }

        # Last-seen mtime of each agent's on-disk public profile file, keyed by
        # agent_id. The web editor runs in a separate process and writes
        # profiles/public/{id}.md on a shared volume; this process caches
        # profile content per Agent, so a per-turn mtime check tells us when an
        # external edit happened and the cache must be invalidated. See
        # _sync_profiles_from_disk.
        self._profile_mtimes: dict[str, float] = {}

        # --- Cohort gate bookkeeping (specs/cohort-system-v2.md) -------------
        # True once a recompute has actually applied a gate to at least one agent.
        self._cohort_gate_active: bool = False
        # Set to the preflight refusal reason while isolation is being forced off
        # (§5.3); None when clean. Surfaced on /admin/cohorts.
        self._cohort_preflight_error: str | None = None
        # Last logged (cohorts, memberships, gated, isolated) signature, so the
        # per-resync INFO line fires on change rather than every 30s.
        self._cohort_log_signature: tuple | None = None
        # Per-agent count of outbound @mentions stripped because the target was
        # outside the sender's cohort (§9). Exposed in the admin UI: a high rate
        # means the topology disagrees with what the agents want to do.
        self._cohort_tags_stripped: dict[str, int] = {}

        # Last wall-clock time the AgentRegistry roster was re-synced (live
        # add/remove of agents as their status flips). See _sync_roster_from_db.
        self._last_roster_poll: float = 0.0

    def _sync_profiles_from_disk(self) -> None:
        """Reload any agent whose public profile file changed on disk since last turn.

        The public profile can be edited from the web app, which runs in a
        separate process and writes profiles/public/{id}.md on a shared
        mounted volume. Each Agent caches its profile content in memory.
        Without this check, a web edit would not reach the running simulation
        until a restart.

        Detection is by file mtime: cheap (one stat() call per agent, no DB
        round-trip) and tied to exactly what the agent reads.
        """
        for agent in self.agents.values():
            mtime = 0.0
            for sub in ("public",):
                path = constants.PROFILES_DIR / sub / f"{agent.agent_id}.md"
                try:
                    mtime = max(mtime, path.stat().st_mtime)
                except OSError:
                    continue  # file may not exist yet — agent falls back to default

            prev = self._profile_mtimes.get(agent.agent_id)
            if prev is None:
                # First observation — record the baseline without reloading.
                self._profile_mtimes[agent.agent_id] = mtime
                continue
            if mtime > prev:
                agent.reload_profiles()
                self._profile_mtimes[agent.agent_id] = mtime
                logger.info(
                    "[%s] Reloaded profiles from disk (external edit detected)",
                    agent.agent_id,
                )

    async def _sync_roster_from_db(self) -> None:
        """Re-sync the live agent roster from AgentRegistry (active_roster_select: status=='active', pi_lab linked to a user).

        Adds agents that have just been activated (and have a usable token) and
        removes agents that have been inactivated/suspended — all without a
        process restart. Tokens are read from the DB row (falling back to .env),
        so a freshly provisioned or changed token is picked up on the next tick
        too (a client is rebuilt only when its resolved token differs).

        Mutates self.agents / self.slack_clients IN PLACE — never reassigned —
        in case anything else in the engine has taken a reference to either dict.
        """
        if not self.session_factory:
            return
        now = deps.time.time()
        if now - self._last_roster_poll < ROSTER_POLL_INTERVAL:
            return
        self._last_roster_poll = now

        try:
            from src.agent.roster_query import active_roster_select
            from src.agent.slack_client import AgentSlackClient
            from src.services.slack_tokens import env_token, is_valid_token

            async with self.session_factory() as db:
                rows = (await db.execute(active_roster_select())).all()

            desired = {r.agent_id: r for r in rows}

            # Role-diff for surviving agents (agents present in both current and
            # desired). Must run even when to_add/to_remove are empty, or a role
            # reassignment on a running agent is invisible until the next add/remove.
            role_changed = False
            for aid, agent in self.agents.items():
                r = desired.get(aid)
                if r is not None and getattr(r, "role", "pi_lab") != agent.role:
                    logger.info("[roster] %s role %s -> %s", aid, agent.role, r.role)
                    agent.role = r.role
                    role_changed = True

            # Token-diff for surviving agents. `main.py` admits every active
            # agent to self.agents regardless of token, so an agent provisioned
            # AFTER startup is in neither to_add nor to_remove: the membership
            # diff below early-returns and the client-building loop (which only
            # runs over to_add) never sees it. It then posts DB-only, silently,
            # until the process restarts. Measured 2026-08-06: 48 bots installed
            # mid-run, tokens all in AgentRegistry, and `Connected as` never rose
            # above the 7 that had tokens at boot. Adopt them here, before the
            # early return, so the docstring's promise is actually true.
            if self.slack_enabled:
                for aid in self.agents:
                    r = desired.get(aid)
                    if r is None:
                        continue
                    # PS-6: resolve exactly as clients are built (main.py's
                    # `_token_for`): the DB token when valid, else the env token.
                    # Comparing the RAW column instead would rebuild every
                    # env-token client on every poll.
                    token = (
                        r.slack_bot_token
                        if is_valid_token(r.slack_bot_token)
                        else env_token(aid)
                    )
                    if not is_valid_token(token):
                        continue  # still tokenless — retry on a later tick
                    existing = self.slack_clients.get(aid)
                    if existing is not None and getattr(existing, "bot_token", None) == token:
                        continue
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not await asyncio.to_thread(client.connect):
                        logger.warning(
                            "[roster] Slack connect failed %s %s — will retry",
                            "re-keying" if existing is not None else "adopting", aid,
                        )
                        continue
                    self.slack_clients[aid] = client
                    logger.info(
                        "[roster] %s Slack client for %s",
                        "Rebuilt (token changed)" if existing is not None
                        else "Adopted (token provisioned after startup)", aid,
                    )

            current = set(self.agents)
            to_remove = current - set(desired)
            to_add = set(desired) - current
            if not to_remove and not to_add:
                # Recompute the gate FIRST: _recompute_allowed_sender_ids ends by
                # refreshing the directory (step 4), so after this line the
                # directory already agrees with the gate. The role branch stays
                # because a role change alters the directory's *contents*
                # (pi_name headings) without moving the gate at all.
                await self._recompute_allowed_sender_ids()
                if role_changed:
                    self.refresh_lab_directories()
                return

            # --- Removals: agent no longer active ---------------------------
            for aid in to_remove:
                self.agents.pop(aid, None)
                self.slack_clients.pop(aid, None)  # Web API only — no socket to close
                bot_name = next(
                    (n for n, a in self._bot_name_to_id.items() if a == aid), None
                )
                if bot_name:
                    self._bot_name_to_id.pop(bot_name, None)
                logger.info("[roster] Removed inactive agent %s from live roster", aid)

            # --- Additions: agent newly active ------------------------------
            for aid in to_add:
                r = desired[aid]
                if self.slack_enabled:
                    token = r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(aid)
                    if not is_valid_token(token):
                        logger.info(
                            "[roster] Agent %s is active but has no usable token yet — "
                            "skipping (will retry next sync once a token is set)", aid,
                        )
                        continue
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not await asyncio.to_thread(client.connect):
                        logger.warning("[roster] Slack connect failed for new agent %s — skipping", aid)
                        continue
                else:
                    # Slack off: admit the agent with a no-op transport (never
                    # gate on a token/connection that doesn't apply in DB-only mode).
                    from src.agent.transport import NullTransport
                    client = NullTransport(agent_id=aid)
                agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
                self.agents[aid] = agent
                self.slack_clients[aid] = client
                self._bot_name_to_id[agent.bot_name.lower()] = aid
                logger.info("[roster] Added newly-active agent %s to live roster", aid)

            # Rebuild cross-agent derived structures after any membership change.
            self.message_log.set_bot_name_map(self._bot_name_to_id)

            # Recompute cohort interaction sets after roster changes so newly
            # active agents get their gate populated this tick.
            await self._recompute_allowed_sender_ids()
        except Exception as exc:
            # A transient DB hiccup must never crash the main loop.
            logger.warning("[roster] roster sync failed: %s", exc)

    def _build_lab_directories(self) -> None:
        """Build a condensed publications directory for each agent (excluding their own lab)."""
        lab_pubs: dict[str, list[str]] = {}
        for agent in self.agents.values():
            profile_text = agent.public_profile
            match = re.search(
                r"## Recent Publications\n(.*?)(?=\n## |\Z)",
                profile_text,
                re.DOTALL,
            )
            if match:
                pubs = [
                    line.strip()
                    for line in match.group(1).strip().split("\n")
                    if line.strip().startswith("- ")
                ]
                if pubs:
                    lab_pubs[agent.agent_id] = pubs[:5]

        for agent in self.agents.values():
            allowed = agent.allowed_sender_ids  # None == gate off
            sections = []
            for other_id, pubs in sorted(lab_pubs.items()):
                if other_id == agent.agent_id:
                    continue
                if allowed is not None and other_id not in allowed:
                    continue  # cohort gate: don't prime this agent with a non-mate's work
                other_agent = self.agents[other_id]
                sections.append(f"### {other_agent.pi_name} Lab")
                sections.extend(pubs)
                sections.append("")
            agent._lab_directory = "\n".join(sections) if sections else None

    # Public alias. `_build_lab_directories` is called from several places whose
    # ordering relative to the cohort gate is the whole bug this name documents:
    # it must run AFTER _recompute_allowed_sender_ids, never before.
    def refresh_lab_directories(self) -> None:
        """Rebuild every agent's lab directory against its CURRENT gate."""
        self._build_lab_directories()

    def _infer_agent_id(self, name: str) -> str | None:
        """Try to infer agent_id from a bot name or display name."""
        name_lower = name.lower()
        # Direct lookup
        if name_lower in self._bot_name_to_id:
            return self._bot_name_to_id[name_lower]
        # Partial match
        for bot_name, agent_id in self._bot_name_to_id.items():
            if agent_id in name_lower or bot_name in name_lower:
                return agent_id
        return None

    def _disable_all_gates(self) -> None:
        """Set every agent's gate to None (no filtering). See v2 §5.4."""
        for agent in self.agents.values():
            agent.allowed_sender_ids = None

    def _validate_star_topology(self) -> list[str]:
        """Check the live cohort gates against the hub-and-spoke ("star") design.

        The design (docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md
        §5) is strictly hub-and-spoke: every ``pi_lab`` agent's cohort is
        ``{lab, hub}`` — it may reach the ``scout_hub`` agent and nothing else. Two
        ways a gate can violate that, checked for every ``pi_lab`` agent whose gate
        is not None (``gate is None`` means isolation is off for that agent, which
        is vacuously fine — an ungated agent can always reach the hub):

        (a) the gate contains another ``pi_lab`` agent — labs can reach each other
            directly, which the hub-only design forbids.
        (b) the gate contains no ``scout_hub`` agent — the hub is unreachable, so
            the agent has nowhere to land a pitch.

        Returns one human-readable violation string per broken rule (a lab-to-lab
        pair is reported once, not once per side); empty when the topology is
        star-shaped, including when every gate is None.
        """
        violations: list[str] = []
        reported_pairs: set[frozenset[str]] = set()
        for agent_id, agent in self.agents.items():
            if agent.role != "pi_lab":
                continue
            gate = agent.allowed_sender_ids
            if gate is None:
                continue

            has_hub = False
            for other_id in gate:
                other = self.agents.get(other_id)
                if other is None or other_id == agent_id:
                    continue
                if other.role == "scout_hub":
                    has_hub = True
                elif other.role == "pi_lab":
                    pair = frozenset((agent_id, other_id))
                    if pair not in reported_pairs:
                        reported_pairs.add(pair)
                        violations.append(
                            f"{agent_id} and {other_id} are both pi_lab agents but "
                            "can reach each other directly — labs may only be "
                            "cohorted with the hub"
                        )

            if not has_hub:
                violations.append(
                    f"{agent_id} has no scout_hub agent in its cohort gate — the "
                    "hub is unreachable, so pitch targets are unsatisfiable"
                )

        return violations

    async def _recompute_allowed_sender_ids(self) -> None:
        """Recompute each live agent's cohort-mate set for the interaction gate.

        Called on the roster-sync cadence (ROSTER_POLL_INTERVAL) and once in setup
        before the first turn, so no turn can run with an unset gate while isolation
        is on.

        The decision logic lives in ``src.services.cohorts.compute_gates`` so the
        engine and the admin UI's preview cannot drift — the whole point of v2 is
        that a documented rule and the running code agreed. This method is the I/O
        and side-effect wrapper: read memberships, apply the computed gates, log on
        change, then reconcile in-memory state (§8 grandfathering, §6.1 pruning).

        On a transient DB error the existing gates are left in place: flapping the
        gate open on every blip would be worse than a briefly stale topology.
        """
        settings = deps.get_settings()
        if not settings.cohort_isolation_enabled:
            self._cohort_preflight_error = None
            self._disable_all_gates()
            self.refresh_lab_directories()
            self._cohort_gate_active = False
            self._cohort_log_signature = None
            # Reconcile state even on the disabled path: turning isolation off must
            # clear grandfathered flags, or cohort_topology_snapshot keeps
            # reporting threads the gate no longer affects.
            self._apply_cohort_gate_to_state()
            return

        rows: list[tuple[Any, str]] = []
        cohort_count = 0
        if self.session_factory:
            try:
                from sqlalchemy import func as sa_func
                from sqlalchemy import select as sa_select

                from src.models import Cohort, CohortMembership

                async with self.session_factory() as db:
                    rows = list((await db.execute(
                        sa_select(CohortMembership.cohort_id, CohortMembership.agent_id)
                    )).all())
                    cohort_count = (await db.execute(
                        sa_select(sa_func.count()).select_from(Cohort)
                    )).scalar() or 0
            except Exception as exc:
                logger.warning("[cohort] membership sync failed: %s", exc)
                # The gates themselves are left in place deliberately (flapping
                # open on every blip is worse than a briefly stale topology —
                # see the docstring). But the directory is DERIVED from those
                # gates, so a gate that is correct-but-stale makes a directory
                # rebuilt from it correct-but-stale too — which is strictly
                # better than leaving it absent. Without this, a newly-added
                # agent whose gate isn't reflected in any directory yet gets
                # _lab_directory = None for the rest of this failed tick, and
                # existing agents' directories omit it until the next
                # successful sync.
                self.refresh_lab_directories()
                return

        gates, reason = compute_gates(
            membership_rows=rows,
            agent_ids=list(self.agents),
            isolation_enabled=True,
            policy=settings.cohort_default_policy,
            cohort_count=cohort_count,
            has_db=self.session_factory is not None,
        )

        if reason is not None:
            if self._cohort_preflight_error != reason:
                logger.error("[cohort] isolation forced OFF: %s", reason)
            self._cohort_preflight_error = reason
            self._disable_all_gates()
            self.refresh_lab_directories()
            self._cohort_gate_active = False
            self._apply_cohort_gate_to_state()
            return
        if self._cohort_preflight_error is not None:
            logger.info("[cohort] preflight now clean — isolation active")
            self._cohort_preflight_error = None

        for aid, gate in gates.items():
            agent = self.agents.get(aid)
            if agent is not None:
                agent.allowed_sender_ids = gate

        summary = summarise_gates(gates)
        self._cohort_gate_active = summary["gated"] > 0
        signature = (
            cohort_count, len(rows), summary["gated"], tuple(summary["isolated"]),
        )
        if signature != self._cohort_log_signature:
            logger.info(
                "[cohort] gate: %d cohorts, %d memberships, %d/%d agents gated, "
                "%d isolated%s",
                cohort_count, len(rows), summary["gated"], summary["total"],
                len(summary["isolated"]),
                (" (" + ", ".join(summary["isolated"]) + ")")
                if summary["isolated"] else "",
            )
            if summary["isolated"]:
                logger.warning(
                    "[cohort] uncohorted agents isolated by policy: %s",
                    ", ".join(summary["isolated"]),
                )
            topology_changed = self._cohort_log_signature is not None
            self._cohort_log_signature = signature
        else:
            topology_changed = False

        self._apply_cohort_gate_to_state()
        # The directory is derived from the gate, so it is refreshed on the same
        # cadence. Cheap: it re-reads in-memory profiles, no I/O.
        self.refresh_lab_directories()
        if topology_changed:
            # The topology moved mid-run — snapshot the new one so the run stays
            # attributable to every configuration it actually ran under (v2 §13.1).
            await self._record_topology_snapshot()

        # Star-topology check: log only. The startup call site (start(), right
        # after the FIRST invocation of this method) raises instead — a live run
        # must not crash on an admin's transient cohort edit, but the edit still
        # needs to show up somewhere an operator will see it.
        for violation in self._validate_star_topology():
            logger.error("[cohort] star-topology violation: %s", violation)

    def _apply_cohort_gate_to_state(self) -> None:
        """Reconcile in-memory agent state with the freshly computed gate.

        **Grandfather** active threads whose partner is no longer permitted
        (v2 §8), because the gate is a *read-time* filter and state outlives a
        membership change. They still get Phase 4 replies (via the reply lane —
        an open conversation is entitled to conclude rather than waste the
        calls already spent); the flag is reported in
        ``cohort_topology_snapshot`` and affects no scheduling. This is also
        the path that marks a *resumed* run's threads: the DB rebuild runs
        before the first recompute, so every restart reconstructs its open
        partnerships gate-blind.
        """
        newly_grandfathered = 0
        for agent in self.agents.values():
            allowed = agent.allowed_sender_ids
            if allowed is None:
                # Gate off for this agent: nothing to grandfather, and a partner
                # that becomes permitted again is un-grandfathered.
                for thread in agent.state.active_threads.values():
                    if thread.grandfathered:
                        thread.grandfathered = False
                continue

            for thread in agent.state.active_threads.values():
                other = thread.other_agent_id
                permitted = bool(other) and other in allowed
                if permitted:
                    if thread.grandfathered:
                        logger.info(
                            "[cohort] %s: thread %s with %s is permitted again "
                            "(un-grandfathered)",
                            agent.agent_id, thread.thread_id, other,
                        )
                        thread.grandfathered = False
                    continue
                if self._channel_visibility.get(thread.channel) == VISIBILITY_COLLAB_PRIVATE:
                    # PI-created pairing outranks the gate (v2 §7) — never
                    # grandfather a private-channel collaboration.
                    thread.grandfathered = False
                    continue
                if not thread.grandfathered:
                    thread.grandfathered = True
                    newly_grandfathered += 1
                    logger.info(
                        "[cohort] %s: thread %s with %s grandfathered — partner is "
                        "outside the cohort; it may still conclude",
                        agent.agent_id, thread.thread_id, other,
                    )

        if newly_grandfathered:
            logger.info(
                "[cohort] state reconciled: %d threads grandfathered",
                newly_grandfathered,
            )

    def cohort_topology_snapshot(self) -> dict[str, Any]:
        """Serialise the gate configuration and its observed effects.

        Written to cohort_audit_events at run start and on every mid-run topology
        change, so a finished run stays attributable to every configuration it
        actually ran under (v2 §13.1). Derived from the live in-memory gate rather
        than re-querying, so it records what the engine actually applied — including
        a preflight override.

        Also carries the counters the admin UI cannot otherwise see: they live in
        this process's memory, and the web app is a different process (v2 §9 req. 4 / §13).
        """
        settings = deps.get_settings()
        grandfathered = sorted(
            f"{aid}:{t.thread_id}"
            for aid, a in self.agents.items()
            for t in a.state.active_threads.values()
            if t.grandfathered
        )
        return {
            "cohort_isolation_enabled": settings.cohort_isolation_enabled,
            "cohort_default_policy": settings.cohort_default_policy,
            "gate_active": self._cohort_gate_active,
            "preflight_error": self._cohort_preflight_error,
            "agents": {
                aid: (
                    None if a.allowed_sender_ids is None
                    else sorted(a.allowed_sender_ids)
                )
                for aid, a in sorted(self.agents.items())
            },
            "counters": {
                "tags_stripped": dict(sorted(self._cohort_tags_stripped.items())),
                "post_type_rejections": dict(sorted(self._rejection_counts().items())),
                "grandfathered_threads": grandfathered,
            },
        }

    async def _record_topology_snapshot(self) -> None:
        """Persist a topology snapshot for this run.

        Called once in setup and again whenever the gate signature changes mid-run.
        Never raises: provenance is valuable but not worth failing a run over.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            from src.models import COHORT_ACTION_TOPOLOGY_SNAPSHOT, COHORT_NAME_ALL
            from src.services.cohorts import record_cohort_audit_event

            async with self.session_factory() as db:
                await record_cohort_audit_event(
                    db,
                    action=COHORT_ACTION_TOPOLOGY_SNAPSHOT,
                    cohort_name=COHORT_NAME_ALL,
                    simulation_run_id=self.simulation_run_id,
                    topology=self.cohort_topology_snapshot(),
                    commit=True,
                )
        except Exception as exc:
            logger.warning("[cohort] topology snapshot failed: %s", exc)

    def _strip_disallowed_tags(
        self, message_text: str | None, agent: Agent
    ) -> tuple[str | None, int]:
        """Remove @BotName mentions of non-cohort agents from an outbound message.

        Defense-in-depth for the cohort gate: the receiving agent already filters
        tags from non-cohort senders (Phase 3), but emitting a tag toward an agent
        that will never respond leaves a dangling ask in the channel. No-op when
        the gate is off for this agent (``allowed_sender_ids is None``).

        Applied from ``_post_message``, so it covers **every** outbound path —
        Phase 4 replies, Phase 5 posts, private-channel messages — rather than just
        the one call site Phase 5 used to have.

        Three deliberate behaviours (specs/cohort-system-v2.md §9):

        - The whole mention is removed and the surrounding whitespace normalised.
          Keeping the bare name ("Great point WisemanBot") reads like an addressed
          message that isn't one.
        - An unknown bot name is left alone and logged at WARNING. A name missing
          from ``_bot_name_to_id`` means the roster is lagging, which is an
          operational problem, not a policy decision — fail open, loudly (§5.1).
        - Self-mentions are never stripped.

        Strips are counted per agent and surfaced in the admin UI: a high rate means
        the cohort topology disagrees with what the agents are trying to do.

        Returns ``(cleaned_text, n_stripped_this_call)`` — the second element is
        how many mentions THIS call removed, never inferred from the shared
        ``self._cohort_tags_stripped`` counter below (that counter is engine-wide
        and any concurrent agent's post can bump it between two reads of it, which
        is exactly the bug this per-call return exists to avoid — see Phase 5's
        caller). No-op paths (gate off, empty/None text, nothing matched) always
        report 0.
        """
        allowed = agent.allowed_sender_ids
        if allowed is None or not message_text:
            return message_text, 0

        stripped = 0

        def _repl(m: re.Match[str]) -> str:
            nonlocal stripped
            bot_name = m.group(1)
            target_id = self._bot_name_to_id.get(bot_name.lower())
            if target_id is None:
                logger.warning(
                    "[%s] cohort gate: unknown bot name @%s in outbound text — "
                    "leaving the mention in place (roster may be lagging)",
                    agent.agent_id, bot_name,
                )
                return m.group(0)
            if target_id == agent.agent_id or target_id in allowed:
                return m.group(0)
            stripped += 1
            logger.debug(
                "[%s] cohort gate: stripped cross-cohort mention @%s",
                agent.agent_id, bot_name,
            )
            return ""

        # The pattern swallows any run of spaces/tabs immediately BEFORE the
        # mention, so "Great point @CravattBot, shall we?" collapses cleanly to
        # "Great point, shall we?" without a global reflow. The lookbehind requires
        # the '@' to start a token, the way a real Slack mention does — without it,
        # "a@subot.example" or a URL path ending in a bot name would be mangled, and
        # this strip now runs on EVERY outbound message.
        cleaned = re.sub(r"[ \t]*(?<![\w./@-])@(\w+[Bb]ot)\b", _repl, message_text)
        if not stripped:
            return message_text, 0

        self._cohort_tags_stripped[agent.agent_id] = (
            self._cohort_tags_stripped.get(agent.agent_id, 0) + stripped
        )
        # Targeted tidy-up only. Deliberately NOT a global whitespace normalisation:
        # stripping leading indentation would mangle the code blocks and bullet lists
        # agents put in messages. Collapse interior runs only after a non-space, and
        # trim end-of-line space; never touch line-leading whitespace.
        cleaned = re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"(?m)[ \t]+$", "", cleaned)
        cleaned = cleaned.lstrip(" \t") if cleaned[:1] in (" ", "\t") else cleaned
        return cleaned, stripped
