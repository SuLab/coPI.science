"""PIPE-15 (R4.13): a NULL stored evidence count is protected."""
import uuid
from types import SimpleNamespace

from src.models import ResearcherProfile
from src.services import profile_pipeline
from src.services.profile_pipeline import PipelineRun, _keep_stored_reason


class _Db:
    async def flush(self):
        return None


def _run(evidence_pub_count):
    profile = ResearcherProfile(profile_version=3, research_summary="Stored summary.",
                                synthesis_validated=None, evidence_pub_count=evidence_pub_count,
                                human_edited_at=None)
    return PipelineRun(user_id=uuid.uuid4(), db=_Db(), job_id=uuid.uuid4(),
                       user=SimpleNamespace(name="Jane Wang"), orcid_id="x", profile=profile,
                       validated=True)


async def test_a_null_stored_count_is_protected(monkeypatch):
    recorded = []

    async def record(job_id, step, detail=""):
        recorded.append((step, detail))

    monkeypatch.setattr(profile_pipeline.job_progress, "record", record)
    await profile_pipeline._step9_store(_run(None))
    assert recorded == [
        ("step9", "Saving profile to database..."),
        ("validation_rejected",
         "Kept the existing profile (version 3): the new synthesis grounded in 0 publications, "
         "down from an unknown number (the stored profile predates evidence tracking)."),
    ]


def test_a_zero_stored_count_is_not_protected():
    assert _keep_stored_reason(_run(0), 0) is None


def test_a_positive_stored_count_keeps_todays_text():
    assert _keep_stored_reason(_run(4), 0) == "grounded in 0 publications, down from 4"
