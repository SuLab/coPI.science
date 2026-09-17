"""Pins that ci.sh gates on a mypy ceiling, mirroring the ruff src/ ratchet.
Same convention as tests/unit/test_cohort_isolation.py::TestMigrationHygiene
for the alembic gate: assert marker strings exist and precede pytest."""

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ci_sh() -> str:
    return (REPO_ROOT / "scripts" / "ci.sh").read_text()


def test_ci_sh_gates_on_a_mypy_ceiling_before_pytest():
    text = _ci_sh()
    assert "MYPY_MAX" in text
    assert "-m mypy" in text
    assert text.index("MYPY_MAX") < text.index("-m pytest")


def test_mypy_max_is_a_real_integer_not_a_placeholder():
    # Guards exactly the failure mode this task's own history hit: a literal
    # "<N>" (or any other non-numeric placeholder) left in MYPY_MAX's default
    # makes every `[ "$mypy_findings" -gt "$MYPY_MAX" ]` comparison in ci.sh
    # fail with "integer expression expected" on every push.
    text = _ci_sh()
    m = re.search(r'MYPY_MAX="\$\{MYPY_MAX:-([^}]+)\}"', text)
    assert m, "MYPY_MAX default not found in the expected ${VAR:-default} shape"
    assert m.group(1).isdigit(), f"MYPY_MAX default {m.group(1)!r} is not a plain integer"


def test_mypy_ceiling_records_its_provenance_and_the_numbers_agree():
    # A ceiling whose comment says "measured N" while the default says something
    # unrelated to N is not auditable, and it goes stale in silence: ci.sh's own
    # comment claimed "(commit 2170efb) ... 145 findings" while a clean export of
    # 2170efb measured 147 (the 145 had been read off a working tree carrying an
    # uncommitted fix). So pin the SHAPE, not the number — the block must name what
    # was measured, the commit and the mypy version it was measured at, and a
    # ceiling with its slack; and measured + slack must equal both the stated
    # ceiling and MYPY_MAX's actual default. Raising the default without
    # re-measuring then fails here instead of passing quietly.
    text = _ci_sh()
    default = re.search(r'MYPY_MAX="\$\{MYPY_MAX:-(\d+)\}"', text)
    assert default, "MYPY_MAX default not found in the expected ${VAR:-default} shape"
    assert "# PROVENANCE." in text, (
        "MYPY_MAX has no provenance block: state what was measured, at which commit "
        "and mypy version, and the ceiling with its slack — a bare number is a number "
        "the next reader cannot check or re-measure"
    )
    block = text[text.index("# PROVENANCE.") : default.start()]

    measured = re.search(r"^#\s+measured\s*:\s*(\d+) findings", block, re.M)
    at_commit = re.search(r"^#\s+at commit\s*:\s*([0-9a-f]{7,40})\b", block, re.M)
    with_mypy = re.search(r"^#\s+with\s*:\s*mypy (\d+\.\d+(?:\.\d+)?)", block, re.M)
    ceiling = re.search(r"^#\s+ceiling\s*:\s*(\d+) findings \(slack (\d+)\)", block, re.M)
    assert measured, f"the MYPY_MAX comment does not say what was measured:\n{block}"
    assert at_commit, f"the MYPY_MAX comment does not name the commit measured:\n{block}"
    assert with_mypy, f"the MYPY_MAX comment does not name the mypy version:\n{block}"
    assert ceiling, f"the MYPY_MAX comment does not state the ceiling and its slack:\n{block}"

    n_measured, n_ceiling, n_slack = (
        int(measured.group(1)),
        int(ceiling.group(1)),
        int(ceiling.group(2)),
    )
    assert n_ceiling == int(default.group(1)), (
        f"the comment states a ceiling of {n_ceiling} but MYPY_MAX defaults to "
        f"{default.group(1)}: re-measure and rewrite the provenance block together"
    )
    assert n_measured + n_slack == n_ceiling, (
        f"measured {n_measured} + slack {n_slack} != ceiling {n_ceiling}"
    )
    assert n_slack > 0, "a ceiling equal to the measured count goes red on the next honest fix"


