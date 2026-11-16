"""The pairwise tournament draw, the win-fraction order and the daily cut timing (spec §7.1,
§7.6, §7.8, F8)."""
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from src.landscaping.comparator import deep_text, tournament_pairs, win_fraction_order
from src.landscaping.stages.cut import cut_due
from src.models.landscape import LandscapeDeep


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


def test_round_robin_up_to_20_else_balanced_10_regular():
    twenty = _ids(20)
    pairs = tournament_pairs(twenty, full_up_to=20, seed=7)
    assert {frozenset(p) for p in pairs} == {
        frozenset((a, b)) for i, a in enumerate(twenty) for b in twenty[i + 1:]
    }
    assert len(pairs) == 190

    for n in (21, 57):
        ids = _ids(n)
        pairs = tournament_pairs(ids, full_up_to=20, seed=7)
        assert all(a != b for a, b in pairs)
        assert len({frozenset(p) for p in pairs}) == len(pairs)
        met: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for a, b in pairs:
            met[a].add(b)
            met[b].add(a)
        assert set(met) == set(ids)
        assert all(len(others) == 10 for others in met.values())
        assert tournament_pairs(ids, full_up_to=20, seed=7) == pairs


def test_win_fraction_counts_a_disagreement_as_half():
    a, b, c = _ids(3)
    # a beats b in both orders; a and c disagree (half each); c beats b in both orders.
    results = [(a, b, [a, a]), (a, c, [a, c]), (b, c, [c, c])]
    # a and c both have 1.5 / 2; the tiebreak puts c first.
    assert win_fraction_order(results, tiebreak={c: 0, a: 1, b: 2}.__getitem__) == [c, a, b]
    assert win_fraction_order(results, tiebreak={a: 0, c: 1, b: 2}.__getitem__) == [a, c, b]

    # A lone disagreement is a tie: the tiebreak alone decides.
    x, y = _ids(2)
    assert win_fraction_order([(x, y, [x, y])], tiebreak={y: 0, x: 1}.__getitem__) == [y, x]
    assert win_fraction_order([(x, y, [x, x])], tiebreak={y: 0, x: 1}.__getitem__) == [x, y]


def test_cut_due_on_drain_or_0600_utc():
    day = datetime(2026, 10, 30, tzinfo=UTC)
    yesterday = day - timedelta(hours=20)
    # Before 06:00: only once S4 has drained.
    assert not cut_due(now=day.replace(hour=3), s4_pending=4, last_cut_at=yesterday)
    assert cut_due(now=day.replace(hour=3), s4_pending=0, last_cut_at=yesterday)
    # At 06:00 UTC, pending work or not.
    assert cut_due(now=day.replace(hour=6), s4_pending=4, last_cut_at=yesterday)
    # One cut per day: whichever came first.
    assert not cut_due(now=day.replace(hour=9), s4_pending=0, last_cut_at=day.replace(hour=6))
    assert not cut_due(now=day.replace(hour=9), s4_pending=4, last_cut_at=day.replace(hour=2))
    # No cut yet.
    assert cut_due(now=day.replace(hour=1), s4_pending=0, last_cut_at=None)


def test_deep_text_prefers_the_s6_summary():
    record = LandscapeDeep(gating={"invented_gate": "met"}, outside_group_count=2)
    # No summary: the judged fields as JSON.
    assert '"invented_gate": "met"' in deep_text(record)
    record.summary = "Invented S6 summary of the idea."
    assert deep_text(record) == "Invented S6 summary of the idea."
    record.summary = "   "
    assert '"outside_group_count": 2' in deep_text(record)
