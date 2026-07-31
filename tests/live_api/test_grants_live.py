"""grants.gov Search2 / fetchOpportunity, live.

Rule L2 governs this file more than any other in the tier: **every funding opportunity
eventually closes.** Nothing here may assert that a particular `opp_id`, FOA number or
title is present or open. Every id used by an assertion is taken from the same live
response the assertion checks, so the tests are self-updating and cannot go stale.

Rule L1 is why the two `..._contract_..._fixture_still_matches_...` tests exist.
`tests/contract/test_grants_contract.py` builds its opportunities from HAND-WRITTEN
dicts — literals, not recorded responses. All 10 of those tests would still pass if
grants.gov renamed or dropped a field, and production would break silently. Rather than
copy those literals here (which would only duplicate the same belief), the drift tests
parse the contract module's source and walk the dicts it actually asserts on, so editing
the fixture moves the drift check with it.

Rule L3: grants.gov answers HTTP 200 with `errorcode: 0` and `msg: "Webservice
Succeeds"` even when its own backend is unavailable — the tell is a `data` block of
`{serverURI, message}` where an opportunity should be. Every assertion below is worded
to separate provider-down / rate-limited / schema-changed / our-parser-broken.
"""

import ast
import pathlib

import httpx
import pytest

from src.agent.grantbot import _parse_close_date
from src.services import grants

pytestmark = [pytest.mark.live_api]

_CONTRACT_FILE = (
    pathlib.Path(__file__).resolve().parents[1] / "contract" / "test_grants_contract.py"
)

# The keys `search_opportunities`/`list_posted_opportunities` promise their callers.
LIST_KEYS = {"id", "number", "title", "agency", "open_date", "close_date"}
SEARCH_KEYS = LIST_KEYS | {"description"}

# camelCase keys mark a contract literal as API-shaped. The same module also contains
# snake_case dicts — the *expected output* of our mapper — and comparing those against
# grants.gov would report drift on field names grants.gov never had.
_API_SHAPED = frozenset({
    "agencyCode", "openDate", "closeDate", "awardCeiling", "awardFloor",
    "categoryOfFundingActivity", "eligibleApplicants", "additionalInformationUrl",
    "synopsis",
})

# Not an FOA prefix any agency issues, and confirmed to return zero hits.
BOGUS_NUMBER = "ZZZ-QQ-99-999"
# Pure nonsense: no English stem, so a working search must return nothing. ("nonsense"
# phrases built from real words like "termite" do match, and would make the control
# pass for the wrong reason.)
GIBBERISH = ["qxzjvbnp plurmfk", "zzqqxxwvv"]

_PAGE_SIZE = 250  # must match list_posted_opportunities' internal page size


def key_paths(obj, prefix="", empty=None):
    """Every dotted key path in a nested dict/list structure.

    ``empty`` collects the paths of containers that are present but EMPTY. That
    distinction is the whole difference between "grants.gov renamed a field" and "this
    particular opportunity has nothing in that list", and conflating them makes the
    drift check cry wolf.
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


def contract_fixture_literals() -> dict[str, list[dict]]:
    """The API-shaped dict literals `tests/contract/test_grants_contract.py` asserts on.

    Read out of that file's source rather than re-declared here: a second hand-written
    copy would drift from the first and the drift test would then be checking my belief
    about their belief. Split into "search" and "detail" by the enclosing test's name,
    because the two endpoints return different shapes and only one of them can be
    verified when the detail backend is down.
    """
    tree = ast.parse(_CONTRACT_FILE.read_text())
    out: dict[str, list[dict]] = {"search": [], "detail": []}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict):
                continue
            try:
                value = ast.literal_eval(node)
            except (ValueError, SyntaxError):
                continue  # not a pure literal (e.g. the envelope built by _search_payload)
            if not isinstance(value, dict) or "number" not in value:
                continue
            if not _API_SHAPED & set(value):
                continue  # a mapped-output expectation, not an API shape
            out["detail" if "detail" in fn.name else "search"].append(value)
    return out


async def _raw_post(url: str, payload: dict) -> dict:
    """A direct call, bypassing our parser — the drift tests must see grants.gov's own
    JSON, and the classification helpers must see the envelope our parser discards."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as exc:  # pragma: no cover - network
        pytest.fail(f"grants.gov {url} was unreachable ({exc!r}) — provider down or the "
                    "container has no egress; this is not a schema change")
    assert resp.status_code == 200, (
        f"grants.gov {url} answered HTTP {resp.status_code} — provider down or rate "
        f"limited (we treat grants.gov as 1 req/s), not a schema change. "
        f"Body: {resp.text[:300]!r}"
    )
    return resp.json()


