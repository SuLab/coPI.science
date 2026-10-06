"""GrantSections: pool, dedupe, dates, caps, D57, D58, line format (spec 2026-10-05 §6.1)."""
from datetime import UTC, date, datetime
from types import SimpleNamespace as NS

import pytest

from src.services import grant_sections as gs

TODAY = date(2026, 10, 5)


def ident(status="resolved", accepted=(1,), pinned=()):
    return NS(status=status, accepted_profile_ids=list(accepted), pinned_profile_ids=list(pinned))


def grant(core, title="T", code="R01", pid=1, first=2019, last=2026, end=(2027, 6, 30),
          vetoed=False):
    return NS(core_project_num=core, title=title, activity_code=code, reporter_profile_id=pid,
              first_fy=first, last_fy=last,
              project_end=datetime(*end, tzinfo=UTC) if end else None,
              vetoed_at=datetime.now(UTC) if vetoed else None)


def funding(key, title=None, funder="Golden Foundation", start=(2020, None), end=(2028, None),
            ids=(), vetoed=False):
    return NS(group_key=key, title=title or f"Funding {key}", funder_name=funder,
              start_year=start[0], start_month=start[1],
              end_year=end[0] if end else None, end_month=end[1] if end else None,
              external_ids=[{"type": "grant_number", "value": v} for v in ids],
              vetoed_at=datetime.now(UTC) if vetoed else None)


def build(identity=None, grants=(), fundings=(), tenure=2010, today=TODAY):
    return gs.build_grant_sections(identity=identity, grants=list(grants),
                                   fundings=list(fundings), tenure_start=tenure, today=today)


def keys(lines):
    return [line.key for line in lines]


def test_no_identity_or_a_non_rendering_status_contributes_no_reporter_rows():
    g = [grant("R01A")]
    assert build(None, g).active == ()
    for status in ("held", "unconfirmed", "no_match", "firehose", "none_confirmed", None):
        assert build(ident(status), g).active == ()


def test_resolved_uses_accepted_ids_and_pinned_uses_only_pinned_ids():
    g = [grant("R01A", pid=1, title="A"), grant("R01B", pid=2, title="B")]
    assert keys(build(ident("resolved", accepted=(1,), pinned=(2,)), g).active) == ["R01A"]
    assert keys(build(ident("pinned", accepted=(1,), pinned=(2,)), g).active) == ["R01B"]


def test_training_codes_and_vetoed_rows_are_not_pooled():
    g = [grant("T32A", code="T32", title="Train"), grant("R01V", vetoed=True, title="V"),
         grant("R01K", title="K")]
    assert keys(build(ident(), g).active) == ["R01K"]


def test_reporter_rows_sharing_a_title_collapse_to_the_latest():
    g = [grant("R01A", title="Same  Title", end=(2027, 1, 1)),
         grant("R01B", title="same title", end=(2028, 1, 1))]
    assert keys(build(ident(), g).active) == ["R01B"]


def test_orcid_rows_duplicating_reporter_are_dropped_by_core_or_title():
    g = [grant("R01GM123456", title="Organoid Platform")]
    f = [funding("a", title="Other", ids=("5R01GM123456-03",)),
         funding("b", title="organoid platform!"), funding("c", title="Kept")]
    assert keys(build(ident(), g, f).active) == ["orcid:c", "R01GM123456"]


def test_orcid_rows_duplicating_each_other_collapse_to_the_latest():
    f = [funding("a", title="X", ids=("G1",), end=(2027, None)),
         funding("b", title="Y", ids=("G1",), end=(2029, None)),
         funding("c", title="Title Z", end=(2027, None)),
         funding("d", title="title z", end=(2030, None))]
    assert keys(build(None, (), f).active) == ["orcid:d", "orcid:b"]


def test_reporter_active_dates_and_the_federal_fiscal_year():
    ended = grant("R01E", title="E", end=(2026, 9, 30))
    open_fy = grant("R01F", title="F", end=None, last=2026)
    s = build(ident(), [ended, open_fy], today=date(2026, 9, 30))
    assert set(keys(s.active)) == {"R01E", "R01F"}
    s = build(ident(), [ended, open_fy], today=date(2026, 10, 1))      # FY2027 began
    assert keys(s.active) == [] and set(keys(s.past)) == {"R01E", "R01F"}


