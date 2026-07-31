"""Public routes: run-window arithmetic and the privacy boundary.

``tests/characterization/test_public_routes.py`` already pins that the ten public
endpoints *render*. This file covers the two things rendering cannot show:

1. **The arithmetic.** The graph routes slice one long-running simulation into
   date-bounded **run windows** (``.notes/cohort-system-v2.md`` §1 renamed the
   concept; the constants live in ``src/routers/public.py``). Three boundaries are
   in play and each is tested *at the exact edge*, never at a comfortable value in
   the middle:

   * ``decided_at >= window_start``      — inclusive lower edge
   * ``decided_at <  window_end``        — exclusive upper edge
   * ``created_at >= window_start_bound``— inclusive lower edge, on *posts*

2. **The privacy boundary.** No public route may render ``collab_private``
   content. Every absence assertion below is paired with a positive control in the
   same test, because "nothing private leaked" is trivially true of a page that
   renders nothing at all — which is exactly what these pages do when the seeding
   is wrong.

Real ASGI requests, real Postgres, real Jinja. Nothing is mocked.

**The module-level graph cache is cleared before every request** (see ``_get``).
``_cached_graph_payload`` memoizes per parameter set for 60s across *all* callers,
so without this a test would read the previous test's payload. The cache itself is
therefore deliberately not exercised here.
"""

import itertools
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.routers import public as public_mod
from src.routers.public import (
    CABO_WINDOW_START,
    JUNE_POST_START,
    SCHULTZ_GROUP_END,
    SCHULTZ_GROUP_START,
    SCHULTZ_PILOT_END,
    SCHULTZ_PILOT_ORCIDS,
    SCHULTZ_PILOT_START,
)
from tests import factories

pytestmark = pytest.mark.integration

TICK = timedelta(microseconds=1)  # the smallest interval Postgres timestamptz stores

# The Cabo window is inlined in the /cabo-graph route rather than exported as a
# constant, so it is re-stated here and anchored to the label the page prints
# (see test_cabo_window_start_is_inclusive_and_its_end_is_exclusive). If someone
# moves the route's window without moving this, the label assertion fails.
CABO_START = datetime(2026, 4, 27, tzinfo=UTC)
CABO_END = datetime(2026, 5, 8, tzinfo=UTC)  # exclusive
CABO_LABEL = "April 27 – May 7, 2026"

# Distinctive, JSON-safe markers. Plain [A-Za-z0-9-] so Jinja's |tojson cannot
# escape them into something a substring search would miss.
PUBLIC_SUMMARY = "PUBLICPROPOSAL-9f3a2b"
PRIVATE_SUMMARY = "PRIVATEPROPOSAL-7c1d84"
PUBLIC_CONTENT = "PUBLICPOSTBODY-51ee0a"
PRIVATE_CONTENT = "PRIVATEPOSTBODY-2a77bc"

# One representative in-window instant per graph route. /scripps-graph has no
# upper bound (decided_at >= March 1, forever), so it shares the Cabo instant.
ROUTE_WINDOWS = [
    ("/cabo-graph", datetime(2026, 5, 1, tzinfo=UTC)),
    ("/scripps-graph", datetime(2026, 5, 1, tzinfo=UTC)),
    ("/schultz-alumni-pilot", datetime(2026, 6, 2, tzinfo=UTC)),
    ("/schultz-group-alumni", datetime(2026, 6, 7, tzinfo=UTC)),
]
GRAPH_ROUTES = [p for p, _ in ROUTE_WINDOWS]

# The complete public surface, pinned. The route-inventory test below compares
# this against the live router, so an endpoint added to public.py without being
# classified here fails the suite rather than quietly escaping the privacy sweep.
ALL_PUBLIC_ROUTES = [
    ("GET", "/"),
    ("POST", "/waitlist"),
    ("GET", "/access-pending"),
    ("POST", "/access-pending/email"),
    ("GET", "/cabo-graph"),
    ("GET", "/scripps-graph"),
    ("GET", "/schultz-alumni-pilot"),
    ("GET", "/schultz-group-alumni"),
    ("POST", "/api/proposal-vote"),
    ("POST", "/api/proposal-vote/{vote_id}/details"),
]

