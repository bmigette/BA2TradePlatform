"""Option selection replay: re-run each recorded option ENTRY / CLOSE action on its tape.

Design ``docs/plans/2026-11-05-option-capture-replay-design.md``. Live records, per decision pass
(one ``TradeManager`` capture record), every broker read the option actions made (provider
``"broker_options"``: chain, quotes, IV, sizing figures; provider ``"broker"``: stock quotes) and
ONE ``"option_action"`` observation per executed action (inputs, the DB rows it read, the outcome).
This module rebuilds each recorded action with the SHARED action code, feeds it a tape account
that answers every read from the recording, and compares what it then does to what live did:

* ``selection`` -- per leg: contract symbol, side, ratio, expiry (a close: the position closed);
* ``pricing`` -- the limit price handed to the account;
* ``sizing`` -- the quantity handed to the account;
* ``record`` -- ``entry_record`` / ``exit_record``, key by key (the differing keys are listed);
* ``refusal`` -- success flag, the refusal message, the error.

Comparison is EXACT (codec bytes via :func:`~app.services.replay.expert_replay.compare_values`).
A read the tape does not hold is a typed :class:`~ba2_common.core.replay.ReplayMiss` and the
action's row is ``missing_capture`` naming the request -- never a live fetch, never a guess. Any
other exception out of the re-run is reported as a ``difference`` whose detail starts ``error:``
(the report's status vocabulary has no ``error``; a re-run that raises where live did not IS a
difference).

NOT run, and named so in every report header: ``historical_provider`` -- re-selecting the same
contract from the parquet chain instead of the broker chain.

**What a replay of ONE action is.**

* The tape is the observations of the action's enclosing record made inside the action's own
  span: ``payload["tape_span"]["start"] <= invocation_seq <`` the action's record (written when it
  ends). A read the evaluator made BEFORE the action is outside it, so it can never be served to
  the action's identical (later, possibly different) request. A payload without ``tape_span`` is
  ``missing_capture``.
* The recorder's own ``replay_now()`` is the first clock read of each action span; the replay
  consumes it with one ``replay_now()`` call before ``execute()``. The clock is handed to the
  replay from that read onwards (found by value in the record's recorded reads).
* A scratch in-memory database (thread-local, schema created) holds the recorded ``db_rows``;
  the instance resolver is replaced for the duration by one that builds the expert from those rows
  and hands out the tape account (the isolation stub refuses every resolution).
* ``submit_option_order`` / ``close_option_position`` on the tape account store a minimal order row
  in the scratch DB (so the action's own "stamp the record on the order row" step works) and
  return it; any other broker write raises.
"""
from __future__ import annotations

import abc
from contextlib import contextmanager
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.interfaces.ReadOnlyAccountInterface import quote_identity
from ba2_common.core.replay import (
    AnalysisRecord,
    ProviderObservation,
    ReplayMiss,
    ReplayStatus,
    SessionBundle,
    load_bundle,
    use_capture_context,
)
from ba2_common.core.replay import option_action_record as oar
from ba2_common.core.replay.clock import replay_now
from ba2_common.core.replay.context import CaptureContext
from ba2_common.core.replay.option_taps import OPTION_PROVIDER, OPTION_TAPPED_METHODS

from app.services.replay.expert_replay import compare_values
from app.services.replay.gather_tape import ReplayTape
from app.services.replay.isolation import refuse, replay_isolation
from app.services.replay.report import (
    CAPABILITY_OPTION_SELECTION,
    STAGE_OPTION_PRICING,
    STAGE_OPTION_RECORD,
    STAGE_OPTION_REFUSAL,
    STAGE_OPTION_SELECTION,
    STAGE_OPTION_SIZING,
    AnalysisResult,
    FieldDiff,
    ReplayReport,
    merge_coverage,
)

__all__ = ["TapeOptionsAccount", "replay_action", "run", "run_bundle"]

