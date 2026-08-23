"""One un-writable row must not take its whole batch down with it.

All three flushers (`_flush_persisted`, `_flush_llm_logs`,
`_flush_pending_assessments`) add N rows, commit ONCE, and on failure re-queue
the whole batch while logging "re-queued for retry". `stop()` makes exactly one
final attempt, so at shutdown that message is false: the batch is lost, quietly,
and a single bad row loses every good row beside it.

Two traps make the naive fix harmful, and both are pinned here.

1. **Only row-specific errors may trigger the per-row fallback.** The commonest
   failure on this path is the pool-checkout timeout `_persist_assessment`'s own
   comment names. A per-row fallback on THAT issues N sequential checkouts; ~15
   rows at a 30 s timeout blows the documented 420 s `docker stop` grace from
   inside `stop()`, which gets the process SIGKILLed and loses the batch PLUS
   everything not yet flushed.
2. **The failed session cannot be reused.** In all three flushers the `except`
   sits OUTSIDE `async with self.session_factory() as db:`, so the session is
   already closed and rolled back. The fallback has to open a NEW one and take a
   savepoint per row, or the first failure leaves the session in
   `PendingRollbackError` and the remainder is lost anyway.
"""
import logging
import time as _real_time

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine

POISON = "poison"


class _Clock:
    """`time` for the simulation module, with a scripted `monotonic`.

    Every other attribute proxies through to the real module, so code under
    test that wants `time.time()` still gets a wall clock.
    """

    def __init__(self, ticks):
        self._ticks = iter(ticks)
        self._last = 0.0

    def monotonic(self):
        try:
            self._last = next(self._ticks)
        except StopIteration:
            pass
        return self._last

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _FakeResult:
    """Just enough of a Result for `_flush_persisted`'s run-stats refresh."""

    def scalar_one_or_none(self):
        return None

    def scalar_one(self):
        return 0


class _FakeNested:
    """A savepoint. Releasing it flushes, exactly like the real thing."""

    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        self._s.savepoints += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._s.pending.clear()
            return False
        self._s._release()
        return False


class _FakeSession:
    def __init__(self, store, savepoint_log, first_commit_error=None):
        self.store = store
        self.pending: list = []
        self.savepoints = 0
        self.commits = 0
        self._first_commit_error = first_commit_error
        self._savepoint_log = savepoint_log

    def add(self, obj):
        self.pending.append(obj)

    async def execute(self, stmt):
        # `_flush_persisted` inserts through a Core statement rather than the
        # ORM. `Insert.values([...])` keeps the dicts verbatim on
        # `_multi_values`, so the fake can see exactly what would be written.
        for group in getattr(stmt, "_multi_values", ()):
            self.pending.extend(group)
        return _FakeResult()

    async def commit(self):
        self.commits += 1
        if self._first_commit_error is not None and self.commits == 1:
            err, self._first_commit_error = self._first_commit_error, None
            self.pending.clear()
            raise err
        self._release()

    async def rollback(self):
        self.pending.clear()

    def _release(self):
        bad = [o for o in self.pending if _is_poison(o)]
        if bad:
            self.pending.clear()
            raise IntegrityError("INSERT", {}, Exception("null value in column"))
        self.store.extend(self.pending)
        self.pending.clear()

    def begin_nested(self):
        self._savepoint_log.append(id(self))
        return _FakeNested(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _is_poison(obj) -> bool:
    if isinstance(obj, dict):
        return obj.get("agent_id") == POISON
    if type(obj).__name__ == "AssessmentDrop":
        # A drop RECORDING the poison row carries the same agent_id but is a
        # different write, on a different table, that must succeed.
        return False
    return getattr(obj, "agent_id", None) == POISON


class _FakeFactory:
    """A new session per call, like `async_sessionmaker`.

    Records every session it handed out so a test can prove the per-row pass
    did NOT reuse the one whose commit failed.
    """

    def __init__(self, store, *, first_commit_error=None):
        self.store = store
        self.sessions: list[_FakeSession] = []
        self.savepoint_log: list[int] = []
        self._first_commit_error = first_commit_error

    def __call__(self):
        # Only the FIRST session gets the batch error; the recovery session must
        # be able to make progress.
        err = self._first_commit_error if not self.sessions else None
        s = _FakeSession(self.store, self.savepoint_log, first_commit_error=err)
        self.sessions.append(s)
        return s


def _engine(factory) -> SimulationEngine:
    eng = SimulationEngine(
        agents=[Agent("hub", "HubBot", "PI hub")], slack_clients={},
        session_factory=factory, simulation_run_id="run-1",
    )
    return eng


def _log_entry(agent_id: str) -> dict:
    return {
        "agent_id": agent_id, "phase": "thread_reply", "model": "m",
        "system_prompt": "s", "messages": [], "response_text": "r",
        "input_tokens": 1, "output_tokens": 1, "latency_ms": 1.0,
    }


async def test_one_bad_row_does_not_lose_the_batch():
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("null value in column")))
    eng = _engine(factory)
    eng._llm_log_buffer = [_log_entry("a"), _log_entry(POISON), _log_entry("b")]

    await eng._flush_llm_logs()

    kept = sorted(r.agent_id for r in store)
    assert kept == ["a", "b"], (
        f"the good rows went down with the poison row: {kept}"
    )
    assert eng._llm_log_buffer == [], (
        "the poison row was re-queued, so every future flush fails the same way"
    )


