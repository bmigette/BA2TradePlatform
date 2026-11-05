"""Option market-data and sizing taps for every ``OptionsAccountInterface`` account.

Live already records an option trade's ``option_trade_record_v1`` but not the chain /
quote / sizing reads that CHOSE and PRICED the contract, so a live option decision could
not be re-run offline. This module defines what is tapped (``OPTION_TAPPED_METHODS``:
method -> provider, identity function) and applies it (:func:`tap_option_methods`).
The tap itself is the ordinary :func:`~ba2_common.core.replay.observe.observe_provider`:
passthrough, record only inside a live capture scope, never raise into the caller.

**Why not decorate the interface.** Concrete accounts (Alpaca, IBKR, Backtest) OVERRIDE
the read methods, so a decorator on the interface method is lost the moment the subclass
defines its own. ``OptionsAccountInterface.__init_subclass__`` therefore calls
:func:`tap_option_methods` on every subclass, and the interface calls it on itself for the
concrete sizing helpers it defines.

**MRO decision.** ``get_positions`` and ``get_account_snapshot`` are NOT defined on
``OptionsAccountInterface``; they come from ``AccountInterface`` (``AlpacaAccount(
AccountInterface, OptionsAccountInterface)``). Looking only at ``cls.__dict__`` would miss
a subclass that inherits ``get_account_snapshot`` unchanged. So the hook resolves each
name through the MRO (``getattr(cls, name)``): an abstract result is skipped (the ABC
keeps working); a result already tapped anywhere in its wrapper chain is left alone (no
double wrap, however deep the subclass chain); anything else -- an override in
``cls.__dict__`` or an inherited untapped method -- is wrapped and set on ``cls`` itself.
Wrapping the resolved attribute on the concrete class means the tap sits exactly where
the call lands, whatever the base order, and the base's own function is left untouched.
A class that sets ``_replay_taps_exempt`` is skipped (``QuoteCachingAccount``: the
wrapped account records). Known limit: an override that calls ``super()`` into a tapped
parent method records twice (same identity, same value); harmless for a lookup replay.

**Errors.** The option taps pass ``record_errors=True``: a read that raised is recorded as an
``error`` observation and re-raised unchanged, so an action that skipped the candidate replays the
same way (see :mod:`~ba2_common.core.replay.recorded_errors` for what can be rebuilt).

**Identity** = call arguments plus ``account_class`` / ``account_id`` (the shape of
``ReadOnlyAccountInterface.quote_identity``). No wall-clock value ever enters an identity:
the option reads take none, and the sizing reads take only caller-supplied numbers.
Dates and enums are made JSON-safe by ``observe.sanitize_identity`` at record time, and
the replay looks up by the same functions.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Mapping, Tuple

from ba2_common.core.replay.observe import observe_provider, tapped_boundary

__all__ = [
    "OPTION_PROVIDER",
    "OPTION_TAPPED_METHODS",
    "tap_option_methods",
]

#: Provider name of every option tap (the replay filters a tape on it).
OPTION_PROVIDER = "broker_options"


def _account_identity(args: Mapping[str, Any]) -> Dict[str, Any]:
    account = args["self"]
    return {
        "account_class": type(account).__name__,
        "account_id": getattr(account, "id", None),
    }


def _first(args: Mapping[str, Any], *names: str) -> Any:
    """The argument bound under the first of ``names`` (an override may name it differently)."""
    for name in names:
        if name in args:
            return args[name]
    raise KeyError(f"none of {names} among the bound arguments {sorted(args)}")


def _with(args: Mapping[str, Any], **fields: Any) -> Dict[str, Any]:
    identity = _account_identity(args)
    identity.update(fields)
    return identity


def chain_identity(args):
    return _with(
        args,
        underlying=_first(args, "underlying", "symbol"),
        expiry_min=args["expiry_min"],
        expiry_max=args["expiry_max"],
        option_type=args["option_type"],
        strike_min=args["strike_min"],
        strike_max=args["strike_max"],
    )


def quote_identity(args):
    return _with(args, contract_symbol=_first(args, "contract_symbol", "symbol"))


def atm_iv_identity(args):
    return _with(args, underlying=_first(args, "underlying", "symbol"))


def underlying_price_identity(args):
    identity = _with(args, underlying=_first(args, "symbol", "underlying"))
    # Only when the signature has one: an override that does not take it is not made
    # to claim a price type it never asked for (same rule as the stock quote tap).
    if "price_type" in args:
        identity["price_type"] = args["price_type"]
    return identity


def account_only_identity(args):
    return _account_identity(args)


def buying_power_identity(args):
    return _with(args, required=args["required"])


def short_put_capacity_identity(args):
    return _with(
        args,
        strike=args["strike"],
        contracts=args["contracts"],
        multiplier=args["multiplier"],
        long_puts=[list(pair) for pair in args["long_puts"]],
    )


#: method -> (provider, identity function). THE list: the guard test, the replay's tape
#: account and ``tap_option_methods`` all read it.
OPTION_TAPPED_METHODS: Dict[str, Tuple[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]]] = {
    "get_option_chain": (OPTION_PROVIDER, chain_identity),
    "get_option_quote": (OPTION_PROVIDER, quote_identity),
    "get_atm_implied_volatility": (OPTION_PROVIDER, atm_iv_identity),
    "get_option_underlying_price": (OPTION_PROVIDER, underlying_price_identity),
    "option_capital_equity": (OPTION_PROVIDER, account_only_identity),
    "get_account_snapshot": (OPTION_PROVIDER, account_only_identity),
    "check_option_buying_power": (OPTION_PROVIDER, buying_power_identity),
    "get_positions": (OPTION_PROVIDER, account_only_identity),
    "check_short_put_assignment_capacity": (OPTION_PROVIDER, short_put_capacity_identity),
}


def tap_option_methods(cls) -> None:
    """Tap every method of ``OPTION_TAPPED_METHODS`` that ``cls`` resolves to an untapped body.

    See the module docstring for the MRO rules. Idempotent.
    """
    if getattr(cls, "_replay_taps_exempt", False):
        return
    for name, (provider, identity) in OPTION_TAPPED_METHODS.items():
        resolved = getattr(cls, name, None)
        if resolved is None or getattr(resolved, "__isabstractmethod__", False):
            continue
        if tapped_boundary(resolved) is not None:
            continue
        if not inspect.isfunction(inspect.getattr_static(cls, name)):
            # A classmethod/staticmethod/property would be silently left untapped.
            raise TypeError(
                f"{cls.__name__}.{name} is not a plain method; it cannot carry the option tap")
        setattr(cls, name, observe_provider(provider, name, identity=identity, record_errors=True)(resolved))