#: Provider name of the stock-quote tap the option sizing path also reads.
STOCK_PROVIDER = "broker"
STOCK_QUOTE_METHOD = "get_instrument_current_price"

STAGES = (STAGE_OPTION_SELECTION, STAGE_OPTION_PRICING, STAGE_OPTION_SIZING,
          STAGE_OPTION_RECORD, STAGE_OPTION_REFUSAL)

_MATCH = ReplayStatus.COVERAGE_MATCH
_DIFFERENCE = ReplayStatus.COVERAGE_DIFFERENCE
_MISSING = ReplayStatus.COVERAGE_MISSING_CAPTURE


# --------------------------------------------------------------------------- #
# The tape
# --------------------------------------------------------------------------- #
class _SpanBundle:
    """A bundle that exposes only the observations of ONE action's window."""

    def __init__(self, bundle: SessionBundle, observations: Sequence[ProviderObservation]):
        self._bundle = bundle
        self._observations = list(observations)

    def observations_for(self, analysis_id):
        return list(self._observations)

    def decode(self, *args, **kwargs):
        return self._bundle.decode(*args, **kwargs)


class SpanTape(ReplayTape):
    """A :class:`ReplayTape` that also remembers every miss it raised.

    The action code has broad handlers of its own; one that turns a miss into a failed result
    would otherwise make an unrecorded read look like a different decision. The miss list lets
    the caller report ``missing_capture`` whatever the action did with the exception.
    """

    def __init__(self, bundle, analysis, observations):
        super().__init__(_SpanBundle(bundle, observations), analysis)
        self.misses: List[ReplayMiss] = []

    def take(self, provider, method, identity):
        try:
            return super().take(provider, method, identity)
        except ReplayMiss as miss:
            self.misses.append(miss)
            raise


# --------------------------------------------------------------------------- #
# The tape account
# --------------------------------------------------------------------------- #
#: Abstract members that WRITE at the broker. Everything else abstract is a READ nobody tapped.
_BROKER_WRITES = frozenset({
    "_submit_order_impl", "_submit_option_order_impl", "cancel_order", "modify_order",
    "adjust_tp", "adjust_sl", "adjust_tp_sl", "refresh_orders", "refresh_positions",
})


def _refusing(name: str):
    kind = "broker_write" if name in _BROKER_WRITES else "untapped_broker_read"

    def method(self, *args, **kwargs):
        raise refuse(kind, analysis_id=self._analysis_id,
                     request_identity={"method": name, "account_class": type(self).__name__},
                     detail=("replay never writes at the broker" if kind == "broker_write"
                             else "this read is not one of the tapped option / quote reads"))

    method.__name__ = name
    return method


