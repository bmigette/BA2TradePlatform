"""Hermetic, deterministic provider fixtures for the Phase 2 Task 6 GATE.

The e2e / reproducibility tests must run the REAL clean experts (FMPEarningsDrift,
FMPInsiderClusterBuy) against a FIXED provider cache so a run is:

  * hermetic  — zero network, no FMP API key, no wall-clock dependence;
  * a "fixed cache" — the SAME inputs every time, so two runs with the same params +
    seed produce a byte-identical equity curve (the reproducibility gate).

These fixtures implement exactly the provider surface the two experts + the price source
touch (verified against the installed packages):

  OHLCV (category ``ohlcv``):
    ``get_ohlcv_data(symbol, start_date=None, end_date=None, interval='1d', ...) -> DataFrame``
    columns ``Date, Open, High, Low, Close, Volume`` (the canonical capitalised set the
    AsOfPriceSource + LiveProviderBundle.price_at_date expect). The fixture serves a
    point-in-time-safe slice (rows with ``Date <= end_date``).

  Fundamentals details (category ``fundamentals_details``) — EarningsDrift:
    ``get_past_earnings(symbol, frequency, end_date, lookback_periods, format_type)``
    -> ``{"earnings": [ {report_date, reported_eps, estimated_eps, surprise_percent}, ... ]}``
    point-in-time-filtered to ``report_date <= end_date`` and most-recent-first (so the
    expert's ``earnings[0]`` is the latest report as of the bar).

  Insider (category ``insider``) — InsiderClusterBuy:
    ``get_insider_transactions(symbol, end_date, lookback_days, as_of, format_type)``
    -> ``{"transactions": [...], "start_date": iso, "end_date": iso}`` with only the
    purchases inside the ``[end_date - lookback_days, end_date]`` window (point-in-time).

  Indicators (category ``indicators``, name ``pandas``) — built lazily from the OHLCV
    provider; only used if a run selects ``sizing_mode='risk_atr'`` (the clean experts use
    the default ``notional`` sizing, so this is a no-op fall-through here, but wiring it
    keeps ``get_provider`` total).

A single ``fixture_get_provider(category, name, **kwargs)`` callable routes every category
to the right fixture instance, so it drops straight into both seams:
  * ``TradeConditions.set_provider_resolver(fixture_get_provider)`` (the engine bundle), and
  * monkeypatch ``ba2_providers.get_provider`` (the handler's price-source construction).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Deterministic price series (one planted "drift up" symbol).
# ---------------------------------------------------------------------------
# A 90-calendar-day daily series for AAPL with a smooth upward drift, generated from a
# fixed arithmetic rule (NO randomness) so it is byte-identical run-to-run. The window the
# tests trade over (e.g. 2024-02-01..2024-03-15) sits AFTER the planted earnings report +
# insider cluster (mid-January) so a BUY signal is live across the run.
_BASE_START = date(2024, 1, 2)
_N_BARS = 90
_START_PRICE = 100.0
_DAILY_DRIFT = 0.40  # +$0.40 close-to-close => a clean, monotone uptrend (deterministic)


def _business_days(start: date, n: int) -> List[date]:
    """``n`` consecutive weekdays (Mon-Fri) from ``start`` — a simple daily trading clock."""
    out: List[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            out.append(d)
        d += timedelta(days=1)
    return out


def _build_price_rows(symbol: str) -> List[Dict[str, Any]]:
    """A deterministic OHLCV bar list for ``symbol`` (monotone uptrend, fixed rule)."""
    days = _business_days(_BASE_START, _N_BARS)
    rows: List[Dict[str, Any]] = []
    close = _START_PRICE
    for d in days:
        open_ = close
        close = round(open_ + _DAILY_DRIFT, 4)
        high = round(max(open_, close) + 0.50, 4)
        low = round(min(open_, close) - 0.50, 4)
        rows.append(
            {
                "Date": d,
                "Open": round(open_, 4),
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": 1_000_000,
            }
        )
    return rows


# The fixed multi-symbol price cache. AAPL is the planted-signal symbol; MSFT is a second
# symbol with NO earnings/insider signal (so the universe is genuinely multi-asset but only
# AAPL trades — proving the universe filter + per-symbol decisioning).
_PRICE_ROWS: Dict[str, List[Dict[str, Any]]] = {
    "AAPL": _build_price_rows("AAPL"),
    "MSFT": _build_price_rows("MSFT"),
}


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date") and callable(getattr(value, "date")):
        return value.date()
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    raise TypeError(f"Cannot normalise {value!r} ({type(value)}) to a date")


# ---------------------------------------------------------------------------
# OHLCV provider
# ---------------------------------------------------------------------------
class FixtureOHLCVProvider:
    """Serves the fixed price cache as a point-in-time DataFrame slice."""

    def get_ohlcv_data(
        self,
        symbol: str,
        start_date: Any = None,
        end_date: Any = None,
        interval: str = "1d",
        use_cache: bool = True,
        max_cache_age_hours: int = 24,
        lookback_days: int = 30,
    ) -> pd.DataFrame:
        rows = _PRICE_ROWS.get(symbol, [])
        lo = _as_date(start_date)
        hi = _as_date(end_date)
        sliced = [
            r
            for r in rows
            if (lo is None or r["Date"] >= lo) and (hi is None or r["Date"] <= hi)
        ]
        return pd.DataFrame(sliced)


# ---------------------------------------------------------------------------
# Fundamentals details (earnings) — EarningsDrift signal
# ---------------------------------------------------------------------------
# A planted positive EPS surprise (+20%) reported on 2024-01-15 for AAPL. With the trade
# window starting ~2 weeks later and ``max_days_since_report`` 30 (the expert default 10 is
# too tight; the e2e payload sets a wider window), the report stays "fresh" across the run.
_EARNINGS: Dict[str, List[Dict[str, Any]]] = {
    "AAPL": [
        {
            "report_date": "2024-01-15",
            "reported_eps": 1.20,
            "estimated_eps": 1.00,
            "surprise_percent": 20.0,
        },
        {
            "report_date": "2023-10-16",
            "reported_eps": 0.95,
            "estimated_eps": 0.95,
            "surprise_percent": 0.0,
        },
    ],
    # MSFT: a stale / no-surprise report -> HOLD (no trade).
    "MSFT": [
        {
            "report_date": "2023-10-20",
            "reported_eps": 2.00,
            "estimated_eps": 2.00,
            "surprise_percent": 0.0,
        }
    ],
}


class FixtureFundamentalsDetailsProvider:
    """Serves the planted earnings, point-in-time filtered (report_date <= end_date)."""

    def get_past_earnings(
        self,
        symbol: str,
        frequency: str = "quarterly",
        end_date: Any = None,
        lookback_periods: int = 1,
        format_type: str = "dict",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        hi = _as_date(end_date)
        rows = _EARNINGS.get(symbol, [])
        visible = [
            r
            for r in rows
            if hi is None or _as_date(r["report_date"]) <= hi
        ]
        # Most-recent-first so the expert's earnings[0] is the latest as-of report.
        visible = sorted(visible, key=lambda r: _as_date(r["report_date"]), reverse=True)
        return {"earnings": visible[: max(lookback_periods, 1)]}


# ---------------------------------------------------------------------------
# Insider provider — InsiderClusterBuy signal
# ---------------------------------------------------------------------------
# A planted insider cluster: 3 distinct insiders buying AAPL on 2024-01-12..2024-01-18,
# $100k each ($300k combined > the $200k default). With ``lookback_days`` wide enough to
# reach back to mid-January from the trade window, the cluster is live across the run.
_INSIDER_TX: Dict[str, List[Dict[str, Any]]] = {
    "AAPL": [
        {"insider_name": "Alice Officer", "transaction_type": "P-Purchase",
         "value": 100_000.0, "transaction_date": "2024-01-12"},
        {"insider_name": "Bob Director", "transaction_type": "P-Purchase",
         "value": 100_000.0, "transaction_date": "2024-01-15"},
        {"insider_name": "Carol CFO", "transaction_type": "P-Purchase",
         "value": 100_000.0, "transaction_date": "2024-01-18"},
    ],
    # MSFT: a single sale -> no cluster -> HOLD (no trade).
    "MSFT": [
        {"insider_name": "Dan Insider", "transaction_type": "S-Sale",
         "value": 50_000.0, "transaction_date": "2024-01-10"},
    ],
}


class FixtureInsiderProvider:
    """Serves the planted insider transactions inside the as-of lookback window."""

    def get_insider_transactions(
        self,
        symbol: str,
        end_date: Any = None,
        lookback_days: Optional[int] = None,
        as_of: Any = None,
        format_type: str = "dict",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        hi = _as_date(end_date) or date.today()
        window = int(lookback_days) if lookback_days else 30
        lo = hi - timedelta(days=window)
        rows = _INSIDER_TX.get(symbol, [])
        visible = [
            r
            for r in rows
            if lo <= _as_date(r["transaction_date"]) <= hi
        ]
        return {
            "transactions": visible,
            "start_date": datetime(lo.year, lo.month, lo.day).isoformat(),
            "end_date": datetime(hi.year, hi.month, hi.day).isoformat(),
        }


# ---------------------------------------------------------------------------
# The single routing callable (drops into both seams)
# ---------------------------------------------------------------------------
def make_fixture_get_provider() -> Callable[..., Any]:
    """Return a ``get_provider(category, name, **kw)`` backed by fresh fixture instances.

    Each call to this factory returns a NEW closure over NEW provider instances (the
    providers are stateless over the fixed cache, so reuse is also fine — fresh instances
    just keep two runs maximally independent for the reproducibility test).

    The ``indicators`` category delegates to the REAL ``ba2_providers.get_provider``
    captured HERE, at factory-build time — BEFORE the e2e harness monkeypatches the module
    attribute to this very closure. Capturing the genuine function avoids the closure
    recursing into itself (the indicators path is only reached for ``risk_atr`` sizing; the
    clean experts use the default ``notional`` sizing, so it is a no-op fall-through for the
    GATE tests, but it must not self-recurse if ever called).
    """
    from ba2_providers import get_provider as _real_get_provider  # genuine, pre-patch

    ohlcv = FixtureOHLCVProvider()
    details = FixtureFundamentalsDetailsProvider()
    insider = FixtureInsiderProvider()

    mapping: Dict[str, Any] = {
        "ohlcv": ohlcv,
        "fundamentals_details": details,
        "fundamentals_overview": details,
        "insider": insider,
    }

    def fixture_get_provider(category: str, name: str = "", **kwargs: Any) -> Any:
        if category == "indicators":
            prov = kwargs.get("ohlcv_provider") or ohlcv
            # Real pandas indicator calc over the fixture OHLCV (risk_atr only).
            return _real_get_provider("indicators", "pandas", ohlcv_provider=prov)
        if category in mapping:
            return mapping[category]
        raise KeyError(
            f"fixture_get_provider: no fixture for category={category!r} name={name!r}"
        )

    return fixture_get_provider


# Trading-window helpers the tests share (kept here so the cache + window stay in one place).
#
# The window starts right after the planted signals (earnings 2024-01-15, insider cluster
# 2024-01-12..18) and runs ~26 bars. With ``max_days_since_report`` / ``lookback_days`` set
# wide (60) in the payload, BOTH signals stay live across the whole run so AAPL is held the
# entire window (a clean, monotone winning trade) while MSFT (no signal) never trades.
TRADE_START = datetime(2024, 1, 19)
TRADE_END = datetime(2024, 2, 23)
UNIVERSE = ["AAPL", "MSFT"]

# The wide decision windows that keep the planted signals fresh across the whole run.
EARNINGS_DRIFT_SETTINGS = {
    "surprise_min_pct": 5.0,
    "max_days_since_report": 60,
    "expected_profit_percent": 8.0,
}
INSIDER_CLUSTER_SETTINGS = {
    "lookback_days": 60,
    "min_insiders": 3,
    "min_total_value": 200_000.0,
    "expected_profit_percent": 10.0,
}
