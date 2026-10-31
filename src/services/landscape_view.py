"""Read model for the landscaping pages, routes and chat record (spec §13).

Every view returns plain dicts of strings, numbers, booleans, dates, lists and dicts, never ORM
objects; ids are strings. The hiding rules live here once, so the pages, the decision and
review routes and the chat record apply the same ones (``hiding_for``, ``hide_assessment``,
``hide_deep``):

- **Blind batch** (L53, D56): until the viewer has a quick decision on an opportunity that is a
  card of a blind batch, its score, band, rank, dimension and provisional scores,
  ``score_rationale`` and ``dimension_rationales`` are hidden.
- **Expert sample** (D63): on the viewer's own unsubmitted expert-sample item of the current
  quarter, S9's and S6's calls are hidden (gates, scores, provisional scores, deal-breaker
  confirmations, panel opinions, rationales, band, recommendation). Both hidings apply until
  both lift.

**Which assessment** (spec §7.10, §13.2, X1). The assessment *shown* for an opportunity is its
newest ``done`` S9 row in the active epoch (highest opportunity version, then newest), never a
candidate epoch's. A version whose S9 has not completed leaves the earlier one shown, so it is
not "changed since your decision" (D50). With no ``done`` row in the active epoch the card says
so, and the last ``done`` row of a retired epoch is its history link. A retired opportunity reads
as history: its last ``done`` assessment of an active or retired epoch, marked retired (D62).

**Independent votes** (L40, L50, L51, D60). ``visible_votes`` decides what anyone sees of other
people's decisions, reviews and status events; tabs and counts are the viewer's own.

**Probe flags and blind batches** (F2, D56). A card's ``probe`` flag is true only once the blind
batch that issued it is superseded by a later batch. Probe opportunities stay out of the staff
lists until then, and a viewer with an undecided card in a blind batch sees none of that batch's
cards in **Current** or **All assessed**.
"""
from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import reports, review_batches
from src.landscaping.intake import _parse_controls
from src.landscaping.review_batches import viewer_cards
from src.landscaping.stages.rerank import card_order
from src.landscaping.structured_lines import structured_line
from src.landscaping.worker import picks_applied
from src.models import AppSetting, User
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeConsult,
    LandscapeCut,
    LandscapeDecision,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeIdea,
    LandscapeIdeaAlias,
    LandscapeIdeaEvidence,
    LandscapeLab,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRanking,
    LandscapeRegressionRun,
    LandscapeRejectAudit,
    LandscapeReport,
    LandscapeReview,
    LandscapeReviewAssignment,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeReviewEvent,
    LandscapeScreen,
    LandscapeSource,
    LandscapeText,
    LandscapeTriage,
)
from src.models.user import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from src.services.assessment_detail import (
    _gating_definitions,
    build_dimension_rows,
    derive_strengths_and_risks,
    key_point_sections,
)
from src.services.rubric_revisions import resolve_revision
from src.services.verdict_fields import VERDICT_FIELDS
from src.services.web_permissions import can_review, is_staff

#: The banner on every view while a candidate or held epoch exists: the pages keep showing the
#: active epoch's last complete cut until the candidate activates (spec §7.11).
REJUDGE_BANNER = "Being re-judged under new criteria"

#: The verdict fields presentation.py strips from an ``OpportunityAssessment`` for a
#: non-staff viewer (``STAFF_VERDICT_FIELDS``); this read model strips them itself (§13.1).
_STAFF_ONLY = frozenset(f.column for f in VERDICT_FIELDS if f.staff_only)

#: S9 columns a blind-batch card hides (L53, D56); ``raw_verdict`` holds them all.
_BLIND_ASSESSMENT = frozenset({
    "weighted_score", "band", "rank", "scores", "score_rationale", "dimension_rationales",
    "raw_verdict",
})
#: S9 columns hidden on an expert's own open sample item (D63).
_EXPERT_ASSESSMENT = _BLIND_ASSESSMENT | {
    "gating", "gating_rationales", "rationale", "recommendation",
}
#: S6 columns a blind-batch card hides: the provisional scores and their reasons (D56).
_BLIND_DEEP = frozenset({
    "provisional_scores", "provisional_weighted_score", "dimension_rationales",
})
#: S6 columns hidden on an expert's own open sample item (D63); ``summary`` is S6's own prose
#: about its calls.
_EXPERT_DEEP = _BLIND_DEEP | {"gating", "gating_rationales", "flags_reviewed", "summary"}

