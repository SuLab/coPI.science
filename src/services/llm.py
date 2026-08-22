"""Anthropic Claude API wrapper."""

import asyncio
import contextvars
import json
import logging
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache, partial
from typing import Any, Callable

import anthropic

from src.config import get_settings

logger = logging.getLogger(__name__)

# The READ timeout every request in this module inherits from the shared client.
#
# 300 s, and the number is measured rather than chosen. Run 8b64a0e0 stalled
# twice at 600.09 / 600.10 s — `read=600` exactly, which is the SDK's own
# default — on HTTP 200 responses, so no overload or rate-limit story survives:
# the connection simply went quiet. 1,167 s (10.6%) of that run was dead air.
#
# NOT 120, which the first audit draft proposed: the run's largest LEGITIMATE
# reply took 119.18 s, 0.8 s under that ceiling, and `max_tokens=16000` at the
# observed 60.7 tok/s authorises up to 264 s of honest generation. 300 cut zero
# legitimate calls in the measured run. The one place it is tight is a
# truncation retry clamped to NONSTREAMING_MAX_TOKENS: 21_333 tokens at 60.7
# tok/s is ~351 s, so a retry that genuinely needs its whole budget can now time
# out. That is a rare path, it already has a loud WARNING, and the alternative is
# keeping a 600 s stall on the common one.
#
# `connect` is deliberately left at the SDK's own 5 s instead of being dragged up
# with the rest — a bare `timeout=300.0` float sets connect/read/write/pool
# alike, and a black-holed SYN is not a long generation. (One of this run's three
# SDK retries was a fast connection failure, not a timeout; that should stay
# fast.)
CLIENT_READ_TIMEOUT_SECONDS = 300.0

# The largest `max_tokens` the Anthropic SDK will accept on a NON-STREAMING
# request. `BaseClient._calculate_nonstreaming_timeout` computes
# `expected_time = 3600 * max_tokens / 128_000` and raises
# ValueError("Streaming is required for operations that may take longer than 10
# minutes...") as soon as that exceeds its 600-second default timeout. So the
# last accepted value is floor(600 * 128_000 / 3600) = 21_333, and 21_334 is
# refused locally, before any HTTP request is made. Probed on both SDKs this
# repo runs: anthropic 1.0.0 (the deployed agent image) and 0.120.2
# (.venv-test) — highest accepted 21_333 in each. The SDK also carries a
# per-model override (`MODEL_NONSTREAMING_TOKENS`, 8192 for the opus-4 ids)
# which names no model configured here, so the formula is the only binding
# limit for us.
#
# Every call in this module is non-streaming, so this is a hard ceiling on what
# a call site may request AND on what a truncation retry may double up to. It
# used to be unreachable in practice: at the old 4000 thread_reply ceiling the
# 2x retry asked for 8000. At 16000 it asks for 32000, which the SDK refuses —
# raising inside the retry, after the truncated first reply has already been
# discarded, so the turn dies with nothing posted, no verdict and no drop row.
# tests/fakes.py's FakeAnthropic enforces the same limit, because the suite
# drives that seam and never reaches the real client, so nothing else in CI can
# see this ceiling at all.
#
# ⚠️ THE SDK NO LONGER ENFORCES THIS FOR US. `Messages.create` applies
# `_calculate_nonstreaming_timeout` only `if not stream and not
# is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT` — and
# `_client_for_key` now passes a timeout of its own (CLIENT_READ_TIMEOUT_SECONDS
# above), so that condition is permanently false. Verified against the installed
# SDK source, both versions this repo runs. The check in `_acreate` is therefore
# the ONLY thing standing between a mis-sized call site and a request the API
# will reject after it has been sent: do not remove it, and do not "simplify" it
# on the grounds that the SDK checks too. tests/unit/
# test_llm_nonstreaming_ceiling.py asserts both halves.
NONSTREAMING_MAX_TOKENS = 21_333

# Module-level callback for logging LLM calls.
# Signature: callback(data: dict) where data contains system_prompt, messages,
# response_text, model, input_tokens, output_tokens, latency_ms, call_stats, and
# any extra keys from log_meta.
#
# ONE callback fires per TURN, not per API call — and a turn is 1..8 real API
# calls at the default `max_tool_rounds=5`: up to max_tool_rounds + 1 tool-capable
# calls (the loop is `range(max_tool_rounds + 1)` — see the comment on it, which
# explains why the `+ 1` stays), a terminating or forced-final call, and at most
# one max_tokens retry. This comment said "1..7" until 2026-08-22, taking the
# setting's name at face value and under-counting the loop by one.
#
# `input_tokens`/`output_tokens` are
# per-turn CUMULATIVE totals: correct for billing, and deliberately left that way
# because SimulationEngine rebuilds `api_call_count` as a row COUNT and the rate
# limiter's `call_times` as one entry per row, so splitting a turn into several
# rows would inflate both after every restart.
#
# `latency_ms` is NOT one of those sums, despite being logged beside them. In
# `generate_with_tools` it is ASSIGNED per call, not accumulated across rounds, so
# a multi-round row carries the LAST call's latency plus any retry's — never the
# wall time of the turn. The two only coincide in `generate_agent_response`, which
# makes one call plus at most a retry. Per-call latency is recoverable from
# `call_stats`; a multi-round turn's total wall time is recorded nowhere.
#
# `call_stats` is what makes the row interpretable anyway: a list with one object
# per real API call, in call order — see `_call_stat`. It is the only place
# `stop_reason`, the requested `max_tokens` ceiling, the thinking/text split of
# `output_tokens`, and the reply's content-BLOCK types are recorded at all.
#
# One row per turn also means a turn that returned nothing. `generate_agent
# _response`'s empty-content branch used to return above the callback, so 26
# billed refusals in run 8b64a0e0 wrote no row at all — see `_emit_call_log`.
_call_log_callback: Callable[[dict], None] | None = None


def set_call_log_callback(callback: Callable[[dict], None] | None) -> None:
    """Register (or clear) a callback that fires after every LLM call."""
    global _call_log_callback
    _call_log_callback = callback


@lru_cache(maxsize=8)
def _client_for_key(api_key: str) -> anthropic.Anthropic:
    """One long-lived client per API key. ``anthropic.Anthropic`` owns an
    httpx connection pool; constructing one per call meant a fresh TCP+TLS
    handshake for every LLM call in the engine — thread replies, up to 8
    specialist consults per concluding turn, memory updates, retries
    (audit 2026-08-21, finding 4: 8 calls -> 8 connections; 1 shared client
    -> 1). The sync client is thread-safe, so one instance is safe under
    ``asyncio.to_thread`` concurrency.

    Also the one place the request timeout is set, so every call in the process
    inherits it — including ``email_inbound.classify_reply``, which used to make
    its own untimed request. See CLIENT_READ_TIMEOUT_SECONDS for why 300, and
    NONSTREAMING_MAX_TOKENS for what setting any timeout at all costs us."""
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=anthropic.Timeout(CLIENT_READ_TIMEOUT_SECONDS, connect=5.0),
    )


# One `cache_control` value, used at both breakpoints. `{"type": "ephemeral"}`
# with no `ttl` is the DEFAULT 5-minute TTL, chosen deliberately over `"1h"`:
# median hub turn inter-arrival is 58 s, so the common case is comfortably inside
# 5 minutes, and the extra hits an hour-long entry would catch do not pay for
# doubling the write premium (1.25x -> 2x base input price; break-even moves from
# two requests to three).
_EPHEMERAL_CACHE = {"type": "ephemeral"}

# Where an agent's system prompt stops being stable. `Agent._compose_system_prompt`
# renders this section LAST — after the base role prompt, the rendered rubric, the
# identity block, the public lab profile and (when present) the lab directory — so
# everything a turn changes lives behind it.
#
# ⚠️ That ordering is what makes caching possible at all. Measured on run
# 8b64a0e0: 90.4% of the hub's prompt is the stable half, and the re-sent system
# prompt is 5.18 M tokens = 40.6% of all input spend. Move working memory earlier,
# or interpolate anything per-turn ahead of it, and the hit rate silently goes to
# zero — no error, no failing request, just the old bill.
# tests/unit/test_llm_prompt_caching.py is the alarm on that.
_STABLE_PREFIX_BOUNDARY = "\n## Your Working Memory\n"


