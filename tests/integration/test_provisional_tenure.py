"""A year the pipeline uses "for this run only" is kept provisionally, so a later
edit export scopes `## Recent Publications` the same way the pipeline's own
export did. A healthy run never leaves one behind."""
import pytest
from sqlalchemy import select

from src.models import AppSetting, Publication, ResearcherProfile
from src.services import corpus as corpus_module
from src.services import profile_export, profile_pipeline
from src.services.jhu_rules import (
    PROVISIONAL_KEY_PREFIX,
    clear_provisional_tenure_start,
    export_tenure_start,
    get_tenure_start,
    set_provisional_tenure_start,
    set_tenure_start,
)
from src.services.user_deletion import delete_user_account
from tests import factories
from tests.characterization.test_profile_pipeline_gm import _install_fakes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_HOPKINS_ADA = [{
    "last": "Lovelace", "fore": "Ada", "initials": "A", "collective": None,
    "affiliations": ["Johns Hopkins University School of Medicine"],
}]
_PAPERS = (("2001", "10.1000/h1", "Hopkins paper one", 2015),
           ("2002", "10.1000/h2", "Hopkins paper two", 2019))


def _hopkins_corpus(monkeypatch):
    async def works(orcid_id, *, strict=False):
        return [{"pmid": p, "doi": d, "title": t, "year": y} for p, d, t, y in _PAPERS]

    async def records(pmids, *, strict=False, permanently_dropped=None):
        return [
            {"pmid": p, "doi": d, "title": t, "abstract": "An abstract.", "journal": "J",
             "year": y, "pub_types": ["Journal Article"], "pmcid": None,
             "authors": list(_HOPKINS_ADA)}
            for p, d, t, y in _PAPERS if p in pmids
        ]

    monkeypatch.setattr(corpus_module, "fetch_orcid_works", works)
    monkeypatch.setattr(corpus_module, "fetch_pubmed_records", records)


def _orcid_down(monkeypatch):
    async def down(orcid_id):
        raise RuntimeError("ORCID 503")

    monkeypatch.setattr(profile_pipeline, "fetch_orcid_profile", down)


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    out = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", out)
    return out


async def _ada(db_session, agent_id):
    user = await factories.make_user(
        db_session, name="Ada Lovelace", orcid="0000-0002-1825-0097",
        email=f"{agent_id}@example.org", institution=None, department=None,
    )
    await factories.make_agent(db_session, user=user, agent_id=agent_id, pi_name="Ada Lovelace")
    db_session.add(Publication(user_id=user.id, title="Pre-Hopkins paper", year=2010, pmid="1999"))
    await db_session.flush()
    return user


def _pubs(markdown):
    return markdown.split("## Recent Publications\n", 1)[1].split("\n## ", 1)[0]


async def _provisional(db_session, user_id):
    return await db_session.scalar(
        select(AppSetting.value).where(AppSetting.key == f"{PROVISIONAL_KEY_PREFIX}{user_id}")
    )


async def test_an_orcid_failure_scopes_later_edit_exports_like_the_pipeline_export(
    client, db_session, monkeypatch, export_dir,
):
    _install_fakes(monkeypatch)
    _hopkins_corpus(monkeypatch)
    _orcid_down(monkeypatch)
    user = await _ada(db_session, "p014ada")

    await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert await get_tenure_start(db_session, user.id) is None, "not recorded as authoritative"
    assert await export_tenure_start(db_session, user.id) == 2015
    pipeline_export = (export_dir / "p014ada.md").read_text(encoding="utf-8")
    assert "Pre-Hopkins paper" not in pipeline_export

    # R1-a: the pipeline created the profile; the save carries its current version.
    version = (await db_session.execute(
        select(ResearcherProfile.profile_version).where(ResearcherProfile.user_id == user.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    resp = await client.post("/profile/save", data={
        "profile_version": str(version),
        "name": user.name, "email": user.email, "institution": "", "department": "",
        "research_summary": "Edited.", "techniques": "", "experimental_models": "",
        "disease_areas": "", "key_targets": "", "keywords": "",
    }, headers=auth_headers(user.id))
    assert resp.status_code == 302
    edit_export = (export_dir / "p014ada.md").read_text(encoding="utf-8")
    assert _pubs(edit_export) == _pubs(pipeline_export)


async def test_a_healthy_run_leaves_no_provisional_key(db_session, monkeypatch, export_dir):
    _install_fakes(monkeypatch)
    _hopkins_corpus(monkeypatch)
    user = await _ada(db_session, "p014ok")
    await set_provisional_tenure_start(db_session, user.id, 2011)  # left by an earlier failure

    await profile_pipeline.run_profile_pipeline(user.id, db_session)

    assert await _provisional(db_session, user.id) is None
    assert await get_tenure_start(db_session, user.id) == 2015


async def test_recording_an_authoritative_year_or_deleting_the_user_clears_it(db_session):
    user = await factories.make_user(db_session)
    await set_provisional_tenure_start(db_session, user.id, 2012)
    assert await export_tenure_start(db_session, user.id) == 2012
    await set_tenure_start(user.id, 2014, "manual", db=db_session)
    assert await _provisional(db_session, user.id) is None
    assert await export_tenure_start(db_session, user.id) == 2014

    await set_provisional_tenure_start(db_session, user.id, 2012)
    await clear_provisional_tenure_start(db_session, user.id)
    assert await _provisional(db_session, user.id) is None

    await set_provisional_tenure_start(db_session, user.id, 2012)
    await delete_user_account(db_session, user)
    assert await _provisional(db_session, user.id) is None
