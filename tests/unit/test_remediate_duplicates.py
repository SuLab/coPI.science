"""Pure-logic tests for scripts/migrate/remediate_duplicates.py.

No database: everything here exercises group classification, strategy selection
and replacement-id generation, which is where a mistake would silently delete a
message or fabricate a Slack timestamp. The DB layer is covered separately
against throwaway Postgres databases (see the report for that change).

The tool is a script, not an importable package, so it is loaded by path. The
``sys.modules`` registration before ``exec_module`` is load-bearing rather than
tidiness: ``@dataclass`` looks its own module up in ``sys.modules`` to resolve
annotations, and without the entry every dataclass in the file raises
``AttributeError: 'NoneType' object has no attribute '__dict__'`` at import.
"""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.agent import ids as ids_mod

_TOOL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "migrate" / "remediate_duplicates.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("_remediate_duplicates", _TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rd = _load_tool()


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

BASE_TIME = datetime(2026, 7, 1, tzinfo=UTC)

#: The 0018 column set, which is what production has when this tool matters most.
COLUMNS_0018 = {
    "agent_id": "chen",
    "channel_id": "C0SLACK1",
    "channel_name": "general",
    "message_length": 100,
    "phase": "new_post",
    "thread_ts": None,
    "visibility": "public",
}

#: The columns 0019 adds.
COLUMNS_0019_EXTRA = {
    "content": "body",
    "sender_name": "ChenBot",
    "is_bot": True,
    "posted_at": 1755000001.000017,
    "slack_ts": None,
    "slack_channel_id": None,
    "slack_thread_ts": None,
}


def make_row(row_id="a1", ts="1755000001.000017", *, run="run-1", seconds=0,
             at_0019=False, **overrides):
    """Build a MessageRow. ``overrides`` set individual columns."""
    columns = dict(COLUMNS_0018)
    if at_0019:
        columns.update(COLUMNS_0019_EXTRA)
    columns.update(overrides)
    created = BASE_TIME + timedelta(seconds=seconds)
    columns.update({"id": row_id, "simulation_run_id": run, "message_ts": ts,
                    "created_at": created})
    return rd.MessageRow(row_id=row_id, run_id=run, message_ts=ts, created_at=created,
                         columns=columns)


WRITER_SLOTS = {0: "WRITER_ENGINE", 1: "WRITER_WEB", 2: "WRITER_GRANTBOT",
                3: "WRITER_ENGINE_AUX"}


def classify(row, *, at_0019=False):
    return rd.classify_origin(row, has_slack_columns=at_0019, writer_slots=WRITER_SLOTS,
                              modulus=100)


def group_of(*rows, **kwargs):
    return rd.DuplicateGroup(rows[0].run_id, rows[0].message_ts, list(rows), **kwargs)


def plan(group, strategy=rd.STRATEGY_RENUMBER, *, used=None, now_us=1_800_000_000_000_000,
         at_0019=False):
    if used is None:
        used = {r.message_ts for r in group.rows}
    rd.plan_group(group, strategy=strategy, used=used, now_us=now_us,
                  has_slack_columns=at_0019, writer_slots=WRITER_SLOTS, modulus=100)
    return group


# --------------------------------------------------------------------------- #
# ts parsing / formatting
# --------------------------------------------------------------------------- #

class TestTsShape:
    @pytest.mark.parametrize(
        ("ts", "expected"),
        [
            ("1755000001.000017", 1755000001000017),
            ("0.000000", 0),
            ("1755000005.000000", 1755000005000000),
            ("1755000005.999999", 1755000005999999),
        ],
    )
    def test_parses_ts_shaped_ids(self, ts, expected):
        assert rd.parse_ts_us(ts) == expected

    @pytest.mark.parametrize(
        "ts",
        [
            None, "", "not-a-slack-ts", "1755000005", "1755000005.", ".000001",
            # Fewer than six fractional digits is AMBIGUOUS (2µs or 200000µs?), so
            # it is refused rather than guessed at.
            "1755000005.2", "1755000005.00001",
            "1755000005.0000001",   # seven digits
            "1755000005.00000a",
            "-1755000005.000001",
            " 1755000005.000001",
            "1755000005.000001 ",
            "1755000005.000001\n",
        ],
    )
    def test_refuses_everything_else(self, ts):
        assert rd.parse_ts_us(ts) is None

    def test_format_round_trips(self):
        for us in (0, 1, 999_999, 1_000_000, 1755000005000099):
            assert rd.parse_ts_us(rd.format_us(us)) == us

    def test_format_agrees_with_the_minter_it_must_match(self):
        # If these two ever disagree, remediated ids stop being the same shape as
        # minted ones and float(ts) ordering silently changes meaning.
        for us in (0, 1, 999_999, 1_000_000, 1755000005000099, 1785894150064299):
            assert rd.format_us(us) == ids_mod._fmt(us)


