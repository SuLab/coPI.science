#!/usr/bin/env python3
"""Prove the `live_slack` test tier cannot reach production Slack. Refuse if it can.

WHY THIS EXISTS
===============
`tests/` carries 61 tests marked `live_slack` that create channels, post messages and
open DMs against a **real** Slack workspace. The intended workspace is `copi-test`
(team ``T0BMVSBMEC8``). The same checkout's ``.env`` holds live **production** bot
tokens and a live app-config token pair, and ``src/config.Settings`` reads that file by
default — so "which workspace does the tier talk to?" is decided by the process
environment, silently, at import time. Until this script existed, the only thing
standing between that tier and production Slack was one session's ``/tmp`` script.

So this is a **refusal**, not a report. Every path that cannot be *proved* safe exits
non-zero:

* a check whose inputs are unavailable is a refusal, never a skip;
* ``auth.test`` erroring, timing out, or answering ``ok`` without a ``team_id`` is a
  refusal — an unreachable Slack is not evidence that a token is harmless;
* checks 1-2 (environment isolation) **gate** check 3 (the network call), so a token is
  never sent anywhere from an environment we have not already proved isolated.

THE FOUR CHECKS (order fixed by docs/plans/2026-09-04-close-remaining-gaps.md Task 2)
=====================================================================================
1. ``SLACK_BOT_TOKEN_CRAVATT``, ``SLACK_BOT_TOKEN_WISEMAN``, ``SLACK_CONFIG_TOKEN`` and
   ``SLACK_CONFIG_REFRESH_TOKEN`` are each **present in the environment and empty**.
   Presence is the load-bearing half: pydantic-settings prefers an env var over the
   ``.env`` file, but only if the var exists, so an *absent* var falls through to the
   live value. Measured, not assumed — with a scratch ``.env`` holding
   ``SLACK_BOT_TOKEN_LOTZ``, ``Settings().get_slack_tokens()["lotz"]`` resolved to the
   file's 25-character value when the var was absent and to ``""`` when it was present
   and empty.
2. ``Settings.get_slack_tokens()`` yields **zero** usable tokens and
   ``slack_tokens.env_token()`` returns ``None`` for every agent id. This is the
   backstop for check 1: it goes through the real settings object, so it also catches a
   production credential this script does not name (measured: **125**
   ``slack_bot_token_*`` fields) and any future one. The mapping is widened by
   ``production_token_map`` to the one field ``get_slack_tokens()`` omits — see there.
3. Every ``SLACK_TEST_BOT_TOKEN_{SU,CRAVATT,WISEMAN}`` that is present resolves, via
   Slack's own ``auth.test``, to team ``T0BMVSBMEC8``. Asking Slack is the only
   authority on which workspace a token belongs to; a variable *named* ``_TEST_`` is
   not evidence.
4. ``SLACK_TEST_WORKSPACE``, ``SLACK_TEST_PI_USER_ID`` and ``SLACK_TEST_BOT_TOKEN_SU``
   are set, because ``tests/conftest.py``'s ``pytest_collection_modifyitems`` skips the
   whole tier without them. A silent skip is what let #20 COR-1b through: the tier
   reported "skipped", nobody noticed, and the regression shipped.

NOTHING HERE PRINTS A CREDENTIAL
================================
Every message reports presence, absence, a length, an agent id or a team id — never a
token, and never a line out of ``.env``. Exceptions from the Slack call are reported by
type name only, so no library's error string can carry a value into a log.

USAGE
=====
``scripts/run_live_slack.sh`` runs this and aborts on a non-zero exit; run it directly
to audit an environment::

    PYTHONPATH=. .venv-test/bin/python scripts/live_slack_preflight.py

``--print-blank-keys`` prints the *names* (never values) of every production Slack
credential the runner must blank, derived from ``Settings``' own fields so a newly-added
agent cannot be missed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # importing src.config eagerly is not worth it for one annotation
    from src.config import Settings

SLACK_API = "https://slack.com/api"

#: The only workspace the live tier may ever touch: `copi-test`.
COPI_TEST_TEAM_ID = "T0BMVSBMEC8"

#: The four production credentials the plan requires proved blank (Global Constraints).
PRODUCTION_CREDENTIAL_KEYS = (
    "SLACK_BOT_TOKEN_CRAVATT",
    "SLACK_BOT_TOKEN_WISEMAN",
    "SLACK_CONFIG_TOKEN",
    "SLACK_CONFIG_REFRESH_TOKEN",
)

#: The probe bots `tests/conftest.py::slack_bot_tokens` reads, keyed by agent id.
FIXTURE_TOKEN_KEYS = (
    "SLACK_TEST_BOT_TOKEN_SU",
    "SLACK_TEST_BOT_TOKEN_CRAVATT",
    "SLACK_TEST_BOT_TOKEN_WISEMAN",
)

#: Without all three, `pytest_collection_modifyitems` skips every `live_slack` test.
TIER_REQUIRED_KEYS = (
    "SLACK_TEST_WORKSPACE",
    "SLACK_TEST_PI_USER_ID",
    "SLACK_TEST_BOT_TOKEN_SU",
)

AuthTest = Callable[[str], Mapping[str, object]]
EnvTokenFn = Callable[[str], str | None]


@dataclass(frozen=True)
class Check:
    """One numbered check's verdict. `detail` is printed, so it holds no secrets."""

    ok: bool
    name: str
    detail: str


