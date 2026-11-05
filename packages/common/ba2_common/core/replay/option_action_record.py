"""Per-action capture record for option ENTRY and CLOSE actions (selection replay input).

The option taps (:mod:`~ba2_common.core.replay.option_taps`) record what the broker
ANSWERED. This module records what the shared action code was ASKED and what it DID, so the
offline replay can rebuild the exact action object, feed it the tape and compare the result.

``record_option_action(action)`` is used as the outermost statement of the four option
``execute`` methods in ``TradeActions``: ``_OptionEntryAction.execute`` (every builder that
does not override it), ``SellCoveredCallAction.execute``, ``RollPMCCShortAction.execute`` and
``CloseOptionAction.execute``. Those are ALL the paths that reach the broker from the two
families (grep: the only ``execute`` overrides below ``_OptionEntryAction`` are the covered
call, which calls ``super().execute()``, and the roll, which does not). Re-entry for the SAME
action object (covered call -> base) is a no-op, so one execute is one record.

Outside a capture scope, or while replaying, it is one ``current_capture()`` check and
nothing else. Inside one it records ONE observation at exit (provider ``"option_action"``,
method = action class name). A recording failure goes to ``context.note_failure`` and never
changes the action's result; the action's own exception propagates unchanged after the
outcome has been recorded as ``error``.

**Where the submit is captured.** ``note_submit`` / ``note_record`` are called from the four
call sites in ``TradeActions`` that hand an order to the account (entry ``_submit_option_order``,
the PMCC roll, the single-leg close, the multi-leg close) and from the two exit-record
stampers. The plan proposed writing the slot inside ``OptionsAccountInterface.submit_option_order``
/ ``close_option_position``; that does not work for the close, whose interface method is abstract
(every concrete account overrides it, and Alpaca/IBKR re-enter ``submit_option_order``, which would
record one close twice). Call-site notes record exactly what the ACTION asked the account to do,
under the method name it used, which is also what the replay's tape account receives.
The slot is a thread-local frame, restored on exit even on exceptions, so consecutive and nested
actions never leak into each other.

Payload (``payload``)::

    {"version": 1,
     "tape_span": {"start": <observation position at entry>},
     "inputs":   {... enough to rebuild the action, see rebuild_option_action ...},
     "db_rows":  [{"model": "TradingOrder", "row": {...model_dump...}}, ...],
     "outcome":  {"status": "returned"|"error", "error": repr|None, "success": bool|None,
                  "message": str|None, "result_data": {...}, "entry_record": ..., "exit_record": ...,
                  "submits": [{"method", "legs", "leg_summary", "quantity", "order_type",
                               "limit_price", "option_strategy", "transaction_id", "position"}]}}

``inputs``: ``action_class``; ``instrument_name``; ``order_recommendation`` (enum);
``account_class`` / ``account_id`` / ``account_declarations`` (the account itself is NOT serialised:
the replay supplies a tape account that carries the recorded name, id and class-level declarations); ``ctor_kwargs`` (every named constructor parameter of the class chain, read back off
the instance -- the EventAction config: strike method/param, DTE window, sizing, liquidity gates,
selection weights, close trigger/target ...); ``expert_recommendation`` (only the fields the
actions read: id, instance_id, symbol, recommended_action, expected_profit_percent, price_at_date,
data); ``existing_order`` (full ``model_dump``); ``attrs`` (``submit_to_broker``, ``close_percent``,
``evaluation_details`` set after construction by the evaluator); ``decision_time`` (ONE
``replay_now()`` read taken first; in capture it is therefore the first clock read of the action's
span, and Task 6 consumes it by calling ``replay_now()`` once before ``execute``).

**DB reads of an execute** (what ``db_rows`` must let a scratch DB reproduce), by code path:

* ``ExpertInstance`` (``_virtual_equity``: ``virtual_equity_pct``; the resolver's expert) and its
  ``ExpertSetting`` rows (``_per_instrument_cap_pct`` -> ``expert.settings[...]``; secret-looking
  keys are dropped and named in ``redacted_setting_keys``);
* ``ExpertRecommendation`` row (``_build_order_data`` -> ``get_instance(ExpertRecommendation, id)``);
* every WAITING / OPENED / CLOSING ``Transaction`` of the expert on ANY symbol
  (``CAPITAL_HOLDING_TRANSACTION_STATUSES``: the symbol reads ``_held_equity_shares``,
  ``_committed_to_underlying``, ``held_covered_calls`` and the duplicate gate are all subsets of it,
  and the used-balance pass reads it across symbols), plus ALL ``TradingOrder`` rows of those
  transactions. CLOSED / FAILED history is NOT recorded: no action or sizing path reads it;
* closes / roll: ``existing_order`` (full dump), its transaction (``get_or_none(Transaction)``),
  every ``TradingOrder`` of that transaction (``_resolve_option_order`` fallback,
  ``_close_multi_leg`` ``txn_orders``, ``_roll_in_flight``, ``_entry_order``), the order's child
  legs (``parent_order_id == order.id``), and the parent chain up to 3 hops
  (``_close_cross_fraction`` -> ``get_instance(TradingOrder, parent_order_id)``).

NOT covered, by design: reads the expert object makes of the ACCOUNT (balance, buying power,
positions) are broker reads, tapped by ``option_taps``; the stock-price bulk fetch inside
``Expert.get_available_equity_balance_detail`` is not an option tap and is a replay gap Task 6
must name; ``TradeActionResult`` / order rows the action WRITES are outputs, not inputs.
"""
from __future__ import annotations

