import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration


async def test_swaps_commit_before_jobs_are_queued(db_session, monkeypatch, tmp_path):
    import scripts.rederive_tenure_starts as r
    from tests import factories

    user = await factories.make_user(db_session)
    cand = r.Candidate(key=f"{r.TENURE_KEY_PREFIX}{user.id}", raw_value='{"year": 2010}', user_id=user.id,
                       name=user.name, orcid=user.orcid, institution=None, stored_year=2010)
    events = []
    orig_commit = db_session.commit

    async def spy_commit():
        events.append("commit")
        await orig_commit()

    async def fake_load(db, orcids):
        return [cand], [], []

    async def fake_rederive(c):
        return r.Outcome(candidate=c, new_year=2008, new_source="orcid_employment")

    async def fake_swap(db, key, expected, new):
        events.append("swap")
        return True

    async def fake_enqueue(db, u, **kw):
        events.append("enqueue")
        return None

    monkeypatch.setattr(db_session, "commit", spy_commit)
    monkeypatch.setattr(r, "load_candidates", fake_load)
    monkeypatch.setattr(r, "rederive", fake_rederive)
    monkeypatch.setattr(r, "_swap_value", fake_swap)
    monkeypatch.setattr(r, "enqueue_profile_job_if_absent", fake_enqueue)
    code = await r.run(db_session, orcids=[], apply=True, backup_dir=tmp_path, allow_unmounted_backup_dir=True)
    assert code == 0
    assert events.index("swap") < events.index("commit") < events.index("enqueue")
