"""Live integration tests for /admin/users and the mid-session access gate.

Two coupled concerns, one module, because they are the same production problem
seen from both ends:

1. **Visibility.** ``users`` rows exist for people who never passed the access
   gate — ``src/routers/auth.py`` creates them at ORCID sign-in so
   /admin/access-requests has something to list. On /admin/users those rows used
   to render identically to fully approved PIs (no ``access_status`` column, no
   filter), so an unapproved account was indistinguishable from a real one.
2. **Enforcement.** ``get_current_user`` only ever checked ``access_status`` at
   sign-in, so denying a user who was already logged in did nothing until their
   cookie expired.

Real ASGI requests, real Postgres, real Jinja templates — same harness as
tests/integration/test_cohort_admin.py.
"""

import base64
import json
import re
from datetime import UTC, datetime

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.models import Job, User
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


def _auth_as(user_id, impersonate_id) -> dict:
    """Session for ``user_id`` plus copi-impersonate pointed at another user.

    Both cookies ride one ``Cookie`` header, as in test_onboarding_flow.py.
    """
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {
        "Cookie": (
            f"copi-session={signer.sign(data).decode()}; "
            f"copi-impersonate={impersonate_id}"
        )
    }


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, is_admin=True, email="admin@example.org")


# ---------------------------------------------------------------------------
# HTML helpers
#
# The access badge is asserted from its own <td>, not by searching the whole
# row: 'bg-green-100 text-green-700' is also the profile-status "Complete" and
# agent-status "Active" badge, so a page-wide substring check would pass even if
# the access column rendered the wrong colour (or never rendered at all).
# ---------------------------------------------------------------------------

_COLUMN_COUNT = 11  # Name, Institution, ORCID, Access, Status, Agent, Pubs,
                    # Version, Claimed, Joined, Last Login


def _row_for(html: str, name: str) -> str:
    rows = [
        m.group(0)
        for m in re.finditer(r"<tr\b.*?</tr>", html, re.S)
        if f">{name}</div>" in m.group(0)
    ]
    assert len(rows) == 1, f"expected exactly one rendered row for {name!r}, got {len(rows)}"
    return rows[0]


def _access_cell(row: str) -> str:
    cells = re.findall(r"<td\b.*?</td>", row, re.S)
    assert len(cells) == _COLUMN_COUNT, (
        f"expected {_COLUMN_COUNT} cells per row, got {len(cells)} — the <td> list "
        "and the column map above have drifted apart, so cells[3] is no longer the "
        "Access column"
    )
    # The <th> list is not checked here (a header-only drift would not move this
    # index); the empty-state colspan is checked against _COLUMN_COUNT in
    # test_an_unknown_access_filter_value_matches_nothing_rather_than_500ing.
    return cells[3]


def _rendered_names(html: str) -> list[str]:
    """Every user name the table drew, in render order."""
    return re.findall(
        r'<div class="text-sm font-medium text-gray-900">([^<]+)</div>', html
    )


@pytest.fixture
async def three_access_states(db_session):
    """One user per access_status, plus explicit created_at so ordering is testable.

    ``created_at`` must be set by hand: its server default is ``func.now()``,
    which in Postgres is the *transaction* timestamp and therefore identical for
    every row the test inserts, leaving ``ORDER BY created_at DESC`` undefined.
    """
    made = {}
    for i, state in enumerate(("allowed", "pending", "denied")):
        made[state] = await factories.make_user(
            db_session,
            name=f"Access {state.title()}",
            email=f"{state}@example.org",
            access_status=state,
            created_at=datetime(2026, 1, 1 + i, 12, 0, tzinfo=UTC),
        )
    await db_session.flush()
    return made


# ===========================================================================
# The Access column
# ===========================================================================