# Three real Schultz-pilot ORCIDs paired with agent_ids that are also in the
# hardcoded Scripps bucket, so one roster satisfies all four routes' very
# different node-selection rules (ORCID list / Scripps set / all agents).
ROSTER = (
    ("su", "SuBot", "0000-0002-9859-4104"),        # Andrew Su
    ("lairson", "LairsonBot", "0000-0001-6701-996X"),  # Luke Lairson
    ("young", "YoungBot", "0000-0001-8562-5736"),  # Travis Young
)

_seq = itertools.count(770000)
_GRAPH_DATA_RE = re.compile(r'<script id="graph-data"[^>]*>(.*?)</script>', re.S)


# --- harness ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    public_mod._GRAPH_CACHE.clear()
    yield
    public_mod._GRAPH_CACHE.clear()


async def _get(client, path, **kwargs):
    """GET with the 60s payload cache dropped, so the response reflects this test."""
    public_mod._GRAPH_CACHE.clear()
    return await client.get(path, **kwargs)


def _ip_headers() -> dict:
    """A unique client IP per call, so the module-global rate limiters in
    public.py (30 votes/min, 10 waitlist signups/hour, per IP, per process) cannot
    make one test's result depend on how many tests ran before it."""
    return {"X-Real-IP": f"198.51.100.{next(_seq) % 254 + 1}"}


def _payload(response) -> dict:
    """The graph JSON the template hands to D3, parsed out of the page."""
    m = _GRAPH_DATA_RE.search(response.text)
    assert m, "the page rendered no #graph-data block at all"
    return json.loads(m.group(1))


def _pairs(response) -> set[frozenset]:
    return {frozenset((link["source"], link["target"])) for link in _payload(response)["links"]}


def test_the_payload_extractor_distinguishes_populated_from_empty():
    """Control for the helper every assertion below leans on. An extractor that
    returned ``{}`` on every page would make the link assertions vacuous."""

    class _R:
        text = (
            '<html><script id="graph-data" type="application/json" nonce="x">'
            '{"nodes": [{"id": "su"}], "links": [{"source": "su", "target": "lairson"}]}'
            "</script></html>"
        )

    assert _payload(_R())["nodes"] == [{"id": "su"}]
    assert _pairs(_R()) == {frozenset(("su", "lairson"))}


@pytest.fixture
async def roster(db_session):
    """Three agents visible to all four routes at once."""
    for _aid, _bot, orcid in ROSTER:
        assert orcid in SCHULTZ_PILOT_ORCIDS, (
            f"{orcid} left SCHULTZ_PILOT_ORCIDS — /schultz-alumni-pilot would render "
            "an empty graph and every assertion in this file would pass vacuously"
        )
    for aid, _bot, _orcid in ROSTER:
        assert aid in public_mod._SCRIPPS, (
            f"agent {aid} left the _SCRIPPS bucket — /scripps-graph would render empty"
        )

    out = {}
    for aid, bot, orcid in ROSTER:
        user = await factories.make_user(
            db_session, orcid=orcid, email=f"{aid}@example.org",
            institution="Scripps Research",
        )
        out[aid] = await factories.make_agent(
            db_session, user=user, agent_id=aid, bot_name=bot,
            pi_name=f"PI {aid}", status="active",
        )
    await db_session.flush()
    return out


@pytest.fixture
async def run(db_session):
    return await factories.make_simulation_run(db_session)


async def _seed_edge(
    db,
    run,
    *,
    a,
    b,
    decided_at,
    summary,
    visibility="public",
    post_created_at=None,
    content=None,
):
    """One graph edge, as the engine actually writes it.

    An edge needs BOTH halves: a ``new_post`` ``AgentMessage`` whose ``created_at``
    clears ``window_start_bound``, and a ``proposal`` ``ThreadDecision`` on that
    post's ``message_ts`` whose ``decided_at`` falls in the decision window.
    ``post_created_at`` defaults to ``decided_at`` so the post boundary is never
    accidentally the thing under test.
    """
    ts = f"{next(_seq)}.000100"
    await factories.make_agent_message(
        db, run=run, agent_id=a, phase="new_post", message_ts=ts,
        created_at=post_created_at if post_created_at is not None else decided_at,
        visibility=visibility,
        content=content if content is not None else f"body for {summary}",
        sender_name=f"{a}Bot", posted_at=float(next(_seq)),
    )
    decision = await factories.make_thread_decision(
        db, run=run, thread_id=ts, agent_a=a, agent_b=b, outcome="proposal",
        origin_visibility=visibility, decided_at=decided_at, summary_text=summary,
    )
    await db.flush()
    return decision


