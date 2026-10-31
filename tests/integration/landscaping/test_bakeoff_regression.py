"""The bake-off and the regression gate against Postgres (spec §10.1, §10.2, §10.4; D43, D65,
X9).

Every model call is faked at ``bakeoff.call_sync`` (the regression gate sends through the same
helper), routed per stage to a function of the model and the user message; normalization and the
evidence lookups are faked too, so nothing leaves the process. Every session shares one
connection whose outer transaction rolls back. Every name below is invented.
"""
import contextlib
import json
import re
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs, ideas
from src.landscaping.eval import bakeoff, regression
from src.landscaping.llm_client import CallResult
from src.landscaping.schemas import DIMENSION_KEYS, GATING_KEYS, KEY_POINT_KEYS
from src.landscaping.scoring import TRIAGE_DIMENSIONS
from src.landscaping.stages import deep, extract
from src.models import AppSetting
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeComparison,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRegressionItem,
    LandscapeRegressionRun,
    LandscapeReport,
    LandscapeScreen,
    LandscapeSimilarity,
    LandscapeTriage,
)

pytestmark = pytest.mark.integration

HAIKU, SONNET, SONNET_55 = bakeoff.CANDIDATE_MODELS
REFERENCE = bakeoff.REFERENCE_MODEL
REFUSE = object()
_INDEX = re.compile(r"\bINV(\d+)\b")
_PAIR = re.compile(r"\bpair-(\d+)\b")


@pytest_asyncio.fixture
async def db_factory(engine):
    async with engine.connect() as conn:
        trans = await conn.begin()

        @contextlib.asynccontextmanager
        async def factory():
            session = AsyncSession(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield session
            finally:
                await session.close()

        try:
            yield factory
        finally:
            if trans.is_active:
                await trans.rollback()


def _user_text(request: dict) -> str:
    content = request["messages"][0]["content"]
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


class FakeModels:
    """``call_sync`` stand-in: ``routes[stage](model, user)`` gives the reply (a dict sent back
    as JSON, or ``REFUSE`` for a refusal the chain could not serve); ``tokens[model]`` is each
    reply's input-token count, so costs differ by model."""

    def __init__(self) -> None:
        self.routes: dict = {}
        self.tokens: dict[str, int] = {}
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, stage, request, *, db_factory, item_ref, skip_models=frozenset(),
                       ledger_stage=None):
        model, user = request["model"], _user_text(request)
        self.calls.append((stage, model, user))
        reply = self.routes[stage](model, user)
        usage = {"input_tokens": self.tokens.get(model, 0), "output_tokens": 0}
        if reply is REFUSE:
            return CallResult(None, "", "refusal", "bio", model, usage, None)
        return CallResult(SimpleNamespace(), json.dumps(reply), "end_turn", None, model, usage,
                          None)

    def stages(self) -> set[str]:
        return {stage for stage, _, _ in self.calls}


@pytest.fixture
def models(monkeypatch):
    fake = FakeModels()
    monkeypatch.setattr(bakeoff, "call_sync", fake)
    return fake


@pytest.fixture
def offline(monkeypatch):
    """Normalization keeps each candidate as written; evidence and S6 records find nothing."""

    async def as_written(idea, *, annotations, criteria):
        return dict(idea)

    async def no_evidence(db, *, targets, disease, agent):
        return {"evidence_summary": {}, "data_flags": {}, "errors": {}}

    async def no_records(db_factory, idea):
        return {}

    monkeypatch.setattr(extract, "_normalized", as_written)
    monkeypatch.setattr(ideas, "evidence_payload", no_evidence)
    monkeypatch.setattr(deep, "_records", no_records)


# --- fixtures in the database --------------------------------------------------------------------


def _quote(i: int) -> str:
    return f"knockdown of INV{i} slowed an invented carcinoma in mice"


