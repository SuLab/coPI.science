"""Every write of ``publications.excluded_at`` in src/ and scripts/ goes through the three
named writers, and every call that builds a row already excluded passes its reason too
(contract C3, R4.2): a writer that forgets ``excluded_reason`` would leave Restore to guess.
A pure AST scan."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRITERS = frozenset({"exclude_by_system", "exclude_publication", "restore_publication"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _python_files():
    for top in ("src", "scripts"):
        yield from sorted((ROOT / top).rglob("*.py"))


def _stray_assignments(tree: ast.AST) -> list[tuple[int, str | None]]:
    """(line, enclosing function) of each ``<x>.excluded_at = …`` outside the writers."""
    found: list[tuple[int, str | None]] = []

    def visit(node: ast.AST, function: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, _FUNCTIONS) else function
            if isinstance(child, ast.Assign):
                targets = child.targets
            elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
                targets = [child.target]
            else:
                targets = []
            for target in targets:
                for sub in ast.walk(target):
                    if (isinstance(sub, ast.Attribute) and sub.attr == "excluded_at"
                            and isinstance(sub.ctx, ast.Store) and function not in WRITERS):
                        found.append((child.lineno, function))
            visit(child, inner)

    visit(tree, None)
    return found


def test_excluded_at_is_written_only_by_the_named_writers():
    stray, without_reason, writers_seen = [], [], set()
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(ROOT))
        stray += [(rel, line, function) for line, function in _stray_assignments(tree)]
        for node in ast.walk(tree):
            if isinstance(node, _FUNCTIONS) and node.name in WRITERS:
                writers_seen.add(node.name)
            if isinstance(node, ast.Call):
                keywords = {k.arg for k in node.keywords}
                if "excluded_at" in keywords and "excluded_reason" not in keywords:
                    without_reason.append((rel, node.lineno))
    assert writers_seen == WRITERS, "control: the scan reached the three writers"
    assert stray == []
    assert without_reason == []
