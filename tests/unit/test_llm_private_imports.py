"""MD-15: `acreate` / `all_text` are llm.py's public API; nothing outside it reaches
for the old private names. AST-based, so prose in comments and docstrings that still
mentions them does not count."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_OLD = {"_acreate", "_all_text"}
_SELF = Path(__file__).name


def _uses_old_names(tree: ast.AST) -> list[str]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _OLD:
            hits.append(f"attribute .{node.attr}")
        elif isinstance(node, ast.ImportFrom):
            hits += [f"import {a.name}" for a in node.names if a.name in _OLD]
        elif isinstance(node, ast.Constant) and node.value in {"_acreate", "_all_text"}:
            hits.append(f"string {node.value!r}")
    return hits


def test_no_private_llm_imports_outside_llm_py():
    hits = []
    for root in ("src", "scripts", "tests"):
        for p in (REPO / root).rglob("*.py"):
            if p.name == "llm.py" and p.parent.name == "services":
                continue
            if "__pycache__" in p.parts or p.name in {"_frozen_llm_turns.py", _SELF}:
                continue
            for hit in _uses_old_names(ast.parse(p.read_text(encoding="utf-8"))):
                hits.append(f"{p.relative_to(REPO)}: {hit}")
    assert hits == []
