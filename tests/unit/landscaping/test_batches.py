"""The batch protocol's pure parts: custom ids, result kinds, the transition table, and the
pricing gate that runs before any write (spec §7.1, D31, D32, D42)."""
import re
import uuid
from types import SimpleNamespace

import pytest

from src.landscaping import batches
from src.landscaping.llm_client import UnpricedModelError
from src.models.landscape import LandscapeScreen


def test_custom_id_matches_api_grammar():
    for seq, item_seq in ((1, 1), (42, 7), (2**63 - 1, 100_000)):
        cid = batches._custom_id(seq, item_seq)
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid)
        assert batches._parse_custom_id(cid) == (seq, item_seq)
    assert batches._parse_custom_id("other-request-1") is None


def _result(kind: str, stop_reason: str | None = None):
    if kind != "succeeded":
        return SimpleNamespace(type=kind)
    return SimpleNamespace(type=kind, message=SimpleNamespace(stop_reason=stop_reason))


def test_result_kinds_mapped():
    assert batches._outcome_kind(_result("succeeded", "refusal"))[0] == "refusal"
    assert batches._outcome_kind(_result("succeeded", "max_tokens"))[0] == "truncated"
    kind, message = batches._outcome_kind(_result("succeeded", "end_turn"))
    assert kind == "succeeded" and message is not None
    for kind in ("errored", "expired", "canceled"):
        assert batches._outcome_kind(_result(kind)) == (kind, None)
        assert kind in batches.OUTCOME_KINDS


def test_transition_table():
    model = "claude-sonnet-5"

    def t(kind, *, attempts=0, truncated_once=False):
        return batches.transition(
            kind, attempts=attempts, truncated_once=truncated_once, model=model
        )

    # First occurrence.
    assert t("errored") == {"status": "pending", "attempts": 1}
    assert t("expired") == {"status": "pending", "attempts": 1}
    assert t("canceled") == {"status": "pending"}
    assert t("truncated") == {"status": "pending", "truncated_once": True}
    assert t("refusal") == {"status": "pending", "fallback_from": model}
    # Second occurrence.
    assert t("errored", attempts=1) == {"status": "failed:errored"}
    assert t("expired", attempts=1) == {"status": "failed:expired"}
    assert t("canceled", attempts=1) == {"status": "pending"}
    assert t("truncated", truncated_once=True) == {"status": "failed:truncated"}
    assert t("refusal", attempts=1, truncated_once=True) == {
        "status": "pending", "fallback_from": model,
    }
    with pytest.raises(ValueError):
        t("succeeded")


async def test_submit_refuses_unpriced_model(monkeypatch):
    def _no_db():
        raise AssertionError("submit touched the database before the pricing check")

    def _no_client():
        raise AssertionError("submit reached the API before the pricing check")

    monkeypatch.setattr(batches, "_batches_client", _no_client)
    with pytest.raises(UnpricedModelError):
        await batches.submit(
            _no_db, stage="screen", row_model=LandscapeScreen,
            items=[(uuid.uuid4(), {"model": "claude-unknown", "max_tokens": 10})],
        )
