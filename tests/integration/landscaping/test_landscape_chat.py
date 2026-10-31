"""The landscaping chat (docs/specs/2026-10-30-blackbird-landscaping-design.md §13.5; L13, L50,
L53, L63, D25, D47, D50, D63): its model, its isolation from the other chat subjects, its
spend sub-cap, the hidings its record applies, its router's refusals, and migration 0078.

The producer runs on the test's session and the model is FakeAsyncAnthropic, so nothing here
spends money. Every name below is invented.
"""
import asyncio
import importlib.util
import json
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from src.config import get_settings
from src.landscaping import epochs
from src.landscaping.review_batches import EXPERTS_KEY
from src.models import USER_ROLE_MANAGER, AppSetting, AssessmentChatTurn, AssessmentChatUsage
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeConsult,
    LandscapeDecision,
    LandscapeDeep,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeIdea,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRanking,
    LandscapeReview,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
)
from src.routers import landscape_chat as chat_router
from src.services import assessment_chat as chat
from src.services.landscape_chat_record import build_landscape_chat_record
from tests import factories
from tests.assessment_chat_support import seed_interview, use_fake_llm, use_test_session
from tests.fakes import ChatScript, FakeAsyncAnthropic
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_project_chat_service import seed_project

pytestmark = pytest.mark.integration

LANDSCAPE_PROMPT = "LANDSCAPE-CHAT-PROMPT-SENTINEL: answer from the opportunity record only."
_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "alembic" / "versions"
_FILENAME = "0078_landscape_chat.py"
_KEYS = iter(range(1, 10_000))

#: Distinct values, so each assertion names exactly one stored field.
S9_SCORE_RATIONALE = "S9-SCORE-RATIONALE-SENTINEL"
S9_DIM_RATIONALE = "S9-DIMENSION-RATIONALE-SENTINEL"
S9_GATE_RATIONALE = "S9-GATE-RATIONALE-SENTINEL"
S9_HEADLINE = "S9-HEADLINE-SENTINEL"
S6_GATE_RATIONALE = "S6-GATE-RATIONALE-SENTINEL"
S6_DIM_RATIONALE = "S6-DIMENSION-RATIONALE-SENTINEL"
S6_FLAG_REASON = "S6-FLAG-REVIEW-SENTINEL"
S6_CONSULT = "S6-CONSULT-CONCERN-SENTINEL"
RANK = 7


