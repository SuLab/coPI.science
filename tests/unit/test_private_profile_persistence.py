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


# ---------------------------------------------------------------------------
# PIHandler._handle_standing_instruction end-to-end: DB-first-then-disk, with
# a stub session_factory (RC-7 follow-ups 2 and 3, Opus review 2026-09-08).
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StubDb:
    """Scripts `db.execute(...)` by call order, matching
    PIHandler._handle_standing_instruction's fixed sequence: the two selects
    inside `persist_private_profile_to_db` (AgentRegistry, then
    ResearcherProfile), then — only when that persist succeeds — the
    revision-lookup selects (AgentRegistry again, then a User join).

    `raise_on_call` lets a test make one specific call in that sequence blow
    up, simulating a genuine DB failure (as opposed to "no such row").
    """

    def __init__(self, results, *, raise_on_call: int | None = None):
        self._results = list(results)
        self._call = 0
        self._raise_on_call = raise_on_call
        self.commits = 0

    async def execute(self, *a, **k):
        self._call += 1
        if self._raise_on_call == self._call:
            raise RuntimeError("connection reset")
        return _StubResult(self._results.pop(0))

    async def commit(self):
        self.commits += 1


class _StubSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StubSessionFactory:
    def __init__(self, db):
        self._db = db

    def __call__(self):
        return _StubSessionCtx(self._db)


def _pi_handler(tmp_path, monkeypatch, db, *, disk_write_result: bool = True):
    """Build a PIHandler wired to a real Agent (disk under tmp_path) and a
    stub DB session_factory, with the LLM and Slack DM calls stubbed out.
    Returns (handler, agent, sent) where `sent` accumulates every DM text."""
    from src.agent import pi_handler as ph
    from src.agent.message_log import MessageLog

    monkeypatch.setattr(agent_module, "PROFILES_DIR", tmp_path)
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "su.md").write_text("old instruction\n")

    agent = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
    handler = ph.PIHandler(
        agents={"su": agent},
        slack_clients={},
        pi_slack_id_to_agent_ids={"U1": ["su"]},
        message_log=MessageLog(),
        session_factory=_StubSessionFactory(db),
    )

    async def _fake_llm(**kwargs):
        return "<profile>new instruction from PI</profile><changes>tightened scope</changes>"

    sent: list[str] = []

    async def _fake_dm(agent_id, pi_slack_id, text):
        sent.append(text)

    revisions_created: list[str] = []

    async def _fake_create_revision(db_arg, **kwargs):
        revisions_created.append(kwargs.get("content"))
        return None

    monkeypatch.setattr(ph, "generate_agent_response", _fake_llm)
    monkeypatch.setattr(handler, "_send_dm", _fake_dm)
    monkeypatch.setattr(
        "src.services.profile_versioning.create_revision", _fake_create_revision
    )
    monkeypatch.setattr(
        agent, "update_private_profile", lambda text: disk_write_result
    )

    return handler, agent, sent, revisions_created


async def test_a_db_failure_tells_the_pi_it_could_not_be_saved_and_skips_the_disk_write(
    tmp_path, monkeypatch,
):
    """RC-7 follow-ups 2+3: when the DB persist fails, (a) the acknowledgement
    must say so rather than claiming success, (b) no profile revision is
    recorded, and (c) the disk write is skipped entirely so the agent's actual
    in-memory profile is not left disagreeing with what the PI was told (an
    unsaved instruction must not silently take effect)."""
    from types import SimpleNamespace

    agent_reg = SimpleNamespace(user_id=uuid.uuid4())
    # raise_on_call=2: the AgentRegistry lookup (call 1) succeeds, then the
    # ResearcherProfile lookup (call 2) blows up — persist_private_profile_to_db
    # catches this and returns False.
    db = _StubDb([agent_reg], raise_on_call=2)

    handler, agent, sent, revisions_created = _pi_handler(tmp_path, monkeypatch, db)
    disk_write_calls = []
    monkeypatch.setattr(
        agent, "update_private_profile",
        lambda text: disk_write_calls.append(text) or True,
    )

    await handler._handle_standing_instruction("su", "U1", "always cite DOIs")

    assert len(sent) == 1
    assert "wasn't able to save it" in sent[0]
    assert "I've updated my private profile" not in sent[0]
    assert revisions_created == [], "a failed DB persist must not record a profile revision"
    assert disk_write_calls == [], "the disk write must be skipped when the DB persist failed"
    # The ack and the agent's actual behaviour must agree: nothing changed.
    assert agent.private_profile == "old instruction\n"


