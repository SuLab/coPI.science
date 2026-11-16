"""Pipeline metrics and reports against Postgres (spec §5.1 "Drift", §10.2, §10.3, §10.6;
L32, D63, D68).

Every session shares one connection whose outer transaction rolls back. Documents, labs,
institutions and identifiers are invented. The S4 repeatability run is patched: the pilot
report must make no model call in a test.
"""
import contextlib
import itertools
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import ledger, reports
from src.models import AppSetting
from src.models.landscape import (
    LandscapeCut,
    LandscapeDecision,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeOpportunity,
    LandscapeReport,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeReviewEvent,
    LandscapeScreen,
    LandscapeTriage,
)

pytestmark = pytest.mark.integration

_counter = itertools.count()
#: Sonnet 5 at $2 / MTok in and $10 / MTok out: 1,000 in and 2,000 out cost $0.022.
USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 2_000,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


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


async def _call(db, *, stage="screen", model="claude-sonnet-5", usage=USAGE) -> None:
    await ledger.record_call(
        db, stage=stage, model=model, batch_id=None, item_seq=None, custom_id=None,
        usage=usage, refusal_category=None, served_by=model, batch=False, latency_ms=None,
    )


def _doc(**kw) -> LandscapeDocument:
    n = next(_counter)
    data = dict(
        kind="work", canonical_external_id=f"doi:10.9999/invented-report-{n}-{uuid.uuid4().hex}",
        s1_status="eligible", published_on=date(2026, 1, 5),
    )
    data.update(kw)
    return LandscapeDocument(**data)


async def _epoch(db) -> LandscapeEpoch:
    epoch = LandscapeEpoch(status="candidate")
    db.add(epoch)
    await db.flush()
    return epoch


async def _opportunity(db) -> uuid.UUID:
    opp = LandscapeOpportunity()
    db.add(opp)
    await db.flush()
    return opp.id


def _decision(opportunity_id, decision: str) -> LandscapeDecision:
    return LandscapeDecision(
        opportunity_id=opportunity_id, reviewer_name=f"Invented Reviewer {next(_counter)}",
        decision=decision,
    )


async def test_cost_per_useful_opportunity(db_factory):
    async with db_factory() as db:
        await _call(db)
        await _call(db)
        pursued, approved, passed, cleared = [await _opportunity(db) for _ in range(4)]
        db.add_all([
            _decision(pursued, "pursue"),
            _decision(pursued, "pursue"),
            _decision(passed, "pass"),
            LandscapeReviewEvent(opportunity_id=approved, action="approved",
                                 actor_name="Invented Manager"),
            LandscapeReviewEvent(opportunity_id=cleared, action="cleared",
                                 actor_name="Invented Manager"),
        ])
        await db.commit()
        metrics = await reports.pipeline_metrics(db)
    cost = metrics["cost_per_useful_opportunity"]
    # $0.044 spent; one pursued and one approved opportunity are useful (L32).
    assert cost["useful"] == 2
    assert cost["spend_usd"] == pytest.approx(0.044)
    assert cost["usd"] == pytest.approx(0.022)
    assert metrics["spend_by_day"]
    assert sum(day.get("screen", 0) for day in metrics["spend_by_day"].values()) == (
        pytest.approx(0.044)
    )


async def test_counts_by_institution(db_factory):
    async with db_factory() as db:
        kept = _doc(qualifying_institution="I145311948", openalex_id="W900001")
        db.add_all([
            kept,
            _doc(qualifying_institution="I145311948", pmid="99000001"),
            _doc(qualifying_institution="I1298154867", openalex_id="W900002"),
            _doc(qualifying_institution=None, s1_status="excluded:not_qualified",
                 kind="grant", external_ids={"appl_id": "9990001"}),
        ])
        await db.flush()
        # A tombstone is history, never counted.
        db.add(_doc(qualifying_institution="I1298154867", merged_into=kept.id))
        await db.commit()
        metrics = await reports.pipeline_metrics(db)
    counts = metrics["counts"]
    assert counts["by_institution"] == {"I145311948": 2, "I1298154867": 1, "unknown": 1}
    assert counts["by_source"] == {"openalex": 2, "pubmed": 1, "reporter": 1}
    assert counts["by_stage"]["s1"] == {"eligible": 3, "excluded:not_qualified": 1}


