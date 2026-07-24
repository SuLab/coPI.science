"""Canonical message-id minting: monotonic, unique, ts-shaped ids.

A *ts-shaped* id is a decimal ``"<seconds>.<microseconds>"`` string, matching
the Slack ts format so the same id column and ``float(ts)`` ordering work whether
a message originated in Slack or was minted locally (Slack-off / DB-origin).

Why integers: ``float64`` cannot hold microsecond precision at current epoch
magnitudes — the ULP at ~1.75e9 s is ~2.4e-7 s, coarser than the 1e-6 s step the
old ``f"{time.time():.6f}"`` scheme relied on. Two ids minted in the same tick
could therefore format to the *same* 6-decimal string (breaking uniqueness) or
fail to be strictly increasing once round-tripped through a float (breaking the
posted_at ordering). We instead carry a monotonic **integer-microsecond**
high-water mark and only format to a string at the very end, never round-tripping
the fractional part through a float. See specs/local-db-conversations.md and the
PR #19 review (H1 flush-loss is separate; this addresses M1 / mint precision).
"""

from __future__ import annotations

import threading
import time


def _fmt(us: int) -> str:
    """Format integer microseconds-since-epoch as a ts-shaped id string."""
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


class TsMinter:
    """Thread-safe, per-instance minter of monotonic, unique ts-shaped ids.

    The monotonic/unique guarantee is **per process** (one counter). Two
    processes cannot share a counter, so cross-process uniqueness is enforced at
    the DB layer (the ``uq_agent_messages_run_ts`` constraint plus conflict
    handling), not here.
    """

    def __init__(self) -> None:
        self._last_us = 0
        self._lock = threading.Lock()

    def seed_floor(self, seconds: float) -> None:
        """Raise the high-water mark so subsequent ids sort after ``seconds``.

        Called after a DB rebuild with the max ``posted_at`` seen, so minted ids
        always sort after restored history.
        """
        with self._lock:
            self._last_us = max(self._last_us, round(seconds * 1_000_000))

    def mint(self) -> str:
        """Return the next monotonic, unique ts-shaped id."""
        with self._lock:
            val_us = max(time.time_ns() // 1000, self._last_us + 1)
            self._last_us = val_us
        return _fmt(val_us)


# Process-wide default minter for writers that don't own a SimulationEngine
# instance — the PI web inbox (src/services/pi_inbox.py) and GrantBot
# (src/agent/grantbot.py). Using it gives them the same per-process monotonic,
# unique guarantee the engine's minter has, replacing raw ``f"{time.time():.6f}"``.
_default = TsMinter()


def mint_local_ts() -> str:
    """Mint a ts-shaped id from the process-wide default minter."""
    return _default.mint()
