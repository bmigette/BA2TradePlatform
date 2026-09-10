"""Gather-tape replay: re-run the LIVE ``_gather`` from the recorded provider returns.

Spec section 2: "Also support replaying the **live gather path** from a tape of
recorded provider returns. This validates mapping/shortcut behavior separately
from processing a saved normalized bundle. A missing tape response must stop that
comparison, not trigger a real request or substitute a historical endpoint."

The tape is an index of this analysis's recorded observations keyed by **exact
request identity** -- built with the very identity functions the taps used
(imported, never re-stated: two copies of one identity dict drift, and a drifted
copy turns a real match into a silent miss). A provider object here answers a
``get_*`` call by looking its arguments up in that index and hands back the
recorded payload; anything the tape does not hold is a typed
:class:`~ba2_common.core.replay.ReplayMiss` naming the request.

Two consequences worth stating plainly, because they are coverage facts and not
defects:

* **Quotes come from the tape too.** Live ``_gather`` reads the price through
  ``self._get_current_price`` -> the account's tapped
  ``get_instrument_current_price``, so the tape serves it from a tape ACCOUNT
  whose class name and id are taken from the recorded observation (they are part
  of the recorded identity, so a tape account that did not carry them could
  never match).
* **A boundary that was never tapped cannot be served.** DeterministicScorer's
  statements, macro and index reads bypass the tapped provider methods, so its
  gather-tape row is ``missing_capture`` naming the first request that was not on
  the tape -- not a green row, and not a live fetch.
"""
from __future__ import annotations

import functools
import importlib
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

from ba2_common.core.interfaces.MarketDataProviderInterface import ohlcv_identity
from ba2_common.core.interfaces.ReadOnlyAccountInterface import quote_identity
from ba2_common.core.replay import (
    AnalysisRecord,
    ProviderObservation,
    ReplayMiss,
    ReplayStatus,
    SessionBundle,
    load_bundle,
    sanitize_identity,
    use_capture_context,
)
from ba2_experts.FMPEarningsDrift import earning_calendar_identity
from ba2_experts.FMPRating import symbol_identity
from ba2_providers.cache.cached_get import insider_get_identity, past_earnings_get_identity

from app.services.replay.expert_replay import (
    RECORDED_EXPERTS,
    build_replay_expert,
    compare_values,
    replay_context,
)
from app.services.replay.isolation import refuse, replay_isolation
from app.services.replay.report import AnalysisResult, ReplayReport

__all__ = ["ReplayTape", "TapeProviderBundle", "run"]

#: The alias layer's ``as_of`` on the LIVE path is always ``None`` (that is what
#: makes it the live path), and the provider signature does not carry it, so the
#: tape states it rather than guessing it back out of ``end_date``.
_LIVE_AS_OF = None


# --------------------------------------------------------------------------- #
# The tape
# --------------------------------------------------------------------------- #
def _key(provider: str, method: str, identity: Any) -> str:
    """A stable lookup key: provider, method and the SANITIZED identity.

    Sanitized on both sides so a datetime, an Enum or a credential-shaped key is
    rendered exactly as the tap rendered it when the observation was recorded.
    """
    canonical = json.dumps(sanitize_identity(identity), sort_keys=True,
                           allow_nan=False, ensure_ascii=False, default=repr)
    return f"{provider}.{method}|{canonical}"


