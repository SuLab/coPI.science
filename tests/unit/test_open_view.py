"""The open view's gate and the read-only viewer (src/web/open_view.py)."""
import pytest
from starlette.requests import Request

from src.config import Settings, get_settings
from src.services.web_permissions import capabilities_for
from src.web.navigation import navigation_for
from src.web.open_view import (
    OPEN_VIEWER_ID,
    is_open_viewer,
    open_view_applies,
    open_viewer,
)

_DETAIL = "/workspace/landscape/3f2a9c1e-8b4d-4e2a-9c7f-1a2b3c4d5e6f"


def _request(path, method="GET", session=None):
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"", "headers": [],
        "scheme": "http", "server": ("testserver", 80), "root_path": "",
        "session": {} if session is None else session,
    })


@pytest.fixture
def open_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "landscape_open_view", True)


def test_off_by_default():
    assert Settings.model_fields["landscape_open_view"].default is False


def test_off_serves_nobody():
    assert get_settings().landscape_open_view is False
    assert not open_view_applies(_request("/workspace/landscape"))


@pytest.mark.parametrize("path", ["/workspace/landscape", _DETAIL])
def test_landscape_gets_are_open(open_on, path):
    assert open_view_applies(_request(path))


@pytest.mark.parametrize("path", [
    "/workspace/landscape/pipeline",
    "/workspace/landscape/not-a-uuid",
    "/workspace/landscape/",
    _DETAIL + "/extra",
    "/workspace/pis",
    "/workspace",
    "/landscape/pause",
    "/reviews/landscape/3f2a9c1e-8b4d-4e2a-9c7f-1a2b3c4d5e6f/decision",
    "/admin/users",
    "/settings",
])
def test_other_paths_are_not(open_on, path):
    assert not open_view_applies(_request(path))


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_only_reads(open_on, method):
    assert not open_view_applies(_request("/workspace/landscape", method=method))
    assert not open_view_applies(_request(_DETAIL, method=method))


def test_a_signed_in_session_keeps_its_own_user(open_on):
    assert not open_view_applies(_request("/workspace/landscape", session={"user_id": "x"}))


def test_viewer_is_read_only_staff_in_memory():
    viewer = open_viewer()
    assert is_open_viewer(viewer) and viewer.id == OPEN_VIEWER_ID
    assert viewer.user_role == "manager" and not viewer.is_admin
    caps = capabilities_for(viewer)
    assert caps.read_only and caps.staff and caps.research
    assert not (caps.admin or caps.pi_surfaces or caps.verify_pi_email or caps.assign_reviews
                or caps.manage_suggestions or caps.chat)
    assert open_viewer() is not viewer


def test_ordinary_users_are_not_read_only():
    class Manager:
        user_role = "manager"
    assert not is_open_viewer(Manager())
    assert not capabilities_for(Manager()).read_only
    assert capabilities_for(Manager()).assign_reviews


def test_viewer_navigation_is_landscape_alone():
    items = navigation_for(_FakeRequest(), capabilities_for(open_viewer()))
    assert [i["section"] for i in items] == ["landscape"]


class _FakeRequest:
    """``page_url`` reads ``request.app.url_path_for`` only."""

    class app:  # noqa: N801 - mimics the attribute ``page_url`` reads
        @staticmethod
        def url_path_for(name, **_):
            return f"/{name}"