# --- run-window arithmetic: the exact edges --------------------------------


def test_the_two_june_windows_are_adjacent():
    """The premise the boundary test rests on. If the windows ever stop touching,
    'lands in exactly one window' is no longer the property being tested — there
    would be a gap (or an overlap) and the next test would be measuring something
    else."""
    assert SCHULTZ_PILOT_END == SCHULTZ_GROUP_START, (
        f"pilot ends {SCHULTZ_PILOT_END}, group starts {SCHULTZ_GROUP_START}"
    )
    assert SCHULTZ_PILOT_START < SCHULTZ_PILOT_END < SCHULTZ_GROUP_END


async def test_a_decision_exactly_on_the_shared_boundary_lands_in_exactly_one_window(
    client, db_session, roster, run
):
    """The instant Jun 5 00:00:00.000000Z belongs to the pilot window's exclusive
    end AND the group window's inclusive start. It must be counted once.

    Catches: ``decided_at < :window_end`` weakened to ``<=`` (the edge would show
    in both), and ``decided_at >= :decided_floor`` tightened to ``>`` (it would
    show in neither). Both mutations survive any test that samples the middle of a
    window.

    Control: the microsecond *before* the boundary lands in the pilot window and
    only there, so 'exactly one' is not satisfied by a pair of broken pages.
    """
    on = SCHULTZ_GROUP_START
    await _seed_edge(db_session, run, a="su", b="lairson", decided_at=on,
                     summary="EDGE-ON-BOUNDARY")
    await _seed_edge(db_session, run, a="su", b="young", decided_at=on - TICK,
                     summary="EDGE-ONE-TICK-EARLIER")

    pilot = await _get(client, "/schultz-alumni-pilot")
    group = await _get(client, "/schultz-group-alumni")
    assert pilot.status_code == 200 and group.status_code == 200

    landed_in = [w for w, r in (("pilot", pilot), ("group", group))
                 if "EDGE-ON-BOUNDARY" in r.text]
    assert len(landed_in) == 1, (
        f"a decision at exactly {on.isoformat()} appeared in {landed_in or 'NO'} "
        "window(s); the boundary instant must be counted exactly once"
    )
    assert landed_in == ["group"], (
        "the boundary instant belongs to the window that OPENS on it "
        "(>= start), not to the one that closes on it (< end)"
    )

    assert "EDGE-ONE-TICK-EARLIER" in pilot.text, (
        "control leg failed: the microsecond before the boundary is not in the "
        "pilot window either, so the page is simply empty and the assertion "
        "above proves nothing"
    )
    assert "EDGE-ONE-TICK-EARLIER" not in group.text

    # And structurally, not just as text: each window holds its own single edge.
    assert _pairs(pilot) == {frozenset(("su", "young"))}
    assert _pairs(group) == {frozenset(("su", "lairson"))}


