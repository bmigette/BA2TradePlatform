"""Recording what a non-smart risk manager DECIDED, per symbol, and why.

The classic manager's only durable output was a count. ``review_and_prioritize_pending_orders``
logged "reviewed 42 pending orders, updated 7, deleted 35" and nothing else, so the question
actually asked of it -- *why was AAPL one of the 35?* -- was answerable only from the process
log, and only until that rotated. This module turns the drop points the manager already has
into a record.

WHAT AN OUTCOME MEANS
---------------------
Every symbol the manager RECEIVED gets exactly one entry, including the ones it never
sized. A symbol that is missing from ``decisions`` is a bug in the caller, not a symbol
that was ignored -- which is why ``build_decisions`` takes the received set explicitly
rather than deriving it from the survivors.

``FUNDED`` carries the quantity. Every other outcome carries a ``reason`` naming the gate
that stopped it, because "not funded" on its own is the same non-answer the counts were.

NEVER DURING A BACKTEST
-----------------------
``TradeRiskManagement`` is shared between the live app and the GA's daily engine
(``daily_engine.py`` calls the same entry point), which runs it thousands of times per
trial across thousands of trials. Writing a row there would add a DB insert to the
innermost loop of the grid and fill the scratch database with records nobody reads.
``record_run`` refuses when ``inmem_trades_active()`` -- the same seam
``_delete_unfunded_orders`` already uses to tell the two worlds apart.

A FAILED RECORD MUST NOT FAIL THE RUN
-------------------------------------
Recording is observability. The manager's job is to size orders, and it has already done
that by the time this is called; an exception escaping here would turn a successful
sizing pass into a failed one and, worse, leave the orders it already persisted behind
without their explanation. Every entry point swallows and logs, exactly as the existing
``log_activity`` call beside it does.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ba2_common.logger import logger

#: The manager that produced a run. ``smart`` is deliberately absent: it has its own
#: table (``SmartRiskManagerJob``) with a graph state this shape could not hold.
MODE_CLASSIC = "classic"
MODE_OPTIONS = "options"

#: The symbol was sized and funded. The ONLY outcome that carries a quantity.
OUTCOME_FUNDED = "FUNDED"
#: Dropped by the expert's buy/sell permissions before any sizing happened.
OUTCOME_PERMISSION = "REFUSED_PERMISSION"
#: Survived permissions but carried no usable recommendation, so it could not be ranked.
OUTCOME_NO_RECOMMENDATION = "REFUSED_NO_RECOMMENDATION"
#: Sized, and the money ran out -- the budget, or the per-instrument cap.
OUTCOME_UNFUNDED = "REFUSED_UNFUNDED"
#: The manager raised on this symbol. Distinct from a refusal: nothing DECIDED anything.
OUTCOME_ERROR = "ERROR"

#: Outcomes that mean "this symbol will not trade". Everything except FUNDED, spelled out
#: rather than derived, so a new outcome has to state which side of the line it is on.
REFUSED_OUTCOMES = (OUTCOME_PERMISSION, OUTCOME_NO_RECOMMENDATION,
                    OUTCOME_UNFUNDED, OUTCOME_ERROR)


def decision(symbol: str, outcome: str, reason: str, *,
             quantity: Optional[float] = None, **extra) -> Dict[str, Any]:
    """One symbol's line in a run.

    ``reason`` is REQUIRED even for ``FUNDED`` -- "sized at 12 shares against a $4,000
    per-instrument cap" is the explanation a reader wants next to the ones that failed,
    and making it optional is how it would end up empty on the interesting half.

    ``quantity`` stays ``None`` for a refusal rather than becoming 0: a refused symbol has
    no quantity, and 0 would read as "sized, at nothing", which is a different (and real)
    outcome the classic manager can also produce.
    """
    if not symbol:
        raise ValueError("a decision must name its symbol")
    if not reason:
        raise ValueError(f"decision for {symbol} ({outcome}) carries no reason; "
                         f"'not funded' without a cause is the non-answer this record exists "
                         f"to replace")
    row: Dict[str, Any] = {"symbol": symbol, "outcome": outcome, "reason": reason}
    if quantity is not None:
        row["quantity"] = float(quantity)
    row.update(extra)
    return row


def build_decisions(received: Sequence[str],
                    *,
                    funded: Mapping[str, float],
                    reasons: Mapping[str, tuple],
                    default_reason: str = "not selected by the risk manager") -> List[Dict[str, Any]]:
    """One entry per RECEIVED symbol, in the order received.

    ``received`` is the authority on the set. A symbol in ``funded`` or ``reasons`` that
    was never received is a caller bug and RAISES -- silently appending it would produce a
    record claiming the manager saw something it did not.

    A received symbol with no recorded outcome falls to ``default_reason`` rather than
    being dropped. That case is itself a signal (a drop point nobody instrumented), so it
    is visible in the UI as an unexplained refusal instead of a missing row.
    """
    order = list(received)
    known = set(order)
    for sym in list(funded) + list(reasons):
        if sym not in known:
            raise ValueError(
                f"{sym} has an outcome but was never in the received set; the received set "
                f"is what makes 'every symbol is accounted for' true")
    out: List[Dict[str, Any]] = []
    for sym in order:
        if sym in funded:
            qty = funded[sym]
            out.append(decision(sym, OUTCOME_FUNDED,
                                reasons.get(sym, (None, f"funded at {qty:g}"))[1]
                                if sym in reasons else f"funded at {qty:g}",
                                quantity=qty))
            continue
        if sym in reasons:
            outcome, reason = reasons[sym]
            out.append(decision(sym, outcome, reason))
            continue
        out.append(decision(sym, OUTCOME_UNFUNDED, default_reason))
    return out


def record_run(*, expert_instance_id: int, account_id: Optional[int], mode: str,
               decisions: Sequence[Mapping[str, Any]],
               context: Optional[Mapping[str, Any]] = None,
               started_at: Optional[float] = None,
               status: str = "COMPLETED",
               error_message: Optional[str] = None) -> Optional[int]:
    """Persist one run. Returns its id, or ``None`` when nothing was written.

    ``None`` is returned -- never raised -- for both of the "did not write" cases: a
    backtest (by design) and a failure to persist (logged). The caller is a risk manager
    that has already finished its real work; see the module docstring.

    ``started_at`` is a ``time.monotonic()`` reading from the top of the run. Monotonic,
    not wall clock, because the duration must not jump when the system clock is adjusted
    mid-run -- the same reason the rest of the codebase times with it.
    """
    try:
        from ba2_common.core.trade_store import inmem_trades_active
        if inmem_trades_active():
            return None
    except Exception as e:  # noqa: BLE001 -- an unavailable seam must not decide "live"
        logger.debug(f"risk-manager run recording skipped; backtest seam unavailable: {e}")
        return None

    try:
        from ba2_common.core.db import add_instance
        from ba2_common.core.models import RiskManagerRun

        rows = [dict(d) for d in decisions]
        run = RiskManagerRun(
            expert_instance_id=expert_instance_id,
            account_id=account_id,
            mode=mode,
            status=status,
            error_message=error_message,
            duration_seconds=(0.0 if started_at is None
                              else round(time.monotonic() - started_at, 3)),
            symbols_received=len(rows),
            symbols_funded=sum(1 for r in rows if r.get("outcome") == OUTCOME_FUNDED),
            decisions=rows,
            context=dict(context or {}),
        )
        return add_instance(run)
    except Exception as e:  # noqa: BLE001 -- observability must never fail the sizing pass
        logger.warning(f"Failed to record {mode} risk-manager run for expert "
                       f"{expert_instance_id}: {e}")
        return None
