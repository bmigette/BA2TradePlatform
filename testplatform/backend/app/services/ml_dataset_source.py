"""The ``alpaca_hist`` dataset source of the dataset wizard (docs/plans/2026-10-27-custom-ml-models-design.md, section 1).

One symbol's regular-session H1 / M15 bars and point-in-time feature groups, read from the local cache only
(``ml_panel.ticker_frame``). Every dataset build path (create, batch, regenerate, duplicate, update) goes through
``fetch_ohlcv_frame`` so they all produce the same frame.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from app.services import ml_panel

ALPACA_HIST_PROVIDER = "alpaca_hist"
ALPACA_HIST_TIMEFRAMES = ("1h", "15m")
#: optional feature groups on top of the always-on technical group (the wizard's ``panel_features``)
PANEL_FEATURE_GROUPS = ("daily", "market", "fundamentals")


def is_alpaca_hist(provider_name: Optional[str]) -> bool:
    return (provider_name or "").lower() == ALPACA_HIST_PROVIDER


def validate_panel_request(provider_name: Optional[str], timeframe: str, panel_features: Optional[Iterable[str]],
                           fundamentals_config: Optional[Dict[str, Any]] = None) -> List[str]:
    """The normalised ``panel_features`` list, or ValueError when the request cannot be built.

    The panel features exist only for the alpaca_hist source; that source offers 1h and 15m only and never carries
    today's fundamentals broadcast (``live_only_snapshot``), which is not point in time."""
    feats = list(panel_features or [])
    bad = [g for g in feats if g not in PANEL_FEATURE_GROUPS]
    if bad:
        raise ValueError(f"unknown panel_features {bad}; known {list(PANEL_FEATURE_GROUPS)}")
    if not is_alpaca_hist(provider_name):
        if feats:
            raise ValueError(f"panel_features need the {ALPACA_HIST_PROVIDER} data provider, got {provider_name!r}")
        return []
    if timeframe not in ALPACA_HIST_TIMEFRAMES:
        raise ValueError(f"the {ALPACA_HIST_PROVIDER} data provider offers {list(ALPACA_HIST_TIMEFRAMES)} only, "
                         f"got {timeframe!r}")
    if fundamentals_config and fundamentals_config.get("live_only_snapshot") is True:
        raise ValueError("live_only_snapshot (today's fundamentals broadcast to every row) is not point in time "
                         f"and cannot be combined with the {ALPACA_HIST_PROVIDER} data provider; use the "
                         "'fundamentals' panel feature")
    return feats


def panel_frame(ticker: str, timeframe: str, first_day: date, last_day: date,
                panel_features: Iterable[str]) -> pd.DataFrame:
    """``ml_panel.ticker_frame`` with the bar end as a ``Date`` column (the wizard datasets' layout)."""
    from ba2_common.config import CACHE_FOLDER
    groups = ["technical"] + list(panel_features)
    frame = ml_panel.ticker_frame(ticker, timeframe, first_day, last_day, groups, CACHE_FOLDER)
    return frame.reset_index().sort_values("Date").reset_index(drop=True)


def fetch_ohlcv_frame(provider_name: Optional[str], ticker: str, timeframe: str, start_date: datetime,
                      end_date: datetime, panel_features: Optional[Iterable[str]] = None,
                      fundamentals_config: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """The OHLCV frame (columns Date, Open, High, Low, Close, Volume) a dataset build starts from; empty when the
    provider has no data. ``alpaca_hist`` reads the local cache and also carries the panel feature columns."""
    from app.api.datasets import get_ohlcv_provider
    from app.services.dataset_handler import INTERVAL_MAP
    name = provider_name or "yfinance"
    feats = validate_panel_request(name, timeframe, panel_features, fundamentals_config)
    if is_alpaca_hist(name):
        return panel_frame(ticker, timeframe, start_date.date(), end_date.date(), feats)
    data_points = get_ohlcv_provider(name).get_data(
        symbol=ticker, start_date=start_date, end_date=end_date, interval=INTERVAL_MAP.get(timeframe, "1d"))
    if not data_points:
        return pd.DataFrame()
    df = pd.DataFrame([{
        'Date': dp.timestamp, 'Open': dp.open, 'High': dp.high,
        'Low': dp.low, 'Close': dp.close, 'Volume': dp.volume
    } for dp in data_points])
    return df.sort_values('Date').reset_index(drop=True)