def _cacheable_system(system_prompt: str) -> list[dict[str, Any]]:
    """Split a system prompt into a cache-marked stable prefix and a live tail.

    The breakpoint goes at the END of the stable prefix, not at the end of the
    whole prompt: marking the volatile half would write a distinct cache entry
    every turn and read none of them, paying the write premium for nothing.

    A prompt with no working-memory section — a specialist persona, a
    profile-synthesis prompt, a phase-1 decision — is static per call site, so
    the whole thing is the prefix and comes back as one marked block.

    One breakpoint here covers the tool definitions too: the API renders
    ``tools`` -> ``system`` -> ``messages``, so a marker on a system block caches
    everything before it. ``tools_for_role`` is deterministic per role, which is
    what makes that safe — a tool list that varied per turn would render at
    position 0 and invalidate every downstream entry.

    Returns blocks, never a bare string, so callers do not have to care which
    shape they got. `_acreate` is the only caller.
    """
    boundary = system_prompt.find(_STABLE_PREFIX_BOUNDARY)
    if boundary <= 0:
        return [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": _EPHEMERAL_CACHE,
            }
        ]
    return [
        {
            "type": "text",
            "text": system_prompt[:boundary],
            "cache_control": _EPHEMERAL_CACHE,
        },
        # No marker: this is the half that changes.
        {"type": "text", "text": system_prompt[boundary:]},
    ]


def get_anthropic_client() -> anthropic.Anthropic:
    settings = get_settings()
    return _client_for_key(settings.anthropic_api_key)


class NonStreamingMaxTokensError(ValueError):
    """A call site asked for more ``max_tokens`` than non-streaming allows.

    Raised by ``_acreate``'s pre-flight check BEFORE any HTTP request is made,
    which is the one property that distinguishes it from every other exception
    in this module: nothing was sent and nothing was billed. That is why the
    failure paths in ``generate_agent_response`` and ``generate_with_tools``
    write a ``llm_call_logs`` row for everything EXCEPT this — a row is a
    rate-limiter slot after the next restart, and booking one for a request that
    never existed is the same error, pointed the other way.

    "No call completed" is deliberately NOT the test used there: a request that
    went out and then hit the 300 s read timeout also has no ``usage`` to
    record, and it was still paid for.

    A ``ValueError`` subclass so nothing catching ValueError has to change —
    including tests/unit/test_llm_nonstreaming_ceiling.py, which asserts the
    message names the constant.
    """


# How many real API requests may be in flight at once, per event loop (the agent
# process runs exactly one, so there it is per process).
#
# 8 = one full specialist panel. That is the largest fan-out a single turn can
# ask for (a concluding hub turn gathers up to 8 ``consult_specialist`` calls),
# so a panel is never re-serialized — that gather is what recovered 2,344 s,
# 21.2% of run 8b64a0e0 — while four concurrent reply-lane turns queue against
# the API rather than opening 32 sockets at once.
#
# There has to be a number here at all because the dedicated pool below removes
# one. ``asyncio.to_thread`` submits to the loop's DEFAULT executor, which is
# ``min(32, cpu_count + 4)`` = **6** threads on this 2-vCPU host (measured in the
# deployed container: ``cpu_count 2``, ``default max_workers 6``). That 6 was an
# accident, but a load-bearing one: nothing in this module paces requests and the
# SDK gives up after two 429s. Sizing "from the intended fan-out" instead
# computes to ``reply_lane_max_in_flight=4`` x 8 specialists = 32 concurrent
# 300-second requests, which is not a throttle at all.
_API_MAX_CONCURRENCY = 8

# The pool the requests actually run on, and why it is not the default one:
# ``src/agent/slack_client.py`` routes EVERY Slack call through
# ``asyncio.to_thread`` as well, so 8 gathered consults at this module's 300 s
# read timeout could starve the Slack pollers, the persist flush and the roster
# sync out of the same 6 threads.
#
# 12 = the 8 above plus slack for orphans. A cancelled ``_acreate`` (shutdown, a
# ``wait_for``) releases its semaphore slot the moment the await unwinds, but the
# thread it left behind stays blocked inside the HTTP request until the read
# timeout fires — up to 300 s. Without that margin a few of those would make the
# POOL the binding constraint again, which is the thing this constant exists to
# stop being decided by accident.
_API_EXECUTOR_MAX_WORKERS = 12

# Named, so a thread dump (or a test) can tell an API request apart from the
# default pool's Slack/DB work at a glance.
_API_THREAD_NAME_PREFIX = "llm-api"

_api_executor: ThreadPoolExecutor | None = None


def _get_api_executor() -> ThreadPoolExecutor:
    """The API thread pool, created on first use.

    LAZILY, because the web tier imports this module too (``synthesize_profile``,
    ``email_inbound.classify_reply``) and most of those processes never make an
    LLM call — 12 idle threads per uvicorn worker would be a cost for nothing.

    Never shut down, deliberately, and that CHANGES SHUTDOWN ORDERING: the
    default executor is drained by ``loop.shutdown_default_executor()`` inside
    ``asyncio.run``, whereas a pool of our own is joined by
    ``concurrent.futures``' atexit hook at INTERPRETER exit. So ``asyncio.run``
    can now return — and ``src/agent/main.py``'s finally-block flush can run —
    while a request is still in flight here, with the process blocking at exit
    instead. That is the right order for the durable flush (the DB, not Slack, is
    the store of record), at the price of a stuck request holding the process
    open for up to CLIENT_READ_TIMEOUT_SECONDS after the run "finished".
    """
    global _api_executor
    if _api_executor is None:
        _api_executor = ThreadPoolExecutor(
            max_workers=_API_EXECUTOR_MAX_WORKERS,
            thread_name_prefix=_API_THREAD_NAME_PREFIX,
        )
    return _api_executor


# One semaphore per LOOP, not one per process. ``asyncio.Semaphore`` binds itself
# to the first running loop that touches it and raises RuntimeError ("is bound to
# a different event loop") on every other one — so a module-level singleton would
# work in the agent process, which has exactly one loop, and fail from the second
# test onwards in a suite that gives each test its own.
#
# Weak keys, but only half the leak they look like they close: an UNCONTENDED
# semaphore never touches the loop, so its entry dies with the loop as intended,
# while a semaphore that has actually contended stores `_loop` internally and
# therefore keeps its own key alive. That is bounded at one entry per loop that
# ever queued a call — one, in the agent process — and is the price of the
# per-loop binding above.
_api_semaphores: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _api_semaphore() -> asyncio.Semaphore:
    """The running loop's API-concurrency semaphore, created on first use."""
    loop = asyncio.get_running_loop()
    semaphore = _api_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_API_MAX_CONCURRENCY)
        _api_semaphores[loop] = semaphore
    return semaphore


async def _acreate(client: anthropic.Anthropic, **kwargs: Any):
    """``client.messages.create`` awaited OFF the event-loop thread.

    ``anthropic.Anthropic`` is the synchronous client, so calling it directly
    from an ``async def`` pins the loop for the entire HTTP request. Every
    caller in this module is async and several are gathered concurrently, so
    that turned `asyncio.gather` into a sequential queue AND froze everything
    else in the process for its duration — the Slack pollers, the DB persist
    flush, the roster sync, and the asyncio SIGTERM handler (so `docker stop`
    escalated to SIGKILL and lost the shutdown flush).

    Running it on a thread keeps the sync client (no SDK swap, no behaviour
    change per call) while giving the loop back. See
    tests/unit/test_llm_event_loop.py.

    On a pool of OUR OWN rather than ``asyncio.to_thread``'s default one, and
    under a semaphore: see _API_MAX_CONCURRENCY and _API_EXECUTOR_MAX_WORKERS for
    both numbers and what the default pool's 6 threads were silently doing to a
    gathered specialist panel — and to every Slack call in the process, which
    reaches its own thread through the same default pool.

    Also the single place ``thinking`` is defaulted, deliberately. On Opus 5 and
    Sonnet 5, OMITTING ``thinking`` runs ADAPTIVE thinking; on the 4.6 pair this
    module was written against, omitting it meant thinking-off. Since
    ``max_tokens`` caps thinking + text TOGETHER, inheriting that default would
    have turned truncation from occasional into routine — a 19-minute production
    run at the old budgets already truncated 11 times with thinking off.
    Defaulting here rather than at each call site means a NEW call site cannot
    silently acquire thinking-on by forgetting the parameter; a site that wants
    it (``generate_with_tools``) passes it explicitly and overrides this.

    Also the one choke point where ``max_tokens`` is checked against
    ``NONSTREAMING_MAX_TOKENS``, because every non-streaming request this module
    makes passes through here. A call site above the limit cannot succeed — the
    SDK raises before sending anything — so raising here, by name, is the
    difference between "max_tokens=32000 exceeds ..." and an SDK ValueError
    about streaming surfacing from somewhere deep inside an agent turn, where
    the broad handler in ``_reply_to_thread`` swallows it and posts nothing.

    And the one place the system prompt's cache breakpoint is applied, for the
    same reason: every non-streaming request in this module comes through here,
    including the truncation retries — which re-send the identical prompt and so
    are pure cache reads. Callers keep passing a plain string; ``_cacheable_system``
    turns it into blocks. An empty (or whitespace-only) prompt is passed through
    untouched, because the API rejects an empty text block.
    """
    kwargs.setdefault("thinking", {"type": "disabled"})
    system = kwargs.get("system")
    if isinstance(system, str) and system.strip():
        kwargs["system"] = _cacheable_system(system)
    requested = kwargs.get("max_tokens")
    if requested is not None and requested > NONSTREAMING_MAX_TOKENS:
        raise NonStreamingMaxTokensError(
            f"max_tokens={requested} exceeds NONSTREAMING_MAX_TOKENS="
            f"{NONSTREAMING_MAX_TOKENS}: the Anthropic SDK refuses any "
            "non-streaming request whose max_tokens implies more than 10 "
            "minutes of generation, so this call could never have succeeded. "
            "Lower the call site's max_tokens, or move that call to the "
            "streaming API."
        )
    # Everything ``asyncio.to_thread`` does except choosing the pool: the
    # ``contextvars.Context`` copy is the part a bare ``run_in_executor`` would
    # drop, and dropping it would silently unbind anything context-local (the
    # agent/phase bindings a logging filter or a tracer reads) inside the thread.
    #
    # The semaphore is acquired around the whole request, so the slot is held for
    # the call's real duration rather than for the submission.
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    async with _api_semaphore():
        return await loop.run_in_executor(
            _get_api_executor(),
            partial(ctx.run, client.messages.create, **kwargs),
        )


