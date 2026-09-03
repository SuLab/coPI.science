"""export_private_profile falls back to private_profile_seed (issue #22 COR-23)."""

from types import SimpleNamespace

from src.services.profile_export import export_private_profile


def test_exports_the_seed_when_there_is_no_live_private_profile(tmp_path, monkeypatch):
    import src.services.profile_export as pe
    monkeypatch.setattr(pe, "PRIVATE_PROFILES_DIR", tmp_path)

    user = SimpleNamespace(name="Test PI")
    profile = SimpleNamespace(private_profile_md=None, private_profile_seed="SEED CONTENT")

    path = export_private_profile(user, profile, "testagent")
    assert path is not None
    assert path.read_text(encoding="utf-8") == "SEED CONTENT\n"


def test_prefers_the_live_profile_over_the_seed_when_both_exist(tmp_path, monkeypatch):
    import src.services.profile_export as pe
    monkeypatch.setattr(pe, "PRIVATE_PROFILES_DIR", tmp_path)

    user = SimpleNamespace(name="Test PI")
    profile = SimpleNamespace(private_profile_md="LIVE CONTENT", private_profile_seed="SEED CONTENT")

    path = export_private_profile(user, profile, "testagent")
    assert path.read_text(encoding="utf-8") == "LIVE CONTENT\n"


def test_returns_none_when_neither_exists(tmp_path, monkeypatch):
    import src.services.profile_export as pe
    monkeypatch.setattr(pe, "PRIVATE_PROFILES_DIR", tmp_path)

    user = SimpleNamespace(name="Test PI")
    profile = SimpleNamespace(private_profile_md=None, private_profile_seed=None)

    assert export_private_profile(user, profile, "testagent") is None
