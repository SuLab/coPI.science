"""scripts/rederive_tenure_starts.py (RCA D7): re-derive paper-sourced tenure
starts against the strict corpus, preview by default, back up before writing.

The ORCID fetch and ``resolve_corpus`` are faked on the loaded module; the
database is the suite's real, rolled-back ``db_session``.
"""

import importlib.util
import json
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update

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


def _paper(year: int, affiliation: str = "Johns Hopkins University School of Medicine") -> dict:
    return {"year": year, "pi_affiliations": [affiliation]}


def _corpus(*papers: dict, cap: int | None = None) -> CorpusResult:
    """A CorpusResult shaped like resolve_corpus's: ``kept == ranked[:cap]``."""
    ranked = list(papers)
    return CorpusResult(kept=ranked[: cap or len(ranked)], flagged=[], ranked=ranked)


def _orcid() -> str:
    digits = f"{uuid.uuid4().int % 10**16:016d}"
    return "-".join(digits[i : i + 4] for i in range(0, 16, 4))


class _Fakes:
    """Per-ORCID canned answers; a value that is an exception is raised.

    ``during_corpus[orcid]`` is an async hook run inside the corpus fetch,
    i.e. between the candidate read and the write.
    """

    def __init__(self, monkeypatch):
        self.profiles: dict[str, object] = {}
        self.corpora: dict[str, object] = {}
        self.during_corpus: dict[str, object] = {}
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
            hook = self.during_corpus.get(orcid)
            if hook is not None:
                await hook()
            answer = self.corpora.get(orcid, CorpusResult(kept=[], flagged=[]))
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(rt, "fetch_orcid_profile", fake_profile)
        monkeypatch.setattr(rt, "resolve_corpus", fake_corpus)


@pytest.fixture
def fakes(monkeypatch):
    return _Fakes(monkeypatch)


async def _pi(db, year: int, source: str = "earliest_hopkins_paper", name: str | None = None) -> User:
    user = User(
        name=f"PI {uuid.uuid4().hex[:6]}" if name is None else name,
        orcid=_orcid(),
        institution="Johns Hopkins",
    )
    db.add(user)
    await db.flush()
    await set_tenure_start(user.id, year, source, db=db)
    await db.flush()
    return user


def _key(user: User) -> str:
    return f"{TENURE_KEY_PREFIX}{user.id}"


async def _raw(db, user: User) -> str | None:
    return (
        await db.execute(select(AppSetting.value).where(AppSetting.key == _key(user)))
    ).scalar_one_or_none()


async def _stored(db, user: User) -> dict:
    return json.loads(await _raw(db, user))


async def _profile_jobs(db, user: User, status: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user.id, Job.type == "generate_profile")
    )
    if status is not None:
        stmt = stmt.where(Job.status == status)
    return (await db.execute(stmt)).scalar_one()


async def _apply(db, orcids, tmp_path, **kw) -> int:
    return await rt.run(
        db, orcids=orcids, apply=True, backup_dir=tmp_path,
        allow_unmounted_backup_dir=True, **kw,
    )


async def test_preview_writes_nothing_and_queues_nothing(db_session, fakes, tmp_path, capsys):
    a = await _pi(db_session, 2018)
    b = await _pi(db_session, 2016)
    fakes.corpora[a.orcid] = _corpus(_paper(2009))
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
    fakes.corpora[user.orcid] = _corpus(_paper(2020), _paper(2009))

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    stored = await _stored(db_session, user)
    assert (stored["year"], stored["source"]) == (2009, "earliest_hopkins_paper")
    job = (
        await db_session.execute(select(Job).where(Job.user_id == user.id))
    ).scalar_one()
    assert job.type == "generate_profile"
    assert job.status == "pending"
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


async def test_the_year_comes_from_the_uncapped_corpus(db_session, fakes, tmp_path):
    """60 records, newest first; only the oldest is Hopkins and it falls
    outside the 50-record ``kept``."""
    user = await _pi(db_session, 2018)
    papers = [_paper(2025 - i, "MIT") for i in range(59)] + [_paper(1960)]
    fakes.corpora[user.orcid] = _corpus(*papers, cap=50)
    assert all(p["year"] != 1960 for p in fakes.corpora[user.orcid].kept)

    await _apply(db_session, [user.orcid], tmp_path)

    assert (await _stored(db_session, user))["year"] == 1960