# The one tool a round may run in parallel with its siblings, and the only one.
# See `_execute_tool_blocks` for why each of the others is excluded by name.
CONCURRENT_TOOL_NAME = "consult_specialist"


async def _execute_tool_blocks(
    tool_use_blocks: list[Any], tool_executor: Any
) -> list[dict[str, Any]]:
    """Run one round's tool blocks and return their results IN BLOCK ORDER.

    ``consult_specialist`` blocks are gathered; every other tool stays as serial
    as it has always been, including relative to the consults. Run 8b64a0e0:
    81% of tool rounds carried two or more blocks and this was a plain ``for``
    loop, so the hub waited 25-40 s per consult in series for calls the API had
    already decided to make in parallel — **2,344 s, 21.2% of the run**, the
    largest single perf item in the audit.

    The narrowness is the design. Each excluded tool breaks under concurrency for
    its own reason:

    - ``ThreadState.abstracts_other`` / ``full_text`` are check-then-increment
      (``tools.py:278`` / ``:292``): two concurrent fetches read the same count
      and both pass a per-thread cap of one.
    - ``agent.record_api_call`` mutates a deque per call. It is safe today only
      because it is synchronous — under ``asyncio`` that makes it atomic — and an
      ``await`` added inside it later would turn this into a silent race.
    - ``_record_specialist_consult`` must land before ``_post_panel_note``; that
      ordering holds inside one consult's own coroutine and nowhere else.
    - ``search_prior_art`` already loses 10 of 125 searches to self-inflicted
      429s, every one on the 3rd POST of its own un-paced tier ladder.
      Parallelism multiplies exactly that.

    Consults are gathered FIRST and the serial blocks run after, rather than
    being interleaved in block order. They are the slow ones (25-40 s against
    sub-second for the rest), so front-loading them costs nothing and keeps the
    partition legible. Nothing depends on the original execution order: the model
    chose every block's input in the same round, before any of them ran, and all
    the results go back in one message.

    Ordering that DOES matter is preserved exactly: the returned list is
    positional, so ``result[i]`` pairs with ``tool_use_blocks[i]``, and each
    result carries its own block's ``tool_use_id``. A mismatched or missing pair
    is a 400 from the API that kills the whole turn.

    An exception from ``tool_executor`` still propagates, as it did serially — in
    practice it cannot, because ``execute_tool`` catches everything and returns an
    error string, but a gather must not be the thing that turns a raise into a
    silently short result list.
    """

    def _result(block: Any, text: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": text,
        }

    # Keyed by block index rather than a pre-sized list with None holes, so a
    # block that somehow went unexecuted raises KeyError on the way out instead
    # of silently shortening the result list — which the API answers with a 400
    # about a missing tool_result and no hint as to which block it lost.
    results: dict[int, dict[str, Any]] = {}

    concurrent = [
        i for i, b in enumerate(tool_use_blocks)
        if getattr(b, "name", None) == CONCURRENT_TOOL_NAME
    ]
    # Only worth a gather at 2+: one block gathered is one block awaited, and
    # taking the serial path there keeps the common round byte-identical to
    # what it was before this change.
    if len(concurrent) > 1:
        outputs = await asyncio.gather(*(
            tool_executor(tool_use_blocks[i].name, tool_use_blocks[i].input)
            for i in concurrent
        ))
        for i, output in zip(concurrent, outputs, strict=True):
            results[i] = _result(tool_use_blocks[i], output)

    for i, block in enumerate(tool_use_blocks):
        if i in results:
            continue
        results[i] = _result(block, await tool_executor(block.name, block.input))

    return [results[i] for i in range(len(tool_use_blocks))]


def _retry_budget(
    max_tokens: int, *, model: str, log_meta: dict[str, str | None] | None
) -> int:
    """The ``max_tokens`` for a truncation retry: 2x, but never above the SDK cap.

    All three retry sites in this module used a bare ``max_tokens * 2``, which is
    fine while the doubling stays under ``NONSTREAMING_MAX_TOKENS`` and fatal the
    moment it does not: the SDK raises ValueError instead of retrying, the first
    (truncated) reply has already been discarded, and the exception unwinds
    through the caller's broad handler — so a thread_reply at the 16000 ceiling
    lost the entire turn, verdict sidecar included, on exactly the concluding
    turns that carry one.

    Clamping is logged at WARNING because the retry did not get what it asked
    for: at the cap it is not a bigger budget at all. It is still worth making —
    the retry passes no ``tools`` and takes ``_acreate``'s thinking-disabled
    default, so the same ceiling buys strictly more *text* than the adaptive
    thinking call that truncated — but an operator reading "retrying with
    max_tokens=21333" after a 16000 truncation should not have to work out why
    the number is not 32000.
    """
    doubled = max_tokens * 2
    if doubled <= NONSTREAMING_MAX_TOKENS:
        return doubled
    logger.warning(
        "Truncation retry clamped to max_tokens=%d (2x %d = %d is above the "
        "SDK's non-streaming limit and would raise instead of retrying) "
        "(model=%s agent=%s phase=%s) — the retry gets no extra budget, only "
        "the room freed by dropping tools and thinking.",
        NONSTREAMING_MAX_TOKENS, max_tokens, doubled, model,
        (log_meta or {}).get("agent_id", "?"), (log_meta or {}).get("phase", "?"),
    )
    return NONSTREAMING_MAX_TOKENS


def _thinking_tokens(usage: Any) -> int | None:
    """``usage.output_tokens_details.thinking_tokens``, or None if unreported.

    Read through two ``getattr``s on purpose. The field is new (anthropic
    0.120's ``OutputTokensDetails``) and ``Usage.output_tokens_details`` is
    ``Optional``, so it is absent both on an older SDK and on any reply the API
    chose not to decompose; the test fakes also leave it off unless a test is
    specifically about thinking. A hard attribute access here would turn a
    missing *observability* field into a raised exception on a real agent turn.

    Why it is worth recording at all: ``max_tokens`` caps thinking + text
    TOGETHER, and ``generate_with_tools`` is the one call site running adaptive
    thinking, so a truncated reply there is usually not a long answer — it is a
    long *deliberation* that left no room for the answer. Without this number
    ``output_tokens`` alone cannot tell those two apart, which is exactly the
    ambiguity that made the thread_reply ceiling a guess.
    """
    details = getattr(usage, "output_tokens_details", None)
    return getattr(details, "thinking_tokens", None)


def _block_types(message: Any) -> list[str | None]:
    """The ``type`` of every content block in a reply, in order.

    The cheapest possible answer to the question run 8b64a0e0 could not answer:
    two turns billed several hundred non-reasoning output tokens and returned no
    text at all, with ``stop_reason='end_turn'``. Summarized thinking and every
    accounting explanation were ruled out (36 calibration rows, 8 negative
    controls); the only surviving hypothesis is a block type ``_all_text``
    ignores — ``redacted_thinking`` — and it stays INFERRED because zero such
    blocks appear in the 5,543 stored ``llm_call_logs`` rows. Nothing had ever
    recorded the types. Now everything does, so the next occurrence names itself.

    Read defensively twice over, because both consumers are logging paths: a
    block without ``.type`` contributes null rather than raising, and a
    ``content`` that is absent or not iterable at all (a stub, a future SDK
    rename) yields an empty list.
    """
    try:
        return [
            getattr(block, "type", None)
            for block in (getattr(message, "content", None) or [])
        ]
    except TypeError:
        return []


