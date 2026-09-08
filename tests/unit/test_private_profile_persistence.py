"""RC-7 (audit 2026-09-08): a failed private-profile disk write must not clobber the
DB copy with stale content.

Root cause (pre-fix): `Agent.update_private_profile` was disk-first and swallowed the
exception on failure, invalidating the in-memory cache (`self._private_profile = None`)
without updating it. `persist_private_profile_to_db` then wrote `self.private_profile`,
whose getter re-reads the (unchanged, stale) file from disk on a cache miss — so a
permission-denied temp-file write silently overwrote the DB with the OLD profile while
the PI was told the new instruction had been recorded.

The fix makes `update_private_profile` return a bool and set the in-memory cache to the
*attempted* new content regardless of disk outcome, and makes `persist_private_profile_to_db`
take the content explicitly rather than ever re-reading `self.private_profile` (disk or
cache) — the two are decoupled, and the DB write cannot regress a successful DB write with
whatever the disk happens to hold.
"""

import logging
import uuid

from src.agent import agent as agent_module
from src.agent.agent import Agent


def _agent(tmp_path, monkeypatch) -> Agent:
    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    a = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
    a._private_profile = "old content"
    return a


# ---------------------------------------------------------------------------
# update_private_profile: bool return, cache always reflects the accepted
# instruction
# ---------------------------------------------------------------------------


def test_a_successful_disk_write_returns_true_and_caches_the_new_content(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    ok = a.update_private_profile("new content")
    assert ok is True
    assert a.private_profile == "new content"
    assert (tmp_path / "private" / "su.md").read_text(encoding="utf-8") == "new content\n"


def test_a_failed_disk_write_returns_false_logs_error_and_still_caches_the_new_content(
    tmp_path, monkeypatch, caplog,
):
    """The core regression: a failed disk write must not leave the agent serving (or
    persisting) stale content — the accepted instruction must win in memory even though
    it could not be written to disk."""
    a = _agent(tmp_path, monkeypatch)

    def _boom(path, text, encoding="utf-8"):
        raise OSError("Permission denied")

    monkeypatch.setattr(agent_module, "atomic_write_text", _boom)

    with caplog.at_level(logging.ERROR, logger="src.agent.agent"):
        ok = a.update_private_profile("new content")

    assert ok is False
    assert any(r.levelno == logging.ERROR for r in caplog.records), (
        "a failed disk write must log an ERROR"
    )
    # The whole point: memory reflects the accepted instruction, not the stale disk file.
    assert a.private_profile == "new content"
    # And the disk file was genuinely never written (control on the assertion above).
    assert not (tmp_path / "private" / "su.md").exists()


# ---------------------------------------------------------------------------
# persist_private_profile_to_db: explicit content, never re-reads disk/cache
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    """Scripts `db.execute(...)` by call order: first the AgentRegistry lookup,
    then the ResearcherProfile lookup — matching persist_private_profile_to_db's
    two sequential selects. Records `commit()` calls and profile mutations."""

    def __init__(self, agent_reg, profile):
        self._results = [_FakeResult(agent_reg), _FakeResult(profile)]
        self._call = 0
        self.committed = False

    async def execute(self, *a, **k):
        result = self._results[self._call]
        self._call += 1
        return result

    async def commit(self):
        self.committed = True


class _BoomingDb:
    async def execute(self, *a, **k):
        raise RuntimeError("connection reset")

    async def commit(self):
        self.committed = True


async def test_persist_writes_the_explicit_content_argument_not_the_cache(tmp_path, monkeypatch):
    """The crux of the fix: even though the in-memory cache holds something else
    entirely, the DB gets exactly the `content` argument."""
    a = _agent(tmp_path, monkeypatch)
    a._private_profile = "whatever happens to be cached"

    from types import SimpleNamespace

    user_id = uuid.uuid4()
    agent_reg = SimpleNamespace(user_id=user_id)
    profile = SimpleNamespace(private_profile_md="old db content")
    db = _FakeDb(agent_reg, profile)

    ok = await a.persist_private_profile_to_db(db, "the new instruction, explicitly")

    assert ok is True
    assert profile.private_profile_md == "the new instruction, explicitly"
    assert db.committed is True


async def test_persist_returns_false_and_logs_when_the_agent_registry_row_is_missing(
    tmp_path, monkeypatch, caplog,
):
    a = _agent(tmp_path, monkeypatch)
    db = _FakeDb(agent_reg=None, profile=None)

    with caplog.at_level(logging.ERROR, logger="src.agent.agent"):
        ok = await a.persist_private_profile_to_db(db, "content")

    assert ok is False
    assert db.committed is False


async def test_persist_returns_false_and_logs_on_a_db_exception(tmp_path, monkeypatch, caplog):
    a = _agent(tmp_path, monkeypatch)
    db = _BoomingDb()

    with caplog.at_level(logging.ERROR, logger="src.agent.agent"):
        ok = await a.persist_private_profile_to_db(db, "content")

    assert ok is False
    assert any(r.levelno == logging.ERROR for r in caplog.records)
