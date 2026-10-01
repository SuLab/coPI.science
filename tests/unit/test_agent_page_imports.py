import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src/routers/agent_page.py"


def test_no_function_level_imports():
    offenders = []
    for node in ast.walk(ast.parse(SRC.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    offenders.append(f"{node.name}:{inner.lineno}")
    assert offenders == []
