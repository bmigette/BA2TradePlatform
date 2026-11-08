"""Stock DECISION-PASS records: ``pass_state`` and the ``rules`` / ``size`` / ``execute`` steps.

Design: ``docs/plans/2026-11-08-stock-decision-pass-replay-design.md``. A live enter-market or
open-positions pass of ANY expert opens one capture scope (``TradeManager._decision_capture_scope``);
inside it this module records, on top of the family-neutral core
(:mod:`~ba2_common.core.replay.action_record`):

* ``pass_state`` -- ONE observation per pass, written when the pass reaches evaluation: the expert
  instance, its FULL resolved settings (credential-shaped keys are absent and NAMED in
  ``redacted_setting_keys``), the enter/open ruleset rows (rules + links), the account class/id and
  class declarations (never credentials), the expert's capital-holding transactions and their
  orders, the recommendation window and every recommendation the selection saw;
* ``rules`` -- one per ``TradeActionEvaluator.evaluate`` (both passes): the recommendation, the
  ruleset, the existing order / transactions and the outcome (action summaries + the evaluation
  details with the value every condition read);
* ``size`` -- one per ``TradeRiskManagement.size_candidate_orders``: the candidate orders, the funded
  set and the RM's own ``decisions`` + ``context`` (the explanation ``_record_candidate_run`` builds,
  handed over by :func:`note_sizing_run` -- no sizing code is repeated here);
* ``execute`` -- one per funded entry (execute + persist + submit) and one per exit-pass execute: the
  action results, the rows it created, and the EFFECT calls the account received
  (``submit_order`` / ``close_transaction`` / ... with the stop price handed to the broker),
  recorded by the account effect taps into a frame that ``accepts_account_effects``.

Option actions are NOT recorded here: the option action record covers them.

Everything is passthrough outside a capture scope and while replaying (one ``current_capture()``
check). A recording failure is counted (``CaptureHealth.STEP_NOT_RECORDED``), listed in the pass
summary (``steps_not_recorded``) and NEVER reaches the decision.

The ``*_view`` functions are the normalised, comparable form of each step. The recorder stores the
view as the step ``outcome``; the offline replay and (later) the backtest engine call the SAME
functions on what their re-run produced, so "same" means the same bytes.

Payload (all steps)::

    {"version": 1,
     "tape_span": {"start", "clock_start", "clock_end"},
     "inputs":   {...},            # per step, see the recorders
     "db_rows":  [{"model", "row"}, ...],
     "outcome":  {...}}            # the *_view
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ba2_common.core.replay import action_record as core
from ba2_common.core.replay.action_record import (
    NOOP_CONTEXT as _NOOP_CONTEXT,
    dump_row as _dump,
    encodable as _encodable,
    encodable_effect,
)
from ba2_common.core.replay.codec import SharedValue
from ba2_common.core.replay.context import CaptureHealth, current_capture
from ba2_common.core.replay.observe import record_observation
from ba2_common.core.replay.option_action_record import _ACCOUNT_DECLARATIONS

__all__ = [
    "DECISION_FAMILIES",
    "DECISION_PROVIDER",
    "KIND_EXECUTE",
    "KIND_PASS_STATE",
    "KIND_RULES",
    "KIND_SIZE",
    "READABLE_VERSIONS",
    "RECORD_KIND",
    "RECORD_VERSION",
    "VOLATILE_ORDER_FIELDS",
    "decision_context",
    "execute_view",
    "is_decision_pass",
    "note_funded",
    "note_gate",
    "note_pass_error",
    "note_selection",
    "note_sizing_run",
    "pass_discard",
    "pass_fail",
    "pass_skip",
    "pass_summary",
    "record_execute",
    "record_pass_state",
    "record_rules",
    "record_size",
    "rules_view",
    "sizing_frame_open",
    "sizing_view",
]

DECISION_PROVIDER = "decision_step"
RECORD_VERSION = 1
READABLE_VERSIONS = (1,)
#: ``branch_flags["record_kind"]`` of a stock decision pass.
RECORD_KIND = "decision_pass"
#: What a session records about itself (``capabilities["decision_families"]``).
DECISION_FAMILIES = ("stock_rules", "stock_size", "stock_execute")

KIND_PASS_STATE = "pass_state"
KIND_RULES = "rules"
KIND_SIZE = "size"
KIND_EXECUTE = "execute"

#: Order-row fields that differ between a live run and its re-run for reasons that are not decisions
#: (primary keys, timestamps, broker ids, links by id): excluded from ``execute_view`` rows.
VOLATILE_ORDER_FIELDS = frozenset({
    "id", "created_at", "broker_order_id", "transaction_id", "depends_on_order", "parent_order_id",
    "legs_broker_ids", "open_price",
})

#: Keys of an action summary that duplicate ``evaluation_details`` (copied into every summary).
_SUMMARY_DUPLICATES = ("condition_evaluations", "rule_evaluations")

#: Models whose rows are stored as their OWN content-addressed object (``SharedValue``) and referenced by
#: hash: the same unchanged row, in ``pass_state`` and in the steps that use it, and across consecutive
#: passes, is one object on disk. A reader decodes the reference transparently.
SHARED_ROW_MODELS = frozenset({"Transaction", "TradingOrder", "ExpertRecommendation"})


#: A row field (a recommendation's ``data`` / ``details``, an order's copy of them) at least this long (JSON
#: characters) is stored as its own object too: the order row repeats the recommendation's ``data`` verbatim,
#: and an order whose status changed keeps pointing at the same bulky field.
_BIG_FIELD_CHARS = 512


def _big_fields_shared(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (dict, list, str)) and len(
                value if isinstance(value, str) else json.dumps(value, default=str)) >= _BIG_FIELD_CHARS:
            out[key] = SharedValue(value)
        else:
            out[key] = value
    return out


def _shared_row(row: Dict[str, Any]) -> SharedValue:
    """One dumped row as a stored-once object (its bulky fields shared as well)."""
    return SharedValue(_big_fields_shared(row))


def _share(entry: Dict[str, Any]) -> Dict[str, Any]:
    """``{"model", "row"}`` with the row shared when its model repeats across passes."""
    if entry["model"] in SHARED_ROW_MODELS:
        return {"model": entry["model"], "row": _shared_row(entry["row"])}
    return entry


def _share_effect_rows(value: Any) -> Any:
    """The row dumps inside the recorded account effects (the order before/after ``submit_order``), shared."""
    if isinstance(value, list):
        return [_share_effect_rows(item) for item in value]
    if isinstance(value, dict):
        if "model" in value and "row" in value and len(value) == 2 and isinstance(value["row"], dict):
            return _share(value)
        return {k: _share_effect_rows(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# the pass context + ledger
# ---------------------------------------------------------------------------
def is_decision_pass(record_or_flags: Any) -> bool:
    """Whether an ``AnalysisRecord`` (or its ``branch_flags`` mapping) is a stock decision pass."""
    flags = getattr(record_or_flags, "branch_flags", record_or_flags)
    return "record_kind" in flags and flags["record_kind"] == RECORD_KIND


def decision_context():
    """The live capture context when it belongs to a decision pass, else ``None``.

    The guard every helper uses, so a note made while an ANALYSIS context is current (an expert
    analysing, a test) never touches the analysis' outcome.
    """
    context = current_capture()
    if context is None or context.is_replay:
        return None
    flags = context.meta["branch_flags"] if "branch_flags" in context.meta else {}
    if not is_decision_pass(flags):
        return None
    return context


def _ledger(context) -> Dict[str, Any]:
    ledger = context.decision_ledger
    if not ledger:
        ledger.update({"symbols": {}, "steps": [], "steps_not_recorded": [], "errors": []})
    return ledger


def _add_record_seconds(context, seconds: float) -> None:
    """Pass-thread time spent recording (inputs built, outcome frozen): what recording COSTS a pass."""
    ledger = _ledger(context)
    ledger["record_seconds"] = (ledger["record_seconds"] if "record_seconds" in ledger else 0.0) + seconds


def _symbol(context, symbol: str) -> Dict[str, Any]:
    symbols = _ledger(context)["symbols"]
    if symbol not in symbols:
        symbols[symbol] = {"gates": []}
    return symbols[symbol]


def note_gate(symbol: str, name: str, passed: bool, detail: Optional[str] = None) -> None:
    """A gate verdict that follows the rules step ("why was a passing rule not traded").

    Called at the duplicate-position, equity and route gates of the enter pass. A no-op outside a
    decision pass; never raises.
    """
    context = decision_context()
    if context is None:
        return
    try:
        _symbol(context, symbol)["gates"].append(
            {"name": name, "passed": bool(passed), "detail": detail})
    except Exception as exc:  # noqa: BLE001 -- recording never reaches the decision
        context.note_failure("gate verdict not noted", exc, CaptureHealth.STEP_NOT_RECORDED)


def note_pass_error(where: str, exc: BaseException) -> None:
    """An error the pass body swallowed (it logs and continues): listed in the pass summary."""
    context = decision_context()
    if context is None:
        return
    try:
        _ledger(context)["errors"].append(f"{where}: {type(exc).__name__}: {exc}")
    except Exception as note_exc:  # noqa: BLE001
        context.note_failure("pass error not noted", note_exc, CaptureHealth.STEP_NOT_RECORDED)


def note_funded(symbol: str, quantity: Any, stop_price: Any) -> None:
    """The RM-sized quantity and safeguard stop handed to a funded entry (before its execute)."""
    context = decision_context()
    if context is None:
        return
    try:
        _symbol(context, symbol)["funded"] = {"quantity": quantity, "stop_price": stop_price}
    except Exception as exc:  # noqa: BLE001
        context.note_failure("funded entry not noted", exc, CaptureHealth.STEP_NOT_RECORDED)


def note_selection(recommendations: Iterable[Any]) -> None:
    """The recommendations the pass will evaluate, in order (listed in the pass summary)."""
    context = decision_context()
    if context is None:
        return
    try:
        _ledger(context)["recommendations"] = [
            {"id": r.id, "symbol": r.symbol} for r in recommendations]
    except Exception as exc:  # noqa: BLE001
        context.note_failure("selection not noted", exc, CaptureHealth.STEP_NOT_RECORDED)


def pass_fail(reason: str) -> None:
    """The pass could not decide (expert, instance or account missing): recorded as an ERROR outcome."""
    context = decision_context()
    if context is None:
        return
    try:
        context.set_outcome(error=reason)
    except Exception as exc:  # noqa: BLE001
        context.note_failure("pass failure not recorded", exc, CaptureHealth.STEP_NOT_RECORDED)


def pass_skip(reason: str) -> None:
    """The pass ended as a DECISION not to act (automation off, no ruleset, no recommendation)."""
    context = decision_context()
    if context is None:
        return
    try:
        context.set_skip(reason)
    except Exception as exc:  # noqa: BLE001
        context.note_failure("pass skip not recorded", exc, CaptureHealth.STEP_NOT_RECORDED)


def pass_discard(reason: str) -> None:
    """The pass ended before it was a decision (lock miss, pending analysis jobs): counted, not written."""
    context = decision_context()
    if context is None:
        return
    try:
        context.discard(reason)
    except Exception as exc:  # noqa: BLE001
        context.note_failure("pass discard not recorded", exc, CaptureHealth.STEP_NOT_RECORDED)


def pass_summary(context, *, created: Sequence[Any] = ()) -> Dict[str, Any]:
    """The frozen summary of a finished pass (the ``decision`` object)."""
    ledger = _ledger(context)
    flags = context.meta["branch_flags"]
    funded = [{"symbol": symbol, **entry["funded"]}
              for symbol, entry in ledger["symbols"].items() if "funded" in entry]
    return {
        "version": RECORD_VERSION,
        "pass": flags["pass"],
        "expert_instance_id": context.meta["expert_instance_id"],
        "recommendations_considered": ledger["recommendations"] if "recommendations" in ledger else [],
        "symbols": ledger["symbols"],
        "funded": funded,
        "created_order_ids": list(created),
        "errors": list(ledger["errors"]),
        "steps": list(ledger["steps"]),
        "steps_not_recorded": list(ledger["steps_not_recorded"]),
    }


# ---------------------------------------------------------------------------
# views (the normalised, comparable form of a step)
# ---------------------------------------------------------------------------
def rules_view(action_summaries, evaluation_details, error: Optional[str] = None) -> Dict[str, Any]:
    """Rules stage: the action summaries (without the evaluation copies each carries) and ONE copy of
    the evaluation details (every condition evaluation with the values it read, every rule verdict)."""
    return {
        "error": error,
        "actions": [{k: v for k, v in summary.items() if k not in _SUMMARY_DUPLICATES}
                    for summary in (action_summaries or [])],
        "evaluation_details": evaluation_details,
    }


def sizing_view(funded, sizing_run, error: Optional[str] = None) -> Dict[str, Any]:
    """Sizing stage: the funded set (symbol, side, quantity, stop price) and the RM's own run record."""
    return {
        "error": error,
        "funded": [{"symbol": o.symbol, "side": o.side, "quantity": o.quantity,
                    "stop_price": o.stop_price} for o in (funded or [])],
        "decisions": None if sizing_run is None else sizing_run["decisions"],
        "context": None if sizing_run is None else sizing_run["context"],
    }