async def _epoch(db_factory, *, status="candidate", changed=None, after_now=False) -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(
            status=status, components={}, snapshot=epochs.build_snapshot(),
            changed_stages=changed,
        )
        if after_now:
            epoch.created_at = func.clock_timestamp()
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _documents(db_factory, n: int) -> None:
    async with db_factory() as db:
        db.add_all([
            LandscapeDocument(
                kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
                title=f"INV{i} in an invented carcinoma", summary_text=f"We show {_quote(i)}.",
                summary_hash=uuid.uuid4().hex, published_on=date(2025 + i % 2, 3, 1),
                document_type="article",
                details={"extra": {"primary_topic": "Invented Oncology"}},
                lab_key=f"lab:invented-{i}", s1_status="eligible",
            )
            for i in range(n)
        ])
        await db.commit()


async def _seed_set(db_factory, items: list[dict]) -> int:
    async with db_factory() as db:
        version = await bakeoff.new_set_version(db, notes="invented test set")
        db.add_all([
            LandscapeRegressionItem(set_version=version, origin=item.pop("origin", "bakeoff"),
                                    **item)
            for item in items
        ])
        await db.commit()
        return version


def _screen_items(n: int, decision: str = "yes") -> list[dict]:
    return [
        {"item_kind": "screen", "item_ref": {"document_id": str(uuid.uuid4()),
                                             "user": f"Title: INV{i} invented"},
         "expected": {"decision": decision}}
        for i in range(n)
    ]


def _index(user: str) -> int:
    return int(_INDEX.search(user).group(1))


def _screen(answer: str):
    return lambda model, user: {"in_scope": answer, "research_kind": [], "reason": "Invented."}


def _extraction(i: int) -> dict:
    return {
        "ideas": [{
            "idea_type": "biology",
            "targets": [{"as_written": f"INV{i}", "kind": "gene", "species": "human",
                         "hgnc_id": f"HGNC:9{i:04d}"}],
            "direction": "inhibit",
            "agent": {"as_written": "", "kind": "other"},
            "disease": {"as_written": "invented carcinoma", "mondo_id": "MONDO:9999999"},
            "stage": "target_validation",
            "evidence_grade": "in_vivo",
            "evidence_summary": f"INV{i} loss slows an invented carcinoma.",
            "target_rubric": [],
            "key_results": [{"claim": "Growth fell.", "quote": _quote(i), "section": "abstract"}],
        }],
        "linked_publications": [],
    }


def _triage(shift: int = 0) -> dict:
    return {
        "gating": {key: "met" for key in GATING_KEYS},
        "scores": {key: 3 + shift for key in TRIAGE_DIMENSIONS},
        "in_therapeutic_scope": True,
        "target_rubric": [],
    }


def _all_agree(fake: FakeModels) -> None:
    fake.routes = {
        "screen": _screen("yes"),
        "extract": lambda model, user: _extraction(_index(user)),
        "triage": lambda model, user: _triage(),
    }


def _verdict() -> dict:
    return {
        "company_or_project": "Invented marsh programme",
        "headline": "Blocking INV1 slows an invented carcinoma",
        "key_points": {key: [f"An invented point on {key}."] for key in KEY_POINT_KEYS},
        "gating": {key: "met" for key in GATING_KEYS},
        "scores": {key: 2 + n % 4 for n, key in enumerate(DIMENSION_KEYS)},
        "recommendation": "conditional",
        "confidence": "Moderate",
    }


async def _report(db_factory, report_id: uuid.UUID) -> dict:
    async with db_factory() as db:
        return (await db.get(LandscapeReport, report_id)).payload


async def _epoch_row(db_factory, epoch_id: uuid.UUID) -> LandscapeEpoch:
    async with db_factory() as db:
        return await db.get(LandscapeEpoch, epoch_id)


# --- the bake-off --------------------------------------------------------------------------------


