from sqlalchemy import create_engine, text

from backend import db as dbmod


def test_repair_marks_usdt_perp_groups_as_xt(monkeypatch):
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE groups (id INTEGER PRIMARY KEY, exchange VARCHAR, dependent_symbol VARCHAR)"))
        c.execute(text("INSERT INTO groups VALUES (1,'nobitex','ENJ/USDT:USDT'),(2,'nobitex','BTC/IRT'),(3,'xt','AXS/USDT:USDT')"))
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._repair_group_exchange()
    with eng.begin() as c:
        rows = dict(c.execute(text("SELECT id, exchange FROM groups")).fetchall())
    assert rows == {1: "xt", 2: "nobitex", 3: "xt"}


def test_init_group_gets_active_exchange():
    import inspect
    from backend.routers import init_router
    src = inspect.getsource(init_router._run_init_job)
    assert "exchange=exchange" in src and "_backbone_params(backbone_cfg, exchange)" in src
