"""L-1 (opus review, audit 2026-09-10): the agent-run process's SIGTERM/SIGINT
path must abort an in-flight Slack retry sleep on the main event-loop thread,
not just flip the simulation's stop flag.

`AgentSlackClient` calls made directly from `_run_simulation`'s coroutine
(src/agent/main.py) and from `SimulationEngine`'s (src/agent/simulation.py,
e.g. the roster-sync connect/reconnect paths) run on the event-loop thread
itself, never through `src.services.slack_executor`'s pool — that pool is
only used by the FastAPI app and the worker. Those calls are therefore bound
to `slack_client.SHUTTING_DOWN`, the module-level fallback event, and nothing
in the agent process ever set it: `shutdown()`'s only effect was
`sim_engine.request_stop()`, which does not interrupt a call already sleeping
through a Slack Retry-After backoff.

`_run_simulation` needs a real DB session factory / agent roster / signal loop
to run end to end, so this pins the fix at the source level instead (mirroring
`tests/integration/test_agent_main_startup.py`'s pin of `_run_simulation`'s
`_reconcile_stale_runs` call, which uses the identical rationale: a fake-session
harness for the whole startup/shutdown block is out of proportion to what a
missing one-line call site needs).

M-1 (opus review, audit 2026-09-10) moved the actual `shutdown` closure out of
`_run_simulation` into `_make_shutdown_handler` (so the grace-delay behaviour
could be pinned behaviourally in `test_agent_main_shutdown_grace.py`) — this
file now just pins that `_run_simulation` wires signal handlers up to it.
"""

import ast
import inspect

from src.agent import main as _main_module


def test_shutdown_signals_the_slack_client_fallback_event():
    src = inspect.getsource(_main_module._run_simulation)
    tree = ast.parse(src)

    calls_make_shutdown_handler = any(
        isinstance(call.func, ast.Name) and call.func.id == "_make_shutdown_handler"
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
    )
    assert calls_make_shutdown_handler, (
        "_run_simulation must wire its SIGTERM/SIGINT handler through "
        "_make_shutdown_handler, which calls slack_client.signal_shutdown() so "
        "an AgentSlackClient call blocked on the main thread eventually aborts, "
        "not just request_stop() (which only stops the NEXT turn from starting)"
    )