import dataclasses
import inspect
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from ba2_common.core.replay.context import current_capture
from ba2_common.core.replay.observe import SECRET_KEY_TOKENS, record_observation

__all__ = [
    "OPTION_ACTION_PROVIDER",
    "RECORD_VERSION",
    "action_ctor_kwargs",
    "build_outcome",
    "db_row_instances",
    "note_record",
    "note_submit",
    "rebuild_option_action",
    "replay_effects",
    "record_option_action",
    "replay_decision_time",
]

OPTION_ACTION_PROVIDER = "option_action"
RECORD_VERSION = 1

#: The constructor parameters every TradeAction takes positionally; everything else is config.
_BASE_PARAMS = frozenset({
    "self", "instrument_name", "account", "order_recommendation",
    "existing_order", "expert_recommendation",
})

#: The recommendation attributes the option actions read (grep of TradeActions: instance_id, id,
#: data, price_at_date, expected_profit_percent, recommended_action; symbol for the record key).
_RECOMMENDATION_FIELDS = (
    "id", "instance_id", "symbol", "recommended_action", "expected_profit_percent",
    "price_at_date", "data",
)

#: Result-data keys that are bulky and NOT a replay comparison input.
_RESULT_DATA_SKIP = frozenset({"evaluation_details", "calculation_preview"})

_tls = threading.local()


# ---------------------------------------------------------------------------
# public context manager + submit slot
# ---------------------------------------------------------------------------
class _Noop:
    """The handle of a disabled recorder: assignments vanish."""

    __slots__ = ()

    def __setattr__(self, name, value):
        pass


_NOOP = _Noop()


class _NoopContext:
    __slots__ = ()

    def __enter__(self):
        return _NOOP

    def __exit__(self, exc_type, exc, tb):
        return False


_NOOP_CONTEXT = _NoopContext()


def record_option_action(action):
    """Context manager around one ``execute``; yields a handle whose ``.result`` the caller sets."""
    context = current_capture()
    if context is None or context.is_replay:
        return _NOOP_CONTEXT
    return _Recorder(action, context)


class _Frame:
    __slots__ = ("action", "submits", "records")

    def __init__(self, action):
        self.action = action
        self.submits: List[Dict[str, Any]] = []
        self.records: Dict[str, Any] = {}


