"""The draft payload carries the thin-record flag (spec 2026-10-09 §4.3)."""
from src.models import ResearcherProfile
from src.services.profile_drafts import build_draft_payload, read_draft


def _payload(**kw):
    return build_draft_payload(
        fields={"research_summary": "S."}, synthesis_validated=True, evidence_pmid_count=1,
        evidence_pub_count=1, evidence_flagged_count=0, base_profile_version=2, job_id=None,
        **kw,
    )


def test_the_flag_round_trips():
    profile = ResearcherProfile(pending_profile=_payload(full_career_fallback=True))
    assert read_draft(profile).full_career_fallback is True


def test_the_flag_defaults_to_false():
    assert _payload()["full_career_fallback"] is False


def test_a_payload_written_before_0064_reads_false():
    payload = _payload()
    del payload["full_career_fallback"]
    assert read_draft(ResearcherProfile(pending_profile=payload)).full_career_fallback is False
