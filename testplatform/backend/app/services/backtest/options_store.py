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


def price_source_spot(price_source: Any, split_basis: Any = None
                      ) -> Callable[[str, date], Optional[float]]:
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

    AS-TRADED (plan Part E2). The option store's strikes and premiums are as traded; the
    close is split-adjusted. With ``split_basis`` (an ``option_split_basis.RunSplitBasis`` —
    every real options run passes one, see ``build_options_run``) the close is multiplied by
    the as-traded factor OF THE CLOSE'S OWN DATE, so the greeks are inverted against the spot
    the contract actually traded against. ``None`` is the identity, for fixture callers whose
    symbols have no splits.
    """
    if split_basis is None:
        def spot(underlying: str, on: date) -> Optional[float]:
            return price_source.close_asof(underlying, on)
        return spot

    def spot(underlying: str, on: date) -> Optional[float]:
        dated = price_source.close_asof_dated(underlying, on)
        if dated is None:
            return None
        px, px_day = dated
        return px * split_basis.factor(underlying, px_day)
    return spot


def spot_scope(config: Dict[str, Any], split_basis: Any = None) -> str:
    """The identity of what this run's price source will answer for a (symbol, date).

    Deliberately the SAME tuple ``price_source.evict_memo_if_working_set_changed`` keys the
    OHLCV memo on — universe, interval, window, warmup — because that is exactly the set of
    inputs over which ``AsOfPriceSource.close_asof`` is a pure function. The parquet reader
    caches its greeks overlay at worker level and those greeks are inverted against these
    closes; see ``ParquetOptionsProvider.__init__`` for the wrong number this prevents.

    With a ``split_basis`` the spot is the close x the as-traded factor, so the factor's
    inputs join the key (``RunSplitBasis.digest`` -- the per-symbol calendar splits the cache
    holds and its basis date). A re-fetched FMP file or calendar that moves a factor is then a
    cache MISS, never an overlay inverted against the old basis. Without one the key is
    exactly what it was, so every run on record keeps its cache identity.
    """
    base = (
        tuple(sorted(config.get("enabled_instruments") or [])),
        config.get("execution_interval", "1d"),
        str(config.get("start_date")), str(config.get("end_date")),
        int(config.get("warmup_days") or 0),
    )
    if split_basis is None:
        return repr(base)
    return repr(base + (("as_traded", split_basis.digest()),))


def build_options_run(config: Dict[str, Any], *, price_source: Any, ohlcv_provider: Any):
    """``(options_provider, split_basis)`` for a run -- ``(None, None)`` on an equity-only run.

    THE ONE PLACE A REAL OPTIONS RUN IS WIRED (``run_daily_backtest``). An options run ALWAYS
    gets a verified ``RunSplitBasis`` for its universe (plan Part E): the reader's greeks spot
    and the account's option spot, intrinsic, settlement and cover all convert through it.
    Building it REFUSES (``SplitBasisRefused``) when any universe symbol's basis cannot be
    stated -- before the first bar, not mid-run. An equity-only run never reaches it, so it
    reads no calendar and no extra file.
    """
    if not config.get("options_cache_db"):
        return None, None
    # EXPLICIT, no "1d" default: the split basis is only defined for the daily clock
    # (``build_run_split_basis`` refuses an intraday interval), so a config that dropped the key
    # must be refused rather than assumed daily. Every production config builder states it
    # (daily_backtest_handler payload, rerun_handler, the GA trial config, the launcher).
    if "execution_interval" not in config:
        raise ValueError("an OPTIONS run must state 'execution_interval'; it is absent from "
                         "this run config")
    from .option_split_basis import build_run_split_basis
    basis = build_run_split_basis(config.get("enabled_instruments") or [], ohlcv_provider,
                                  interval=config["execution_interval"])
    return build_options_provider(config, price_source=price_source, split_basis=basis), basis


def build_options_provider(config: Dict[str, Any], *, price_source: Any, split_basis: Any = None):
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
        # NO E4 GUARD on this store, and its greeks are NOT in the as-traded basis: they were
        # inverted at BUILD time (fetch_options) against the FMP close of the day, which is
        # split-ADJUSTED, so on a symbol with a later split every stored delta/iv is wrong by
        # the split. ``split_basis`` still converts everything the ACCOUNT computes (spot for
        # strike selection, intrinsic, settlement, cover), but delta-based selection and the
        # stored greeks here are unconverted. Deliberately not refused: every sqlite option
        # number on record came from this path. Use the thetadata store for split-affected
        # universes (plan Part E; the stage-1 grid reads thetadata).
        from .options_provider import HistoricalOptionsProvider
        return HistoricalOptionsProvider(config["options_cache_db"])
    from .parquet_options_provider import ParquetOptionsProvider
    root = config.get("options_parquet_root") or default_options_parquet_root(store)
    rate = config.get("options_risk_free_rate")
    return ParquetOptionsProvider(
        root, spot_source=price_source_spot(price_source, split_basis),
        risk_free_rate=default_options_risk_free_rate() if rate is None else float(rate),
        spot_scope=spot_scope(config, split_basis),
        basis_guard=split_basis is not None,
        basis_guard_split_dates=None if split_basis is None else _split_dates_of(split_basis))


def _split_dates_of(split_basis: Any):
    """``symbol -> ex-dates`` of the run's verified split basis (the guard's forced checks)."""
    def dates(symbol: str):
        b = split_basis.basis_of(symbol)
        return () if b is None else tuple(s.date for s in b.splits)
    return dates
