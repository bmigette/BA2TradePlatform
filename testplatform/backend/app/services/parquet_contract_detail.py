"""Contract detail (IV/greeks) for a saved run that read a PARQUET option store.

The trade popup shows, per leg, the IV/greeks the option store held for the contract at
the entry and exit. The sqlite store has them as columns. The parquet stores
(``tastytrade``, ``thetadata``) do NOT: ``ParquetOptionsProvider`` inverts every bar at read
time (Black-Scholes from the bar's own close against the run's underlying close, at the
run's risk-free rate), so the only faithful number is the one that inversion gives with the
RUN's inputs. This module rebuilds that reader, for one underlying at a time:

* **The same factory.** ``options_store.build_options_run`` (an as-traded run) or
  ``build_options_provider(split_basis=None)`` (a run from before the split basis), handed
  the run's recorded ``options_store`` / ``options_parquet_root`` / ``options_risk_free_rate``
  / ``execution_interval``.
* **The same spot.** An ``AsOfPriceSource`` over ``MemoizedOHLCVProvider(cached_only=True)``,
  bounded to the run's window (``start - warmup_days .. end``), holding the underlying's bars
  exactly as ``AsOfPriceSource.preload`` binds them (``read_window`` ->
  ``_ohlcv_arrays_from_df``). ``preload`` itself is not called: it flushes the process-wide
  bar cache and can publish derived arrays, and a popup may do neither.
* **The same inversion.** ``_Underlying.bar_dict`` -> ``greeks_tuple`` ->
  ``compute_iv_and_greeks``, over a PRIVATE overlay (``parquet_options_provider.
  read_only_overlay``) so the worker caches are neither read-through-built nor evicted.

WHICH SPOT BASIS THE RUN USED is read from its own results: every options run since the
as-traded split basis (plan Part E, 2026-09-23) records ``option_basis_guard`` (the E4 guard
exists exactly when the run has a split basis). Absent -> the run predates it and inverted
against the split-ADJUSTED close; that is reproduced, and the E4 parity check below decides
whether it may be shown.

A SECOND CHECK, PER BAR: the E4 guard (``option_basis_guard.BasisGuard``) compares the spot
used with the chain's own put-call-parity spot on that session. An as-traded run would have
refused on a mismatch, so a mismatch now means today's caches are not the run's; a
pre-split-basis run that mismatches computed its greeks against a spot off the chain's basis.
Either way the values are WITHHELD with the reason -- a plausible wrong greek is worse than
none.

READ-ONLY: no provider is fetched from, nothing is published, created or migrated; the
split-basis and parquet-byte memos it touches are in-memory only. Every limit is said in the
returned ``reason``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.services.backtest_trade_chart import ContractDetailRefused, _json_blob

logger = logging.getLogger(__name__)

#: The options-run flag ``build_options_provider`` requires (``options_cache_db``); for a
#: parquet store it is not otherwise read. A recorded path is used when there is one.
_OPTIONS_RUN_FLAG = "<recorded options run>"


@dataclass(frozen=True)
class ParquetRunInputs:
    """Everything ``build_options_provider`` reads for a parquet run, as the run recorded it."""

    store: str
    options_cache_db: str
    parquet_root: Optional[str]
    risk_free_rate: Optional[float]
    execution_interval: str
    warmup_days: int
    start: datetime
    end: datetime
    #: True: the run had a split basis (as-traded spot). False: it predates it.
    as_traded: bool

    def run_config(self, underlying: str) -> Dict[str, Any]:
        """The run config, narrowed to ONE underlying.

        Narrowing is exact for the greeks: the spot of X is X's close x X's own as-traded
        factor, and neither depends on the other universe symbols. It changes the overlay's
        ``spot_scope`` string, which only keys caches -- and this overlay is private anyway.
        """
        return {
            "options_cache_db": self.options_cache_db,
            "options_store": self.store,
            "options_parquet_root": self.parquet_root,
            "options_risk_free_rate": self.risk_free_rate,
            "execution_interval": self.execution_interval,
            "enabled_instruments": [underlying],
            "start_date": self.start,
            "end_date": self.end,
            "warmup_days": self.warmup_days,
        }


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def parquet_run_inputs(provenance: Any, backtest: Any) -> Tuple[Optional[ParquetRunInputs], Optional[str]]:
    """``(inputs, None)`` when the run's parquet reader can be rebuilt, else ``(None, reason)``.

    Nothing is defaulted that the run might have set differently: an unrecorded interval,
    warm-up or window refuses. The risk-free rate is the one input whose absence is the norm
    (no launcher records it); the run then resolved ``default_options_risk_free_rate()`` in
    its own process, which is reproduced -- unless THIS process overrides that default through
    the environment, in which case the run's value cannot be told and it refuses.
    """
    from app.services.backtest import options_store as ostore

    try:
        store = ostore.resolve_options_store({"options_store": provenance.store})
    except ValueError as exc:
        return None, str(exc)
    if store == ostore.SQLITE:
        return None, "the recorded store is sqlite, not a parquet store"

    interval = provenance.execution_interval
    if not interval:
        return None, ("its execution_interval is not recorded, and the underlying closes the "
                      "greeks were inverted against depend on it")
    if provenance.warmup_days is None:
        return None, ("its warmup_days is not recorded, so the run's price window cannot be "
                      "rebuilt")
    try:
        warmup_days = int(provenance.warmup_days)
    except (TypeError, ValueError):
        return None, f"its recorded warmup_days {provenance.warmup_days!r} is not a number"
    start = _as_datetime(getattr(backtest, "start_date", None))
    end = _as_datetime(getattr(backtest, "end_date", None))
    if start is None or end is None:
        return None, "the saved result carries no start/end date, so the run's window is unknown"

    rate = provenance.risk_free_rate
    if rate is not None:
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            return None, f"its recorded options_risk_free_rate {rate!r} is not a number"
    elif os.environ.get(ostore._RATE_ENV):
        return None, (f"it did not record its risk-free rate, and this process overrides the "
                      f"default through {ostore._RATE_ENV}, so the rate the run inverted at "
                      f"cannot be established")

    results = _json_blob(getattr(backtest, "results", None))
    if not isinstance(results, dict):
        return None, ("its results are not readable, so whether it priced on the as-traded "
                      "split basis cannot be told")
    if "option_basis_guard" in results:
        if not isinstance(results["option_basis_guard"], dict):
            return None, ("its results record no split-basis guard for a parquet reader, which "
                          "an as-traded run always has; the spot basis cannot be established")
        as_traded = True
    else:
        as_traded = False

    return ParquetRunInputs(
        store=store, options_cache_db=provenance.db_path or _OPTIONS_RUN_FLAG,
        parquet_root=provenance.parquet_root, risk_free_rate=rate,
        execution_interval=str(interval), warmup_days=warmup_days,
        start=start, end=end, as_traded=as_traded), None


@dataclass
class _UnderlyingContext:
    """One underlying's rebuilt reader: the run's provider (for its spot/rate/root) plus a
    private overlay over the published arrays."""

    provider: Any
    overlay: Any


class ParquetContractReader:
    """``latest_bar_on_or_before`` over a parquet store, with the RUN's greeks.

    The sqlite reader's contract (``OptionsHistoryCache.latest_bar_on_or_before``): the
    contract's latest bar dated <= the day, as a dict carrying ``date`` and the
    ``_CONTRACT_FIELDS`` keys, or None. ``_Underlying.latest_row_on_or_before`` is the clamp
    (the provider's ``get_bar`` is exact-date only).

    Setup is per underlying and memoised for the life of this reader (one popup request), so
    a transaction's legs and their entry/exit reads load the underlying once. A setup that
    cannot be faithful raises ``ContractDetailRefused`` with the reason, every time it is
    asked.
    """

    def __init__(self, inputs: ParquetRunInputs):
        self.inputs = inputs
        self._contexts: Dict[str, Any] = {}

    @property
    def db_path(self) -> str:
        """What ``contract_detail`` reports as the ``source``."""
        root = self.inputs.parquet_root or "the platform default tree"
        return f"{self.inputs.store} parquet store ({root}), greeks derived at read time"

    # -- setup --------------------------------------------------------------
    def _context(self, underlying: str) -> _UnderlyingContext:
        ctx = self._contexts.get(underlying)
        if ctx is None:
            try:
                ctx = self._build_context(underlying)
            except ContractDetailRefused as exc:
                ctx = exc
            self._contexts[underlying] = ctx
        if isinstance(ctx, ContractDetailRefused):
            raise ctx
        return ctx

    def _build_context(self, underlying: str) -> _UnderlyingContext:
        from ba2_common.core.split_basis import SplitBasisRefused
        from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

        from app.services.backtest.options_cache import OptionsCacheMiss
        from app.services.backtest.options_store import build_options_provider, build_options_run
        from app.services.backtest.parquet_options_provider import read_only_overlay
        from app.services.backtest.price_source import (
            AsOfPriceSource, BacktestCacheMiss, MemoizedOHLCVProvider,
        )

        inputs = self.inputs
        config = inputs.run_config(underlying)
        interval = inputs.execution_interval
        fetch_start = inputs.start - timedelta(days=inputs.warmup_days)
        # The run's OHLCV reader: MemoizedOHLCVProvider(FMPOHLCVProvider, cached_only=True).
        # In cached_only mode it uses the inner provider for its CLASS NAME (the cache
        # directory) and never calls it; constructing a real one would read the API key and
        # create the cache folder, so an uninitialised instance of the same class stands in.
        ohlcv = MemoizedOHLCVProvider(FMPOHLCVProvider.__new__(FMPOHLCVProvider), fetch_start,
                                      inputs.end, interval=interval, cached_only=True)
        ps = AsOfPriceSource(ohlcv_provider=ohlcv, interval=interval)
        try:
            # Exactly the arrays ``preload`` binds (read_window -> _ohlcv_arrays_from_df), for
            # the same [start - warmup, end] window, without its cache flush/publish.
            ps.load_bars_df(underlying, ohlcv.read_window(underlying, fetch_start, inputs.end,
                                                          interval))
        except BacktestCacheMiss:
            raise ContractDetailRefused(
                f"no cached {interval} FMP bars for {underlying} on this host, and the greeks "
                f"are inverted against them")
        try:
            if inputs.as_traded:
                provider, _basis = build_options_run(config, price_source=ps,
                                                     ohlcv_provider=ohlcv)
            else:
                provider = build_options_provider(config, price_source=ps, split_basis=None)
        except SplitBasisRefused as exc:
            raise ContractDetailRefused(
                f"the run's as-traded split basis for {underlying} cannot be rebuilt on this "
                f"host, so its spot cannot be reproduced: {exc}")
        except OptionsCacheMiss as exc:
            raise ContractDetailRefused(f"the run's option store is not on this host: {exc}")
        except ValueError as exc:
            raise ContractDetailRefused(f"the run's option reader cannot be rebuilt: {exc}")

        overlay = read_only_overlay(provider.root, underlying, provider.risk_free_rate)
        if overlay is None:
            raise ContractDetailRefused(
                f"the {inputs.store} option arrays for {underlying} are not built on this host "
                f"(tools/build_shared_arrays.py builds them); this read-only view does not "
                f"parse the store itself")
        return _UnderlyingContext(provider=provider, overlay=overlay)

    # -- the reader contract ---------------------------------------------------
    def latest_bar_on_or_before(self, occ_symbol: str, on_or_before: str) -> Optional[Dict[str, Any]]:
        from app.services.backtest.option_basis_guard import BasisGuard, OptionSpotBasisMismatch
        from app.services.backtest.parquet_options_provider import _underlying_of

        underlying = _underlying_of(occ_symbol)   # the provider's own routing (get_bar)
        ctx = self._context(underlying)
        u = ctx.overlay
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.latest_row_on_or_before(ci, date.fromisoformat(on_or_before).toordinal())
        if i < 0:
            return None
        bar_day = u.raw.date_of_ord[u.bar_ord_l[i]]
        spot_source = ctx.provider.spot_source

        # The E4 check on THIS bar's session. A fresh guard each time: a guard samples after
        # its first evaluable session, and every bar shown here must be checked.
        guard = BasisGuard(spot_source)
        try:
            guard.check(u, underlying, bar_day)
        except OptionSpotBasisMismatch as exc:
            if self.inputs.as_traded:
                raise ContractDetailRefused(
                    f"the run's as-traded spot, rebuilt from today's caches, is off the chain's "
                    f"own basis on {bar_day.isoformat()} -- the run would have refused that, so "
                    f"the caches changed since and its greeks cannot be reproduced ({exc})")
            raise ContractDetailRefused(
                f"this run predates the as-traded split basis (2026-09-23): its reader inverted "
                f"{bar_day.isoformat()} against a spot off the chain's own basis, so the greeks "
                f"it used were wrong and are not shown ({exc})")

        row = u.bar_dict(i, ci, spot_source)
        row["notes"] = self._notes(ctx, bar_day, checked=not guard.unevaluable)
        row["missing_greeks_note"] = (
            "the run's Black-Scholes inversion gives no iv for this bar (no underlying close, or "
            "a premium outside the no-arbitrage bounds)")
        return row

    def _notes(self, ctx: _UnderlyingContext, bar_day: date, checked: bool) -> List[str]:
        inputs = self.inputs
        spot = ("the run's as-traded underlying close" if inputs.as_traded else
                "the split-ADJUSTED underlying close (this run predates the as-traded split "
                "basis)")
        rate = f"r={ctx.provider.risk_free_rate:g}"
        if inputs.risk_free_rate is None:
            rate += " (not recorded on the run: the platform default it resolved)"
        notes = [f"not stored by the {inputs.store} store: derived by the run's own reader, "
                 f"Black-Scholes from this bar's close against {spot}, {rate}, from the current "
                 f"caches"]
        if not checked:
            notes.append(f"the chain's put-call-parity spot could not be evaluated on "
                         f"{bar_day.isoformat()}, so this bar's spot basis is unchecked")
        return notes


def open_parquet_contract_reader(provenance: Any, backtest: Any
                                 ) -> Tuple[Optional[ParquetContractReader], Optional[str]]:
    """``(reader, None)`` or ``(None, the notice text)`` for a run whose store is parquet."""
    inputs, reason = parquet_run_inputs(provenance, backtest)
    if inputs is None:
        return None, (
            f"the run read a '{provenance.store}' option store, whose greeks are not stored but "
            f"derived at read time; they are not shown because {reason}")
    return ParquetContractReader(inputs), None
