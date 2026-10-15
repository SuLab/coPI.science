"""R1.4: scripts/scrub_job_error_api_keys.py scrubs api_key values from stored
jobs.last_error text, prints counts only, and never overwrites a row changed since it
was read."""
import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import Job, User

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scrub_job_error_api_keys.py"
SECRET = "SECRETKEY0123456789"
KEYED = (
    "corpus stage s1 failed: Client error '404 Not Found' for url "
    "'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed"
    f"&api_key={SECRET}&tool=copi-science'"
)
REDACTED = KEYED.replace(SECRET, "REDACTED")
STATUSES = ("pending", "processing", "completed", "failed", "dead")


def _load():
    spec = importlib.util.spec_from_file_location("scrub_job_error_api_keys", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sj = _load()


async def _seed(db) -> dict[str, uuid.UUID]:
    """Five keyed rows (one per status), one already redacted, one keyless, one NULL;
    each job has its own user, so the one-active-job index allows them."""
    texts = {s: KEYED for s in STATUSES}
    texts |= {"redacted": REDACTED, "keyless": "plain failure", "null": None}
    status_of = {"redacted": "completed", "keyless": "failed", "null": "failed"}
    ids: dict[str, uuid.UUID] = {}
    for label, text in texts.items():
        user = User(name=f"Scrub {label}", orcid=f"SCRUB-{uuid.uuid4().hex[:12]}",
                    email=f"scrub-{uuid.uuid4().hex[:12]}@example.invalid")
        db.add(user)
        await db.flush()
        job = Job(type="generate_profile", user_id=user.id, payload={"tag": "scrub"},
                  status=status_of.get(label, label), last_error=text)
        db.add(job)
        await db.flush()
        ids[label] = job.id
    return ids


async def _text(db, job_id):
    return (await db.execute(
        select(Job.last_error).where(Job.id == job_id).execution_options(populate_existing=True)
    )).scalar_one()


async def test_preview_counts_by_status_and_writes_nothing(db_session):
    ids = await _seed(db_session)
    counts = await sj.scrub(db_session, await sj.read_candidates(db_session), apply=False)
    assert counts.holding == 5
    assert counts.mentioning == {"pending": 1, "processing": 1, "completed": 2, "failed": 1,
                                 "dead": 1}
    for label in STATUSES:
        assert await _text(db_session, ids[label]) == KEYED
    assert sj.report_lines(counts, apply=False) == [
        "jobs.last_error rows mentioning api_key=: 6 "
        "(pending=1, processing=1, completed=2, failed=1, dead=1)",
        "  already redacted: 1",
        "  holding a key value: 5",
        "would scrub: 5",
    ]


async def test_apply_scrubs_every_status_and_keeps_the_rest_of_the_text(db_session):
    ids = await _seed(db_session)
    counts = await sj.scrub(db_session, await sj.read_candidates(db_session), apply=True)
    assert counts.scrubbed == 5 and counts.changed_since_read == 0
    for label in STATUSES:
        assert await _text(db_session, ids[label]) == REDACTED
    assert await _text(db_session, ids["keyless"]) == "plain failure"
    assert await _text(db_session, ids["null"]) is None
    assert sj.exit_code(counts, apply=True) == 0


async def test_a_second_apply_changes_nothing(db_session):
    await _seed(db_session)
    await sj.scrub(db_session, await sj.read_candidates(db_session), apply=True)
    again = await sj.scrub(db_session, await sj.read_candidates(db_session), apply=True)
    assert again.holding == 0 and again.scrubbed == 0
    assert sj.exit_code(again, apply=True) == 0


async def test_a_row_changed_since_read_is_skipped(db_session):
    ids = await _seed(db_session)
    rows = await sj.read_candidates(db_session)
    await db_session.execute(
        update(Job).where(Job.id == ids["failed"]).values(last_error="edited by hand")
    )
    counts = await sj.scrub(db_session, rows, apply=True)
    assert counts.changed_since_read == 1 and counts.scrubbed == 4
    assert await _text(db_session, ids["failed"]) == "edited by hand"
    assert sj.exit_code(counts, apply=True) == 1


# ---- main(), end to end, on committed rows ------------------------------------------


@pytest.fixture
def committed(engine, monkeypatch):
    """Committed seed rows that main()'s own session can see; removed afterwards."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(sj, "get_session_factory", lambda: factory)

    async def seed():
        async with factory() as db:
            ids = await _seed(db)
            await db.commit()
            return ids

    async def cleanup():
        async with factory() as db:
            await db.execute(delete(User).where(User.orcid.like("SCRUB-%")))
            await db.commit()

    ids = asyncio.run(seed())
    try:
        yield factory, ids
    finally:
        asyncio.run(cleanup())


def test_main_prints_counts_only(committed, capsys):
    _factory, ids = committed
    with pytest.raises(SystemExit) as stop:
        sj.main([])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert not any(str(job_id) in out for job_id in ids.values())
    assert "holding a key value: 5" in out


def test_main_apply_exits_1_when_a_row_changed_since_read(committed, monkeypatch, capsys):
    factory, ids = committed
    real_read = sj.read_candidates

    async def read_then_change(db):
        rows = await real_read(db)
        async with factory() as other:
            await other.execute(
                update(Job).where(Job.id == ids["dead"]).values(last_error="edited by hand"))
            await other.commit()
        return rows

    monkeypatch.setattr(sj, "read_candidates", read_then_change)
    with pytest.raises(SystemExit) as stop:
        sj.main(["--apply"])
    assert stop.value.code == 1
    assert "changed since read, left for a re-run: 1" in capsys.readouterr().out
