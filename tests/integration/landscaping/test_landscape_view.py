"""The landscaping read model against Postgres (spec §13.1-§13.4, §7.10, §10.6; L40, L48,
L50, L51, L53, D50, D56, D60, D62, D63, F2).

Every row is written directly in one rolled-back session; every name is invented.
"""
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.models.landscape import (
    LandscapeAssessment,
    LandscapeCut,
    LandscapeDecision,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeFetch,
    LandscapeIdea,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRanking,
    LandscapeRejectAudit,
    LandscapeReview,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeScreen,
    LandscapeSource,
    LandscapeTriage,
)
from src.models.user import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from src.services import landscape_view as view
from tests.factories import make_user

pytestmark = pytest.mark.integration

_counter = itertools.count(1)
WEEK_1 = date(2026, 11, 2)
STAFF_SENTINEL = "STAFF-ONLY-SENTINEL"


@dataclass(frozen=True)
class _Card:
    opp: LandscapeOpportunity
    idea: LandscapeIdea
    assessment: LandscapeAssessment | None


def _quarter() -> str:
    return view._quarter(datetime.now(UTC))


async def _epoch(db, status: str = "active") -> LandscapeEpoch:
    epoch = LandscapeEpoch(id=uuid.uuid4(), status=status)
    db.add(epoch)
    await db.flush()
    return epoch


async def _assess(db, epoch, opp_id, *, version: int = 1, status: str = "done", **values):
    row = LandscapeAssessment(
        id=uuid.uuid4(), epoch_id=epoch.id, opportunity_id=opp_id, opportunity_version=version,
        status=status, **values,
    )
    db.add(row)
    await db.flush()
    return row


def _verdict(**overrides) -> dict:
    values = dict(
        headline="An invented headline",
        elevator_pitch="First invented sentence. Second invented sentence. Third one.",
        weighted_score=4.1, band="advance",
        scores={"scientific_merit": 4, "unmet_need": 5},
        score_rationale="Invented score rationale.",
        dimension_rationales={"scientific_merit": "Invented dimension reason."},
        gating={"novelty": "met"}, gating_rationales={"novelty": "Invented gate reason."},
        recommendation="advance", rationale="Invented rationale.",
        key_points={"why_it_matters": ["An invented point."]},
        claim_support={"why_it_matters": ["doc:invented"]},
        structured_lines=["Inhibit INVLV · invented fibrosis · small molecule"],
    )
    values.update(overrides)
    return values


async def _card(
    db, epoch, *, probe: bool = False, status: str | None = "done", rank: int | None = None,
    label: str = "Invented programme", **values,
) -> _Card:
    """One single-idea opportunity with an S6 record and, unless ``status`` is None, an S9
    assessment of that status in ``epoch``."""
    n = next(_counter)
    idea = LandscapeIdea(
        id=uuid.uuid4(), epoch_id=epoch.id, idea_key=f"biology;invented-lv-{n}",
        idea_type="biology", direction="inhibit",
        targets=[{"hgnc_id": "HGNC:90002", "symbol": "INVLV"}],
        disease={"mondo_id": "MONDO:9999998", "label": "invented fibrosis"}, version_hash="v1",
    )
    opp = LandscapeOpportunity(id=uuid.uuid4(), version_counter=1)
    db.add_all([idea, opp])
    await db.flush()
    db.add(LandscapeOpportunityVersion(
        opportunity_id=opp.id, version=1, epoch_id=epoch.id, member_idea_keys=[idea.idea_key],
        label=label, status="done", is_probe=probe,
    ))
    db.add(LandscapeDeep(
        idea_id=idea.id, idea_version="v1", epoch_id=epoch.id, status="done",
        gating={"novelty": "met"}, flags_reviewed={"target_essential": "cleared"},
        provisional_scores={"scientific_merit": 4}, provisional_weighted_score=3.9,
        dimension_rationales={"scientific_merit": "Invented S6 reason."},
        summary="Invented S6 summary.",
    ))
    if rank is not None:
        db.add(LandscapeRanking(epoch_id=epoch.id, idea_id=idea.id, position=rank))
    await db.flush()
    assessment = None
    if status is not None:
        assessment = await _assess(db, epoch, opp.id, status=status, **(_verdict() | values))
    return _Card(opp, idea, assessment)


async def _batch(db, issued_on: date, cards, *, blind: bool = False, probes=()) -> uuid.UUID:
    batch = LandscapeReviewBatch(id=uuid.uuid4(), issued_on=issued_on, blind=blind)
    db.add(batch)
    await db.flush()
    db.add_all(
        LandscapeReviewBatchItem(
            batch_id=batch.id, opportunity_id=card.opp.id, is_probe=card in probes,
            position=n if blind else None,
        )
        for n, card in enumerate(cards, start=1)
    )
    await db.flush()
    return batch.id


