"""Every src/ function is 200 lines or fewer (spec §9.8), docstring included."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LIMIT = 200


def test_no_function_over_the_limit():
    over = []
    for p in sorted((REPO / "src").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n = node.end_lineno - node.lineno + 1
                if n > LIMIT:
                    over.append(f"{p.relative_to(REPO)}:{node.lineno} {node.name} {n}")
    assert over == [], "\n".join(over)
