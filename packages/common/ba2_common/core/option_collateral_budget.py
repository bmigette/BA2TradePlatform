"""The equity RM's budget, net of the collateral this expert's option positions reserve (E1b).

WHY (plan 2026-09-25 option-stage2-build, "Added after the E1 review", E1b). A style run can
hold a cash-secured-put member AND a stock-buying member (O_CC / O_PP) in one expert. The
expert's used balance (``MarketExpertInterface._calculate_used_balance``) charges an open short
put only its PREMIUM (open price x quantity x 100, plus any loss), while the broker sets aside
the full assignment cost (strike x 100 per contract,
``OptionsAccountInterface.option_reserve_required``). The classic RM sized stock off that
budget, so it could spend the cash the put is secured by -- a trade live Alpaca refuses.

THE RULE. Each open option POSITION of the expert is charged the GREATER of its reserve (the
same ``option_reserve`` the option path's own buying-power gate reads, via
``reserved_option_buying_power_detail``) and what the used balance already charged it::

    extra     = sum over the expert's option positions of max(0, reserve - used charge)
    available = min(balances.available, virtual - used - extra)

WHY ``min`` AND NOT ``balances.available - extra`` -- the live double-subtraction question.
``balances.available`` is already ``min(virtual - used, broker buying power, exposure
headroom)``. On live Alpaca the broker term is the snapshot's ``buying_power``, which is the
Reg-T REMAINING buying power (``regt_buying_power``, APP 1160): Alpaca has ALREADY reduced it by
the collateral it holds against every open cash-secured put. Subtracting ``extra`` from the
clamped figure would therefore charge the collateral twice whenever the broker term is the
binding one. Charging it only on the expert's OWN term (``virtual - used``) and taking the
``min`` again charges each position's collateral exactly once in both runtimes:

* live: ``min(virtual - used - extra, RegT_bp (already net), headroom)``;
* backtest: ``min(virtual - used - extra, cash, headroom)`` -- the backtest's buying power is
  plain cash (``BacktestAccount.get_account_info``), which does not net the collateral, but
  it is never the binding term here: a backtest account carries ONE expert, so cash
  (= equity + the premium collected) is above ``virtual - used - extra`` whenever a reserve
  is held.

BYTE-IDENTICAL WITHOUT A RESERVE. No open option position (every pure-equity run) -> the SAME
``balances`` object is returned and nothing is read. Option positions that reserve nothing
(debit structures, covered calls, protective puts) -> ``extra`` is 0 -> the same object again.
Pure-option runs never reach the equity RM at all: their entries self-size.

LOUD. An account that cannot report its reserves while the expert holds option positions, or a
reserve pool with an unreadable row, raises ``ValueError``: an unknown collateral is not zero.
"""
from typing import Any

from ba2_common.core.interfaces.MarketExpertInterface import ExpertBalance


def rm_equity_budget(balances: ExpertBalance, account: Any) -> ExpertBalance:
    """``balances`` with ``available`` (and ``used``) charged the option collateral -- see the
    module docstring. Returns ``balances`` itself when no collateral is reserved."""
    if not balances.option_used:
        return balances
    detail = getattr(account, "reserved_option_buying_power_detail", None)
    if detail is None:
        raise ValueError(
            f"account {getattr(account, 'id', '?')} ({type(account).__name__}) holds option "
            f"positions for this expert but cannot report their collateral reserve; the equity "
            f"budget cannot be stated without it")
    ids = [tid for tid, _used in balances.option_used]
    pool = detail(transaction_ids=ids)
    if not pool.is_measurable:
        raise ValueError(
            f"account {getattr(account, 'id', '?')}: the collateral reserved by this expert's "
            f"option positions is UNKNOWN ({'; '.join(pool.unmeasurable)}), so the equity "
            f"budget cannot be stated -- an unknown reserve is not zero")
    reserve_of = dict(pool.by_transaction)
    extra = 0.0
    for tid, used_charge in balances.option_used:
        reserve = reserve_of[tid] if tid in reserve_of else 0.0
        if reserve > used_charge:
            extra += reserve - used_charge
    if extra <= 0.0:
        return balances
    net = balances.virtual - balances.used - extra
    return ExpertBalance(virtual=balances.virtual, used=balances.used + extra,
                         available=min(balances.available, net),
                         option_used=balances.option_used)
