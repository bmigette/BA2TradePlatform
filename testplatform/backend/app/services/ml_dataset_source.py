"""The ``alpaca_hist`` dataset source of the dataset wizard (docs/plans/2026-10-27-custom-ml-models-design.md, section 1).

Two flavours, both read from the local cache only:

* ``alpaca_hist`` 1h / 15m: one symbol's regular-session bars and point-in-time feature groups
  (``ml_panel.ticker_frame``; the wizard's ``panel_features``);
* ``alpaca_hist`` 1d (ML exploration v2, Phase 7): the DAILY point-in-time feature store
  (``ml_features.build_features``) with the chosen groups ``daily_groups`` (t, mtf, m, x, mac, f, ag, at, i, s), the
  coverage mask at ``daily_min_share`` and the store's ``filled`` flag. Cross-sectional values (group x, the breadth of
  group m) are taken over ``daily_universe``, the tickers created together; it is stored with the dataset so a
  regeneration or a duplicate rebuilds the same numbers.

Two properties of the daily source are choices of the wizard, not of the store, and a reader of a result should know
them:

* the coverage mask is decided per calendar YEAR over the dataset's own span (a (group, year) holding less than
  ``daily_min_share`` complete rows is blanked and flagged). It is a completeness rule over the rows present, not a
  look-ahead on any value; a different span can mask a different set of years;
* the universe is the user's own ticker list (survivorship): ``daily_universe`` is exactly the tickers created together,
  the cross-sectional ranks (group x) and the market breadth (group m) are taken over them and over nothing else. A
  breadth over fewer than ``DAILY_BREADTH_MIN_UNIVERSE`` tickers means nothing, so that column is dropped then.

Every dataset build path (create, batch, regenerate, duplicate, update) goes through ``fetch_ohlcv_frame`` so they all
produce the same frame.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from app.services import ml_panel

ALPACA_HIST_PROVIDER = "alpaca_hist"
ALPACA_HIST_TIMEFRAMES = ("1h", "15m")
#: optional feature groups on top of the always-on technical group (the wizard's ``panel_features``)
PANEL_FEATURE_GROUPS = ("daily", "market", "fundamentals")


#: the daily point-in-time feature store source (timeframe of an ``alpaca_hist`` dataset built from it)
DAILY_TIMEFRAME = "1d"
#: group id -> one-line description, in the store's order (``ml_features.GROUPS``; a test pins that they match)
DAILY_GROUP_DESCRIPTIONS: Dict[str, str] = {
    "t": "Technical: returns, moving-average distances, RSI / ADX / SAR, ATR, volume z-scores (scale-free)",
    "mtf": "Weekly multi-timeframe: weekly trend and momentum, visible only once the week has ended",
    "m": "Market: SPY / QQQ / IWM / TLT / GLD returns, relative strength vs SPY and the sector ETF, breadth of the batch (dropped below 5 tickers)",
    "x": "Cross-sectional: the symbol's rank and z-score among the batch's tickers (needs at least 5 tickers; your list, so survivorship)",
    "mac": "Macro: FRED first-release series (VIX, yields, spreads, CPI, payrolls, unemployment), as published",
    "f": "Fundamentals: earnings surprise and growth, margins, balance-sheet ratios, after the filing date",
    "ag": "Analyst grades: net upgrades and number of graders over the last 30 / 90 days",
    "at": "Analyst price targets: consensus target vs price, target revisions, number of analysts",
    "i": "Insiders: net open-market insider buying and selling over the market cap",
    "s": "Market structure: support / resistance distance, break of structure and change of character",
}
DAILY_GROUP_IDS: Tuple[str, ...] = tuple(DAILY_GROUP_DESCRIPTIONS)
#: coverage mask threshold of a (group, year) when the request names none: the store's own (``ensure_feature_store``)
DAILY_DEFAULT_MIN_SHARE = 0.7
#: the breadth of group m (share of the universe above its 50-session average) is dropped for a universe thinner than this
DAILY_BREADTH_MIN_UNIVERSE = 5
BREADTH_COLUMN = "m_breadth_sma50"
#: the keys of ``generation_config`` (and of the build config) that carry the daily options
DAILY_KEYS: Tuple[str, ...] = ("daily_groups", "daily_min_share", "daily_universe")
#: flag columns of the store that the dataset keeps but a model must not read as a feature (see job_handler)
DAILY_FLAG_COLUMNS: Tuple[str, ...] = ("filled",)


def is_alpaca_hist(provider_name: Optional[str]) -> bool:
    return (provider_name or "").lower() == ALPACA_HIST_PROVIDER


def is_daily_source(provider_name: Optional[str], timeframe: Optional[str]) -> bool:
    """The dataset is built from the daily point-in-time feature store."""
    return is_alpaca_hist(provider_name) and timeframe == DAILY_TIMEFRAME


def daily_request_from(config: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The daily options of a ``generation_config`` / build config as one dict, None when it carries none."""
    if not config or not config.get("daily_groups"):
        return None
    return {k: config[k] for k in DAILY_KEYS}