class _Recorder:
    """The live recorder (only built inside a capture scope, never in replay)."""

    def __init__(self, action, context):
        self.action = action
        self.context = context
        self.result: Any = None
        self._inner = False
        self._active = False
        self._previous = None
        self._frame: Optional[_Frame] = None
        self._inputs: Optional[Dict[str, Any]] = None
        self._db_rows: Optional[List[Dict[str, Any]]] = None
        self._redacted: List[str] = []
        self._tape_start: Optional[int] = None

    def __enter__(self):
        self._tape_start = self.context.observation_position
        current = getattr(_tls, "frame", None)
        if current is not None and current.action is self.action:
            self._inner = True              # covered call -> base execute: one record
            return _NOOP
        try:
            self._inputs = _build_inputs(self.action)
            self._db_rows, self._redacted = _collect_db_rows(self.action)
        except Exception as exc:
            self.context.note_failure("option action inputs not recorded", exc)
            return _NOOP
        self._previous = current
        self._frame = _Frame(self.action)
        _tls.frame = self._frame
        self._active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._inner:
            return False
        if self._active:
            _tls.frame = self._previous
            try:
                self._record(exc)
            except Exception as record_exc:      # never change the action's outcome
                self.context.note_failure("option action outcome not recorded", record_exc)
        return False

    def _record(self, exc):
        action = self.action
        outcome = build_outcome(
            exc, self.result, self._frame,
            lambda value, label: _encodable(self.context, value, label))
        inputs = dict(self._inputs)
        inputs["redacted_setting_keys"] = self._redacted
        payload = {
            "version": RECORD_VERSION,
            # The action's reads are exactly the observations with start <= invocation_seq < this
            # record's own seq; anything the evaluator read earlier is outside the window.
            "tape_span": {"start": self._tape_start},
            "inputs": inputs,
            "db_rows": self._db_rows,
            "outcome": outcome,
        }
        recommendation = getattr(action, "expert_recommendation", None)
        order = getattr(action, "existing_order", None)
        record_observation(
            provider=OPTION_ACTION_PROVIDER,
            method=type(action).__name__,
            identity={
                "action_class": type(action).__name__,
                "expert_instance_id": getattr(recommendation, "instance_id", None),
                "symbol": action.instrument_name,
                "transaction_id": getattr(order, "transaction_id", None),
                "recommendation_id": getattr(recommendation, "id", None),
            },
            payload=payload,
        )


def build_outcome(exc, result, frame, encodable) -> Dict[str, Any]:
    """The ``outcome`` block of an action record, from what one ``execute`` did.

    ONE derivation for two callers: the live recorder (``_Recorder._record``) and the offline
    replay, which builds the outcome of the re-run the same way so the two are compared like
    for like. ``encodable(value, label)`` is the recorder's codec guard (the replay passes an
    identity function: it compares the live objects).
    """
    outcome: Dict[str, Any] = {
        "status": "error" if exc is not None else "returned",
        "error": repr(exc) if exc is not None else None,
        "success": None, "message": None, "result_data": None,
        "entry_record": None, "exit_record": None,
        "submits": [],
    }
    if exc is None and result is not None:
        outcome["success"], outcome["message"], data = _result_fields(result)
        if isinstance(data, dict):
            outcome["entry_record"] = encodable(data.get("entry_record"), "entry_record")
            outcome["result_data"] = encodable(
                {k: v for k, v in data.items() if k not in _RESULT_DATA_SKIP}, "result data")
    if "exit_record" in frame.records:
        outcome["exit_record"] = encodable(frame.records["exit_record"], "exit_record")
    if outcome["entry_record"] is None and "entry_record" in frame.records:
        outcome["entry_record"] = encodable(frame.records["entry_record"], "entry_record")
    outcome["submits"] = frame.submits
    return outcome


@contextmanager
def replay_effects(action):
    """Offline replay: collect what one re-run ``execute`` submits and stamps.

    The live recorder is a no-op while replaying (it must never write into the tape it reads), but
    the replay still needs the SAME submit / record notes the recorder collected, in the same
    shape, so it installs the recorder's own frame without recording anything. Restores the
    previous frame on exit, exceptions included.
    """
    frame = _Frame(action)
    previous = getattr(_tls, "frame", None)
    _tls.frame = frame
    try:
        yield frame
    finally:
        _tls.frame = previous


def _result_fields(result):
    """``(success, message, data)`` of an action result: the real dict, or an attribute object."""
    if isinstance(result, dict):
        return result["success"], result["message"], result["data"]
    return (getattr(result, "success", None), getattr(result, "message", None),
            getattr(result, "data", None))


def note_submit(method: str, **call) -> None:
    """Called where an option action hands an order to the account. Never raises."""
    frame = getattr(_tls, "frame", None)
    if frame is None:
        return
    try:
        legs = call.get("legs")
        entry = {"method": method}
        entry.update(call)
        if legs is not None:
            entry["legs"] = list(legs)
            entry["leg_summary"] = [_leg_summary(leg) for leg in legs]
        frame.submits.append(entry)
    except Exception as exc:
        context = current_capture()
        if context is not None:
            context.note_failure("option submit not noted", exc)


def note_record(name: str, value: Any) -> None:
    """Called where an action stamps its ``entry_record`` / ``exit_record``. Never raises."""
    frame = getattr(_tls, "frame", None)
    if frame is None:
        return
    frame.records[name] = value


def _leg_summary(leg) -> Dict[str, Any]:
    return {
        "contract_symbol": leg.contract_symbol, "side": leg.side, "ratio_qty": leg.ratio_qty,
        "position_intent": leg.position_intent, "option_type": leg.option_type,
        "strike": leg.strike, "expiry": leg.expiry,
    }


