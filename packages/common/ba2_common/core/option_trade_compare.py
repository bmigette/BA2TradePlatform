"""Pair live option structures with their backtest twins and diff them field by field.

BT/live option parity, plan Part C5. Pure functions over plain rows (no DB, no network): the
CLI ``tools/compare_option_trade_records.py`` loads the rows read-only and hands them here.

INPUTS
------
* LIVE: ``TradingOrder``-like dicts (enums by NAME or value, ``data`` as a dict) and
  ``Transaction``-like dicts keyed by id. A structure is one order carrying
  ``data["entry_record"]`` (a multi-leg PARENT, or a single-leg ticket) plus its leg children;
  its exits are the opposite-side fills of the same contracts in the same transaction, whose
  ``exit_record`` rides the closing order or its parent (``option_trade_record`` module doc).
* BACKTEST: the persisted ``Backtest.trades`` rows. A leg is a row; the legs of one entry are
  the rows sharing ``transaction_id`` and the ENTRY BAR (``entry_time`` date) -- a later entry
  in the same transaction (a roll) is its own structure, as it is live. The structure-level
  record parts ride the FIRST leg row only (``BacktestAccount._attach_option_records``).

THE PAIRING KEY: ``(underlying, option_strategy, data_session)``
----------------------------------------------------------------
``data_session`` is the completed session whose data the decision read, and it is written by
the SAME code on both sides into every leg snapshot of the entry record
(``leg_snapshot(data_session=...)``, fed by ``decision_data_session(account.decision_label())``).
Live decides during session N(D) and reads D; the backtest decides on bar D and reads D, and
stamps its next-bar-open fill with D too. So the record's ``data_session`` is IDENTICAL on
both sides by construction and needs no calendar arithmetic: it is the cleanest key. Raw fill
dates are NOT comparable (live N(D) vs backtest D) and are never used as the key directly.

When a side has no usable entry record (``{"error": ...}``, or none at all) the key is
DERIVED from the fill instead and the structure says so (``key_source == "fill"``):
* live: ``prior_regular_session(order.created_at)`` -- the live decision rule itself
  (``created_at`` is stored naive UTC by the platform; a naive value is read as UTC);
* backtest: the ``entry_time`` date, which IS bar D.

A structure whose strategy is unknown on one side (a backtest row with no record carries no
``option_strategy``) pairs in a SECOND pass on ``(underlying, data_session)`` alone, and the
pair's ``note`` says so. Nothing is skipped silently: every structure is paired, or listed as
unmatched with the most specific reason the other side supports, and every record error or
absence is in ``issues`` and in the diff.

DIFF CONVENTIONS
----------------
One row per (pair, phase, leg, field): ``live``, ``backtest``, ``abs_delta = backtest - live``,
``rel_delta = |abs_delta| / |live|`` (numeric fields), ``within_tolerance``. Greeks and IV are
SOURCE-TAGGED: each such row carries both paths' ``greeks_source`` and ``source_mismatch`` --
a broker greek and a Black-Scholes-from-close greek are shown side by side, never compared as
if they were one measurement. P&L is GROSS (``(exit - entry) * qty * dir * multiplier``) on
both sides, since live commissions are not on the order rows; the backtest's
commission-inclusive ``pnl`` is kept in the pair metadata.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ba2_common.core.option_trade_record import (
    LEG_SNAPSHOT_FIELDS, SETTLEMENT_CLOSE_PRECEDENCE, settlement_close_reason,
)

#: ``key_source`` values: where a structure's ``data_session`` came from.
KEY_FROM_RECORD = "record"
KEY_FROM_FILL = "fill"

#: Leg snapshot fields compared numerically.
_SNAPSHOT_NUMERIC = ("dte", "spot", "moneyness_pct", "bid", "ask", "mid", "spread_pct", "last",
                     "iv", "delta", "gamma", "theta", "vega", "rho", "open_interest", "volume")
#: Leg snapshot fields whose value depends on HOW greeks were measured: source-tagged rows.
_SOURCE_TAGGED = frozenset({"iv", "delta", "gamma", "theta", "vega", "rho"})
#: Leg identity fields compared exactly.
_LEG_EXACT = ("contract_symbol", "right", "side", "expiry", "ratio_qty")
#: Structure snapshot fields compared (numeric ones get tolerances, the rest exact).
_STRUCT_NUMERIC = ("quantity", "multiplier", "net_price", "leg_count", "max_loss", "max_profit")
_STRUCT_EXACT = ("max_loss_state", "max_profit_state", "payoff_unavailable_reason")

_EXECUTED = frozenset({"filled", "partially_filled"})
_SETTLEMENT_VALUES = frozenset(r.value for r in SETTLEMENT_CLOSE_PRECEDENCE)


# ============================================================ small helpers

def _enum(value: Any) -> Optional[str]:
    """An enum stored by NAME (``"FILLED"``) or value (``"filled"``), or a member: lower str."""
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, (bool, str)):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _instant(value: Any) -> Optional[datetime]:
    """A tz-aware instant from a DB value. The platform writes UTC and sqlite drops the zone,
    so a NAIVE value is read as UTC (stated in the module doc)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _day(value: Any) -> Optional[str]:
    """``YYYY-MM-DD`` of a date/datetime/ISO string (its OWN calendar date, no zone shift)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value)
    return s[:10] if len(s) >= 10 else None


def _live_data_session(created_at: Any) -> Optional[str]:
    """The session a live decision at ``created_at`` read: ``prior_regular_session`` of it."""
    moment = _instant(created_at)
    if moment is None:
        return None
    from ba2_common.core.market_calendar import prior_regular_session
    return prior_regular_session(moment).isoformat()


def _wavg(fills: Sequence[Tuple[Optional[float], Optional[float]]]):
    """(quantity-weighted average price, total quantity) over ``(price, qty)`` pairs."""
    usable = [(p, q) for p, q in fills if p is not None and q is not None and q > 0]
    total = sum(q for _, q in usable)
    if total <= 0:
        return None, None
    return sum(p * q for p, q in usable) / total, total


def _record_status(record: Any) -> Tuple[str, Optional[str]]:
    """(``ok`` | ``error`` | ``missing``, error text) of a record dict."""
    if not isinstance(record, dict):
        return "missing", None
    if "error" in record:
        return "error", str(record["error"])
    return "ok", None


def _combine_triggers(triggers: Iterable[Optional[str]]) -> Optional[str]:
    """ONE structure trigger from its legs': the single value, the settlement precedence when
    every leg settled, else the sorted distinct values joined by ``|`` (visible, not picked)."""
    vals = sorted({t for t in triggers if t})
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    if set(vals) <= _SETTLEMENT_VALUES:
        return settlement_close_reason(vals)
    return "|".join(vals)


def _slippage(side: Optional[str], fill: Optional[float], mid: Optional[float]) -> Optional[float]:
    """Fill vs quote mid, POSITIVE = paid away (bought above / sold below the mid)."""
    if fill is None or mid is None or side not in ("buy", "sell"):
        return None
    return fill - mid if side == "buy" else mid - fill


def _signed(side: Optional[str]) -> Optional[float]:
    return 1.0 if side == "buy" else (-1.0 if side == "sell" else None)


def _gross(side: Optional[str], entry: Optional[float], exit_: Optional[float],
           qty: Optional[float], mult: Optional[float]) -> Optional[float]:
    d = _signed(side)
    if None in (d, entry, exit_, qty, mult):
        return None
    return (exit_ - entry) * qty * d * mult


def _leg_record(contract: str, side: Optional[str], right: Optional[str], strike: Any,
                expiry: Any, snapshot: Optional[dict]) -> Dict[str, Any]:
    ratio = (snapshot or {}).get("ratio_qty")
    return {"contract_symbol": contract, "side": side, "right": right,
            "strike": _num(strike), "expiry": _day(expiry),
            "ratio_qty": int(ratio) if _num(ratio) is not None else 1,
            "entry_snapshot": snapshot, "exit_snapshot": None, "entry_fill": None,
            "exit_fill": None, "quantity": None, "exit_quantity": None, "multiplier": None,
            "exit_trigger": None, "exit_record_status": None, "exit_record_error": None,
            "gross_pnl": None}


def _finish_structure(s: Dict[str, Any]) -> Dict[str, Any]:
    """Derived structure-level numbers from its legs (identical arithmetic on both sides)."""
    legs = s["legs"]
    net_fill = net_mid = 0.0
    fill_ok = mid_ok = bool(legs)
    gross = 0.0
    gross_ok = bool(legs)
    for leg in legs:
        d = _signed(leg["side"])
        mid = (leg["entry_snapshot"] or {}).get("mid")
        if d is None or leg["entry_fill"] is None:
            fill_ok = False
        else:
            net_fill += d * leg["ratio_qty"] * leg["entry_fill"]
        if d is None or _num(mid) is None:
            mid_ok = False
        else:
            net_mid += d * leg["ratio_qty"] * float(mid)
        if leg["gross_pnl"] is None:
            gross_ok = False
        else:
            gross += leg["gross_pnl"]
    s["entry_net_fill"] = net_fill if fill_ok else None
    s["entry_net_mid"] = net_mid if mid_ok else None
    s["entry_net_slippage_vs_mid"] = (net_fill - net_mid) if fill_ok and mid_ok else None
    s["net_gross_pnl"] = gross if gross_ok else None
    return s


# ============================================================ live

def live_structures(orders: Sequence[Dict[str, Any]], transactions: Dict[Any, Dict[str, Any]],
                    issues: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Normalised live option structures from order and transaction rows (not mutated).

    ``issues`` (optional) collects problems that belong to no structure (an option order with
    no transaction, a transaction with no identifiable entry)."""
    issues = issues if issues is not None else []
    opt = [o for o in orders if _enum(o.get("asset_class")) == "option"]

    def order_key(o):
        t = _instant(o.get("created_at"))
        return (t is None, t or datetime.min.replace(tzinfo=timezone.utc), o.get("id") or 0)

    by_txn: Dict[Any, List[dict]] = {}
    for o in opt:
        if o.get("transaction_id") is None:
            issues.append(f"live order {o.get('id')} ({o.get('contract_symbol') or o.get('symbol')}) "
                          f"is an option order with no transaction; not part of any structure")
            continue
        by_txn.setdefault(o["transaction_id"], []).append(o)

    out: List[Dict[str, Any]] = []
    for txn_id, txn_orders in sorted(by_txn.items(), key=lambda kv: str(kv[0])):
        txn_orders = sorted(txn_orders, key=order_key)
        by_id = {o.get("id"): o for o in txn_orders}
        children: Dict[Any, List[dict]] = {}
        for o in txn_orders:
            if o.get("parent_order_id") is not None:
                children.setdefault(o["parent_order_id"], []).append(o)
        heads = [o for o in txn_orders if "entry_record" in (o.get("data") or {})]
        if not heads:
            fallback = [o for o in txn_orders
                        if o.get("parent_order_id") is None
                        and _enum(o.get("option_strategy")) != "close"
                        and not str(o.get("position_intent") or "").endswith("to_close")
                        and "exit_record" not in (o.get("data") or {})]
            if not fallback:
                issues.append(f"live transaction {txn_id}: option orders but no identifiable "
                              f"entry order; not compared")
                continue
            heads = [fallback[0]]
        txn = transactions.get(txn_id) or {}
        for head in heads:
            out.append(_live_structure(head, txn_orders, by_id, children, txn, heads))
    return out