# --------------------------------------------------------------------------- #
# Origin classification
# --------------------------------------------------------------------------- #

class TestClassifyOrigin:
    def test_local_channel_is_confirmed_local(self):
        origin = classify(make_row(channel_id="local:cryo-em"))
        assert origin.verdict == rd.ORIGIN_LOCAL_CONFIRMED
        assert "never" in origin.evidence

    def test_malformed_ts_is_confirmed_local(self):
        origin = classify(make_row(ts="not-a-slack-ts"))
        assert origin.verdict == rd.ORIGIN_LOCAL_CONFIRMED
        assert "not ts-shaped" in origin.evidence

    def test_slack_ts_equal_to_message_ts_is_confirmed_slack(self):
        row = make_row(ts="1755000001.000017", at_0019=True, slack_ts="1755000001.000017")
        assert classify(row, at_0019=True).verdict == rd.ORIGIN_SLACK_CONFIRMED

    def test_slack_ts_differing_from_message_ts_is_confirmed_local(self):
        # 0019 keeps the two apart, so message_ts is a canonical id by construction.
        row = make_row(ts="1755000001.000017", at_0019=True, slack_ts="1755000009.123456")
        assert classify(row, at_0019=True).verdict == rd.ORIGIN_LOCAL_CONFIRMED

    def test_slack_ts_columns_ignored_when_the_revision_predates_them(self):
        # Same row, but told the schema has no slack_ts: must fall through to the
        # residue heuristic rather than trusting a column it cannot see.
        row = make_row(ts="1755000001.000017", at_0019=True, slack_ts="1755000001.000017")
        assert classify(row, at_0019=False).verdict == rd.ORIGIN_SLACK_PRESUMED

    @pytest.mark.parametrize(
        ("residue", "writer"),
        [("000000", "WRITER_ENGINE"), ("000001", "WRITER_WEB"),
         ("000002", "WRITER_GRANTBOT"), ("000003", "WRITER_ENGINE_AUX"),
         ("123400", "WRITER_ENGINE"), ("999903", "WRITER_ENGINE_AUX")],
    )
    def test_writer_slot_residue_is_presumed_local(self, residue, writer):
        origin = classify(make_row(ts=f"1755000007.{residue}"))
        assert origin.verdict == rd.ORIGIN_LOCAL_PRESUMED
        assert writer in origin.evidence

    @pytest.mark.parametrize("residue", ["000017", "000042", "000057", "000088", "000091",
                                         "123456", "999999"])
    def test_anything_else_in_a_slack_channel_is_presumed_slack(self, residue):
        origin = classify(make_row(ts=f"1755000007.{residue}"))
        assert origin.verdict == rd.ORIGIN_SLACK_PRESUMED
        assert origin.is_slack

    def test_local_channel_beats_the_residue_heuristic(self):
        # Legacy rows minted by the pre-writer-slot f"{time.time():.6f}" scheme have
        # arbitrary residues; the channel is the stronger signal and must win.
        origin = classify(make_row(ts="1755000003.000057", channel_id="local:cryo-em"))
        assert origin.verdict == rd.ORIGIN_LOCAL_CONFIRMED

    def test_is_slack_property_covers_exactly_the_two_slack_verdicts(self):
        assert rd.Origin(rd.ORIGIN_SLACK_CONFIRMED, "").is_slack
        assert rd.Origin(rd.ORIGIN_SLACK_PRESUMED, "").is_slack
        assert not rd.Origin(rd.ORIGIN_LOCAL_CONFIRMED, "").is_slack
        assert not rd.Origin(rd.ORIGIN_LOCAL_PRESUMED, "").is_slack


# --------------------------------------------------------------------------- #
# Renumber safety
# --------------------------------------------------------------------------- #

class TestRenumberVerdict:
    def test_confirmed_local_is_safe(self):
        verdict, _ = rd.renumber_verdict(
            make_row(), rd.Origin(rd.ORIGIN_LOCAL_CONFIRMED, ""), has_slack_columns=False
        )
        assert verdict == rd.RENUMBER_SAFE

    def test_presumed_local_is_safe_but_says_what_it_costs_if_wrong(self):
        verdict, reason = rd.renumber_verdict(
            make_row(), rd.Origin(rd.ORIGIN_LOCAL_PRESUMED, ""), has_slack_columns=False
        )
        assert verdict == rd.RENUMBER_SAFE
        assert "backfill_slack_ts" in reason

    def test_confirmed_slack_is_safe_at_0019_because_slack_ts_holds_the_timestamp(self):
        verdict, reason = rd.renumber_verdict(
            make_row(), rd.Origin(rd.ORIGIN_SLACK_CONFIRMED, ""), has_slack_columns=True
        )
        assert verdict == rd.RENUMBER_SAFE
        assert "slack_ts keeps" in reason

    def test_presumed_slack_is_never_safe(self):
        for has_slack in (False, True):
            verdict, reason = rd.renumber_verdict(
                make_row(), rd.Origin(rd.ORIGIN_SLACK_PRESUMED, ""),
                has_slack_columns=has_slack,
            )
            assert verdict == rd.RENUMBER_UNSAFE
            assert "only record" in reason

    def test_the_0018_reason_names_the_missing_column(self):
        _, reason = rd.renumber_verdict(
            make_row(), rd.Origin(rd.ORIGIN_SLACK_PRESUMED, ""), has_slack_columns=False
        )
        assert "0018 schema has no slack_ts column" in reason


