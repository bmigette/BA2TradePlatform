"""Read-only margin review probes using real methods and mocked broker/store ports.

All application data is temporary; no network, broker submission or production DB
is used. Assertions record the reviewed behavior, including identified defects.
Run with the project Python environment from the repository root.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import importlib
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import MethodType, SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="margin-review-") as temporary, ExitStack() as stack:
        os.environ.update(BA2_HOME=temporary, DB_FILE=str(Path(temporary) / "unused.sqlite"),
                          CACHE_FOLDER=str(Path(temporary) / "cache"),
                          LOG_FOLDER=str(Path(temporary) / "logs"),
                          BA2_FILE_LOGGING="0", BA2_STDOUT_LOGGING="0")
        for path in (ROOT, ROOT / "packages/common", ROOT / "packages/providers", ROOT / "packages/experts",
                     ROOT / "testplatform/backend"):
            sys.path.insert(0, str(path))
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("No network in audit")))

        from ba2_common.core.account_types import AccountSnapshot
        from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface as Account
        from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface as Expert
        from ba2_common.core.TradeRiskManagement import TradeRiskManagement
        from ba2_common.core.TradeActions import AdjustTakeProfitAction, AdjustStopLossAction
        from ba2_common.core.types import OrderDirection, OrderRecommendation, ReferenceValue
        from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit

        settings = dict(margin_enabled=True, margin_factor=1.8, commission_per_trade=0.0,
                        risk_per_trade_pct=1.0, atr_risk_budget_pct=1.0, atr_multiplier=2.0,
                        atr_period=14, min_stop_loss_pct=0.0, use_atr_stop=False,
                        max_virtual_equity_per_instrument_percent=100.0,
                        diversification_factor=1.0, sizing_mode="notional")
        state = dict(equity=10000.0, buying_power=20000.0, price=100.0, transactions=[])
        account = NS(id=1, get_balance=lambda: state["equity"],
                     get_setting_with_interface_default=lambda key, **kw: settings[key],
                     get_account_info=lambda: {"buying_power": state["buying_power"]},
                     get_account_snapshot=lambda: AccountSnapshot(
                         equity=state["equity"], margin_multiplier=2.0, buying_power=state["buying_power"]),
                     get_instrument_current_price=lambda symbols, **kw:
                         {s: state["price"] for s in symbols} if isinstance(symbols, list) else state["price"])
        # Bind production methods rather than duplicate their calculations.
        for name in ("get_tradable_balance", "get_option_tradable_balance", "_margin_enabled", "_margin_factor",
                     "_plain_balance", "_stock_multiplier_from", "_buying_power_from", "_tradable_balance",
                     "_effective_factor", "get_option_margin_multiplier"):
            setattr(account, name, MethodType(getattr(Account, name), account))
        instance = NS(id=1, account_id=1, virtual_equity_pct=100.0)
        expert = NS(id=1, settings=settings,
                    get_setting_with_interface_default=lambda key, **kw: settings[key],
                    _get_enabled_instruments_config=lambda: {})
        for name in ("get_virtual_balance", "get_available_balance", "_calculate_used_balance"):
            setattr(expert, name, MethodType(getattr(Expert, name), expert))
        expert._get_actual_available_balance = Expert._get_actual_available_balance
        expert_module = importlib.import_module("ba2_common.core.interfaces.MarketExpertInterface")
        stack.enter_context(patch.object(expert_module, "get_instance", return_value=instance))
        resolver = NS(get_account_instance=lambda account_id: account,
                      get_expert_instance=lambda expert_id: expert)
        stack.enter_context(patch("ba2_common.core.instance_resolver.get_instance_resolver", return_value=resolver))
        stack.enter_context(patch("ba2_common.core.trade_store.transactions_where",
                                 side_effect=lambda **kw: list(state["transactions"])))
        stack.enter_context(patch("ba2_common.config.get_min_tp_sl_percent", return_value=1.0))
        stack.enter_context(patch("ba2_common.core.regime_overlay.get_stressed", return_value=False))
        rm = TradeRiskManagement()
        tk = object.__new__(SmartRiskManagerToolkit)
        tk.expert, tk.logger = expert, logging.getLogger("margin-audit")
        tk.get_current_price = lambda symbol: state["price"]

        def order(side=OrderDirection.BUY):
            return NS(id=1, symbol="NEW", side=side, quantity=0, stop_price=95.0,
                      limit_price=None, open_price=100.0, data={}, expert_recommendation_id=None)

        def holding(quantity, entry, symbol="OLD"):
            return NS(id=2, symbol=symbol, open_price=entry, quantity=quantity,
                      multiplier=1, side=OrderDirection.BUY)

        observations = {}
        brackets = []
        for enabled in (False, True):
            settings["margin_enabled"] = enabled
            for side, rec in ((OrderDirection.BUY, OrderRecommendation.BUY),
                              (OrderDirection.SELL, OrderRecommendation.SELL)):
                entry = order(side)
                reference = ReferenceValue.ORDER_OPEN_PRICE.value
                tp = AdjustTakeProfitAction("NEW", account, rec, entry, reference_value=reference, percent=10.0)
                sl = AdjustStopLossAction("NEW", account, rec, entry, reference_value=reference, percent=-5.0)
                tp_price, sl_price = tp.compute_price(entry), sl.compute_price(entry)
                entry.stop_price = sl_price
                quantity = rm._risk_atr_quantity(entry, "NEW", 100.0, expert, 18000.0, 18000.0, account)
                expected = (110.0, 95.0) if side == OrderDirection.BUY else (90.0, 105.0)
                assert abs(tp_price - expected[0]) < 1e-8 and abs(sl_price - expected[1]) < 1e-8
                assert quantity == (36 if enabled else 20)
                brackets.append(dict(margin=enabled, side=side.value, virtual=expert.get_virtual_balance(),
                                     quantity=quantity, tp=round(tp_price, 2), sl=sl_price,
                                     dollar_loss_at_stop=quantity * abs(100.0 - sl_price)))
        observations["tp_sl_and_classic_risk_sizing"] = brackets

        settings.update(margin_enabled=True, atr_risk_budget_pct=0.5, risk_per_trade_pct=5.0)
        classic_qty = rm._risk_atr_quantity(order(), "NEW", 100.0, expert, 18000.0, 18000.0, account)
        smart = tk._auto_size_by_risk("NEW", OrderDirection.BUY, sl_price=95.0)
        assert (classic_qty, smart["quantity"]) == (18, 180)
        observations["smart_classic_budget_divergence"] = dict(
            virtual=expert.get_virtual_balance(), risk_budget_pct=0.5, stop_gene_pct=5.0,
            classic_quantity=classic_qty, smart_quantity=smart["quantity"],
            classic_loss_at_stop=classic_qty * 5.0, smart_loss_at_stop=smart["quantity"] * 5.0)

        # Half of the expert's 18k allocation is occupied; a 10% instrument cap
        # should still be 1.8k, separately constrained by 9k remaining capacity.
        settings.update(atr_risk_budget_pct=1.0, risk_per_trade_pct=1.0)
        state["transactions"] = [holding(90, 100.0)]
        state["buying_power"] = 11000.0
        candidate = order()
        recommendation = NS(expected_profit_percent=10.0, confidence=80.0)
        result = rm._size_prioritized_orders(expert, instance, 1, [(candidate, recommendation)], 0.10)
        assert result[-2:] == (9000.0, 900.0) and candidate.quantity == 9
        observations["classic_instrument_cap_uses_remaining_funds"] = dict(
            virtual=expert.get_virtual_balance(), available=expert.get_available_balance(),
            configured_cap_pct=10.0, actual_cap=result[-1], virtual_based_cap=1800.0,
            actual_new_shares=candidate.quantity)

        # All existing positions belong to this expert: no oversubscription or
        # manual trades are needed for the profitable-position accounting gap.
        state.update(equity=11800.0, buying_power=3800.0, price=110.0,
                     transactions=[holding(180, 100.0)])
        available = expert.get_available_balance()
        virtual = expert.get_virtual_balance()
        marked_exposure = 180 * 110.0
        assert (virtual, available) == (21240.0, 3240.0)
        observations["profitable_position_overstates_headroom"] = dict(
            equity=11800.0, virtual=virtual, existing_marked_exposure=marked_exposure,
            reported_available=available, ceiling_headroom=virtual - marked_exposure,
            overstatement=available - (virtual - marked_exposure))

        # The account is already at its 1.8x ceiling because of other positions.
        # Its broker allows 2x; this empty expert still sees that spare broker BP.
        state.update(equity=10000.0, buying_power=2000.0, price=100.0, transactions=[])
        available = expert.get_available_balance()
        assert available == 2000.0
        observations["account_ceiling_is_not_an_entry_gate"] = dict(
            equity=10000.0, account_ceiling=18000.0, other_positions_notional=18000.0,
            broker_remaining_bp=2000.0, expert_reported_available=available,
            ceiling_headroom=0.0)

        # NaN from a broker passes the newly introduced multiplier reader and
        # ends up selecting the full configured factor rather than refusing.
        bad = AccountSnapshot(margin_multiplier=float("nan"), buying_power=20000.0)
        multiplier = account._stock_multiplier_from(bad)
        amount = account._tradable_balance(asset="stock", balance=10000.0,
                                           multiplier=multiplier, remaining_bp=20000.0)
        assert amount == 18000.0
        observations["nonfinite_broker_multiplier_not_rejected"] = dict(
            supplied_multiplier="NaN", returned_tradable_balance=amount, should_refuse=True)

        # Follow-up: the real backtest cash accessor feeds the shared expert's
        # equity calculation. Reproduce its effect after a 1k purchase at no P&L.
        from app.services.backtest.backtest_account import BacktestAccount
        settings["margin_enabled"] = False
        state.update(equity=4000.0, buying_power=3000.0, price=100.0,
                     transactions=[holding(10, 100.0)])
        live_virtual, live_available = expert.get_virtual_balance(), expert.get_available_balance()
        ledger_fields = NS(_cash=3000.0, _equity_cap=None)
        with patch.object(account, "get_balance", side_effect=lambda: BacktestAccount.get_balance(ledger_fields)):
            backtest_virtual = expert.get_virtual_balance()
            backtest_available = expert.get_available_balance()
        assert (live_virtual, live_available) == (4000.0, 3000.0)
        assert (backtest_virtual, backtest_available) == (3000.0, 2000.0)
        observations["backtest_cash_equity_contract_mismatch"] = dict(
            real_equity=4000.0, cash=3000.0, existing_position=1000.0,
            live_virtual=live_virtual, live_available=live_available,
            backtest_virtual=backtest_virtual, backtest_available=backtest_available)

        # Accepted current-state equivalence: the comparison account changes
        # with equity; it is not a promise of identical compounded trajectories.
        state["transactions"] = []
        equivalents = []
        for leveraged_equity in (2000.0, 1900.0, 2100.0):
            expected_capital = leveraged_equity * 2.0
            settings.update(margin_enabled=True, margin_factor=2.0)
            state.update(equity=leveraged_equity, buying_power=expected_capital)
            levered_virtual = expert.get_virtual_balance()
            settings["margin_enabled"] = False
            state.update(equity=expected_capital, buying_power=expected_capital)
            unlevered_virtual = expert.get_virtual_balance()
            assert levered_virtual == unlevered_virtual == expected_capital
            equivalents.append(dict(leveraged_equity=leveraged_equity, leverage=2.0,
                                    matching_unlevered_equity=expected_capital,
                                    shared_virtual_balance=levered_virtual))
        observations["accepted_current_equity_equivalence"] = equivalents

        report = dict(reviewed_head=subprocess.check_output(
            ["git", "-c", "safe.directory=" + ROOT.as_posix(), "rev-parse", "HEAD"],
            cwd=ROOT, text=True).strip(),
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            network_calls=0, production_writes=0, observations=observations)
        destination = Path(__file__).with_name("reproductions_2026-09-09.json")
        destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