async def test_each_access_status_renders_its_own_badge(
    client, admin, three_access_states
):
    """All three states render, each with its own colour.

    The three colours are asserted against each other in one test on purpose: a
    template that hard-coded a single badge class would satisfy any one of these
    assertions alone.
    """
    r = await client.get("/admin/users", headers=_auth(admin.id))
    assert r.status_code == 200
    assert ">Access<" in r.text, "the Access column header is missing"

    expected = {
        "allowed": ("bg-green-100 text-green-700", "Allowed"),
        "pending": ("bg-amber-100 text-amber-700", "Pending"),
        "denied": ("bg-red-100 text-red-700", "Denied"),
    }
    for state, (css, label) in expected.items():
        cell = _access_cell(_row_for(r.text, f"Access {state.title()}"))
        assert css in cell, f"{state} badge should be {css}, cell was: {cell}"
        assert label in cell, f"{state} badge should be labelled {label!r}"

    # And the colours really are distinct, so the loop above is not comparing a
    # cell against a class every cell happens to carry.
    assert len({css for css, _ in expected.values()}) == 3


async def test_a_non_allowed_row_is_not_hidden_from_the_page(
    client, admin, three_access_states
):
    """The fix must surface these rows, not filter them out — /admin/access-requests
    is driven by the very same rows, and hiding them here would leave the admin
    with no way to see that an unapproved account exists at all."""
    r = await client.get("/admin/users", headers=_auth(admin.id))
    names = set(_rendered_names(r.text))
    assert {"Access Allowed", "Access Pending", "Access Denied"} <= names


# ===========================================================================
# The access filter
# ===========================================================================


async def test_access_filter_narrows_to_a_single_status(
    client, admin, three_access_states
):
    """Each value shows only its own users; the unfiltered page shows all three.

    The default-shows-everything leg is the control: a filter that dropped every
    row would otherwise pass the three narrowing assertions.
    """
    unfiltered = await client.get("/admin/users", headers=_auth(admin.id))
    assert unfiltered.status_code == 200
    all_names = set(_rendered_names(unfiltered.text))
    assert {"Access Allowed", "Access Pending", "Access Denied"} <= all_names

    for state in ("allowed", "pending", "denied"):
        r = await client.get(
            f"/admin/users?access_filter={state}", headers=_auth(admin.id)
        )
        assert r.status_code == 200
        names = set(_rendered_names(r.text))
        assert f"Access {state.title()}" in names, f"{state} row was filtered out"
        for other in {"allowed", "pending", "denied"} - {state}:
            assert f"Access {other.title()}" not in names, (
                f"access_filter={state} also showed the {other} row"
            )


async def test_access_filter_preselects_itself_and_leaves_the_others_alone(
    client, admin, three_access_states
):
    """The <select> must round-trip its own value without disturbing the
    pre-existing filters' markup (they share one form and one applyFilter())."""
    r = await client.get("/admin/users?access_filter=denied", headers=_auth(admin.id))
    assert r.status_code == 200
    assert re.search(r'<option value="denied"[^>]*\bselected\b', r.text), (
        "access_filter=denied did not preselect its own option"
    )
    # The other two access options must NOT be selected.
    for other in ("allowed", "pending"):
        assert not re.search(rf'<option value="{other}"[^>]*\bselected\b', r.text), (
            f"{other} was also marked selected"
        )
    # The sibling filters are untouched and still default to All.
    assert 'id="status-filter"' in r.text and 'id="claimed-filter"' in r.text


