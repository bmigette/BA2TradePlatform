"""Stock decision-pass replay: re-run each recorded enter-market / open-positions pass on its own tape.

Design ``docs/plans/2026-11-08-stock-decision-pass-replay-design.md`` (sections 2, 3, 8). Live records,
per pass (one ``TradeManager`` capture record): ``pass_state`` (the expert, its settings, the rulesets,
the open book, every recommendation the selection saw), one ``rules`` step per recommendation, one ``size``
step per enter pass, one ``execute`` step per funded entry / exit action, and every broker read the pass
made (provider ``"broker"`` / ``"broker_options"``). This module rebuilds the pass in a scratch database
from ``pass_state`` and re-runs the SHARED code on a tape account that answers every read from the
recording:

* selection      -- ``trade_cycle.select_enter_candidates`` over the recorded recommendations (enter);
* rules          -- the real ``TradeActionEvaluator.evaluate``, then the gates of the enter loop (open
                    position, equity, route) with the same shared functions;
* sizing         -- the real ``TradeRiskManagement.size_candidate_orders`` over the candidates
                    ``trade_cycle.build_entry_candidate`` builds;
* execution      -- ``trade_cycle.stage_funded_entry`` + ``trade_cycle.submit_funded_entry`` (enter) or
                    ``TradeActionEvaluator.execute`` (exit), the account receiving the effect calls.

Comparison is EXACT (codec bytes via :func:`~app.services.replay.expert_replay.compare_values`; a numeric
difference reports its delta, never a tolerance). A read the tape does not hold is a typed
:class:`~ba2_common.core.replay.ReplayMiss` and the step's row is ``missing_capture`` naming the step and
the request. A step whose predecessor is ``missing_capture`` is ``missing_capture`` itself ("depends on
rules step seq N"), never skipped and never guessed. Any other exception out of a re-run is the step's
own outcome (``error``), compared with what live recorded.

What is NOT replayed, and said so in every report header: the smart risk manager, option entries/exits,
broker fills, and what the broker ADAPTER builds (protective legs, the adapter's own rows): the replay
compares what the shared code HANDED the account, not what an adapter did with it. The account answers
each effect call with the return live recorded.
"""
from __future__ import annotations

import inspect
from collections import deque
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ba2_common.core.replay import (
    AnalysisRecord,
    ProviderObservation,
    ReplayMiss,
    ReplayStatus,
    SessionBundle,
    load_bundle,
    use_capture_context,
)
from ba2_common.core.replay import action_record as core
from ba2_common.core.replay import decision_record as dr
from ba2_common.core.replay.account_taps import (
    ACCOUNT_PROVIDER,
    SIZING_READ_TAPPED_METHODS,
    _call_arguments,
    _plain,
)
from ba2_common.core.replay.context import CaptureContext
from ba2_common.core.replay.option_taps import OPTION_PROVIDER

from app.services.replay.expert_replay import compare_values
from app.services.replay.isolation import refuse, replay_isolation
from app.services.replay.option_selection import (
    SpanTape,
    TapeOptionsAccount,
    _ReplayExpert,
    _ScratchResolver,
    _install_refusals,
    _miss_text,
    _resolver,
    _scratch_trading_db,
)
from app.services.replay.provenance import finish_report
from app.services.replay.report import (
    STAGE_DECISION_ORDER,
    STAGE_DECISION_RULES,
    STAGE_DECISION_SIZING,
    STAGE_DECISION_SUBMIT,
    STAGE_DECISION_TPSL,
    STAGE_EXECUTION,
    STAGE_RULES_AND_SIZING,
    STAGE_SELECTION,
    AnalysisResult,
    FieldDiff,
    ReplayReport,
)

__all__ = ["STAGES", "TapeAccount", "normalise_effects", "run", "run_bundle"]

STAGES = (STAGE_SELECTION, STAGE_RULES_AND_SIZING, STAGE_EXECUTION, STAGE_DECISION_RULES,
          STAGE_DECISION_SIZING, STAGE_DECISION_TPSL, STAGE_DECISION_ORDER, STAGE_DECISION_SUBMIT)

_MATCH = ReplayStatus.COVERAGE_MATCH
_DIFFERENCE = ReplayStatus.COVERAGE_DIFFERENCE
_MISSING = ReplayStatus.COVERAGE_MISSING_CAPTURE
_UNSUPPORTED = ReplayStatus.COVERAGE_UNSUPPORTED
_NOT_RUN = ReplayStatus.COVERAGE_NOT_RUN

KIND_SELECTION = "selection"
KIND_PASS = "pass"


# --------------------------------------------------------------------------- #
# The tape account
# --------------------------------------------------------------------------- #
class RecordedEffectError(Exception):
    """An account effect that raised live: re-raised with live's own ``repr`` so the step's error text
    (``repr(exc)``) is the recorded one."""

    def __init__(self, recorded_repr: str):
        super().__init__(recorded_repr)
        self.recorded_repr = recorded_repr

    def __repr__(self) -> str:
        return self.recorded_repr


class UnrecordedEffect(Exception):
    """The re-run made an account effect call live did not make (or made it in another order)."""


#: Order fields a broker answer changes: copied from the recorded return onto the re-run's order.
_ORDER_IDENTITY = frozenset({"id", "created_at", "transaction_id", "account_id", "symbol"})