async def _decide(db, user, card: _Card, decision: str = "pursue", reason: str = "") -> None:
    db.add(LandscapeDecision(
        opportunity_id=card.opp.id, reviewer_id=user.id, reviewer_name=user.name,
        decision=decision, reason=reason,
        landscape_assessment_id=card.assessment.id if card.assessment else None,
    ))
    await db.flush()


async def _review(db, user, card: _Card, comment: str = "Invented comment.") -> None:
    db.add(LandscapeReview(
        opportunity_id=card.opp.id, reviewer_id=user.id, reviewer_name=user.name, score=3,
        comment=comment,
    ))
    await db.flush()


async def _judgment(db, user, card: _Card) -> LandscapeExpertJudgment:
    row = LandscapeExpertJudgment(
        id=uuid.uuid4(), opportunity_id=card.opp.id, expert_id=user.id, expert_name=user.name,
        quarter=_quarter(),
        landscape_assessment_id=card.assessment.id if card.assessment else None,
    )
    db.add(row)
    await db.flush()
    return row


def _ids(cards: list[dict]) -> list[str]:
    return [c["opportunity_id"] for c in cards]


# --- independent votes ------------------------------------------------------------------------


def _without_assign_list(view_dict: dict) -> str:
    """The view's repr without the staff assign form's ``review_capable_users``: that list
    names every user who may review, decided or not, so it says nothing about votes."""
    return repr({k: v for k, v in view_dict.items() if k != "review_capable_users"})


@pytest.mark.parametrize("role", [USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_votes_hidden_until_own_decision(db_session, role):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card])
    other = await make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Invented Other")
    await _decide(db_session, other, card, "pass", reason="OTHER-REASON-SENTINEL")
    await _review(db_session, other, card, comment="OTHER-COMMENT-SENTINEL")
    viewer = await make_user(db_session, user_role=role)

    before = await view.detail_view(db_session, viewer, card.opp.id)
    assert before["decisions"] == [] and before["reviews"] == []
    assert before["status_events"] == [] and before["votes_unlocked"] is False
    assert "SENTINEL" not in repr(before)
    assert "Invented Other" not in _without_assign_list(before)

    await _decide(db_session, viewer, card)
    after = await view.detail_view(db_session, viewer, card.opp.id)
    assert after["votes_unlocked"] is True
    assert sorted(d["decision"] for d in after["decisions"]) == ["pass", "pursue"]
    assert [r["comment"] for r in after["reviews"]] == ["OTHER-COMMENT-SENTINEL"]


async def test_counts_hidden_too(db_session):
    epoch = await _epoch(db_session)
    cards = [await _card(db_session, epoch) for _ in range(2)]
    await _batch(db_session, WEEK_1, cards)
    other = await make_user(db_session, user_role=USER_ROLE_MANAGER, name="Invented Counter")
    for card in cards:
        await _decide(db_session, other, card)
        await _review(db_session, other, card)
    viewer = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    lists = await view.lists_view(db_session, viewer, view="current", tab="all", assignment="all")
    assert lists["counts"] == {"unreviewed": 2, "reviewed": 0, "all": 2}
    detail = await view.detail_view(db_session, viewer, cards[0].opp.id)
    assert (detail["decisions"], detail["reviews"], detail["status_events"]) == ([], [], [])
    assert "Invented Counter" not in repr(lists) + _without_assign_list(detail)
    decisions = [SimpleNamespace(opportunity_id=cards[0].opp.id, reviewer_id=other.id)]
    assert view.visible_votes(viewer, cards[0].opp.id, decisions, [], []) == ([], [], [])


async def test_own_review_visible_before_deciding(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card])
    viewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    other = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _review(db_session, viewer, card, comment="My invented review.")
    await _decide(db_session, other, card)
    await _review(db_session, other, card, comment="Their invented review.")

    detail = await view.detail_view(db_session, viewer, card.opp.id)
    assert [r["comment"] for r in detail["reviews"]] == ["My invented review."]
    assert detail["reviews"][0]["mine"] is True
    assert detail["decisions"] == []


async def test_full_review_does_not_unlock(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card])
    viewer = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    other = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _decide(db_session, other, card, "pass")
    await _review(db_session, other, card, comment="Hidden invented review.")
    await _review(db_session, viewer, card)

    detail = await view.detail_view(db_session, viewer, card.opp.id)
    assert detail["votes_unlocked"] is False
    assert detail["decisions"] == []
    assert "Hidden invented review." not in repr(detail)