class TapeOptionsAccount(AccountInterface, OptionsAccountInterface):
    """A broker account that answers from one action's tape and submits nothing.

    Every tapped method is answered by identity lookup using the SAME identity functions the taps
    recorded with (imported from ``option_taps`` / ``ReadOnlyAccountInterface``, never restated).
    The class is subclassed per run under the RECORDED account class name (that name is part of
    every recorded identity); ``id`` is the recorded account id.
    """

    # The replay's own methods answer from the recording; wrapping them in capture taps would
    # record a replay into the tape it is reading.
    _replay_taps_exempt = True

    def __init__(self, tape: ReplayTape, *, account_id: Any, analysis_id: str,
                 price_type_in_quote: bool, price_type_in_underlying: bool):
        # No super().__init__: the live constructors load the AccountDefinition row from the
        # trading database, which replay may not open.
        self.id = account_id
        self._tape = tape
        self._analysis_id = analysis_id
        self._price_type_in_quote = price_type_in_quote
        self._price_type_in_underlying = price_type_in_underlying
        self.submitted: List[Dict[str, Any]] = []

    # -- tapped reads -------------------------------------------------------
    def _serve(self, method: str, **arguments):
        provider, identity = OPTION_TAPPED_METHODS[method]
        return self._tape.take(provider, method, identity({"self": self, **arguments}))

    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type=None,
                         strike_min=None, strike_max=None):
        return self._serve("get_option_chain", underlying=underlying, expiry_min=expiry_min,
                           expiry_max=expiry_max, option_type=option_type,
                           strike_min=strike_min, strike_max=strike_max)

    def get_option_quote(self, contract_symbol):
        return self._serve("get_option_quote", contract_symbol=contract_symbol)

    def get_atm_implied_volatility(self, underlying):
        return self._serve("get_atm_implied_volatility", underlying=underlying)

    def get_option_underlying_price(self, symbol, price_type=None):
        arguments: Dict[str, Any] = {"symbol": symbol}
        if self._price_type_in_underlying:
            arguments["price_type"] = price_type
        return self._serve("get_option_underlying_price", **arguments)

    def option_capital_equity(self):
        return self._serve("option_capital_equity")

    def get_account_snapshot(self):
        return self._serve("get_account_snapshot")

    def get_positions(self):
        return self._serve("get_positions")

    def check_option_buying_power(self, required):
        return self._serve("check_option_buying_power", required=required)

    def check_short_put_assignment_capacity(self, *, strike, contracts, multiplier=100,
                                            long_puts=()):
        return self._serve("check_short_put_assignment_capacity", strike=strike,
                           contracts=contracts, multiplier=multiplier, long_puts=long_puts)

    # -- the stock quote the option sizing path also reads ------------------
    def get_instrument_current_price(self, symbol_or_symbols, price_type="bid"):
        arguments: Dict[str, Any] = {"self": self, "symbol_or_symbols": symbol_or_symbols}
        if self._price_type_in_quote:
            arguments["price_type"] = price_type
        return self._tape.take(STOCK_PROVIDER, STOCK_QUOTE_METHOD, quote_identity(arguments))

    # -- the two writes the action performs ---------------------------------
    def _order_row(self, *, symbol, contract_symbol, quantity, side, option_strategy):
        from ba2_common.core.db import add_instance, get_instance
        from ba2_common.core.models import TradingOrder
        from ba2_common.core.types import AssetClass, OrderStatus, OrderType

        order_id = add_instance(TradingOrder(
            account_id=self.id, symbol=symbol, underlying_symbol=symbol,
            contract_symbol=contract_symbol, quantity=quantity, side=side,
            order_type=OrderType.MARKET, status=OrderStatus.PENDING,
            asset_class=AssetClass.OPTION, comment=option_strategy))
        return get_instance(TradingOrder, order_id)

    def submit_option_order(self, legs, quantity, order_type="limit", limit_price=None,
                            option_strategy=None, expert_recommendation_id=None,
                            transaction_id=None):
        first = legs[0]
        underlying = first.underlying
        if underlying is None:
            raise refuse("replay_order", analysis_id=self._analysis_id,
                         request_identity={"contract_symbol": first.contract_symbol},
                         detail="an option leg without an underlying cannot be booked")
        row = self._order_row(symbol=underlying,
                              contract_symbol=first.contract_symbol if len(legs) == 1 else None,
                              quantity=quantity, side=first.side, option_strategy=option_strategy)
        self.submitted.append({"method": "submit_option_order", "order_id": row.id})
        return row

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        from ba2_common.core.types import OrderDirection

        close_side = (OrderDirection.SELL if position.side == OrderDirection.BUY
                      else OrderDirection.BUY)
        row = self._order_row(symbol=position.underlying,
                              contract_symbol=position.contract_symbol,
                              quantity=position.quantity, side=close_side, option_strategy=None)
        self.submitted.append({"method": "close_option_position", "order_id": row.id})
        return row


def _install_refusals(cls) -> None:
    """Give every abstract member the class does not implement a loud refusal."""
    for name in sorted(cls.__abstractmethods__):
        setattr(cls, name, _refusing(name))
    abc.update_abstractmethods(cls)


