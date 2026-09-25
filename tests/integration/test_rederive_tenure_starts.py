"""scripts/rederive_tenure_starts.py (RCA D7): re-derive paper-sourced tenure
starts against the strict corpus, preview by default, back up before writing.

The ORCID fetch and ``resolve_corpus`` are faked on the loaded module; the
database is the suite's real, rolled-back ``db_session``.
"""

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.models import AppSetting, Job, User
from src.services.corpus import CorpusResult, CorpusStageError
from src.services.jhu_rules import LEGACY_TENURE_KEY, TENURE_KEY_PREFIX, set_tenure_start

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rederive_tenure_starts.py"


def _load():
    spec = importlib.util.spec_from_file_location("rederive_tenure_starts", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rt = _load()

_HOPKINS_EMPLOYMENT = {
    "organization": "Johns Hopkins University",
    "start_year": 2011,
    "current": True,
}


def _paper(year: int) -> dict:
    return {"year": year, "pi_affiliations": ["Johns Hopkins University School of Medicine"]}


def _orcid() -> str:
    digits = f"{uuid.uuid4().int % 10**16:016d}"
    return "-".join(digits[i : i + 4] for i in range(0, 16, 4))


class _Fakes:
    """Per-ORCID canned answers; a value that is an exception is raised."""

    def __init__(self, monkeypatch):
        self.profiles: dict[str, object] = {}
        self.corpora: dict[str, object] = {}
        self.orcid_calls: list[str] = []
        self.corpus_calls: list[tuple] = []

        async def fake_profile(orcid):
            self.orcid_calls.append(orcid)
            answer = self.profiles.get(orcid, {"orcid": orcid, "employments": []})
            if isinstance(answer, BaseException):
                raise answer
            return answer

        async def fake_corpus(orcid, name, institution, *, cap):
            self.corpus_calls.append((orcid, name, institution, cap))
            answer = self.corpora.get(orcid, CorpusResult(kept=[], flagged=[]))
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(rt, "fetch_orcid_profile", fake_profile)
        monkeypatch.setattr(rt, "resolve_corpus", fake_corpus)


@pytest.fixture
def fakes(monkeypatch):
    return _Fakes(monkeypatch)


async def _pi(db, year: int, source: str = "earliest_hopkins_paper") -> User:
    user = User(name=f"PI {uuid.uuid4().hex[:6]}", orcid=_orcid(), institution="Johns Hopkins")
    db.add(user)
    await db.flush()
    await set_tenure_start(user.id, year, source, db=db)
    await db.flush()
    return user


async def _raw(db, user: User) -> str | None:
    return (
        await db.execute(
            select(AppSetting.value).where(AppSetting.key == f"{TENURE_KEY_PREFIX}{user.id}")
        )
    ).scalar_one_or_none()


async def _stored(db, user: User) -> dict:
    return json.loads(await _raw(db, user))


async def _profile_jobs(db, user: User) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.user_id == user.id, Job.type == "generate_profile")
        )
    ).scalar_one()


async def test_preview_writes_nothing_and_queues_nothing(db_session, fakes, tmp_path, capsys):
    a = await _pi(db_session, 2018)
    b = await _pi(db_session, 2016)
    fakes.corpora[a.orcid] = CorpusResult(kept=[_paper(2009)], flagged=[])
    before_a, before_b = await _raw(db_session, a), await _raw(db_session, b)

    code = await rt.run(
        db_session, orcids=[a.orcid, b.orcid], apply=False, backup_dir=tmp_path
    )

    assert code == 0
    assert await _raw(db_session, a) == before_a
    assert await _raw(db_session, b) == before_b
    assert await _profile_jobs(db_session, a) == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert f"{a.orcid}  2018 -> 2009 (earliest_hopkins_paper) CHANGE" in out
    assert b.orcid in out  # every paper-sourced row is listed


async def test_a_disagreeing_paper_row_is_overwritten_and_its_profile_queued(
    db_session, fakes, tmp_path
):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = CorpusResult(kept=[_paper(2020), _paper(2009)], flagged=[])

    code = await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    assert code == 0
    stored = await _stored(db_session, user)
    assert (stored["year"], stored["source"]) == (2009, "earliest_hopkins_paper")
    job = (
        await db_session.execute(select(Job).where(Job.user_id == user.id))
    ).scalar_one()
    assert job.type == "generate_profile"
    assert job.payload == {"user_id": str(user.id), "orcid": user.orcid}
    # The pipeline queues the grants/industry refresh itself.
    others = (
        await db_session.execute(
            select(func.count()).select_from(Job).where(
                Job.user_id == user.id, Job.type != "generate_profile"
            )
        )
    ).scalar_one()
    assert others == 0
    # Called the way the pipeline calls it.
    assert fakes.corpus_calls == [
        (user.orcid, user.name, user.institution, rt.CORPUS_CAP)
    ]


async def test_orcid_employment_wins_when_it_is_available(db_session, fakes, tmp_path):
    user = await _pi(db_session, 2018)
    fakes.profiles[user.orcid] = {"orcid": user.orcid, "employments": [_HOPKINS_EMPLOYMENT]}
    fakes.corpora[user.orcid] = CorpusResult(kept=[_paper(2005)], flagged=[])

    await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    stored = await _stored(db_session, user)
    assert (stored["year"], stored["source"]) == (2011, "orcid_employment")
    assert fakes.corpus_calls == []
    assert await _profile_jobs(db_session, user) == 1