async def test_cabo_window_start_is_inclusive_and_its_end_is_exclusive(
    client, db_session, roster, run
):
    """Both edges of a closed-open window, at the exact instant, plus proof that
    the excluded row is otherwise perfectly renderable.

    Catches: ``>=``→``>`` on the start (April 27 00:00 would vanish), ``<``→``<=``
    on the end (May 8 00:00 would appear), and a silent move of either constant
    (the page's own label is asserted).
    """
    await _seed_edge(db_session, run, a="su", b="lairson", decided_at=CABO_START,
                     summary="CABO-AT-START")
    await _seed_edge(db_session, run, a="su", b="young", decided_at=CABO_END - TICK,
                     summary="CABO-LAST-INSTANT")
    await _seed_edge(db_session, run, a="lairson", b="young", decided_at=CABO_END,
                     summary="CABO-AT-END")

    r = await _get(client, "/cabo-graph")
    assert r.status_code == 200
    assert CABO_LABEL in r.text, (
        "the /cabo-graph route no longer claims the window this test asserts on; "
        f"expected the page to say {CABO_LABEL!r}"
    )
    assert "CABO-AT-START" in r.text, "the inclusive start instant was dropped"
    assert "CABO-LAST-INSTANT" in r.text, "the last representable instant was dropped"
    assert "CABO-AT-END" not in r.text, (
        f"a decision at exactly {CABO_END.isoformat()} appeared; window_end is exclusive"
    )

    # Control: the excluded row is a normal, public, well-formed proposal — it
    # renders on /scripps-graph, whose window has no upper bound. So "CABO-AT-END
    # is absent" is about the window arithmetic, not about a malformed fixture.
    scripps = await _get(client, "/scripps-graph")
    assert "CABO-AT-END" in scripps.text, (
        "control leg failed: the excluded decision does not render anywhere, so "
        "its absence from /cabo-graph proves nothing about window_end"
    )


async def test_the_post_creation_bound_is_inclusive_at_the_exact_instant(
    client, db_session, roster, run
):
    """``window_posts`` bounds *posts* by ``created_at >= window_start_bound``,
    independently of when the proposal was decided. Both decisions below sit well
    inside the pilot decision window; only the originating posts differ, by one
    microsecond across the bound.

    Catches: ``>=``→``>`` on the post bound, and removal of the
    ``thread_id IN (SELECT message_ts FROM window_posts)`` join (which would let
    the pre-bound post's edge through).

    Note the consequence, which is real rather than hypothetical: for
    /schultz-alumni-pilot ``JUNE_POST_START == SCHULTZ_PILOT_START``, so that
    window has ZERO lead-in — a thread opened May 31 and decided June 2 is
    silently dropped. The route's own docstring warns against exactly this ("a
    thread can be opened a couple days before its proposal lands").
    """
    decided = SCHULTZ_PILOT_START + timedelta(days=1)
    await _seed_edge(db_session, run, a="su", b="lairson", decided_at=decided,
                     post_created_at=JUNE_POST_START, summary="POST-AT-BOUND")
    await _seed_edge(db_session, run, a="su", b="young", decided_at=decided,
                     post_created_at=JUNE_POST_START - TICK,
                     summary="POST-ONE-TICK-BEFORE-BOUND")

    r = await _get(client, "/schultz-alumni-pilot")
    assert r.status_code == 200
    assert "POST-AT-BOUND" in r.text, (
        "a post created at exactly window_start_bound was excluded; the bound is >="
    )
    assert "POST-ONE-TICK-BEFORE-BOUND" not in r.text, (
        "a post created one microsecond before window_start_bound was included"
    )
    assert _pairs(r) == {frozenset(("su", "lairson"))}


async def test_a_decision_before_the_window_start_is_excluded(
    client, db_session, roster, run
):
    """The lower decision edge from the other side, with its control one tick later.

    Catches ``decided_at >= :decided_floor`` loosened to ``>=`` on a different
    column, or dropped entirely — either of which would leak the whole simulation's
    history into a window that claims four days of it.
    """
    await _seed_edge(db_session, run, a="su", b="lairson",
                     decided_at=SCHULTZ_PILOT_START - TICK, summary="BEFORE-WINDOW")
    await _seed_edge(db_session, run, a="su", b="young",
                     decided_at=SCHULTZ_PILOT_START, summary="AT-WINDOW-START")

    r = await _get(client, "/schultz-alumni-pilot")
    assert r.status_code == 200
    assert "AT-WINDOW-START" in r.text, "the inclusive start instant was dropped"
    assert "BEFORE-WINDOW" not in r.text, (
        "a decision one microsecond before window_start was included"
    )