def _load_migrate(module_name: str, filename: str):
    """A scripts/migrate module under the name tests/unit/test_migration_checks.py gives it
    (postflight imports preflight under that name), reused when already loaded."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, _ROOT / "scripts" / "migrate" / filename
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pf = _load_migrate("copi_migrate_preflight", "preflight.py")
po = _load_migrate("copi_migrate_postflight", "postflight.py")


# --- fixtures and builders -------------------------------------------------------------------


@pytest.fixture
def install_llm(monkeypatch, db_session, tmp_path):
    """The producer on the test session, the landscaping prompt on a fixture file; returns a
    function that installs a FakeAsyncAnthropic with the given scripts."""
    use_test_session(monkeypatch, db_session)
    prompt = tmp_path / "chat.md"
    prompt.write_text(LANDSCAPE_PROMPT, encoding="utf-8")
    monkeypatch.setattr(chat, "LANDSCAPE_PROMPT_PATH", prompt)

    def _install(*scripts: ChatScript) -> FakeAsyncAnthropic:
        fake = FakeAsyncAnthropic(list(scripts))
        use_fake_llm(monkeypatch, fake)
        return fake

    return _install


@pytest.fixture
def switches_on(asgi_app, monkeypatch):
    """Both chat switches on and chat.md recorded."""
    asgi_app.state.assessment_chat_enabled = True
    asgi_app.state.landscape_chat_enabled = True
    monkeypatch.setattr(chat_router.prompt_set, "chat_prompt_matches", lambda: True)
    return asgi_app


async def _active_epoch(db) -> uuid.UUID:
    epoch = LandscapeEpoch(status="active", snapshot=epochs.build_snapshot())
    db.add(epoch)
    await db.flush()
    return epoch.id


async def seed_opportunity(db, epoch_id: uuid.UUID) -> uuid.UUID:
    """One opportunity of one idea, with a done S6 record, a consult, an S7 position and a
    done S9 assessment in ``epoch_id``."""
    key = f"biology;invented-chat-{next(_KEYS)}"
    idea = LandscapeIdea(
        epoch_id=epoch_id, idea_key=key, idea_type="biology", direction="inhibit",
        targets=[{"hgnc_id": "HGNC:90001", "symbol": "INVCH"}],
        disease={"mondo_id": "MONDO:9999999", "label": "invented fibrosis"},
        version_hash="v1", jhu_group_count=1,
    )
    db.add(idea)
    await db.flush()
    deep = LandscapeDeep(
        idea_id=idea.id, idea_version="v1", epoch_id=epoch_id, status="done",
        summary="An invented deep summary.",
        gating={"credible_science": "met"},
        gating_rationales={"credible_science": S6_GATE_RATIONALE},
        provisional_scores={"scientific_credibility": 3},
        dimension_rationales={"scientific_credibility": S6_DIM_RATIONALE},
        provisional_weighted_score=3.0,
        flags_reviewed={"target_essential": {"status": "confirmed", "reason": S6_FLAG_REASON}},
        disease_profile={"prevalence": {"value": "An invented prevalence.", "support": "estimate"}},
    )
    db.add(deep)
    await db.flush()
    db.add(LandscapeConsult(deep_id=deep.id, domain="clinical", opinion={"concerns": [S6_CONSULT]}))
    db.add(LandscapeRanking(epoch_id=epoch_id, idea_id=idea.id, position=RANK))
    opportunity = LandscapeOpportunity(version_counter=1)
    db.add(opportunity)
    await db.flush()
    db.add(LandscapeOpportunityVersion(
        opportunity_id=opportunity.id, version=1, epoch_id=epoch_id, member_idea_keys=[key],
        label="Invented opportunity", status="done",
    ))
    db.add(LandscapeAssessment(
        opportunity_id=opportunity.id, opportunity_version=1, epoch_id=epoch_id, status="done",
        headline=S9_HEADLINE, recommendation="conditional", band="advance",
        weighted_score=3.75, score_rationale=S9_SCORE_RATIONALE,
        scores={"scientific_credibility": 4},
        dimension_rationales={"scientific_credibility": S9_DIM_RATIONALE},
        gating={"credible_science": "met"},
        gating_rationales={"credible_science": S9_GATE_RATIONALE},
    ))
    await db.flush()
    return opportunity.id


async def _manager(db, name: str | None = None):
    extra = {"name": name} if name else {}
    return await factories.make_user(db, user_role=USER_ROLE_MANAGER, **extra)


async def _ask(db, user, question="What is this opportunity?", **subject):
    prepared = await chat.prepare_turn(db, user=user, question_raw=question, **subject)
    await chat.run_turn(prepared, asyncio.Queue())
    return prepared


async def _questions(db, user, **subject) -> list[str]:
    history = await chat.list_history(db, user=user, **subject)
    return [turn["question"] for turn in history["turns"]]


def _doc(record, title: str) -> str:
    """One document's blocks joined, found by its title."""
    for document in record.documents:
        if document["title"] == title:
            return "\n".join(block["text"] for block in document["source"]["content"])
    raise AssertionError(f"no {title!r} document")