def test_ci_sh_states_the_local_only_stance():
    text = _ci_sh()
    assert "CI is deliberately local-only" in text


def test_ci_sh_gates_the_stages_in_the_documented_order():
    # Structural check of the gate itself: the header's own ordering claims must
    # match what actually runs, and each stage must precede the full pytest run
    # (the expensive stage; nothing gates on a red gate having already spent the
    # ~7 minutes of pytest for nothing).
    text = _ci_sh()
    pytest_idx = text.index("-m pytest")
    for marker in ("alembic heads", "uniq -d", "ruff check src", "piptools compile", "-m mypy"):
        assert marker in text, f"missing gate stage marker: {marker!r}"
        assert text.index(marker) < pytest_idx, f"{marker!r} must run before pytest"


def test_mypy_ceiling_ratchet_has_the_same_shape_as_the_ruff_ratchet():
    # SRC_LINT_MAX and MYPY_MAX are both ceilings that can only go down, never up —
    # pin that both declarations use the identical ${VAR:-N} bash-default idiom so
    # neither can silently regress into an unconditional assignment that ignores an
    # operator override.
    text = _ci_sh()
    assert re.search(r'SRC_LINT_MAX="\$\{SRC_LINT_MAX:-\d+\}"', text)
    assert re.search(r'MYPY_MAX="\$\{MYPY_MAX:-\d+\}"', text)


# --- Behavioural pins: deleting ci.sh's `exit 1`s must not go unnoticed. ---
#
# These actually run scripts/ci.sh in a subprocess (same shape
# test_run_migration_sh.py uses for bash), with the expensive steps disabled
# (CI_MIGRATION_DB=none, LOCKCHECK=none) so only the cheap offline steps
# (alembic sanity, both ruff passes) and the mypy step itself run before we
# reach the assertion. mypy over src/ takes ~2-3s here, so this stays fast.


# Markers for ci.sh's opt-in lock smoke step (step 6), which is the expensive one:
# it installs requirements.lock with --require-hashes into a throwaway Python 3.11
# venv. SMOKE_STEP_RAN is the step's own success output; a nested gate run must never
# print it, because every nested run in this suite wants the step's default skip.
SMOKE_STEP_RAN = "PASS  requirements.lock installs"
SMOKE_STEP_SKIPPED = "skipped (opt-in"


# ci.sh reads ten knobs from the environment (LOCK_SMOKE, LOCKCHECK, LOCKCHECK_PYTHON,
# CI_MIGRATION_DB, MIGCHECK_PORT, MIGRATION_FLOOR, MYPY_MAX, SRC_LINT_MAX, COV_MIN,
# VENV_PY), so a nested run whose env is built as `{**os.environ, ...}` silently adopts
# whichever of them the operator happened to export. For example, with
# LOCK_SMOKE=1 in the parent shell: the child runs the real `uv pip install
# --require-hashes -r requirements.lock` — ~2 s on a warm uv cache, and long enough on a
# cold one to trip this file's own `timeout=180`.
#
# Hence an ALLOW-LIST and not a denylist of the knob names known today: a denylist is the
# same defect again the next time ci.sh grows a knob. The child gets what it needs to find
# its tools and nothing else. PATH and HOME alone are in fact sufficient here (verified:
# alembic, both ruff passes, the lockfile check, mypy and `docker info` all succeed under
# `env -i PATH=... HOME=... ./scripts/ci.sh`); the rest cover machines where they are not.
_NESTED_GATE_ENV_ALLOWLIST = (
    "PATH",  # bash, docker, uv, and the coreutils the gate shells out to
    "HOME",  # ~/.docker/config.json, uv's download cache, ruff/mypy caches
    "TMPDIR",  # ci.sh's own `mktemp -d` scratch dirs
    "DOCKER_HOST",  # ci.sh exits early unless `docker info` succeeds
    "XDG_RUNTIME_DIR",  # rootless Docker derives its default socket path from it
    "LANG",  # keep the operator's text encoding for the child's diagnostics
)