def _live_structure(head, txn_orders, by_id, children, txn, heads) -> Dict[str, Any]:
    data = head.get("data") or {}
    record = data.get("entry_record")
    status, err = _record_status(record)
    s_issues: List[str] = []
    ref = {"transaction_id": head.get("transaction_id"), "order_id": head.get("id")}
    if status == "missing":
        s_issues.append(f"live txn {ref['transaction_id']} order {ref['order_id']}: entry "
                        f"record missing")
    elif status == "error":
        s_issues.append(f"live txn {ref['transaction_id']} order {ref['order_id']}: entry "
                        f"record error: {err}")
    snaps = {leg.get("contract_symbol"): leg
             for leg in ((record or {}).get("legs") or []) if isinstance(leg, dict)}
    if status == "ok" and (record or {}).get("legs_without_quote"):
        s_issues.append(f"live txn {ref['transaction_id']}: legs without quote in the entry "
                        f"record: {record['legs_without_quote']}")

    if head.get("contract_symbol"):
        leg_orders = [head]
    else:
        kids = children.get(head.get("id"), [])
        if status == "ok":
            named = set(snaps) | set((record or {}).get("legs_without_quote") or [])
            leg_orders = [k for k in kids if k.get("contract_symbol") in named]
        else:
            leg_orders = [k for k in kids
                          if not str(k.get("position_intent") or "").endswith("to_close")]

    # PAIRING KEY
    sessions = sorted({leg.get("data_session") for leg in snaps.values() if leg.get("data_session")})
    if status == "ok" and sessions:
        data_session, key_source = sessions[0], KEY_FROM_RECORD
        if len(sessions) > 1:
            s_issues.append(f"live txn {ref['transaction_id']}: entry legs disagree on "
                            f"data_session {sessions}")
    else:
        data_session, key_source = _live_data_session(head.get("created_at")), KEY_FROM_FILL

    unit_ids = {head.get("id")} | {o.get("id") for o in leg_orders}
    other_heads = {h.get("id") for h in heads if h is not head}
    head_t = _instant(head.get("created_at"))
    legs: List[Dict[str, Any]] = []
    exit_times: List[datetime] = []
    exit_statuses: List[Tuple[str, Optional[str]]] = []
    exit_carriers_seen = set()
    for lo in leg_orders:
        contract = lo.get("contract_symbol")
        side = _enum(lo.get("side"))
        leg = _leg_record(contract, side, _enum(lo.get("option_type")), lo.get("strike"),
                          lo.get("expiry"), snaps.get(contract))
        leg["multiplier"] = _num(lo.get("multiplier") or head.get("multiplier"))
        if _enum(lo.get("status")) in _EXECUTED:
            leg["entry_fill"] = _num(lo.get("open_price"))
            leg["quantity"] = _num(lo.get("filled_qty")) or _num(lo.get("quantity"))
        # EXITS: opposite-side fills of this contract, elsewhere in the transaction, not before
        # the entry, and not an opening leg of a later entry (a roll re-opening the contract).
        exits = []
        for o in txn_orders:
            if o.get("id") in unit_ids or o.get("contract_symbol") != contract:
                continue
            if o.get("parent_order_id") in other_heads and not str(
                    o.get("position_intent") or "").endswith("to_close"):
                continue
            if _enum(o.get("status")) not in _EXECUTED or _enum(o.get("side")) == side:
                continue
            t = _instant(o.get("created_at"))
            if head_t is not None and t is not None and t < head_t:
                continue
            exits.append(o)
        if exits:
            px, q = _wavg([(_num(o.get("open_price")),
                            _num(o.get("filled_qty")) or _num(o.get("quantity"))) for o in exits])
            leg["exit_fill"], leg["exit_quantity"] = px, q
            last = exits[-1]
            carrier = last if "exit_record" in (last.get("data") or {}) else by_id.get(
                last.get("parent_order_id"))
            xrec = (carrier.get("data") or {}).get("exit_record") if carrier else None
            xs, xerr = _record_status(xrec)
            leg["exit_record_status"], leg["exit_record_error"] = xs, xerr
            if isinstance(xrec, dict):
                leg["exit_trigger"] = xrec.get("trigger")
                if xrec.get("lean"):
                    leg["exit_record_status"] = "lean"
                for xl in xrec.get("legs") or []:
                    if isinstance(xl, dict) and xl.get("contract_symbol") == contract:
                        leg["exit_snapshot"] = xl
            if carrier is not None and id(carrier) not in exit_carriers_seen:
                exit_carriers_seen.add(id(carrier))
                exit_statuses.append((leg["exit_record_status"], xerr))
            elif carrier is None:
                exit_statuses.append(("missing", None))
            lt = _instant(last.get("created_at"))
            if lt is not None:
                exit_times.append(lt)
            leg["gross_pnl"] = _gross(side, leg["entry_fill"], leg["exit_fill"],
                                      min(x for x in (leg["quantity"], leg["exit_quantity"])
                                          if x is not None)
                                      if leg["quantity"] is not None else None,
                                      leg["multiplier"])
        legs.append(leg)

    exit_block = _exit_block(legs, exit_statuses,
                             derived_session=(_live_data_session(max(exit_times))
                                              if exit_times else None),
                             close_reason=txn.get("close_reason"))
    for st, e in exit_statuses:
        if st == "error":
            s_issues.append(f"live txn {ref['transaction_id']}: exit record error: {e}")
        elif st == "missing":
            s_issues.append(f"live txn {ref['transaction_id']}: a close carries no exit record")
    if not any(_enum(lo.get("status")) in _EXECUTED for lo in leg_orders):
        s_issues.append(f"live txn {ref['transaction_id']} order {ref['order_id']}: entry "
                        f"never filled (status {_enum(head.get('status'))})")
    return _finish_structure({
        "path": "live", "ref": ref,
        "underlying": (head.get("underlying_symbol") or txn.get("symbol")
                       or head.get("symbol") or "").upper(),
        "strategy": head.get("option_strategy") or txn.get("option_strategy"),
        "data_session": data_session, "key_source": key_source,
        "entry_record_status": status, "entry_record_error": err,
        "structure": (record or {}).get("structure") if status == "ok" else None,
        "entry_time": str(head.get("created_at")) if head.get("created_at") is not None else None,
        "legs": legs, "exit": exit_block, "issues": s_issues,
        "backtest_net_pnl_incl_commission": None,
    })


