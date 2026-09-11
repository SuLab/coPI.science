"""The four ``PI_INBOUND_*`` constants must live in a dependency-free
``src.agent.inbound_state`` module, not be imported by
``src.services.pi_inbox`` (the web/worker request path that records a PI
message) from ``src.agent.simulation`` inside its own function body —
otherwise that drags the whole simulation engine module into the web/worker
process just to read one string constant.
"""

import subprocess
import sys

from src.agent import inbound_state


def test_the_four_constants_live_in_inbound_state():
    assert inbound_state.PI_INBOUND_PENDING == "pending"
    assert inbound_state.PI_INBOUND_INGESTED == "ingested"
    assert inbound_state.PI_INBOUND_HANDLED == "handled"
    assert inbound_state.PI_INBOUND_MAX_ATTEMPTS == 3


def test_simulation_still_re_exports_them_for_existing_callers():
    """Every existing ``from src.agent.simulation import PI_INBOUND_*`` (tests
    and src.agent.simulation's own internals) must keep working unchanged."""
    from src.agent import simulation

    assert simulation.PI_INBOUND_PENDING is inbound_state.PI_INBOUND_PENDING
    assert simulation.PI_INBOUND_INGESTED is inbound_state.PI_INBOUND_INGESTED
    assert simulation.PI_INBOUND_HANDLED is inbound_state.PI_INBOUND_HANDLED
    assert simulation.PI_INBOUND_MAX_ATTEMPTS is inbound_state.PI_INBOUND_MAX_ATTEMPTS


def test_importing_pi_inbox_does_not_pull_in_the_simulation_engine():
    """A fresh process that imports
    ``src.services.pi_inbox`` alone (no other src.agent.simulation import
    already in sys.modules) must not transitively import
    ``src.agent.simulation`` — that module is the request path's own web/
    worker footprint, not the always-on agent-run process."""
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; import src.services.pi_inbox; "
            "assert 'src.agent.simulation' not in sys.modules, "
            "sorted(m for m in sys.modules if m.startswith('src.agent'))",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_pi_inbox_source_never_imports_src_agent_simulation():
    """Pins the fix at the AST level, independent of whether a given import
    happens to be module-level or deferred inside a function body (a deferred
    import still drags the engine into the web/worker process's memory the
    first time the request path actually runs -- import-time laziness is not
    the property being protected here)."""
    import ast
    import inspect

    import src.services.pi_inbox as pi_inbox

    tree = ast.parse(inspect.getsource(pi_inbox))
    offending = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "src.agent.simulation"
    ]
    assert offending == [], (
        f"src/services/pi_inbox.py must not import from src.agent.simulation "
        f"anywhere, found: {[ast.dump(n) for n in offending]}"
    )


def test_inbound_state_module_has_no_imports_at_all():
    """Dependency-free means exactly that -- prose (including the module
    docstring) may reference ``src.agent.simulation`` by name for
    documentation, but no actual import statement may exist. Parsed via
    ``ast`` rather than a naive line scan so docstring prose containing the
    words "from"/"import" can't produce a false positive."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(inbound_state))
    import_nodes = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert import_nodes == [], (
        f"src.agent.inbound_state must have no imports at all, found: {import_nodes}"
    )
