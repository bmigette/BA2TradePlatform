"""Account taps for the stock decision path: sizing reads and the EFFECT calls.

Two tables, applied to every account class by ``ReadOnlyAccountInterface.__init_subclass__`` (the
same mechanism and MRO rules as :mod:`~ba2_common.core.replay.option_taps`, see its docstring: a
concrete account OVERRIDES the method, so a decorator on the interface is lost the moment a
subclass defines its own; the hook resolves each name through the MRO, skips abstract results, never
double-wraps a method that is already tapped anywhere in its wrapper chain, and wraps the resolved
attribute on the concrete class). A class that sets ``_replay_taps_exempt`` is skipped.

**Sizing reads** (``SIZING_READ_TAPPED_METHODS``, provider ``"broker"``): what the stock sizing and
rules code asks the account: ``get_account_snapshot``, ``get_positions``,
``get_instrument_current_price``, ``get_balance``, ``get_account_info``, ``has_pending_closing_order`` and
``get_fractionable``.

* ``get_instrument_current_price`` is tapped on the base class; an OVERRIDE (``AlpacaAccount``
  normalises ``price_type`` and calls ``super()``) used to escape the tap, so a quote the caller
  read under the caller's own arguments was only recorded under the normalised ones (and
  ``BacktestAccount``'s override not at all). The override is now wrapped too: the outer wrapper
  records the CALLER's identity at ``tap_depth`` 0, the inherited inner tap records the normalised
  read at depth 1 (the replay's tape window reads depth 0 only).
* ``get_account_snapshot`` / ``get_positions`` on an ``OptionsAccountInterface`` account stay with
  the option taps (provider ``"broker_options"``, unchanged, so every recording made so far still
  resolves and the option record is byte-identical). On a stock-only account (``TastyTradeAccount``
  and any future one) they are tapped here under ``"broker"`` with the same identity.

**Effects** (``EFFECT_METHODS``): ``submit_order``, ``close_transaction``, ``reduce_transaction``,
``adjust_tp``, ``adjust_sl`` and ``adjust_tp_sl``. ``adjust_tp_sl`` is not in the design's list of
five but ``TradeActionEvaluator`` (ruleset TP/SL) and the open-positions code call it directly: left
out, a combined TP/SL change would have no effect record at all while its nested ``adjust_tp`` would.
An effect call records ONE entry into the current frame via
:func:`~ba2_common.core.replay.action_record.note_account_effect`:

* only inside a capture scope (not in replay), and only into a frame that accepts account effects
  (the stock decision-step recorders; the option family notes its own submits and is unaffected);
* OUTERMOST call only: a tapped effect reached from inside another (``adjust_tp`` -> ``adjust_tp_sl``,
  ``close_transaction`` -> ``submit_order``, ``submit_order`` -> ``adjust_tp_sl`` bracket set-up)
  is passthrough, so one decision is one effect;
* inside a decision PASS but outside every step frame (dependent TP/SL legs submitted by the waiting-
  trigger check, compensation, refresh) the effect goes to the pass-level ``effects_outside_steps``
  list (``decision_record.note_unframed_effect``), never dropped;
* arguments are snapshotted BEFORE the call (``submit_order`` mutates the order), the return value
  or the exception text after; the exception is re-raised unchanged.

Outside a capture scope (live and backtest) a wrapper is one ``current_capture()`` check and then
the original call: zero behaviour change.
"""
from __future__ import annotations

import functools
import inspect
import threading
from typing import Any, Callable, Dict, Mapping, Tuple

from ba2_common.core.replay.action_record import dump_row, note_account_effect
from ba2_common.core.replay.context import current_capture
from ba2_common.core.replay.observe import observe_provider, redact_text, tapped_boundary
from ba2_common.logger import logger
from ba2_common.core.replay.option_taps import _account_identity, _with, account_only_identity

__all__ = [
    "ACCOUNT_PROVIDER",
    "EFFECT_ATTRIBUTE",
    "EFFECT_METHODS",
    "SIZING_READ_TAPPED_METHODS",
    "effect_boundary",
    "tap_account_methods",
    "untapped_methods",
]

#: Provider name of the stock account read taps (the base ``get_instrument_current_price`` tap uses it).
ACCOUNT_PROVIDER = "broker"

#: Marker on an effect wrapper (the effect-tap analogue of ``observe.OBSERVED_ATTRIBUTE``).
EFFECT_ATTRIBUTE = "_replay_effect_boundary"


def fractionable_identity(args):
    return _with(args, symbols=sorted({str(s) for s in args["symbols"]}))


def closing_order_identity(args):
    return _with(args, transaction_id=args["transaction_id"])


def _quote_identity(args):
    # Lazy: ReadOnlyAccountInterface imports this module for its __init_subclass__ hook.
    from ba2_common.core.interfaces.ReadOnlyAccountInterface import quote_identity
    return quote_identity(args)


