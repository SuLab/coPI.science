def test_web_engine_pre_pings_and_recycles(monkeypatch):
    import src.database as database

    class _S:
        database_url = "postgresql+asyncpg://u:p@localhost:5499/none"

    monkeypatch.setattr(database, "get_settings", lambda: _S())
    engine = database._get_engine()  # creating an engine opens no connection
    try:
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 1800
    finally:
        engine.sync_engine.dispose()
