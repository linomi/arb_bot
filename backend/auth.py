"""
Basic username/password authentication with a signed session cookie.

Credentials come from the environment:
  AUTH_USERNAME  (required to enable auth)
  AUTH_PASSWORD  (required to enable auth)
  SESSION_SECRET (optional; falls back to ENCRYPTION_KEY or a generated warning)

Cookie lasts 5 days, but it is tied to the current username/password and to this server
process: changing .env or restarting the service asks for the password again
(set AUTH_RELOGIN_ON_RESTART=0 to keep logins across restarts). If AUTH_USERNAME / AUTH_PASSWORD are unset, authentication
is disabled (open access) so existing deployments keep working until configured.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# 5 days in seconds
SESSION_MAX_AGE = 5 * 24 * 60 * 60
COOKIE_NAME = "arb_session"

_security = HTTPBasic(auto_error=False)

# Random per process: a restart invalidates every browser session (unless AUTH_RELOGIN_ON_RESTART=0).
_BOOT_NONCE = secrets.token_hex(8)


def _relogin_on_restart() -> bool:
    return (os.environ.get("AUTH_RELOGIN_ON_RESTART", "1").strip().lower() not in ("0", "false", "no", "off"))


def session_stamp() -> str:
    """Fingerprint stored in the session cookie. It changes when the username or password in
    .env changes (and on every restart by default), so old cookies stop working."""
    user = (os.environ.get("AUTH_USERNAME") or "").strip()
    pwd = (os.environ.get("AUTH_PASSWORD") or "").strip()
    msg = f"{user}\x00{pwd}\x00{_BOOT_NONCE if _relogin_on_restart() else ''}"
    return hmac.new(get_session_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def _mark_authenticated(request: Request, username: str) -> None:
    request.session["authenticated"] = True
    request.session["user"] = username
    request.session["stamp"] = session_stamp()


def auth_enabled() -> bool:
    user = (os.environ.get("AUTH_USERNAME") or "").strip()
    pwd = (os.environ.get("AUTH_PASSWORD") or "").strip()
    return bool(user and pwd)


def get_auth_credentials() -> tuple[str, str]:
    user = (os.environ.get("AUTH_USERNAME") or "").strip()
    pwd = (os.environ.get("AUTH_PASSWORD") or "").strip()
    if not user or not pwd:
        raise RuntimeError(
            "AUTH_USERNAME and AUTH_PASSWORD must both be set in .env to enable auth."
        )
    return user, pwd


def get_session_secret() -> str:
    secret = (
        (os.environ.get("SESSION_SECRET") or "").strip()
        or (os.environ.get("ENCRYPTION_KEY") or "").strip()
    )
    if not secret:
        # Fallback so the app still starts; log a warning in main.
        secret = secrets.token_urlsafe(32)
        os.environ["SESSION_SECRET"] = secret
    return secret


def _password_matches(plain: str, expected: str) -> bool:
    """Compare plain password to env value; if expected looks like bcrypt hash, use bcrypt."""
    exp = (expected or "").strip()
    if exp.startswith("$2a$") or exp.startswith("$2b$") or exp.startswith("$2y$"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain.encode("utf-8"), exp.encode("utf-8"))
        except Exception:
            return False
    return secrets.compare_digest(plain, exp)


def verify_password(username: str, password: str) -> bool:
    if not auth_enabled():
        return True
    expected_user, expected_pwd = get_auth_credentials()
    user_ok = secrets.compare_digest(username, expected_user)
    pwd_ok = _password_matches(password, expected_pwd)
    return user_ok and pwd_ok


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    session = request.session
    if session.get("authenticated") is True and session.get("user"):
        return secrets.compare_digest(str(session.get("stamp") or ""), session_stamp())
    return False


async def require_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(_security),
) -> None:
    """FastAPI dependency: allow if session cookie is valid or Basic Auth succeeds."""
    if not auth_enabled():
        return

    if is_authenticated(request):
        return

    # Fallback: HTTP Basic (useful for curl / scripts)
    if credentials is not None:
        if verify_password(credentials.username, credentials.password):
            # Establish session so subsequent browser requests work
            _mark_authenticated(request, credentials.username)
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Basic realm=\"Stat-Arb Bot\""},
    )


def login(request: Request, username: str, password: str) -> bool:
    if not verify_password(username, password):
        return False
    _mark_authenticated(request, username)
    return True


def logout(request: Request) -> None:
    request.session.clear()
