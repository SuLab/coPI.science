"""Static checks for #27 I4: upper caps on version-sensitive deps, plotly out
of the runtime install, an alembic floor high enough for `if_exists=` in
migration downgrades, and a lockfile that exists and names every runtime
dependency. Does not attempt to re-validate pip-compile's own hash pinning —
that's pip's job at install time (--require-hashes, wired in Task 27.6)."""

import re
import tomllib
from pathlib import Path

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