async def test_only_the_first_proposal_on_a_thread_is_published(
    client, db_session, roster, run
):
    """``thread_first`` keeps the EARLIEST decision per (pair, thread).

    The query's own comment says why: a later row on the same thread is a
    re-proposal made after a PI reopened and refined it, so its summary carries
    human feedback that was never meant for the public graph. This is arithmetic
    with a privacy edge, and nothing else asserts it.

    Catches: ``ORDER BY a, b, thread_id, decided_at ASC`` flipped to ``DESC`` (the
    human-influenced text would be published), and removal of the ``DISTINCT ON``
    (the pair would be double-counted as two joint proposals).

    Control: the first proposal IS published, so "the later one is absent" is not
    the empty page again.
    """
    decided = datetime(2026, 6, 7, tzinfo=UTC)
    ts = f"{next(_seq)}.000100"
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", phase="new_post", message_ts=ts,
        created_at=decided, visibility="public", content="the originating post",
        posted_at=float(next(_seq)),
    )
    for offset, summary in ((0, "FIRST-BOT-PROPOSAL"), (2, "LATER-REPROPOSAL-AFTER-PI")):
        await factories.make_thread_decision(
            db_session, run=run, thread_id=ts, agent_a="su", agent_b="lairson",
            outcome="proposal", origin_visibility="public",
            decided_at=decided + timedelta(hours=offset), summary_text=summary,
        )
    await db_session.flush()

    r = await _get(client, "/schultz-group-alumni")
    assert r.status_code == 200
    # Control first, so neither assertion below can pass on an empty page: the pair
    # renders, exactly once, no matter which of the two summaries got picked.
    links = _payload(r)["links"]
    assert len(links) == 1 and links[0]["weight"] == 1, (
        f"one thread must render as one edge counting one joint proposal: {links}"
    )
    assert "LATER-REPROPOSAL-AFTER-PI" not in r.text, (
        "the re-proposal made after a PI reopened and refined the thread was "
        "published; only the bots' own first proposal belongs on the public graph"
    )
    assert "FIRST-BOT-PROPOSAL" in r.text, (
        "the bots' first proposal was dropped along with the re-proposal"
    )


# --- an empty window must render ------------------------------------------


@pytest.mark.parametrize("path,decided_at", ROUTE_WINDOWS, ids=[p for p, _ in ROUTE_WINDOWS])
async def test_an_empty_window_renders_and_a_populated_one_shows_its_edge(
    client, db_session, roster, run, path, decided_at
):
    """An empty run window is a 200 with an empty graph, not a 500 — a page that
    divides by ``len(nodes)`` or indexes ``palette[0]`` fails here.

    Control, in the same test: the same route with one in-window edge renders that
    edge. Without it, "renders" is satisfied by a page that always shows nothing,
    which is also what every privacy assertion in this file would then be testing.
    """
    empty = await _get(client, path)
    assert empty.status_code == 200, f"{path} 500s on an empty window"
    assert "text/html" in empty.headers["content-type"]
    payload = _payload(empty)
    assert payload == {"nodes": [], "links": []}, f"{path} was not actually empty: {payload}"

    await _seed_edge(db_session, run, a="su", b="lairson", decided_at=decided_at,
                     summary=PUBLIC_SUMMARY)
    populated = await _get(client, path)
    assert populated.status_code == 200
    assert PUBLIC_SUMMARY in populated.text, (
        f"control leg failed: {path} shows nothing even with an in-window edge, so "
        "'the empty window renders' above is not distinguishable from a broken route"
    )
    assert _pairs(populated) == {frozenset(("su", "lairson"))}


# --- the privacy boundary --------------------------------------------------


