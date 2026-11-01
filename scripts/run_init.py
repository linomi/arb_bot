#!/usr/bin/env python3
"""Run the group initializer headless (e.g. on a many-core PC) and write the kept groups to a JSON file.

    python scripts/run_init.py --config server_config.json --out groups.json [--workers N] [--method random|sector]

When XT is unreachable from the PC, download on a host that can reach it and backtest on the PC:

    python scripts/run_init.py --config server_config.json --save-data prices.json --fetch-only   # on the server
    python scripts/run_init.py --config server_config.json --data prices.json --out groups.json  # on the PC

``--config`` is a JSON file {"backbone": {...}, "init": {...}} -- the server's settings, so the search uses
exactly what the server trades with. It runs the same code as the Initialization tab (XT public market
data, same backtest, same ranking) but needs no database. Import the result on the server with
scripts/import_groups.py.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _download(md, symbols: list[str], resolution: str, bars: int, concurrency: int = 8) -> dict:
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(sym):
        nonlocal done
        async with sem:
            data = await md.get_ohlc(sym, resolution, bars)
        done += 1
        print(f"\rdownloading OHLC {done}/{len(symbols)}", end="", flush=True)
        return sym, data

    got = await asyncio.gather(*[one(s) for s in symbols], return_exceptions=True)
    print()
    out = {}
    for item in got:
        if isinstance(item, Exception):
            print(f"  OHLC failed: {item}")
            continue
        out[item[0]] = item[1]
    return out


async def _fetch(init_cfg: dict, backbone_cfg: dict) -> tuple[list[str], dict]:
    from backend.exchange.xt import XTClient
    from backend.utils import seconds_to_resolution

    md = XTClient(api_key=None, api_secret=None)  # public endpoints only
    try:
        symbols = await md.get_liquid_symbols(top_n=int(init_cfg["top_n_liquid_symbols"]),
                                              quote=init_cfg.get("quote_currency", "USDT"))
        st = int(backbone_cfg["sampling_time"])
        bars = max(int(backbone_cfg["window_size"]) + 10, int(init_cfg["backtest_days"] * 86400 / max(1, st)))
        resolution = seconds_to_resolution(st)
        print(f"{len(symbols)} liquid symbols, {bars} bars of {resolution}m")
        return symbols, await _download(md, symbols, resolution, bars)
    finally:
        await md.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help='JSON {"backbone": {...}, "init": {...}}')
    ap.add_argument("--out", help="where to write the kept groups (JSON)")
    ap.add_argument("--save-data", help="also save the downloaded symbols + OHLC to this file")
    ap.add_argument("--data", help="use OHLC saved with --save-data instead of downloading")
    ap.add_argument("--fetch-only", action="store_true", help="download (and --save-data) only, no backtests")
    ap.add_argument("--workers", type=int, default=None, help="worker processes (default: physical cores)")
    ap.add_argument("--method", choices=("random", "sector"), default=None, help="default: the config's init.method")
    args = ap.parse_args()

    from backend.engine.init_methods import random_init, sector_init, _default_workers
    from backend.routers.init_router import _backbone_params

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    backbone_cfg, init_cfg = cfg["backbone"], cfg["init"]
    method = args.method or init_cfg.get("method", "random")

    if not args.fetch_only and not args.out:
        ap.error("--out is required unless --fetch-only")
    t0 = time.perf_counter()
    if args.data:
        saved = json.loads(Path(args.data).read_text(encoding="utf-8"))
        symbols, ohlc = saved["symbols"], saved["ohlc"]
        print(f"{len(ohlc)} symbols from {args.data} (downloaded {saved.get('downloaded_at')})")
    else:
        symbols, ohlc = asyncio.run(_fetch(init_cfg, backbone_cfg))
    if args.save_data:
        from backend.exchange import contract_sizes
        Path(args.save_data).write_text(json.dumps({
            "downloaded_at": dt.datetime.utcnow().isoformat() + "Z", "symbols": symbols, "ohlc": ohlc,
            "contract_sizes": contract_sizes.lookup_many(symbols)}), encoding="utf-8")
        print(f"saved OHLC of {len(ohlc)} symbols -> {args.save_data}")
    if args.data and saved.get("contract_sizes"):
        from backend.exchange import contract_sizes
        contract_sizes.update(saved["contract_sizes"])  # backtests round to the same whole contracts as live
    if args.fetch_only:
        return 0
    if len(ohlc) < 2:
        print("OHLC download failed for almost all symbols")
        return 1

    params = _backbone_params(backbone_cfg, "xt", init_cfg)
    workers = args.workers or _default_workers()
    t1 = time.perf_counter()

    def progress(cur, total, syms):
        el = time.perf_counter() - t1
        eta = el / cur * (total - cur) if cur else 0
        print(f"\rbacktested {cur}/{total}  elapsed {el:5.0f}s  eta {eta:5.0f}s", end="", flush=True)

    common = dict(ohlc_by_symbol=ohlc, min_group_size=int(init_cfg["min_group_size"]),
                  max_group_size=int(init_cfg["max_group_size"]), keep_top_n=int(init_cfg["keep_top_n"]),
                  backbone_params=params, progress_cb=progress, max_workers=workers)
    print(f"{method} search with {workers} workers")
    if method == "sector":
        ranked = sector_init(max_combos_per_sector=int(init_cfg.get("max_combos_per_sector", 200)), **common)
    else:
        ranked = random_init(num_candidate_groups=int(init_cfg["num_candidate_groups"]), **common)
    print()

    out = {
        "created_at": dt.datetime.utcnow().isoformat() + "Z",
        "method": method,
        "liquid_symbols_considered": len(symbols),
        "exchange": "xt",
        "backbone": backbone_cfg,
        "init": init_cfg,
        "groups": [{k: g.get(k) for k in ("symbols", "dependent_symbol", "source", "sector", "score",
                                           "backtest_metrics", "oos_metrics", "oos_score")}
                   for g in ranked],
    }
    Path(args.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"kept {len(ranked)} group(s) in {time.perf_counter() - t0:.0f}s -> {args.out}")
    for g in ranked:
        m, o = g["backtest_metrics"], g.get("oos_metrics") or {}
        print(f"  {g['dependent_symbol']:>18} ~ {', '.join(s for s in g['symbols'] if s != g['dependent_symbol'])}: "
              f"IS trades={m['trade_count']} win={m['win_rate']:.0%} pnl={m['total_pnl']:.4g} | "
              f"OOS trades={o.get('trade_count')} pnl={o.get('total_pnl')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
