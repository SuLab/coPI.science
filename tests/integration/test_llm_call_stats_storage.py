"""`llm_call_logs.call_stats` round-trips through the real buffered flush path.

The unit tests pin the LIST src/services/llm.py builds. This pins the other half:
that `SimulationEngine._on_llm_call` -> `_flush_llm_logs` carries it onto the row,
that Postgres holds it as a queryable JSONB *array* (the whole reason it is not
`json` like `messages_json`, and not stuffed into `messages_json` at all), and
that a pre-0032-shaped row with `call_stats` absent still reads back cleanly
rather than as an empty list or a JSONB `null` scalar.

The SQL-level assertions are deliberate, and the lesson is 0031's: through the
ORM a SQL NULL and the JSONB scalar `null` are the same Python `None`, so an
ORM-only test cannot tell the two encodings apart — and `WHERE call_stats IS
NULL`, the obvious way to count un-instrumented rows, silently disagrees with
them. `jsonb_array_length` / `jsonb_array_elements` are also exactly how the
column is meant to be read, so exercising them here proves the column is
actually queryable and not just storable.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.simulation import SimulationEngine
from src.models import USER_ROLE_ADMIN, LlmCallLog, SimulationRun
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


_STATS = [
    {
        "seq": 1, "kind": "round", "max_tokens": 16000, "input_tokens": 12043,
        "output_tokens": 16000, "thinking_tokens": 9604,
        "stop_reason": "max_tokens", "latency_ms": 31204.6,
    },
    {
        "seq": 2, "kind": "final", "max_tokens": 16000, "input_tokens": 14880,
        "output_tokens": 2210, "thinking_tokens": None,
        "stop_reason": "end_turn", "latency_ms": 8112.0,
    },
]


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


def _entry(**over) -> dict:
    """The dict shape src/services/llm.py hands `_call_log_callback`."""
    base = {
        "agent_id": "blackbird",
        "phase": "thread_reply",
        "channel": "scout-wang",
        "model": "claude-test",
        "system_prompt": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "response_text": "reply",
        "input_tokens": 26923,
        "output_tokens": 18210,
        "latency_ms": 39316.6,
        "completed_at": datetime.now(UTC),
    }
    base.update(over)
    return base


async def _flush_one(factory, run_id, entry) -> None:
    """Through the real engine buffer + flush, not a hand-built LlmCallLog."""
    sim = SimulationEngine(
        agents=[], slack_clients={}, session_factory=factory, simulation_run_id=run_id,
    )
    sim._on_llm_call(entry)
    await sim._flush_llm_logs()
    assert sim._llm_log_buffer == [], "a successful flush must drain the buffer"


@pytest.mark.asyncio
async def test_call_stats_round_trips_through_the_buffered_flush(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        await _flush_one(factory, run_id, _entry(call_stats=_STATS))

        async with factory() as db:
            row = (await db.execute(
                select(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)
            )).scalar_one()
        assert row.call_stats == _STATS
        # The cumulative columns are untouched by this change and stay the
        # per-turn totals the admin page sums (item 5).
        assert (row.input_tokens, row.output_tokens) == (26923, 18210)
        # A per-entry null must survive as JSON null, not become 0 — "the SDK
        # did not report a thinking split" and "there was no thinking" are
        # different answers, and only the second licenses a conclusion.
        assert row.call_stats[1]["thinking_tokens"] is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_postgres_holds_it_as_a_queryable_jsonb_array(engine):
    """The column exists to be asked questions in SQL. This is the query the
    thread_reply sizing exercise had to answer by reading log text instead."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        await _flush_one(factory, run_id, _entry(call_stats=_STATS))

        async with factory() as db:
            shape = (await db.execute(text(
                "SELECT jsonb_typeof(call_stats) AS json_type, "
                "       jsonb_array_length(call_stats) AS n_calls "
                "  FROM llm_call_logs WHERE simulation_run_id = :run"
            ), {"run": str(run_id)})).one()
            assert shape.json_type == "array"
            assert shape.n_calls == 2

            truncated = (await db.execute(text(
                "SELECT c->>'kind' AS kind, "
                "       (c->>'max_tokens')::int AS ceiling, "
                "       (c->>'thinking_tokens')::int AS thinking "
                "  FROM llm_call_logs l, jsonb_array_elements(l.call_stats) c "
                " WHERE l.simulation_run_id = :run "
                "   AND c->>'stop_reason' = 'max_tokens'"
            ), {"run": str(run_id)})).all()
        assert [(r.kind, r.ceiling, r.thinking) for r in truncated] == [
            ("round", 16000, 9604)
        ], (
            "one truncating TOOL ROUND, at a 16000 ceiling, 60% of it spent "
            "thinking — none of which was recordable before this column"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_an_entry_without_call_stats_stores_a_real_sql_null(engine):
    """Every row written before 0032, and any producer that supplies nothing.

    A SQL NULL, not the JSONB scalar `null`: the column is mapped
    `JSONB(none_as_null=True)` precisely because SQLAlchemy's JSON default
    writes Python None as `null`, giving "not recorded" two physical encodings
    that `IS NULL` cannot both see. That bug shipped once already, on
    `opportunity_assessments.missing_domains`, and needed migration 0031 to
    clean up 15 rows.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        await _flush_one(factory, run_id, _entry())  # no call_stats key at all

        async with factory() as db:
            row = (await db.execute(
                select(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)
            )).scalar_one()
            assert row.call_stats is None
            # Everything else about the row is unchanged, so an old-shaped row
            # is still fully readable — this is what the admin page renders.
            assert (row.phase, row.channel, row.response_text) == (
                "thread_reply", "scout-wang", "reply",
            )

            shape = (await db.execute(text(
                "SELECT call_stats IS NULL AS sql_null, "
                "       jsonb_typeof(call_stats) AS json_type "
                "  FROM llm_call_logs WHERE simulation_run_id = :run"
            ), {"run": str(run_id)})).one()
        assert shape.sql_null is True
        assert shape.json_type is None
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# The admin LLM-calls page: the only reader of the token columns (item 5).
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="call-stats-admin@example.org"
    )


async def test_the_admin_page_still_renders_its_totals_and_per_row_tokens(
    client, db_session, admin
):
    """The non-breaking check, asserted rather than argued.

    `admin_llm_calls` reads SUM(input_tokens) / SUM(output_tokens) /
    AVG(latency_ms) into three stat tiles, and the per-row line prints
    `input+output tok` and `latency ms`. Those three columns keep their per-turn
    cumulative meaning under this change — nothing was split into one row per API
    call — so both surfaces must be untouched. A row with `call_stats` NULL and a
    row with it populated are rendered side by side here on purpose: the mixed
    state is what production looks like across the 0032 deploy.
    """
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        channel="scout-wang", response_text="INSTRUMENTED-ROW",
        input_tokens=26923, output_tokens=18210, latency_ms=39316.6,
        call_stats=_STATS,
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="memory",
        response_text="PRE-0032-ROW",
        input_tokens=1000, output_tokens=500, latency_ms=683.4,
        call_stats=None,
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id)
        )
    ).text

    assert "INSTRUMENTED-ROW" in html
    assert "PRE-0032-ROW" in html, "a NULL call_stats row must still render"
    # Stat tiles: SUM/SUM/AVG over both rows, comma-formatted by the template.
    assert "27,923" in html, "Input Tokens tile"
    assert "18,710" in html, "Output Tokens tile"
    assert "20000.0ms" in html, "Avg Latency tile"
    # Per-row tokens/latency line, unchanged.
    assert "26923+18210 tok" in html
    assert "1000+500 tok" in html
    assert "39317ms" in html


async def test_a_turn_with_a_truncated_call_is_badged_and_a_clean_one_is_not(
    client, db_session, admin
):
    """The one thing worth scanning a page of turns for. Before 0032 a truncating
    TOOL ROUND produced no log line, no retry and no row content, so this page
    could not have shown it at any cost."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        response_text="TRUNCATED-TURN", call_stats=_STATS,
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        response_text="CLEAN-TURN",
        call_stats=[{
            "seq": 1, "kind": "final", "max_tokens": 16000, "input_tokens": 900,
            "output_tokens": 120, "thinking_tokens": None,
            "stop_reason": "end_turn", "latency_ms": 1200.0,
        }],
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id)
        )
    ).text

    # Counted on the badge's own class, not on the word "truncated" or on the
    # template's indentation — "truncated" appears in prose elsewhere on this
    # page's rows and whitespace is not a contract.
    assert html.count("bg-orange-100") == 1, "exactly one of the two turns truncated"
    assert (
        "1 of 2 API call(s) in this turn stopped before finishing "
        "(max_tokens; ceiling 16000)"
    ) in html
    # The call count makes a multi-call row legible as such; the sums above are
    # otherwise a total of an unknown number of addends.
    assert "2 calls" in html
    assert "1 call<" in html


