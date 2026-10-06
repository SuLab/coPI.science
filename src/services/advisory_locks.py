"""The one registry of Postgres advisory-lock keys, and the session-level lock
the engine (and the supervisor's finalize routine) hold on a dedicated
connection.

Two key shapes (spec §8.3, SA2-15/SA3-09):

* **Fixed keys** — ``int.from_bytes(sha256(name)[:8], "big", signed=True)``,
  the convention ``assessment_chat._SPEND_LOCK_KEY`` already uses (C22).
  ``tests/unit/test_advisory_locks.py`` asserts they are distinct from each
  other and from the spend lock.
* **Per-entity keys** — computed in SQL by ``hashtextextended('<ns>:' ||
  CAST(:id AS text), 0)`` (deterministic and signed on Postgres 15, C24), for
  example ``corpus:<user_id>``, ``provision:<agent_id>`` and ``agent:<user_id>``
  (``lock_agent_persona``).
  ``CAST`` rather than ``::`` because SQLAlchemy ``text()`` does not bind
  ``:id`` directly before ``::`` (SA4-16).

A bigint advisory key appears in ``pg_locks`` as ``classid = key >> 32``,
``objid = key & 0xffffffff``, ``objsubid = 1`` (C24); ``advisory_lock_held``
masks both halves so negative keys match, and filters on the current database
so a lock taken in a scratch database on the same server is ignored (SA3-07).
"""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from src.database import make_engine


def fixed_key(name: str) -> int:
    """The signed 64-bit key for a fixed lock name (C22)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


#: Held by the one running simulation engine for its whole life (B4).
ENGINE_LOCK_KEY: int = fixed_key("engine")
#: Held by the one running worker (Phase 3, AP-11).
WORKER_LOCK_KEY: int = fixed_key("worker")
#: Taken per transaction by the last-admin check (Phase 3, RA-09).
ADMIN_INVARIANT_LOCK_KEY: int = fixed_key("admin_invariant")
#: Taken per transaction by every activation of a hub-role agent, so two concurrent
#: activations cannot both see "no other active hub" (spec §6.8 C-05, D14).
HUB_ROSTER_LOCK_KEY: int = fixed_key("hub_roster")


def entity_key_sql(namespace: str) -> str:
    """SQL for a per-entity key, bound through ``:id`` (for example
    ``corpus:<user_id>`` or ``provision:<agent_id>``).

    Bind ``id`` as a ``str`` (``str(user.id)`` for a UUID): the parameter is typed
    ``text`` by the cast, and asyncpg refuses a ``uuid.UUID`` for it."""
    return f"hashtextextended('{namespace}:' || CAST(:id AS text), 0)"


async def lock_agent_persona(db: AsyncSession, user_id: uuid.UUID) -> None:
    """pg_advisory_xact_lock on agent:<user_id>, held until the caller's transaction ends:
    serialises persona file writes, the persona revision transaction and account deletion
    for one PI (spec 2026-10-05 §4.3)."""
    await db.execute(
        text(f"SELECT pg_advisory_xact_lock({entity_key_sql('agent')})"), {"id": str(user_id)}
    )


_HELD_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks "
    "WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid = ((CAST(:key AS bigint) >> 32) & 4294967295)::oid "
    "AND objid = (CAST(:key AS bigint) & 4294967295)::oid)"
)


async def advisory_lock_held(db: AsyncSession, key: int) -> bool:
    """True when some session in this database holds session-level ``key``."""
    return bool(await db.scalar(_HELD_SQL, {"key": key}))


class SessionAdvisoryLock:
    """A session-level advisory lock on a dedicated AUTOCOMMIT connection
    outside any pool (spec §8.3). AUTOCOMMIT keeps the connection out of an
    open transaction, so ``idle_in_transaction_session_timeout`` cannot drop it.
    The lock lives exactly as long as the connection."""

    def __init__(self, database_url: str, key: int) -> None:
        self._url = database_url
        self._key = key
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        self._held = False

    async def acquire(self) -> bool:
        """``pg_try_advisory_lock``; True when this connection now holds it."""
        self._engine = make_engine("advisory_lock", self._url)
        self._conn = await self._engine.connect()
        self._held = bool(await self._conn.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
        ))
        return self._held

    async def check(self) -> None:
        """``SELECT 1`` on the lock connection; raises if the connection died."""
        if self._conn is None:
            raise RuntimeError("advisory lock connection is not open")
        await self._conn.execute(text("SELECT 1"))

    async def backend_pid(self) -> int:
        """The lock connection's server pid (diagnostics and tests)."""
        if self._conn is None:
            raise RuntimeError("advisory lock connection is not open")
        return int(await self._conn.scalar(text("SELECT pg_backend_pid()")))

    async def release(self) -> None:
        """``pg_advisory_unlock`` (best effort), then close. Idempotent."""
        conn, engine = self._conn, self._engine
        self._conn, self._engine = None, None
        try:
            if conn is not None and self._held:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
        except Exception:  # noqa: BLE001 — a dead connection already released it
            pass
        finally:
            self._held = False
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if engine is not None:
                await engine.dispose()
