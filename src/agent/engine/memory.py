"""Queued working-memory updates and the memory-synthesis call (spec §7.1)."""

from __future__ import annotations

import asyncio
import logging

from src.agent import claim_check
from src.agent.agent import Agent
from src.agent.authorship_rules import (
    ClaimReason,
    lab_self_names,
    strip_ungrounded_authorship_lines,
)
from src.agent.engine import deps
from src.agent.engine.context import EngineContext, via
from src.agent.engine.helpers import _was_truncated
from src.agent.message_log import is_panel_note
from src.agent.role_capabilities import hub_role_names
from src.models.agent_activity import VISIBILITY_PUBLIC
from src.models.authorship_claim_check import MODE_ENFORCE, OUTCOME_REJECTED

logger = logging.getLogger("src.agent.simulation")


class Memory:
    """The queue of working-memory updates and the drain that applies them."""

    agents = via("ctx")
    message_log = via("ctx")
    session_factory = via("ctx")

    OWNED_STATE: tuple[str, ...] = ("_pending_memory_events", "_memory_drain_lock")

    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx
        # Working-memory events deferred from _close_thread. The close used
        # to run its two _update_agent_memory LLM calls inside the thread
        # lock + BOTH agents' locks + a reply-lane semaphore slot; in the
        # star topology every close shares the hub's agent lock, so closes
        # serialized on two LLM calls each and blocked semaphore slots for
        # the duration (docs/audits/2026-08-21-perf-memory-race, finding 1).
        # Queued here and drained OUTSIDE the dispatch fan-out, one event at
        # a time — sequential draining is what preserves the lost-update
        # guarantee the agent lock used to provide for these calls: no two
        # updates for one agent can interleave, and each reads the memory
        # text its predecessor wrote. Entries are (agent_id, event,
        # visibility, channel_id); agent_id (not the Agent object) because a
        # roster sync can rebuild the object between enqueue and drain.
        self._pending_memory_events: list[tuple[str, str, str, str | None]] = []
        # Guards against two drains running at once (main loop vs stop(), or
        # a future second call site): concurrent drains would pop same-agent
        # events into overlapping LLM calls — exactly the lost update this
        # queue exists to prevent.
        self._memory_drain_lock = asyncio.Lock()

    def enqueue(self, event: tuple[str, str, str, str | None]) -> None:
        """Queue one working-memory update for the next drain (spec §7.2 rule 1).

        ``event`` is ``(agent_id, event, visibility, channel_id)``; ``_close_thread``
        is the producer.
        """
        self._pending_memory_events.append(event)

    def drop_pending(self) -> None:
        """Discard every queued working-memory update (spec §7.2 rule 1); ``stop()``
        calls it after the bounded shutdown drain, having logged what is dropped."""
        self._pending_memory_events.clear()

    async def _drain_memory_events(self, limit: int | None = None) -> int:
        """Run queued working-memory updates, strictly FIFO, one at a time.

        Called from the main loop after the reply-lane dispatch and from
        stop() (bounded). Sequential draining under the drain lock is what
        preserves the lost-update guarantee for same-agent updates: each one
        reads the memory text its predecessor wrote. Agents are resolved by
        id at drain time — the roster can change (or an Agent object be
        rebuilt by _sync_roster_from_db) between enqueue and drain, and a
        stale reference would write memory for an object the engine no
        longer owns. _update_agent_memory never raises, so one bad event
        cannot wedge the queue.
        """
        drained = 0
        async with self._memory_drain_lock:
            while self._pending_memory_events:
                if limit is not None and drained >= limit:
                    break
                agent_id, event, visibility, channel_id = (
                    self._pending_memory_events.pop(0)
                )
                agent = self.agents.get(agent_id)
                if agent is None:
                    logger.info(
                        "[memory] dropping queued memory event for %s — no "
                        "longer on the roster", agent_id,
                    )
                    drained += 1
                    continue
                await self._update_agent_memory(
                    agent, event, visibility, channel_id
                )
                drained += 1
        return drained

    async def _still_on_roster(self, agent_id: str) -> bool:
        """AG-13: is the agent still on the live roster (``active_roster_select``)?
        Checked after the model call, because a deactivation or user deletion can
        land during it. No user-link check: the hub has none and still gets
        memory updates. A failed read keeps today's behaviour (write)."""
        if not self.session_factory:
            return True
        from src.agent.roster_query import active_roster_select

        try:
            async with self.session_factory() as db:
                rows = (await db.execute(active_roster_select())).all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] roster re-check failed (%s); writing anyway", exc)
            return True
        return any(r.agent_id == agent_id for r in rows)

    async def _sweep_authorship_lines(
        self, agent: Agent, text: str, visibility: str, channel_id: str | None,
    ) -> str | None:
        """The memory text to write after the authorship sweep (GATE-13), or None to
        keep the existing memory.

        Lines whose authorship claim the lab's records cannot back are recorded as one
        ``rejected`` row (phase ``memory``) when there are any. In log mode, always the
        hub's, the text is returned unchanged; in enforce mode they are stripped, and a
        result with nothing left keeps the existing memory."""
        try:
            mode = claim_check.effective_mode(
                agent.role, deps.get_settings().authorship_claim_check_mode,
            )
            cleaned, stripped = strip_ungrounded_authorship_lines(
                text,
                claim_check.record_for_agent(agent),
                lab_self_names(
                    agent.agent_id, agent.bot_name,
                    None if agent.role in hub_role_names() else agent.pi_name,
                ),
                speaker_id=agent.agent_id,
                roster=claim_check.roster_entries(self.agents),
            )
        except Exception:
            # Fail open: a fault in the rules or in reading the mode keeps the memory text
            # as written, so a log-mode run behaves exactly as before the sweep existed.
            logger.warning(
                "[%s] Memory authorship sweep failed; memory kept unswept",
                agent.agent_id, exc_info=True,
            )
            return text
        if not stripped:
            return text
        await claim_check.record_check(
            self.session_factory,
            simulation_run_id=self.ctx.simulation_run_id,
            agent_id=agent.agent_id,
            thread_key=claim_check.memory_thread_key(visibility, channel_id),
            phase="memory",
            mode=mode,
            outcome=OUTCOME_REJECTED,
            reasons=[
                ClaimReason(
                    claim_check.MEMORY_REASON_CODE,
                    "memory line not grounded in this lab's publication records: " + line[:300],
                )
                for line in stripped
            ],
            excerpt="\n".join(stripped),
        )
        if mode != MODE_ENFORCE:
            logger.info(
                "[%s] Memory update (log only): %d authorship line(s) would be stripped",
                agent.agent_id, len(stripped),
            )
            return text
        for line in stripped:
            logger.warning(
                "[%s] Memory update: stripped ungrounded authorship line: %s",
                agent.agent_id, line[:160],
            )
        if not cleaned.strip():
            logger.warning(
                "[%s] Memory update: every line was an ungrounded authorship claim — "
                "keeping the existing working memory",
                agent.agent_id,
            )
            return None
        return cleaned

    async def _update_agent_memory(
        self,
        agent: Agent,
        event: str,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> None:
        """Incrementally update an agent's working memory after a significant event.

        Triggered by thread closure, via the _pending_memory_events queue
        (_close_thread enqueues; _drain_memory_events is the only caller).

        visibility/channel_id: controls which memory segment is updated and
            which subset of the message log is used as synthesis context, per
            G2. v1 callers always pass public (the default); the
            thread-closure path will pass the thread's visibility once
            private-channel migration lands.
        """
        try:
            # Gather recent activity for context — filter the message log to
            # entries with matching visibility. Public syntheses never see
            # private-channel messages, and vice-versa. See §G2.
            agent_entries = [
                e for e in self.message_log._entries
                if e.sender_agent_id == agent.agent_id
                and e.visibility == visibility
                # A panel note is the hub's own bookkeeping, not something it
                # said. Left in, it would feed its own consult log back into
                # its working memory — the one place a synthesis could quietly
                # re-derive panel opinion into text every later prompt reads.
                and not is_panel_note(e)
            ]
            messages_text = "\n".join(
                f"[#{e.channel}] {e.content[:200]}"
                for e in agent_entries[-20:]
            ) if agent_entries else "(no recent messages)"

            system_prompt = agent.build_thread_reply_system_prompt(
                visibility=visibility, channel_id=channel_id,
            )
            messages = [
                {
                    "role": "user",
                    "content": f"""Update your working memory. The event that triggered this update:
{event}

Your recent messages for context:
{messages_text}

Your current working memory:
{agent.working_memory or "(empty)"}

Write the complete updated working memory. Incorporate the new event, keep existing
entries that are still relevant, and remove anything outdated. Summarize:
(a) Ideas pitched and their screening status (what the hub asked for, conditions
    it named)
(b) Feedback or directions from your PI (if any)
(c) Current priorities

Keep it concise — under 300 words.""",
                }
            ]

            agent.record_api_call()
            # See `_was_truncated`; same collection idiom as the two sites above.
            stop_reasons: list[str] = []
            response = await deps.generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                # 1800. Was 800, then 1100 on a tokenizer estimate for the
                # Sonnet 5 migration — but measured output on Sonnet 5 is
                # 715-1295 tokens (run 2026-08-19 14:45), so 1100 truncated.
                # Thinking is disabled here, so this is tokenizer growth plus
                # a more verbose model, not thinking sharing the budget.
                #
                # 2600, up from 1800, on 2026-08-21. `call_stats` (migration
                # 0032) makes per-call output measurable, and over run 076e80b6
                # the largest memory update returned 1646 output tokens — 91% of
                # 1800, against the 715-1295 band this ceiling was sized to. A
                # truncated memory write is quiet damage: the only guard below
                # is "empty or blank", so a half-written summary is stored as
                # the working memory and carried into every later turn with
                # nothing in the logs to say the file is short. A ceiling is not
                # a spend — the prompt still asks for under 300 words.
                max_tokens=2600,
                log_meta={"agent_id": agent.agent_id, "phase": "memory"},
                on_retry=agent.record_api_call,
                on_stop_reason=stop_reasons.append,
            )
            if _was_truncated(stop_reasons):
                # REFUSED outright — the strictest of the three answers, because
                # this is the only site with something GOOD already in place. The
                # guard below is "empty or blank", which a half-sentence sails
                # past, so a truncated synthesis replaced the agent's working
                # memory and was then carried into every later prompt with
                # nothing in the logs to say the file had shrunk. Measured in run
                # 8b64a0e0: a complete 1,977-character memory replaced by a
                # 1,437-character one, twice (the file on disk and a
                # `profile_revisions` row). A stale memory is strictly better
                # than a truncated one; the next trigger writes a fresh one.
                logger.warning(
                    "[%s] Memory update: response was TRUNCATED (%s) — keeping "
                    "the existing working memory rather than overwriting it "
                    "with a partial synthesis",
                    agent.agent_id, ", ".join(stop_reasons) or "?",
                )
                return
            if not response or not response.strip():
                logger.warning("[%s] Memory update: empty response", agent.agent_id)
                return
            if not await self._still_on_roster(agent.agent_id):
                logger.info(
                    "[%s] Memory update skipped: the agent left the live roster "
                    "during the update (deactivated or deleted)", agent.agent_id,
                )
                return
            # The single write point feeds both the file and the revision, and every
            # later prompt reads that file, so sweeping here is complete (GATE-13).
            swept = await self._sweep_authorship_lines(agent, response, visibility, channel_id)
            if swept is None:
                return
            response = swept
            agent.update_working_memory_file(
                response, visibility=visibility, channel_id=channel_id,
            )
            logger.info(
                "[%s] Working memory updated (visibility=%s, trigger: %s)",
                agent.agent_id, visibility, event[:60],
            )

            # Record revision
            if self.session_factory:
                try:
                    from sqlalchemy import select as sa_sel

                    from src.models import AgentRegistry
                    from src.services.profile_versioning import create_revision
                    async with self.session_factory() as db:
                        agent_reg = (await db.execute(
                            sa_sel(AgentRegistry)
                            .where(AgentRegistry.agent_id == agent.agent_id)
                        )).scalar_one_or_none()
                        if agent_reg:
                            await create_revision(
                                db,
                                agent_registry_id=agent_reg.id,
                                profile_type="memory",
                                content=response,
                                mechanism="agent",
                                change_summary=event[:200],
                            )
                            await db.commit()
                except Exception as rev_exc:
                    logger.warning("[%s] Profile revision failed: %s", agent.agent_id, rev_exc)
        except Exception as exc:
            logger.error("[%s] Working memory update failed: %s", agent.agent_id, exc)
