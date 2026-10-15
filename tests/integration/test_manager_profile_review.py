"""The manager PI page's review cards and their routes (spec 2026-10-05 §6.3 Review UIs)."""
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_MANAGER,
    Job,
    ProfileRevision,
    Publication,
    PublicationCandidate,
)
from src.models.job import INTERACTIVE_PRIORITY
from src.routers.workspace import pi_profile as manager_routes
from src.services import profile_export, profile_review
from src.services.jhu_rules import set_tenure_start
from src.services.profile_drafts import build_draft_payload
from src.services.profile_jobs import REGENERATION_LIMIT_REASON
from src.services.profile_limits import SUMMARY_MAX_CHARS
from src.services.profile_review import GENERATION_NOTE_STAFF_TEXT, GenerationNote
from tests import factories
from tests.flash_support import session_flashes
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
    assert job.status == "pending" and job.priority == INTERACTIVE_PRIORITY
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
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="31", title="Withheld one",
                                        reason="orcid_listed_author_mismatch", status="pending"))
    db_session.add(Job(type="generate_profile", status="completed", user_id=pi.id, payload={
        "progress": [{"step": "corpus_rechecked", "detail": "Re-checked."}]}))
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await db_session.commit()
    staff = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))
    assert "Shown candidate" in staff.text and "Draft summary." in staff.text
    seen = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(rev.id))
    assert seen.status_code == 200 and "Shown candidate" not in seen.text
    assert "Withheld papers" in staff.text and "Latest generation notes" in staff.text
    assert "Withheld papers" not in seen.text and "Latest generation notes" not in seen.text


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


def _exclude_path(pi, row):
    return f"/pis/{pi.id}/publications/{row.id}/exclude"


async def test_the_publication_list_offers_exclude_on_every_row_for_staff(
    client, db_session, public,
):
    pi, _p, _a, mgr = await _setup(db_session)
    rows = [
        Publication(user_id=pi.id, pmid="41", title="Anchored paper", year=2021, provenance="s1"),
        Publication(user_id=pi.id, doi="10.1/oa", title="OpenAlex work", year=2022,
                    provenance="openalex"),
        Publication(user_id=pi.id, pmid="43", title="Kept paper", year=2023, provenance="manual"),
        Publication(user_id=pi.id, pmid="44", title="Unanchored paper", year=2024,
                    provenance="unanchored"),
    ]
    db_session.add_all(rows)
    await db_session.commit()
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    for row in rows:
        assert _exclude_path(pi, row) in page


async def test_a_reviewer_sees_no_exclude_on_the_publication_list(client, db_session, public):
    from src.models import USER_ROLE_REVIEWER

    pi, _p, _a, _mgr = await _setup(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="45", title="Shown", year=2021,
                               provenance="s1"))
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await db_session.commit()
    r = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(rev.id))
    assert r.status_code == 200 and "/exclude" not in r.text


