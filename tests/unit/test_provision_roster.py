"""scripts/provision_slack_bots.py: roster freshness and per-agent scopes.

The script is loaded by path. ``main()`` binds an HTTP server, calls Slack and
blocks on OAuth callbacks, so only its pure helpers are exercised here.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_slack_bots.py"


def _load():
    spec = importlib.util.spec_from_file_location("copi_provision_slack_bots", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


psb = _load()

_ROSTER = [
    {"id": "alpha", "name": "AlphaBot", "pi": "A", "status": "active", "has_token": False},
    {"id": "beta", "name": "BetaBot", "pi": "B", "status": "pending", "has_token": False},
    {"id": "gamma", "name": "GammaBot", "pi": "G", "status": "inactive", "has_token": False},
    {"id": "delta", "name": "DeltaBot", "pi": "D", "status": "suspended", "has_token": True},
]


@pytest.fixture
def roster_path(tmp_path, monkeypatch):
    path = tmp_path / "agent_roster.json"
    monkeypatch.setattr(psb, "ROSTER_PATH", path)
    return path


def _write(path: Path, age_s: float) -> None:
    path.write_text(json.dumps(_ROSTER))
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def test_stale_roster_is_refused(roster_path):
    _write(roster_path, age_s=2 * 3600)
    with pytest.raises(RuntimeError) as exc:
        psb.load_roster()
    assert "export_agent_roster.py" in str(exc.value)
    assert "docker-compose.prod.yml" in str(exc.value)


def test_fresh_roster_loads_and_filters(roster_path):
    _write(roster_path, age_s=60)
    assert [r["id"] for r in psb.load_roster()] == ["alpha", "beta"]


def test_allow_stale_bypasses_the_age_check(roster_path):
    _write(roster_path, age_s=2 * 3600)
    assert [r["id"] for r in psb.load_roster(None)] == ["alpha", "beta"]


def test_missing_roster_message_names_the_prod_command(roster_path):
    with pytest.raises(RuntimeError) as exc:
        psb.load_roster()
    msg = str(exc.value)
    assert "docker compose -f docker-compose.prod.yml run" in msg
    assert "blackbird-app python scripts/export_agent_roster.py" in msg
    assert '-v "$PWD/data:/app/data"' in msg
    assert "exec app" not in msg


def test_scopes_for_extends_only_the_named_agent():
    base = ["channels:read", "chat:write", "users:read"]
    add = {"su": {"groups:write"}}
    omit = {"wiseman": {"chat:write"}}

    assert psb.scopes_for("su", {}, add, base=base) == base + ["groups:write"]
    assert psb.scopes_for("SU", {}, add, base=base) == base + ["groups:write"]
    assert psb.scopes_for("other", omit, add, base=base) is None
    assert psb.scopes_for("wiseman", omit, add, base=base) == ["channels:read", "users:read"]
    # An added scope already in the base set is not duplicated.
    assert psb.scopes_for("su", {}, {"su": {"chat:write"}}, base=base) == base
    # The base list itself is never mutated.
    assert base == ["channels:read", "chat:write", "users:read"]
