"""Migration 0065 (spec 2026-10-09 §5.4): 'openalex' provenance and one PMID-less row per PI
per DOI."""
import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from src.models import Publication
from src.models.publication import PROVENANCE_CHECK_SQL
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _m0065():
    spec = importlib.util.spec_from_file_location(
        "_m0065", _VERSIONS / "0065_non_pubmed_publications.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_check_constant_equals_the_model_value():
    assert _m0065().PROVENANCE_CHECK_SQL == PROVENANCE_CHECK_SQL


@pytest.mark.parametrize("value", ["openalex", "s1", "s1,s2"])
async def test_the_widened_check_admits_the_non_pubmed_values(db_session, value):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, doi="10.1/ok", title="t", provenance=value))
    await db_session.flush()


@pytest.mark.parametrize("value", ["openalex2", "s2", "orcid"])
async def test_the_check_still_refuses_anything_else(db_session, value):
    pi = await factories.make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Publication(user_id=pi.id, doi="10.1/x", title="t", provenance=value))
            await db_session.flush()


async def test_one_pmid_less_row_per_pi_per_doi_whatever_its_case(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, doi="10.1/A", title="t", provenance="openalex"))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Publication(user_id=pi.id, doi="10.1/a", title="t2",
                                       provenance="openalex"))
            await db_session.flush()


async def test_a_pubmed_row_and_another_pi_may_share_the_doi(db_session):
    pi, other = await factories.make_user(db_session), await factories.make_user(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, pmid="1", doi="10.1/s", title="PubMed"),
        Publication(user_id=pi.id, doi="10.1/s", title="PMID-less", provenance="openalex"),
        Publication(user_id=other.id, doi="10.1/s", title="Other PI", provenance="openalex"),
    ])
    await db_session.flush()
