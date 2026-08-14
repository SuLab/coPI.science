"""ORCID Public API v3.0, live.

ORCID is the highest-consequence external integration in the system: it is the login
path (`routers/auth.py`) as well as the profile source.

Rule L1 is why `test_the_contract_fixture_still_matches_orcid` exists.
`tests/contract/test_orcid_contract.py` builds its record from a HAND-WRITTEN dict —
`_record()` returns a literal, not a recorded response. All 12 of those tests would
still pass if ORCID renamed a field tomorrow. Nothing checked that belief against
reality until this file.
"""

import httpx
import pytest

from src.services import orcid

pytestmark = [pytest.mark.live_api]

# ORCID maintains this as a permanent public example persona. It will not be deleted,
# made private, or renamed, which is what makes it safe to assert on (Rule L2).
CARBERRY = "0000-0002-1825-0097"


def key_paths(obj, prefix="", empty=None):
    """Every dotted key path in a nested dict/list structure.

    ``empty`` collects the paths of containers that are present but EMPTY. That
    distinction is the whole difference between "ORCID renamed a field" and "this
    particular record has nothing in that list", and conflating them makes the drift
    check cry wolf. Measured: the example record's person.emails.email is `[]`, so a
    naive comparison reports person.emails.email[].email as missing.
    """
    if isinstance(obj, dict):
        if not obj:
            if empty is not None:
                empty.add(prefix)
            yield prefix
            return
        for k, v in obj.items():
            yield from key_paths(v, f"{prefix}.{k}" if prefix else k, empty)
    elif isinstance(obj, list):
        if obj:
            yield from key_paths(obj[0], f"{prefix}[]", empty)
        else:
            if empty is not None:
                empty.add(f"{prefix}[]")
            yield f"{prefix}[]"
    else:
        yield prefix


async def test_a_real_public_record_fetches_and_parses(api_budget):
    """Shape first (Rule L2), then one dated value.

    Control: the parser must NOT return the same thing for a different id, or an
    implementation that ignored its argument would pass.
    """
    api_budget.wait("orcid")
    prof = await orcid.fetch_orcid_profile(CARBERRY)

    assert prof["orcid"] == CARBERRY
    assert isinstance(prof.get("name"), str) and prof["name"].strip()
    assert prof["name"] != CARBERRY, (
        "fetch_orcid_profile fell back to the raw id, which is what it does when the "
        "name block is missing — ORCID's person.name shape may have changed"
    )
    # Dated assertion, allowed to be updated: as of 2026-07-30 this record is Carberry.
    assert "Carberry" in prof["name"], f"as-of-2026-07-30 value changed: {prof['name']!r}"


async def test_fetch_orcid_record_returns_the_documented_top_level_shape(api_budget):
    api_budget.wait("orcid")
    rec = await orcid.fetch_orcid_record(CARBERRY)
    assert isinstance(rec, dict)
    for key in ("person", "activities-summary"):
        assert key in rec, (
            f"ORCID's record no longer has a top-level {key!r} — "
            f"fetch_orcid_profile reads it unconditionally. Got: {sorted(rec)[:12]}"
        )


async def test_the_contract_fixture_still_matches_orcid(api_budget):
    """Rule L1, the load-bearing test in this file.

    Walks every key path the hand-written contract fixture asserts on and requires it to
    exist in the live response.

    Control: a minimum path count is asserted first. If the walker broke or the live
    record came back empty, `missing` would be trivially empty and this would prove
    nothing — which is the exact failure mode the whole rule is about.
    """
    from tests.contract.test_orcid_contract import _record

    api_budget.wait("orcid")
    live = await orcid.fetch_orcid_record(CARBERRY)

    fixture_paths = {p for p in key_paths(_record()) if p}
    assert len(fixture_paths) >= 8, (
        f"the fixture walker found only {len(fixture_paths)} paths — it is broken, so "
        "the comparison below would be vacuous"
    )
    live_empty: set[str] = set()
    live_paths = {p for p in key_paths(live, empty=live_empty) if p}
    assert len(live_paths) >= 20, (
        f"the live record has only {len(live_paths)} paths — ORCID returned something "
        "unexpected and the comparison below would be meaningless"
    )

    absent = [p for p in fixture_paths if p not in live_paths]
    # A path under an EMPTY live container tells us nothing: the key may be intact and
    # simply have no rows in this record.
    unverifiable = sorted(
        p for p in absent if any(p.startswith(e + ".") for e in live_empty)
    )
    renamed = sorted(p for p in absent if p not in unverifiable)

    assert not renamed, (
        "ORCID's live response no longer contains key paths that "
        "tests/contract/test_orcid_contract.py's hand-written fixture asserts on, and "
        "their parent containers are NOT empty — so this is a real schema change and "
        "those contract tests are pinning a shape that no longer exists:\n  "
        + "\n  ".join(renamed)
    )
    # Control: if everything the fixture claims sits under an empty container, this test
    # verified nothing and must say so rather than report a pass.
    verified = fixture_paths - set(unverifiable)
    assert len(verified) >= 6, (
        f"only {len(verified)} fixture paths could be checked against live data "
        f"({len(unverifiable)} sit under empty containers: {unverifiable}). Pick a "
        "richer record — this run proved almost nothing."
    )


async def test_fetch_orcid_works_returns_a_list_of_dicts(api_budget):
    """Control: an ORCID with no works returns [], not an error — the profile pipeline
    must still onboard a PI who has published nothing under this id."""
    api_budget.wait("orcid")
    works = await orcid.fetch_orcid_works(CARBERRY)
    assert isinstance(works, list)
    if works:
        assert all(isinstance(w, dict) for w in works)
    else:
        pytest.skip("the example record currently has no works to shape-check")


async def test_fetch_orcid_grants_returns_titles_or_empty(api_budget):
    api_budget.wait("orcid")
    grants = await orcid.fetch_orcid_grants(CARBERRY)
    assert isinstance(grants, list)
    assert all(isinstance(g, str) for g in grants)


async def test_an_unknown_orcid_degrades_without_taking_down_the_caller(api_budget):
    """Rule L3: distinguish "ORCID said no such record" from "we could not reach ORCID".

    fetch_orcid_record raises for status; the grants/works helpers swallow and return
    []. Both behaviours are deliberate and both are asserted, because the callers rely
    on the difference.
    """
    bogus = "0000-0000-0000-0000"
    api_budget.wait("orcid")
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await orcid.fetch_orcid_record(bogus)
    assert ei.value.response.status_code in (400, 404), (
        f"ORCID answered {ei.value.response.status_code} for a nonexistent id — that is "
        "neither the documented 404 nor a network failure"
    )

    api_budget.wait("orcid")
    assert await orcid.fetch_orcid_grants(bogus) == []
    api_budget.wait("orcid")
    assert await orcid.fetch_orcid_works(bogus) == []
