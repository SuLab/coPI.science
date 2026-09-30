"""Every users.email write in src/ goes through src/services/user_email.py."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "src" / "services" / "user_email.py"


def _violations_in(source: str, label: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == "email":
                found.append(f"{label}:{node.lineno} assigns .email")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "User"
            and any(k.arg == "email" for k in node.keywords)
        ):
            found.append(f"{label}:{node.lineno} constructs User(email=...)")
    return found


def test_no_users_email_write_outside_the_helper():
    found = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path != HELPER:
            found += _violations_in(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    assert found == [], found


def test_the_scanner_catches_both_shapes():
    assert _violations_in("user.email = 'x'\nUser(name='n', email='y')\n", "t") == [
        "t:1 assigns .email", "t:2 constructs User(email=...)",
    ]
