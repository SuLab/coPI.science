"""LC-04: one tolerant JSON extractor (json_extract.extract_json). The Phase-5
action parser and the verdict sidecar's strict parse stay byte-for-byte (B24;
MD-1 and S2-11 are ACCEPTed) and are the only other functions allowed to pull
JSON out of model text. A function "pulls JSON out of text" when it calls
json.loads AND either carries a ``` fence literal or slices on '{'/'}' positions."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ALLOWED = {
    ("src/services/json_extract.py", "extract_json"),
    ("src/agent/engine/post_lane.py", "_parse_phase5_response"),
    ("src/agent/engine/sidecar.py", "_extract_assessment_json"),
    ("src/agent/engine/sidecar.py", "_sidecar_has_valid_json_block"),
}


def _is_extractor(fn: ast.AST) -> bool:
    calls_loads = fence = brace_find = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "loads" and getattr(node.func.value, "id", None) == "json":
                calls_loads = True
            if node.func.attr in {"find", "rfind", "index", "rindex"} and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and a.value in {"{", "}"}:
                    brace_find = True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "```" in node.value:
            fence = True
    return calls_loads and (fence or brace_find)


def _found() -> set[tuple[str, str]]:
    out = set()
    for path in (REPO / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_extractor(node):
                out.add((str(path.relative_to(REPO)), node.name))
    return out


def test_only_the_allowed_parsers_extract_json():
    assert _found() - ALLOWED == set()


def test_the_rule_is_not_vacuous():
    assert ("src/services/json_extract.py", "extract_json") in _found()


def test_no_private_copies_remain():
    import src.services.llm as llm
    assert not hasattr(llm, "_extract_json")
    from src.agent.engine import sidecar
    assert not hasattr(sidecar, "_extract_json")