def refusals(results: Iterable[Check]) -> list[Check]:
    """The checks that failed. A non-empty result means: do not run the tier."""
    return [c for c in results if not c.ok]


def _verdict(name: str, problems: list[str], evidence: list[str]) -> Check:
    if problems:
        return Check(ok=False, name=name, detail="; ".join(problems))
    return Check(ok=True, name=name, detail="; ".join(evidence))


def check_production_credentials_blanked(env: Mapping[str, str]) -> Check:
    """Check 1 — the four named credentials are present in `env` and empty."""
    problems: list[str] = []
    evidence: list[str] = []
    for key in PRODUCTION_CREDENTIAL_KEYS:
        if key not in env:
            problems.append(
                f"{key} is ABSENT from the environment, so pydantic-settings falls "
                "through to .env and the live value wins — export it as empty"
            )
        elif env[key] != "":
            problems.append(f"{key} is still SET (len={len(env[key])}) — it must be empty")
        else:
            evidence.append(f"{key} present and empty")
    return _verdict("1. production Slack credentials blanked", problems, evidence)


def check_no_usable_token_resolves(
    tokens: Mapping[str, str], env_token: EnvTokenFn
) -> Check:
    """Check 2 — `Settings.get_slack_tokens()` and `env_token()` yield nothing usable.

    Goes through the project's own `is_valid_token`, so "usable" means exactly what the
    rest of the code means by it: an `xoxb-` token that is not `xoxb-placeholder`.
    """
    from src.services.slack_tokens import is_valid_token

    problems: list[str] = []
    usable = sorted(aid for aid, tok in tokens.items() if is_valid_token(tok))
    if usable:
        problems.append(
            f"get_slack_tokens() still resolves {len(usable)} usable token(s) for "
            f"agent id(s) {', '.join(usable)} — blank those SLACK_BOT_TOKEN_* vars too"
        )
    resolvable = sorted(aid for aid in tokens if env_token(aid) is not None)
    if resolvable:
        problems.append(
            f"env_token() still resolves a token for agent id(s) {', '.join(resolvable)}"
        )
    return _verdict(
        "2. no usable production bot token resolves",
        problems,
        [f"0 usable of {len(tokens)} configured agent ids; env_token() None for all"],
    )


def check_fixture_tokens_are_copi_test(
    env: Mapping[str, str], auth_test: AuthTest, *, isolated: bool
) -> Check:
    """Check 3 — every present fixture token belongs to `copi-test`, per Slack.

    `isolated` is checks 1-2's combined verdict. When it is false this check refuses
    without calling Slack at all: an environment that still carries a production
    credential is not one to start sending tokens from, and an un-run check is a
    refusal, never a pass.
    """
    name = f"3. every fixture token resolves to {COPI_TEST_TEAM_ID} (copi-test)"
    if not isolated:
        return Check(
            ok=False,
            name=name,
            detail=(
                "NOT ATTEMPTED — checks 1-2 did not prove the environment isolated, so "
                "no token was sent to Slack. Fix those first; an un-run check refuses."
            ),
        )

    present = [k for k in FIXTURE_TOKEN_KEYS if env.get(k)]
    if not present:
        return Check(
            ok=False,
            name=name,
            detail=(
                "no SLACK_TEST_BOT_TOKEN_* is set, so there is nothing to prove — "
                "refusing rather than passing vacuously"
            ),
        )

    problems: list[str] = []
    evidence: list[str] = []
    for key in present:
        try:
            resp = auth_test(env[key])
        except Exception as exc:
            # Deliberately broad: a transport error, a timeout, a non-JSON body and a
            # JSON body that is not an object are all "we could not prove the
            # workspace", which is a refusal. Only the exception's TYPE NAME is
            # reported, so no library's error string can carry a token into a log.
            problems.append(
                f"{key}: auth.test raised {type(exc).__name__} — Slack unreachable is "
                "not evidence the token is safe"
            )
            continue
        if not resp.get("ok"):
            problems.append(f"{key}: auth.test error={resp.get('error') or 'unknown'}")
            continue
        team = resp.get("team_id")
        if not team:
            problems.append(f"{key}: auth.test returned ok with no team_id")
        elif team != COPI_TEST_TEAM_ID:
            problems.append(
                f"{key}: resolves to team {team}, NOT {COPI_TEST_TEAM_ID} — refusing"
            )
        else:
            evidence.append(f"{key}: {team} (len={len(env[key])})")

    for key in FIXTURE_TOKEN_KEYS:
        if key not in present:
            evidence.append(f"{key}: absent — fixtures needing that probe bot will skip")
    return _verdict(name, problems, evidence)