#: method -> (provider, identity function). THE list: the guard test and ``tap_account_methods``.
SIZING_READ_TAPPED_METHODS: Dict[str, Tuple[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]]] = {
    # The balance every expert sizes against (``get_tradable_balance`` -> ``_plain_balance`` -> here). At
    # Alpaca / TastyTrade it is its own broker call, NOT derived from the snapshot, so it needs its own tap.
    "get_balance": (ACCOUNT_PROVIDER, account_only_identity),
    # The buying-power clamp falls back to it when the snapshot carries no figure
    # (``MarketExpertInterface._read_actual_buying_power``): a broker read like the others.
    "get_account_info": (ACCOUNT_PROVIDER, account_only_identity),
    "get_account_snapshot": (ACCOUNT_PROVIDER, account_only_identity),
    "get_positions": (ACCOUNT_PROVIDER, account_only_identity),
    "get_instrument_current_price": (ACCOUNT_PROVIDER, _quote_identity),
    "has_pending_closing_order": (ACCOUNT_PROVIDER, closing_order_identity),
    "get_fractionable": (ACCOUNT_PROVIDER, fractionable_identity),
}

#: The account methods through which a decision has an EFFECT on the book.
EFFECT_METHODS: Tuple[str, ...] = (
    "submit_order", "close_transaction", "reduce_transaction",
    "adjust_tp", "adjust_sl", "adjust_tp_sl",
)

_effect_state = threading.local()


# ---------------------------------------------------------------------------
# effect wrapper
# ---------------------------------------------------------------------------
def _plain(value: Any) -> Any:
    """A recordable form of an argument / return: rows dump, transactions by id, rest verbatim."""
    name = type(value).__name__
    if name == "Transaction" and hasattr(value, "model_dump"):
        return {"transaction_id": getattr(value, "id", None)}
    if hasattr(value, "model_dump") and hasattr(value, "id"):
        return dump_row(value)
    return value


def _call_arguments(signature, args, kwargs) -> Dict[str, Any]:
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    return {name: _plain(value) for name, value in bound.arguments.items() if name != "self"}