def _order_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if k not in VOLATILE_ORDER_FIELDS}


def execute_view(results, effects, rows_after, error: Optional[str] = None) -> Dict[str, Any]:
    """Execute stage: the action results, the ordered account effects and the rows it created/changed
    (orders without ids/timestamps/broker ids; the transaction with its TP/SL)."""
    orders, transactions = [], []
    for entry in rows_after or []:
        if entry["model"] == "TradingOrder":
            orders.append(_order_fields(entry["row"]))
        elif entry["model"] == "Transaction":
            row = entry["row"]
            transactions.append({k: v for k, v in row.items()
                                 if k not in ("id", "open_date", "close_date")})
    return {
        "error": error,
        "results": [
            {"action_type": r.get("action_type"), "success": r.get("success"),
             "message": r.get("message"),
             "data": None if not isinstance(r.get("data"), dict) else
             {k: v for k, v in r["data"].items() if k not in core.RESULT_DATA_SKIP}}
            for r in (results or []) if isinstance(r, dict)],
        "effects": list(effects or []),
        "orders": orders,
        "transactions": transactions,
    }


# ---------------------------------------------------------------------------
# step recorders
# ---------------------------------------------------------------------------
class _Step:
    """The unique subject of one step frame (so re-entry suppression never swallows a step)."""

    __slots__ = ("kind", "symbol")

    def __init__(self, kind: str, symbol: str = ""):
        self.kind = kind
        self.symbol = symbol


