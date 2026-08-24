"""Durable store for BlackbirdBot's screening verdicts.

Before this table an assessment existed only as a Slack message: nothing was
queryable, nothing was rankable, and the machine-readable verdict the rubric
(Part C.6) calls for was never emitted at all. One row per posted :mag:
Opportunity Assessment.

Every rubric field is nullable because a sparse or partly-unparseable verdict
must still be recorded — losing the assessment is strictly worse than storing an
incomplete one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class OpportunityAssessment(Base):
    __tablename__ = "opportunity_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The scouting agent (blackbird) and the lab it assessed.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subject_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: The interview thread this verdict came out of (the hub's thread root ts).
    #: One interview yields exactly one assessment — but until 0036 the table did
    #: not record WHICH interview, so that invariant was unenforceable and, worse,
    #: unrehydratable: after a restart `SimulationEngine._assessed_threads` is
    #: empty and the engine cannot tell a first verdict from a re-capture of a
    #: turn it already stored. Indexed for exactly that per-run lookup.
    #: NULL on every row written before 0036, and on any row whose thread could
    #: not be identified. Deliberately NOT unique with `simulation_run_id`: all 63
    #: historical rows are NULL, so a unique index would have to be partial
    #: (`WHERE thread_id IS NOT NULL`), and choosing that belongs with the change
    #: that starts writing the column, not with the one that adds it.
    thread_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )

    company_or_project: Mapped[str | None] = mapped_column(Text, nullable=True)
    funnel_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Computed by src/services/blackbird_rubric.py, NOT taken from the model.
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # `none_as_null=True` on every one of these: see `missing_domains` below for
    # the full account. Without it SQLAlchemy writes Python `None` as the JSONB
    # scalar `null`, which is a SECOND physical encoding of "absent" that
    # `WHERE col IS NULL` does not match. Migration 0036 normalized the rows
    # already written that way; tests/unit/test_json_none_as_null.py is the drift
    # alarm that stops a new column reintroducing it a third time.
    gating: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    red_flags: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    derisking_milestones: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sidecar item 10 (rubric v2.1.0): the single experiment Blackbird should
    # fund next — the line staff act on, so it is a first-class column rather
    # than a raw_verdict spelunk. NULL for every row written before 0037 (never
    # backfilled: old verdicts were not asked for one) and for a verdict that
    # names none.
    recommended_next_experiment: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # The verdict exactly as emitted, so a schema change never loses the original.
    raw_verdict: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    # The specialist floor's finding, recorded rather than enforced by
    # discarding. `_specialist_floor_gap` used to REFUSE an advance/conditional
    # verdict whose panel was never convened — but it runs after the concluding
    # reply is already in Slack, so refusing meant the PI had been told and
    # Blackbird kept nothing. Both production refusals (gordy, 2026-08-17) lost
    # a real conditional verdict that way. Storing it flagged keeps the record
    # and keeps the warning; it does not mean the gap is acceptable.
    #
    # True means "we looked and found a gap". False does NOT mean "the panel
    # was fine" on its own — it needs BOTH columns below to become a finding:
    # `missing_domains` to rule out the unverifiable `[]` case, and `panel_owed`
    # to say whether any floor evaluated this verdict in the first place. Two of
    # the three were not enough, which is how 12 rows came back green.
    # `src/services/assessment_detail.panel_state` is the one reader that
    # combines all three; nothing else should re-derive it.
    panel_incomplete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Which domains were missing. THREE states, written by
    # `SimulationEngine._persist_assessment`:
    #   [names] — `panel_incomplete=True`. These domains were owed by the
    #             verdict's own content and never consulted in this interview.
    #   NULL    — `panel_incomplete=False`, NO GAP RECORDED. Read this column
    #             ALONE and that is all it says: it does NOT mean "verified
    #             complete". A NULL covers both "the floor evaluated this
    #             verdict and found nothing owed and unconsulted" and "no floor
    #             ever ran on it", and only `panel_owed` below separates the two.
    #   []      — `panel_incomplete=False`, panel UNVERIFIED: the floor could
    #             not check at all. Either the verdict named no
    #             `subject_agent_id`, or the process had recorded no consult
    #             for anyone (`_floor_verifiable`) — the ordinary state after a
    #             restart, and production's last exit was a SIGKILL. Not
    #             evidence of a gap, so it is not flagged; not evidence of a
    #             complete panel either, so it must never be counted as one.
    #
    # This comment used to call NULL "panel VERIFIED complete", and that reading
    # is the defect `panel_owed` exists to end: 12 production rows written by a
    # floor that exempted them (storing "no panel was owed" as exactly this NULL)
    # were later re-read as completed audits, at least five of them with a
    # demonstrable gap. `src/services/assessment_detail.panel_state` is the one
    # place the three columns are turned into a finding, and it reaches
    # "verified" ONLY via `panel_owed is True`.
    #
    # Rows written before 2026-08-19 have NULL for both the "no gap found" and
    # the unverified case — that conflation is what [] exists to end. Rows
    # written before 0036 have NULL in `panel_owed` too, so for them the
    # remaining split is not recoverable at all; they render `unrecorded`.
    #
    # `none_as_null=True` is load-bearing, not decoration. SQLAlchemy's JSON type
    # defaults it False, which persists Python `None` as the JSONB scalar `null`
    # rather than as SQL NULL — so the state documented above as NULL was stored
    # as something `WHERE missing_domains IS NULL` does not match. Measured on
    # production 2026-08-20: 15 rows held `jsonb_typeof = 'null'` while 18 older
    # rows held a true SQL NULL, one logical state in two encodings. Both read
    # back as `None` through the ORM, so the damage was confined to SQL-level
    # readers — which is precisely the reader the three-state contract above
    # invites. `[]` is unaffected: it is not None, so it still stores as an
    # array and stays distinguishable. Migration 0031 normalized the 15 rows.
    missing_domains: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # Was this verdict OWED a specialist panel, as judged when the row was
    # written? THREE states, and the third is the whole point of the column:
    #   True  — a panel was owed under the rules in force at write time, so the
    #           floor evaluated this verdict. `panel_incomplete`/`missing_domains`
    #           above then say what it found; an empty gap here is a real finding,
    #           and this is the ONLY combination that means "verified complete".
    #   False — no panel was owed (the floor determined this and recorded it).
    #   NULL  — the row was written before 0036. We do not know whether any floor
    #           ran at all, and saying "verified" for it is a claim nobody made.
    # Deliberately NOT backfilled: guessing would manufacture exactly the
    # verification this column exists to stop asserting.
    #
    # It exists because the read path used to re-derive this by calling
    # `panel_is_owed` at RENDER time, which answers a different question — "would
    # a panel be owed under today's rules" — and therefore silently re-labels
    # rows written under an older rule every time the predicate moves. It moved
    # twice in 2026-08 alone. A durable fact belongs in a column.
    panel_owed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Which rubric document scored this row (prompts/rubric/blackbird-rubric.toml
    # [meta].version, plus a short content hash of the file bytes). NULL means the
    # row predates stamping. This is what keeps pre- and post-calibration verdicts
    # comparable once the rubric starts being edited.
    rubric_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rubric_content_hash: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityAssessment subject={self.subject_agent_id} "
            f"rec={self.recommendation} score={self.weighted_score}>"
        )


class AssessmentDrop(Base):
    """One verdict that was lost, whether generated and discarded or never produced at all.

    The counterpart to the table above, and the reason it exists: every way an
    assessment can be lost is silent.

    For every reason except ``empty_reply``, the concluding reply has already
    been posted to Slack and the thread closes normally; for ``empty_reply``
    nothing was posted at all — the turns themselves failed. Either way the
    only trace is one WARNING line in a container log nobody is tailing — so an
    empty /admin/assessments page is indistinguishable from "no ideas screened
    yet".

    Recording a drop is strictly best-effort and must never cost the reply that
    already went out, exactly like the assessment write itself.

    ``reason`` is one of:
      * ``specialist_floor``     — HISTORICAL ONLY: an advance/conditional verdict
        whose required panel was never convened (``_specialist_floor_gap``), back
        when that gap meant refusing the verdict outright. As of the fix recorded
        on ``OpportunityAssessment.panel_incomplete`` above, that gap is stored
        flagged instead of discarded, so this path can no longer produce a new
        row — the three that already carry this reason predate the change.
      * ``unparseable_sidecar``  — an ``<assessment_json>`` tag was present but
        yielded no usable verdict (commonly a max_tokens truncation that ate the
        closing tag).
      * ``missing_sidecar``      — the reply concluded, did not decline, and
        carried no sidecar at all.
      * ``premature_sidecar``    — HISTORICAL ONLY as of 2026-08-22. Meant "a
        sidecar arrived on a turn that neither concluded the interview nor closed
        the thread, so a later turn is still owed the verdict". That promise was
        unbacked: nothing scheduled the later turn, nothing tracked the debt, and
        nothing kept the discarded JSON. Run 8b64a0e0 refused two verdicts this
        way at ordinal 10 — one of them the run's highest-scoring idea and its
        only ``route-to-incubation`` — and the run's timer ended both interviews
        minutes later. The gate now trusts the sidecar and lets
        ``_retire_superseded_verdict`` (which shipped in the same commit as this
        reason) handle "a later turn knows more". No new rows.
      * ``closed_before_verdict`` — a reply from a NON-hub agent closed the
        interview with ⏸️ before the hub reached a verdict. ⏸️ is an instruction
        to both roles, and ``_check_thread_outcome`` acts on whoever replied, so
        a lab bot declining its own pitch ends the hub's screen: seven times on
        run 8b64a0e0, none with an assessment. The hub's OWN ⏸️ decline is not
        recorded here — that is Outcome 2, where most interviews are meant to end.
      * ``duplicate_thread_verdict`` — one interview yields one assessment, and
        this row is the verdict that did not become it: either a re-capture of a
        turn already stored (or anything following a verdict whose reply closed
        the interview), or an earlier provisional verdict SUPERSEDED by a later
        concluding one — in which case the earlier row was retired and this drop
        is the only remaining trace of it. See
        ``SimulationEngine._assessed_threads`` and
        ``_retire_superseded_verdict``.
      * ``empty_reply``          — the interview was ABANDONED: two consecutive
        replies produced no usable text (an llm.py empty reply — its ERROR
        names the stop reason — or a reply that could not be parsed into a
        Slack message), so the engine backed off and no later turn exists to
        produce the verdict. Unlike every other reason, NOTHING was posted to
        Slack for the failing turns. Recorded at any ordinal, hub-only. Added
        2026-08-21 after run 076e80b6 measured 13 empty replies in 90 minutes
        and stranded a thread at message count 2.
      * ``unwritable_row``       — a stored assessment row that the database
        refused even ALONE, during ``_recover_rows_individually``'s per-row
        retry after the whole batch it belonged to failed
        (``_flush_persisted``). Unlike every reason above, this is not a GATE
        decision — the engine wanted the row and the database would not take
        it. The verdict was already fully formed (concluded, parsed, and
        assembled into a row) before it was lost, so this drop is its ONLY
        surviving trace; the verdict itself rides along in ``raw_verdict``
        exactly as the row would have stored it, and ``detail`` names the
        database's own exception plus the channel/thread it came from. Not
        retried — the row already failed twice, batch then alone — and
        recording it is itself best-effort, since a malformed row must not take
        the surviving verdicts of its batch down with it. See
        ``SimulationEngine._record_unwritable_assessment``. Added 2026-08-22.
    """

    __tablename__ = "assessment_drops"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The verdict that was dropped, exactly as the model emitted it — the whole
    #: point of the column. Before 0035 a drop recorded only a reason and a
    #: sentence of prose, so refusing a sidecar DESTROYED it: run 8b64a0e0 lost
    #: markham (which recomputes to 3.04, that run's highest score, and its only
    #: `route-to-incubation`) and weeraratna, recoverable afterwards only by luck,
    #: because `llm_call_logs.response_text` happens to keep the raw response.
    #: A refusal is now non-destructive whatever the gate policy of the day is.
    #: NULL for a drop with nothing to keep (`empty_reply`, `missing_sidecar`).
    #:
    #: `none_as_null=True` was missing when 0035 added this column, so "nothing to
    #: keep" got stored two ways at once — production held 15 SQL NULLs and 2 JSONB
    #: `null`s for one logical state, and the obvious operator query ("which
    #: refusals kept their verdict?", i.e. `WHERE raw_verdict IS NOT NULL`)
    #: returned exactly the 2 rows that kept nothing. This is the same defect 0031
    #: fixed on `OpportunityAssessment.missing_domains` six weeks earlier, on a
    #: brand-new column, because the only guard was a test about that one column.
    #: 0036 normalized the 2 rows and added
    #: tests/unit/test_json_none_as_null.py — a walk over `Base.metadata` — so a
    #: third occurrence fails the gate instead of reaching production.
    raw_verdict: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<AssessmentDrop subject={self.subject_agent_id} reason={self.reason}>"
        )