def effect_tap(method: str) -> Callable:
    def decorator(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            context = current_capture()
            if context is None or context.is_replay or getattr(_effect_state, "active", False):
                return fn(*args, **kwargs)

            try:
                call = _call_arguments(signature, args, kwargs)
            except Exception as exc:
                context.note_failure(f"account effect {method} arguments not recorded", exc)
                call = {"arguments_unavailable": True}

            _effect_state.active = True
            try:
                try:
                    result = fn(*args, **kwargs)
                except BaseException as raised:
                    # BaseException too (CancelledError, KeyboardInterrupt): the call may already have
                    # reached the broker, so the attempt is recorded, then the exit is re-raised
                    # unchanged. The text goes through the credential redaction (a broker error can
                    # echo a URL with a key).
                    _note(context, method, call, error=redact_text(repr(raised)))
                    raise
            finally:
                _effect_state.active = False
            _note(context, method, call, returned=result)
            return result

        setattr(wrapper, EFFECT_ATTRIBUTE, method)
        return wrapper

    return decorator


def _note(context, method: str, call: Dict[str, Any], **outcome: Any) -> None:
    try:
        if "returned" in outcome:
            outcome["returned"] = _plain(outcome["returned"])
        if not note_account_effect(method, call=call, **outcome):
            # No accepting frame: inside a decision pass this is an effect outside every step (the
            # pass-level list keeps it); elsewhere it is not recorded by design.
            from ba2_common.core.replay.decision_record import note_unframed_effect
            note_unframed_effect(context, method, call, **outcome)
    except Exception as exc:                    # recording never changes the call's outcome
        context.note_failure(f"account effect {method} not noted", exc)


def effect_boundary(fn):
    """The effect marker of ``fn`` (searching the wrapper chain), or ``None`` when untapped."""
    seen = 0
    while fn is not None and seen < 16:
        marker = getattr(fn, EFFECT_ATTRIBUTE, None)
        if marker is not None:
            return marker
        fn = getattr(fn, "__wrapped__", None)
        seen += 1
    return None


# ---------------------------------------------------------------------------
# applying the tables
# ---------------------------------------------------------------------------
def _is_options_account(cls) -> bool:
    return any(base.__name__ == "OptionsAccountInterface" for base in cls.__mro__)


def _still_abstract(cls) -> bool:
    """Whether ``cls`` still has an abstract method. ``inspect.isabstract`` cannot be asked from
    ``__init_subclass__`` (``ABCMeta`` computes ``__abstractmethods__`` after it ran)."""
    names = {name for klass in cls.__mro__ for name in vars(klass)}
    return any(getattr(getattr(cls, name, None), "__isabstractmethod__", False) for name in names)


#: (class, name) -> reason, for tapped names a class defines as something other than a plain method.
#: Filled by ``_resolve`` (class creation), read by ``untapped_methods`` (the guard test fails on it).
_UNTAPPABLE: Dict[Tuple[type, str], str] = {}


def _resolve(cls, name):
    resolved = getattr(cls, name, None)
    if resolved is None or getattr(resolved, "__isabstractmethod__", False):
        return None
    if not inspect.isfunction(inspect.getattr_static(cls, name)):
        # A classmethod/staticmethod/property cannot carry the tap. Raising here would break the IMPORT
        # of the account module (and with it the app), so it is logged at ERROR and registered: the
        # guard test (``untapped_methods``) fails on it, and the method stays untapped (visible).
        reason = "is not a plain method; it cannot carry the account tap"
        _UNTAPPABLE[(cls, name)] = reason
        logger.error(f"replay account taps: {cls.__name__}.{name} {reason}; it is NOT recorded")
        return None
    return resolved


def tap_account_methods(cls) -> None:
    """Tap every sizing read and effect method that ``cls`` resolves to an untapped body. Idempotent."""
    if getattr(cls, "_replay_taps_exempt", False):
        return
    from ba2_common.core.replay.option_taps import OPTION_TAPPED_METHODS

    options_account = _is_options_account(cls)
    abstract = _still_abstract(cls)
    for name, (provider, identity) in SIZING_READ_TAPPED_METHODS.items():
        if name in OPTION_TAPPED_METHODS and (options_account or abstract):
            # The option tap owns it on an options account (provider "broker_options"); on an
            # abstract base it is left to the concrete leaf, so that an options account which
            # inherits it unchanged does not find it already tapped under the wrong provider.
            continue
        resolved = _resolve(cls, name)
        if resolved is None or tapped_boundary(resolved) is not None:
            continue
        setattr(cls, name, observe_provider(provider, name, identity=identity, record_errors=True)(resolved))

    for name in EFFECT_METHODS:
        resolved = _resolve(cls, name)
        if resolved is None or effect_boundary(resolved) is not None:
            continue
        setattr(cls, name, effect_tap(name)(resolved))


def untapped_methods(cls) -> list:
    """The sizing reads / effect methods ``cls`` leaves untapped, as ``"Class.method"`` strings.

    THE guard predicate (tests call it on every concrete account class): for each name of the two
    tables, the method ``cls`` resolves to must carry a tap (an account read tap, an option tap, or an
    effect tap), and no non-exempt class of the MRO may define one of them without a tap -- an
    override that wraps nothing and is then shadowed by a tapped subclass would still be a hole for
    any class that inherits it directly.
    """
    from ba2_common.core.replay.option_taps import OPTION_TAPPED_METHODS

    problems = []
    if getattr(cls, "_replay_taps_exempt", False) or not _in_tap_scope(cls):
        return problems
    abstract_cls = _still_abstract(cls)
    names = list(SIZING_READ_TAPPED_METHODS) + list(EFFECT_METHODS)
    for klass in cls.__mro__:
        for name in names:
            if (klass, name) in _UNTAPPABLE:
                problems.append(f"{klass.__name__}.{name} ({_UNTAPPABLE[(klass, name)]})")

    def carries_tap(fn):
        return tapped_boundary(fn) is not None or effect_boundary(fn) is not None

    for name in names:
        resolved = getattr(cls, name, None)
        if resolved is None or getattr(resolved, "__isabstractmethod__", False):
            continue
        if name in OPTION_TAPPED_METHODS and abstract_cls:
            continue                           # an abstract base leaves these to its concrete leaf
        if not carries_tap(resolved):
            problems.append(f"{cls.__name__}.{name}")
    for klass in cls.__mro__:
        if getattr(klass, "_replay_taps_exempt", False) or klass is object:
            continue
        for name in names:
            if name not in vars(klass):
                continue
            defined = vars(klass)[name]
            if getattr(defined, "__isabstractmethod__", False) or not inspect.isfunction(defined):
                continue
            if not _in_tap_scope(klass):
                continue                       # a mixin outside the account hierarchy
            if name in OPTION_TAPPED_METHODS and _still_abstract(klass):
                continue                       # an abstract base leaves these to its concrete leaf
            if not carries_tap(defined):
                problems.append(f"{klass.__name__}.{name} (defined in the MRO of {cls.__name__})")
    return sorted(set(problems))


def _in_tap_scope(klass) -> bool:
    """Whether the ``__init_subclass__`` hook runs for ``klass`` (it is a ReadOnlyAccountInterface)."""
    return any(base.__name__ == "ReadOnlyAccountInterface" for base in klass.__mro__)
