#!/usr/bin/env python
"""Re-measure split-basis override ratios under the PLATEAU CONVENTION (read-only, no network).

The convention (``ba2_common.core.split_basis_overrides`` module docstring): with
r(d) = put-call-parity spot / (FMP close x the factor of every OTHER calendar row and override
entry of the symbol), an ``add_price_adjustment`` at event date c gets

    ratio = median(r over the <= 60 evaluable sessions before c, after the previous cut)
          / median(r over the <= 60 evaluable sessions from c on, before the next cut)

rounded to 4 decimals (6 below 0.1). Fixed-ratio corporate actions (a reverse split missing
from the calendar) use their NOMINAL ratio instead -- the plateau there carries borrow.

The parity spot is the option basis guard's own (``option_basis_guard.parity_spot``, the same
pair gates), read from the run's ThetaData store; the FMP close and split calendar are the
cached ones; the symbol's shipped adds at the given cuts are removed before measuring.

    python tools/measure_basis_plateaus.py FLEX 2023-12-04,2024-01-03
    python tools/measure_basis_plateaus.py TRP 2024-10-02 --window 60

Prints each interval's median (p10-p90, n) and the ratio of every cut, then the residual with
those ratios applied.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from typing import Dict, List, Sequence

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "testplatform", "backend"))


def _round(r: float) -> float:
    return round(r, 4 if abs(r) >= 0.1 else 6)


def plateau_ratios(series: Dict[date, float], cuts: Sequence[date], window: int = 60
                   ) -> List[dict]:
    """The convention: for every cut, median of the <= ``window`` sessions before it (after the
    previous cut) over the median of the <= ``window`` from it (before the next cut). PURE."""
    import numpy as np

    days = sorted(series)
    bounds = [date.min] + list(cuts) + [date.max]
    out = []
    for i, c in enumerate(cuts):
        left = [series[d] for d in days if bounds[i] <= d < c][-window:]
        right = [series[d] for d in days if c <= d < bounds[i + 2]][:window]
        if not left or not right:
            raise ValueError(f"cut {c}: no evaluable session on one side")
        ml, mr = float(np.median(left)), float(np.median(right))
        out.append({"cut": c, "left_n": len(left), "left": ml, "right_n": len(right),
                    "right": mr, "ratio": _round(ml / mr),
                    "left_p10_p90": (float(np.quantile(left, .1)), float(np.quantile(left, .9)))})
    return out


def measure(symbol: str, cuts: Sequence[date], start: date = date(2020, 1, 1),
            end: date = date(2026, 3, 31)) -> Dict[date, float]:
    """r(d) of ``symbol`` with its shipped adds at ``cuts`` removed (module docstring)."""
    import logging
    logging.disable(logging.INFO)
    import pandas as pd
    from ba2_common import config
    from ba2_common.core import split_basis_overrides as ovr
    from app.services.backtest import option_split_basis as osb
    from app.services.backtest.option_basis_guard import parity_spot
    from app.services.backtest.options_store import default_options_parquet_root
    from app.services.backtest.parquet_options_provider import _Underlying, _raw_underlying

    cutset = set(cuts)
    ovr.BASIS_OVERRIDES = tuple(o for o in ovr.BASIS_OVERRIDES if not (
        o.symbol == symbol and o.kind == ovr.KIND_ADD_PRICE_ADJUSTMENT and o.event_date in cutset))
    osb.clear_split_basis_memo()
    path = os.path.join(config.CACHE_FOLDER, "FMPOHLCVProvider", f"{symbol}_1d.parquet")
    basis = osb._resolve_one(symbol, path)
    df = pd.read_parquet(path, columns=["Date", "Close"])
    d = pd.to_datetime(df["Date"])
    d = d.dt.tz_localize(None) if getattr(d.dt, "tz", None) is not None else d
    closes = dict(zip(d.dt.date, df["Close"].astype(float)))
    u = _Underlying(_raw_underlying(default_options_parquet_root("thetadata"), symbol), 0.0)
    out = {}
    for day in sorted(closes):
        if not (start <= day <= end):
            continue
        par = parity_spot(u, day.toordinal())
        if par is None:
            continue
        out[day] = par / (closes[day] * basis.factor(day))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("cuts", help="comma-separated event dates (ISO)")
    ap.add_argument("--window", type=int, default=60)
    args = ap.parse_args(argv)
    cuts = sorted(date.fromisoformat(c) for c in args.cuts.split(","))
    series = measure(args.symbol.upper(), cuts)
    res = plateau_ratios(series, cuts, args.window)
    for r in res:
        print(f"{args.symbol} cut {r['cut']}: left n={r['left_n']} median {r['left']:.4f} "
              f"[{r['left_p10_p90'][0]:.4f},{r['left_p10_p90'][1]:.4f}]  right n={r['right_n']} "
              f"median {r['right']:.4f}  ratio {r['ratio']}")
    import numpy as np
    resid = {d: v / float(np.prod([r["ratio"] for r in res if d < r["cut"]] or [1.0]))
             for d, v in series.items()}
    vals = list(resid.values())
    print(f"residual: median {np.median(vals):.4f}, sessions >5%: "
          f"{sum(abs(v - 1) > 0.05 for v in vals)}, >2%: {sum(abs(v - 1) > 0.02 for v in vals)} "
          f"of {len(vals)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