_install_refusals(TapeOptionsAccount)

def _tape_account(tape: SpanTape, inputs: Dict[str, Any], analysis_id: str) -> TapeOptionsAccount:
    """A tape account named and numbered as the recorded one, with the recorded identity shapes."""
    # Named like the recorded account (the name is part of every recorded identity) and carrying
    # its class-level declarations (e.g. OPTION_GREEKS_SOURCE, without which an entry record
    # cannot be built).
    account_class = type(inputs["account_class"], (TapeOptionsAccount,),
                         dict(inputs["account_declarations"]))
    quotes = tape.identities(STOCK_PROVIDER, STOCK_QUOTE_METHOD)
    underlying = tape.identities(OPTION_PROVIDER, "get_option_underlying_price")
    return account_class(
        tape, account_id=inputs["account_id"], analysis_id=analysis_id,
        # The base method carries ``price_type``, an override may not; the recorded identity says
        # which shape was written. With nothing recorded any lookup misses, whichever is chosen.
        price_type_in_quote=(not quotes) or "price_type" in quotes[0],
        price_type_in_underlying=(not underlying) or "price_type" in underlying[0])


# --------------------------------------------------------------------------- #
# The scratch database, resolver and expert
# --------------------------------------------------------------------------- #
class _ReplayExpert(MarketExpertInterface):
    """The shared capital / settings calculation on the recorded ``ExpertInstance`` rows.

    Nothing but an id: settings, ``virtual_equity_pct`` and transactions are read from the scratch
    database, the account from the resolver, exactly as for a live expert.
    """

    def __init__(self, expert_id):
        self.id = expert_id

    @classmethod
    def description(cls):
        return "option selection replay expert"

    def render_market_analysis(self, market_analysis):
        raise refuse("replay_expert", detail="a replay expert renders nothing")

    def run_analysis(self, symbol, market_analysis):
        raise refuse("replay_expert", detail="a replay expert analyses nothing")


class _ScratchResolver:
    """The instance resolver of one replayed action: experts from the scratch DB, the tape account."""

    def __init__(self, account: TapeOptionsAccount, analysis_id: str):
        self._account = account
        self._analysis_id = analysis_id
        self._experts: Dict[Any, _ReplayExpert] = {}

    def get_expert_instance(self, expert_id):
        from ba2_common.core.db import get_instance
        from ba2_common.core.models import ExpertInstance

        if expert_id not in self._experts:
            if get_instance(ExpertInstance, expert_id) is None:
                raise refuse("db_row", analysis_id=self._analysis_id,
                             request_identity={"model": "ExpertInstance", "id": expert_id},
                             detail="the action's expert instance was not recorded")
            self._experts[expert_id] = _ReplayExpert(expert_id)
        return self._experts[expert_id]

    def get_account_instance(self, account_id):
        return self._account

    def get_account_instance_from_transaction(self, transaction):
        return self._account


@contextmanager
def _scratch_trading_db(instances: Sequence[Any]):
    """A thread-local in-memory trading DB holding the recorded rows; gone on exit."""
    from ba2_common.core import OptionRiskManagement as option_rm
    from ba2_common.core import db

    db.configure_db_threadlocal(":memory:")
    try:
        db.init_db()
        # The option risk manager keeps per-sleeve process state; start from none.
        option_rm.reset_thread_state()
        with db.get_db() as session:
            for instance in instances:
                session.add(instance)
            session.commit()
        yield
    finally:
        try:
            option_rm.reset_thread_state()
        finally:
            db.clear_threadlocal_db()


@contextmanager
def _resolver(resolver):
    from ba2_common.core import instance_resolver

    previous = instance_resolver.get_instance_resolver()
    instance_resolver.set_instance_resolver(resolver)
    try:
        yield
    finally:
        instance_resolver.set_instance_resolver(previous)


