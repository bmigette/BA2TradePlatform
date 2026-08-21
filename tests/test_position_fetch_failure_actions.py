"""TradeActions must report an unverified position book as such, not as "no position".

Companion to ``test_position_fetch_failure_conditions.py``, which fixes the
money-losing half of this bug class. ``TradeAction.get_current_position`` carried
the identical swallow -- iterate ``get_positions()`` unguarded, absorb the
resulting ``TypeError``, ``return None``.

The DIRECTION here was already safe: both consumers (``SellAction``,
``CloseAction``) fail CLOSED, refusing to trade on a ``None``. So this is a
truthfulness fix, not a safety fix: an outage produced "No long position to sell
for AAPL", which is a claim about the broker's book that nobody verified, and
which sends whoever reads the activity log looking for a position that may well
exist. The two states must be distinguishable in the result message.

Both actions must still REFUSE to trade -- that part is a regression guard.
"""
import pytest

from ba2_trade_platform.core.TradeActions import SellAction, CloseAction
from ba2_trade_platform.core.types import OrderRecommendation
from ba2_common.core.portfolio_allocation import PositionFetchFailed
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
)


def _account(positions):
    """(account, recommendation) — the result row requires a recommendation FK."""
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    account._positions = positions
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(
        instance_id=ei.id, symbol="AAPL", recommended_action=OrderRecommendation.SELL)
    return account, rec


def _sell(account_and_rec):
    account, rec = account_and_rec
    return SellAction("AAPL", account, OrderRecommendation.SELL, expert_recommendation=rec)


def _close(account_and_rec):
    # No existing_order -> the legacy broker-position path, which is the one that reads
    # get_current_position(). The preferred path delegates to close_transaction().
    account, rec = account_and_rec
    return CloseAction("AAPL", account, OrderRecommendation.SELL, expert_recommendation=rec)


class TestGetCurrentPositionContract:
    def test_raises_position_fetch_failed_when_get_positions_returns_none(self):
        with pytest.raises(PositionFetchFailed):
            _sell(_account(None)).get_current_position()

    def test_returns_none_for_a_symbol_absent_from_a_confirmed_book(self):
        assert _sell(_account([])).get_current_position() is None

    def test_reads_a_dict_shaped_position_book(self):
        account = _account([{"symbol": "AAPL", "qty": 10.0}])
        assert _sell(account).get_current_position() == 10.0


class TestSellDuringAnOutage:
    def test_still_refuses_to_sell(self):
        result = _sell(_account(None)).execute()
        assert result["success"] is False

    def test_does_not_claim_there_is_no_position(self):
        result = _sell(_account(None)).execute()
        assert "No long position to sell" not in result["message"], (
            "reported an unverified book as a confirmed absence of a position")
        assert "unverified" in result["message"].lower()

    def test_a_confirmed_flat_book_still_says_no_position(self):
        """The honest message must stay reachable for the case it describes."""
        result = _sell(_account([])).execute()
        assert result["success"] is False
        assert "No long position to sell" in result["message"]


class TestCloseDuringAnOutage:
    def test_still_refuses_to_close(self):
        result = _close(_account(None)).execute()
        assert result["success"] is False

    def test_does_not_claim_there_is_no_position(self):
        result = _close(_account(None)).execute()
        assert "No position to close" not in result["message"]
        assert "unverified" in result["message"].lower()

    def test_a_confirmed_flat_book_still_says_no_position(self):
        result = _close(_account([])).execute()
        assert result["success"] is False
        assert "No position to close" in result["message"]
