"""pending_profile has no writer — admin_users must not branch on it."""

import inspect

from src.routers import admin


def test_admin_users_does_not_read_pending_profile():
    source = inspect.getsource(admin.admin_users)
    assert "pending_profile" not in source