async def test_tabs_are_per_viewer(db_session):
    epoch = await _epoch(db_session)
    cards = [await _card(db_session, epoch) for _ in range(2)]
    await _batch(db_session, WEEK_1, cards)
    viewer = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    other = await make_user(db_session, user_role=USER_ROLE_REVIEWER)

    async def tabs():
        out = {}
        for tab in ("unreviewed", "reviewed", "all"):
            got = await view.lists_view(
                db_session, viewer, view="current", tab=tab, assignment="all"
            )
            out[tab] = (sorted(_ids(got["cards"])), got["counts"])
        return out

    before = await tabs()
    await _decide(db_session, other, cards[0])
    assert await tabs() == before
    assert before["unreviewed"][1] == {"unreviewed": 2, "reviewed": 0, "all": 2}

    await _decide(db_session, viewer, cards[0])
    mine = await tabs()
    assert mine["reviewed"][0] == [str(cards[0].opp.id)]
    assert mine["unreviewed"][0] == [str(cards[1].opp.id)]


# --- blind batches ----------------------------------------------------------------------------


async def test_blind_batch_hides_score_band_rank(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch, rank=1)
    await _batch(db_session, WEEK_1, [card], blind=True)
    viewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)

    shown = (await view.batch_view(db_session, viewer))["this_week"]["cards"][0]
    assert (shown["score"], shown["band"], shown["rank"]) == (None, None, None)
    assert shown["blind_hidden"] is True
    assert shown["headline"] == "An invented headline"

    await _decide(db_session, viewer, card)
    shown = (await view.batch_view(db_session, viewer))["this_week"]["cards"][0]
    # The rank waits for the next batch: a probe of this batch has none (F2).
    assert (shown["score"], shown["band"], shown["rank"]) == (4.1, "advance", None)
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), [await _card(db_session, epoch)])
    shown = (await view.batch_view(db_session, viewer))["earlier_batches"][0]["cards"][0]
    assert (shown["score"], shown["band"], shown["rank"]) == (4.1, "advance", 1)


async def test_blind_hides_rationales(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card], blind=True)
    viewer = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    detail = await view.detail_view(db_session, viewer, card.opp.id)
    for key in ("score_rationale", "dimension_rationales", "scores", "weighted_score", "band"):
        assert key not in detail["assessment"]
    deep = detail["members"][0]["deep"]
    assert "provisional_scores" not in deep and "dimension_rationales" not in deep
    assert "Invented score rationale." not in repr(detail)
    assert "Invented dimension reason." not in repr(detail)
    assert view.hide_assessment(
        {"score_rationale": "x", "headline": "y"}, view.Hiding(blind=True, expert=False)
    ) == {"headline": "y"}

    await _decide(db_session, viewer, card)
    detail = await view.detail_view(db_session, viewer, card.opp.id)
    assert detail["assessment"]["score_rationale"] == "Invented score rationale."
    assert detail["members"][0]["deep"]["provisional_scores"] == {"scientific_merit": 4}


async def test_undecided_blind_cards_left_out_of_current_and_all(db_session):
    epoch = await _epoch(db_session)
    blind = [await _card(db_session, epoch) for _ in range(2)]
    normal = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [normal])
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), blind, blind=True)
    viewer = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    async def listed(which):
        got = await view.lists_view(db_session, viewer, view=which, tab="all", assignment="all")
        return sorted(_ids(got["cards"]))

    assert await listed("current") == await listed("all") == [str(normal.opp.id)]
    await _decide(db_session, viewer, blind[0])
    assert await listed("current") == [str(normal.opp.id)]
    await _decide(db_session, viewer, blind[1])
    everything = sorted(str(c.opp.id) for c in [*blind, normal])
    assert await listed("current") == await listed("all") == everything


async def test_probe_flags_hidden_until_superseded(db_session):
    epoch = await _epoch(db_session)
    in_cut = await _card(db_session, epoch, rank=1)
    probe = await _card(db_session, epoch, probe=True)
    await _batch(db_session, WEEK_1, [in_cut, probe], blind=True, probes=(probe,))
    viewer = await make_user(db_session, user_role=USER_ROLE_ADMIN)
    await _decide(db_session, viewer, in_cut)
    await _decide(db_session, viewer, probe)

    cards = (await view.batch_view(db_session, viewer))["this_week"]["cards"]
    assert [c["probe"] for c in cards] == [False, False]
    # A rank on the in-cut card alone would flag the unranked probe: neither shows one.
    assert [c["rank"] for c in cards] == [None, None]
    assert (await view.detail_view(db_session, viewer, probe.opp.id))["probe"] is False
    for card in (in_cut, probe):
        detail = await view.detail_view(db_session, viewer, card.opp.id)
        assert detail["assessment"]["rank"] is None
    pipeline = await view.pipeline_view(db_session, viewer)
    assert pipeline["blind_batches"] == []
    listed = await view.lists_view(db_session, viewer, view="all", tab="all", assignment="all")
    assert str(probe.opp.id) not in _ids(listed["cards"])

    later = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), [later])
    earlier = (await view.batch_view(db_session, viewer))["earlier_batches"][0]["cards"]
    assert {c["opportunity_id"]: c["probe"] for c in earlier} == {
        str(in_cut.opp.id): False, str(probe.opp.id): True,
    }
    assert {c["opportunity_id"]: c["rank"] for c in earlier} == {
        str(in_cut.opp.id): 1, str(probe.opp.id): None,
    }
    assert (await view.detail_view(db_session, viewer, in_cut.opp.id))["assessment"]["rank"] == 1
    assert (await view.detail_view(db_session, viewer, probe.opp.id))["probe"] is True
    flagged = (await view.pipeline_view(db_session, viewer))["blind_batches"][0]["cards"]
    assert {c["opportunity_id"]: c["probe"] for c in flagged} == {
        str(in_cut.opp.id): False, str(probe.opp.id): True,
    }


