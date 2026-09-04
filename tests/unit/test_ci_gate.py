"""Pins that ci.sh gates on a mypy ceiling, mirroring the ruff src/ ratchet.
Same convention as tests/unit/test_cohort_isolation.py::TestMigrationHygiene
for the alembic gate: assert marker strings exist and precede pytest."""

import re
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
