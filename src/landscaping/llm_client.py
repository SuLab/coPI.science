"""Landscaping request builders, the synchronous client and the refusal fallback chain.

One builder per stage (D32) serves the batch path, the synchronous path, fallback re-runs
and the bake-off, so a stage's request is the same wherever it is sent (spec §9.2). Model
JSON is parsed by callers, through ``src.services.json_extract.extract_json``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.config import get_settings
from src.landscaping.ledger import record_call, usage_from
from src.services import llm
from src.services.job_queue import job_error_text
from src.services.llm_pricing import PRICES

STAGES = (
    "screen", "extract", "triage", "deep", "deep_extract", "rerank", "similarity", "label", "final",
)
#: The reject audit is built with the audited stage's builder (D66) but ledgered as its own stage.
LEDGER_STAGES = STAGES + ("audit",)

# Every limit is at most llm.NONSTREAMING_MAX_TOKENS (21,333), batch bodies included, so one
# rule covers both paths (spec §9.2); the retry limit is the one a "max_tokens" stop retries with.
SCREEN_MAX_TOKENS = 4_000
SCREEN_RETRY_MAX_TOKENS = 8_000
EXTRACT_MAX_TOKENS = 12_000
EXTRACT_RETRY_MAX_TOKENS = 21_333
TRIAGE_MAX_TOKENS = 8_000
TRIAGE_RETRY_MAX_TOKENS = 16_000
DEEP_MAX_TOKENS = 16_000
DEEP_RETRY_MAX_TOKENS = 21_333
DEEP_EXTRACT_MAX_TOKENS = 16_000
DEEP_EXTRACT_RETRY_MAX_TOKENS = 21_333
RERANK_MAX_TOKENS = 8_000
RERANK_RETRY_MAX_TOKENS = 16_000
SIMILARITY_MAX_TOKENS = 6_000
SIMILARITY_RETRY_MAX_TOKENS = 12_000
LABEL_MAX_TOKENS = 4_000
LABEL_RETRY_MAX_TOKENS = 8_000
FINAL_MAX_TOKENS = 16_000
FINAL_RETRY_MAX_TOKENS = 21_333


@dataclass(frozen=True)
class StageSpec:
    """A stage's request constants; ``model_setting`` names the ``Settings`` field holding
    its model."""

    stage: str
    model_setting: str
    effort: str
    max_tokens: int
    retry_max_tokens: int


# Effort per stage (D32, §9.2). The S6 full-text re-extraction runs on the deep model at
# S6's effort; the S5 tie-break and the S7 rebuild use the rerank builder.
STAGE_SPECS: dict[str, StageSpec] = {
    spec.stage: spec
    for spec in (
        StageSpec("screen", "landscape_screen_model", "low",
                  SCREEN_MAX_TOKENS, SCREEN_RETRY_MAX_TOKENS),
        StageSpec("extract", "landscape_extract_model", "medium",
                  EXTRACT_MAX_TOKENS, EXTRACT_RETRY_MAX_TOKENS),
        StageSpec("triage", "landscape_triage_model", "medium",
                  TRIAGE_MAX_TOKENS, TRIAGE_RETRY_MAX_TOKENS),
        StageSpec("deep", "landscape_deep_model", "high",
                  DEEP_MAX_TOKENS, DEEP_RETRY_MAX_TOKENS),
        StageSpec("deep_extract", "landscape_deep_model", "high",
                  DEEP_EXTRACT_MAX_TOKENS, DEEP_EXTRACT_RETRY_MAX_TOKENS),
        StageSpec("rerank", "landscape_rerank_model", "medium",
                  RERANK_MAX_TOKENS, RERANK_RETRY_MAX_TOKENS),
        StageSpec("similarity", "landscape_similarity_model", "medium",
                  SIMILARITY_MAX_TOKENS, SIMILARITY_RETRY_MAX_TOKENS),
        StageSpec("label", "landscape_label_model", "medium",
                  LABEL_MAX_TOKENS, LABEL_RETRY_MAX_TOKENS),
        StageSpec("final", "landscape_final_model", "high",
                  FINAL_MAX_TOKENS, FINAL_RETRY_MAX_TOKENS),
    )
}


class UnpricedModelError(Exception):
    """Work was about to be sent to a model ``llm_pricing.PRICES`` does not price (§9.4)."""


def require_priced(model: str) -> None:
    """Raise ``UnpricedModelError`` unless ``model`` is in ``PRICES``: landscaping never
    sends work whose cost the ledger could not record (spec §9.4)."""
    if model not in PRICES:
        raise UnpricedModelError(f"model {model!r} is not priced in llm_pricing.PRICES")


def build_request(
    stage: str, *, model: str, system: str, user: str, schema: dict | None,
    tools: list[dict] | None = None, retry: bool = False,
) -> dict:
    """The request body for ``stage``, as a Message Batch ``params`` dict or the keyword
    arguments of a synchronous call.

    Adaptive thinking and the stage's effort always; ``schema`` becomes a JSON-schema
    ``output_config.format``. ``retry=True`` uses the stage's ``retry_max_tokens``, for the
    one re-run after a ``max_tokens`` stop. No sampling parameters are ever set (§9.2).
    """
    spec = STAGE_SPECS[stage]
    output_config: dict[str, Any] = {"effort": spec.effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": spec.retry_max_tokens if retry else spec.max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if tools:
        request["tools"] = tools
    return request


@dataclass
class CallResult:
    """The outcome of ``call_sync``.

    ``message`` is the last response (None when the call raised, ``error`` then holding the
    stored error text). ``stop_reason`` is ``"refusal"`` when every model in the chain
    refused and ``"max_tokens"`` when the retry was truncated too. ``served_by`` is the
    model of the last attempt; ``usage`` is that attempt's (``ledger.USAGE_KEYS``).
    """

    message: Any | None
    text: str
    stop_reason: str | None
    refusal_category: str | None
    served_by: str
    usage: dict
    error: str | None


def _text_of(message: Any) -> str:
    return "".join(
        block.text for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


def _chain(first: str, skip_models: frozenset[str]) -> list[str]:
    """The request's model, then ``landscape_fallback_models``, de-duplicated, without
    ``skip_models`` (D42)."""
    chain: list[str] = []
    for model in (first, *get_settings().landscape_fallback_models):
        if model not in chain and model not in skip_models:
            chain.append(model)
    return chain


async def _attempt(
    client: Any, request: dict, *, db_factory, ledger_stage: str, item_ref: str,
) -> CallResult:
    """One API call and its ledger row. An exception becomes ``CallResult.error`` and still
    writes a row (with no usage), since a request that went out may have been billed."""
    model = request["model"]
    require_priced(model)
    started = time.monotonic()
    message: Any | None = None
    error: str | None = None
    try:
        message = await llm.abeta_create(client, **request)
    except Exception as exc:  # noqa: BLE001 - every API failure is reported, not raised
        error = job_error_text(exc)
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = usage_from(message) if message is not None else {}
    category = llm.refusal_category(message) if message is not None else None
    async with db_factory() as db:
        await record_call(
            db, stage=ledger_stage, model=model, batch_id=None, item_seq=None, custom_id=None,
            usage=usage, refusal_category=category,
            served_by=model if message is not None else None,
            batch=False, latency_ms=latency_ms, item_ref=item_ref,
        )
        await db.commit()
    return CallResult(
        message=message,
        text=_text_of(message) if message is not None else "",
        stop_reason=getattr(message, "stop_reason", None),
        refusal_category=category,
        served_by=model,
        usage=usage,
        error=error,
    )


async def call_sync(
    stage: str, request: dict, *, db_factory, item_ref: str,
    skip_models: frozenset[str] = frozenset(), ledger_stage: str | None = None,
) -> CallResult:
    """Send ``request`` synchronously down the client-side refusal chain (D42, §9.3).

    The chain is ``request["model"]``, then ``landscape_fallback_models``, de-duplicated and
    without ``skip_models``: a batch refusal passes its batch model there, so that model is
    never billed again. Each model that refuses is followed by the next. A ``max_tokens``
    stop is retried once on the same model with the stage's ``retry_max_tokens`` (unless the
    request already uses it); a second stop is returned as is. An API error ends the call.
    ``require_priced`` runs before each attempt, so an unpriced model raises
    ``UnpricedModelError`` before anything is sent to it. Every attempt writes one ledger
    row, committed through ``db_factory`` under ``ledger_stage`` (default ``stage``).

    No ``fallbacks`` parameter is sent: the ledger must name the model that served (§9.3).
    """
    retry_max_tokens = STAGE_SPECS[stage].retry_max_tokens
    row_stage = ledger_stage or stage
    client = llm.get_anthropic_client()
    result = CallResult(None, "", "refusal", None, "", {}, None)
    for model in _chain(request["model"], skip_models):
        attempt_request = {**request, "model": model}
        result = await _attempt(
            client, attempt_request, db_factory=db_factory, ledger_stage=row_stage,
            item_ref=item_ref,
        )
        if result.stop_reason == "max_tokens" and attempt_request["max_tokens"] < retry_max_tokens:
            result = await _attempt(
                client, {**attempt_request, "max_tokens": retry_max_tokens},
                db_factory=db_factory, ledger_stage=row_stage, item_ref=item_ref,
            )
        if result.error is not None or result.stop_reason != "refusal":
            return result
    return result