def nested_gate_env(**overrides) -> dict[str, str]:
    """Build the environment for a nested `./scripts/ci.sh` run from the allow-list.

    Any knob the nested run depends on is passed here explicitly, so what a test
    exercises cannot change with what the operator exported. `CI_MIGRATION_DB=none` is
    unconditional: no nested run in this suite wants the alembic round trip, which costs
    a throwaway Postgres container and a free host port.
    """
    env = {k: os.environ[k] for k in _NESTED_GATE_ENV_ALLOWLIST if k in os.environ}
    env["CI_MIGRATION_DB"] = "none"
    env.update(overrides)
    return env


# The banner ci.sh actually echoes when it starts the full pytest run. It is the
# marker to assert on: asserting `"-m pytest" not in proc.stdout` instead would be
# vacuous, since ci.sh never prints the command line it runs (no `set -x`) — that
# string is never in stdout regardless of whether the pytest step was reached.
# `"==> pytest" in output` is the only string that is actually present once the
# gate reaches the pytest step. Grep before you trust a not-in assertion.
PYTEST_BANNER = "==> pytest"


class GateRun(NamedTuple):
    """One nested `./scripts/ci.sh` run, stopped at the pytest banner."""

    output: str  # the child's stdout and stderr, interleaved in the order printed
    returncode: int | None  # None when the run was stopped early at the banner
    reached_pytest: bool


