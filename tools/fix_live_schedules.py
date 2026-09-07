#!/usr/bin/env python
"""Repair a live ExpertInstance's deployed configuration to what its genome was scored on.

    BA2_LIVE_DB=... python tools/fix_live_schedules.py                  # dry run (default)
    BA2_LIVE_DB=... python tools/fix_live_schedules.py --apply
    BA2_LIVE_DB=... python tools/fix_live_schedules.py --only cap --apply

Two repairs, both from the 2026-09-07 parity review, both of them cases where a deploy wrote
something live never read or never wrote something live needed:

1. ENTRY CADENCE. The GA searches the entry weekday per individual (``schedule:<day>`` genes)
   and the decoded days REPLACE the run-level cadence for that individual.
   ``_derive_export_payload`` exported the RUN-LEVEL override instead, so every deploy carried
   the grid's launch cadence -- Monday-only for all of goal2020 -- whatever the genome chose.
   Three of the six live instances (8, 9, 11) have genomes that never trade Monday at all,
   which was the only day live was firing.

2. SCREENER MARKET-CAP CEILING. ``live_settings_from_universe`` copied the run-level
   ``market_cap_max`` verbatim; ``StockScreener`` reads ``screener_market_cap_max`` and so kept
   its own default of 0, which means NO CEILING. Instances 8-12 have been screening an
   unbounded universe, so "small" and "mid" stopped describing the band their genome was
   selected on.

The exporter/importer are fixed for FUTURE deploys, but a re-import cannot repair these: the
importer's schedule block is inside ``if created`` and uses setdefault, so an existing instance
keeps whatever wrong value it already holds. This tool does exactly those two repairs, through
the expert's own ``save_settings`` -- the same path a settings save from the UI takes, so value
typing follows get_settings_definitions -- and touches nothing else.

``execution_schedule_open_positions`` is deliberately NOT changed: exits must be evaluated every
weekday whatever the entry cadence is, which is what live already has. The stale unprefixed
``market_cap_max`` row is left in place: it is inert, and deleting a settings row is a bigger
action than adding the one that was missing.

Run POST /api/reload afterwards (or restart) so JobManager rebuilds the cron triggers and the
instance/settings caches are dropped.
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
    """The genome's cadence as LIVE should run it: weekdays only, all-off repaired to Monday.

    WEEKEND GENES ARE NOISE, and must not survive into a live schedule. A daily-clock backtest
    has no weekend bars, so a saturday/sunday gene is something the GA could never evaluate --
    it stays ON in perfectly good genomes because nothing ever selected against it. Live has a
    real scheduler with no market-open guard on the cron (JobManager._parse_schedule builds a
    plain day_of_week trigger), so copying those bits arms a Saturday 09:30 entry pass into a
    closed market: behaviour no backtest ever scored. Three of the six deployed genomes carry
    one (7 and 10 saturday, 11 sunday).
    """
    by_day = {k[len("schedule:"):]: bool(v) for k, v in sp.items()
              if isinstance(k, str) and k.startswith("schedule:")}
    if not by_day:
        return None
    days = {d: (by_day.get(d, False) and d in DAYS[:5]) for d in DAYS}
    if not any(days.values()):
        days[DAYS[0]] = True
    return days


def _trading(days):
    """Weekdays only -- a daily bar clock never produces a Saturday or Sunday entry, so a
    weekend gene is noise rather than a difference."""
    return [d for d in DAYS[:5] if days.get(d)]


def _expert_for(inst):
    from ba2_trade_platform.modules.experts import experts as _live_experts
    cls = next((c for c in _live_experts if c.__name__ == inst.expert), None)
    if cls is None:
        print(f"  !! expert {inst.expert!r} not in the live registry")
        return None
    return cls(inst.id)


def _opt_screener_base(test, bt_id):
    """The run-level screener base settings of a backtest's parent optimization."""
    oid = test.execute("select optimization_id from backtests where id=?", (bt_id,)).fetchone()
    if not oid or oid[0] is None:
        return {}
    row = test.execute("select optimization_config from strategy_optimizations where id=?",
                       (oid[0],)).fetchone()
    if row is None:
        return {}
    cfg = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    return (((cfg or {}).get("backtest") or {}).get("screener_opt") or {}).get(
        "base_settings") or {}


