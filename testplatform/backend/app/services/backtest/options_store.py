"""WHICH option store a backtest reads — one explicit choice, defaulting to sqlite.

There are now TWO readers behind one seam (see ``parquet_options_provider``'s module
docstring for the seam's four-method contract):

  ``sqlite``   ``options_provider.HistoricalOptionsProvider`` over the Alpaca-built
               ``OptionsHistoryCache``. THE DEFAULT, and it must stay the default: every
               backtest number on record was produced against it, so a run that does not ask
               for anything else must be bit-identical to one launched before this module
               existed.
  ``parquet``  ``parquet_options_provider.ParquetOptionsProvider`` over the TastyTrade/dxfeed
               parquet tree written by ``tools/warm_options_history.py``.

THE STORE DETERMINES THE VENDOR, AND THE VENDOR DETERMINES THE HISTORY FLOOR. That chain is
the whole reason this selection is not a private detail of ``run_daily_backtest``:
``daily_backtest_handler.validate_options_window`` refuses a window the SERVING vendor cannot
cover (Alpaca 2024-01-18 measured, dxfeed/TastyTrade 2022-10-01), and a floor naming a vendor
the store does not hold is precisely the lie that seam exists to prevent. ``STORE_VENDOR``
below is the single place the two are tied together.

RESOLUTION ORDER, most specific first:
  1. ``config["options_store"]`` — the payload/optimizer key, forwarded per trial.
  2. ``BACKTEST_OPTIONS_STORE`` — env, for a whole worker/job without threading a key through
     every launcher.
  3. ``"sqlite"``.
An unrecognised value RAISES rather than falling back: a typo must not silently reinstate the
default store and produce numbers nobody asked for.
"""
from __future__ import annotations

import os
import pathlib
from datetime import date
from typing import Any, Callable, Dict, Optional

SQLITE = "sqlite"
TASTYTRADE = "tastytrade"
THETADATA = "thetadata"
OPTIONS_STORES = (SQLITE, TASTYTRADE, THETADATA)

#: Superseded names, accepted on input and normalised. NOT offered in ``OPTIONS_STORES``.
#:
#: ``parquet`` named the FILE FORMAT, which stopped identifying anything the moment ThetaData
#: became the second parquet-backed store. Stores are named for their VENDOR because the vendor
#: is what the name has to answer: whose history, and therefore which floor.
#:
#: The alias is load-bearing, not courtesy: every options optimization_config and Backtest on
#: record carries ``options_store: "parquet"``, and an unrecognised value RAISES here by design,
#: so dropping it would break re-runs, deploys and warm starts across the whole existing archive.
STORE_ALIASES = {"parquet": TASTYTRADE}

#: Store -> the vendor whose history it holds. Consumed by
#: ``daily_backtest_handler.backtest_options_provider``; the values must be keys of
#: ``ba2_providers.options.OPTIONS_HISTORY_PROVIDERS`` or the floor lookup raises.
STORE_VENDOR = {SQLITE: "alpaca", TASTYTRADE: "tastytrade", THETADATA: "thetadata"}

#: Store -> its sub-directory of CACHE_FOLDER. Each vendor writes its OWN tree and they must
#: never resolve to one directory: the two hold different contracts over different windows, and
#: the warmer already keeps them apart (tools/warm_options_history.py picks the directory from
#: ``--provider``). TastyTrade's comes from the WRITER's own constant rather than being retyped,
#: because a hand-copied directory name is how a reader drifts from its writer.
_STORE_DIRS = {TASTYTRADE: None, THETADATA: "ThetaDataOptionsProvider"}

#: Sub-directory of CACHE_FOLDER holding the parquet tree. Imported from the writer so the
#: reader can never drift from it.
_PARQUET_DIR_ENV = "BACKTEST_OPTIONS_PARQUET_ROOT"

#: The ONE declared default for the Black-Scholes rate, at the wiring boundary rather than in
#: the reader (which requires it explicitly). Taken from the cache BUILDER's own fallback so
#: greeks derived at read time from the parquet and greeks baked into the sqlite at build time
#: are inverted against the same assumption. A backtest is hermetic, so there is no per-day
#: FRED series here the way ``fetch_options.build_cache`` has one — rho is the smallest greek
#: for short-dated equity options, which is why the builder itself tolerates a flat rate.
_RATE_ENV = "BACKTEST_OPTIONS_RISK_FREE_RATE"


def resolve_options_store(config: Optional[Dict[str, Any]] = None) -> str:
    """The store this run reads. See the module docstring for the resolution order."""
    raw = None
    if config is not None:
        raw = config.get("options_store")
    if raw is None:
        raw = os.environ.get("BACKTEST_OPTIONS_STORE")
    store = str(raw).strip().lower() if raw else SQLITE
    store = STORE_ALIASES.get(store, store)      # 'parquet' -> 'tastytrade'; see STORE_ALIASES
    if store not in OPTIONS_STORES:
        raise ValueError(
            f"Unknown options store {raw!r}. Choose one of {list(OPTIONS_STORES)}. "
            f"(Refusing to fall back to {SQLITE!r}: a typo must not silently pick a store.)")
    return store


