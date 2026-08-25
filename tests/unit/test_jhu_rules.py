"""JHU tenure-window rules (docs/specs/2026-08-13-jhu-instance-rules-design.md R2).

The scoping rule: profiles are synthesized only from papers with
``year >= tenure_start``. Per-paper affiliation filtering stays rejected;
affiliations are used only to (a) identify a Hopkins EMPLOYMENT in ORCID and
(b) date the PI's earliest Hopkins-affiliated paper — and (b) must look at the
PI's OWN affiliation on the paper (finding H2: a 2005 paper whose only Hopkins
author is a co-author must not date the PI's tenure).

Persistence rules (findings H1/M1): entries are keyed by ``user_id`` in
per-user AppSetting rows written via upsert in whatever session the caller
provides (the pipeline passes a SHORT dedicated one, never the job session,
because the worker commits mid-pipeline state on failure); the legacy
agent_id-keyed ``jhu_tenure_start`` map is read as a fallback.
"""

import uuid

import pytest

from src.models import AppSetting
from src.services import jhu_rules
from src.services.jhu_rules import (
    derive_employment_start,
    derive_start_from_papers,
    get_tenure_start,
    is_hopkins_affiliation,
    set_tenure_start,
    tenure_filter,
)

# ---------------------------------------------------------------------------
# tenure_filter
# ---------------------------------------------------------------------------


def test_tenure_filter_is_identity_when_no_start_is_known():
    pubs = [{"year": 1999}, {"year": None}, {}]
    assert tenure_filter(pubs, None) == pubs


def test_tenure_filter_keeps_the_boundary_year_and_drops_undated():
    pubs = [
        {"year": 2010, "t": "before"},
        {"year": 2011, "t": "boundary"},
        {"year": 2020, "t": "after"},
        {"year": None, "t": "undated"},
        {"t": "missing"},
    ]
    kept = tenure_filter(pubs, 2011)
    assert [p["t"] for p in kept] == ["boundary", "after"]


# ---------------------------------------------------------------------------
# is_hopkins_affiliation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Johns Hopkins University School of Medicine, Baltimore, MD",
        "Dept of Epidemiology, Bloomberg School of Public Health",
        "JHU Applied Physics Laboratory",
        "Sidney Kimmel Comprehensive Cancer Center, JHMI",
    ],
)
def test_hopkins_affiliations_match(text):
    assert is_hopkins_affiliation(text)


@pytest.mark.parametrize(
    "text",
    [
        "University of Maryland, Baltimore",
        "Jhunjhunwala Institute of Technology",
        "",
        None,
    ],
)
def test_non_hopkins_affiliations_do_not_match(text):
    assert not is_hopkins_affiliation(text)


# ---------------------------------------------------------------------------
# derive_employment_start — over the employments list orcid.py extracts
# ---------------------------------------------------------------------------


def _emp(org, start_year, current=True):
    return {"organization": org, "start_year": start_year, "current": current}


def test_employment_start_comes_from_the_current_hopkins_employment():
    emps = [
        _emp("Stanford University", 2001, current=False),
        _emp("Johns Hopkins University", 2011, current=True),
    ]
    assert derive_employment_start(emps) == 2011


def test_an_ended_hopkins_employment_does_not_count():
    emps = [
        _emp("Johns Hopkins University", 2004, current=False),
        _emp("University of Chicago", 2010, current=True),
    ]
    assert derive_employment_start(emps) is None


def test_joint_current_hopkins_appointments_take_the_earliest_start():
    emps = [
        _emp("Johns Hopkins Bloomberg School of Public Health", 2018),
        _emp("Johns Hopkins University School of Medicine", 2014),
    ]
    assert derive_employment_start(emps) == 2014


def test_a_current_hopkins_employment_without_a_start_year_yields_none():
    assert derive_employment_start([_emp("Johns Hopkins University", None)]) is None


# ---------------------------------------------------------------------------
# derive_start_from_papers — the PI's OWN affiliations only (H2)
# ---------------------------------------------------------------------------


def test_paper_tier_uses_the_earliest_paper_where_the_pi_herself_is_at_hopkins():
    records = [
        {"year": 2005, "pi_affiliations": ["University of Somewhere"]},
        {"year": 2018, "pi_affiliations": ["Johns Hopkins University"]},
        {"year": 2020, "pi_affiliations": ["Johns Hopkins University"]},
    ]
    assert derive_start_from_papers(records) == 2018


def test_a_co_author_only_hopkins_paper_never_dates_tenure():
    # The corpus annotates each record with the PI's own matched affiliations;
    # a record where only a co-author is at Hopkins carries none here.
    records = [
        {"year": 2005, "pi_affiliations": ["Uni X, Dept of Neuro"]},
        {"year": 2007, "pi_affiliations": []},
    ]
    assert derive_start_from_papers(records) is None


def test_undated_papers_cannot_date_tenure():
    records = [{"year": None, "pi_affiliations": ["Johns Hopkins University"]}]
    assert derive_start_from_papers(records) is None


# ---------------------------------------------------------------------------
# persistence: user_id-keyed upsert rows + legacy agent_id map fallback
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.integration


@pytest.mark.integration
async def test_set_then_get_round_trips_with_provenance(db_session):
    user_id = uuid.uuid4()
    await set_tenure_start(
        user_id, 2015, "orcid_employment", db=db_session
    )
    assert await get_tenure_start(db_session, user_id) == 2015

    row = await db_session.get(AppSetting, f"{jhu_rules.TENURE_KEY_PREFIX}{user_id}")
    assert row is not None
    import json

    stored = json.loads(row.value)
    assert stored["year"] == 2015
    assert stored["source"] == "orcid_employment"
    assert stored["derived_at"]


@pytest.mark.integration
async def test_set_is_an_upsert_not_a_duplicate_insert(db_session):
    user_id = uuid.uuid4()
    await set_tenure_start(user_id, 2012, "orcid_employment", db=db_session)
    await set_tenure_start(user_id, 2016, "manual", db=db_session)
    assert await get_tenure_start(db_session, user_id) == 2016


@pytest.mark.integration
async def test_legacy_agent_id_map_is_read_as_a_fallback(db_session):
    import json

    db_session.add(
        AppSetting(key="jhu_tenure_start", value=json.dumps({"wu": 2009}))
    )
    await db_session.flush()

    user_id = uuid.uuid4()  # no per-user row exists
    assert await get_tenure_start(db_session, user_id, agent_id="wu") == 2009
    assert await get_tenure_start(db_session, user_id, agent_id="zzz") is None