def test_orcid_active_rules():
    f = [funding("endmonth", end=(2026, 10)), funding("ended", end=(2026, 9)),
         funding("open_recent", start=(2022, 1), end=None),
         funding("open_old", start=(2020, 1), end=None),
         funding("undated", start=(None, None), end=None)]
    s = build(None, (), f)
    assert set(keys(s.active)) == {"orcid:endmonth", "orcid:open_recent"}
    assert "orcid:undated" not in keys(s.past)


def test_caps_and_order():
    many = [funding(f"k{i}", title=f"T{i}", start=(2015, None), end=(2027 + i, None))
            for i in range(20)]
    s = build(None, (), many)
    assert len(s.active) == gs.ACTIVE_CAP
    assert s.active[0].key == "orcid:k19" and s.active[-1].key == "orcid:k5"
    old = [funding(f"p{i}", title=f"P{i}", start=(2011, None), end=(2012 + i, None))
           for i in range(14)]
    s = build(None, (), old)
    assert len(s.past) == gs.PAST_CAP and s.past[0].key == "orcid:p13"


def test_reporter_rows_from_before_the_tenure_year_are_not_past():
    """Rows stored under org_only (or an older tenure year) before a tenure edit: before
    the fix every stored RePORTER row counted as in tenure and showed as a Past line."""
    old = grant("R01OLD", title="Before tenure", first=2001, last=2004, end=(2005, 6, 30))
    recent = grant("R01NEW", title="In tenure", first=2012, last=2015, end=(2016, 6, 30))
    undated = grant("R01NUL", title="No dates", first=None, last=None, end=None)
    assert keys(build(ident(), [old, recent, undated], tenure=2010).past) == ["R01NEW"]


def test_d57_no_tenure_means_active_only():
    s = build(ident(), [grant("R01A", end=(2015, 1, 1))], [funding("x", end=(2012, None))],
              tenure=None)
    assert s.active == () and s.past == () and gs.grant_section_lines(s) == []


def test_d58_orcid_past_needs_an_in_tenure_date():
    f = [funding("pre", start=(2005, None), end=(2012, None)),
         funding("in", start=(2011, None), end=(2013, None)),
         funding("endonly", start=(None, None), end=(2014, None)),
         funding("oldend", start=(None, None), end=(2009, None))]
    assert set(keys(build(None, (), f, tenure=2010).past)) == {"orcid:in", "orcid:endonly"}


def test_line_format_and_headings():
    s = build(ident(),
              [grant("R01A", title="Organoid\nPlatform", first=2019, end=(2027, 6, 30)),
               grant("R21B", title="Old", code="R21", first=2011, last=2013, end=None)],
              [funding("x", title="Award", funder="", start=(2024, None), end=None)],
              tenure=2010)
    lines = gs.grant_section_lines(s)
    assert lines[0] == "## Active Grants\n"
    assert "- Organoid Platform (NIH R01, 2019–2027)" in lines
    assert "- Award (ORCID, 2024–)" in lines
    assert "## Past Grants (since 2010)\n" in lines and "- Old (NIH R21, 2011–2013)" in lines
    assert lines.index("## Active Grants\n") < lines.index("## Past Grants (since 2010)\n")


@pytest.mark.integration
async def test_load_grant_sections_reads_identity_rows_fundings_and_tenure(db_session):
    from src.models import PiGrant, PiGrantIdentity, PiOrcidFunding
    from src.services.jhu_rules import set_tenure_start
    from tests import factories

    pi = await factories.make_user(db_session)
    await set_tenure_start(pi.id, 2012, "manual", db=db_session)
    db_session.add(PiGrantIdentity(user_id=pi.id, status="resolved", accepted_profile_ids=[7]))
    db_session.add(PiGrant(user_id=pi.id, core_project_num="R01AA000001", reporter_profile_id=7,
                           title="Live", activity_code="R01", org_name="JHU",
                           tenure_filter_mode="org_and_year", first_fy=2020, last_fy=2026,
                           project_end=datetime(2027, 6, 30, tzinfo=UTC)))
    db_session.add(PiOrcidFunding(user_id=pi.id, group_key="k", title="Gone", funder_name="F",
                                  start_year=2013, end_year=2015))
    await db_session.flush()
    s = await gs.load_grant_sections(db_session, pi.id, TODAY)
    assert [x.title for x in s.active] == ["Live"]
    assert [x.title for x in s.past] == ["Gone"] and s.tenure_start == 2012