# --- access -----------------------------------------------------------------------------------


async def test_reviewer_cannot_get_lists_view(db_session):
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    with pytest.raises(PermissionError):
        await view.lists_view(db_session, reviewer, view="current", tab="all", assignment="all")
    with pytest.raises(PermissionError):
        await view.pipeline_view(db_session, reviewer)


async def test_staff_only_fields_never_reach_reviewer(db_session):
    epoch = await _epoch(db_session)
    card = await _card(
        db_session, epoch,
        strengths=[f"{STAFF_SENTINEL}-strength"], risks=[f"{STAFF_SENTINEL}-risk"],
        competitive_landscape=[f"{STAFF_SENTINEL}-landscape"],
        evidence_maturity=[f"{STAFF_SENTINEL}-maturity"],
    )
    await _batch(db_session, WEEK_1, [card])
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _decide(db_session, reviewer, card)

    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    page = await view.batch_view(db_session, reviewer)
    assert STAFF_SENTINEL not in repr(detail) + repr(page)
    for key in ("strengths", "risks", "competitive_landscape", "evidence_maturity"):
        assert key not in detail["assessment"]

    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    staff = await view.detail_view(db_session, manager, card.opp.id)
    assert staff["assessment"]["strengths"] == [f"{STAFF_SENTINEL}-strength"]


async def test_reviewer_404_outside_issued_batches(db_session):
    epoch = await _epoch(db_session)
    unissued = await _card(db_session, epoch)
    issued = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [issued])
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)

    assert await view.can_view(db_session, reviewer, unissued.opp.id) is False
    assert await view.detail_view(db_session, reviewer, unissued.opp.id) is None
    assert await view.detail_view(db_session, reviewer, uuid.uuid4()) is None
    assert await view.detail_view(db_session, reviewer, issued.opp.id) is not None
    pi = await make_user(db_session)
    assert await view.can_view(db_session, pi, issued.opp.id) is False


async def test_expert_opens_own_unissued_sample_item(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    expert = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    stranger = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    judgment = await _judgment(db_session, expert, card)

    assert await view.can_view(db_session, expert, card.opp.id) is True
    detail = await view.detail_view(db_session, expert, card.opp.id)
    assert detail["expert_item"]["judgment_id"] == str(judgment.id)
    assert await view.detail_view(db_session, stranger, card.opp.id) is None
    items = (await view.expert_view(db_session, expert))["items"]
    assert _ids(items) == [str(card.opp.id)]


# --- expert hiding ----------------------------------------------------------------------------


_EXPERT_HIDDEN = ("gating", "gating_rationales", "scores", "weighted_score", "band",
                  "recommendation", "rationale", "score_rationale", "dimension_rationales")


async def test_expert_hiding_on_every_surface(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card])
    expert = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    judgment = await _judgment(db_session, expert, card)

    batch_card = (await view.batch_view(db_session, expert))["this_week"]["cards"][0]
    listed = (await view.lists_view(
        db_session, expert, view="current", tab="all", assignment="all"
    ))["cards"][0]
    sampled = (await view.expert_view(db_session, expert))["items"][0]
    for shown in (batch_card, listed, sampled):
        assert shown["expert_hidden"] is True
        assert (shown["score"], shown["band"], shown["recommendation"]) == (None, None, None)
    detail = await view.detail_view(db_session, expert, card.opp.id)
    for key in _EXPERT_HIDDEN:
        assert key not in detail["assessment"]
    deep = detail["members"][0]["deep"]
    for key in ("gating", "flags_reviewed", "provisional_scores", "summary"):
        assert key not in deep
    assert detail["members"][0]["consults"] == []
    assert detail["dimensions"] == []

    judgment.submitted_at = func.now()
    await db_session.flush()
    detail = await view.detail_view(db_session, expert, card.opp.id)
    assert detail["assessment"]["gating"] == {"novelty": "met"}
    assert detail["members"][0]["deep"]["flags_reviewed"] == {"target_essential": "cleared"}
    batch_card = (await view.batch_view(db_session, expert))["this_week"]["cards"][0]
    assert (batch_card["score"], batch_card["band"]) == (4.1, "advance")


