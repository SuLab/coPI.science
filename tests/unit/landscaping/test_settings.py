"""Landscaping settings, guards and the per-epoch rerank lock key (spec §9.1, §12.3)."""
import logging
import uuid

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.services import advisory_locks as al
from src.services.assessment_chat import _SPEND_LOCK_KEY

_FORBIDDEN_MODELS = {"claude-opus-5-5", "claude-haiku-4-5-20251001"}


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_landscape_defaults():
    s = _s()
    assert s.landscape_enabled is False
    assert s.landscape_screen_model == s.landscape_extract_model == s.landscape_triage_model == "claude-sonnet-5"
    for name in ("deep", "rerank", "label", "final", "similarity", "audit"):
        assert getattr(s, f"landscape_{name}_model") == "claude-opus-5"
    assert s.landscape_fallback_models == ["claude-opus-5", "claude-opus-4-8"]
    assert s.llm_landscape_chat_model == "claude-opus-5"
    assert s.landscape_daily_usd_cap is None
    assert s.landscape_batch_min_items == 500
    assert s.landscape_batch_max_wait_minutes == 60
    assert s.landscape_batch_poll_seconds == 300
    assert s.landscape_sync_concurrency == 4
    assert s.landscape_running_stale_minutes == 30
    assert s.landscape_reject_audit_size == 100
    assert s.landscape_ncbi_max_rps == 3.0
    assert s.landscape_europepmc_max_rps == 1.0
    assert s.landscape_bioc_max_rps == 3.0
    assert s.landscape_pubtator_max_rps == 3.0
    assert s.landscape_openfda_daily_requests == 1000
    assert s.landscape_window_days == 1096
    assert s.landscape_cut_fraction == 0.01
    assert s.landscape_chat_enabled is False
    assert s.landscape_chat_daily_usd_limit == 50.0


def test_no_forbidden_model_in_landscape_settings():
    for name, field in Settings.model_fields.items():
        if not (name.startswith("landscape_") or name == "llm_landscape_chat_model"):
            continue
        default = field.default
        values = default if isinstance(default, list) else [default]
        assert not (set(values) & _FORBIDDEN_MODELS), name


def test_optional_cap_guard():
    assert _s(landscape_daily_usd_cap=None).landscape_daily_usd_cap is None
    assert _s(landscape_daily_usd_cap=25.0).landscape_daily_usd_cap == 25.0
    for bad in (float("nan"), float("inf"), 0, -1):
        with pytest.raises(ValidationError):
            _s(landscape_daily_usd_cap=bad)


def test_chat_subcap_guard(caplog):
    assert _s().landscape_chat_daily_usd_limit == 50.0
    with pytest.raises(ValidationError):
        _s(landscape_chat_daily_usd_limit=float("nan"))
    with caplog.at_level(logging.WARNING):
        s = _s(landscape_chat_daily_usd_limit=0)
    assert s.landscape_chat_daily_usd_limit == 50.0
    assert "LANDSCAPE_CHAT_DAILY_USD_LIMIT" in caplog.text


def test_rerank_lock_key_per_epoch():
    a, b = uuid.uuid4(), uuid.uuid4()
    ka, kb = al.landscape_rerank_lock_key(a), al.landscape_rerank_lock_key(b)
    assert ka != kb
    assert ka == al.fixed_key(f"landscape_rerank:{a}")
    distinct = [al.ENGINE_LOCK_KEY, al.WORKER_LOCK_KEY, al.ADMIN_INVARIANT_LOCK_KEY,
                al.HUB_ROSTER_LOCK_KEY, al.LANDSCAPE_WORKER_LOCK_KEY, _SPEND_LOCK_KEY]
    assert ka not in distinct and kb not in distinct
