"""Mixin for option-account TEST DOUBLES: they publish the account snapshot the capital headroom
reads (``OptionsAccountInterface.option_capital_equity``). A double whose balance IS its equity
(no positions, no other source of capital) says so by inheriting this."""
from __future__ import annotations


class EquityFromBalance:
    """``get_account_snapshot()`` whose equity (and cash) is the double's ``get_balance()``."""

    def get_account_snapshot(self):
        from ba2_common.core.account_types import AccountSnapshot
        equity = self.get_balance()
        return AccountSnapshot(cash=equity, equity=equity, net_liquidation=equity)