def repair_schedules(ns, test) -> int:
    """Move each instance's ENTRY cadence onto the days its genome was scored on."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertInstance

    print("--- entry cadence ---")
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

        expert = _expert_for(inst)
        if expert is None:
            continue
        cur = expert.settings.get("execution_schedule_enter_market") or {}
        if isinstance(cur, str):
            cur = json.loads(cur)
        cur_days = cur.get("days") or {}

        # Compare ALL SEVEN days, not just the trading ones: a stored saturday=True is
        # invisible to _trading() but arms a real Saturday cron, so a weekday-only comparison
        # would report "already ok" and leave the weekend bit in place.
        if all(bool(cur_days.get(d)) == want_days[d] for d in DAYS):
            print(f"  inst {inst_id}: already {_trading(want_days)} -- ok")
            continue
        weekend_dropped = [d for d in DAYS[5:] if cur_days.get(d)]

        # Keep the instance's own time-of-day and basis: only the DAY selection was optimized
        # (matching _build_daily_trial_config, which keeps the run-level ``times``).
        new = {"days": want_days,
               "times": cur.get("times") or ["09:30"],
               "time_basis": cur.get("time_basis") or "market"}
        note = f"   (dropping inert weekend gene: {weekend_dropped})" if weekend_dropped else ""
        print(f"  inst {inst_id} (bt {bt_id}): {_trading(cur_days)} -> "
              f"{_trading(want_days)}{note}")
        changed += 1
        if ns.apply:
            expert.save_settings({"execution_schedule_enter_market": (new, None)})

    print(f"  {changed} instance(s) {'repaired' if ns.apply else 'would change'}.\n")
    return changed


def repair_cap_ceiling(ns, test) -> int:
    """Write the screener's UPPER market-cap bound under the name StockScreener reads."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertInstance

    print("--- screener market-cap ceiling ---")
    changed = 0
    for inst_id, bt_id in DEPLOYED:
        inst = get_instance(ExpertInstance, inst_id)
        if inst is None:
            continue
        want = _opt_screener_base(test, bt_id).get("market_cap_max")
        expert = _expert_for(inst)
        if expert is None:
            continue
        cur = expert.settings.get("screener_market_cap_max")
        if want is None:
            print(f"  inst {inst_id}: backtest set no ceiling -- live {cur!r} left alone")
            continue
        if cur is not None and float(cur) == float(want):
            print(f"  inst {inst_id}: already ${float(want):,.0f} -- ok")
            continue
        print(f"  inst {inst_id} (bt {bt_id}): screener_market_cap_max {cur!r} -> "
              f"${float(want):,.0f}")
        changed += 1
        if ns.apply:
            expert.save_settings({"screener_market_cap_max": (float(want), None)})

    print(f"  {changed} instance(s) {'repaired' if ns.apply else 'would change'}.\n")
    return changed


#: Live-only screener floors, with the class default each falls back to. The metric store the
#: backtests screened on applies each of these ONLY when the setting is present, and these runs'
#: screener_settings carried none of them -- so the backtest had no price, volume or float floor
#: at all while live silently applied three. 118 of backtest 1425's 185 recorded entries were
#: below live's $20 floor, i.e. trades the deployed instance could never take.
#:
#: Disabling them live (0 = off) makes the live screen the one that was actually measured.
LIVE_ONLY_FLOORS = {
    "screener_price_min": 20.0,
    "screener_volume_min": 500_000,
    "screener_float_min": 10_000_000,
}


def repair_screener_floors(ns, test) -> int:
    """Turn off the live-only price/volume/float floors the backtests never applied."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import ExpertInstance

    print("--- live-only screener floors ---")
    changed = 0
    for inst_id, _bt_id in DEPLOYED:
        inst = get_instance(ExpertInstance, inst_id)
        if inst is None:
            continue
        expert = _expert_for(inst)
        if expert is None:
            continue
        for key, default in LIVE_ONLY_FLOORS.items():
            cur = expert.settings.get(key)
            effective = default if cur is None else float(cur)
            if effective == 0:
                continue
            print(f"  inst {inst_id}: {key} {effective:,.0f} -> 0 (off)")
            changed += 1
            if ns.apply:
                expert.save_settings({key: (0.0, None)})
    print(f"  {changed} setting(s) {'repaired' if ns.apply else 'would change'}.\n")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    ap.add_argument("--only", choices=("schedule", "cap", "floors"),
                    help="run just one of the repairs")
    ns = ap.parse_args()

    import sqlite3
    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(LIVE_DB)

    test = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True)
    mode = "APPLY" if ns.apply else "DRY RUN"
    print(f"LIVE_DB = {LIVE_DB}")
    print(f"TEST_DB = {TEST_DB}")
    print(f"mode    = {mode}\n")

    changed = 0
    if ns.only in (None, "schedule"):
        changed += repair_schedules(ns, test)
    if ns.only in (None, "cap"):
        changed += repair_cap_ceiling(ns, test)
    if ns.only in (None, "floors"):
        changed += repair_screener_floors(ns, test)

    if changed and ns.apply:
        print("Now POST /api/reload (or restart): JobManager rebuilds the cron triggers and "
              "the instance/settings caches are dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
