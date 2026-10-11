import importlib

from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware


def _app(auth_mod):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="s" * 32, session_cookie="arb_session")

    @app.post("/login")
    def login(request: __import__("starlette.requests", fromlist=["Request"]).Request):
        return {"ok": auth_mod.login(request, "admin", "pw1")}

    @app.get("/me", dependencies=[Depends(auth_mod.require_auth)])
    def me():
        return {"ok": True}

    return app


def test_session_dies_on_password_change_and_restart(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "pw1")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.delenv("AUTH_RELOGIN_ON_RESTART", raising=False)
    from backend import auth
    importlib.reload(auth)
    c = TestClient(_app(auth))
    assert c.get("/me").status_code == 401
    assert c.post("/login").json()["ok"] is True
    assert c.get("/me").status_code == 200

    # password changed in .env + restart -> old cookie rejected
    monkeypatch.setenv("AUTH_PASSWORD", "pw2")
    assert c.get("/me").status_code == 401
    monkeypatch.setenv("AUTH_PASSWORD", "pw1")
    assert c.get("/me").status_code == 200

    # simulated restart: new boot nonce -> relogin required
    auth._BOOT_NONCE = "other-process"
    assert c.get("/me").status_code == 401

    # opt out: keep sessions across restarts
    c2 = TestClient(_app(auth))
    assert c2.post("/login").json()["ok"] is True
    monkeypatch.setenv("AUTH_RELOGIN_ON_RESTART", "0")
    auth._BOOT_NONCE = "yet-another"
    assert c2.post("/login").json()["ok"] is True
    auth._BOOT_NONCE = "again-different"
    assert c2.get("/me").status_code == 200
