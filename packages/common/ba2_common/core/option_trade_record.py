"""The option trade record: ONE snapshot shape, written identically by live and backtest.

BT/live option parity, plan Part C (C1). The live path must store enough about each option
entry to be compared, field for field, with the backtest's entry for the same decision. Both
paths reach the SAME submit choke point (``TradeActions._OptionEntryAction._submit_option_order``)
and build the record with the functions below, so the keys and the arithmetic cannot drift;
only the SOURCE VALUES differ (a broker quote live, the as-of bar in a backtest).

Schema ``option_trade_record_v1``::

    {"version": "option_trade_record_v1",
     "legs": [leg_snapshot(...), ...],          # one per leg that had a chain contract
     "legs_without_quote": [contract_symbol],   # legs built with no chain contract (named)
     "structure": structure_snapshot(...)}

and, on every option CLOSING order (``data["exit_record"]``, plan Part C3)::

    {"version": "option_trade_record_v1",
     "trigger": OptionCloseReason value,        # WHY it closed (never a price guess)
     "rule_id": EventAction id | None, "rule_name": str | None,
     "legs": [leg_snapshot(...), ...],          # the legs the close priced from a quote
     "legs_without_quote": [contract_symbol]}

RULES, stated once:

* ``spot`` is REQUIRED. A missing / non-finite / non-positive spot is REFUSED (ValueError),
  never recorded as 0 -- moneyness against a fabricated spot is a fabricated number.
* Unknown stays unknown: a greek, a quote side, open interest or volume the source did not
  publish is ``None``, never 0. A non-finite number (NaN / inf) is also ``None``: it is not a
  measurement, and it is not valid JSON either.
* Every key of ``LEG_SNAPSHOT_FIELDS`` is ALWAYS present (value possibly ``None``), so two
  records compare key by key without a presence test.
* JSON-serialisable: dates are ISO ``YYYY-MM-DD``, datetimes ISO 8601 WITH their zone (a naive
  ``quote_time`` is refused -- an instant with no zone cannot be compared across the paths).

Pure: no DB, no network, no broker.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ba2_common.core.option_spread_model import quoted_half_spread
from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionCloseReason, OrderDirection

OPTION_TRADE_RECORD_VERSION = "option_trade_record_v1"

#: The per-leg keys, in record order. Every one is present in every leg snapshot.
LEG_SNAPSHOT_FIELDS = (
    "contract_symbol", "side", "ratio_qty", "position_intent", "right", "strike", "expiry", "dte", "data_session", "spot",
    "moneyness_pct", "bid", "ask", "mid", "spread_pct", "last", "iv", "delta", "gamma",
    "theta", "vega", "rho", "open_interest", "volume", "greeks_source", "quote_time",
)

#: The structure-level keys, in record order. Every one is present in every structure snapshot.
STRUCTURE_SNAPSHOT_FIELDS = (
    "strategy", "quantity", "multiplier", "net_price", "leg_count",
    "max_loss", "max_loss_state", "max_profit", "max_profit_state", "breakevens",
    "payoff_unavailable_reason",
)


def _num(value: Any) -> Optional[float]:
    """``float(value)`` for a real finite number, else None. Never raises.

    ``bool`` and ``str`` are refused (a ``True`` greek or a ``"0.5"`` price is a bug to see as
    unknown, not to parse), non-finite is unknown."""
    if value is None or isinstance(value, (bool, str, bytes)):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def usable_spot(value: Any) -> Optional[float]:
    """``float(value)`` when it is a usable underlying price (a finite number > 0, including
    numpy / Decimal numbers), else None. THE one spot rule of the record: ``leg_snapshot``
    refuses on it, and the submit path refuses the entry on it before the broker."""
    v = _num(value)
    return v if v is not None and v > 0 else None


def _count(value: Any) -> Optional[int]:
    """An integer count (open interest, volume) or None when unknown. Never raises."""
    v = _num(value)
    return None if v is None else int(v)


def _iso_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise TypeError(f"expected a date, got a datetime {value!r}")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"expected a date, got {type(value).__name__} {value!r}")


def _iso_instant(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"quote_time must be a datetime or None, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"quote_time {value!r} has no time zone; an instant must carry one")
    return value.isoformat()


def moneyness_pct(right: Optional[str], strike: Optional[float], spot: float) -> Optional[float]:
    """Signed distance of the strike from spot, in percent, POSITIVE = OUT OF THE MONEY.

    * call: ``(K / S - 1) * 100`` -- a call struck above spot is OTM and reads positive;
    * put:  ``(1 - K / S) * 100`` -- a put struck below spot is OTM and reads positive.

    So a 5 %-OTM call and a 5 %-OTM put both read +5.0, and an ITM leg of either right reads
    negative. None when the right or strike is unknown."""
    k = _num(strike)
    if k is None or right not in ("call", "put"):
        return None
    if right == "call":
        return (k / spot - 1.0) * 100.0
    return (1.0 - k / spot) * 100.0


def quote_mid_and_spread_pct(bid: Any, ask: Any):
    """``(mid, spread_pct)`` for a VALID two-sided quote, else ``(None, None)``.

    VALID is exactly ``option_spread_model.quoted_half_spread``'s rule (both finite,
    ``bid > 0``, ``ask > bid``): a zero bid is a one-sided market and a locked / crossed
    quote is what a close-proxy store writes (``bid = ask = close``), so neither says what a
    fill would cross. ``spread_pct`` is the QUOTED spread over the mid, ``(ask - bid) / mid
    * 100`` -- unfloored: the spread model's one-tick floor is a charging rule, not a quote."""
    b, a = _num(bid), _num(ask)
    if b is None or a is None or quoted_half_spread(b, a) is None:
        return None, None
    mid = (a + b) / 2.0
    return mid, (a - b) / mid * 100.0