async def test_blind_and_expert_hiding_both_apply(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card], blind=True)
    expert = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    judgment = await _judgment(db_session, expert, card)

    assert await view.hiding_for(db_session, expert, card.opp.id) == view.Hiding(True, True)
    await _decide(db_session, expert, card)
    assert await view.hiding_for(db_session, expert, card.opp.id) == view.Hiding(False, True)
    detail = await view.detail_view(db_session, expert, card.opp.id)
    assert "scores" not in detail["assessment"] and "gating" not in detail["assessment"]

    judgment.submitted_at = func.now()
    await db_session.flush()
    assert await view.hiding_for(db_session, expert, card.opp.id) == view.Hiding(False, False)
    detail = await view.detail_view(db_session, expert, card.opp.id)
    assert detail["assessment"]["scores"] == {"scientific_merit": 4, "unmet_need": 5}


# --- epochs, history and missing assessments ---------------------------------------------------


async def test_epoch_switch_shows_last_complete_cut(db_session):
    active = await _epoch(db_session)
    card = await _card(db_session, active, headline="The active headline")
    db_session.add(LandscapeCut(
        epoch_id=active.id, status="complete", n=40, k=1, ordered_idea_ids=[str(card.idea.id)],
        completed_at=datetime(2026, 10, 1, tzinfo=UTC),
    ))
    db_session.add(LandscapeCut(
        epoch_id=active.id, status="boundary_pending", n=50, k=1, ordered_idea_ids=[],
    ))
    candidate = await _epoch(db_session, "candidate")
    db_session.add(LandscapeCut(
        epoch_id=candidate.id, status="complete", n=60, k=1, ordered_idea_ids=[],
        completed_at=datetime(2026, 10, 20, tzinfo=UTC),
    ))
    await _assess(db_session, candidate, card.opp.id, **_verdict(headline="A candidate headline"))
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    lists = await view.lists_view(db_session, manager, view="current", tab="all", assignment="all")
    assert lists["banner"] == "Being re-judged under new criteria"
    assert lists["cut"]["n"] == 40
    assert [c["headline"] for c in lists["cards"]] == ["The active headline"]
    assert lists["epoch"]["id"] == str(active.id)


async def test_detail_pins_active_epoch_not_newest(db_session):
    active = await _epoch(db_session)
    card = await _card(db_session, active, headline="The active headline")
    candidate = await _epoch(db_session, "candidate")
    newer = await _assess(
        db_session, candidate, card.opp.id, **_verdict(headline="A newer candidate headline")
    )
    newer.created_at = datetime.now(UTC) + timedelta(days=1)
    await db_session.flush()
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    detail = await view.detail_view(db_session, manager, card.opp.id)
    assert detail["assessment"]["id"] == str(card.assessment.id)
    assert detail["assessment"]["headline"] == "The active headline"
    assert str(newer.id) not in [h["assessment_id"] for h in detail["history"]]


async def test_card_without_completed_assessment_says_so(db_session):
    retired = await _epoch(db_session, "retired")
    active = await _epoch(db_session)
    card = await _card(db_session, active, status="failed:invalid", error="An invented error")
    old = await _assess(db_session, retired, card.opp.id, **_verdict(headline="Old headline"))
    await _batch(db_session, WEEK_1, [card])
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)

    shown = (await view.batch_view(db_session, reviewer))["this_week"]["cards"][0]
    assert shown["no_assessment"] is True
    assert shown["failure"] == {"status": "failed:invalid", "error": "An invented error"}
    assert shown["history_assessment_id"] == str(old.id)
    assert shown["headline"] is None
    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    assert detail["assessment"] is None and detail["no_assessment"] is True
    assert detail["history_assessment_id"] == str(old.id)


async def test_blind_detail_hides_scores_until_decision(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card], blind=True)
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)

    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    assert detail["hiding"] == {"blind": True, "expert": False}
    assert "weighted_score" not in detail["assessment"] and "band" not in detail["assessment"]
    assert detail["dimensions"] == []

    await _decide(db_session, reviewer, card)
    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    assert detail["assessment"]["weighted_score"] == 4.1
    assert {row["key"] for row in detail["dimensions"]} >= {"scientific_merit", "unmet_need"}


async def test_retired_opportunity_reads_as_history(db_session):
    epoch = await _epoch(db_session)
    gone = await _card(db_session, epoch, headline="A retired headline")
    gone.opp.retired_at = datetime.now(UTC)
    current = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [gone])
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), [current])
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)

    detail = await view.detail_view(db_session, reviewer, gone.opp.id)
    assert detail["retired"] is True and detail["is_history"] is True
    assert detail["assessment"]["headline"] == "A retired headline"
    assert detail["no_assessment"] is False
    page = await view.batch_view(db_session, reviewer)
    assert str(gone.opp.id) not in _ids(page["carry_overs"])


