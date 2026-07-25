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
the fractional part through a float.

Why a writer slot: a per-process counter cannot stop two *processes* minting the
identical id in the same microsecond, and the DB's only recourse — the
``uq_agent_messages_run_ts`` constraint plus the on-conflict guard — resolves
such a collision by *dropping* one of the two messages. Slack never had this
problem (it issues a globally unique ts) and the DB is now the sole durable
store, so a dropped message is unrecoverable. Each minter therefore owns a
residue class of the microsecond field: ids are quantized to a
``WRITER_SLOT_MODULUS``-microsecond slot and stamped with the writer's id in the
low digits, making a cross-writer collision structurally impossible while
keeping the id ts-shaped, float-parseable and strictly ordered. The cost is
resolution (a writer can mint one id per slot), which is orders of magnitude
above the real posting rate.

See specs/local-db-conversations.md, the PR #19 review (M1 / mint precision) and
.notes/db-conversations-residual-2026-07-24.md (R1).
"""

from __future__ import annotations

import threading
import time

# Microseconds per slot. The low 2 digits of the 6-digit microsecond field carry
# the writer id, so ids from different writers can never coincide. Keep this a
# power of ten so the ids stay readable and the slot boundary is obvious.
WRITER_SLOT_MODULUS = 100

# Writer ids (0 <= id < WRITER_SLOT_MODULUS). Every process/minter that writes a
# canonical id into a shared table needs a distinct one. Both the engine's own
# minter and the module default used *within* the engine process are listed, so
# they can never collide with each other either.
WRITER_ENGINE = 0        # SimulationEngine._ts_minter (agent_messages)
WRITER_WEB = 1           # web app process (PI messages + DMs)
WRITER_GRANTBOT = 2      # grantbot process (funding posts)
WRITER_ENGINE_AUX = 3    # module default inside the engine process (PI DMs)


def _fmt(us: int) -> str:
    """Format integer microseconds-since-epoch as a ts-shaped id string."""
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


class TsMinter:
    """Thread-safe minter of monotonic ts-shaped ids, unique across writers.

    Ids are unique **per process** by the instance's own counter, and unique
    **across processes** by ``writer_id``: every id this minter returns is
    congruent to ``writer_id`` modulo ``WRITER_SLOT_MODULUS``, a residue class no
    other correctly-configured minter uses. The DB's
    ``uq_agent_messages_run_ts`` constraint remains the backstop, but it should
    no longer be reachable by concurrent minting.
    """

    def __init__(self, writer_id: int = WRITER_ENGINE) -> None:
        if not 0 <= writer_id < WRITER_SLOT_MODULUS:
            raise ValueError(
                f"writer_id must be in [0, {WRITER_SLOT_MODULUS}), got {writer_id}"
            )
        self._writer_id = writer_id
        self._last_slot = 0
        self._lock = threading.Lock()

    @property
    def writer_id(self) -> int:
        return self._writer_id

    def seed_floor(self, seconds: float) -> None:
        """Raise the high-water mark so subsequent ids sort after ``seconds``.

        Called after a DB rebuild with the max ``posted_at`` seen, so minted ids
        always sort after restored history — including history minted by another
        writer, whose ids fall in a different residue class.
        """
        with self._lock:
            self._last_slot = max(
                self._last_slot, round(seconds * 1_000_000) // WRITER_SLOT_MODULUS
            )

    def mint(self) -> str:
        """Return the next monotonic id in this writer's residue class."""
        with self._lock:
            slot = time.time_ns() // 1000 // WRITER_SLOT_MODULUS
            # Strictly advance: never reuse a slot, so ids stay unique and
            # increasing even when several are minted inside one slot window.
            if slot <= self._last_slot:
                slot = self._last_slot + 1
            self._last_slot = slot
        return _fmt(slot * WRITER_SLOT_MODULUS + self._writer_id)


# Process-wide default minter for writers that don't own a SimulationEngine
# instance — the PI web inbox (src/services/pi_inbox.py) and GrantBot
# (src/agent/grantbot.py). Each *process* must claim its writer id at startup
# via set_default_writer_id(); the default below is the web app, the most
# common host for this minter. See WRITER_* above.
_default = TsMinter(WRITER_WEB)


def set_default_writer_id(writer_id: int) -> None:
    """Claim a writer id for this process's default minter.

    Call at process entry, before anything mints. Swaps in a minter for the new
    residue class, carrying over the outgoing minter's high-water mark so ids
    stay monotonic across the swap even if something already minted (a fresh
    counter could otherwise reuse a slot within the same microsecond window).
    """
    global _default
    old = _default
    new = TsMinter(writer_id)
    with old._lock:
        new._last_slot = old._last_slot
    _default = new


def default_writer_id() -> int:
    """Return the writer id the process-wide default minter is using."""
    return _default.writer_id


def mint_local_ts() -> str:
    """Mint a ts-shaped id from the process-wide default minter."""
    return _default.mint()