# --------------------------------------------------------------------------- #
# Payload comparison: the whole basis for "safe to delete"
# --------------------------------------------------------------------------- #

class TestPayloadIdentity:
    def test_rows_differing_only_in_id_are_identical(self):
        assert make_row("a1").payload() == make_row("a2").payload()

    def test_rows_differing_only_in_created_at_are_identical(self):
        assert make_row("a1", seconds=0).payload() == make_row("a2", seconds=5).payload()

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("agent_id", "patel"),
            ("channel_id", "C0SLACK2"),
            ("channel_name", "proteomics"),
            ("message_length", 0),
            ("phase", "thread_reply"),
            ("thread_ts", "1755000000.000005"),
            ("visibility", "collab_private"),
        ],
    )
    def test_any_other_column_makes_them_divergent_at_0018(self, column, value):
        assert make_row("a1").payload() != make_row("a2", **{column: value}).payload()

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("content", "something else entirely"),
            ("content", ""),
            ("sender_name", "PatelBot"),
            ("is_bot", False),
            ("posted_at", 1.0),
            ("slack_ts", "1755000001.000017"),
            ("slack_channel_id", "C0SLACK9"),
            ("slack_thread_ts", "1755000000.000005"),
        ],
    )
    def test_the_0019_columns_count_as_payload_too(self, column, value):
        left = make_row("a1", at_0019=True)
        right = make_row("a2", at_0019=True, **{column: value})
        assert left.payload() != right.payload()

    @pytest.mark.parametrize(
        ("channel", "expected_resolution"),
        [
            # Renumberable rows: the empty twin moves, both survive.
            ("local:j", rd.RESOLUTION_RENUMBER),
            # Slack-presumed rows: no id may move either, so it goes to a human.
            ("C0SLACK1", rd.RESOLUTION_NEEDS_HUMAN),
        ],
    )
    @pytest.mark.parametrize("strategy", rd.STRATEGIES)
    def test_content_only_divergence_is_never_resolved_by_deleting(
        self, channel, expected_resolution, strategy
    ):
        # The headline guarantee: a row carrying text its twin does not is NOT a
        # redundant copy, and no strategy may drop it -- whatever else matches.
        left = make_row("a1", at_0019=True, content="IMPORTANT", channel_id=channel)
        right = make_row("a2", at_0019=True, content="", channel_id=channel, seconds=1)
        group = plan(group_of(left, right), strategy, at_0019=True)
        assert group.kind == rd.KIND_DIVERGENT
        assert group.resolution == expected_resolution
        assert rd.ACTION_DELETE not in [r.action for r in group.rows]

    def test_sort_key_survives_a_null_created_at(self):
        row = make_row("a1")
        row.created_at = None
        assert row.sort_key()[0] is False  # sorts first, does not raise


# --------------------------------------------------------------------------- #
# Replacement id generation
# --------------------------------------------------------------------------- #

