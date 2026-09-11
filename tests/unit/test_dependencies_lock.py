"""Static checks: upper caps on version-sensitive deps, plotly out of the
runtime install, an alembic floor high enough for `if_exists=` in migration
downgrades, and a lockfile that exists and names every runtime dependency.
Does not attempt to re-validate pip-compile's own hash pinning — that's pip's
job at install time (--require-hashes)."""

import os
import re
import subprocess
import tomllib
from pathlib import Path

from tests.unit.test_ci_gate import SMOKE_STEP_RAN, SMOKE_STEP_SKIPPED, nested_gate_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def _dep_names(deps: list[str]) -> set[str]:
    return {re.split(r"[><=\[!~]", d, maxsplit=1)[0].strip() for d in deps}


def test_version_sensitive_deps_have_upper_caps():
    deps = {
        re.split(r"[><=\[!~]", d, maxsplit=1)[0].strip(): d
        for d in _pyproject()["project"]["dependencies"]
    }
    for name in ("fastapi", "sqlalchemy", "anthropic", "slack-sdk"):
        assert "<" in deps[name], f"{name} has no upper cap: {deps[name]!r}"
    # Cap the PRE-1.0 packages too, where a minor bump is a breaking change by
    # convention. These five sit on request-handling and database
    # paths (serving, every outbound call, the DB driver, form parsing, the CLI entry).
    for name in ("uvicorn", "httpx", "asyncpg", "python-multipart", "typer"):
        assert "<" in deps[name], f"pre-1.0 dep {name} has no upper cap: {deps[name]!r}"


def test_floors_clear_the_advisories_named_in_issue_27_i4():
    """Floors must raise past known CVEs: jinja2 <3.1.6 carries 6 advisories,
    python-multipart <0.0.31 carries 16, authlib <1.7.1 carries 22. The lock
    already resolves above all three, so these floors change nothing today —
    they stop a future resolve walking back into them."""
    from packaging.requirements import Requirement
    from packaging.version import Version

    deps = {
        Requirement(d).name.lower().replace("_", "-"): Requirement(d)
        for d in _pyproject()["project"]["dependencies"]
    }
    for name, first_safe in (("jinja2", "3.1.6"), ("python-multipart", "0.0.31"), ("authlib", "1.7.1")):
        req = deps[name]
        assert not req.specifier.contains(Version(first_safe).base_version + "a1", prereleases=True) or \
            req.specifier.contains(Version(first_safe)), f"{name}: {req}"
        # the floor must exclude everything below the first safe release
        below = str(Version(first_safe).major) + "." + str(Version(first_safe).minor) + ".0"
        if Version(below) < Version(first_safe):
            assert not req.specifier.contains(Version(below)), (
                f"{name} still allows {below}, which is below the first safe release {first_safe}"
            )


def test_plotly_is_a_scripts_extra_not_a_runtime_dependency():
    proj = _pyproject()["project"]
    assert "plotly" not in _dep_names(proj["dependencies"])
    assert any(d.startswith("plotly") for d in proj["optional-dependencies"]["scripts"])


def test_alembic_floor_supports_if_exists_downgrades():
    # alembic/versions/*.py downgrade() calls use if_exists= (op.drop_index,
    # op.drop_constraint, op.drop_column) which requires alembic>=1.16.
    deps = {
        re.split(r"[><=\[!~]", d, maxsplit=1)[0].strip(): d
        for d in _pyproject()["project"]["dependencies"]
    }
    alembic_spec = deps["alembic"]
    match = re.search(r">=\s*(\d+)\.(\d+)", alembic_spec)
    assert match, f"alembic has no >= floor: {alembic_spec!r}"
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (1, 16), f"alembic floor too low for if_exists=: {alembic_spec!r}"