async def test_drift_lists_unknown_hopkins_institutions(db_factory):
    authorship = {
        "author": {"display_name": "Invented Author"},
        "institutions": [
            {"id": "https://openalex.org/I145311948", "display_name": "Johns Hopkins University"},
            # Excluded by §5.1 (not in Baltimore): already decided, never proposed.
            {"id": "https://openalex.org/I2802946424",
             "display_name": "Johns Hopkins University Applied Physics Laboratory"},
            {"id": "https://openalex.org/I999000001",
             "display_name": "Johns Hopkins Invented Annex"},
            {"id": "https://openalex.org/I999000002", "display_name": "Invented Lineage Center",
             "lineage": ["https://openalex.org/I999000002", "https://openalex.org/I2799853436"]},
            {"id": "https://openalex.org/I999000003", "display_name": "Invented Other College"},
        ],
    }
    async with db_factory() as db:
        db.add_all([_doc(qualifying_authorships=[authorship]),
                    _doc(qualifying_authorships=[authorship])])
        await db.commit()
        report = await reports.drift_report(db)
    listed = {entry["id"]: entry for entry in report["unknown_institutions"]}
    assert set(listed) == {"I999000001", "I999000002"}
    assert listed["I999000001"]["display_name"] == "Johns Hopkins Invented Annex"
    assert listed["I999000001"]["documents"] == 2
    assert report["documents_checked"] == 2


async def _setting(db, key: str, value: str) -> None:
    db.add(AppSetting(key=key, value=value))
    await db.flush()


async def test_positive_control_trace_names_dropping_stage(db_factory):
    async with db_factory() as db:
        empty = await reports.positive_control_trace(db)
        assert empty["controls"] == [] and empty["note"]

        epoch = await _epoch(db)
        screened_out = _doc(doi="10.9999/invented-control-a")
        triaged_out = _doc(doi="10.9999/invented-control-b")
        in_cut = _doc(doi="10.9999/invented-control-c")
        filtered = _doc(doi="10.9999/invented-control-d", s1_status="excluded:out_of_window")
        db.add_all([screened_out, triaged_out, in_cut, filtered])
        await db.flush()
        db.add(LandscapeScreen(document_id=screened_out.id, epoch_id=epoch.id,
                               status="excluded:screen", decision="no"))
        ideas = {}
        for doc, triage_status in ((triaged_out, "excluded:gate"), (in_cut, "done")):
            db.add(LandscapeScreen(document_id=doc.id, epoch_id=epoch.id, status="done",
                                   decision="yes"))
            extraction = LandscapeExtraction(document_id=doc.id, epoch_id=epoch.id,
                                             basis="summary", status="done")
            idea = LandscapeIdea(epoch_id=epoch.id, idea_type="biology", version_hash="v1",
                                 idea_key=f"invented-control-idea-{uuid.uuid4().hex}")
            db.add_all([extraction, idea])
            await db.flush()
            db.add(LandscapeIdeaEvidence(idea_id=idea.id, extraction_id=extraction.id,
                                         candidate_index=0))
            db.add(LandscapeTriage(idea_id=idea.id, idea_version="v1", epoch_id=epoch.id,
                                   status=triage_status))
            ideas[doc.id] = idea.id
        db.add(LandscapeCut(epoch_id=epoch.id, status="complete", n=2, k=1,
                            ordered_idea_ids=[str(ideas[in_cut.id]), str(ideas[triaged_out.id])]))
        await _setting(db, "landscape_positive_controls", (
            "10.9999/invented-control-a, 10.9999/invented-control-b; "
            "10.9999/invented-control-c 10.9999/invented-control-d 10.9999/invented-missing"
        ))
        await db.commit()
        trace = await reports.positive_control_trace(db)

    assert trace["note"] is None
    by_id = {c["identifier"]: c for c in trace["controls"]}
    a = by_id["doi:10.9999/invented-control-a"]
    assert a["dropped_at"] == "S2" and a["stages"] == {"S1": "passed", "S2": "dropped"}
    b = by_id["doi:10.9999/invented-control-b"]
    assert b["dropped_at"] == "S4"
    assert b["stages"] == {"S1": "passed", "S2": "passed", "S3": "passed", "S4": "dropped"}
    c = by_id["doi:10.9999/invented-control-c"]
    assert c["dropped_at"] is None and c["stages"]["S5"] == "passed"
    assert by_id["doi:10.9999/invented-control-d"]["dropped_at"] == "S1"
    missing = by_id["doi:10.9999/invented-missing"]
    assert missing["document_id"] is None and missing["pending_at"] == "S1"