async def test_an_empty_user_name_is_filled_from_orcid(db_session, fakes, tmp_path):
    user = await _pi(db_session, 2018, name="")
    fakes.profiles[user.orcid] = {"orcid": user.orcid, "name": "Jane Doe", "employments": []}
    fakes.corpora[user.orcid] = _corpus(_paper(2009))

    await rt.run(db_session, orcids=[user.orcid], apply=False, backup_dir=tmp_path)

    assert fakes.corpus_calls == [
        (user.orcid, "Jane Doe", user.institution, rt.CORPUS_CAP)
    ]


async def test_orcid_employment_wins_when_it_is_available(db_session, fakes, tmp_path):
    user = await _pi(db_session, 2018)
    fakes.profiles[user.orcid] = {"orcid": user.orcid, "employments": [_HOPKINS_EMPLOYMENT]}
    fakes.corpora[user.orcid] = _corpus(_paper(2005))

    await _apply(db_session, [user.orcid], tmp_path)

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

    await _apply(db_session, [u.orcid for u in users], tmp_path)

    assert [await _raw(db_session, u) for u in users] == before
    assert (
        await db_session.execute(
            select(AppSetting.value).where(AppSetting.key == LEGACY_TENURE_KEY)
        )
    ).scalar_one() == legacy
    assert fakes.orcid_calls == []
    for u in users:
        assert await _profile_jobs(db_session, u) == 0


async def test_an_unscoped_run_covers_every_paper_row(db_session, fakes, tmp_path, capsys):
    """``run(orcids=[])`` is exactly what production runs."""
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = _corpus(_paper(2009))
    orphan_key = f"{TENURE_KEY_PREFIX}{uuid.uuid4()}"
    orphan_value = json.dumps({"year": 2014, "source": "earliest_hopkins_paper"})
    db_session.add(AppSetting(key=orphan_key, value=orphan_value))
    await db_session.flush()

    code = await _apply(db_session, [], tmp_path)

    assert code == 0
    assert (await _stored(db_session, user))["year"] == 2009
    assert await _profile_jobs(db_session, user) == 1
    assert (
        await db_session.execute(select(AppSetting.value).where(AppSetting.key == orphan_key))
    ).scalar_one() == orphan_value
    assert f"{orphan_key}  SKIP no_user" in capsys.readouterr().out


