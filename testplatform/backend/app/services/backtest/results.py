"""Convert a finished ``BacktestAccount`` run into the ``Backtest`` results dict + metrics.

Phase 2 Task 5. The legacy ML path (``backtest_handler._convert_bt_results``) gets its
metrics from a ``backtesting.py`` stats object; the daily engine has NO such object, so this
module computes the SAME metric set directly from the account's per-bar equity snapshots
(``get_balance_history``) and filled trades (``get_filled_trades``). The OUTPUT shape is kept
byte-compatible with ``_convert_bt_results`` so the SAME ``Backtest`` columns + ``Backtest.to_dict``
camelCase contract + ``Backtesting.tsx`` UI consume it unchanged.

Reuses the existing guards from ``backtest_handler``:
  * ``_safe_float``  — NaN/Inf -> default (used on every metric).
  * profit-factor cap at 999.99 (mirrored from ``_convert_bt_results``).

No defaults rule (``backend/CLAUDE.md``): ``config`` is read via ``config[...]`` for the
load-bearing keys (the handler validates fail-early before calling here); only the genuinely
optional ``commission_per_trade`` (already folded into the ledger) is absent from this layer.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# Import the metric-coercion helpers from the lightweight ``metrics_utils`` module, NOT from
# ``backtest_handler`` (the legacy ML path), which top-imports the tsai/torch/darts training
# stack (~7s of startup) that the expert backtest never uses. See metrics_utils for details.
from app.services.backtest.metrics_utils import _safe_float, _safe_duration_days


# Trading days per year — the standard convention used by backtesting.py for annualisation.
_TRADING_DAYS_PER_YEAR = 252
_PROFIT_FACTOR_CAP = 999.99


def build_results(account: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """Build the full results dict for a finished daily-engine run.

    Args:
        account: the finished ``BacktestAccount`` (read via ``get_balance_history`` /
            ``get_filled_trades``). Any object exposing those two methods works (tests
            inject a lightweight stub).
        config: the run config. Required key: ``initial_capital``.

    Returns:
        A dict carrying every reused ``Backtest`` metric column plus ``equity_curve`` /
        ``drawdown_curve`` / ``trades`` (same keys ``_convert_bt_results`` emits, so
        ``handle_daily_backtest._persist_results`` maps it 1:1 onto the columns).
    """
    initial = float(config["initial_capital"])

    snaps = account.get_balance_history()
    equity_curve = [
        {"date": _iso(s["date"]), "equity": _safe_float(s["net_liquidating_value"], initial)}
        for s in snaps
    ]
    drawdown_curve = _drawdown_curve(equity_curve)
    # Prefer round-trip trades (entry+exit paired with realised P&L) when the account exposes
    # them — that's what makes win_rate/profit_factor/expectancy meaningful. Fall back to the
    # per-fill rows for accounts/stubs that don't implement the pairing.
    if hasattr(account, "get_round_trip_trades"):
        raw_trades = account.get_round_trip_trades()
    else:
        raw_trades = account.get_filled_trades()
    trades = [_trade_row(t) for t in raw_trades]

    final = equity_curve[-1]["equity"] if equity_curve else initial

    metrics = _compute_metrics(equity_curve, drawdown_curve, trades, initial, final, config)
    metrics["equity_curve"] = equity_curve
    metrics["drawdown_curve"] = drawdown_curve
    metrics["trades"] = trades
    # Positions still OPEN at the end of the run. total_trades counts CLOSED round-trips, so a
    # buy-and-hold (no exit rule) shows 0 trades while equity still moves (entry commission +
    # the held position's mark-to-market). Surfacing these explains "0 trades but P&L changed".
    metrics["open_positions"] = _open_positions(account)
    return metrics


def _open_positions(account: Any) -> List[Dict[str, Any]]:
    """JSON-safe snapshot of positions still open at run end (empty if the account/stub
    doesn't expose get_positions)."""
    if not hasattr(account, "get_positions"):
        return []
    out: List[Dict[str, Any]] = []
    try:
        for p in account.get_positions():
            get = (lambda k: p.get(k)) if isinstance(p, dict) else (lambda k: getattr(p, k, None))
            out.append({
                "symbol": get("symbol"),
                "qty": _safe_float(get("qty")),
                "avg_price": _safe_float(get("avg_price")),
                "current_price": _safe_float(get("current_price")),
                "unrealized_pl": _safe_float(get("unrealized_pl")),
            })
    except Exception:  # noqa: BLE001 — open-position surfacing must never fail the run
        return out
    return out


# ---------------------------------------------------------------------------
# Curves
# ---------------------------------------------------------------------------
def _drawdown_curve(equity_curve: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Running-peak drawdown as a percentage (negative or zero), per equity point.

    drawdown_pct = (equity - running_peak) / running_peak * 100  (<= 0).
    """
    out: List[Dict[str, Any]] = []
    peak = None
    for pt in equity_curve:
        eq = pt["equity"]
        if peak is None or eq > peak:
            peak = eq
        dd = ((eq - peak) / peak * 100.0) if peak and peak != 0 else 0.0
        out.append({"date": pt["date"], "drawdown": _safe_float(dd)})
    return out


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------
def _trade_row(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a filled-trade dict to the field names ``Backtest._transform_trades_for_frontend``
    consumes (``entry_time``/``exit_time``/``direction``/``entry_price``/``exit_price``/``size``/
    ``pnl``/``pnl_pct``/``bars_held``/``exit_reason``).

    ``BacktestAccount.get_filled_trades`` currently returns per-FILL rows
    (``symbol``/``qty``/``side``/``date``/``price``) — a round-trip P&L join is a later
    refinement. We map what is present and leave round-trip fields (exit/pnl) at safe zeros
    so the UI renders the trade list without KeyErrors. ``side``/``direction`` is normalised
    to the ``buy``/``sell`` vocabulary ``_transform_trades_for_frontend`` maps to long/short.
    """
    side = trade.get("side")
    direction = _normalise_direction(side if side is not None else trade.get("direction"))
    entry_time = trade.get("entry_time", trade.get("date"))
    entry_price = trade.get("entry_price", trade.get("price"))
    return {
        "symbol": trade.get("symbol"),
        "entry_time": _iso(entry_time),
        "exit_time": _iso(trade.get("exit_time")),
        "direction": direction,
        "entry_price": _safe_float(entry_price),
        "exit_price": _safe_float(trade.get("exit_price")),
        "size": _safe_float(trade.get("size", trade.get("qty"))),
        "pnl": _safe_float(trade.get("pnl")),
        "pnl_pct": _safe_float(trade.get("pnl_pct")),
        "bars_held": int(trade.get("bars_held", 0) or 0),
        "exit_reason": trade.get("exit_reason", "unknown"),
    }


def _normalise_direction(value: Any) -> str:
    """Map any direction representation to the ``buy``/``sell`` vocabulary."""
    if value is None:
        return "buy"
    s = str(value).lower()
    if s in ("buy", "long", "b"):
        return "buy"
    if s in ("sell", "short", "s"):
        return "sell"
    return "buy"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def _compute_metrics(
    equity_curve: List[Dict[str, Any]],
    drawdown_curve: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    initial: float,
    final: float,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute every reused ``Backtest`` metric column from the curves + trades.

    Mirrors the metric *set* of ``backtest_handler._convert_bt_results`` (same keys, same
    rounding, same profit-factor cap) but derives the values from the equity series and the
    trade list directly (no ``backtesting.py`` stats object). All values pass through
    ``_safe_float`` so NaN/Inf never reach the DB.
    """
    equities = [pt["equity"] for pt in equity_curve]
    n_points = len(equities)

    # --- returns -----------------------------------------------------------
    total_return = ((final - initial) / initial * 100.0) if initial else 0.0
    equity_peak = max(equities) if equities else initial

    # --- per-step returns (for risk metrics) -------------------------------
    # Annualise from the ACTUAL calendar time the equity curve spans, not the point COUNT —
    # the fill clock may be daily, 5min, or a skip-flat (irregularly-spaced) curve.
    years = _years_spanned(equity_curve)
    periods_per_year = _periods_per_year(n_points, years)
    step_returns = _step_returns(equities)
    volatility = _annualized_volatility(step_returns, periods_per_year)
    annualized_return = _annualized_return(initial, final, years)
    sharpe = _sharpe(step_returns, periods_per_year)
    sortino = _sortino(step_returns, periods_per_year)

    # --- drawdown ----------------------------------------------------------
    dd_values = [pt["drawdown"] for pt in drawdown_curve]  # <= 0
    max_drawdown = min(dd_values) if dd_values else 0.0  # most negative
    neg_dd = [d for d in dd_values if d < 0]
    avg_drawdown = (sum(neg_dd) / len(neg_dd)) if neg_dd else 0.0
    max_dd_duration = _max_drawdown_duration_days(drawdown_curve)
    calmar = (annualized_return / abs(max_drawdown)) if max_drawdown else 0.0

    # --- trade quality -----------------------------------------------------
    pnls = [t["pnl"] for t in trades]
    pnl_pcts = [t["pnl_pct"] for t in trades]
    total_trades = len(trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = (winning_trades / total_trades * 100.0) if total_trades else 0.0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = _PROFIT_FACTOR_CAP  # all winners -> Inf, capped
    else:
        profit_factor = 0.0
    if profit_factor > 999:
        profit_factor = _PROFIT_FACTOR_CAP

    expectancy = (sum(pnl_pcts) / total_trades) if total_trades else 0.0
    avg_trade = expectancy  # arithmetic mean trade-return % (same as expectancy here)
    best_trade = max(pnl_pcts) if pnl_pcts else 0.0
    worst_trade = min(pnl_pcts) if pnl_pcts else 0.0
    sqn = _sqn(pnls)

    avg_trade_duration = _avg_trade_duration_days(trades)
    exposure_time = _exposure_time(trades, n_points)

    # --- benchmark (no per-symbol B&H reconstruction in v1) ----------------
    buy_hold_return = 0.0  # multi-asset B&H benchmark is Phase 3 (universe reconstruction)

    return {
        # Basic trade metrics
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round(_safe_float(win_rate), 2),
        # Return metrics
        "total_return": round(_safe_float(total_return), 2),
        "annualized_return": round(_safe_float(annualized_return), 2),
        "buy_hold_return": round(_safe_float(buy_hold_return), 2),
        # Risk metrics
        "sharpe_ratio": round(_safe_float(sharpe), 2),
        "sortino_ratio": round(_safe_float(sortino), 2),
        "calmar_ratio": round(_safe_float(calmar), 2),
        "volatility": round(_safe_float(volatility), 2),
        # Drawdown metrics
        "max_drawdown": round(_safe_float(max_drawdown), 2),
        "avg_drawdown": round(_safe_float(avg_drawdown), 2),
        "max_drawdown_duration": round(_safe_float(max_dd_duration), 1),
        # Trade quality metrics
        "profit_factor": round(_safe_float(profit_factor), 2),
        "expectancy": round(_safe_float(expectancy), 2),
        "sqn": round(_safe_float(sqn), 2),
        "avg_trade": round(_safe_float(avg_trade), 2),
        "best_trade": round(_safe_float(best_trade), 2),
        "worst_trade": round(_safe_float(worst_trade), 2),
        # Duration metrics
        "avg_trade_duration": round(_safe_float(avg_trade_duration), 1),
        "exposure_time": round(_safe_float(exposure_time), 2),
        # Equity metrics
        "final_equity": round(_safe_float(final, initial), 2),
        "equity_peak": round(_safe_float(equity_peak, initial), 2),
        # Run config echoed into the result so the fill granularity is visible after the fact
        # (History / report): the FILL clock interval (e.g. 5min for precise TP/SL) and the
        # analysis cadence (weekly when run_schedule_override pins a single weekday, else daily).
        "execution_interval": config.get("execution_interval", "1d"),
        "analysis_cadence": _analysis_cadence_label(config.get("run_schedule_override")),
    }


def _analysis_cadence_label(run_schedule_override: Any) -> str:
    """'weekly' when the override pins exactly one weekday on, 'daily' when none/empty, else
    'custom' (a multi-day schedule)."""
    if not run_schedule_override:
        return "daily"
    days = run_schedule_override.get("days") if isinstance(run_schedule_override, dict) else None
    if not days:
        return "daily"
    on = [d for d, v in days.items() if v]
    return "weekly" if len(on) == 1 else ("daily" if not on else "custom")


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------
def _step_returns(equities: List[float]) -> List[float]:
    """Per-bar simple returns ``(e[i]/e[i-1] - 1)`` (drops the first point)."""
    out: List[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev and prev != 0:
            out.append(equities[i] / prev - 1.0)
        else:
            out.append(0.0)
    return out


def _mean(xs: List[float]) -> float:
    return (sum(xs) / len(xs)) if xs else 0.0


def _std(xs: List[float]) -> float:
    """Sample standard deviation (ddof=1), matching backtesting.py's annualisation base."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _years_spanned(equity_curve: List[Dict[str, Any]]) -> float:
    """Calendar years between the first and last equity-curve timestamps.

    Annualisation must be driven by the ACTUAL elapsed wall-clock time, NOT the equity-point
    COUNT. The fill clock can be daily, 5min, or — with the skip-flat-bars optimisation — an
    irregularly-spaced curve where the point count bears no fixed relationship to elapsed time.
    Using the point count (the old ``(n_points-1)/252`` assumption) made a 5min curve look like
    hundreds of "years", collapsing annualised_return -> ~0 and therefore Calmar -> ~0.01.
    """
    if not equity_curve:
        return 0.0
    first = _parse_date(equity_curve[0]["date"])
    last = _parse_date(equity_curve[-1]["date"])
    if first is None or last is None:
        return 0.0
    secs = (last - first).total_seconds()
    return secs / (365.25 * 86400.0) if secs > 0 else 0.0


def _periods_per_year(n_points: int, years: float) -> float:
    """Empirical sampling frequency (return-steps per calendar year), used to annualise the
    per-bar volatility / Sharpe / Sortino instead of the hard-coded daily ``252``.

    Derived from the real curve cadence (``(n_points-1) / years``) so the same scaling is
    correct for daily, 5min, AND skip-flat curves. Falls back to the daily convention when the
    calendar span is unavailable (single-point or undated curve)."""
    if years > 0 and n_points >= 2:
        return (n_points - 1) / years
    return float(_TRADING_DAYS_PER_YEAR)


def _annualized_volatility(step_returns: List[float], periods_per_year: float) -> float:
    """Annualised volatility (%) of per-bar returns, scaled by the curve's actual cadence."""
    return _std(step_returns) * math.sqrt(periods_per_year) * 100.0


def _annualized_return(initial: float, final: float, years: float) -> float:
    """Geometric annualised return (%) over the actual ``years`` of calendar time elapsed."""
    if initial <= 0 or final <= 0 or years <= 0:
        return 0.0
    return ((final / initial) ** (1.0 / years) - 1.0) * 100.0


def _sharpe(step_returns: List[float], periods_per_year: float) -> float:
    """Annualised Sharpe ratio (risk-free rate = 0), from per-bar returns."""
    sd = _std(step_returns)
    if sd == 0:
        return 0.0
    return _mean(step_returns) / sd * math.sqrt(periods_per_year)


def _sortino(step_returns: List[float], periods_per_year: float) -> float:
    """Annualised Sortino ratio (downside deviation, risk-free rate = 0)."""
    downside = [r for r in step_returns if r < 0]
    if len(downside) < 1:
        return 0.0
    dd = math.sqrt(sum(r * r for r in downside) / len(downside))
    if dd == 0:
        return 0.0
    return _mean(step_returns) / dd * math.sqrt(periods_per_year)


def _sqn(pnls: List[float]) -> float:
    """System Quality Number = mean(trade PnL) / std(trade PnL) * sqrt(N)."""
    n = len(pnls)
    if n < 2:
        return 0.0
    sd = _std(pnls)
    if sd == 0:
        return 0.0
    return _mean(pnls) / sd * math.sqrt(n)


def _max_drawdown_duration_days(drawdown_curve: List[Dict[str, Any]]) -> float:
    """Longest stretch (in calendar days) the equity spent below a prior peak.

    A drawdown 'spell' starts at the first negative drawdown after a 0 and ends when the
    curve returns to 0 (recovery). The longest spell's calendar span is returned.
    """
    longest = 0.0
    spell_start: Optional[Any] = None
    prev_date = None
    for pt in drawdown_curve:
        d = _parse_date(pt["date"])
        if pt["drawdown"] < 0:
            if spell_start is None:
                spell_start = prev_date if prev_date is not None else d
        else:
            if spell_start is not None and prev_date is not None:
                longest = max(longest, _days_between(spell_start, prev_date))
            spell_start = None
        prev_date = d
    # An unrecovered drawdown at the end of the run counts up to the last point.
    if spell_start is not None and prev_date is not None:
        longest = max(longest, _days_between(spell_start, prev_date))
    return longest


def _avg_trade_duration_days(trades: List[Dict[str, Any]]) -> float:
    """Mean ``bars_held`` across trades (treated as days for the daily engine)."""
    if not trades:
        return 0.0
    return _mean([float(t.get("bars_held", 0) or 0) for t in trades])


def _exposure_time(trades: List[Dict[str, Any]], n_points: int) -> float:
    """Approx % of bars with at least one position open (sum of bars_held / total bars).

    A coarse v1 proxy (true per-bar position-count tracking is a later refinement); capped
    at 100% so the column stays in range.
    """
    if n_points <= 0:
        return 0.0
    held = sum(int(t.get("bars_held", 0) or 0) for t in trades)
    return min(held / n_points * 100.0, 100.0)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def _iso(value: Any) -> Optional[str]:
    """ISO-format a date/datetime; pass through strings; ``None`` -> ``None``."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _parse_date(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _days_between(a: Any, b: Any) -> float:
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return 0.0
    return abs((db - da).days)
