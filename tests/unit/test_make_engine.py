"""Each engine keeps exactly today's arguments (spec §9.6). The six original
sites carry five distinct argument sets (the supervisor and the agent's roster
read differ from the agent's main engine); the heartbeat and advisory-lock
engines are two more roles."""
import ast
from pathlib import Path

from sqlalchemy.pool import NullPool

from src import database

REPO = Path(__file__).resolve().parents[2]


def test_per_role_arguments_are_todays(monkeypatch):
    s = type("S", (), {"db_pool_size": 17, "db_max_overflow": 23, "database_url": "postgresql+asyncpg://x/y"})()
    monkeypatch.setattr(database, "get_settings", lambda: s)
    assert database.engine_kwargs("web") == {
        "echo": False, "pool_size": 5, "max_overflow": 10, "pool_pre_ping": True, "pool_recycle": 1800}
    assert database.engine_kwargs("worker") == {"echo": False}
    assert database.engine_kwargs("agent") == {"pool_size": 17, "max_overflow": 23, "pool_pre_ping": True}
    assert database.engine_kwargs("supervisor") == {"pool_pre_ping": True}
    assert database.engine_kwargs("agent_roster") == {}
    assert database.engine_kwargs("cli") == {}
    assert database.engine_kwargs("heartbeat") == {"pool_size": 1, "max_overflow": 0, "pool_pre_ping": True}
    assert database.engine_kwargs("advisory_lock") == {"poolclass": NullPool, "isolation_level": "AUTOCOMMIT"}
    assert database.engine_kwargs("provision_lock") == {"poolclass": NullPool}


def test_no_other_create_async_engine_in_src():
    hits = []
    for p in (REPO / "src").rglob("*.py"):
        if "__pycache__" in p.parts or p == REPO / "src/database.py":
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("create_async_engine", "_cae"):
                hits.append(str(p.relative_to(REPO)))
    assert hits == []