async def test_pipeline_lists_cards_without_completed_assessment(db_session):
    epoch = await _epoch(db_session)
    failing = await _card(db_session, epoch, status="failed:no_deep")
    fine = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [failing, fine])
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    pipeline = await view.pipeline_view(db_session, manager)
    assert _ids(pipeline["no_assessment_cards"]) == [str(failing.opp.id)]
    assert pipeline["no_assessment_cards"][0]["failure"]["status"] == "failed:no_deep"
    assert pipeline["phase"] == "pilot" and pipeline["paused"] is False


async def test_pipeline_lists_filtered_items_with_rescue_fields(db_session):
    """§13.4: per-source last fetch, incomplete slices, the reject audit's disagreements and
    the filtered-items browser, each excluded row carrying what the rescue form posts."""
    epoch = await _epoch(db_session)
    doc = LandscapeDocument(
        kind="work", canonical_external_id=f"doi:10.9999/invented-lv-{uuid.uuid4().hex}",
        title="Invented fen survey", summary_text="An invented survey of fen soils.",
        published_on=date(2026, 3, 1), document_type="article", s1_status="eligible",
    )
    idea = LandscapeIdea(
        id=uuid.uuid4(), epoch_id=epoch.id, idea_key="biology;invented-lv-filtered",
        idea_type="biology", direction="inhibit",
        targets=[{"hgnc_id": "HGNC:90003", "symbol": "INVLW"}],
        disease={"mondo_id": "MONDO:9999997", "label": "invented sclerosis"}, version_hash="v2",
    )
    db_session.add_all([doc, idea, LandscapeSource(source="openalex", enabled=True)])
    await db_session.flush()
    screen = LandscapeScreen(
        document_id=doc.id, epoch_id=epoch.id, status="excluded:screen", decision="no",
        reason="Invented: a survey, not a therapy.",
    )
    triage = LandscapeTriage(
        idea_id=idea.id, idea_version="v2", epoch_id=epoch.id, status="excluded:low_score",
        exclusion_reason="Invented low score.",
    )
    stale = LandscapeTriage(
        idea_id=idea.id, idea_version="v1", epoch_id=epoch.id, status="excluded:low_score",
    )
    deep = LandscapeDeep(
        idea_id=idea.id, idea_version="v2", epoch_id=epoch.id, status="excluded:deep_gate",
        gating={"novelty": "not_met", "feasibility": "met"},
    )
    fetch = LandscapeFetch(
        source="openalex", slice_key="month:2026-01", status="incomplete",
        expected_count=10, received_count=7,
    )
    db_session.add_all([screen, triage, stale, deep, fetch])
    await db_session.flush()
    db_session.add(LandscapeRejectAudit(
        item_kind="screen", item_id=screen.id, epoch_id=epoch.id, status="done",
        original_decision="no", audit_decision="yes", agree=False, reason="Invented reason.",
    ))
    await db_session.flush()
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    pipeline = await view.pipeline_view(db_session, manager)

    screen_fields = {"item_kind": "screen", "item_id": str(screen.id), "stage": "screen",
                     "expected_status": "excluded:screen"}
    filtered = pipeline["filtered"]
    assert filtered["screen"]["count"] == 1
    assert filtered["screen"]["items"][0] | screen_fields == filtered["screen"]["items"][0]
    assert filtered["screen"]["items"][0]["label"] == "Invented fen survey"
    assert filtered["triage"]["count"] == 1  # the stale version is not listed
    assert filtered["triage"]["items"][0]["item_id"] == str(triage.id)
    assert filtered["triage"]["items"][0]["reason"] == "Invented low score."
    assert filtered["deep_gate"]["items"][0]["stage"] == "deep_gate"
    assert filtered["deep_gate"]["items"][0]["item_kind"] == "deep"
    assert filtered["deep_gate"]["items"][0]["reason"] == "Gates not met: novelty"
    (disagreement,) = pipeline["audit_disagreements"]
    assert disagreement["audit_decision"] == "yes" and disagreement["rescue"] == screen_fields
    (source,) = pipeline["sources"]
    assert source["source"] == "openalex" and source["last_fetch"]["status"] == "incomplete"
    assert [s["slice_key"] for s in pipeline["incomplete_slices"]] == ["month:2026-01"]


async def test_changed_since_compares_assessment_ids(db_session):
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    await _batch(db_session, WEEK_1, [card])
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _decide(db_session, reviewer, card)

    card.opp.version_counter = 2
    db_session.add(LandscapeOpportunityVersion(
        opportunity_id=card.opp.id, version=2, epoch_id=epoch.id,
        member_idea_keys=[card.idea.idea_key], label="Invented programme", status="done",
    ))
    pending = await _assess(db_session, epoch, card.opp.id, version=2, status="pending")
    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    assert detail["changed_since_decision"] is False
    assert detail["assessment"]["id"] == str(card.assessment.id)

    pending.status = "done"
    await db_session.flush()
    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    assert detail["changed_since_decision"] is True
    assert detail["assessment"]["id"] == str(pending.id)

    decided = SimpleNamespace(landscape_assessment_id=card.assessment.id)
    assert view.changed_since_decision(decided, card.assessment.id) is False
    assert view.changed_since_decision(decided, None) is False
    assert view.changed_since_decision(None, card.assessment.id) is False


