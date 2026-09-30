import pytest

from src.agent.engine.headlines import should_announce


@pytest.mark.parametrize("kw,expected", [
    (dict(trigger="capture", already_announced=False, terminal=True), True),
    (dict(trigger="capture", already_announced=False, terminal=False), False),
    (dict(trigger="capture", already_announced=False, terminal=True, queued=True), False),
    (dict(trigger="capture", already_announced=True, terminal=True), False),
    (dict(trigger="thread-close", already_announced=False), True),
    (dict(trigger="thread-close", already_announced=True), False),
    (dict(trigger="shutdown", already_announced=False, end_class="TODAY"), True),
    (dict(trigger="shutdown", already_announced=False, end_class="FINALIZE"), True),
    (dict(trigger="shutdown", already_announced=False, end_class="HOLD", interview_ended=False), False),
    (dict(trigger="shutdown", already_announced=False, end_class="HOLD", interview_ended=True), True),
    (dict(trigger="finalize", already_announced=False), True),
    (dict(trigger="finalize", already_announced=True), False),
])
def test_policy_table(kw, expected):
    assert should_announce(**kw) is expected


def test_unknown_trigger_and_class_raise():
    with pytest.raises(ValueError):
        should_announce(trigger="whenever", already_announced=False)
    with pytest.raises(ValueError):
        should_announce(trigger="shutdown", already_announced=False, end_class="SOMETIMES")


def test_policy_has_one_owner():
    """S1-08: every announce decision in the engine goes through should_announce."""
    from pathlib import Path

    engine_dir = Path("src/agent/engine")
    for name in ("verdicts.py", "threads.py"):
        src = (engine_dir / name).read_text(encoding="utf-8")
        assert "should_announce(" in src, name
        assert ".announced" not in src, f"{name} still reads _HeldVerdict.announced"
    assert "announced:" not in (engine_dir / "helpers.py").read_text(encoding="utf-8")