def run_gate_until_pytest(
    env: dict[str, str],
    *,
    cwd: Path = REPO_ROOT,
    timeout: float = 180.0,
    stop_marker: str | None = PYTEST_BANNER,
) -> GateRun:
    """Run the gate, streaming its output, and stop it the moment it announces pytest.

    Streaming rather than `subprocess.run(..., timeout=...)` is what makes "the gate
    stopped before pytest" a reachable assertion. pytest is the ~7-minute step, so a
    gate that WRONGLY reaches it outlives any capturing call's timeout: the call dies
    on `TimeoutExpired` before a single assertion executes, and the pin degrades into
    a wall clock that reports the wrong cause. Reading line by line turns the banner
    itself into the stop condition — a mutant is caught in seconds, by an assertion
    that names it. `timeout` stays as a watchdog for a gate that hangs before the
    banner, not as the mechanism.

    `stop_marker=None` reads the child to EOF instead, for the one caller that needs
    what the gate prints AFTER pytest. Use it only with a pytest step that has been
    stubbed out (see the gated-tier test below) — on a real run it waits the full
    ~8 minutes.
    """
    proc = subprocess.Popen(
        ["./scripts/ci.sh"],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    seen: list[str] = []
    reached_pytest = False
    stopped_early = False
    returncode: int | None = None
    try:
        for line in proc.stdout:
            seen.append(line)
            if PYTEST_BANNER in line:
                reached_pytest = True
            if stop_marker is not None and stop_marker in line:
                stopped_early = True
                break
        if not stopped_early:
            returncode = proc.wait(timeout=10)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        proc.stdout.close()
    return GateRun("".join(seen), returncode, reached_pytest)


def test_ci_sh_fails_when_mypy_findings_exceed_the_ceiling():
    # Mutation check on the `exit 1` in ci.sh's mypy-ceiling branch: with MYPY_MAX=0
    # the step must fail the gate, and must do it before pytest ever starts. Delete
    # that `exit 1` and this test fails on the banner assertion below in ~15 s.
    run = run_gate_until_pytest(nested_gate_env(LOCKCHECK="none", MYPY_MAX="0"))
    assert PYTEST_BANNER not in run.output, (
        "gate must stop at the mypy ceiling, not reach pytest — the mypy step's "
        f"`exit 1` is gone:\n{run.output}"
    )
    assert not run.reached_pytest, run.output
    assert run.returncode not in (0, None), run.output
    assert "rose to" in run.output
    assert SMOKE_STEP_RAN not in run.output, (
        "the nested gate ran the opt-in lock smoke step: it inherited LOCK_SMOKE=1 from "
        "the operator's shell instead of building its environment from the allow-list"
    )
    assert SMOKE_STEP_SKIPPED in run.output


def test_ci_sh_mypy_ceiling_positive_control_lets_the_gate_proceed():
    # Mirror-image control: a ceiling comfortably above today's finding count must
    # NOT trip, and the gate must keep going past the mypy step (into pytest, which
    # this test does not wait for — the helper stops the child the moment it sees the
    # banner, so this never pays pytest's ~7-minute cost). Without this control the
    # negative test above is satisfiable by a gate that dies before mypy for an
    # unrelated reason.
    run = run_gate_until_pytest(nested_gate_env(LOCKCHECK="none", MYPY_MAX="99999"))
    assert run.reached_pytest, f"gate never reached the pytest step:\n{run.output}"
    assert "rose to" not in run.output, (
        f"mypy ceiling tripped when it should not have:\n{run.output}"
    )
    assert SMOKE_STEP_RAN not in run.output, (
        "the nested gate ran the opt-in lock smoke step: it inherited LOCK_SMOKE=1 from "
        f"the operator's shell instead of building its environment from the allow-list:\n{run.output}"
    )
    assert SMOKE_STEP_SKIPPED in run.output, run.output


# --- The gate must say which tests it did not run. ---
#
# `ci.sh` runs `pytest tests/` with NO `-m` expression, so nothing is deselected:
# tests/conftest.py's `pytest_collection_modifyitems` adds a `skip` marker to every
# `live_slack` test when the workspace credentials are absent, and to every `live_api`
# test when `LIVE_API_TESTS` is unset. Both tiers therefore land inside the gate's own
# "N skipped" tally, indistinguishable from an ordinary skip. That silence lets a
# data-loss regression reach HEAD undetected when the test that catches it lives only
# in a gated tier that never ran.

GATED_TIER_BANNER = "==> gated tiers this gate did NOT run"

# marker -> the string the notice must name as the way to run that tier. Not a
# free-text hint: `scripts/run_live_slack.sh` is tracked (it refuses to start unless
# its preflight proves the production credentials are blanked), and naming anything
# else — a scratch path, a bare `pytest -m live_slack` — is how an operator ends up
# pointing the tier at the production workspace.
GATED_TIERS = {
    "live_slack": "scripts/run_live_slack.sh",
    "live_api": "LIVE_API_TESTS=1",
}

_TIER_COUNTS: dict[str, int] = {}


def measure_tier(marker: str) -> int:
    """Count a gated tier independently of how ci.sh counts it.

    `--collect-only` imports the test modules and stops; no fixture runs, so this
    needs no Slack workspace and no third-party API key, and it is safe in the
    ordinary offline suite. Cached because it costs ~6 s (the whole suite is
    collected either way) and three assertions below want the same two numbers.

    Deliberately a DIFFERENT method from ci.sh's: the gate counts node-id lines
    (`grep -c '::'`), this reads pytest's own "N/M tests collected" summary. Counting
    the same way twice would agree with the gate on a shared systematic error — a
    warnings-summary header repeating a node id, say — and prove nothing.
    """
    if marker not in _TIER_COUNTS:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-m", marker,
             "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, (
            f"collecting `-m {marker}` failed (exit {proc.returncode}); the marker may "
            f"have been renamed:\n{proc.stdout}{proc.stderr}"
        )
        summary = re.search(r"^(\d+)(?:/\d+)? tests? collected", proc.stdout, re.M)
        assert summary, f"pytest printed no collection summary for `-m {marker}`:\n{proc.stdout}"
        _TIER_COUNTS[marker] = int(summary.group(1))
    return _TIER_COUNTS[marker]


def _logical_lines(block: str) -> list[str]:
    r"""Fold bash `\` continuations, so a wrapped command is scanned as one line."""
    return re.sub(r"\\\n\s*", " ", block).splitlines()


def _tier_notice_block(text: str) -> str:
    assert GATED_TIER_BANNER in text, (
        "ci.sh never says which tiers it did not run. Both live tiers are skipped at "
        "collection and disappear into the gate's own skip count, so an operator "
        "reading a green gate cannot tell that ~100 tests were not executed"
    )
    return text[text.index(GATED_TIER_BANNER) :]


