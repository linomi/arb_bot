import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)

from backend.db import init_db, SessionLocal
from backend import config_service
from backend.engine.bot_engine import bot_engine
from backend.routers import config_router, groups_router, init_router, bot_router, credentials_router

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Stat-Arb Bot")

app.include_router(config_router.router)
app.include_router(groups_router.router)
app.include_router(init_router.router)
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
    # Background trading loop starts here and runs for the life of the
    # process, independent of any browser tab (see engine/bot_engine.py).
    bot_engine.start_background_loop()


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
