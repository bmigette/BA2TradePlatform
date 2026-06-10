"""Tests for ba2_trade_platform.core.utils helper functions."""
import pytest
from datetime import datetime, timezone
from ba2_trade_platform.core.utils import calculate_transaction_pnl
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import OrderDirection, TransactionStatus
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance,
    create_recommendation, create_trading_order, create_transaction,
)


class TestCalculateTransactionPnl:
    def test_long_profit(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=110.0,
            open_date=datetime.now(timezone.utc),
        )
        pnl = calculate_transaction_pnl(tx)
        assert pnl == pytest.approx(100.0)  # (110-100)*10

    def test_long_loss(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=90.0,
            open_date=datetime.now(timezone.utc),
        )
        pnl = calculate_transaction_pnl(tx)
        assert pnl == pytest.approx(-100.0)  # (90-100)*10

    def test_short_profit(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.SELL,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=90.0,
            open_date=datetime.now(timezone.utc),
        )
        pnl = calculate_transaction_pnl(tx)
        assert pnl == pytest.approx(100.0)  # (100-90)*10

    def test_short_loss(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.SELL,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=110.0,
            open_date=datetime.now(timezone.utc),
        )
        pnl = calculate_transaction_pnl(tx)
        assert pnl == pytest.approx(-100.0)  # (100-110)*10

    def test_missing_close_price_returns_none(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=100.0, close_price=None,
            open_date=datetime.now(timezone.utc),
        )
        assert calculate_transaction_pnl(tx) is None

    def test_missing_open_price_returns_none(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
            status=TransactionStatus.OPENED, open_price=None, close_price=110.0,
            open_date=datetime.now(timezone.utc),
        )
        assert calculate_transaction_pnl(tx) is None

    def test_zero_quantity_returns_none(self):
        tx = Transaction(
            symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=110.0,
            open_date=datetime.now(timezone.utc),
        )
        # quantity is falsy (0.0), so returns None
        assert calculate_transaction_pnl(tx) is None

    def test_breakeven_long(self):
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=100.0,
            open_date=datetime.now(timezone.utc),
        )
        assert calculate_transaction_pnl(tx) == pytest.approx(0.0)

    def test_option_long_applies_contract_multiplier(self):
        # 1 long call, premium 14.65 -> 14.61. Cash P&L = (14.61-14.65)*1*100 = -4.00
        tx = Transaction(
            symbol="SPY", quantity=1.0, side=OrderDirection.BUY,
            status=TransactionStatus.CLOSED, open_price=14.65, close_price=14.61,
            open_date=datetime.now(timezone.utc), multiplier=100,
        )
        assert calculate_transaction_pnl(tx) == pytest.approx(-4.0)

    def test_option_short_applies_contract_multiplier(self):
        # Sold a put for 2.00, bought back at 0.50 -> (2.00-0.50)*3*100 = +450
        tx = Transaction(
            symbol="TSLA", quantity=3.0, side=OrderDirection.SELL,
            status=TransactionStatus.CLOSED, open_price=2.0, close_price=0.5,
            open_date=datetime.now(timezone.utc), multiplier=100,
        )
        assert calculate_transaction_pnl(tx) == pytest.approx(450.0)

    def test_equity_multiplier_none_unchanged(self):
        # Equity rows leave multiplier null -> treated as 1 (no behaviour change).
        tx = Transaction(
            symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
            status=TransactionStatus.CLOSED, open_price=100.0, close_price=110.0,
            open_date=datetime.now(timezone.utc), multiplier=None,
        )
        assert calculate_transaction_pnl(tx) == pytest.approx(100.0)


