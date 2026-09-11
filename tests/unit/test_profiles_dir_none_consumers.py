"""PRIVATE_PROFILES_DIR/PROFILES_DIR default to None (a lazy-accessor
pattern), so two consumers must not read the module constants directly
instead of going through the accessor:

  * src/routers/onboarding.py's GET /onboarding/private-profile disk fallback
  * src/services/profile_pipeline.py's private-seed adoption/export logic

Neither test here monkeypatches the constant -- only COPI_PROFILES_DIR +
get_settings.cache_clear(), exactly like scripts/live_slack_preflight.py's
runtime check and test_profiles_dir_setting.py's coverage of the same
accessor. Reading the module constant directly raises TypeError
(`NoneType / str`); going through the accessor resolves under the
env-configured directory.
"""

from types import SimpleNamespace

import pytest

from src.config import get_settings


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    yield
    get_settings.cache_clear()


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Returns None for the first query (ResearcherProfile lookup, so the
    on-disk fallback branch is reached), then agent_reg for the second
    (AgentRegistry lookup)."""

    def __init__(self, agent_reg):
        self._agent_reg = agent_reg
        self._calls = 0

    async def execute(self, *args, **kwargs):
        self._calls += 1
        return _FakeResult(None if self._calls == 1 else self._agent_reg)


@pytest.mark.asyncio
async def test_onboarding_private_profile_disk_fallback_honors_copi_profiles_dir(
    tmp_path, monkeypatch
):
    import src.routers.onboarding as onboarding_router

    monkeypatch.setenv("COPI_PROFILES_DIR", str(tmp_path))
    get_settings.cache_clear()

    private_dir = tmp_path / "private"
    private_dir.mkdir()
    (private_dir / "testagent.md").write_text("ON DISK CONTENT", encoding="utf-8")

    captured = {}

    def _fake_template_response(request, name, context):
        captured.update(context)
        return SimpleNamespace(context=context)

    monkeypatch.setattr(
        onboarding_router.templates, "TemplateResponse", _fake_template_response
    )

    user = SimpleNamespace(id="u1", onboarding_complete=False)
    agent_reg = SimpleNamespace(agent_id="testagent")
    db = _FakeDB(agent_reg)
    request = SimpleNamespace()

    await onboarding_router.private_profile(request=request, db=db, current_user=user)

    assert captured["profile_content"] == "ON DISK CONTENT"