def _encodable(context, value, label):
    """``value`` if the exact codec can store it, else a visible marker (and a counted failure)."""
    if value is None:
        return None
    from ba2_common.core.replay import codec
    try:
        codec.encode(value)
        return value
    except Exception as exc:
        context.note_failure(f"option action {label} not encodable", exc)
        return {"unencodable": repr(value)[:2000]}


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def action_ctor_kwargs(action) -> Dict[str, Any]:
    """The action's config: each named constructor parameter of its class chain, read off it.

    ``**kwargs`` sinks are skipped (the base ctor stores every parameter it honours under its own
    name -- see the comment on the selection-weight block). A named parameter the instance does not
    carry is a recorder bug and raises (caught by the caller as a counted capture failure).
    """
    names: List[str] = []
    for klass in type(action).__mro__:
        init = klass.__dict__.get("__init__")
        if init is None or klass is object:
            continue
        for name, parameter in inspect.signature(init).parameters.items():
            if name in _BASE_PARAMS or parameter.kind in (
                    inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue
            if name not in names:
                names.append(name)
    return {name: getattr(action, name) for name in names}


def _recommendation_fields(recommendation) -> Optional[Dict[str, Any]]:
    if recommendation is None:
        return None
    return {name: getattr(recommendation, name)
            for name in _RECOMMENDATION_FIELDS if hasattr(recommendation, name)}


#: Class-level declarations of the ACCOUNT that the action code reads and that a replay's tape
#: account cannot learn from a tapped read. ``OPTION_GREEKS_SOURCE`` decides whether the entry
#: record can be built at all (``OptionGreeksSourceUndeclared``).
_ACCOUNT_DECLARATIONS = ("OPTION_GREEKS_SOURCE",)


def _account_declarations(account) -> Dict[str, Any]:
    out = {}
    for name in _ACCOUNT_DECLARATIONS:
        value = getattr(account, name, None)
        # A declaration is a string tag or absent; a test double's auto-attribute is neither.
        out[name] = value if isinstance(value, str) else None
    return out


def _build_inputs(action) -> Dict[str, Any]:
    from ba2_common.core.replay.clock import replay_now

    order = action.existing_order
    attrs: Dict[str, Any] = {
        "submit_to_broker": action.submit_to_broker,
        "close_percent": action.close_percent,
    }
    details = getattr(action, "evaluation_details", None)
    if details:
        attrs["evaluation_details"] = _encodable(current_capture(), details, "evaluation_details")
    account = action.account
    return {
        "action_class": type(action).__name__,
        "instrument_name": action.instrument_name,
        "order_recommendation": action.order_recommendation,
        "account_class": type(account).__name__,
        "account_id": getattr(account, "id", None),
        "account_declarations": _account_declarations(account),
        "ctor_kwargs": action_ctor_kwargs(action),
        "expert_recommendation": _recommendation_fields(action.expert_recommendation),
        "existing_order": _dump(order)["row"] if order is not None else None,
        "attrs": attrs,
        "decision_time": replay_now(),
    }


# ---------------------------------------------------------------------------
# db rows
# ---------------------------------------------------------------------------
def _dump(obj) -> Dict[str, Any]:
    return {"model": type(obj).__name__, "row": obj.model_dump()}


def _is_secret(key: Any) -> bool:
    text = str(key).lower()
    return any(token in text for token in SECRET_KEY_TOKENS)


def _expert_id(action) -> Optional[int]:
    recommendation = action.expert_recommendation
    expert_id = getattr(recommendation, "instance_id", None) if recommendation is not None else None
    if expert_id is None and action.existing_order is not None:
        getter = getattr(action.existing_order, "get_expert_id", None)
        if callable(getter):
            expert_id = getter()
    return expert_id


def _collect_db_rows(action):
    """``(rows, redacted setting keys)``: every row an execute reads (module docstring)."""
    from sqlmodel import Session, select

    from ba2_common.core.db import get_db
    from ba2_common.core.models import (
        ExpertInstance, ExpertRecommendation, ExpertSetting, TradingOrder, Transaction,
    )
    from ba2_common.core.trade_store import get_or_none, orders_where, transactions_where
    from ba2_common.core.types import CAPITAL_HOLDING_TRANSACTION_STATUSES

    rows: Dict[tuple, Dict[str, Any]] = {}
    redacted: List[str] = []

    def add(obj):
        if obj is None:
            return
        key = (type(obj).__name__, obj.id)
        if key not in rows:
            rows[key] = _dump(obj)

    order = action.existing_order
    if order is not None:
        rows[("TradingOrder", order.id)] = _dump(order)        # the object the action holds
        seen = 0
        parent_id = order.parent_order_id
        while parent_id and seen < 3:
            parent = get_or_none(TradingOrder, parent_id)
            add(parent)
            parent_id = parent.parent_order_id if parent is not None else None
            seen += 1
        if order.id is not None:
            for child in orders_where(parent_order_id=order.id):
                add(child)
        if order.transaction_id is not None:
            add(get_or_none(Transaction, order.transaction_id))
            for sibling in orders_where(transaction_id=order.transaction_id):
                add(sibling)

    expert_id = _expert_id(action)
    recommendation = action.expert_recommendation
    recommendation_id = getattr(recommendation, "id", None)
    if recommendation_id is not None:
        add(get_or_none(ExpertRecommendation, recommendation_id))
    if expert_id is not None:
        add(get_or_none(ExpertInstance, expert_id))
        with Session(get_db().bind) as session:
            for setting in session.exec(
                    select(ExpertSetting).where(ExpertSetting.instance_id == expert_id)).all():
                if _is_secret(setting.key):
                    redacted.append(setting.key)
                else:
                    add(setting)
        # THE BOUND. Every transaction read of an execute filters on WAITING / OPENED / CLOSING
        # (CAPITAL_HOLDING_TRANSACTION_STATUSES): ``_committed_to_underlying`` (symbol, all three),
        # ``_held_equity_shares`` (symbol, OPENED), ``_own_equity_transactions`` /
        # ``held_covered_calls`` (symbol, WAITING/OPENED), the duplicate-position gate
        # (``open_or_waiting_position``), the option risk manager (OPENED / WAITING of the expert)
        # and the expert's used-balance pass (``capital_transactions``, ANY symbol, all three).
        # Nothing reads a CLOSED / FAILED row, so history is not recorded: the set is the expert's
        # capital-holding transactions on any symbol, plus all their orders below. The transaction
        # an existing order belongs to is added separately above, whatever its status.
        transactions = list(transactions_where(
            expert_id=expert_id, statuses=list(CAPITAL_HOLDING_TRANSACTION_STATUSES)))
        ids = []
        for transaction in transactions:
            add(transaction)
            if transaction.id not in ids:
                ids.append(transaction.id)
        if ids:
            for held in orders_where(transaction_ids=ids):
                add(held)

    ordered = sorted(rows.items(), key=lambda item: (item[0][0], item[0][1] or 0))
    return [value for _, value in ordered], sorted(set(redacted))


def db_row_instances(payload: Dict[str, Any]) -> list:
    """The recorded ``db_rows`` as model instances, ready to ``session.add`` into a scratch DB."""
    from ba2_common.core import models

    instances = []
    for entry in payload["db_rows"]:
        model = getattr(models, entry["model"])
        instances.append(model(**entry["row"]))
    return instances


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------
def rebuild_option_action(payload: Dict[str, Any], account):
    """The recorded action, rebuilt: same class, constructor config, recommendation and order.

    ``payload`` is the observation payload; ``account`` is the tape account the replay supplies.
    Only the two option families may be rebuilt (a class name outside them is refused).
    """
    from ba2_common.core import TradeActions
    from ba2_common.core.models import ExpertRecommendation, TradingOrder

    inputs = payload["inputs"]
    klass = getattr(TradeActions, inputs["action_class"], None)
    if not (inspect.isclass(klass) and issubclass(
            klass, (TradeActions._OptionEntryAction, TradeActions.CloseOptionAction))):
        raise ValueError(f"{inputs['action_class']!r} is not an option entry/close action")
    fields = inputs["expert_recommendation"]
    recommendation = ExpertRecommendation(**fields) if fields is not None else None
    existing = inputs["existing_order"]
    order = TradingOrder(**existing) if existing is not None else None
    action = klass(inputs["instrument_name"], account, inputs["order_recommendation"],
                   order, recommendation, **inputs["ctor_kwargs"])
    attrs = inputs["attrs"]
    action.submit_to_broker = attrs["submit_to_broker"]
    action.close_percent = attrs["close_percent"]
    if "evaluation_details" in attrs:
        action.evaluation_details = attrs["evaluation_details"]
    return action


def replay_decision_time(payload: Dict[str, Any]):
    """The recorded decision clock (the recorder's own first read of the action's span)."""
    return payload["inputs"]["decision_time"]
