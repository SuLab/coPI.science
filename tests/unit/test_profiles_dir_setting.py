"""`Settings.profiles_dir` (audit 2026-09-08 RC-13).

Root cause: `PROFILES_DIR = Path("profiles")` (src/agent/agent.py) and the equivalent
literals in src/agent/tools.py, src/routers/agent_page.py and
src/services/profile_export.py are CWD-relative literals with nothing configurable. On
a host where `profiles/` is root-owned (see CLAUDE.md's UID 10001 precondition), a
disk write under one of these paths fails -- silently, in the case RC-7 closes -- and
the live tier's preflight said nothing about it.

This test covers only the setting itself (env var `COPI_PROFILES_DIR`, default
unchanged); the per-file constant conversions are covered by each file's own test
suite (unchanged behaviour) plus tests/unit/test_live_slack_preflight.py's new check 6
coverage.
"""

from pathlib import Path

from src.config import Settings, get_settings


def test_profiles_dir_defaults_to_profiles():
    s = Settings(_env_file=None)
    assert s.profiles_dir == "profiles"


def test_profiles_dir_honors_copi_profiles_dir_env_var(monkeypatch):
    monkeypatch.setenv("COPI_PROFILES_DIR", "/var/lib/copi/profiles")
    s = Settings(_env_file=None)
    assert s.profiles_dir == "/var/lib/copi/profiles"


def test_profiles_dir_accepts_a_direct_kwarg(monkeypatch):
    monkeypatch.setenv("COPI_PROFILES_DIR", "/tmp/custom-profiles")
    s = Settings(_env_file=None)
    assert s.profiles_dir == "/tmp/custom-profiles"


def test_profiles_dir_field_name_is_not_a_second_env_var(monkeypatch):
    """`populate_by_name=True` on the model would also accept the bare field
    name (`PROFILES_DIR`) as an environment source alongside the intended
    `COPI_PROFILES_DIR` alias. Only the alias should be honoured."""
    monkeypatch.setenv("PROFILES_DIR", "/oops")
    s = Settings(_env_file=None)
    assert s.profiles_dir == "profiles"


def test_agent_profiles_dir_accessor_honors_a_later_env_var_change(monkeypatch):
    """REV3-6 (opus review, audit 2026-09-08): PROFILES_DIR used to be resolved
    from get_settings() once, at import time -- a later COPI_PROFILES_DIR +
    get_settings.cache_clear() (exactly what scripts/live_slack_preflight.py's
    runtime check does) had no effect on the module constant every path
    builder actually used. `_profiles_dir()` re-reads get_settings() on every
    call, so it is not stuck with the import-time snapshot."""
    import src.agent.agent as agent_module

    assert agent_module.PROFILES_DIR is None, (
        "the module constant must stay unset by default so the accessor's "
        "'not overridden' branch actually resolves live -- tests that "
        "monkeypatch PROFILES_DIR directly are the only thing that should "
        "make it non-None"
    )

    monkeypatch.setenv("COPI_PROFILES_DIR", "/tmp/rev3-6-profiles")
    get_settings.cache_clear()
    try:
        assert agent_module._profiles_dir() == Path("/tmp/rev3-6-profiles")
    finally:
        get_settings.cache_clear()


def test_a_monkeypatched_profiles_dir_still_wins_over_the_live_setting(monkeypatch):
    """The accessor pattern must not regress the many existing tests that
    monkeypatch PROFILES_DIR directly to a tmp_path -- an explicit override
    takes priority over whatever get_settings() would otherwise resolve."""
    import src.agent.agent as agent_module

    monkeypatch.setattr(agent_module, "PROFILES_DIR", Path("/explicit-override"))
    monkeypatch.setenv("COPI_PROFILES_DIR", "/tmp/should-be-ignored")
    get_settings.cache_clear()
    try:
        assert agent_module._profiles_dir() == Path("/explicit-override")
    finally:
        get_settings.cache_clear()
