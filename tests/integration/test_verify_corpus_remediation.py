"""scripts/verify_corpus_remediation.py: PASS/FAIL per check, read-only (D68).

The suite's database is shared, so assertions test membership, not totals."""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models import AppSetting, Job
from src.services import profile_export
from src.services.openalex_budget import Meter
from tests import factories

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration
_SINCE = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def healthy_meter(monkeypatch):
    import scripts.verify_corpus_remediation as v

    async def read_meter():
        return Meter(1000, 1000, 600)

    monkeypatch.setattr(v, "read_meter", read_meter)


@pytest.mark.parametrize("meter", [None, Meter(1000, 4, 600)])
async def test_an_unverified_free_budget_prevents_a_corpus_verification_request(
    db_session, monkeypatch, meter,
):
    import scripts.verify_corpus_remediation as v

    pi = await factories.make_user(db_session)

    async def read_meter():
        return meter

    async def forbidden(*args, **kwargs):
        raise AssertionError("No corpus network request may spend unconfirmed free credits")

    monkeypatch.setattr(v, "read_meter", read_meter)
    monkeypatch.setattr(v, "resolve_corpus", forbidden)
    result = await v.check_unstored_anchored(db_session, orcids=[pi.orcid])
    assert not result.passed and "Unverified" in "\n".join(result.details)


async def test_exhaustion_during_resolve_fails_instead_of_reporting_verified_coverage(
    db_session, monkeypatch,
):
    import scripts.verify_corpus_remediation as v
    from src.services import openalex_budget as ob

    pi = await factories.make_user(db_session)

    async def exhausted():
        return Meter(1000, 0, 600)

    async def resolve(*args, **kwargs):
        await ob.check_request_budget()
        raise AssertionError("A corpus request must not run after the free budget is exhausted")

    monkeypatch.setattr(ob, "read_meter", exhausted)
    monkeypatch.setattr(v, "resolve_corpus", resolve)
    result = await v.check_unstored_anchored(db_session, orcids=[pi.orcid])
    assert not result.passed and "Unverified" in "\n".join(result.details)
    await ob.check_request_budget()  # The script's task-local guard was restored.


async def test_a_corpus_stage_failure_cannot_pass_live_coverage(db_session, monkeypatch):
    import scripts.verify_corpus_remediation as v
    from src.services.corpus import CorpusStageError

    pi = await factories.make_user(db_session)

    async def failed(*args, **kwargs):
        raise CorpusStageError("OpenAlex transport failed")

    monkeypatch.setattr(v, "resolve_corpus", failed)
    result = await v.check_unstored_anchored(db_session, orcids=[pi.orcid])
    assert not result.passed and any("Unverified" in d and pi.orcid in d for d in result.details)


async def test_completeness_passes_on_a_completed_job_and_fails_on_a_dead_one(db_session):
    import scripts.verify_corpus_remediation as v

    done = await factories.make_user(db_session)
    dead = await factories.make_user(db_session, name="Dead Pi")
    db_session.add_all([
        Job(type="generate_profile", user_id=done.id, payload={}, status="completed",
            completed_at=_SINCE + timedelta(hours=1)),
        Job(type="generate_profile", user_id=dead.id, payload={}, status="dead",
            completed_at=_SINCE + timedelta(hours=1), last_error="boom"),
    ])
    await db_session.flush()
    result = await v.check_completeness(db_session, _SINCE)
    assert result.passed is False and any("Dead Pi" in d and "boom" in d for d in result.details)
    assert all(f"({done.orcid})" not in d for d in result.details)


async def test_a_staged_draft_counts_as_regenerated(db_session):
    import scripts.verify_corpus_remediation as v

    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, pending_profile={"fields": {}},
                                 pending_profile_created_at=_SINCE + timedelta(hours=2))
    others = await v.check_completeness(db_session, _SINCE)
    assert all(f"({pi.orcid})" not in d for d in others.details)


async def test_no_id_names_fails_for_a_pi_named_by_orcid_id(db_session):
    import scripts.verify_corpus_remediation as v

    await factories.make_user(db_session, name="0000-0002-1825-0097")
    assert (await v.check_no_id_names(db_session)).passed is False


async def test_render_diff_reports_a_stale_file(db_session, tmp_path, monkeypatch):
    import scripts.verify_corpus_remediation as v

    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    agent = await factories.make_agent(db_session, user=pi)
    (tmp_path / f"{agent.agent_id}.md").write_text("stale", encoding="utf-8")
    result = await v.check_render_diff(db_session)
    assert result.passed is False and agent.agent_id in "\n".join(result.details)


async def test_the_unanchored_marker_check(db_session):
    import scripts.verify_corpus_remediation as v

    existing = await db_session.get(AppSetting, v.APPLIED_KEY)
    if existing is None:
        assert (await v.check_unanchored_applied(db_session)).passed is False
        existing = AppSetting(key=v.APPLIED_KEY)
        db_session.add(existing)
    existing.value = '{"at": "2026-10-07T00:00:00Z", "pis": 1, "rows_marked": 0, "failed": []}'
    await db_session.flush()
    assert (await v.check_unanchored_applied(db_session)).passed is True
    existing.value = '{"at": "2026-10-07T00:00:00Z", "pis": 1, "rows_marked": 0, "failed": ["x"]}'
    await db_session.flush()
    assert (await v.check_unanchored_applied(db_session)).passed is False


async def test_unstored_anchored_is_report_only(db_session, monkeypatch):
    import scripts.verify_corpus_remediation as v
    from src.models import Publication
    from src.services.corpus import CorpusResult

    pi = await factories.make_user(db_session, name="Ada Lovelace")
    db_session.add(Publication(user_id=pi.id, pmid="500", title="Stored"))
    await db_session.flush()

    async def fake_resolve(orcid, name, institution, **kw):
        return CorpusResult(kept=[], flagged=[], ranked=[
            {"pmid": "400", "stages": ["s1"], "title": "Old", "year": 2001},
            {"pmid": "600", "stages": ["s3"], "title": "New", "year": 2026},
            {"pmid": "700", "stages": ["s4"], "title": "Unanchored", "year": 2026},
        ])

    monkeypatch.setattr(v, "resolve_corpus", fake_resolve)
    result = await v.check_unstored_anchored(db_session, orcids=[pi.orcid])
    assert result.passed is True
    joined = "\n".join(result.details)
    assert "older unstored anchored 400" in joined and "newer unstored anchored 600" in joined
    assert "700" not in joined and all(d.startswith("WARN") for d in result.details)
