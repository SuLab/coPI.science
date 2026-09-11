"""build_cabo_sankey.py must stay runnable by the operator it documents.

Two things are pinned here:

1. The ``DEFAULT_START`` comment must not read like an open TODO — ``--start``
   has taken a real argparse value
   (``git log -S"add_argument.*--start" -- scripts/build_cabo_sankey.py``).
   This is asserted against the *contiguous comment block* above the
   assignment, and never against the default date itself: pinning the literal
   date makes a test raise ``StopIteration`` (a test *error*, not a failure)
   the moment anyone changes the date or reflows the comment.
2. ``plotly`` lives in the optional ``scripts`` extra, so it is absent from
   ``requirements.lock`` and therefore from the runtime image. The
   documented invocation must still parse its arguments, and must name the
   extra instead of dying on a bare ``ModuleNotFoundError``.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_cabo_sankey.py"
SCRIPT = SCRIPT_PATH.read_text()


def _comment_block_above(assignment_prefix: str) -> str:
    """The contiguous ``#`` comment block immediately above an assignment.

    Returns "" when the assignment is missing or has no comment above it, so
    callers get a readable assertion failure rather than StopIteration.
    """
    lines = SCRIPT.splitlines()
    idx = next(
        (i for i, line in enumerate(lines) if line.startswith(assignment_prefix)), None
    )
    if idx is None:
        return ""
    block: list[str] = []
    i = idx - 1
    while i >= 0 and lines[i].lstrip().startswith("#"):
        block.append(lines[i])
        i -= 1
    return "\n".join(reversed(block))


def _run_without_plotly(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script with plotly made unimportable, as the built image has it.

    A stub ``plotly`` module on PYTHONPATH shadows any site-packages copy
    (.venv-test installs plotly, which is exactly what masks this defect
    locally) and raises the same error a missing install would.
    """
    stub = tmp_path / "noplotly"
    stub.mkdir()
    (stub / "plotly.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'plotly'\", name='plotly')\n"
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(stub)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_default_start_comment_mentions_the_start_flag():
    comment = _comment_block_above("DEFAULT_START = ")
    assert comment, (
        "no `DEFAULT_START = ` assignment with a comment block above it in "
        f"{SCRIPT_PATH} — the comment explaining that the date is only a "
        "default must stay attached to it"
    )
    assert "--start" in comment, (
        "the comment above DEFAULT_START must say --start already "
        f"parameterizes the window, so it does not read as an open TODO; got:\n{comment}"
    )


def test_help_works_without_plotly(tmp_path):
    """--help must not need a plotting library."""
    proc = _run_without_plotly(tmp_path, "--help")
    assert proc.returncode == 0, (
        "`python scripts/build_cabo_sankey.py --help` must work in an image "
        "that has no plotly (the runtime image has none — plotly is in the "
        f"`scripts` extra).\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "--start" in proc.stdout


def test_missing_plotly_names_the_extra_instead_of_a_traceback(tmp_path):
    """The build path must fail with an actionable message, before the DB."""
    proc = _run_without_plotly(tmp_path, "--out", str(tmp_path / "out"))
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert ".[scripts]" in combined, (
        "a run without plotly must name the extra that supplies it "
        f"(pip install '.[scripts]'), not just raise.\n{combined}"
    )
    assert "Traceback" not in combined, (
        f"the missing-plotly path must not surface a raw traceback.\n{combined}"
    )


def test_header_does_not_claim_scripts_needs_docker_cp():
    """`COPY . .` bakes scripts/ into the image; the dev file bind-mounts it.

    The stale "scripts/ isn't mounted — docker cp it in first" sentence
    contradicts the script's own `docker compose exec app python scripts/...`
    line.
    """
    assert "isn't mounted" not in SCRIPT
    assert "docker compose cp scripts/build_cabo_sankey.py" not in SCRIPT