def detail_backend_outage(raw: dict) -> str | None:
    """grants.gov's own "my backend is down" body, or None if this is a real response.

    fetchOpportunity returns HTTP 200 / errorcode 0 / msg "Webservice Succeeds" and puts
    `{serverURI, message}` in `data` when the apply07 backend is unavailable. Without
    this check, `fetch_opportunity_detail` returning None during an outage is
    indistinguishable from our parser dropping a valid opportunity (Rule L3).
    """
    data = raw.get("data")
    if isinstance(data, dict) and "serverURI" in data and "number" not in data:
        return str(data.get("message") or data)[:300]
    return None


# --------------------------------------------------------------------------- T3.1


async def test_list_posted_opportunities_returns_a_wellformed_page(api_budget):
    """Shape, key set, paging arithmetic and date parseability — never an opportunity.

    Control: the raw probe supplies `hitCount` independently, so "we got a lot of rows"
    cannot be satisfied by a pager that silently stopped after page one, and cannot be
    called a failure when grants.gov genuinely has few postings.
    """
    agencies = grants.BIOMEDICAL_AGENCIES
    api_budget.wait("grants")
    probe = await _raw_post(grants.SEARCH_URL, {
        "oppStatuses": "posted",
        "agencies": "|".join(agencies),
        "rows": 1,
        "startRecordNum": 0,
    })
    hit_count = probe.get("data", {}).get("hitCount")
    assert isinstance(hit_count, int) and hit_count > 0, (
        "grants.gov search2 did not return an integer data.hitCount for posted "
        f"{agencies} opportunities — either the envelope changed shape (schema) or the "
        f"provider is degraded. Got: {probe.get('data', {}).get('hitCount')!r}"
    )

    # Charge the budget for every page list_posted_opportunities is about to request;
    # it paginates internally and never sees the rate limiter.
    for _ in range(min(hit_count // _PAGE_SIZE + 1, 10)):
        api_budget.wait("grants")
    listed = await grants.list_posted_opportunities()

    assert listed, (
        f"grants.gov reports {hit_count} posted {agencies} opportunities but "
        "list_posted_opportunities parsed none — the data.oppHits path is broken "
        "(schema change or parser), not an empty catalogue"
    )
    for item in listed[:50]:
        assert set(item) == LIST_KEYS, (
            "list_posted_opportunities' mapped keys changed — callers "
            f"(agent/grantbot.py, agent/tools.py) read {sorted(LIST_KEYS)}. "
            f"Got {sorted(item)}"
        )
        assert item["id"], f"an opportunity came back with no id: {item}"
        assert isinstance(item["number"], str) and item["number"].strip(), (
            f"empty `number` — hit.number is gone from grants.gov's response: {item}"
        )
        assert isinstance(item["title"], str) and item["title"].strip(), (
            f"empty `title` — hit.title is gone from grants.gov's response: {item}"
        )

    ids = [str(o["id"]) for o in listed]
    assert len(set(ids)) == len(ids), (
        f"list_posted_opportunities returned {len(ids) - len(set(ids))} duplicate ids — "
        "startRecordNum is not advancing, so every page is the same page"
    )
    if hit_count > _PAGE_SIZE:
        assert len(listed) > _PAGE_SIZE, (
            f"grants.gov reports {hit_count} hits but we collected {len(listed)} "
            f"(= one page of {_PAGE_SIZE}) — pagination stopped after the first page"
        )
    assert len(listed) >= hit_count * 0.9, (
        f"collected {len(listed)} of {hit_count} reported hits — the pager is dropping "
        "pages (a little slack is allowed for the index changing mid-run)"
    )

    # The agency filter is a parameter we send; if it stopped being honoured we would
    # be flooding GrantBot with every agency's postings and never notice.
    off_target = sorted({o["agency"] for o in listed} - set(agencies))
    assert not off_target, (
        f"asked grants.gov for {agencies} and got {off_target} back — the `agencies` "
        "payload field was ignored or is being joined with the wrong separator"
    )

    # Dates: `_parse_close_date` (agent/grantbot.py) returns None for anything it cannot
    # read, and a None deadline is treated as "rolling" and PASSES the lead-time filter.
    # A format change would therefore silently disable lead-time filtering entirely,
    # which is exactly the kind of failure only a live test can see.
    dated = [o for o in listed if o["close_date"]]
    assert dated, (
        "no posted opportunity carried a close_date — hit.closeDate is gone, and "
        "grantbot's lead-time filter would treat every FOA as rolling"
    )
    unparsed = [o["close_date"] for o in dated if _parse_close_date(o["close_date"]) is None]
    assert len(unparsed) <= len(dated) * 0.1, (
        f"{len(unparsed)} of {len(dated)} close_dates are unparseable by "
        "src.agent.grantbot._parse_close_date, which accepts %m/%d/%Y, %Y-%m-%d and "
        f"%Y/%m/%d — grants.gov changed its date format. Examples: {unparsed[:5]}"
    )
    opened = [o for o in listed if o["open_date"]]
    bad_open = [o["open_date"] for o in opened if _parse_close_date(o["open_date"]) is None]
    assert len(bad_open) <= len(opened) * 0.1, (
        f"{len(bad_open)} of {len(opened)} open_dates are unparseable — grants.gov "
        f"changed its date format. Examples: {bad_open[:5]}"
    )


# --------------------------------------------------------------------------- T3.2


async def test_the_contract_search_fixture_still_matches_grants_gov(api_budget):
    """Rule L1, the load-bearing test in this file.

    Walks every key path the hand-written search-hit literals in
    tests/contract/test_grants_contract.py assert on and requires it to exist in a live
    oppHits entry.

    Control: minimum path counts are asserted on both sides first. If the extractor
    found nothing, or grants.gov returned an empty page, `renamed` would be trivially
    empty and a pass would prove nothing — which is the precise failure mode Rule L1 is
    about.
    """
    fixtures = contract_fixture_literals()["search"]
    assert len(fixtures) >= 3, (
        f"only extracted {len(fixtures)} search-hit literals from {_CONTRACT_FILE.name} "
        "— the AST extractor is broken (or the contract file was restructured), so the "
        "comparison below would be vacuous"
    )
    fixture_paths = {p for f in fixtures for p in key_paths(f) if p}
    assert len(fixture_paths) >= 6, (
        f"the fixture walker found only {len(fixture_paths)} paths: {sorted(fixture_paths)}"
    )

    api_budget.wait("grants")
    raw = await _raw_post(grants.SEARCH_URL, {
        "oppStatuses": "posted",
        "agencies": "|".join(grants.BIOMEDICAL_AGENCIES),
        "rows": 25,
        "startRecordNum": 0,
    })
    hits = raw.get("data", {}).get("oppHits") or []
    assert len(hits) >= 5, (
        f"grants.gov returned {len(hits)} oppHits for a broad posted search — provider "
        "degraded or the envelope moved; the drift comparison would be meaningless"
    )

    # Union across hits: an optional key absent from one opportunity is not a rename.
    live_empty: set[str] = set()
    live_paths = {p for h in hits for p in key_paths(h, empty=live_empty) if p}
    assert len(live_paths) >= 8, (
        f"a live oppHit has only {len(live_paths)} key paths ({sorted(live_paths)}) — "
        "grants.gov's response shrank dramatically"
    )

    absent = [p for p in fixture_paths if p not in live_paths]
    # A path under an EMPTY live container tells us nothing: the key may be intact and
    # simply have no rows in these opportunities.
    unverifiable = sorted(p for p in absent if any(p.startswith(e + ".") for e in live_empty))
    renamed = sorted(p for p in absent if p not in unverifiable)

    assert not renamed, (
        "grants.gov's live search2 oppHits no longer contain key paths that "
        "tests/contract/test_grants_contract.py's hand-written fixtures assert on, and "
        "their parent containers are NOT empty — this is a real schema difference and "
        "those contract tests are pinning a shape grants.gov does not return:\n  "
        + "\n  ".join(renamed)
        + f"\nLive keys actually present: {sorted(live_paths)}"
    )
    verified = fixture_paths - set(unverifiable)
    assert len(verified) >= 5, (
        f"only {len(verified)} fixture paths could be checked against live data "
        f"({len(unverifiable)} sit under empty containers: {unverifiable}) — this run "
        "proved almost nothing"
    )


async def test_the_contract_detail_fixture_still_matches_grants_gov(api_budget):
    """Rule L1 for fetchOpportunity. Same technique as the search drift test.

    Rule L3: when grants.gov's detail backend is unavailable this SKIPS with the
    provider's own message rather than passing. A green run here must mean "verified",
    never "could not look".
    """
    fixtures = contract_fixture_literals()["detail"]
    assert len(fixtures) >= 1, (
        f"extracted no detail literals from {_CONTRACT_FILE.name} — the AST extractor "
        "is broken and this comparison would be vacuous"
    )
    fixture_paths = {p for f in fixtures for p in key_paths(f) if p}
    assert len(fixture_paths) >= 10, (
        f"the fixture walker found only {len(fixture_paths)} detail paths: "
        f"{sorted(fixture_paths)}"
    )

    api_budget.wait("grants")
    page = await grants.search_opportunities("cancer", agencies=["HHS-NIH11"], rows=5)
    assert page, (
        "could not obtain any live opportunity to look up — search2 is down or its "
        "oppHits path moved; the detail fixture is unchecked either way"
    )
    opp_id = str(page[0]["id"])

    api_budget.wait("grants")
    raw = await _raw_post(grants.DETAIL_URL, {"oppId": opp_id})
    outage = detail_backend_outage(raw)
    if outage:
        pytest.skip(
            "PROVIDER DOWN, not a schema change: grants.gov fetchOpportunity answered "
            f"HTTP 200 / errorcode {raw.get('errorcode')!r} / msg {raw.get('msg')!r} but "
            f"its backend reported {outage!r}. The detail-endpoint half of "
            "test_grants_contract.py is therefore UNVERIFIED."
        )

    live_empty: set[str] = set()
    live_paths = {p for p in key_paths(raw.get("data", {}), empty=live_empty) if p}
    assert len(live_paths) >= 8, (
        f"the detail response has only {len(live_paths)} key paths — grants.gov "
        "returned something unexpected and the comparison would be meaningless"
    )
    absent = [p for p in fixture_paths if p not in live_paths]
    unverifiable = sorted(p for p in absent if any(p.startswith(e + ".") for e in live_empty))
    renamed = sorted(p for p in absent if p not in unverifiable)
    assert not renamed, (
        "grants.gov's fetchOpportunity response no longer contains key paths that "
        "tests/contract/test_grants_contract.py asserts on, and their parents are not "
        "empty — a real schema change:\n  " + "\n  ".join(renamed)
        + f"\nLive keys actually present: {sorted(live_paths)}"
    )
    verified = fixture_paths - set(unverifiable)
    assert len(verified) >= 6, (
        f"only {len(verified)} of {len(fixture_paths)} detail fixture paths were "
        f"checkable ({len(unverifiable)} under empty containers: {unverifiable})"
    )


# --------------------------------------------------------------------------- T3.3


async def test_search_opportunities_narrows(api_budget):
    """Two independent narrowings, because they catch two different mutations: dropping
    the `keyword` field, and dropping/mis-joining the `agencies` field.

    Control: each narrow query must return >= 1. Without that, "fewer" is satisfied by a
    search that is simply broken — the single most likely way this test would lie.
    """
    rows = 200  # must exceed the narrow result counts or the cap decides the comparison

    api_budget.wait("grants")
    broad = await grants.search_opportunities("research", rows=rows)
    api_budget.wait("grants")
    narrow = await grants.search_opportunities("cancer", rows=rows)
    api_budget.wait("grants")
    narrower = await grants.search_opportunities("cancer", agencies=["HHS-NIH11"], rows=rows)

    assert len(broad) >= 1, (
        "the broad query returned nothing at all — grants.gov is down, rate limiting "
        "us, or search2's oppHits path moved. Nothing below can be concluded"
    )
    assert len(narrow) >= 1, (
        "CONTROL FAILED: the narrow query ('cancer') returned nothing, so a smaller "
        "result set would prove only that the search is broken, not that it narrows"
    )
    assert len(narrow) < len(broad), (
        f"'cancer' returned {len(narrow)} and 'research' returned {len(broad)} — a more "
        "specific keyword did not narrow the result set, so the `keyword` field is "
        "probably not reaching grants.gov (or both queries hit the rows cap of "
        f"{rows}, which would also make this comparison meaningless)"
    )

    assert len(narrower) >= 1, (
        "CONTROL FAILED: 'cancer' filtered to HHS-NIH11 returned nothing, so 'fewer "
        "than unfiltered' proves nothing"
    )
    assert len(narrower) < len(narrow), (
        f"filtering 'cancer' to HHS-NIH11 returned {len(narrower)}, the same or more "
        f"than the unfiltered {len(narrow)} — the `agencies` payload field is being "
        "ignored"
    )
    off_target = sorted({o["agency"] for o in narrower} - {"HHS-NIH11"})
    assert not off_target, (
        f"asked for HHS-NIH11 only and got {off_target} — the agency filter is not "
        "being applied (wrong payload key, or the '|' join changed)"
    )
    for opp in narrower:
        assert set(opp) == SEARCH_KEYS, (
            "search_opportunities' mapped keys changed — callers read "
            f"{sorted(SEARCH_KEYS)}. Got {sorted(opp)}"
        )


# --------------------------------------------------------------------------- T3.4


async def test_fetch_opportunity_detail_round_trips_an_id_from_the_live_page(api_budget):
    """Rule L2: the id comes from the live page fetched moments earlier, so this test
    can never go stale the way a pinned opp_id would.

    Rule L3, four outcomes: unreachable (raises), grants.gov's own backend down
    (documented envelope -> skip), a real body that our parser threw away (fail, ours),
    or the round trip (pass).
    """
    api_budget.wait("grants")
    page = await grants.search_opportunities("cancer", agencies=["HHS-NIH11"], rows=5)
    assert page, (
        "search2 returned no opportunity to look up — provider down or the oppHits "
        "path moved; the round trip is untested either way"
    )
    source = page[0]
    opp_id, opp_number = str(source["id"]), source["number"]

    api_budget.wait("grants")
    try:
        detail = await grants.fetch_opportunity_detail(opp_id)
    except httpx.HTTPError as exc:
        pytest.fail(
            f"fetch_opportunity_detail({opp_id!r}) raised {exc!r} — grants.gov's detail "
            "endpoint is unreachable or non-200. Note the caller (agent/tools.py) does "
            "not catch this"
        )

    if detail is None:
        api_budget.wait("grants")
        raw = await _raw_post(grants.DETAIL_URL, {"oppId": opp_id})
        outage = detail_backend_outage(raw)
        if outage:
            # Graceful degradation still asserted: a valid id during an outage must
            # yield None, not a half-populated dict the agents would post as fact.
            assert detail is None
            pytest.skip(
                "PROVIDER DOWN, not our parser: grants.gov fetchOpportunity answered "
                f"HTTP 200 / errorcode {raw.get('errorcode')!r} / msg {raw.get('msg')!r} "
                f"for the live id {opp_id} but its backend reported {outage!r}. "
                "fetch_opportunity_detail correctly returned None. The id round trip is "
                "UNVERIFIED today."
            )
        pytest.fail(
            f"OUR PARSER: grants.gov returned a real body for oppId {opp_id} but "
            "fetch_opportunity_detail returned None — its `data.get('number')` guard is "
            f"discarding a valid opportunity. Live data keys: "
            f"{sorted(raw.get('data', {}))}"
        )

    assert str(detail["id"]) == opp_id, (
        f"asked for oppId {opp_id} and got back id {detail['id']!r} — the detail "
        "endpoint is answering with a different opportunity, which would attribute the "
        "wrong FOA to a PI"
    )
    assert detail["number"].upper() == opp_number.upper(), (
        f"id {opp_id} is number {opp_number!r} in search2 but {detail['number']!r} in "
        "fetchOpportunity — the two endpoints disagree about the same opportunity"
    )
    assert set(detail) >= LIST_KEYS | {"synopsis", "award_ceiling", "eligibility"}, (
        f"fetch_opportunity_detail's mapped keys changed; agent/tools.py and "
        f"agent/foa_cache.py read them. Got {sorted(detail)}"
    )

    # Round trip the other way: number -> opportunity must reach the same id.
    api_budget.wait("grants")
    api_budget.wait("grants")  # by_number searches, then fetches detail
    by_number = await grants.fetch_opportunity_by_number(opp_number)
    assert by_number is not None, (
        f"fetch_opportunity_by_number({opp_number!r}) found nothing for a number that "
        "grants.gov returned seconds ago — its keyword search (rows=5) did not surface "
        "the exact match"
    )
    assert str(by_number["id"]) == opp_id, (
        f"number {opp_number!r} resolved to id {by_number['id']!r}, not {opp_id} — the "
        "number->id lookup is matching the wrong opportunity"
    )


# --------------------------------------------------------------------------- T3.5


async def test_an_unknown_opportunity_number_returns_none(api_budget):
    """Absence assertion + its positive control, in that order of importance.

    Control: a number taken from the live page must resolve in the SAME test. Without
    it, `None` for the bogus number is equally well explained by grants.gov being down,
    which is the Rule L3 confusion this test exists to prevent.
    """
    api_budget.wait("grants")
    page = await grants.search_opportunities("cancer", agencies=["HHS-NIH11"], rows=5)
    assert page, (
        "no live opportunity available — grants.gov is down, so the negative result "
        "below would prove nothing"
    )
    real_number = page[0]["number"]

    api_budget.wait("grants")
    api_budget.wait("grants")  # search, then (attempted) detail
    found = await grants.fetch_opportunity_by_number(real_number)
    assert found is not None, (
        f"CONTROL FAILED: {real_number!r} came from grants.gov seconds ago but "
        "fetch_opportunity_by_number returned None for it. Until a real number "
        "resolves, `None` for a fake one means nothing"
    )
    assert found.get("number", "").upper() == real_number.upper(), (
        f"asked for {real_number!r}, got {found.get('number')!r} — the number match in "
        "fetch_opportunity_by_number is returning a different opportunity"
    )

    api_budget.wait("grants")
    try:
        missing = await grants.fetch_opportunity_by_number(BOGUS_NUMBER)
    except httpx.HTTPError as exc:
        pytest.fail(
            f"fetch_opportunity_by_number({BOGUS_NUMBER!r}) raised {exc!r} instead of "
            "returning None — an unknown FOA number must degrade, not propagate a "
            "transport error to the agent loop"
        )
    assert missing is None, (
        f"a nonexistent FOA number resolved to {missing!r} — grants.gov's keyword search "
        "is fuzzy, and the exact-number guard in fetch_opportunity_by_number is the only "
        "thing stopping an agent from citing an unrelated opportunity"
    )


# --------------------------------------------------------------------------- T3.6


async def test_search_for_researchers_matches_real_keywords_and_not_gibberish(api_budget):
    """Positive and negative in one call, so "no results" can be attributed.

    Control: the real-keyword researcher must come back non-empty. `search_for_researchers`
    swallows every exception per keyword, so an empty result for the gibberish researcher
    is otherwise indistinguishable from grants.gov refusing the request entirely.

    The real keyword is listed twice on purpose: that guarantees the dedup path is
    exercised, so the uniqueness assertion below cannot pass vacuously.
    """
    keyword = "cancer immunotherapy"
    query = {"real": [keyword, keyword], "gibberish": GIBBERISH}

    for _ in range(len(query["real"]) + len(query["gibberish"])):
        api_budget.wait("grants")
    out = await grants.search_for_researchers(query, max_per_query=5)

    assert set(out) == {"real", "gibberish"}, (
        f"search_for_researchers dropped a researcher from its result map: {sorted(out)}"
        " — every agent_id must get a key even when nothing matched"
    )
    real = out["real"]
    assert real, (
        f"CONTROL FAILED: {keyword!r} matched no posted "
        f"{grants.BIOMEDICAL_AGENCIES} opportunity. Either grants.gov is down/rate "
        "limiting (search_for_researchers swallows the exception and logs a warning) or "
        "the query is not reaching it. The gibberish result below proves nothing until "
        "this passes"
    )
    assert out["gibberish"] == [], (
        f"nonsense keywords {GIBBERISH} matched {len(out['gibberish'])} opportunities "
        f"({[o['number'] for o in out['gibberish'][:5]]}) — the keyword is being ignored "
        "and every researcher would be handed the same generic list"
    )

    assert len(real) >= 2, (
        f"only {len(real)} result(s) for {keyword!r}; the duplicate-keyword dedup "
        "control needs at least two, so the uniqueness assertion below is weak"
    )
    numbers = [o["number"] for o in real]
    assert len(set(numbers)) == len(numbers), (
        f"the same keyword was searched twice and produced duplicate FOA numbers "
        f"({len(numbers) - len(set(numbers))} of them) — the `seen_for_agent` dedup in "
        "search_for_researchers is not working, and agents would see each opportunity "
        "once per matching keyword"
    )
    for opp in real:
        assert set(opp) == SEARCH_KEYS | {"matched_keyword"}, (
            "search_for_researchers' result keys changed — grantbot.py reads them to "
            f"build its prompt. Got {sorted(opp)}"
        )
        assert opp["matched_keyword"] == keyword, (
            f"matched_keyword is {opp['matched_keyword']!r}, not the keyword that "
            "produced the hit — the provenance tag GrantBot cites is wrong"
        )
        assert opp["agency"] in grants.BIOMEDICAL_AGENCIES, (
            f"{opp['agency']!r} is outside the default agency filter "
            f"{grants.BIOMEDICAL_AGENCIES} — search_for_researchers is not passing "
            "`agencies` through to search_opportunities"
        )