async def test_decided_card_without_completed_assessment_is_not_changed(db_session):
    """With no completed assessment shown there is nothing to compare, on the card and in
    the "changed" list alike (D50)."""
    old = await _epoch(db_session)
    card = await _card(db_session, old)
    await _batch(db_session, WEEK_1, [card])
    reviewer = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _decide(db_session, reviewer, card)
    old.status = "retired"
    await db_session.flush()
    new = await _epoch(db_session)
    card.opp.version_counter = 2
    db_session.add(LandscapeOpportunityVersion(
        opportunity_id=card.opp.id, version=2, epoch_id=new.id,
        member_idea_keys=[card.idea.idea_key], label="Invented programme", status="done",
    ))
    await _assess(db_session, new, card.opp.id, version=2, status="failed:invalid")
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), [await _card(db_session, new)])

    page = await view.batch_view(db_session, reviewer)
    (shown,) = page["earlier_batches"][0]["cards"]
    assert shown["no_assessment"] is True
    assert shown["changed_since_decision"] is False
    assert str(card.opp.id) not in _ids(page["changed"])
    detail = await view.detail_view(db_session, reviewer, card.opp.id)
    assert detail["changed_since_decision"] is False


async def test_blind_results_withheld_from_staff_with_undecided_card(db_session):
    """A superseded blind batch's pursue counts are other reviewers' votes, withheld from a
    staff viewer until they have decided every card of that batch (L40, L50, D60)."""
    epoch = await _epoch(db_session)
    in_cut = await _card(db_session, epoch, rank=1)
    probe = await _card(db_session, epoch, probe=True)
    blind_id = await _batch(db_session, WEEK_1, [in_cut, probe], blind=True, probes=(probe,))
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), [await _card(db_session, epoch)])
    other = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await _decide(db_session, other, in_cut)
    await _decide(db_session, other, probe, "pass")
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    async def results() -> list[str]:
        pipeline = await view.pipeline_view(db_session, manager)
        return [r["batch_id"] for r in pipeline["metrics"]["blind_results"]]

    await _decide(db_session, manager, in_cut)
    assert await results() == []
    await _decide(db_session, manager, probe)
    assert await results() == [str(blind_id)]


# --- the expert hiding's span and fields -------------------------------------------------------


async def test_expert_hiding_outlasts_its_quarter(db_session):
    """An unsubmitted judgment drawn in an earlier quarter still hides S9's calls; the
    newest judgment decides, as it is the one the expert submits."""
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    expert = await make_user(db_session, user_role=USER_ROLE_REVIEWER)
    earlier = view._quarter(datetime.now(UTC) - timedelta(days=120))
    drawn = datetime.now(UTC) - timedelta(days=120)
    db_session.add(LandscapeExpertJudgment(
        opportunity_id=card.opp.id, expert_id=expert.id, expert_name=expert.name,
        quarter=earlier, drawn_at=drawn, landscape_assessment_id=card.assessment.id,
    ))
    await db_session.flush()

    assert await view.hiding_for(db_session, expert, card.opp.id) == view.Hiding(False, True)
    detail = await view.detail_view(db_session, expert, card.opp.id)
    assert "gating" not in detail["assessment"] and "scores" not in detail["assessment"]
    assert _ids((await view.expert_view(db_session, expert))["items"]) == [str(card.opp.id)]

    db_session.add(LandscapeExpertJudgment(
        opportunity_id=card.opp.id, expert_id=expert.id, expert_name=expert.name,
        quarter=_quarter(), drawn_at=datetime.now(UTC), submitted_at=datetime.now(UTC),
        landscape_assessment_id=card.assessment.id,
    ))
    await db_session.flush()
    assert await view.hiding_for(db_session, expert, card.opp.id) == view.Hiding(False, False)


