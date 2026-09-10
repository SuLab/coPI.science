"""A dedicated thread pool for synchronous Slack Web API calls.

Every ``asyncio.to_thread`` wrapper around a blocking ``AgentSlackClient``/
``httpx`` Slack call previously shared the event loop's *default* executor
(``min(32, os.cpu_count() + 4)`` threads) with every other ``to_thread`` user in
the process. Each Slack call can block for up to
``slack_client.RATE_LIMIT_WAIT_BUDGET_SECONDS`` (180s) under a sustained
throttle, so a run of Slack calls could occupy enough of that shared pool to
starve unrelated ``to_thread`` work (DB migrations off the loop, CPU-bound
helpers, etc.) that has nothing to do with Slack.

``run_slack_call`` gives Slack I/O its own bounded pool instead, so a Slack
throttle can only ever starve *other Slack calls*, never the rest of the
process. Use it everywhere a synchronous Slack Web API call (through
``AgentSlackClient`` or the ``slack_web``/``slack_provisioning`` httpx helpers)
is moved off the event loop; leave non-Slack ``to_thread`` call sites alone.
"""

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")

# 8 workers: comfortably more than the number of Slack calls any single turn or
# request makes concurrently, small enough that a sustained throttle across the
# whole pool is a Slack-specific incident rather than a process-wide one.
_SLACK_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="slack-io")


async def run_slack_call(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a synchronous Slack call on the dedicated Slack I/O executor.

    Drop-in replacement for ``asyncio.to_thread(fn, *args, **kwargs)`` at every
    Slack call site — same signature, same "runs in a worker thread and awaits
    the result" behaviour, same exception propagation. The only difference is
    *which* thread pool it runs on.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_SLACK_EXECUTOR, functools.partial(fn, *args, **kwargs))
