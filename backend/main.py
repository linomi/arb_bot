import logging
from pathlib import Path

from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")
load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

from backend.db import init_db, SessionLocal
from backend import config_service
from backend.engine.bot_engine import bot_engine
from backend.routers import (
    config_router,
    groups_router,
    init_router,
    bot_router,
    credentials_router,
    manual_router,
    symbols_router,
    debug_router,
    auth_router,
)
from backend import security
from backend import auth
from backend.routers.groups_router import assign_codenames

BASE_DIR = _BASE_DIR
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Stat-Arb Bot")

# Session cookie: 5 days, signed with SESSION_SECRET / ENCRYPTION_KEY
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_session_secret(),
    session_cookie=auth.COOKIE_NAME,
    max_age=auth.SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,
)

# Auth is optional: if AUTH_USERNAME+AUTH_PASSWORD are set, protect API routes.
_auth_dep = [Depends(auth.require_auth)] if auth.auth_enabled() else []

app.include_router(auth_router.router)  # login/logout/status always public

app.include_router(config_router.router, dependencies=_auth_dep)
app.include_router(groups_router.router, dependencies=_auth_dep)
app.include_router(manual_router.router, dependencies=_auth_dep)
app.include_router(init_router.router, dependencies=_auth_dep)
app.include_router(symbols_router.router, dependencies=_auth_dep)
app.include_router(bot_router.router, dependencies=_auth_dep)
app.include_router(credentials_router.router, dependencies=_auth_dep)
app.include_router(debug_router.router, dependencies=_auth_dep)


@app.on_event("startup")
async def on_startup():
    init_db()
    db = SessionLocal()
    try:
        config_service.seed_defaults_if_missing(db)
        # One-time style fix: old random-BTCIRT-… names → silent_spread style
        try:
            changes = assign_codenames(db, force_all=False)
            if changes:
                log.info("Renamed %d legacy group name(s) to codenames", len(changes))
                for c in changes[:12]:
                    log.info("  group %s: %s → %s", c["id"], c["old"], c["new"])
        except Exception:
            log.exception("assign_codenames failed (non-fatal)")
    finally:
        db.close()

    try:
        security._require_fernet()
        log.info("ENCRYPTION_KEY OK — credentials can be saved.")
    except security.EncryptionNotConfigured as e:
        log.warning(
            "ENCRYPTION_KEY not configured — saving API credentials will fail. %s",
            e,
        )

    if auth.auth_enabled():
        log.info(
            "Basic auth ENABLED (cookie lasts 5 days). "
            "Set AUTH_USERNAME / AUTH_PASSWORD in .env."
        )
    else:
        log.warning(
            "Basic auth DISABLED — set AUTH_USERNAME and AUTH_PASSWORD in .env "
            "to protect the UI and API."
        )

    bot_engine.start_background_loop()


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
