"""Phase 0 of the Opus 5 -> Opus 5.5 migration: replay real requests on claude-opus-5-5.

docs/plans/2026-09-28-opus-5-5-migration-plan.md, Task 0. Replays a stratified sample of
stored ``llm_call_logs`` requests — plus profile-synthesis and review-bot inputs rebuilt
read-only — on ``claude-opus-5-5`` in the POST-migration request shape, and a small Opus 5
control arm in TODAY's shape, and records METRICS ONLY (never prompt or response text), so
the plan's D2 gate 1 and Task 5 ceiling rule can be judged.

It writes nothing to the database (every transaction is READ ONLY at the wire level),
installs no ``llm_call_logs`` callback, enqueues no job and posts nothing to Slack.

Modes:
  (default)       dry run — sample counts only; nothing leaves the host
  --count-tokens  also sends every sampled request to ``messages.count_tokens``
                  (generates nothing, bills nothing)
  --apply         spends money — ``--max-calls`` hard cap, and an abort once the running
                  cost reaches $55; an exception after a request was sent is charged its
                  worst case

Run it in a one-off app container off the deployed image (SDK 1.8.0) with the tree's code
mounted, so the helpers it imports are the branch's:

  docker compose -f docker-compose.prod.yml run --rm -T --no-deps -e PYTHONPATH=/app \\
    -v "$PWD/src:/app/src:ro" -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/docs:/app/docs" \\
    blackbird-app python scripts/dev/opus55_replay.py [--count-tokens | --apply]

The pure helpers (classification, statistics, the latency fit, the ceiling rule, the gates)
are module-level and import nothing heavy, so tests/unit/test_opus55_replay.py can load this
file by path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.services.llm_pricing import cost_for_tokens  # noqa: E402

NEW = "claude-opus-5-5"
OLD = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
# The two latest runs: bf6da580 ran HEAD's tools and prompts (hub v1.8.0, rubric v3.5.0);
# 75ca77f9 ran hub v1.7.0 / rubric v3.4.0 (plan E9). Each record carries its run's stamps.
RUNS = (
    "bf6da580-fd47-491f-8388-6129117dd28d",
    "75ca77f9-7cd1-4841-b7d5-35185ff04652",
)
PERSONAS = (
    "budget", "chemistry", "clinical", "commercial",
    "legal", "scientific", "talent", "technologic",
)
# The call sites' ceilings today (plan E1).
CURRENT_CEILING = {
    "consult": 4000, "new_post": 3300, "thread_reply": 16000, "review": 8000, "profile": 4000,
}
SINGLE_EFFORT = "low"
LOOP_EFFORT = "medium"
NONSTREAMING_CAP = 21_333
COST_CAP_USD = Decimal("55")
DEFAULT_MAX_CALLS = 260
READ_TIMEOUT_S = 300.0
# E20: Opus 5 refusal rates per route on production call_stats (2026-08-19 -> 09-25).
OPUS5_REFUSAL_RATE = {
    "consult": 87 / 1890, "new_post": 5 / 195, "thread_reply_first_lab": 10 / 450,
    "thread_reply_first_hub": 40 / 593, "hub_final": 40 / 593, "forced_final": 0.0,
    "profile": 0.0, "review": 0.0,
}
SAMPLE_PLAN = {
    "consult_a_per_persona": 15, "consult_b_per_persona": 5, "new_post": 10,
    "first_lab": 20, "first_hub": 20, "hub_final": 10, "forced_final": 3,
    "control_consult": 10, "control_first_hub": 10, "profile": 3, "review": 3,
}
REVIEW_CASES = ("baseline_scientific_gap", "band_recommendation_mismatch", "rubric_calibration")
DUMMY_TOOL = {
    "name": "note",
    "description": "Record a short note. Use only when asked.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}
_SLACK_RE = re.compile(r"<slack_message>.*?</slack_message>", re.S)


# ---------------------------------------------------------------------------
# Pure helpers (tested)
# ---------------------------------------------------------------------------


def fallback_entry(has_tools: bool) -> dict[str, Any]:
    """D4: pinned to Opus 5 at today's settings — thinking on iff the request carries tools."""
    return {
        "model": OLD,
        "thinking": {"type": "adaptive"} if has_tools else {"type": "disabled"},
        "output_config": {"effort": "high"},
    }


def replay_max_tokens(route_kind: str) -> int:
    """2x today's ceiling so truncation does not censor the size distribution; thread_reply
    keeps 16000 because doubling it would push a full reply past the 300 s read timeout."""
    current = CURRENT_CEILING[route_kind]
    if route_kind == "thread_reply":
        return current
    return min(2 * current, NONSTREAMING_CAP)