@pytest.mark.parametrize("path,decided_at", ROUTE_WINDOWS, ids=[p for p, _ in ROUTE_WINDOWS])
async def test_no_graph_route_exposes_collab_private_content(
    client, db_session, roster, run, path, decided_at
):
    """THE load-bearing test: ``collab_private`` never reaches a public page.

    Seeds two real proposals in the same run and the same window — one ``public``,
    one ``collab_private`` — and asserts the private one leaks nothing: not its
    summary, not the body of the post it came from, not its decision id, and not
    even the *edge*, whose bare existence would disclose that two named PIs are
    collaborating privately.

    Catches: deleting ``AND origin_visibility = 'public'`` from the ``pairs`` CTE.

    Control, same test: the public proposal in the same window IS rendered. This
    is the whole point — these pages show nothing at all under a dozen unrelated
    faults, and "no private content" is true of every one of them.
    """
    await _seed_edge(db_session, run, a="su", b="lairson", decided_at=decided_at,
                     summary=PUBLIC_SUMMARY, content=PUBLIC_CONTENT, visibility="public")
    private = await _seed_edge(
        db_session, run, a="su", b="young", decided_at=decided_at,
        summary=PRIVATE_SUMMARY, content=PRIVATE_CONTENT, visibility="collab_private",
    )

    r = await _get(client, path)
    assert r.status_code == 200

    assert PUBLIC_SUMMARY in r.text, (
        f"control leg failed: {path} does not render the PUBLIC proposal either, so "
        "the private-content assertions below are vacuous"
    )
    assert frozenset(("su", "lairson")) in _pairs(r), "control leg failed: no public edge"

    assert PRIVATE_SUMMARY not in r.text, f"{path} LEAKED a collab_private summary"
    assert PRIVATE_CONTENT not in r.text, f"{path} LEAKED a collab_private message body"
    assert str(private.id) not in r.text, f"{path} LEAKED a collab_private decision id"
    assert private.thread_id not in r.text, f"{path} LEAKED a collab_private thread id"
    assert frozenset(("su", "young")) not in _pairs(r), (
        f"{path} disclosed the EXISTENCE of a private collaboration as a graph edge"
    )
    assert "young" not in {n["id"] for n in _payload(r)["nodes"]}, (
        f"{path} rendered a PI whose only activity is private"
    )


async def _seed_every_window(db, run):
    """A public + a private proposal inside every one of the four route windows."""
    seeded = []
    for _path, decided_at in ROUTE_WINDOWS:
        await _seed_edge(db, run, a="su", b="lairson", decided_at=decided_at,
                         summary=PUBLIC_SUMMARY, content=PUBLIC_CONTENT)
        seeded.append(
            await _seed_edge(db, run, a="su", b="young", decided_at=decided_at,
                             summary=PRIVATE_SUMMARY, content=PRIVATE_CONTENT,
                             visibility="collab_private")
        )
    return seeded


async def _exercise(client, method, path, ctx):
    """Drive one public endpoint with a request it will actually accept."""
    if path == "/waitlist":
        return await client.post(path, data={"email": "sweep@example.edu"},
                                 headers=_ip_headers())
    if path == "/access-pending/email":
        return await client.post(path, data={"email": "sweep@example.edu"},
                                 headers=_ip_headers())
    if path == "/api/proposal-vote":
        # Aimed straight at the private decision: the endpoint must neither accept
        # the vote nor echo anything about the row back.
        return await client.post(
            path,
            json={"decision_id": str(ctx["private_id"]), "vote": "up",
                  "voter_token": "sweep-tok"},
            headers=_ip_headers(),
        )
    if path == "/api/proposal-vote/{vote_id}/details":
        return await client.post(
            f"/api/proposal-vote/{ctx['vote_id']}/details",
            json={"details": "sweep", "voter_token": "sweep-tok"},
            headers=_ip_headers(),
        )
    assert method == "GET", f"no request builder for {method} {path}"
    return await _get(client, path, headers=_ip_headers())