async def test_access_filter_composes_with_the_claimed_filter(
    client, db_session, admin
):
    """The Python-side filters are applied in sequence, so a row must have to
    satisfy both. Three users cover every combination that matters."""
    await factories.make_user(
        db_session, name="Pending Claimed", email="pc@example.org",
        access_status="pending", claimed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    await factories.make_user(
        db_session, name="Pending Unclaimed", email="pu@example.org",
        access_status="pending", claimed_at=None,
    )
    await factories.make_user(
        db_session, name="Allowed Claimed", email="ac@example.org",
        access_status="allowed", claimed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    await db_session.flush()

    r = await client.get(
        "/admin/users?access_filter=pending&claimed_filter=claimed",
        headers=_auth(admin.id),
    )
    assert r.status_code == 200
    names = set(_rendered_names(r.text))
    assert "Pending Claimed" in names
    assert "Pending Unclaimed" not in names, "claimed_filter was ignored"
    assert "Allowed Claimed" not in names, "access_filter was ignored"


async def test_an_unknown_access_filter_value_matches_nothing_rather_than_500ing(
    client, admin, three_access_states
):
    """A hand-typed or stale query string must not raise; it simply matches no row."""
    r = await client.get(
        "/admin/users?access_filter=not-a-status", headers=_auth(admin.id)
    )
    assert r.status_code == 200
    assert _rendered_names(r.text) == []
    assert "No users found" in r.text
    # This is the only page state that renders the empty-state row, and its
    # colspan is the one place the column count is hard-coded in the template —
    # so it is also the only place a stale count is observable. Left behind by an
    # added column, "No users found" renders inside the Name column with ten
    # blank cells beside it; _access_cell's per-row count never sees this row.
    assert f'colspan="{_COLUMN_COUNT}"' in r.text


# ===========================================================================
# Ordering
# ===========================================================================


async def test_users_are_ordered_newest_first(client, db_session, three_access_states):
    """The query had no ORDER BY at all, so row order was whatever Postgres
    returned and could differ between two loads of the same page."""
    # The admin is created last here so it cannot coincidentally sort first.
    admin_user = await factories.make_user(
        db_session, is_admin=True, name="Ordering Admin", email="oadmin@example.org",
        created_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    await db_session.flush()

    r = await client.get("/admin/users", headers=_auth(admin_user.id))
    assert r.status_code == 200
    assert _rendered_names(r.text) == [
        "Access Denied",     # 2026-01-03
        "Access Pending",    # 2026-01-02
        "Access Allowed",    # 2026-01-01
        "Ordering Admin",    # 2025-06-01
    ]


# ===========================================================================
# The mid-session access gate (src/dependencies.py::get_current_user)
# ===========================================================================


@pytest.mark.parametrize("state", ["denied", "pending"])
async def test_a_non_allowed_user_is_bounced_mid_session(client, db_session, state):
    """A valid, correctly-signed session for a non-allowed user must not work.

    This is the revocation case: auth.py refuses to *mint* a session for these
    users, but nothing re-checked afterwards, so a user denied while logged in
    kept full access until their cookie expired.
    """
    user = await factories.make_user(
        db_session, is_admin=True, email=f"{state}admin@example.org",
        access_status=state,
    )
    await db_session.flush()

    r = await client.get("/admin/users", headers=_auth(user.id))
    assert r.status_code == 302, f"a {state} user reached the page"
    assert "/login" in r.headers["location"]
    # The session is cleared, not merely refused — otherwise every subsequent
    # request re-does this lookup and the user stays "logged in" in the UI.
    assert "copi-session=null" in r.headers.get("set-cookie", ""), (
        "the bounced session cookie was not cleared"
    )


async def test_an_allowed_user_still_passes_the_gate(client, db_session):
    """Control for the two bounces above: same route, same cookie machinery, the
    only difference being access_status.

    A non-admin allowed user gets 403 from get_admin_user — which is the proof
    that get_current_user let it through, since a bounce would be a 302.
    """
    allowed_admin = await factories.make_user(
        db_session, is_admin=True, email="okadmin@example.org", access_status="allowed"
    )
    allowed_plain = await factories.make_user(
        db_session, is_admin=False, email="okplain@example.org", access_status="allowed"
    )
    await db_session.flush()

    r = await client.get("/admin/users", headers=_auth(allowed_admin.id))
    assert r.status_code == 200

    r2 = await client.get("/admin/users", headers=_auth(allowed_plain.id))
    assert r2.status_code == 403, (
        "an allowed non-admin must reach the admin check (403), not the access "
        "bounce (302)"
    )


async def test_an_admin_can_still_impersonate_a_pending_user(client, db_session, admin):
    """The gate is checked on the *signed-in* identity, before impersonation.

    Admins deliberately view pending/denied accounts to debug them — that is the
    whole point of /admin/impersonate, and gating the impersonated user instead
    would break it. /settings is used because it renders the banner and, unlike
    /admin/*, does not require the impersonated user to be an admin.
    """
    pending = await factories.make_user(
        db_session, name="Pending Target", email="ptarget@example.org",
        access_status="pending",
    )
    await db_session.flush()

    r = await client.get("/settings", headers=_auth_as(admin.id, pending.id))
    assert r.status_code == 200, "impersonating a pending user was broken by the gate"
    assert "Viewing as Pending Target" in r.text, (
        "the impersonate cookie was inert, so this test proves nothing"
    )


async def test_a_non_allowed_admin_cannot_use_impersonation_to_get_back_in(
    client, db_session
):
    """The other side of the ordering: because the gate runs first, a *revoked*
    admin cannot launder their own session through the impersonate cookie.

    This is the ONLY test that fails if the access check in get_current_user is
    moved after the impersonation block — every other test here (and the
    impersonation tests in test_onboarding_flow.py) still passes with the two
    swapped, because they use an allowed admin.
    """
    revoked = await factories.make_user(
        db_session, is_admin=True, email="revoked@example.org", access_status="denied"
    )
    victim = await factories.make_user(
        db_session, name="Allowed Victim", email="victim2@example.org",
        access_status="allowed",
    )
    await db_session.flush()

    r = await client.get("/settings", headers=_auth_as(revoked.id, victim.id))
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
    # A 302 body is empty, so "the victim's name is not in r.text" would hold
    # however the gate behaved. What is worth asserting is that the revoked
    # session was destroyed rather than merely refused: copi-impersonate carries
    # no identity of its own, so the next request has nothing left to launder.
    assert "copi-session=null" in r.headers.get("set-cookie", ""), (
        "the revoked session cookie survived the bounce"
    )


# ===========================================================================
# POST /admin/impersonate — the create-on-miss branch
# ===========================================================================


async def test_impersonating_an_unknown_orcid_creates_an_allowed_user(
    client, db_session, admin, monkeypatch
):
    """An ORCID with no ``users`` row is created already approved, not pending.

    An admin typing an ORCID into the impersonate box is creating that record on
    purpose, so it is vetted by definition. Left on the model default
    ("pending") it becomes a phantom entry in /admin/access-requests that nobody
    asked for — and, being non-allowed, one that the mid-session gate above
    would bounce the moment anyone actually signed in as it.
    """
    calls = []

    async def _fake_fetch(orcid):
        calls.append(orcid)
        return {"name": "Fetched Newcomer", "institution": "Fetched Institute"}

    # admin.py binds `fetch_orcid_profile` into its own namespace at import time,
    # so the stub has to replace the name there, not on src.services.orcid.
    # Unstubbed this route makes a live ORCID call from a test.
    monkeypatch.setattr("src.routers.admin.fetch_orcid_profile", _fake_fetch)

    orcid = "0000-0002-1825-0097"
    r = await client.post(
        "/admin/impersonate", data={"orcid": f"  {orcid}  "}, headers=_auth(admin.id)
    )
    assert r.status_code == 302
    assert calls == [orcid], "the ORCID was not looked up, or not stripped first"

    row = (
        await db_session.execute(
            select(User.id, User.name, User.access_status, User.institution)
            .where(User.orcid == orcid)
        )
    ).mappings().one()
    assert row["name"] == "Fetched Newcomer"
    assert row["institution"] == "Fetched Institute"
    assert row["access_status"] == "allowed"
    # The cookie proves the route got past creation to actually impersonating the
    # new row, so the assertions above are not describing a half-finished write.
    assert f"copi-impersonate={row['id']}" in r.headers.get("set-cookie", "")
    # And the profile build was queued — the whole point of creating the row.
    job_types = (
        await db_session.execute(select(Job.type).where(Job.user_id == row["id"]))
    ).scalars().all()
    assert job_types == ["generate_profile"]

    # Not a phantom access request: absent from the pending filter, present in
    # the allowed one. The second half is the control — a row that rendered
    # nowhere at all would satisfy the first on its own.
    h = _auth(admin.id)
    pending_page = await client.get("/admin/users?access_filter=pending", headers=h)
    assert "Fetched Newcomer" not in _rendered_names(pending_page.text)
    allowed_page = await client.get("/admin/users?access_filter=allowed", headers=h)
    assert "Fetched Newcomer" in _rendered_names(allowed_page.text)