def daily_column_prefixes() -> Tuple[str, ...]:
    """Column prefixes of the daily store's groups (and of the ``<prefix>missing`` coverage flags)."""
    from app.services import ml_features
    return tuple(spec.prefix for spec in ml_features.GROUPS.values())


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
    if timeframe == DAILY_TIMEFRAME:
        if feats:
            raise ValueError(f"panel_features {feats} are the intraday groups of the {ALPACA_HIST_PROVIDER} "
                             f"{list(ALPACA_HIST_TIMEFRAMES)} source; the '{DAILY_TIMEFRAME}' source takes daily_groups")
    elif timeframe not in ALPACA_HIST_TIMEFRAMES:
        raise ValueError(f"the {ALPACA_HIST_PROVIDER} data provider offers {list(ALPACA_HIST_TIMEFRAMES)} "
                         f"(panel_features) or '{DAILY_TIMEFRAME}' (daily_groups), got {timeframe!r}")
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


def validate_daily_request(provider_name: Optional[str], timeframe: str, daily_groups: Optional[Iterable[str]],
                           daily_min_share: Optional[float], ticker: str,
                           universe: Optional[Iterable[str]]) -> Dict[str, Any]:
    """The normalised daily options as ``generation_config`` keys (``DAILY_KEYS``), ``{}`` for any other source; ValueError
    when the request cannot be built.

    ``daily_groups`` (non-empty, known, no duplicates) and the ``daily_min_share`` coverage threshold in (0, 1] (absent:
    ``DAILY_DEFAULT_MIN_SHARE``, the store's own) belong to the alpaca_hist 1d source only. ``universe`` is the set of
    tickers created together (None: just ``ticker``); ``ticker`` is always part of it. Group x ranks the symbol among
    the universe, so it needs at least ``ml_features.X_MIN_MEMBERS`` tickers."""
    groups = list(daily_groups or [])
    if not is_daily_source(provider_name, timeframe):
        if groups:
            raise ValueError(f"daily_groups need the {ALPACA_HIST_PROVIDER} data provider with timeframe "
                             f"'{DAILY_TIMEFRAME}', got {provider_name!r} / {timeframe!r}")
        if daily_min_share is not None:
            raise ValueError(f"daily_min_share needs the {ALPACA_HIST_PROVIDER} data provider with timeframe "
                             f"'{DAILY_TIMEFRAME}', got {provider_name!r} / {timeframe!r}")
        return {}
    if not groups:
        raise ValueError(f"the {ALPACA_HIST_PROVIDER} data provider with timeframe '{DAILY_TIMEFRAME}' needs daily_groups "
                         f"(the daily feature store; known {list(DAILY_GROUP_IDS)}); the intraday source offers "
                         f"{list(ALPACA_HIST_TIMEFRAMES)} with panel_features")
    bad = [g for g in groups if g not in DAILY_GROUP_DESCRIPTIONS]
    if bad:
        raise ValueError(f"unknown daily_groups {bad}; known {list(DAILY_GROUP_IDS)}")
    if len(set(groups)) != len(groups):
        raise ValueError(f"duplicate daily_groups in {groups}")
    share = DAILY_DEFAULT_MIN_SHARE if daily_min_share is None else float(daily_min_share)
    if not 0.0 < share <= 1.0:
        raise ValueError(f"daily_min_share must be in (0, 1], got {daily_min_share!r}")
    members = list(dict.fromkeys(list(universe or []) + [ticker]))
    if "x" in groups:
        from app.services import ml_features
        if len(members) < ml_features.X_MIN_MEMBERS:
            raise ValueError(
                f"daily group x ranks the symbol among its universe and needs at least {ml_features.X_MIN_MEMBERS} "
                f"tickers created together (the batch mode of the wizard); got {len(members)}: {members}")
    return {"daily_groups": groups, "daily_min_share": share, "daily_universe": members}


# ------------------------------------------------------------------------------------------ the daily frame
_MEMO_LOCK = threading.Lock()
_MEMO: Dict[str, Any] = {}


def clear_daily_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()


def _cache_stamp(cache_root: Path) -> Optional[Tuple[int, int]]:
    """(size, mtime) of SPY's daily file, the cache end: a cache refresh invalidates the memoised universe build."""
    p = Path(cache_root) / "AlpacaHistoryOHLCVProvider" / "SPY_1d.parquet"
    try:
        st = p.stat()
    except FileNotFoundError:
        return None                       # build_features refuses without SPY; the stamp only keys the memo
    return st.st_size, st.st_mtime_ns


def _source_stamp(universe: Tuple[str, ...], groups: Tuple[str, ...], cache_root: Path) -> str:
    """(size, mtime) of every source file the build of ``groups`` reads - the daily bars of the universe and of the context
    ETFs, the FMP histories of its group, the FRED series, the metric store, the market-condition manifests - as the
    feature store's own request key computes them (``ml_features._input_fingerprints``)."""
    from app.services import ml_features as F
    from app.services import ml_universe
    uses_sector = any(F.GROUPS[g].needs_sector for g in groups)
    context = ["SPY"] + (sorted(set(F.MARKET_ETFS) | set(F.SECTOR_ETF.values())) if uses_sector else [])
    load = list(dict.fromkeys(list(universe) + context))
    fp = F._input_fingerprints(load, list(universe), list(groups), cache_root, ml_universe.store_dir_for(cache_root),
                               uses_store=uses_sector)
    return json.dumps(fp, sort_keys=True, default=str)


