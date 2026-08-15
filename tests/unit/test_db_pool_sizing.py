"""The agent engine must have room for every concurrent task, and a write that
fails on pool checkout must not vanish."""


def test_pool_is_larger_than_the_max_concurrent_reply_tasks():
    from src.config import get_settings

    s = get_settings()
    total = s.db_pool_size + s.db_max_overflow
    # Each reply task can hold one session; the loop's own pollers and flushers
    # need headroom on top.
    assert total >= s.reply_lane_max_in_flight + 10, (
        f"pool of {total} cannot serve {s.reply_lane_max_in_flight} concurrent "
        "reply tasks plus the loop's own writers"
    )


def test_agent_engine_is_constructed_with_explicit_pool_settings():
    import inspect

    from src.agent import main as agent_main

    src = inspect.getsource(agent_main)
    assert "pool_size=settings.db_pool_size" in src
    assert "max_overflow=settings.db_max_overflow" in src
