"""The industry score is a manager-facing indicator ONLY. Nothing that builds
profile text, the agent's system prompt, tool results, or the engine may import
the score/evidence modules (adversarial D16/D17)."""
import ast
from pathlib import Path

FORBIDDEN = {"src.services.industry_score", "src.services.industry_evidence", "src.services.industry_sources"}
CONSUMERS = [
    "src/services/profile_export.py", "src/services/profile_pipeline.py", "src/agent/simulation.py",
    "src/agent/tools.py", "src/agent/thread_guidance.py", "src/agent/specialists.py", "src/services/review_bot.py",
]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_score_modules_are_not_imported_by_profile_or_engine_code():
    for rel in CONSUMERS:
        mods = _imports(Path(rel))
        hit = {m for m in mods if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}
        assert not hit, f"{rel} imports {hit}"


def test_score_columns_never_appear_in_profile_export_text():
    src = Path("src/services/profile_export.py").read_text()
    assert "industry" not in src.lower()