class _StepRecorder(core.FrameRecorder):
    """Common lifecycle of the three step kinds; a kind supplies inputs and its outcome view."""

    provider = DECISION_PROVIDER
    version = RECORD_VERSION
    family = "decision step"
    failure_kind = CaptureHealth.STEP_NOT_RECORDED
    kind = ""

    def __init__(self, step: _Step, context, symbol: str = ""):
        super().__init__(step, context)
        self.symbol = symbol
        self.observation_id: Optional[str] = None
        self.accepts_account_effects = self.kind == KIND_EXECUTE

    def method_name(self):
        return self.kind

    def collect_db_rows(self):
        return [], []

    def __enter__(self):
        started = time.perf_counter()
        try:
            return super().__enter__()
        finally:
            _add_record_seconds(self.context, time.perf_counter() - started)

    def __exit__(self, exc_type, exc, tb):
        started = time.perf_counter()
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            _add_record_seconds(self.context, time.perf_counter() - started)

    def _fail(self, message, exc):
        super()._fail(message, exc)
        try:
            _ledger(self.context)["steps_not_recorded"].append(
                {"kind": self.kind, "symbol": self.symbol, "error": f"{message}: {exc}"})
        except Exception:  # noqa: BLE001 -- the failure is already counted
            pass

    def emit(self, **observation):
        self.observation_id = record_observation(**observation)
        ledger = _ledger(self.context)
        if self.observation_id is None:
            ledger["steps_not_recorded"].append(
                {"kind": self.kind, "symbol": self.symbol, "error": "observation not recorded"})
        else:
            ledger["steps"].append(
                {"kind": self.kind, "symbol": self.symbol, "observation_id": self.observation_id})
            self.after_emit(ledger)

    def after_emit(self, ledger):
        pass

    def _pass_name(self) -> str:
        return self.context.meta["branch_flags"]["pass"]