def first_request(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first call of a stored tool-loop turn: everything before the first assistant
    message. A stored conversation holds the input plus each round's assistant content and
    tool results (the final reply is not appended), so this is the turn's opening request."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            break
        out.append(message)
    if not out:
        raise ValueError("stored conversation has no leading user message")
    return out


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _iterations(usage: Any) -> list[dict[str, Any]] | None:
    raw = _get(usage, "iterations")
    if not raw:
        return None
    return [
        {
            "type": _get(it, "type"),
            "model": _get(it, "model"),
            "input_tokens": _get(it, "input_tokens"),
            "output_tokens": _get(it, "output_tokens"),
            "cache_read_input_tokens": _get(it, "cache_read_input_tokens"),
            "cache_creation_input_tokens": _get(it, "cache_creation_input_tokens"),
        }
        for it in raw
    ]


def classify(message: Any) -> dict[str, Any]:
    """Who served a reply, derived STRUCTURALLY (fallback blocks and usage.iterations), never
    from a model-string comparison (plan D2, C16)."""
    content = _get(message, "content") or []
    block_types = [_get(b, "type") for b in content]
    fallbacks = []
    for block in content:
        if _get(block, "type") != "fallback":
            continue
        trigger = _get(block, "trigger")
        fallbacks.append({
            "from": _get(_get(block, "from_") or _get(block, "from"), "model"),
            "to": _get(_get(block, "to"), "model"),
            "category": _get(trigger, "category"),
        })
    usage = _get(message, "usage")
    iterations = _iterations(usage)
    has_fallback_iteration = any(i["type"] == "fallback_message" for i in iterations or [])
    has_message_iteration = any(i["type"] == "message" for i in iterations or [])
    sticky = has_fallback_iteration and not fallbacks and not has_message_iteration
    stop_details = _get(message, "stop_details")
    stop_reason = _get(message, "stop_reason")
    details = _get(usage, "output_tokens_details")
    return {
        "served_model": _get(message, "model"),
        "served_by_new": not fallbacks and not has_fallback_iteration,
        "fallbacks": fallbacks or None,
        "sticky": sticky,
        "lost": stop_reason == "refusal",
        "stop_reason": stop_reason,
        "refusal_category": _get(stop_details, "category") if stop_reason == "refusal" else None,
        "recommended_model": _get(stop_details, "recommended_model"),
        "fallback_credit_token": _get(stop_details, "fallback_credit_token") is not None,
        "block_types": block_types,
        "iterations": iterations,
        "input_tokens": _get(usage, "input_tokens"),
        "output_tokens": _get(usage, "output_tokens"),
        "thinking_tokens": _get(details, "thinking_tokens"),
        "cache_read_input_tokens": _get(usage, "cache_read_input_tokens"),
        "cache_creation_input_tokens": _get(usage, "cache_creation_input_tokens"),
    }


def call_cost(info: dict[str, Any], requested: str) -> Decimal | None:
    """Dollars for one reply: per billed attempt when ``usage.iterations`` is present (a
    ``message`` entry whose model is None is the requested model's own attempt — 1.8.0 returns
    it that way, plan E18), else the top-level usage at the serving model."""
    def price(model: str, tokens: dict[str, Any]) -> Decimal | None:
        return cost_for_tokens(
            model,
            input_tokens=tokens.get("input_tokens") or 0,
            output_tokens=tokens.get("output_tokens") or 0,
            cache_read=tokens.get("cache_read_input_tokens") or 0,
            cache_creation=tokens.get("cache_creation_input_tokens") or 0,
        )

    if info.get("iterations"):
        total = Decimal(0)
        for it in info["iterations"]:
            cost = price(it.get("model") or requested, it)
            if cost is None:
                return None
            total += cost
        return total
    return price(info.get("served_model") or requested, info)


def worst_case_cost(prompt_chars: int, max_tokens: int) -> Decimal:
    """An exception after a request was sent (a timeout, a dropped connection) is billed but
    returns no usage: charge input (~3 chars/token) at the dearer cache-write rate and a full
    ``max_tokens`` at BOTH models' output rates (a rescue is a second generation), and the
    input at both models' cache-write rates (a rescue re-reads it)."""
    input_tokens = Decimal(prompt_chars) / 3
    # Both attempts bill their input: 5.5 at its $5 cache-write rate, Opus 5 at $6.25.
    return (input_tokens * Decimal("11.25") + Decimal(max_tokens) * Decimal("45")) / Decimal(
        1_000_000
    )


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def upper95(k: int, n: int) -> float | None:
    """One-sided 95 % Clopper-Pearson upper bound on a rate with k events in n trials."""
    if n <= 0:
        return None
    if k >= n:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > 0.05:
            lo = mid
        else:
            hi = mid
    return hi


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1); None on no data."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


@dataclass
class LatencyFit:
    """``latency = a(route) + b * output_tokens`` for one model (plan Task 5)."""

    b: float | None
    a: dict[str, float] = field(default_factory=dict)
    a_pooled: float | None = None
    n_slope: int = 0

    def project(self, route: str, tokens: int) -> float | None:
        if self.b is None:
            return None
        a = self.a.get(route, self.a_pooled)
        if a is None:
            return None
        return a + self.b * tokens


def fit_latency(points: list[tuple[str, int, float]]) -> LatencyFit:
    """``points`` = (route, output_tokens, latency_s). ``b`` is fitted POOLED on calls with
    >= 2,000 output tokens (prompt-read time must not masquerade as slow generation; a
    per-route set can be empty); ``a(route)`` is the route's MAXIMUM residual — a worst case,
    not a median; a route without points takes the pooled maximum residual."""
    slope_points = [(t, s) for _r, t, s in points if t is not None and t >= 2000 and s is not None]
    if len(slope_points) < 2:
        return LatencyFit(b=None, n_slope=len(slope_points))
    mean_t = sum(t for t, _ in slope_points) / len(slope_points)
    mean_s = sum(s for _, s in slope_points) / len(slope_points)
    var = sum((t - mean_t) ** 2 for t, _ in slope_points)
    if var == 0:
        return LatencyFit(b=None, n_slope=len(slope_points))
    b = sum((t - mean_t) * (s - mean_s) for t, s in slope_points) / var
    residuals: dict[str, float] = {}
    for route, tokens, seconds in points:
        if tokens is None or seconds is None:
            continue
        r = seconds - b * tokens
        residuals[route] = max(residuals.get(route, r), r)
    return LatencyFit(
        b=b,
        a=residuals,
        a_pooled=max(residuals.values()) if residuals else None,
        n_slope=len(slope_points),
    )


def decline_time(route: str, fallback_points: list[tuple[int, float]], fit_o5: LatencyFit,
                 served_new_latencies: list[float]) -> float | None:
    """D(route): the worst ``latency - L_o5(route, output_tokens)`` over the route's
    fallback-served calls; with no fallback on the route, its maximum 5.5-served latency."""
    if fallback_points and fit_o5.b is not None:
        values = [
            seconds - (fit_o5.project(route, tokens) or 0.0)
            for tokens, seconds in fallback_points
        ]
        return max(values)
    return max(served_new_latencies) if served_new_latencies else None


def roundup_500(value: float) -> int:
    return int(math.ceil(value / 500.0) * 500)


def ceiling_rule(*, route: str, current: int, max_observed_output: int | None,
                 fit55: LatencyFit, fit_o5: LatencyFit, decline: float | None) -> dict[str, Any]:
    """Plan Task 5's rule for one route. thread_reply is the caller's special case."""
    proposed = current
    if max_observed_output is not None:
        proposed = max(current, roundup_500(1.5 * max_observed_output))
    retry = min(2 * proposed, NONSTREAMING_CAP)
    l55_new = fit55.project(route, proposed)
    l55_cur = fit55.project(route, current)
    lo5_cur = fit_o5.project(route, current)
    lo5_new = fit_o5.project(route, proposed)
    raised = proposed > current

    def le(x: float | None, bound: float | None) -> bool | None:
        if x is None or bound is None:
            return None
        return x <= bound

    clauses = {
        "i_nonstreaming_cap": proposed <= NONSTREAMING_CAP,
        "ii_latency": bool(le(l55_new, 270.0))
        or (not raised and bool(le(l55_cur, lo5_cur))),
        "iii_rollback": True if not raised else le(lo5_new, 300.0),
        "iv_retry": True if not raised else (
            le(fit55.project(route, retry), 300.0) is not False
            and le(fit_o5.project(route, retry), 300.0) is not False
            and fit55.b is not None and fit_o5.b is not None
        ),
        "v_rescue": None if decline is None or lo5_new is None else decline + lo5_new <= 300.0,
    }
    blocking = [k for k in ("i_nonstreaming_cap", "ii_latency", "iii_rollback", "iv_retry")
                if clauses[k] is not True]
    return {
        "route": route,
        "current": current,
        "max_observed_output": max_observed_output,
        "proposed": proposed,
        "retry": retry,
        "projected_s": {"l55_new": l55_new, "l55_current": l55_cur,
                        "lo5_new": lo5_new, "lo5_current": lo5_cur, "decline": decline},
        "clauses": clauses,
        "go": not blocking,
        "blocking": blocking,
        "accepted_risk": clauses["v_rescue"] is False,
    }


