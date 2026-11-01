#!/usr/bin/env python3
"""Import groups found by scripts/run_init.py into this bot's database (dry run unless --apply).

    python scripts/import_groups.py groups.json            # show what would be created
    python scripts/import_groups.py groups.json --apply    # create them as candidates
    python scripts/import_groups.py groups.json --apply --activate

Groups are saved exactly like the Initialization tab saves them (generated name, source, exchange, the
settings they were found with as params_snapshot, backtest metrics). A group with the same symbols and
dependent on the same exchange is skipped. Existing groups are never changed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", help="JSON written by scripts/run_init.py")
    ap.add_argument("--apply", action="store_true", help="write the groups (default: dry run)")
    ap.add_argument("--activate", action="store_true", help="create them active instead of candidate")
    args = ap.parse_args()

    from backend.db import SessionLocal
    from backend.group_names import generate_group_name
    from backend.models import Group

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    exchange = data.get("exchange", "xt")
    db = SessionLocal()
    try:
        existing = {(g.exchange, g.dependent_symbol, tuple(sorted(g.symbols or []))): g
                    for g in db.query(Group).all()}
        used = {g.name for g in existing.values()}
        status = "active" if args.activate else "candidate"
        created = 0
        for cand in data.get("groups", []):
            key = (exchange, cand["dependent_symbol"], tuple(sorted(cand["symbols"])))
            if key in existing:
                print(f"skip (exists as #{existing[key].id} {existing[key].name}): {cand['symbols']}")
                continue
            name = generate_group_name(used=used)
            used.add(name)
            print(f"{'create' if args.apply else 'would create'} {name} [{status}] "
                  f"dep={cand['dependent_symbol']} symbols={cand['symbols']}")
            if args.apply:
                g = Group(name=name, symbols=cand["symbols"], dependent_symbol=cand["dependent_symbol"],
                          source=cand.get("source") or "random", sector=cand.get("sector"), status=status,
                          exchange=exchange, params_snapshot=data.get("backbone"),
                          backtest_metrics=cand.get("backtest_metrics"))
                db.add(g)
                db.flush()
                existing[key] = g
            created += 1
        if args.apply:
            db.commit()
            print(f"created {created} group(s)")
        else:
            print(f"dry run: {created} group(s) would be created; re-run with --apply")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
