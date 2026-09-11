from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from src.services import display_format as f


def test_money_two_decimals_and_floor_prefix():
    assert f.money(Decimal("31.17807215")) == "$31.18"
    assert f.money(0) == "$0.00"
    assert f.money(Decimal("9"), floor=True) == "≥ $9.00"


def test_money_sub_cent_is_not_reported_as_zero():
    # A positive amount that rounds to $0.00 must stay distinguishable from an
    # agent that spent nothing at all; exact zero still reads "$0.00".
    assert f.money(Decimal("0.00005")) == "< $0.01"
    assert f.money(0) == "$0.00"
    # The floor claim outranks the sub-cent claim: "≥ under a cent" is nonsense.
    assert f.money(Decimal("0.00005"), floor=True) == "≥ $0.00"


def test_count_thousands_separated():
    assert f.count(133820) == "133,820"
    assert f.count(0) == "0"


def test_compact_tokens():
    assert f.compact(812) == "812"
    assert f.compact(94_365) == "94.4K"
    assert f.compact(1_318_439) == "1.3M"
    assert f.compact(2_474_129_000) == "2.5B"


def test_duration_buckets():
    assert f.duration(47) == "47s"
    assert f.duration(60) == "1m 00s"
    assert f.duration(3849) == "1h 04m"
    assert f.duration(2 * 86400 + 3 * 3600) == "2d 03h"


def test_hour_label_is_utc_month_day_hour_and_locale_free():
    assert f.hour_label(datetime(2026, 9, 9, 18, tzinfo=UTC)) == "Sep 9 18:00"
    est = datetime(2026, 9, 9, 14, tzinfo=timezone(timedelta(hours=-4)))
    assert f.hour_label(est) == "Sep 9 18:00"
    assert f.hour_label(datetime(2026, 1, 1, 0)) == "Jan 1 00:00"  # naive -> UTC


def test_timestamp_minute_precision_and_dash_for_none():
    assert f.timestamp(datetime(2026, 9, 9, 18, 47, 56, 231218, tzinfo=UTC)) == "2026-09-09 18:47 UTC"
    assert f.timestamp(None) == "—"


def test_epoch_hm():
    assert f.epoch_hm(1788979762.728549) == "18:49"  # 2026-09-09T18:49:22Z


def test_plural_percent_whole():
    assert f.plural(1, "call") == "1 call"
    assert f.plural(7, "call") == "7 calls"
    assert f.percent(0.2894) == "29%"
    assert f.percent(0) == "0%"
    assert f.whole(0.0) == "0" and f.whole(3.0) == "3" and f.whole(12.5) == "12.5"


def test_epoch_label_carries_the_date_for_multi_day_runs():
    assert f.epoch_label(1788979762.728549) == "Sep 9 18:49"  # 2026-09-09T18:49:22Z
    assert f.epoch_label(1767225600.0) == "Jan 1 00:00"       # 2026-01-01T00:00:00Z


def test_whole_mid_rounds_a_count_axis_mid_tick_to_an_integer():
    assert f.whole_mid(3.5) == "4"
    assert f.whole_mid(6) == "6"
    assert f.whole_mid(0.0) == "0"