def thread_reply_ceiling(max_hub_output: int | None) -> dict[str, Any]:
    """Plan Task 5: thread_reply stays 16,000 unless 5.5's max hub output reaches 13,000."""
    escalate = max_hub_output is not None and max_hub_output >= 13_000
    return {"route": "thread_reply", "current": 16000, "proposed": 16000,
            "max_observed_output": max_hub_output, "owner_decision_needed": escalate}


def lost_gate(route: str, lost: int, n: int) -> bool:
    """D2 gate 1, *lost*: count rules for small n, rates for n >= 30; 0 for profile/review."""
    if route in ("profile", "review"):
        return lost == 0
    if n < 30:
        return lost <= 1
    return lost / n <= max(2 * OPUS5_REFUSAL_RATE.get(route, 0.0), 0.05)


def served_gate(served: int, n: int, *, persona: bool) -> bool:
    """D2 gate 1, *served by 5.5*: >= 80 % per route, >= 60 % per consult persona."""
    if n == 0:
        return False
    return served / n >= (0.60 if persona else 0.80)


def group_of(record: dict[str, Any]) -> str:
    """Arm B consults are their own group (a different effort); forced_final is arm B only."""
    if record.get("arm") == "B" and record.get("route") == "consult":
        return "consult:B"
    return record["route"]


def summarize(records: list[dict[str, Any]], key: str = "group", model: str = NEW) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if r.get("model") == model and r.get("status") == "ok":
            name = group_of(r) if key == "group" else r[key]
            groups[name].append(r)
    out: dict[str, Any] = {}
    for name, rows in sorted(groups.items()):
        n = len(rows)
        served = sum(1 for r in rows if r["served_by_new"])
        lost = sum(1 for r in rows if r["lost"])
        sticky = sum(1 for r in rows if r["sticky"])
        parse_ok = sum(1 for r in rows if r.get("parse_ok"))
        baseline = [r for r in rows if r.get("baseline_ok") is not None]
        base_ok = sum(1 for r in baseline if r["baseline_ok"])
        new_ok_on_baseline_rows = sum(1 for r in baseline if r.get("parse_ok"))
        both = sum(1 for r in baseline if r["baseline_ok"] and r.get("parse_ok"))
        categories: dict[str, int] = defaultdict(int)
        for r in rows:
            for fb in r.get("fallbacks") or []:
                categories[str(fb.get("category"))] += 1
            if r.get("refusal_category"):
                categories["refused:" + r["refusal_category"]] += 1
        outs = [r["output_tokens"] for r in rows if r.get("output_tokens") is not None]
        thinks = [r["thinking_tokens"] for r in rows if r.get("thinking_tokens") is not None]
        lats = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
        out[name] = {
            "n": n,
            "served_by_new": served,
            "not_served_upper95": upper95(n - served, n),
            "sticky": sticky,
            "lost": lost,
            "lost_upper95": upper95(lost, n),
            "parse_ok": parse_ok,
            "baseline_n": len(baseline),
            "baseline_ok": base_ok,
            "new_ok_on_baseline_rows": new_ok_on_baseline_rows,
            "both_ok": both,
            "max_tokens_stops": sum(1 for r in rows if r.get("stop_reason") == "max_tokens"),
            "early_text_stops": sum(1 for r in rows if r.get("early_text_stop")),
            "categories": dict(categories),
            "output_p50": percentile(outs, 0.5),
            "output_p95": percentile(outs, 0.95),
            "output_max": max(outs) if outs else None,
            "thinking_max": max(thinks) if thinks else None,
            "latency_p50": percentile(lats, 0.5),
            "latency_p95": percentile(lats, 0.95),
            "latency_max": max(lats) if lats else None,
        }
    return out


def sends_requests(*, apply: bool, count_tokens: bool) -> bool:
    """Only --apply (spends) and --count-tokens (sends prompts, generates nothing) reach the API."""
    return apply or count_tokens


def slack_ok(text: str) -> bool:
    return bool(_SLACK_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Budget, client, one call
# ---------------------------------------------------------------------------


class Budget:
    def __init__(self, cap: Decimal, max_calls: int) -> None:
        self.cap = cap
        self.max_calls = max_calls
        self.spent = Decimal(0)
        self.calls = 0
        self.unpriced = 0
        self.lock = asyncio.Lock()

    def exhausted(self) -> bool:
        return self.spent >= self.cap or self.calls >= self.max_calls


def _client():
    """Production's client settings (300 s read timeout) with NO SDK retries: the default 2
    would re-send — and re-bill — a timed-out request invisibly (plan Task 0 Step 4)."""
    from src.config import get_settings
    from src.services import llm

    return llm._client_for_key(get_settings().anthropic_api_key).with_options(max_retries=0)


def _prompt_chars(kwargs: dict[str, Any]) -> int:
    return len(json.dumps(kwargs.get("system"), default=str)) + len(
        json.dumps(kwargs.get("messages"), default=str)
    )


async def issue(client: Any, budget: Budget, *, beta: bool, kwargs: dict[str, Any]) -> tuple[
    Any, dict[str, Any]
]:
    """Send one request, charge it, and return (message or None, record fields)."""
    async with budget.lock:
        if budget.exhausted():
            return None, {"status": "skipped_budget"}
        budget.calls += 1
    create = client.beta.messages.create if beta else client.messages.create
    t0 = time.monotonic()
    try:
        message = await asyncio.to_thread(create, **kwargs)
    except Exception as exc:  # noqa: BLE001 — every failure is a data point, not an abort
        import anthropic

        latency = time.monotonic() - t0
        # A timeout or dropped connection may have been processed and billed with no usage
        # returned; an HTTP status error (400/429/5xx) returned no generation.
        maybe_billed = isinstance(exc, anthropic.APIConnectionError)
        charge = (worst_case_cost(_prompt_chars(kwargs), kwargs.get("max_tokens", 0))
                  if maybe_billed else Decimal(0))
        async with budget.lock:
            budget.spent += charge
        status_code = getattr(exc, "status_code", None)
        return None, {
            "status": "error",
            "error_class": type(exc).__name__,
            "status_code": status_code,
            "error_message": (str(getattr(exc, "message", exc)) or "")[:300],
            "latency_s": round(latency, 1),
            "timed_out": latency >= READ_TIMEOUT_S - 1,
            "charged_usd": float(charge),
        }
    latency = time.monotonic() - t0
    info = classify(message)
    cost = call_cost(info, kwargs["model"])
    async with budget.lock:
        if cost is None:
            budget.unpriced += 1
        else:
            budget.spent += cost
    info.update({
        "status": "ok",
        "latency_s": round(latency, 1),
        "cost_usd": float(cost) if cost is not None else None,
        "input_transformations": [
            t.model_dump() if hasattr(t, "model_dump") else t
            for t in (getattr(message, "input_transformations", None) or [])
        ] or None,
    })
    return message, info


def _text(message: Any) -> str:
    from src.services.llm import _all_text

    return _all_text(message) if message is not None else ""


# ---------------------------------------------------------------------------
# Sampling (read-only)
# ---------------------------------------------------------------------------


def _round_robin(groups: dict[Any, list[Any]], count: int) -> list[Any]:
    """Take ``count`` items spreading over the groups in order (distinct threads/phases)."""
    queues = [list(v) for _k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))]
    out: list[Any] = []
    while len(out) < count and any(queues):
        for q in queues:
            if q and len(out) < count:
                out.append(q.pop(0))
    return out


