#!/usr/bin/env python3
"""Apply the recommended strategy settings to the bot's database (dry run unless --apply).

    python scripts/apply_recommended_settings.py            # show what would change
    python scripts/apply_recommended_settings.py --apply    # write it (validated like a UI save)

Why these values: on 1-minute bars a 1-sigma residual move was ~0.13% of gross, against ~0.22% round-trip
costs, and stops at |z| 8 lost ~4 winners each. Fifteen-minute bars, an out-of-sample z, a tighter stop, an
expected-value gate and small, out-of-sample-checked groups target that. fee_rate, leverage and trade size
are left alone: set fee_rate to your real XT tier yourself.

Groups chosen under the old settings were selected on 1-minute data: re-run Initialization afterwards.
The bot reads config every cycle, so no restart is needed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RECOMMENDED = {
    "backbone": {
        "sampling_time": 900,
        "window_size": 200,
        "stationarity_method": "engle_granger",
        "half_life_max_fraction": 0.25,
        "z_entry": 2.5,
        "z_close": 0.5,
        "z_stop_loss": 4.5,
        "z_entry_max": 3.5,
        "target_profit_rate": 0.003,
        "entry_retry_cooldown_sec": 600,
        "max_open_trades": 3,
        "max_holding_hours": 48,
        "max_holding_halflives": 3.0,
        "zscore_method": "oos",
        "ev_gate": True,
        "ev_win_rate": 0.65,
        "ev_stop_overshoot": 0.5,
        "jump_max_sigma": 4.0,
        "jump_bars": 2,
        "vol_regime_max": 2.0,
        "vol_regime_bars": 20,
    },
    "init": {
        "max_group_size": 3,
        "num_candidate_groups": 500,
        "top_n_liquid_symbols": 60,
        "backtest_days": 30,
        "require_oos_positive": True,
        "keep_top_n": 5,
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()

    from backend import config_service
    from backend.db import SessionLocal

    db = SessionLocal()
    try:
        changed = 0
        for section, values in RECOMMENDED.items():
            current = config_service.get_section(db, section)
            patch = {k: v for k, v in values.items() if current.get(k) != v}
            for k, v in patch.items():
                print(f"{section}.{k}: {current.get(k)!r} -> {v!r}")
            changed += len(patch)
            if patch and args.apply:
                config_service.update_section(db, section, patch)
        if not changed:
            print("already up to date")
        elif args.apply:
            print(f"applied {changed} change(s); now re-run Initialization to pick groups on the new settings")
        else:
            print(f"dry run: {changed} change(s); re-run with --apply to write them")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