def leg_snapshot(contract: OptionContract, *, side: OrderDirection, ratio_qty: int,
                 position_intent: Optional[str], spot: float, data_session: date,
                 decision_label: date, greeks_source: Optional[str],
                 quote_time: Optional[datetime]) -> Dict[str, Any]:
    """The record of ONE chosen contract as the decision saw it. See the module rules.

    ``side`` / ``ratio_qty`` / ``position_intent`` are the ORDER LEG's (``OptionLeg``): the
    record is self-contained, no join against ``data["legs"]`` needed. ``side`` is stored
    ``"buy"`` / ``"sell"``.

    ``dte`` is calendar days from ``decision_label`` (the session the order executes in, the
    label every option DTE window is anchored on); ``data_session`` is the completed session
    whose data the decision read.

    GREEKS SOURCE, PER ROW: the contract's own ``greeks_source`` when the chain row states one
    (``"broker"`` live; ``"bs_from_close"`` / ``"chain_snapshot"`` per backtest row), else
    ``greeks_source`` -- the ACCOUNT's declaration. Neither -> REFUSED (ValueError): a record
    that cannot say how its greeks were measured cannot be compared across the two paths."""
    source = contract.greeks_source or greeks_source
    if not source:
        raise ValueError(f"leg_snapshot({contract.symbol!r}): no greeks source -- the chain "
                         f"row states none and the account declares no OPTION_GREEKS_SOURCE")
    if side not in (OrderDirection.BUY, OrderDirection.SELL):
        raise ValueError(f"leg_snapshot({contract.symbol!r}): side {side!r} is neither BUY "
                         f"nor SELL")
    ratio = _num(ratio_qty)
    if ratio is None or ratio <= 0:
        raise ValueError(f"leg_snapshot({contract.symbol!r}): ratio_qty {ratio_qty!r} must "
                         f"be a positive number")
    s = usable_spot(spot)
    if s is None:
        raise ValueError(f"leg_snapshot({getattr(contract, 'symbol', contract)!r}): spot "
                         f"{spot!r} is not a usable underlying price; refusing to record a "
                         f"moneyness against it")
    if not isinstance(decision_label, date) or isinstance(decision_label, datetime):
        raise TypeError(f"decision_label must be a date, got {decision_label!r}")
    right_raw = getattr(contract.option_type, "value", contract.option_type)
    right = str(right_raw).lower() if right_raw is not None else None
    strike = _num(contract.strike)
    expiry = contract.expiry
    mid, spread_pct = quote_mid_and_spread_pct(contract.bid, contract.ask)
    return {
        "contract_symbol": contract.symbol,
        "side": side.value.lower(),
        "ratio_qty": int(ratio),
        "position_intent": position_intent,
        "right": right,
        "strike": strike,
        "expiry": _iso_date(expiry),
        "dte": (expiry - decision_label).days if isinstance(expiry, date) else None,
        "data_session": _iso_date(data_session),
        "spot": s,
        "moneyness_pct": moneyness_pct(right, strike, s),
        "bid": _num(contract.bid),
        "ask": _num(contract.ask),
        "mid": mid,
        "spread_pct": spread_pct,
        "last": _num(contract.last),
        "iv": _num(contract.implied_volatility),
        "delta": _num(contract.delta),
        "gamma": _num(contract.gamma),
        "theta": _num(contract.theta),
        "vega": _num(contract.vega),
        "rho": _num(contract.rho),
        "open_interest": _count(contract.open_interest),
        "volume": _count(contract.volume),
        "greeks_source": source,
        "quote_time": _iso_instant(quote_time),
    }


