"""Recompute a backtest's equity/drawdown curves + metrics for a HIDDEN-TRADE what-if,
WITHOUT re-running the simulation.

The stored per-bar snapshot keeps only AGGREGATE net-liq (cash + total MTM), so a single
hidden trade's contribution cannot be subtracted from it exactly — which is why the old
client-side approximation produced cliffs / negative equity for a back-loaded mega-winner.

But net-liq is exactly reconstructable from the trade LEDGER + the same FMP OHLCV cache the
engine read:

    net_liq(t) = initial + realised_pnl(trades closed by t)
                         + unrealised_pnl(trades still open at t)
    unrealised for an open trade = direction * size * (close(t) - entry_price)

So we replay the ledger (NOT the signal engine) over the stored curve's own date axis,
reading each open position's close from the on-disk parquet cache. Excluding a trade is then
exact at bar-close granularity and PER-TRADE (a re-run could only drop a whole symbol, and
would also re-path every later entry). Reconstructing with nothing excluded reproduces the
stored curve (within entry-commission rounding), which is the built-in correctness check.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from .results import _compute_metrics, _drawdown_curve


def _interval_token(axis) -> str:
    """Infer the OHLCV cache interval token from the modal spacing of the curve's date axis.
    native_cache resolves alias spellings (5m/5min), so the coarse mapping below is enough."""
    secs: List[float] = []
    for i in range(1, min(len(axis), 400)):
        secs.append((axis[i] - axis[i - 1]).total_seconds())
    if not secs:
        return "1d"
    m = statistics.median(secs)
    if m <= 90:
        return "1min"
    if m <= 360:
        return "5min"
    if m <= 1200:
        return "15min"
    if m <= 2400:
        return "30min"
    if m <= 5400:
        return "1h"
    return "1d"


def _closes_on_axis(symbol: str, interval: str, axis):
    """Return the symbol's close series re-indexed onto ``axis`` (forward-filled), or None if the
    on-disk OHLCV cache has no parquet for it. Read directly from the native cache — hermetic, no
    network — matching the engine's ``cached_only`` price source."""
    import pandas as pd

    try:
        from ba2_common.core import native_cache
        p = native_cache.find_timeseries_path("FMPOHLCVProvider", symbol, interval)
    except Exception:  # noqa: BLE001
        p = None
    if not p:
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None
    if df is None or len(df) == 0 or "Date" not in df.columns or "Close" not in df.columns:
        return None
    idx = pd.to_datetime(df["Date"], utc=True)
    s = pd.Series(df["Close"].to_numpy(dtype="float64"), index=idx).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(axis, method="ffill").to_numpy(dtype="float64")


def recompute_curves(
    initial_capital: float,
    trades: List[Dict[str, Any]],
    equity_curve: List[Dict[str, Any]],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    exclude_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    """Reconstruct equity_curve / drawdown_curve / metrics from the trade ledger + OHLCV cache,
    excluding the 1-based trade ids in ``exclude_ids`` (matching the API's id = index+1 scheme).

    ``trades`` are the INTERNAL stored rows (entry_time/exit_time/direction/entry_price/size/pnl/
    pnl_pct/...). Returns a dict with ``equity_curve``, ``drawdown_curve`` and every reused metric
    column (same keys/rounding as the live runner's ``_compute_metrics``)."""
    import numpy as np
    import pandas as pd

    if not equity_curve:
        raise ValueError("backtest has no stored equity_curve to recompute against")
    exclude: Set[int] = {int(i) for i in (exclude_ids or [])}

    axis = pd.to_datetime([p["date"] for p in equity_curve], utc=True)
    axis_i8 = axis.asi8  # int64 UTC-ns, ascending — for searchsorted
    interval = _interval_token([d.to_pydatetime() for d in axis[: min(len(axis), 400)]])
    initial = float(initial_capital or 0.0) or 10000.0

    nlv = np.full(len(axis), float(initial), dtype="float64")
    close_cache: Dict[str, Any] = {}
    included: List[Dict[str, Any]] = []

    def _idx(ts_value: int) -> int:
        return int(np.searchsorted(axis_i8, ts_value, side="left"))

    for i, t in enumerate(trades):
        if (i + 1) in exclude:
            continue
        included.append(t)
        pnl = float(t.get("pnl") or 0.0)
        et = t.get("entry_time")
        xt = t.get("exit_time")
        ei = _idx(pd.Timestamp(et, tz="UTC").value) if et else 0
        xi = _idx(pd.Timestamp(xt, tz="UTC").value) if xt else len(axis)
        # Realised P&L lands from the exit bar onward.
        if xi < len(nlv):
            nlv[xi:] += pnl
        # Unrealised mark-to-market across the hold [entry, exit).
        if xi > ei:
            sym = t.get("symbol")
            if sym not in close_cache:
                close_cache[sym] = _closes_on_axis(sym, interval, axis)
            cs = close_cache[sym]
            size = float(t.get("size") or 0.0)
            epx = float(t.get("entry_price") or 0.0)
            dirn = 1.0 if str(t.get("direction", "buy")).lower() in ("buy", "long") else -1.0
            if cs is not None:
                seg = cs[ei:xi]
                unreal = dirn * size * (seg - epx)
                nlv[ei:xi] += np.where(np.isfinite(unreal), unreal, 0.0)
            else:
                # No OHLCV for this symbol (e.g. an option leg) — ramp its P&L linearly over the
                # hold so the curve still moves smoothly instead of dropping a cliff at exit.
                n = xi - ei
                nlv[ei:xi] += pnl * (np.arange(1, n + 1, dtype="float64") / n)

    out_equity = [
        {"date": equity_curve[i]["date"], "equity": float(max(0.0, nlv[i]))}
        for i in range(len(axis))
    ]
    out_dd = _drawdown_curve(out_equity)
    final = out_equity[-1]["equity"] if out_equity else initial
    metrics = _compute_metrics(out_equity, out_dd, included, initial, final, {})
    metrics["equity_curve"] = out_equity
    metrics["drawdown_curve"] = out_dd
    metrics["final_equity"] = final
    metrics["total_trades"] = len(included)
    return metrics