async def test_the_per_row_fallback_opens_a_new_session():
    """Trap 2. The failed session is already closed and rolled back."""
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("null value in column")))
    eng = _engine(factory)
    eng._llm_log_buffer = [_log_entry("a"), _log_entry(POISON)]

    await eng._flush_llm_logs()

    assert len(factory.sessions) == 2, (
        "the per-row pass reused the session whose commit had already failed"
    )
    assert factory.savepoint_log, "the per-row pass took no savepoint at all"
    assert set(factory.savepoint_log) == {id(factory.sessions[1])}, (
        "savepoints were taken on the failed session, not the fresh one"
    )


async def test_a_pool_timeout_does_not_trigger_the_per_row_fallback():
    """Trap 1. N sequential checkouts at a 30s timeout blows the stop grace.

    CONTROL, NOT A TDD CYCLE — this passed before the fix as well as after,
    because the old code re-queued every failure unconditionally and so could not
    fail it. Its teeth come from the mutation direction instead: widening the
    gate to `except Exception` (which is the harmful "obvious" fix) turns it red.
    """
    store: list = []
    factory = _FakeFactory(store, first_commit_error=OperationalError(
        "SELECT 1", {}, Exception("QueuePool limit ... connection timed out")))
    eng = _engine(factory)
    rows = [_log_entry("a"), _log_entry("b")]
    eng._llm_log_buffer = list(rows)

    await eng._flush_llm_logs()

    assert len(factory.sessions) == 1, (
        "a pool-checkout timeout triggered a per-row retry — N more checkouts "
        "on a pool that is already exhausted, from inside the stop grace period"
    )
    assert store == []
    assert eng._llm_log_buffer == rows, (
        "a pool timeout is transient; the batch must be re-queued intact"
    )


async def test_a_failed_shutdown_flush_says_lost_not_requeued(caplog):
    """`stop()` makes exactly ONE final attempt, so "re-queued for retry" lies."""
    store: list = []
    factory = _FakeFactory(store, first_commit_error=OperationalError(
        "SELECT 1", {}, Exception("QueuePool limit ... connection timed out")))
    eng = _engine(factory)
    eng._llm_log_buffer = [_log_entry("a"), _log_entry("b")]

    with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
        await eng._flush_llm_logs(final=True)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "LOST" in text, f"the shutdown flush still claimed a retry: {text!r}"
    assert "2" in text, f"the LOST message does not name the row count: {text!r}"
    assert "re-queued for retry" not in text
    assert eng._llm_log_buffer == [], (
        "nothing will drain this buffer after stop() — holding the rows only "
        "hides the loss"
    )


async def test_the_assessment_flusher_isolates_its_poison_row():
    """The row that is the actual product of the pipeline gets the same treatment."""
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("value too long for type character varying")))
    eng = _engine(factory)
    eng._pending_assessments = [
        {"simulation_run_id": "run-1", "agent_id": "a", "channel_name": "c"},
        {"simulation_run_id": "run-1", "agent_id": POISON, "channel_name": "c"},
        {"simulation_run_id": "run-1", "agent_id": "b", "channel_name": "c"},
    ]

    await eng._flush_pending_assessments()

    # Filtered by type: the store also holds the `AssessmentDrop` this loss now
    # writes (FIX 6), whose agent_id is the poison row's by design.
    kept = sorted(
        r.agent_id for r in store if type(r).__name__ == "OpportunityAssessment"
    )
    assert kept == ["a", "b"], f"the good assessments went with the bad one: {kept}"
    assert eng._pending_assessments == []


async def test_the_message_flusher_isolates_its_poison_row(monkeypatch):
    """`_flush_persisted` inserts through a Core statement, not the ORM."""
    from src.agent.message_log import LogEntry

    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("value too long for type character varying")))
    eng = _engine(factory)
    eng._pending_persist = [
        LogEntry(ts="1.1", channel="general", sender_agent_id="a",
                 sender_name="A", content="x", posted_at=1.1),
        LogEntry(ts="2.2", channel="general", sender_agent_id=POISON,
                 sender_name="P", content="x", posted_at=2.2),
        LogEntry(ts="3.3", channel="general", sender_agent_id="b",
                 sender_name="B", content="x", posted_at=3.3),
    ]

    await eng._flush_persisted()

    kept = sorted(r["agent_id"] for r in store)
    assert kept == ["a", "b"], f"the good messages went with the bad one: {kept}"
    assert eng._pending_persist == []