#: ``app_settings`` words that mean true (``landscape_paused``), as the worker reads them.
_TRUE_WORDS = ("true", "1", "yes", "on")
_UNACTIVATED = ("candidate", "held")
#: Epochs whose assessments are history; a candidate's never are (§7.11).
_HISTORY_EPOCHS = ("active", "retired")
_REVIEW_ROLES = (USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Hiding:
    """What one viewer may not see yet on one opportunity."""

    blind: bool  # an undecided card of a blind batch (L53, D56)
    expert: bool  # the viewer's own unsubmitted expert-sample item this quarter (D63)


# --- small helpers ----------------------------------------------------------------------------


def _sid(value: Any) -> str | None:
    return str(value) if value is not None else None


def _quarter(now: datetime) -> str:
    """``YYYY-Qn``, as the worker names the expert-sample quarter."""
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def _row_values(row: Any) -> dict:
    """A model row's column values by column name, ids as strings."""
    return {
        column.key: _sid(value) if isinstance(value, uuid.UUID) else value
        for column in row.__table__.columns
        for value in (getattr(row, column.key),)
    }


def _pitch(text: str | None) -> str | None:
    """The first two sentences of an elevator pitch (§13.1)."""
    if not text:
        return None
    return " ".join(_SENTENCE_END.split(text.strip())[:2])


async def _setting(db: AsyncSession, key: str) -> str | None:
    """An ``app_settings`` value with whitespace and one pair of quotes stripped."""
    value = await db.scalar(select(AppSetting.value).where(AppSetting.key == key))
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


# --- hiding -----------------------------------------------------------------------------------


@dataclass
class _Viewer:
    """One viewer's own state: their quick decisions, the blind-batch cards and their open
    expert items this quarter."""

    user: Any
    staff: bool
    admin: bool
    decisions: dict[uuid.UUID, LandscapeDecision]
    blind: set[uuid.UUID]
    expert: dict[uuid.UUID, LandscapeExpertJudgment]

    def hiding(self, opportunity_id: uuid.UUID) -> Hiding:
        return Hiding(
            blind=opportunity_id in self.blind and opportunity_id not in self.decisions,
            expert=opportunity_id in self.expert,
        )


async def _viewer(db: AsyncSession, user: Any, only: uuid.UUID | None = None) -> _Viewer:
    """The viewer's state, for every opportunity or only for ``only``."""
    D, B, BI, J = (
        LandscapeDecision, LandscapeReviewBatch, LandscapeReviewBatchItem,
        LandscapeExpertJudgment,
    )
    decisions = select(D).where(D.reviewer_id == user.id)
    blind = select(BI.opportunity_id).join(B, B.id == BI.batch_id).where(B.blind.is_(True))
    expert = select(J).where(
        J.expert_id == user.id, J.submitted_at.is_(None), J.quarter == _quarter(datetime.now(UTC))
    )
    if only is not None:
        decisions = decisions.where(D.opportunity_id == only)
        blind = blind.where(BI.opportunity_id == only)
        expert = expert.where(J.opportunity_id == only)
    return _Viewer(
        user=user,
        staff=is_staff(user),
        admin=user.user_role == USER_ROLE_ADMIN,
        decisions={d.opportunity_id: d for d in (await db.scalars(decisions)).all()},
        blind=set((await db.scalars(blind)).all()),
        expert={j.opportunity_id: j for j in (await db.scalars(expert)).all()},
    )


async def hiding_for(db, user, opportunity_id: uuid.UUID) -> Hiding:
    """The hidings that apply to `user` on this opportunity now."""
    return (await _viewer(db, user, opportunity_id)).hiding(opportunity_id)


def hide_assessment(values: dict, hiding: Hiding) -> dict:
    """A copy of an S9 assessment's values (keys are ``LandscapeAssessment`` column names)
    without the fields `hiding` hides. Unhidden keys pass through unchanged.

    Blind: ``weighted_score``, ``band``, ``rank``, ``scores``, ``score_rationale``,
    ``dimension_rationales``. Expert: those plus ``gating``, ``gating_rationales``,
    ``rationale`` and ``recommendation``. Either drops ``raw_verdict``, which holds them all."""
    hidden: frozenset[str] = frozenset()
    if hiding.blind:
        hidden |= _BLIND_ASSESSMENT
    if hiding.expert:
        hidden |= _EXPERT_ASSESSMENT
    return {k: v for k, v in values.items() if k not in hidden}


def hide_deep(values: dict, hiding: Hiding) -> dict:
    """A copy of an S6 result's values (keys are ``LandscapeDeep`` column names) without the
    fields `hiding` hides: the provisional scores and their ``dimension_rationales`` for a blind
    card; every S6 call (gates, their rationales, flags reviewed, provisional and dimension
    scores, and S6's ``summary``) for an expert's item. Panel opinions are separate
    ``LandscapeConsult`` rows: a caller drops them itself when ``hiding.expert``."""
    hidden: frozenset[str] = frozenset()
    if hiding.blind:
        hidden |= _BLIND_DEEP
    if hiding.expert:
        hidden |= _EXPERT_DEEP
    return {k: v for k, v in values.items() if k not in hidden}


async def can_view(db, user, opportunity_id: uuid.UUID) -> bool:
    """Staff: any opportunity; a reviewer: one in an issued batch or in their own expert
    sample, any quarter (D50, D63). False for a PI and for an unknown id."""
    if not can_review(user) or await db.get(LandscapeOpportunity, opportunity_id) is None:
        return False
    if is_staff(user):
        return True
    BI, J = LandscapeReviewBatchItem, LandscapeExpertJudgment
    return bool(await db.scalar(select(or_(
        exists().where(BI.opportunity_id == opportunity_id),
        exists().where(J.opportunity_id == opportunity_id, J.expert_id == user.id),
    ))))


# --- votes ------------------------------------------------------------------------------------


def visible_votes(user, opportunity_id, decisions, reviews, events) -> tuple[list, list, list]:
    """The decisions, reviews and status events on `opportunity_id` that `user` may see
    (L40, L50, L51), each list in its given order.

    Until `user` has a quick decision on this opportunity, only their own items (by
    ``reviewer_id`` / ``actor_id``), whatever their role, so no count of anyone else's can be
    derived either. A full review never unlocks; the decision is keyed on the opportunity, so a
    new assessment does not re-blind a viewer who has decided. Items of other opportunities
    are dropped."""
    decisions = [d for d in decisions if d.opportunity_id == opportunity_id]
    reviews = [r for r in reviews if r.opportunity_id == opportunity_id]
    events = [e for e in events if e.opportunity_id == opportunity_id]
    if any(d.reviewer_id == user.id for d in decisions):
        return decisions, reviews, events
    return (
        [d for d in decisions if d.reviewer_id == user.id],
        [r for r in reviews if r.reviewer_id == user.id],
        [e for e in events if e.actor_id == user.id],
    )


def changed_since_decision(decision, shown_assessment_id: uuid.UUID | None) -> bool:
    """Whether `decision` was made on an assessment other than the one shown now (D50). No
    decision is never changed; None (no completed assessment) differs from any decided id."""
    if decision is None:
        return False
    return decision.landscape_assessment_id != shown_assessment_id


def strip_staff_only(view: dict, user) -> dict:
    """`view` without the staff-only verdict fields (``strengths``, ``risks``,
    ``competitive_landscape``, ``evidence_maturity``) for a non-staff `user`, at its top level
    and in its ``assessment`` dict; staff get `view` unchanged."""
    if is_staff(user):
        return view
    out = {k: v for k, v in view.items() if k not in _STAFF_ONLY}
    if isinstance(out.get("assessment"), dict):
        out["assessment"] = {
            k: v for k, v in out["assessment"].items() if k not in _STAFF_ONLY
        }
    return out


def _decision_dict(row: LandscapeDecision, user: Any) -> dict:
    return {
        "id": _sid(row.id), "reviewer_id": _sid(row.reviewer_id),
        "reviewer_name": row.reviewer_name, "decision": row.decision,
        "reason_tags": list(row.reason_tags or []), "reason": row.reason,
        "landscape_assessment_id": _sid(row.landscape_assessment_id), "edited": row.edited,
        "created_at": row.created_at, "updated_at": row.updated_at,
        "mine": row.reviewer_id == user.id,
    }


def _review_dict(row: LandscapeReview, user: Any) -> dict:
    return {
        "id": _sid(row.id), "reviewer_id": _sid(row.reviewer_id),
        "reviewer_name": row.reviewer_name, "score": row.score, "comment": row.comment,
        "dimension_scores": row.dimension_scores, "rubric_version": row.rubric_version,
        "landscape_assessment_id": _sid(row.landscape_assessment_id), "edited": row.edited,
        "created_at": row.created_at, "updated_at": row.updated_at,
        "mine": row.reviewer_id == user.id,
    }


def _event_dict(row: LandscapeReviewEvent, user: Any) -> dict:
    return {
        "id": _sid(row.id), "action": row.action, "actor_name": row.actor_name,
        "landscape_assessment_id": _sid(row.landscape_assessment_id),
        "created_at": row.created_at, "mine": row.actor_id == user.id,
    }


# --- epochs and batches -----------------------------------------------------------------------


@dataclass
class _Epochs:
    active: LandscapeEpoch | None
    status: dict[uuid.UUID, str]
    rejudging: bool

    @property
    def active_id(self) -> uuid.UUID | None:
        return self.active.id if self.active is not None else None

    def summary(self) -> dict | None:
        epoch = self.active
        if epoch is None:
            return None
        return {
            "id": _sid(epoch.id), "status": epoch.status,
            "prompt_set_version": epoch.prompt_set_version,
            "criteria_version": epoch.criteria_version, "activated_at": epoch.activated_at,
        }


async def _epochs(db: AsyncSession) -> _Epochs:
    rows = (await db.scalars(select(LandscapeEpoch))).all()
    return _Epochs(
        active=next((e for e in rows if e.status == "active"), None),
        status={e.id: e.status for e in rows},
        rejudging=any(e.status in _UNACTIVATED for e in rows),
    )


@dataclass
class _Issued:
    """Every issued batch (newest first) and its items."""

    batches: list[LandscapeReviewBatch]
    items: list[LandscapeReviewBatchItem]

    @property
    def newest(self) -> date | None:
        return self.batches[0].issued_on if self.batches else None

    def superseded(self, batch: LandscapeReviewBatch) -> bool:
        return self.newest is not None and batch.issued_on < self.newest

    def batch_of(self) -> dict[uuid.UUID, LandscapeReviewBatch]:
        return {b.id: b for b in self.batches}

    def visible_probes(self) -> set[uuid.UUID]:
        """Probes whose blind batch is superseded: the only probe flags any page shows (F2)."""
        batches = self.batch_of()
        return {
            i.opportunity_id for i in self.items
            if i.is_probe and self.superseded(batches[i.batch_id])
        }


async def _issued(db: AsyncSession) -> _Issued:
    B, BI = LandscapeReviewBatch, LandscapeReviewBatchItem
    batches = (await db.scalars(select(B).order_by(B.issued_on.desc()))).all()
    items = (await db.scalars(select(BI).order_by(BI.created_at, BI.id))).all()
    return _Issued(list(batches), list(items))


def _banner(ep: _Epochs) -> str | None:
    return REJUDGE_BANNER if ep.rejudging else None


# --- cards ------------------------------------------------------------------------------------


@dataclass
class _Pick:
    shown: LandscapeAssessment | None  # newest done in the active epoch
    display: LandscapeAssessment | None  # what the card or page renders
    latest_active: LandscapeAssessment | None  # newest active-epoch row of any status
    history: LandscapeAssessment | None  # the last done row of a retired epoch


def _pick(rows: Sequence[LandscapeAssessment], ep: _Epochs, retired: bool) -> _Pick:
    def order(a: LandscapeAssessment) -> tuple:
        return (a.opportunity_version or 0, a.created_at, str(a.id))

    in_active = [a for a in rows if ep.active_id is not None and a.epoch_id == ep.active_id]
    shown = max((a for a in in_active if a.status == "done"), key=order, default=None)
    history = max(
        (a for a in rows if a.status == "done" and ep.status.get(a.epoch_id) == "retired"),
        key=lambda a: (a.created_at, str(a.id)), default=None,
    )
    display = shown
    if retired:
        display = max(
            (a for a in rows if a.status == "done"
             and ep.status.get(a.epoch_id) in _HISTORY_EPOCHS),
            key=lambda a: (a.created_at, str(a.id)), default=None,
        )
    return _Pick(shown, display, max(in_active, key=order, default=None), history)


async def _assessments(
    db: AsyncSession, opportunity_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[LandscapeAssessment]]:
    A = LandscapeAssessment
    rows = (await db.scalars(
        select(A).where(A.opportunity_id.in_(list(opportunity_ids))).order_by(A.created_at)
    )).all()
    out: dict[uuid.UUID, list[LandscapeAssessment]] = defaultdict(list)
    for row in rows:
        out[row.opportunity_id].append(row)
    return out


async def _versions(
    db: AsyncSession, opportunity_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, dict[int, LandscapeOpportunityVersion]]:
    V = LandscapeOpportunityVersion
    rows = (await db.scalars(select(V).where(V.opportunity_id.in_(list(opportunity_ids))))).all()
    out: dict[uuid.UUID, dict[int, LandscapeOpportunityVersion]] = defaultdict(dict)
    for row in rows:
        out[row.opportunity_id][row.version] = row
    return out


def _card_version(
    opp: LandscapeOpportunity, versions: Mapping[int, LandscapeOpportunityVersion],
    display: LandscapeAssessment | None,
) -> LandscapeOpportunityVersion | None:
    """The version the displayed assessment judged, else the latest."""
    if display is not None and display.opportunity_version in versions:
        return versions[display.opportunity_version]
    return versions.get(opp.version_counter) or (
        versions[max(versions)] if versions else None
    )


async def _idea_ids(
    db: AsyncSession, wanted: Iterable[tuple[uuid.UUID, str]]
) -> dict[tuple[uuid.UUID, str], uuid.UUID]:
    """``(epoch_id, idea_key) -> idea id``, directly or through the epoch's aliases."""
    wanted = set(wanted)
    if not wanted:
        return {}
    keys = {key for _, key in wanted}
    epoch_ids = {epoch for epoch, _ in wanted}
    Idea, AL = LandscapeIdea, LandscapeIdeaAlias
    found = {
        (epoch, key): idea_id
        for idea_id, epoch, key in (await db.execute(
            select(Idea.id, Idea.epoch_id, Idea.idea_key)
            .where(Idea.idea_key.in_(keys), Idea.epoch_id.in_(epoch_ids))
        )).all()
    }
    if wanted - found.keys():
        for idea_id, epoch, key in (await db.execute(
            select(AL.idea_id, AL.epoch_id, AL.old_key)
            .where(AL.old_key.in_(keys), AL.epoch_id.in_(epoch_ids))
        )).all():
            found.setdefault((epoch, key), idea_id)
    return {pair: found[pair] for pair in wanted if pair in found}


async def _lab_names(
    db: AsyncSession, idea_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Each idea's labs, named by person (§13.1): the display names behind its documents."""
    idea_ids = list(idea_ids)
    if not idea_ids:
        return {}
    E, X, Doc, Lab = LandscapeIdeaEvidence, LandscapeExtraction, LandscapeDocument, LandscapeLab
    rows = (await db.execute(
        select(E.idea_id, Doc.lab_key, Lab.display_name)
        .join(X, X.id == E.extraction_id).join(Doc, Doc.id == X.document_id)
        .outerjoin(Lab, Lab.lab_key == Doc.lab_key)
        .where(E.idea_id.in_(idea_ids), Doc.merged_into.is_(None), Doc.lab_key.is_not(None))
    )).all()
    out: dict[uuid.UUID, set[str]] = defaultdict(set)
    for idea_id, lab_key, name in rows:
        out[idea_id].add(name or lab_key)
    return {idea_id: sorted(names) for idea_id, names in out.items()}


async def _last_complete_cut(db: AsyncSession, epoch_id: uuid.UUID) -> LandscapeCut | None:
    """The epoch's newest complete cut; a ``boundary_pending`` one is not current yet (§7.6)."""
    C = LandscapeCut
    return await db.scalar(
        select(C).where(C.epoch_id == epoch_id, C.status == "complete")
        .order_by(C.completed_at.desc().nulls_last(), C.created_at.desc()).limit(1)
    )


async def _ranks(
    db: AsyncSession, epoch_id: uuid.UUID | None
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """The active epoch's S7 positions and its current cut's S5 ranks (D30)."""
    if epoch_id is None:
        return {}, {}
    s7 = dict((await db.execute(
        select(LandscapeRanking.idea_id, LandscapeRanking.position)
        .where(LandscapeRanking.epoch_id == epoch_id)
    )).all())
    cut = await _last_complete_cut(db, epoch_id)
    s5 = {
        uuid.UUID(str(idea_id)): n
        for n, idea_id in enumerate((cut.ordered_idea_ids or []) if cut else [], start=1)
    }
    return s7, s5


@dataclass
class _Cards:
    """Card dicts by opportunity, with what ordering and carry-overs need."""

    by_id: dict[uuid.UUID, dict]
    members: dict[uuid.UUID, list[uuid.UUID]]
    shown: dict[uuid.UUID, uuid.UUID | None]
    retired: frozenset[uuid.UUID]
    s7: dict[uuid.UUID, int]
    s5: dict[uuid.UUID, int]

    def order(self, opportunity_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
        """Card order (D30): best member's S7 position, else its S5 rank."""
        return card_order(
            {o: self.members.get(o, []) for o in opportunity_ids},
            s7_positions=self.s7, s5_ranks=self.s5,
        )


def _failure(pick: _Pick) -> dict:
    """Why a card has no completed assessment in the active epoch (§7.10)."""
    row = pick.latest_active
    return {
        "status": row.status if row is not None else "not_assessed",
        "error": row.error if row is not None else None,
    }


def _best_rank(members: Sequence[uuid.UUID], s7: Mapping, s5: Mapping) -> int | None:
    ranks = [s7.get(i, s5.get(i)) for i in members]
    ranks = [r for r in ranks if r is not None]
    return min(ranks) if ranks else None


def _card(
    viewer: _Viewer, opp: LandscapeOpportunity, version: LandscapeOpportunityVersion | None,
    pick: _Pick, *, probe: bool, labs: list[str], rank: int | None,
) -> dict:
    hiding = viewer.hiding(opp.id)
    retired = opp.retired_at is not None
    values = hide_assessment(_row_values(pick.display), hiding) if pick.display else {}
    decision = viewer.decisions.get(opp.id)
    shown_id = pick.shown.id if pick.shown is not None else None
    no_assessment = pick.shown is None and not retired
    return {
        "opportunity_id": _sid(opp.id),
        "label": version.label if version is not None else None,
        "headline": values.get("headline"),
        "structured_lines": list(values.get("structured_lines") or []),
        "pitch": _pitch(values.get("elevator_pitch")),
        "labs": labs,
        "score": values.get("weighted_score"),
        "band": values.get("band"),
        "rank": None if hiding.blind or hiding.expert else rank,
        "recommendation": values.get("recommendation"),
        "assessment_id": _sid(pick.display.id) if pick.display is not None else None,
        "no_assessment": no_assessment,
        "failure": _failure(pick) if no_assessment else None,
        "history_assessment_id": _sid(pick.history.id) if pick.history is not None else None,
        "retired": retired,
        "probe": probe,
        "blind_hidden": hiding.blind,
        "expert_hidden": hiding.expert,
        "my_decision": _decision_dict(decision, viewer.user) if decision else None,
        "changed_since_decision": (
            not retired and changed_since_decision(decision, shown_id)
        ),
    }


async def _load_cards(
    db: AsyncSession, viewer: _Viewer, ep: _Epochs, opportunity_ids: Iterable[uuid.UUID],
    visible_probes: set[uuid.UUID],
) -> _Cards:
    """Card dicts (§13.1) for these opportunities; unknown ids are skipped.

    Each card: ``opportunity_id`` (str), ``label`` (str|None), ``headline`` (str|None),
    ``structured_lines`` (list[str]), ``pitch`` (str|None, two sentences), ``labs``
    (list[str]), ``score`` (float|None), ``band`` (str|None), ``rank`` (int|None, the best
    member's S7 position or S5 rank; None for a probe not yet revealed), ``recommendation``
    (str|None), ``assessment_id`` (str|None, the assessment rendered), ``no_assessment``
    (bool), ``failure``
    ({"status", "error"}|None), ``history_assessment_id`` (str|None), ``retired`` (bool),
    ``probe`` (bool), ``blind_hidden`` and ``expert_hidden`` (bool), ``my_decision`` (decision
    dict|None) and ``changed_since_decision`` (bool). Score, band, rank and recommendation
    are None where the viewer's hiding hides them."""
    ids = set(opportunity_ids)
    opps = {o.id: o for o in (await db.scalars(
        select(LandscapeOpportunity).where(LandscapeOpportunity.id.in_(list(ids)))
    )).all()}
    assessments = await _assessments(db, opps)
    versions = await _versions(db, opps)
    picks = {
        o: _pick(assessments.get(o, []), ep, opp.retired_at is not None)
        for o, opp in opps.items()
    }
    chosen = {o: _card_version(opp, versions.get(o, {}), picks[o].display)
              for o, opp in opps.items()}
    wanted = {(v.epoch_id, key) for v in chosen.values() if v for key in v.member_idea_keys or []}
    idea_ids = await _idea_ids(db, wanted)
    members = {
        o: [idea_ids[(v.epoch_id, k)] for k in (v.member_idea_keys or [])
            if (v.epoch_id, k) in idea_ids]
        if v else []
        for o, v in chosen.items()
    }
    labs = await _lab_names(db, {i for ids_ in members.values() for i in ids_})
    s7, s5 = await _ranks(db, ep.active_id)
    # An unrevealed probe's rank lies beyond the cut, so showing it would flag it (F2).
    unranked = {
        o for o, v in chosen.items() if v is not None and v.is_probe and o not in visible_probes
    }
    by_id = {
        o: _card(
            viewer, opp, chosen[o], picks[o], probe=o in visible_probes,
            labs=sorted({name for i in members[o] for name in labs.get(i, [])}),
            rank=None if o in unranked else _best_rank(members[o], s7, s5),
        )
        for o, opp in opps.items()
    }
    return _Cards(
        by_id=by_id, members=members,
        shown={o: p.shown.id if p.shown else None for o, p in picks.items()},
        retired=frozenset(o for o, opp in opps.items() if opp.retired_at is not None),
        s7=s7, s5=s5,
    )


def _batch_order(
    batch: LandscapeReviewBatch, items: Sequence[LandscapeReviewBatchItem], cards: _Cards
) -> list[LandscapeReviewBatchItem]:
    """A blind batch in its stored shuffled order, a normal one in live card order (D30, D62)."""
    if batch.blind:
        return sorted(items, key=lambda i: (i.position is None, i.position or 0))
    by_opp = {i.opportunity_id: i for i in items}
    return [by_opp[o] for o in cards.order(by_opp)]


# --- the views --------------------------------------------------------------------------------


async def batch_view(db, user) -> dict:
    """The viewer's ``/workspace/landscape`` page (§13.1; L45-L47, D30, D50, D62): this
    week's batch, the viewer's carry-overs and changed cards, and the earlier batches.

    Returns ``{"epoch": {"id", "status", "prompt_set_version", "criteria_version",
    "activated_at"}|None, "banner": str|None, "is_staff": bool, "this_week": batch|None,
    "carry_overs": [card], "changed": [card], "earlier_batches": [batch]}``. A batch is
    ``{"batch_id": str, "issued_on": date, "blind": bool, "superseded": bool, "cards":
    [card]}``; every card is a ``_load_cards`` card plus ``batch_id`` (str), ``issued_on``
    (date), ``blind`` (bool) and, on carry-overs and changed cards, ``kind``
    (``carry_over``/``changed``). Blind-batch cards hide score, band and rank until the
    viewer decides (L53, D56); an expert's own open sample item hides S9's calls (D63). A card
    with no completed assessment in the active epoch says so (``no_assessment``, ``failure``)
    and links the last earlier one (``history_assessment_id``, §7.10)."""
    viewer = await _viewer(db, user)
    ep = await _epochs(db)
    issued = await _issued(db)
    cards = await _load_cards(
        db, viewer, ep, {i.opportunity_id for i in issued.items}, issued.visible_probes()
    )
    grouped: dict[uuid.UUID, list[LandscapeReviewBatchItem]] = defaultdict(list)
    for item in issued.items:
        if item.opportunity_id in cards.by_id:
            grouped[item.batch_id].append(item)
    ordered = {b.id: _batch_order(b, grouped.get(b.id, []), cards) for b in issued.batches}

    def batch_dict(batch: LandscapeReviewBatch) -> dict:
        return {
            "batch_id": _sid(batch.id), "issued_on": batch.issued_on, "blind": batch.blind,
            "superseded": issued.superseded(batch),
            "cards": [_on_batch(cards.by_id[i.opportunity_id], batch) for i in ordered[batch.id]],
        }

    pairs = [(b, i) for b in issued.batches for i in ordered[b.id]]
    extra = viewer_cards(pairs, viewer.decisions.values(), cards.shown, cards.retired, user)
    batches = issued.batch_of()
    extra_cards = [
        {**_on_batch(cards.by_id[c["opportunity_id"]], batches[c["batch_id"]]), "kind": c["kind"]}
        for c in extra
    ]
    return {
        "epoch": ep.summary(),
        "banner": _banner(ep),
        "is_staff": viewer.staff,
        "this_week": batch_dict(issued.batches[0]) if issued.batches else None,
        "carry_overs": [c for c in extra_cards if c["kind"] == "carry_over"],
        "changed": [c for c in extra_cards if c["kind"] == "changed"],
        "earlier_batches": [batch_dict(b) for b in issued.batches[1:]],
    }


def _on_batch(card: dict, batch: LandscapeReviewBatch) -> dict:
    return {**card, "batch_id": _sid(batch.id), "issued_on": batch.issued_on,
            "blind": batch.blind}


def _withheld_blind(issued: _Issued, viewer: _Viewer) -> set[uuid.UUID]:
    """Every card of each blind batch in which the viewer has an undecided card (F2)."""
    by_batch: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for item in issued.items:
        by_batch[item.batch_id].append(item.opportunity_id)
    out: set[uuid.UUID] = set()
    for batch in issued.batches:
        opps = by_batch.get(batch.id, [])
        if batch.blind and any(o not in viewer.decisions for o in opps):
            out.update(opps)
    return out


async def _list_candidates(
    db: AsyncSession, ep: _Epochs, view: str
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """``(opportunities, probes)`` for a staff list: **Current** is every live non-probe
    opportunity whose latest version belongs to the active epoch; **All assessed** every
    opportunity with a ``done`` assessment of an active or retired epoch."""
    V, Opp = LandscapeOpportunityVersion, LandscapeOpportunity
    probes = set((await db.scalars(select(V.opportunity_id).where(V.is_probe.is_(True)))).all())
    if view == "current":
        if ep.active_id is None:
            return set(), probes
        rows = (await db.scalars(
            select(V.opportunity_id).join(Opp, Opp.id == V.opportunity_id).where(
                V.version == Opp.version_counter, V.epoch_id == ep.active_id,
                Opp.retired_at.is_(None), V.is_probe.is_(False),
            )
        )).all()
        return set(rows), probes
    history = [e for e, s in ep.status.items() if s in _HISTORY_EPOCHS]
    A = LandscapeAssessment
    rows = (await db.scalars(
        select(A.opportunity_id).where(
            A.status == "done", A.epoch_id.in_(history), A.opportunity_id.is_not(None)
        ).distinct()
    )).all()
    return set(rows), probes


async def lists_view(
    db, user, *, view: Literal["current", "all"], tab: Literal["unreviewed", "reviewed", "all"],
    assignment: str,
) -> dict:
    """The staff lists, **Current** and **All assessed** (§13.1, L48).

    Staff only: anyone else gets ``PermissionError`` (the page answers 403). ``view`` other
    than ``"all"`` reads as ``"current"``; ``tab`` other than ``unreviewed``/``reviewed`` as
    ``"all"``; ``assignment`` ``"mine"`` keeps the viewer's assigned cards, anything else all.
    Tabs are per viewer: *unreviewed* is no quick decision by this viewer (D60). A viewer with
    an undecided card in a blind batch sees none of that batch's cards (F2), and a probe
    appears only once its blind batch is superseded.

    Returns ``{"view": str, "tab": str, "assignment": "mine"|"all", "epoch": dict|None,
    "banner": str|None, "cut": {"n", "k", "completed_at"}|None, "counts": {"unreviewed":
    int, "reviewed": int, "all": int}, "cards": [card]}``, cards as ``_load_cards`` makes them,
    in card order (D30)."""
    if not is_staff(user):
        raise PermissionError("the landscape lists are for managers and admins (L48)")
    view = "all" if view == "all" else "current"
    tab = tab if tab in ("unreviewed", "reviewed") else "all"
    assignment = "mine" if assignment == "mine" else "all"
    viewer = await _viewer(db, user)
    ep = await _epochs(db)
    issued = await _issued(db)
    candidates, probes = await _list_candidates(db, ep, view)
    visible_probes = issued.visible_probes()
    candidates -= _withheld_blind(issued, viewer)
    candidates -= probes - visible_probes
    if assignment == "mine":
        R = LandscapeReviewAssignment
        candidates &= set((await db.scalars(
            select(R.opportunity_id).where(R.assignee_id == user.id)
        )).all())
    cards = await _load_cards(db, viewer, ep, candidates, visible_probes)
    ordered = [cards.by_id[o] for o in cards.order(cards.by_id)]
    reviewed = [c for c in ordered if c["my_decision"] is not None]
    unreviewed = [c for c in ordered if c["my_decision"] is None]
    return {
        "view": view, "tab": tab, "assignment": assignment,
        "epoch": ep.summary(), "banner": _banner(ep), "cut": await _cut_summary(db, ep),
        "counts": {"unreviewed": len(unreviewed), "reviewed": len(reviewed), "all": len(ordered)},
        "cards": {"unreviewed": unreviewed, "reviewed": reviewed}.get(tab, ordered),
    }


async def _cut_summary(db: AsyncSession, ep: _Epochs) -> dict | None:
    """The active epoch's last complete cut: N, K and when it completed."""
    if ep.active_id is None:
        return None
    cut = await _last_complete_cut(db, ep.active_id)
    return {"n": cut.n, "k": cut.k, "completed_at": cut.completed_at} if cut else None


async def expert_view(db, user) -> dict:
    """The viewer's open expert-sample items this quarter (L55, D63), whether or not issued.

    Returns ``{"quarter": "YYYY-Qn", "is_expert": bool, "items": [card]}``; each card is a
    ``_load_cards`` card (S9's calls hidden) plus ``judgment_id`` (str), ``drawn_at``
    (datetime) and ``issued_before_draw`` (bool|None)."""
    viewer = await _viewer(db, user)
    ep = await _epochs(db)
    issued = await _issued(db)
    experts = {u.id for u in await review_batches._experts(db)}
    cards = await _load_cards(db, viewer, ep, viewer.expert, issued.visible_probes())
    items = [
        {**cards.by_id[o], "judgment_id": _sid(j.id), "drawn_at": j.drawn_at,
         "issued_before_draw": j.issued_before_draw}
        for o, j in sorted(viewer.expert.items(), key=lambda kv: (kv[1].drawn_at, str(kv[0])))
        if o in cards.by_id
    ]
    return {"quarter": _quarter(datetime.now(UTC)), "is_expert": user.id in experts,
            "items": items}


# --- the detail page --------------------------------------------------------------------------


def _consult_dict(row: LandscapeConsult, *, admin: bool, label: str | None) -> dict:
    """A panel opinion in the dict shape ``derive_strengths_and_risks`` reads."""
    opinion = row.opinion if isinstance(row.opinion, dict) else {}
    concerns = [c for c in opinion.get("concerns") or [] if isinstance(c, str)]
    established = opinion.get("established")
    if not opinion:
        read_state = None
    else:
        read_state = "defaulted" if opinion.get("signal_was_defaulted") else "parsed"
    return {
        "domain": f"{row.domain} · {label}" if label else row.domain,
        "verdict_signal": opinion.get("verdict_signal"),
        "confidence": opinion.get("confidence"),
        "concerns": concerns,
        "concern_count": len(concerns),
        "questions_to_ask": [
            q for q in opinion.get("questions_to_ask") or [] if isinstance(q, str)
        ],
        "established": list(established) if isinstance(established, list) else None,
        "reply_truncated": False,
        "read_state": read_state,
        "raw_opinion": row.raw_text if admin else None,
        "served_by_model": row.served_by_model,
        "created_at": row.created_at,
    }


async def _sources(db: AsyncSession, idea_id: uuid.UUID) -> tuple[list[dict], list[dict]]:
    """The idea's documents (§13.2 Sources) and the labs behind them, with links."""
    E, X, Doc = LandscapeIdeaEvidence, LandscapeExtraction, LandscapeDocument
    docs = (await db.scalars(
        select(Doc).join(X, X.document_id == Doc.id).join(E, E.extraction_id == X.id)
        .where(E.idea_id == idea_id, Doc.merged_into.is_(None)).distinct()
    )).all()
    docs = sorted(docs, key=lambda d: (d.published_on or date.min, str(d.id)), reverse=True)
    texts = {t.document_id: t for t in (await db.scalars(
        select(LandscapeText).where(LandscapeText.document_id.in_([d.id for d in docs]))
    )).all()} if docs else {}
    lab_keys = sorted({d.lab_key for d in docs if d.lab_key})
    labs = (await db.scalars(
        select(LandscapeLab).where(LandscapeLab.lab_key.in_(lab_keys))
    )).all() if lab_keys else []
    sources = [
        {
            "document_id": _sid(d.id), "kind": d.kind, "document_type": d.document_type,
            "title": d.title, "published_on": d.published_on, "doi": d.doi, "pmid": d.pmid,
            "role_basis": d.role_basis, "institution": d.qualifying_institution,
            "license": (texts[d.id].license if d.id in texts else None) or d.license,
            "text_basis": texts[d.id].text_basis if d.id in texts else None,
            "lab_key": d.lab_key,
        }
        for d in docs
    ]
    lab_rows = [
        {
            "lab_key": lab.lab_key, "name": lab.display_name or lab.lab_key,
            "orcid_url": f"https://orcid.org/{lab.orcid}" if lab.orcid else None,
            "openalex_url": (
                f"https://openalex.org/{lab.openalex_author_id.rsplit('/', 1)[-1]}"
                if lab.openalex_author_id else None
            ),
        }
        for lab in labs
    ]
    return sources, lab_rows


async def _member(
    db: AsyncSession, idea: LandscapeIdea, hiding: Hiding, *, admin: bool, label: str | None,
) -> tuple[dict, list[dict]]:
    """One member idea's section (§13.2) and its panel opinions for the evidence summary."""
    D = LandscapeDeep
    deep = await db.scalar(
        select(D).where(D.epoch_id == idea.epoch_id, D.idea_id == idea.id)
        .order_by(D.created_at.desc(), D.id.desc()).limit(1)
    )
    consults: list[dict] = []
    if deep is not None and not hiding.expert:
        consults = [
            _consult_dict(row, admin=admin, label=label)
            for row in (await db.scalars(
                select(LandscapeConsult).where(LandscapeConsult.deep_id == deep.id)
                .order_by(LandscapeConsult.created_at, LandscapeConsult.domain)
            )).all()
        ]
    sources, labs = await _sources(db, idea.id)
    deep_values = hide_deep(_row_values(deep), hiding) if deep is not None else None
    if deep_values is not None:
        # A probe's S6 row has no cut, so ``cut_id`` would flag it before its batch is
        # superseded (F2).
        deep_values.pop("cut_id", None)
        if not admin:
            deep_values.pop("search_transcript", None)
    section = {
        "idea_id": _sid(idea.id), "idea_key": idea.idea_key, "idea_type": idea.idea_type,
        "structured_line": structured_line(idea), "direction": idea.direction,
        "targets": idea.targets, "agent": idea.agent, "disease": idea.disease,
        "modality_variants": idea.modality_variants, "stage": idea.stage,
        "evidence_grade": idea.evidence_grade, "jhu_group_count": idea.jhu_group_count,
        "outside_group_count": idea.outside_group_count,
        "evidence_summary": idea.evidence_summary, "data_flags": idea.data_flags,
        "deep": deep_values, "consults": consults, "sources": sources, "labs": labs,
    }
    return section, consults


async def _members(
    db: AsyncSession, version: LandscapeOpportunityVersion | None, hiding: Hiding, *, admin: bool
) -> tuple[list[dict], list[dict]]:
    if version is None:
        return [], []
    keys = list(version.member_idea_keys or [])
    idea_ids = await _idea_ids(db, {(version.epoch_id, k) for k in keys})
    ordered = list(dict.fromkeys(idea_ids[(version.epoch_id, k)] for k in keys
                                 if (version.epoch_id, k) in idea_ids))
    ideas = {i.id: i for i in (await db.scalars(
        select(LandscapeIdea).where(LandscapeIdea.id.in_(ordered))
    )).all()} if ordered else {}
    sections: list[dict] = []
    consults: list[dict] = []
    for n, idea_id in enumerate(ordered, start=1):
        label = f"idea {n}" if len(ordered) > 1 else None
        section, opinions = await _member(db, ideas[idea_id], hiding, admin=admin, label=label)
        sections.append(section)
        consults.extend(opinions)
    return sections, consults


def _key_points(values: Mapping) -> list[dict]:
    """The key-point groups with each bullet's sourced-or-estimate badge (``claim_support``,
    L30): ``[{"label", "group", "bullets": [{"text", "support": list[str] | "estimate"}]}]``."""
    key_points = values.get("key_points")
    support = values.get("claim_support")
    support = support if isinstance(support, dict) else {}
    groups: dict[tuple, str] = {}
    if isinstance(key_points, dict):
        for group, bullets in key_points.items():
            if isinstance(bullets, list):
                clean = tuple(b.strip() for b in bullets if isinstance(b, str) and b.strip())
                groups.setdefault(clean, group)
    out = []
    for section in key_point_sections(key_points):
        group = groups.get(tuple(section.points))
        refs = support.get(group) if group else None
        refs = refs if isinstance(refs, list) else []
        out.append({
            "label": section.label, "group": group,
            "bullets": [
                {"text": text, "support": refs[n] if n < len(refs) else "estimate"}
                for n, text in enumerate(section.points)
            ],
        })
    return out


def _verdict(values: dict | None, hiding: Hiding, consults: list[dict]) -> dict:
    """Dimension rows, gate definitions and the evidence summary, from what is not hidden."""
    if values is None:
        return {"dimensions": [], "revision_provenance": None, "scale_max": None,
                "gating_definitions": {}, "verdict_signals": None, "key_points": []}
    revision, provenance = resolve_revision(
        values.get("rubric_version"), values.get("rubric_content_hash")
    )
    subject = SimpleNamespace(**values)
    scored = not (hiding.blind or hiding.expert)
    dimensions = build_dimension_rows(subject, revision) if scored else []
    return {
        "dimensions": dimensions,
        "revision_provenance": provenance,
        "scale_max": revision.scale_max if revision is not None else None,
        "gating_definitions": _gating_definitions(revision, provenance),
        "verdict_signals": derive_strengths_and_risks(
            subject, dimensions=dimensions, consults=consults, revision=revision,
            revision_provenance=provenance,
        ),
        "key_points": _key_points(values),
    }


async def _votes(db: AsyncSession, user: Any, opportunity_id: uuid.UUID) -> dict:
    D, R, E = LandscapeDecision, LandscapeReview, LandscapeReviewEvent
    decisions, reviews, events = visible_votes(
        user, opportunity_id,
        (await db.scalars(select(D).where(D.opportunity_id == opportunity_id)
                          .order_by(D.created_at, D.id))).all(),
        (await db.scalars(select(R).where(R.opportunity_id == opportunity_id)
                          .order_by(R.created_at, R.id))).all(),
        (await db.scalars(select(E).where(E.opportunity_id == opportunity_id)
                          .order_by(E.created_at, E.id))).all(),
    )
    unlocked = any(d.reviewer_id == user.id for d in decisions)
    events_out = [_event_dict(e, user) for e in events]
    return {
        "votes_unlocked": unlocked,
        "decisions": [_decision_dict(d, user) for d in decisions],
        "reviews": [_review_dict(r, user) for r in reviews],
        "status_events": events_out,
        "review_status": events_out[-1] if events_out else None,
    }


async def _assignments(db: AsyncSession, opportunity_id: uuid.UUID, staff: bool) -> dict:
    R = LandscapeReviewAssignment
    rows = (await db.scalars(
        select(R).where(R.opportunity_id == opportunity_id).order_by(R.created_at, R.id)
    )).all()
    users: list[dict] = []
    if staff:
        users = [
            {"id": _sid(u.id), "name": u.name, "user_role": u.user_role}
            for u in (await db.scalars(
                select(User).where(User.user_role.in_(_REVIEW_ROLES)).order_by(User.name)
            )).all()
        ]
    return {
        "assignments": [
            {"id": _sid(a.id), "assignee_id": _sid(a.assignee_id),
             "assignee_name": a.assignee_name, "assigned_by_name": a.assigned_by_name,
             "created_at": a.created_at}
            for a in rows
        ],
        "review_capable_users": users,
    }


def _history(rows: Sequence[LandscapeAssessment], ep: _Epochs, display_id) -> list[dict]:
    """Older versions and retired epochs' assessments, newest first; never a candidate's."""
    return [
        {
            "assessment_id": _sid(a.id), "epoch_id": _sid(a.epoch_id),
            "epoch_status": ep.status.get(a.epoch_id), "opportunity_version": a.opportunity_version,
            "status": a.status, "headline": a.headline, "created_at": a.created_at,
        }
        for a in sorted(rows, key=lambda a: (a.created_at, str(a.id)), reverse=True)
        if a.id != display_id and ep.status.get(a.epoch_id) in _HISTORY_EPOCHS
    ]


async def detail_view(db, user, opportunity_id: uuid.UUID) -> dict | None:
    """The detail page (§13.2): the opportunity's current assessment IN THE ACTIVE EPOCH,
    its member idea sections and its history; None (the route answers 404) unless
    ``can_view``.

    Returns ``{"opportunity_id": str, "label": str|None, "theme": str|None, "retired": bool,
    "is_history": bool, "probe": bool, "blind": bool, "hiding": {"blind": bool, "expert":
    bool}, "epoch": dict|None, "banner": str|None, "assessment": dict|None, "no_assessment":
    bool, "failure": {"status", "error"}|None, "history_assessment_id": str|None, "history":
    [{"assessment_id", "epoch_id", "epoch_status", "opportunity_version", "status",
    "headline", "created_at"}], "members": [member], "dimensions": [row],
    "revision_provenance": str|None, "scale_max": float|None, "gating_definitions": {key:
    {"title", "description"}}, "verdict_signals": dict|None, "key_points": [{"label",
    "group", "bullets": [{"text", "support"}]}], "my_decision": dict|None,
    "changed_since_decision": bool, "votes_unlocked": bool, "decisions": [dict], "reviews":
    [dict], "status_events": [dict], "review_status": dict|None, "assignments": [dict],
    "review_capable_users": [{"id", "name", "user_role"}] (staff only, else []),
    "expert_item": {"judgment_id", "quarter", "drawn_at"}|None, "is_staff": bool,
    "is_admin": bool}``.

    ``assessment`` holds the shown ``LandscapeAssessment`` columns, ids as strings, without
    what the viewer's hiding hides, without ``raw_verdict`` unless the viewer is an admin, and
    without the staff-only verdict fields for a reviewer. A member is the idea's facets
    (``idea_id``, ``idea_key``, ``idea_type``, ``structured_line``, ``direction``,
    ``targets``, ``agent``, ``disease``, ``modality_variants``, ``stage``,
    ``evidence_grade``, ``jhu_group_count``, ``outside_group_count``, ``evidence_summary``,
    ``data_flags``), ``deep`` (its newest S6 row's columns after ``hide_deep``, or None),
    ``consults`` (panel opinions; [] on an expert's open item), ``sources`` and ``labs``.
    A blind-batch card hides what ``batch_view`` hides until the viewer decides; an expert's
    own sample item hides S9's and S6's calls until they submit (D63). A retired
    opportunity reads as history: its last assessment, ``retired`` and ``is_history`` set
    (D62)."""
    if not await can_view(db, user, opportunity_id):
        return None
    viewer = await _viewer(db, user, opportunity_id)
    hiding = viewer.hiding(opportunity_id)
    ep = await _epochs(db)
    issued = await _issued(db)
    opp = await db.get(LandscapeOpportunity, opportunity_id)
    rows = (await _assessments(db, [opportunity_id])).get(opportunity_id, [])
    retired = opp.retired_at is not None
    pick = _pick(rows, ep, retired)
    version = _card_version(opp, (await _versions(db, [opportunity_id])).get(opportunity_id, {}),
                            pick.display)
    values = hide_assessment(_row_values(pick.display), hiding) if pick.display else None
    if values is not None and not viewer.admin:
        values.pop("raw_verdict", None)
    members, consults = await _members(db, version, hiding, admin=viewer.admin)
    decision = viewer.decisions.get(opportunity_id)
    judgment = viewer.expert.get(opportunity_id)
    shown_id = pick.shown.id if pick.shown is not None else None
    no_assessment = pick.shown is None and not retired
    batches = issued.batch_of()
    on_blind = any(batches[i.batch_id].blind for i in issued.items
                   if i.opportunity_id == opportunity_id)
    view = {
        "opportunity_id": _sid(opportunity_id),
        "label": version.label if version else None,
        "theme": version.theme if version else None,
        "retired": retired, "is_history": retired,
        "probe": opportunity_id in issued.visible_probes(),
        "blind": on_blind,
        "hiding": {"blind": hiding.blind, "expert": hiding.expert},
        "epoch": ep.summary(), "banner": _banner(ep),
        "assessment": values,
        "no_assessment": no_assessment,
        "failure": _failure(pick) if no_assessment else None,
        "history_assessment_id": _sid(pick.history.id) if pick.history else None,
        "history": _history(rows, ep, pick.display.id if pick.display else None),
        "members": members,
        **_verdict(values, hiding, consults),
        "my_decision": _decision_dict(decision, user) if decision else None,
        "changed_since_decision": not retired and changed_since_decision(decision, shown_id),
        **await _votes(db, user, opportunity_id),
        **await _assignments(db, opportunity_id, viewer.staff),
        "expert_item": (
            {"judgment_id": _sid(judgment.id), "quarter": judgment.quarter,
             "drawn_at": judgment.drawn_at}
            if judgment is not None else None
        ),
        "is_staff": viewer.staff, "is_admin": viewer.admin,
    }
    return strip_staff_only(view, user)


# --- the pipeline page ------------------------------------------------------------------------


async def _latest_report(db: AsyncSession, kind: str) -> LandscapeReport | None:
    return await db.scalar(
        select(LandscapeReport).where(LandscapeReport.kind == kind)
        .order_by(LandscapeReport.created_at.desc()).limit(1)
    )


def _report_dict(row: LandscapeReport | None) -> dict | None:
    if row is None:
        return None
    return {"id": _sid(row.id), "created_at": row.created_at, "payload": row.payload}


async def _epoch_rows(db: AsyncSession) -> list[dict]:
    """Every epoch, newest first, with its hold, override and regression run (§7.11, §10.4)."""
    E, R = LandscapeEpoch, LandscapeRegressionRun
    epochs_ = (await db.scalars(select(E).order_by(E.created_at.desc(), E.id))).all()
    run_ids = [e.regression_run_id for e in epochs_ if e.regression_run_id]
    runs = {r.id: r for r in (await db.scalars(
        select(R).where(R.id.in_(run_ids))
    )).all()} if run_ids else {}
    out = []
    for e in epochs_:
        run = runs.get(e.regression_run_id)
        out.append({
            "id": _sid(e.id), "status": e.status, "created_at": e.created_at,
            "prompt_set_version": e.prompt_set_version, "criteria_version": e.criteria_version,
            "changed_stages": list(e.changed_stages or []), "hold_reason": e.hold_reason,
            "override_actor_id": _sid(e.override_actor_id), "override_reason": e.override_reason,
            "override_at": e.override_at, "drained_at": e.drained_at,
            "activated_at": e.activated_at, "retired_at": e.retired_at,
            "regression_run": {
                "id": _sid(run.id), "status": run.status, "set_version": run.set_version,
                "started_at": run.started_at, "finished_at": run.finished_at,
                "results": run.results,
            } if run is not None else None,
        })
    return out


async def _blind_batches(db: AsyncSession, issued: _Issued) -> list[dict]:
    """Superseded blind batches with each card's probe flag (F2), newest first."""
    batches = [b for b in issued.batches if b.blind and issued.superseded(b)]
    if not batches:
        return []
    items = [i for i in issued.items if i.batch_id in {b.id for b in batches}]
    V, Opp = LandscapeOpportunityVersion, LandscapeOpportunity
    labels = dict((await db.execute(
        select(V.opportunity_id, V.label).join(Opp, Opp.id == V.opportunity_id)
        .where(V.version == Opp.version_counter,
               V.opportunity_id.in_([i.opportunity_id for i in items]))
    )).all())
    return [
        {
            "batch_id": _sid(b.id), "issued_on": b.issued_on,
            "cards": [
                {"opportunity_id": _sid(i.opportunity_id), "label": labels.get(i.opportunity_id),
                 "probe": i.is_probe, "position": i.position}
                for i in sorted((i for i in items if i.batch_id == b.id),
                                key=lambda i: (i.position is None, i.position or 0))
            ],
        }
        for b in batches
    ]


#: Rows the pipeline page lists per filtered stage and for each other list, newest first.
_PIPELINE_ROWS = 50


async def _judging_epoch(db: AsyncSession) -> LandscapeEpoch | None:
    """The epoch the pipeline lists read: the active one, else the newest unactivated one."""
    return await db.scalar(
        select(LandscapeEpoch).where(LandscapeEpoch.status.in_(("active", *_UNACTIVATED)))
        .order_by((LandscapeEpoch.status == "active").desc(), LandscapeEpoch.created_at.desc())
        .limit(1)
    )


def _fetch_dict(row: LandscapeFetch) -> dict:
    return {
        "source": row.source, "slice_key": row.slice_key, "status": row.status,
        "started_at": row.started_at, "finished_at": row.finished_at,
        "expected_count": row.expected_count, "received_count": row.received_count,
        "error": row.error,
    }


async def _source_rows(db: AsyncSession) -> list[dict]:
    """Per source: enabled, last success, completeness ratio, next due and its newest fetch
    (§13.4: per-source completeness and last fetch)."""
    F = LandscapeFetch
    newest = {
        row.source: row for row in (await db.scalars(
            select(F).distinct(F.source).order_by(F.source, F.started_at.desc())
        )).all()
    }
    return [
        {
            "source": src.source, "enabled": src.enabled, "last_success_at": src.last_success_at,
            "completeness": src.last_completeness_ratio, "next_due_at": src.next_due_at,
            "last_fetch": _fetch_dict(newest[src.source]) if src.source in newest else None,
        }
        for src in (await db.scalars(
            select(LandscapeSource).order_by(LandscapeSource.source)
        )).all()
    ]


async def _incomplete_slices(db: AsyncSession) -> list[dict]:
    """Slices whose newest fetch ended ``incomplete`` or ``failed``, newest first (§13.4)."""
    F = LandscapeFetch
    latest = (await db.scalars(
        select(F).distinct(F.source, F.slice_key)
        .order_by(F.source, F.slice_key, F.started_at.desc())
    )).all()
    ended = [row for row in latest if row.status in ("incomplete", "failed")]
    ended.sort(key=lambda row: row.started_at, reverse=True)
    return [_fetch_dict(row) for row in ended[:_PIPELINE_ROWS]]


def _rescue_fields(item_kind: str, row: Any, stage: str) -> dict:
    """What the rescue form posts for one excluded stage row (``rescue.rescue``, L43)."""
    return {"item_kind": item_kind, "item_id": _sid(row.id), "stage": stage,
            "expected_status": row.status}


def _excluded_reason(status: str) -> str:
    return status.split(":", 1)[1] if ":" in status else status


async def _filtered_screen(db: AsyncSession, epoch_id: uuid.UUID) -> dict:
    S, D = LandscapeScreen, LandscapeDocument
    where = (S.epoch_id == epoch_id, S.status == "excluded:screen")
    rows = (await db.execute(
        select(S, D.title).join(D, D.id == S.document_id).where(*where)
        .order_by(S.updated_at.desc(), S.id).limit(_PIPELINE_ROWS)
    )).all()
    return {
        "count": await db.scalar(select(func.count()).select_from(S).where(*where)) or 0,
        "items": [
            {**_rescue_fields("screen", row, "screen"), "label": title,
             "reason": row.reason or _excluded_reason(row.status), "at": row.updated_at}
            for row, title in rows
        ],
    }


async def _filtered_ideas(
    db: AsyncSession, epoch_id: uuid.UUID, model: Any, where: Any, item_kind: str, stage: str,
    reason: Callable[[Any], str | None],
) -> dict:
    """Excluded S4 or S6 rows at their idea's current version, with the idea's line."""
    Idea = LandscapeIdea
    clauses = (model.epoch_id == epoch_id, where, model.idea_version == Idea.version_hash)
    rows = (await db.execute(
        select(model, Idea).join(Idea, Idea.id == model.idea_id).where(*clauses)
        .order_by(model.updated_at.desc(), model.id).limit(_PIPELINE_ROWS)
    )).all()
    count = await db.scalar(
        select(func.count()).select_from(model).join(Idea, Idea.id == model.idea_id)
        .where(*clauses)
    )
    return {
        "count": count or 0,
        "items": [
            {**_rescue_fields(item_kind, row, stage), "label": structured_line(idea),
             "reason": reason(row), "at": row.updated_at}
            for row, idea in rows
        ],
    }


def _deep_gate_reason(row: LandscapeDeep) -> str:
    failed = sorted(k for k, v in (row.gating or {}).items() if v == "not_met")
    return "Gates not met: " + ", ".join(failed) if failed else _excluded_reason(row.status)


async def _filtered_items(db: AsyncSession, epoch: LandscapeEpoch | None) -> dict:
    """The filtered-items browser (§13.4, §10.5): per stage, how many rows that stage excluded
    in the judging epoch and the newest of them, each with its reason and the fields the
    rescue form posts. S4 and S6 rows count only at their idea's current version."""
    if epoch is None:
        return {}
    return {
        "screen": await _filtered_screen(db, epoch.id),
        "triage": await _filtered_ideas(
            db, epoch.id, LandscapeTriage, LandscapeTriage.status.startswith("excluded:"),
            "triage", "triage",
            lambda row: row.exclusion_reason or _excluded_reason(row.status),
        ),
        "deep_gate": await _filtered_ideas(
            db, epoch.id, LandscapeDeep, LandscapeDeep.status == "excluded:deep_gate",
            "deep", "deep_gate", _deep_gate_reason,
        ),
    }


async def _audit_disagreements(db: AsyncSession, epoch: LandscapeEpoch | None) -> list[dict]:
    """The reject audit's disagreements in the judging epoch, newest first (§10.5, §13.4). A
    row still excluded carries the rescue form's fields; ``rescue`` is None otherwise."""
    if epoch is None:
        return []
    A = LandscapeRejectAudit
    rows = (await db.scalars(
        select(A).where(A.epoch_id == epoch.id, A.agree.is_(False))
        .order_by(A.updated_at.desc(), A.id).limit(_PIPELINE_ROWS)
    )).all()
    kinds = {"screen": (LandscapeScreen, "screen"), "triage": (LandscapeTriage, "triage")}
    out = []
    for row in rows:
        model, stage = kinds.get(row.item_kind, (None, None))
        target = await db.get(model, row.item_id) if model is not None else None
        rescuable = target is not None and str(target.status).startswith("excluded:")
        out.append({
            "item_kind": row.item_kind, "item_id": _sid(row.item_id),
            "original_decision": row.original_decision, "audit_decision": row.audit_decision,
            "reason": row.reason, "at": row.updated_at,
            "rescue": _rescue_fields(row.item_kind, target, stage) if rescuable else None,
        })
    return out


async def pipeline_view(db, user) -> dict:
    """The staff pipeline page (§13.4); anyone else gets ``PermissionError`` (403).

    Returns ``{"phase": "pilot"|"backfill"|"continuous" (unset reads ``pilot``), "paused":
    bool, "epochs": [{"id", "status", "created_at", "prompt_set_version",
    "criteria_version", "changed_stages", "hold_reason", "override_actor_id",
    "override_reason", "override_at", "drained_at", "activated_at", "retired_at",
    "regression_run": {"id", "status", "set_version", "started_at", "finished_at",
    "results"}|None}], "metrics": reports.pipeline_metrics, "drift": reports.drift_report,
    "positive_control_trace": reports.positive_control_trace, "reports": {"pilot", "bakeoff",
    "probes": {"id", "created_at", "payload"}|None}, "picks_applied": bool,
    "positive_controls": [str] (the stored DOIs and PMIDs), "experts": [{"id", "name",
    "user_role"}], "review_users": [{"id", "name", "user_role", "expert": bool}],
    "blind_batches": [{"batch_id", "issued_on", "cards": [{"opportunity_id", "label",
    "probe", "position"}]}] (superseded blind batches only, F2), "no_assessment_cards":
    [card], "sources": [{"source", "enabled", "last_success_at", "completeness",
    "next_due_at", "last_fetch": fetch|None}], "incomplete_slices": [fetch], "filtered":
    {"screen"|"triage"|"deep_gate": {"count", "items": [{"item_kind", "item_id", "stage",
    "expected_status", "label", "reason", "at"}]}} (empty without an epoch),
    "audit_disagreements": [{"item_kind", "item_id", "original_decision", "audit_decision",
    "reason", "at", "rescue": {"item_kind", "item_id", "stage", "expected_status"}|None}],
    "is_admin": bool}``, where a fetch is ``{"source", "slice_key", "status", "started_at",
    "finished_at", "expected_count", "received_count", "error"}``. The filtered lists, the
    audit disagreements and the incomplete slices hold the newest ``_PIPELINE_ROWS`` each; a
    filtered item's ``item_kind``, ``item_id``, ``stage`` and ``expected_status`` are what
    the rescue form posts to ``/landscape/rescue``. ``no_assessment_cards`` are the live
    issued cards with no completed assessment in the active epoch (§7.10), as ``_load_cards``
    makes them. The metrics dicts are JSON values with keys that vary by data (days, stages,
    sources)."""
    if not is_staff(user):
        raise PermissionError("the landscape pipeline page is for managers and admins")
    viewer = await _viewer(db, user)
    ep = await _epochs(db)
    issued = await _issued(db)
    bakeoff = await _latest_report(db, "bakeoff")
    judging = await _judging_epoch(db)
    experts = await review_batches._experts(db)
    expert_ids = {u.id for u in experts}
    cards = await _load_cards(
        db, viewer, ep, {i.opportunity_id for i in issued.items}, issued.visible_probes()
    )
    review_users = (await db.scalars(
        select(User).where(User.user_role.in_(_REVIEW_ROLES)).order_by(User.name)
    )).all()
    return {
        "phase": await _setting(db, "landscape_phase") or "pilot",
        "paused": (await _setting(db, "landscape_paused") or "").lower() in _TRUE_WORDS,
        "epochs": await _epoch_rows(db),
        "metrics": await reports.pipeline_metrics(db),
        "drift": await reports.drift_report(db),
        "positive_control_trace": await reports.positive_control_trace(db),
        "reports": {
            "pilot": _report_dict(await _latest_report(db, "pilot")),
            "bakeoff": _report_dict(bakeoff),
            "probes": _report_dict(await _latest_report(db, "probes")),
        },
        "picks_applied": picks_applied(bakeoff),
        "positive_controls": [
            value for _, value in _parse_controls(
                await db.scalar(select(AppSetting.value)
                                .where(AppSetting.key == "landscape_positive_controls"))
            )
        ],
        "experts": [{"id": _sid(u.id), "name": u.name, "user_role": u.user_role} for u in experts],
        "review_users": [
            {"id": _sid(u.id), "name": u.name, "user_role": u.user_role,
             "expert": u.id in expert_ids}
            for u in review_users
        ],
        "blind_batches": await _blind_batches(db, issued),
        "no_assessment_cards": [
            cards.by_id[o] for o in cards.order(cards.by_id)
            if cards.by_id[o]["no_assessment"]
        ],
        "sources": await _source_rows(db),
        "incomplete_slices": await _incomplete_slices(db),
        "filtered": await _filtered_items(db, judging),
        "audit_disagreements": await _audit_disagreements(db, judging),
        "is_admin": viewer.admin,
    }