def test_ci_sh_names_every_gated_tier_and_how_to_run_it_after_pytest():
    text = _ci_sh()
    block = _tier_notice_block(text)
    assert text.index(GATED_TIER_BANNER) > text.index(PYTEST_BANNER), (
        "the notice must come AFTER the pytest step. Printed before it, ~8 minutes of "
        "test output sits between the notice and the operator's prompt, which is the "
        "same silence the notice exists to fix"
    )
    for marker, howto in GATED_TIERS.items():
        assert marker in block, f"the notice does not name the {marker} tier:\n{block}"
        assert howto in block, (
            f"the notice names the {marker} tier but not how to run it ({howto!r}):\n{block}"
        )


def test_the_gate_does_not_run_either_gated_tier_while_reporting_it():
    # The notice counts; it must not execute. `--collect-only` on both lines is the
    # pin: drop it and the gate starts posting into a real Slack workspace and calling
    # ORCID/NCBI/grants.gov on every push, from an environment that deliberately has no
    # credentials for either.
    block = _tier_notice_block(_ci_sh())
    # Commands the notice RUNS, not the how-to strings it prints, so the two cannot be
    # confused: an `echo` naming `pytest -m live_api` is the point of the step.
    counting = [
        line
        for line in _logical_lines(block)
        if "-m pytest" in line and not line.lstrip().startswith(("echo", "printf", "#"))
    ]
    assert counting, f"the notice does not count anything with pytest:\n{block}"
    for line in counting:
        assert "--collect-only" in line, (
            f"the notice RUNS a gated tier instead of collecting it: {line.strip()!r}"
        )
        assert "LIVE_API_TESTS" not in line and "SLACK_TEST_" not in line, (
            "the notice's counting command ungates the tier it is only supposed to "
            f"count: {line.strip()!r}"
        )


def test_the_gated_tier_counts_are_measured_by_the_gate_not_written_into_it():
    # A literal "61" in ci.sh is the same defect as the stale MYPY_MAX provenance
    # comment two steps above it: correct on the day it was typed, silently wrong the
    # first time somebody adds a test to the tier, and wrong in the direction that
    # under-reports what the gate skipped. So assert the real count appears NOWHERE in
    # the notice — the only way to pass is to measure it at run time.
    block = _tier_notice_block(_ci_sh())
    assert "--collect-only" in block, "the notice does not measure anything"
    for marker in GATED_TIERS:
        n = measure_tier(marker)
        assert not re.search(rf"(?<!\d){n}(?!\d)", block), (
            f"the {marker} count ({n}) is written into ci.sh. Count it at run time with "
            f"`pytest tests/ -m {marker} --collect-only -q` instead. (If {n} is a "
            f"coincidence — a date, an issue number — reword that text; do not leave a "
            f"number a reader will take for the count.)"
        )


def test_the_printed_gated_tier_counts_are_the_real_ones():
    # The end-to-end pin, and the only one that can catch a count that is measured but
    # measured WRONG (the wrong marker, a path that misses tests/live_api, a `grep -c`
    # that counts summary lines). It runs the real gate to completion with the pytest
    # step stubbed to a collection: PYTEST_ADDOPTS is prepended to ci.sh's own pytest
    # command line, so `--collect-only --no-cov` turns the ~8-minute step into a ~4 s
    # one that still exits 0 (--no-cov also disables --cov-fail-under, verified). Every
    # other step — alembic, both ruff passes, mypy, the notice itself — is the real one.
    run = run_gate_until_pytest(
        nested_gate_env(LOCKCHECK="none", PYTEST_ADDOPTS="--collect-only --no-cov -q"),
        stop_marker=None,
        timeout=600,
    )
    assert run.reached_pytest, f"the gate never reached the pytest step:\n{run.output}"
    assert run.returncode == 0, f"the nested gate exited {run.returncode}:\n{run.output}"
    assert GATED_TIER_BANNER in run.output, (
        f"a completed gate run printed no gated-tier notice:\n{run.output}"
    )
    for marker in GATED_TIERS:
        printed = re.search(rf"^\s*{marker}: (\d+) tests\b", run.output, re.M)
        assert printed, f"the gate printed no count for the {marker} tier:\n{run.output}"
        assert int(printed.group(1)) == measure_tier(marker), (
            f"the gate says {printed.group(1)} {marker} tests were not run; there are "
            f"actually {measure_tier(marker)}"
        )