async def test_cheapest_passing_model_chosen(db_factory, models, offline):
    await _epoch(db_factory)
    await _documents(db_factory, 10)
    _all_agree(models)
    models.routes["screen"] = lambda model, user: {
        "in_scope": "no" if model == HAIKU else "yes", "reason": "Invented."
    }
    models.tokens = {HAIKU: 10, SONNET: 1000, SONNET_55: 100}

    payload = await _report(
        db_factory, await bakeoff.run_bakeoff(db_factory, sample_sizes=(10, 5, 5))
    )

    screen = payload["stages"]["screen"]
    assert screen["candidates"][HAIKU]["passed"] is False
    assert screen["pick"] == SONNET_55 and screen["passed"] is True
    assert payload["stages"]["extract"]["pick"] == HAIKU
    assert payload["stages"]["triage"]["pick"] == HAIKU
    assert payload["recommended_settings"] == {
        "LANDSCAPE_SCREEN_MODEL": SONNET_55,
        "LANDSCAPE_EXTRACT_MODEL": HAIKU,
        "LANDSCAPE_TRIAGE_MODEL": HAIKU,
    }
    assert payload["shortfall"] == []


async def test_bakeoff_writes_no_stage_rows(db_factory, models, offline):
    await _epoch(db_factory)
    await _documents(db_factory, 6)
    _all_agree(models)

    report_id = await bakeoff.run_bakeoff(db_factory, sample_sizes=(6, 3, 3))

    async with db_factory() as db:
        for model in (LandscapeScreen, LandscapeExtraction, LandscapeIdea, LandscapeTriage,
                      LandscapeDeep, LandscapeComparison, LandscapeAssessment):
            assert await db.scalar(select(func.count()).select_from(model)) == 0, model
        version = (await db.get(LandscapeReport, report_id)).payload["regression_set_version"]
        kinds = (
            await db.scalars(
                select(LandscapeRegressionItem.item_kind)
                .where(LandscapeRegressionItem.set_version == version)
            )
        ).all()
    assert sorted(set(kinds)) == ["extract", "screen", "triage"]
    assert kinds.count("screen") == 6
    stages = models.stages()
    assert stages == {"screen", "extract", "triage"}


async def test_no_pass_picks_best_haiku_or_sonnet_and_flags(db_factory, models, offline):
    await _epoch(db_factory)
    await _documents(db_factory, 10)
    _all_agree(models)
    # Each candidate screens out some of what the reference keeps: none reaches recall 0.95.
    misses = {HAIKU: 10, SONNET: 5, SONNET_55: 2}
    models.routes["screen"] = lambda model, user: {
        "in_scope": "no" if model != REFERENCE and _index(user) < misses[model] else "yes",
    }

    payload = await _report(
        db_factory, await bakeoff.run_bakeoff(db_factory, sample_sizes=(10, 5, 5))
    )

    screen = payload["stages"]["screen"]
    assert screen["pick"] == SONNET_55
    assert screen["passed"] is False and screen["shortfall"] is True
    assert payload["shortfall"] == ["screen"]
    assert payload["recommended_settings"]["LANDSCAPE_SCREEN_MODEL"] == SONNET_55


async def test_reference_refusals_excluded_from_agreement(db_factory, models, offline):
    await _epoch(db_factory)
    await _documents(db_factory, 10)
    _all_agree(models)

    def screen(model, user):
        refused = _index(user) < 2
        if model == REFERENCE and refused:
            return REFUSE
        # The candidates screen the two refused documents out: counted, recall would fall.
        return {"in_scope": "no" if refused else "yes"}

    models.routes["screen"] = screen

    report_id = await bakeoff.run_bakeoff(db_factory, sample_sizes=(10, 5, 5))

    payload = await _report(db_factory, report_id)
    stage = payload["stages"]["screen"]
    assert stage["reference_refused"] == 2
    assert all(stage["candidates"][m]["recall"] == 1.0 for m in bakeoff.CANDIDATE_MODELS)
    assert all(stage["candidates"][m]["items"] == 8 for m in bakeoff.CANDIDATE_MODELS)
    async with db_factory() as db:
        seeded = await db.scalar(
            select(func.count()).select_from(LandscapeRegressionItem).where(
                LandscapeRegressionItem.set_version == payload["regression_set_version"],
                LandscapeRegressionItem.item_kind == "screen",
            )
        )
    assert seeded == 8


