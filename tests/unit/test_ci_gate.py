"""Pins that ci.sh gates on a mypy ceiling, mirroring the ruff src/ ratchet.
Same convention as tests/unit/test_cohort_isolation.py::TestMigrationHygiene
for the alembic gate: assert marker strings exist and precede pytest."""

import os
import re
import subprocess
from pathlib import Path

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


def test_local_only_ci_stance_has_a_pointer_to_the_decision():
    text = _ci_sh()
    assert "issue #27 I1" in text


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


# --- Behavioural pins for #27 I8: deleting ci.sh's `exit 1`s must not go unnoticed. ---
#
# These actually run scripts/ci.sh in a subprocess (same shape
# test_run_migration_sh.py uses for bash), with the expensive steps disabled
# (CI_MIGRATION_DB=none, LOCKCHECK=none) so only the cheap offline steps
# (alembic sanity, both ruff passes) and the mypy step itself run before we
# reach the assertion. mypy over src/ takes ~2-3s here, so this stays fast.


def _ci_env(**overrides) -> dict:
    env = {**os.environ, "CI_MIGRATION_DB": "none", "LOCKCHECK": "none"}
    env.update(overrides)
    return env


def test_ci_sh_fails_when_mypy_findings_exceed_the_ceiling():
    # Mutation check on ci.sh:403's `exit 1` (mypy ceiling): with MYPY_MAX=0 the
    # step must fail the gate before ever reaching pytest.
    proc = subprocess.run(
        ["./scripts/ci.sh"],
        cwd=REPO_ROOT,
        env=_ci_env(MYPY_MAX="0"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "rose to" in proc.stdout + proc.stderr
    assert "-m pytest" not in proc.stdout, "gate must stop at the mypy ceiling, not reach pytest"


def test_ci_sh_mypy_ceiling_positive_control_lets_the_gate_proceed():
    # Mirror-image control: a ceiling comfortably above today's finding count must
    # NOT trip, and the gate must keep going past the mypy step (into pytest,
    # which this test does not wait for — it kills the process the moment it
    # sees the "==> pytest" banner, so this never pays pytest's ~7-minute cost).
    proc = subprocess.Popen(
        ["./scripts/ci.sh"],
        cwd=REPO_ROOT,
        env=_ci_env(MYPY_MAX="99999"),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    seen = []
    reached_pytest = False
    try:
        for line in proc.stdout:
            seen.append(line)
            if "==> pytest" in line:
                reached_pytest = True
                break
            if "rose to" in line:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    output = "".join(seen)
    assert reached_pytest, f"gate never reached the pytest step:\n{output}"
    assert "rose to" not in output, f"mypy ceiling tripped when it should not have:\n{output}"
