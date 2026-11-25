"""The S9 switch (src/landscaping/s9_switch.py, ``LANDSCAPE_FINAL_ENABLED``)."""
from src.config import Settings, get_settings
from src.landscaping.eval import regression
from src.landscaping.s9_switch import final_enabled


def test_on_by_default():
    assert Settings(_env_file=None).landscape_final_enabled is True
    assert Settings(_env_file=None, landscape_final_enabled=False).landscape_final_enabled is False
    assert final_enabled() is True


def test_off_reads_the_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "landscape_final_enabled", False)
    assert final_enabled() is False


def test_regression_checks_no_s9_only_kind_when_off(monkeypatch):
    on = regression.kinds_to_check(None, first_epoch=False)
    assert {"final", "expert"} <= set(on)
    assert regression.kinds_to_check(["final"], first_epoch=False) == ["final", "expert"]
    monkeypatch.setattr(get_settings(), "landscape_final_enabled", False)
    assert regression.kinds_to_check(None, first_epoch=False) == [
        k for k in on if k not in ("final", "expert")
    ]
    assert regression.kinds_to_check(["final"], first_epoch=False) == []
    assert regression.kinds_to_check(["deep", "final"], first_epoch=False) == ["deep"]