def _quarter(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


# --- the service ------------------------------------------------------------------------------


async def test_model_is_opus_5_for_landscape_subject(db_session, install_llm):
    fake = install_llm()
    opp = await seed_opportunity(db_session, await _active_epoch(db_session))
    user = await _manager(db_session)
    prepared = await _ask(db_session, user, landscape_opportunity_id=opp)
    assert get_settings().llm_landscape_chat_model == "claude-opus-5"
    assert prepared.model == "claude-opus-5"
    assert fake.calls[0]["model"] == "claude-opus-5"
    assert fake.calls[0]["system"] == [{"type": "text", "text": LANDSCAPE_PROMPT}]
    usage = await db_session.get(AssessmentChatUsage, prepared.usage_id)
    assert (usage.subject, usage.model, usage.landscape_opportunity_id) == (
        "landscape", "claude-opus-5", opp
    )


async def test_assessment_subject_model_unchanged(db_session, install_llm):
    fake = install_llm()
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    prepared = await _ask(db_session, user, assessment_id=seeded.assessment_id)
    assert prepared.model == get_settings().llm_assessment_chat_model
    assert fake.calls[0]["model"] == get_settings().llm_assessment_chat_model
    assert (prepared.assessment_id, prepared.project_id, prepared.landscape_opportunity_id) == (
        seeded.assessment_id, None, None
    )
    usage = await db_session.get(AssessmentChatUsage, prepared.usage_id)
    assert (usage.subject, usage.landscape_opportunity_id) == ("assessment", None)


async def test_no_cross_subject_reads(db_session, install_llm):
    install_llm()
    epoch_id = await _active_epoch(db_session)
    opp_a = await seed_opportunity(db_session, epoch_id)
    opp_b = await seed_opportunity(db_session, epoch_id)
    seeded = await seed_interview(db_session)
    pid = await seed_project(db_session)
    user = await _manager(db_session)
    await _ask(db_session, user, "ABOUT OPPORTUNITY A", landscape_opportunity_id=opp_a)
    await _ask(db_session, user, "ABOUT THE ASSESSMENT", assessment_id=seeded.assessment_id)
    await _ask(db_session, user, "ABOUT THE PROJECT", project_id=pid)
    assert await _questions(db_session, user, landscape_opportunity_id=opp_a) == [
        "ABOUT OPPORTUNITY A"
    ]
    assert await _questions(db_session, user, landscape_opportunity_id=opp_b) == []
    assert await _questions(db_session, user, assessment_id=seeded.assessment_id) == [
        "ABOUT THE ASSESSMENT"
    ]
    assert await _questions(db_session, user, project_id=pid) == ["ABOUT THE PROJECT"]
    # Clearing one subject leaves the others' turns, in both directions.
    assert await chat.clear_history(
        db_session, landscape_opportunity_id=opp_a, user_id=user.id
    ) == 1
    assert await _questions(db_session, user, assessment_id=seeded.assessment_id) == [
        "ABOUT THE ASSESSMENT"
    ]
    assert await _questions(db_session, user, project_id=pid) == ["ABOUT THE PROJECT"]
    await _ask(db_session, user, "AGAIN ABOUT A", landscape_opportunity_id=opp_a)
    assert await chat.clear_history(
        db_session, assessment_id=seeded.assessment_id, user_id=user.id
    ) == 1
    assert await _questions(db_session, user, landscape_opportunity_id=opp_a) == ["AGAIN ABOUT A"]
    turns = (
        await db_session.execute(
            select(AssessmentChatTurn).where(AssessmentChatTurn.user_id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    for turn in turns:
        named = [turn.assessment_id, turn.project_id, turn.landscape_opportunity_id]
        assert sum(value is not None for value in named) == 1
    with pytest.raises(ValueError):
        await chat.prepare_turn(
            db_session, user=user, question_raw="q", assessment_id=seeded.assessment_id,
            landscape_opportunity_id=opp_a,
        )


async def test_subcap_stops_landscape_but_not_assessment_chat(db_session, install_llm, monkeypatch):
    fake = install_llm()
    monkeypatch.setattr(get_settings(), "landscape_chat_daily_usd_limit", 2.0)
    opp = await seed_opportunity(db_session, await _active_epoch(db_session))
    seeded = await seed_interview(db_session)
    user = await _manager(db_session)
    other = await _manager(db_session)
    # Another user's landscaping question with no usage recorded counts the $2.50 reserve,
    # over the $2 sub-cap, while the per-user and shared totals stay far below their limits.
    db_session.add(AssessmentChatUsage(
        id=uuid.uuid4(), user_id=other.id, landscape_opportunity_id=opp, subject="landscape",
        context_tier="staff", model="claude-opus-5", status="failed", usage_by_model=None,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    ))
    await db_session.flush()
    with pytest.raises(chat.ChatError) as refused:
        await chat.prepare_turn(
            db_session, landscape_opportunity_id=opp, user=user, question_raw="q"
        )
    assert (refused.value.status, refused.value.code) == (429, "landscape_spend_limit")
    assert fake.calls == []
    await _ask(db_session, user, assessment_id=seeded.assessment_id)
    assert len(fake.calls) == 1


# --- the record -------------------------------------------------------------------------------


async def test_reviews_doc_respects_independent_votes(db_session):
    opp = await seed_opportunity(db_session, await _active_epoch(db_session))
    viewer = await _manager(db_session)
    other = await _manager(db_session, name="Invented Other Reviewer")
    db_session.add(LandscapeDecision(
        opportunity_id=opp, reviewer_id=other.id, reviewer_name=other.name, decision="pass",
        reason="OTHER-REASON-SENTINEL",
    ))
    db_session.add(LandscapeReview(
        opportunity_id=opp, reviewer_id=other.id, reviewer_name=other.name, score=2,
        comment="OTHER-COMMENT-SENTINEL",
    ))
    await db_session.flush()
    before = _doc(await build_landscape_chat_record(db_session, opp, viewer), "Reviews")
    assert "OTHER-REASON-SENTINEL" not in before
    assert "OTHER-COMMENT-SENTINEL" not in before
    assert "Invented Other Reviewer" not in before
    # Only the viewer's own quick decision unlocks the others' votes (L50, L51).
    db_session.add(LandscapeDecision(
        opportunity_id=opp, reviewer_id=viewer.id, reviewer_name=viewer.name,
        decision="pursue", reason="OWN-REASON",
    ))
    await db_session.flush()
    after = _doc(await build_landscape_chat_record(db_session, opp, viewer), "Reviews")
    assert "OTHER-REASON-SENTINEL" in after
    assert "OTHER-COMMENT-SENTINEL" in after


async def test_verdict_doc_blinded_and_expert_hidden(db_session):
    epoch_id = await _active_epoch(db_session)
    blind_opp = await seed_opportunity(db_session, epoch_id)
    expert_opp = await seed_opportunity(db_session, epoch_id)
    viewer = await _manager(db_session)
    expert = await _manager(db_session)

    # An undecided card of a blind batch: its scores, band, rank and rationales are hidden.
    batch = LandscapeReviewBatch(issued_on=date.today(), blind=True)
    db_session.add(batch)
    await db_session.flush()
    db_session.add(LandscapeReviewBatchItem(batch_id=batch.id, opportunity_id=blind_opp, position=1))
    await db_session.flush()
    verdict = _doc(await build_landscape_chat_record(db_session, blind_opp, viewer), "Verdict")
    assert S9_HEADLINE in verdict
    for hidden in (S9_SCORE_RATIONALE, S9_DIM_RATIONALE, f"rank: {RANK}", "band: advance",
                   "weighted score", "score 4"):
        assert hidden not in verdict, hidden

    # The expert's own unsubmitted sample item: S9's and S6's calls are hidden everywhere.
    db_session.add(AppSetting(key=EXPERTS_KEY, value=json.dumps([str(expert.id)])))
    db_session.add(LandscapeExpertJudgment(
        opportunity_id=expert_opp, expert_id=expert.id, expert_name=expert.name,
        quarter=_quarter(date.today()), issued_before_draw=False,
    ))
    await db_session.flush()
    record = await build_landscape_chat_record(db_session, expert_opp, expert)
    verdict = _doc(record, "Verdict")
    panel = _doc(record, "Panel")
    disease = _doc(record, "Disease profile")
    evidence = _doc(record, "Evidence")
    assert S9_HEADLINE in verdict
    for hidden in (S9_SCORE_RATIONALE, S9_DIM_RATIONALE, S9_GATE_RATIONALE, f"rank: {RANK}",
                   "band: advance", "weighted score", "Recommendation"):
        assert hidden not in verdict, hidden
    for hidden in (S6_GATE_RATIONALE, S6_DIM_RATIONALE, S6_CONSULT):
        assert hidden not in panel, hidden
    for text_ in (disease, evidence):
        for hidden in (S6_GATE_RATIONALE, S6_DIM_RATIONALE, S6_FLAG_REASON, S6_CONSULT):
            assert hidden not in text_, hidden

    # A viewer with neither hiding sees every call.
    record = await build_landscape_chat_record(db_session, expert_opp, viewer)
    assert S9_SCORE_RATIONALE in _doc(record, "Verdict")
    assert f"rank: {RANK}" in _doc(record, "Verdict")
    assert S6_CONSULT in _doc(record, "Panel")
    assert S6_FLAG_REASON in _doc(record, "Evidence")


# --- the router -------------------------------------------------------------------------------


async def _routes(client, opportunity_id, headers) -> list:
    base = f"/landscape-chat/{opportunity_id}"
    body = {"question": "What is this opportunity?", "via": "bubble"}
    return [
        await client.get(base, headers=headers),
        await client.post(base + "/messages", headers=headers, json=body),
        await client.post(base + "/clear", headers=headers, json=body),
        await client.post(base + "/opened", headers=headers, json=body),
    ]


async def test_switch_off_503(client, db_session, asgi_app, install_llm, monkeypatch):
    """Either switch off refuses every route with 503 ``disabled`` before the opportunity is
    looked up, so an unknown id is not told apart; with both on, the unknown id is 404."""
    fake = install_llm()
    monkeypatch.setattr(chat_router.prompt_set, "chat_prompt_matches", lambda: True)
    opp = await seed_opportunity(db_session, await _active_epoch(db_session))
    user = await _manager(db_session)
    headers = auth_headers(user.id)
    for assessment_on, landscape_on in ((True, False), (False, True)):
        asgi_app.state.assessment_chat_enabled = assessment_on
        asgi_app.state.landscape_chat_enabled = landscape_on
        for target in (opp, uuid.uuid4()):
            responses = await _routes(client, target, headers)
            assert [(r.status_code, r.json()) for r in responses] == (
                [(503, {"error": "disabled"})] * 4
            )
    asgi_app.state.assessment_chat_enabled = True
    asgi_app.state.landscape_chat_enabled = True
    responses = await _routes(client, uuid.uuid4(), headers)
    assert [(r.status_code, r.json()) for r in responses] == [(404, {"error": "not_found"})] * 4
    responses = await _routes(client, "not-a-uuid", headers)
    assert [r.status_code for r in responses] == [404] * 4
    assert fake.calls == []
    history = await client.get(f"/landscape-chat/{opp}", headers=headers)
    assert history.status_code == 200
    assert history.json()["turns"] == []


async def test_unrecorded_chat_md_edit_503(client, db_session, switches_on, install_llm, monkeypatch):
    fake = install_llm()
    opp = await seed_opportunity(db_session, await _active_epoch(db_session))
    user = await _manager(db_session)
    monkeypatch.setattr(chat_router.prompt_set, "chat_prompt_matches", lambda: False)
    response = await client.post(
        f"/landscape-chat/{opp}/messages", headers=auth_headers(user.id),
        json={"question": "What is this opportunity?"},
    )
    assert (response.status_code, response.json()) == (503, {"error": "prompt_unrecorded"})
    assert fake.calls == []
    ledger_row = await db_session.scalar(
        select(AssessmentChatUsage.id).where(AssessmentChatUsage.user_id == user.id)
    )
    assert ledger_row is None


# --- migration 0078 ---------------------------------------------------------------------------


def _later_revisions() -> list[str]:
    """Migration files above 0078, newest first: their downgrades run before 0078's."""
    later = [p.name for p in _VERSIONS.glob("0*.py") if int(p.name.split("_", 1)[0]) > 78]
    return sorted(later, reverse=True)


def _module(filename: str):
    name = "_m" + filename.split("_", 1)[0]
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str, filename: str = _FILENAME) -> None:
    mod = _module(filename)
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session) -> set[tuple[str, str]]:
    rows = await db_session.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'landscape_opportunity_id'"
    ))
    return {(t, c) for t, c in rows.all()}