async def test_s4_repeatability_metric(db_factory, models):
    epoch_id = await _epoch(db_factory)
    async with db_factory() as db:
        for i in range(4):
            idea = LandscapeIdea(
                epoch_id=epoch_id, idea_key=f"biology;INV{i};inhibit;invented", idea_type="biology",
                version_hash=f"v{i}",
            )
            db.add(idea)
            await db.flush()
            db.add(LandscapeTriage(
                idea_id=idea.id, idea_version=f"v{i}", epoch_id=epoch_id, status="done",
                raw={"dossier": f"Dossier of INV{i}"},
            ))
        await db.commit()
    asked: dict[int, int] = {}

    def triage(model, user):
        i = _index(user)
        asked[i] = asked.get(i, 0) + 1
        # The second answer on INV3 moves every score by 2: not within ±1.
        return _triage(shift=2 if i == 3 and asked[i] == 2 else 0)

    models.routes["triage"] = triage

    result = await bakeoff.run_s4_repeatability(db_factory, n=50)

    assert result["ideas"] == 4
    assert result["consistent"] == 3
    assert result["share"] == 0.75
    assert result["model"] == epochs.build_snapshot()["models"]["triage"]
    assert asked == {0: 2, 1: 2, 2: 2, 3: 2}


# --- the regression gate -------------------------------------------------------------------------


async def test_regression_fail_holds_epoch(db_factory, models):
    await _seed_set(db_factory, _screen_items(10))
    epoch_id = await _epoch(db_factory)
    models.routes["screen"] = _screen("no")

    assert await regression.run_regression(db_factory, epoch_id) is False

    epoch = await _epoch_row(db_factory, epoch_id)
    assert epoch.status == "held"
    assert "screen" in epoch.hold_reason
    async with db_factory() as db:
        run = await db.get(LandscapeRegressionRun, epoch.regression_run_id)
    assert run.status == "failed"
    assert run.results["stages"]["screen"]["passed"] is False


async def test_regression_pass_allows_activation(db_factory, models):
    await _seed_set(db_factory, _screen_items(10))
    epoch_id = await _epoch(db_factory)
    models.routes["screen"] = _screen("yes")

    assert await regression.run_regression(db_factory, epoch_id) is True
    assert (await _epoch_row(db_factory, epoch_id)).status == "candidate"

    async def no_hook(db, old_id, new_id):
        return None

    async with db_factory() as db:
        await epochs.activate(db, epoch_id, on_activate=no_hook)
    assert (await _epoch_row(db_factory, epoch_id)).status == "active"


def _pair_items(n: int) -> list[dict]:
    return [
        {"item_kind": "pair", "origin": "frozen",
         "item_ref": {"first": f"pair-{i} first", "second": f"pair-{i} second"},
         "expected": {"winner": "A"}}
        for i in range(n)
    ]


def _final_items(n: int) -> list[dict]:
    verdict = _verdict()
    return [
        {"item_kind": "final", "origin": "frozen", "item_ref": {"user": f"Opportunity {i}"},
         "expected": {"recommendation": verdict["recommendation"],
                      "gating": verdict["gating"], "scores": verdict["scores"]}}
        for i in range(n)
    ]


async def test_final_md_only_change_runs_s9_items(db_factory, models):
    await _epoch(db_factory, status="active")
    await _seed_set(db_factory, _screen_items(5) + _pair_items(5) + _final_items(5))
    epoch_id = await _epoch(db_factory, changed=["final"])
    models.routes["final"] = lambda model, user: _verdict()

    assert await regression.run_regression(db_factory, epoch_id) is True

    assert models.stages() == {"final"}
    assert len(models.calls) == 5
    async with db_factory() as db:
        run = await db.scalar(
            select(LandscapeRegressionRun).where(LandscapeRegressionRun.epoch_id == epoch_id)
        )
    assert set(run.results["stages"]) == {"final"}


