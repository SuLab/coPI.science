"""Anthropic Claude API wrapper."""

import asyncio
import json
import logging
import time
from typing import Any, Callable

import anthropic

from src.config import get_settings

logger = logging.getLogger(__name__)

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
NONSTREAMING_MAX_TOKENS = 21_333

# Module-level callback for logging LLM calls.
# Signature: callback(data: dict) where data contains system_prompt, messages,
# response_text, model, input_tokens, output_tokens, latency_ms, call_stats, and
# any extra keys from log_meta.
#
# ONE callback fires per TURN, not per API call — and a turn is 1..7 real API
# calls (up to max_tool_rounds tool rounds, a terminating or forced-final call,
# and at most one max_tokens retry). `input_tokens`/`output_tokens` are therefore
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
# `stop_reason`, the requested `max_tokens` ceiling, and the thinking/text split
# of `output_tokens` are recorded at all.
_call_log_callback: Callable[[dict], None] | None = None


def set_call_log_callback(callback: Callable[[dict], None] | None) -> None:
    """Register (or clear) a callback that fires after every LLM call."""
    global _call_log_callback
    _call_log_callback = callback


def get_anthropic_client() -> anthropic.Anthropic:
    settings = get_settings()
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


async def _acreate(client: anthropic.Anthropic, **kwargs: Any):
    """``client.messages.create`` awaited OFF the event-loop thread.

    ``anthropic.Anthropic`` is the synchronous client, so calling it directly
    from an ``async def`` pins the loop for the entire HTTP request. Every
    caller in this module is async and several are gathered concurrently, so
    that turned `asyncio.gather` into a sequential queue AND froze everything
    else in the process for its duration — the Slack pollers, the DB persist
    flush, the roster sync, and the asyncio SIGTERM handler (so `docker stop`
    escalated to SIGKILL and lost the shutdown flush).

    ``to_thread`` keeps the sync client (no SDK swap, no behaviour change per
    call) while giving the loop back. See tests/unit/test_llm_event_loop.py.

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
    """
    kwargs.setdefault("thinking", {"type": "disabled"})
    requested = kwargs.get("max_tokens")
    if requested is not None and requested > NONSTREAMING_MAX_TOKENS:
        raise ValueError(
            f"max_tokens={requested} exceeds NONSTREAMING_MAX_TOKENS="
            f"{NONSTREAMING_MAX_TOKENS}: the Anthropic SDK refuses any "
            "non-streaming request whose max_tokens implies more than 10 "
            "minutes of generation, so this call could never have succeeded. "
            "Lower the call site's max_tokens, or move that call to the "
            "streaming API."
        )
    return await asyncio.to_thread(client.messages.create, **kwargs)


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
        "stop_reason": getattr(message, "stop_reason", None),
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
            "Empty reply from the model (stop_reason=%s model=%s agent=%s "
            "phase=%s site=%s) — the caller will treat this as no answer, so "
            "the turn is skipped and any verdict it carried is lost.",
            getattr(message, "stop_reason", "?"),
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
) -> str:
    """Generate an agent response via Claude.

    ``on_retry``, if given, fires once — synchronously, before this returns —
    exactly when the max_tokens retry below actually makes a second API call.
    A caller that books one call against a rate limiter or budget for this
    whole turn (e.g. ``Agent.record_api_call``) should pass that callable here
    so a retried turn is booked as the two real API calls it made, not one.
    Optional and additive: omitting it changes nothing about behavior or the
    return contract.
    """
    settings = get_settings()
    model = model or settings.llm_agent_model
    client = get_anthropic_client()
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
        # Per-turn billing totals (cumulative across the retry below), plus the
        # per-CALL breakdown. The two are deliberately different questions: the
        # totals answer "what did this turn cost", call_stats answers "which
        # call truncated and how much was it allowed" — and one row can only
        # answer the second by carrying a list.
        total_input_tokens = message.usage.input_tokens
        total_output_tokens = message.usage.output_tokens
        call_stats = [
            _call_stat(
                seq=1, kind="final", max_tokens=max_tokens,
                message=message, latency_ms=latency_ms,
            )
        ]
        if not message.content:
            agent_id = (log_meta or {}).get("agent_id", "?")
            phase = (log_meta or {}).get("phase", "?")
            sys_chars = len(system_prompt)
            user_chars = sum(len(m.get("content", "")) for m in messages)
            user_tail = (messages[-1].get("content", "")[-400:] if messages else "")
            logger.error(
                "Claude returned empty content (model=%s agent=%s phase=%s "
                "stop=%r sys_chars=%d user_chars=%d in_tok=%d out_tok=%d) "
                "user_tail=%r",
                model, agent_id, phase, message.stop_reason,
                sys_chars, user_chars,
                message.usage.input_tokens, message.usage.output_tokens,
                user_tail,
            )
            return ""
        response_text = _all_text(message)
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
            response_text = _all_text(retry_msg) or response_text
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

        if _call_log_callback and log_meta:
            from datetime import datetime, timezone
            _call_log_callback({
                "system_prompt": system_prompt,
                "messages": messages,
                "response_text": response_text,
                "model": model,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "latency_ms": latency_ms,
                "call_stats": call_stats,
                "completed_at": datetime.now(timezone.utc),
                **log_meta,
            })

        return response_text
    except Exception as exc:
        logger.error("Failed to generate agent response: %s", exc)
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
    should_continue: Callable[[], bool] | None = None,
) -> str:
    """
    Generate a response with Anthropic tool-use API.

    Loops: call API -> if tool_use blocks, execute tools, append results,
    re-call until we get a final text response or hit max_tool_rounds.

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
                response_text = _all_text(retry_msg) or response_text
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

            if _call_log_callback and log_meta:
                from datetime import datetime, timezone
                _call_log_callback({
                    "system_prompt": system_prompt,
                    "messages": conversation,
                    "response_text": response_text,
                    "model": model,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "latency_ms": latency_ms,
                    "call_stats": call_stats,
                    "completed_at": datetime.now(timezone.utc),
                    **log_meta,
                })

            return response_text

        # Append the assistant message with tool_use blocks
        conversation.append({
            "role": "assistant",
            "content": [b.model_dump() for b in message.content],
        })

        # Execute each tool call and build tool_result blocks
        tool_results = []
        for tool_block in tool_use_blocks:
            result_text = await tool_executor(tool_block.name, tool_block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": result_text,
            })

        conversation.append({"role": "user", "content": tool_results})

        logger.debug(
            "Tool-use round %d: %d tool calls",
            round_num + 1,
            len(tool_use_blocks),
        )

    # Exhausted max rounds — force a final call without tools
    logger.warning("Max tool rounds (%d) reached, forcing final response", max_tool_rounds)
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
        response_text = _all_text(retry_msg) or response_text
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

    if _call_log_callback and log_meta:
        from datetime import datetime, timezone
        _call_log_callback({
            "system_prompt": system_prompt,
            "messages": conversation,
            "response_text": response_text,
            "model": model,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "latency_ms": latency_ms,
            "call_stats": call_stats,
            "completed_at": datetime.now(timezone.utc),
            **log_meta,
        })

    return response_text


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