async def _run_stamps(db: Any) -> dict[str, str]:
    from sqlalchemy import select

    from src.models import SimulationRun

    run_ids = [uuid.UUID(r) for r in RUNS]
    rows = (await db.execute(select(SimulationRun).where(SimulationRun.id.in_(run_ids)))).scalars()
    stamps: dict[str, str] = {}
    for run in rows:
        text = ((run.config or {}).get("run_start_announcement") or {}).get("text", "")
        hub = re.search(r"Hub prompts: (v[\d.]+)", text)
        rubric = re.search(r"Rubric: (v[\d.]+)", text)
        stamps[str(run.id)] = f"hub {hub.group(1) if hub else '?'} rubric {rubric.group(1) if rubric else '?'}"
    return stamps


async def sample(db: Any) -> dict[str, list[Any]]:
    from sqlalchemy import select

    from src.models import AgentRegistry, LlmCallLog

    base = (
        select(LlmCallLog)
        .where(LlmCallLog.simulation_run_id.in_([uuid.UUID(r) for r in RUNS]),
               LlmCallLog.model == OLD)
        .order_by(LlmCallLog.created_at)
    )
    roles = {
        a.agent_id: a.role
        for a in (await db.execute(select(AgentRegistry))).scalars()
    }
    out: dict[str, list[Any]] = {}

    latest = uuid.UUID(RUNS[0])
    per_persona_a: dict[str, list[Any]] = {}
    consult_b: list[Any] = []
    for persona in PERSONAS:
        rows = list((await db.execute(base.where(LlmCallLog.phase == f"consult_{persona}"))).scalars())
        # The run on HEAD's prompts first (plan Task 0 Step 3), then the older one.
        rows.sort(key=lambda r: (r.simulation_run_id != latest, r.created_at))
        by_thread: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            by_thread[r.thread_ts or str(r.id)].append(r)
        a_n = SAMPLE_PLAN["consult_a_per_persona"]
        picked = _round_robin(by_thread, a_n + SAMPLE_PLAN["consult_b_per_persona"])
        arm_a = sorted(picked[:a_n], key=lambda r: r.created_at)  # production order per persona
        per_persona_a[persona] = arm_a
        consult_b += picked[a_n:]
    # Interleaved ACROSS personas (production order kept within each), so an early stop still
    # covers every persona — the 2026-09-28 run's persona-major order replayed only `budget`.
    out["consult_a"] = _round_robin(per_persona_a, sum(len(v) for v in per_persona_a.values()))
    out["consult_b"] = consult_b

    new_posts = list((await db.execute(base.where(LlmCallLog.phase == "new_post"))).scalars())
    new_posts.sort(key=lambda r: (r.simulation_run_id != latest, r.created_at))
    out["new_post"] = new_posts[: SAMPLE_PLAN["new_post"]]

    thread_rows = list((await db.execute(base.where(LlmCallLog.phase == "thread_reply"))).scalars())
    sidecar = [r for r in thread_rows if roles.get(r.agent_id) == "scout_hub"
               and "<assessment_json>" in (r.response_text or "")]
    sidecar.sort(key=lambda r: r.created_at, reverse=True)
    out["hub_final"] = sidecar[: SAMPLE_PLAN["hub_final"]]
    out["forced_final"] = sidecar[SAMPLE_PLAN["hub_final"]: SAMPLE_PLAN["hub_final"] + SAMPLE_PLAN["forced_final"]]
    used_turns = {(r.thread_ts, r.message_ordinal) for r in sidecar}

    def by_phase(rows: list[Any]) -> dict[str, list[Any]]:
        groups: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            groups[r.thread_phase or "none"].append(r)
        return groups

    thread_rows.sort(key=lambda r: (r.simulation_run_id != latest, r.created_at))
    hub_first = [r for r in thread_rows if roles.get(r.agent_id) == "scout_hub"
                 and (r.thread_ts, r.message_ordinal) not in used_turns
                 and "<assessment_json>" not in (r.response_text or "")]
    lab_first = [r for r in thread_rows if roles.get(r.agent_id) == "pi_lab"]
    out["first_hub"] = _round_robin(by_phase(hub_first), SAMPLE_PLAN["first_hub"])
    out["first_lab"] = _round_robin(by_phase(lab_first), SAMPLE_PLAN["first_lab"])
    out["roles"] = [roles]
    return out