def structure_snapshot(legs: Sequence[Dict[str, Any]], *, strategy: str, quantity: int,
                       multiplier: int, net_price: float, max_loss: Optional[float],
                       max_profit: Optional[float], breakevens: Optional[Sequence[float]],
                       max_loss_state: Optional[str] = None,
                       max_profit_state: Optional[str] = None,
                       payoff_unavailable_reason: Optional[str] = None) -> Dict[str, Any]:
    """The structure-level record: what was submitted and its payoff limits.

    ``net_price`` is the submitted limit in the submit path's own convention (a multi-leg
    limit is the signed net, positive debit / negative credit; a single-leg limit is the
    positive premium). ``max_loss`` / ``max_profit`` are DOLLARS PER ONE STRUCTURE UNIT (one
    contract of the order); None when not a measurement, with the payoff engine's state
    (``MEASURED`` / ``UNBOUNDED`` / ``UNMEASURABLE``) beside it so an unlimited tail is never
    confused with "could not work it out". ``breakevens`` is None when the payoff could not be
    derived (``payoff_unavailable_reason`` says why), and a possibly-empty list otherwise.

    ``legs`` are the leg snapshots of the same record: checked for the full key set (a record
    with a short leg snapshot is refused, not written) and counted."""
    for i, leg in enumerate(legs):
        missing = [k for k in LEG_SNAPSHOT_FIELDS if k not in leg]
        if missing:
            raise ValueError(f"structure_snapshot: leg {i} lacks {missing}")
    q = _num(quantity)
    m = _num(multiplier)
    if q is None or m is None:
        raise ValueError(f"structure_snapshot: quantity {quantity!r} / multiplier "
                         f"{multiplier!r} must be numbers")
    return {
        "strategy": strategy,
        "quantity": int(q),
        "multiplier": int(m),
        "net_price": _num(net_price),
        "leg_count": len(legs),
        "max_loss": _num(max_loss),
        "max_loss_state": max_loss_state,
        "max_profit": _num(max_profit),
        "max_profit_state": max_profit_state,
        "breakevens": (None if breakevens is None
                       else [b for b in (_num(x) for x in breakevens) if b is not None]),
        "payoff_unavailable_reason": payoff_unavailable_reason,
    }


def entry_record(legs: List[Dict[str, Any]], structure: Dict[str, Any],
                 legs_without_quote: Sequence[str]) -> Dict[str, Any]:
    """Assemble the versioned record from its parts."""
    return {
        "version": OPTION_TRADE_RECORD_VERSION,
        "legs": list(legs),
        "legs_without_quote": list(legs_without_quote),
        "structure": structure,
    }


