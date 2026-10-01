"""Verbatim copies of generate_agent_response and generate_with_tools as they stood
at the Phase 3 base (before LC-05 rebuilt them on _billed_call/_retry_if_truncated),
kept so tests/unit/test_llm_call_parity.py can prove the rebuilt functions send the
same requests and fire the same callbacks. Do not edit the function bodies.

The private helpers they call are bound to the live module; `get_anthropic_client`
is looked up through `llm` at call time so one monkeypatch reaches both copies.
"""
# ruff: noqa
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from src.services import llm
from src.services.llm import (
    _EPHEMERAL_CACHE,
    NonStreamingMaxTokensError,
    _call_stat,
    _emit_call_log,
    _execute_tool_blocks,
    _log_empty_reply,
    _notify_stop_reason,
    _retry_budget,
    acreate,
    all_text,
    get_settings,
)

logger = logging.getLogger("src.services.llm")


def get_anthropic_client():
    return llm.get_anthropic_client()


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
    this returns — with the ``stop_reason`` of the reply whose text is being
    returned: the retry's when a retry ran and returned, the FIRST pass's
    ``max_tokens`` when the retry raised and its text is being salvaged (see the
    handler at the bottom). It never raises into this function and never changes
    what is returned: a partial answer still comes back, because whether a
    partial answer may be posted / persisted / credited differs per call site.
    See ``_notify_stop_reason``.

    Raises only when there is nothing to hand back — a first call that failed, or
    a call refused before it was issued (``NonStreamingMaxTokensError``).
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
    # The best answer in hand, and the reply that produced it, if the turn dies
    # from here on — same two locals, same meaning, as ``generate_with_tools``.
    # ``None`` means "nothing salvageable, the exception IS the outcome".
    recovered_text: str | None = None
    recovered_message: Any = None
    try:
        t0 = time.monotonic()
        message = await acreate(
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
        response_text = all_text(message)
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
            # Truncated is not worthless: this text is a real, billed answer,
            # and if the retry dies it is the only one there will ever be.
            # `message` (not `retry_msg`) is what `on_stop_reason` should then
            # report — `max_tokens`, i.e. "incomplete" — which is what makes the
            # fallthrough safe for the specialist floor.
            recovered_text, recovered_message = response_text, message
            retry_msg = await acreate(
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
            # `all_text(retry_msg) or response_text` only defended against "".
            # A retry that comes back as "\n\n   \n" is TRUTHY, so it won the
            # `or` and replaced a truncated-but-usable first pass with
            # blankness — which the engine reads as "the model said nothing"
            # and skips, losing the turn to the very retry meant to save it.
            #
            # Tested, not stripped: `response_text` is what lands in
            # `llm_call_logs.response_text` VERBATIM and what the backfill
            # scripts regex for `<assessment_json>`, so storing the stripped
            # text here would quietly rewrite the record of the reply.
            retry_text = all_text(retry_msg)
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
        # Both halves of what generate_with_tools' guard does. THE RECORD:
        # both calls of a retried turn are billed; without this the turn wrote
        # no row at all, and SimulationEngine rebuilds `api_call_count` and the
        # rate limiter's `call_times` from these rows — per CALL, summing
        # `COALESCE(jsonb_array_length(call_stats), 1)`, so a lost row refunds
        # BOTH billed calls, not one. A failure here therefore silently refunded
        # the throttle at the next restart. The row carries
        # the first pass's truncated text too: it is what the dropped-verdict
        # backfill regexes `llm_call_logs.response_text` for, and it was paid
        # for.
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
        if recovered_text is None:
            # Nothing succeeded yet, so the exception IS the outcome — and a
            # mis-sized max_tokens, the one error this module raises by name,
            # must stay as loud as it was.
            logger.error("Failed to generate agent response: %s", exc)
            raise
        # THE TEXT: the retry died on top of a truncated-but-usable first pass.
        # This half was deferred when the record half landed, because
        # src/agent/tools.py's consult path tested `stop_reasons[-1] ==
        # "refusal"` and a fallthrough reports `max_tokens` — so a truncated
        # specialist opinion would have been credited to the panel as a complete
        # one. That call site now uses `is_truncated_stop`, which covers both, so
        # returning the text can no longer launder an unfinished opinion.
        logger.exception(
            "The max_tokens retry failed after %d billed call(s) (model=%s "
            "agent=%s phase=%s) — returning the %d character(s) the first pass "
            "already produced rather than losing them with the exception.",
            len(call_stats), model, (log_meta or {}).get("agent_id", "?"),
            (log_meta or {}).get("phase", "?"), len(recovered_text),
        )
        _notify_stop_reason(on_stop_reason, recovered_message)
        return recovered_text




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
    # billing and stay cumulative (see the comment on `_call_log_callback` for
    # why); this list is what makes a single row interpretable — 78.6% of
    # thread_reply rows are 2+ calls, so "output_tokens" on its own is a sum of
    # unknown addends.
    call_stats: list[dict[str, Any]] = []
    seq = 0
    # The tool_result block currently carrying the message-side cache
    # breakpoint, so the next round can take the marker off it. See where it is
    # set, below.
    cached_tool_result: dict[str, Any] | None = None

    # Everything below runs under ONE guard, at the bottom of this function.
    # See it for why the class of bug it closes cannot be fixed at the retry
    # sites: the tool-round `acreate`, `_execute_tool_blocks` and
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
            message = await acreate(
                client,
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=conversation,
                tools=tools,
                # ADAPTIVE here, unlike every other call site in this module (which
                # take acreate's thinking-disabled default). This is the only call
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
                response_text = all_text(message)
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
                    retry_msg = await acreate(
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
                    retry_text = all_text(retry_msg)
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
        message = await acreate(
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
        response_text = all_text(message)
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
            retry_msg = await acreate(
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
            retry_text = all_text(retry_msg)
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
        # `call_times` ledger from exactly those rows, per CALL (summing
        # `COALESCE(jsonb_array_length(call_stats), 1)`), so all six calls
        # stopped existing at the next restart, not one.
        #
        # The row is written for EVERY failure except the one request that was
        # never issued (`NonStreamingMaxTokensError`, raised by `acreate`'s
        # pre-flight check before any I/O). Not `if call_stats`, which was the
        # first version of this line: a first-round `acreate` that raises AFTER
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
