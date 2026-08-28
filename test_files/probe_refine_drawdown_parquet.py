"""What the SILENTLY-DISABLED intraday drawdown refinement was worth, on the parquet store.

Ad-hoc probe (test_files/, not collected by pytest). Runs ONE real single backtest twice in
one process against the local TastyTrade parquet tree:

  A. as shipped now — ``results._build_refine_drawdown_fn`` binds the reader's named
     ``delta_at_entry`` seam, so the refinement RUNS;
  B. with ``_build_refine_drawdown_fn`` forced to None — the pre-fix behaviour on this
     backend, where it bound ``options.cache.db_path`` (sqlite-only) and returned None.

and prints max_drawdown / calmar_ratio / option_consistent_annual_return for both. Any
difference is the fitness gap two identical runs used to show purely because of which store
served the options.

Run:
  FMP_API_KEY=dummy-hermetic-probe BACKTEST_OPTIONS_STORE=parquet \
  BA2_HOME=/tmp/ba2-refine-probe \
  PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend:. \
  ./venv/bin/python test_files/probe_refine_drawdown_parquet.py \
      --kinds O_LC --symbols GOOG,BAC,INTC,F,T --start 2023-01-10 --end 2023-03-28
"""
from __future__ import annotations

import argparse
import importlib
import logging
import sys
from pathlib import Path

logging.disable(logging.INFO)

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "testplatform" / "backend"))

probe = importlib.import_module("probe_option_engine_structures")


def _fitness(res, cfg):
    from app.services.strategy_fitness import compute_fitness

    out = {}
    for metric in ("calmar_ratio", "option_consistent_annual_return"):
        try:
            out[metric] = compute_fitness(metric, res)
        except Exception as e:  # noqa: BLE001 — probe
            out[metric] = f"<{type(e).__name__}: {e}>"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default="O_LC")
    ap.add_argument("--symbols", default="GOOG,BAC,INTC,F,T")
    ap.add_argument("--start", default="2023-01-10")
    ap.add_argument("--end", default="2023-03-28")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    probe._init_scratch_db()
    m = probe._launcher()
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    from app.services.backtest import results as results_mod
    from app.services.backtest.daily_backtest_handler import run_daily_backtest

    real = results_mod._build_refine_drawdown_fn
    for kind in [k.strip() for k in a.kinds.split(",") if k.strip()]:
        cfg = probe.build_trial_config(m, kind, symbols, a.start, a.end, a.capital,
                                       a.seed, False)
        rows = {}
        for label, fn in (("REFINED (after)", real), ("SKIPPED (before)", lambda *_: None)):
            results_mod._build_refine_drawdown_fn = fn
            try:
                res = run_daily_backtest(dict(cfg))
            finally:
                results_mod._build_refine_drawdown_fn = real
            rows[label] = res

        print(f"\n=== {kind} {symbols} {a.start}..{a.end} (parquet store) ===")
        hdr = f"{'':18s} {'trades':>7} {'total_ret':>11} {'max_dd':>10} {'calmar':>10} {'opt_car':>10}"
        print(hdr)
        for label, res in rows.items():
            f = _fitness(res, cfg)
            def _n(v):
                return f"{v:10.4f}" if isinstance(v, (int, float)) else f"{str(v):>10}"
            print(f"{label:18s} {res.get('total_trades'):>7} "
                  f"{_n(res.get('total_return'))} {_n(res.get('max_drawdown'))} "
                  f"{_n(f['calmar_ratio'])} {_n(f['option_consistent_annual_return'])}")
        a_, b_ = rows["REFINED (after)"], rows["SKIPPED (before)"]
        same = a_.get("max_drawdown") == b_.get("max_drawdown")
        print(f"  max_drawdown identical? {same}"
              + ("" if same else "  <- the gap the silent skip hid"))


if __name__ == "__main__":
    main()