class TestHasExistingTransactionsForExpertAndSymbol:
    def test_no_transactions(self):
        from ba2_trade_platform.core.utils import has_existing_transactions_for_expert_and_symbol
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        assert has_existing_transactions_for_expert_and_symbol(ei.id, "ZZZZZ") is False

    def test_with_opened_transaction(self):
        from ba2_trade_platform.core.utils import has_existing_transactions_for_expert_and_symbol
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        create_transaction(symbol="AAPL", expert_id=ei.id, status=TransactionStatus.OPENED)
        assert has_existing_transactions_for_expert_and_symbol(ei.id, "AAPL") is True

    def test_with_closed_transaction_returns_false(self):
        from ba2_trade_platform.core.utils import has_existing_transactions_for_expert_and_symbol
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        create_transaction(symbol="MSFT", expert_id=ei.id, status=TransactionStatus.CLOSED)
        assert has_existing_transactions_for_expert_and_symbol(ei.id, "MSFT") is False


class TestGetLatestRecommendationIdForSymbol:
    """Tests for resolving the latest recommendation for a symbol.

    Used by the per-symbol "Place Order" button so a manually-created order is
    linked back to an expert (via TradingOrder.expert_recommendation_id ->
    ExpertRecommendation.instance_id -> Transaction.expert_id). Previously that
    button passed no recommendation, leaving Transaction.expert_id NULL.
    """

    def test_returns_none_when_no_recommendations(self):
        from ba2_trade_platform.core.utils import get_latest_recommendation_id_for_symbol
        assert get_latest_recommendation_id_for_symbol("ZZZZZ") is None

    def test_returns_latest_recommendation_id(self):
        from ba2_trade_platform.core.utils import get_latest_recommendation_id_for_symbol
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        create_recommendation(instance_id=ei.id, symbol="BRUN")
        latest = create_recommendation(instance_id=ei.id, symbol="BRUN")

        assert get_latest_recommendation_id_for_symbol("BRUN") == latest.id

    def test_filters_by_expert_instance_ids(self):
        from ba2_trade_platform.core.utils import get_latest_recommendation_id_for_symbol
        acct_def = create_account_definition()
        ei_a = create_expert_instance(account_id=acct_def.id)
        ei_b = create_expert_instance(account_id=acct_def.id)
        rec_a = create_recommendation(instance_id=ei_a.id, symbol="BRUN")
        # rec_b is newer overall, but we restrict to expert A
        create_recommendation(instance_id=ei_b.id, symbol="BRUN")

        result = get_latest_recommendation_id_for_symbol("BRUN", expert_instance_ids=[ei_a.id])

        assert result == rec_a.id

    def test_empty_expert_instance_ids_returns_none(self):
        """An empty (but not None) filter means 'no experts in scope' -> no match."""
        from ba2_trade_platform.core.utils import get_latest_recommendation_id_for_symbol
        acct_def = create_account_definition()
        ei = create_expert_instance(account_id=acct_def.id)
        create_recommendation(instance_id=ei.id, symbol="BRUN")

        assert get_latest_recommendation_id_for_symbol("BRUN", expert_instance_ids=[]) is None


class TestGetAccountIdForRecommendation:
    """Tests for resolving the owning account of a recommendation.

    A manually-placed order must submit to the account that owns the
    recommending expert (recommendation -> ExpertInstance -> account_id), not
    just the first configured account.
    """

    def test_returns_none_for_missing_recommendation(self):
        from ba2_trade_platform.core.utils import get_account_id_for_recommendation
        assert get_account_id_for_recommendation(999999) is None

    def test_returns_none_for_none_id(self):
        from ba2_trade_platform.core.utils import get_account_id_for_recommendation
        assert get_account_id_for_recommendation(None) is None

    def test_returns_account_of_recommending_expert(self):
        from ba2_trade_platform.core.utils import get_account_id_for_recommendation
        acct_a = create_account_definition(name="Account A")
        acct_b = create_account_definition(name="Account B")
        # Expert lives on account B, not the first-created account A
        ei = create_expert_instance(account_id=acct_b.id)
        rec = create_recommendation(instance_id=ei.id, symbol="BRUN")

        assert get_account_id_for_recommendation(rec.id) == acct_b.id