async def test_a_revision_bookkeeping_failure_after_a_committed_profile_row_still_acknowledges_success(
    tmp_path, monkeypatch,
):
    """Opus follow-up review (2026-09-08): `persist_private_profile_to_db` commits
    internally and returns True, but the profile-revision bookkeeping that followed
    it (two more SELECTs, create_revision, a second commit) used to sit inside the
    SAME outer try/except as the persist call. An exception there flipped `db_ok`
    back to False AFTER the profile row was already durably committed, producing a
    three-way divergence: DB = new content, disk + in-memory cache = old content,
    and the PI told "wasn't able to save it" -- a false negative on top of an
    inconsistent state that a later disk export could resurrect. Once the profile
    row is committed, nothing downstream may un-commit it."""
    from types import SimpleNamespace

    agent_reg = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    profile = SimpleNamespace(private_profile_md="old db content")
    # Calls 1-2: persist_private_profile_to_db's own two selects (AgentRegistry,
    # ResearcherProfile) succeed and it commits. Call 3: the revision block's
    # AgentRegistry re-lookup -- blows up, AFTER the profile row is already saved.
    db = _StubDb([agent_reg, profile], raise_on_call=3)

    handler, agent, sent, revisions_created = _pi_handler(tmp_path, monkeypatch, db)
    disk_write_calls = []
    monkeypatch.setattr(
        agent, "update_private_profile",
        lambda text: disk_write_calls.append(text) or True,
    )

    await handler._handle_standing_instruction("su", "U1", "always cite DOIs")

    assert len(sent) == 1
    assert "I've updated my private profile" in sent[0], (
        "the profile row committed successfully -- a downstream revision-"
        "bookkeeping failure must not turn that into a false 'could not be "
        "saved' report"
    )
    assert "wasn't able to save it" not in sent[0]
    assert profile.private_profile_md == "new instruction from PI"  # the DB write stuck
    assert disk_write_calls == ["new instruction from PI"], (
        "the disk write must still be attempted once the profile row is committed"
    )
    assert revisions_created == [], (
        "the revision bookkeeping itself failed, so nothing was recorded"
    )
    assert db.commits == 1, "only persist's own commit -- the revision's second commit never ran"


async def test_a_disk_failure_alone_still_acknowledges_success_and_the_db_holds_the_new_content(
    tmp_path, monkeypatch,
):
    """RC-7's main fix, exercised through the full handler: the DB is primary,
    so a disk write failure (permission denied, a read-only mount, ...) after
    a successful DB persist must not be reported to the PI as a failure — and
    the DB row must actually carry the new content, not something stale."""
    from types import SimpleNamespace

    agent_reg = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    profile = SimpleNamespace(private_profile_md="old db content")
    # persist_private_profile_to_db's two selects, then the revision lookup's
    # two selects (AgentRegistry again, then the PI User row).
    db = _StubDb([agent_reg, profile, agent_reg, None])

    handler, agent, sent, revisions_created = _pi_handler(
        tmp_path, monkeypatch, db, disk_write_result=False,  # disk write "fails"
    )

    await handler._handle_standing_instruction("su", "U1", "always cite DOIs")

    assert len(sent) == 1
    assert "I've updated my private profile" in sent[0]
    assert "wasn't able to save it" not in sent[0]
    assert profile.private_profile_md == "new instruction from PI"
    assert revisions_created == ["new instruction from PI"]
    assert db.commits == 2  # one inside persist_private_profile_to_db, one after the revision


class _RaisingExitSessionCtx(_StubSessionCtx):
    """A session whose teardown blows up (a dropped connection on close/rollback)."""

    async def __aexit__(self, exc_type, exc, tb):
        raise RuntimeError("connection lost during session close")


async def test_a_session_teardown_failure_after_a_committed_profile_row_still_acknowledges_success(
    tmp_path, monkeypatch,
):
    """Opus tail review (2026-09-10): `db_ok` was assigned inside the `async with`, so an
    exception from the session's `__aexit__` -- after persist had already committed --
    still reached the outer handler and flipped it to False. Only "the profile row was
    never committed" may produce the retry acknowledgement."""
    from types import SimpleNamespace

    agent_reg = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    profile = SimpleNamespace(private_profile_md="old db content")
    db = _StubDb([agent_reg, profile, agent_reg, None])

    handler, agent, sent, revisions_created = _pi_handler(tmp_path, monkeypatch, db)
    handler.session_factory = lambda: _RaisingExitSessionCtx(db)
    disk_write_calls = []
    monkeypatch.setattr(
        agent, "update_private_profile",
        lambda text: disk_write_calls.append(text) or True,
    )

    await handler._handle_standing_instruction("su", "U1", "always cite DOIs")

    assert len(sent) == 1
    assert "I've updated my private profile" in sent[0]
    assert "wasn't able to save it" not in sent[0]
    assert profile.private_profile_md == "new instruction from PI"
    assert disk_write_calls == ["new instruction from PI"]
