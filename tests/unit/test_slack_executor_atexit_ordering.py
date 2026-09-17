"""A plain ``atexit.register`` for ``shutdown_slack_executor`` runs too late
to abort a pool sleeper.

``atexit`` callbacks registered the normal way run AFTER the interpreter's
own ``threading._shutdown()``, which joins every non-daemon thread
(including this module's pool workers) to completion first. A worker
sleeping through ``_sleep_interruptibly`` at interpreter exit therefore
finishes its FULL sleep (nothing has set ``SHUTDOWN_REQUESTED`` yet) before
``atexit`` ever gets a chance to run ``shutdown_slack_executor``.

``threading._register_atexit`` hooks run BEFORE that thread join, so
registering ``signal_shutdown`` there (in addition to the existing
``atexit.register`` backstop, which still matters for the pool's own
``shutdown()`` call) sets the event in time for an in-flight sleeper to
abort instead of running to completion.

This is exercised via a real subprocess (not in-process monkeypatching of
``atexit``/``threading._shutdown``, which would not reproduce the actual
ordering) that starts a 5s pool sleeper and then falls off the end of
``__main__`` — a real, unforced interpreter exit.
"""

import subprocess
import sys
import textwrap
import time

_SCRIPT = textwrap.dedent(
    """
    import asyncio
    from src.agent.slack_client import _sleep_interruptibly
    from src.services.slack_executor import run_slack_call

    async def main():
        # Fire-and-forget: do not await it. The interpreter exits at the end
        # of __main__ while this is still sleeping on the pool.
        asyncio.ensure_future(run_slack_call(_sleep_interruptibly, 5))
        await asyncio.sleep(0.2)

    asyncio.run(main())
    """
)


def test_a_pool_sleeper_is_aborted_at_interpreter_exit_not_run_to_completion():
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=10,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr
    assert elapsed < 3.0, (
        f"process took {elapsed:.2f}s to exit — the pool sleeper was not "
        f"aborted before threading._shutdown() joined it (stderr={result.stderr!r})"
    )
