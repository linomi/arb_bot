"""Login / logout / session-status endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from backend import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginBody, request: Request):
    if not auth.auth_enabled():
        return {"ok": True, "auth_required": False, "message": "Auth is disabled"}
    if not auth.login(request, body.username.strip(), body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    return {
        "ok": True,
        "auth_required": True,
        "user": body.username.strip(),
        "max_age_days": 5,
    }


@router.post("/logout")
def logout(request: Request):
    auth.logout(request)
    return {"ok": True}


@router.get("/status")
def status_endpoint(request: Request):
    enabled = auth.auth_enabled()
    if not enabled:
        return {"authenticated": True, "auth_required": False, "user": None}
    ok = auth.is_authenticated(request)
    return {
        "authenticated": ok,
        "auth_required": True,
        "user": request.session.get("user") if ok else None,
        "max_age_days": 5,
    }
