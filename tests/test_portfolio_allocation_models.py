"""The five portfolio-allocation tables: round-trip, idempotency keys, computed properties."""
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import (
    PortfolioAllocationConfig,
    PortfolioAllocationLabel,
    PortfolioAllocationRun,
    PortfolioAllocationSymbol,
    PortfolioIncomeEvent,
)


def test_allocation_label_round_trips_with_its_fields(mock_account_def):
    add_instance(PortfolioAllocationLabel(
        account_id=mock_account_def.id, label="ARK26", target_pct=40.0,
        sort_order=2, comment="growth sleeve"))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationLabel)).one()
        assert row.label == "ARK26"
        assert row.target_pct == 40.0
        assert row.sort_order == 2
        assert row.comment == "growth sleeve"


def test_duplicate_label_on_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioAllocationLabel(
        account_id=mock_account_def.id, label="ARK26", target_pct=40.0))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioAllocationLabel(
            account_id=mock_account_def.id, label="ARK26", target_pct=60.0))


def test_same_symbol_in_two_labels_is_allowed(mock_account_def):
    add_instance(PortfolioAllocationSymbol(
        account_id=mock_account_def.id, label="ARK26", symbol="TSLA", weight_pct=50.0))
    add_instance(PortfolioAllocationSymbol(
        account_id=mock_account_def.id, label="HighRisk", symbol="TSLA", weight_pct=25.0))
    with get_db() as session:
        rows = session.exec(select(PortfolioAllocationSymbol)).all()
        assert sorted(r.label for r in rows) == ["ARK26", "HighRisk"]


def test_duplicate_external_id_on_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioIncomeEvent(
        account_id=mock_account_def.id, external_id="act-1",
        event_date=date(2026, 8, 1), event_type="DEPOSIT", amount=1000.0))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioIncomeEvent(
            account_id=mock_account_def.id, external_id="act-1",
            event_date=date(2026, 8, 1), event_type="DEPOSIT", amount=1000.0))


def test_income_event_open_amount_is_the_unconsumed_remainder():
    event = PortfolioIncomeEvent(
        account_id=1, external_id="act-1", event_date=date(2026, 8, 1),
        event_type="DIVIDEND", amount=250.0, consumed_amount=90.0)
    assert event.open_amount == 160.0


def test_income_event_open_amount_never_goes_negative():
    event = PortfolioIncomeEvent(
        account_id=1, external_id="act-1", event_date=date(2026, 8, 1),
        event_type="DEPOSIT", amount=100.0, consumed_amount=140.0)
    assert event.open_amount == 0.0


def test_run_net_buy_value_is_buys_minus_sells():
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 submitted_buy_value=5000.0, submitted_sell_value=1200.0)
    assert run.net_buy_value == 3800.0


def test_run_net_buy_value_is_zero_when_sells_exceed_buys():
    run = PortfolioAllocationRun(account_id=1, mode="REBALANCE",
                                 submitted_buy_value=1000.0, submitted_sell_value=4000.0)
    assert run.net_buy_value == 0.0


def test_run_json_columns_round_trip(mock_account_def):
    add_instance(PortfolioAllocationRun(
        account_id=mock_account_def.id, mode="INVEST_LABEL", scope_label="ARK26",
        plan_json={"rows": [{"symbol": "TSLA", "side": "BUY"}], "scale_factor": 0.61},
        order_ids=[11, 12, 13]))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationRun)).one()
        assert row.plan_json["scale_factor"] == 0.61
        assert row.plan_json["rows"][0]["symbol"] == "TSLA"
        assert row.order_ids == [11, 12, 13]


def test_allocation_config_defaults_to_cost_mode_and_whole_shares(mock_account_def):
    add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig)).one()
        assert row.valuation_mode == "cost"
        assert row.allow_fractional is False


def test_allocation_config_round_trips_market_mode(mock_account_def):
    add_instance(PortfolioAllocationConfig(
        account_id=mock_account_def.id, valuation_mode="market", allow_fractional=True))
    with get_db() as session:
        row = session.exec(select(PortfolioAllocationConfig)).one()
        assert row.valuation_mode == "market"
        assert row.allow_fractional is True


def test_a_second_config_row_for_one_account_is_rejected(mock_account_def):
    add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
    with pytest.raises(IntegrityError):
        add_instance(PortfolioAllocationConfig(account_id=mock_account_def.id))
