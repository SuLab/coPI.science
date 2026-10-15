"""R1.3: every jobs.last_error write goes through job_queue.job_error_text, which
redacts before it truncates."""
import ast
from pathlib import Path

from src.services.job_queue import LAST_ERROR_MAX_CHARS, job_error_text

ROOT = Path(__file__).resolve().parents[2]


def test_job_error_text_redacts_then_truncates():
    # 1980 + 22 redacted characters overflow the cap, and the redaction still fits inside it.
    text = job_error_text(RuntimeError("x" * 1980 + " api_key=SECRETKEY0123&db=1"))
    assert "SECRETKEY0123" not in text
    assert "api_key=REDACTED" in text
    assert len(text) == LAST_ERROR_MAX_CHARS == 2000
    assert job_error_text("plain") == "plain"


def _is_job_error_text_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "job_error_text"


def test_every_last_error_write_goes_through_job_error_text():
    writes: list[tuple[str, int, bool]] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "last_error":
                        writes.append((rel, node.lineno, _is_job_error_text_call(node.value)))
            elif isinstance(node, ast.keyword) and node.arg == "last_error":
                writes.append((rel, node.value.lineno, _is_job_error_text_call(node.value)))
    bad = [(rel, line) for rel, line, ok in writes if not ok]
    assert not bad, f"last_error written without job_error_text: {bad}"
    assert sum(1 for rel, _, _ in writes if rel == "src/worker/main.py") >= 4, writes
