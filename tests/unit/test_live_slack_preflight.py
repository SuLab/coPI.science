"""The live-Slack tier's isolation preflight must REFUSE, not warn.

`scripts/live_slack_preflight.py` is the only thing standing between the `live_slack`
tier and **production** Slack, in a checkout whose `.env` holds live production bot
tokens and a live app-config token pair. Its contract is therefore inverted from a
normal check: every path that cannot be *proved* safe is a refusal (exit non-zero), and
nothing it prints may contain a credential.

These tests drive the four checks the plan specifies
(`docs/plans/2026-09-04-close-remaining-gaps.md` Task 2 Step 2) through injected fakes —
no Slack call, no `.env` read, no network.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.live_slack_preflight import (
    COPI_TEST_TEAM_ID,
    FIXTURE_TOKEN_KEYS,
    PRODUCTION_CREDENTIAL_KEYS,
    TIER_REQUIRED_KEYS,
    check_no_operator_supplied_database,
    check_profiles_dir_writable,
    production_credential_keys,
    production_token_map,
    refusals,
    run_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# A real, writable directory for the tests that don't care about check 6 -- set by the
# autouse fixture below (from pytest's tmp_path, so it is cleaned up automatically)
# rather than an import-time tempfile.mkdtemp() that would leak on every test run. The
# 20-odd call sites that only exercise checks 1-5 read this module global instead of
# each plumbing their own tmp_path through; tests that DO care about check 6 pass
# their own tmp_path.
_DEFAULT_TEST_PROFILES_DIR: Path


@pytest.fixture(autouse=True)
def _default_test_profiles_dir(tmp_path):
    global _DEFAULT_TEST_PROFILES_DIR
    _DEFAULT_TEST_PROFILES_DIR = tmp_path / "default-profiles"
    _DEFAULT_TEST_PROFILES_DIR.mkdir()

# Fabricated, token-shaped, and deliberately not real. Nothing in this file may hold a
# credential, and nothing the preflight prints may echo one back.
FIXTURE_TOKENS = {
    "SLACK_TEST_BOT_TOKEN_SU": "xoxb-fixture-su-0000",
    "SLACK_TEST_BOT_TOKEN_CRAVATT": "xoxb-fixture-cravatt-0000",
    "SLACK_TEST_BOT_TOKEN_WISEMAN": "xoxb-fixture-wiseman-0000",
}

ISOLATED_ENV = {
    # Present AND empty: an absent var falls through to `.env`, an empty one overrides it.
    **{k: "" for k in PRODUCTION_CREDENTIAL_KEYS},
    "SLACK_TEST_WORKSPACE": "1",
    "SLACK_TEST_PI_USER_ID": "U0FIXTUREPI",
    **FIXTURE_TOKENS,
}

# What `Settings.get_slack_tokens()` returns once the production keys are blanked.
NO_PROD_TOKENS = {"su": "", "cravatt": "", "wiseman": "", "grantbot": ""}


def _no_env_token(agent_id: str) -> str | None:
    return None


def _copi_test_auth(token: str) -> dict:
    return {"ok": True, "team_id": COPI_TEST_TEAM_ID}


def _checks(env=None, tokens=None, env_token=None, auth_test=None, profiles_dir=None):
    return run_checks(
        env=ISOLATED_ENV if env is None else env,
        tokens=NO_PROD_TOKENS if tokens is None else tokens,
        env_token=env_token or _no_env_token,
        auth_test=auth_test or _copi_test_auth,
        profiles_dir=_DEFAULT_TEST_PROFILES_DIR if profiles_dir is None else profiles_dir,
    )


def _why(results) -> str:
    return "\n".join(f"[{'ok' if c.ok else 'REFUSE'}] {c.name}: {c.detail}" for c in results)


# --- the positive control: a proven-isolated environment must pass ------------------


def test_a_fully_isolated_environment_passes_every_check():
    results = _checks()
    assert refusals(results) == [], _why(results)
    assert len(results) >= 6, _why(results)


def test_the_four_checks_run_in_the_order_the_plan_specifies():
    names = [c.name for c in _checks()]
    assert names[0].startswith("1."), names
    assert names[1].startswith("2."), names
    assert names[2].startswith("3."), names
    assert names[3].startswith("4."), names


# --- check 1: the four production credentials -------------------------------------


def test_refuses_when_a_production_token_is_still_set():
    env = {**ISOLATED_ENV, "SLACK_BOT_TOKEN_CRAVATT": "xoxb-not-blanked-at-all"}
    results = _checks(env=env)
    bad = refusals(results)
    assert bad, "a non-blanked production bot token must be a refusal"
    assert any("SLACK_BOT_TOKEN_CRAVATT" in c.detail for c in bad), _why(results)


def test_refuses_when_the_config_token_pair_is_still_set():
    for key in ("SLACK_CONFIG_TOKEN", "SLACK_CONFIG_REFRESH_TOKEN"):
        results = _checks(env={**ISOLATED_ENV, key: "xoxe.xoxp-not-blanked"})
        assert any(key in c.detail for c in refusals(results)), _why(results)


def test_refuses_when_a_production_key_is_merely_absent():
    # The whole reason the check is "present AND empty": an absent env var falls
    # through to `.env`, which holds the live value. Absent is NOT isolated.
    env = {k: v for k, v in ISOLATED_ENV.items() if k != "SLACK_BOT_TOKEN_WISEMAN"}
    results = _checks(env=env)
    bad = refusals(results)
    assert bad, "an absent production key must be a refusal, not a pass"
    assert any("SLACK_BOT_TOKEN_WISEMAN" in c.detail for c in bad), _why(results)


def test_never_echoes_a_production_token_value():
    secret = "xoxb-9999-do-not-print-me"
    results = _checks(env={**ISOLATED_ENV, "SLACK_BOT_TOKEN_CRAVATT": secret})
    printed = _why(results)
    assert secret not in printed
    assert "do-not-print-me" not in printed


# --- check 2: nothing resolves a usable token, `.env` fallback included -----------


def test_refuses_when_settings_still_resolves_a_usable_bot_token():
    results = _checks(tokens={**NO_PROD_TOKENS, "lotz": "xoxb-leaked-through-dotenv"})
    bad = refusals(results)
    assert bad, "a usable token from get_slack_tokens() must be a refusal"
    assert any("lotz" in c.detail for c in bad), _why(results)
    assert "xoxb-leaked-through-dotenv" not in _why(results)


def test_a_placeholder_token_is_not_treated_as_usable():
    # `xoxb-placeholder` is the project's recognised no-op value for seeded rows
    # (src/services/slack_tokens.is_valid_token); it grants nothing, so it must not
    # make the preflight refuse a genuinely isolated environment.
    results = _checks(tokens={**NO_PROD_TOKENS, "lotz": "xoxb-placeholder"})
    assert refusals(results) == [], _why(results)


def test_refuses_when_env_token_still_resolves_for_any_agent():
    results = _checks(env_token=lambda agent_id: "xoxb-still-resolvable")
    bad = refusals(results)
    assert bad, "env_token() returning a token must be a refusal"
    assert any("env_token" in c.detail for c in bad), _why(results)


# --- check 3: every fixture token must be copi-test, proved by Slack --------------


def test_refuses_when_a_fixture_token_resolves_to_a_foreign_team():
    def foreign(token: str) -> dict:
        if "wiseman" in token:
            return {"ok": True, "team_id": "T0PRODUCTIONXX"}
        return {"ok": True, "team_id": COPI_TEST_TEAM_ID}

    results = _checks(auth_test=foreign)
    bad = refusals(results)
    assert bad, "a fixture token on another workspace must be a refusal"
    assert any("SLACK_TEST_BOT_TOKEN_WISEMAN" in c.detail for c in bad), _why(results)
    assert any("T0PRODUCTIONXX" in c.detail for c in bad), _why(results)


def test_refuses_when_auth_test_returns_not_ok():
    results = _checks(auth_test=lambda token: {"ok": False, "error": "invalid_auth"})
    bad = refusals(results)
    assert bad, "auth.test failing must be a refusal, not an assumption the token is fine"
    assert any("invalid_auth" in c.detail for c in bad), _why(results)


def test_refuses_when_auth_test_is_unreachable():
    def unreachable(token: str) -> dict:
        raise OSError("All connection attempts failed")

    results = _checks(auth_test=unreachable)
    bad = refusals(results)
    assert bad, "an unreachable Slack must be a refusal (fail closed), not a traceback"
    assert any("OSError" in c.detail for c in bad), _why(results)


def test_refuses_when_auth_test_answers_ok_with_no_team_id():
    results = _checks(auth_test=lambda token: {"ok": True})
    assert refusals(results), "a missing team_id cannot prove isolation"


def test_does_not_call_slack_from_an_environment_it_has_not_proved_isolated():
    calls = []

    def recording(token: str) -> dict:
        calls.append(token)
        return {"ok": True, "team_id": COPI_TEST_TEAM_ID}

    env = {**ISOLATED_ENV, "SLACK_CONFIG_TOKEN": "xoxe.xoxp-still-live"}
    results = _checks(env=env, auth_test=recording)
    assert calls == [], "checks 1-2 must gate the network call, so a leaky env talks to nobody"
    # And the un-run check is itself a refusal, never a silent pass.
    assert any(c.name.startswith("3.") and not c.ok for c in results), _why(results)


def test_refuses_rather_than_passing_vacuously_with_no_fixture_token_at_all():
    # "every token present resolves to copi-test" is trivially true of an empty set.
    # A check that proved nothing must not report OK.
    env = {k: v for k, v in ISOLATED_ENV.items() if k not in FIXTURE_TOKEN_KEYS}
    results = _checks(env=env)
    three = [c for c in results if c.name.startswith("3.")]
    assert three and not three[0].ok, _why(results)


def test_never_echoes_a_fixture_token_value():
    results = _checks()
    printed = _why(results)
    for value in FIXTURE_TOKENS.values():
        assert value not in printed


# --- check 4: the tier's own env, because a silent skip is the failure mode -------


def test_refuses_when_the_tier_env_is_incomplete():
    for key in TIER_REQUIRED_KEYS:
        env = {k: v for k, v in ISOLATED_ENV.items() if k != key}
        results = _checks(env=env)
        bad = refusals(results)
        assert bad, f"missing {key} must refuse: conftest would silently skip all 61 tests"
        assert any(key in c.detail for c in bad), _why(results)


def test_refuses_when_a_tier_variable_is_set_but_empty():
    # conftest.pytest_collection_modifyitems tests truthiness, so "" skips the tier
    # exactly as absence does.
    results = _checks(env={**ISOLATED_ENV, "SLACK_TEST_PI_USER_ID": ""})
    assert any("SLACK_TEST_PI_USER_ID" in c.detail for c in refusals(results))


def test_the_keys_it_checks_are_the_keys_conftest_reads():
    # If the fixture contract moves, this preflight must move with it, or it proves
    # isolation for an environment the tier does not actually use.
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text()
    for key in TIER_REQUIRED_KEYS:
        assert key in conftest, f"{key} is not read by tests/conftest.py"
    for key in FIXTURE_TOKEN_KEYS:
        stem = key.replace("SLACK_TEST_BOT_TOKEN_", "").lower()
        assert stem in conftest, f"no fixture in conftest for {key}"


# --- the token map check 2 actually reads, and the runner's blank list ------------


def _settings(**overrides):
    # `_env_file=None` so this never reads the checkout's .env: the point of these two
    # tests is the FIELD SET, and reading .env would put real values in a test process.
    from src.config import Settings

    return Settings(_env_file=None, **overrides)


def test_production_token_map_covers_the_field_get_slack_tokens_forgets():
    # get_slack_tokens() is a hand-written dict literal and omits `grantbot`
    # (deliberately — src/agent/simulation.py:4614). Grantbot posts into the production
    # workspace, so check 2 must still see that field, or a live grantbot token passes
    # every check this preflight makes.
    settings = _settings(slack_bot_token_grantbot="xoxb-grantbot-would-post-to-prod")
    assert "grantbot" not in settings.get_slack_tokens(), (
        "get_slack_tokens() now includes grantbot; production_token_map's reason to "
        "exist has changed — re-read it rather than deleting this test"
    )
    mapped = production_token_map(settings)
    assert mapped["grantbot"] == "xoxb-grantbot-would-post-to-prod"
    assert set(settings.get_slack_tokens()) <= set(mapped)

    results = run_checks(
        env=ISOLATED_ENV,
        tokens=mapped,
        env_token=_no_env_token,
        auth_test=_copi_test_auth,
        profiles_dir=_DEFAULT_TEST_PROFILES_DIR,
    )
    assert any("grantbot" in c.detail for c in refusals(results)), _why(results)


def test_the_blank_key_list_names_every_slack_credential_field_and_no_values():
    keys = production_credential_keys()
    expected = {
        name.upper()
        for name in type(_settings()).model_fields
        if name.startswith("slack_bot_token_")
        or name in ("slack_config_token", "slack_config_refresh_token")
    }
    assert expected <= set(keys), sorted(expected - set(keys))
    assert set(PRODUCTION_CREDENTIAL_KEYS) <= set(keys)
    assert "SLACK_BOT_TOKEN_GRANTBOT" in keys
    # Names only. A value would be a leak, and `=` is how one would sneak in.
    assert all(k == k.upper() and "=" not in k for k in keys)
    assert all(k.startswith("SLACK_") for k in keys)


def test_the_runner_blanks_the_derived_key_list_rather_than_a_hardcoded_four():
    text = _runner()
    assert "--print-blank-keys" in text
    assert 'export "${key}="' in text, "the runner must export each key EMPTY, not unset it"


# --- the runner refuses to start on a non-zero preflight -------------------------


def _runner() -> str:
    return (REPO_ROOT / "scripts" / "run_live_slack.sh").read_text()


def test_the_runner_never_sources_dotenv():
    text = _runner()
    for forbidden in ("source .env", ". .env", "source $REPO_ROOT/.env", "dotenv"):
        assert forbidden not in text, f"run_live_slack.sh must never source .env ({forbidden!r})"


def test_the_runner_aborts_before_pytest_when_the_preflight_refuses():
    # End to end, in a deliberately-bad environment: no tier credentials at all, so
    # check 4 refuses and the runner must never reach pytest. Offline by construction
    # -- check 3 is gated behind 1-2 and there are no fixture tokens to test anyway.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "VENV_PY": sys.executable,
    }
    proc = subprocess.run(
        ["bash", "scripts/run_live_slack.sh"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "REFUS" in out.upper(), out
    assert "= test session starts" not in out, f"pytest must never start: {out}"

# ---------------------------------------------------------------------------
# Check 5 — an operator-supplied database is an unbounded token source.
# Added after an audit of the first live run found that checks 1-2 bound only the
# ENVIRONMENT, while AgentRegistry.slack_bot_token is DB-first and authoritative
# (CLAUDE.md) and _sync_roster_from_db re-reads it every ~30s. That run was safe only
# because agent_registry.agent_id is UNIQUE and the fixtures would have collided with
# any pre-existing roster -- a schema accident, not a control.
# ---------------------------------------------------------------------------


def test_an_unset_test_database_url_passes_check_five():
    check = check_no_operator_supplied_database({})
    assert check.ok
    assert "empty agent_registry" in check.detail


def test_an_operator_supplied_database_is_refused():
    check = check_no_operator_supplied_database(
        {"TEST_DATABASE_URL": "postgresql+asyncpg://copi:copi@127.0.0.1:55434/copi_verify"}
    )
    assert not check.ok
    assert "AgentRegistry.slack_bot_token" in check.detail


def test_check_five_never_echoes_a_dsn_password():
    check = check_no_operator_supplied_database(
        {"TEST_DATABASE_URL": "postgresql+asyncpg://someuser:hunter2@db.internal:5432/prod"}
    )
    assert not check.ok
    assert "hunter2" not in check.detail
    assert "someuser" not in check.detail
    assert "<redacted>" in check.detail


def test_check_five_gates_check_three_so_no_token_is_sent():
    """A leaky database must stop the tier BEFORE any auth.test call, exactly as a
    leaky environment does -- an un-run check is a refusal, never a pass."""
    called: list[str] = []

    def _auth_test(token: str):
        called.append(token)
        return {"ok": True, "team_id": COPI_TEST_TEAM_ID}

    env = {k: "" for k in PRODUCTION_CREDENTIAL_KEYS}
    env.update(dict.fromkeys(TIER_REQUIRED_KEYS, "x"))
    env["SLACK_TEST_BOT_TOKEN_SU"] = "xoxb-test"
    env["TEST_DATABASE_URL"] = "postgresql://x/y"

    results = run_checks(
        env=env,
        tokens={},
        env_token=lambda _a: None,
        auth_test=_auth_test,
        profiles_dir=_DEFAULT_TEST_PROFILES_DIR,
    )

    assert called == [], "a token was sent to Slack despite an operator-supplied database"
    by_name = {c.name[0]: c for c in results}
    assert not by_name["5"].ok
    assert not by_name["3"].ok and "NOT ATTEMPTED" in by_name["3"].detail


# ---------------------------------------------------------------------------
# Check 6 (audit 2026-09-08 RC-13) — the resolved profiles/{public,private,memory}
# directories must be writable by the current uid before the tier (or any agent code)
# tries to write a profile there. RC-7's silent-clobber bug was triggered by exactly
# this: a permission-denied temp-file write that got swallowed instead of refused up
# front. Must run BEFORE any Slack call, same as checks 1/2/5.
# ---------------------------------------------------------------------------


def test_check_six_creates_and_accepts_a_writable_profiles_dir(tmp_path):
    target = tmp_path / "profiles"
    target.mkdir()
    check = check_profiles_dir_writable(target)
    assert check.ok, check.detail
    for sub in ("public", "private", "memory"):
        assert (target / sub).is_dir(), f"{sub} was not created"


def test_check_six_refuses_a_nonexistent_profiles_dir_with_the_exact_remedy(tmp_path):
    """A mistyped/nonexistent COPI_PROFILES_DIR (audit 2026-09-08, opus review): the
    old behaviour created `profiles_dir` itself via `mkdir(parents=True)`, which
    silently created an arbitrary directory tree instead of refusing a typo. Only the
    three subdirs are created; profiles_dir itself must already exist."""
    target = tmp_path / "typo-ed-profiles-dir"
    assert not target.exists()
    check = check_profiles_dir_writable(target)
    assert not check.ok
    assert not target.exists(), "a mistyped profiles_dir must not be silently created"
    assert "chown" in check.detail
    assert "COPI_PROFILES_DIR" in check.detail


def test_check_six_quotes_a_profiles_dir_containing_a_space_in_the_remedy(tmp_path):
    """R-5 (audit 2026-09-10): the remedy interpolates `profiles_dir` raw into a
    `sudo chown -R ... <dir>` suggestion. Unquoted, a path with a space (or shell
    metacharacters) renders as a copy-paste line that runs `chown` against the wrong
    (truncated) path, or worse. `shlex.quote` makes the suggested command safe to
    paste regardless of what's in the path."""
    import shlex

    target = tmp_path / "typo-ed profiles dir"
    assert not target.exists()
    check = check_profiles_dir_writable(target)
    assert not check.ok
    quoted = shlex.quote(str(target))
    assert f"chown -R $(id -u):$(id -g) {quoted}" in check.detail, check.detail
    # The unquoted path is fine to appear elsewhere (e.g. the "does not exist"
    # prefix) — only the copy-paste `chown` command itself must be quoted.
    assert f"chown -R $(id -u):$(id -g) {target}" not in check.detail


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignores directory permission bits, so the read-only probe can't fail",
)
def test_check_six_refuses_a_read_only_profiles_dir_with_the_exact_remedy(tmp_path):
    target = tmp_path / "profiles"
    target.mkdir()
    target.chmod(stat.S_IREAD | stat.S_IEXEC)  # r-x: cannot create subdirectories
    try:
        check = check_profiles_dir_writable(target)
    finally:
        target.chmod(stat.S_IRWXU)  # restore so tmp_path cleanup can remove it
    assert not check.ok
    assert "chown" in check.detail
    assert "COPI_PROFILES_DIR" in check.detail