def _step_failed(context, kind: str, symbol: str, exc: BaseException) -> None:
    """A step recorder that could not even be built: counted and listed, the decision goes on."""
    context.note_failure(f"decision step {kind} not recorded", exc, CaptureHealth.STEP_NOT_RECORDED)
    try:
        _ledger(context)["steps_not_recorded"].append(
            {"kind": kind, "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
    except Exception:  # noqa: BLE001 -- already counted
        pass


def _recommendation_row(recommendation) -> Optional[SharedValue]:
    """The recommendation's row, shared: byte-identical to the one ``pass_state`` holds, so a step carries
    only the hash instead of repeating it."""
    return None if recommendation is None else _shared_row(_dump(recommendation)["row"])


def _account_block(account) -> Dict[str, Any]:
    return {"account_class": type(account).__name__, "account_id": getattr(account, "id", None),
            "account_declarations": core.account_declarations(account, _ACCOUNT_DECLARATIONS)}


# -- rules ------------------------------------------------------------------
class _RulesRecorder(_StepRecorder):
    kind = KIND_RULES

    def __init__(self, evaluator, context, instrument_name, recommendation, ruleset_id, existing_order):
        super().__init__(_Step(KIND_RULES, instrument_name), context, instrument_name)
        self.evaluator = evaluator
        self.recommendation = recommendation
        self.ruleset_id = ruleset_id
        self.existing_order = existing_order

    def identity(self):
        return {"kind": KIND_RULES, "pass": self._pass_name(), "symbol": self.symbol,
                "recommendation_id": getattr(self.recommendation, "id", None),
                "expert_instance_id": getattr(self.recommendation, "instance_id", None)}

    def build_inputs(self):
        evaluator = self.evaluator
        return {
            "pass": self._pass_name(),
            "instrument_name": self.symbol,
            "ruleset_id": self.ruleset_id,
            "recommendation": _recommendation_row(self.recommendation),
            "existing_order": (None if self.existing_order is None
                               else _shared_row(_dump(self.existing_order)["row"])),
            "existing_transaction_ids": [t.id for t in evaluator.existing_transactions],
            **_account_block(evaluator.account),
        }

    def outcome_block(self, exc, encode):
        summaries = self.result
        error = None if exc is None else repr(exc)
        details = None
        if exc is None:
            details = encode(self.evaluator.get_evaluation_details(), "evaluation_details")
        view = rules_view(encode(summaries, "action summaries"), details, error)
        view["status"] = "error" if exc is not None else "returned"
        return view

    def after_emit(self, ledger):
        summaries = self.result or []
        errors = [s["error"] for s in summaries if isinstance(s, dict) and "error" in s]
        _symbol(self.context, self.symbol)["rules"] = {
            "observation_id": self.observation_id,
            "actions": [getattr(s["action_type"], "value", s["action_type"])
                        for s in summaries if isinstance(s, dict) and "action_type" in s],
            "errors": errors,
        }


def record_rules(evaluator, instrument_name, recommendation, ruleset_id, existing_order):
    """Context manager around one ``TradeActionEvaluator.evaluate``; the caller sets ``.result``."""
    context = decision_context()
    if context is None:
        return _NOOP_CONTEXT
    try:
        return _RulesRecorder(evaluator, context, instrument_name, recommendation, ruleset_id,
                              existing_order)
    except Exception as exc:  # noqa: BLE001 -- recording never reaches the decision
        _step_failed(context, KIND_RULES, instrument_name, exc)
        return _NOOP_CONTEXT


# -- size -------------------------------------------------------------------
class _SizeRecorder(_StepRecorder):
    kind = KIND_SIZE

    def __init__(self, manager, context, expert_instance_id, candidates, option_decisions):
        symbols = [order.symbol for order, _rec in candidates]
        super().__init__(_Step(KIND_SIZE), context, ",".join(sorted(symbols)))
        self.manager = manager
        self.expert_instance_id = expert_instance_id
        self.candidates = candidates
        self.option_decisions = option_decisions

    def identity(self):
        return {"kind": KIND_SIZE, "pass": self._pass_name(),
                "expert_instance_id": self.expert_instance_id, "symbols": self.symbol}

    def build_inputs(self):
        provider = self.manager.indicator_provider
        return {
            "pass": self._pass_name(),
            "expert_instance_id": self.expert_instance_id,
            "candidates": [{"order": _shared_row(_dump(order)["row"]),
                            "recommendation": _recommendation_row(rec)}
                           for order, rec in self.candidates],
            "option_decisions": list(self.option_decisions or []),
            "risk_manager": {"as_of": self.manager.as_of,
                             "indicator_provider": None if provider is None else type(provider).__name__},
        }

    def outcome_block(self, exc, encode):
        run = self._frame.records["sizing_run"] if "sizing_run" in self._frame.records else None
        view = sizing_view(self.result if exc is None else [], encode(run, "sizing run"),
                           None if exc is None else repr(exc))
        view["status"] = "error" if exc is not None else "returned"
        return view

    def after_emit(self, ledger):
        run = self._frame.records["sizing_run"] if "sizing_run" in self._frame.records else None
        for row in (run["decisions"] if run else []):
            if "symbol" in row:
                _symbol(self.context, row["symbol"])["size"] = {
                    k: row[k] for k in ("outcome", "reason", "quantity") if k in row}


def record_size(manager, expert_instance_id, candidates, option_decisions):
    """Context manager around one ``size_candidate_orders``; the caller sets ``.result`` (funded)."""
    context = decision_context()
    if context is None or not candidates:
        return _NOOP_CONTEXT
    try:
        return _SizeRecorder(manager, context, expert_instance_id, candidates, option_decisions)
    except Exception as exc:  # noqa: BLE001 -- recording never reaches the decision
        _step_failed(context, KIND_SIZE, "", exc)
        return _NOOP_CONTEXT


def note_sizing_run(decisions, context_figures) -> None:
    """Hand the RM's own explanation (``decisions`` + ``context``) to the open ``size`` step.

    Called from ``TradeRiskManagement._record_candidate_run`` before ``record_run``, so the same
    explanation is available whether or not a ``RiskManagerRun`` row is written. A no-op without an
    open frame; never raises.
    """
    core.note_record("sizing_run", {"decisions": decisions, "context": context_figures})


def sizing_frame_open() -> bool:
    """Whether a decision ``size`` step is recording (the RM then builds its run explanation even
    where no ``RiskManagerRun`` row is written)."""
    return decision_context() is not None and getattr(core._tls, "frame", None) is not None


# -- execute ----------------------------------------------------------------
class _ExecuteRecorder(_StepRecorder):
    kind = KIND_EXECUTE

    def __init__(self, evaluator, context, symbol, recommendation, submit_to_broker):
        super().__init__(_Step(KIND_EXECUTE, symbol), context, symbol)
        self.evaluator = evaluator
        self.recommendation = recommendation
        self.submit_to_broker = submit_to_broker
        #: Set by the caller once the entry order exists (entries): its rows are re-read at exit.
        self.order = None

    def identity(self):
        return {"kind": KIND_EXECUTE, "pass": self._pass_name(), "symbol": self.symbol,
                "recommendation_id": getattr(self.recommendation, "id", None),
                "expert_instance_id": getattr(self.recommendation, "instance_id", None)}

    def build_inputs(self):
        ledger_symbol = _symbol(self.context, self.symbol)
        rules = ledger_symbol["rules"] if "rules" in ledger_symbol else None
        return {
            "pass": self._pass_name(),
            "instrument_name": self.symbol,
            "recommendation_id": getattr(self.recommendation, "id", None),
            "rules_observation_id": None if rules is None else rules["observation_id"],
            "submit_to_broker": self.submit_to_broker,
            "funded": ledger_symbol["funded"] if "funded" in ledger_symbol else None,
            "existing_transaction_ids": [t.id for t in self.evaluator.existing_transactions],
            **_account_block(self.evaluator.account),
        }

    def _rows_after(self, results) -> List[Dict[str, Any]]:
        from ba2_common.core.models import Transaction
        from ba2_common.core.trade_store import get_or_none, orders_where

        order_ids: List[int] = []
        for result in results or []:
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, dict) and data.get("order_id"):
                order_ids.append(data["order_id"])
        if self.order is not None and self.order.id is not None:
            order_ids.append(self.order.id)
        rows: Dict[tuple, Dict[str, Any]] = {}
        transaction_ids: List[int] = []
        for order_id in dict.fromkeys(order_ids):
            from ba2_common.core.models import TradingOrder
            order = get_or_none(TradingOrder, order_id)
            if order is None:
                continue
            rows[("TradingOrder", order.id)] = _dump(order)
            for leg in orders_where(depends_on_order=order.id):
                rows[("TradingOrder", leg.id)] = _dump(leg)
            if order.transaction_id is not None:
                transaction_ids.append(order.transaction_id)
        for transaction_id in dict.fromkeys(transaction_ids):
            transaction = get_or_none(Transaction, transaction_id)
            if transaction is not None:
                rows[("Transaction", transaction.id)] = _dump(transaction)
        return core.sorted_rows(rows)

    def outcome_block(self, exc, encode):
        results = self.result if exc is None else None
        effects = _share_effect_rows([encodable_effect(entry, encode) for entry in self._frame.submits])
        try:
            rows = self._rows_after(results)
        except Exception as row_exc:  # noqa: BLE001 -- counted; the step keeps what it has
            self._fail("execute rows not re-read", row_exc)
            rows = []
        view = execute_view(encode(results, "execute results"), effects, encode(rows, "rows"),
                            None if exc is None else repr(exc))
        view["orders"] = [_big_fields_shared(order) for order in view["orders"]]
        view["status"] = "error" if exc is not None else "returned"
        return view

    def after_emit(self, ledger):
        _symbol(self.context, self.symbol)["execute"] = {
            "observation_id": self.observation_id,
            "effects": [e["method"] for e in self._frame.submits],
        }


def record_execute(evaluator, symbol, recommendation, submit_to_broker):
    """Context manager around one execute (+ persist + submit for an entry); the caller sets
    ``.result`` (the evaluator's results) and, for an entry, ``.order``. Account effect taps record
    into it."""
    context = decision_context()
    if context is None:
        return _NOOP_CONTEXT
    try:
        if _has_option_action(evaluator):
            return _NOOP_CONTEXT
        return _ExecuteRecorder(evaluator, context, symbol, recommendation, submit_to_broker)
    except Exception as exc:  # noqa: BLE001 -- recording never reaches the decision
        _step_failed(context, KIND_EXECUTE, symbol, exc)
        return _NOOP_CONTEXT


def _has_option_action(evaluator) -> bool:
    """Option actions size and submit themselves and are covered by the option action record."""
    from ba2_common.core.types import get_option_action_values

    option_values = set(get_option_action_values())
    for action in evaluator.trade_actions:
        value = evaluator._get_action_type_from_action(action)
        if getattr(value, "value", value) in option_values:
            return True
    return False


# ---------------------------------------------------------------------------
# pass_state
# ---------------------------------------------------------------------------
def record_pass_state(*, expert, expert_instance, pass_name: str, lookback_days: int,
                      cutoff_time, decision_time, account, ruleset_id: Optional[int],
                      fetched_recommendations: Iterable[Any],
                      selected_recommendations: Iterable[Any]) -> Optional[str]:
    """Write the ``pass_state`` observation of the open decision pass. Never raises.

    Called once, after the recommendation selection and before the first rule is evaluated.
    Returns the observation id (``None``: no pass open, or the record failed -- counted and listed
    under ``steps_not_recorded``).
    """
    context = decision_context()
    if context is None:
        return None
    started = time.perf_counter()
    try:
        return _record_pass_state(
            context, expert, expert_instance, pass_name, lookback_days, cutoff_time, decision_time,
            account, ruleset_id, fetched_recommendations, selected_recommendations)
    finally:
        _add_record_seconds(context, time.perf_counter() - started)


def _record_pass_state(context, expert, expert_instance, pass_name, lookback_days, cutoff_time,
                       decision_time, account, ruleset_id, fetched_recommendations,
                       selected_recommendations) -> Optional[str]:
    try:
        fetched = list(fetched_recommendations)
        rows, redacted = _pass_state_rows(expert_instance, ruleset_id, fetched)
        flags: Dict[str, Any] = {}
        settings, dropped = expert._recorded_settings(None, flags)
        if "settings_resolution_failed" in flags:
            raise RuntimeError(f"settings not resolvable: {flags['settings_resolution_failed']}")
        recorded_redacted = sorted(set(redacted) | set(dropped))
        selected = list(selected_recommendations)
        payload = {
            "version": RECORD_VERSION,
            "inputs": {
                "pass": pass_name,
                "expert_instance_id": expert_instance.id,
                "expert_class": expert_instance.expert,
                "lookback_days": lookback_days,
                "cutoff_time": cutoff_time,
                "decision_time": decision_time,
                "ruleset_id": ruleset_id,
                "settings": _encodable(context, settings, "settings", "decision step"),
                "redacted_setting_keys": recorded_redacted,
                **_account_block(account),
                "recommendations_considered": [
                    {"id": r.id, "symbol": r.symbol, "created_at": r.created_at}
                    for r in fetched],
                "recommendations_selected": [r.id for r in selected],
            },
            "db_rows": rows,
        }
        observation_id = record_observation(
            provider=DECISION_PROVIDER, method=KIND_PASS_STATE,
            identity={"kind": KIND_PASS_STATE, "pass": pass_name,
                      "expert_instance_id": expert_instance.id},
            payload=payload)
        if observation_id is None:
            raise RuntimeError("pass_state observation not recorded")
        _ledger(context)["steps"].append(
            {"kind": KIND_PASS_STATE, "symbol": "", "observation_id": observation_id})
        return observation_id
    except Exception as exc:  # noqa: BLE001 -- recording never reaches the decision
        context.note_failure("pass_state not recorded", exc, CaptureHealth.STEP_NOT_RECORDED)
        try:
            _ledger(context)["steps_not_recorded"].append(
                {"kind": KIND_PASS_STATE, "symbol": "", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001
            pass
        return None


def _pass_state_rows(expert_instance, ruleset_id, fetched):
    """``(db_rows, redacted setting keys)`` the pass reads (module docstring)."""
    from ba2_common.core.db import get_db
    from ba2_common.core.models import Ruleset, RulesetEventActionLink
    from ba2_common.core.trade_store import get_or_none
    from sqlmodel import select

    rows: Dict[tuple, Dict[str, Any]] = {}

    def add(obj):
        if obj is None:
            return
        key = (type(obj).__name__, getattr(obj, "id", None))
        if key not in rows:
            rows[key] = _dump(obj)

    redacted = core.collect_expert_rows(add, expert_instance.id)
    if ruleset_id is not None:
        from ba2_common.core.db import ruleset_event_actions

        add(get_or_none(Ruleset, ruleset_id))
        for rule in ruleset_event_actions(ruleset_id):
            add(rule)
        with get_db() as session:
            for link in session.exec(select(RulesetEventActionLink).where(
                    RulesetEventActionLink.ruleset_id == ruleset_id)).all():
                rows[("RulesetEventActionLink", (link.ruleset_id, link.eventaction_id))] = _dump(link)
    for recommendation in fetched:
        add(recommendation)

    def order(item):
        return (item[0][0], str(item[0][1]))

    return [_share(value) for _key, value in sorted(rows.items(), key=order)], sorted(set(redacted))