def is_truncated_stop(stop_reason: str | None) -> bool:
    """True when the model stopped before finishing its answer.

    Both values mean "the text in hand is incomplete": ``refusal`` is the
    classifier cutting the generation, ``max_tokens`` is the ceiling doing it.
    A ``refusal``-only test misses every turn that truncated, retried, and
    truncated again — and misses a fallthrough from the retry path, which
    reports ``max_tokens`` even when the first pass was refused.

    Public and defined ONCE, because the call sites that act on this are
    scattered (a specialist opinion published as ``⚠️ caution``, a
    working-memory write, a hub reply posted to Slack) and they were disagreeing
    about which reasons count — which is how run 8b64a0e0 posted 4 truncated
    replies as complete while a refusal-truncated memory overwrote a complete
    one. ``_notify_stop_reason`` reports "" for a reply carrying no stop_reason,
    so the None case is spelled out here rather than left to a caller.
    """
    return stop_reason in {"refusal", "max_tokens"}


def _notify_stop_reason(
    callback: Callable[[str], None] | None, message: Any
) -> None:
    """Hand the FINAL call's ``stop_reason`` to the call site, exactly once.

    ``stop_reason`` is compared against ``"max_tokens"`` at nine sites in this
    module and branched on NOWHERE else in ``src/``, so every other terminal
    reason — ``refusal`` above all — reached callers as an ordinary string. Run
    8b64a0e0 paid for that three ways: 4 truncated hub replies were posted to
    Slack as if complete, a refusal-truncated working-memory write replaced a
    complete 1,977-char memory with a 1,437-char one (twice — a
    ``profile_revisions`` row and the file on disk), and 3 truncated specialist
    opinions were published as ``⚠️ caution`` and fed back into the next prompt.

    This layer only REPORTS. Whether a partial answer is worth posting, worth
    persisting, or worth crediting to a panel is a call-site decision and they
    differ — so the text is still returned unchanged either way.

    Never raises into the caller: an observability hook that can kill a turn is
    worse than the silence it replaces. Reports the empty string rather than None
    when the reply carries no ``stop_reason``, so a call site comparing against
    ``"refusal"`` has a ``str`` in hand as the interface promises.
    """
    if callback is None:
        return
    try:
        callback(getattr(message, "stop_reason", None) or "")
    except Exception:  # noqa: BLE001 — a reporting hook must not cost the reply
        logger.exception(
            "on_stop_reason callback raised; the reply is unaffected"
        )


def _sum_reported(call_stats: list[dict[str, Any]], key: str) -> int | None:
    """A turn's total for one OPTIONAL per-call number, or None if none reported.

    None rather than 0, because the columns these feed are nullable for a
    reason: a turn whose SDK never reported ``cache_read_input_tokens`` and a
    turn that genuinely read nothing from the cache are different facts, and only
    the second one belongs in an average. Entries that reported the field are
    summed even when their siblings did not — a partial answer is still the best
    available one.
    """
    reported = [c.get(key) for c in call_stats if c.get(key) is not None]
    return sum(reported) if reported else None


def _emit_call_log(
    *,
    system_prompt: str,
    messages: list[Any],
    response_text: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    call_stats: list[dict[str, Any]],
    log_meta: dict[str, Any] | None,
    wall_ms: float | None = None,
) -> None:
    """Write one ``llm_call_logs`` row for one TURN, if anyone is listening.

    Extracted so the four return paths in this module cannot drift apart — which
    is exactly what happened: ``generate_agent_response``'s empty-content branch
    returned "" ABOVE its callback, so a reply with no content blocks wrote no
    row at all. 26 billed, refused calls vanished that way in run 8b64a0e0,
    reconciling three separate ways (838 − 812 = 558 − 532 = 26), while
    ``tools.py``'s ``on_api_call`` had already fired for every one of them.

    That mattered more than the missing observability: ``SimulationEngine``
    rebuilds ``api_call_count`` as a row COUNT and the rate limiter's
    ``call_times`` ledger as one entry per row, so a refusal booked a throttle
    slot in-process and then disappeared at restart. Logging these rows moves
    both counters TOWARDS the truth — and it does mean a restart now replays
    refusals into ``call_times`` that it previously ignored, which is the correct
    direction (they were real, billed calls) but is a real change in post-restart
    allowance.

    The ``_call_log_callback and log_meta`` gate is unchanged, so an
    uninstrumented call site still writes nothing.

    The callback itself cannot kill the turn: it is ``SimulationEngine
    ._on_llm_call``, which appends to a buffer and may spawn a flush task, and
    it used to be invoked bare — from four call sites of which two have no
    ``try`` at all and two sit under a handler that re-raises. So a failing log
    sink would have destroyed a finished, fully-billed reply on its way out the
    door. Same principle as ``_notify_stop_reason`` and ``_log_empty_reply``: an
    observability hook must not cost the reply it is describing.
    """
    callback = _call_log_callback
    if not (callback and log_meta):
        return
    payload = {
        "system_prompt": system_prompt,
        "messages": messages,
        "response_text": response_text,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        # The TURN's wall time, which `latency_ms` deliberately is not — that one
        # carries only the LAST API call's latency (532 of 532 rows on run
        # 8b64a0e0), so summing the column understated real LLM wait by 25%.
        # Measured around the whole call, so unlike a sum over `call_stats` it
        # also includes the tool execution between rounds, which is what actually
        # makes a hub turn slow.
        "wall_ms": wall_ms,
        # The cached input this turn read and wrote, per-turn cumulative like
        # `input_tokens` beside it — but SUMMED FROM `call_stats` rather than
        # accumulated at each of the six sites that add up the other totals.
        # Derived in one place, they cannot drift from the entries they are
        # supposed to total, and the failure path in `generate_with_tools` gets
        # them right for free. None (not 0) when no call reported either field:
        # "the SDK told us nothing" and "the cache was read zero times" are
        # different answers, and the columns are nullable for that reason.
        "cache_read_input_tokens": _sum_reported(
            call_stats, "cache_read_input_tokens"
        ),
        "cache_creation_input_tokens": _sum_reported(
            call_stats, "cache_creation_input_tokens"
        ),
        "call_stats": call_stats,
        "completed_at": datetime.now(timezone.utc),
        **log_meta,
    }
    try:
        callback(payload)
    except Exception:  # noqa: BLE001 — a logging hook must not cost the reply
        logger.exception(
            "LLM call log callback raised; the reply is unaffected, but this "
            "turn's row is lost (agent=%s phase=%s)",
            log_meta.get("agent_id", "?"), log_meta.get("phase", "?"),
        )


def _call_stat(
    *, seq: int, kind: str, max_tokens: int, message: Any, latency_ms: float
) -> dict[str, Any]:
    """One entry of ``llm_call_logs.call_stats`` — the record of ONE real API call.

    ``kind`` is one of:
      ``round``        - a tool-use round (the reply carried tool_use blocks)
      ``final``        - the terminating call whose reply carried no tool_use
      ``forced_final`` - the no-tools call made after the tool loop ended
      ``retry``        - a max_tokens retry, always the entry after the one it retries

    ``max_tokens`` is the ceiling actually REQUESTED for this call, which is the
    other half of a truncation report: ``stop_reason='max_tokens'`` says the
    model wanted more, and this says how much it was allowed. Every field is
    read defensively so a stub or a future SDK that drops one degrades to null
    rather than raising inside a logging path.
    """
    usage = getattr(message, "usage", None)
    return {
        "seq": seq,
        "kind": kind,
        "max_tokens": max_tokens,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "thinking_tokens": _thinking_tokens(usage),
        # The CACHED halves of the input, which ``input_tokens`` above EXCLUDES:
        # a cache hit MOVES tokens out of ``input_tokens``, it does not add to
        # them. Unread by anything in this repo until 2026-08-22, so the
        # prompt-caching change silently gutted every input-token number the
        # system records — 184 of the 228 rows on the one run that used caching
        # report fewer input tokens than the system prompt alone can be, one
        # ``call_stats`` entry recording 2 input tokens for a 30 KB prompt.
        #
        # Kept as their own numbers rather than folded into ``input_tokens``,
        # for the reason src/models/agent_activity.py gives for adding ``wall_ms``
        # instead of redefining ``latency_ms``: a column that means "uncached
        # input" before a date and "all input" after it cannot be summed across
        # the change. They are also billed at three different rates. Whatever
        # wants the total can add the three.
        #
        # Read through ``getattr`` like ``_thinking_tokens``: both fields are
        # ``Optional[int]`` and are None on any reply that used no cache
        # (verified on the deployed anthropic 1.0.0), and an older SDK does not
        # carry them at all — so a bare arithmetic use of either is a TypeError
        # raised inside a billed turn.
        #
        # ``usage.cache_creation`` is deliberately NOT read: it is the same
        # ``cache_creation_input_tokens`` broken out per TTL bucket
        # (``ephemeral_5m_input_tokens`` + ``ephemeral_1h_input_tokens``), so
        # recording it alongside would count every cache write twice. There is
        # nothing to "complete" here later.
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        "cache_creation_input_tokens": getattr(
            usage, "cache_creation_input_tokens", None
        ),
        "stop_reason": getattr(message, "stop_reason", None),
        # What the reply was actually MADE of. `stop_reason` says why generation
        # ended; this says what came back, which is the only way to tell a
        # refusal apart from a reply whose every block is of a type `_all_text`
        # skips. See `_block_types`.
        "block_types": _block_types(message),
        "latency_ms": round(latency_ms, 1),
    }