# ---------------------------------------------------------------------------
# The cache-token columns (0036), which nothing in the unit suite can reach.
#
# `tests/unit/test_llm_call_stats.py` asserts the two counts on the PAYLOAD the
# fake callback receives. That is the producer half. This is the consumer half,
# and it had no test at all: `_llm_log_record` maps the payload key by key, so a
# key the producer adds and the mapper does not read reaches the callback and
# stops there — silently, with the column NULL forever and the data recoverable
# only by digging back into `call_stats`. Both surfaces looked covered; the seam
# between them was not.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cache_counts_round_trip_through_the_buffered_flush(engine):
    """The columns exist to be SUMmed: billable input volume is
    `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
    (see LlmCallLog's docstring), and that sum is unavailable while either
    column is NULL on every row."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        await _flush_one(
            factory,
            run_id,
            _entry(
                call_stats=_STATS,
                cache_read_input_tokens=118_204,
                cache_creation_input_tokens=30_512,
            ),
        )

        async with factory() as db:
            row = (await db.execute(
                select(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)
            )).scalar_one()
            assert row.cache_read_input_tokens == 118_204
            assert row.cache_creation_input_tokens == 30_512
            # The uncached tail is a SEPARATE number, not a total — the whole
            # reason there are three columns rather than one.
            assert row.input_tokens == 26923

            billable = (await db.execute(text(
                "SELECT sum(input_tokens) AS uncached, "
                "       sum(cache_read_input_tokens) AS cache_read, "
                "       sum(cache_creation_input_tokens) AS cache_write "
                "  FROM llm_call_logs WHERE simulation_run_id = :run"
            ), {"run": str(run_id)})).one()
        assert (billable.uncached, billable.cache_read, billable.cache_write) == (
            26923, 118_204, 30_512
        ), "the aggregate query the columns were added for must not return NULL"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_an_entry_without_the_cache_counts_stores_null_not_zero(engine):
    """`_sum_reported` yields None when no API call reported either field, and
    None must reach the column as NULL. Zero would be a claim — "the cache was
    read zero times" — that nothing measured."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    try:
        await _flush_one(factory, run_id, _entry(call_stats=_STATS))  # no cache keys

        async with factory() as db:
            row = (await db.execute(
                select(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)
            )).scalar_one()
            assert row.cache_read_input_tokens is None
            assert row.cache_creation_input_tokens is None
            # Everything else on the row is unaffected: an uninstrumented
            # producer still writes a complete row.
            assert (row.input_tokens, row.call_stats) == (26923, _STATS)
    finally:
        await _delete_run(factory, run_id)