async def test_a_per_row_pass_that_exhausts_its_deadline_requeues_the_rest(
    monkeypatch,
):
    """The deadline is the second half of trap 1 — a slow recovery must stop."""
    import src.agent.simulation as sim

    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("null value in column")))
    eng = _engine(factory)
    eng._llm_log_buffer = [_log_entry(str(i)) for i in range(4)]

    # A shim bound to the simulation module only. Patching `time.monotonic`
    # itself patches the real stdlib module for every other consumer in the
    # process — pytest's own fixture teardown included.
    monkeypatch.setattr(sim, "time", _Clock([0.0, 0.0, 100.0]))

    await eng._flush_llm_logs()

    assert len(store) == 1, f"expected one row written before the deadline: {store}"
    assert len(eng._llm_log_buffer) == 3, (
        "the rows the deadline stopped us attempting must be re-queued, not "
        f"dropped: {eng._llm_log_buffer}"
    )


@pytest.mark.parametrize("flusher", [
    "_flush_persisted", "_flush_llm_logs", "_flush_pending_assessments",
])
def test_no_flusher_falls_back_on_a_bare_exception(flusher):
    """A structural guard on trap 1, in case a future edit widens the gate.

    Parsed with `ast`, not grepped. The first version of this test asserted
    `"_ROW_LEVEL_DB_ERRORS" in inspect.getsource(...)`, and review defeated it
    with `if isinstance(exc, Exception):  # _ROW_LEVEL_DB_ERRORS` — the mutation
    stayed green because a COMMENT satisfied the substring. What is asserted now
    is the shape of the actual call: every `isinstance(...)` that guards the
    recovery must name `_ROW_LEVEL_DB_ERRORS` as its second argument, as a bare
    Name. A comment cannot satisfy that, and neither can
    `isinstance(exc, Exception)`.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(getattr(SimulationEngine, flusher)))
    tree = ast.parse(src)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
    ]
    assert calls, (
        f"{flusher} has no isinstance() gate at all — its per-row recovery is "
        "either missing or entered on a bare `except Exception`, which re-arms "
        "the pool-timeout storm"
    )
    second_args = {
        arg.id for call in calls
        for arg in [call.args[1]] if isinstance(arg, ast.Name)
    }
    assert second_args == {"_ROW_LEVEL_DB_ERRORS"}, (
        f"{flusher} gates its per-row recovery on {second_args or 'a non-Name'} "
        "rather than _ROW_LEVEL_DB_ERRORS"
    )

    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_recover_rows_individually" in called, (
        f"{flusher} never CALLS the per-row recovery (a mention in a comment or "
        "a docstring is not a call)"
    )


# ----------------------------------------------------------------------
# FIX 6 — a verdict lost to per-row recovery must leave an AssessmentDrop.
#
# `_recover_rows_individually` logs "DROPPING one un-writable ... row" and, for
# `what="assessment"`, that was a SCREENING VERDICT discarded on one log line —
# while every OTHER way a verdict fails to land writes an `AssessmentDrop`. The
# per-row path is one A3.4 itself created: before it, a poison row lost its whole
# batch loudly and re-queued; after it, one row is dropped quietly.
# ----------------------------------------------------------------------


def _verdict_row(agent_id: str, **over) -> dict:
    row = {
        "simulation_run_id": "run-1",
        "agent_id": agent_id,
        "channel_name": "single-cell-omics",
        "subject_agent_id": "gordy",
        "thread_id": "t-1",
        "slack_ts": "1700000001.000100",
        "raw_verdict": {"recommendation": "advance", "weighted_score": 3.04},
    }
    row.update(over)
    return row


async def test_an_assessment_lost_to_per_row_recovery_leaves_a_drop():
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("value too long for type character varying")))
    eng = _engine(factory)
    eng._pending_assessments = [
        _verdict_row("a", thread_id="t-a"),
        _verdict_row(POISON, thread_id="t-poison"),
        _verdict_row("b", thread_id="t-b"),
    ]

    await eng._flush_pending_assessments()

    drops = [o for o in store if type(o).__name__ == "AssessmentDrop"]
    assert len(drops) == 1, (
        "the verdict the database refused vanished on a single log line, while "
        f"every other way of losing one writes a drop row: {drops}"
    )
    drop = drops[0]
    assert drop.agent_id == POISON
    assert drop.thread_id == "t-poison", "the drop must identify the interview"
    assert drop.subject_agent_id == "gordy"
    assert drop.raw_verdict == {"recommendation": "advance", "weighted_score": 3.04}, (
        "the whole point of raw_verdict: a gate decision must not also destroy "
        "the verdict"
    )
    assert drop.reason and drop.reason not in {
        "specialist_floor", "unparseable_sidecar", "missing_sidecar",
        "premature_sidecar", "closed_before_verdict", "duplicate_thread_verdict",
        "empty_reply",
    }, f"the reason must be distinguishable from the existing vocabulary: {drop.reason}"
    # And the good rows still landed.
    assert sorted(
        o.agent_id for o in store if type(o).__name__ == "OpportunityAssessment"
    ) == ["a", "b"]


async def test_the_other_flushers_do_not_write_assessment_drops():
    """A message or an LLM-log row is not a verdict; only assessments get drops."""
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("null value in column")))
    eng = _engine(factory)
    eng._llm_log_buffer = [_log_entry("a"), _log_entry(POISON)]

    await eng._flush_llm_logs()

    assert [o for o in store if type(o).__name__ == "AssessmentDrop"] == []


async def test_a_failing_drop_write_does_not_raise_into_the_flush(caplog):
    """Best-effort, and loudly so — the flush must finish either way."""
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("value too long")))
    eng = _engine(factory)

    async def _boom_drop(*a, **kw):
        raise RuntimeError("the drops table is gone too")

    eng._record_assessment_drop = _boom_drop
    eng._pending_assessments = [
        _verdict_row("a", thread_id="t-a"),
        _verdict_row(POISON, thread_id="t-poison"),
    ]

    with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
        await eng._flush_pending_assessments()

    assert sorted(
        o.agent_id for o in store if type(o).__name__ == "OpportunityAssessment"
    ) == ["a"], "a failed drop write took the surviving verdict with it"
    assert eng._pending_assessments == []
    assert any(
        "the drops table is gone too" in r.getMessage() for r in caplog.records
    ), "a failed drop write must be loud, not silent"


class _MappingWithoutGet:
    """Unpacks with ``**`` (``keys``/``__getitem__``) but has NO ``.get``.

    The shape that makes the error handler raise WHILE HANDLING. Not reachable
    from production today — `_pending_assessments` has exactly one append site
    (`_persist_assessment`) and it always appends a dict — which is precisely why
    the claim in `_record_unwritable_assessment`'s docstring ("the wrapper is for
    everything before that point, a malformed row") has to be pinned by a test
    rather than trusted: nothing else can falsify it.
    """

    def __init__(self, data: dict):
        self._d = data

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k]


async def test_a_malformed_row_does_not_escape_the_drop_handler(caplog):
    """The `except` must not touch `row` — it is handling `row` being unusable."""
    eng = _engine(_FakeFactory([]))

    with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
        # Must not raise. Pre-fix the handler called `row.get("thread_id")`
        # while handling the failure of `row.get("agent_id")`.
        await eng._record_unwritable_assessment(
            _MappingWithoutGet({"agent_id": "a"}), RuntimeError("refused"),
        )

    assert any(
        "un-writable assessment" in r.getMessage() for r in caplog.records
    ), "a row too broken to record must still be loud"


async def test_a_malformed_row_does_not_abort_the_rest_of_the_flush():
    """End to end: the handler's own failure must not escape `_flush_pending_assessments`.

    The loop that calls `_record_unwritable_assessment` sits INSIDE that
    flusher's `except`, so an exception there leaves the function entirely —
    skipping `_report_flush_failure` and the re-queue, and taking every later
    lost row's drop with it. The third row here is the discriminator: it is a
    perfectly ordinary poison dict that is owed a drop, and pre-fix it never got
    one because the second row aborted the loop.
    """
    store: list = []
    factory = _FakeFactory(store, first_commit_error=IntegrityError(
        "INSERT", {}, Exception("value too long")))
    eng = _engine(factory)
    eng._pending_assessments = [
        _verdict_row("a", thread_id="t-a"),
        _MappingWithoutGet(_verdict_row(POISON, thread_id="t-broken")),
        _verdict_row(POISON, thread_id="t-ordinary"),
    ]

    await eng._flush_pending_assessments()

    drops = [o for o in store if type(o).__name__ == "AssessmentDrop"]
    assert [d.thread_id for d in drops] == ["t-ordinary"], (
        "the malformed row's handler raised out of the flusher, so the NEXT "
        f"lost verdict never got its drop row: {[d.thread_id for d in drops]}"
    )
    assert sorted(
        o.agent_id for o in store if type(o).__name__ == "OpportunityAssessment"
    ) == ["a"]
    assert eng._pending_assessments == []