def _all_text(message: Any) -> str:
    """Every ``text`` block's text, joined with a newline — not just the first.

    Supersedes the block-0-only helper this replaced. That was safe while
    a reply carried at most one text block, but a thinking-enabled turn can
    interleave several, and the hub's concluding reply emits its
    ``<assessment_json>`` sidecar LAST — so returning the first block dropped the
    verdict while leaving the visible half of the reply intact.

    Returns "" when there is no text block at all (a refusal, or a
    thinking-only reply). Callers treat "" as "no answer".
    """
    parts = [
        block.text
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    return "\n".join(parts)


def _log_empty_reply(
    message: Any, *, model: str, log_meta: dict[str, Any] | None, where: str
) -> None:
    """Say loudly that a call is about to return no text, and why.

    Only this layer can see ``stop_reason``: callers receive a plain string, so
    an empty reply reaches the engine indistinguishable from a model that
    genuinely said nothing. A FIRST-pass ``max_tokens`` truncation is handled
    by the retry path and not routed here — but the retry's own empty outcome
    IS (the ``*_retry`` sites), as is every other terminal stop reason:
    ``refusal``, a thinking-only reply, an unrecognised future value.

    Never raises: this runs on a failure path, and a logging error must not
    replace the failure it is describing.
    """
    try:
        meta = log_meta or {}
        logger.error(
            "Empty reply from the model (stop_reason=%s block_types=%s model=%s "
            "agent=%s phase=%s site=%s) — the caller will treat this as no "
            "answer, so the turn is skipped and any verdict it carried is lost.",
            getattr(message, "stop_reason", "?"),
            # The same list `_call_stat` records, in the line an operator reads
            # first. `[]` is a refusal; `['thinking', ...]` with no 'text' is the
            # unresolved end_turn shape and should be reported.
            _block_types(message),
            model,
            meta.get("agent_id", "?"),
            meta.get("phase", "?"),
            where,
        )
    except Exception:  # noqa: BLE001 — never let logging mask the failure
        logger.error("Empty reply from the model; stop_reason unavailable")


async def synthesize_profile(context_text: str, researcher_name: str) -> dict[str, Any]:
    """
    Call Claude Opus to synthesize a researcher profile from assembled context.
    Returns structured profile dict.
    """
    settings = get_settings()
    prompt_path = "prompts/profile-synthesis.md"
    try:
        with open(prompt_path) as f:
            system_prompt = f.read()
    except FileNotFoundError:
        system_prompt = _default_synthesis_prompt()

    user_message = f"""Please synthesize a researcher profile for {researcher_name} from the following information:

{context_text}

Return your response as valid JSON matching the specified schema."""

    client = get_anthropic_client()
    try:
        message = await _acreate(
            client,
            model=settings.llm_profile_model,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        response_text = _all_text(message)

        try:
            return _extract_json(response_text)
        except ValueError:
            logger.error(
                "Profile synthesis response for %s could not be parsed; full text:\n%s",
                researcher_name, response_text,
            )
            raise
    except Exception as exc:
        logger.error("Failed to synthesize profile for %s: %s", researcher_name, exc)
        raise


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON object from LLM response text."""
    # Try direct parse first
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # Look for JSON code block
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            block = text[start:end].strip()
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
            # Claude sometimes drops the opening brace inside the fence — try
            # wrapping when the block looks like the body of an object.
            if block.startswith('"') and ":" in block:
                try:
                    return json.loads("{" + block.rstrip(", \n") + "}")
                except json.JSONDecodeError:
                    pass

    # Look for any JSON block
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass

    # Try to find { ... } block
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from LLM response: {text[:200]}")


async def generate_agent_response(
    system_prompt: str,
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 1000,
    log_meta: dict[str, str | None] | None = None,
    on_retry: Callable[[], None] | None = None,
    on_stop_reason: Callable[[str], None] | None = None,
) -> str:
    """Generate an agent response via Claude.

    ``on_retry``, if given, fires once — synchronously, before this returns —
    exactly when the max_tokens retry below actually makes a second API call.
    A caller that books one call against a rate limiter or budget for this
    whole turn (e.g. ``Agent.record_api_call``) should pass that callable here
    so a retried turn is booked as the two real API calls it made, not one.
    Optional and additive: omitting it changes nothing about behavior or the
    return contract.

    ``on_stop_reason``, if given, fires exactly once — synchronously, before
    this returns — with the FINAL API call's ``stop_reason`` (so the retry's,
    when a retry ran). It never raises into this function and never changes what
    is returned: a partial answer still comes back, because whether a partial
    answer may be posted / persisted / credited differs per call site. See
    ``_notify_stop_reason``.
    """
    # Start of the TURN, for `wall_ms`. Distinct from the per-call `t0`
    # below: this one spans every round, every retry and the tool execution
    # between them, which is the number `latency_ms` has never carried.
    _turn_t0 = time.monotonic()
    settings = get_settings()
    model = model or settings.llm_agent_model
    client = get_anthropic_client()
    # Per-turn billing totals (cumulative across the retry below), plus the
    # per-CALL breakdown. The two are deliberately different questions: the
    # totals answer "what did this turn cost", call_stats answers "which call
    # truncated and how much was it allowed" — and one row can only answer the
    # second by carrying a list.
    #
    # Bound BEFORE the `try`, with `response_text` and `latency_ms`, because the
    # failure path at the bottom reports them: a retry that raises used to take
    # the record of both billed calls with it.
    total_input_tokens = 0
    total_output_tokens = 0
    call_stats: list[dict[str, Any]] = []
    latency_ms = 0.0
    response_text = ""
    try:
        t0 = time.monotonic()
        message = await _acreate(
            client,
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        total_input_tokens += message.usage.input_tokens
        total_output_tokens += message.usage.output_tokens
        call_stats.append(
            _call_stat(
                seq=1, kind="final", max_tokens=max_tokens,
                message=message, latency_ms=latency_ms,
            )
        )
        if not message.content:
            agent_id = (log_meta or {}).get("agent_id", "?")
            phase = (log_meta or {}).get("phase", "?")
            sys_chars = len(system_prompt)
            user_chars = sum(len(m.get("content", "")) for m in messages)
            user_tail = (messages[-1].get("content", "")[-400:] if messages else "")
            # ONE error line, deliberately. This branch keeps its own early
            # return rather than falling through to `_log_empty_reply`, so a
            # single billed call still produces a single ERROR — and this is the
            # only place `user_tail` exists, which is what names the prompt that
            # caused it.
            logger.error(
                "Claude returned empty content (model=%s agent=%s phase=%s "
                "stop=%r sys_chars=%d user_chars=%d in_tok=%d out_tok=%d) "
                "user_tail=%r",
                model, agent_id, phase, getattr(message, "stop_reason", None),
                sys_chars, user_chars,
                message.usage.input_tokens, message.usage.output_tokens,
                user_tail,
            )
            # The row, BEFORE the return. This used to be the one exit from this
            # module that wrote nothing at all (C5) — see `_emit_call_log`.
            _emit_call_log(
                system_prompt=system_prompt,
                messages=messages,
                response_text="",
                model=model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                latency_ms=latency_ms,
                call_stats=call_stats,
                log_meta=log_meta,
                wall_ms=(time.monotonic() - _turn_t0) * 1000,
            )
            _notify_stop_reason(on_stop_reason, message)
            return ""
        response_text = _all_text(message)
        # The reply that ENDED this turn, which is what `on_stop_reason`
        # reports. Reassigned by the retry below; `message` deliberately keeps
        # pointing at the first call so the "still truncated" log line and the
        # token accumulation can both name the right one.
        final_message = message
        if not response_text.strip() and message.stop_reason != "max_tokens":
            _log_empty_reply(
                message, model=model, log_meta=log_meta, where="single_call"
            )

        # Retry once with higher max_tokens if response was truncated
        if message.stop_reason == "max_tokens":
            retry_max = _retry_budget(max_tokens, model=model, log_meta=log_meta)
            logger.warning(
                "Response truncated (stop_reason=max_tokens, %d tokens). "
                "Retrying with max_tokens=%d",
                message.usage.output_tokens, retry_max,
            )
            t0 = time.monotonic()
            retry_msg = await _acreate(
                client,
                model=model,
                max_tokens=retry_max,
                system=system_prompt,
                messages=messages,
            )
            # This is a second real, billed API call for what the caller
            # booked as one turn — fire the caller's own accounting hook (if
            # any) so a rate limiter sized to "one call per turn" isn't
            # quietly undercounting the one turn most likely to retry: the
            # phase-5 assessment, whose body runs long enough to hit
            # max_tokens before its <assessment_json> sidecar at the end.
            if on_retry is not None:
                on_retry()
            retry_latency = (time.monotonic() - t0) * 1000
            latency_ms += retry_latency
            final_message = retry_msg
            # `_all_text(retry_msg) or response_text` only defended against "".
            # A retry that comes back as "\n\n   \n" is TRUTHY, so it won the
            # `or` and replaced a truncated-but-usable first pass with
            # blankness — which the engine reads as "the model said nothing"
            # and skips, losing the turn to the very retry meant to save it.
            #
            # Tested, not stripped: `response_text` is what lands in
            # `llm_call_logs.response_text` VERBATIM and what the backfill
            # scripts regex for `<assessment_json>`, so storing the stripped
            # text here would quietly rewrite the record of the reply.
            retry_text = _all_text(retry_msg)
            response_text = retry_text if retry_text.strip() else response_text
            if not response_text.strip():
                _log_empty_reply(
                    retry_msg, model=model, log_meta=log_meta,
                    where="single_call_retry",
                )
            # ACCUMULATE, matching latency_ms above and generate_with_tools.
            # This line used to be `message = retry_msg  # use retry stats for
            # logging`, which made the logged row carry ONLY the retry's tokens:
            # the first call was real and billed even though its truncated text
            # was thrown away, so every retried turn under-reported its input
            # AND output tokens by a whole call. The per-call split now lives in
            # call_stats, so the row no longer has to choose one call's numbers.
            total_input_tokens += retry_msg.usage.input_tokens
            total_output_tokens += retry_msg.usage.output_tokens
            call_stats.append(
                _call_stat(
                    seq=2, kind="retry", max_tokens=retry_max,
                    message=retry_msg, latency_ms=retry_latency,
                )
            )

            if retry_msg.stop_reason == "max_tokens":
                # The retry doubled max_tokens and STILL truncated. The
                # retry's (still-truncated) text is returned below — it is
                # still the best available answer — but this must be loud:
                # for phase 5 the <assessment_json> verdict sidecar is
                # emitted last, so a still-truncated response silently drops
                # the machine-readable verdict while the Slack post can still
                # look complete.
                agent_id = (log_meta or {}).get("agent_id", "?")
                phase = (log_meta or {}).get("phase", "?")
                logger.error(
                    "Response still truncated after 2x max_tokens retry "
                    "(model=%s agent=%s phase=%s retry_max_tokens=%d "
                    "out_tok=%d) — returning the truncated text; anything "
                    "the model emits last (e.g. a phase-5 <assessment_json> "
                    "sidecar) may be missing from it.",
                    # retry_msg, not `message`: this used to read through the
                    # `message = retry_msg` alias, which is gone now that the
                    # token totals accumulate. The number that belongs in a
                    # "the RETRY still truncated" line is the retry's own.
                    model, agent_id, phase, retry_max, retry_msg.usage.output_tokens,
                )

        _emit_call_log(
            system_prompt=system_prompt,
            messages=messages,
            response_text=response_text,
            model=model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            latency_ms=latency_ms,
            call_stats=call_stats,
            log_meta=log_meta,
            wall_ms=(time.monotonic() - _turn_t0) * 1000,
        )
        # `final_message`, not `message`: when a retry ran it is the retry that
        # ended the turn, so a turn that truncated and then recovered must report
        # `end_turn` — otherwise every recovered turn looks truncated to the
        # caller.
        _notify_stop_reason(on_stop_reason, final_message)

        return response_text
    except Exception as exc:
        logger.error("Failed to generate agent response: %s", exc)
        # The record half of what generate_with_tools' guard does, and ONLY the
        # record half. Both calls of a retried turn are billed; without this the
        # turn wrote no row at all, and SimulationEngine rebuilds
        # `api_call_count` and the rate limiter's `call_times` from these rows —
        # so a failure here silently refunded the throttle at the next restart.
        # The row carries the first pass's truncated text too: it is what the
        # dropped-verdict backfill regexes `llm_call_logs.response_text` for, and
        # it was paid for.
        #
        # What this deliberately does NOT do is RETURN that text, unlike
        # generate_with_tools. A fallthrough here would report
        # stop_reason=max_tokens, and src/agent/tools.py's consult path still
        # tests `stop_reasons[-1] == "refusal"` — so a truncated specialist
        # opinion would be credited to the panel as a complete one, which is the
        # exact defect the stop-reason contract was introduced to stop. Deferred
        # until that call site adopts `is_truncated_stop`; do not "complete" this
        # before then.
        #
        # The one failure that writes nothing is the request that was never
        # issued — see NonStreamingMaxTokensError.
        if not isinstance(exc, NonStreamingMaxTokensError):
            _emit_call_log(
                system_prompt=system_prompt,
                messages=messages,
                response_text=response_text,
                model=model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                latency_ms=latency_ms,
                call_stats=call_stats,
                log_meta=log_meta,
                wall_ms=(time.monotonic() - _turn_t0) * 1000,
            )
        raise


async def make_decision(
    system_prompt: str,
    messages: list[dict[str, str]],
    model: str | None = None,
    log_meta: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Phase 1 agent decision call. Returns structured JSON decision.
    """
    settings = get_settings()
    model = model or settings.llm_agent_model
    response_text = await generate_agent_response(
        system_prompt=system_prompt,
        messages=messages,
        model=model,
        max_tokens=300,
        log_meta=log_meta,
    )
    return _extract_json(response_text)


async def generate_with_tools(
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_executor: Any,  # async callable(tool_name, tool_input) -> str
    model: str | None = None,
    max_tokens: int = 1000,
    max_tool_rounds: int = 5,
    log_meta: dict[str, str | None] | None = None,
    on_retry: Callable[[], None] | None = None,
    on_stop_reason: Callable[[str], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> str:
    """
    Generate a response with Anthropic tool-use API.

    Loops: call API -> if tool_use blocks, execute tools, append results,
    re-call until we get a final text response or hit max_tool_rounds.

    ``max_tool_rounds`` names the rounds, and the loop makes
    ``max_tool_rounds + 1`` tool-capable calls — the setting under-counts the
    calls by one, deliberately and permanently. See the comment on the loop
    itself for why the ``+ 1`` stays.

    Returns the final text response.

    ``on_retry``, same contract as ``generate_agent_response``'s: it fires
    once — synchronously, before this returns — exactly when one of this
    function's two internal max_tokens retries (the "final text" branch's,
    or the max-tool-rounds fallback's; at most one runs per call) actually
    makes a second API call. A caller that books one call against a rate
    limiter or budget for this whole turn (e.g. ``Agent.record_api_call``)
    should pass that callable here so a retried turn is booked as the two
    real API calls it made, not one. Optional and additive: omitting it
    changes nothing about behavior or the return contract.

    ``on_stop_reason``, same contract as ``generate_agent_response``'s: it
    fires exactly once, with the stop_reason of whichever call actually ended
    the turn — the terminating text call, the forced final call, or either
    one's retry. A tool ROUND's stop_reason is never reported here (it is in
    ``call_stats``); the question this answers is "is the answer I am holding
    complete?".

    ``should_continue``, if given, is polled before each tool round AFTER the
    first. Returning False stops the loop from opening a NEW round; it does not
    abort anything in flight, so no issued call is wasted and the turn still
    falls through to the forced final call below and returns a usable reply.

    This exists for cooperative shutdown. ``SimulationEngine.request_stop()``
    only flips a flag, and the durable flush runs in main.py's finally — which
    needs the main loop to RETURN. One thread_reply turn measured up to 134
    seconds here (5 rounds x a real API call each), so `docker stop` expired
    mid-turn and SIGKILLed the process before the flush, losing the in-flight
    turn's buffered rows. Polling the engine's own `_running` flag bounds a
    stopping turn to the round already underway plus one final call.

    Omitting it is exactly the pre-existing behaviour: the loop runs to
    max_tool_rounds as before.
    """
    # Start of the TURN, for `wall_ms`. Distinct from the per-call `t0`
    # below: this one spans every round, every retry and the tool execution
    # between them, which is the number `latency_ms` has never carried.
    _turn_t0 = time.monotonic()
    settings = get_settings()
    model = model or settings.llm_agent_model
    client = get_anthropic_client()

    # Work with a mutable copy of messages
    conversation = list(messages)
    total_input_tokens = 0
    total_output_tokens = 0
    # One entry per REAL API call, in call order. The totals above are per-turn
    # billing and stay cumulative (see the module docstring on call_stats and
    # SimulationEngine's restart rebuilds, which count rows, not calls); this
    # list is what makes a single row interpretable — 78.6% of thread_reply rows
    # are 2+ calls, so "output_tokens" on its own is a sum of unknown addends.
    call_stats: list[dict[str, Any]] = []
    seq = 0
    # The tool_result block currently carrying the message-side cache
    # breakpoint, so the next round can take the marker off it. See where it is
    # set, below.
    cached_tool_result: dict[str, Any] | None = None

    # Everything below runs under ONE guard, at the bottom of this function.
    # See it for why the class of bug it closes cannot be fixed at the retry
    # sites: the tool-round `_acreate`, `_execute_tool_blocks` and
    # `b.model_dump()` can each raise after a round has been BILLED, and every
    # one of them used to take the whole turn's record with it.
    #
    # These are read by that guard, alongside `call_stats` and the two totals
    # above, so they are bound before the `try` and kept current as the turn
    # proceeds.
    latency_ms = 0.0
    # The best answer in hand, and the reply that produced it, if the turn dies
    # from here on. `None` means "nothing salvageable — re-raise"; "" means "the
    # turn got far enough that returning nothing is the honest outcome".
    recovered_text: str | None = None
    recovered_message: Any = None

    try:
        # `+ 1` — one more tool-capable call than the setting names, and it
        # STAYS. It reads like an off-by-one and behaves like headroom nobody
        # has ever needed: across all 1,121 stored rows carrying `call_stats`,
        # the most rounds any turn used is 4 against a budget of 6, and no
        # caller anywhere passes `max_tool_rounds` at all. Removing it would
        # also delete coverage rather than add it — `max_tool_rounds=1` is used
        # as SETUP to force a two-round turn in 12 places across 6 test files
        # (the only multi-round path the suite exercises), so
        # `range(max_tool_rounds)` would turn every one of them into a
        # single-round turn that asserts nothing.
        # The documentation was wrong, not the loop; it has been corrected
        # instead (the module comment on `call_stats`, this function's
        # docstring, and the "Max tool rounds" warning below).
        for round_num in range(max_tool_rounds + 1):
            # Round 0 always runs — without it this returns nothing at all. From
            # round 1 on, a stop request ends the loop rather than opening another
            # round. `break` (not `return`) so control reaches the forced-final call
            # below and the caller still gets a reply to post.
            if round_num > 0 and should_continue is not None and not should_continue():
                logger.info(
                    "Stop requested — ending the tool loop after %d round(s) "
                    "instead of %d, so shutdown is not blocked by further calls",
                    round_num, max_tool_rounds,
                )
                break

            t0 = time.monotonic()
            message = await _acreate(
                client,
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=conversation,
                tools=tools,
                # ADAPTIVE here, unlike every other call site in this module (which
                # take _acreate's thinking-disabled default). This is the only call
                # that passes `tools`, and on Opus 5 a thinking-DISABLED turn can
                # write a tool call into its visible TEXT instead of emitting a
                # tool_use block: the turn succeeds, the call never runs, and no
                # error is raised. For the hub that would mean silently skipping
                # consult_specialist — the panel would look convened and never be.
                # Thinking shares the max_tokens budget, so this path's budget was
                # raised alongside this change (src/agent/simulation.py).
                # The truncation retry below deliberately does NOT set this: it
                # passes no `tools`, so it carries no tool-in-text risk and is
                # better off spending its whole budget on the answer.
                thinking={"type": "adaptive"},
            )
            latency_ms = (time.monotonic() - t0) * 1000
            total_input_tokens += message.usage.input_tokens
            total_output_tokens += message.usage.output_tokens

            # Check if the response contains tool use
            tool_use_blocks = [b for b in message.content if b.type == "tool_use"]

            # Recorded for EVERY round, not just the terminating one. `stop_reason`
            # used to be inspected only inside the `not tool_use_blocks` branch
            # below, so a tool-use round that hit max_tokens left no log line and no
            # DB trace whatsoever — the single blindest spot in this module, and the
            # one that made the truncation count on the last sizing exercise a guess.
            seq += 1
            call_stats.append(
                _call_stat(
                    seq=seq,
                    kind="final" if not tool_use_blocks else "round",
                    max_tokens=max_tokens,
                    message=message,
                    latency_ms=latency_ms,
                )
            )

            if not tool_use_blocks:
                # Final text response — no more tool calls
                response_text = _all_text(message)
                # The call that ended the turn; reassigned by the retry below.
                final_message = message
                if not response_text.strip() and message.stop_reason != "max_tokens":
                    _log_empty_reply(
                        message, model=model, log_meta=log_meta, where="final"
                    )

                # Retry once with higher max_tokens if response was truncated
                if message.stop_reason == "max_tokens":
                    retry_max = _retry_budget(
                        max_tokens, model=model, log_meta=log_meta
                    )
                    logger.warning(
                        "Response truncated (stop_reason=max_tokens, %d tokens). "
                        "Retrying with max_tokens=%d",
                        message.usage.output_tokens, retry_max,
                    )
                    # If the retry throws, THIS is the answer: truncated, billed,
                    # and the best one available — the retry is the call most
                    # likely to fail (it asks for up to NONSTREAMING_MAX_TOKENS,
                    # ~351 s of generation against a 300 s read timeout) and the
                    # reply it would discard is a concluding hub turn's verdict.
                    # `message`, not `final_message`: the stop_reason reported
                    # must be true of the TEXT being returned, which is
                    # `max_tokens`.
                    recovered_text, recovered_message = response_text, message
                    t0 = time.monotonic()
                    retry_msg = await _acreate(
                        client,
                        model=model,
                        max_tokens=retry_max,
                        system=system_prompt,
                        messages=conversation,
                    )
                    # Second real, billed API call for what the caller booked as
                    # one turn — fire the caller's own accounting hook (if any),
                    # same reasoning as generate_agent_response's retry (B0).
                    if on_retry is not None:
                        on_retry()
                    retry_latency = (time.monotonic() - t0) * 1000
                    latency_ms += retry_latency
                    total_input_tokens += retry_msg.usage.input_tokens
                    total_output_tokens += retry_msg.usage.output_tokens
                    seq += 1
                    call_stats.append(
                        _call_stat(
                            seq=seq, kind="retry", max_tokens=retry_max,
                            message=retry_msg, latency_ms=retry_latency,
                        )
                    )
                    final_message = retry_msg
                    # Whitespace is truthy and is not an answer — see the same
                    # three lines in generate_agent_response for the whole story.
                    retry_text = _all_text(retry_msg)
                    response_text = (
                        retry_text if retry_text.strip() else response_text
                    )
                    if not response_text.strip():
                        _log_empty_reply(
                            retry_msg, model=model, log_meta=log_meta,
                            where="final_retry",
                        )
                    if retry_msg.stop_reason == "max_tokens":
                        # Loud and specific, matching generate_agent_response: a
                        # silent still-truncated retry here drops the tail of a
                        # phase-4 reply (e.g. the closing </slack_message> tag)
                        # with no trace in the logs.
                        agent_id = (log_meta or {}).get("agent_id", "?")
                        phase = (log_meta or {}).get("phase", "?")
                        logger.error(
                            "Response still truncated after 2x max_tokens retry "
                            "(model=%s agent=%s phase=%s retry_max_tokens=%d "
                            "out_tok=%d) — returning the truncated text; anything "
                            "the model emits last (e.g. a closing tag) may be "
                            "missing from it.",
                            model, agent_id, phase, retry_max,
                            retry_msg.usage.output_tokens,
                        )

                _emit_call_log(
                    system_prompt=system_prompt,
                    messages=conversation,
                    response_text=response_text,
                    model=model,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    latency_ms=latency_ms,
                    call_stats=call_stats,
                    log_meta=log_meta,
                    wall_ms=(time.monotonic() - _turn_t0) * 1000,
                )
                _notify_stop_reason(on_stop_reason, final_message)

                return response_text

            # Append the assistant message with tool_use blocks
            conversation.append({
                "role": "assistant",
                "content": [b.model_dump() for b in message.content],
            })

            # Execute this round's tool calls — consults together, the rest serially
            # — and build one tool_result per block, in block order.
            tool_results = await _execute_tool_blocks(tool_use_blocks, tool_executor)

            # The message-side cache breakpoint, ROLLED FORWARD rather than
            # accumulated: the API allows at most 4 `cache_control` blocks per
            # request and a 5-round turn would want 6. Moving it is free — a marker
            # designates where to check for and write a cache entry, it is not part
            # of the content being matched, so dropping the previous round's does not
            # invalidate the entry it wrote. The tool outputs are the bulk of what
            # each subsequent round re-sends, so this is where the second breakpoint
            # earns its keep.
            #
            # Caveat worth knowing: a breakpoint walks back at most 20 content blocks
            # looking for a prior entry, and one round of 8 consults contributes 16
            # blocks. A round that wide can miss the previous round's entry even
            # though the marker is placed correctly.
            if tool_results:
                if cached_tool_result is not None:
                    cached_tool_result.pop("cache_control", None)
                tool_results[-1]["cache_control"] = _EPHEMERAL_CACHE
                cached_tool_result = tool_results[-1]

            conversation.append({"role": "user", "content": tool_results})

            logger.debug(
                "Tool-use round %d: %d tool calls",
                round_num + 1,
                len(tool_use_blocks),
            )

        # Exhausted max rounds — force a final call without tools.
        #
        # `max_tool_rounds + 1`, because that is how many tool-capable calls the
        # loop above actually made; naming the SETTING here under-counted them by
        # one for as long as this line has existed.
        logger.warning(
            "Max tool rounds (%d) reached, forcing final response",
            max_tool_rounds + 1,
        )
        # From here on, an exception returns "" instead of raising: the tool loop
        # is over, its rounds are billed and recorded, and there is nothing left
        # to try. Deliberately NOT a fallthrough into the accounting below —
        # `response_text` is unbound at this point and `message` still points at
        # the LAST TOOL ROUND, so falling through would add that round's tokens a
        # second time and label the entry `forced_final`, inventing an API call
        # that never happened.
        recovered_text = ""
        t0 = time.monotonic()
        message = await _acreate(
            client,
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=conversation,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        total_input_tokens += message.usage.input_tokens
        total_output_tokens += message.usage.output_tokens
        # `forced_final` rather than `final`: this call is reached either by
        # exhausting max_tool_rounds or by the cooperative-shutdown `break` above,
        # and both are worth telling apart from a turn that finished on its own —
        # the tool loop spent its budget before the answer was written.
        seq += 1
        call_stats.append(
            _call_stat(
                seq=seq, kind="forced_final", max_tokens=max_tokens,
                message=message, latency_ms=latency_ms,
            )
        )
        response_text = _all_text(message)
        # The call that ended the turn; reassigned by the retry below.
        final_message = message
        if not response_text.strip() and message.stop_reason != "max_tokens":
            _log_empty_reply(
                message, model=model, log_meta=log_meta, where="forced_final"
            )

        # Retry once with higher max_tokens if response was truncated
        if message.stop_reason == "max_tokens":
            retry_max = _retry_budget(max_tokens, model=model, log_meta=log_meta)
            logger.warning(
                "Response truncated after max rounds (stop_reason=max_tokens, %d tokens). "
                "Retrying with max_tokens=%d",
                message.usage.output_tokens, retry_max,
            )
            # Same as the other retry site: a retry that throws must not take
            # the truncated forced-final reply down with it.
            recovered_text, recovered_message = response_text, message
            t0 = time.monotonic()
            retry_msg = await _acreate(
                client,
                model=model,
                max_tokens=retry_max,
                system=system_prompt,
                messages=conversation,
            )
            # Second real, billed API call for what the caller booked as one
            # turn — same accounting hook as the other retry site above (B0).
            if on_retry is not None:
                on_retry()
            retry_latency = (time.monotonic() - t0) * 1000
            latency_ms += retry_latency
            total_input_tokens += retry_msg.usage.input_tokens
            total_output_tokens += retry_msg.usage.output_tokens
            seq += 1
            call_stats.append(
                _call_stat(
                    seq=seq, kind="retry", max_tokens=retry_max,
                    message=retry_msg, latency_ms=retry_latency,
                )
            )
            final_message = retry_msg
            # Whitespace is truthy and is not an answer — see the same three lines
            # in generate_agent_response for the whole story.
            retry_text = _all_text(retry_msg)
            response_text = retry_text if retry_text.strip() else response_text
            if not response_text.strip():
                _log_empty_reply(
                    retry_msg, model=model, log_meta=log_meta,
                    where="forced_final_retry",
                )
            if retry_msg.stop_reason == "max_tokens":
                # This retry site never re-checked stop_reason at all before this
                # fix — a still-truncated response after exhausting max_tool_rounds
                # AND doubling max_tokens passed silently. Loud and specific, same
                # as the other retry site.
                agent_id = (log_meta or {}).get("agent_id", "?")
                phase = (log_meta or {}).get("phase", "?")
                logger.error(
                    "Response still truncated after 2x max_tokens retry "
                    "(model=%s agent=%s phase=%s retry_max_tokens=%d "
                    "out_tok=%d) — returning the truncated text; anything "
                    "the model emits last (e.g. a closing tag) may be "
                    "missing from it.",
                    model, agent_id, phase, retry_max, retry_msg.usage.output_tokens,
                )

        _emit_call_log(
            system_prompt=system_prompt,
            messages=conversation,
            response_text=response_text,
            model=model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            latency_ms=latency_ms,
            call_stats=call_stats,
            log_meta=log_meta,
            wall_ms=(time.monotonic() - _turn_t0) * 1000,
        )
        _notify_stop_reason(on_stop_reason, final_message)

        return response_text
    except Exception as exc:
        # ``Exception``, so ``CancelledError`` (a BaseException since 3.8) still
        # propagates untouched: a cancelled turn is not a failed one, and the
        # cooperative-shutdown path must not be silently converted into a reply.
        #
        # ONE guard for the whole turn, not four patches at the retry sites.
        # Measured: an exception anywhere after the first call wrote
        # `rows written: 0` for a turn that had made 6 real API calls — and
        # `SimulationEngine` rebuilds `api_call_count` and the rate limiter's
        # `call_times` ledger from exactly those rows, so the calls stopped
        # existing at the next restart.
        #
        # The row is written for EVERY failure except the one request that was
        # never issued (`NonStreamingMaxTokensError`, raised by `_acreate`'s
        # pre-flight check before any I/O). Not `if call_stats`, which was the
        # first version of this line: a first-round `_acreate` that raises AFTER
        # the request went out — a 300 s APITimeoutError, the latent trigger this
        # whole guard exists for — leaves `call_stats` empty while having been
        # fully billed, and would have written nothing. An empty `call_stats` on
        # the row says exactly that: the turn is recorded, no call completed.
        if not isinstance(exc, NonStreamingMaxTokensError):
            _emit_call_log(
                system_prompt=system_prompt,
                messages=conversation,
                response_text=recovered_text or "",
                model=model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                latency_ms=latency_ms,
                call_stats=call_stats,
                log_meta=log_meta,
                wall_ms=(time.monotonic() - _turn_t0) * 1000,
            )
        if recovered_text is None:
            # Nothing to salvage, so the exception IS the outcome. Swallowing it
            # here would turn the one error this module raises BY NAME — a call
            # site above NONSTREAMING_MAX_TOKENS — into a turn that silently
            # said nothing, which is the failure mode that constant exists to
            # prevent.
            raise
        logger.exception(
            "LLM turn failed after %d billed call(s) (model=%s agent=%s "
            "phase=%s) — returning the %d character(s) already in hand rather "
            "than losing them with the exception.",
            len(call_stats), model, (log_meta or {}).get("agent_id", "?"),
            (log_meta or {}).get("phase", "?"), len(recovered_text),
        )
        # The reply that ended the turn is the one whose text we are returning —
        # so a fallthrough from a retry site reports `max_tokens`, NOT `refusal`,
        # even when the first pass was refused and the retry is what died. That
        # is why callers need `is_truncated_stop` rather than a `== "refusal"`
        # test. `None` here (the forced-final case) reports "".
        _notify_stop_reason(on_stop_reason, recovered_message)
        return recovered_text


def _default_synthesis_prompt() -> str:
    return """You are a scientific profile synthesizer. Given information about a researcher's publications, grants, and submitted texts, generate a structured JSON profile.

Output ONLY valid JSON with this schema:
{
  "research_summary": "150-250 word narrative connecting research themes",
  "techniques": ["array of specific techniques"],
  "experimental_models": ["array of model systems, organisms, cell lines, databases"],
  "disease_areas": ["array of disease areas or biological processes"],
  "key_targets": ["array of specific molecular targets, proteins, pathways"],
  "keywords": ["additional MeSH-style keywords"]
}

Guidelines:
- Research summary: 150-250 word narrative, not a list. Connect themes. Weight recent publications more heavily.
- Be specific: "CRISPR-Cas9 screening in K562 cells" not "CRISPR"
- For computational labs, include databases and computational resources as experimental models
- Extract specific molecular targets, not just pathways
- Do NOT quote or reference user-submitted text directly in any output"""