async def test_manual_curated_and_employment_rows_are_never_touched(
    db_session, fakes, tmp_path
):
    users = [
        await _pi(db_session, 2018, "manual"),
        await _pi(db_session, 2017, "curated-2026-08-13"),
        await _pi(db_session, 2016, "orcid_employment"),
    ]
    legacy = json.dumps({"someagent": 2015})
    db_session.add(AppSetting(key=LEGACY_TENURE_KEY, value=legacy))
    await db_session.flush()
    for u in users:
        fakes.profiles[u.orcid] = {"orcid": u.orcid, "employments": [_HOPKINS_EMPLOYMENT]}
    before = [await _raw(db_session, u) for u in users]

    await rt.run(
        db_session, orcids=[u.orcid for u in users], apply=True, backup_dir=tmp_path
    )

    assert [await _raw(db_session, u) for u in users] == before
    assert (
        await db_session.execute(
            select(AppSetting.value).where(AppSetting.key == LEGACY_TENURE_KEY)
        )
    ).scalar_one() == legacy
    assert fakes.orcid_calls == []
    for u in users:
        assert await _profile_jobs(db_session, u) == 0


async def test_a_corpus_failure_skips_the_pi_with_no_write(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = CorpusStageError("PubMed efetch batch failed")
    before = await _raw(db_session, user)

    code = await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    assert code == 0
    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert "SKIP corpus_stage_failed" in capsys.readouterr().out


async def test_an_orcid_failure_skips_the_pi_with_no_write(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.profiles[user.orcid] = RuntimeError("ORCID 503")
    fakes.corpora[user.orcid] = CorpusResult(kept=[_paper(2009)], flagged=[])
    before = await _raw(db_session, user)

    await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    # A paper year is never derived without employments to consult (D8).
    assert fakes.corpus_calls == []
    assert "SKIP orcid_unavailable" in capsys.readouterr().out


async def test_a_pending_profile_job_is_not_duplicated(db_session, fakes, tmp_path):
    user = await _pi(db_session, 2018)
    db_session.add(
        Job(
            type="generate_profile",
            user_id=user.id,
            status="pending",
            payload={"user_id": str(user.id), "orcid": user.orcid},
        )
    )
    await db_session.flush()
    fakes.corpora[user.orcid] = CorpusResult(kept=[_paper(2009)], flagged=[])

    await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    assert (await _stored(db_session, user))["year"] == 2009
    assert await _profile_jobs(db_session, user) == 1


async def test_max_changes_aborts_before_any_write(db_session, fakes, tmp_path, capsys):
    a = await _pi(db_session, 2018)
    b = await _pi(db_session, 2017)
    for u in (a, b):
        fakes.corpora[u.orcid] = CorpusResult(kept=[_paper(2009)], flagged=[])
    before = [await _raw(db_session, a), await _raw(db_session, b)]

    code = await rt.run(
        db_session, orcids=[a.orcid, b.orcid], apply=True, max_changes=1, backup_dir=tmp_path
    )

    assert code != 0
    assert [await _raw(db_session, a), await _raw(db_session, b)] == before
    assert await _profile_jobs(db_session, a) == 0
    assert await _profile_jobs(db_session, b) == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert "ABORT" in out
    assert a.orcid in out and b.orcid in out  # the table is still printed


async def test_the_backup_is_written_before_the_first_write_and_restores_it(
    db_session, fakes, tmp_path, monkeypatch
):
    changed = await _pi(db_session, 2018)
    kept = await _pi(db_session, 2012)
    fakes.corpora[changed.orcid] = CorpusResult(kept=[_paper(2009)], flagged=[])
    fakes.corpora[kept.orcid] = CorpusResult(kept=[_paper(2012)], flagged=[])
    originals = {changed.id: await _raw(db_session, changed), kept.id: await _raw(db_session, kept)}

    real_set = rt.set_tenure_start
    seen_backup: list[dict] = []

    async def checked_set(user_id, year, source, *, db):
        files = list(tmp_path.glob("tenure_starts_*.json"))
        assert len(files) == 1, "backup must exist before the first write"
        seen_backup.append(json.loads(files[0].read_text()))
        await real_set(user_id, year, source, db=db)

    monkeypatch.setattr(rt, "set_tenure_start", checked_set)

    await rt.run(
        db_session, orcids=[changed.orcid, kept.orcid], apply=True, backup_dir=tmp_path
    )

    assert len(seen_backup) == 1
    saved = {row["key"]: row["value"] for row in seen_backup[0]["rows"]}
    # Every candidate row is saved, not only the changed one.
    assert saved == {
        f"{TENURE_KEY_PREFIX}{changed.id}": originals[changed.id],
        f"{TENURE_KEY_PREFIX}{kept.id}": originals[kept.id],
    }
    assert (await _stored(db_session, changed))["year"] == 2009

    (backup,) = tmp_path.glob("tenure_starts_*.json")
    assert await rt.restore(db_session, backup) == 0
    assert await _raw(db_session, changed) == originals[changed.id]
    assert await _raw(db_session, kept) == originals[kept.id]


async def test_no_rederived_year_is_reported_not_deleted(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = CorpusResult(kept=[{"year": 2004, "pi_affiliations": ["MIT"]}], flagged=[])
    before = await _raw(db_session, user)

    await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert "SKIP no_rederived_year" in capsys.readouterr().out
