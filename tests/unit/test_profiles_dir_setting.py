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

from src.config import Settings


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