def snapshot_legs(pairs: Sequence[Tuple[Any, Optional[OptionContract]]], *, spot: Any,
                  data_session: date, decision_label: date, greeks_source: Optional[str]
                  ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(leg snapshots, legs_without_quote)`` for ``(order leg, chain contract)`` pairs.

    THE ONE loop both records use: the entry (each ``OptionLeg`` with its own ``quote``) and
    the exit (each CLOSING leg with the quote the close priced itself from). A pair whose
    contract is None gets no snapshot and is NAMED in ``legs_without_quote`` -- a leg the
    record could not describe is visible, never silently dropped. ``leg`` is read for
    ``contract_symbol`` / ``side`` / ``ratio_qty`` / ``position_intent`` only."""
    snapshots: List[Dict[str, Any]] = []
    without_quote: List[str] = []
    for leg, contract in pairs:
        if contract is None:
            without_quote.append(leg.contract_symbol)
            continue
        snapshots.append(leg_snapshot(
            contract, side=leg.side, ratio_qty=leg.ratio_qty,
            position_intent=leg.position_intent, spot=spot, data_session=data_session,
            decision_label=decision_label, greeks_source=greeks_source,
            quote_time=contract.quote_time))
    return snapshots, without_quote


def contract_from_quote(quote: Any, leg: Any) -> Optional[OptionContract]:
    """The ``OptionContract`` a leg snapshot reads, from what a CLOSE priced itself with.

    A close reads ``account.get_option_quote`` -- an ``OptionQuote``, which carries the market
    (bid/ask/last/iv/greeks/volume and its quote ``timestamp``) but not the contract's terms.
    The terms come from the closing ``leg`` (right / strike / expiry / underlying), so the
    snapshot describes the same contract with the same fields an entry snapshot has.
    ``open_interest`` is not on a quote and stays None (unknown, never 0); ``greeks_source``
    stays None so the ACCOUNT's declaration applies, as it does for any row that states none.
    None when there is no quote. An ``OptionContract`` is returned unchanged."""
    if quote is None:
        return None
    if isinstance(quote, OptionContract):
        return quote
    return OptionContract(
        symbol=getattr(quote, "symbol", None) or leg.contract_symbol,
        underlying=leg.underlying,
        option_type=leg.option_type,
        strike=leg.strike,
        expiry=leg.expiry,
        bid=quote.bid, ask=quote.ask, last=quote.last,
        implied_volatility=quote.implied_volatility,
        delta=quote.delta, gamma=quote.gamma, theta=quote.theta, vega=quote.vega,
        open_interest=None,
        volume=getattr(quote, "volume", None),
        rho=getattr(quote, "rho", None),
        quote_time=getattr(quote, "timestamp", None),
        greeks_source=None,
    )


def _close_reason_value(trigger: Any) -> str:
    """The ``OptionCloseReason`` value of ``trigger`` (a member or its string). An unknown
    trigger is REFUSED (ValueError): a free-text reason is exactly the drift the enum exists
    to stop."""
    return OptionCloseReason(getattr(trigger, "value", trigger)).value


def exit_record(trigger: Any, *, rule_id: Optional[int] = None,
                rule_name: Optional[str] = None,
                legs: Sequence[Dict[str, Any]] = (),
                legs_without_quote: Sequence[str] = ()) -> Dict[str, Any]:
    """The versioned EXIT record of one closing order (plan Part C3)::

        {"version", "trigger": OptionCloseReason value, "rule_id", "rule_name",
         "legs": [leg_snapshot...], "legs_without_quote": [contract_symbol...]}

    ``trigger`` says WHY the close happened (see ``OptionCloseReason``); ``rule_id`` /
    ``rule_name`` name the firing ``EventAction`` when a rule fired it (None otherwise).
    ``legs`` carry a snapshot for each leg the close fetched a quote for; the rest are named
    in ``legs_without_quote`` (an expiry settlement or a liquidation prices from no quote)."""
    for i, leg in enumerate(legs):
        missing = [k for k in LEG_SNAPSHOT_FIELDS if k not in leg]
        if missing:
            raise ValueError(f"exit_record: leg {i} lacks {missing}")
    return {
        "version": OPTION_TRADE_RECORD_VERSION,
        "trigger": _close_reason_value(trigger),
        "rule_id": rule_id,
        "rule_name": rule_name,
        "legs": list(legs),
        "legs_without_quote": list(legs_without_quote),
    }


def exit_record_lean(trigger: Any, *, rule_id: Optional[int] = None,
                     rule_name: Optional[str] = None) -> Dict[str, Any]:
    """The exit record of a close on a run that does NOT record option trades (a GA fitness
    trial, ``option_trade_records`` False): only WHY it closed -- what the trade row's
    ``exit_reason`` reads -- with no leg snapshot and no spot read. ``lean`` says so."""
    return {"version": OPTION_TRADE_RECORD_VERSION, "trigger": _close_reason_value(trigger),
            "rule_id": rule_id, "rule_name": rule_name, "lean": True}


#: WHICH settlement names a structure's ``Transaction.close_reason`` when its legs settle
#: DIFFERENTLY at expiry (a spread with one leg assigned and one expired). The most
#: consequential event wins -- an assignment delivered shares, an exercise moved stock at the
#: strike, an expiry moved nothing -- so the reason does not depend on the ORDER the OCC
#: reports the legs in (live) or the order the legs are iterated in (backtest). One rule, both
#: runtimes: ``settlement_close_reason`` is called by the backtest's combo settlement and by
#: the live OCC-activity reconciler.
SETTLEMENT_CLOSE_PRECEDENCE = (
    OptionCloseReason.ASSIGNED, OptionCloseReason.EXERCISED, OptionCloseReason.EXPIRED_OTM,
)


def settlement_close_reason(triggers: Sequence[Any]) -> str:
    """The ``close_reason`` of a structure whose legs settled with ``triggers`` (each an
    ``OptionCloseReason`` or its value): the first of ``SETTLEMENT_CLOSE_PRECEDENCE`` present.
    A trigger outside the settlement set, or none at all, is REFUSED (ValueError) -- this is
    only ever asked about settlements."""
    values = {_close_reason_value(t) for t in triggers}
    for reason in SETTLEMENT_CLOSE_PRECEDENCE:
        if reason.value in values:
            others = values - {r.value for r in SETTLEMENT_CLOSE_PRECEDENCE}
            if others:
                raise ValueError(f"settlement_close_reason: {sorted(others)} are not "
                                 f"settlement events")
            return reason.value
    raise ValueError(f"settlement_close_reason: no settlement event in {sorted(values)}")


def exit_record_error(trigger: Any, error: str, *, rule_id: Optional[int] = None,
                      rule_name: Optional[str] = None) -> Dict[str, Any]:
    """The exit record when its snapshots could not be built. The TRIGGER is still recorded
    (it never depends on a quote); ``error`` says what failed. A close is never blocked by
    its record."""
    return {
        "version": OPTION_TRADE_RECORD_VERSION,
        "trigger": _close_reason_value(trigger),
        "rule_id": rule_id,
        "rule_name": rule_name,
        "error": error,
    }
