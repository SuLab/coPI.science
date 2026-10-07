"""The manager PI page's review cards and their routes (spec 2026-10-05 §6.3 Review UIs)."""
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_MANAGER,
    Job,
    ProfileRevision,
    Publication,
    PublicationCandidate,
)
from src.routers.workspace import pi_profile as manager_routes
from src.services import profile_export
from src.services.profile_drafts import build_draft_payload
from src.services.profile_limits import SUMMARY_MAX_CHARS
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def public(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    return tmp_path


async def _setup(db_session, **profile_kw):
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=3, **profile_kw)
    agent = await factories.make_agent(db_session, user=pi)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.commit()
    return pi, profile, agent, mgr


def _draft(base_version=3, summary="Draft summary."):
    return build_draft_payload(
        fields={"research_summary": summary, "techniques": ["x", "y", "z"],
                "experimental_models": [], "disease_areas": ["d"], "key_targets": [],
                "keywords": []},
        synthesis_validated=True, evidence_pmid_count=5, evidence_pub_count=4,
        evidence_flagged_count=0, base_profile_version=base_version, job_id=None,
    )


async def _post(client, path, mgr, data=None):
    return await client.post(path, data=data or {}, headers=auth_headers(mgr.id),
                             follow_redirects=False)


async def test_accept_writes_fields_bumps_version_and_stamps_generated_at(client, db_session, public):
    edited = datetime.now(UTC) - timedelta(days=1)
    pi, profile, agent, mgr = await _setup(db_session, pending_profile=_draft(),
                                           pending_profile_created_at=datetime.now(UTC),
                                           human_edited_at=edited)
    r = await _post(client, f"/workspace/pis/{pi.id}/draft/accept", mgr)
    assert r.status_code == 302 and "error" not in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.research_summary == "Draft summary." and profile.profile_version == 4
    assert profile.pending_profile is None and profile.profile_generated_at > edited
    assert profile.human_edited_at == edited and profile.evidence_pub_count == 4
    mechs = (await db_session.execute(select(ProfileRevision.mechanism).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert "draft_accept" in mechs and (public / f"{agent.agent_id}.md").exists()


async def test_accept_refuses_a_stale_draft(client, db_session, public):
    pi, profile, _agent, mgr = await _setup(db_session, pending_profile=_draft(base_version=2))
    r = await _post(client, f"/workspace/pis/{pi.id}/draft/accept", mgr)
    assert "error=draft_stale" in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.profile_version == 3 and profile.pending_profile is not None


async def test_accept_refuses_an_oversized_legacy_draft_without_exporting(
    client, db_session, public,
):
    pi, profile, agent, mgr = await _setup(
        db_session, research_summary="Keep this profile.",
        pending_profile=_draft(summary="x" * (SUMMARY_MAX_CHARS + 1)),
    )
    slug = agent.agent_id
    r = await _post(client, f"/workspace/pis/{pi.id}/draft/accept", mgr)
    assert "error=summary_too_long" in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.research_summary == "Keep this profile."
    assert profile.profile_version == 3 and profile.pending_profile is not None
    assert not (public / f"{slug}.md").exists()


async def test_accept_refuses_while_a_generation_is_in_flight(client, db_session, public):
    pi, profile, _agent, mgr = await _setup(db_session, pending_profile=_draft())
    db_session.add(Job(type="generate_profile", user_id=pi.id, payload={}, status="pending"))
    await db_session.commit()
    r = await _post(client, f"/workspace/pis/{pi.id}/draft/accept", mgr)
    assert "error=profile_generating" in r.headers["location"]


async def test_discard_clears_the_draft(client, db_session, public):
    pi, profile, _agent, mgr = await _setup(db_session, pending_profile=_draft(),
                                            pending_profile_created_at=datetime.now(UTC))
    await _post(client, f"/workspace/pis/{pi.id}/draft/discard", mgr)
    await db_session.refresh(profile)
    assert profile.pending_profile is None and profile.pending_profile_created_at is None


async def test_candidate_accept_stores_a_manual_row_from_pubmed(client, db_session, public, monkeypatch):
    pi, _profile, agent, mgr = await _setup(db_session)
    cand = PublicationCandidate(user_id=pi.id, pmid="321", title="Cand", reason="no_orcid_anchor",
                                status="pending")
    db_session.add(cand)
    await db_session.commit()

    async def fetch(pmids, *, strict=False, permanently_dropped=None):
        return [{"pmid": "321", "title": "From PubMed", "abstract": "A.", "journal": "J",
                 "year": 2022, "doi": "10.1/321", "pmcid": None}]

    monkeypatch.setattr(manager_routes, "fetch_pubmed_records", fetch)
    r = await _post(client, f"/workspace/pis/{pi.id}/candidates/{cand.id}/accept", mgr)
    assert r.status_code == 302
    row = (await db_session.execute(select(Publication).where(
        Publication.user_id == pi.id, Publication.pmid == "321"))).scalar_one()
    assert (row.title, row.provenance, row.abstract) == ("From PubMed", "manual", "A.")
    await db_session.refresh(cand)
    assert cand.status == "accepted" and cand.decided_by_user_id == mgr.id


async def test_candidate_accept_with_pubmed_down_writes_nothing(client, db_session, public, monkeypatch):
    pi, _profile, _agent, mgr = await _setup(db_session)
    cand = PublicationCandidate(user_id=pi.id, pmid="322", title="C", reason="no_orcid_anchor",
                                status="pending")
    db_session.add(cand)
    await db_session.commit()

    async def down(pmids, **kw):
        raise ConnectionError("down")

    monkeypatch.setattr(manager_routes, "fetch_pubmed_records", down)
    r = await _post(client, f"/workspace/pis/{pi.id}/candidates/{cand.id}/accept", mgr)
    assert "error=pubmed_unreachable" in r.headers["location"]
    await db_session.refresh(cand)
    assert cand.status == "pending"


async def test_another_pis_candidate_is_404(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    other = await factories.make_user(db_session)
    cand = PublicationCandidate(user_id=other.id, pmid="9", title="C", reason="no_orcid_anchor",
                                status="pending")
    db_session.add(cand)
    await db_session.commit()
    r = await _post(client, f"/workspace/pis/{pi.id}/candidates/{cand.id}/reject", mgr)
    assert r.status_code == 404


async def test_reject_keeps_the_row_rejected(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    cand = PublicationCandidate(user_id=pi.id, pmid="10", title="C", reason="no_orcid_anchor",
                                status="pending")
    db_session.add(cand)
    await db_session.commit()
    await _post(client, f"/workspace/pis/{pi.id}/candidates/{cand.id}/reject", mgr)
    await db_session.refresh(cand)
    assert cand.status == "rejected" and cand.decided_by_user_id == mgr.id


async def test_keep_and_exclude(client, db_session, public):
    pi, _p, agent, mgr = await _setup(db_session)
    keep = Publication(user_id=pi.id, pmid="20", title="Keep me", provenance="unanchored")
    drop = Publication(user_id=pi.id, pmid="21", title="Drop me", provenance="unanchored", year=2023)
    db_session.add_all([keep, drop])
    await db_session.commit()
    await _post(client, f"/workspace/pis/{pi.id}/publications/{keep.id}/keep", mgr)
    await _post(client, f"/workspace/pis/{pi.id}/publications/{drop.id}/exclude", mgr)
    await db_session.refresh(keep)
    await db_session.refresh(drop)
    assert keep.provenance == "manual"
    assert drop.excluded_at is not None and drop.excluded_by_user_id == mgr.id
    assert "Drop me" not in (public / f"{agent.agent_id}.md").read_text(encoding="utf-8")


async def test_restore_undoes_an_exclude_and_reexports(client, db_session, public):
    pi, _p, agent, mgr = await _setup(db_session)
    row = Publication(user_id=pi.id, pmid="22", title="Restore me", provenance="unanchored",
                      year=2023)
    db_session.add(row)
    await db_session.commit()
    await _post(client, f"/workspace/pis/{pi.id}/publications/{row.id}/exclude", mgr)
    page = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert "Excluded papers (1)" in page.text
    assert f"/publications/{row.id}/restore" in page.text
    r = await _post(client, f"/workspace/pis/{pi.id}/publications/{row.id}/restore", mgr)
    assert r.status_code == 302 and "error" not in r.headers["location"]
    await db_session.refresh(row)
    assert row.excluded_at is None and row.excluded_by_user_id is None
    assert "Restore me" in (public / f"{agent.agent_id}.md").read_text(encoding="utf-8")
    revs = (await db_session.execute(select(ProfileRevision).where(
        ProfileRevision.agent_registry_id == agent.id,
        ProfileRevision.mechanism == "paper_review"))).scalars().all()
    assert len(revs) == 2 and all(r.changed_by_user_id == mgr.id for r in revs)


async def test_restoring_a_row_that_is_not_excluded_is_404(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    row = Publication(user_id=pi.id, pmid="23", title="Never excluded")
    db_session.add(row)
    await db_session.commit()
    r = await _post(client, f"/workspace/pis/{pi.id}/publications/{row.id}/restore", mgr)
    assert r.status_code == 404


async def test_regenerate_queues_interactive_and_refuses_in_flight_or_recent(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    r = await _post(client, f"/workspace/pis/{pi.id}/regenerate", mgr)
    assert "error" not in r.headers["location"]
    [job] = (await db_session.execute(select(Job).where(
        Job.user_id == pi.id, Job.type == "generate_profile"))).scalars().all()
    assert job.status == "pending" and job.priority == manager_routes.INTERACTIVE_PRIORITY
    r = await _post(client, f"/workspace/pis/{pi.id}/regenerate", mgr)
    assert "error=regenerate_refused" in r.headers["location"]
    job.status, job.completed_at = "completed", datetime.now(UTC) - timedelta(minutes=10)
    await db_session.commit()
    r = await _post(client, f"/workspace/pis/{pi.id}/regenerate", mgr)
    assert "error=regenerate_refused" in r.headers["location"]
    job.completed_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.commit()
    r = await _post(client, f"/workspace/pis/{pi.id}/regenerate", mgr)
    assert "error" not in r.headers["location"]


async def test_reexport_rewrites_the_file_and_clears_the_card(client, db_session, public):
    pi, _p, agent, mgr = await _setup(db_session)
    agent.persona_export_failed_at = datetime.now(UTC)
    await db_session.commit()
    page = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert "Persona file out of date" in page.text
    await _post(client, f"/workspace/pis/{pi.id}/persona/reexport", mgr)
    await db_session.refresh(agent)
    assert agent.persona_export_failed_at is None
    page = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert "Persona file out of date" not in page.text


async def test_the_cards_render_for_staff_and_not_for_reviewers(client, db_session, public):
    from src.models import USER_ROLE_REVIEWER

    pi, _p, _a, mgr = await _setup(db_session, pending_profile=_draft())
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="30", title="Shown candidate",
                                        reason="no_orcid_anchor", status="pending"))
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await db_session.commit()
    staff = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert "Shown candidate" in staff.text and "Draft summary." in staff.text
    seen = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(rev.id))
    assert seen.status_code == 200 and "Shown candidate" not in seen.text


async def test_an_impersonated_action_logs_a_warning(client, db_session, public, caplog):
    from src.models import USER_ROLE_ADMIN

    pi, _p, _a, mgr = await _setup(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.commit()
    with caplog.at_level(logging.WARNING, logger="src.routers.workspace._pi_common"):
        await client.post(f"/workspace/pis/{pi.id}/regenerate",
                          headers=auth_headers(admin.id, impersonate=mgr.id),
                          follow_redirects=False)
    assert "impersonated" in caplog.text


async def test_the_directory_says_generating_only_for_a_generation(db_session):
    from src.services.directory import list_pi_directory

    pi = await factories.make_user(db_session, name="Zz Directory Pi")
    await factories.make_profile(db_session, user=pi, research_summary="")
    db_session.add(Job(type="industry_evidence", user_id=pi.id, payload={}, status="pending"))
    await db_session.flush()
    rows = await list_pi_directory(db_session, page=1, status_filter="generating",
                                   institution_filter=None, claimed_filter=None, roles=("pi",))
    assert all(row["user"].id != pi.id for row in rows)


@pytest.mark.parametrize("action", ["keep", "discard"])
async def test_keep_and_discard_log_the_impersonation(client, db_session, public, caplog, action):
    from src.models import USER_ROLE_ADMIN

    pi, _p, _a, mgr = await _setup(db_session, pending_profile=_draft(),
                                   pending_profile_created_at=datetime.now(UTC))
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    row = Publication(user_id=pi.id, pmid="40", title="Kept", provenance="unanchored")
    db_session.add(row)
    await db_session.commit()
    path = (f"/workspace/pis/{pi.id}/publications/{row.id}/keep" if action == "keep"
            else f"/workspace/pis/{pi.id}/draft/discard")
    with caplog.at_level(logging.INFO, logger="src.routers.workspace._pi_common"):
        await client.post(path, headers=auth_headers(admin.id, impersonate=mgr.id),
                          follow_redirects=False)
    assert "impersonated" in caplog.text and str(mgr.id) in caplog.text
