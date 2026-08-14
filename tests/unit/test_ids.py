"""Tests for canonical ts-shaped id minting (src/agent/ids.py)."""

import pytest

from src.agent.ids import (
    WRITER_ENGINE,
    WRITER_GRANTBOT,
    WRITER_SLOT_MODULUS,
    WRITER_WEB,
    TsMinter,
    default_writer_id,
    mint_local_ts,
    set_default_writer_id,
)


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

    def test_seed_floor_from_another_writers_id_still_sorts_after(self):
        # Restored history can be a *different* writer's id, which sits in a
        # different residue class — the floor must clear it regardless.
        other = TsMinter(WRITER_GRANTBOT)
        history = float(other.mint())
        m = TsMinter(WRITER_ENGINE)
        m.seed_floor(history)
        assert float(m.mint()) > history


class TestWriterSlots:
    """R1: two processes minting in the same microsecond must not collide."""

    def test_ids_carry_the_writer_id_in_the_low_digits(self):
        for writer_id in (WRITER_ENGINE, WRITER_WEB, WRITER_GRANTBOT):
            m = TsMinter(writer_id)
            us = round(float(m.mint()) * 1_000_000)
            # float() round-trips only ~0.25us at this magnitude, so compare the
            # residue on the integer microseconds parsed from the string parts.
            secs, _, micros = m.mint().partition(".")
            assert int(micros) % WRITER_SLOT_MODULUS == writer_id
            assert us % WRITER_SLOT_MODULUS == writer_id

    def test_concurrent_writers_never_produce_the_same_id(self):
        # The exact scenario the DB constraint used to catch by dropping a
        # message: independent minters running flat out at the same instant.
        engine = TsMinter(WRITER_ENGINE)
        web = TsMinter(WRITER_WEB)
        grantbot = TsMinter(WRITER_GRANTBOT)
        ids = []
        for _ in range(500):
            ids.append(engine.mint())
            ids.append(web.mint())
            ids.append(grantbot.mint())
        assert len(set(ids)) == len(ids)

    def test_same_wall_clock_instant_still_disjoint(self, monkeypatch):
        # Pin the clock so every mint sees the identical microsecond — the
        # per-process counters alone would hand out the same id here.
        import time as time_mod

        monkeypatch.setattr(time_mod, "time_ns", lambda: 1_800_000_000_000_000_000)
        engine = TsMinter(WRITER_ENGINE)
        web = TsMinter(WRITER_WEB)
        assert engine.mint() != web.mint()

    def test_rejects_an_out_of_range_writer_id(self):
        with pytest.raises(ValueError):
            TsMinter(WRITER_SLOT_MODULUS)
        with pytest.raises(ValueError):
            TsMinter(-1)


class TestModuleDefaultMinter:
    def test_mint_local_ts_is_monotonic_across_calls(self):
        # The process-wide minter used by the web PI inbox and GrantBot.
        a = mint_local_ts()
        b = mint_local_ts()
        assert b > a
        assert a != b

    def test_set_default_writer_id_switches_residue_class(self):
        original = default_writer_id()
        try:
            set_default_writer_id(WRITER_GRANTBOT)
            assert default_writer_id() == WRITER_GRANTBOT
            _, _, micros = mint_local_ts().partition(".")
            assert int(micros) % WRITER_SLOT_MODULUS == WRITER_GRANTBOT
        finally:
            set_default_writer_id(original)

    def test_set_default_writer_id_keeps_ids_monotonic_across_the_swap(self):
        original = default_writer_id()
        try:
            set_default_writer_id(WRITER_WEB)
            before = mint_local_ts()
            # A fresh counter would restart from the current microsecond and
            # could reuse the slot just consumed; the high-water mark carries.
            set_default_writer_id(WRITER_GRANTBOT)
            after = mint_local_ts()
            assert float(after) > float(before)
        finally:
            set_default_writer_id(original)
