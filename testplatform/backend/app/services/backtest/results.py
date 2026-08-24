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
load-bearing keys (the handler validates fail-early before calling here). That includes
``config["account_settings"]["commission_per_trade"]`` -- it is NOT optional and it is NOT a
top-level key; reading it with a ``.get(...) or 0.0`` silently priced the intraday-drawdown
refinement at zero cost on every run.
"""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# Import the metric-coercion helpers from the lightweight ``metrics_utils`` module, NOT from
# ``backtest_handler`` (the legacy ML path), which top-imports the tsai/torch/darts training
# stack (~7s of startup) that the expert backtest never uses. See metrics_utils for details.
from app.services.backtest.metrics_utils import _safe_float, _safe_duration_days
from ba2_common.logger import logger


# Trading days per year — the standard convention used by backtesting.py for annualisation.
_TRADING_DAYS_PER_YEAR = 252
_PROFIT_FACTOR_CAP = 999.99
# Floor for the calmar_ratio denominator (percentage points). A near-zero-but-nonzero
# max_drawdown (e.g. a handful of quick, mostly-winning trades whose daily-bar equity curve
# never dips) divides annualized_return by almost nothing, producing an absurd calmar_ratio for
# a genuinely modest real return. A true zero-drawdown run already short-circuits to 0.0 via the
# `if max_drawdown else` guard below; this floor catches the tiny-nonzero case that guard misses.
# 1% is a conservative floor: real strategies essentially never hold a full percentage point of
# drawdown headroom, so this only suppresses the degenerate thin-sample case, not genuine edge.
_MIN_DRAWDOWN_FLOOR_PCT = 1.0

# Worker-process-level cache of each symbol's full-window 5-minute OHLCV frame, used by the
# intraday drawdown refinement (mirrors options_provider.py's _WORKER_CHAIN_CACHE /
# _WORKER_BAR_CACHE pattern). A GA trial-serving process handles MANY trials sequentially over
# its life, and the universe/date-range is the same across every trial in a job -- reading and
# filtering a symbol's multi-year 5-minute parquet cache (tens of thousands of rows) on every
# trial's every flagged trade was the dominant cost of this refinement (measured: ~15-30x
# slower per trial on a trade-heavy individual). Caching the loaded frame ONCE per symbol, on
# first request, and reusing it for the rest of the worker's life turns "N reads per trial" into
# "<= len(universe) reads for the whole job". Bounded (not unbounded) for the same reason
# options_provider.py's caches are: a remote worker's process pool is long-lived across many
# different optimization jobs touching different universes.
_WORKER_5M_BARS_CACHE_MAX = int(os.getenv("BT_5M_BARS_CACHE_MAX", "300"))
_WORKER_5M_BARS_CACHE: "OrderedDict[Tuple[str, Any, Any], Any]" = OrderedDict()


def _finite(value: Any, what: str, default: Optional[float] = None) -> float:
    """Finite-or-RAISE float coercion — the strict counterpart of ``_safe_float``.

    ``_safe_float`` maps NaN/Inf to a default (0.0 for almost every metric). That is exactly
    wrong at the boundaries where a NaN means "this run computed nonsense": it converts a broken
    equity point into "flat at the initial capital", a broken trade P&L into "a scratch trade",
    and a broken risk metric into "zero risk" — so a NaN run scores as FLAWLESS (drawdown 0 ->
    calmar_ratio == annualised_return) and, being a good-looking result, is never scrutinised.

    Zero and negative are LEGITIMATE and pass through untouched (a wiped-out equity curve, a
    scratch trade, a losing book). ``None`` maps to ``default`` when one is supplied (an absent
    optional field), and raises when it is not.
    """
    if value is None:
        if default is not None:
            return float(default)
        raise ValueError(f"{what} is None; a missing value cannot be scored")
    try:
        result = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{what} is not numeric: {value!r}") from e
    if not math.isfinite(result):
        raise ValueError(
            f"{what} is not finite ({value!r}). The run produced nonsense and is rejected "
            f"rather than coerced to a harmless-looking number."
        )
    return result


def clear_worker_5m_bars_cache() -> None:
    """Drop every cached 5-minute frame (test isolation / explicit reset)."""
    _WORKER_5M_BARS_CACHE.clear()


def _get_5m_bars_cached(provider: Any, symbol: str, start_date: Any, end_date: Any) -> Any:
    """Worker-cached ``provider.get_ohlcv_data(symbol, ..., interval="5m")`` for the whole
    [start_date, end_date] window -- see the cache docstring above."""
    key = (symbol, start_date, end_date)
    df = _WORKER_5M_BARS_CACHE.get(key)
    if df is not None:
        _WORKER_5M_BARS_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return df
    df = provider.get_ohlcv_data(symbol, start_date=start_date, end_date=end_date, interval="5m")
    _WORKER_5M_BARS_CACHE[key] = df
    while len(_WORKER_5M_BARS_CACHE) > _WORKER_5M_BARS_CACHE_MAX:
        _WORKER_5M_BARS_CACHE.popitem(last=False)
    return df


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
    # Strict (finite-or-raise): a NaN equity point used to be swapped for ``initial``, i.e. a
    # broken bar silently became "the account was flat here" — which is what let a NaN run come
    # out looking riskless. Zero/negative equity is legitimate and still passes.
    equity_curve = [
        {"date": _iso(s["date"]),
         "equity": _finite(s["net_liquidating_value"], "equity_curve point")}
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

    refine_drawdown_fn = _build_refine_drawdown_fn(account, config)
    metrics = _compute_metrics(
        equity_curve, drawdown_curve, trades, initial, final, config, refine_drawdown_fn,
    )
    metrics["equity_curve"] = equity_curve
    metrics["drawdown_curve"] = drawdown_curve
    metrics["trades"] = trades
    # See BacktestAccount.snapshot_equity / DailyBacktestEngine.run: the sim stops early the
    # moment net_liquidating_value hits zero, since anything simulated past that point is
    # meaningless. strategy_fitness.compute_fitness reads this to invalidate the trial instead
    # of scoring it on the (already-clamped-at-0) numbers a real-money account could never
    # actually produce.
    metrics["account_wiped_out"] = bool(getattr(account, "_wiped_out", False))
    # Positions still OPEN at the end of the run. total_trades counts CLOSED round-trips, so a
    # buy-and-hold (no exit rule) shows 0 trades while equity still moves (entry commission +
    # the held position's mark-to-market). Surfacing these explains "0 trades but P&L changed".
    metrics["open_positions"] = _open_positions(account)
    return metrics


def _build_refine_drawdown_fn(account: Any, config: Dict[str, Any]) -> Optional[Any]:
    """Build the ``refine_drawdown_fn(trades, max_drawdown) -> max_drawdown`` closure that
    ``_compute_metrics`` calls, wiring ``intraday_drawdown.refine_max_drawdown``'s
    dependency-injected callables to REAL data sources: the account's own daily price source
    (``_price``) for daily bar lows / underlying prices, its options cache (``_options.cache``)
    for delta-at-entry, and the shared FMP OHLCV provider's 5-minute bars for the intraday
    re-pricing window. Returns None (skip refinement entirely) if the account doesn't expose
    the private attributes this needs (e.g. a lightweight test stub) -- this is purely a
    refinement layer, never a hard dependency.
    """
    price = getattr(account, "_price", None)
    options = getattr(account, "_options", None)
    if price is None or options is None:
        return None
    cache = getattr(options, "cache", None)
    if cache is None:
        return None
    # ``commission_per_trade`` lives under ``account_settings`` (the BacktestAccount's resolved
    # config -- see daily_backtest_handler._build_config), NEVER at the top level. Reading it as
    # ``config.get("commission_per_trade") or 0.0`` therefore hit the ``or 0.0`` on EVERY run,
    # so the intraday-drawdown refinement always priced its worst case as if trading were free:
    # a less-negative worst-case P&L -> a smaller estimated dip -> an understated max_drawdown
    # and an OVERSTATED calmar_ratio. Explicit indexing (house rule: no defaults on config) so a
    # missing key fails the run loudly instead of silently zeroing a cost.
    commission = float(config["account_settings"]["commission_per_trade"])
    # get_provider() constructs a FRESH provider instance every call (no internal caching) --
    # build it ONCE per backtest here, not once per flagged trade inside _bars_5m_between.
    from ba2_providers import get_provider

    ohlcv_5m_provider = get_provider("ohlcv", "fmp")
    window_start = config.get("start_date")
    window_end = config.get("end_date")

    def _bars_5m_for_symbol(symbol: str):
        # Worker-process-level cache (see _get_5m_bars_cached / _WORKER_5M_BARS_CACHE above):
        # the universe + date window is the same across every trial in a job, so this turns
        # "re-read this symbol's multi-year parquet cache on every trial" into "read it once
        # per worker, for the life of the process".
        return _get_5m_bars_cached(ohlcv_5m_provider, symbol, window_start, window_end)

    def _daily_bar_low(symbol: str, dt: Any) -> Optional[float]:
        bar = price.bar_at(symbol, dt)
        return bar.get("low") if bar else None

    def _prior_daily_bar_low(symbol: str, dt: Any) -> Optional[float]:
        bar = price.prev_bar(symbol, dt)
        return bar.get("low") if bar else None

    def _underlying_price_at(symbol: str, dt: Any) -> Optional[float]:
        return price.close_at(symbol, dt)

    def _delta_at_entry(underlying: str, contract: str, dt: Any) -> Optional[float]:
        # Route through options_provider's OWN worker-cached chain history (bisect over an
        # already-loaded-once-per-underlying structure) instead of OptionsHistoryCache's raw
        # methods, which open a fresh sqlite3 connection on every call. The backtest's normal
        # option pricing/entry path already populates this cache for every underlying this
        # trial touches, so by the time refinement runs it's typically already warm.
        from app.services.backtest.options_provider import _chain_history

        as_of = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)
        hist = _chain_history(cache.db_path, underlying)
        snapshot = hist.latest_as_of(as_of)
        if snapshot is None:
            return None
        for row in hist.by_asof.get(snapshot, []):
            if row.get("occ_symbol") == contract:
                return row.get("delta")
        return None

    def _bars_5m_between(symbol: str, entry: Any, exit_: Any) -> List[Dict[str, Optional[float]]]:
        df = _bars_5m_for_symbol(symbol)
        if df is None or df.empty or entry is None or exit_ is None:
            return []
        window = df[(df["Date"] >= entry) & (df["Date"] <= exit_)]
        if window.empty:
            return []
        return [{"Low": row["Low"], "High": row["High"]} for _, row in window.iterrows()]

    def _refine(trades: List[Dict[str, Any]], max_drawdown: float) -> float:
        from app.services.backtest.intraday_drawdown import refine_max_drawdown

        # `trades` here carries entry_time/exit_time as ISO STRINGS (_trade_row already ran
        # them through _iso() for JSON/DB storage) -- every price-source lookup below needs
        # real datetimes, so parse them back once per trade rather than in every callable.
        parsed_trades = [
            {**t, "entry_time": _parse_date(t.get("entry_time")), "exit_time": _parse_date(t.get("exit_time"))}
            for t in trades
        ]
        return refine_max_drawdown(
            parsed_trades,
            max_drawdown,
            equity_at=lambda dt: getattr(account, "_equity_at", lambda _dt: None)(dt),
            daily_bar_low=_daily_bar_low,
            prior_daily_bar_low=_prior_daily_bar_low,
            delta_at_entry=_delta_at_entry,
            underlying_price_at=_underlying_price_at,
            bars_5m_between=_bars_5m_between,
            commission_per_trade=commission,
        )

    return _refine


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
        # Strict: the equity points are already validated finite, so a non-finite drawdown here
        # would mean the arithmetic itself broke — reject rather than record a clean-looking 0.0
        # (which reads as "no drawdown at all" and inflates calmar_ratio).
        out.append({"date": pt["date"], "drawdown": _finite(dd, "drawdown_curve point")})
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
    # Strict (finite-or-raise) on the money/price/quantity fields: a NaN P&L used to become 0.0,
    # i.e. an invented SCRATCH trade — indistinguishable from a real flat round-trip, and it
    # quietly improves win_rate's denominator, profit_factor, expectancy and SQN. A genuine 0.0
    # (a real scratch) and a genuine negative are legitimate and still pass. ``default=0.0`` is
    # kept ONLY for the fields the per-FILL fallback rows legitimately do not carry (a filled
    # order has no exit/pnl yet); the presence of a NON-FINITE value always raises.
    return {
        "symbol": trade.get("symbol"),
        "entry_time": _iso(entry_time),
        "exit_time": _iso(trade.get("exit_time")),
        "direction": direction,
        "entry_price": _finite(entry_price, "trade.entry_price", default=0.0),
        "exit_price": _finite(trade.get("exit_price"), "trade.exit_price", default=0.0),
        "size": _finite(trade.get("size", trade.get("qty")), "trade.size", default=0.0),
        # Contract multiplier (100 for an option, 1 for equity). ``default=1.0`` covers the
        # per-FILL fallback rows, equities and trade blobs persisted before the round-trip
        # recorder published it — 1 is the correct no-op for all three.
        "multiplier": _finite(trade.get("multiplier"), "trade.multiplier", default=1.0),
        "pnl": _finite(trade.get("pnl"), "trade.pnl", default=0.0),
        "pnl_pct": _finite(trade.get("pnl_pct"), "trade.pnl_pct", default=0.0),
        "bars_held": int(trade.get("bars_held", 0) or 0),
        "exit_reason": trade.get("exit_reason", "unknown"),
        # Only set for option legs (passed through unchanged, no frontend consumer today) --
        # lets the intraday_drawdown refinement look up delta/underlying bars per trade.
        "contract_symbol": trade.get("contract_symbol"),
        "underlying_symbol": trade.get("underlying_symbol"),
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
    refine_drawdown_fn: Optional[Any] = None,
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
    if refine_drawdown_fn is not None and dd_values:
        # Best-effort: a daily-bar equity curve can hide a real intraday dip for a
        # single-bar-held option trade, or one whose exit day made a new low vs. the day
        # before -- see intraday_drawdown.py. Only ever makes max_drawdown MORE negative.
        # Gated on dd_values (a curve exists at all), NOT on max_drawdown being nonzero --
        # an exact-zero daily-bar drawdown is exactly the case this refinement exists to catch
        # (a quick option trade whose entry/exit bars never register a dip on the daily curve).
        try:
            max_drawdown = refine_drawdown_fn(trades, max_drawdown)
        except Exception as e:  # noqa: BLE001 -- refinement must never fail the backtest
            logger.debug(f"intraday drawdown refinement failed, using daily-only figure: {e}")
    neg_dd = [d for d in dd_values if d < 0]
    avg_drawdown = (sum(neg_dd) / len(neg_dd)) if neg_dd else 0.0
    max_dd_duration = _max_drawdown_duration_days(drawdown_curve)
    # Floor-based denominator applies whenever there IS an equity curve (dd_values non-empty),
    # including an exact-zero recorded drawdown -- NOT just the near-zero-but-nonzero case. A
    # genuinely clean, profitable run should be ranked via the 1% floor like a near-zero one,
    # not zeroed out entirely (that made every zero-drawdown trial tie at calmar=0.0 regardless
    # of how profitable it was, which defeated calmar-based ranking for thin/quiet strategies
    # such as options). Only a truly empty curve (no data at all) falls back to 0.0.
    calmar = (annualized_return / max(abs(max_drawdown), _MIN_DRAWDOWN_FLOOR_PCT)) if dd_values else 0.0

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

    # --- profit cap (optimization robustness) ------------------------------
    # A single once-in-a-lifetime winner (e.g. a sub-$1 stock that 90x'd and never closed) can be
    # ~97% of P&L and dominate the fitness, so the GA overfits to one lucky, non-reproducible
    # trade. TWO complementary caps build the ADJUSTED return/calmar (raw metrics untouched; with
    # neither cap set the adjusted values equal the raw ones exactly):
    #   1. ``profit_cap_pct`` — caps EACH trade's gain at that % of the capital deployed in it
    #      (cost basis = entry_price x size). Stops a low-priced name's huge %-move from counting.
    #   2. ``profit_share_cap_pct`` — caps each trade's gain at that % of the run's NET profit, so
    #      no single trade contributes more than (say) 25% of total return even if its %-on-basis
    #      is modest. A trade can pass cap #1 (508% of a $14k basis = $69k) yet still be 60% of the
    #      book's profit; cap #2 is what bounds THAT. Applied as a single pass against the net
    #      profit AFTER cap #1 (no iteration — capping the top trade shrinks net, which would spiral
    #      if re-applied; one pass deducts the dominant trade's excess and is stable/monotone).
    # The excess removed by both caps is deducted from final equity to get adjusted_*.
    cap_pct = config.get("profit_cap_pct")
    share_cap_pct = config.get("profit_share_cap_pct")
    has_basis_cap = cap_pct is not None and float(cap_pct) > 0
    has_share_cap = share_cap_pct is not None and float(share_cap_pct) > 0
    adjusted_total_return = total_return
    adjusted_annualized_return = annualized_return
    adjusted_calmar = calmar
    # Trade-quality metrics also distorted by a mega-winner — recomputed on the capped pnls below.
    adjusted_profit_factor = profit_factor
    adjusted_expectancy = expectancy
    adjusted_avg_trade = avg_trade
    adjusted_best_trade = best_trade
    adjusted_worst_trade = worst_trade
    adjusted_sqn = sqn
    if has_basis_cap or has_share_cap:
        cap_frac = (float(cap_pct) / 100.0) if has_basis_cap else None
        # Stage 1 — per-trade basis cap (cp1 == raw pnl when cap #1 is off).
        cp1: List[float] = []
        for t in trades:
            p = t.get("pnl") or 0.0
            if has_basis_cap:
                # Capital DEPLOYED, not the bare premium: an option's basis is
                # premium x contracts x CONTRACT MULTIPLIER. Without the multiplier the basis
                # was 100x too small for every option trade, so a nominal "2000%" cap (the
                # launcher default) actually truncated any option gain above 20% of the capital
                # really at risk -- a large, invisible PENALTY on exactly the runs the option
                # grid produces. Rows with no multiplier (equities, per-fill fallback rows,
                # legacy persisted blobs) use 1, which is an exact no-op.
                mult = t.get("multiplier") or 1.0
                cost = (t.get("entry_price") or 0.0) * (t.get("size") or 0.0) * mult
                cp1.append(min(p, cost * cap_frac) if (p > 0 and cost > 0) else p)
            else:
                cp1.append(p)
        # Stage 2 — portfolio-share cap: bound each trade at share% of NET profit after stage 1.
        # Only meaningful when the book is net-profitable; for a net-losing run "% of total return"
        # is undefined, so we skip it (leaving cp1 as the adjusted pnls).
        share_abs = None
        if has_share_cap:
            net_after_basis = sum(cp1)
            if net_after_basis > 0:
                share_abs = (float(share_cap_pct) / 100.0) * net_after_basis
        excess = 0.0
        adj_pnls: List[float] = []
        adj_pcts: List[float] = []
        for t, p1 in zip(trades, cp1):
            p = t.get("pnl") or 0.0
            cp = p1
            if share_abs is not None and cp > share_abs:
                cp = share_abs
            excess += max(0.0, p - cp)
            adj_pnls.append(cp)
            # pnl_pct is equity-relative (pnl / equity_at_entry); scale it by the same factor the
            # dollar pnl was capped so best/worst/expectancy reflect the capped trade.
            pct = t.get("pnl_pct") or 0.0
            adj_pcts.append(pct * (cp / p) if p else pct)
        adj_final = final - excess
        adjusted_total_return = ((adj_final - initial) / initial * 100.0) if initial else 0.0
        adjusted_annualized_return = _annualized_return(initial, adj_final, years)
        adjusted_calmar = (adjusted_annualized_return / max(abs(max_drawdown), _MIN_DRAWDOWN_FLOOR_PCT)) if dd_values else 0.0
        # Trade-quality on capped pnls (mirrors the raw block above).
        a_wins = [p for p in adj_pnls if p > 0]
        a_losses = [p for p in adj_pnls if p < 0]
        a_gp, a_gl = sum(a_wins), abs(sum(a_losses))
        if a_gl > 0:
            adjusted_profit_factor = a_gp / a_gl
        elif a_gp > 0:
            adjusted_profit_factor = _PROFIT_FACTOR_CAP
        else:
            adjusted_profit_factor = 0.0
        if adjusted_profit_factor > 999:
            adjusted_profit_factor = _PROFIT_FACTOR_CAP
        adjusted_expectancy = (sum(adj_pcts) / total_trades) if total_trades else 0.0
        adjusted_avg_trade = adjusted_expectancy
        adjusted_best_trade = max(adj_pcts) if adj_pcts else 0.0
        adjusted_worst_trade = min(adj_pcts) if adj_pcts else 0.0
        adjusted_sqn = _sqn(adj_pnls)

    return {
        # Basic trade metrics
        "total_trades": total_trades,
        # Trade FREQUENCY: trades / calendar-year of the run. Used by the optional fitness
        # trade-frequency scale (``fitness_trade_scale``) so the GA can down-weight statistically
        # thin (few-trade) configs that win on a handful of lucky trades.
        "avg_trades_per_year": round(_finite((total_trades / years) if years else 0.0,
                                             "avg_trades_per_year"), 2),
        "fitness_trade_scale": bool(config.get("fitness_trade_scale")),
        # Cap (trades/year) for the scale: avg_trades_per_year is clamped to this before scaling so
        # the GA is not rewarded for over-trading. None -> the fitness default (100 = factor <= 1.0).
        "fitness_trade_scale_cap": config.get("fitness_trade_scale_cap"),
        # Trades/year that earns FULL credit (factor 1.0) for the scale. None -> fitness default 100.
        "fitness_trade_scale_target": config.get("fitness_trade_scale_target"),
        # Optional win-rate fitness factor (2 * win_rate_fraction; see strategy_fitness.py).
        "fitness_win_rate_factor": bool(config.get("fitness_win_rate_factor")),
        # Optional SPREAD-STRESS level (bps). When > 0 the GA additionally scores the run as if
        # the spread were this much wider and ranks on the worse of the two, selecting against
        # genomes whose per-trade edge barely clears the modelled cost. Echoed here (like the
        # cap/scale knobs above) so compute_fitness reads it off `results` and every path --
        # local pool, remote worker, top-N re-run -- picks it up from the config that crossed
        # the wire, with no separate plumbing to keep in sync.
        "stress_spread_bps": _safe_float(config.get("stress_spread_bps") or 0.0),
        # Optional ROBUSTNESS-adjusted fitness. Same echo mechanism, same reason: when set, the GA
        # ranks on base_fitness x concentration x monte-carlo x spread factors instead of the raw
        # metric, and BOTH numbers plus every component land in the results blob (fitness_raw /
        # fitness_robust / robustness) so a persisted row can always be decomposed. Scores are NOT
        # comparable across this flag.
        "robust_fitness": bool(config.get("robust_fitness")),
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        # THE METRIC BOUNDARY. Every number below goes through ``_finite`` (finite-or-RAISE),
        # NOT ``_safe_float`` (NaN/Inf -> 0.0). Coercing here is what let a NaN run score as a
        # flawless one: a NaN max_drawdown became 0.0, and calmar_ratio = annualised_return /
        # max(|0.0|, 1.0) = the full annualised return with no risk charged at all. Legitimate
        # zeros and negatives are untouched — only a value that is not a number at all raises.
        "win_rate": round(_finite(win_rate, "win_rate"), 2),
        # Return metrics
        "total_return": round(_finite(total_return, "total_return"), 2),
        "annualized_return": round(_finite(annualized_return, "annualized_return"), 2),
        "buy_hold_return": round(_finite(buy_hold_return, "buy_hold_return"), 2),
        # Profit-capped (per-trade) variants — equal to the raw values when no cap is set. The
        # optimizer uses the adjusted fitness so one lucky mega-winner can't dominate the search.
        "profit_cap_pct": (float(cap_pct) if has_basis_cap else None),
        "profit_share_cap_pct": (float(share_cap_pct) if has_share_cap else None),
        "adjusted_total_return": round(_finite(adjusted_total_return, "adjusted_total_return"), 2),
        "adjusted_annualized_return": round(_finite(adjusted_annualized_return, "adjusted_annualized_return"), 2),
        "adjusted_calmar_ratio": round(_finite(adjusted_calmar, "adjusted_calmar_ratio"), 2),
        "adjusted_profit_factor": round(_finite(adjusted_profit_factor, "adjusted_profit_factor"), 2),
        "adjusted_expectancy": round(_finite(adjusted_expectancy, "adjusted_expectancy"), 2),
        "adjusted_avg_trade": round(_finite(adjusted_avg_trade, "adjusted_avg_trade"), 2),
        "adjusted_best_trade": round(_finite(adjusted_best_trade, "adjusted_best_trade"), 2),
        "adjusted_worst_trade": round(_finite(adjusted_worst_trade, "adjusted_worst_trade"), 2),
        "adjusted_sqn": round(_finite(adjusted_sqn, "adjusted_sqn"), 2),
        # Risk metrics
        "sharpe_ratio": round(_finite(sharpe, "sharpe_ratio"), 2),
        "sortino_ratio": round(_finite(sortino, "sortino_ratio"), 2),
        "calmar_ratio": round(_finite(calmar, "calmar_ratio"), 2),
        "volatility": round(_finite(volatility, "volatility"), 2),
        # Drawdown metrics
        "max_drawdown": round(_finite(max_drawdown, "max_drawdown"), 2),
        "avg_drawdown": round(_finite(avg_drawdown, "avg_drawdown"), 2),
        "max_drawdown_duration": round(_finite(max_dd_duration, "max_drawdown_duration"), 1),
        # Trade quality metrics
        "profit_factor": round(_finite(profit_factor, "profit_factor"), 2),
        "expectancy": round(_finite(expectancy, "expectancy"), 2),
        "sqn": round(_finite(sqn, "sqn"), 2),
        "avg_trade": round(_finite(avg_trade, "avg_trade"), 2),
        "best_trade": round(_finite(best_trade, "best_trade"), 2),
        "worst_trade": round(_finite(worst_trade, "worst_trade"), 2),
        # Duration metrics
        "avg_trade_duration": round(_finite(avg_trade_duration, "avg_trade_duration"), 1),
        "exposure_time": round(_finite(exposure_time, "exposure_time"), 2),
        # Equity metrics
        "final_equity": round(_finite(final, "final_equity", default=initial), 2),
        "equity_peak": round(_finite(equity_peak, "equity_peak", default=initial), 2),
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
    """Geometric annualised return (%) over the actual ``years`` of calendar time elapsed.

    ``final <= 0`` (equity wiped out, or driven under water by the profit-cap adjustment)
    returns **-100.0**, NOT 0.0. The geometric formula is undefined for a non-positive
    final value, but the ANSWER is not ambiguous: everything was lost, i.e. -100%/yr. The
    old 0.0 made a total loss indistinguishable from BREAKEVEN, and since
    ``consistent_annual_return`` ranks directly on this number, that inverted the ordering
    of the worst configs: PremiumSeller's smoke run scored its single worst individual
    (-34.1% real return, adjusted_total_return -141%, i.e. adjusted final equity -$8,198)
    at fitness 0.0 — ABOVE a merely -26% config — and five heavy losers tied at 0.0 as
    "best". High trade counts made it worse, since more winners means more capped "excess"
    subtracted from final equity.

    ``initial <= 0`` / ``years <= 0`` still return 0.0: those are genuinely undefined
    inputs (no capital deployed, no time elapsed), not a loss.
    """
    if initial <= 0 or years <= 0:
        return 0.0
    if final <= 0:
        return -100.0
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


# ---------------------------------------------------------------------------
# Yearly breakdown (BT detail "Yearly Breakdown" tab)
# ---------------------------------------------------------------------------
_PARTIAL_YEAR_MIN_DAYS = 182.62  # ~6 months; matches strategy_fitness._CAR_PARTIAL_YEAR_MIN_DAYS

def yearly_breakdown(
    equity_curve: List[Dict[str, Any]],
    drawdown_curve: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Per-calendar-year return/drawdown/sharpe/trades, computed from the FULL (non-downsampled)
    curves — the UI's chart curves are LTTB-thinned to ~2000 points for display, which would
    silently drop the true per-year peak/trough, so this must run on the raw DB columns, not
    ``Backtest.to_dict()``'s output.

    Year boundaries mirror ``strategy_fitness._calendar_year_returns``: anchored on the first
    equity point and the last point of every calendar year, with a <~6-month partial year at the
    run's start/end merged into its neighbor (so a 2-week stub can't appear as its own "year").
    Drawdown per year is the min of the (already running-peak-relative) drawdown_curve within
    that year's date range -- NOT reset to a fresh peak at year start, consistent with how the
    single overall max_drawdown metric is computed. Sharpe uses the same _step_returns/_sharpe/
    _periods_per_year helpers as the overall metric, applied to just that year's equity points.
    Trades are attributed to the year their EXIT falls in (a trade's P&L is realized at exit).
    """
    pts = []
    for p in equity_curve or []:
        d = _parse_date(p.get("date"))
        e = p.get("equity")
        if d is None or e is None:
            continue
        pts.append((d, float(e)))
    if len(pts) < 2:
        return []

    # Anchor points: first point (opening) + last point of every calendar year.
    anchors = [pts[0]]
    for i in range(1, len(pts)):
        if pts[i][0].year != pts[i - 1][0].year:
            anchors.append(pts[i - 1])
    anchors.append(pts[-1])
    anchors = [a for i, a in enumerate(anchors) if i == 0 or a[0] != anchors[i - 1][0]]
    if len(anchors) < 2:
        return []

    segs = [[anchors[i - 1][0], anchors[i][0]] for i in range(1, len(anchors))]
    min_secs = _PARTIAL_YEAR_MIN_DAYS * 86400.0
    if len(segs) >= 2 and (segs[0][1] - segs[0][0]).total_seconds() < min_secs:
        segs[1][0] = segs[0][0]
        segs.pop(0)
    if len(segs) >= 2 and (segs[-1][1] - segs[-1][0]).total_seconds() < min_secs:
        segs[-2][1] = segs[-1][1]
        segs.pop()

    dd_pts = [(d, p["drawdown"]) for p in (drawdown_curve or [])
              if (d := _parse_date(p.get("date"))) is not None and p.get("drawdown") is not None]

    out: List[Dict[str, Any]] = []
    for start_dt, end_dt in segs:
        seg_pts = [(d, e) for d, e in pts if start_dt <= d <= end_dt]
        if len(seg_pts) < 2:
            continue
        seg_equities = [e for _d, e in seg_pts]
        start_eq, end_eq = seg_equities[0], seg_equities[-1]
        return_pct = ((end_eq / start_eq - 1.0) * 100.0) if start_eq else 0.0

        seg_years = max((end_dt - start_dt).total_seconds() / (365.25 * 86400.0), 1e-9)
        periods_per_year = _periods_per_year(len(seg_equities), seg_years)
        step_returns = _step_returns(seg_equities)
        sharpe = _sharpe(step_returns, periods_per_year)

        seg_dd = [dd for d, dd in dd_pts if start_dt <= d <= end_dt]
        max_dd = min(seg_dd) if seg_dd else 0.0

        seg_trades = [t for t in (trades or [])
                      if (exit_dt := _parse_date(t.get("exit_time"))) is not None
                      and start_dt <= exit_dt <= end_dt]
        n_trades = len(seg_trades)
        wins = sum(1 for t in seg_trades if (t.get("pnl") or 0.0) > 0)
        win_rate = (wins / n_trades * 100.0) if n_trades else 0.0

        # Label the segment by whichever year contains most of its span (a merged partial-year
        # segment can straddle two calendar years).
        label_year = max({start_dt.year, end_dt.year}, key=lambda y: min(end_dt.year, y) - max(start_dt.year, y - 1))

        out.append({
            "year": label_year,
            "startDate": _iso(start_dt),
            "endDate": _iso(end_dt),
            "returnPct": round(_safe_float(return_pct), 2),
            "maxDrawdownPct": round(_safe_float(max_dd), 2),
            "sharpeRatio": round(_safe_float(sharpe), 2),
            "totalTrades": n_trades,
            "winRate": round(_safe_float(win_rate), 2),
        })
    return out
