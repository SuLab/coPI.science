"""Spec §7.5: the constants and helpers the web app and the tool layer share live in
dependency-free modules, so the web process never imports the engine entry point and
roles/tools/agent import in one direction."""

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _src_imports(rel: str) -> set[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
        elif isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
    return out


def test_the_web_app_does_not_import_the_engine_entry_point():
    code = "import sys, src.main; print('src.agent.main' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_leaf_modules_are_dependency_free():
    for rel in ("src/agent/tool_definitions.py", "src/agent/dois.py"):
        assert not {m for m in _src_imports(rel) if m.startswith("src.")}, rel


def test_roles_no_longer_imports_tools_and_tools_no_longer_imports_agent():
    assert "src.agent.tools" not in _src_imports("src/agent/roles.py")
    assert "src.agent.agent" not in _src_imports("src/agent/tools.py")


def test_the_moved_names_are_the_same_objects():
    from src.agent import agent, dois, tool_definitions, tools
    from src.agent import main as agent_main
    from src.services import build_info

    assert tools.TOOL_DEFINITIONS is tool_definitions.TOOL_DEFINITIONS
    assert agent._extract_dois is dois.extract_dois
    assert tools.paper_ids_in is dois.paper_ids_in
    assert agent_main.API_CALL_UNITS_NOTE is build_info.API_CALL_UNITS_NOTE
