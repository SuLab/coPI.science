"""The industry score is a manager-facing indicator ONLY (adversarial D16/D17; spec
2026-10-05 §6.2, P32). Nothing that builds profile text, a prompt, a tool result, the
assessment chat or the review bot may reach the industry evidence or score: not by
importing an industry module (under any alias: `from src.services import
industry_evidence as ie` counts), not by naming `PiIndustryEvidence` or `PiIndustryScore`,
and not by naming their tables in a string (raw SQL).

The check is transitive. It walks every first-party import, function-level imports
included, from the builders: every module under src/agent/, src/services/assessment_chat*,
src/services/profile_*, grant_enrichment and review_bot. A helper module in between hides
nothing. Imports under `if TYPE_CHECKING:` are skipped (they never run).

Exempt, and not walked into: the `src.models` package (its __init__ re-exports every model,
and `src.models.enrichment` defines the two classes; models are data, the rule is about
who reads them), and `src.worker.main`, which dispatches every job type,
`industry_evidence` included (the builders import its JobContext only under
TYPE_CHECKING)."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_MODULES = (
    "src.services.industry_score", "src.services.industry_evidence", "src.services.industry_sources",
)
#: Kept under this name for tests/unit/test_pi_companies.py, which applies the module rule
#: to pi_companies directly.
FORBIDDEN = set(FORBIDDEN_MODULES)
FORBIDDEN_NAMES = frozenset({"PiIndustryEvidence", "PiIndustryScore"})
FORBIDDEN_TABLES = ("pi_industry_evidence", "pi_industry_scores")
EXEMPT = ("src.models", "src.worker.main")


def _module_name(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _module_path(module: str) -> Path | None:
    base = ROOT.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _exempt(module: str) -> bool:
    return any(module == e or module.startswith(e + ".") for e in EXEMPT)


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _nodes(tree: ast.AST):
    """Every node except the body of an `if TYPE_CHECKING:` block (its `else` is kept)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child):
                stack.extend(child.orelse)
            else:
                stack.append(child)


def _from_module(node: ast.ImportFrom, package: str) -> str | None:
    """The absolute module of a `from … import`, a relative one resolved against
    ``package`` (the importing module's package); None past the top."""
    if not node.level:
        return node.module
    parts = package.split(".")
    if node.level - 1 > len(parts) - 1:
        return None
    base = ".".join(parts[: len(parts) - (node.level - 1)])
    return f"{base}.{node.module}" if node.module else base


def _scan(source: str, package: str = "src.services") -> tuple[set[str], set[str], set[str]]:
    """(dotted names imported, `from M import a` also giving `M.a`, a relative import
    resolved against ``package``; identifiers and attribute names used or imported;
    string constants)."""
    imported: set[str] = set()
    names: set[str] = set()
    strings: set[str] = set()
    for node in _nodes(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and (module := _from_module(node, package)):
            imported.add(module)
            imported |= {f"{module}.{a.name}" for a in node.names}
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    return imported, names, strings


def _imports(path: Path) -> set[str]:
    """The dotted names `path` imports (`_scan`), for tests/unit/test_pi_companies.py."""
    return _scan(path.read_text(encoding="utf-8"))[0]


def _package(module: str) -> str:
    """The package a module's relative imports resolve against (itself for a package)."""
    path = _module_path(module)
    return module if path is not None and path.name == "__init__.py" else module.rsplit(".", 1)[0]


def violations(source: str, package: str = "src.services") -> list[str]:
    """What in `source` reaches the industry score: forbidden modules, class names, tables."""
    imported, names, strings = _scan(source, package)
    found = sorted(m for m in imported if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_MODULES))
    found += sorted(names & FORBIDDEN_NAMES)
    found += [t for t in FORBIDDEN_TABLES if any(t in s for s in strings)]
    return found


def _roots() -> list[str]:
    roots = [_module_name(p) for p in sorted((ROOT / "src" / "agent").rglob("*.py"))]
    for path in sorted((ROOT / "src" / "services").glob("*.py")):
        if path.stem.startswith(("assessment_chat", "profile_")) or path.stem in ("grant_enrichment", "review_bot"):
            roots.append(_module_name(path))
    return roots


def reachable() -> dict[str, str | None]:
    """Every first-party module the builders reach -> the module that first imported it
    (None for a builder itself)."""
    parent: dict[str, str | None] = dict.fromkeys(_roots())
    queue = list(parent)
    seen: set[str] = set()
    while queue:
        module = queue.pop()
        if module in seen or _exempt(module):
            continue
        seen.add(module)
        path = _module_path(module)
        if path is None:
            continue
        imported, _, _ = _scan(path.read_text(encoding="utf-8"), _package(module))
        for name in sorted(imported):
            target = name
            while target.startswith("src.") and _module_path(target) is None:
                target = target.rsplit(".", 1)[0]
            if target.startswith("src.") and target not in parent:
                parent[target] = module
                queue.append(target)
    return {m: p for m, p in parent.items() if m in seen}


def _chain(module: str, reached: dict[str, str | None]) -> str:
    out = [module]
    while reached.get(out[-1]):
        out.append(reached[out[-1]])
    return " <- ".join(out)


def test_no_builder_reaches_the_industry_score():
    reached = reachable()
    problems = {}
    for module in reached:
        path = _module_path(module)
        if path is not None and (found := violations(path.read_text(encoding="utf-8"), _package(module))):
            problems[module] = (found, _chain(module, reached))
    assert not problems, problems


def test_the_walk_reaches_the_known_helpers():
    """Control: the walk is not vacuous. None of these is a builder; each is reached only
    through one (profile_pipeline -> company_discovery -> coi_llm -> coi_attribution, …)."""
    reached = reachable()
    for module in ("src.services.company_discovery", "src.services.coi_attribution",
                   "src.services.company_discovery_budget", "src.services.grant_sections",
                   "src.services.pi_companies", "src.services.llm_pricing"):
        assert module in reached, module
    assert not [m for m in reached if _exempt(m)]


@pytest.mark.parametrize("source,expected", [
    ("from src.services import industry_evidence as ie\n", ["src.services.industry_evidence"]),
    ("import src.services.industry_score\n", ["src.services.industry_score"]),
    ("def f():\n    from src.services.industry_sources.registry import SOURCES\n",
     ["src.services.industry_sources.registry", "src.services.industry_sources.registry.SOURCES"]),
    ("from src.models import PiIndustryScore\n", ["PiIndustryScore"]),
    ("import src.models\nx = src.models.PiIndustryEvidence\n", ["PiIndustryEvidence"]),
    ("Q = 'SELECT raw_sum FROM pi_industry_scores'\n", ["pi_industry_scores"]),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
     "    from src.services.industry_score import WEIGHTS\n", []),
    ("from src.services import industry_jobs_note\n", []),
    ("from .industry_score import WEIGHTS\n",
     ["src.services.industry_score", "src.services.industry_score.WEIGHTS"]),
    ("from . import industry_evidence\n", ["src.services.industry_evidence"]),
])
def test_the_checker_catches_planted_references(source, expected):
    assert violations(source) == expected


def test_score_columns_never_appear_in_profile_export_text():
    src = (ROOT / "src" / "services" / "profile_export.py").read_text()
    assert "industry" not in src.lower()
