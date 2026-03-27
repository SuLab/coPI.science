"""Anthropic Claude API wrapper."""

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
        message = client.messages.create(
            model=settings.llm_profile_model,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        response_text = message.content[0].text

        # Extract JSON from response
        return _extract_json(response_text)
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
            try:
                return json.loads(text[start:end].strip())
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
) -> str:
    """Generate an agent response via Claude."""
    settings = get_settings()
    model = model or settings.llm_agent_model
    client = get_anthropic_client()
    try:
        t0 = time.monotonic()
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        if not message.content:
            logger.warning("Claude returned empty content (model=%s)", model)
            return ""
        response_text = message.content[0].text

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
) -> str:
    """
    Generate a response with Anthropic tool-use API.

    Loops: call API -> if tool_use blocks, execute tools, append results,
    re-call until we get a final text response or hit max_tool_rounds.

    Returns the final text response.
    """
    settings = get_settings()
    model = model or settings.llm_agent_model
    client = get_anthropic_client()

    # Work with a mutable copy of messages
    conversation = list(messages)
    total_input_tokens = 0
    total_output_tokens = 0

    for round_num in range(max_tool_rounds + 1):
        t0 = time.monotonic()
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=conversation,
            tools=tools,
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
                retry_msg = client.messages.create(
                    model=model,
                    max_tokens=retry_max,
                    system=system_prompt,
                    messages=conversation,
                )
                retry_latency = (time.monotonic() - t0) * 1000
                latency_ms += retry_latency
                total_input_tokens += retry_msg.usage.input_tokens
                total_output_tokens += retry_msg.usage.output_tokens
                retry_texts = [b for b in retry_msg.content if b.type == "text"]
                if retry_texts:
                    response_text = retry_texts[0].text
                if retry_msg.stop_reason == "max_tokens":
                    logger.warning(
                        "Response still truncated after retry (%d tokens)",
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
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=conversation,
    )
    latency_ms = (time.monotonic() - t0) * 1000
    total_input_tokens += message.usage.input_tokens
    total_output_tokens += message.usage.output_tokens
    response_text = message.content[0].text if message.content else ""

    # Retry once with higher max_tokens if response was truncated
    if message.stop_reason == "max_tokens":
        retry_max = max_tokens * 2
        logger.warning(
            "Response truncated after max rounds (stop_reason=max_tokens, %d tokens). "
            "Retrying with max_tokens=%d",
            message.usage.output_tokens, retry_max,
        )
        t0 = time.monotonic()
        retry_msg = client.messages.create(
            model=model,
            max_tokens=retry_max,
            system=system_prompt,
            messages=conversation,
        )
        retry_latency = (time.monotonic() - t0) * 1000
        latency_ms += retry_latency
        total_input_tokens += retry_msg.usage.input_tokens
        total_output_tokens += retry_msg.usage.output_tokens
        retry_texts = [b for b in retry_msg.content if b.type == "text"]
        if retry_texts:
            response_text = retry_texts[0].text

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