async def test_s7_pairs_pass_at_80_percent(db_factory, models):
    await _epoch(db_factory, status="active")
    await _seed_set(db_factory, _pair_items(10))
    agreeing = {"n": 8}
    models.routes["rerank"] = lambda model, user: {
        "winner": "A" if int(_PAIR.search(user).group(1)) < agreeing["n"] else "B",
        "reason": "Invented.",
    }

    at_80 = await _epoch(db_factory, changed=["rerank"])
    assert await regression.run_regression(db_factory, at_80) is True

    agreeing["n"] = 7
    at_70 = await _epoch(db_factory, changed=["rerank"])
    assert await regression.run_regression(db_factory, at_70) is False
    assert (await _epoch_row(db_factory, at_70)).status == "held"


async def test_s8_similarity_pass_at_85_percent(db_factory, models):
    await _epoch(db_factory, status="active")
    await _seed_set(db_factory, [
        {"item_kind": "similarity", "origin": "frozen",
         "item_ref": {"user": f"# Idea A\n\npair-{i} a\n\n# Idea B\n\npair-{i} b"},
         "expected": {"verdict": "same"}}
        for i in range(20)
    ])
    agreeing = {"n": 17}
    models.routes["similarity"] = lambda model, user: {
        "same_program": int(_PAIR.search(user).group(1)) < agreeing["n"], "reason": "Invented.",
    }

    at_85 = await _epoch(db_factory, changed=["similarity"])
    assert await regression.run_regression(db_factory, at_85) is True

    agreeing["n"] = 16
    at_80 = await _epoch(db_factory, changed=["similarity"])
    assert await regression.run_regression(db_factory, at_80) is False


async def test_first_epoch_checks_s2_s4_only(db_factory, models):
    await _seed_set(db_factory, _screen_items(5) + _pair_items(5) + _final_items(5))
    epoch_id = await _epoch(db_factory)
    models.routes["screen"] = _screen("yes")

    assert await regression.run_regression(db_factory, epoch_id) is True

    assert models.stages() == {"screen"}
    async with db_factory() as db:
        run = await db.scalar(
            select(LandscapeRegressionRun).where(LandscapeRegressionRun.epoch_id == epoch_id)
        )
    assert run.results["first_epoch"] is True
    assert set(run.results["stages"]) == {"screen"}


async def _active_epoch_with_outputs(db_factory) -> uuid.UUID:
    """An active epoch with one assessed opportunity of one idea, an S6 record, one answered
    comparison and one similarity verdict."""
    epoch_id = await _epoch(db_factory, status="active")
    verdict = _verdict()
    async with db_factory() as db:
        first, second = (
            LandscapeIdea(epoch_id=epoch_id, idea_key=f"biology;INV{i};inhibit;invented",
                          idea_type="biology", version_hash=f"v{i}",
                          targets=[{"as_written": f"INV{i}", "hgnc_id": f"HGNC:9{i:04d}"}])
            for i in (1, 2)
        )
        db.add_all([first, second])
        await db.flush()
        db.add(LandscapeDeep(
            idea_id=first.id, idea_version="v1", epoch_id=epoch_id, status="done",
            summary="An invented S6 record.", gating=verdict["gating"],
            provisional_scores=verdict["scores"],
        ))
        opportunity = LandscapeOpportunity(version_counter=1)
        db.add(opportunity)
        await db.flush()
        db.add(LandscapeOpportunityVersion(
            opportunity_id=opportunity.id, version=1, epoch_id=epoch_id, status="done",
            member_idea_keys=[first.idea_key], label="Invented programme",
        ))
        db.add(LandscapeAssessment(
            opportunity_id=opportunity.id, opportunity_version=1, epoch_id=epoch_id,
            status="done", recommendation="conditional", gating=verdict["gating"],
            scores=verdict["scores"],
        ))
        db.add(LandscapeComparison(
            idea_a=first.id, version_a="v1", idea_b=second.id, version_b="v2",
            epoch_id=epoch_id, basis="deep", ask_order=2, status="done",
            winner_idea_id=first.id,
        ))
        db.add(LandscapeSimilarity(
            idea_a=first.id, version_a="v1", idea_b=second.id, version_b="v2",
            epoch_id=epoch_id, verdict="same",
        ))
        await db.commit()
    return epoch_id


