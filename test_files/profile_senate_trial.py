"""Why is Senate S5 ~6x slower per trial than S1-S4?  (2026-07-28)

S5 (opt 226) ran 6.5h without finishing generation 1, and every remote trial hit the master's
fixed 1800s budget -- 12 timeouts, 4 slots x 3 retries, worker declared dead. remote150 was NOT
swapping (79.7% used) and the scoring shards were untouched (zero delta files => cache hits, no
recompute), so the usual suspects are ruled out. The remaining hypothesis is simply that an S5
trial legitimately takes longer than 1800s.

This measures that instead of guessing. It runs ONE trial of each strategy through the SAME
path the GA uses (_build_daily_trial_config -> run_daily_backtest) over a SHORT window, so the
RATIO is available in minutes rather than hours, and profiles where the time goes.

    .venv\\Scripts\\python.exe test_files\\profile_senate_trial.py --strategies S3,S5
    .venv\\Scripts\\python.exe test_files\\profile_senate_trial.py --strategies S5 --profile

RUN ON AN IDLE BOX. Every previous throughput estimate taken during a contended run (8.9 days,
12.7 h/strategy, 30.6 h/strategy) turned out to be a swapping artifact, not a real measurement.

logging.disable(INFO) is deliberate: a direct run_daily_backtest() call bypasses the GA's own
log suppression and is 10x+ slower without it, which would swamp the very thing being measured.
"""
import argparse
import cProfile
import io
import os
import pstats
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "testplatform"))

from ba2test_launcher import (  # noqa: E402
    _enter_backend, _build_strategy, _expert_run_settings, _EXPERT_OPT,
)

EXPERT = "FMPSenateTraderWeight"


def build_config(strategy_kind, universe, start, end, interval):
    # both live in the backend package -- importable only after _enter_backend()
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.backtest.daily_backtest_handler import derive_warmup_days
    from app.services.strategy_param_space import decode_params

    spec = _EXPERT_OPT[EXPERT]
    strat = _build_strategy(strategy_kind, f"profile-{strategy_kind}", EXPERT)

    backtest_block = {
        "engine": "daily",
        "enabled_instruments": universe,
        "experts": [{"class": EXPERT, "settings": _expert_run_settings(spec, universe)}],
        "start_date": start, "end_date": end,
        "initial_capital": 10000.0,
        "account_settings": {
            "starting_cash": 10000.0, "commission_per_trade": 1.0,
            "slippage_bps": 0.0, "spread_bps": 20.0, "fill_model": "next_bar_open",
        },
        "warmup_days": derive_warmup_days([EXPERT]),
        "seed": 42,
        "subtype": "daily_expert",
        "execution_interval": interval,
        "labels": [], "backtest_id": int(time.time()),
        "name": f"profile-{strategy_kind}",
    }
    # The rules reach a trial ONLY through decoded['entry_rules']/['exit_rules'] -- passing
    # decoded={} runs NO strategy at all (first attempt did exactly that and produced identical
    # trades=59 for S3 and S5, which is what exposed the mistake). decode_params(strat, {})
    # applies no gene substitution, i.e. the strategy's authored BASELINE -- the fair
    # like-for-like comparison point.
    decoded = decode_params(strat, {})
    return _build_daily_trial_config(backtest_block, decoded, {"backtest_cfg": backtest_block})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="S5",
                    help="ONE per process: a second strategy in the same process reuses warm "
                         "OHLCV/scoring caches and looks artificially fast (the first attempt "
                         "showed S5 3.3x 'faster' purely from running second)")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2023-06-30", help="short window: the RATIO is the point")
    ap.add_argument("--interval", default="5min")
    ap.add_argument("--profile", action="store_true", help="cProfile the run (adds overhead)")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    _enter_backend()
    import logging
    logging.disable(logging.INFO)   # see module docstring -- 10x without this

    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    uni = open(os.path.expanduser("~/Documents/ba2/senate_universe.csv")).read().strip()
    universe = [s.strip() for s in uni.split(",") if s.strip()]
    print(f"universe: {len(universe)} symbols   window {args.start}..{args.end} @ {args.interval}\n")

    results = {}
    for kind in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        cfg = build_config(kind, universe, args.start, args.end, args.interval)
        print(f"--- {kind}: running ...", flush=True)
        t0 = time.perf_counter()
        if args.profile:
            pr = cProfile.Profile(); pr.enable()
            res = run_daily_backtest(cfg)
            pr.disable()
        else:
            res = run_daily_backtest(cfg)
        dt = time.perf_counter() - t0
        results[kind] = (dt, res.get("total_trades"))
        print(f"    {kind}: {dt:8.1f}s   trades={res.get('total_trades')}   "
              f"return={res.get('total_return_pct')}")
        if args.profile:
            s = io.StringIO()
            pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(args.top)
            print("\n".join(s.getvalue().splitlines()[:args.top + 12]))

    if len(results) > 1:
        print("\n=== RATIO ===")
        base = min(results.values(), key=lambda v: v[0])[0]
        for k, (dt, tr) in sorted(results.items(), key=lambda kv: kv[1][0]):
            print(f"  {k:4s} {dt:8.1f}s  ({dt / base:5.2f}x)  trades={tr}")


if __name__ == "__main__":
    main()
