"""Tests for canonical ts-shaped id minting (src/agent/ids.py)."""

from src.agent.ids import TsMinter, mint_local_ts


class TestTsMinter:
    def test_monotonic_unique_and_float_ordered(self):
        m = TsMinter()
        ids = [m.mint() for _ in range(2000)]
        # Distinct strings (DB uniqueness) ...
        assert len(set(ids)) == len(ids)
        # ... and strictly increasing once parsed to float (posted_at ordering).
        floats = [float(x) for x in ids]
        assert all(b > a for a, b in zip(floats, floats[1:], strict=False))

    def test_ts_shape_is_seconds_dot_six_microsecond_digits(self):
        m = TsMinter()
        secs, _, micros = m.mint().partition(".")
        assert secs.isdigit()
        assert len(micros) == 6 and micros.isdigit()

    def test_seed_floor_pushes_ids_after_a_future_high_water_mark(self):
        import time

        m = TsMinter()
        future = time.time() + 3600
        m.seed_floor(future)
        assert float(m.mint()) > future

    def test_seed_floor_never_lowers_the_mark(self):
        m = TsMinter()
        first = m.mint()
        m.seed_floor(0.0)  # far below the current wall clock — must be ignored
        assert m.mint() > first


class TestModuleDefaultMinter:
    def test_mint_local_ts_is_monotonic_across_calls(self):
        # The process-wide minter used by the web PI inbox and GrantBot.
        a = mint_local_ts()
        b = mint_local_ts()
        assert b > a
        assert a != b
