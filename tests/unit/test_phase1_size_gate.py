"""Spec §7.7: every function in code Phase 1 moved is at C901 <= 20 and at most 200
lines. Phase 3 (§9.8) extends the C901 gate to all of src/ in ci.sh."""

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
MOVED = [
    *sorted((ROOT / "src/agent/engine").glob("*.py")),
    ROOT / "src/agent/simulation.py",
    *sorted((ROOT / "src/routers/admin").glob("*.py")),
    ROOT / "src/services/simulation_view.py",
    ROOT / "src/services/profile_pipeline.py",
    ROOT / "src/services/job_progress.py",
]


def test_moved_code_passes_the_complexity_gate():
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *map(str, MOVED), "--select", "C901",
         "--config", "lint.mccabe.max-complexity=20", "--output-format=concise", "--no-cache"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_no_moved_function_is_longer_than_200_lines():
    long = []
    for path in MOVED:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        long += [
            f"{path.relative_to(ROOT)}::{n.name} ({n.end_lineno - n.lineno + 1})"
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.end_lineno - n.lineno + 1 > 200
        ]
    assert long == [], "\n".join(long)