async def test_a_corpus_failure_skips_the_pi_with_no_write(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = CorpusStageError("PubMed efetch batch failed")
    before = await _raw(db_session, user)

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert "SKIP corpus_stage_failed" in capsys.readouterr().out


async def test_any_corpus_exception_skips_the_pi_with_no_write(
    db_session, fakes, tmp_path, capsys
):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = RuntimeError("OpenAlex exploded")
    before = await _raw(db_session, user)

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert list(tmp_path.iterdir()) == []
    assert "SKIP corpus_error: RuntimeError" in capsys.readouterr().out


async def test_an_orcid_failure_skips_the_pi_with_no_write(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.profiles[user.orcid] = RuntimeError("ORCID 503")
    fakes.corpora[user.orcid] = _corpus(_paper(2009))
    before = await _raw(db_session, user)

    await _apply(db_session, [user.orcid], tmp_path)

    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    # A paper year is never derived without employments to consult (D8).
    assert fakes.corpus_calls == []
    assert "SKIP orcid_unavailable" in capsys.readouterr().out


async def test_a_manual_edit_made_during_the_run_survives(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = _corpus(_paper(2009))
    manual = json.dumps({"year": 2013, "source": "manual", "derived_at": "x"})

    async def manager_edits_the_row():
        await db_session.execute(
            update(AppSetting).where(AppSetting.key == _key(user)).values(value=manual)
        )

    fakes.during_corpus[user.orcid] = manager_edits_the_row

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    assert await _raw(db_session, user) == manual
    assert await _profile_jobs(db_session, user) == 0
    out = capsys.readouterr().out
    assert "SKIP changed_since_read" in out
    assert "rewritten=0 changed_since_read=1" in out


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
    fakes.corpora[user.orcid] = _corpus(_paper(2009))

    await _apply(db_session, [user.orcid], tmp_path)

    assert (await _stored(db_session, user))["year"] == 2009
    assert await _profile_jobs(db_session, user) == 1


async def test_a_processing_profile_job_is_reported_not_duplicated(
    db_session, fakes, tmp_path, capsys
):
    """A processing job read the old year already; its output is stale."""
    user = await _pi(db_session, 2018)
    db_session.add(
        Job(
            type="generate_profile",
            user_id=user.id,
            status="processing",
            payload={"user_id": str(user.id), "orcid": user.orcid},
        )
    )
    await db_session.flush()
    fakes.corpora[user.orcid] = _corpus(_paper(2009))

    await _apply(db_session, [user.orcid], tmp_path)

    assert await _profile_jobs(db_session, user, "processing") == 1
    assert await _profile_jobs(db_session, user, "pending") == 0
    out = capsys.readouterr().out
    assert "profile_jobs_queued_behind_processing=1" in out


async def test_apply_commits_swaps_once_then_each_queue_call(db_session, fakes, tmp_path, monkeypatch):
    a = await _pi(db_session, 2018)
    b = await _pi(db_session, 2017)
    for u in (a, b):
        fakes.corpora[u.orcid] = _corpus(_paper(2009))
    commits: list[int] = []
    real_commit = db_session.commit

    async def counting_commit():
        commits.append(1)
        await real_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)

    await _apply(db_session, [a.orcid, b.orcid], tmp_path)

    assert len(commits) == 3  # the swaps, then one per queued user
    assert (await _stored(db_session, a))["year"] == 2009
    assert (await _stored(db_session, b))["year"] == 2009


async def test_max_changes_aborts_before_any_write(db_session, fakes, tmp_path, capsys):
    a = await _pi(db_session, 2018)
    b = await _pi(db_session, 2017)
    for u in (a, b):
        fakes.corpora[u.orcid] = _corpus(_paper(2009))
    before = [await _raw(db_session, a), await _raw(db_session, b)]

    code = await _apply(db_session, [a.orcid, b.orcid], tmp_path, max_changes=1)

    assert code != 0
    assert [await _raw(db_session, a), await _raw(db_session, b)] == before
    assert await _profile_jobs(db_session, a) == 0
    assert await _profile_jobs(db_session, b) == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert "ABORT" in out
    assert a.orcid in out and b.orcid in out  # the table is still printed


async def test_apply_refuses_an_unmounted_backup_dir(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = _corpus(_paper(2009))
    before = await _raw(db_session, user)
    assert not os.path.ismount(tmp_path)

    code = await rt.run(db_session, orcids=[user.orcid], apply=True, backup_dir=tmp_path)

    assert code != 0
    assert await _raw(db_session, user) == before
    assert fakes.orcid_calls == []
    assert list(tmp_path.iterdir()) == []
    assert "not a mount point" in capsys.readouterr().out


async def test_the_backup_is_written_before_the_first_write_and_restores_it(
    db_session, fakes, tmp_path, monkeypatch
):
    changed = await _pi(db_session, 2018)
    kept = await _pi(db_session, 2012)
    fakes.corpora[changed.orcid] = _corpus(_paper(2009))
    fakes.corpora[kept.orcid] = _corpus(_paper(2012))
    originals = {changed.id: await _raw(db_session, changed), kept.id: await _raw(db_session, kept)}

    real_swap = rt._swap_value
    seen_backup: list[dict] = []

    async def checked_swap(db, key, expected, new):
        files = list(tmp_path.glob("tenure_starts_*.json"))
        assert len(files) == 1, "backup must exist before the first write"
        seen_backup.append(json.loads(files[0].read_text()))
        return await real_swap(db, key, expected, new)

    monkeypatch.setattr(rt, "_swap_value", checked_swap)
    await _apply(db_session, [changed.orcid, kept.orcid], tmp_path)
    monkeypatch.setattr(rt, "_swap_value", real_swap)

    assert len(seen_backup) == 1
    (backup,) = tmp_path.glob("tenure_starts_*.json")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    saved = {row["key"]: row for row in seen_backup[0]["rows"]}
    # Every candidate row is saved, with the exact value this run wrote.
    assert set(saved) == {_key(changed), _key(kept)}
    assert saved[_key(changed)]["old"] == originals[changed.id]
    assert saved[_key(changed)]["written"] == await _raw(db_session, changed)
    assert saved[_key(kept)] == {"key": _key(kept), "old": originals[kept.id], "written": None}
    assert (await _stored(db_session, changed))["year"] == 2009

    assert await rt.restore(db_session, backup) == 0
    assert await _raw(db_session, changed) == originals[changed.id]
    assert await _raw(db_session, kept) == originals[kept.id]


async def _applied_backup(db, fakes, tmp_path, user: User) -> Path:
    fakes.corpora[user.orcid] = _corpus(_paper(2009))
    assert await _apply(db, [user.orcid], tmp_path) == 0
    (backup,) = tmp_path.glob("tenure_starts_*.json")
    return backup


async def test_restore_leaves_a_row_edited_since_apply(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    backup = await _applied_backup(db_session, fakes, tmp_path, user)
    await set_tenure_start(user.id, 2013, "manual", db=db_session)
    manual = await _raw(db_session, user)

    assert await rt.restore(db_session, backup) == 0

    assert await _raw(db_session, user) == manual
    assert "SKIP changed_since_apply" in capsys.readouterr().out


async def test_restore_never_resurrects_a_deleted_pi(db_session, fakes, tmp_path, capsys):
    gone = await _pi(db_session, 2018)
    purged = await _pi(db_session, 2017)
    fakes.corpora[purged.orcid] = _corpus(_paper(2009))
    backup = await _applied_backup(db_session, fakes, tmp_path, gone)
    # (Two applies would write two backups; fold `purged` into the same file.)
    doc = json.loads(backup.read_text())
    purged_written = json.dumps({"year": 2009, "source": "earliest_hopkins_paper", "derived_at": "x"})
    doc["rows"].append(
        {"key": _key(purged), "old": await _raw(db_session, purged), "written": purged_written}
    )
    backup.write_text(json.dumps(doc))
    await db_session.execute(
        update(AppSetting).where(AppSetting.key == _key(purged)).values(value=purged_written)
    )
    # `gone`: user row deleted, key left behind. `purged`: both deleted.
    gone_raw = await _raw(db_session, gone)
    await db_session.execute(delete(User).where(User.id.in_([gone.id, purged.id])))
    await db_session.execute(delete(AppSetting).where(AppSetting.key == _key(purged)))
    await db_session.flush()

    assert await rt.restore(db_session, backup) == 0

    assert (
        await db_session.execute(select(AppSetting.value).where(AppSetting.key == _key(gone)))
    ).scalar_one() == gone_raw
    assert (
        await db_session.execute(select(AppSetting.value).where(AppSetting.key == _key(purged)))
    ).scalar_one_or_none() is None
    out = capsys.readouterr().out
    assert out.count("SKIP user_deleted") == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(format="something-else"),
        lambda doc: doc["rows"][0].update(key="slack_config_token"),
        lambda doc: doc["rows"][0].update(key=f"{TENURE_KEY_PREFIX}not-a-uuid"),
        lambda doc: doc["rows"][0].update(old="not json"),
        lambda doc: doc["rows"][0].update(old=json.dumps({"year": "2018", "source": "manual"})),
        lambda doc: doc["rows"][0].update(written=json.dumps({"year": 2018, "source": 7})),
    ],
    ids=["format", "foreign-key", "bad-uuid", "old-not-json", "year-not-int", "source-not-str"],
)
async def test_restore_refuses_a_file_it_did_not_write(db_session, fakes, tmp_path, capsys, mutate):
    user = await _pi(db_session, 2018)
    backup = await _applied_backup(db_session, fakes, tmp_path, user)
    after_apply = await _raw(db_session, user)
    doc = json.loads(backup.read_text())
    mutate(doc)
    backup.write_text(json.dumps(doc))

    assert await rt.restore(db_session, backup) == 2

    assert await _raw(db_session, user) == after_apply
    assert "ABORT" in capsys.readouterr().out


async def test_no_rederived_year_is_reported_not_deleted(db_session, fakes, tmp_path, capsys):
    user = await _pi(db_session, 2018)
    fakes.corpora[user.orcid] = _corpus(_paper(2004, "MIT"))
    before = await _raw(db_session, user)

    await _apply(db_session, [user.orcid], tmp_path)

    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert "SKIP no_rederived_year" in capsys.readouterr().out


async def test_an_incomplete_corpus_skips_the_pi_with_no_write(
    db_session, fakes, tmp_path, capsys
):
    # resolve_corpus returned normally but dropped a PMID permanently (its
    # single re-fetch came back 400). That PMID could be the earliest Hopkins
    # paper, so the 2009 re-derived from what survived must not be written.
    user = await _pi(db_session, 2018)
    corpus = _corpus(_paper(2009))
    corpus.permanently_dropped = ["31000000"]
    fakes.corpora[user.orcid] = corpus
    before = await _raw(db_session, user)

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert "SKIP incomplete_corpus: 1 records permanently unavailable (31000000)" in out


@pytest.mark.parametrize(
    "value",
    [
        json.dumps({"year": "2015", "source": "earliest_hopkins_paper"}),
        json.dumps({"year": True, "source": "earliest_hopkins_paper"}),
    ],
    ids=["string-year", "bool-year"],
)
async def test_a_row_restore_could_not_accept_is_reported_not_a_candidate(
    db_session, fakes, tmp_path, capsys, value
):
    # `int("2015")` used to make this a candidate; its `old` value then made
    # restore's validator refuse the WHOLE backup, the good rows included.
    odd = await _pi(db_session, 2018)
    await db_session.execute(
        update(AppSetting).where(AppSetting.key == _key(odd)).values(value=value)
    )
    good = await _pi(db_session, 2018)
    fakes.corpora[odd.orcid] = _corpus(_paper(2009))
    fakes.corpora[good.orcid] = _corpus(_paper(2009))

    code = await _apply(db_session, [odd.orcid, good.orcid], tmp_path)

    assert code == 0
    assert await _raw(db_session, odd) == value
    assert await _profile_jobs(db_session, odd) == 0
    assert odd.orcid not in fakes.orcid_calls
    out = capsys.readouterr().out
    assert f"{_key(odd)}  SKIP unparseable_value" in out
    (backup,) = tmp_path.glob("tenure_starts_*.json")
    doc = json.loads(backup.read_text())
    assert {row["key"] for row in doc["rows"]} == {_key(good)}
    # The backup this run wrote is one its own --restore accepts.
    assert rt._validate_backup(doc) is None
    assert await rt.restore(db_session, backup) == 0
    assert (await _stored(db_session, good))["year"] == 2018


async def test_a_later_year_is_marked_suspect_in_the_preview(
    db_session, fakes, tmp_path, capsys
):
    user = await _pi(db_session, 2012)
    fakes.corpora[user.orcid] = _corpus(_paper(2015))

    await rt.run(db_session, orcids=[user.orcid], apply=False, backup_dir=tmp_path)

    out = capsys.readouterr().out
    assert f"{user.orcid}  2012 -> 2015 (earliest_hopkins_paper) CHANGE LATER (suspect)" in out


async def test_a_later_year_is_skipped_under_apply_by_default(
    db_session, fakes, tmp_path, capsys
):
    # Thinning can only make a stored year too LATE; a later re-derived year
    # narrows the window and hides in-tenure papers, so it is not applied
    # without an explicit --allow-later.
    user = await _pi(db_session, 2012)
    fakes.corpora[user.orcid] = _corpus(_paper(2015))
    before = await _raw(db_session, user)

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    assert await _raw(db_session, user) == before
    assert await _profile_jobs(db_session, user) == 0
    assert list(tmp_path.iterdir()) == []
    assert "SKIP later_than_stored" in capsys.readouterr().out


async def test_a_later_employment_year_is_applied_without_allow_later(
    db_session, fakes, tmp_path
):
    # The guard is about thinned papers. ORCID employment outranks any paper
    # year in the pipeline, so a later employment start is applied as it is.
    user = await _pi(db_session, 2005)
    fakes.profiles[user.orcid] = {"orcid": user.orcid, "employments": [_HOPKINS_EMPLOYMENT]}

    code = await _apply(db_session, [user.orcid], tmp_path)

    assert code == 0
    stored = await _stored(db_session, user)
    assert (stored["year"], stored["source"]) == (2011, "orcid_employment")
    assert await _profile_jobs(db_session, user) == 1


async def test_a_later_year_is_applied_with_allow_later(db_session, fakes, tmp_path):
    user = await _pi(db_session, 2012)
    fakes.corpora[user.orcid] = _corpus(_paper(2015))

    code = await _apply(db_session, [user.orcid], tmp_path, allow_later=True)

    assert code == 0
    stored = await _stored(db_session, user)
    assert (stored["year"], stored["source"]) == (2015, "earliest_hopkins_paper")
    assert await _profile_jobs(db_session, user) == 1