async def test_refresh_after_activation_freezes_new_baseline(db_factory, models, offline):
    first_version = await _seed_set(db_factory, _screen_items(3))
    active_id = await _active_epoch_with_outputs(db_factory)

    version = await regression.refresh_after_activation(db_factory, epoch_id=active_id)

    assert version == first_version + 1
    async with db_factory() as db:
        items = (
            await db.scalars(
                select(LandscapeRegressionItem)
                .where(LandscapeRegressionItem.set_version == version)
            )
        ).all()
    by_kind = {}
    for item in items:
        by_kind.setdefault(item.item_kind, []).append(item)
    assert len(by_kind["screen"]) == 3  # the bake-off's items carry forward
    assert [i.expected["recommendation"] for i in by_kind["final"]] == ["conditional"]
    assert by_kind["deep"][0].expected["gating"] == _verdict()["gating"]
    # Asked with idea B shown first, so the stored winner (idea A's row) is answer B.
    assert by_kind["pair"][0].expected == {"winner": "B"}
    assert by_kind["similarity"][0].expected == {"verdict": "same"}
    assert {i.origin for i in by_kind["final"] + by_kind["deep"]} == {"frozen"}
    assert models.calls == []

    candidate = await _epoch(db_factory)
    with pytest.raises(ValueError):
        await regression.refresh_after_activation(db_factory, epoch_id=candidate)


async def test_positive_controls_join_the_set(db_factory, models):
    async with db_factory() as db:
        db.add(LandscapeDocument(
            kind="work", canonical_external_id="doi:10.9999/invented-control",
            doi="10.9999/invented-control", title="INV7 in an invented carcinoma",
            summary_text=f"We show {_quote(7)}.", s1_status="eligible",
        ))
        db.add(AppSetting(key="landscape_positive_controls",
                          value=json.dumps(["10.9999/invented-control", "10.9999/never-stored"])))
        await db.commit()

    version = await regression.freeze_regression_set(db_factory, reason="invented test")

    async with db_factory() as db:
        items = (
            await db.scalars(
                select(LandscapeRegressionItem).where(
                    LandscapeRegressionItem.set_version == version,
                    LandscapeRegressionItem.origin == "positive_control",
                )
            )
        ).all()
    assert len(items) == 1
    assert items[0].item_kind == "screen"
    assert items[0].expected == {"kept": True}
    assert "INV7" in items[0].item_ref["user"]


async def test_set_versioned_additions_do_not_change_running_candidate(db_factory, models):
    first = await _seed_set(db_factory, _screen_items(10, "yes"))
    epoch_id = await _epoch(db_factory, after_now=True)
    # Later versions: one frozen from the database, one whose references the candidate fails.
    frozen = await regression.freeze_regression_set(db_factory, reason="invented later version")
    failing = await _seed_set(db_factory, _screen_items(10, "no"))
    assert first < frozen < failing
    models.routes["screen"] = _screen("yes")

    assert await regression.run_regression(db_factory, epoch_id) is True

    epoch = await _epoch_row(db_factory, epoch_id)
    assert epoch.regression_set_version == first
    async with db_factory() as db:
        run = await db.get(LandscapeRegressionRun, epoch.regression_run_id)
        first_items = await db.scalar(
            select(func.count()).select_from(LandscapeRegressionItem)
            .where(LandscapeRegressionItem.set_version == first)
        )
    assert run.set_version == first
    assert first_items == 10
