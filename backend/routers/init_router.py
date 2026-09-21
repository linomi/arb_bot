import asyncio
import traceback
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from backend.db import get_db, SessionLocal
from backend import config_service
from backend.models import Group
from backend.schemas import InitRunRequest
from backend.exchange import factory
from backend.utils import seconds_to_resolution
from backend.engine.init_methods import random_init, sector_init
from backend import init_progress

log = logging.getLogger("init_router")

router = APIRouter(prefix="/api/init", tags=["init"])

# Concurrent OHLC downloads (Nobitex rate limits apply; keep modest).
_OHLC_CONCURRENCY = 8


@router.get("/progress")
def get_init_progress():
    return init_progress.get_progress()


@router.post("/run")
async def run_init(body: InitRunRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not init_progress.start("Queued…"):
        raise HTTPException(409, "Initialization is already running. Wait for it to finish.")

    init_cfg = dict(config_service.get_section(db, "init"))
    backbone_cfg = dict(config_service.get_section(db, "backbone"))
    method = body.method or init_cfg.get("method", "random")
    activate = bool(body.activate)

    background_tasks.add_task(_run_init_job, method, activate, init_cfg, backbone_cfg)
    return {"status": "started", "method": method}


async def _run_init_job(method: str, activate: bool, init_cfg: dict, backbone_cfg: dict):
    db = SessionLocal()
    md_client = None
    try:
        init_progress.update(phase="fetch_symbols", message="Fetching liquid markets…", percent=2)
        md_client = factory.build_market_data_client(db)

        symbols = await md_client.get_liquid_symbols(
            top_n=int(init_cfg["top_n_liquid_symbols"]),
            quote=init_cfg.get("quote_currency", "IRT"),
        )
        if len(symbols) < 2:
            raise RuntimeError(
                "Fewer than 2 liquid symbols returned by the exchange — check base_url/network."
            )

        resolution = seconds_to_resolution(int(backbone_cfg["sampling_time"]))
        bars = max(
            int(backbone_cfg["window_size"]) + 10,
            int(init_cfg["backtest_days"] * 86400 / max(1, int(backbone_cfg["sampling_time"]))),
        )

        n_sym = len(symbols)
        init_progress.update(
            phase="fetch_ohlc",
            message=f"Downloading OHLC for {n_sym} symbols ({bars} bars, {resolution})…",
            current=0,
            total=n_sym,
            percent=5,
        )

        # Parallel OHLC fetch with a concurrency cap
        sem = asyncio.Semaphore(_OHLC_CONCURRENCY)
        done_count = 0
        lock = asyncio.Lock()

        async def _fetch_one(sym: str):
            nonlocal done_count
            async with sem:
                data = await md_client.get_ohlc(sym, resolution, bars)
            async with lock:
                done_count += 1
                i = done_count
                pct = 5 + 35 * i / n_sym
                init_progress.update(
                    phase="fetch_ohlc",
                    message=f"Downloading OHLC {i}/{n_sym}: {sym}",
                    current=i,
                    total=n_sym,
                    percent=pct,
                )
            return sym, data

        pairs = await asyncio.gather(*[_fetch_one(s) for s in symbols], return_exceptions=True)
        ohlc_by_symbol = {}
        for item in pairs:
            if isinstance(item, Exception):
                log.warning("OHLC fetch failed: %s", item)
                continue
            sym, data = item
            ohlc_by_symbol[sym] = data

        if len(ohlc_by_symbol) < 2:
            raise RuntimeError("OHLC download failed for almost all symbols.")

        def backtest_progress(current: int, total: int, symbols_tuple):
            pct = 40 + 50 * current / max(1, total)
            label = "-".join(symbols_tuple[:4])
            if len(symbols_tuple) > 4:
                label += "…"
            init_progress.update(
                phase="backtest",
                message=f"Backtesting group {current}/{total}: {label}",
                current=current,
                total=total,
                percent=pct,
            )

        init_progress.update(
            phase="backtest",
            message="Building candidate groups and running backtests (parallel)…",
            percent=40,
        )

        backbone_params = _backbone_params(backbone_cfg)
        loop = asyncio.get_event_loop()

        def _do_backtest():
            if method == "sector":
                return sector_init(
                    ohlc_by_symbol=ohlc_by_symbol,
                    min_group_size=int(init_cfg["min_group_size"]),
                    max_group_size=int(init_cfg["max_group_size"]),
                    keep_top_n=int(init_cfg["keep_top_n"]),
                    backbone_params=backbone_params,
                    max_combos_per_sector=int(init_cfg.get("max_combos_per_sector", 200)),
                    progress_cb=backtest_progress,
                )
            return random_init(
                ohlc_by_symbol=ohlc_by_symbol,
                min_group_size=int(init_cfg["min_group_size"]),
                max_group_size=int(init_cfg["max_group_size"]),
                num_candidate_groups=int(init_cfg["num_candidate_groups"]),
                keep_top_n=int(init_cfg["keep_top_n"]),
                backbone_params=backbone_params,
                progress_cb=backtest_progress,
            )

        # Outer executor keeps the asyncio loop free for progress polling;
        # inner ThreadPool in init_methods parallelizes the groups.
        with ThreadPoolExecutor(max_workers=1) as pool:
            ranked = await loop.run_in_executor(pool, _do_backtest)

        init_progress.update(
            phase="persist",
            message=f"Saving top {len(ranked)} groups…",
            percent=92,
        )

        created_ids = []
        for cand in ranked:
            name = f"{method}-{'-'.join(cand['symbols'])}"[:80]
            g = Group(
                name=name,
                symbols=cand["symbols"],
                dependent_symbol=cand["dependent_symbol"],
                source=cand["source"],
                sector=cand.get("sector"),
                status="active" if activate else "candidate",
                params_snapshot=backbone_cfg,
                backtest_metrics=cand["backtest_metrics"],
            )
            db.add(g)
            db.flush()
            created_ids.append(g.id)
        db.commit()

        result = {
            "method": method,
            "liquid_symbols_considered": len(symbols),
            "candidates_evaluated": len(ranked),
            "groups_created": created_ids,
        }
        init_progress.finish(result)
        log.info("init finished: %s", result)

    except Exception as e:
        log.exception("init failed")
        try:
            db.rollback()
        except Exception:
            pass
        init_progress.fail(str(e) or traceback.format_exc())
    finally:
        if md_client is not None:
            try:
                await md_client.aclose()
            except Exception:
                pass
        db.close()


def _backbone_params(backbone_cfg: dict) -> dict:
    return {
        "window_size": int(backbone_cfg["window_size"]),
        "adf_alpha": float(backbone_cfg["adf_alpha"]),
        "kpss_alpha": float(backbone_cfg["kpss_alpha"]),
        "z_entry": float(backbone_cfg["z_entry"]),
        "z_close": float(backbone_cfg["z_close"]),
        "z_stop_loss": float(backbone_cfg["z_stop_loss"]),
        "transaction_fee_rate": float(backbone_cfg["fee_rate"]) + float(backbone_cfg["slippage_rate"]),
    }