def test_lockfile_exists_and_covers_every_runtime_dependency():
    lock_path = REPO_ROOT / "requirements.lock"
    assert lock_path.exists(), "requirements.lock is missing — run pip-compile"
    lock_text = lock_path.read_text()
    for name in _dep_names(_pyproject()["project"]["dependencies"]):
        # pip-compile may emit either the '-' or '_' spelling of a PEP 503
        # name (e.g. slack-sdk vs slack_sdk); accept both rather than
        # pinning to whichever one happened to come out of a given run. It
        # also preserves any requested extra in the pin itself (e.g.
        # `sqlalchemy[asyncio]==2.0.52`, `uvicorn[standard]==0.52.4`), so
        # tolerate an optional bracketed extras suffix before the `==`.
        escaped = re.escape(name).replace("\\-", "[-_]")
        pattern = rf"(?im)^{escaped}(\[[^\]]*\])?=="
        assert re.search(pattern, lock_text), f"{name} not pinned in requirements.lock"


def _ci_sh() -> str:
    return (REPO_ROOT / "scripts" / "ci.sh").read_text()


def test_ci_sh_runs_the_deterministic_lock_check_before_the_suite():
    """The gate checks lock-vs-pyproject consistency, offline, not lock-vs-PyPI.

    The original implementation diffed the committed lock against a fresh pip-compile,
    which makes the gate red whenever any of ~200 transitive packages publishes a
    release with no repository change, and two back-to-back resolves can disagree
    depending on HTTP cache state. See the step's comment in scripts/ci.sh.
    """
    text = _ci_sh()
    assert "scripts/check_lockfile.py" in text
    assert text.index("scripts/check_lockfile.py") < text.index("-m pytest")
    # strict mode keeps the fresh-resolve comparison available, as a NOTE not a failure
    assert "LOCKCHECK=strict" in text
    assert "does not fail the gate" in text


def test_ci_sh_lockcheck_has_a_documented_skip_valve():
    # The default check is offline and needs no interpreter hunting, but strict mode
    # still resolves against a 3.11 interpreter (the lock was cut against 3.11 to match
    # the Dockerfile base, not $VENV_PY's 3.12) and must SKIP rather than fail when one
    # is unavailable.
    text = _ci_sh()
    assert "LOCKCHECK" in text
    assert "LOCKCHECK=none" in text
    assert "3.11" in text


def _strip_comment_lines(text: str) -> list[str]:
    """Mirrors scripts/ci.sh's `grep -v '^#'` used to compare lockfiles."""
    return [line for line in text.splitlines() if not line.startswith("#")]


def test_lockfile_comparison_ignores_pip_composes_own_header_but_not_real_pin_drift():
    # pip-compile's header records the --output-file path it was invoked with, so a raw
    # diff between the committed lock and a freshly regenerated scratch file can never
    # match even when every pin is byte-identical. scripts/ci.sh works around this by
    # diffing both sides with comment lines stripped first — verify that mechanism here
    # on synthetic fixtures, without shelling out to pip-compile at all.
    committed = (
        "#\n# This file is autogenerated by pip-compile\n"
        "#    --output-file=requirements.lock\n#\nfastapi==0.111.0\n"
    )
    regenerated_same_pins = (
        "#\n# This file is autogenerated by pip-compile\n"
        "#    --output-file=/tmp/scratch.abc123\n#\nfastapi==0.111.0\n"
    )
    regenerated_drifted = regenerated_same_pins.replace("0.111.0", "0.112.0")

    # Raw text differs (only the header's recorded path differs) even though the pins
    # agree — this is exactly why ci.sh never diffs the raw files directly.
    assert committed != regenerated_same_pins

    # Stripping comment lines makes the pins-only comparison agree when only the
    # header differs...
    assert _strip_comment_lines(committed) == _strip_comment_lines(regenerated_same_pins)
    # ...and still catches a real pin drift.
    assert _strip_comment_lines(committed) != _strip_comment_lines(regenerated_drifted)