async def test_a_refusal_truncated_turn_is_badged_on_the_operator_page(
    client, db_session, admin
):
    """`refusal` is a truncation too — `src/services/llm.py::is_truncated_stop`
    is the one definition of that predicate, and the engine, the specialist
    floor and the Slack posting path all read it.

    This page labelled a turn truncated on `stop_reason == 'max_tokens'` alone,
    so a refusal-truncated turn rendered as COMPLETE on the one surface an
    operator would open to audit truncation. Run 8b64a0e0 posted four truncated
    replies as complete and overwrote a working memory with a refusal-truncated
    synthesis; this page could not have shown either.
    """
    run = await factories.make_simulation_run(db_session)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        response_text="REFUSAL-TRUNCATED-TURN",
        call_stats=[{
            "seq": 1, "kind": "final", "max_tokens": 16000, "input_tokens": 900,
            "output_tokens": 16000, "thinking_tokens": None,
            "stop_reason": "refusal", "latency_ms": 21000.0,
        }],
    )
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="blackbird", phase="thread_reply",
        response_text="CLEAN-TURN",
        call_stats=[{
            "seq": 1, "kind": "final", "max_tokens": 16000, "input_tokens": 900,
            "output_tokens": 120, "thinking_tokens": None,
            "stop_reason": "end_turn", "latency_ms": 1200.0,
        }],
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id)
        )
    ).text

    # The badge's own class, for the same reason the max_tokens test counts it:
    # "truncated" appears in prose elsewhere on this page.
    assert html.count("bg-orange-100") == 1, (
        "the refusal turn is badged and the end_turn turn is not"
    )
    assert "refusal" in html, "the title must name WHICH stop reason cut it off"


def test_the_page_reads_the_truncation_predicate_rather_than_a_copy_of_it():
    """The set `{refusal, max_tokens}` is defined once, in
    `src/services/llm.py::is_truncated_stop`. The template is the last reader
    that used to hand-copy half of it; it must now call the function, or a third
    stop reason added to the predicate leaves this page disagreeing again.

    Asserted on the Jinja environment that actually renders the page, not on the
    template text: a template can name a test that was never registered, and
    that failure is a silently empty selection rather than an error.
    """
    from src.routers.admin import templates as admin_templates
    from src.services.llm import is_truncated_stop

    assert admin_templates.env.tests.get("truncated_stop") is is_truncated_stop