def _universe_features(universe: Tuple[str, ...], first_day: date, last_day: date, groups: Tuple[str, ...],
                       min_share: float, cache_root: Path) -> pd.DataFrame:
    """The coverage-masked feature store of the whole ``universe`` over ``[first_day, last_day]``, built ONCE for all the
    datasets of a batch (they run in a thread pool with the same request): the last build is kept, and reused only while
    every source file it read is unchanged (``_source_stamp``)."""
    from app.services import ml_features as F
    key = (universe, first_day, last_day, groups, min_share, str(cache_root), _source_stamp(universe, groups, cache_root))
    with _MEMO_LOCK:
        if _MEMO.get("key") == key:
            return _MEMO["frame"]
        df = F.build_features(list(universe), first_day, last_day, list(groups), cache_root, sector_of="store")
        df = F.apply_coverage_mask(df, F.coverage_report(df, list(groups)), min_share)
        _MEMO["key"], _MEMO["frame"] = key, df
        return df


def daily_frame(ticker: str, first_day: date, last_day: date, request: Mapping[str, Any]) -> pd.DataFrame:
    """One ticker's wizard frame from the daily feature store: ``Date`` (the session), Open / High / Low / Close /
    Volume (the cached daily bars), ``filled`` (the bar is a fabricated flat fill), then the feature columns of the
    chosen groups with the coverage mask applied (a thin (group, year) is NaN with ``<prefix>missing`` = 1).

    Only listed sessions are kept. ``label_bar_filled`` is left out: the daily targets (``ml_targets.daily_targets``) are
    already NaN where their entry or exit bar is not a real print. The coverage mask is per calendar year over the
    built span (a completeness rule, not a value look-ahead) and the universe is the user's own ticker list
    (survivorship); see the module docstring. ``m_breadth_sma50`` is dropped for a universe below
    ``DAILY_BREADTH_MIN_UNIVERSE`` tickers."""
    from ba2_common.config import CACHE_FOLDER
    from app.services import ml_features as F
    cache_root = Path(CACHE_FOLDER)
    feats = _universe_features(tuple(request["daily_universe"]), first_day, last_day, tuple(request["daily_groups"]),
                               float(request["daily_min_share"]), cache_root)
    f = feats.xs(ticker, level="symbol")
    bars, _ = F.load_daily([ticker], first_day, last_day, cache_root, cache_end=F.last_cached_bar("SPY", cache_root))
    b = bars[ticker]
    if not b.index.equals(f.index):
        raise ValueError(f"{ticker}: the daily bars and the feature store do not cover the same sessions")
    keep = f["listed"].to_numpy(dtype=bool)
    feature_cols = [c for c in f.columns if c not in ("listed", "ended", "filled", "label_bar_filled")]
    if len(request["daily_universe"]) < DAILY_BREADTH_MIN_UNIVERSE:
        # the breadth of one to four tickers is that ticker's own trend, not a market breadth
        feature_cols = [c for c in feature_cols if c != BREADTH_COLUMN]
    out = pd.DataFrame({"Date": f.index, "Open": b["open"].to_numpy(), "High": b["high"].to_numpy(),
                        "Low": b["low"].to_numpy(), "Close": b["close"].to_numpy(), "Volume": b["volume"].to_numpy(),
                        "filled": f["filled"].to_numpy(dtype=bool)})
    out = pd.concat([out, f[feature_cols].reset_index(drop=True)], axis=1)
    return out[keep].reset_index(drop=True)


def fetch_ohlcv_frame(provider_name: Optional[str], ticker: str, timeframe: str, start_date: datetime,
                      end_date: datetime, panel_features: Optional[Iterable[str]] = None,
                      fundamentals_config: Optional[Dict[str, Any]] = None,
                      daily: Optional[Mapping[str, Any]] = None) -> pd.DataFrame:
    """The OHLCV frame (columns Date, Open, High, Low, Close, Volume) a dataset build starts from; empty when the
    provider has no data. ``alpaca_hist`` reads the local cache and also carries the feature columns: the intraday
    panel groups (1h / 15m) or, at 1d, the daily store's groups (``daily``: the ``DAILY_KEYS`` of the dataset's
    generation_config, see ``daily_request_from``)."""
    from app.api.datasets import get_ohlcv_provider
    from app.services.dataset_handler import INTERVAL_MAP
    name = provider_name or "yfinance"
    feats = validate_panel_request(name, timeframe, panel_features, fundamentals_config)
    d = daily or {}
    req = validate_daily_request(name, timeframe, d.get("daily_groups"), d.get("daily_min_share"), ticker,
                                 d.get("daily_universe"))
    if req:
        return daily_frame(ticker, start_date.date(), end_date.date(), req)
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
