"""The verdict store: capture a hub reply's sidecar, persist and supersede assessment rows, the retry queue, drops, and the rehydrate on resume (spec §7.1). Implements VerdictLedgerPort for Headlines."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.agent.agent import Agent
from src.agent.engine.constants import _ROW_LEVEL_DB_ERRORS
from src.agent.engine.context import EngineContext, via
from src.agent.engine.headlines import should_announce
from src.agent.engine.helpers import _HeldVerdict
from src.agent.engine.sidecar import (
    _ASSESSMENT_RE,
    _ASSESSMENT_UNCLOSED_RE,
    _DIMENSION_RATIONALE_CHARS,
    _HEADLINE_SOFT_LIMIT,
    _HUB_BULLET_CHARS,
    _HUB_BULLETS_MAX,
    _HUB_BULLETS_MIN,
    _KEY_POINT_BULLET_CHARS,
    _KEY_POINT_GROUP_BULLETS,
    _KEY_POINTS_MAX,
    _KEY_POINTS_MIN,
    _PITCH_CITATION_RE,
    _PITCH_WORD_LIMIT,
    _PROJECT_SOFT_LIMIT,
    _bounded_str,
    _extract_assessment_json,
    _normalize_gating,
    _reply_opens_with_pause,
    _sidecar_has_valid_json_block,
)
from src.agent.specialists import panel_is_owed
from src.agent.state import ThreadState
from src.agent.thread_guidance import CONCLUDE, phase4_guidance
from src.models import AssessmentDrop, OpportunityAssessment
from src.services.assessment_detail import (
    KEY_POINT_ACCEPTED_KEYS,
    KEY_POINT_COMPANIES,
    KEY_POINT_RISK,
    LEGACY_KEY_POINT_GROUPS,
    RETIRED_KEY_POINT_GROUPS,
    classify_key_point,
    key_point_shape,
    normalize_bullets,
    normalize_dimension_rationales,
    normalize_gating_rationales,
    normalize_key_points,
)
from src.services.assessment_headline import (
    PITCH_DISPLAY_CHARS,
    PROJECT_DISPLAY_CHARS,
    _clip_at_sentence,
)
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.verdict_fields import VERDICT_FIELD, sidecar_column_kwargs

if TYPE_CHECKING:
    from datetime import datetime

    from src.agent.engine.headlines import Headlines
    from src.agent.engine.panel import Panel
    from src.agent.engine.persistence import Persistence

logger = logging.getLogger("src.agent.simulation")


class UpsertResult(NamedTuple):
    """What ``Verdicts.upsert`` did with one verdict (spec §8.1)."""

    outcome: str  # "inserted" | "updated" | "already_applied" | "stale"
    assessment_id: uuid.UUID


_SUPERSEDE_DETAIL = (
    "verdict from message ordinal {old} superseded by the interview's "
    "concluding verdict at ordinal {new}; one interview yields one assessment, "
    "and the later verdict is the better-informed one"
)
_STALE_DETAIL = (
    "queued verdict from message ordinal {new} arrived after the verdict from "
    "ordinal {old} had landed; the newer verdict is kept"
)
#: Columns the in-place UPDATE never writes from the new verdict: the key, the
#: announcement stamps (coalesced so a set stamp is never overwritten) and the
#: three 0054 columns (set explicitly).
_UPSERT_NOT_COPIED = frozenset({
    "id", "simulation_run_id", "thread_id", "summary_posted_at", "summary_claimed_at",
    "verdict_revision", "verdict_write_id", "verdict_ordinal",
})
#: Sidecar item 1's companion (scout_hub >= 1.10.0, migration 0058): one
#: sentence per gate, warned about past this many characters, never clipped.
_GATING_RATIONALE_CHARS = VERDICT_FIELD["gating_rationales"].soft_bound
#: The one key-point group that may carry a `Companies:` bullet (scout_hub >=
#: 1.10.0, prompt item 7).
_KEY_POINT_COMPANIES_GROUP = "lab_background"


def _warn_key_point_group(
    agent_id: str, group_key: str, group: list, expected: int,
) -> None:
    """Warn (never drop) when one current key-point group misses the scout_hub
    1.10.0 shape: ``expected`` main bullets, at most one ``Risk:`` bullet, and at
    most one ``Companies:`` bullet, in ``lab_background`` only (prompt item 7).
    Bullets are told apart by ``classify_key_point``, the classifier the
    assessment pages render with, so a label the page shows as a label (any
    case, spacing or bold around it) is never warned as an unlabelled extra."""
    labels = [classify_key_point(b)[0] for b in group if isinstance(b, str)]
    main = labels.count(None)
    if main < expected:
        logger.warning(
            "[%s] Assessment key_points.%s carries %d main bullet(s) (contract "
            "asks for %d; a Risk: or Companies: bullet does not count as one)",
            agent_id, group_key, main, expected,
        )
    elif main > expected:
        logger.warning(
            "[%s] Assessment key_points.%s carries %d unlabelled extra bullet(s) "
            "(contract asks for %d main bullet; an extra bullet must begin Risk:, "
            "or Companies: in %s)",
            agent_id, group_key, main - expected, expected, _KEY_POINT_COMPANIES_GROUP,
        )
    for label, shown in ((KEY_POINT_RISK, "Risk:"), (KEY_POINT_COMPANIES, "Companies:")):
        if labels.count(label) > 1:
            logger.warning(
                "[%s] Assessment key_points.%s repeats the %s label on %d bullets "
                "(contract allows one)",
                agent_id, group_key, shown, labels.count(label),
            )
    if KEY_POINT_COMPANIES in labels and group_key != _KEY_POINT_COMPANIES_GROUP:
        logger.warning(
            "[%s] Assessment key_points.%s carries a Companies: bullet (contract "
            "allows one only in %s)",
            agent_id, group_key, _KEY_POINT_COMPANIES_GROUP,
        )
    for bullet in group:
        if isinstance(bullet, str) and len(bullet) > _KEY_POINT_BULLET_CHARS:
            logger.warning(
                "[%s] Assessment key_points.%s has a %d-char bullet (contract "
                "asks for at most %d)",
                agent_id, group_key, len(bullet), _KEY_POINT_BULLET_CHARS,
            )


class Verdicts:
    """The verdict store: what each interview concluded, held or queued for a retry.

    Implements ``VerdictLedgerPort`` (the ledger methods at the end), the
    only view ``Headlines`` has of it.
    """

    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    _computed_score_and_band = via("_panel")
    _floor_verifiable = via("_panel")
    _seed_consults_from_db = via("_panel")
    _specialist_floor_gap = via("_panel")
    _recover_rows_individually = via("_persistence")
    _report_flush_failure = via("_persistence")
    flush_before_dependent = via("_persistence")

    OWNED_STATE: tuple[str, ...] = (
        "_assessed_threads", "_pending_assessments", "_landed_write_ids", "_pruned_write_ids",
    )

    def __init__(
        self,
        ctx: EngineContext,
        *,
        persistence: Persistence,
        panel: Panel,
        headlines: Headlines,
    ) -> None:
        self.ctx = ctx
        self._persistence = persistence
        self._panel = panel
        # Write ids this process has landed. The ordinal rule (step 5) orders only
        # this process's own writes: a resume rebuilds message_count from the
        # history, so ordinals from an earlier process are not comparable, and a
        # row an earlier process landed is always superseded, as before 0054.
        self._landed_write_ids: set[uuid.UUID] = set()
        # Write ids whose verdict `_prune_queued_for_thread` already recorded as
        # a drop. A queued verdict whose first commit reached the server but
        # lost its acknowledgement is also the landed row, so the supersede that
        # follows must not record the same raw verdict a second time.
        self._pruned_write_ids: set[uuid.UUID] = set()
        self._headlines = headlines
        # Threads whose interview has already produced a verdict, so a second
        # `<assessment_json>` sidecar on the same thread cannot become a second
        # `opportunity_assessments` row. Run 60c53424 wrote THREE rows for one
        # pearce interview and run 88d81cd8 wrote up to three per thread for
        # five different labs; see `_capture_hub_assessment` for the full
        # mechanism. A thread is recorded once its verdict is HELD — committed,
        # or queued on `_pending_assessments` for a retry that will still land
        # it.
        #
        # The VALUE (see `_HeldVerdict`) is what makes last-write-wins possible:
        # a same-turn re-capture is still refused as a duplicate, but a strictly
        # later reply that concludes or closes the interview supersedes a
        # provisional earlier verdict instead of being turned away by it — the
        # interview's one row is updated in place (`upsert`, keyed by run and
        # thread since migration 0055) so the one-interview-one-assessment
        # invariant still holds.
        #
        # Process-local on purpose. It is the same scope as the duplicates it
        # prevents (every observed one came from a single process). A restart
        # used to leave it empty, because `opportunity_assessments.slack_ts` is
        # the REPLY's ts and the table carried no thread_id of its own; since
        # migration 0036 it does, and `_rehydrate_assessed_threads` rebuilds
        # this map at startup. A row with a NULL `thread_id` (every pre-0036
        # row) still cannot be placed, so a restart mid-interview can still let
        # a second verdict through for one — but `max_thread_messages` closes a
        # thread the turn after it concludes, so there is normally no second
        # concluding turn to come back to.
        self._assessed_threads: dict[str, _HeldVerdict] = {}

        # DB persistence buffer for OpportunityAssessment rows that failed
        # their first write attempt (e.g. a pool-checkout timeout) — queued
        # here by _persist_assessment instead of being dropped, and drained by
        # _flush_pending_assessments on the SAME per-turn cadence as
        # _pending_persist/_llm_log_buffer above (see _run_main_loop and
        # stop()), so the shutdown flush covers the last assessment of a run
        # too. This table is the actual product of the screening pipeline.
        self._pending_assessments: list[dict] = []

    async def _capture_hub_assessment(
        self, agent: Agent, thread: ThreadState, raw_response: str,
        slack_ts: str | None, *, closes_thread: bool,
    ) -> None:
        """Option A relocation: extract the hub's `<assessment_json>` verdict
        sidecar from its own raw Phase-4 CONCLUDE reply and persist it.

        ``closes_thread`` is whether the reply this sidecar rode in on ENDS the
        interview — the same ⏸️ decision ``_check_thread_outcome`` acts on
        moments later, hoisted in ``_reply_to_thread`` and passed down so both
        read one answer (see ``_reply_closes_thread``). Keyword-only and
        REQUIRED, with no default: a silent ``False`` here is exactly the bug
        this argument exists to fix, and every caller genuinely knows the
        answer.

        ``raw_response`` is the full LLM response from BEFORE
        ``_extract_slack_message`` discarded everything outside
        ``<slack_message>`` — the sidecar is written outside that block by
        design (see ``phase4-thread-reply.md``'s "Concluding with an
        Opportunity Assessment" section), so it was never in the text Slack
        actually received. (``_post_message`` also strips it unconditionally
        as a backstop regardless — see ``_strip_assessment_sidecar`` — so the
        sidecar cannot leak to Slack even if a model mistakenly wrote it
        inside the block instead.)

        Mirrors the two outcomes the old Phase-5 ``new_post`` handling of
        this same artifact logged (persisted / present-but-unusable), with
        one deliberate omission: Phase 5 only ever reached that code after
        the model explicitly declared ``post_type: "opportunity_
        assessment"``, so an absent sidecar there was a genuine anomaly
        worth a WARNING every time. Every Phase-4 reply runs through here
        regardless of whether it is the interview's concluding turn, and a
        sidecar is expected on at most 1 of every 12 — logging "no sidecar"
        on every ordinary interview turn would be pure noise, so that case is
        silent here. Only a sidecar tag that IS present but broken is
        anomalous.

        Never raises: a failure to extract or persist a verdict must not cost
        the reply that has already been posted to Slack by the time this
        runs (``_persist_assessment`` already self-guards its own DB write;
        this wraps the extraction step too, for the same reason).
        """
        try:
            verdict = _extract_assessment_json(raw_response)
            if verdict is not None:
                refusal = self._sidecar_refusal(
                    agent.role, thread, closes_thread=closes_thread,
                )
                if refusal is not None:
                    reason, detail = refusal
                    logger.warning(
                        "[%s] Phase 4: REFUSED an <assessment_json> sidecar for "
                        "%s on thread %s — %s. The reply is already in Slack; "
                        "the verdict is recorded as a drop, not stored.",
                        agent.agent_id, thread.other_agent_id or "?",
                        thread.thread_id, detail,
                    )
                    await self._record_assessment_drop(
                        agent.agent_id, reason,
                        subject_agent_id=thread.other_agent_id,
                        thread_id=thread.thread_id,
                        detail=detail,
                        # Keep the verdict itself. A refusal is a decision about
                        # WHERE this verdict belongs, never a licence to destroy
                        # it — see AssessmentDrop.raw_verdict.
                        raw_verdict=verdict,
                    )
                    return
                write_id = uuid.uuid4()
                ordinal = thread.message_count + 1
                # The model is asked for `subject_agent_id` in the sidecar,
                # but unlike Phase 5's standalone post, a Phase-4 CONCLUDE
                # reply always has a real interview thread behind it — the PI
                # being screened is exactly `thread.other_agent_id`. Passed
                # as a fallback (not written into `verdict` itself) so
                # `raw_verdict` stays exactly what the model emitted — see
                # _persist_assessment's docstring.
                held, replacement_id = await self._persist_assessment(
                    agent.agent_id, thread.channel, verdict, slack_ts=slack_ts,
                    subject_agent_id_fallback=thread.other_agent_id,
                    thread=thread, write_id=write_id, ordinal=ordinal,
                )
                if held:
                    terminal = self._verdict_is_terminal(
                        agent.role, thread, closes_thread=closes_thread,
                    )
                    # A queued replacement is never claimed at capture (spec
                    # P0-08, SA6-01). With `replacement_id is None` the verdict is
                    # only on `_pending_assessments`, so a by-thread claim here
                    # would stamp the row still holding the SUPERSEDED verdict
                    # before the new one has landed on it. The close path and the
                    # stop sweep (which runs after the final assessment flush)
                    # post it once the row is written.
                    queued_only = replacement_id is None and bool(
                        self.session_factory and self.simulation_run_id
                    )
                    # Announce once per interview, and only a verdict that ends
                    # it. The ledger carries an earlier announcement forward
                    # because that headline is already public and unretractable:
                    # a second one would describe a row that replaced a row
                    # nobody knew had been replaced. Since a provisional sidecar
                    # is STORED rather than refused, one interview can hold
                    # several in turn, and design D14 says a verdict that is not
                    # final does not post YET.
                    announce = should_announce(
                        trigger="capture",
                        already_announced=self._headlines.is_announced(thread.thread_id),
                        terminal=terminal, queued=queued_only,
                    )
                    self._assessed_threads[thread.thread_id] = _HeldVerdict(
                        ordinal=ordinal,
                        # `final` is CLOSED, not merely concluding — see
                        # `_HeldVerdict`. A CONCLUDE ordinal can repeat.
                        final=closes_thread,
                        slack_ts=slack_ts,
                    )
                    if announce:
                        await self._headlines.announce_captured(
                            agent, thread, verdict, slack_ts,
                        )
                    elif terminal and queued_only and not self._headlines.is_announced(
                        thread.thread_id
                    ):
                        logger.warning(
                            "[%s] The verdict for %s was queued, not committed; its "
                            "#assessments-summary headline waits for the "
                            "interview's close or the shutdown sweep",
                            agent.agent_id, thread.other_agent_id or "?",
                        )
                    elif not terminal:
                        logger.info(
                            "[%s] Provisional verdict stored for %s (message "
                            "ordinal %d); no #assessments-summary headline until "
                            "the interview concludes",
                            agent.agent_id, thread.other_agent_id or "?",
                            thread.message_count + 1,
                        )
                    # §8.1: no retire. The row was updated in place, and the
                    # UPDATE keeps `summary_posted_at`/`summary_claimed_at`, so a
                    # verdict whose interview was already announced stays
                    # announced — the carry-forward of spec §8.2.
            elif _ASSESSMENT_UNCLOSED_RE.search(raw_response or ""):
                await self._record_unusable_sidecar(agent, thread, raw_response)
        except Exception as exc:  # noqa: BLE001 — never lose a posted reply over this
            logger.error(
                "[%s] Failed to extract/persist the assessment sidecar for "
                "thread %s: %s",
                agent.agent_id, thread.thread_id, exc,
            )

    async def _record_unusable_sidecar(
        self, agent: Agent, thread: ThreadState, raw_response: str,
    ) -> None:
        """Log and record the drop for an ``<assessment_json>`` sidecar that is present
        but unusable. Split out of ``_capture_hub_assessment`` (spec §7.7)."""
        # An <assessment_json> opening tag is present but
        # _extract_assessment_json found no usable verdict in it —
        # anomalous regardless of turn type, unlike plain absence.
        if _sidecar_has_valid_json_block(raw_response or ""):
            logger.warning(
                "[%s] Phase 4: concluding reply's <assessment_json> "
                "sidecar parsed as valid JSON but was not an object "
                "— verdict lost",
                agent.agent_id,
            )
            drop_detail = "sidecar parsed as valid JSON but was not an object"
        else:
            logger.warning(
                "[%s] Phase 4: concluding reply's <assessment_json> "
                "sidecar was present but unparseable — verdict lost",
                agent.agent_id,
            )
            drop_detail = (
                "sidecar present but unparseable (commonly a max_tokens "
                "truncation that ate the closing tag)"
            )
        await self._record_assessment_drop(
            agent.agent_id,
            "unparseable_sidecar",
            subject_agent_id=thread.other_agent_id,
            thread_id=thread.thread_id,
            detail=drop_detail,
        )

    def _warn_if_hub_conclude_missing_assessment(
        self, agent: Agent, thread: ThreadState, response_text: str, raw_response: str,
    ) -> str | None:
        """Absent-sidecar detection gap: warn when a hub's structurally-
        concluding reply is neither a decline nor a persistable verdict.

        Returns the ``AssessmentDrop`` reason (``"missing_sidecar"``) when it
        warned, else ``None``. Kept synchronous — its callers include tests that
        invoke it directly — so the async call site does the recording.

        ``_capture_hub_assessment`` already warns when an ``<assessment_json>``
        tag is PRESENT but broken (unparseable, or valid JSON that is not an
        object) — it is deliberately silent when the tag is simply absent,
        because that is the ordinary case on every one of the ~11 non-
        concluding turns of an interview. That silence becomes a real gap at
        the one turn where thread_guidance.py's own CONCLUDE branch tells the
        hub it MUST either decline (⏸️) or close with an inline verdict that
        carries the sidecar (see ``_SCOUT_HUB[CONCLUDE]``) — a reply that does
        neither is a concluding, non-decline verdict that produced nothing
        persistable, and nothing upstream of this ever says so.

        Fires only when ALL THREE hold:
          (a) the thread is at its structural CONCLUDE point. Deliberately
              delegates to ``thread_guidance.phase4_guidance`` — the exact
              function that decided THIS reply's guidance — rather than
              re-deriving the cutoff from ``settings.max_thread_messages``:
              thread_guidance's CONCLUDE branch is a literal 12, not settings-
              derived, so the two can drift apart if ``max_thread_messages``
              is ever configured to anything else. Reading from
              thread_guidance itself keeps this check correct either way.
              Under the default settings this fires for a genuinely real
              reply: a thread with 11 existing messages passes the earlier
              system-enforced-close check (11 < 12), generates a reply at
              ordinal 12 -> CONCLUDE, and is inspected here — see
              ``Agent.build_phase4_prompt``'s ordinal-fix comment for why
              this was NOT true before that fix.
          (b) the posted reply does NOT open with the ⏸️ decline convention
              (see ``_reply_opens_with_pause``).
          (c) no ``<assessment_json>`` tag — well-formed or truncated — is
              present anywhere in the raw response. A present-but-broken tag
              is already covered by ``_capture_hub_assessment``'s own
              warnings above and must not double-warn here.

        Never raises and never persists anything itself — purely an
        observability signal for a case that otherwise leaves no trace at
        all: the reply already posted (this runs after `_post_message`
        succeeded) and no DB row was ever going to exist for it either way.
        """
        # +1: thread.message_count is the prior count; phase4_guidance's
        # contract is the ordinal of the reply just generated — the same
        # correction Agent.build_phase4_prompt applies for this same reply
        # (see that call site's comment for the full rationale).
        message_ordinal = thread.message_count + 1
        thread_phase, _, _ = phase4_guidance(agent.role, message_ordinal)
        if thread_phase != CONCLUDE:
            return None
        if _reply_opens_with_pause(response_text):
            return None
        if _ASSESSMENT_RE.search(raw_response or "") or _ASSESSMENT_UNCLOSED_RE.search(
            raw_response or ""
        ):
            return None
        logger.warning(
            "[%s] Phase 4: thread %s concluded (message_ordinal=%d) with a "
            "non-decline verdict but no persistable <assessment_json> "
            "sidecar was found",
            agent.agent_id, thread.thread_id, message_ordinal,
        )
        return "missing_sidecar"

    async def _persist_assessment(
        self, agent_id: str, channel: str, verdict: dict, slack_ts: str | None = None,
        *, subject_agent_id_fallback: str | None = None, thread: ThreadState | None = None,
        write_id: uuid.UUID | None = None, ordinal: int | None = None,
    ) -> tuple[bool, uuid.UUID | None]:
        """Store a scouting verdict. Best-effort: a failure here must never cost
        the Slack post that already went out.

        Returns ``(held, assessment_id)``. ``held`` is whether the verdict is
        HELD — committed, or queued on ``_pending_assessments`` for a retry
        that will still land it. False means nothing was stored and nothing
        will be: the engine has no database (see ``__init__``).
        ``_capture_hub_assessment`` uses ``held`` to decide whether the
        thread has had its one verdict; a queued row counts, because letting a
        second verdict through while the first is still queued lands BOTH.

        ``assessment_id`` is the row's primary key when the write committed
        (inserted, updated in place, or already applied), and ``None`` when the
        verdict was queued for a retry or arrived stale. Since §8.1 there is one
        row per (run, thread): a later verdict updates it in place (``upsert``),
        so no review row is ever re-pointed or deleted.

        ``write_id`` and ``ordinal`` default to a fresh id and
        ``thread.message_count + 1``; a queued retry carries the same pair, so a
        late flush can never overwrite a newer verdict.

        ``slack_ts`` is the canonical post id ``_post_message`` returned for
        the post/reply the verdict came from (F7) — the row's link back to
        the Slack message it summarises. Optional and defaulted to ``None``
        so every existing direct caller (tests driving this method on a
        stub) keeps working unchanged.

        ``subject_agent_id_fallback``, when given, is AUTHORITATIVE for the
        ``subject_agent_id`` column and the specialist-floor check below — it
        overrides whatever the verdict itself named, because the engine knows
        the interview partner and the model has never been shown that id (see
        the inline note at the override). It is never written into
        ``raw_verdict``, which always stays exactly what the model emitted
        (see that field's own note below). Option A's caller
        (``_capture_hub_assessment``) passes ``thread.other_agent_id`` here:
        unlike Phase 5's old standalone post, a Phase-4 CONCLUDE reply always
        has a real interview thread behind it, so the engine always knows who
        the sidecar is about. The name is kept for its existing callers; it is
        a fallback only in the sense that callers without a thread omit it.

        ``thread``, when given, is passed straight through to
        ``_specialist_floor_gap`` for two things now. First, the fail-open
        decision, read from ``thread.floor_armed`` (latched once per turn, at
        the top of ``_reply_to_thread``, before this same turn's own consult
        calls or any other task's writes can reach it) instead of a live,
        process-global ``_specialist_consults`` read at this later point in
        the same turn — see that method's docstring, and
        ``ThreadState.floor_armed``'s own comment, for why a plain live read
        here is unsafe under concurrency. Second, ``thread.thread_id``, which
        now joins the consult record together with ``subject_agent_id`` — see
        ``_specialist_floor_gap``'s docstring for why a PI-only join let a
        PI's second interview inherit the first one's panel. There is no
        separate ``thread_id`` parameter here because ``thread`` already
        carries it: every caller with a real interview (Option A's
        ``_capture_hub_assessment``, above) passes ``thread`` for exactly this
        reason, and a caller with none — direct callers, all pre-existing
        tests — omits it and joins against the ``None``-keyed slot instead.

        The weighted score and band are computed here from the verdict's own
        dimension scores, never taken from the model's ``weighted_score`` field
        — the model is instructed to leave that at 0 but will sometimes fill in
        a flattering number anyway. ``recommendation`` (the model's judgement,
        which can legitimately be "route-to-incubation" — a value band() can
        never produce) and the computed ``band`` are kept in separate columns
        and neither ever overwrites the other. The verdict exactly as emitted
        is kept verbatim in ``raw_verdict`` regardless of what could be parsed
        out of it (and regardless of ``subject_agent_id_fallback``), so
        nothing is ever lost to — or invented by — a decision made here.
        """
        assessment_kwargs = await self._assessment_row(
            agent_id, channel, verdict, slack_ts,
            subject_agent_id_fallback=subject_agent_id_fallback, thread=thread,
        )
        if assessment_kwargs is None:
            logger.debug(
                "[%s] Skipping assessment persistence — no database configured",
                agent_id,
            )
            return False, None
        if write_id is None:
            write_id = uuid.uuid4()
        if ordinal is None and thread is not None:
            ordinal = thread.message_count + 1
        # Pre-generated so a buffered row carries a FIXED id from the moment it
        # is built: a NULL-thread retry whose previous attempt reached the server
        # surfaces as a PK violation (an `unwritable_row` drop) instead of landing
        # a second row. A threaded verdict is deduplicated by `write_id` instead.
        assessment_kwargs["id"] = uuid.uuid4()
        thread_id = assessment_kwargs.get("thread_id")
        if thread_id is not None:
            await self._prune_queued_for_thread(agent_id, thread_id)
        if not await self.flush_before_dependent():
            # S2-06: never commit a verdict ahead of the reply row it came from.
            self._pending_assessments.append({
                **assessment_kwargs,
                "verdict_write_id": write_id,
                "verdict_ordinal": ordinal,
            })
            logger.warning(
                "[%s] Verdict for thread %s queued until its reply row is flushed",
                agent_id, thread_id,
            )
            return True, None
        try:
            result = await self.upsert(thread, assessment_kwargs, write_id, ordinal)
        except Exception as exc:  # noqa: BLE001 — never lose a posted assessment
            # This row is the actual product of the screening pipeline. Queue it
            # for retry (drained by _flush_pending_assessments on the same
            # cadence as _pending_persist/_llm_log_buffer — see _run_main_loop
            # and stop()) instead of dropping it. Still loud: a pool-checkout
            # timeout on the FIRST attempt is worth an ERROR + traceback even
            # though it is recoverable.
            self._pending_assessments.append({
                **assessment_kwargs,
                "verdict_write_id": write_id,
                "verdict_ordinal": ordinal,
            })
            logger.error(
                "[%s] Failed to persist assessment on first attempt, queued "
                "for retry: %s",
                agent_id, exc, exc_info=True,
            )
            return True, None
        if result.outcome == "stale":
            logger.warning(
                "[%s] Assessment for thread %s arrived stale (ordinal %s) and was "
                "recorded as a drop; the newer stored verdict stands",
                agent_id, thread_id, ordinal,
            )
            return False, None
        logger.info(
            "[%s] Assessment stored: %s -> %s (%s, %s)",
            agent_id, assessment_kwargs.get("subject_agent_id") or "?",
            assessment_kwargs.get("recommendation") or "?",
            assessment_kwargs.get("weighted_score"), assessment_kwargs.get("band"),
        )
        return True, result.assessment_id

    async def _prune_queued_for_thread(self, agent_id: str, thread_id: str) -> None:
        """S2-09: a new verdict for a thread first removes any verdict for the
        same thread still on the retry queue, recording each as a
        ``duplicate_thread_verdict`` drop with its raw verdict — even when the
        new verdict's own write then succeeds (SA3-15). Each pruned write id is
        remembered, so ``_upsert_in`` does not record it again if that verdict
        also landed (a lost acknowledgement)."""
        queued = [r for r in self._pending_assessments if r.get("thread_id") == thread_id]
        if not queued:
            return
        self._pending_assessments[:] = [
            r for r in self._pending_assessments if r.get("thread_id") != thread_id
        ]
        for row in queued:
            await self._record_assessment_drop(
                agent_id, "duplicate_thread_verdict",
                subject_agent_id=row.get("subject_agent_id"),
                thread_id=thread_id,
                detail=(
                    f"queued verdict from message ordinal {row.get('verdict_ordinal')} "
                    "was replaced by a newer verdict before it landed"
                ),
                raw_verdict=row.get("raw_verdict"),
            )
            if row.get("verdict_write_id") is not None:
                self._pruned_write_ids.add(row["verdict_write_id"])

    async def _insert_if_absent(self, db, new_id: uuid.UUID, values: dict) -> uuid.UUID | None:
        """``INSERT ... ON CONFLICT (run, thread) DO NOTHING`` at revision 1;
        the new id, or ``None`` when the interview already has its row."""
        insert_values = {k: v for k, v in values.items() if k != "verdict_revision"}
        return (await db.execute(
            pg_insert(OpportunityAssessment)
            .values(id=new_id, verdict_revision=1, **insert_values)
            .on_conflict_do_nothing(index_elements=["simulation_run_id", "thread_id"])
            .returning(OpportunityAssessment.id)
        )).scalar_one_or_none()

    async def _upsert_in(self, db, row: dict) -> UpsertResult:
        """Steps 1-6 of the §8.1 write protocol inside ``db``'s transaction.

        ``row`` is a fully built ``opportunity_assessments`` row carrying
        ``verdict_write_id`` and ``verdict_ordinal``. Never commits; the caller
        does (step 7).

        1. A NULL-thread verdict is a plain INSERT: NULL threads never conflict.
        2. Otherwise INSERT ... ON CONFLICT (simulation_run_id, thread_id) DO
           NOTHING with revision 1; an inserted row is done.
        3. Otherwise lock the existing row FOR NO KEY UPDATE, which a concurrent
           review or chat-turn FK insert does not wait on (C24). A row deleted
           in the meantime is inserted afresh.
        4. The same write id is a retry whose acknowledgement was lost: done.
        5. A lower incoming ordinal is stale when the landed row is one THIS
           process wrote: keep the newer verdict and record the incoming one as a
           ``duplicate_thread_verdict`` drop (C23), once. A row an earlier process
           landed is always superseded (ordinals restart on a resume).
        6. Otherwise record the current verdict as a drop (unless
           ``_prune_queued_for_thread`` already recorded that write id: one drop
           per raw verdict), then UPDATE every
           column a replacement row would carry, ``created_at = now()``,
           revision + 1, and the new write id and ordinal. The announcement
           stamps are kept (COALESCE), so an announced interview stays
           announced: the carry-forward of spec §8.2.
        """
        values = {k: v for k, v in row.items() if k != "id"}
        new_id = row.get("id") or uuid.uuid4()
        write_id = values.get("verdict_write_id")
        ordinal = values.get("verdict_ordinal")
        if values.get("thread_id") is None:
            db.add(OpportunityAssessment(id=new_id, verdict_revision=1, **{
                k: v for k, v in values.items() if k != "verdict_revision"
            }))
            await db.flush()
            return UpsertResult("inserted", new_id)
        inserted = await self._insert_if_absent(db, new_id, values)
        if inserted is not None:
            self._note_landed(write_id)
            return UpsertResult("inserted", inserted)
        current = (await db.execute(
            select(
                OpportunityAssessment.id,
                OpportunityAssessment.verdict_write_id,
                OpportunityAssessment.verdict_ordinal,
                OpportunityAssessment.raw_verdict,
            )
            .where(
                OpportunityAssessment.simulation_run_id == values["simulation_run_id"],
                OpportunityAssessment.thread_id == values["thread_id"],
            )
            .with_for_update(key_share=True)
        )).one_or_none()
        if current is None:
            # Deleted between the conflict and the lock: the slot is free again.
            inserted = await self._insert_if_absent(db, new_id, values)
            if inserted is None:
                raise RuntimeError(
                    f"assessment row for thread {values['thread_id']} vanished "
                    "and reappeared during upsert"
                )
            self._note_landed(write_id)
            return UpsertResult("inserted", inserted)
        if write_id is not None and current.verdict_write_id == write_id:
            return UpsertResult("already_applied", current.id)
        landed = current.verdict_ordinal or 0
        incoming = ordinal or 0
        drop_common = dict(
            simulation_run_id=values["simulation_run_id"],
            agent_id=values["agent_id"],
            subject_agent_id=values.get("subject_agent_id") or None,
            thread_id=values["thread_id"],
            reason="duplicate_thread_verdict",
        )
        if landed > incoming and current.verdict_write_id in self._landed_write_ids:
            await self._record_stale_once(db, drop_common, values.get("raw_verdict"), incoming, landed)
            return UpsertResult("stale", current.id)
        if current.verdict_write_id not in self._pruned_write_ids:
            db.add(AssessmentDrop(
                **drop_common,
                detail=_SUPERSEDE_DETAIL.format(old=landed, new=incoming),
                raw_verdict=current.raw_verdict,
            ))
            await db.flush()
        set_ = {k: v for k, v in values.items() if k not in _UPSERT_NOT_COPIED}
        for stamp in ("summary_posted_at", "summary_claimed_at"):
            if values.get(stamp) is not None:
                set_[stamp] = func.coalesce(getattr(OpportunityAssessment, stamp), values[stamp])
        await db.execute(
            update(OpportunityAssessment)
            .where(OpportunityAssessment.id == current.id)
            .values(
                **set_,
                created_at=func.now(),
                verdict_revision=func.coalesce(OpportunityAssessment.verdict_revision, 1) + 1,
                verdict_write_id=write_id,
                verdict_ordinal=ordinal,
            )
            .execution_options(synchronize_session=False)
        )
        self._note_landed(write_id)
        return UpsertResult("updated", current.id)

    def _note_landed(self, write_id: uuid.UUID | None) -> None:
        if write_id is not None:
            self._landed_write_ids.add(write_id)

    async def _record_stale_once(
        self, db, drop_common: dict, raw_verdict: dict | None, incoming: int, landed: int,
    ) -> None:
        """Record a stale queued verdict as a drop, unless the same raw verdict is
        already recorded for this interview: a lost-ack retry of a stale write
        must not add a second drop (correction 12)."""
        existing = (await db.execute(
            select(AssessmentDrop.id).where(
                AssessmentDrop.simulation_run_id == drop_common["simulation_run_id"],
                AssessmentDrop.thread_id == drop_common["thread_id"],
                AssessmentDrop.reason == "duplicate_thread_verdict",
                AssessmentDrop.raw_verdict == raw_verdict,
            ).limit(1)
        )).first()
        if existing is None:
            db.add(AssessmentDrop(
                **drop_common,
                detail=_STALE_DETAIL.format(new=incoming, old=landed),
                raw_verdict=raw_verdict,
            ))
        await db.flush()

    async def upsert(
        self, thread: ThreadState | None, verdict: dict, write_id: uuid.UUID,
        ordinal: int | None,
    ) -> UpsertResult:
        """Write one verdict under the §8.1 protocol and commit (step 7).

        ``verdict`` is the fully built row (``_assessment_row``'s kwargs).
        Raises on a database error; the caller queues the row with the same
        ``write_id`` and ``ordinal`` so a late flush can never overwrite a newer
        verdict (step 5)."""
        row = {
            **verdict,
            "thread_id": (thread.thread_id if thread is not None else None) or None,
            "verdict_write_id": write_id,
            "verdict_ordinal": ordinal,
        }
        async with self.session_factory() as db:
            result = await self._upsert_in(db, row)
            await db.commit()
        return result

    async def _assessment_row(
        self, agent_id: str, channel: str, verdict: dict, slack_ts: str | None = None,
        *, subject_agent_id_fallback: str | None = None, thread: ThreadState | None = None,
    ) -> dict | None:
        """Build the ``opportunity_assessments`` row for one verdict — exactly the
        kwargs ``_persist_assessment`` has always written (seed, floor, warnings,
        normalisation). ``None`` when the engine has no database. Split out of
        ``_persist_assessment`` so the §8.1 equality test can compute "today's
        replacement row" for a sequence."""
        subject_view, gap, floor_verifiable = await self._panel_floor_findings(
            agent_id, verdict, subject_agent_id_fallback, thread,
        )

        # The engine can run without a database (see __init__) — in that mode
        # this is a silent no-op, matching every other run-scoped write in
        # this class (e.g. _close_thread above, :2823 in the class).
        if not self.session_factory or not self.simulation_run_id:
            return None

        scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
        computed_score, computed_band = self._computed_score_and_band(verdict)
        # Was a panel OWED here, as judged right now, by the same predicate the
        # floor above just used? Computed once and stored, rather than left for
        # the admin page to re-derive at render time — which is what it used to
        # do, and which silently relabels every older row each time the
        # predicate widens (twice in 2026-08 alone: 12 production rows written
        # by the recommendation-only floor rendered a green "panel verified" box
        # for a floor that never ran). See OpportunityAssessment.panel_owed and
        # src/services/assessment_detail.panel_state.
        #
        # `verdict`, not `subject_view`: the two differ only in
        # `subject_agent_id`, and neither field this reads comes from there.
        panel_owed = panel_is_owed(verdict.get("recommendation"), computed_band)
        gating = _normalize_gating(verdict.get("gating"))
        red_flags = verdict.get("red_flags")
        milestones = verdict.get("suggested_derisking_milestones")
        key_points = verdict.get("key_points")
        # subject_agent_id/funnel_stage/recommendation/confidence are bounded
        # VARCHAR columns (see src/models/opportunity.py); every other field
        # degrades per-field on a bad value (wrong type -> None), but a
        # too-long *string* in one of these four is still the right type and
        # would sail past an isinstance check straight into a DataError at
        # commit — which the outer except then drops the WHOLE row for. Clip
        # instead of dropping: a truncated recommendation is still useful for
        # triage, an absent one is not.
        subject_agent_id = _bounded_str(subject_view.get("subject_agent_id"), 50)
        # The plain sidecar fields (funnel_stage/recommendation/confidence among
        # them, bounded the same way) come from the VERDICT_FIELDS registry.
        sidecar_columns = sidecar_column_kwargs(verdict)
        self._warn_headline_and_pitch_shape(agent_id, verdict)
        normalized_key_points = self._normalized_key_points(agent_id, key_points)
        self._warn_bullet_fields_shape(agent_id, verdict)
        _rationales = self._normalized_dimension_rationales(agent_id, verdict, scores)
        gating_rationales = self._normalized_gating_rationales(agent_id, verdict, gating)
        return self._assessment_kwargs(
            agent_id=agent_id, channel=channel, verdict=verdict, slack_ts=slack_ts,
            thread=thread, subject_agent_id=subject_agent_id,
            sidecar_columns=sidecar_columns, computed_score=computed_score,
            computed_band=computed_band, gating=gating, scores=scores,
            red_flags=red_flags, milestones=milestones,
            normalized_key_points=normalized_key_points, _rationales=_rationales,
            gating_rationales=gating_rationales,
            gap=gap, floor_verifiable=floor_verifiable, panel_owed=panel_owed,
        )

    async def _panel_floor_findings(
        self, agent_id: str, verdict: dict, subject_agent_id_fallback: str | None,
        thread: ThreadState | None,
    ) -> tuple[dict, set[str], bool]:
        """The specialist-floor findings for a verdict: ``(subject_view, gap,
        floor_verifiable)``. Split out of ``_persist_assessment`` (spec §7.7)."""
        # A view with the engine-known subject applied, used ONLY for the
        # subject-derived column and the specialist-floor check — never for
        # `raw_verdict`, which must stay byte-for-byte what the model sent.
        #
        # This OVERRIDES the model's own field rather than only filling a blank
        # one. The phase-4 prompt never shows the hub a PI's `agent_id`: it gets
        # `{other_agent_name}` (bot_name, "WangBot") and `{other_agent_lab}`
        # (pi_name), so it can only guess, and it guesses what it was shown.
        # Consults are recorded under the real `agent_id`, so a guessed
        # "WangBot" made _specialist_floor_gap join against a key that never
        # exists, refuse the verdict, and discard it — after the concluding
        # reply had already gone out, with no later turn to recover it. The
        # caller's value is ground truth (`thread.other_agent_id`: an interview
        # is 1:1 with the hub), so it wins.
        subject_view = verdict
        if subject_agent_id_fallback:
            subject_view = {**verdict, "subject_agent_id": subject_agent_id_fallback}

        # Before the two floor questions below, give the in-memory record a
        # chance to be rehydrated from `specialist_consults` — otherwise a
        # restart mid-interview makes every verdict that follows it permanently
        # UNVERIFIABLE, whatever the panel actually did. Additive, narrow and
        # non-raising; see `_seed_consults_from_db` for why it cannot launder an
        # unconsulted domain into a consulted one.
        await self._seed_consults_from_db(subject_view, thread)

        gap = self._specialist_floor_gap(subject_view, thread=thread)
        # An empty `gap` is two different findings, and the row has to say
        # which: the panel really was complete, or the floor had nothing to
        # check it against (no subject to join on, or a process that has
        # recorded no consult for anyone — the ordinary post-restart state,
        # and production's last exit was a SIGKILL). See `_floor_verifiable`.
        floor_verifiable = self._floor_verifiable(subject_view, thread=thread)
        if gap:
            logger.warning(
                "[%s] Assessment for %s stored with an INCOMPLETE PANEL — "
                "recommendation %r required the %s specialist(s), never "
                "consulted during the interview. The verdict is flagged, not "
                "discarded: this check runs after the concluding reply is "
                "already in Slack, so refusing it left the PI told and "
                "Blackbird holding nothing.",
                agent_id, subject_view.get("subject_agent_id") or "?",
                verdict.get("recommendation"), ", ".join(sorted(gap)),
            )
        return subject_view, gap, floor_verifiable

    def _warn_headline_and_pitch_shape(self, agent_id: str, verdict: dict) -> None:
        """Warn (never drop) when the headline, project label or pitch misses the
        contract's shape. Split out of ``_persist_assessment`` (spec §7.7)."""
        # Shape checks are WARNINGS, never drops (A4). Nothing here can verify
        # that a headline tells the whole story — only that it is the shape the
        # contract asks for. A verdict that misses the shape is still the
        # archive's copy of that verdict.
        if isinstance(verdict.get("headline"), str) and len(
            verdict["headline"]
        ) > _HEADLINE_SOFT_LIMIT:
            logger.warning(
                "[%s] Assessment headline is %d chars (contract asks for <=%d): %s",
                agent_id, len(verdict["headline"]), _HEADLINE_SOFT_LIMIT,
                verdict["headline"][:80],
            )
        if isinstance(verdict.get("company_or_project"), str) and len(
            verdict["company_or_project"]
        ) > _PROJECT_SOFT_LIMIT:
            logger.warning(
                "[%s] Assessment company_or_project is %d chars (contract asks "
                "for <=%d); the #assessments-summary headline clips it at %d: %s",
                agent_id, len(verdict["company_or_project"]), _PROJECT_SOFT_LIMIT,
                PROJECT_DISPLAY_CHARS, verdict["company_or_project"][:80],
            )
        if isinstance(verdict.get("elevator_pitch"), str) and len(
            verdict["elevator_pitch"].split()
        ) > _PITCH_WORD_LIMIT:
            logger.warning(
                "[%s] Assessment elevator_pitch is %d words (contract asks for <=%d)",
                agent_id, len(verdict["elevator_pitch"].split()), _PITCH_WORD_LIMIT,
            )
        # The scout_hub 1.7.0 citation budget's ONLY runtime alarm. Item 8 moved
        # the provenance citation from sentence two to sentence four (element 4
        # since 1.10.0, whose element 1 may be two sentences) and asks that
        # elements 1-4 END within ~550 chars, so the citation completes
        # inside the 600 that `#assessments-summary` publishes. Nothing else
        # checks that: a within-bound pitch whose element 4 ends at 640 stores
        # clean, warns nothing, and publishes a citation-free excerpt to a
        # channel the post cannot be retracted from. Compare `_HEADLINE_SOFT_LIMIT`
        # — the same class of write-path drift alarm, for the same reason.
        # Cheap and total: reuse the real clipper rather than re-deriving the
        # boundary, so this can never disagree with what actually posts.
        _pitch = verdict.get("elevator_pitch")
        if isinstance(_pitch, str) and _pitch:
            # Compare citation SETS, not "is there any citation left". A pitch
            # whose element 1 carries a trial registry URL that survives the
            # cut would otherwise mask the element-4 DOI that did not —
            # which is the only case this alarm exists for.
            _cited = set(_PITCH_CITATION_RE.findall(_pitch))
            if _cited:
                _excerpt = _clip_at_sentence(_pitch, PITCH_DISPLAY_CHARS) or ""
                _lost = _cited - set(_PITCH_CITATION_RE.findall(_excerpt))
                if _lost:
                    logger.warning(
                        "[%s] Assessment elevator_pitch cites %d source(s) the "
                        "#assessments-summary excerpt (first %d chars, cut at a "
                        "sentence boundary) does not carry: %s. Elements 1-4 "
                        "must END within ~550 chars; the published pitch will "
                        "be missing this provenance.",
                        agent_id, len(_lost), PITCH_DISPLAY_CHARS,
                        ", ".join(sorted(_lost))[:200],
                    )

    def _normalized_key_points(self, agent_id: str, key_points: Any) -> Any:
        """Normalize ``key_points`` once and warn on what will be stored. Split out
        of ``_persist_assessment`` (spec §7.7)."""
        # Normalize ONCE, and run the soft-bound checks below against what
        # will actually be STORED (blank bullets stripped), not the raw
        # sidecar — otherwise `["x", "  "]` passes a two-bullet count check
        # and stores one bullet. The raw value is inspected only when
        # normalization rejected it (the field is then stored NULL).
        normalized_key_points = normalize_key_points(key_points)
        checked_key_points = (
            normalized_key_points if normalized_key_points is not None else key_points
        )
        if isinstance(checked_key_points, list) and not (
            _KEY_POINTS_MIN <= len(checked_key_points) <= _KEY_POINTS_MAX
        ):
            logger.warning(
                "[%s] Assessment carries %d key_points (contract asks for %d-%d)",
                agent_id, len(checked_key_points), _KEY_POINTS_MIN, _KEY_POINTS_MAX,
            )
        elif isinstance(key_points, dict):
            # The whole field is about to be DROPPED (stored NULL, kept only in
            # `raw_verdict`) for any dict `normalize_key_points` rejects: an
            # empty object, an unknown group key, a null group value, or a
            # value that is not a list of strings. The reason names which.
            if normalized_key_points is None:
                unknown = sorted(set(key_points) - KEY_POINT_ACCEPTED_KEYS)
                if not key_points:
                    reason = "an empty object"
                elif unknown:
                    reason = f"unknown group key(s) {unknown}"
                elif any(v is None for v in key_points.values()):
                    reason = "a group value is null"
                else:
                    reason = "a group value is not a list of strings"
                logger.warning(
                    "[%s] Assessment key_points was DROPPED (stored NULL; the "
                    "value survives only in raw_verdict): %s. Keys present: %s",
                    agent_id, reason, sorted(key_points),
                )
            shape = key_point_shape(checked_key_points)
            retired_keys = {k for k, _ in RETIRED_KEY_POINT_GROUPS}
            # Retired keys are subtracted here too: `key_questions` is both a
            # legacy (1.3.0-1.7.1) and a retired (1.8.0) group name, and it was
            # never reported as "pre-1.8.0" while 1.8.0 was current — the
            # retired-key warning below is the one that names it now.
            legacy_only = sorted(
                set(checked_key_points)
                & (
                    {k for k, _ in LEGACY_KEY_POINT_GROUPS}
                    - set(_KEY_POINT_GROUP_BULLETS)
                    - retired_keys
                )
            )
            if shape in ("legacy", "mixed"):
                # A stale prompt (scout_hub < 1.8.0) on this image: stored and
                # rendered under the legacy labels, never dropped — but the
                # per-group checks below describe the CURRENT contract, so a
                # legacy object gets this one warning instead of "omits 4 of 6".
                logger.warning(
                    "[%s] Assessment key_points uses pre-1.8.0 group name(s) %s; "
                    "stored and rendered under the legacy labels. Is "
                    "prompts/roles/scout_hub at 1.10.0 on this host?",
                    agent_id, legacy_only,
                )
            if shape in ("current", "mixed"):
                # Its OWN site, not the legacy branch above: a retired key is
                # evidence of neither shape (`key_point_shape`), so a 1.8.0
                # sidecar classifies "current" and never reaches that branch.
                # Milder than the legacy warning (§6.4 of
                # docs/specs/2026-09-28-assessment-chat-entry-and-key-points-design.md)
                # — the group still stores and renders under the label and in the
                # slot 1.8.0 gave it — so it carries no stale-prompt question; that
                # hint stays on the genuinely pre-1.8.0 path above.
                retired = sorted(set(checked_key_points) & retired_keys)
                if retired:
                    logger.warning(
                        "[%s] Assessment key_points carries group(s) %s retired "
                        "as of scout_hub 1.9.0; stored and rendered under the "
                        "1.8.0 label",
                        agent_id, retired,
                    )
                for group_key, expected in _KEY_POINT_GROUP_BULLETS.items():
                    group = checked_key_points.get(group_key)
                    if isinstance(group, list):
                        _warn_key_point_group(agent_id, group_key, group, expected)
                # An ABSENT group is `None` and fails the isinstance above, so
                # the count check cannot see it; a partial object still stores
                # (normalize accepts a subset), so name the omission here.
                absent = [
                    k for k in _KEY_POINT_GROUP_BULLETS if k not in checked_key_points
                ]
                if absent:
                    logger.warning(
                        "[%s] Assessment key_points omits %d of %d groups: %s",
                        agent_id, len(absent), len(_KEY_POINT_GROUP_BULLETS),
                        ", ".join(absent),
                    )
        return normalized_key_points

    def _warn_bullet_fields_shape(self, agent_id: str, verdict: dict) -> None:
        """Warn (never drop) on the strengths/risks/landscape/maturity bullet
        fields. Split out of ``_persist_assessment`` (spec §7.7)."""
        # Sidecar items 11/12 (0049) and 13/14 (0050): strengths, risks,
        # competitive_landscape, evidence_maturity. Same soft-bound policy as
        # key_points above — a shape violation is warned about, never a drop
        # by itself. `normalize_bullets` is the only thing that drops the
        # whole field, and only for a genuine type violation (A20).
        for _field_name in (
            "strengths", "risks", "competitive_landscape", "evidence_maturity",
        ):
            _raw_bullets = verdict.get(_field_name)
            if isinstance(_raw_bullets, list):
                if not (_HUB_BULLETS_MIN <= len(_raw_bullets) <= _HUB_BULLETS_MAX):
                    logger.warning(
                        "[%s] Assessment %s carries %d bullets (contract asks for %d-%d)",
                        agent_id, _field_name, len(_raw_bullets),
                        _HUB_BULLETS_MIN, _HUB_BULLETS_MAX,
                    )
                for _bullet in _raw_bullets:
                    if isinstance(_bullet, str) and len(_bullet) > _HUB_BULLET_CHARS:
                        logger.warning(
                            "[%s] Assessment %s bullet is %d chars (contract asks for <=%d): %s",
                            agent_id, _field_name, len(_bullet), _HUB_BULLET_CHARS,
                            _bullet[:80],
                        )
                if normalize_bullets(_raw_bullets) is None:
                    logger.warning(
                        "[%s] Assessment %s was DROPPED (stored NULL; the value "
                        "survives only in raw_verdict): not a non-empty list of "
                        "non-blank strings",
                        agent_id, _field_name,
                    )
            elif _raw_bullets is not None:
                logger.warning(
                    "[%s] Assessment %s was DROPPED (stored NULL; the value "
                    "survives only in raw_verdict): not a list",
                    agent_id, _field_name,
                )

    def _normalized_dimension_rationales(
        self, agent_id: str, verdict: dict, scores: dict,
    ) -> dict | None:
        """Normalize ``dimension_rationales`` and warn on what is wrong with them.
        Split out of ``_persist_assessment`` (spec §7.7)."""
        # Sidecar item 2's companion (0052). Three warnings, no drops beyond
        # what the normalizer already refuses: an over-long sentence, a
        # malformed map, and a dimension that was SCORED but not explained —
        # the last is the one a reader of the Evidence summary actually feels,
        # because the row renders a score with no reason beside it.
        _raw_rationales = verdict.get("dimension_rationales")
        _rationales = normalize_dimension_rationales(_raw_rationales)
        # A map whose every value is blank (the skeleton left unfilled) holds
        # no reasons rather than a malformed one: it stores NULL like a drop,
        # but the per-dimension warning below names every gap, so it is not
        # also reported as a DROPPED field.
        _all_blank = isinstance(_raw_rationales, dict) and bool(_raw_rationales) and all(
            v is None or (isinstance(v, str) and not v.strip())
            for v in _raw_rationales.values()
        )
        if _raw_rationales is not None and _rationales is None and not _all_blank:
            logger.warning(
                "[%s] Assessment dimension_rationales was DROPPED (stored NULL; "
                "the value survives only in raw_verdict): not a non-empty map of "
                "dimension key to non-blank sentence",
                agent_id,
            )
        # Two keys that normalize to one dimension (`Venture_Potential` and
        # `venture_potential`) keep only the later reason; say so rather than
        # losing one silently.
        if _rationales and isinstance(_raw_rationales, dict):
            _slugs = [
                k.strip().lower() for k, v in _raw_rationales.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            ]
            _collided = sorted({s for s in _slugs if _slugs.count(s) > 1})
            if _collided:
                logger.warning(
                    "[%s] Assessment dimension_rationales has keys that collide "
                    "after lower-casing (%s); only the last reason for each is stored",
                    agent_id, ", ".join(_collided),
                )
        for _key, _text in (_rationales or {}).items():
            if len(_text) > _DIMENSION_RATIONALE_CHARS:
                logger.warning(
                    "[%s] Assessment dimension_rationales.%s is %d chars "
                    "(contract asks for <=%d)",
                    agent_id, _key, len(_text), _DIMENSION_RATIONALE_CHARS,
                )
        # Keys compared after the same `.strip().lower()` the normalizer and
        # the read path apply, so a differently-cased score key is not
        # reported as unexplained when its rationale will in fact render. A
        # score counts as SCORED here exactly when the read path's
        # `_score_value` would render it — a real number, never a bool — so the
        # warning never names a dimension the page does not show.
        _unexplained = sorted(
            k for k, v in scores.items()
            if isinstance(k, str)
            and isinstance(v, (int, float)) and not isinstance(v, bool)
            and k.strip().lower() not in (_rationales or {})
        )
        if _unexplained:
            logger.warning(
                "[%s] Assessment has %d scored dimension(s) with no rationale: %s",
                agent_id, len(_unexplained), ", ".join(_unexplained),
            )
        return _rationales

    def _normalized_gating_rationales(
        self, agent_id: str, verdict: dict, gating: dict | None,
    ) -> dict | None:
        """Normalize ``gating_rationales`` and warn on what is wrong with them,
        as ``_normalized_dimension_rationales`` does for the dimensions.

        ``gating`` is the already-normalized gate map (``_normalize_gating``):
        a gate whose state survived but has no reason is the row the Evidence
        summary shows with only the rubric definition beside it, so it is named.
        Warnings only (A4): ``raw_verdict`` keeps the original either way."""
        _raw = verdict.get("gating_rationales")
        _reasons = normalize_gating_rationales(_raw)
        # A map whose every value is blank (the skeleton left unfilled) holds
        # no reasons rather than a malformed one: it stores NULL, and the
        # per-gate warning below names every gap instead of a DROPPED line.
        _all_blank = isinstance(_raw, dict) and bool(_raw) and all(
            v is None or (isinstance(v, str) and not v.strip()) for v in _raw.values()
        )
        if _raw is not None and _reasons is None and not _all_blank:
            logger.warning(
                "[%s] Assessment gating_rationales was DROPPED (stored NULL; the "
                "value survives only in raw_verdict): not a non-empty map of gate "
                "key to non-blank sentence",
                agent_id,
            )
        if _reasons and isinstance(_raw, dict):
            _slugs = [
                k.strip().lower() for k, v in _raw.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            ]
            _collided = sorted({s for s in _slugs if _slugs.count(s) > 1})
            if _collided:
                logger.warning(
                    "[%s] Assessment gating_rationales has keys that collide after "
                    "lower-casing (%s); only the last reason for each is stored",
                    agent_id, ", ".join(_collided),
                )
        for _key, _text in (_reasons or {}).items():
            if len(_text) > _GATING_RATIONALE_CHARS:
                logger.warning(
                    "[%s] Assessment gating_rationales.%s is %d chars "
                    "(contract asks for <=%d)",
                    agent_id, _key, len(_text), _GATING_RATIONALE_CHARS,
                )
        # Compared after the same `.strip().lower()` the normalizer applies, so
        # a differently-cased gate key is not reported when its reason renders.
        _unexplained = sorted(
            k for k in (gating or {})
            if isinstance(k, str) and k.strip().lower() not in (_reasons or {})
        )
        if _unexplained:
            logger.warning(
                "[%s] Assessment has %d gate(s) with no reason: %s",
                agent_id, len(_unexplained), ", ".join(_unexplained),
            )
        return _reasons

    def _assessment_kwargs(
        self, *, agent_id, channel, verdict, slack_ts, thread, subject_agent_id,
        sidecar_columns, computed_score, computed_band,
        gating, scores, red_flags, milestones, normalized_key_points, _rationales,
        gating_rationales, gap, floor_verifiable, panel_owed,
    ) -> dict:
        """The column values of the assessment row. Split out of
        ``_persist_assessment`` (spec §7.7)."""
        # Built once, up front, so a failed first attempt has a plain dict —
        # not a session-bound ORM instance — ready to hand straight to
        # _pending_assessments for a later retry.
        return dict(
            simulation_run_id=self.simulation_run_id,
            agent_id=agent_id,
            subject_agent_id=subject_agent_id,
            # Bounded like `subject_agent_id`. `channel_name` is
            # String(100) NOT NULL and was passed raw, so an over-long channel
            # name was the one string field left able to DataError the whole row
            # out of existence. `or ""` because the column is NOT NULL and
            # `_bounded_str` answers None for a non-string / empty value: an
            # empty channel name is a degraded row, a missing verdict is a lost
            # one.
            channel_name=_bounded_str(channel, 100) or "",
            slack_ts=slack_ts,
            # The interview this verdict came out of. NULL for a caller with no
            # thread (direct callers, pre-existing tests) — never "", which
            # `WHERE thread_id IS NULL` would not match and which would collide
            # across interviews. It is what lets a restarted process rehydrate
            # `_assessed_threads` (see `_rehydrate_assessed_threads`) instead of
            # treating the interview's own concluding verdict as a first one.
            thread_id=(thread.thread_id if thread is not None else None) or None,
            # The plain sidecar fields: company_or_project, headline,
            # elevator_pitch, score_rationale, strengths, risks,
            # competitive_landscape, evidence_maturity, funnel_stage,
            # recommendation, confidence, rationale and
            # recommended_next_experiment. Their treatments and the reasons for
            # them live in src/services/verdict_fields.py.
            **sidecar_columns,
            key_points=normalized_key_points,
            # Sidecar item 2's companion (0052): the per-dimension reasons.
            # Degrades to None on a wrong shape like its narrative siblings;
            # raw_verdict keeps the original either way.
            dimension_rationales=_rationales,
            # Sidecar item 1's companion (0058): the per-gate reasons, stored and
            # degraded exactly like `dimension_rationales` above. The in-place
            # update in `_upsert_in` copies it like every other built column.
            gating_rationales=gating_rationales,
            weighted_score=computed_score,
            band=computed_band,
            gating=gating,
            scores=scores or None,
            red_flags=red_flags if isinstance(red_flags, list) else None,
            derisking_milestones=(
                milestones if isinstance(milestones, list) else None
            ),
            raw_verdict=verdict,
            # WHICH rubric produced this row. The weights, thresholds and
            # prompt text all come from one document now
            # (prompts/rubric/blackbird-rubric.toml, loaded once per process),
            # so a score is only comparable to another score written under the
            # same version — and the content hash catches an edit that shipped
            # without a version bump. Stamped from the module-level constants:
            # the rubric cannot change under a running process, so these are
            # the same values the startup banner reported.
            rubric_version=RUBRIC_VERSION,
            rubric_content_hash=RUBRIC_CONTENT_HASH,
            panel_incomplete=bool(gap),
            # Three states, one column — see OpportunityAssessment.missing_domains:
            #   [names] a real gap, these domains were owed and never consulted
            #   NULL    NO GAP RECORDED — read this column ALONE and that is all
            #           it says. It covers BOTH "a floor evaluated this verdict
            #           and found nothing owed and unconsulted" and "no floor
            #           ran on it at all", and only `panel_owed` below (written
            #           two lines from here) separates them. This comment used
            #           to call NULL "VERIFIED complete (or none was owed)",
            #           which is the exact reading `panel_owed` exists to end:
            #           12 production rows written by a floor that EXEMPTED them
            #           were later re-read as completed audits, at least five
            #           with a demonstrable gap.
            #   []      the floor could not be checked at all; this row is
            #           UNVERIFIED, and must not be counted as a clean panel
            # `panel_incomplete` stays False for [] on purpose: we have no
            # evidence of a gap, only an inability to look. The distinction is
            # what keeps spec §10's panel-gap surface from reading every
            # post-restart verdict as a vetted one.
            missing_domains=sorted(gap) if gap else (None if floor_verifiable else []),
            # The fourth state `missing_domains` alone cannot express. NULL there
            # means "no gap recorded", which is a verification only if a floor
            # ran at all — and that answer belongs to the moment of the write,
            # not to whatever the predicate says on the day someone opens the
            # page. Deliberately NOT nullable-by-omission: every row this method
            # writes states a real boolean, so NULL in the column means exactly
            # "written before 0036" (or backfilled, or hand-built by a test).
            panel_owed=panel_owed,
            # Ships together with the phase4 prompt instruction to write
            # `rationale`/`recommended_next_experiment` as Markdown: the stamp
            # is what gates rendering, so a new row is always marked, never
            # inferred from content. See OpportunityAssessment.prose_format.
            prose_format="markdown",
        )

    @staticmethod
    def _verdict_is_terminal(
        role: str, thread: ThreadState, *, closes_thread: bool
    ) -> bool:
        """Is this reply the LAST word the interview will get?

        True when the reply closes the thread (⏸️) or is the turn the guidance
        asks to conclude on. Two consequences, and they must agree, which is why
        both callers ask this one function: a terminal verdict marks
        ``_HeldVerdict.final`` so nothing later can re-capture it, and it is the
        only thing that releases the public ``#assessments-summary`` headline.

        NOT an admission test. ``_sidecar_refusal`` deliberately no longer asks
        it — a non-terminal sidecar is stored as provisional rather than
        destroyed. Announcing one, though, is not reversible: the headline goes
        to a Slack channel and cannot be retracted when a later turn supersedes
        the row it described.
        """
        thread_phase, _, _ = phase4_guidance(role, thread.message_count + 1)
        return closes_thread or thread_phase == CONCLUDE

    def _sidecar_refusal(
        self, role: str, thread: ThreadState, *, closes_thread: bool,
    ) -> tuple[str, str] | None:
        """Why this thread may not turn a sidecar into a verdict, or ``None``.

        Returns the ``AssessmentDrop.reason`` and the human-facing detail, so the
        log line and the stored row can never say different things. ``None`` means
        PERSIST — and when the thread already holds a verdict, ``None`` means this
        one SUPERSEDES it. This is the only place either decision is made; the
        caller infers the supersession from ``_assessed_threads`` alone (see
        ``_capture_hub_assessment``), so the two cannot drift apart.

        **A sidecar is now trusted on its own.** Emitting one IS the hub saying
        "this is my verdict", and that is a better signal than either proxy this
        gate used to compute from outside the artifact. The only refusals left
        are re-captures (below); an EARLY verdict is accepted as provisional and
        superseded by any later one.

        Two rounds of evidence forced that. The gate first asked only "is the
        ordinal 12", which destroyed every ``pass`` — delivering one opens with
        ⏸️, and ⏸️ closes the thread 3-8 ms later in this same code path, so no
        ordinal-12 turn ever arrives (run 076e80b6: 4 of 5 refusals were the
        thread's terminal message; only 1 of 62 threads reached 12). The fix
        added ``or closes_thread``, which rescued declines and left positives
        exposed, because the prompts bind the two to MUTUALLY EXCLUSIVE outcomes:
        ``phase4-thread-reply.md``'s Outcome 1 is verdict + sidecar and NO ⏸️,
        Outcome 2 is ⏸️ and "emit no sidecar". So the only sidecar the code
        reliably accepted was one the prompt forbids. Run 8b64a0e0 measured the
        result: the CONCLUDE door was offered **once in 140 hub reply turns**, 0
        of 15 sidecars used it, all 13 stored verdicts came through the ⏸️ door,
        and the two refused at ordinal 10 included the run's highest-scoring
        idea (markham, 3.04, its only ``route-to-incubation``) — refused 6
        minutes before the run's timer ended the interview that was supposedly
        still owed a verdict. A prompt-COMPLIANT model would have stored nothing
        at all that run.

        The "wait for a better-informed turn" instinct behind the old refusal is
        right and is now served by ``upsert``, which landed in the SAME commit as the
        refusal it makes unnecessary. Last write wins,
        so a later turn still overrides an earlier one; the difference is that
        the interview is never left with nothing when that later turn does not
        come.

        ``duplicate_thread_verdict`` — the thread already holds a verdict this
        reply may not replace, in one of two ways:
          * the held verdict is ``final`` (its reply concluded or closed the
            interview): there is no legitimate later turn, so anything after it
            is a re-capture.
          * this reply is not strictly LATER than the held one (same ordinal =
            the same turn captured twice). A true duplicate, refused.
        See ``_assessed_threads`` for why the record is process-local.

        ``closes_thread`` still matters, just not for admission: the caller uses it
        to decide whether the verdict is TERMINAL — which marks the held record
        ``final`` and is the only thing that releases the public
        ``#assessments-summary`` headline. A provisional verdict is stored and
        visible to staff; it is not announced.

        The ordinal arithmetic is ``_warn_if_hub_conclude_missing_assessment``'s
        exactly — prior count plus one, because ``phase4_guidance`` wants the
        ordinal of the reply just generated.
        """
        ordinal = thread.message_count + 1
        held = self._assessed_threads.get(thread.thread_id)
        if held is not None:
            if held.final:
                return (
                    "duplicate_thread_verdict",
                    "this interview already closed with a verdict (message "
                    f"ordinal {held.ordinal}); one interview yields one "
                    "assessment",
                )
            if ordinal <= held.ordinal:
                return (
                    "duplicate_thread_verdict",
                    f"this interview's verdict from message ordinal {held.ordinal} "
                    "is already stored; re-capturing the same turn is not a new "
                    "verdict",
                )
            # Later, and the earlier verdict was provisional: last write wins.
            # A later turn has strictly more of the interview behind it (more
            # answers, more consults), so it is better-informed by construction —
            # which is the same argument the old `premature_sidecar` arm used to
            # justify DESTROYING the early one, applied in the direction that
            # keeps data. ``upsert`` updates the interview's row in place.
            return None
        return None

    async def rehydrate(self, closed_ids: set[str]) -> None:
        """Rebuild ``_assessed_threads`` from this run's stored verdicts.

        The map is process-local, so a restart used to leave the engine blind to
        every verdict it had already written: the interview's own later turn
        looked like a FIRST verdict and landed a second row, and a lab bot
        ⏸️-closing a thread that already held one produced a spurious
        ``closed_before_verdict`` drop. ``opportunity_assessments.thread_id``
        (migration 0036, written by ``_persist_assessment``) is what makes this
        answerable at all — before it the table did not record which interview a
        verdict came from.

        Every field of the restored record is a decision about which way to
        fail, and three of them are not guesses:

        * ``ordinal=0``, deliberately NOT the stored ``verdict_ordinal`` (0054).
          After a resume ``message_count`` is rebuilt from the thread history, so
          a stored ordinal at or above it would make ``_sidecar_refusal`` refuse
          the interview's legitimate LATER verdict
          (``if ordinal <= held.ordinal``); zero costs at most a spurious
          ``duplicate_thread_verdict`` drop if the very same turn is re-captured.
          ``verdict_ordinal`` is still written by ``upsert`` and orders a queued
          write against the landed row there.
        * ``revision`` is READ from ``verdict_revision``; a pre-0054 row reads
          NULL and so 1.
        * the headlines ledger is seeded, not defaulted, from
          ``summary_posted_at`` (migration 0041). It used to be hardcoded
          ``False`` with the reasoning that ``True`` "would suppress the
          headline for a verdict stored provisionally before the restart — a
          silent D12 breach". That was the right call against a schema with no
          answer in it, but it traded one breach for another: a verdict whose
          headline was ALREADY public got a second one, and a headline cannot
          be retracted. The column answers the question directly, so neither
          trade is necessary. A pre-0041 row reads NULL and therefore False,
          which is exactly the old behaviour.
        * ``final`` is DERIVED, not defaulted: closing a thread writes a
          ``ThreadDecision``, so ``thread_id in closed_ids`` is the
          real answer. This must therefore run AFTER ``_rebuild_agent_state``
          populates that set. ``final=True`` as a "conservative" default would be
          the worst of the three: ``_sidecar_refusal`` refuses EVERYTHING on a
          final thread, so the interview's own concluding verdict would be
          refused and only its ``raw_verdict`` would survive, on a drop row.

        Rows with a NULL ``thread_id`` (every row written before 0036, and any
        verdict whose thread could not be identified) are skipped: they cannot be
        placed, and placing them under a guessed thread is how a real verdict
        gets refused. Ordered oldest-first so that a thread carrying several
        historical rows is represented by its NEWEST — the same last-write-wins
        rule ``upsert`` applies.

        Never raises: a failed read costs the de-duplication, not the run.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(
                        OpportunityAssessment.thread_id,
                        OpportunityAssessment.slack_ts,
                        OpportunityAssessment.summary_posted_at,
                        OpportunityAssessment.verdict_revision,
                    )
                    .where(
                        OpportunityAssessment.simulation_run_id == self.simulation_run_id,
                        OpportunityAssessment.thread_id.is_not(None),
                    )
                    .order_by(OpportunityAssessment.created_at)
                )).all()
        except Exception as exc:  # noqa: BLE001 — never fail startup over this
            logger.warning(
                "Failed to rehydrate the assessed-thread record: %s — this "
                "process may store a SECOND verdict for an interview it already "
                "assessed before the restart",
                exc,
            )
            return
        for thread_id, slack_ts, summary_posted_at, verdict_revision in rows:
            self._assessed_threads[thread_id] = _HeldVerdict(
                ordinal=0,
                final=thread_id in closed_ids,
                slack_ts=slack_ts,
                revision=verdict_revision or 1,
            )
            if summary_posted_at is not None:
                self._headlines.mark_announced(thread_id)
        if rows:
            # `len(rows)` — the number of stored verdicts read — NOT
            # `len(self._assessed_threads)`. The two are equal only while every
            # thread holds exactly one row, which is precisely the invariant this
            # mechanism exists because production BROKE (one pearce interview
            # held three), so the count diverged exactly when an operator was
            # reading this line to size the damage.
            logger.info(
                "Rehydrated %d stored verdict(s) across %d interview(s) from "
                "opportunity_assessments — a verdict already stored for one of "
                "these threads will be superseded rather than duplicated",
                len(rows), len(self._assessed_threads),
            )

    async def _record_assessment_drop(
        self,
        agent_id: str,
        reason: str,
        *,
        subject_agent_id: str | None = None,
        thread_id: str | None = None,
        detail: str | None = None,
        raw_verdict: dict | None = None,
    ) -> None:
        """Record that a verdict was lost — generated and discarded, or, for
        ``empty_reply``, never produced at all.

        ``raw_verdict`` is the discarded verdict itself, and passing it whenever
        one exists is the point of the column. Without it a refusal is
        irreversible: run 8b64a0e0 refused markham's sidecar — 3.04, that run's
        highest score and its only ``route-to-incubation`` — and the JSON
        survived only because ``llm_call_logs.response_text`` happens to keep the
        whole response. A gate decision about WHERE a verdict belongs must never
        also be a decision to destroy it.

        Best-effort in exactly the same sense as ``_persist_assessment``: for
        every reason except ``empty_reply`` and ``reply_failed`` the concluding
        reply is already in Slack by the time any of these fire; for those two
        nothing was ever generated or posted. Either way nothing here may raise, and a
        DB-less engine is a silent no-op.

        This exists because every loss path is otherwise invisible — one WARNING
        in a container log — which leaves an empty ``/admin/assessments`` page
        meaning either "nothing screened yet" or "everything screened and every
        verdict thrown away", with no way to tell them apart. See
        ``AssessmentDrop`` for the ``reason`` vocabulary.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                db.add(AssessmentDrop(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    subject_agent_id=(subject_agent_id or None),
                    thread_id=(thread_id or None),
                    reason=reason,
                    detail=detail,
                    raw_verdict=raw_verdict,
                ))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — visibility must never cost a reply
            # This is already the fallback path for a verdict _persist_assessment
            # lost — if the fallback's own write fails there is nothing left to
            # requeue it into (same reasoning as _persist_assessment above), so
            # make the double loss unmistakable: ERROR + a full traceback.
            logger.error(
                "[%s] Failed to record assessment drop (%s): %s — LOST, the "
                "verdict AND its drop record are both gone now",
                agent_id, reason, exc, exc_info=True,
            )

    async def _flush_pending_assessments(self, *, final: bool = False) -> list[str | None]:
        """Retry OpportunityAssessment rows queued by _persist_assessment.

        _persist_assessment attempts an immediate write; a failure there
        (most commonly the pool-checkout timeout that Task 2 of
        docs/plans/2026-08-14-two-lane-concurrent-scheduler.md sized the pool for)
        appends the fully-built row here instead of dropping it. Mirrors
        _flush_persisted's buffer/retry pattern exactly, just against
        _pending_assessments instead of _pending_persist.

        Must be drained by the SAME per-turn cadence as _flush_persisted/
        _flush_llm_logs (see _run_main_loop) so the shutdown flush in
        stop() covers it too — a buffer only retried on the next assessment
        would strand the last one at shutdown, which is exactly the
        durability gap this exists to close. An opportunity_assessments row
        is the actual product of the screening pipeline, so a repeat failure
        here stays at ERROR (louder than _flush_persisted/_flush_llm_logs'
        WARNING on the same kind of retry failure).

        Returns the thread ids of the rows it wrote (``stop()`` sweeps their
        headlines again).
        """
        if not self._pending_assessments:
            return []
        if not self.session_factory or not self.simulation_run_id:
            self._pending_assessments.clear()
            return []
        if not final and not await self.flush_before_dependent():
            return []
        rows = self._pending_assessments
        self._pending_assessments = []
        try:
            async with self.session_factory() as db:
                for row in rows:
                    await self._upsert_in(db, row)
                await db.commit()
            logger.info("Flushed %d queued assessment(s) to DB", len(rows))
            return [r.get("thread_id") for r in rows]
        except Exception as exc:
            # Same re-queue-in-front reasoning as _flush_persisted: new
            # failures may have been appended to _pending_assessments while
            # we were awaiting the (failed) commit, so put this batch back in
            # front to preserve retry order. And, on a ROW-level error only,
            # isolate the poison row first so the verdicts beside it survive —
            # these rows are the actual product of the screening pipeline.
            # `_upsert_in` applies the §8.1 protocol per row, so a stale queued
            # verdict becomes a drop instead of an overwrite.
            requeue = rows
            lost: list = []
            if isinstance(exc, _ROW_LEVEL_DB_ERRORS):
                async def _one(db, row):
                    await self._upsert_in(db, row)

                _written, lost, requeue = await self._recover_rows_individually(
                    rows, _one, what="assessment",
                )
                # A row the database refuses outright is a SCREENING VERDICT
                # discarded, and every other way of losing one writes an
                # `AssessmentDrop`. Sequenced after `_recover_rows_individually`
                # returns (so its session is already closed) rather than inside
                # the per-row loop, which would nest a second checkout inside the
                # recovery session on a pool that may already be under pressure.
                for lost_row, lost_exc in lost:
                    await self._record_unwritable_assessment(lost_row, lost_exc)
            if self._report_flush_failure(
                what="assessment", requeue=requeue, exc=exc, final=final,
                log=logger.error, exc_info=True,
            ):
                self._pending_assessments[0:0] = requeue
            unwritten = [*requeue, *(row for row, _exc in lost)]
            return [
                r.get("thread_id") for r in rows if not any(r is u for u in unwritten)
            ]

    async def _record_unwritable_assessment(
        self, row: dict, exc: BaseException,
    ) -> None:
        """An assessment row the database refused, kept as an ``AssessmentDrop``.

        The per-row recovery in ``_recover_rows_individually`` is a path A3.4
        itself created: before it, a poison row lost its whole batch loudly and
        re-queued; after it, ONE row is dropped. For an
        ``opportunity_assessments`` row that means a screening verdict discarded
        on a single log line — while every other way a verdict fails to land
        (``missing_sidecar``, ``duplicate_thread_verdict``,
        ``closed_before_verdict``, ...) writes a drop row. "Every way an
        assessment can be lost is silent" is the exact defect ``AssessmentDrop``
        exists to end.

        ``reason='unwritable_row'`` — deliberately outside the existing
        vocabulary, because this is the only reason that is not a GATE decision:
        the engine wanted the row and the database refused it. The verdict itself
        rides along in ``raw_verdict``, so the refusal is non-destructive like
        every other.

        Best-effort, and it may never raise into the flush path. It is not
        retried: the row already failed twice (batch, then alone), and the drop
        is the record OF that, not another attempt at it.
        ``_record_assessment_drop`` already opens its own session — which
        matters here, because the recovery session may be in an aborted
        transaction — and already swallows its own failures with an ERROR. The
        wrapper is for everything before that point (a malformed ``row``), so a
        bad row cannot take the surviving verdicts of its batch down with it.

        THE HANDLER MUST NOT TOUCH ``row``. That is the whole failure it is
        handling: an earlier version read ``row.get("thread_id")`` inside the
        ``except`` and so raised WHILE HANDLING a row that had no ``.get`` —
        and because the loop that calls this sits inside
        ``_flush_pending_assessments``'s own ``except``, that escaped the
        flusher entirely, skipping ``_report_flush_failure``, the re-queue, and
        every later lost row's drop. Hence the two locals: bound to ``None``
        BEFORE the ``try``, filled inside it, and read by the handler in place of
        ``row``. Not reachable from production today (``_pending_assessments``
        has one append site and it always appends a dict) — which is exactly why
        the claim above needed a test rather than trust.
        """
        thread_id = None
        slack_ts = None
        try:
            thread_id = row.get("thread_id")
            slack_ts = row.get("slack_ts")
            await self._record_assessment_drop(
                row.get("agent_id") or "unknown",
                "unwritable_row",
                subject_agent_id=row.get("subject_agent_id"),
                thread_id=thread_id,
                detail=(
                    f"the database refused this row: {type(exc).__name__}: {exc} "
                    f"(channel={row.get('channel_name')!r} "
                    f"slack_ts={slack_ts!r})"
                ),
                raw_verdict=row.get("raw_verdict"),
            )
        except Exception as drop_exc:  # noqa: BLE001 — never cost the batch
            logger.error(
                "Failed to record the drop for an un-writable assessment "
                "(thread=%s slack_ts=%s): %s — the verdict AND its drop record "
                "are both gone now",
                thread_id, slack_ts, drop_exc,
                exc_info=True,
            )

    # --- VerdictLedgerPort (spec §7.2 rule 2): what Headlines may know --------

    def patch_pending_summary(self, thread_id: str, posted_at: datetime) -> None:
        """Stamp every still-queued row of ``thread_id`` so a later flush does not
        write NULL over a headline that is already public."""
        for queued in self._pending_assessments:
            if queued.get("thread_id") == thread_id:
                queued["summary_posted_at"] = posted_at

    def assessed_thread_ids(self) -> list[str]:
        """Threads holding a verdict, in insertion order."""
        return list(self._assessed_threads)