def check_tier_environment_complete(env: Mapping[str, str]) -> Check:
    """Check 4 — the three vars `tests/conftest.py` needs, or the tier silently skips."""
    problems: list[str] = []
    evidence: list[str] = []
    for key in TIER_REQUIRED_KEYS:
        value = env.get(key, "")
        if not value:
            problems.append(
                f"{key} is missing or empty — tests/conftest.py would skip all "
                "live_slack tests and report success"
            )
        else:
            evidence.append(f"{key} present (len={len(value)})")
    return _verdict("4. the tier's own environment is complete", problems, evidence)


def check_no_operator_supplied_database(env: Mapping[str, str]) -> Check:
    """Check 5 — the tier must build its own throwaway database, not reuse one.

    Checks 1-2 bound what the ENVIRONMENT can hand out, and that is not the whole
    surface: CLAUDE.md records `AgentRegistry.slack_bot_token` as the AUTHORITATIVE
    token source, ahead of `.env` and `get_slack_tokens()`, and the running engine
    re-reads it every ~30s in `_sync_roster_from_db` (`src/agent/simulation.py`). A
    roster row carrying a production `xoxb-` token therefore reaches Slack through a
    client the fixtures never built and the off-channel guard never sees, no matter how
    thoroughly the environment was blanked.

    An audit of the first live run found the blast radius was bounded only by accident:
    `agent_registry.agent_id` is UNIQUE, so the seeded fixtures would have collided with
    any pre-existing roster and errored out. That is a lucky schema constraint, not a
    control. So: refuse when `TEST_DATABASE_URL` is set at all, and let the suite spin
    its own container, whose roster is empty by construction.
    """
    dsn = env.get("TEST_DATABASE_URL", "")
    if not dsn:
        return _verdict(
            "5. no operator-supplied database",
            [],
            ["TEST_DATABASE_URL unset — the suite builds a throwaway Postgres with an "
             "empty agent_registry, so no DB-sourced bot token can exist"],
        )
    # Report the host/db shape only. A DSN can carry a password.
    shape = re.sub(r"//[^@/]*@", "//<redacted>@", dsn)
    return _verdict(
        "5. no operator-supplied database",
        [
            f"TEST_DATABASE_URL is set ({shape}) — refusing. AgentRegistry.slack_bot_token "
            "is DB-first and authoritative, so a roster row in that database would reach "
            "Slack through _sync_roster_from_db regardless of a blanked environment. "
            "Unset it and let the suite build its own."
        ],
        [],
    )


def check_profiles_dir_writable(profiles_dir: Path) -> Check:
    """Check 6 (audit 2026-09-08 RC-13) — the resolved
    `profiles/{public,private,memory}` directories exist and are writable by the
    current process, creating them if missing.

    Runs before any Slack call (see `run_checks`): a `profiles_dir` that turns out to
    be read-only is exactly the failure mode RC-7 traced to a silent DB clobber
    (`Agent.update_private_profile` swallowed the write error and the caller re-read
    the stale file), and the live tier should refuse up front rather than discover it
    mid-run with a PI told their instruction was saved when it was not.
    """
    problems: list[str] = []
    evidence: list[str] = []
    for sub in ("public", "private", "memory"):
        target = profiles_dir / sub
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".preflight-write-probe"
            probe.write_text("")
            probe.unlink()
            evidence.append(f"{target} is writable")
        except OSError as exc:
            problems.append(
                f"{target} is not writable ({exc.strerror or exc}). Fix with either: "
                f"`sudo chown -R $(id -u):$(id -g) {profiles_dir}` (or the deploy "
                "image's UID 10001 -- see CLAUDE.md's UID 10001 precondition), or "
                f"point elsewhere with `COPI_PROFILES_DIR=<a-writable-directory>`."
            )
    return _verdict("6. profiles/ is writable", problems, evidence)


