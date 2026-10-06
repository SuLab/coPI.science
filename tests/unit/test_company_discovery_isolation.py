"""Company discovery must not import what tests/unit/test_enrichment_isolation.py forbids
(the industry score/evidence modules, `industry_sources/pubmed_coi.py` among them), nor the
profile pipeline (which imports it). The COI attribution it shares with the industry side
lives in `src/services/coi_attribution.py`, which is guarded here too."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = {
    "src.services.industry_score",
    "src.services.industry_evidence",
    "src.services.industry_sources",
    "src.services.profile_pipeline",
}
MODULES = [
    "src/services/company_discovery.py",
    "src/services/company_discovery_budget.py",
    "src/services/coi_attribution.py",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / "src/services/company_sources").glob("*.py")),
]


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def test_discovery_modules_import_nothing_forbidden():
    assert len(MODULES) == 8, MODULES
    for rel in MODULES:
        hit = {m for m in _imports(ROOT / rel) if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}
        assert not hit, f"{rel} imports {hit}"