def default_options_parquet_root(store: str = TASTYTRADE) -> str:
    """Where *store*'s parquet tree lives.

    ``BACKTEST_OPTIONS_PARQUET_ROOT`` overrides the full path for whichever store is selected;
    otherwise ``<CACHE_FOLDER>/<ProviderDir>`` — ``TastyTradeOptionsProvider`` or
    ``ThetaDataOptionsProvider``, matching what ``OptionHistoryParquetStore`` and
    ``tools/warm_options_history.py`` write and what the chain viewer
    (``services/option_cache_reader``) reads.

    The two trees are SEPARATE and must stay so: they hold different contracts over different
    windows, and collapsing them would mix vendors inside one run — the same class of error as a
    floor naming a vendor the store does not hold.

    The directory is NOT created on demand — unlike the sqlite path, an absent parquet root means
    "no data", and creating an empty one would turn a loud ``OptionsCacheMiss`` into a silent
    zero-trade run.

    The default argument keeps the old no-argument call site working: before ThetaData there was
    only one parquet tree, and it was TastyTrade's.
    """
    explicit = os.environ.get(_PARQUET_DIR_ENV)
    if explicit:
        return explicit
    store = STORE_ALIASES.get(store, store)
    if store not in _STORE_DIRS:
        raise ValueError(
            f"{store!r} has no parquet tree (stores with one: {sorted(_STORE_DIRS)}).")
    provider_dir = _STORE_DIRS[store]
    if provider_dir is None:
        # Taken from the WRITER so the reader can never drift from it.
        from ba2_providers.options.parquet_store import PROVIDER_DIR
        provider_dir = PROVIDER_DIR
    import ba2_common.config as cfg
    return str(pathlib.Path(cfg.CACHE_FOLDER) / provider_dir)


def default_options_risk_free_rate() -> float:
    """Flat risk-free rate for read-time Black-Scholes inversion. See ``_RATE_ENV``."""
    explicit = os.environ.get(_RATE_ENV)
    if explicit:
        return float(explicit)
    from .fetch_options import _FALLBACK_RISK_FREE_RATE
    return float(_FALLBACK_RISK_FREE_RATE)


def price_source_spot(price_source: Any) -> Callable[[str, date], Optional[float]]:
    """``(underlying, bar_date) -> close`` over the run's ``AsOfPriceSource``.

    ``close_asof`` (last known close AT OR BEFORE the date, no clock required) rather than
    ``close_at``: the underlying can legitimately lack an exact bar on an option bar's date
    (half-days, the clock being the union of every symbol's timestamps), and forward-filling
    the last known close is what ``fetch_options`` did too (``_nearest_on_or_before``) when it
    inverted the sqlite store's greeks.

    NEVER LOOKAHEAD: it is only ever called with the date of a bar the reader has ALREADY
    clamped to <= the engine clock, and it returns a close at or before that date. On an
    intraday ``execution_interval`` the daily-midnight key resolves to the previous session's
    last bar rather than that day's close — staler, still causal, and immaterial to a greek.
    """
    def spot(underlying: str, on: date) -> Optional[float]:
        return price_source.close_asof(underlying, on)
    return spot


def spot_scope(config: Dict[str, Any]) -> str:
    """The identity of what this run's price source will answer for a (symbol, date).

    Deliberately the SAME tuple ``price_source.evict_memo_if_working_set_changed`` keys the
    OHLCV memo on — universe, interval, window, warmup — because that is exactly the set of
    inputs over which ``AsOfPriceSource.close_asof`` is a pure function. The parquet reader
    caches its greeks overlay at worker level and those greeks are inverted against these
    closes; see ``ParquetOptionsProvider.__init__`` for the wrong number this prevents.
    """
    return repr((
        tuple(sorted(config.get("enabled_instruments") or [])),
        config.get("execution_interval", "1d"),
        str(config.get("start_date")), str(config.get("end_date")),
        int(config.get("warmup_days") or 0),
    ))


def build_options_provider(config: Dict[str, Any], *, price_source: Any):
    """The run's option reader, or None when the run does not use options.

    ``config['options_cache_db']`` remains the OPTIONS-RUN FLAG for every store (it is what
    ``strategy_uses_options`` derives and what every launcher already forwards); for the
    parquet-backed stores it is not otherwise read — the tree comes from
    ``config['options_parquet_root']`` / ``default_options_parquet_root(store)``.

    The root is resolved FOR THE SELECTED STORE. Passing the store here is what keeps a
    ``thetadata`` run off the TastyTrade tree: both are parquet and both would load, so an
    un-keyed default would silently serve one vendor's contracts under the other's floor.
    """
    if not config.get("options_cache_db"):
        return None
    store = resolve_options_store(config)
    if store == SQLITE:
        from .options_provider import HistoricalOptionsProvider
        return HistoricalOptionsProvider(config["options_cache_db"])
    from .parquet_options_provider import ParquetOptionsProvider
    root = config.get("options_parquet_root") or default_options_parquet_root(store)
    rate = config.get("options_risk_free_rate")
    return ParquetOptionsProvider(
        root, spot_source=price_source_spot(price_source),
        risk_free_rate=default_options_risk_free_rate() if rate is None else float(rate),
        spot_scope=spot_scope(config))