class ReplayTape:
    """Every recorded provider return of ONE analysis, addressable by identity."""

    def __init__(self, bundle: SessionBundle, analysis: AnalysisRecord):
        self.analysis_id = analysis.analysis_id
        self._entries: Dict[str, List[Any]] = {}
        self._observations: Tuple[ProviderObservation, ...] = tuple(
            sorted(bundle.observations_for(analysis.analysis_id),
                   key=lambda o: o.invocation_seq))
        self._taken: Dict[str, int] = {}
        for observation in self._observations:
            payload = (None if observation.payload_object is None
                       else bundle.decode(observation.payload_object))
            key = _key(observation.provider, observation.method, observation.request_identity)
            self._entries.setdefault(key, []).append(payload)

    # -- reading

    def methods(self) -> Tuple[str, ...]:
        return tuple(f"{o.provider}.{o.method}" for o in self._observations)

    def has(self, provider: str, method: str) -> bool:
        return any(o.provider == provider and o.method == method
                   for o in self._observations)

    def identities(self, provider: str, method: str) -> Tuple[Dict[str, Any], ...]:
        return tuple(dict(o.request_identity) for o in self._observations
                     if o.provider == provider and o.method == method)

    def take(self, provider: str, method: str, identity: Any) -> Any:
        """The next recorded return for this exact request, in recorded order.

        Repeated calls with the same identity are served in the order they were
        observed (a second quote read of the same symbol is a distinct event and
        may hold a different price). When they run out the answer is a miss, not
        the last value again.
        """
        key = _key(provider, method, identity)
        recorded = self._entries.get(key)
        used = self._taken.get(key, 0)
        if recorded is None or used >= len(recorded):
            raise refuse(
                "tape" if recorded is None else "tape_exhausted",
                analysis_id=self.analysis_id,
                request_identity={"provider": provider, "method": method,
                                  "identity": sanitize_identity(identity)},
                detail=("no recorded observation with this identity"
                        if recorded is None else
                        f"all {len(recorded)} recorded return(s) already consumed"))
        self._taken[key] = used + 1
        return recorded[used]


# --------------------------------------------------------------------------- #
# Tape actors (their CLASS NAME and id are part of the recorded identity)
# --------------------------------------------------------------------------- #
_ACTOR_CLASSES: Dict[Tuple[type, str], type] = {}

#: Stands in for an actor name the tape has no observation to take it from, so
#: the resulting lookup misses loudly instead of matching something else.
UNRECORDED_ACTOR = "__unrecorded__"


def _actor_class(base: type, recorded_name: str) -> type:
    """A subclass of ``base`` NAMED like the recorded provider/account class.

    ``type(x).__name__`` is part of several recorded identities, so a tape object
    can only reproduce those identities by carrying the recorded name.
    """
    key = (base, recorded_name)
    if key not in _ACTOR_CLASSES:
        _ACTOR_CLASSES[key] = type(recorded_name, (base,), {})
    return _ACTOR_CLASSES[key]


def _actor_name(tape: ReplayTape, provider: str, method: str, field: str) -> str:
    """The recorded actor name for a tapped method; all observations must agree."""
    names = {identity[field] for identity in tape.identities(provider, method)
             if field in identity}
    if not names:
        return UNRECORDED_ACTOR
    if len(names) > 1:
        raise refuse("tape_ambiguous_actor", analysis_id=tape.analysis_id,
                     request_identity={"provider": provider, "method": method,
                                       field: sorted(str(n) for n in names)},
                     detail="one analysis recorded several actors for one method")
    return str(next(iter(names)))


class _TapeActor:
    def __init__(self, tape: ReplayTape):
        self._tape = tape


class _TapeInsiderProvider(_TapeActor):
    def get_insider_transactions(self, symbol, end_date=None, lookback_days=None,
                                 as_of=None, format_type="dict", **_kw):
        return self._tape.take("provider_cache", "insider_get", insider_get_identity({
            "provider": self, "symbol": symbol, "as_of": as_of,
            "lookback": lookback_days, "format_type": format_type}))


class _TapeDetailsMixin:
    """The fundamentals-details methods the recorded experts actually call."""

    def get_past_earnings(self, symbol, frequency="quarterly", end_date=None,
                          lookback_periods=1, format_type="dict", **_kw):
        return self._tape.take(
            "provider_cache", "past_earnings_get", past_earnings_get_identity({
                "provider": self, "symbol": symbol, "as_of": _LIVE_AS_OF,
                "frequency": frequency, "lookback_periods": lookback_periods,
                "format_type": format_type}))

    def _refuse_statement(self, statement, symbol, **kwargs):
        raise refuse("tape", analysis_id=self._tape.analysis_id,
                     request_identity={"statement": statement, "symbol": symbol,
                                       **{k: repr(v) for k, v in kwargs.items()}},
                     detail="financial statements are not a tapped boundary in this delivery")

    def get_balance_sheet(self, symbol, *a, **kw):
        self._refuse_statement("balance_sheet", symbol, **kw)

    def get_income_statement(self, symbol, *a, **kw):
        self._refuse_statement("income_statement", symbol, **kw)

    def get_cashflow_statement(self, symbol, *a, **kw):
        self._refuse_statement("cashflow_statement", symbol, **kw)


