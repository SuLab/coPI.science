"""Readers that must ignore the non-PubMed corpus (spec 2026-10-09 §5.5, §5.7)."""
import sys
from pathlib import Path

import pytest

from src.models import Publication
from tests import factories

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration


async def _seed(db_session):
    pi = await factories.make_user(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, pmid="1", title="PubMed paper", year=2020),
        Publication(user_id=pi.id, doi="10.1/x", title="Conference paper", year=2021,
                    provenance="openalex"),
        Publication(user_id=pi.id, doi="10.1/y", title="Legacy PMID-less row", year=2019),
    ])
    await db_session.flush()
    return pi


async def test_the_repair_script_does_not_load_non_pubmed_rows(db_session):
    from scripts.repair_pi_corpus import load_stored_publications

    pi = await _seed(db_session)
    loaded = await load_stored_publications(db_session, pi.id)
    assert sorted(p.title for p in loaded) == ["Legacy PMID-less row", "PubMed paper"]


async def test_company_discovery_reads_pmid_rows_only(db_session):
    from src.services.company_discovery import _publication_years

    pi = await _seed(db_session)
    assert await _publication_years(db_session, pi.id) == {"1": 2020}
