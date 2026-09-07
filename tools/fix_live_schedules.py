#!/usr/bin/env python
"""Repair a live ExpertInstance's ENTRY cadence to the one its genome was scored on.

    BA2_LIVE_DB=... python tools/fix_live_schedules.py           # dry run (default)
    BA2_LIVE_DB=... python tools/fix_live_schedules.py --apply

WHY. The GA searches the entry weekday per individual (``schedule:<day>`` genes) and the
decoded days REPLACE the run-level cadence for that individual. ``_derive_export_payload``
exported the RUN-LEVEL override instead, so every deploy carried the grid's launch cadence
-- Monday-only for all of goal2020 -- no matter what the genome chose. Found 2026-09-07 on
five of six prod instances; three of them (8, 9, 11) had genomes that never trade Monday at
all, which is the only day live was firing.

The export/import pair is fixed (schedule_override_from_genes), but a re-import only writes
schedules for a NEWLY CREATED instance and uses setdefault, so it cannot repair an existing
instance that already holds a wrong value. This tool does exactly that one repair, through
the expert's own ``save_settings`` -- the same path a settings save from the UI takes, so
value typing follows get_settings_definitions -- and touches nothing else.

``execution_schedule_open_positions`` is deliberately NOT changed: exits must be evaluated
every weekday regardless of the entry cadence, which is what live already has.

Run POST /api/reload afterwards (or restart) so JobManager rebuilds the cron triggers.
"""
import argparse
import json
import os
import sys

REPO = os.environ.get("BA2_REPO", r"C:\Users\basti\Documents\dev\BA2TradePlatform")
for p in (REPO, os.path.join(REPO, "packages", "experts")):
    if p not in sys.path:
        sys.path.insert(0, p)

LIVE_DB = os.environ.get("BA2_LIVE_DB", os.path.expanduser(r"~\Documents\ba2\trade\db.sqlite"))
TEST_DB = os.environ.get(
    "BA2_TEST_DB", os.path.expanduser(r"~\Documents\ba2\test\dl_forecasting.db"))

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: (live instance id, source backtest id) for the six goal2020 deploys.
DEPLOYED = [(7, 1107), (8, 1298), (9, 1363), (10, 1173), (11, 1088), (12, 1330)]


def _genome_days(sp):
    """Mirrors decode_params: genes to a full day map, all-off repaired to the first weekday."""
    by_day = {k[len("schedule:"):]: bool(v) for k, v in sp.items()
              if isinstance(k, str) and k.startswith("schedule:")}
    if not by_day:
        return None
    days = {d: by_day.get(d, False) for d in DAYS}
    if not any(days.values()):
        days[DAYS[0]] = True
    return days


def _trading(days):
    return [d for d in DAYS[:5] if days.get(d)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    ns = ap.parse_args()

    import sqlite3
    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(LIVE_DB)
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertInstance

    test = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True)
    print(f"LIVE_DB = {LIVE_DB}\nTEST_DB = {TEST_DB}\nmode    = "
          f"{'APPLY' if ns.apply else 'DRY RUN'}\n")

    changed = 0
    for inst_id, bt_id in DEPLOYED:
        inst = get_instance(ExpertInstance, inst_id)
        if inst is None:
            print(f"  !! instance {inst_id} not found")
            continue
        row = test.execute("select strategy_params from backtests where id=?", (bt_id,)).fetchone()
        if row is None:
            print(f"  !! backtest {bt_id} not found")
            continue
        sp = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
        want_days = _genome_days(sp)
        if want_days is None:
            print(f"  inst {inst_id}: backtest {bt_id} carries no schedule genes -- left alone")
            continue

        from ba2_trade_platform.modules.experts import experts as _live_experts
        cls = next((c for c in _live_experts if c.__name__ == inst.expert), None)
        if cls is None:
            print(f"  !! expert {inst.expert!r} not in the live registry")
            continue
        expert = cls(inst_id)
        cur = expert.settings.get("execution_schedule_enter_market") or {}
        if isinstance(cur, str):
            cur = json.loads(cur)
        cur_days = cur.get("days") or {}

        if _trading(cur_days) == _trading(want_days):
            print(f"  inst {inst_id}: already {_trading(want_days)} -- ok")
            continue

        # Keep the instance's own time-of-day and basis: only the DAY selection was optimized
        # (matching _build_daily_trial_config, which keeps the run-level ``times``).
        new = {"days": want_days,
               "times": cur.get("times") or ["09:30"],
               "time_basis": cur.get("time_basis") or "market"}
        print(f"  inst {inst_id} (bt {bt_id}): {_trading(cur_days)} -> {_trading(want_days)}")
        changed += 1
        if ns.apply:
            expert.save_settings({"execution_schedule_enter_market": (new, None)})

    print(f"\n{changed} instance(s) {'repaired' if ns.apply else 'would change'}.")
    if changed and ns.apply:
        print("Now POST /api/reload (or restart) so JobManager rebuilds the cron triggers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
