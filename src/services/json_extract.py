"""Pull one JSON object out of a model's prose. Pure — no SDK, no DB, no engine.

Why this is its own module rather than a function on the one that had it first:
``src/services/llm.py`` has carried this algorithm since profile synthesis was
written, and ``src/agent/specialists.py`` needs the identical behaviour — but
specialists.py is deliberately dependency-free on the engine and on the
Anthropic SDK (see its module docstring), and importing ``llm`` would give it
both. A copy-paste would have been the third copy of a parser whose failure mode
is silent: it does not raise where a reader will see it, it degrades an opinion.

The measured reason it was extracted, rather than left in place with a comment:
run 8b64a0e0 laundered 6 of 168 specialist consults into
``caution``/``low``/no-concerns because ``parse_opinion`` used a bare
``json.loads``. One of the six inverted a ``blocking``/``high`` opinion that was
then published into the PI's own interview thread as "⚠️ caution". Replaying
this function verbatim over those six recovers all 3 whose ``stop_reason`` was
``end_turn`` (a COMPLETE object followed by the model's own commentary, in one
case after a closing code fence) and none of the 3 whose ``stop_reason`` was
``refusal`` (an object cut mid-array). That split is the design: everything here
finds an object that is *there*, and nothing here repairs one that is not.
See docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, H5.

Callers: ``src.agent.specialists.parse_opinion`` (which must never raise, so it
wraps) and ``src.services.llm`` (profile synthesis, which lets the ValueError
out after logging the full text). ``tests/unit/test_json_extract.py`` replays a
shared corpus through both and is the drift alarm between them.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["extract_json"]


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from LLM response text.

    Raises ``ValueError`` (including ``json.JSONDecodeError``, its subclass)
    when no object can be found. The message carries the first 200 characters of
    the input, because in ``parse_opinion``'s case the exception is swallowed and
    the message is the only trace a reader gets.

    The branches below are tried most-specific first, so a well-formed reply
    never pays for the tolerance the malformed ones need.

    The annotation says ``dict`` and the common cases deliver one, but a fenced
    ``[1, 2]`` comes back as a list: the two fence branches parse whatever is
    inside the fence. That is the behaviour this had when it was lifted out of
    ``llm.py``, kept byte-for-byte on purpose: ``parse_opinion`` tests
    ``isinstance(..., dict)`` on the result anyway, and tightening it here while
    the original copy still exists would make the drift alarm in
    ``tests/unit/test_json_extract.py`` fail for the one reason it must not —
    a difference this module introduced itself.
    """
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

    # Try to find { ... } block. This is the branch that recovers the trailing-
    # prose case, and the reason it is LAST: `rfind` takes the outermost closing
    # brace, which is right for "object then commentary" and wrong for
    # "commentary containing braces". A truncated object has no closing brace at
    # all, so `end` lands at 0 and this falls through to the raise rather than
    # guessing.
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from LLM response: {text[:200]}")
