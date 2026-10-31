"""0077: the 42 landscape_* tables, their uniqueness rules (spec §11.1), the assessment's
one-complete-key CHECK (D59), the one-active-epoch index, and the downgrade round trip. The
database is at head, so the round trip first takes off any later revision, newest first."""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.landscaping.schema_check import schema_ready
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[3] / "alembic" / "versions"
_FILENAME = "0077_landscape.py"

TABLES = {
    "landscape_sources", "landscape_fetches", "landscape_labs", "landscape_evidence_cache",
    "landscape_documents", "landscape_document_links", "landscape_texts",
    "landscape_batches", "landscape_batch_items", "landscape_regression_sets",
    "landscape_epochs", "landscape_regression_items", "landscape_regression_runs",
    "landscape_screens", "landscape_extractions", "landscape_ideas", "landscape_idea_links",
    "landscape_extraction_links", "landscape_idea_evidence", "landscape_idea_aliases",
    "landscape_triage", "landscape_cuts", "landscape_candidate_groupings", "landscape_deep",
    "landscape_consults", "landscape_comparisons", "landscape_rankings", "landscape_similarity",
    "landscape_opportunities", "landscape_opportunity_versions", "landscape_assessments",
    "landscape_decisions", "landscape_reviews", "landscape_review_events",
    "landscape_review_assignments", "landscape_review_batches", "landscape_review_batch_items",
    "landscape_expert_judgments", "landscape_reject_audits", "landscape_rescues",
    "landscape_llm_calls", "landscape_reports",
}


def _later_revisions() -> list[str]:
    """Migration files above 0077, newest first: their downgrades run before 0077's."""
    later = [p.name for p in _VERSIONS.glob("0*.py") if int(p.name.split("_", 1)[0]) > 77]
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


async def _tables(db_session) -> set[str]:
    rows = (await db_session.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name LIKE 'landscape\\_%'"
    ))).scalars()
    return set(rows)


async def _insert(db_session, sql: str, **params) -> uuid.UUID:
    """Run one ``INSERT … RETURNING id`` and return the id."""
    return (await db_session.execute(text(sql), params)).scalar_one()


async def _expect_integrity_error(db_session, sql: str, constraint: str, **params) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await db_session.execute(text(sql), params)
    assert constraint in str(excinfo.value)


async def _epoch(db_session, status: str = "candidate") -> uuid.UUID:
    return await _insert(
        db_session,
        "INSERT INTO landscape_epochs (id, status) VALUES (gen_random_uuid(), :s) RETURNING id",
        s=status,
    )


async def _document(db_session, external_id: str) -> uuid.UUID:
    return await _insert(
        db_session,
        "INSERT INTO landscape_documents (id, kind, canonical_external_id, s1_status) "
        "VALUES (gen_random_uuid(), 'work', :ext, 'eligible') RETURNING id",
        ext=external_id,
    )


_IDEA = (
    "INSERT INTO landscape_ideas (id, epoch_id, idea_key, idea_type) "
    "VALUES (gen_random_uuid(), :epoch, :key, 'biology') RETURNING id"
)
_BATCH = (
    "INSERT INTO landscape_batches (id, stage, model, row_table) "
    "VALUES (gen_random_uuid(), 'screen', 'claude-sonnet-5', 'landscape_screens') RETURNING id"
)
_LEDGER = (
    "INSERT INTO landscape_llm_calls (id, stage, model, batch_id, item_seq) "
    "VALUES (gen_random_uuid(), 'screen', 'claude-sonnet-5', :batch, :seq) RETURNING id"
)
_ASSESSMENT = (
    "INSERT INTO landscape_assessments "
    "(id, epoch_id, opportunity_id, opportunity_version, grouping_id) "
    "VALUES (gen_random_uuid(), :epoch, :opp, :version, :grouping) RETURNING id"
)


async def test_upgrade_creates_every_landscape_table(db_session):
    assert len(TABLES) == 42
    assert await _tables(db_session) == TABLES