class _TapeDetailsProvider(_TapeDetailsMixin, _TapeActor):
    """For every expert except FMPEarningsDrift (which needs an FMP-typed provider)."""


class _TapeOHLCVProvider(_TapeActor):
    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d",
                       use_cache=True, max_cache_age_hours=24, lookback_days=30):
        return self._tape.take("market_data", "get_ohlcv_data", ohlcv_identity({
            "self": self, "symbol": symbol, "interval": interval,
            "start_date": start_date, "end_date": end_date,
            "lookback_days": lookback_days, "use_cache": use_cache}))


class _TapeAccount(_TapeActor):
    """Answers a quote read from the recorded broker observation."""

    #: Set per analysis: the base method carries ``price_type``, IBKR's override
    #: does not, and the recorded identity says which shape was written.
    price_type_in_identity = True
    id = None

    def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
        args: Dict[str, Any] = {"self": self, "symbol_or_symbols": symbol_or_symbols}
        if self.price_type_in_identity:
            args["price_type"] = price_type
        return self._tape.take("broker", "get_instrument_current_price",
                               quote_identity(args))


def _tape_account(tape: ReplayTape) -> _TapeAccount:
    """A tape account carrying the recorded account class name, id and identity shape."""
    identities = tape.identities("broker", "get_instrument_current_price")
    name = _actor_name(tape, "broker", "get_instrument_current_price", "account_class")
    cls = _actor_class(_TapeAccount, name)
    account = cls(tape)
    account.id = identities[0]["account_id"] if identities else None
    account.price_type_in_identity = bool(identities) and "price_type" in identities[0]
    return account


# --------------------------------------------------------------------------- #
# The provider bundle
# --------------------------------------------------------------------------- #
class TapeProviderBundle:
    """A :class:`ba2_common.core.backtest_context.ProviderBundle` backed by the tape.

    Every accessor either returns a tape actor or refuses. There is no live
    fallback anywhere in this class: that is the whole point of it.
    """

    def __init__(self, tape: ReplayTape, *, details: Any = None):
        self._tape = tape
        self._insider = _actor_class(
            _TapeInsiderProvider,
            _actor_name(tape, "provider_cache", "insider_get", "provider"))(tape)
        self._ohlcv = _actor_class(
            _TapeOHLCVProvider,
            _actor_name(tape, "market_data", "get_ohlcv_data", "provider"))(tape)
        self._details = details if details is not None else _actor_class(
            _TapeDetailsProvider,
            _actor_name(tape, "provider_cache", "past_earnings_get", "provider"))(tape)

    def ohlcv(self):
        return self._ohlcv

    def fundamentals_details(self):
        return self._details

    def insider(self):
        return self._insider

    def fundamentals_overview(self):
        raise self._refuse("fundamentals_overview")

    def news(self):
        raise self._refuse("news")

    def indicators(self):
        raise self._refuse("indicators")

    def price_at_date(self, symbol, as_of):
        # The live gather never calls this (as_of is None there); a call means the
        # replay took the HISTORICAL branch, which is a different comparison.
        raise refuse("provider", analysis_id=self._tape.analysis_id,
                     request_identity={"method": "price_at_date", "symbol": symbol,
                                       "as_of": as_of},
                     detail="gather-tape replays the LIVE branch only")

    def _refuse(self, category: str) -> ReplayMiss:
        return refuse("provider", analysis_id=self._tape.analysis_id,
                      request_identity={"category": category},
                      detail="not a tapped boundary in this delivery")


@functools.lru_cache(maxsize=1)
def _fmp_details_base() -> type:
    """The tape's FMP-typed details base, built once (a fresh class per analysis
    would defeat :func:`_actor_class`'s cache and leak a type per call)."""
    from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
        FMPCompanyDetailsProvider,
    )
    return type("_TapeFMPDetails", (_TapeDetailsMixin, FMPCompanyDetailsProvider), {})


