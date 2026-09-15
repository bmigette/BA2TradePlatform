"""Read-only code audit reproductions; every order and DB write is synthetic.

Run with a Python 3.12 environment containing this repository's dependencies.
--site-packages can point the bundled Python at the existing trade environment.
Production databases, account resolvers and broker adapters are never used.
Assertions document the observed bugs, not desired regression-test behavior.
"""

# ---------------------------------------------------------------------------
# STATUS 2026-09-07, AFTER THE FIXES: THIS SCRIPT IS EXPECTED TO FAIL.
#
# Every assertion here asserts the DEFECTIVE behaviour on purpose -- that is what
# made it evidence. All five findings (PA-01..PA-05) and the advisory-budget
# design risk have since been fixed, so the assertions no longer hold, and the
# first one to be reached raises. That failure is the proof, not a regression.
#
# It is kept unchanged as the audit record. The guards that keep the defects
# fixed assert the OPPOSITE and live with the code:
#
#   packages/common/tests/test_allocator_audit_fixes_engine.py   (PA-02, PA-05)
#   tests/test_portfolio_allocation_submit.py                    (PA-01, PA-04,
#       classes TestAStalePlanCannotBeSubmittedTwice,             the budget block)
#       TestTheAccountMustStillBeEligible, TestAKnownUnfundablePlanIsRefused
#
# To re-read what each scenario demonstrated, read the assertions; to check the
# behaviour today, run the two suites above.
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-packages")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    task_temp = Path(tempfile.mkdtemp(prefix="ba2_allocator_audit_"))
    os.environ.update(
        BA2_HOME=str(task_temp), DB_FILE=str(task_temp / "unused.sqlite"),
        LOG_FOLDER=str(task_temp / "logs"),
        BA2_FILE_LOGGING="0", BA2_STDOUT_LOGGING="0",
    )
    sys.path[:0] = [str(root), *(str(root / "packages" / p)
                                for p in ("common", "providers", "experts"))]
    if args.site_packages:
        sys.path.append(args.site_packages)

    from datetime import date, datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import patch
    from sqlmodel import SQLModel, create_engine
    from sqlalchemy.pool import StaticPool
    from ba2_common.core import db, portfolio_allocation as pa
    from ba2_common.core import portfolio_allocation_store as store
    from ba2_common.core.models import AccountDefinition, ExpertInstance, TradingOrder
    from ba2_common.core.account_types import MarginInfo, OrderImpact
    from ba2_common.core.types import OrderDirection, OrderStatus
    from ba2_trade_platform.core import portfolio_allocation_service as svc
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import evaluate_gate
    svc.logger.disabled = True
    pa.logger.disabled = True

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    db._engine = engine

    def reset():
        SQLModel.metadata.drop_all(engine)
        SQLModel.metadata.create_all(engine)
        db.add_instance(AccountDefinition(id=1, name="AUDIT FAKE", provider="FAKE"))

    class FakeAccount:
        id = 1

        def __init__(self):
            self.sent = []
            self.held = {}
            self.snapshot_reads = 0
            self.position_reads = 0
            self.manual = True
            self.setting_reads = 0

        def get_setting_with_interface_default(self, key, log_warning=False):
            assert key == "manual_trading_enabled"
            self.setting_reads += 1
            return self.manual

        def get_market_hours(self):
            return SimpleNamespace(is_known=True, is_open=True)

        def refresh_orders(self, fetch_all=False):
            return None

        def get_positions(self):
            self.position_reads += 1
            return [SimpleNamespace(symbol=s, qty=q, cost_basis=q * 100,
                                    market_value=q * 100, side=OrderDirection.BUY)
                    for s, q in self.held.items()]

        def get_account_snapshot(self):
            self.snapshot_reads += 1
            return pa.AccountSnapshot(buying_power=0, cash=0, margin_multiplier=1)

        def submit_order(self, order, is_closing_order=False):
            self.sent.append({"symbol": order.symbol, "quantity": order.quantity,
                              "side": order.side.value})
            self.held[order.symbol] = self.held.get(order.symbol, 0) + order.quantity
            fresh = db.get_instance(TradingOrder, order.id)
            fresh.status = OrderStatus.FILLED
            fresh.filled_qty = order.quantity
            fresh.open_price = 100.0
            db.update_instance(fresh)
            return fresh

    def base(bp=1000, held=0):
        return pa.BaseSnapshot(available_buying_power=bp, managed_value=held,
                               base_notional=bp + held, default_bp_factor=1,
                               valuation_mode=pa.VALUATION_MODE_MARKET, cash=bp)

    def buy_plan():
        return pa.compute_allocation(
            1000, 1000, [pa.LabelTarget("L", 100, [pa.SymbolTarget("BBB", 100)])],
            {"BBB": pa.PositionState("BBB", price=100)}, {},
            allow_fractional=False, default_bp_factor=1,
            valuation_mode=pa.VALUATION_MODE_MARKET)

    source_files = [
        "packages/common/ba2_common/core/portfolio_allocation.py",
        "packages/common/ba2_common/core/portfolio_allocation_store.py",
        "ba2_trade_platform/core/portfolio_allocation_service.py",
        "ba2_trade_platform/ui/pages/portfolio_allocation.py",
        "ba2_trade_platform/ui/pages/portfolio_allocation_wizard.py",
    ]
    evidence = {"metadata": {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": "synthetic in-memory SQLite only",
        "broker": "FakeAccount only; deliberately accepts submitted orders",
        "sources_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                           for p in source_files},
    }}
    reset()
    labels = [pa.LabelTarget("L", 100, [pa.SymbolTarget("AAA", 50),
                                       pa.SymbolTarget("BBB", 50)])]
    margin = {s: MarginInfo(symbol=s, bp_factor=1, fractionable=False)
              for s in ("AAA", "BBB")}
    plan = pa.compute_allocation(
        200, 200, labels, {s: pa.PositionState(s, price=100) for s in margin}, margin,
        allow_fractional=False, default_bp_factor=1,
        valuation_mode=pa.VALUATION_MODE_MARKET)
    checked = pa.apply_order_impacts(
        plan, {s: OrderImpact(s, change_in_buying_power=-200) for s in margin},
        available_buying_power=200, margin=margin)
    true_cost = sum(r.delta_quantity * 200 for r in checked.buy_rows)
    assert checked.required_buying_power == 200 and true_cost == 400
    assert pa.validate_plan_budget(checked) is None
    evidence["P1_precheck_rounding_reclaim"] = {
        "available_bp": 200, "reported_bp": checked.required_buying_power,
        "cost_at_prechecked_rate": true_cost,
        "orders": [{"symbol": r.symbol, "qty": r.delta_quantity,
                    "reported_bp": r.bp_cost, "reasons": r.reasons}
                   for r in checked.buy_rows],
    }

    reset()
    labels = [pa.LabelTarget("L", 100, [pa.SymbolTarget("AAA", 0),
                                       pa.SymbolTarget("BBB", 100)])]
    current = {"AAA": pa.PositionState("AAA", quantity=10, cost_basis=1000, price=100),
               "BBB": pa.PositionState("BBB", price=100)}
    plan = pa.compute_allocation(1000, 0, labels, current, {}, allow_fractional=False,
                                 default_bp_factor=1, valuation_mode=pa.VALUATION_MODE_MARKET)
    filtered = pa.filter_plan_rows(plan, ["BBB"])
    warning = pa.validate_plan_budget(filtered)
    assert warning is not None
    account = FakeAccount()
    with patch.object(svc, "log_activity"):
        result = svc.run_allocation(account, filtered, current, base(0, 1000),
                                    mode=pa.ALLOCATION_MODE_REBALANCE)
    assert not result["blocked"] and account.sent[0]["quantity"] == 10
    # This is explicitly advisory in the current implementation. Keep it
    # separate from bugs that contradict the advertised safety behavior.
    evidence["design_risk_removed_funding_sell"] = {
        "available_bp": 0, "required_bp": filtered.required_buying_power,
        "budget_validator": warning, "blocked": result["blocked"],
        "orders_sent_to_fake_broker": account.sent,
    }

    reset()
    account = FakeAccount()
    store.upsert_income_event(1, "audit-deposit", date(2026, 9, 1), "DEPOSIT", 1000)
    normal_remember = svc.remember_fractional_choice
    raced_runs = []

    def interleave_reconcile(account_id, allow_fractional):
        normal_remember(account_id, allow_fractional)
        # A second page's income refresh runs during this legal gap: the run
        # exists, but submit_plan has not created its first order yet.
        raced_runs.extend(svc.reconcile_unconsumed_runs(account))

    with patch.object(svc, "log_activity"), patch.object(
            svc, "remember_fractional_choice", interleave_reconcile):
        result = svc.run_allocation(account, buy_plan(), {}, base(),
                                    mode=pa.ALLOCATION_MODE_REBALANCE)
    assert result["run_id"] in raced_runs
    assert result["filled_buy_value"] == 1000
    assert result["income_consumed"] == 0
    assert store.get_open_income_total(1) == 1000
    assert store.get_unconsumed_runs(1) == []
    evidence["P1_reconcile_during_submission"] = {
        "run_finalized_before_first_order": raced_runs,
        "filled_buy_value": result["filled_buy_value"],
        "income_consumed": result["income_consumed"],
        "income_still_shown_open": store.get_open_income_total(1),
        "eligible_for_later_reconciliation": len(store.get_unconsumed_runs(1)),
    }

    reset()
    account = FakeAccount()
    # Leave enough real cash for BOTH buys: broker buying-power checks would
    # accept the duplicate, which spends the reserve instead of rejecting it.
    plan = pa.compute_allocation(
        2000, 2000, [pa.LabelTarget("L", 100, [pa.SymbolTarget("BBB", 100)])],
        {"BBB": pa.PositionState("BBB", price=100)}, {},
        allow_fractional=False, default_bp_factor=1,
        valuation_mode=pa.VALUATION_MODE_MARKET, unallocated_pct=50)
    results = []
    with patch.object(svc, "log_activity"):
        for _ in range(2):
            results.append(svc.run_allocation(account, plan, {}, base(2000),
                                              mode=pa.ALLOCATION_MODE_REBALANCE))
    assert account.held["BBB"] == 20
    assert account.position_reads == account.snapshot_reads == 0
    evidence["P1_stale_rebalance_two_dialogs"] = {
        "reviewed_target_qty": 10, "final_fake_position_qty": account.held["BBB"],
        "initial_cash": 2000, "reviewed_cash_reserve": plan.reserved_notional,
        "cash_after_both_fills": 2000 - sum(r["filled_buy_value"] for r in results),
        "new_position_reads_at_submit": account.position_reads,
        "new_snapshot_reads_at_submit": account.snapshot_reads,
        "run_ids": [r["run_id"] for r in results],
        "orders_sent_to_fake_broker": account.sent,
    }

    reset()
    account = FakeAccount()
    plan = buy_plan()
    # A settings change after opening the dry run: its account is no longer
    # manual, and an expert was enabled in another page.
    account.manual = False
    db.add_instance(ExpertInstance(account_id=1, expert="AUDIT FAKE", enabled=True))
    assert not evaluate_gate(1, account.manual, ["AUDIT FAKE"]).allowed
    assert not evaluate_gate(1, True, ["AUDIT FAKE"]).allowed
    with patch.object(svc, "log_activity"):
        result = svc.run_allocation(account, plan, {}, base(),
                                    mode=pa.ALLOCATION_MODE_REBALANCE)
    assert not result["blocked"] and len(account.sent) == 1
    assert account.setting_reads == 0
    evidence["P2_manual_expert_gate_not_rechecked"] = {
        "manual_flag": account.manual, "enabled_expert_in_memory_db": True,
        "page_gate_allows": False, "submission_blocked": result["blocked"],
        "setting_reads_at_submission": account.setting_reads,
        "orders_sent_to_fake_broker": account.sent,
    }

    reset()
    account = FakeAccount()
    account.held = {"AAA": 10}
    labels = [pa.LabelTarget("L", 100, [pa.SymbolTarget("AAA", 0),
                                       pa.SymbolTarget("BBB", 100)])]
    current = {"AAA": pa.PositionState("AAA", quantity=10, cost_basis=1000,
                                        price=100, transaction_ids=[]),
               "BBB": pa.PositionState("BBB", price=100)}
    plan = pa.compute_allocation(1000, 0, labels, current, {}, allow_fractional=False,
                                 default_bp_factor=1, valuation_mode=pa.VALUATION_MODE_MARKET)
    with patch.object(svc, "log_activity") as log_activity:
        result = svc.run_allocation(account, plan, current, base(0, 1000),
                                    mode=pa.ALLOCATION_MODE_REBALANCE)
    assert plan.released_buying_power == 1000
    assert account.held["AAA"] == 10
    assert result["outcomes"][0].status == svc.OUTCOME_SKIPPED
    assert account.sent == [{"symbol": "BBB", "quantity": 10.0, "side": "BUY"}]
    severity = log_activity.call_args.args[0].value
    evidence["P2_untracked_sell_used_as_funding"] = {
        "planned_sell_release": plan.released_buying_power,
        "actual_sell_value": result["filled_sell_value"],
        "outcomes": [o.to_dict() for o in result["outcomes"]],
        "activity_severity": severity, "remaining_fake_AAA_qty": account.held["AAA"],
    }

    print(json.dumps(evidence, indent=2))
    out = Path(__file__).with_name("portfolio_allocator_audit_evidence_2026-09-07.json")
    out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