async def sample_profiles(db: Any) -> list[dict[str, Any]]:
    """3 PIs with a validated stored profile and >= 5 abstracts; context rebuilt with the
    pipeline's own ``_build_synthesis_context`` over the STORED corpus, tenure-filtered as the
    pipeline does — approximate: no live ORCID/PMC fetch, today's grant_titles (plan Task 0)."""
    from sqlalchemy import func, select

    from src.models import Publication, ResearcherProfile, User
    from src.services.jhu_rules import get_tenure_start, tenure_filter
    from src.services.profile_pipeline import _build_synthesis_context

    counts = (
        select(Publication.user_id, func.count().label("n"))
        .where(Publication.abstract.is_not(None))
        .group_by(Publication.user_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(User, ResearcherProfile)
            .join(ResearcherProfile, ResearcherProfile.user_id == User.id)
            .join(counts, counts.c.user_id == User.id)
            .where(ResearcherProfile.synthesis_validated.is_(True), counts.c.n >= 5)
            .order_by(ResearcherProfile.profile_generated_at.desc().nullslast())
            .limit(SAMPLE_PLAN["profile"])
        )
    ).all()
    out = []
    for user, profile in rows:
        pubs = (
            await db.execute(
                select(Publication)
                .where(Publication.user_id == user.id)
                .order_by(Publication.year.desc().nullslast(), Publication.pmid.desc())
            )
        ).scalars().all()
        records = [
            {"pmid": p.pmid, "pmcid": p.pmcid, "title": p.title, "abstract": p.abstract,
             "journal": p.journal, "year": p.year, "methods_text": p.methods_text}
            for p in pubs
        ]
        tenure = await get_tenure_start(db, user.id)
        in_tenure = [r for r in tenure_filter(records, tenure) if r.get("abstract")]
        methods = {r["pmid"]: r["methods_text"] for r in in_tenure
                   if r.get("pmcid") and r.get("methods_text")}
        methods = dict(list(methods.items())[:10])
        context = _build_synthesis_context(
            orcid_profile={"name": user.name, "institution": user.institution,
                           "department": user.department},
            grant_titles=list(profile.grant_titles or []),
            publications=in_tenure,
            methods_by_pmid=methods,
        )
        baseline = {
            "research_summary": profile.research_summary or "",
            "techniques": list(profile.techniques or []),
            "experimental_models": list(profile.experimental_models or []),
            "disease_areas": list(profile.disease_areas or []),
            "key_targets": list(profile.key_targets or []),
            "keywords": list(profile.keywords or []),
        }
        out.append({"user_id": str(user.id), "name": user.name, "context": context,
                    "baseline": baseline, "context_chars": len(context)})
    return out


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def new_shape(*, system: str, messages: list[Any], route_kind: str, effort: str,
              tools: list[dict[str, Any]] | None = None, extra: dict[str, Any] | None = None,
              binding: str | None = None) -> dict[str, Any]:
    from src.services.llm import _cacheable_system

    betas = [FALLBACK_BETA]
    thinking: dict[str, Any] = {"type": "adaptive"}
    if binding:
        betas.append(BINDING_BETA)
        thinking["block_binding"] = {"prefix_mismatch_behavior": binding}
    kwargs: dict[str, Any] = {
        "model": NEW,
        "max_tokens": replay_max_tokens(route_kind),
        "system": _cacheable_system(system),
        "messages": messages,
        "thinking": thinking,
        "output_config": {"effort": effort},
        "betas": betas,
        "fallbacks": [fallback_entry(bool(tools))],
    }
    if tools:
        kwargs["tools"] = tools
    kwargs.update(extra or {})
    return kwargs