def _fmp_details_provider(tape: ReplayTape):
    """An FMP-TYPED details provider, because FMPEarningsDrift branches on its type.

    ``_gather``'s live calendar shortcut runs only when the details provider is an
    ``FMPCompanyDetailsProvider``. A plain tape object would silently send the
    replay down the per-symbol branch instead -- a DIFFERENT gather path, whose
    result would be compared as if it were the same one. So the tape's details
    provider for this expert subclasses the real one (constructed without its
    ``__init__``, which reads settings) and overrides everything it is asked for.
    """
    name = _actor_name(tape, "provider_cache", "past_earnings_get", "provider")
    base = _fmp_details_base()
    cls = _actor_class(base, name)
    provider = cls.__new__(cls)
    provider._tape = tape
    # The api key is not part of any recorded identity (the tap drops credentials),
    # and nothing on the tape path uses it -- but _gather READS the attribute, so
    # it must exist. An explicit sentinel, never a real key.
    provider.api_key = "replay-tape"
    return provider


@contextmanager
def _earnings_calendar_from_tape(tape: ReplayTape):
    """Serve FMPEarningsDrift's module-level bulk-calendar fetch from the tape."""
    module = importlib.import_module("ba2_experts.FMPEarningsDrift")
    original = module._fetch_earnings_calendar_by_symbol

    def from_tape(api_key, from_date, to_date):
        return tape.take("fmp", "earning_calendar", earning_calendar_identity(
            {"from_date": from_date, "to_date": to_date}))

    module._fetch_earnings_calendar_by_symbol = from_tape
    try:
        yield
    finally:
        module._fetch_earnings_calendar_by_symbol = original


# --------------------------------------------------------------------------- #
# Per-expert wiring
# --------------------------------------------------------------------------- #
def _setting(settings: Dict[str, Any], key: str, analysis: AnalysisRecord) -> Any:
    """A recorded setting, or a loud miss. Never a default: the RECORD is the source."""
    if key not in settings:
        raise refuse("settings_key", analysis_id=analysis.analysis_id,
                     request_identity={"setting": key},
                     detail="the recorded settings do not carry this gather-time key")
    return settings[key]


def _prepare(expert_class: str, expert, tape: ReplayTape, settings: Dict[str, Any],
             analysis: AnalysisRecord, stack: ExitStack) -> TapeProviderBundle:
    """Set the gather-time attributes live resolved before ``_gather``, and wire the tape.

    The attribute names and the conversions mirror each expert's own live call
    site (and its ``analyze_as_of`` prologue) exactly; the ``or 0`` forms are
    theirs too -- they turn a stored ``None`` into the number the expert would
    have used, and they are NOT a default for an absent key (an absent key is a
    miss, above).
    """
    expert._gather_symbol = analysis.symbol
    account = _tape_account(tape)
    expert._get_current_price = lambda symbol: account.get_instrument_current_price(symbol)

    if expert_class == "FMPRating":
        expert._gather_window_days = int(_setting(settings, "price_target_window_days", analysis))
        expert._gather_max_analyst_age = int(
            _setting(settings, "max_analyst_age_months", analysis) or 0)
        for attribute, method in (
            ("_fetch_price_target_consensus", "price_target_consensus"),
            ("_fetch_upgrade_downgrade", "upgrade_downgrade_consensus"),
            ("_fetch_grades_historical", "grades_historical"),
            ("_fetch_price_target_history", "price_target_history"),
            ("_fetch_analyst_grades", "analyst_grades"),
        ):
            setattr(expert, attribute, _fmp_symbol_fetch(tape, method))
        return TapeProviderBundle(tape)

    if expert_class == "FMPEarningsDrift":
        expert._gather_max_days_since_report = int(
            _setting(settings, "max_days_since_report", analysis))
        expert._gather_expected_profit_mode = _setting(settings, "expected_profit_mode", analysis)
        details = (_fmp_details_provider(tape)
                   if tape.has("fmp", "earning_calendar") else None)
        if details is not None:
            stack.enter_context(_earnings_calendar_from_tape(tape))
        return TapeProviderBundle(tape, details=details)

    if expert_class == "FMPInsiderClusterBuy":
        expert._gather_lookback_days = int(_setting(settings, "lookback_days", analysis))
        expert._gather_expected_profit_mode = _setting(settings, "expected_profit_mode", analysis)
        return TapeProviderBundle(tape)

    if expert_class == "DeterministicScorer":
        from ba2_experts.DeterministicScorer import data as ds_data

        expert._gather_w_analyst = float(_setting(settings, "w_analyst", analysis) or 0.0)
        expert._gather_w_earnings = float(_setting(settings, "w_earnings", analysis) or 0.0)
        expert._gather_index_symbol = str(_setting(settings, "index_symbol", analysis))
        expert._gather_use_model_target = bool(_setting(settings, "use_model_target", analysis))

        def _refuse_api_key():
            raise refuse("fmp_api_key", analysis_id=analysis.analysis_id,
                         request_identity={"expert_class": expert_class},
                         detail="the dated grades/target fetches are not a tapped boundary")

        expert._get_fmp_api_key = _refuse_api_key
        # The module memoizes OHLCV per symbol across calls; one analysis's frame
        # must never be served to the next one's gather.
        ds_data.reset_caches()
        stack.callback(ds_data.reset_caches)
        return TapeProviderBundle(tape)

    raise refuse("expert_class", analysis_id=analysis.analysis_id,
                 request_identity={"expert_class": expert_class},
                 detail="no gather-tape wiring for this expert")