async def _check(db_session, name: str) -> str:
    return (await db_session.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"
    ), {"name": name})).scalar_one()


async def test_migration_0078_round_trip(db_session):
    chat_tables = {"assessment_chat_turns", "assessment_chat_usage", "assessment_chat_opens"}
    assert {t for t, _c in await _columns(db_session)} == chat_tables
    opp = await seed_opportunity(db_session, await _active_epoch(db_session))
    user = await _manager(db_session)
    turn, usage = chat._new_rows(
        landscape_opportunity_id=opp, user_id=user.id, tier="staff", question="q",
        model="claude-opus-5", record_sha="a" * 12, prompt_sha="b" * 12,
        created_at=datetime.now(UTC), verdict_revision=1,
    )
    turn.status = usage.status = "complete"
    db_session.add_all([turn, usage])
    await db_session.flush()
    usage_id = usage.id
    db_session.expunge_all()

    for filename in _later_revisions():
        await _run(db_session, "downgrade", filename)
    await _run(db_session, "downgrade")
    assert await _columns(db_session) == set()
    assert "landscape" not in await _check(db_session, "ck_assessment_chat_usage_subject")
    assert "landscape" not in await _check(db_session, "ck_assessment_chat_turns_one_subject")
    # The landscaping turn is gone; its ledger row stays, its subject cleared.
    assert (await db_session.execute(text(
        "SELECT count(*) FROM assessment_chat_turns WHERE user_id = :u"), {"u": user.id}
    )).scalar_one() == 0
    assert (await db_session.execute(text(
        "SELECT subject FROM assessment_chat_usage WHERE id = :id"), {"id": usage_id}
    )).one() == (None,)

    await _run(db_session, "upgrade")
    for filename in reversed(_later_revisions()):
        await _run(db_session, "upgrade", filename)
    assert {t for t, _c in await _columns(db_session)} == chat_tables
    assert "landscape_opportunity_id" in await _check(
        db_session, "ck_assessment_chat_turns_one_subject"
    )
    for table in ("assessment_chat_usage", "assessment_chat_opens"):
        assert "'landscape'" in await _check(db_session, f"ck_{table}_subject")