@pytest.mark.parametrize(
    "method,path", ALL_PUBLIC_ROUTES, ids=[f"{m} {p}" for m, p in ALL_PUBLIC_ROUTES]
)
async def test_every_public_route_withholds_collab_private_content(
    client, db_session, roster, run, method, path
):
    """The same private proposal, held against all ten public endpoints.

    The four graph routes carry a positive control (the public proposal renders).
    The other six render no message content at all by design, so their control is
    different in kind but not weaker: the test asserts, by direct query in the same
    transaction, that the private row WAS present and visible while the request ran.
    Without that leg a fixture that silently failed to seed would score green here.
    """
    private_rows = await _seed_every_window(db_session, run)
    public_decision = (await db_session.execute(
        text(
            "SELECT id FROM thread_decisions "
            "WHERE summary_text = :s AND origin_visibility = 'public' LIMIT 1"
        ),
        {"s": PUBLIC_SUMMARY},
    )).scalar_one()
    created = await client.post(
        "/api/proposal-vote",
        json={"decision_id": str(public_decision), "vote": "up", "voter_token": "sweep-tok"},
        headers=_ip_headers(),
    )
    assert created.status_code == 200, f"could not seed a vote to exercise: {created.text}"
    ctx = {"private_id": private_rows[0].id, "vote_id": created.json()["id"]}

    # Control for every case: the private content really is in the database, in
    # this transaction, right now.
    stored = (await db_session.execute(
        text(
            "SELECT count(*) FROM thread_decisions "
            "WHERE summary_text = :s AND origin_visibility = 'collab_private'"
        ),
        {"s": PRIVATE_SUMMARY},
    )).scalar_one()
    assert stored == len(ROUTE_WINDOWS), (
        f"control leg failed: expected {len(ROUTE_WINDOWS)} private proposals in the "
        f"DB, found {stored}; an absence assertion against no data proves nothing"
    )

    r = await _exercise(client, method, path, ctx)
    assert r.status_code < 500, f"{method} {path} -> {r.status_code}: {r.text[:400]}"

    body = r.text
    for marker, what in (
        (PRIVATE_SUMMARY, "a collab_private proposal summary"),
        (PRIVATE_CONTENT, "a collab_private message body"),
    ):
        assert marker not in body, f"{method} {path} LEAKED {what}"
    for row in private_rows:
        assert str(row.id) not in body, f"{method} {path} LEAKED a private decision id"
        assert row.thread_id not in body, f"{method} {path} LEAKED a private thread id"

    if path in GRAPH_ROUTES:
        assert PUBLIC_SUMMARY in body, (
            f"control leg failed: {path} rendered no public proposal either"
        )

    if path == "/api/proposal-vote":
        # This endpoint renders no content, so "the marker is absent" is true of it
        # no matter what. What it CAN leak is existence: a 200 tells an anonymous
        # caller that the private proposal id is real. Only a 404 withholds that.
        assert r.status_code == 404, (
            f"the vote endpoint answered {r.status_code} for a collab_private "
            "proposal; anything but 404 confirms the private row exists"
        )