async def _blind_batch(db, issued_on: date) -> None:
    batch = LandscapeReviewBatch(issued_on=issued_on, blind=True)
    db.add(batch)
    await db.flush()
    in_cut, probe = await _opportunity(db), await _opportunity(db)
    db.add_all([
        LandscapeReviewBatchItem(batch_id=batch.id, opportunity_id=in_cut, position=1),
        LandscapeReviewBatchItem(batch_id=batch.id, opportunity_id=probe, is_probe=True,
                                 position=0),
        _decision(in_cut, "pursue"),
        _decision(in_cut, "pursue"),
        _decision(probe, "pass"),
        _decision(probe, "pursue"),
    ])
    await db.flush()


async def test_blind_metric_hidden_until_superseded(db_factory):
    async with db_factory() as db:
        await _blind_batch(db, date(2026, 3, 2))
        await db.commit()
        hidden = await reports.pipeline_metrics(db)
        assert hidden["blind_results"] == []

        db.add(LandscapeReviewBatch(issued_on=date(2026, 3, 9), blind=False))
        await db.commit()
        shown = await reports.pipeline_metrics(db)

    (result,) = shown["blind_results"]
    assert result["issued_on"] == "2026-03-02"
    assert result["in_cut"] == {"decisions": 2, "pursued": 2, "pursue_rate": 1.0}
    assert result["probes"] == {"decisions": 2, "pursued": 1, "pursue_rate": 0.5}


async def test_pilot_report_holds_every_section(db_factory, monkeypatch):
    repeatability = {"ideas": 50, "identical_gates_and_scores_within_1": 0.92}

    async def _repeat(factory, *, n=50):
        return repeatability

    monkeypatch.setattr(reports.bakeoff, "run_s4_repeatability", _repeat)
    async with db_factory() as db:
        db.add(LandscapeReport(kind="bakeoff", payload={"picks": {"screen": "invented-model"}}))
        db.add(LandscapeReport(kind="probes", payload={
            "probe": "reporter-pi", "at": "2026-01-01T00:00:00+00:00", "finding": {"orcid": False},
        }))
        db.add(LandscapeFetch(source="openalex", slice_key="month:2026-01", status="complete",
                              expected_count=100, received_count=100))
        await _call(db, stage="deep", model="claude-opus-5",
                    usage={**USAGE, "output_tokens": 16_000})
        await _call(db, stage="deep", model="claude-opus-5")
        await _call(db, stage="final", model="claude-opus-5")
        await db.commit()

    report_id = await reports.pilot_report(db_factory)

    async with db_factory() as db:
        report = await db.get(LandscapeReport, report_id)
    assert report.kind == "pilot"
    payload = report.payload
    assert {
        "bakeoff", "probes", "completeness", "s4_repeatability", "normalization",
        "positive_controls", "truncation", "cost_throughput",
    } <= set(payload)
    assert payload["bakeoff"]["payload"] == {"picks": {"screen": "invented-model"}}
    assert payload["probes"]["reporter-pi"]["finding"] == {"orcid": False}
    assert payload["s4_repeatability"] == repeatability
    assert "slices" in payload["completeness"] and "by_source" in payload["completeness"]
    assert {"rate", "ambiguous_names"} <= set(payload["normalization"])
    # No control was entered, and the report says so (§10.2).
    assert payload["positive_controls"]["controls"] == []
    assert payload["positive_controls"]["note"]
    assert payload["truncation"]["deep"]["calls"] == 2
    assert payload["truncation"]["deep"]["truncated"] == 1
    assert payload["truncation"]["final"]["truncated"] == 0
    deep_cost = payload["cost_throughput"]["stages"]["deep"]
    assert deep_cost["calls"] == 2
    assert deep_cost["projected_cost_usd"] == pytest.approx(
        deep_cost["cost_usd"] * payload["cost_throughput"]["backfill_factor"]
    )