async def test_unique_constraints(db_session):
    # (kind, canonical_external_id) on documents.
    doc_a = await _document(db_session, "doi:10.1000/invented-a")
    doc_b = await _document(db_session, "doi:10.1000/invented-b")
    await _expect_integrity_error(
        db_session,
        "INSERT INTO landscape_documents (id, kind, canonical_external_id, s1_status) "
        "VALUES (gen_random_uuid(), 'work', 'doi:10.1000/invented-a', 'eligible')",
        "uq_landscape_documents_kind_external_id",
    )

    # (epoch_id, idea_key) on ideas.
    epoch = await _epoch(db_session)
    idea = await _insert(db_session, _IDEA, epoch=epoch, key="bio:HGNC:1|inhibit|MONDO:1")
    await _expect_integrity_error(
        db_session, _IDEA, "uq_landscape_ideas_epoch_key",
        epoch=epoch, key="bio:HGNC:1|inhibit|MONDO:1",
    )

    # (idea_id, idea_version, epoch_id) on triage.
    triage = (
        "INSERT INTO landscape_triage (id, idea_id, idea_version, epoch_id) "
        "VALUES (gen_random_uuid(), :idea, 'v1', :epoch)"
    )
    await db_session.execute(text(triage), {"idea": idea, "epoch": epoch})
    await _expect_integrity_error(
        db_session, triage, "uq_landscape_triage_idea_version_epoch", idea=idea, epoch=epoch,
    )

    # (epoch_id, doc_a, doc_b) on extraction links.
    link = (
        "INSERT INTO landscape_extraction_links (id, epoch_id, doc_a, doc_b) "
        "VALUES (gen_random_uuid(), :epoch, :a, :b)"
    )
    await db_session.execute(text(link), {"epoch": epoch, "a": doc_a, "b": doc_b})
    await _expect_integrity_error(
        db_session, link, "uq_landscape_extraction_links_epoch_pair",
        epoch=epoch, a=doc_a, b=doc_b,
    )

    # (epoch_id, grouping_key) on candidate groupings.
    cut = await _insert(
        db_session,
        "INSERT INTO landscape_cuts (id, epoch_id, n, k) "
        "VALUES (gen_random_uuid(), :epoch, 100, 1) RETURNING id",
        epoch=epoch,
    )
    grouping = (
        "INSERT INTO landscape_candidate_groupings "
        "(id, epoch_id, cut_id, grouping_key, member_idea_keys) "
        "VALUES (gen_random_uuid(), :epoch, :cut, :key, '[]'::jsonb)"
    )
    await db_session.execute(text(grouping), {"epoch": epoch, "cut": cut, "key": "a" * 64})
    await _expect_integrity_error(
        db_session, grouping, "uq_landscape_candidate_groupings_epoch_key",
        epoch=epoch, cut=cut, key="a" * 64,
    )

    # (opportunity_id, reviewer_id) on decisions.
    reviewer = await factories.make_user(db_session)
    opportunity = await _insert(
        db_session,
        "INSERT INTO landscape_opportunities (id) VALUES (gen_random_uuid()) RETURNING id",
    )
    decision = (
        "INSERT INTO landscape_decisions "
        "(id, opportunity_id, reviewer_id, reviewer_name, decision) "
        "VALUES (gen_random_uuid(), :opp, :reviewer, 'Invented Reviewer', 'pursue')"
    )
    await db_session.execute(text(decision), {"opp": opportunity, "reviewer": reviewer.id})
    await _expect_integrity_error(
        db_session, decision, "uq_landscape_decisions_opportunity_reviewer",
        opp=opportunity, reviewer=reviewer.id,
    )

    # (batch_id, item_seq) on batch items and on the ledger.
    batch = await _insert(db_session, _BATCH)
    item = (
        "INSERT INTO landscape_batch_items (id, batch_id, item_seq, row_id) "
        "VALUES (gen_random_uuid(), :batch, 1, gen_random_uuid())"
    )
    await db_session.execute(text(item), {"batch": batch})
    await _expect_integrity_error(
        db_session, item, "uq_landscape_batch_items_batch_seq", batch=batch,
    )
    await _insert(db_session, _LEDGER, batch=batch, seq=1)
    await _expect_integrity_error(
        db_session, _LEDGER, "uq_landscape_llm_calls_batch_seq", batch=batch, seq=1,
    )


async def test_ledger_allows_many_synchronous_rows(db_session):
    first = await _insert(db_session, _LEDGER, batch=None, seq=None)
    second = await _insert(db_session, _LEDGER, batch=None, seq=None)
    assert first != second


async def test_assessment_needs_one_complete_key(db_session):
    epoch = await _epoch(db_session)
    opportunity = await _insert(
        db_session,
        "INSERT INTO landscape_opportunities (id) VALUES (gen_random_uuid()) RETURNING id",
    )
    await _expect_integrity_error(
        db_session, _ASSESSMENT, "ck_landscape_assessments_one_key",
        epoch=epoch, opp=None, version=None, grouping=None,
    )
    await _expect_integrity_error(
        db_session, _ASSESSMENT, "ck_landscape_assessments_one_key",
        epoch=epoch, opp=opportunity, version=None, grouping=None,
    )
    # A complete opportunity key is accepted.
    await _insert(
        db_session, _ASSESSMENT, epoch=epoch, opp=opportunity, version=1, grouping=None,
    )


async def test_same_idea_key_in_two_epochs_allowed(db_session):
    first = await _epoch(db_session, "retired")
    second = await _epoch(db_session, "candidate")
    key = "bio:HGNC:2|activate|MONDO:2"
    a = await _insert(db_session, _IDEA, epoch=first, key=key)
    b = await _insert(db_session, _IDEA, epoch=second, key=key)
    assert a != b


async def test_one_active_epoch(db_session):
    await _epoch(db_session, "active")
    await _expect_integrity_error(
        db_session,
        "INSERT INTO landscape_epochs (id, status) VALUES (gen_random_uuid(), 'active')",
        "uq_landscape_epochs_one_active",
    )
    # Any number of non-active epochs coexist with it.
    await _epoch(db_session, "retired")
    await _epoch(db_session, "retired")


async def test_round_trip(db_session):
    later = _later_revisions()
    for filename in later:
        await _run(db_session, "downgrade", filename)
    await _run(db_session, "downgrade")
    assert await _tables(db_session) == set()
    assert await schema_ready(db_session) is False
    await _run(db_session, "upgrade")
    for filename in reversed(later):
        await _run(db_session, "upgrade", filename)
    assert await _tables(db_session) == TABLES
    assert await schema_ready(db_session) is True
