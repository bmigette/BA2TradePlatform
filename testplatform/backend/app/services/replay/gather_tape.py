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
* **A boundary that was never tapped cannot be served.** Every boundary a
  recorded expert reads is tapped as of the second delivery -- including
  DeterministicScorer's statements, its dated analyst history and its FRED macro
  reads, which used to make its gather-tape row a declared ``missing_capture``.
  What made that possible is the identity rule (see
  :mod:`ba2_common.core.replay.observe`): each of those requests carries a window
  that used to be a raw ``datetime.now()`` and is now a recorded ``replay_now``
  read, so the replayed request is the recorded one and the tape can be keyed on
  it. A boundary added later without a tap is still a ``missing_capture`` naming
  the first request that was not on the tape -- never a live fetch.

**Branches come from the RECORD, never from the tape.** "There is a calendar
response on the tape" and "the calendar branch ran" are different statements.
Inferring the second from the first means a missing response silently reroutes
the replay down the other branch and its bundle gets compared as if the recorded
branch had produced it -- so the branch is read from ``branch_flags``, and a
recorded branch whose response is absent is a ``missing_capture``.
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
from ba2_providers.fundamentals.details.FMPCompanyDetailsProvider import (
    earnings_estimates_identity,
    past_earnings_identity,
    statement_identity,
)
from ba2_providers.macro.fred_series import series_identity

