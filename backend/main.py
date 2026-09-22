import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

# Always load .env from project root (not CWD — systemd may start elsewhere).
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")
load_dotenv()  # also allow CWD override

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
)
from backend import security

BASE_DIR = _BASE_DIR
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Stat-Arb Bot")

app.include_router(config_router.router)
app.include_router(groups_router.router)
app.include_router(manual_router.router)
app.include_router(init_router.router)
app.include_router(symbols_router.router)
app.include_router(bot_router.router)
app.include_router(credentials_router.router)


@app.on_event("startup")
async def on_startup():
    init_db()
    db = SessionLocal()
    try:
        config_service.seed_defaults_if_missing(db)
    finally:
        db.close()

    # Surface encryption readiness early (credentials save needs this).
    try:
        security._require_fernet()
        log.info("ENCRYPTION_KEY OK — credentials can be saved.")
    except security.EncryptionNotConfigured as e:
        log.warning(
            "ENCRYPTION_KEY not configured — saving API credentials will fail. %s",
            e,
        )

    bot_engine.start_background_loop()


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
