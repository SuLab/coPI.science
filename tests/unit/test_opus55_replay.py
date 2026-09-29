"""Pure helpers of scripts/dev/opus55_replay.py (plan 2026-09-28 Task 0).

The script is loaded by path (scripts/ is not a package), the idiom of
tests/unit/test_eval_review_bot_grader.py. Nothing here reaches the API or a database: the
classification, statistics, latency fit, ceiling rule and gates are what decide the plan's
go/no-go, so they are the part worth pinning.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "opus55_replay", ROOT / "scripts" / "dev" / "opus55_replay.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


R = _load()


def _msg(content, *, stop_reason="end_turn", model="claude-opus-5-5", iterations=None,
         stop_details=None, output_tokens=100, thinking_tokens=None):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=model,
        stop_details=stop_details,
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=output_tokens,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            output_tokens_details=SimpleNamespace(thinking_tokens=thinking_tokens),
            iterations=iterations,
        ),
    )


def _it(kind, model, out=10):
    return SimpleNamespace(type=kind, model=model, input_tokens=5, output_tokens=out,
                           cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _fallback_block(category="bio"):
    return SimpleNamespace(
        type="fallback",
        from_=SimpleNamespace(model="claude-opus-5-5"),
        to=SimpleNamespace(model="claude-opus-5"),
        trigger=SimpleNamespace(type="refusal", category=category),
    )


def test_fallback_entry_keeps_thinking_on_only_when_the_request_carries_tools():
    assert R.fallback_entry(True) == {
        "model": "claude-opus-5", "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert R.fallback_entry(False)["thinking"] == {"type": "disabled"}


def test_replay_ceiling_doubles_single_calls_and_keeps_thread_reply():
    assert R.replay_max_tokens("consult") == 8000
    assert R.replay_max_tokens("review") == 16000
    assert R.replay_max_tokens("thread_reply") == 16000
    assert all(R.replay_max_tokens(k) <= 21_333 for k in R.CURRENT_CEILING)


def test_first_request_is_everything_before_the_first_assistant_message():
    stored = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
    ]
    assert R.first_request(stored) == [{"role": "user", "content": "u0"}]
    with pytest.raises(ValueError):
        R.first_request([{"role": "assistant", "content": "x"}])


def test_classify_a_plain_reply_is_served_by_the_new_model():
    info = R.classify(_msg([SimpleNamespace(type="text", text="hi")],
                           iterations=[_it("message", None)], thinking_tokens=7))
    assert info["served_by_new"] is True
    assert info["sticky"] is False and info["lost"] is False and info["fallbacks"] is None
    assert info["thinking_tokens"] == 7


def test_classify_a_rescue_names_the_fallback_and_its_category():
    info = R.classify(_msg(
        [_fallback_block("bio"), SimpleNamespace(type="text", text="hi")],
        model="claude-opus-5",
        iterations=[_it("message", None, out=0), _it("fallback_message", "claude-opus-5")],
    ))
    assert info["served_by_new"] is False
    assert info["fallbacks"] == [{"from": "claude-opus-5-5", "to": "claude-opus-5", "category": "bio"}]
    assert info["sticky"] is False


def test_classify_a_sticky_turn_has_no_fallback_block():
    info = R.classify(_msg([SimpleNamespace(type="text", text="hi")], model="claude-opus-5",
                           iterations=[_it("fallback_message", "claude-opus-5")]))
    assert info["served_by_new"] is False and info["sticky"] is True
    assert info["fallbacks"] is None


def test_classify_a_refusal_is_lost_with_its_category():
    info = R.classify(_msg([], stop_reason="refusal",
                           stop_details=SimpleNamespace(category="reasoning_extraction",
                                                        recommended_model=None,
                                                        fallback_credit_token=None)))
    assert info["lost"] is True
    assert info["refusal_category"] == "reasoning_extraction"


def test_call_cost_prices_each_attempt_and_reads_a_none_model_as_the_requested_one():
    info = R.classify(_msg([], iterations=[
        SimpleNamespace(type="message", model=None, input_tokens=1_000_000, output_tokens=0,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-opus-5", input_tokens=0,
                        output_tokens=1_000_000, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0),
    ]))
    # $4 (5.5 input) + $25 (Opus 5 output)
    assert R.call_cost(info, "claude-opus-5-5") == Decimal("29")


def test_upper_bounds_match_the_plan():
    assert R.upper95(0, 15) == pytest.approx(0.181, abs=0.002)
    assert R.upper95(0, 20) == pytest.approx(0.139, abs=0.002)
    assert R.upper95(3, 3) == 1.0
    assert R.upper95(0, 0) is None


def test_latency_fit_pools_the_slope_and_takes_each_route_s_worst_residual():
    points = [("a", 2000, 12.0), ("a", 4000, 22.0), ("b", 3000, 20.0), ("b", 500, 30.0)]
    fit = R.fit_latency(points)
    assert fit.b == pytest.approx(0.005, rel=0.05)
    assert fit.a["b"] == pytest.approx(30.0 - fit.b * 500)
    assert fit.a_pooled == max(fit.a.values())
    assert R.fit_latency([("a", 900, 10.0), ("a", 1000, 11.0)]).b is None


def _fit(b, a):
    return R.LatencyFit(b=b, a={"thread_reply": a, "review": a, "consult": a}, a_pooled=a)


def test_thread_reply_at_16000_passes_when_5_5_matches_opus_5():
    same = _fit(1 / 60.7, 20.0)  # 20 + 263.6 s > 270 s: only the no-regression clause saves it
    rule = R.ceiling_rule(route="thread_reply", current=16000, max_observed_output=None,
                          fit55=same, fit_o5=same, decline=None)
    assert rule["go"] is True and rule["clauses"]["ii_latency"] is True


def test_thread_reply_fails_when_5_5_is_20_percent_slower():
    rule = R.ceiling_rule(route="thread_reply", current=16000, max_observed_output=None,
                          fit55=_fit(1.2 / 60.7, 20.0), fit_o5=_fit(1 / 60.7, 20.0), decline=None)
    assert rule["go"] is False and "ii_latency" in rule["blocking"]


def test_a_route_without_a_slope_cannot_pass():
    rule = R.ceiling_rule(route="consult", current=4000, max_observed_output=3000,
                          fit55=R.LatencyFit(b=None), fit_o5=_fit(1 / 80.9, 10.0), decline=None)
    assert rule["go"] is False


def test_decline_time_defaults_to_the_route_s_worst_served_latency():
    assert R.decline_time("consult", [], _fit(0.01, 5.0), [12.0, 40.0]) == 40.0
    d = R.decline_time("consult", [(1000, 50.0)], _fit(0.01, 5.0), [12.0])
    assert d == pytest.approx(50.0 - (5.0 + 10.0))


def test_a_raised_review_ceiling_whose_doubled_retry_is_too_slow_fails_clause_iv():
    fit = _fit(1 / 60.7, 10.0)
    rule = R.ceiling_rule(route="review", current=8000, max_observed_output=6000,
                          fit55=fit, fit_o5=fit, decline=None)
    assert rule["proposed"] == 9000 and rule["retry"] == 18000
    assert rule["clauses"]["iv_retry"] is False and rule["go"] is False


def test_thread_reply_escalates_only_at_13000():
    assert R.thread_reply_ceiling(12_999)["owner_decision_needed"] is False
    assert R.thread_reply_ceiling(13_000)["owner_decision_needed"] is True


def test_gates_count_small_samples_and_rate_large_ones():
    assert R.lost_gate("new_post", 1, 10) is True
    assert R.lost_gate("new_post", 2, 10) is False
    assert R.lost_gate("consult", 6, 120) is True  # 5 % <= max(2 x 4.6 %, 5 %)
    assert R.lost_gate("consult", 12, 120) is False
    assert R.lost_gate("profile", 1, 3) is False
    assert R.served_gate(16, 20, persona=False) is True
    assert R.served_gate(15, 20, persona=False) is False
    assert R.served_gate(9, 15, persona=True) is True
    assert R.served_gate(8, 15, persona=True) is False


def test_summarize_keeps_arm_b_consults_apart_and_compares_on_the_same_rows():
    rows = [
        {"route": "consult", "arm": "A", "model": R.NEW, "status": "ok", "served_by_new": True,
         "lost": False, "sticky": False, "parse_ok": True, "baseline_ok": True,
         "output_tokens": 100, "latency_s": 3.0},
        {"route": "consult", "arm": "A", "model": R.NEW, "status": "ok", "served_by_new": False,
         "lost": False, "sticky": True, "parse_ok": False, "baseline_ok": True,
         "output_tokens": 200, "latency_s": 4.0},
        {"route": "consult", "arm": "B", "model": R.NEW, "status": "ok", "served_by_new": True,
         "lost": False, "sticky": False, "parse_ok": True, "baseline_ok": True,
         "output_tokens": 300, "latency_s": 5.0},
        {"route": "consult", "arm": "control", "model": R.OLD, "status": "ok",
         "served_by_new": True, "lost": False, "sticky": False, "output_tokens": 1,
         "latency_s": 1.0},
    ]
    s = R.summarize(rows)
    assert set(s) == {"consult", "consult:B"}
    assert s["consult"]["n"] == 2 and s["consult"]["sticky"] == 1
    assert s["consult"]["baseline_ok"] == 2 and s["consult"]["new_ok_on_baseline_rows"] == 1
    assert R.summarize(rows, model=R.OLD)["consult"]["n"] == 1


def test_only_apply_or_count_tokens_reach_the_api():
    assert R.sends_requests(apply=False, count_tokens=False) is False
    assert R.sends_requests(apply=True, count_tokens=False) is True
    assert R.sends_requests(apply=False, count_tokens=True) is True
    args = R._parse_args([])
    assert args.apply is False and args.count_tokens is False


def _rec(route, arm, model, out, lat, *, served=True, persona=None):
    return {"route": route, "arm": arm, "persona": persona, "model": model, "status": "ok",
            "served_by_new": served, "lost": False, "sticky": False, "fallbacks": None,
            "refusal_category": None, "parse_ok": True, "baseline_ok": True,
            "stop_reason": "end_turn", "output_tokens": out, "thinking_tokens": 0,
            "latency_s": lat}


def test_analyse_runs_end_to_end_on_every_route_kind():
    """The analysis runs AFTER the money is spent; it must not crash on a real record mix."""
    records = []
    for i, persona in enumerate(R.PERSONAS):
        records.append(_rec("consult", "A", R.NEW, 2000 + 100 * i, 30.0 + i, persona=persona))
        records.append(_rec("consult", "B", R.NEW, 2500, 35.0, persona=persona))
    for route in ("new_post", "thread_reply_first_lab", "thread_reply_first_hub", "hub_final",
                  "forced_final", "profile", "review"):
        records.append(_rec(route, "B" if route == "forced_final" else "A", R.NEW, 3000, 45.0))
    records.append(_rec("hub_final", "A", R.NEW, 2800, 90.0, served=False))
    records.append(_rec("consult", "control", R.OLD, 2200, 28.0, persona="legal"))
    records.append(_rec("thread_reply_first_hub", "control", R.OLD, 4000, 55.0))
    records.append({"route": "consult", "arm": "A", "model": R.NEW, "status": "error",
                    "persona": "legal", "baseline_ok": True})
    result = R.analyse(records)
    assert set(result["ceilings"]) == {"consult", "new_post", "review", "profile", "thread_reply"}
    assert "consult:B" in result["gate1"]
    assert isinstance(result["gate1_go"], bool)


def test_round_robin_interleaves_groups_so_an_early_stop_covers_each():
    groups = {"budget": [1, 2, 3], "legal": [4, 5], "talent": [6]}
    assert R._round_robin(groups, 4) == [1, 4, 6, 2]
    assert R._round_robin(groups, 10) == [1, 4, 6, 2, 5, 3]


def test_worst_case_charges_input_for_both_attempts_and_both_outputs():
    # 3,000,000 chars ~ 1M input tokens at $5 + $6.25; 1M output tokens at $20 + $25.
    assert R.worst_case_cost(3_000_000, 1_000_000) == Decimal("56.25")
