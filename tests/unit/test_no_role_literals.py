"""§8.5 tripwire: role names appear as literals only in the five exempt files."""
import ast
import re
from pathlib import Path

ROLE_NAMES = {"pi_lab", "scout_hub"}
EXEMPT = {
    Path("src/agent/roles.py"),            # DEFAULT_ROLE
    Path("src/agent/role_capabilities.py"),  # registry keys and guidance_set values
    Path("src/agent/thread_guidance.py"),  # GUIDANCE_SETS keys
    Path("src/models/agent_registry.py"),  # the column default
    Path("src/services/review_bot.py"),    # prompt text, B22
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_no_role_literals_in_src():
    offenders = []
    for path in sorted(Path("src").rglob("*.py")):
        if path in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in ROLE_NAMES and id(node) not in docs:
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], offenders


def test_no_role_literals_in_templates():
    pattern = re.compile(r"""['"](pi_lab|scout_hub)['"]""")
    offenders = [
        f"{p}:{i}"
        for p in sorted(Path("templates").rglob("*.html"))
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], offenders