async def test_excluding_an_openalex_row_from_the_list(client, db_session, public):
    pi, _p, agent, mgr = await _setup(db_session)
    row = Publication(user_id=pi.id, doi="10.1/oa", title="OpenAlex work", year=2022,
                      provenance="openalex")
    db_session.add(row)
    await db_session.commit()
    await _post(client, _exclude_path(pi, row).replace("/pis/", "/workspace/pis/"), mgr)
    await db_session.refresh(row)
    assert row.excluded_reason == "staff"
    summaries = (await db_session.execute(select(ProfileRevision.change_summary).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert "Paper excluded: DOI 10.1/oa" in summaries


async def test_restoring_a_pmid_less_row_names_its_doi(client, db_session, public):
    pi, _p, agent, mgr = await _setup(db_session)
    row = Publication(user_id=pi.id, doi="10.1/oa", title="OpenAlex work", year=2022,
                      provenance="openalex", excluded_at=datetime.now(UTC),
                      excluded_reason="openalex_affiliation")
    db_session.add(row)
    await db_session.commit()
    await _post(client, f"/workspace/pis/{pi.id}/publications/{row.id}/restore", mgr)
    await db_session.refresh(row)
    summaries = (await db_session.execute(select(ProfileRevision.change_summary).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert "Paper restored: DOI 10.1/oa" in summaries
    assert row.provenance == "manual"


async def test_excluded_papers_show_their_reason(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, pmid="51", title="Typed", year=2020, provenance="s1",
                    excluded_at=datetime.now(UTC), excluded_reason="publication_type"),
        Publication(user_id=pi.id, pmid="52", title="Legacy", year=2020, provenance="s1",
                    excluded_at=datetime.now(UTC)),
    ])
    await db_session.commit()
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "PubMed lists a publication type Blackbird leaves out" in page
    assert "excluded before reasons were recorded" in page


async def _accept_with_optional_twin(client, db_session, monkeypatch, *, with_twin):
    pi, _p, _a, mgr = await _setup(db_session)
    twin = Publication(user_id=pi.id, doi="10.1/w", year=2022, provenance="openalex",
                       title="Synthetic lethality screens in organoids")
    cand = PublicationCandidate(user_id=pi.id, pmid="555", title="Cand",
                                reason="no_orcid_anchor", status="pending")
    db_session.add_all([twin, cand] if with_twin else [cand])
    await db_session.commit()

    async def fetch(pmids, *, strict=False, permanently_dropped=None):
        return [{"pmid": "555", "title": "Synthetic lethality screens in organoids",
                 "abstract": "A.", "journal": "J", "year": 2022, "doi": "10.1/W",
                 "pmcid": None}]

    flashes = []
    monkeypatch.setattr(manager_routes, "fetch_pubmed_records", fetch)
    monkeypatch.setattr(manager_routes, "flash", lambda *args, **kw: flashes.append(args[1]))
    r = await _post(client, f"/workspace/pis/{pi.id}/candidates/{cand.id}/accept", mgr)
    assert r.status_code == 302
    row = (await db_session.execute(select(Publication).where(
        Publication.user_id == pi.id, Publication.pmid == "555"))).scalar_one()
    return row, twin, flashes


async def test_accepting_a_candidate_excludes_its_pmid_less_twin(
    client, db_session, public, monkeypatch,
):
    row, twin, flashes = await _accept_with_optional_twin(
        client, db_session, monkeypatch, with_twin=True)
    await db_session.refresh(twin)
    assert row.provenance == "manual" and twin.excluded_reason == "pubmed_twin"
    assert flashes == [
        "Paper added to the corpus. Its copy stored without a PMID moved to Excluded papers."]


async def test_accepting_without_a_twin_excludes_nothing(
    client, db_session, public, monkeypatch,
):
    _row, _twin, flashes = await _accept_with_optional_twin(
        client, db_session, monkeypatch, with_twin=False)
    assert flashes == ["Paper added to the corpus."]


async def test_withheld_candidates_render_with_accept_and_reject(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    cand = PublicationCandidate(user_id=pi.id, pmid="70", title="Withheld paper",
                                reason="orcid_listed_author_mismatch", status="pending")
    db_session.add(cand)
    await db_session.commit()
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "Withheld papers (1)" in page and "Candidate papers (0)" in page
    assert f"/candidates/{cand.id}/accept" in page and f"/candidates/{cand.id}/reject" in page


async def test_withheld_summary_shows_the_latest_corpus_flagged_detail(
    client, db_session, public,
):
    pi, _p, _a, mgr = await _setup(db_session)
    db_session.add(Job(type="generate_profile", status="completed", user_id=pi.id, payload={
        "progress": [{"step": "corpus_flagged", "detail": (
            "3 records withheld as not confirmed to be the PI's (3 no individual author "
            "match): 1, 2, 3")}]}))
    await db_session.commit()
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "Latest generation: 3 records withheld as not confirmed" in page


async def test_generation_notes_show_labelled_notable_steps(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Job(type="generate_profile", status="completed", user_id=pi.id, payload={
        "progress": [
            {"step": "start", "detail": "Starting pipeline"},
            {"step": "step3", "detail": "Resolving publication corpus"},
            {"step": "corpus_rechecked", "detail": "Stored publications re-checked: …"},
            {"step": "unvalidated", "detail": "It was saved as a draft for you to edit."},
            {"step": "complete", "detail": "Profile generation complete."},
        ]}))
    await db_session.flush()
    notes = await profile_review.latest_generation_notes(db_session, pi.id)
    assert [(n.label, n.detail) for n in notes.notes] == [
        ("Stored papers re-checked", "Stored publications re-checked: …"),
        ("Saved although it failed the quality checks", GENERATION_NOTE_STAFF_TEXT["unvalidated"]),
    ]


async def test_generation_notes_use_the_newest_job(db_session):
    pi = await factories.make_user(db_session)
    now = datetime.now(UTC)
    db_session.add_all([
        Job(type="generate_profile", status="completed", user_id=pi.id,
            enqueued_at=now - timedelta(days=2),
            payload={"progress": [{"step": "corpus_truncated", "detail": "Old note"}]}),
        Job(type="generate_profile", status="completed", user_id=pi.id,
            enqueued_at=now - timedelta(days=1),
            payload={"progress": [{"step": "corpus_rechecked", "detail": "New note"}]}),
    ])
    await db_session.flush()
    notes = await profile_review.latest_generation_notes(db_session, pi.id)
    assert notes.notes == [GenerationNote("corpus_rechecked", "Stored papers re-checked",
                                          "New note")]


async def test_no_job_means_no_notes_section(client, db_session, public):
    pi, _p, _a, mgr = await _setup(db_session)
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "Latest generation notes" not in page


_OUTSIDE = "Outside the tenure window, used by the full-career profile"


async def _fallback_rows(db_session, mgr_id, *, full_career_fallback=True):
    pi, _p, _a, mgr = await _setup(db_session, full_career_fallback=full_career_fallback)
    await set_tenure_start(pi.id, 2020, "manual", db=db_session)
    a = Publication(user_id=pi.id, pmid="61", title="In window A", year=2021, provenance="s1")
    b = Publication(user_id=pi.id, pmid="62", title="Before window B", year=2015,
                    provenance="s1")
    c = Publication(user_id=pi.id, pmid="63", title="Undated C", year=None, provenance="s1")
    d = Publication(user_id=pi.id, pmid="64", title="Excluded D", year=2014, provenance="s1",
                    excluded_at=datetime.now(UTC), excluded_by_user_id=mgr.id,
                    excluded_reason="staff")
    db_session.add_all([a, b, c, d])
    await db_session.commit()
    return pi, mgr, a, b, c


async def test_a_fallback_profile_lists_pre_tenure_rows_with_exclude(client, db_session, public):
    pi, mgr, a, b, c = await _fallback_rows(db_session, None)
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    start = page.index(f"{_OUTSIDE} (2)")
    outside = page[start:page.index("</details>", start)]
    assert _exclude_path(pi, b) in outside and _exclude_path(pi, c) in outside
    assert "Excluded D" not in outside and "In window A" not in outside
    assert "In window A" in page[:start]


async def test_no_outside_list_without_the_fallback(client, db_session, public):
    pi, mgr, *_rows = await _fallback_rows(db_session, None, full_career_fallback=False)
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert _OUTSIDE not in page


async def test_a_reviewer_never_sees_the_outside_list(client, db_session, public):
    from src.models import USER_ROLE_REVIEWER

    pi, _mgr, *_rows = await _fallback_rows(db_session, None)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    await db_session.commit()
    r = await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(rev.id))
    assert r.status_code == 200 and _OUTSIDE not in r.text


async def test_excluding_a_pre_tenure_row(client, db_session, public):
    pi, mgr, _a, b, _c = await _fallback_rows(db_session, None)
    await _post(client, f"/workspace/pis/{pi.id}/publications/{b.id}/exclude", mgr)
    await db_session.refresh(b)
    assert b.excluded_reason == "staff"
    page = (await client.get(f"/workspace/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    start = page.index(f"{_OUTSIDE} (1)")
    assert "Before window B" in page[:start]
    assert "Before window B" not in page[start:page.index("</details>", start)]


async def test_accept_draft_caps_and_cleans_tag_lists(client, db_session, public):
    draft = build_draft_payload(
        fields={"research_summary": "Draft summary.",
                "techniques": ["## Deep\nlearning", 7, ""],
                "experimental_models": [], "disease_areas": [], "key_targets": [],
                "keywords": [f"k{i}" for i in range(40)]},
        synthesis_validated=True, evidence_pmid_count=5, evidence_pub_count=4,
        evidence_flagged_count=0, base_profile_version=3, job_id=None,
    )
    pi, profile, _a, mgr = await _setup(db_session, pending_profile=draft,
                                        pending_profile_created_at=datetime.now(UTC))
    r = await _post(client, f"/workspace/pis/{pi.id}/draft/accept", mgr)
    assert "error" not in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.keywords == [f"k{i}" for i in range(30)]
    assert profile.techniques == ["Deep learning"]


async def test_accept_draft_refuses_a_non_list_field(client, db_session, public):
    draft = build_draft_payload(
        fields={"research_summary": "Draft summary.", "techniques": [],
                "experimental_models": "x", "disease_areas": [], "key_targets": [],
                "keywords": []},
        synthesis_validated=True, evidence_pmid_count=5, evidence_pub_count=4,
        evidence_flagged_count=0, base_profile_version=3, job_id=None,
    )
    pi, profile, _a, mgr = await _setup(db_session, pending_profile=draft,
                                        pending_profile_created_at=datetime.now(UTC))
    r = await _post(client, f"/workspace/pis/{pi.id}/draft/accept", mgr)
    assert "error=no_draft" in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.profile_version == 3 and profile.pending_profile is not None


async def _generations(db, user_id, status, *hours_ago, newest_completed_minutes_ago=None):
    now = datetime.now(UTC)
    for hours in hours_ago:
        enqueued = now - timedelta(hours=hours)
        db.add(Job(type="generate_profile", user_id=user_id, status=status, payload={},
                   enqueued_at=enqueued,
                   completed_at=enqueued + timedelta(minutes=5) if status == "completed" else None))
    if newest_completed_minutes_ago is not None:
        done = now - timedelta(minutes=newest_completed_minutes_ago)
        db.add(Job(type="generate_profile", user_id=user_id, status="completed", payload={},
                   enqueued_at=done - timedelta(minutes=10), completed_at=done))
    await db.commit()


async def _generation_count(db, user_id):
    return await db.scalar(select(func.count()).select_from(Job).where(
        Job.user_id == user_id, Job.type == "generate_profile"))


def _flash_texts(response):
    return [f["text"] for f in session_flashes(response)]


async def test_staff_retry_limited(client, db_session, public):
    pi, _profile, _agent, mgr = await _setup(db_session)
    await _generations(db_session, pi.id, "dead", 1, 2, 3, 4, 5)
    r = await _post(client, f"/workspace/pis/{pi.id}/profile/retry", mgr)
    assert r.status_code == 302
    assert REGENERATION_LIMIT_REASON in _flash_texts(r)
    assert await _generation_count(db_session, pi.id) == 5


async def test_regenerate_cooldown_still_first(client, db_session, public):
    pi, _profile, _agent, mgr = await _setup(db_session)
    await _generations(db_session, pi.id, "completed", 2, 3, 4, 5,
                       newest_completed_minutes_ago=10)
    r = await _post(client, f"/workspace/pis/{pi.id}/regenerate", mgr)
    texts = _flash_texts(r)
    assert any("less than an hour ago" in t for t in texts)
    assert REGENERATION_LIMIT_REASON not in texts


async def test_regenerate_card_shows_cap_reason(db_session):
    pi, _profile, _agent, _mgr = await _setup(db_session)
    await _generations(db_session, pi.id, "completed", 2, 3, 4, 5, 6)
    cards = await profile_review.load_review_cards(db_session, pi.id)
    assert cards.regenerate_refusal == REGENERATION_LIMIT_REASON


async def test_legacy_manager_routes_are_capped(client, db_session, public):
    pi, _profile, _agent, mgr = await _setup(db_session)
    await _generations(db_session, pi.id, "completed", 2, 3, 4, 5, 6)
    r = await _post(client, f"/manager/pis/{pi.id}/regenerate", mgr)
    assert "error=regenerate_refused" in r.headers["location"]
    assert REGENERATION_LIMIT_REASON in _flash_texts(r)
    assert await _generation_count(db_session, pi.id) == 5