def test_the_privacy_sweep_covers_every_public_route():
    """A new endpoint in public.py must be visibly absent, not silently uncovered.

    ``ALL_PUBLIC_ROUTES`` is compared against the live router, so adding a route —
    say, a page that lists recent proposals — fails here until it is classified and
    swept. ``GRAPH_ROUTES`` (the content-rendering subset) is asserted to be a real,
    non-empty subset, so the classification cannot be emptied to make this pass.
    """
    live = {
        (m, r.path)
        for r in public_mod.router.routes
        for m in getattr(r, "methods", set())
        if m in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    pinned = set(ALL_PUBLIC_ROUTES)
    assert live == pinned, (
        "src/routers/public.py's route surface changed.\n"
        f"  new / unswept: {sorted(live - pinned)}\n"
        f"  gone: {sorted(pinned - live)}\n"
        "Classify each new route: add it to ALL_PUBLIC_ROUTES, and to ROUTE_WINDOWS "
        "too if it renders message content."
    )
    assert {("GET", p) for p in GRAPH_ROUTES} <= pinned, (
        "a content-rendering route in GRAPH_ROUTES is not a real public GET route"
    )
    assert len(GRAPH_ROUTES) == 4, (
        "the content-rendering classification was emptied or expanded without "
        "updating this pin"
    )


async def test_the_vote_endpoint_refuses_a_private_proposal_but_accepts_a_public_one(
    client, db_session, run
):
    """Both legs in one test. The characterization suite pins each half separately;
    a 404-for-everything endpoint satisfies the private half on its own, so the two
    are asserted together here.

    Catches: deleting ``AND origin_visibility = 'public'`` from the vote lookup,
    which would both write rows for private proposals and confirm their existence.
    """
    private = await factories.make_thread_decision(
        db_session, run=run, outcome="proposal", origin_visibility="collab_private",
        summary_text=PRIVATE_SUMMARY,
    )
    public = await factories.make_thread_decision(
        db_session, run=run, outcome="proposal", origin_visibility="public",
        summary_text=PUBLIC_SUMMARY,
    )
    await db_session.flush()

    refused = await client.post(
        "/api/proposal-vote",
        json={"decision_id": str(private.id), "vote": "up", "voter_token": "vote-tok"},
        headers=_ip_headers(),
    )
    assert refused.status_code == 404, (
        f"a collab_private proposal was votable: {refused.status_code} {refused.text[:300]}"
    )
    assert PRIVATE_SUMMARY not in refused.text
    assert str(private.id) not in refused.text, (
        "the 404 body echoed the private decision id back"
    )

    accepted = await client.post(
        "/api/proposal-vote",
        json={"decision_id": str(public.id), "vote": "up", "voter_token": "vote-tok"},
        headers=_ip_headers(),
    )
    assert accepted.status_code == 200, (
        "control leg failed: the endpoint refuses public proposals too, so the 404 "
        f"above says nothing about visibility ({accepted.status_code} {accepted.text[:300]})"
    )
    assert uuid.UUID(accepted.json()["id"])

    # And nothing was written for the private one.
    votes = (await db_session.execute(
        text("SELECT count(*) FROM proposal_votes WHERE thread_decision_id = :d"),
        {"d": str(private.id)},
    )).scalar_one()
    assert votes == 0, "a vote row was persisted against a collab_private proposal"


async def test_a_private_decision_on_a_public_post_still_does_not_render(
    client, db_session, roster, run
):
    """``window_posts`` does not filter on ``agent_messages.visibility`` — the
    only privacy filter in the edge query is ``origin_visibility`` on the decision.
    This pins that the single filter is load-bearing on its own: a private decision
    hanging off a perfectly public post is still withheld.

    Control: the public decision on an equally public post does render.
    """
    decided = datetime(2026, 6, 7, tzinfo=UTC)
    await _seed_edge(db_session, run, a="su", b="lairson", decided_at=decided,
                     summary=PUBLIC_SUMMARY, visibility="public")

    ts = f"{next(_seq)}.000100"
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", phase="new_post", message_ts=ts,
        created_at=decided, visibility="public", content="an ordinary public post",
        posted_at=float(next(_seq)),
    )
    await factories.make_thread_decision(
        db_session, run=run, thread_id=ts, agent_a="su", agent_b="young",
        outcome="proposal", origin_visibility="collab_private", decided_at=decided,
        summary_text=PRIVATE_SUMMARY,
    )
    await db_session.flush()

    r = await _get(client, "/schultz-group-alumni")
    assert r.status_code == 200
    assert PUBLIC_SUMMARY in r.text, "control leg failed: no public edge rendered"
    assert PRIVATE_SUMMARY not in r.text, (
        "a collab_private decision leaked because its originating post was public"
    )
    assert _pairs(r) == {frozenset(("su", "lairson"))}


# --- window constants are internally consistent ----------------------------


def test_the_declared_windows_do_not_overlap_or_invert():
    """Cheap arithmetic on the constants themselves. A typo that made a window
    inverted (start > end) would render an always-empty page, and every 'renders'
    test in the characterization suite would still pass."""
    for name, start, end in (
        ("cabo", CABO_START, CABO_END),
        ("schultz pilot", SCHULTZ_PILOT_START, SCHULTZ_PILOT_END),
        ("schultz group", SCHULTZ_GROUP_START, SCHULTZ_GROUP_END),
    ):
        assert start < end, f"{name} window is inverted: {start} .. {end}"
    assert CABO_WINDOW_START <= CABO_START, (
        "the Cabo post bound must not sit after the decision window it feeds"
    )
    assert JUNE_POST_START <= SCHULTZ_PILOT_START
    assert JUNE_POST_START <= SCHULTZ_GROUP_START
    assert CABO_END <= SCHULTZ_PILOT_START, "the Cabo and June windows overlap"