def run_checks(
    *,
    env: Mapping[str, str],
    tokens: Mapping[str, str],
    env_token: EnvTokenFn,
    auth_test: AuthTest,
    profiles_dir: Path,
) -> list[Check]:
    """The six checks, in order. Every dependency is injected, so this is unit-testable
    without Slack, without `.env` and without a network.

    Check 5 gates check 3 alongside 1 and 2: an operator-supplied database is an
    unbounded token source, so it is not an environment to start sending tokens from.
    Check 6 (profiles/ writability) is not a Slack-isolation concern, so it does not
    gate check 3 -- but it is still computed before check 3's Slack call, same as 1, 2
    and 5, so a profiles/ problem is never masked by a network call that ran first."""
    one = check_production_credentials_blanked(env)
    two = check_no_usable_token_resolves(tokens, env_token)
    five = check_no_operator_supplied_database(env)
    six = check_profiles_dir_writable(profiles_dir)
    three = check_fixture_tokens_are_copi_test(
        env, auth_test, isolated=one.ok and two.ok and five.ok
    )
    four = check_tier_environment_complete(env)
    return [one, two, three, four, five, six]


def slack_auth_test(token: str) -> Mapping[str, object]:
    """`auth.test` for one token. Raises on anything that is not a parsed JSON body.

    The raise is deliberate: `check_fixture_tokens_are_copi_test` turns any exception
    into a refusal, and reports the exception's *type name only*, so no transport error
    string can carry a token into the output.
    """
    import httpx

    resp = httpx.post(
        f"{SLACK_API}/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    body = resp.json()
    if not isinstance(body, dict):
        raise TypeError("auth.test did not return a JSON object")
    return body


def production_token_map(settings: Settings) -> dict[str, str]:
    """Every production bot token this process could reach, keyed by agent id.

    `Settings.get_slack_tokens()` is the mapping the plan's check 2 names, and it is
    what the rest of the code uses — but it is a hand-maintained dict literal and it
    **omits `grantbot`** (measured: 125 `slack_bot_token_*` fields, 124 mapping entries;
    the omission is deliberate and documented at `simulation.py:4614`). Grantbot posts
    funding calls into the production workspace, so a live `SLACK_BOT_TOKEN_GRANTBOT`
    is exactly what this harness exists to keep out. Union the two, so check 2 covers
    every field and any future one the literal forgets.
    """
    tokens: dict[str, str] = dict(settings.get_slack_tokens())
    prefix = "slack_bot_token_"
    for name in type(settings).model_fields:
        if name.startswith(prefix):
            tokens.setdefault(name[len(prefix):], getattr(settings, name))
    return tokens


def production_credential_keys() -> list[str]:
    """Every env var name that can hand this process a production Slack credential.

    Derived from `Settings`' own fields rather than a hand-written list, so a PI added
    to `config.py` after this script was written is blanked too. `--print-blank-keys`
    prints these NAMES for the runner to export as empty; no value is ever read.
    """
    from src.config import Settings

    keys = {
        name.upper()
        for name in Settings.model_fields
        if name.startswith("slack_bot_token_")
        or name in ("slack_config_token", "slack_config_refresh_token")
    }
    keys.update(PRODUCTION_CREDENTIAL_KEYS)
    return sorted(keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--print-blank-keys",
        action="store_true",
        help="print the NAMES of the production Slack credential vars to blank, one per line",
    )
    args = parser.parse_args(argv)

    if args.print_blank_keys:
        for key in production_credential_keys():
            print(key)
        return 0

    from src.config import get_settings
    from src.services.slack_tokens import env_token

    settings = get_settings()
    results = run_checks(
        env=os.environ,
        tokens=production_token_map(settings),
        env_token=env_token,
        auth_test=slack_auth_test,
        profiles_dir=Path(settings.profiles_dir),
    )

    print(f"live-Slack preflight — the only permitted workspace is {COPI_TEST_TEAM_ID} (copi-test)")
    for check in results:
        print(f"  [{'  OK  ' if check.ok else 'REFUSE'}] {check.name}")
        print(f"           {check.detail}")

    bad = refusals(results)
    if bad:
        # Flush first: stderr is unbuffered and stdout is not when piped, so without
        # this the refusal line lands ABOVE the checks that explain it in any log.
        sys.stdout.flush()
        print(
            f"\nREFUSING to run the live Slack tier: {len(bad)} of {len(results)} checks "
            "could not prove isolation from production Slack.",
            file=sys.stderr,
        )
        return 1
    print("\nPREFLIGHT OK — isolation proved; the tier may run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