from app.services.replay.expert_replay import (
    RECORDED_EXPERTS,
    build_replay_expert,
    compare_values,
    replay_context,
)
from app.services.replay.isolation import refuse, replay_isolation
from app.services.replay.report import AnalysisResult, ReplayReport, merge_coverage

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

    No ``default=`` fallback: a value the sanitizer could not render is a defect
    in the identity, and repr-ing it would build a key that silently depends on a
    memory address. It raises a typed miss instead.
    """
    _refuse_non_finite(identity, provider, method)
    safe = sanitize_identity(identity)
    try:
        canonical = json.dumps(safe, sort_keys=True, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise refuse("tape_identity", request_identity={"provider": provider,
                                                        "method": method},
                     detail=f"the request identity is not representable: {exc}") from exc
    return f"{provider}.{method}|{canonical}"


def _refuse_non_finite(identity: Any, provider: str, method: str, path: str = "") -> None:
    """A NaN (or inf) anywhere in an identity makes the request unmatchable.

    ``NaN != NaN``, so a recorded NaN key can never be looked up again by any
    honest means -- the sanitizer renders it as the string ``'nan'`` and a match
    on that would be an accident of formatting, not an identity match. It is a
    ``missing_capture`` (this request cannot be served from the tape), never a
    ``difference`` (which would claim the calculation disagreed).
    """
    if isinstance(identity, float):
        if identity != identity or identity in (float("inf"), float("-inf")):
            raise refuse("tape_identity_non_finite",
                         request_identity={"provider": provider, "method": method,
                                           "path": path or "identity"},
                         detail=f"a non-finite value ({identity!r}) cannot be matched")
        return
    if isinstance(identity, dict):
        for key, value in identity.items():
            _refuse_non_finite(value, provider, method, f"{path}[{key!r}]")
    elif isinstance(identity, (list, tuple)):
        for index, value in enumerate(identity):
            _refuse_non_finite(value, provider, method, f"{path}[{index}]")


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
    return _actor_name_across(tape, field, (provider, method))


#: Every tapped boundary a fundamentals-details provider can be recorded under.
#: One analysis reaches it through one or two of them (EarningsDrift goes through
#: the ``cached_get`` alias AND the provider method; DeterministicScorer only
#: through the provider methods), so the tape actor's NAME -- which is part of
#: those identities -- has to be taken from whichever ones were recorded.
_DETAILS_BOUNDARIES = (
    ("provider_cache", "past_earnings_get"),
    ("fundamentals_details", "get_past_earnings"),
    ("fundamentals_details", "get_balance_sheet"),
    ("fundamentals_details", "get_income_statement"),
    ("fundamentals_details", "get_cashflow_statement"),
    ("fundamentals_details", "get_earnings_estimates"),
)


def _actor_name_across(tape: ReplayTape, field: str, *boundaries) -> str:
    """The recorded actor name across several boundaries; they must all agree.

    Two different names for one actor inside one analysis is not something to
    pick a winner from: it means the record does not say which object answered,
    so the replay refuses rather than guessing.
    """
    names = set()
    for provider, method in boundaries:
        names.update(identity[field] for identity in tape.identities(provider, method)
                     if field in identity)
    if not names:
        return UNRECORDED_ACTOR
    if len(names) > 1:
        raise refuse("tape_ambiguous_actor", analysis_id=tape.analysis_id,
                     request_identity={"boundaries": [f"{p}.{m}" for p, m in boundaries],
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


class _TapeStatementsMixin:
    """The fundamentals-details methods, served from the PROVIDER-METHOD taps.

    DeterministicScorer and the analyst-target estimator call these methods
    directly (never through the ``cached_get`` alias layer), so this is the shape
    their reads were recorded in. Every signature mirrors the provider's exactly,
    defaults included: the identity is built from the bound arguments, so a
    default that differed here would build a key the tap never wrote.
    """

    def _statement(self, method, symbol, frequency, end_date, start_date,
                   lookback_periods, as_of, format_type):
        return self._tape.take("fundamentals_details", method, statement_identity({
            "self": self, "symbol": symbol, "frequency": frequency,
            "start_date": start_date, "end_date": end_date,
            "lookback_periods": lookback_periods, "as_of": as_of,
            "format_type": format_type}))

    def get_balance_sheet(self, symbol, frequency="annual", end_date=None,
                          start_date=None, lookback_periods=None, as_of=None,
                          format_type="markdown"):
        return self._statement("get_balance_sheet", symbol, frequency, end_date,
                               start_date, lookback_periods, as_of, format_type)

    def get_income_statement(self, symbol, frequency="annual", end_date=None,
                             start_date=None, lookback_periods=None, as_of=None,
                             format_type="markdown"):
        return self._statement("get_income_statement", symbol, frequency, end_date,
                               start_date, lookback_periods, as_of, format_type)

    def get_cashflow_statement(self, symbol, frequency="annual", end_date=None,
                               start_date=None, lookback_periods=None, as_of=None,
                               format_type="markdown"):
        return self._statement("get_cashflow_statement", symbol, frequency, end_date,
                               start_date, lookback_periods, as_of, format_type)

    def get_past_earnings(self, symbol, frequency="annual", end_date=None,
                          lookback_periods=8, format_type="markdown"):
        return self._tape.take(
            "fundamentals_details", "get_past_earnings", past_earnings_identity({
                "self": self, "symbol": symbol, "frequency": frequency,
                "end_date": end_date, "lookback_periods": lookback_periods,
                "format_type": format_type}))

    def get_earnings_estimates(self, symbol, frequency="annual", as_of_date=None,
                               lookback_periods=4, format_type="markdown"):
        return self._tape.take(
            "fundamentals_details", "get_earnings_estimates",
            earnings_estimates_identity({
                "self": self, "symbol": symbol, "frequency": frequency,
                "as_of_date": as_of_date, "lookback_periods": lookback_periods,
                "format_type": format_type}))


class _TapeDetailsMixin(_TapeStatementsMixin):
    """The same provider, for the experts that reach it through ``cached_get``.

    THE CONSTRAINT: one live ``get_past_earnings`` call through the alias layer
    records TWO observations under two different identities -- the alias's
    (``provider_cache.past_earnings_get``, carrying the uniform as_of/lookback
    request) and the provider method's (``fundamentals_details.get_past_earnings``,
    carrying the window that answered it). A tape actor has ONE
    ``get_past_earnings``, so it can serve only one of them, and choosing by "which
    one is on the tape" would be exactly the inference this module refuses
    everywhere else. So the choice is made by EXPERT in :func:`_prepare`: the two
    experts that call the alias get this class, the ones that call the provider
    method directly get :class:`_TapeStatementsMixin`'s version.
    """

    def get_past_earnings(self, symbol, frequency="quarterly", end_date=None,
                          lookback_periods=1, format_type="dict", **_kw):
        return self._tape.take(
            "provider_cache", "past_earnings_get", past_earnings_get_identity({
                "provider": self, "symbol": symbol, "as_of": _LIVE_AS_OF,
                "frequency": frequency, "lookback_periods": lookback_periods,
                "format_type": format_type}))


class _TapeDetailsProvider(_TapeDetailsMixin, _TapeActor):
    """For every expert except FMPEarningsDrift (which needs an FMP-typed provider)."""


class _TapeStatementsProvider(_TapeStatementsMixin, _TapeActor):
    """For the experts that call the provider methods directly (DeterministicScorer)."""


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
            _actor_name_across(tape, "provider", *_DETAILS_BOUNDARIES))(tape)

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
    name = _actor_name_across(tape, "provider", *_DETAILS_BOUNDARIES)
    base = _fmp_details_base()
    cls = _actor_class(base, name)
    provider = cls.__new__(cls)
    provider._tape = tape
    # The api key is not part of any recorded identity (the tap drops credentials),
    # and nothing on the tape path uses it -- but _gather READS the attribute, so
    # it must exist. An explicit sentinel, never a real key.
    provider.api_key = "replay-tape"
    return provider


def _tape_statements_provider(tape: ReplayTape) -> Any:
    """A details provider carrying the recorded provider class name (part of the
    statement identities) whose every method reads from the tape."""
    return _actor_class(_TapeStatementsProvider,
                        _actor_name_across(tape, "provider", *_DETAILS_BOUNDARIES))(tape)


@contextmanager
def _macro_series_from_tape(tape: ReplayTape):
    """Serve the FRED point-in-time reads from the tape.

    ``DeterministicScorer.data.fetch_macro_series`` calls the module-level
    ``fred_series.get_series_as_of`` directly (the macro store is a flat per-series
    file, not a provider), so the tape has to stand in for the module attribute the
    way it does for the bulk earnings calendar.
    """
    module = importlib.import_module("ba2_providers.macro.fred_series")
    original = module.get_series_as_of

    def from_tape(series_id, as_of):
        return tape.take("macro", "get_series_as_of",
                         series_identity({"series_id": series_id, "as_of": as_of}))

    module.get_series_as_of = from_tape
    try:
        yield
    finally:
        module.get_series_as_of = original


@contextmanager
def _analyst_history_from_tape(tape: ReplayTape):
    """Serve the dated grades / price-target histories from the tape.

    These are FMPRating's MODULE-level cached fetchers -- the single boundary both
    it and DeterministicScorer record through -- imported inside
    ``data.fetch_grades_history`` / ``fetch_price_targets``, so replacing the
    module attribute is what the replay can reach.
    """
    module = importlib.import_module("ba2_experts.FMPRating")
    originals = {name: getattr(module, name) for name in
                 ("fetch_grades_historical_cached", "fetch_price_target_history_cached")}

    def from_tape(method):
        def fetch(api_key, symbol):
            return tape.take("fmp", method, symbol_identity({"symbol": symbol}))
        return fetch

    module.fetch_grades_historical_cached = from_tape("grades_historical")
    module.fetch_price_target_history_cached = from_tape("price_target_history")
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(module, name, original)


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


def _branch(analysis: AnalysisRecord, flag: str) -> Any:
    """Which branch the LIVE gather took, read off the record.

    A miss when the flag is absent, because the alternative -- inferring the
    branch from which observations happen to be present -- is exactly how a
    missing response turns into a confident match down the other branch.
    """
    if flag not in analysis.branch_flags:
        raise refuse("branch_flag", analysis_id=analysis.analysis_id,
                     request_identity={"branch_flag": flag},
                     detail="the record does not say which branch the live gather took")
    return analysis.branch_flags[flag]


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
        branch = _branch(analysis, "fmp_rating_branch")
        if branch != "live_snapshot":
            raise refuse("branch", analysis_id=analysis.analysis_id,
                         request_identity={"fmp_rating_branch": branch},
                         detail="gather-tape replays the LIVE snapshot branch only")
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
        # The RECORD says which branch ran, not the tape. When the calendar branch
        # ran, the tape presents an FMP-typed provider (the branch is chosen by
        # isinstance) and serves the bulk calendar; if that response is missing the
        # gather MISSES -- it does not quietly take the per-symbol branch instead.
        if bool(_branch(analysis, "earnings_calendar_branch")):
            stack.enter_context(_earnings_calendar_from_tape(tape))
            return TapeProviderBundle(tape, details=_fmp_details_provider(tape))
        return TapeProviderBundle(tape)

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
                         detail="this gather weighted the analyst section at zero, so "
                                "the live path never read an FMP key")

        # The RECORD says whether the live gather had a key, not the tape. Inferring
        # it from "is there an analyst response on the tape?" is how a missing
        # response quietly reroutes the replay down the no-coverage branch, whose
        # empty bundle then compares as a plausible DIFFERENCE instead of the
        # missing capture it is.
        expert._get_fmp_api_key = _refuse_api_key
        if expert._gather_w_analyst > 0:
            if bool(_branch(analysis, "ds_analyst_key_present")):
                stack.enter_context(_analyst_history_from_tape(tape))
                # A sentinel, never a key: the tape answers both fetches and nothing
                # on this path uses the value.
                expert._get_fmp_api_key = lambda: "replay-tape"
            else:
                expert._get_fmp_api_key = lambda: None
        # The macro read is unconditional in this gather.
        stack.enter_context(_macro_series_from_tape(tape))
        # The module memoizes OHLCV (and the macro series) per key across calls; one
        # analysis's frame must never be served to the next one's gather.
        ds_data.reset_caches()
        stack.callback(ds_data.reset_caches)
        return TapeProviderBundle(tape, details=_tape_statements_provider(tape))

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
            with use_capture_context(replay_context(analysis, ReplayStatus.PHASE_GATHER)):
                produced = expert._gather(providers, as_of=None)
    except ReplayMiss as miss:
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE, str(miss))
    except Exception as exc:
        return result(ReplayStatus.COVERAGE_DIFFERENCE,
                      f"the live gather failed on the tape: {type(exc).__name__}: {exc}")

    try:
        diffs = compare_values(recorded_bundle, produced, "bundle")
    except ReplayMiss as miss:
        # The COMPARISON could not run (a bundle field the codec refuses). One
        # analysis's coverage gap, not a reason to abandon the whole report.
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"the comparison could not run: {miss}")
    except Exception as exc:  # noqa: BLE001 -- same containment, one row at a time
        return result(ReplayStatus.COVERAGE_MISSING_CAPTURE,
                      f"the comparison could not run: {type(exc).__name__}: {exc}")
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
    merge_coverage(bundle_dir, report)
    if out_dir is not None:
        report.write(out_dir)
    return report