def old_shape(*, system: str, messages: list[Any], max_tokens: int,
              tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """TODAY's request (plan E1): thinking disabled, or adaptive with tools; no effort."""
    from src.services.llm import _cacheable_system

    kwargs: dict[str, Any] = {
        "model": OLD,
        "max_tokens": max_tokens,
        "system": _cacheable_system(system),
        "messages": messages,
        "thinking": {"type": "adaptive"} if tools else {"type": "disabled"},
    }
    if tools:
        kwargs["tools"] = tools
    return kwargs


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@dataclass
class Job:
    route: str
    arm: str
    persona: str | None
    ref: str
    run: str | None
    beta: bool
    kwargs: dict[str, Any]
    parse: Any  # callable(message, text) -> dict
    baseline_ok: bool | None


def _consult_parse(persona: str):
    def parse(_message: Any, text: str) -> dict[str, Any]:
        from src.agent.specialists import has_usable_content, parse_opinion

        ok = has_usable_content(text) and not parse_opinion(text, domain=persona).signal_was_defaulted
        return {"parse_ok": ok}
    return parse


def _consult_baseline(persona: str, text: str) -> bool:
    from src.agent.specialists import has_usable_content, parse_opinion

    return has_usable_content(text) and not parse_opinion(text, domain=persona).signal_was_defaulted


def _first_parse(_message: Any, text: str) -> dict[str, Any]:
    types = [getattr(b, "type", None) for b in (getattr(_message, "content", None) or [])]
    tool = "tool_use" in types
    slack = slack_ok(text)
    early = (not tool and not slack and bool(text.strip())
             and getattr(_message, "stop_reason", None) == "end_turn")
    return {"parse_ok": tool or slack, "early_text_stop": early}


def _first_baseline(row: Any) -> bool | None:
    stats = row.call_stats or []
    if not stats:
        return None
    first = stats[0]
    if first.get("kind") == "round":
        return True
    if first.get("kind") == "final":
        return slack_ok(row.response_text or "")
    return None


def _sidecar_parse(_message: Any, text: str) -> dict[str, Any]:
    from src.agent.engine.sidecar import _extract_assessment_json

    return {"parse_ok": _extract_assessment_json(text) is not None,
            "empty_text": not text.strip()}


def _post_parse(_message: Any, text: str) -> dict[str, Any]:
    return {"parse_ok": slack_ok(text)}


def _profile_parse(_message: Any, text: str) -> dict[str, Any]:
    from src.services.llm import _extract_json
    from src.services.profile_pipeline import _validate_profile

    try:
        return {"parse_ok": bool(_validate_profile(_extract_json(text)))}
    except ValueError:
        return {"parse_ok": False}


def build_jobs(sampled: dict[str, list[Any]], stamps: dict[str, str],
               profiles: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> tuple[
    list[Job], list[Job]
]:
    """(phase A jobs, phase B jobs). Phase B (arm B, forced-final) runs after phase A so
    sticky routing from phase A cannot reach it (disjoint conversations, C16)."""
    from src.agent.tools import tools_for_role
    from src.services.profile_pipeline import _validate_profile

    roles = sampled["roles"][0]
    hub_tools = tools_for_role("scout_hub")
    lab_tools = tools_for_role("pi_lab")
    a: dict[str, list[Job]] = defaultdict(list)
    b: list[Job] = []

    def consult_job(row: Any, arm: str, effort: str) -> Job:
        persona = row.phase.removeprefix("consult_")
        return Job(
            route="consult", arm=arm, persona=persona, ref=str(row.id),
            run=stamps.get(str(row.simulation_run_id)), beta=True,
            kwargs=new_shape(system=row.system_prompt, messages=row.messages_json,
                             route_kind="consult", effort=effort),
            parse=_consult_parse(persona),
            baseline_ok=_consult_baseline(persona, row.response_text or ""),
        )

    for row in sampled["consult_a"]:
        a["consult"].append(consult_job(row, "A", SINGLE_EFFORT))
    for row in sampled["consult_b"]:
        b.append(consult_job(row, "B", LOOP_EFFORT))

    for row in sampled["new_post"]:
        a["new_post"].append(Job(
            route="new_post", arm="A", persona=None, ref=str(row.id),
            run=stamps.get(str(row.simulation_run_id)), beta=True,
            kwargs=new_shape(system=row.system_prompt, messages=row.messages_json,
                             route_kind="new_post", effort=SINGLE_EFFORT),
            parse=_post_parse, baseline_ok=slack_ok(row.response_text or ""),
        ))

    for key, route, tools in (("first_lab", "thread_reply_first_lab", lab_tools),
                              ("first_hub", "thread_reply_first_hub", hub_tools)):
        for row in sampled[key]:
            a[route].append(Job(
                route=route, arm="A", persona=None, ref=str(row.id),
                run=stamps.get(str(row.simulation_run_id)), beta=True,
                kwargs=new_shape(system=row.system_prompt, messages=first_request(row.messages_json),
                                 route_kind="thread_reply", effort=LOOP_EFFORT, tools=tools),
                parse=_first_parse, baseline_ok=_first_baseline(row),
            ))

    for row in sampled["hub_final"]:
        a["hub_final"].append(Job(
            route="hub_final", arm="A", persona=None, ref=str(row.id),
            run=stamps.get(str(row.simulation_run_id)), beta=True,
            kwargs=new_shape(system=row.system_prompt, messages=row.messages_json,
                             route_kind="thread_reply", effort=LOOP_EFFORT,
                             tools=hub_tools if roles.get(row.agent_id) == "scout_hub" else lab_tools,
                             binding="drop_block"),
            parse=_sidecar_parse, baseline_ok=True,
        ))
    for row in sampled["forced_final"]:
        b.append(Job(
            route="forced_final", arm="B", persona=None, ref=str(row.id),
            run=stamps.get(str(row.simulation_run_id)), beta=True,
            kwargs=new_shape(system=row.system_prompt, messages=row.messages_json,
                             route_kind="thread_reply", effort=LOOP_EFFORT, tools=hub_tools,
                             binding="drop_block", extra={"tool_choice": {"type": "none"}}),
            parse=_sidecar_parse, baseline_ok=True,
        ))

    # Opus 5 control arm, TODAY's shape, interleaved with phase A.
    for row in sampled["consult_a"][:: max(1, len(sampled["consult_a"]) // SAMPLE_PLAN["control_consult"])][: SAMPLE_PLAN["control_consult"]]:
        a["control"].append(Job(
            route="consult", arm="control", persona=row.phase.removeprefix("consult_"),
            ref=str(row.id), run=stamps.get(str(row.simulation_run_id)), beta=False,
            kwargs=old_shape(system=row.system_prompt, messages=row.messages_json,
                             max_tokens=CURRENT_CEILING["consult"]),
            parse=_consult_parse(row.phase.removeprefix("consult_")), baseline_ok=None,
        ))
    for row in sampled["first_hub"][: SAMPLE_PLAN["control_first_hub"]]:
        a["control"].append(Job(
            route="thread_reply_first_hub", arm="control", persona=None, ref=str(row.id),
            run=stamps.get(str(row.simulation_run_id)), beta=False,
            kwargs=old_shape(system=row.system_prompt, messages=first_request(row.messages_json),
                             max_tokens=CURRENT_CEILING["thread_reply"], tools=hub_tools),
            parse=_first_parse, baseline_ok=None,
        ))

    system_prompt = (
        Path("prompts/profile-synthesis.md").read_text()
        if Path("prompts/profile-synthesis.md").exists() else None
    )
    if system_prompt is None:
        from src.services.llm import _default_synthesis_prompt

        system_prompt = _default_synthesis_prompt()
    for p in profiles:
        user_message = (
            f"Please synthesize a researcher profile for {p['name']} from the following "
            f"information:\n\n{p['context']}\n\nReturn your response as valid JSON matching the "
            "specified schema."
        )
        a["profile"].append(Job(
            route="profile", arm="A", persona=None, ref=p["user_id"], run=None, beta=True,
            kwargs=new_shape(system=system_prompt,
                             messages=[{"role": "user", "content": user_message}],
                             route_kind="profile", effort=SINGLE_EFFORT),
            parse=_profile_parse, baseline_ok=bool(_validate_profile(p["baseline"])),
        ))
    for r in reviews:
        a["review"].append(Job(
            route="review", arm="A", persona=None, ref=r["name"], run=None, beta=True,
            kwargs=new_shape(system=r["system_prompt"],
                             messages=[{"role": "user", "content": r["user_message"]}],
                             route_kind="review", effort=SINGLE_EFFORT),
            parse=r["parse"], baseline_ok=r["baseline_ok"],
        ))

    # Round-robin across routes, so a budget abort truncates every route evenly.
    ordered: list[Job] = []
    queues = [list(v) for _k, v in sorted(a.items())]
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    return ordered, b


async def build_reviews(db: Any) -> list[dict[str, Any]]:
    """The three production-like review-bot cases via eval_review_bot._build, graded with its
    own grader; the baseline is the graded 2026-09-02 Opus 5 run of the same case."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "eval_review_bot", Path(__file__).resolve().parents[1] / "eval_review_bot.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    from src.services import review_bot

    cases = {c["name"]: c for c in json.loads(Path("scripts/review_bot_eval_cases.json").read_text())}
    baseline_path = Path("docs/audits/2026-09-02-review-pipeline/eval-results.json")
    baseline: dict[str, bool] = {}
    if baseline_path.exists():
        for rec in json.loads(baseline_path.read_text()).get("results", []):
            g = rec.get("grade") or {}
            if rec.get("name") not in baseline and isinstance(g, dict):
                baseline[rec["name"]] = bool(g.get("target_valid")) and bool(g.get("target_expected"))
    out = []
    for name in REVIEW_CASES:
        case = cases[name]
        try:
            built = await module._build(db, case)
        except Exception as exc:  # noqa: BLE001 — a missing assessment skips the case
            out.append({"name": name, "error": repr(exc)})
            continue

        def parse(_message: Any, text: str, case=case, built=built) -> dict[str, Any]:
            target, suggestion = review_bot._parse_model_output(text)
            grade = module.grade(case, target=target, suggestion=suggestion, raw=text,
                                 corpus=built["corpus"],
                                 transcript_available=built["transcript_available"])
            return {"parse_ok": bool(grade.get("target_valid")) and bool(grade.get("target_expected"))}

        out.append({"name": name, "system_prompt": built["system_prompt"],
                    "user_message": built["user_message"], "parse": parse,
                    "baseline_ok": baseline.get(name)})
    return out


_SINK: Any = None  # the JSONL file each finished record is appended to, as it finishes


async def run_job(client: Any, budget: Budget, job: Job, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        message, info = await issue(client, budget, beta=job.beta, kwargs=job.kwargs)
    record = {
        "route": job.route, "arm": job.arm, "persona": job.persona, "ref": job.ref,
        "run": job.run, "model": job.kwargs["model"], "max_tokens": job.kwargs["max_tokens"],
        "baseline_ok": job.baseline_ok, **info,
    }
    if message is not None:
        try:
            record.update(job.parse(message, _text(message)))
        except Exception as exc:  # noqa: BLE001
            record["parse_error"] = type(exc).__name__
    if _SINK is not None:
        # Written as each call finishes, so a crash later in the run cannot lose data that
        # was already paid for; `--analyse <file>` rebuilds the analysis from it.
        _SINK.write(json.dumps(record, default=str) + "\n")
        _SINK.flush()
    return record


# ---------------------------------------------------------------------------
# Probes: shape (Step 1), echo (Step 6), rate limits (Step 7)
# ---------------------------------------------------------------------------


async def shape_probe(client: Any, budget: Budget) -> list[dict[str, Any]]:
    ok_prompt = [{"role": "user", "content": "Reply with the single word OK."}]
    out = []
    for label, tools in (("single_call", None), ("tool_loop", [DUMMY_TOOL])):
        kwargs = {"model": NEW, "max_tokens": 256, "messages": ok_prompt,
                  "thinking": {"type": "adaptive"},
                  "output_config": {"effort": SINGLE_EFFORT if tools is None else LOOP_EFFORT},
                  "betas": [FALLBACK_BETA], "fallbacks": [fallback_entry(bool(tools))]}
        if tools:
            kwargs.update({"tools": tools, "tool_choice": {"type": "none"}})
        _m, info = await issue(client, budget, beta=True, kwargs=kwargs)
        out.append({"label": label, **{k: info.get(k) for k in (
            "status", "status_code", "error_message", "served_model", "stop_reason")}})
    return out


async def echo_probe(client: Any, budget: Budget, db: Any) -> dict[str, Any]:
    """Step 6: the content that fell back 4 of 4 in E19, non-streaming, with a dummy tool."""
    from sqlalchemy import select

    from src.models import AssessmentChatUsage
    from src.services import assessment_chat
    from src.services.assessment_chat_record import load_chat_record

    row = (await db.execute(select(AssessmentChatUsage).order_by(AssessmentChatUsage.created_at))).scalars().first()
    if row is None or row.assessment_id is None:
        return {"status": "no_chat_assessment"}
    loaded = await load_chat_record(db, row.assessment_id, tier="staff")
    if loaded is None:
        return {"status": "record_missing"}
    record, _assessment = loaded
    prompt, _hash = assessment_chat.load_system_prompt()
    messages = assessment_chat.build_messages(record, [], "summarize this assessment")
    kwargs = {"model": NEW, "max_tokens": 4000, "system": [{"type": "text", "text": prompt}],
              "messages": messages, "tools": [DUMMY_TOOL], "thinking": {"type": "adaptive"},
              "output_config": {"effort": LOOP_EFFORT}, "betas": [FALLBACK_BETA],
              "fallbacks": [fallback_entry(True)]}
    first, info = await issue(client, budget, beta=True, kwargs=kwargs)
    result: dict[str, Any] = {"first": {k: info.get(k) for k in (
        "status", "served_by_new", "fallbacks", "sticky", "stop_reason", "block_types")}}
    if first is None or not info.get("fallbacks"):
        result["echo"] = "not_triggered"
        return result
    content = [b.model_dump(by_alias=True) for b in first.content]
    follow = [*messages, {"role": "assistant", "content": content}]
    tool_uses = [b for b in first.content if getattr(b, "type", None) == "tool_use"]
    if tool_uses:
        follow.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": t.id, "content": "noted"} for t in tool_uses]})
    else:
        follow.append({"role": "user", "content": "Thank you. One more sentence, please."})
    kwargs2 = {**kwargs, "messages": follow, "betas": [FALLBACK_BETA, BINDING_BETA],
               "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}}
    _second, info2 = await issue(client, budget, beta=True, kwargs=kwargs2)
    result["echo"] = {k: info2.get(k) for k in ("status", "status_code", "error_message",
                                                "served_by_new", "sticky", "stop_reason")}
    return result


async def rate_limit_probe(client: Any, budget: Budget) -> dict[str, Any]:
    out: dict[str, Any] = {}
    prompt = [{"role": "user", "content": "Reply with the single word OK."}]
    for model, beta in ((NEW, True), (OLD, False)):
        async with budget.lock:
            budget.calls += 1
        try:
            if beta:
                raw = await asyncio.to_thread(
                    client.beta.messages.with_raw_response.create, model=model, max_tokens=64,
                    messages=prompt, output_config={"effort": "low"})
            else:
                raw = await asyncio.to_thread(
                    client.messages.with_raw_response.create, model=model, max_tokens=64,
                    messages=prompt, thinking={"type": "disabled"})
            out[model] = {k: v for k, v in raw.headers.items() if k.startswith("anthropic-ratelimit")}
        except Exception as exc:  # noqa: BLE001
            out[model] = {"error": type(exc).__name__}
    return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


_CEILING_ROUTE = {
    "consult": "consult", "new_post": "new_post", "review": "review", "profile": "profile",
    "thread_reply_first_lab": "thread_reply", "thread_reply_first_hub": "thread_reply",
    "hub_final": "thread_reply", "forced_final": "thread_reply",
}


def analyse(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r.get("status") == "ok" and r.get("output_tokens") is not None]
    new_served = [r for r in ok if r["model"] == NEW and r["served_by_new"]]
    new_fallback = [r for r in ok if r["model"] == NEW and not r["served_by_new"]]
    control = [r for r in ok if r["model"] == OLD]
    fit55 = fit_latency([(_CEILING_ROUTE[r["route"]], r["output_tokens"], r["latency_s"])
                         for r in new_served])
    fit_o5 = fit_latency([(_CEILING_ROUTE[r["route"]], r["output_tokens"], r["latency_s"])
                          for r in control])
    ceilings = {}
    for route in ("consult", "new_post", "review", "profile"):
        outs = [r["output_tokens"] for r in new_served if _CEILING_ROUTE[r["route"]] == route]
        fb = [(r["output_tokens"], r["latency_s"]) for r in new_fallback
              if _CEILING_ROUTE[r["route"]] == route]
        lat = [r["latency_s"] for r in new_served if _CEILING_ROUTE[r["route"]] == route]
        ceilings[route] = ceiling_rule(
            route=route, current=CURRENT_CEILING[route],
            max_observed_output=max(outs) if outs else None, fit55=fit55, fit_o5=fit_o5,
            decline=decline_time(route, fb, fit_o5, lat),
        )
    hub_outs = [r["output_tokens"] for r in new_served
                if r["route"] in ("thread_reply_first_hub", "hub_final", "forced_final")]
    tr = thread_reply_ceiling(max(hub_outs) if hub_outs else None)
    tr_rule = ceiling_rule(
        route="thread_reply", current=16000, max_observed_output=None, fit55=fit55, fit_o5=fit_o5,
        decline=decline_time(
            "thread_reply",
            [(r["output_tokens"], r["latency_s"]) for r in new_fallback
             if _CEILING_ROUTE[r["route"]] == "thread_reply"],
            fit_o5,
            [r["latency_s"] for r in new_served if _CEILING_ROUTE[r["route"]] == "thread_reply"],
        ),
    )
    ceilings["thread_reply"] = {**tr_rule, **tr}

    by_route = summarize([r for r in records if r.get("arm") in ("A", "B")])
    by_persona = summarize([r for r in records if r.get("route") == "consult" and r.get("arm") == "A"],
                           key="persona")
    gate: dict[str, Any] = {}
    for route, s in by_route.items():
        base_route = route.split(":")[0]
        gate[route] = {
            "lost": lost_gate(base_route, s["lost"], s["n"]),
            "served": served_gate(s["served_by_new"], s["n"], persona=False),
            "parse": s["new_ok_on_baseline_rows"] >= s["baseline_ok"] if s["baseline_n"] else None,
            "reasoning_extraction": not any(
                "reasoning_extraction" in k for k in s["categories"]
            ) if route.startswith(("thread_reply", "hub_final", "forced_final")) else True,
        }
    persona_gate = {p: served_gate(s["served_by_new"], s["n"], persona=True)
                    for p, s in by_persona.items()}
    ceiling_go = all(c.get("go", True) for c in ceilings.values())
    verdict = (
        all(all(v is not False for v in g.values()) for g in gate.values())
        and all(persona_gate.values())
        and ceiling_go
        and not ceilings["thread_reply"].get("owner_decision_needed")
    )
    return {
        "by_route": by_route, "by_persona": by_persona,
        "control": summarize(control, model=OLD),
        "fit_5_5": vars(fit55), "fit_opus_5": vars(fit_o5),
        "ceilings": ceilings, "gate1": gate, "persona_gate": persona_gate,
        "gate1_go": verdict,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(*, apply: bool, count_tokens: bool, max_calls: int, out_dir: Path) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from src.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool,
                                 execution_options={"postgresql_readonly": True})
    report: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "plan":
                              "docs/plans/2026-09-28-opus-5-5-migration-plan.md#task-0"}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            stamps = await _run_stamps(db)
            sampled = await sample(db)
            profiles = await sample_profiles(db)
            reviews = await build_reviews(db)
            phase_a, phase_b = build_jobs(sampled, stamps, profiles,
                                          [r for r in reviews if "error" not in r])
            report["stamps"] = stamps
            report["sample_counts"] = {
                k: len(v) for k, v in sampled.items() if k != "roles"
            } | {"profile": len(profiles), "review": len([r for r in reviews if "error" not in r]),
                 "review_errors": [r for r in reviews if "error" in r],
                 "profile_context_chars": [p["context_chars"] for p in profiles],
                 "phase_a_jobs": len(phase_a), "phase_b_jobs": len(phase_b)}
            print(json.dumps(report["sample_counts"], indent=2, default=str))
            if not sends_requests(apply=apply, count_tokens=count_tokens):
                print("dry run: nothing sent")
                return 0
            client = _client()
            if count_tokens and not apply:
                total = 0
                for job in phase_a + phase_b:
                    kw = {k: v for k, v in job.kwargs.items()
                          if k in ("model", "system", "messages", "tools", "thinking")}
                    kw["thinking"] = {"type": "adaptive"}
                    res = await asyncio.to_thread(client.messages.count_tokens, **kw)
                    total += res.input_tokens
                print(f"count_tokens: {total} input tokens over {len(phase_a) + len(phase_b)} requests")
                return 0

            budget = Budget(COST_CAP_USD, max_calls)
            report["shape_probe"] = await shape_probe(client, budget)
            if any(p["status"] != "ok" for p in report["shape_probe"]):
                print("array-form fallback rejected; see shape_probe — stopping (plan D4)")
                report["budget"] = {"spent_usd": float(budget.spent), "calls": budget.calls}
                _write(report, out_dir)
                return 2
            report["rate_limits"] = await rate_limit_probe(client, budget)
            # Persisted NOW, not only at the end: a stopped run must not lose the probe
            # evidence and sample counts (the 2026-09-28 run did).
            _write(report, out_dir, name="probes")
            global _SINK
            out_dir.mkdir(parents=True, exist_ok=True)
            sink_path = out_dir / f"records-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
            report["records_file"] = str(sink_path)
            with sink_path.open("w") as sink:
                _SINK = sink
                sem = asyncio.Semaphore(4)
                records = list(await asyncio.gather(*(run_job(client, budget, j, sem) for j in phase_a)))
                records += list(await asyncio.gather(*(run_job(client, budget, j, sem) for j in phase_b)))
                _SINK = None
            try:
                report["echo_probe"] = await echo_probe(client, budget, db)
            except Exception as exc:  # noqa: BLE001 — the probe must not cost the run's data
                report["echo_probe"] = {"status": "error", "error": repr(exc)[:300]}
            report["records"] = records
            try:
                report["analysis"] = analyse(records)
            except Exception as exc:  # noqa: BLE001 — records are already on disk
                report["analysis"] = {"error": repr(exc)[:500], "gate1_go": None,
                                      "gate1": None, "persona_gate": None, "ceilings": None}
            report["budget"] = {"spent_usd": float(budget.spent), "calls": budget.calls,
                                "unpriced": budget.unpriced, "cap_usd": float(budget.cap)}
    finally:
        await engine.dispose()
    path = _write(report, out_dir)
    a = report["analysis"]
    print(json.dumps({"gate1_go": a["gate1_go"], "gate1": a["gate1"],
                      "persona_gate": a["persona_gate"], "ceilings": a["ceilings"],
                      "budget": report["budget"]}, indent=2, default=str))
    print(f"report: {path}")
    return 0


def _write(report: dict[str, Any], out_dir: Path, name: str = "replay") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def analyse_file(path: Path) -> dict[str, Any]:
    """Rebuild the analysis offline from a records JSONL or a report JSON (no API, no DB)."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        records = json.loads(text)["records"]
    return analyse(records)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--analyse", type=Path, default=None,
                        help="recompute the analysis from a records .jsonl or report .json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--count-tokens", action="store_true")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--out-dir", type=Path,
                        default=Path("docs/audits/2026-09-28-opus-5-5-migration"))
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.analyse is not None:
        print(json.dumps(analyse_file(args.analyse), indent=2, default=str))
        sys.exit(0)
    sys.exit(asyncio.run(main(apply=args.apply, count_tokens=args.count_tokens,
                              max_calls=min(args.max_calls, DEFAULT_MAX_CALLS),
                              out_dir=args.out_dir)))