async def test_expert_hiding_covers_every_s9_call(db_session):
    """Red flags, risks, strengths, confidence and funnel stage are S9's calls or their
    reasons, and the verdict signals list red flags and gates; descriptive fields stay."""
    epoch = await _epoch(db_session)
    card = await _card(
        db_session, epoch, red_flags=["RED-FLAG-SENTINEL"], risks=["RISK-SENTINEL"],
        strengths=["STRENGTH-SENTINEL"], confidence="High", funnel_stage="FUNNEL-SNT",
        member_notes=[{"idea": "1", "note": "An invented member note."}],
    )
    expert = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    await _judgment(db_session, expert, card)

    detail = await view.detail_view(db_session, expert, card.opp.id)
    for key in (*_EXPERT_HIDDEN, "red_flags", "risks", "strengths", "confidence",
                "funnel_stage"):
        assert key not in detail["assessment"], key
    assert detail["verdict_signals"] is None
    assert "SENTINEL" not in repr(detail) and "FUNNEL-SNT" not in repr(detail)
    assert detail["assessment"]["headline"] == "An invented headline"
    assert detail["assessment"]["member_notes"] == [
        {"idea": "1", "note": "An invented member note."}
    ]
    assert detail["assessment"]["key_points"] == {"why_it_matters": ["An invented point."]}

    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    unhidden = await view.detail_view(db_session, manager, card.opp.id)
    assert unhidden["assessment"]["red_flags"] == ["RED-FLAG-SENTINEL"]
    assert unhidden["verdict_signals"] is not None


# --- member resolution ------------------------------------------------------------------------


async def test_probe_members_resolve_in_shown_assessments_epoch(db_session):
    """A probe is not re-minted at activation, so its version keeps the retired epoch while
    the active epoch re-assesses it; the page shows the active epoch's member, S6 and rank."""
    retired = await _epoch(db_session, "retired")
    active = await _epoch(db_session)
    key = f"biology;invented-lv-probe-{next(_counter)}"
    ideas = {}
    for epoch, summary in ((retired, "RETIRED-S6-SUMMARY"), (active, "ACTIVE-S6-SUMMARY")):
        idea = LandscapeIdea(
            id=uuid.uuid4(), epoch_id=epoch.id, idea_key=key, idea_type="biology",
            direction="inhibit", targets=[{"hgnc_id": "HGNC:90004", "symbol": "INVLX"}],
            disease={"mondo_id": "MONDO:9999996", "label": "invented nephritis"},
            version_hash="v1",
        )
        db_session.add(idea)
        await db_session.flush()
        db_session.add(LandscapeDeep(
            idea_id=idea.id, idea_version="v1", epoch_id=epoch.id, status="done",
            summary=summary,
        ))
        ideas[epoch.id] = idea
    db_session.add(LandscapeRanking(epoch_id=active.id, idea_id=ideas[active.id].id, position=3))
    opp = LandscapeOpportunity(id=uuid.uuid4(), version_counter=1)
    db_session.add(opp)
    await db_session.flush()
    db_session.add(LandscapeOpportunityVersion(
        opportunity_id=opp.id, version=1, epoch_id=retired.id, member_idea_keys=[key],
        label="Invented probe", status="done", is_probe=True,
    ))
    assessment = await _assess(db_session, active, opp.id, **_verdict())
    probe = _Card(opp, ideas[active.id], assessment)
    await _batch(db_session, WEEK_1, [probe], blind=True, probes=(probe,))
    await _batch(db_session, WEEK_1 + timedelta(weeks=1), [await _card(db_session, active)])
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)
    await _decide(db_session, manager, probe)

    detail = await view.detail_view(db_session, manager, opp.id)
    (member,) = detail["members"]
    assert member["idea_id"] == str(ideas[active.id].id)
    assert member["deep"]["summary"] == "ACTIVE-S6-SUMMARY"
    assert "RETIRED-S6-SUMMARY" not in repr(detail)
    assert detail["assessment"]["rank"] == 3
    (shown,) = (await view.batch_view(db_session, manager))["earlier_batches"][0]["cards"]
    assert shown["rank"] == 3


async def test_member_shows_passed_s6_record(db_session):
    """A member's S6 record is the one S8 judges by (``_passed_deep``): never a newer
    ``pending`` or ``failed:*`` row, and a ``rescued`` row counts as passed."""
    epoch = await _epoch(db_session)
    card = await _card(db_session, epoch)
    later = datetime.now(UTC) + timedelta(hours=1)
    card.idea.version_hash = "v3"
    for version, status in (("v2", "failed:invalid"), ("v3", "pending")):
        db_session.add(LandscapeDeep(
            idea_id=card.idea.id, idea_version=version, epoch_id=epoch.id, status=status,
            summary=f"UNPASSED-{status}", created_at=later,
        ))
    rescued = await _card(db_session, epoch)
    row = await db_session.scalar(
        select(LandscapeDeep).where(LandscapeDeep.idea_id == rescued.idea.id)
    )
    row.status = "rescued"
    await db_session.flush()
    manager = await make_user(db_session, user_role=USER_ROLE_MANAGER)

    detail = await view.detail_view(db_session, manager, card.opp.id)
    deep = detail["members"][0]["deep"]
    assert (deep["status"], deep["summary"]) == ("done", "Invented S6 summary.")
    assert "UNPASSED" not in repr(detail)
    detail = await view.detail_view(db_session, manager, rescued.opp.id)
    assert detail["members"][0]["deep"]["status"] == "rescued"
