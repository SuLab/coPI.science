"""Anthropic Claude API wrapper."""

import asyncio
import json
import logging
import time
from typing import Any, Callable

import anthropic

from src.config import get_settings

logger = logging.getLogger(__name__)

# Module-level callback for logging LLM calls.
# Signature: callback(data: dict) where data contains system_prompt, messages,
# response_text, model, input_tokens, output_tokens, latency_ms, and any extra
# keys from log_meta.
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
    """
    kwargs.setdefault("thinking", {"type": "disabled"})
    return await asyncio.to_thread(client.messages.create, **kwargs)


def _first_text(message: Any) -> str:
    """The first ``text`` block's text, or ``""`` when the reply has none.

    Replaces ``message.content[0].text``, which assumed block 0 is always text.
    That held while thinking was off, but a thinking-enabled reply leads with a
    ``ThinkingBlock`` — which carries ``.thinking``, not ``.text`` — so indexing
    block 0 raises ``AttributeError`` instead of returning the answer. Filtering
    by type is correct under every ``thinking`` setting, so this stays right even
    if a call site later turns thinking on.

    Returning "" for a reply with no text block matches what the callers already
    do with an empty response (log it loudly, treat it as no answer) — see
    ``generate_agent_response``'s empty-content branch and
    ``src/agent/specialists.has_usable_content``.
    """
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


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
        response_text = _first_text(message)

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
    log_meta: dict[str, str] | None = None,
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
        if not message.content:
            agent_id = (log_meta or {}).get("agent_id", "?")
            phase = (log_meta or {}).get("phase", "?")
            sys_chars = len(system_prompt)
            user_chars = sum(len(m.get("content", "")) for m in messages)
            user_tail = (messages[-1].get("content", "")[-400:] if messages else "")
            logger.warning(
                "Claude returned empty content (model=%s agent=%s phase=%s "
                "stop=%r sys_chars=%d user_chars=%d in_tok=%d out_tok=%d) "
                "user_tail=%r",
                model, agent_id, phase, message.stop_reason,
                sys_chars, user_chars,
                message.usage.input_tokens, message.usage.output_tokens,
                user_tail,
            )
            return ""
        response_text = _first_text(message)

        # Retry once with higher max_tokens if response was truncated
        if message.stop_reason == "max_tokens":
            retry_max = max_tokens * 2
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
            if retry_msg.content:
                response_text = _first_text(retry_msg)
            message = retry_msg  # use retry stats for logging

            if message.stop_reason == "max_tokens":
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
                    model, agent_id, phase, retry_max, message.usage.output_tokens,
                )

        if _call_log_callback and log_meta:
            from datetime import datetime, timezone
            _call_log_callback({
                "system_prompt": system_prompt,
                "messages": messages,
                "response_text": response_text,
                "model": model,
                "input_tokens": message.usage.input_tokens,
                "output_tokens": message.usage.output_tokens,
                "latency_ms": latency_ms,
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
    log_meta: dict[str, str] | None = None,
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
        text_blocks = [b for b in message.content if b.type == "text"]

        if not tool_use_blocks:
            # Final text response — no more tool calls
            response_text = text_blocks[0].text if text_blocks else ""

            # Retry once with higher max_tokens if response was truncated
            if message.stop_reason == "max_tokens":
                retry_max = max_tokens * 2
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
                retry_texts = [b for b in retry_msg.content if b.type == "text"]
                if retry_texts:
                    response_text = retry_texts[0].text
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
    response_text = _first_text(message)

    # Retry once with higher max_tokens if response was truncated
    if message.stop_reason == "max_tokens":
        retry_max = max_tokens * 2
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
        retry_texts = [b for b in retry_msg.content if b.type == "text"]
        if retry_texts:
            response_text = retry_texts[0].text
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