# --------------------------------------------------------------------------- #
# Views and comparison
# --------------------------------------------------------------------------- #
def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _selection_view(submits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for submit in submits:
        if submit.get("legs") is not None:
            legs = [{"contract_symbol": leg.contract_symbol, "side": _plain(leg.side),
                     "ratio_qty": leg.ratio_qty, "expiry": _plain(leg.expiry)}
                    for leg in submit["legs"]]
        else:
            position = submit["position"]
            legs = [{"contract_symbol": position.contract_symbol, "side": _plain(position.side),
                     "expiry": _plain(position.expiry)}]
        out.append({"method": submit["method"], "legs": legs})
    return out


def _pricing_view(submits):
    return [{"method": s["method"], "limit_price": s["limit_price"]} for s in submits]


def _sizing_view(submits):
    return [{"method": s["method"],
             "quantity": (s["quantity"] if "quantity" in s else s["position"].quantity)}
            for s in submits]


def _record_view(outcome: Dict[str, Any]) -> Dict[str, Any]:
    return {"entry_record": outcome["entry_record"], "exit_record": outcome["exit_record"]}


def _refusal_view(outcome: Dict[str, Any]) -> Dict[str, Any]:
    view = {"status": outcome["status"], "success": outcome["success"], "error": outcome["error"]}
    # A success message names the order it placed; only a refusal's wording is the finding.
    if outcome["success"] is False:
        view["message"] = outcome["message"]
    return view


_VIEWS = (
    (STAGE_OPTION_SELECTION, "selection", lambda o: _selection_view(o["submits"])),
    (STAGE_OPTION_PRICING, "pricing", lambda o: _pricing_view(o["submits"])),
    (STAGE_OPTION_SIZING, "sizing", lambda o: _sizing_view(o["submits"])),
    (STAGE_OPTION_RECORD, "record", _record_view),
    (STAGE_OPTION_REFUSAL, "refusal", _refusal_view),
)


def compare_outcomes(recorded: Dict[str, Any], produced: Dict[str, Any]
                     ) -> Tuple[Dict[str, str], List[Tuple[str, Any, Any]]]:
    """``({stage: status}, field diffs)`` -- exact, stage by stage."""
    statuses: Dict[str, str] = {}
    diffs: List[Tuple[str, Any, Any]] = []
    for stage, label, view in _VIEWS:
        found = compare_values(view(recorded), view(produced), label)
        statuses[stage] = _DIFFERENCE if found else _MATCH
        diffs += found
    return statuses, diffs


# --------------------------------------------------------------------------- #
# One action
# --------------------------------------------------------------------------- #
class _Outcome:
    """The result of replaying one action, before it becomes a report row."""

    def __init__(self, status: str, detail: str, stages: Dict[str, str],
                 diffs: Sequence[Any] = ()):
        self.status = status
        self.detail = detail
        self.stages = stages
        self.diffs = tuple(diffs)


def _all_stages(status: str) -> Dict[str, str]:
    return {stage: status for stage in STAGES}


def _missing(detail: str) -> _Outcome:
    return _Outcome(_MISSING, detail, _all_stages(_MISSING))


def _miss_text(miss: ReplayMiss) -> str:
    return str(miss)


def _clock_span(analysis: AnalysisRecord, span: Dict[str, Any]
                ) -> Tuple[List[str], List[str]]:
    """The record's clock reads (and phases) taken inside ONE action's own span.

    ``[clock_start, clock_end)`` as counted by the recorder at the action's entry and exit. The
    replay is handed no read beyond them, so an extra ``replay_now()`` call in the re-run is a
    typed ``ReplayMiss`` (the recorded action did not make it), never the NEXT action's read.
    """
    reads = list(analysis.clock_reads)
    phases = list(analysis.clock_read_phases)
    start, end = span["clock_start"], span["clock_end"]
    return reads[start:end], (phases[start:end] if len(phases) == len(reads) else [])


def action_window(observations: Sequence[ProviderObservation], start: int, end: int
                  ) -> List[ProviderObservation]:
    """The tape of ONE action: the broker reads it made itself, in recorded order.

    Exactly the observations with ``start <= invocation_seq < end`` (entry of the action to its own
    record, written at exit), of the two broker providers, and at ``tap_depth == 0``. A read made
    by another tapped read (``option_capital_equity`` calling ``get_account_snapshot``) is
    recorded too, but replay serves the OUTER read directly: leaving the inner one on the tape
    would hand its (older) value to a later direct read of the same identity.
    """
    return [o for o in observations
            if start <= o.invocation_seq < end
            and o.provider in (OPTION_PROVIDER, STOCK_PROVIDER)
            and o.tap_depth == 0]


def replay_action(bundle: SessionBundle, analysis: AnalysisRecord,
                  observation: ProviderObservation, observations: Sequence[ProviderObservation],
                  clock_start: int) -> Tuple[_Outcome, int]:
    """Replay ONE recorded action. Returns the outcome and the clock index to continue from."""
    try:
        payload = bundle.decode(observation.payload_object)
    except Exception as exc:  # noqa: BLE001 -- a payload the codec refuses is a coverage gap
        return _missing(f"the recorded action could not be decoded: {type(exc).__name__}: {exc}"), \
            clock_start
    if payload["version"] != oar.RECORD_VERSION:
        return _missing(f"unknown option action record version {payload['version']!r}"), \
            clock_start

    if "tape_span" not in payload:
        return _missing("the action record carries no tape_span, so the reads that were the "
                        "action's own cannot be told from the evaluator's"), clock_start
    start = payload["tape_span"]["start"]
    if start is None:
        return _missing("tape_span.start was not captured"), clock_start
    marked = [s["method"] for s in payload["outcome"]["submits"] if "unencodable" in s]
    if marked:
        return _missing(f"the recorded {', '.join(marked)} submit could not be stored by the "
                        f"codec (unencodable marker), so there is nothing to compare"), clock_start
    window = action_window(observations, start, observation.invocation_seq)

    decision_time = oar.replay_decision_time(payload)
    span = payload["tape_span"]
    if span.get("clock_start") is None or span.get("clock_end") is None:
        return _missing("the action record carries no clock span (tape_span.clock_start/"
                        "clock_end), so the clock reads that were the action's own cannot be "
                        "told from the neighbouring actions'"), clock_start
    reads, phases = _clock_span(analysis, span)
    if not reads or datetime.fromisoformat(reads[0]) != decision_time:
        return _missing(f"the action's decision clock read {decision_time.isoformat()} is not "
                        f"the first of its recorded clock span [{span['clock_start']}, "
                        f"{span['clock_end']}) of the record's {len(analysis.clock_reads)} reads"),             clock_start
    next_start = span["clock_end"]

    try:
        tape = SpanTape(bundle, analysis, window)
        account = _tape_account(tape, payload["inputs"], analysis.analysis_id)
        instances = oar.db_row_instances(payload)
    except Exception as exc:  # noqa: BLE001
        return _missing(f"could not rebuild the recorded inputs: {type(exc).__name__}: {exc}"), \
            next_start

    context = CaptureContext.for_replay(
        analysis_id=analysis.analysis_id, clock_reads=reads, clock_read_phases=phases,
        phase=(phases[0] if phases else ReplayStatus.PHASE_UNKNOWN),
        expert_class=analysis.expert_class, symbol=analysis.symbol, use_case=analysis.use_case,
        expert_instance_id=analysis.expert_instance_id)

    produced: Optional[Dict[str, Any]] = None
    try:
        with _scratch_trading_db(instances), _resolver(_ScratchResolver(account, analysis.analysis_id)):
            action = oar.rebuild_option_action(payload, account)
            with use_capture_context(context):
                # The recorder's own clock read is the first of the action's span.
                consumed = replay_now()
                if consumed != decision_time:
                    raise refuse("clock_read", analysis_id=analysis.analysis_id,
                                 request_identity={"recorded": decision_time.isoformat(),
                                                   "replayed": consumed.isoformat()},
                                 detail="the replayed decision clock differs from the recorded one")
                with oar.replay_effects(action) as frame:
                    result, error = None, None
                    try:
                        result = action.execute()
                    except ReplayMiss:
                        raise
                    except Exception as exc:  # noqa: BLE001 -- the action's own error is an outcome
                        error = exc
                produced = oar.build_outcome(error, result, frame, lambda value, label: value)
    except ReplayMiss as miss:
        return _missing(_miss_text(miss)), next_start
    except Exception as exc:  # noqa: BLE001 -- the replay itself could not run this action
        return _Outcome(_DIFFERENCE, f"error: {exc!r}", _all_stages(_DIFFERENCE),
                        [("replay", "", repr(exc))]), next_start

    if tape.misses:
        # The action swallowed a miss into some other outcome; the miss is the finding.
        return _missing(_miss_text(tape.misses[0])), next_start

    if produced["status"] == "error" and payload["outcome"]["status"] != "error":
        statuses, diffs = compare_outcomes(payload["outcome"], produced)
        return _Outcome(_DIFFERENCE, f"error: {produced['error']}", statuses, diffs), next_start

    statuses, diffs = compare_outcomes(payload["outcome"], produced)
    if not diffs:
        return _Outcome(_MATCH, "re-ran to the recorded contracts, limit, quantity, record "
                                "and outcome", statuses), next_start
    differing = [stage for stage in STAGES if statuses[stage] == _DIFFERENCE]
    keys = ", ".join(sorted({label for label, _r, _p in diffs}))
    return _Outcome(_DIFFERENCE, f"{len(diffs)} difference(s) in {', '.join(differing)}: {keys}",
                    statuses, diffs), next_start


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def _recorded_outcome(payload_outcome: Optional[Dict[str, Any]]) -> str:
    return "unknown" if payload_outcome is None else str(payload_outcome["status"])


def run_bundle(bundle: SessionBundle, bundle_dir: str = "") -> ReplayReport:
    """Replay every recorded option action of ``bundle`` (isolation must already be entered)."""
    report = ReplayReport(
        session_id=bundle.session.session_id,
        bundle_dir=str(bundle_dir),
        capability=CAPABILITY_OPTION_SELECTION,
    )
    stage_results: Dict[str, Dict[str, str]] = {stage: {} for stage in STAGES}
    for analysis in bundle.analyses:
        observations = sorted(bundle.observations_for(analysis.analysis_id),
                              key=lambda o: o.invocation_seq)
        clock_start = 0
        for observation in observations:
            if observation.provider != oar.OPTION_ACTION_PROVIDER:
                continue
            outcome, clock_start = replay_action(
                bundle, analysis, observation, observations, clock_start)
            identity = observation.request_identity
            row = AnalysisResult(
                analysis_id=analysis.analysis_id,
                expert_class=observation.method,
                symbol=str(identity["symbol"]),
                use_case=analysis.use_case,
                recorded_outcome=analysis.outcome,
                status=outcome.status,
                attempt_id=f"{analysis.attempt_id}#{observation.invocation_seq}",
                detail=outcome.detail,
                field_diffs=[FieldDiff.coerce(d) for d in outcome.diffs],
            )
            report.results.append(row)
            for stage in STAGES:
                stage_results[stage][row.row_id] = outcome.stages[stage]
    report.stage_results = stage_results
    return report


def run(bundle_dir, out_dir=None) -> ReplayReport:
    """Re-run every recorded option entry / close action against its own tape."""
    bundle = load_bundle(bundle_dir)
    with replay_isolation():
        report = run_bundle(bundle, str(Path(bundle_dir)))
    merge_coverage(bundle_dir, report)
    if out_dir is not None:
        report.write(out_dir)
    return report