class TestMintReplacementTs:
    def test_lands_in_the_remediation_slot(self):
        new = rd.mint_replacement_ts("1755000005.000000", {"1755000005.000000"}, now_us=1)
        assert rd.parse_ts_us(new) % 100 == rd.REMEDIATION_WRITER_SLOT
        assert new == "1755000005.000099"

    def test_never_lands_in_a_live_writer_slot(self):
        for residue in range(100):
            new = rd.mint_replacement_ts(
                f"1755000005.{residue:06d}", {f"1755000005.{residue:06d}"}, now_us=1
            )
            assert rd.parse_ts_us(new) % 100 not in WRITER_SLOTS

    def test_stays_in_the_original_neighbourhood_so_ordering_holds(self):
        original = "1755000005.000000"
        new = rd.mint_replacement_ts(original, {original}, now_us=1)
        delta = rd.parse_ts_us(new) - rd.parse_ts_us(original)
        assert 0 < delta < 100

    def test_is_always_after_the_id_it_replaces(self):
        for residue in range(100):
            original = f"1755000005.{residue:06d}"
            new = rd.mint_replacement_ts(original, {original}, now_us=1)
            assert rd.parse_ts_us(new) > rd.parse_ts_us(original)

    def test_probes_past_taken_ids(self):
        original = "1755000005.000000"
        used = {original, "1755000005.000099", "1755000005.000199", "1755000005.000299"}
        assert rd.mint_replacement_ts(original, used, now_us=1) == "1755000005.000399"

    def test_probes_past_an_id_that_is_only_a_thread_pointer(self):
        # load_used_ids feeds thread_ts and thread_decisions.thread_id in here too,
        # so a replacement can never quietly re-parent someone else's reply.
        original = "1755000005.000000"
        used = {original, "1755000005.000099"}
        assert rd.mint_replacement_ts(original, used, now_us=1) == "1755000005.000199"

    def test_an_original_already_in_the_remediation_slot_still_moves(self):
        original = "1755000005.000099"
        assert rd.mint_replacement_ts(original, {original}, now_us=1) == "1755000005.000199"

    def test_an_original_in_the_remediation_slot_moves_even_if_used_is_empty(self):
        original = "1755000005.000099"
        assert rd.mint_replacement_ts(original, set(), now_us=1) != original

    def test_malformed_original_falls_back_to_the_clock(self):
        new = rd.mint_replacement_ts("not-a-slack-ts", set(), now_us=1_800_000_000_000_000)
        assert new == "1800000000.000099"

    def test_gives_up_rather_than_looping_forever(self):
        original = "1755000005.000000"
        used = {original} | {
            rd.format_us((rd.parse_ts_us(original) // 100 + n) * 100 + 99) for n in range(20)
        }
        with pytest.raises(RuntimeError, match="no free id"):
            rd.mint_replacement_ts(original, used, now_us=1, max_probes=20)

    def test_a_run_of_replacements_is_collision_free(self):
        used = {"1755000005.000000"}
        minted = []
        for _ in range(50):
            new = rd.mint_replacement_ts("1755000005.000000", used, now_us=1)
            used.add(new)
            minted.append(new)
        assert len(set(minted)) == 50
        assert minted == sorted(minted, key=rd.parse_ts_us)


# --------------------------------------------------------------------------- #
# Anchor selection
# --------------------------------------------------------------------------- #

class TestPickAnchor:
    def _rows(self):
        rows = [make_row("a1", seconds=0), make_row("a2", seconds=5),
                make_row("a3", seconds=9)]
        for row in rows:
            row.renumber_verdict = rd.RENUMBER_SAFE
        return rows

    def test_keep_earliest_takes_the_oldest(self):
        assert rd.pick_anchor(self._rows(), strategy=rd.STRATEGY_KEEP_EARLIEST,
                              reply_channel_ids=set()).row_id == "a1"

    def test_keep_latest_takes_the_newest(self):
        assert rd.pick_anchor(self._rows(), strategy=rd.STRATEGY_KEEP_LATEST,
                              reply_channel_ids=set()).row_id == "a3"

    def test_the_unsafe_row_always_wins(self):
        rows = self._rows()
        rows[2].renumber_verdict = rd.RENUMBER_UNSAFE
        for strategy in rd.STRATEGIES:
            assert rd.pick_anchor(rows, strategy=strategy,
                                  reply_channel_ids=set()).row_id == "a3"

    def test_a_thread_root_in_the_replies_channel_beats_the_clock(self):
        rows = self._rows()
        rows[1].columns["channel_id"] = "C0THREAD"
        assert rd.pick_anchor(rows, strategy=rd.STRATEGY_KEEP_EARLIEST,
                              reply_channel_ids={"C0THREAD"}).row_id == "a2"

    def test_an_unsafe_row_still_beats_the_replies_channel(self):
        rows = self._rows()
        rows[1].columns["channel_id"] = "C0THREAD"
        rows[0].renumber_verdict = rd.RENUMBER_UNSAFE
        assert rd.pick_anchor(rows, strategy=rd.STRATEGY_KEEP_EARLIEST,
                              reply_channel_ids={"C0THREAD"}).row_id == "a1"

    def test_ties_break_on_the_primary_key_so_runs_are_reproducible(self):
        rows = [make_row("a9", seconds=0), make_row("a2", seconds=0)]
        for row in rows:
            row.renumber_verdict = rd.RENUMBER_SAFE
        assert rd.pick_anchor(rows, strategy=rd.STRATEGY_KEEP_EARLIEST,
                              reply_channel_ids=set()).row_id == "a2"


# --------------------------------------------------------------------------- #
# Group classification and strategy selection
# --------------------------------------------------------------------------- #

class TestPlanGroupRedundant:
    def _identical_slack_pair(self):
        return group_of(make_row("a1", seconds=0), make_row("a2", seconds=5))

    def test_renumber_refuses_a_slack_born_identical_pair_and_names_the_fix(self):
        group = plan(self._identical_slack_pair(), rd.STRATEGY_RENUMBER)
        assert group.kind == rd.KIND_REDUNDANT
        assert group.resolution == rd.RESOLUTION_NEEDS_DELETE_STRATEGY
        assert not group.resolved
        assert "--strategy keep-earliest" in group.reason
        assert all(r.action == rd.ACTION_KEEP for r in group.rows)

    def test_keep_earliest_deletes_the_later_copies(self):
        group = plan(self._identical_slack_pair(), rd.STRATEGY_KEEP_EARLIEST)
        assert group.resolution == rd.RESOLUTION_DELETE
        assert group.anchor_id == "a1"
        assert [r.action for r in group.rows] == [rd.ACTION_KEEP, rd.ACTION_DELETE]

    def test_keep_latest_deletes_the_earlier_copies(self):
        group = plan(self._identical_slack_pair(), rd.STRATEGY_KEEP_LATEST)
        assert group.resolution == rd.RESOLUTION_DELETE
        assert group.anchor_id == "a2"
        assert [r.action for r in group.rows] == [rd.ACTION_DELETE, rd.ACTION_KEEP]

    def test_a_three_way_identical_group_keeps_exactly_one(self):
        group = group_of(make_row("a1", seconds=0), make_row("a2", seconds=1),
                         make_row("a3", seconds=2))
        plan(group, rd.STRATEGY_KEEP_EARLIEST)
        assert [r.action for r in group.rows].count(rd.ACTION_KEEP) == 1
        assert [r.action for r in group.rows].count(rd.ACTION_DELETE) == 2

    def test_an_all_local_identical_group_can_be_renumbered_instead(self):
        group = group_of(make_row("a1", channel_id="local:x", seconds=0),
                         make_row("a2", channel_id="local:x", seconds=5))
        plan(group, rd.STRATEGY_RENUMBER)
        assert group.kind == rd.KIND_REDUNDANT
        assert group.resolution == rd.RESOLUTION_RENUMBER
        assert group.resolved
        # ...but it says out loud that this doubles the message in rebuilt history.
        assert "appear twice" in group.reason
        assert group.rows[1].new_message_ts is not None

    def test_at_0019_an_identical_confirmed_slack_pair_is_renumberable(self):
        # The same data that 0018 can only fix by deleting: at 0019 slack_ts holds
        # the Slack timestamp, so the canonical id is free to move.
        rows = [
            make_row("a1", at_0019=True, slack_ts="1755000001.000017", seconds=0),
            make_row("a2", at_0019=True, slack_ts="1755000001.000017", seconds=5),
        ]
        group = plan(group_of(*rows), rd.STRATEGY_RENUMBER, at_0019=True)
        assert group.kind == rd.KIND_REDUNDANT
        assert group.resolution == rd.RESOLUTION_RENUMBER


class TestPlanGroupDivergent:
    def test_one_slack_row_plus_one_local_row_renumbers_the_local_one(self):
        slack = make_row("a1", message_length=250, seconds=0)
        local = make_row("a2", channel_id="local:cryo-em", message_length=0, seconds=1)
        group = plan(group_of(slack, local), rd.STRATEGY_RENUMBER)
        assert group.kind == rd.KIND_DIVERGENT
        assert group.resolution == rd.RESOLUTION_RENUMBER
        assert group.anchor_id == "a1"
        assert slack.action == rd.ACTION_KEEP
        assert local.action == rd.ACTION_RENUMBER
        assert local.new_message_ts == "1755000001.000099"

    def test_two_slack_born_divergent_rows_need_a_human(self):
        group = plan(
            group_of(make_row("a1", message_length=310, seconds=0),
                     make_row("a2", message_length=44, channel_id="C0SLACK2", seconds=1)),
            rd.STRATEGY_RENUMBER,
        )
        assert group.resolution == rd.RESOLUTION_NEEDS_HUMAN
        assert not group.resolved
        assert "REFUSING to guess" in group.reason
        assert all(r.action == rd.ACTION_KEEP for r in group.rows)

    @pytest.mark.parametrize("strategy", rd.STRATEGIES)
    def test_no_strategy_can_talk_it_into_guessing(self, strategy):
        group = plan(
            group_of(make_row("a1", message_length=310, seconds=0),
                     make_row("a2", message_length=44, channel_id="C0SLACK2", seconds=1)),
            strategy,
        )
        assert group.resolution == rd.RESOLUTION_NEEDS_HUMAN

    @pytest.mark.parametrize("strategy", rd.STRATEGIES)
    def test_a_divergent_group_is_never_resolved_by_deletion(self, strategy):
        group = plan(
            group_of(make_row("a1", channel_id="local:x", message_length=1, seconds=0),
                     make_row("a2", channel_id="local:x", message_length=2, seconds=1)),
            strategy,
        )
        assert group.resolution == rd.RESOLUTION_RENUMBER
        assert rd.ACTION_DELETE not in [r.action for r in group.rows]

    def test_a_three_way_divergent_group_gets_two_distinct_replacements(self):
        rows = [make_row(f"a{n}", channel_id="local:x", message_length=n, seconds=n)
                for n in (1, 2, 3)]
        group = plan(group_of(*rows), rd.STRATEGY_RENUMBER)
        assert group.resolution == rd.RESOLUTION_RENUMBER
        new = [r.new_message_ts for r in rows if r.new_message_ts]
        assert new == ["1755000001.000099", "1755000001.000199"]
        assert len(set(new)) == 2

    def test_the_reason_names_the_row_whose_ts_is_frozen(self):
        slack = make_row("a1", message_length=250, seconds=0)
        local = make_row("a2", channel_id="local:x", message_length=0, seconds=1)
        group = plan(group_of(slack, local), rd.STRATEGY_RENUMBER)
        assert "a1" in group.reason

    def test_replacements_avoid_ids_already_used_elsewhere_in_the_run(self):
        rows = [make_row("a1", channel_id="local:x", message_length=1, seconds=0),
                make_row("a2", channel_id="local:x", message_length=2, seconds=1)]
        used = {"1755000001.000017", "1755000001.000099", "1755000001.000199"}
        plan(group_of(*rows), rd.STRATEGY_RENUMBER, used=used)
        assert rows[1].new_message_ts == "1755000001.000299"

    def test_planning_adds_its_own_output_to_the_used_set(self):
        used = {"1755000001.000017"}
        rows = [make_row("a1", channel_id="local:x", message_length=1, seconds=0),
                make_row("a2", channel_id="local:x", message_length=2, seconds=1)]
        plan(group_of(*rows), rd.STRATEGY_RENUMBER, used=used)
        assert rows[1].new_message_ts in used

    def test_two_groups_sharing_a_used_set_cannot_collide(self):
        used = {"1755000001.000017", "1755000001.000018"}
        first = group_of(make_row("a1", channel_id="local:x", message_length=1, seconds=0),
                         make_row("a2", channel_id="local:x", message_length=2, seconds=1))
        second = group_of(
            make_row("b1", ts="1755000001.000018", channel_id="local:x",
                     message_length=3, seconds=2),
            make_row("b2", ts="1755000001.000018", channel_id="local:x",
                     message_length=4, seconds=3),
        )
        plan(first, rd.STRATEGY_RENUMBER, used=used)
        plan(second, rd.STRATEGY_RENUMBER, used=used)
        assert first.rows[1].new_message_ts != second.rows[1].new_message_ts

    def test_a_malformed_ts_group_is_renumbered_from_the_clock(self):
        rows = [make_row("a1", ts="not-a-slack-ts", message_length=7, seconds=0),
                make_row("a2", ts="not-a-slack-ts", message_length=9, seconds=1)]
        group = plan(group_of(*rows), rd.STRATEGY_RENUMBER, now_us=1_800_000_000_000_000)
        assert group.resolution == rd.RESOLUTION_RENUMBER
        assert rows[1].new_message_ts == "1800000000.000099"

    def test_replanning_a_group_clears_the_previous_plan(self):
        # plan_group is called once per group per run, but it must be re-entrant:
        # --apply plans under the table lock, and a stale action would be a write.
        rows = [make_row("a1", channel_id="local:x", message_length=1, seconds=0),
                make_row("a2", channel_id="local:x", message_length=2, seconds=1)]
        group = group_of(*rows)
        plan(group, rd.STRATEGY_RENUMBER)
        assert rows[1].action == rd.ACTION_RENUMBER
        # Re-plan the same objects as a NEEDS_HUMAN group.
        rows[0].columns["channel_id"] = "C0SLACK1"
        rows[1].columns["channel_id"] = "C0SLACK2"
        plan(group, rd.STRATEGY_RENUMBER)
        assert group.resolution == rd.RESOLUTION_NEEDS_HUMAN
        assert all(r.action == rd.ACTION_KEEP for r in rows)
        assert all(r.new_message_ts is None for r in rows)


class TestThreadAwareness:
    def test_a_referenced_group_reports_its_inbound_pointers(self):
        group = group_of(
            make_row("a1", seconds=0),
            make_row("a2", channel_id="local:mirror", message_length=33, seconds=1),
            thread_reply_count=2, thread_decision_count=1,
            thread_reply_channel_ids={"C0SLACK1"},
        )
        plan(group, rd.STRATEGY_RENUMBER)
        assert group.referenced
        # The row in the replies' channel keeps the ts, so the pointers still land.
        assert group.anchor_id == "a1"

    def test_an_unreferenced_group_is_not_flagged(self):
        group = group_of(make_row("a1"), make_row("a2", channel_id="local:x"))
        plan(group, rd.STRATEGY_RENUMBER)
        assert not group.referenced

    def test_needs_human_advice_mentions_the_thread_pointers(self):
        group = group_of(make_row("a1", message_length=1),
                         make_row("a2", message_length=2, channel_id="C0SLACK2"),
                         thread_reply_count=2, thread_decision_count=1)
        plan(group, rd.STRATEGY_RENUMBER)
        advice = "\n".join(rd.needs_human_advice(group))
        assert "conversations.replies" in advice
        assert "2 reply row(s)" in advice
        assert "thread root" in advice

    def test_needs_human_advice_omits_the_thread_step_when_nothing_points_here(self):
        group = group_of(make_row("a1", message_length=1),
                         make_row("a2", message_length=2, channel_id="C0SLACK2"))
        plan(group, rd.STRATEGY_RENUMBER)
        advice = "\n".join(rd.needs_human_advice(group))
        assert "thread root" not in advice
        assert "conversations.replies" in advice


# --------------------------------------------------------------------------- #
# The id-scheme guard
# --------------------------------------------------------------------------- #

class _FakeIds:
    WRITER_SLOT_MODULUS = 100
    WRITER_ENGINE = 0
    WRITER_WEB = 1
    __file__ = "/fake/src/agent/ids.py"


class TestLoadIdScheme:
    def test_reads_the_real_module(self):
        modulus, slots, path = rd.load_id_scheme()
        assert modulus == ids_mod.WRITER_SLOT_MODULUS
        assert slots[ids_mod.WRITER_ENGINE] == "WRITER_ENGINE"
        assert slots[ids_mod.WRITER_WEB] == "WRITER_WEB"
        assert path.endswith("src/agent/ids.py")

    def _with_fake(self, monkeypatch, fake):
        """Simulate load_id_scheme() importing a stale/altered baked copy of src/.

        Patching ``sys.modules["src.agent.ids"]`` alone does NOT work, and finding
        that out is the reason this helper exists: ``from src.agent import ids``
        imports the ``src.agent`` PACKAGE and then does a plain ``getattr`` for
        ``ids``. Once the submodule has been imported anywhere, that attribute is
        already bound and sys.modules is never consulted, so the fake was ignored
        and five of these tests were silently asserting against the real module.
        Both are patched below so the substitution holds either way.
        """
        import src.agent

        monkeypatch.setattr(src.agent, "ids", fake)
        monkeypatch.setitem(sys.modules, "src.agent.ids", fake)

    def test_accepts_a_matching_scheme(self, monkeypatch):
        self._with_fake(monkeypatch, _FakeIds)
        modulus, slots, path = rd.load_id_scheme()
        assert modulus == 100
        assert slots == {0: "WRITER_ENGINE", 1: "WRITER_WEB"}
        assert path == "/fake/src/agent/ids.py"

    def test_rejects_a_different_modulus(self, monkeypatch):
        class Drifted(_FakeIds):
            WRITER_SLOT_MODULUS = 1000

        self._with_fake(monkeypatch, Drifted)
        with pytest.raises(rd.SchemeError, match="WRITER_SLOT_MODULUS"):
            rd.load_id_scheme()

    def test_rejects_a_scheme_that_has_claimed_the_remediation_slot(self, monkeypatch):
        class Claimed(_FakeIds):
            WRITER_SOMETHING_NEW = rd.REMEDIATION_WRITER_SLOT

        self._with_fake(monkeypatch, Claimed)
        with pytest.raises(rd.SchemeError, match="is now claimed by"):
            rd.load_id_scheme()

    def test_rejects_a_scheme_with_no_writers_at_all(self, monkeypatch):
        class Empty:
            WRITER_SLOT_MODULUS = 100
            __file__ = "/fake/ids.py"

        self._with_fake(monkeypatch, Empty)
        with pytest.raises(rd.SchemeError, match="no WRITER_"):
            rd.load_id_scheme()

    def test_ignores_non_integer_writer_attributes(self, monkeypatch):
        class Mixed(_FakeIds):
            WRITER_NAMES = ("engine", "web")
            WRITER_ENABLED = True  # bool is an int subclass; must not become a slot

        self._with_fake(monkeypatch, Mixed)
        _, slots, _ = rd.load_id_scheme()
        assert slots == {0: "WRITER_ENGINE", 1: "WRITER_WEB"}

    def test_the_remediation_slot_is_in_range_and_free_in_the_real_scheme(self):
        _, slots, _ = rd.load_id_scheme()
        assert 0 <= rd.REMEDIATION_WRITER_SLOT < ids_mod.WRITER_SLOT_MODULUS
        assert rd.REMEDIATION_WRITER_SLOT not in slots


# --------------------------------------------------------------------------- #
# DSN handling and the report envelope
# --------------------------------------------------------------------------- #

class TestDsn:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("postgresql+asyncpg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
            ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
            ("postgres://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
            ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ],
    )
    def test_normalise(self, given, expected):
        assert rd.normalise_dsn(given) == expected

    def test_redaction_hides_the_password_and_keeps_the_host(self):
        masked = rd.redact_dsn("postgresql+asyncpg://copi:s3cret@db.example:5432/copi")
        assert "s3cret" not in masked
        assert "db.example:5432/copi" in masked
        assert "copi:***@" in masked

    def test_redaction_leaves_a_passwordless_dsn_alone(self):
        assert rd.redact_dsn("postgresql+asyncpg://h/d") == "postgresql+asyncpg://h/d"


class TestJsonEnvelope:
    def test_group_json_is_serialisable_and_complete(self):
        import json

        group = plan(
            group_of(make_row("a1", at_0019=True, message_length=250, seconds=0),
                     make_row("a2", at_0019=True, channel_id="local:x", seconds=1)),
            rd.STRATEGY_RENUMBER, at_0019=True,
        )
        blob = json.loads(json.dumps(rd.group_to_json(group)))
        assert blob["kind"] == rd.KIND_DIVERGENT
        assert blob["resolution"] == rd.RESOLUTION_RENUMBER
        assert blob["row_count"] == 2
        assert blob["resolved"] is True
        assert {r["action"] for r in blob["rows"]} == {rd.ACTION_KEEP, rd.ACTION_RENUMBER}
        # created_at is a datetime in the row; it has to survive json.dumps.
        assert isinstance(blob["rows"][0]["columns"]["created_at"], str)
        assert blob["rows"][0]["origin_evidence"]

    def test_envelope_reports_the_counts_and_the_exit_code(self):
        schema = {"alembic_revision": "0018", "has_content": False, "has_slack_ts": False,
                  "constraint_present": False, "total_rows": 127,
                  "null_message_ts_rows": 65}
        resolvable = plan(
            group_of(make_row("a1", message_length=250, seconds=0),
                     make_row("a2", channel_id="local:x", seconds=1)),
            rd.STRATEGY_RENUMBER,
        )
        refused = plan(
            group_of(make_row("b1", ts="1755000002.000042", message_length=1, seconds=0),
                     make_row("b2", ts="1755000002.000042", message_length=2,
                              channel_id="C0SLACK2", seconds=1)),
            rd.STRATEGY_RENUMBER,
        )
        envelope = rd._envelope(
            schema, "postgresql+asyncpg://u:p@h/d", rd.STRATEGY_RENUMBER, False,
            [resolvable, refused], None, rd.EXIT_REMAIN,
        )
        assert envelope["summary"]["duplicate_groups"] == 2
        assert envelope["summary"]["rows_in_groups"] == 4
        assert envelope["summary"]["rows_to_renumber"] == 1
        assert envelope["summary"]["rows_to_delete"] == 0
        assert envelope["summary"]["unresolved_groups"] == 1
        assert envelope["summary"]["by_resolution"] == {
            rd.RESOLUTION_RENUMBER: 1, rd.RESOLUTION_NEEDS_HUMAN: 1,
        }
        assert envelope["exit_code"] == rd.EXIT_REMAIN
        assert "p@" not in envelope["database"]

    def test_truncation_keeps_the_length_visible(self):
        rendered = rd._fmt_value("x" * 500)
        assert "500 chars" in rendered
        assert len(rendered) < 120


class TestExitCodes:
    def test_the_contract_is_the_documented_one(self):
        assert (rd.EXIT_CLEAN, rd.EXIT_REMAIN, rd.EXIT_FOUND_DRY_RUN) == (0, 1, 2)

    def test_operational_and_usage_codes_cannot_be_mistaken_for_a_verdict(self):
        assert rd.EXIT_OPERATIONAL not in (0, 1, 2)
        assert rd.EXIT_USAGE not in (0, 1, 2, rd.EXIT_OPERATIONAL)

    def test_a_usage_error_exits_64_not_argparse_default_2(self):
        parser = rd.build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--strategy", "not-a-strategy"])
        assert exc.value.code == rd.EXIT_USAGE

    def test_the_default_strategy_is_the_non_destructive_one(self):
        args = rd.build_parser().parse_args([])
        assert args.strategy == rd.STRATEGY_RENUMBER
        assert args.apply is False
        assert args.as_json is False

    def test_main_reports_a_missing_dsn_as_operational(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert rd.main([]) == rd.EXIT_OPERATIONAL

    def test_unresolved_resolutions_are_exactly_the_two_refusals(self):
        assert rd.UNRESOLVED == {
            rd.RESOLUTION_NEEDS_DELETE_STRATEGY, rd.RESOLUTION_NEEDS_HUMAN,
        }
