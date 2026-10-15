"""0068: publications.excluded_reason and its CHECK (contract C3). The database is at head."""
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from src.models import Publication
from src.models.publication import (
    CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH,
    EXCLUDED_REASON_CHECK_SQL,
    EXCLUDED_REASONS,
    SYSTEM_EXCLUDED_REASONS,
)
from tests import factories

pytestmark = pytest.mark.integration
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_CHECK = "ck_publications_excluded_reason"


def _m0068():
    spec = importlib.util.spec_from_file_location(
        "_m0068", _VERSIONS / "0068_publication_excluded_reason.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def _flush(db_session, **kw):
    user = await factories.make_user(db_session)
    db_session.add(Publication(user_id=user.id, title="T", **kw))
    await db_session.flush()


def test_the_check_constant_equals_the_model_value():
    assert _m0068().EXCLUDED_REASON_CHECK_SQL == EXCLUDED_REASON_CHECK_SQL


def test_the_reasons_are_contract_c3():
    assert EXCLUDED_REASONS == (
        "staff", "superseded", "pubmed_twin", "duplicate_title", "publication_type",
        "openalex_affiliation", "repair_script",
    )
    assert all(len(reason) <= 32 for reason in EXCLUDED_REASONS)
    assert SYSTEM_EXCLUDED_REASONS == frozenset(EXCLUDED_REASONS) - {"staff"}


@pytest.mark.parametrize("reason", EXCLUDED_REASONS)
async def test_every_reason_is_admitted(db_session, reason):
    await _flush(db_session, pmid="1", excluded_at=datetime.now(UTC), excluded_reason=reason)


@pytest.mark.parametrize("reason", ["orcid", "Staff", ""])
async def test_an_unknown_reason_is_refused(db_session, reason):
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _flush(db_session, pmid="1", excluded_at=datetime.now(UTC),
                         excluded_reason=reason)
    assert _CHECK in str(excinfo.value)


async def test_null_reason_is_admitted_with_or_without_excluded_at(db_session):
    await _flush(db_session, pmid="1", excluded_reason=None)
    await _flush(db_session, pmid="2", excluded_at=datetime.now(UTC), excluded_reason=None)


def test_candidate_reason_constant():
    assert CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH == "orcid_listed_author_mismatch"
    assert len(CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH) <= 40


def test_the_not_theirs_reasons_exist():
    # The set Phase 5's C12 NOT_THEIRS_REASONS reads as "not the PI's" (F9).
    assert {"staff", "openalex_affiliation", "repair_script"} <= set(EXCLUDED_REASONS)
