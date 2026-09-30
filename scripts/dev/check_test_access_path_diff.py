#!/usr/bin/env python3
"""Phase 1 test-diff checker (spec §7.6). DEV-ONLY: deleted at the end of Phase 1.

Every test file changed since ``--base`` must differ from its base version only by:

* import statements;
* patch-target strings and the module objects handed to ``monkeypatch.setattr``:
  ``src.agent.simulation.<N>`` -> ``src.agent.engine.deps.<N>`` for the seam names,
  -> ``src.agent.engine.constants.<N>`` for the patched constants, and
  ``src.routers.admin.<N>`` -> ``src.routers.admin.<module>.<N>``;
* ``<expr>.<member>`` -> ``<expr>.<unit>.<member>`` and ``SimulationEngine.<member>``
  -> ``<UnitClass>.<member>`` (and the matching ``getsource`` targets);

or be a function listed in ``EXCEPTIONS`` with a reviewed reason. Both trees are
normalised (imports dropped, the rewrites above undone) and compared function by
function with ``ast.dump``. Assertions, literals, fixtures and parametrisations are
compared as-is, so any change to them fails.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys

UNIT_ATTRS = frozenset({
    "persistence", "llm_log", "channel_directory", "slack_io", "panel", "headlines",
    "verdicts", "threads", "memory", "reply_lane", "post_lane", "scheduler", "control",
    "roster", "rebuild", "run_announcer", "ctx", "run_state",
})
UNIT_CLASSES = frozenset({
    "Persistence", "LlmLog", "ChannelDirectory", "SlackIO", "Panel", "Headlines",
    "Verdicts", "Threads", "Memory", "ReplyLane", "PostLane", "Scheduler", "Control",
    "Roster", "Rebuild", "RunAnnouncer", "EngineContext", "RunState",
})
SEAM = frozenset({"generate_with_tools", "generate_agent_response", "get_settings",
                  "time", "datetime", "load_role", "get_build_info"})
PATCHED_CONSTANTS = frozenset({"SEEDED_CHANNELS", "CHANNEL_POLL_INTERVAL",
                               "HEADLINES_MAX_AT_SHUTDOWN", "PROFILES_DIR",
                               "_UNIVERSAL_CHANNELS", "_CHANNEL_KEYWORDS"})
ADMIN_MODULES = frozenset({"_common", "users", "jobs", "runs", "discussions", "agents",
                           "assessments", "impersonation", "access", "cohorts", "simulation"})
ENGINE_MODULE_PREFIX = "src.agent.engine"

#: "<path>::<qualname>" -> reviewed reason. Filled by the Phase 1 tasks that need it.
EXCEPTIONS: dict[str, str] = {
    "tests/integration/test_slack_cohort_live.py::cohort_engine":
        "patches a seam name and a patched constant: two module objects (§7.6)",
    "tests/integration/test_full_run_live.py::full_run":
        "patches a seam name and a patched constant: two module objects (§7.6)",
    "tests/integration/test_cohort_scenarios.py::scenario_db":
        "save/restore and direct assignment across deps and constants (spec §7.6, SA6-09)",
    "tests/integration/test_cohort_scenarios.py::_build_engine":
        "save/restore and direct assignment across deps and constants (spec §7.6, SA6-09)",
    "tests/unit/test_simulation_logic.py::test_prior_threads_per_pair_storage_is_capped":
        "reads a constant off the simulation module and patches a seam name on deps: "
        "two module objects (§7.6)",
}


def _norm_string(value: str) -> str:
    parts = value.split(".")
    if value.startswith(ENGINE_MODULE_PREFIX + ".") and len(parts) == 5 and parts[3] in ("deps", "constants"):
        return "src.agent.simulation." + parts[4]
    if value.startswith("src.routers.admin.") and len(parts) == 5 and parts[3] in ADMIN_MODULES:
        return "src.routers.admin." + parts[4]
    return value


class _Normalise(ast.NodeTransformer):
    def __init__(self, engine_aliases: set[str], admin_aliases: set[str], class_aliases: set[str]):
        self.engine_aliases = engine_aliases
        self.admin_aliases = admin_aliases
        self.class_aliases = class_aliases

    def visit_Import(self, node):  # imports may change freely
        return None

    def visit_ImportFrom(self, node):
        return None

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(_norm_string(node.value)), node)
        return node

    def visit_Name(self, node):
        if node.id in self.engine_aliases:
            return ast.copy_location(ast.Name("__ENGINE_MODULE__", node.ctx), node)
        if node.id in self.admin_aliases:
            return ast.copy_location(ast.Name("__ADMIN_MODULE__", node.ctx), node)
        if node.id in self.class_aliases or node.id in UNIT_CLASSES:
            return ast.copy_location(ast.Name("SimulationEngine", node.ctx), node)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        # <expr>.<unit>.<member>  ->  <expr>.<member>
        if isinstance(node.value, ast.Attribute) and node.value.attr in UNIT_ATTRS:
            return ast.copy_location(ast.Attribute(node.value.value, node.attr, node.ctx), node)
        # admin.<module>.<name> -> admin.<name>
        if (isinstance(node.value, ast.Attribute) and node.value.attr in ADMIN_MODULES
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "__ADMIN_MODULE__"):
            return ast.copy_location(ast.Attribute(node.value.value, node.attr, node.ctx), node)
        return node


def _aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    engine, admin, classes = set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound = a.asname or a.name.split(".")[0]
                if a.name == "src.agent.simulation" or a.name.startswith(ENGINE_MODULE_PREFIX):
                    engine.add(bound)
                if a.name == "src.routers.admin" or a.name.startswith("src.routers.admin."):
                    admin.add(bound)
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                bound = a.asname or a.name
                full = f"{n.module}.{a.name}"
                if n.module in ("src.agent", "src.agent.engine") and (
                    a.name == "simulation" or full.startswith(ENGINE_MODULE_PREFIX)
                ):
                    engine.add(bound)
                elif n.module == "src.routers" and a.name == "admin":
                    admin.add(bound)
                elif n.module == "src.routers.admin" and a.name in ADMIN_MODULES:
                    admin.add(bound)
                elif n.module.startswith(ENGINE_MODULE_PREFIX) and a.name in UNIT_CLASSES:
                    classes.add(bound)
    return engine, admin, classes


def _units(source: str) -> dict[str, str]:
    """qualname -> normalised ast.dump, plus '<module>' for top-level non-def code."""
    tree = ast.parse(source)
    norm = _Normalise(*_aliases(tree)).visit(tree)
    out: dict[str, str] = {}
    module_rest = []

    def walk(body, prefix):
        for node in body:
            if node is None:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + node.name] = ast.dump(node, include_attributes=False)
            elif isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")
                header = ast.ClassDef(node.name, node.bases, node.keywords, [], node.decorator_list, [])
                out[prefix + node.name + ".<class>"] = ast.dump(header, include_attributes=False)
            elif not prefix:
                module_rest.append(ast.dump(node, include_attributes=False))

    walk(norm.body, "")
    out["<module>"] = "\n".join(module_rest)
    return out


def compare(path: str, old: str | None, new: str | None) -> list[str]:
    if old is None or new is None:
        key = f"{path}::<file>"
        return [] if key in EXCEPTIONS else [f"{path}: file added or deleted"]
    a, b = _units(old), _units(new)
    problems = []
    for name in sorted(set(a) | set(b)):
        if a.get(name) != b.get(name) and f"{path}::{name}" not in EXCEPTIONS:
            problems.append(f"{path}::{name}: changed beyond an access-path rewrite")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _show(ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main(base: str) -> int:
    changed = [p for p in _git("diff", "--name-only", base, "--", "tests").split() if p.endswith(".py")]
    changed += [p for p in _git("ls-files", "--others", "--exclude-standard", "tests").split() if p.endswith(".py")]
    problems = []
    for path in sorted(set(changed)):
        new = open(path, encoding="utf-8").read() if subprocess.run(["test", "-f", path]).returncode == 0 else None
        old = _show(base, path)
        if old is None:
            continue  # a NEW test file (for example test_engine_import_graph.py) is not a migration
        problems += compare(path, old, new)
    for p in problems:
        print(p)
    print(f"{len(problems)} problem(s) in {len(set(changed))} changed test file(s)")
    return 1 if problems else 0


def _self_test() -> int:
    old = (
        "import src.agent.simulation as sim\n"
        "from src.agent.simulation import SimulationEngine\n"
        "def test_a(monkeypatch, eng):\n"
        "    monkeypatch.setattr('src.agent.simulation.get_settings', f)\n"
        "    monkeypatch.setattr(sim, 'SEEDED_CHANNELS', [1])\n"
        "    assert eng._flush_persisted is not None\n"
        "    src = inspect.getsource(SimulationEngine._post_message)\n"
        "    assert 'x' in src\n"
    )
    ok = (
        "import src.agent.engine.constants as sim\n"
        "from src.agent.engine.slack_io import SlackIO\n"
        "def test_a(monkeypatch, eng):\n"
        "    monkeypatch.setattr('src.agent.engine.deps.get_settings', f)\n"
        "    monkeypatch.setattr(sim, 'SEEDED_CHANNELS', [1])\n"
        "    assert eng.persistence._flush_persisted is not None\n"
        "    src = inspect.getsource(SlackIO._post_message)\n"
        "    assert 'x' in src\n"
    )
    bad = ok.replace("assert 'x' in src", "assert 'y' in src")
    failures = []
    if compare("t.py", old, ok):
        failures.append("an access-path-only change was rejected")
    if not compare("t.py", old, bad):
        failures.append("an assertion change was accepted")
    for f in failures:
        print("SELF-TEST FAIL:", f)
    print("self-test ok" if not failures else "self-test FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        sys.exit(_self_test())
    if not ns.base:
        ap.error("--base is required")
    sys.exit(main(ns.base))
