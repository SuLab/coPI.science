"""The specialist panel: consult records, the in-thread panel note, the consult seed from the DB and the specialist floor (spec §7.1)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.agent.engine import deps
from src.agent.engine.context import EngineContext, via
from src.agent.message_log import PHASE_PANEL_NOTE
from src.agent.specialists import (
    clip_question,
    clip_rate_warning,
    format_panel_note,
    panel_is_owed,
    required_domains_for,
)
from src.agent.state import ThreadState
from src.models import SpecialistConsult
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score

if TYPE_CHECKING:
    from src.agent.engine.slack_io import SlackIO

logger = logging.getLogger("src.agent.simulation")


class Panel:
    """Specialist consults recorded per interview and the floor check over them."""

    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    _post_message = via("_slack_io")

    OWNED_STATE: tuple[str, ...] = (
        "_specialist_consults",
        "_consult_signal_counts",
        "_consult_signal_counts_by_domain",
        "_panel_notes_posted",
        "_panel_notes_clipped",
        "_panel_note_clip_warned",
        "_consults_seeded",
    )

    def __init__(self, ctx: EngineContext, *, slack_io: SlackIO) -> None:
        self.ctx = ctx
        self._slack_io = slack_io
        # (pi_agent_id, thread_id) -> the specialist domains consulted during
        # that interview. Keyed per INTERVIEW, not per PI: a PI's second
        # interview must convene its own panel rather than inherit the first
        # one's. `huganir` was assessed 4 times in run 1787010946 and every
        # assessment after the first rode on the first interview's consults.
        # `thread_id` is None for direct callers that have no interview.
        # In-memory on purpose: it is read by _persist_assessment one LLM call
        # later, in the SAME process. A restart clears it, and the floor then
        # fails OPEN for threads that predate the restart — see
        # _persist_assessment.
        #
        # Why fail open, now that a gap no longer costs the verdict: flagging
        # instead would mark every thread that survived a restart as
        # panel_incomplete, including the ones whose panel genuinely WAS
        # convened before the restart cleared this map. That is a false
        # accusation on a real number. What failing open costs is subtler and
        # is what `_floor_verifiable` exists to stop: an unverifiable verdict
        # used to be stored as `panel_incomplete=False, missing_domains=NULL`,
        # indistinguishable from a verified-complete panel, so every
        # post-restart verdict silently inflated the clean-panel count. It is
        # now recorded as the third state, `missing_domains=[]` — unverified.
        self._specialist_consults: dict[tuple[str, str | None], set[str]] = {}

        # (subject, thread_id) keys whose specialist_consults rows this process
        # has already merged into the map (S2-03): the seed runs once per key.
        self._consults_seeded: set[tuple[str, str | None]] = set()

        # verdict_signal -> count, for the whole run. The panel returned caution
        # or blocking on 142/142 consults in run 1787010946 and never once
        # cleared anything; a signal with no variance carries no information,
        # and it took an audit to notice. Tallied so the run says so itself.
        self._consult_signal_counts: dict[str, int] = {}

        # signal counts per DOMAIN, for domain_flatness_warning. The run-level
        # tally above cannot see a single stuck domain.
        self._consult_signal_counts_by_domain: dict[str, dict[str, int]] = {}

        # Panel-note clipping drift (see specialists.clip_rate_warning): every
        # successful consult that posts a note counts toward the denominator,
        # and every one whose question got clipped to PANEL_NOTE_QUESTION_CHARS
        # counts toward the numerator. `_panel_note_clip_warned` makes the log
        # line fire once per run/process rather than once per note once the
        # threshold is crossed. The latch is deliberately conservative: one
        # crossing means the calibration was exceeded on a real sample and is
        # worth one look. The rate DOES un-cross back below the floor mid-run
        # (observed: 3 clipped of 22 latches, then 100 unclipped notes later
        # sitting at 2.5%) — but re-warning or un-warning as it oscillates
        # would turn a drift signal into a ticker, so the latch stays latched.
        # The logged tally is the FIRST crossing's counts, not the run's
        # final rate.
        self._panel_notes_posted: int = 0
        self._panel_notes_clipped: int = 0
        self._panel_note_clip_warned: bool = False

    async def _record_specialist_consult(
        self,
        agent_id: str,
        *,
        subject_agent_id: str | None,
        thread_id: str | None,
        channel_name: str | None,
        domain: str,
        question: str,
        context_excerpt: str | None,
        verdict_signal: str,
        confidence: str,
        concerns: list | None,
        questions_to_ask: list | None,
        raw_opinion: str,
        truncated: bool | None = None,
        # `read_state` is stored as of 0038. Defaults to None so a caller
        # written before this parameter existed keeps recording "not stated"
        # rather than asserting a read that was never checked.
        read_state: str | None = None,
        # The specialist contract's positive-evidence field, produced by the
        # `on_consult_record` call in `tools.py` since the personas gained an
        # `established` key. Still `None`-defaulted: a persona reply that omits
        # the key, and every reply written before the key existed, parses to an
        # empty tuple, and rows from before this column stay NULL. A NULL here
        # therefore means "never asked", not "the specialist established
        # nothing" — the same distinction the column's own comment draws.
        established: list | None = None,
    ) -> None:
        """Write one successful consult to ``specialist_consults``.

        Best-effort in exactly the same sense as ``_record_assessment_drop``:
        never raises, a DB-less engine is a silent no-op, and a failure is an
        ERROR with a traceback and nothing else. The consult itself has already
        happened and has already been credited to the floor in memory
        (``_note_consult``, fired first by ``_execute_consult_specialist``) —
        losing this row costs visibility and post-restart verifiability, never
        the opinion the hub is about to act on.

        Called from the ``on_consult_record`` closure in ``_reply_to_thread``,
        which is fired on a WIDER path than ``on_consult``: a refused domain, a
        missing persona file, a failed call or an empty reply all write nothing,
        but a reply the API cut off mid-sentence DOES write a row (it is the only
        evidence the attempt happened) while not counting toward the floor. So
        "a row here means the domain counts as consulted" holds only for
        ``truncated`` in ``(False, None)`` — the qualification
        ``src/models/specialist_consult.py`` states and the one a reader of these
        rows as evidence of a convened panel (``_seed_consults_from_db``) has to
        apply.

        ``truncated`` defaults to ``None`` — "not stated" — so a caller written
        before the column existed keeps its old meaning rather than asserting
        completeness it never checked. ``src/agent/tools.py`` always sends a
        real boolean.

        Awaited inline by the tool call rather than dispatched as a background
        task: an orphaned task would outlive the turn, and the engine's
        shutdown path flushes its own buffers only — a `docker stop` landing
        between the consult and the write would lose the row it was created to
        keep.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                db.add(SpecialistConsult(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    subject_agent_id=(subject_agent_id or None),
                    thread_id=(thread_id or None),
                    channel_name=(channel_name or None),
                    domain=domain,
                    question=question,
                    context_excerpt=context_excerpt,
                    verdict_signal=verdict_signal,
                    confidence=confidence,
                    concerns=concerns,
                    questions_to_ask=questions_to_ask,
                    raw_opinion=raw_opinion,
                    truncated=truncated,
                    read_state=read_state,
                    # `[]` and NULL are different answers here, exactly as
                    # they are for `concerns` above: NULL is "never asked",
                    # `[]` is "asked, nothing came back". Collapsing `[]` to
                    # NULL was harmless while no call site produced the field,
                    # but `tools.py` now always sends a real list, so
                    # collapsing it would relabel every zero-positive opinion
                    # as unasked. `[]` is NOT "named nothing" specifically:
                    # `_str_tuple` yields the empty tuple for a missing key
                    # too, so it also covers a persona that ignored the key.
                    # That ambiguity is accepted; conflating it with NULL is
                    # not.
                    established=None if established is None else list(established),
                    rubric_version=RUBRIC_VERSION,
                    rubric_content_hash=RUBRIC_CONTENT_HASH,
                ))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — a record must not cost the opinion
            logger.error(
                "[%s] Failed to record the %s consult for %r (thread %s): %s — "
                "the opinion still stands and still counts for the floor "
                "in-process, but this run's panel is no longer reconstructable "
                "after a restart",
                agent_id, domain, subject_agent_id or "?", thread_id or "?", exc,
                exc_info=True,
            )

    async def _post_panel_note(
        self,
        agent_id: str,
        *,
        channel: str | None,
        thread_ts: str | None,
        domain: str,
        question: str,
        verdict_signal: str,
        truncated: bool | None = None,
        read_state: str | None = None,
        **_withheld,
    ) -> None:
        """Post the one-line, signal-level trace of a successful consult into
        the interview thread.

        Why at all: the evaluation panel was previously invisible in Slack. A
        human watching an interview saw the hub go quiet for 30-40 seconds per
        consult and then produce a verdict shaped by opinions nobody in the
        workspace could see. The note makes the panel legible AT THE MOMENT it
        is engaged — posted from inside the turn's tool rounds, so it lands
        before the hub's eventual reply and the thread reads in the order things
        actually happened.

        Why so thin: an interview thread is visible to every lab in the
        workspace. A specialist's opinion paraphrases the PI's confidential
        statements back at them and quotes Blackbird's internal rubric, so
        ``concerns``, ``questions_to_ask``, ``confidence`` and the opinion body
        are NOT published. ``**_withheld`` is where they land — named for what
        it does, and load-bearing in two directions: it lets this be called
        with the same ``**fields`` the durable writer takes (one closure, one
        contract), and it means a field added to that contract later is
        withheld by DEFAULT rather than leaking the first time someone forgets.
        ``format_panel_note`` then takes only the three publishable values, so
        there is no parameter through which the rest could reach Slack. That
        three-argument signature IS the enforcement and is deliberately not
        widened.

        ``truncated`` and ``read_state`` are the two fields pulled back out of
        ``**_withheld``, and for the opposite reason to the rest: they are not
        withheld from the note, they CANCEL it. ``truncated`` was the original
        special case — a consult the API cut off mid-sentence parsed to
        nothing, so ``verdict_signal`` is the schema's DEFAULT ``gap`` and
        no specialist ever said it — and this note goes into the PI's own
        interview thread, which every lab in the workspace can read. Publishing
        a parse failure as ``gap`` states a panel opinion that does not
        exist, and ``src/agent/tools.py`` has already refused to credit that
        domain to the floor for exactly this reason; the note must agree with
        the floor. It was absorbed silently by ``**_withheld`` until 2026-08-22,
        which is why it is spelled out as a parameter rather than read out of
        the catch-all: a named parameter is visible in the signature, a dict key
        is not.

        ``read_state`` (``src/agent/specialists.py::read_state_for``)
        generalises that same reasoning to a reply that arrived COMPLETE and
        simply failed to parse — not truncated, but just as unread: ``gap``
        there is also a parse default, not something a specialist said. Any
        ``read_state`` other than ``"parsed"`` cancels the note the same way
        ``truncated`` does. ``read_state=None`` is treated as "post it" rather
        than "unread": ``None`` means a caller written before this parameter
        existed, and failing closed on that would silently stop every note the
        moment a call site was missed rather than updated. ``truncated`` stays
        in the signature alongside it (not folded into ``read_state`` and
        removed) so a caller that supplies only one of the two still fails
        closed.

        The DURABLE row is still written either way — it is the only evidence
        the attempt happened, and it now carries ``truncated=True`` so the floor
        keeps refusing it across a restart. Only the workspace-visible claim is
        skipped.

        Best-effort, in exactly the sense ``_record_specialist_consult`` is:
        never raises, so it cannot cost the consult, the turn or the reply. It
        runs SECOND, after the durable record — if only one of the two can
        happen it must be the artifact a verdict is audited against, not the
        courtesy note.

        `phase=PHASE_PANEL_NOTE` is the whole reason no prompt file had to
        change: the row exists, it is in the thread, and every agent-facing
        read of the message log skips it (see src/agent/message_log.py). The
        flag is read HERE rather than cached at startup so an operator can
        disable notes with a `.env` edit + container recreate and no rebuild.
        """
        if not channel:
            # No channel, nowhere to post. A consult made outside a thread
            # (a direct tool call, a test) has no interview to annotate.
            return
        # CANCELLED for any opinion we did not actually read. `truncated` was
        # the original special case and its reasoning was right — "no
        # specialist ever said it" — but it covered only an API cut-off. A
        # reply that arrived COMPLETE and failed to parse is not truncated,
        # and posted a workspace-visible "gap" for a verdict `parse_opinion`
        # had defaulted. `read_state` is the general predicate; `truncated` is
        # kept beside it so a caller that supplies only one still fails closed.
        if truncated or (read_state is not None and read_state != "parsed"):
            # See the docstring: the signal on an unread opinion is a parse
            # default, not something a specialist said. Logged rather than
            # silent — a note that does not appear is otherwise
            # indistinguishable from `panel_notes_in_thread=false`.
            logger.info(
                "[%s] Panel note skipped for the %s consult (thread %s): "
                "truncated=%r, read_state=%r, so its signal is a parse "
                "default and not something a specialist said. The durable "
                "row still stands and the domain still does not count "
                "toward the floor.",
                agent_id, domain, thread_ts or "?", truncated, read_state,
            )
            return
        try:
            if not deps.get_settings().panel_notes_in_thread:
                return
            posted = await self._post_message(
                agent_id,
                channel,
                format_panel_note(
                    domain=domain,
                    verdict_signal=verdict_signal,
                    question=question,
                ),
                thread_ts=thread_ts,
                phase=PHASE_PANEL_NOTE,
            )
            if posted:
                # Definitional consistency with what was actually posted: the
                # note's question is exactly what `clip_question` returns, so
                # asking whether IT changed the text (rather than re-deriving
                # a length test here) can never drift from what shipped.
                was_clipped = clip_question(question) != (question or "").strip()
                self._panel_notes_posted += 1
                if was_clipped:
                    self._panel_notes_clipped += 1
                if not self._panel_note_clip_warned:
                    alarm = clip_rate_warning(
                        self._panel_notes_clipped, self._panel_notes_posted,
                    )
                    if alarm:
                        logger.warning("%s", alarm)
                        self._panel_note_clip_warned = True
        except Exception as exc:  # noqa: BLE001 — a note must not cost the opinion
            logger.error(
                "[%s] Failed to post the %s panel note to #%s (thread %s): %s — "
                "the consult itself stands, is recorded, and still counts for "
                "the floor; only the in-thread trace of it is missing",
                agent_id, domain, channel, thread_ts or "?", exc, exc_info=True,
            )

    async def _seed_consults_from_db(
        self, verdict: dict, thread: ThreadState | None,
    ) -> None:
        """Merge this interview's ``specialist_consults`` rows into the in-memory
        consult record, once per interview per process.

        The floor's in-memory map dies with the process, so before this every
        verdict written after a restart was UNVERIFIABLE — stored with
        ``missing_domains=[]`` no matter how thorough the panel had been (see
        ``_floor_verifiable``). Production's normal exit is a SIGKILL, so that
        was the ordinary case, not a corner one. The table now outlives the
        process, so the record can be read back.

        Deliberately ADDITIVE and narrow:

        * Once per (subject, thread) per process, whatever memory holds
          (S2-03): a restart mid-interview leaves memory holding only the
          consults made since, and ignoring the table then would undercount the
          panel. The merge is additive; on the normal path memory already holds
          every committed row (it is written on the same success path), so
          nothing is added and ``floor_armed`` is not touched — the floor is
          armed here only when the table contributed a domain memory lacked,
          which is exactly the restart case. A failed SELECT returns before the
          key is recorded, so the next verdict retries.
        * Only for a verdict that owes a panel at all, asked through
          ``panel_is_owed`` — the SAME question ``_specialist_floor_gap`` asks,
          on the same two inputs (the model's recommendation and the COMPUTED
          band). This used to test ``recommendation not in
          _PANEL_REQUIRED_FOR``, the recommendation-only rule the floor
          abandoned, so it skipped rehydration for exactly the verdicts the new
          floor holds to the panel: a verdict written ``pass`` that scores into
          the ``conditional`` band is owed a panel, and the seed refused to look
          for its consults. Production stamped such a verdict
          ``panel_incomplete=true`` naming four domains, THREE of which were
          recorded as consulted on that very thread.
        * Keyed on ``(run, subject, thread)``, the same triple the in-memory map
          uses. A different run's rows, or the same PI's OTHER interview, must
          not satisfy this interview's panel — the exact hole
          ``_specialist_floor_gap``'s docstring records.
        * TRUNCATED consults are excluded. A row exists for every consult that
          produced text, including one the API cut off mid-sentence — that row is
          the only evidence the attempt happened, and ``src/agent/tools.py``
          deliberately writes it while NOT crediting the domain in memory. Left
          in this SELECT it would undo that refusal on the next restart, turning
          an unread specialist into a consulted one. The filter is
          ``truncated IS NOT TRUE``, never ``= False``: NULL is a third state
          ("written before migration 0036"), and reading it as truncated would
          invalidate every pre-migration row's credit on no evidence at all.
          Rows written before 0036 therefore keep counting, which means three
          known-truncated production consults still credit the floor —
          unrecoverable from the table, and cheaper than the alternative.
          Subject to that, this can only ever turn "we have no record" into
          "here is the record".

        Arming the floor off these rows does not weaken
        ``ThreadState.floor_armed``'s latch. The latch exists to stop a
        DIFFERENT interview's consult, landing mid-await in another task, from
        retroactively arming this verdict; these rows are this interview's own,
        already committed before this turn began, and the seed only ever moves
        the latch False -> True. After the seed the global map really is
        non-empty, which is precisely what ``floor_armed`` asserts.

        Never raises: this runs inside ``_persist_assessment``, ahead of the
        write, and a failed SELECT must cost the fallback, not the verdict.
        """
        if not panel_is_owed(
            verdict.get("recommendation"), self._computed_score_and_band(verdict)[1]
        ):
            return
        subject = verdict.get("subject_agent_id")
        if not isinstance(subject, str) or not subject:
            return
        if not self.session_factory or not self.simulation_run_id:
            return
        thread_id = thread.thread_id if thread is not None else None
        key = (subject, thread_id)
        if key in self._consults_seeded:
            return
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                domains = (await db.execute(
                    sa_select(SpecialistConsult.domain).where(
                        SpecialistConsult.simulation_run_id == self.simulation_run_id,
                        SpecialistConsult.subject_agent_id == subject,
                        SpecialistConsult.thread_id == thread_id,
                        # IS NOT TRUE, not `== False`: NULL is "written before
                        # 0036", and those rows must keep crediting the floor
                        # exactly as they do today. See the docstring.
                        SpecialistConsult.truncated.is_not(True),
                    )
                )).scalars().all()
        except Exception as exc:  # noqa: BLE001 — never lose a verdict over a lookup
            logger.error(
                "Failed to read back the consult record for %r (thread %s): %s "
                "— this verdict's panel will be stored as UNVERIFIED",
                subject, thread_id or "?", exc, exc_info=True,
            )
            return
        self._consults_seeded.add(key)
        held = self._specialist_consults.setdefault(key, set())
        new = set(domains) - held
        if not new:
            return
        held.update(new)
        if thread is not None:
            thread.floor_armed = True
        logger.info(
            "[specialists] floor merged %d recorded consult(s) for %r (thread %s) "
            "from specialist_consults that this process had not seen "
            "(restarted mid-interview?)",
            len(new), subject, thread_id or "?",
        )

    def _record_consult(
        self, pi_agent_id: str, domain: str, thread_id: str | None = None,
    ) -> None:
        """Note a successful consult, keyed on the interview it happened in.

        Keyed on ``(pi, thread)`` rather than the PI alone. One PI's consults
        are NOT cumulative across interviews: a second interview is a second
        idea and owes its own panel. ``thread_id`` is None for direct callers
        that have no interview to name.
        """
        if not pi_agent_id:
            return
        self._specialist_consults.setdefault((pi_agent_id, thread_id), set()).add(domain)

    def _note_consult(
        self, pi_agent_id: str, domain: str, signal: str, thread_id: str | None = None,
    ) -> None:
        """Record a consult AND tally its signal.

        Two concerns, deliberately kept apart: `_record_consult` answers "does
        the floor consider this domain covered", which is per-interview; the
        tallies answer "what is this panel's signal mix" (run-level) and "is
        any one domain stuck on one label" (per-domain).

        `signal` is whatever the caller passes, and `src/agent/tools.py` passes
        `specialists.DEFAULTED_TALLY_LABEL` rather than a verdict label for a
        consult whose signal could not be READ — so the tallies carry the read
        failures as their own bucket and `signal_mix_report` /
        `domain_flatness_warning` exclude them from the mix they judge. The
        floor is unaffected: `_record_consult` runs first and takes only the
        domain.
        """
        self._record_consult(pi_agent_id, domain, thread_id)
        self._consult_signal_counts[signal] = (
            self._consult_signal_counts.get(signal, 0) + 1
        )
        by_domain = self._consult_signal_counts_by_domain.setdefault(domain, {})
        by_domain[signal] = by_domain.get(signal, 0) + 1

    def _consulted_domains(
        self, pi_agent_id: str, thread_id: str | None = None,
    ) -> frozenset[str]:
        """Domains consulted about this PI in this interview; empty for an
        interview we have no record of."""
        return frozenset(self._specialist_consults.get((pi_agent_id, thread_id), ()))

    @staticmethod
    def _computed_score_and_band(verdict: dict) -> tuple[float | None, str | None]:
        """The weighted score and band this verdict's own scores imply.

        One definition, because two callers now need it and they must agree: the
        row writer (`_persist_assessment`) and the specialist floor, which gates
        on the COMPUTED band as well as the model's written recommendation.

        An empty/missing ``scores`` map is "we don't know", not "we scored it a
        0.00 pass" — ``rubric_weighted_score({})`` returns 0.0 and that bands as
        ``pass``, a real and decisive decline the model never made. Both columns
        are nullable for exactly this case; leave them unset rather than record a
        verdict nobody rendered.

        One scale, one evidence bar (src/services/blackbird_rubric.py).
        """
        scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
        if not scores:
            return None, None
        score = rubric_weighted_score(scores)
        return score, rubric_band(score)

    def _specialist_floor_gap(
        self, verdict: dict, *, thread: ThreadState | None = None,
    ) -> set[str]:
        """Domains this verdict was obliged to consult but did not.

        Empty means the verdict may be persisted. Whether a panel is owed at all
        is ``specialists.panel_is_owed``'s question, not this method's, and it
        weighs BOTH the model's written recommendation and the COMPUTED band:
        either can pull a verdict into the panel and neither can pull it out,
        and anything unreadable fails CLOSED.

        This docstring used to state the abandoned rule — "only ``advance`` and
        ``conditional`` are held to the panel: a ``pass`` costs Blackbird
        nothing". Two things were wrong with it. A verdict that SCORES into
        advance/conditional owed a panel however the hub chose to label it (3 of
        the 4 conditional bands in the stored corpus are written ``pass``), and
        ``route-to-incubation`` — the incubation grant Blackbird exists to award,
        and the one recommendation that commits real money — was exempted as
        though it were a decline. It is the last verdict that should go
        unreviewed.

        ``thread``, when given, supplies ``floor_armed`` — whether
        ``_specialist_consults`` has been seen non-empty at any point in this
        thread's life, latched once per turn at the top of
        ``_reply_to_thread`` (see that latch's comment, and
        ``ThreadState.floor_armed``'s own comment, for the full history: a
        plain live read here was the original concurrency bug, and freezing
        the value forever at activation was a second bug fixed in a later
        round). It is consulted INSTEAD OF a live global map-emptiness check
        at persist time, because persist happens even later in the same turn
        as the latch, after this turn's own tool calls and after any `await` —
        long enough for a DIFFERENT interview's consult, landing in another
        task, to have changed the live map. ``thread=None`` (every direct
        caller with no thread to offer, and all pre-existing tests) falls back
        to a live global read, matching this method's behavior before
        ``floor_armed`` existed at all.

        The record is keyed on ``(subject, thread)``, not on the PI alone. An
        earlier version keyed on the PI (``subject_agent_id``) only, back when
        the artifact was a standalone Phase-5 post with no interview thread of
        its own, and that keying survived Option A's move into the Phase-4
        CONCLUDE reply even though a real thread existed at persist time by
        then — one PI's specialist consults were treated as cumulative across
        however many interview threads that PI had open. That let a PI's
        SECOND interview inherit the FIRST interview's consults and never
        convene its own panel: ``huganir`` was assessed 4 times in one run and
        ``hart`` 4, and only the first of each ever faced a panel. Keying on
        the thread as well as the PI gives each interview its own empty slot
        to start from. ``thread=None`` (every direct caller with no thread to
        offer, and all pre-existing tests) reads the ``None``-keyed slot for
        that PI — the same slot ``_record_consult`` writes to when it, too, is
        called with no ``thread_id``.

        FAILS OPEN in the two cases ``_floor_verifiable`` names, both of which
        mean "we have no record", never "the panel approved". An empty return
        is therefore ambiguous ON ITS OWN — "no gap" and "no way to tell" look
        identical here — which is why ``_persist_assessment`` asks
        ``_floor_verifiable`` as well and records the difference on the row
        (``missing_domains`` NULL vs ``[]``). Do not read an empty set as a
        clean bill of health without asking that question too.

        Note the second fail-open condition is about the whole map, not this
        PI's slot. An earlier version failed open whenever the SUBJECT had no
        consults, which quietly excused the commonest failure of all: a hub
        that simply never convenes a panel. If the map holds entries for other
        PIs, this process demonstrably records consults, so an absent PI means
        the panel really was skipped for them — and the floor bites.

        It does not fail open once any consult exists for that PI either: a hub
        that consulted one cheap specialist must not thereby buy an exemption
        from the rest.
        """
        # Gate on the COMPUTED band as well as the model's written
        # recommendation, and stop exempting `route-to-incubation`. Keying on
        # `recommendation` alone let a verdict that scores into `conditional`
        # exempt itself by writing `pass` — 3 of the 4 conditional bands in the
        # v2 corpus do exactly that — and it exempted Blackbird's own POSITIVE
        # outcome on the reasoning that "a decline costs Blackbird nothing".
        # `route-to-incubation` is the grant Blackbird exists to award; it is the
        # last verdict that should go unreviewed. See `panel_is_owed`.
        _, band = self._computed_score_and_band(verdict)
        if not panel_is_owed(verdict.get("recommendation"), band):
            return set()

        unverifiable = self._floor_unverifiable_reason(verdict, thread)
        if unverifiable is not None:
            # Both fail-open branches log the same way, at INFO, naming the
            # reason and the consequence — an operator reading this line needs
            # to know the row it produced says "unverified", not "clean".
            logger.info(
                "[specialists] floor fails open for subject %r: %s. The verdict "
                "is stored with missing_domains=[] — panel UNVERIFIED, which is "
                "not the same as verified complete (NULL).",
                verdict.get("subject_agent_id") or "?", unverifiable,
            )
            return set()

        # Guaranteed a non-empty str by the check above.
        subject = verdict.get("subject_agent_id")
        consulted = self._consulted_domains(
            subject, thread.thread_id if thread is not None else None
        )
        return set(required_domains_for(verdict, band=band) - consulted)

    def _floor_unverifiable_reason(
        self, verdict: dict, thread: ThreadState | None,
    ) -> str | None:
        """Why this verdict's panel cannot be checked at all, or ``None``.

        The single definition of ``_specialist_floor_gap``'s two fail-open
        conditions, so the gap computation and the "was this even checkable"
        question asked by ``_persist_assessment`` can never drift apart. The
        string is human-facing: it is logged, and it is the reason the row is
        written with ``missing_domains=[]``.
        """
        _, band = self._computed_score_and_band(verdict)
        if not panel_is_owed(verdict.get("recommendation"), band):
            # No panel was owed, so there is nothing to be unable to verify.
            return None
        subject = verdict.get("subject_agent_id")
        if not isinstance(subject, str) or not subject:
            return "it names no subject_agent_id, so there is no consult record to join to"
        armed = thread.floor_armed if thread is not None else bool(self._specialist_consults)
        if not armed:
            return (
                "this process has recorded no consult for ANY PI (restarted "
                "mid-interview?), so an absent record proves nothing"
            )
        return None

    def _floor_verifiable(
        self, verdict: dict, *, thread: ThreadState | None = None,
    ) -> bool:
        """Whether an empty ``_specialist_floor_gap`` means anything.

        ``_specialist_floor_gap`` returns an empty set both when the panel was
        genuinely complete and when there was no record to check it against —
        and the second case is the NORMAL state right after a restart, which
        production reaches by SIGKILL. Storing both as
        ``panel_incomplete=False, missing_domains=NULL`` counted every
        unverifiable verdict as a verified-complete panel and silently
        under-reported the one number the whole instrumentation exists to
        produce (spec §10's panel-gap surface).

        False here means "we could not check", never "the panel failed" — the
        row still stores ``panel_incomplete=False``, because we have no
        evidence of a gap either. It is recorded as the third state the column
        already anticipated: ``missing_domains=[]``.

        True for a verdict no panel was owed for (a ``pass`` that also bands
        ``pass``): nothing to verify is not the same as failing to verify.
        """
        return self._floor_unverifiable_reason(verdict, thread) is None