def _fmp_symbol_fetch(tape: ReplayTape, method: str) -> Callable[[str], Any]:
    def fetch(symbol):
        return tape.take("fmp", method, symbol_identity({"symbol": symbol}))
    return fetch


# --------------------------------------------------------------------------- #
# One analysis
# --------------------------------------------------------------------------- #
def replay_gather(bundle: SessionBundle, analysis: AnalysisRecord) -> AnalysisResult:
    def result(status: str, detail: str = "",
               field_diffs: Sequence[Tuple[str, str, str]] = ()) -> AnalysisResult:
        return AnalysisResult(
            analysis_id=analysis.analysis_id, expert_class=analysis.expert_class,
            symbol=analysis.symbol, use_case=analysis.use_case,
            recorded_outcome=analysis.outcome, status=status, detail=detail,
            field_diffs=tuple(field_diffs))

    if analysis.expert_class not in RECORDED_EXPERTS:
        return result(ReplayStatus.COVERAGE_UNSUPPORTED,
                      f"{analysis.expert_class} is not one of the recorded experts "
                      f"{list(RECORDED_EXPERTS)}")
    if analysis.bundle_capture_status != ReplayStatus.CAPTURE_CAPTURED \
            or analysis.bundle_object is None:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"no recorded bundle to compare against "
                      f"(bundle_capture_status={analysis.bundle_capture_status})")
    if analysis.settings_object is None:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      "no recorded settings; replay will not invent them")

    try:
        recorded_bundle = bundle.decode(analysis.bundle_object)
        settings = bundle.decode(analysis.settings_object)
        tape = ReplayTape(bundle, analysis)
        expert = build_replay_expert(analysis.expert_class, analysis)
    except ReplayMiss as miss:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE, str(miss))
    except Exception as exc:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"could not rebuild the recorded inputs: {type(exc).__name__}: {exc}")

    try:
        with ExitStack() as stack:
            providers = _prepare(analysis.expert_class, expert, tape, settings,
                                 analysis, stack)
            with use_capture_context(replay_context(analysis)):
                produced = expert._gather(providers, as_of=None)
    except ReplayMiss as miss:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE, str(miss))
    except Exception as exc:
        return result(ReplayStatus.COVERAGE_DIFFERENCE,
                      f"the live gather failed on the tape: {type(exc).__name__}: {exc}")

    diffs = compare_values(recorded_bundle, produced, "bundle")
    if not diffs:
        return result(ReplayStatus.COVERAGE_MATCH, "gather reproduced the recorded bundle")
    changed = ", ".join(name for name, _r, _p in diffs[:5])
    return result(ReplayStatus.COVERAGE_DIFFERENCE,
                  f"{len(diffs)} bundle field(s) differ: {changed}", diffs)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def run(bundle_dir, out_dir=None) -> ReplayReport:
    """Re-run every recorded analysis's live ``_gather`` against its own tape."""
    bundle = load_bundle(bundle_dir)
    report = ReplayReport(
        session_id=bundle.session.session_id,
        bundle_dir=str(Path(bundle_dir)),
        capability=ReplayStatus.CAPABILITY_GATHER_TAPE,
    )
    with replay_isolation():
        for analysis in bundle.analyses:
            report.results.append(replay_gather(bundle, analysis))
    if out_dir is not None:
        report.write(out_dir)
    return report
