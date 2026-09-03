"""Unit tests for src.database.make_engine (issue #25 P3). No connection is made —
create_async_engine only builds the Engine/Pool objects; SQLAlchemy connects lazily
on first checkout."""

from src.database import make_engine


def test_make_engine_applies_pre_ping_recycle_and_timeout_by_default():
    engine = make_engine("postgresql+asyncpg://u:p@localhost/db")
    pool = engine.pool
    assert pool._pre_ping is True
    assert pool._recycle == 1800
    assert pool._timeout == 30


def test_make_engine_overrides_win_over_the_defaults():
    engine = make_engine(
        "postgresql+asyncpg://u:p@localhost/db",
        pool_size=5, max_overflow=10, pool_recycle=60,
    )
    pool = engine.pool
    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool._recycle == 60