def test_postflight_expects_0078_objects():
    assert pf.DEFAULT_TARGET == "0078"
    assert "0078" in po.VERIFIED_REVISIONS
    planned = pf.planned_objects_between("0077", "0078")
    assert {(o.kind, o.table, o.name) for o in planned} == {
        ("column", "assessment_chat_turns", "landscape_opportunity_id"),
        ("column", "assessment_chat_usage", "landscape_opportunity_id"),
        ("column", "assessment_chat_opens", "landscape_opportunity_id"),
        ("constraint", "assessment_chat_turns", "fk_assessment_chat_turns_landscape_opportunity"),
        ("constraint", "assessment_chat_usage", "fk_assessment_chat_usage_landscape_opportunity"),
        ("constraint", "assessment_chat_opens", "fk_assessment_chat_opens_landscape_opportunity"),
        ("index", "assessment_chat_turns", "ix_assessment_chat_turns_landscape_conversation"),
    }
    assert pf.tables_created_between("0077", "0078") == frozenset()
    for table in ("assessment_chat_turns", "assessment_chat_usage", "assessment_chat_opens"):
        assert (table, "landscape_opportunity_id", "uuid", True, "") in po.EXPECTED_COLUMNS
    assert po.EXPECTED_INDEXES["ix_assessment_chat_turns_landscape_conversation"] == (
        "USING btree (landscape_opportunity_id, user_id, context_tier, created_at)")
    assert po.EXPECTED_CONSTRAINTS["fk_assessment_chat_turns_landscape_opportunity"] == (
        "assessment_chat_turns",
        "FOREIGN KEY (landscape_opportunity_id) REFERENCES landscape_opportunities(id)"
        " ON DELETE CASCADE")
    for table in ("assessment_chat_usage", "assessment_chat_opens"):
        assert po.EXPECTED_CONSTRAINTS[f"fk_{table}_landscape_opportunity"] == (
            table,
            "FOREIGN KEY (landscape_opportunity_id) REFERENCES landscape_opportunities(id)"
            " ON DELETE SET NULL")
        assert po.EXPECTED_CONSTRAINTS[f"ck_{table}_subject"] == (table, "'landscape'")
    assert po.EXPECTED_CONSTRAINTS["ck_assessment_chat_turns_one_subject"] == (
        "assessment_chat_turns",
        "num_nonnulls(assessment_id, project_id, landscape_opportunity_id) = 1")