def test_ci_sh_fails_the_gate_on_real_lockfile_drift(tmp_path):
    # Behavioural mutation check on the lock step's `exit 1` — the static test above
    # only pins the substring; this actually runs scripts/ci.sh (same shape
    # test_run_migration_sh.py uses for bash) and proves it fails closed when
    # pyproject.toml declares a constraint the committed lock's pin does not satisfy.
    # No network: the check reads the two files.
    #
    # A symlink farm mirrors every top-level entry of the real repo except
    # pyproject.toml, which is a real copy with one dependency's cap lowered enough
    # that pip-compile must resolve an older pin than what is already committed in
    # (the symlinked, untouched) requirements.lock — never touches the real
    # pyproject.toml. ci.sh derives REPO_ROOT from its own invoked path
    # (`cd "$(dirname "${BASH_SOURCE[0]}")/.."`), so invoking the farm's symlinked
    # scripts/ci.sh makes REPO_ROOT resolve to tmp_path, and every relative
    # reference the script makes (alembic/, tests/, .venv-test/, requirements.lock,
    # pyproject.toml) resolves through the farm.
    for entry in os.listdir(REPO_ROOT):
        if entry in (".git", "pyproject.toml"):
            continue
        (tmp_path / entry).symlink_to(REPO_ROOT / entry)

    original = (REPO_ROOT / "pyproject.toml").read_text()
    mutated = original.replace('"anthropic>=0.26.0,<1.0.0"', '"anthropic>=0.26.0,<0.100.0"')
    assert mutated != original, "mutation target line not found — pyproject.toml moved"
    (tmp_path / "pyproject.toml").write_text(mutated)

    proc = subprocess.run(
        ["./scripts/ci.sh"],
        cwd=tmp_path,
        # No LOCKCHECK override: this test needs the lockfile step to actually run.
        env=nested_gate_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "requirements.lock does not match pyproject.toml" in combined
    assert "anthropic" in combined, "the failure must name the package that drifted"
    assert "==> mypy" not in proc.stdout, "gate must stop at the lockfile check, not reach mypy"
    # The lock-consistency failure is fatal before step 6 even announces itself, so this
    # site cannot pay for the smoke step's install however the environment is built —
    # pin that ordering, since it is the reason an inherited LOCK_SMOKE=1 is invisible here.
    assert "==> lock smoke test" not in combined, combined
    assert SMOKE_STEP_RAN not in combined, combined


def test_lock_smoke_step_is_documented_and_opt_in():
    # The freshness gate above proves the lock MATCHES pyproject.toml;
    # nothing proves it actually WORKS on the Python 3.11 the Dockerfile installs
    # it on. Static pin that the opt-in smoke step exists, is off by default, and
    # names the three entry points it imports.
    text = _ci_sh()
    assert "LOCK_SMOKE" in text
    assert re.search(r'LOCK_SMOKE:-\}"\s*!=\s*"1"', text) or '"${LOCK_SMOKE:-}" != "1"' in text
    for module in ("src.main", "src.worker.main", "src.agent.main"):
        assert module in text, f"lock smoke step does not mention {module!r}"
    assert "--require-hashes" in text


def test_lock_smoke_step_defaults_to_a_visible_skip():
    # Behavioural: with LOCK_SMOKE unset (the default), the step must print its
    # one-line skip note rather than silently doing nothing — cheap to check since
    # the step's own default path costs nothing (it's an early bash `if`, no venv
    # created). Piggybacks on MYPY_MAX=0 to stop the gate quickly afterwards,
    # same shape as test_ci_gate.py's mypy behavioural tests.
    proc = subprocess.run(
        ["./scripts/ci.sh"],
        cwd=REPO_ROOT,
        # LOCK_SMOKE must not reach the child: this test asserts the step's DEFAULT
        # behaviour, and an ambient LOCK_SMOKE=1 from the operator's own shell would make
        # it run for real and fail the assertion below (which is exactly what happened on
        # a `LOCK_SMOKE=1 ./scripts/ci.sh` gate run). nested_gate_env() handles that by
        # construction — it never copies a variable it was not asked for.
        env=nested_gate_env(LOCKCHECK="none", MYPY_MAX="0"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert "==> lock smoke test" in proc.stdout
    assert SMOKE_STEP_SKIPPED in proc.stdout
    assert SMOKE_STEP_RAN not in proc.stdout, proc.stdout
    assert proc.returncode != 0, proc.stdout + proc.stderr
