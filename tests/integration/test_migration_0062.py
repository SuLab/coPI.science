"""0062: publications provenance/exclusion/doi_verified, publication_candidates,
researcher_profiles.human_edited_at / evidence_flagged_count,
agents.persona_export_failed_at; the FK rules under a raw DELETE FROM users; the
provenance and status checks; the downgrade. The database is at head."""
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models import Publication
from src.models.publication import CANDIDATE_STATUSES, PROVENANCE_CHECK_SQL
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _m0062():
    spec = importlib.util.spec_from_file_location(
        "_m0062", _VERSIONS / "0062_corpus_provenance_profile_drafts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _run(db_session, name: str) -> None:
    mod = _m0062()
    conn = await db_session.connection()

    def _call(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mod, name)()

    await conn.run_sync(_call)


async def _columns(db_session, table: str) -> set[str]:
    return set((await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    ), {"t": table})).scalars())


async def _candidate(db_session, user_id, pmid="1", status="pending", decided_by=None):
    await db_session.execute(text(
        "INSERT INTO publication_candidates (id, user_id, pmid, title, reason, status, "
        "decided_by_user_id) VALUES (:i, :u, :p, 't', 'no_orcid_anchor', :s, :d)"),
        {"i": uuid.uuid4(), "u": user_id, "p": pmid, "s": status, "d": decided_by})


def test_the_constants_equal_the_model_values():
    mod = _m0062()
    assert mod.PROVENANCE_CHECK_SQL == PROVENANCE_CHECK_SQL
    assert tuple(mod.CANDIDATE_STATUSES) == CANDIDATE_STATUSES


async def test_both_check_constraints_exist(db_session):
    """The parity test does not compare CHECK constraints; this does."""
    names = set((await db_session.execute(text(
        "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname IN "
        "('ck_publications_provenance', 'ck_publication_candidates_status')"
    ))).scalars())
    assert names == {"ck_publications_provenance", "ck_publication_candidates_status"}


async def test_raw_user_delete_cascades_candidates_and_nulls_actor_columns(db_session):
    pi = await factories.make_user(db_session)
    staff = await factories.make_user(db_session)
    await _candidate(db_session, pi.id, decided_by=staff.id, status="rejected")
    other = await factories.make_user(db_session)
    await _candidate(db_session, other.id, decided_by=staff.id, status="accepted")
    pub = Publication(user_id=other.id, pmid="9", title="t", excluded_by_user_id=staff.id)
    db_session.add(pub)
    await db_session.flush()

    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": pi.id})
    assert (await db_session.execute(text(
        "SELECT count(*) FROM publication_candidates WHERE user_id = :u"), {"u": pi.id}
    )).scalar_one() == 0

    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": staff.id})
    assert (await db_session.execute(text(
        "SELECT decided_by_user_id FROM publication_candidates WHERE user_id = :u"),
        {"u": other.id})).scalar_one() is None
    assert (await db_session.execute(text(
        "SELECT excluded_by_user_id FROM publications WHERE user_id = :u"), {"u": other.id}
    )).scalar_one() is None


@pytest.mark.parametrize("value", ["manual", "unanchored", "s1", "s3", "s1,s3", "s3,s4", "s1,s2,s4", "s1,s2,s3,s4"])
async def test_the_provenance_check_admits_the_documented_values(db_session, value):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="1", title="t", provenance=value))
    await db_session.flush()


@pytest.mark.parametrize("value", ["s5", "s4", "s2", "s2,s4", "orcid", "s1;s3", ""])
async def test_the_provenance_check_refuses_anything_else(db_session, value):
    pi = await factories.make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Publication(user_id=pi.id, pmid="1", title="t", provenance=value))
            await db_session.flush()


async def test_the_status_check_names_its_three_values_in_the_catalog(db_session):
    defn = (await db_session.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_publication_candidates_status'"
    ))).scalar_one()
    assert all(f"'{s}'" in defn for s in CANDIDATE_STATUSES)


async def test_one_candidate_per_pmid_and_known_statuses_only(db_session):
    pi = await factories.make_user(db_session)
    await _candidate(db_session, pi.id)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _candidate(db_session, pi.id)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _candidate(db_session, pi.id, pmid="2", status="maybe")


async def test_downgrade_drops_everything_and_upgrade_restores_it(db_session):
    await _run(db_session, "downgrade")
    assert {"provenance", "excluded_at", "excluded_by_user_id", "doi_verified"}.isdisjoint(
        await _columns(db_session, "publications"))
    assert await _columns(db_session, "publication_candidates") == set()
    assert {"human_edited_at", "evidence_flagged_count"}.isdisjoint(
        await _columns(db_session, "researcher_profiles"))
    assert "persona_export_failed_at" not in await _columns(db_session, "agents")
    await _run(db_session, "upgrade")
    assert "doi_verified" in await _columns(db_session, "publications")
    assert {"stages", "decided_by_user_id"} <= await _columns(db_session, "publication_candidates")
    assert "human_edited_at" in await _columns(db_session, "researcher_profiles")
    assert "persona_export_failed_at" in await _columns(db_session, "agents")
