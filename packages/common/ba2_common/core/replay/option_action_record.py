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

    {"version": 2,           # 1 = before ``decision_frame_time`` existed (still read)
     "tape_span": {"start": <observation position at entry>,
                   "clock_start": <clock reads recorded at entry>,
                   "clock_end": <clock reads recorded at exit>},
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
span, and Task 6 consumes it by calling ``replay_now()`` once before ``execute``);
``decision_frame_time`` (version 2): the ``decision_time`` of the live decision frame
(``market_condition_live.current_decision()``) the action ran under, read off the frame WITHOUT a
clock read, or ``None`` when no frame was open (then ``live_decision_time()`` read the clock
itself, and that read is inside the recorded clock span). The frame instant can differ from the
recorder's own read, and it is the frame that live used for the DTE / session label.

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

import inspect
from typing import Any, Dict, List, Optional

from ba2_common.core.replay import action_record as core
from ba2_common.core.replay.action_record import (
    NOOP_CONTEXT as _NOOP_CONTEXT,
    Frame as _Frame,
    _tls,
    build_outcome,
    db_row_instances,
    dump_row as _dump,
    encodable as _encodable,
    is_secret as _is_secret,
    note_record,
    replay_effects,
)
from ba2_common.core.replay.context import current_capture
from ba2_common.core.replay.observe import record_observation

# The family-independent mechanics (frame, outcome, row dump, redaction, version gate, recorder
# lifecycle) live in :mod:`~ba2_common.core.replay.action_record`; this module is the OPTION family
# on top of it. Names re-exported above keep their historical import path.
__all__ = [
    "OPTION_ACTION_PROVIDER",
    "RECORD_VERSION",
    "READABLE_VERSIONS",
    "replay_decision_frame_time",
    "NO_FRAME_FIELD",
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
RECORD_VERSION = 2
#: Versions the replay reads. 1 predates ``inputs["decision_frame_time"]``.
READABLE_VERSIONS = (1, 2)

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
_RESULT_DATA_SKIP = core.RESULT_DATA_SKIP


# ---------------------------------------------------------------------------
# public context manager + submit slot
# ---------------------------------------------------------------------------
def record_option_action(action):
    """Context manager around one ``execute``; yields a handle whose ``.result`` the caller sets."""
    context = current_capture()
    if context is None or context.is_replay:
        return _NOOP_CONTEXT
    return _Recorder(action, context)


class _Recorder(core.FrameRecorder):
    """The live option recorder (only built inside a capture scope, never in replay)."""

    provider = OPTION_ACTION_PROVIDER
    version = RECORD_VERSION
    family = "option action"

    def method_name(self):
        return type(self.action).__name__

    def identity(self):
        action = self.action
        recommendation = getattr(action, "expert_recommendation", None)
        order = getattr(action, "existing_order", None)
        return {
            "action_class": type(action).__name__,
            "expert_instance_id": getattr(recommendation, "instance_id", None),
            "symbol": action.instrument_name,
            "transaction_id": getattr(order, "transaction_id", None),
            "recommendation_id": getattr(recommendation, "id", None),
        }

    def emit(self, **observation):
        record_observation(**observation)

    def build_inputs(self):
        return _build_inputs(self.action)

    def collect_db_rows(self):
        return _collect_db_rows(self.action)


def note_submit(method: str, **call) -> None:
    """Called where an option action hands an order to the account. Never raises."""
    core.note_effect(method, _leg_summary, **call)


def _leg_summary(leg) -> Dict[str, Any]:
    return {
        "contract_symbol": leg.contract_symbol, "side": leg.side, "ratio_qty": leg.ratio_qty,
        "position_intent": leg.position_intent, "option_type": leg.option_type,
        "strike": leg.strike, "expiry": leg.expiry,
    }


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
    return core.account_declarations(account, _ACCOUNT_DECLARATIONS)


def _live_frame_time():
    """``decision_time`` of the open live decision frame, or ``None`` when there is none."""
    from ba2_common.core.market_condition_live import current_decision

    frame = current_decision()
    return None if frame is None else frame.decision_time


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
        # Read off the frame object: NO clock read (the recorded clock span is unchanged).
        "decision_frame_time": _live_frame_time(),
    }


# ---------------------------------------------------------------------------
# db rows
# ---------------------------------------------------------------------------
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
    from ba2_common.core.models import ExpertRecommendation, TradingOrder, Transaction
    from ba2_common.core.trade_store import get_or_none, orders_where

    rows: Dict[tuple, Dict[str, Any]] = {}

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
    # THE BOUND (core.collect_expert_rows): the expert instance, its non-secret settings and its
    # capital-holding transactions on ANY symbol with all their orders. Every transaction read of an
    # execute filters on WAITING / OPENED / CLOSING (``_committed_to_underlying``,
    # ``_held_equity_shares``, ``held_covered_calls``, the duplicate-position gate, the option risk
    # manager and the expert's used-balance pass); nothing reads a CLOSED / FAILED row, so history is
    # not recorded. The transaction an existing order belongs to is added above, whatever its status.
    redacted = core.collect_expert_rows(add, expert_id)
    return core.sorted_rows(rows), sorted(set(redacted))


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


class _NoFrameField:
    """Sentinel: a version-1 record, written before ``decision_frame_time`` was captured."""

    def __repr__(self):
        return "NO_FRAME_FIELD"


NO_FRAME_FIELD = _NoFrameField()


def replay_decision_frame_time(payload: Dict[str, Any]):
    """The live decision frame's instant (``datetime``), ``None`` (live held no frame: it read
    the clock itself), or :data:`NO_FRAME_FIELD` (a version-1 record: unknown, must be inferred
    or refused by the caller, never assumed)."""
    if payload["version"] < 2:
        return NO_FRAME_FIELD
    return payload["inputs"]["decision_frame_time"]