class TapeAccount(TapeOptionsAccount):
    """A broker account that answers a decision pass from its tape and submits nothing.

    The stock sizing reads (``SIZING_READ_TAPPED_METHODS``) are answered by identity lookup with the
    identity functions the taps recorded with. ``get_account_snapshot`` / ``get_positions`` were
    recorded under ``broker_options`` on an option account and under ``broker`` on a stock-only one;
    the provider is the one the tape holds. The effect methods RECORD the call (in the shape the live
    effect taps record) into the open replay frame and answer with the return live recorded.

    Subclassed per run under the RECORDED account class name (that name is part of every recorded
    identity); ``id`` is the recorded account id; ``settings`` are the recorded non-secret account
    settings.
    """

    _replay_taps_exempt = True

    def __init__(self, tape, *, account_id, analysis_id, settings, price_type_in_quote):
        super().__init__(tape, account_id=account_id, analysis_id=analysis_id,
                         price_type_in_quote=price_type_in_quote, price_type_in_underlying=False)
        self._settings_cache = dict(settings)
        self._effects: deque = deque()
        self._effect_index = 0

    # -- per step ----------------------------------------------------------------------------
    def use_tape(self, tape, effects: Sequence[Dict[str, Any]] = ()) -> None:
        """Serve the next step from ``tape``; ``effects`` are the effect calls live recorded for it."""
        self._tape = tape
        self._effects = deque(effects)
        self._effect_index = 0

    # -- tapped reads ------------------------------------------------------------------------
    def _read(self, method: str, **arguments):
        provider, identity = SIZING_READ_TAPPED_METHODS[method]
        value = identity({"self": self, **arguments})
        for candidate in (OPTION_PROVIDER, ACCOUNT_PROVIDER):
            if self._tape.identities(candidate, method):
                provider = candidate
                break
        return self._tape.take(provider, method, value)

    def get_balance(self):
        return self._read("get_balance")

    def get_account_info(self):
        return self._read("get_account_info")

    def get_account_snapshot(self):
        return self._read("get_account_snapshot")

    def get_positions(self):
        return self._read("get_positions")

    def has_pending_closing_order(self, transaction_id):
        return self._read("has_pending_closing_order", transaction_id=transaction_id)

    def get_fractionable(self, symbols):
        return self._read("get_fractionable", symbols=symbols)

    # -- effects -----------------------------------------------------------------------------
    def _effect(self, method: str, signature, args, kwargs, apply=None):
        call = _call_arguments(signature, (self, *args), kwargs)
        index = self._effect_index
        self._effect_index += 1
        recorded = self._effects[index] if index < len(self._effects) else None
        if recorded is None or recorded["method"] != method:
            core.note_effect(method, call=call)
            raise UnrecordedEffect(
                f"the re-run called {method} where live recorded "
                f"{'nothing more' if recorded is None else recorded['method']} (effect #{index + 1})")
        if "error" in recorded:
            core.note_effect(method, call=call, error=recorded["error"])
            raise RecordedEffectError(recorded["error"])
        returned = recorded["returned"] if "returned" in recorded else None
        result = apply(call, recorded, returned, args, kwargs) if apply is not None else returned
        core.note_effect(method, call=call, returned=_plain(result))
        return result

    def submit_order(self, trading_order, tp_price=None, sl_price=None, is_closing_order=False):
        def apply(call, recorded, returned, args, kwargs):
            # The broker's answer (status, broker ids, fills) is what live recorded: copy what the adapter
            # CHANGED between the order it was handed and the one it returned onto the re-run's order.
            if isinstance(returned, dict) and "row" in returned and isinstance(call.get("trading_order"), dict):
                before, after = recorded["call"]["trading_order"]["row"], returned["row"]
                from ba2_common.core.db import update_instance

                for key, value in after.items():
                    if key not in _ORDER_IDENTITY and before.get(key) != value:
                        setattr(trading_order, key, value)
                update_instance(trading_order)
                return trading_order
            return returned

        return self._effect("submit_order", _SIGNATURES["submit_order"],
                            (trading_order, tp_price, sl_price, is_closing_order), {}, apply)

    def close_transaction(self, transaction_id):
        return self._effect("close_transaction", _SIGNATURES["close_transaction"], (transaction_id,), {})

    def reduce_transaction(self, transaction_id, quantity):
        return self._effect("reduce_transaction", _SIGNATURES["reduce_transaction"],
                            (transaction_id, quantity), {})

    def adjust_tp(self, transaction, new_tp_price, source=""):
        return self._effect("adjust_tp", _SIGNATURES["adjust_tp"], (transaction, new_tp_price, source), {})

    def adjust_sl(self, transaction, new_sl_price, source=""):
        return self._effect("adjust_sl", _SIGNATURES["adjust_sl"], (transaction, new_sl_price, source), {})

    def adjust_tp_sl(self, transaction, new_tp_price=None, new_sl_price=None, source=""):
        return self._effect("adjust_tp_sl", _SIGNATURES["adjust_tp_sl"],
                            (transaction, new_tp_price, new_sl_price, source), {})


def _signatures() -> Dict[str, inspect.Signature]:
    from ba2_common.core.interfaces.AccountInterface import AccountInterface

    return {name: inspect.signature(getattr(AccountInterface, name))
            for name in ("submit_order", "close_transaction", "reduce_transaction",
                         "adjust_tp", "adjust_sl", "adjust_tp_sl")}


_SIGNATURES = _signatures()
_install_refusals(TapeAccount)

#: One class per (recorded account class name, declarations): see ``option_selection._ACCOUNT_CLASSES``.
_ACCOUNT_CLASSES: Dict[Tuple[str, Tuple[Tuple[str, Any], ...]], type] = {}


def _account_class(name: str, declarations: Dict[str, Any]) -> type:
    key = (name, tuple(sorted(declarations.items())))
    if key not in _ACCOUNT_CLASSES:
        _ACCOUNT_CLASSES[key] = type(name, (TapeAccount,), dict(declarations))
    return _ACCOUNT_CLASSES[key]


# --------------------------------------------------------------------------- #
# Views and normalisation
# --------------------------------------------------------------------------- #
def _without_status(view: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in view.items() if key != "status"}