def _exit_block(legs, statuses, *, derived_session, close_reason) -> Dict[str, Any]:
    closed = [leg for leg in legs if leg["exit_fill"] is not None or leg["exit_trigger"]]
    if not closed:
        status = "open"
    elif any(st == "error" for st, _ in statuses):
        status = "error"
    elif any(st == "missing" for st, _ in statuses):
        status = "missing"
    elif any(st == "lean" for st, _ in statuses):
        status = "lean"
    else:
        status = "ok"
    if closed and len(closed) < len(legs):
        status = f"partial:{status}"
    sessions = sorted({(leg["exit_snapshot"] or {}).get("data_session") for leg in closed
                       if (leg["exit_snapshot"] or {}).get("data_session")})
    if sessions:
        session, source = sessions[-1], KEY_FROM_RECORD
    else:
        session, source = (derived_session, KEY_FROM_FILL) if closed else (None, None)
    errors = sorted({e for st, e in statuses if st == "error" and e})
    return {"status": status, "trigger": _combine_triggers(leg["exit_trigger"] for leg in closed),
            "data_session": session, "data_session_source": source,
            "close_reason": close_reason, "errors": errors}


# ============================================================ backtest

def backtest_structures(trades: Sequence[Dict[str, Any]],
                        issues: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Normalised backtest option structures from ``Backtest.trades`` rows (not mutated)."""
    issues = issues if issues is not None else []
    units: Dict[Any, List[dict]] = {}
    order: List[Any] = []
    for i, row in enumerate(trades or ()):
        if not row.get("contract_symbol"):
            continue
        txn = row.get("transaction_id")
        key = (txn, _day(row.get("entry_time"))) if txn is not None else ("row", i)
        if key not in units:
            units[key] = []
            order.append(key)
        units[key].append(row)
    return [_bt_structure(k, units[k]) for k in order]


def _bt_structure(key, rows) -> Dict[str, Any]:
    txn = key[0] if key[0] != "row" else None
    ref = {"transaction_id": txn, "entry_bar": key[1] if txn is not None else None}
    s_issues: List[str] = []
    head = next((r for r in rows if isinstance(r.get("entry_record"), dict)
                 and "version" in r["entry_record"]), None)
    if head is not None:
        status, err = _record_status(head["entry_record"])
    else:
        status, err = "missing", None
    if status == "missing":
        why = ("the run did not record option trades (option_trade_records False?)"
               if all("entry_record" not in r for r in rows) else "no record head row")
        s_issues.append(f"backtest txn {txn} bar {key[1]}: entry record missing ({why})")
    elif status == "error":
        s_issues.append(f"backtest txn {txn} bar {key[1]}: entry record error: {err}")
    elif head["entry_record"].get("legs_without_quote"):
        s_issues.append(f"backtest txn {txn}: legs without quote in the entry record: "
                        f"{head['entry_record']['legs_without_quote']}")
    strategy = next((r.get("option_strategy") for r in rows if r.get("option_strategy")), None)

    legs = []
    for r in rows:
        er = r.get("entry_record") if isinstance(r.get("entry_record"), dict) else {}
        snap = er.get("leg") if isinstance(er.get("leg"), dict) else None
        side = _enum(r.get("direction"))
        leg = _leg_record(r.get("contract_symbol"), side, _enum(r.get("option_type")),
                          r.get("strike"), r.get("expiry"), snap)
        leg["multiplier"] = _num(r.get("multiplier"))
        leg["entry_fill"] = _num(r.get("entry_price"))
        leg["quantity"] = _num(r.get("size"))
        is_open = r.get("exit_reason") == "open_at_end"
        xr = r.get("exit_record")
        if not is_open:
            leg["exit_fill"] = _num(r.get("exit_price"))
            leg["exit_quantity"] = leg["quantity"]
            xs, xerr = _record_status(xr)
            leg["exit_record_status"], leg["exit_record_error"] = xs, xerr
            leg["exit_trigger"] = (xr or {}).get("trigger") if isinstance(xr, dict) else None
            if leg["exit_trigger"] is None and r.get("exit_reason") != "unrecorded":
                leg["exit_trigger"] = r.get("exit_reason")
            if isinstance(xr, dict) and isinstance(xr.get("leg"), dict):
                leg["exit_snapshot"] = xr["leg"]
            leg["gross_pnl"] = _gross(side, leg["entry_fill"], leg["exit_fill"],
                                      leg["quantity"], leg["multiplier"])
            leg["_exit_day"] = _day(r.get("exit_time"))
            if r.get("exit_reason") == "unrecorded":
                s_issues.append(f"backtest txn {txn}: {r.get('contract_symbol')} closed with "
                                f"no exit record (exit_reason 'unrecorded')")
        leg["_net_pnl"] = _num(r.get("pnl"))
        legs.append(leg)

    sessions = sorted({(leg["entry_snapshot"] or {}).get("data_session") for leg in legs
                       if (leg["entry_snapshot"] or {}).get("data_session")})
    if status == "ok" and sessions:
        data_session, key_source = sessions[0], KEY_FROM_RECORD
        if len(sessions) > 1:
            s_issues.append(f"backtest txn {txn}: entry legs disagree on data_session {sessions}")
        if sessions[0] != key[1]:
            s_issues.append(f"backtest txn {txn}: entry record data_session {sessions[0]} != "
                            f"entry bar {key[1]} (the D == data_session invariant is broken)")
    else:
        data_session, key_source = key[1], KEY_FROM_FILL

    statuses = [(leg["exit_record_status"], leg["exit_record_error"]) for leg in legs
                if leg["exit_record_status"] is not None]
    for st, e in sorted(set(statuses), key=str):
        if st == "error":
            s_issues.append(f"backtest txn {txn}: exit record error: {e}")
    exit_days = [leg.pop("_exit_day") for leg in legs if "_exit_day" in leg]
    net_pnls = [leg.pop("_net_pnl") for leg in legs]
    # ``open_at_end`` is the run ending, not a close: an open structure has no close reason,
    # as its live twin's open transaction has none.
    reasons = sorted({r.get("exit_reason") for r in rows
                      if r.get("exit_reason") and r.get("exit_reason") != "open_at_end"})
    exit_block = _exit_block(legs, statuses,
                             derived_session=max(d for d in exit_days if d) if any(exit_days) else None,
                             close_reason="|".join(reasons) if reasons else None)
    return _finish_structure({
        "path": "backtest", "ref": ref,
        "underlying": (rows[0].get("underlying_symbol") or rows[0].get("symbol") or "").upper(),
        "strategy": strategy, "data_session": data_session, "key_source": key_source,
        "entry_record_status": status, "entry_record_error": err,
        "structure": head["entry_record"].get("structure") if status == "ok" else None,
        "entry_time": rows[0].get("entry_time"),
        "legs": legs, "exit": exit_block, "issues": s_issues,
        "backtest_net_pnl_incl_commission": (sum(net_pnls) if all(p is not None for p in net_pnls)
                                             else None),
    })


# ============================================================ pairing

def _describe(s) -> str:
    return f"{s['underlying']} {s['strategy']} on {s['data_session']}"


def _unmatched_reason(s, others, other_name, self_name) -> str:
    if s["data_session"] is None:
        return (f"no pairing key: entry record {s['entry_record_status']} and no fill time "
                f"to derive the session from")
    same_u = [o for o in others if o["underlying"] == s["underlying"]]
    same_day = [o for o in same_u if o["data_session"] == s["data_session"]]
    if same_day:
        strategies = sorted({str(o["strategy"]) for o in same_day})
        return (f"{other_name} entered {s['underlying']} on {s['data_session']} with a "
                f"different strategy ({', '.join(strategies)}); or its twin is already paired")
    same_strat = sorted({o["data_session"] for o in same_u
                         if o["strategy"] == s["strategy"] and o["data_session"]})
    if same_strat:
        near = sorted(same_strat, key=lambda d: abs((date.fromisoformat(d) - date.fromisoformat(
            s["data_session"])).days))[:3]
        return (f"{other_name} entered the same {s['underlying']} {s['strategy']} on other "
                f"session(s) {', '.join(sorted(near))}, not on {s['data_session']}")
    if same_u:
        return (f"{self_name} entered, {other_name} did not: {other_name} traded "
                f"{s['underlying']} only with other strategies/sessions")
    return f"{self_name} entered, {other_name} did not: no {other_name} {s['underlying']} structure"


def pair_structures(live: Sequence[dict], backtest: Sequence[dict]):
    """``(pairs, unmatched_live, unmatched_backtest)``.

    ``pairs`` are ``(live, backtest, note)``. Pass 1: exact ``(underlying, strategy,
    data_session)``, first-come in input order. Pass 2: leftovers whose strategy is UNKNOWN on
    one side pair on ``(underlying, data_session)``, noted. Unmatched items are
    ``(structure, reason)``."""
    pairs: List[Tuple[dict, dict, str]] = []
    used_l, used_b = set(), set()

    def k3(s):
        return (s["underlying"], s["strategy"], s["data_session"])

    bt_by_key: Dict[tuple, List[int]] = {}
    for j, b in enumerate(backtest):
        if b["strategy"] is not None and b["data_session"] is not None:
            bt_by_key.setdefault(k3(b), []).append(j)
    for i, s in enumerate(live):
        if s["strategy"] is None or s["data_session"] is None:
            continue
        for j in bt_by_key.get(k3(s), []):
            if j not in used_b:
                used_l.add(i)
                used_b.add(j)
                pairs.append((s, backtest[j], ""))
                break
    for i, s in enumerate(live):
        if i in used_l or s["data_session"] is None:
            continue
        for j, b in enumerate(backtest):
            if j in used_b or b["underlying"] != s["underlying"] \
                    or b["data_session"] != s["data_session"]:
                continue
            if s["strategy"] is None or b["strategy"] is None:
                used_l.add(i)
                used_b.add(j)
                pairs.append((s, b, f"strategy unknown on the "
                                    f"{'live' if s['strategy'] is None else 'backtest'} side; "
                                    f"paired on (underlying, data_session) only"))
                break
    unmatched_live = [(s, _unmatched_reason(s, backtest, "backtest", "live"))
                      for i, s in enumerate(live) if i not in used_l]
    unmatched_bt = [(b, _unmatched_reason(b, live, "live", "backtest"))
                    for j, b in enumerate(backtest) if j not in used_b]
    return pairs, unmatched_live, unmatched_bt


# ============================================================ diffing

@dataclass
class Tolerances:
    """A numeric diff is within tolerance when ``|delta| <= abs`` OR ``rel_delta <= rel``.
    ``fields`` overrides ``(abs, rel)`` per field name (all phases)."""
    abs_tol: float = 1e-6
    rel_tol: float = 0.0
    fields: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    def for_field(self, name: str) -> Tuple[float, float]:
        return self.fields[name] if name in self.fields else (self.abs_tol, self.rel_tol)


def _row(ctx, phase, leg, fld, lv, bv, tol: Tolerances, *, numeric: bool,
         sources: Tuple[Optional[str], Optional[str]] = (None, None), note: str = ""):
    row = {**ctx, "phase": phase, "leg": leg, "field": fld, "live": lv, "backtest": bv,
           "abs_delta": None, "rel_delta": None, "within_tolerance": None,
           "live_source": sources[0], "backtest_source": sources[1],
           "source_mismatch": (sources[0] != sources[1]) if (sources[0] or sources[1]) else None,
           "note": note}
    if numeric:
        lnum, bnum = _num(lv), _num(bv)
        if lnum is None and bnum is None:
            row["within_tolerance"] = lv is None and bv is None
        elif lnum is None or bnum is None:
            row["within_tolerance"] = False
            row["note"] = (note + " " if note else "") + (
                "missing on live" if lnum is None else "missing on backtest")
        else:
            delta = bnum - lnum
            row["abs_delta"] = delta
            row["rel_delta"] = (abs(delta) / abs(lnum)) if lnum != 0 else (0.0 if delta == 0 else None)
            a, r = tol.for_field(fld)
            row["within_tolerance"] = abs(delta) <= a or (
                row["rel_delta"] is not None and row["rel_delta"] <= r)
    else:
        row["within_tolerance"] = lv == bv
    # A source-tagged row keeps its value verdict; ``source_mismatch`` says the two numbers
    # are different measurements, and the summary reports such rows under their own heading.
    return row


def _match_legs(live_legs, bt_legs):
    """Legs paired by (right, side), then by strike order within that group; the rest alone."""
    def groups(legs):
        g: Dict[tuple, List[dict]] = {}
        for leg in legs:
            g.setdefault((leg["right"], leg["side"]), []).append(leg)
        for v in g.values():
            v.sort(key=lambda x: (x["strike"] is None, x["strike"] or 0.0, x["expiry"] or ""))
        return g
    gl, gb = groups(live_legs), groups(bt_legs)
    out = []
    for key in sorted(set(gl) | set(gb), key=str):
        a, b = gl.get(key, []), gb.get(key, [])
        for i in range(max(len(a), len(b))):
            out.append((a[i] if i < len(a) else None, b[i] if i < len(b) else None))
    return out


def _rec_status_text(status, err):
    return f"error: {err}" if status == "error" else status


def diff_pair(live: dict, bt: dict, tol: Tolerances, ctx: Dict[str, Any]) -> List[dict]:
    rows: List[dict] = []
    add = rows.append
    add(_row(ctx, "record", None, "entry_record",
             _rec_status_text(live["entry_record_status"], live["entry_record_error"]),
             _rec_status_text(bt["entry_record_status"], bt["entry_record_error"]), tol,
             numeric=False))
    add(_row(ctx, "record", None, "exit_record",
             "; ".join([live["exit"]["status"]] + live["exit"]["errors"]),
             "; ".join([bt["exit"]["status"]] + bt["exit"]["errors"]), tol, numeric=False))
    add(_row(ctx, "structure", None, "strategy", live["strategy"], bt["strategy"], tol,
             numeric=False))
    add(_row(ctx, "structure", None, "data_session", live["data_session"], bt["data_session"],
             tol, numeric=False, note=f"key from {live['key_source']} / {bt['key_source']}"))
    ls, bs = live["structure"] or {}, bt["structure"] or {}
    for f in _STRUCT_NUMERIC:
        add(_row(ctx, "structure", None, f, ls.get(f), bs.get(f), tol, numeric=True))
    for f in _STRUCT_EXACT:
        add(_row(ctx, "structure", None, f, ls.get(f), bs.get(f), tol, numeric=False))
    lb, bb = ls.get("breakevens"), bs.get("breakevens")
    be = _row(ctx, "structure", None, "breakevens", lb, bb, tol, numeric=False)
    if isinstance(lb, list) and isinstance(bb, list) and len(lb) == len(bb) and lb:
        deltas = [float(b) - float(a) for a, b in zip(lb, bb)]
        be["abs_delta"] = max(deltas, key=abs)
        a_tol, _ = tol.for_field("breakevens")
        be["within_tolerance"] = all(abs(d) <= a_tol for d in deltas)
    add(be)
    for f in ("entry_net_fill", "entry_net_mid", "entry_net_slippage_vs_mid", "net_gross_pnl"):
        add(_row(ctx, "structure", None, f, live[f], bt[f], tol, numeric=True))
    lx, bx = live["exit"], bt["exit"]
    add(_row(ctx, "exit", None, "exit_trigger", lx["trigger"], bx["trigger"], tol, numeric=False))
    add(_row(ctx, "exit", None, "exit_data_session", lx["data_session"], bx["data_session"], tol,
             numeric=False, note=f"from {lx['data_session_source']} / {bx['data_session_source']}"))
    add(_row(ctx, "exit", None, "close_reason", lx["close_reason"], bx["close_reason"], tol,
             numeric=False))

    for ll, bl in _match_legs(live["legs"], bt["legs"]):
        name = (ll or bl)["contract_symbol"]
        if ll is None or bl is None:
            add(_row(ctx, "entry", name, "leg",
                     ll["contract_symbol"] if ll else None,
                     bl["contract_symbol"] if bl else None, tol, numeric=False,
                     note="leg present on one side only"))
            continue
        les, bes = ll["entry_snapshot"] or {}, bl["entry_snapshot"] or {}
        for f in _LEG_EXACT:
            add(_row(ctx, "entry", name, f, ll.get(f), bl.get(f), tol, numeric=False))
        add(_row(ctx, "entry", name, "strike", ll["strike"], bl["strike"], tol, numeric=True))
        add(_row(ctx, "entry", name, "data_session", les.get("data_session"),
                 bes.get("data_session"), tol, numeric=False))
        _snapshot_rows(add, ctx, "entry", name, les, bes, tol)
        add(_row(ctx, "entry", name, "quantity", ll["quantity"], bl["quantity"], tol, numeric=True))
        add(_row(ctx, "entry", name, "entry_fill", ll["entry_fill"], bl["entry_fill"], tol,
                 numeric=True))
        add(_row(ctx, "entry", name, "entry_slippage_vs_mid",
                 _slippage(ll["side"], ll["entry_fill"], _num(les.get("mid"))),
                 _slippage(bl["side"], bl["entry_fill"], _num(bes.get("mid"))), tol, numeric=True))
        lxs, bxs = ll["exit_snapshot"] or {}, bl["exit_snapshot"] or {}
        add(_row(ctx, "exit", name, "exit_trigger", ll["exit_trigger"], bl["exit_trigger"], tol,
                 numeric=False))
        add(_row(ctx, "exit", name, "exit_data_session", lxs.get("data_session"),
                 bxs.get("data_session"), tol, numeric=False))
        _snapshot_rows(add, ctx, "exit", name, lxs, bxs, tol)
        add(_row(ctx, "exit", name, "exit_fill", ll["exit_fill"], bl["exit_fill"], tol,
                 numeric=True))
        exit_side = {"buy": "sell", "sell": "buy"}.get(ll["side"])
        add(_row(ctx, "exit", name, "exit_slippage_vs_mid",
                 _slippage(exit_side, ll["exit_fill"], _num(lxs.get("mid"))),
                 _slippage({"buy": "sell", "sell": "buy"}.get(bl["side"]), bl["exit_fill"],
                           _num(bxs.get("mid"))), tol, numeric=True))
        add(_row(ctx, "exit", name, "gross_pnl", ll["gross_pnl"], bl["gross_pnl"], tol,
                 numeric=True))
    return rows


def _snapshot_rows(add, ctx, phase, name, ls: dict, bs: dict, tol: Tolerances) -> None:
    src = (ls.get("greeks_source"), bs.get("greeks_source"))
    add(_row(ctx, phase, name, "greeks_source", src[0], src[1], tol, numeric=False))
    for f in _SNAPSHOT_NUMERIC:
        if f in _SOURCE_TAGGED:
            add(_row(ctx, phase, name, f, ls.get(f), bs.get(f), tol, numeric=True, sources=src))
        else:
            add(_row(ctx, phase, name, f, ls.get(f), bs.get(f), tol, numeric=True))


# ============================================================ report

def _in_window(s, start, end, symbols) -> bool:
    if symbols and s["underlying"] not in symbols:
        return False
    ds = s["data_session"]
    if ds is None:
        return True
    if start and ds < start:
        return False
    if end and ds > end:
        return False
    return True


def _brief(s) -> Dict[str, Any]:
    return {"underlying": s["underlying"], "strategy": s["strategy"],
            "data_session": s["data_session"], "key_source": s["key_source"],
            "entry_record_status": s["entry_record_status"], "ref": s["ref"],
            "entry_time": s["entry_time"], "exit_status": s["exit"]["status"],
            "exit_trigger": s["exit"]["trigger"]}


def compare(live: Sequence[dict], backtest: Sequence[dict], tol: Tolerances, *,
            start: Optional[str] = None, end: Optional[str] = None,
            symbols: Optional[Sequence[str]] = None,
            extra_issues: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The full comparison report (JSON-serialisable). ``start``/``end`` are inclusive ISO
    dates on the pairing ``data_session``; ``symbols`` filters underlyings."""
    syms = {s.upper() for s in symbols} if symbols else None
    start = _day(start) if start else None
    end = _day(end) if end else None
    live = [s for s in live if _in_window(s, start, end, syms)]
    backtest = [s for s in backtest if _in_window(s, start, end, syms)]
    pairs, ul, ub = pair_structures(live, backtest)
    report_pairs, diffs = [], []
    for n, (l_s, b_s, note) in enumerate(pairs, 1):
        ctx = {"pair_id": n, "underlying": l_s["underlying"],
               "strategy": l_s["strategy"] or b_s["strategy"],
               "data_session": l_s["data_session"]}
        report_pairs.append({**ctx, "note": note, "live": _brief(l_s), "backtest": _brief(b_s),
                             "backtest_net_pnl_incl_commission":
                                 b_s["backtest_net_pnl_incl_commission"]})
        diffs.extend(diff_pair(l_s, b_s, tol, ctx))
    issues = list(extra_issues or [])
    for s in list(live) + list(backtest):
        issues.extend(s["issues"])

    def rec_errors(structs):
        n = 0
        for s in structs:
            n += s["entry_record_status"] == "error"
            n += len(s["exit"]["errors"]) or ("error" in s["exit"]["status"])
        return n

    out_of_tol = [d for d in diffs if d["within_tolerance"] is False]
    by_field: Dict[Tuple[str, str, bool], Dict[str, Any]] = {}
    for d in out_of_tol:
        k = (d["phase"], d["field"], bool(d["source_mismatch"]))
        agg = by_field.setdefault(k, {"phase": k[0], "field": k[1], "sources_differ": k[2],
                                      "count": 0, "max_abs_delta": None, "max_rel_delta": None,
                                      "example_pair": d["pair_id"]})
        agg["count"] += 1
        if d["abs_delta"] is not None and (agg["max_abs_delta"] is None
                                           or abs(d["abs_delta"]) > abs(agg["max_abs_delta"])):
            agg["max_abs_delta"] = d["abs_delta"]
            agg["example_pair"] = d["pair_id"]
        if d["rel_delta"] is not None and (agg["max_rel_delta"] is None
                                           or d["rel_delta"] > agg["max_rel_delta"]):
            agg["max_rel_delta"] = d["rel_delta"]
    top = sorted(by_field.values(), key=lambda a: (-a["count"], a["phase"], a["field"]))
    summary = {
        "live_structures": len(live), "backtest_structures": len(backtest),
        "paired": len(pairs),
        "match_rate_live": (len(pairs) / len(live)) if live else None,
        "match_rate_backtest": (len(pairs) / len(backtest)) if backtest else None,
        "unmatched_live": len(ul), "unmatched_backtest": len(ub),
        "diff_rows": len(diffs), "out_of_tolerance": len(out_of_tol),
        "record_errors": {"live": rec_errors(live), "backtest": rec_errors(backtest)},
        "records_missing": {"live": sum(s["entry_record_status"] == "missing" for s in live),
                            "backtest": sum(s["entry_record_status"] == "missing"
                                            for s in backtest)},
        "key_from_fill": {"live": sum(s["key_source"] == KEY_FROM_FILL for s in live),
                          "backtest": sum(s["key_source"] == KEY_FROM_FILL for s in backtest)},
        "top_discrepancies": top,
        "tolerances": {"abs": tol.abs_tol, "rel": tol.rel_tol,
                       "fields": {k: list(v) for k, v in tol.fields.items()}},
        "window": {"start": start, "end": end, "symbols": sorted(syms) if syms else None},
    }
    return {"summary": summary, "pairs": report_pairs, "diffs": diffs,
            "unmatched_live": [{**_brief(s), "reason": r} for s, r in ul],
            "unmatched_backtest": [{**_brief(s), "reason": r} for s, r in ub],
            "issues": issues}


def summarize(report: Dict[str, Any], *, limit: int = 20) -> str:
    """The human summary printed by the CLI."""
    s = report["summary"]

    def pct(x):
        return "n/a" if x is None else f"{x * 100:.1f}%"

    lines = [
        "Option trade records: live vs backtest",
        f"  window: {s['window']['start'] or '-'} .. {s['window']['end'] or '-'}"
        f"  symbols: {', '.join(s['window']['symbols']) if s['window']['symbols'] else 'all'}",
        f"  structures: live {s['live_structures']}, backtest {s['backtest_structures']}, "
        f"paired {s['paired']}  (match rate: live {pct(s['match_rate_live'])}, "
        f"backtest {pct(s['match_rate_backtest'])})",
        f"  records: errors live {s['record_errors']['live']} / backtest "
        f"{s['record_errors']['backtest']}; missing live {s['records_missing']['live']} / "
        f"backtest {s['records_missing']['backtest']}; key derived from fill live "
        f"{s['key_from_fill']['live']} / backtest {s['key_from_fill']['backtest']}",
        f"  diff rows {s['diff_rows']}, out of tolerance {s['out_of_tolerance']} "
        f"(abs {s['tolerances']['abs']:g}, rel {s['tolerances']['rel']:g})",
    ]
    same = [t for t in s["top_discrepancies"] if not t["sources_differ"]]
    diff_src = [t for t in s["top_discrepancies"] if t["sources_differ"]]

    def fmt(t):
        a = "" if t["max_abs_delta"] is None else f" max|d| {abs(t['max_abs_delta']):.6g}"
        r = "" if t["max_rel_delta"] is None else f" max rel {t['max_rel_delta'] * 100:.2f}%"
        return f"    {t['phase']:9s} {t['field']:26s} x{t['count']}{a}{r} (e.g. pair {t['example_pair']})"

    if same:
        lines.append("  top discrepancies by field:")
        lines += [fmt(t) for t in same[:limit]]
    if diff_src:
        lines.append("  greeks/IV where the sources differ (shown, not a like-for-like diff):")
        lines += [fmt(t) for t in diff_src[:limit]]
    for label, key in (("unmatched live", "unmatched_live"),
                       ("unmatched backtest", "unmatched_backtest")):
        items = report[key]
        if items:
            lines.append(f"  {label} ({len(items)}):")
            for u in items[:limit]:
                lines.append(f"    {u['underlying']} {u['strategy']} {u['data_session']} "
                             f"{u['ref']}: {u['reason']}")
    if report["issues"]:
        lines.append(f"  issues ({len(report['issues'])}):")
        lines += [f"    {i}" for i in report["issues"][:limit]]
    return "\n".join(lines)
