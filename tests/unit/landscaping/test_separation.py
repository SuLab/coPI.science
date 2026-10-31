"""K12: landscaping is separate from the per-PI and roster data. No landscaping module imports
the publication, enrichment or company models, or names ``ResearcherProfile`` or
``AgentRegistry``."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_MODULES = ("src.models.publication", "src.models.enrichment", "src.models.pi_company")
FORBIDDEN_NAMES = frozenset({"ResearcherProfile", "AgentRegistry"})


def _landscaping_files() -> list[Path]:
    files = sorted((ROOT / "src" / "landscaping").rglob("*.py"))
    files += sorted((ROOT / "src" / "services").glob("landscape_*.py"))
    return files


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _named(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                names.add(node.asname)
    return names


def test_no_per_pi_or_roster_imports():
    files = _landscaping_files()
    assert files, "no landscaping modules found"
    offences = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT)
        for module in _imported_modules(tree):
            if any(module == bad or module.startswith(f"{bad}.") for bad in FORBIDDEN_MODULES):
                offences.append(f"{rel} imports {module}")
        offences += [f"{rel} names {name}" for name in sorted(_named(tree) & FORBIDDEN_NAMES)]
    assert offences == []