def test_check_six_runs_before_check_threes_slack_call(monkeypatch):
    """Actually pins call ORDER (not just that both eventually ran), by recording
    each into a shared list: `check_profiles_dir_writable` (check 6, real -- via the
    module attribute run_checks calls through) must be recorded before `auth_test`
    (check 3's Slack call, injected)."""
    import scripts.live_slack_preflight as m

    call_order: list[str] = []

    real_check_six = m.check_profiles_dir_writable

    def _spy_check_six(profiles_dir):
        call_order.append("check_6")
        return real_check_six(profiles_dir)

    def _auth_test(token: str):
        call_order.append("check_3_auth_test")
        return {"ok": True, "team_id": COPI_TEST_TEAM_ID}

    monkeypatch.setattr(m, "check_profiles_dir_writable", _spy_check_six)

    m.run_checks(
        env=ISOLATED_ENV,
        tokens=NO_PROD_TOKENS,
        env_token=_no_env_token,
        auth_test=_auth_test,
        profiles_dir=_DEFAULT_TEST_PROFILES_DIR,
    )

    assert "check_6" in call_order and "check_3_auth_test" in call_order, call_order
    assert call_order.index("check_6") < call_order.index("check_3_auth_test"), (
        f"check 6 must run before check 3's Slack call; observed order: {call_order}"
    )


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignores directory permission bits, so the read-only probe can't fail",
)
def test_check_six_refusal_is_visible_alongside_an_otherwise_isolated_environment(tmp_path):
    read_only = tmp_path / "preflight-readonly"
    read_only.mkdir()
    read_only.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        results = _checks(profiles_dir=read_only)
    finally:
        read_only.chmod(stat.S_IRWXU)  # restore so cleanup can remove it

    by_name = {c.name[0]: c for c in results}
    assert not by_name["6"].ok, _why(results)