def normalise_effects(effects: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The recorded / re-run account effects in a comparable form.

    The order and transaction rows inside an effect carry ids, timestamps and broker ids that differ
    between the live database and the scratch one for reasons that are not decisions
    (:data:`~ba2_common.core.replay.decision_record.VOLATILE_ORDER_FIELDS`); a transaction id is replaced
    by its ordinal in the step. Everything else -- prices, quantities, sides, order types, the stop handed
    to the broker -- is compared verbatim.
    """
    ordinals: Dict[Any, int] = {}

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            if set(value) == {"model", "row"} and isinstance(value["row"], dict):
                row = value["row"]
                drop = dr.VOLATILE_ORDER_FIELDS if value["model"] == "TradingOrder" else {"id", "open_date", "close_date"}
                return {"model": value["model"],
                        "row": {k: walk(v) for k, v in row.items() if k not in drop}}
            out = {}
            for key, item in value.items():
                if key == "transaction_id" and isinstance(item, int):
                    out[key] = ordinals.setdefault(item, len(ordinals) + 1)
                else:
                    out[key] = walk(item)
            return out
        return value

    return walk(list(effects))


#: Fields the execute order/transaction views do not compare: the adapter writes them (protective legs,
#: the broker's TP/SL bookkeeping on the transaction), so only the shared code's own build is compared.
_ADAPTER_TRANSACTION_FIELDS = frozenset({"take_profit", "stop_loss", "meta_data", "status", "quantity",
                                         "open_price", "close_price", "created_at"})


def _main_orders(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The entry/main orders of an execute view (protective legs wait on another order's status)."""
    return [order for order in orders
            if "depends_order_status_trigger" not in order or order["depends_order_status_trigger"] is None]


def _results_comparable(results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The action results with their database ids replaced by ordinals (a scratch database numbers its
    rows differently from the live one; which results name the SAME row is what is compared)."""
    ordinals: Dict[Tuple[str, Any], int] = {}

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key.endswith("_id") and isinstance(item, int) and not isinstance(item, bool):
                    out[key] = ordinals.setdefault((key, item), sum(1 for k, _ in ordinals if k == key) + 1)
                else:
                    out[key] = walk(item)
            return out
        return value

    return walk(list(results))


def _tpsl_prices(effects: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keys = ("tp_price", "sl_price", "new_tp_price", "new_sl_price")
    out = []
    for effect in effects:
        call = effect["call"] if "call" in effect else {}
        out.append({"method": effect["method"], **{k: call[k] for k in keys if k in call}})
    return out


def _execute_stage_views(view: Dict[str, Any]) -> Dict[str, Any]:
    effects = view["effects"]
    return {
        STAGE_DECISION_TPSL: _tpsl_prices(effects),
        STAGE_DECISION_ORDER: {
            "orders": _main_orders(view["orders"]),
            "transactions": [{k: v for k, v in t.items() if k not in _ADAPTER_TRANSACTION_FIELDS}
                             for t in view["transactions"]],
        },
        STAGE_DECISION_SUBMIT: {"error": view["error"], "results": _results_comparable(view["results"]),
                                "effects": normalise_effects(effects)},
    }


# --------------------------------------------------------------------------- #
# Result rows
# --------------------------------------------------------------------------- #
class _Row:
    """One replayed step (or pass-level fact), before it becomes a report row."""

    def __init__(self, kind: str, symbol: str, seq: int, status: str, detail: str,
                 stages: Dict[str, str], diffs: Sequence[Any] = ()):
        self.kind = kind
        self.symbol = symbol
        self.seq = seq
        self.status = status
        self.detail = detail
        self.stages = stages
        self.diffs = tuple(diffs)


def _stages(**filled: str) -> Dict[str, str]:
    stages = {stage: _NOT_RUN for stage in STAGES}
    stages.update(filled)
    return stages


def _all(kind_stages: Sequence[str], status: str) -> Dict[str, str]:
    return _stages(**{stage: status for stage in kind_stages})


#: The stages each kind of row fills.
_KIND_STAGES = {
    KIND_SELECTION: (STAGE_SELECTION,),
    dr.KIND_RULES: (STAGE_RULES_AND_SIZING, STAGE_DECISION_RULES),
    dr.KIND_SIZE: (STAGE_RULES_AND_SIZING, STAGE_DECISION_SIZING),
    dr.KIND_EXECUTE: (STAGE_EXECUTION, STAGE_DECISION_TPSL, STAGE_DECISION_ORDER, STAGE_DECISION_SUBMIT),
    KIND_PASS: (),
}


def _row(kind: str, symbol: str, seq: int, status: str, detail: str,
         diffs: Sequence[Any] = (), stages: Optional[Dict[str, str]] = None) -> _Row:
    return _Row(kind, symbol, seq, status, detail,
                stages if stages is not None else _all(_KIND_STAGES[kind], status), diffs)


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
class _Step:
    """A recorded decision step: its observation and decoded payload."""

    def __init__(self, observation: ProviderObservation, payload: Dict[str, Any]):
        self.observation = observation
        self.payload = payload
        self.seq = observation.invocation_seq
        self.kind = observation.method
        identity = observation.request_identity
        self.symbol = str(identity["symbol"]) if "symbol" in identity else (
            str(identity["symbols"]) if "symbols" in identity else "")
        self.span = payload["tape_span"] if "tape_span" in payload else None


class _PassReplay:
    """Replays ONE recorded decision pass."""

    def __init__(self, bundle: SessionBundle, analysis: AnalysisRecord):
        self.bundle = bundle
        self.analysis = analysis
        self.observations = sorted(bundle.observations_for(analysis.analysis_id),
                                   key=lambda o: o.invocation_seq)
        self.rows: List[_Row] = []
        self.flags = dict(analysis.branch_flags)
        self.pass_name = self.flags["pass"] if "pass" in self.flags else ""
        self.summary: Dict[str, Any] = {}
        self.state_obs: Optional[ProviderObservation] = None
        self.state: Optional[Dict[str, Any]] = None
        self.steps: List[_Step] = []
        self.undecodable: List[Tuple[ProviderObservation, str]] = []
        self.account: Optional[TapeAccount] = None
        self.evaluators: Dict[str, Any] = {}
        self.rules_missing: Dict[str, str] = {}      # symbol -> why its rules step is missing_capture

    # -- decoding ------------------------------------------------------------------------
    def decode(self) -> None:
        for observation in self.observations:
            if observation.provider != dr.DECISION_PROVIDER:
                continue
            try:
                payload = self.bundle.decode(observation.payload_object)
            except Exception as exc:  # noqa: BLE001 -- a payload the codec refuses is a coverage gap
                self.undecodable.append((observation, f"{type(exc).__name__}: {exc}"))
                continue
            if observation.method == dr.KIND_PASS_STATE:
                self.state_obs, self.state = observation, payload
            else:
                self.steps.append(_Step(observation, payload))
        if self.analysis.decision_object:
            self.summary = self.bundle.decode(self.analysis.decision_object)

    def steps_of(self, kind: str) -> List[_Step]:
        return [step for step in self.steps if step.kind == kind]

    # -- windows -------------------------------------------------------------------------
    def _window(self, start: int, end: int) -> List[ProviderObservation]:
        """The tape of one stretch of the pass: its depth-0 reads that were not decision steps."""
        return [o for o in self.observations
                if start <= o.invocation_seq < end and o.provider != dr.DECISION_PROVIDER
                and o.tap_depth == 0]

    def _tape(self, start: int, end: int) -> SpanTape:
        return SpanTape(self.bundle, self.analysis, self._window(start, end))

    def _previous_end(self, step: _Step) -> Tuple[int, int]:
        """``(observation seq, clock index)`` where the stretch before ``step`` begins."""
        previous = [s for s in self.steps if s.seq < step.seq]
        if not previous:
            return (self.state_obs.invocation_seq + 1 if self.state_obs else 0), 1
        last = max(previous, key=lambda s: s.seq)
        return last.seq + 1, (last.span["clock_end"] if last.span else 1)

    def _gap_after(self, step: _Step) -> Tuple[int, int, int, int]:
        """The reads/clock between ``step`` and the next recorded step (or the pass' end)."""
        following = [s for s in self.steps if s.seq > step.seq]
        end_seq = min((s.span["start"] for s in following if s.span), default=None)
        if end_seq is None:
            end_seq = max((o.invocation_seq for o in self.observations), default=step.seq) + 1
        nxt = min(following, key=lambda s: s.seq) if following else None
        clock_end = nxt.span["clock_start"] if nxt and nxt.span else len(self.analysis.clock_reads)
        return step.seq + 1, end_seq, step.span["clock_end"], clock_end

    def _context(self, start: int, end: int) -> CaptureContext:
        reads = list(self.analysis.clock_reads)
        phases = list(self.analysis.clock_read_phases)
        return CaptureContext.for_replay(
            analysis_id=self.analysis.analysis_id, clock_reads=reads[start:end],
            clock_read_phases=(phases[start:end] if len(phases) == len(reads) else []),
            phase=ReplayStatus.PHASE_UNKNOWN, expert_class=self.analysis.expert_class,
            symbol=self.analysis.symbol, use_case=self.analysis.use_case,
            expert_instance_id=self.analysis.expert_instance_id, branch_flags=self.flags)

    # -- run -----------------------------------------------------------------------------
    def run(self) -> List[_Row]:
        self.decode()
        if self.state is None:
            self._all_missing("the pass' pass_state was not recorded or could not be decoded"
                              + ("".join(f" [{o.method}: {why}]" for o, why in self.undecodable)))
            return self.rows
        for observation, why in self.undecodable:
            self.rows.append(_row(observation.method, str(observation.request_identity.get("symbol", "")),
                                  observation.invocation_seq, _MISSING,
                                  f"the recorded {observation.method} step could not be decoded: {why}"))
        for failed in (self.summary["steps_not_recorded"] if "steps_not_recorded" in self.summary else []):
            kind = failed["kind"]
            if kind in _KIND_STAGES and kind != dr.KIND_PASS_STATE:
                self.rules_missing.setdefault(failed["symbol"], f"the recorder could not record the step: {failed['error']}") \
                    if kind == dr.KIND_RULES else None
                self.rows.append(_row(kind, failed["symbol"], 0, _MISSING,
                                      f"{kind} step not recorded: {failed['error']}"))
            elif kind == dr.KIND_PASS_STATE:
                self.rows.append(_row(KIND_PASS, "", 0, _MISSING, f"pass_state not recorded: {failed['error']}"))
        inputs = self.state["inputs"]
        try:
            instances = core.db_row_instances(self.state)
        except Exception as exc:  # noqa: BLE001
            self._all_missing(f"could not rebuild the recorded pass_state rows: {type(exc).__name__}: {exc}")
            return self.rows

        analysis_id = self.analysis.analysis_id
        first_tape = self._tape(0, 0)
        quotes = [o for o in self.observations if o.provider == ACCOUNT_PROVIDER
                  and o.method == "get_instrument_current_price"]
        price_type_in_quote = (not quotes) or "price_type" in quotes[0].request_identity
        account_class = _account_class(inputs["account_class"], inputs["account_declarations"])
        self.account = account_class(
            first_tape, account_id=inputs["account_id"], analysis_id=analysis_id,
            settings=inputs["account_settings"] if "account_settings" in inputs else {},
            price_type_in_quote=price_type_in_quote)
        try:
            with ExitStack() as stack:
                stack.enter_context(_scratch_trading_db(instances))
                stack.enter_context(_resolver(_ScratchResolver(self.account, analysis_id)))
                self._indicator_seam(stack)
                if self.pass_name == "enter-market":
                    self._enter(inputs)
                else:
                    self._exit(inputs)
        except Exception as exc:  # noqa: BLE001 -- the replay itself could not run this pass
            self.rows.append(_row(KIND_PASS, "", 0, _DIFFERENCE, f"error: {exc!r}",
                                  [("replay", "", repr(exc))]))
        return self.rows

    def _all_missing(self, detail: str) -> None:
        self.rows.append(_row(KIND_PASS, "", 0, _MISSING, detail))
        for step in self.steps:
            self.rows.append(_row(step.kind, step.symbol, step.seq, _MISSING, f"depends on pass_state: {detail}"))

    def _indicator_seam(self, stack: ExitStack) -> None:
        """Serve the ATR read the RM makes from the tape, when live recorded one (else no provider, as live)."""
        identities = [o for o in self.observations if o.provider == "indicators" and o.method == "get_indicator"]
        if not identities:
            return
        from ba2_common.core import TradeRiskManagement as rm_module

        shim = _TapeIndicatorProvider(self, identities[0].request_identity["provider"])
        previous = rm_module.get_indicator_provider_resolver()
        rm_module.set_indicator_provider_resolver(lambda _expert_id: shim)
        stack.callback(rm_module.set_indicator_provider_resolver, previous)

    # -- one step ------------------------------------------------------------------------
    def _run_step(self, tape: SpanTape, context: CaptureContext, effects, body):
        """Run ``body(frame)`` under a replay context on ``tape``; returns ``(value, error, frame)``.

        A ``ReplayMiss`` propagates (the caller turns it into ``missing_capture``); any other exception is
        the step's own outcome.
        """
        self.account.use_tape(tape, effects)
        with use_capture_context(context), core.replay_effects(None, True) as frame:
            try:
                return body(frame), None, frame
            except ReplayMiss:
                raise
            except Exception as exc:  # noqa: BLE001 -- the step's own error is an outcome
                return None, exc, frame

    def _miss_row(self, kind, symbol, seq, miss: ReplayMiss, tape=None) -> _Row:
        return _row(kind, symbol, seq, _MISSING, f"{kind} step{(' ' + symbol) if symbol else ''}: {_miss_text(miss)}")

    def _swallowed(self, tape: SpanTape) -> Optional[ReplayMiss]:
        return tape.misses[0] if tape.misses else None

    # -- enter ---------------------------------------------------------------------------
    def _enter(self, inputs: Dict[str, Any]) -> None:
        from ba2_common.core.models import ExpertRecommendation
        from ba2_common.core.trade_cycle import (
            ENTRY_ROUTE_OPTION, build_entry_candidate, fired_entry_lot_size, select_enter_candidates)
        from ba2_common.core.db import get_db, get_instance
        from ba2_common.core.hold_entry import evaluate_hold_entries
        from ba2_common.core.types import OrderRecommendation
        from sqlmodel import select

        expert_id = inputs["expert_instance_id"]
        expert = _ScratchResolver(self.account, self.analysis.analysis_id).get_expert_instance(expert_id)
        recorded_selected = list(inputs["recommendations_selected"])

        # -- Selection: recomputed from the recorded rows --------------------------------------
        with get_db() as session:
            statement = select(ExpertRecommendation).where(
                ExpertRecommendation.instance_id == expert_id,
                ExpertRecommendation.created_at >= inputs["cutoff_time"],
            ).order_by(ExpertRecommendation.created_at.desc())
            if not evaluate_hold_entries(expert.settings):
                statement = statement.where(ExpertRecommendation.recommended_action != OrderRecommendation.HOLD)
            fetched = session.exec(statement).all()
            recommendations = select_enter_candidates(fetched)
            produced_ids = [r.id for r in recommendations]
        by_id = {rec_id: get_instance(ExpertRecommendation, rec_id) for rec_id in produced_ids}
        found = compare_values(recorded_selected, produced_ids, "recommendations_selected", with_deltas=True)
        self.rows.append(_row(KIND_SELECTION, "", 0, _DIFFERENCE if found else _MATCH,
                              (f"{len(found)} difference(s) in the selected recommendations" if found else
                               f"re-selected the recorded {len(produced_ids)} recommendation(s) in the recorded order"),
                              found))
        if found:
            self._skip_remaining("the selection differs from the recorded one")
            return

        ruleset_id = inputs["ruleset_id"]
        rules_by_symbol = {s.symbol: s for s in self.steps_of(dr.KIND_RULES)}
        candidates: List[Tuple[Any, Any, Any]] = []         # (candidate, evaluator, recommendation)
        gates_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        symbols_summary = self.summary["symbols"] if "symbols" in self.summary else {}

        for recommendation in [by_id[i] for i in produced_ids]:
            symbol = recommendation.symbol
            step = rules_by_symbol.get(symbol)
            if step is None:
                if symbol not in self.rules_missing:
                    self.rules_missing[symbol] = "no rules step was recorded for it"
                    self.rows.append(_row(dr.KIND_RULES, symbol, 0, _MISSING,
                                          f"rules step {symbol}: no rules step was recorded for the selected recommendation"))
                continue
            row, evaluator, summaries = self._replay_rules(step, recommendation, ruleset_id, None, None)
            self.rows.append(row)
            if row.status == _MISSING:
                self.rules_missing[symbol] = f"depends on rules step {symbol} (seq {step.seq}): {row.detail}"
                continue
            self.evaluators[symbol] = evaluator
            actions = summaries or []
            if not actions or any("error" in s for s in actions):
                continue
            # -- gates (the enter loop's, in its order), on the stretch between this step and the next --
            recorded_gates = (symbols_summary[symbol]["gates"] if symbol in symbols_summary else [])
            gate_row = self._replay_gates(step, symbol, recommendation, expert, actions, recorded_gates)
            self._merge_gate_into_rules(row, gate_row[0])
            if gate_row[1] is None:                               # the gates could not be read from the tape
                self.rules_missing[symbol] = f"depends on the gates of {symbol} (rules step seq {step.seq}): {gate_row[0].detail}"
                continue
            passed, route = gate_row[1]
            if not passed or route == ENTRY_ROUTE_OPTION:
                continue
            candidate = build_entry_candidate(recommendation, self.account.id, lot_size=fired_entry_lot_size(evaluator))
            candidates.append((candidate, evaluator, recommendation))

        # -- sizing ------------------------------------------------------------------------------
        size_steps = self.steps_of(dr.KIND_SIZE)
        execute_by_symbol = {s.symbol: s for s in self.steps_of(dr.KIND_EXECUTE)}
        funded = None
        size_row: Optional[_Row] = None
        size_step = size_steps[0] if size_steps else None
        if self.rules_missing:
            why = "; ".join(f"{sym}: {reason}" for sym, reason in sorted(self.rules_missing.items()))
            for step in size_steps:
                self.rows.append(_row(dr.KIND_SIZE, step.symbol, step.seq, _MISSING,
                                      f"size step depends on the rules of every candidate: {why}"))
            size_row = _row(dr.KIND_SIZE, "", 0, _MISSING, f"depends on the rules steps: {why}")
        elif not candidates:
            for step in size_steps:
                self.rows.append(_row(dr.KIND_SIZE, step.symbol, step.seq, _DIFFERENCE,
                                      "size step: live sized candidates but the re-run builds none (no rule fires, or a gate stops them)",
                                      [("candidates", f"{step.symbol}", "<none>")]))
            size_row = _row(dr.KIND_SIZE, "", 0, _DIFFERENCE, "the re-run builds no candidate")
        elif size_step is None:
            self.rows.append(_row(dr.KIND_SIZE, "", 0, _DIFFERENCE,
                                  "size step: the re-run sizes candidates but live recorded no size step",
                                  [("size", "<not recorded>", "sized")]))
            size_row = _row(dr.KIND_SIZE, "", 0, _DIFFERENCE, "no recorded size step")
        else:
            funded, size_row = self._replay_size(size_step, candidates, expert_id)
            self.rows.append(size_row)
            if size_row.status in (_MISSING, _UNSUPPORTED):
                funded = None

        # -- execution (per funded entry, in candidate order) ------------------------------------
        funded_by_symbol = {} if funded is None else {o.symbol: o for o in funded}
        for candidate, evaluator, recommendation in candidates:
            symbol = candidate.symbol
            step = execute_by_symbol.pop(symbol, None)
            fo = funded_by_symbol.get(symbol)
            if funded is None:
                if step is not None:
                    self.rows.append(_row(dr.KIND_EXECUTE, symbol, step.seq, _MISSING,
                                          f"execute step {symbol} depends on the size step: {size_row.detail}"))
                continue
            funded_now = fo is not None and fo.quantity and fo.quantity > 0
            if step is None and funded_now:
                self.rows.append(_row(dr.KIND_EXECUTE, symbol, 0, _DIFFERENCE,
                                      f"execute step {symbol}: the re-run funds it ({fo.quantity}) but live recorded no execute",
                                      [(f"execute[{symbol}]", "<not recorded>", f"funded {fo.quantity}")]))
                continue
            if step is not None and not funded_now:
                self.rows.append(_row(dr.KIND_EXECUTE, symbol, step.seq, _DIFFERENCE,
                                      f"execute step {symbol}: live executed it but the re-run does not fund it",
                                      [(f"execute[{symbol}]", "executed", "not funded")]))
                continue
            if step is None:
                continue
            self.rows.append(self._replay_entry_execute(step, evaluator, recommendation, fo))
        # Recorded executes the re-run has no candidate for: named, never dropped.
        for step in execute_by_symbol.values():
            if self.rules_missing or (size_row is not None and size_row.status == _MISSING):
                status, why = _MISSING, f"depends on the size step / rules steps: {size_row.detail if size_row else ''}"
                diffs = ()
            else:
                status, why = _DIFFERENCE, "live executed it but the re-run built no candidate for it"
                diffs = [(f"execute[{step.symbol}]", "executed", "no candidate")]
            self.rows.append(_row(dr.KIND_EXECUTE, step.symbol, step.seq, status,
                                  f"execute step {step.symbol}: {why}", diffs))

    def _skip_remaining(self, why: str) -> None:
        for step in self.steps:
            self.rows.append(_row(step.kind, step.symbol, step.seq, _MISSING, f"not replayed: {why}"))

    # -- rules ---------------------------------------------------------------------------
    def _replay_rules(self, step: _Step, recommendation, ruleset_id, existing_transactions, existing_order,
                      *, pre_reads: Optional[Tuple[int, int]] = None):
        from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

        symbol = step.symbol
        if step.span is None or step.span["start"] is None or step.span["clock_start"] is None:
            return _row(dr.KIND_RULES, symbol, step.seq, _MISSING, "the rules step carries no tape_span"), None, None
        tape = self._tape(step.span["start"], step.seq)
        context = self._context(step.span["clock_start"], step.span["clock_end"])
        evaluator_box: Dict[str, Any] = {}

        def body(frame):
            evaluator = TradeActionEvaluator(account=self.account, instrument_name=symbol,
                                             existing_transactions=existing_transactions)
            evaluator_box["evaluator"] = evaluator
            return evaluator.evaluate(instrument_name=symbol, expert_recommendation=recommendation,
                                      ruleset_id=ruleset_id, existing_order=existing_order)

        try:
            summaries, error, _frame = self._run_step(tape, context, (), body)
        except ReplayMiss as miss:
            return self._miss_row(dr.KIND_RULES, symbol, step.seq, miss), None, None
        swallowed = self._swallowed(tape)
        if swallowed is not None:
            return self._miss_row(dr.KIND_RULES, symbol, step.seq, swallowed), None, None
        evaluator = evaluator_box["evaluator"]
        details = evaluator.get_evaluation_details() if error is None else None
        produced = dr.rules_view(summaries, details, None if error is None else repr(error))
        found = compare_values(_without_status(step.payload["outcome"]), produced, "rules", with_deltas=True)
        status = _DIFFERENCE if found else _MATCH
        detail = (f"{len(found)} difference(s) in the rules evaluation" if found
                  else "re-evaluated the ruleset to the recorded actions and condition values")
        return _row(dr.KIND_RULES, symbol, step.seq, status, detail, found), evaluator, summaries

    def _merge_gate_into_rules(self, rules_row: _Row, gate_row: _Row) -> None:
        """The gate verdicts are part of the symbol's Rules finding: a gate difference / missing_capture
        is folded into its rules row (a match changes nothing)."""
        if gate_row.status == _MATCH:
            return
        if gate_row.status == _MISSING:
            rules_row.status = _MISSING
            rules_row.detail = gate_row.detail
        else:
            rules_row.diffs = tuple(rules_row.diffs) + tuple(gate_row.diffs)
            if rules_row.status != _MISSING:
                rules_row.status = _DIFFERENCE
            rules_row.detail = f"{rules_row.detail}; gates: {gate_row.detail}"
        rules_row.stages = _all(_KIND_STAGES[dr.KIND_RULES], rules_row.status)

    def _replay_gates(self, step: _Step, symbol, recommendation, expert, actions, recorded_gates):
        """Re-run the enter loop's gates; returns ``(row, (passed, route) | None)`` (None: missing_capture)."""
        from ba2_common.core.trade_cycle import MixedEntryActionsRefused, entry_route, open_or_waiting_position

        start, end, clock_start, clock_end = self._gap_after(step)
        tape = self._tape(start, end)
        context = self._context(clock_start, clock_end)
        verdicts: List[Dict[str, Any]] = []
        outcome: Dict[str, Any] = {}

        def body(frame):
            existing = open_or_waiting_position(expert.id, symbol)
            verdicts.append({"name": "open_or_waiting_position", "passed": existing is None,
                             "detail": None if existing is None else f"transaction {existing.id} {existing.status}"})
            if existing:
                outcome["stop"] = True
                return
            ok, reason = expert.has_sufficient_equity_for_trading()
            verdicts.append({"name": "sufficient_equity", "passed": bool(ok), "detail": reason})
            if not ok:
                outcome["stop"] = True
                return
            try:
                route = entry_route(s["action_type"] for s in actions)
            except MixedEntryActionsRefused as mixed:
                verdicts.append({"name": "entry_route", "passed": False, "detail": str(mixed)})
                outcome["stop"] = True
                return
            verdicts.append({"name": "entry_route", "passed": True, "detail": str(route)})
            outcome["route"] = route

        try:
            _, error, _ = self._run_step(tape, context, (), body)
        except ReplayMiss as miss:
            return _row(dr.KIND_RULES, symbol, step.seq, _MISSING,
                        f"gates of {symbol} (after the rules step): {_miss_text(miss)}"), None
        swallowed = self._swallowed(tape)
        if swallowed is not None:
            return _row(dr.KIND_RULES, symbol, step.seq, _MISSING,
                        f"gates of {symbol} (after the rules step): {_miss_text(swallowed)}"), None
        if error is not None:
            verdicts.append({"name": "error", "passed": False, "detail": repr(error)})
        found = compare_values(list(recorded_gates), verdicts, f"gates[{symbol}]", with_deltas=True)
        status = _DIFFERENCE if found else _MATCH
        row = _row(dr.KIND_RULES, symbol, step.seq, status,
                   f"{len(found)} difference(s) in the gate verdicts" if found else "gate verdicts match", found)
        passed = not outcome.get("stop", False) and error is None
        return row, (passed, outcome.get("route"))

    # -- sizing --------------------------------------------------------------------------
    def _replay_size(self, step: _Step, candidates, expert_id):
        from ba2_common.core.TradeRiskManagement import TradeRiskManagement

        if step.span is None or step.span["start"] is None or step.span["clock_start"] is None:
            return None, _row(dr.KIND_SIZE, step.symbol, step.seq, _MISSING, "the size step carries no tape_span")
        recorded_inputs = step.payload["inputs"]
        if recorded_inputs["option_decisions"]:
            return None, _row(dr.KIND_SIZE, step.symbol, step.seq, _UNSUPPORTED,
                              "the pass also contains option entries, which the equity sizing context includes")
        tape = self._tape(step.span["start"], step.seq)
        context = self._context(step.span["clock_start"], step.span["clock_end"])
        manager = TradeRiskManagement(indicator_provider=None, as_of=recorded_inputs["risk_manager"]["as_of"])
        # The candidates as the RM receives them (it sizes them IN PLACE: quantity and stop are written on them).
        candidates_in = [_candidate_fields(core.dump_row(c)["row"]) for (c, _e, _r) in candidates]

        def body(frame):
            return manager.size_candidate_orders(expert_id, [(c, rec) for (c, _e, rec) in candidates])

        try:
            funded, error, frame = self._run_step(tape, context, (), body)
        except ReplayMiss as miss:
            return None, self._miss_row(dr.KIND_SIZE, step.symbol, step.seq, miss)
        swallowed = self._swallowed(tape)
        if swallowed is not None:
            return None, self._miss_row(dr.KIND_SIZE, step.symbol, step.seq, swallowed)
        run = frame.records["sizing_run"] if "sizing_run" in frame.records else None
        produced = dr.sizing_view(funded if error is None else [], run, None if error is None else repr(error))
        recorded = _without_status(step.payload["outcome"])
        found = compare_values(recorded, produced, "sizing", with_deltas=True)
        candidates_found = compare_values(
            [_candidate_fields(c["order"]) for c in recorded_inputs["candidates"]],
            candidates_in, "candidates", with_deltas=True)
        found = candidates_found + found
        status = _DIFFERENCE if found else _MATCH
        detail = (f"{len(found)} difference(s) in the sizing" if found
                  else "re-sized to the recorded funded set, stops, decisions and context")
        return (funded if error is None else []), _row(dr.KIND_SIZE, step.symbol, step.seq, status, detail, found)

    # -- execution -----------------------------------------------------------------------
    def _replay_entry_execute(self, step: _Step, evaluator, recommendation, fo) -> _Row:
        from ba2_common.core.trade_cycle import stage_funded_entry, submit_funded_entry

        symbol = step.symbol
        if step.span is None or step.span["start"] is None or step.span["clock_start"] is None:
            return _row(dr.KIND_EXECUTE, symbol, step.seq, _MISSING, "the execute step carries no tape_span")
        tape = self._tape(step.span["start"], step.seq)
        context = self._context(step.span["clock_start"], step.span["clock_end"])
        recorded_effects = step.payload["outcome"]["effects"]
        marked = [e["method"] for e in recorded_effects if "unencodable" in e]
        if marked:
            return _row(dr.KIND_EXECUTE, symbol, step.seq, _MISSING,
                        f"the recorded {', '.join(marked)} effect could not be stored by the codec")
        staged_box: Dict[str, Any] = {}

        def body(frame):
            staged = stage_funded_entry(evaluator, symbol, fo.quantity, fo.stop_price or None)
            staged_box["staged"] = staged
            if staged.order is None:
                return None
            submitted = submit_funded_entry(self.account, staged.order, fo.stop_price or None,
                                            retries=3, backoff_s=0.0, sleep=lambda _s: None)
            if not submitted:
                raise UnrecordedEffect("submit_funded_entry returned nothing (live compensates the unsent entry)")
            return submitted

        try:
            _, error, frame = self._run_step(tape, context, recorded_effects, body)
        except ReplayMiss as miss:
            return self._miss_row(dr.KIND_EXECUTE, symbol, step.seq, miss)
        swallowed = self._swallowed(tape)
        if swallowed is not None:
            return self._miss_row(dr.KIND_EXECUTE, symbol, step.seq, swallowed)
        staged = staged_box["staged"] if "staged" in staged_box else None
        return self._execute_row(step, staged.results if staged else None, staged.order if staged else None,
                                 frame, error)

    def _execute_row(self, step: _Step, results, order, frame, error) -> _Row:
        rows = dr.execute_rows_after(results, order)
        effects = list(frame.submits)
        produced = dr.execute_view(results, effects, rows, None if error is None else repr(error))
        recorded = step.payload["outcome"]
        recorded_stage = _execute_stage_views(recorded)
        produced_stage = _execute_stage_views(produced)
        diffs: List[Any] = []
        stages: Dict[str, str] = {STAGE_EXECUTION: _MATCH}
        for stage in (STAGE_DECISION_TPSL, STAGE_DECISION_ORDER, STAGE_DECISION_SUBMIT):
            found = compare_values(recorded_stage[stage], produced_stage[stage], stage, with_deltas=True)
            stages[stage] = _DIFFERENCE if found else _MATCH
            diffs += found
        if diffs:
            stages[STAGE_EXECUTION] = _DIFFERENCE
        status = _DIFFERENCE if diffs else _MATCH
        detail = (f"{len(diffs)} difference(s) in the execution" if diffs else
                  "re-executed to the recorded order build, protective prices and account effects")
        return _row(dr.KIND_EXECUTE, step.symbol, step.seq, status, detail, diffs,
                    stages=_stages(**stages))

    # -- exit ----------------------------------------------------------------------------
    def _exit(self, inputs: Dict[str, Any]) -> None:
        from ba2_common.core.entry_order import EntryOrderUnresolved, oldest_entry_order, positions_to_manage
        from ba2_common.core.db import get_db
        from ba2_common.core.models import ExpertRecommendation, Transaction
        from ba2_common.core.types import TransactionStatus
        from sqlmodel import select

        expert_id = inputs["expert_instance_id"]
        ruleset_id = inputs["ruleset_id"]
        rules_by_symbol = {s.symbol: s for s in self.steps_of(dr.KIND_RULES)}
        execute_by_symbol = {s.symbol: s for s in self.steps_of(dr.KIND_EXECUTE)}
        with get_db() as session:
            selected = []
            for rec_id in inputs["recommendations_selected"]:
                rec = session.get(ExpertRecommendation, rec_id)
                selected.append(rec)
            for recommendation in selected:
                symbol = recommendation.symbol
                step = rules_by_symbol.get(symbol)
                # The book: reads (has_pending_closing_order) happen BEFORE the rules step, on the stretch
                # between the previous step and this one.
                if step is not None:
                    start, clock_start = self._previous_end(step)
                    pre_tape = self._tape(start, step.span["start"] if step.span else start)
                    pre_context = self._context(clock_start, step.span["clock_start"] if step.span else clock_start)
                else:
                    pre_tape, pre_context = self._tape(0, 0), self._context(0, 0)
                book: Dict[str, Any] = {}

                def body(frame):
                    all_transactions = session.exec(select(Transaction).where(
                        Transaction.symbol == symbol, Transaction.status == TransactionStatus.OPENED,
                        Transaction.expert_id == expert_id)).all()
                    existing = positions_to_manage(all_transactions, self.account)
                    book["transactions"] = existing
                    if not existing:
                        return None
                    try:
                        book["order"] = oldest_entry_order(existing, session=session)
                    except EntryOrderUnresolved as exc:
                        book["unresolved"] = str(exc)

                try:
                    _, error, _ = self._run_step(pre_tape, pre_context, (), body)
                except ReplayMiss as miss:
                    if step is not None:
                        self.rows.append(_row(dr.KIND_RULES, symbol, step.seq, _MISSING,
                                              f"book of {symbol} (before the rules step): {_miss_text(miss)}"))
                        self.rules_missing[symbol] = "its book could not be read from the tape"
                    continue
                if error is not None:
                    self.rows.append(_row(KIND_PASS, symbol, 0, _DIFFERENCE, f"error reading the book of {symbol}: {error!r}",
                                          [("book", "", repr(error))]))
                    continue
                if not book.get("transactions") or "unresolved" in book:
                    if step is not None:
                        self.rows.append(_row(dr.KIND_RULES, symbol, step.seq, _DIFFERENCE,
                                              f"live evaluated {symbol} but the re-run has no position to manage",
                                              [(f"book[{symbol}]", "positions", "none")]))
                    continue
                if step is None:
                    self.rows.append(_row(dr.KIND_RULES, symbol, 0, _MISSING,
                                          f"rules step {symbol}: no rules step was recorded for a position the re-run manages"))
                    continue
                row, evaluator, summaries = self._replay_rules(
                    step, recommendation, ruleset_id, book["transactions"], book["order"])
                self.rows.append(row)
                if row.status == _MISSING:
                    self.rules_missing[symbol] = row.detail
                    continue
                actions = summaries or []
                exec_step = execute_by_symbol.pop(symbol, None)
                fires = bool(actions) and not any("error" in s for s in actions)
                if exec_step is None:
                    if fires:
                        self.rows.append(_row(dr.KIND_EXECUTE, symbol, 0, _DIFFERENCE,
                                              f"execute step {symbol}: the re-run fires actions but live recorded no execute",
                                              [(f"execute[{symbol}]", "<not recorded>", "executed")]))
                    continue
                if not fires:
                    self.rows.append(_row(dr.KIND_EXECUTE, symbol, exec_step.seq, _DIFFERENCE,
                                          f"execute step {symbol}: live executed but the re-run fires no action",
                                          [(f"execute[{symbol}]", "executed", "no action")]))
                    continue
                self.rows.append(self._replay_exit_execute(exec_step, evaluator))
        for step in execute_by_symbol.values():
            self.rows.append(_row(dr.KIND_EXECUTE, step.symbol, step.seq, _MISSING,
                                  f"execute step {step.symbol}: its rules step is missing_capture or the symbol was not re-evaluated"))

    def _replay_exit_execute(self, step: _Step, evaluator) -> _Row:
        symbol = step.symbol
        if step.span is None or step.span["start"] is None or step.span["clock_start"] is None:
            return _row(dr.KIND_EXECUTE, symbol, step.seq, _MISSING, "the execute step carries no tape_span")
        tape = self._tape(step.span["start"], step.seq)
        context = self._context(step.span["clock_start"], step.span["clock_end"])
        recorded_effects = step.payload["outcome"]["effects"]
        marked = [e["method"] for e in recorded_effects if "unencodable" in e]
        if marked:
            return _row(dr.KIND_EXECUTE, symbol, step.seq, _MISSING,
                        f"the recorded {', '.join(marked)} effect could not be stored by the codec")
        submit_to_broker = step.payload["inputs"]["submit_to_broker"]

        def body(frame):
            return evaluator.execute(submit_to_broker=submit_to_broker)

        try:
            results, error, frame = self._run_step(tape, context, recorded_effects, body)
        except ReplayMiss as miss:
            return self._miss_row(dr.KIND_EXECUTE, symbol, step.seq, miss)
        swallowed = self._swallowed(tape)
        if swallowed is not None:
            return self._miss_row(dr.KIND_EXECUTE, symbol, step.seq, swallowed)
        return self._execute_row(step, results, None, frame, error)


def _candidate_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if k not in dr.VOLATILE_ORDER_FIELDS}


class _TapeIndicatorProvider:
    """Serves ``get_indicator`` (the ATR read of the RM) from the tape, by the recorded identity."""

    def __init__(self, replay: "_PassReplay", recorded_class_name: str):
        self._replay = replay
        self.__class__ = type(recorded_class_name, (_TapeIndicatorProvider,), {})

    def get_indicator(self, symbol, indicator, start_date=None, end_date=None, lookback_days=None,
                      interval="1d", format_type="markdown", **extra):
        from ba2_common.core.interfaces.MarketIndicatorsInterface import indicator_identity

        identity = indicator_identity({
            "self": self, "symbol": symbol, "indicator": indicator, "start_date": start_date,
            "end_date": end_date, "lookback_days": lookback_days, "interval": interval,
            "format_type": format_type, **extra})
        return self._replay.account._tape.take("indicators", "get_indicator", identity)


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def _pass_rows(bundle: SessionBundle, analysis: AnalysisRecord) -> List[_Row]:
    if analysis.outcome == ReplayStatus.OUTCOME_SKIP:
        return [_row(KIND_PASS, "", 0, _NOT_RUN,
                     f"the pass ended as a recorded decision not to act ({analysis.skip_reason}); no step to replay")]
    if analysis.outcome == ReplayStatus.OUTCOME_ERROR:
        return [_row(KIND_PASS, "", 0, _MISSING, f"the pass was recorded as an error: {analysis.error}")]
    if analysis.outcome != ReplayStatus.OUTCOME_DECISION:
        return [_row(KIND_PASS, "", 0, _MISSING, f"unexpected pass outcome {analysis.outcome!r}")]
    return _PassReplay(bundle, analysis).run()


def run_bundle(bundle: SessionBundle, bundle_dir: str = "") -> ReplayReport:
    """Replay every recorded decision pass of ``bundle`` (isolation must already be entered)."""
    report = ReplayReport(
        session_id=bundle.session.session_id, bundle_dir=str(bundle_dir),
        capability=ReplayStatus.CAPABILITY_DECISION)
    stage_results: Dict[str, Dict[str, str]] = {stage: {} for stage in STAGES}
    for analysis in bundle.analyses:
        if not dr.is_decision_pass(analysis):
            continue
        rows = _pass_rows(bundle, analysis)
        for position, row in enumerate(rows):
            result = AnalysisResult(
                analysis_id=analysis.analysis_id,
                expert_class=row.kind,
                symbol=row.symbol,
                use_case=analysis.use_case,
                recorded_outcome=analysis.outcome,
                status=row.status,
                attempt_id=f"{analysis.attempt_id}#{row.seq}.{position}",
                detail=row.detail,
                field_diffs=[FieldDiff.coerce(d) for d in row.diffs],
            )
            report.results.append(result)
            for stage in STAGES:
                stage_results[stage][result.row_id] = row.stages[stage]
    report.stage_results = stage_results
    return report


def run(bundle_dir, out_dir=None) -> ReplayReport:
    """Re-run every recorded stock decision pass against its own tape."""
    bundle = load_bundle(bundle_dir)
    with replay_isolation():
        report = run_bundle(bundle, str(Path(bundle_dir)))
    return finish_report(report, bundle, bundle_dir, out_dir)
