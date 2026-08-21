"""A broker positions-fetch OUTAGE must never be read as "the account is flat".

``get_positions()`` is tri-state on purpose (see ``AlpacaAccount.get_positions``
and the 2026-07-03 incident):

    None    -> the fetch FAILED. Nothing is known.
    []      -> the fetch succeeded and the account is genuinely FLAT.
    [...]   -> these are the positions.

THE BUG THESE TESTS PIN. ``TradeCondition.get_current_position`` iterated the
result unguarded and swallowed the resulting ``TypeError`` into ``return None``
-- the same value it uses for "no position in this symbol". So during an outage
``has_account_position()`` was ``False`` and ``HasNoPositionAccountCondition``
evaluated **TRUE**: a live ruleset saying "when there is no position, BUY" fired
and opened a DUPLICATE position on top of one the broker was already holding.

That is the unsafe direction. An unverified book must make position-dependent
rules REFUSE to fire, never fire eagerly, so both account-level conditions are
False when the state is unknown.
"""
import pytest

from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.TradeConditions import (
    HasNoPositionAccountCondition, HasPositionAccountCondition,
)
from ba2_trade_platform.core.types import (
    ExpertEventType, ExpertActionType, ExpertEventRuleType, OrderRecommendation,
)
from ba2_trade_platform.core.models import Ruleset, EventAction
from ba2_trade_platform.core.db import add_instance
from ba2_common.core.portfolio_allocation import PositionFetchFailed
from tests.conftest import MockAccount
from tests.factories import (
    create_account_definition, create_expert_instance, create_recommendation,
    link_rule_to_ruleset,
)


def _ruleset(event_type: ExpertEventType, action_type: ExpertActionType) -> int:
    """One rule: <event_type> -> <action_type>, no other gating."""
    rs_id = add_instance(Ruleset(
        name=f"{event_type.value} -> {action_type.value}",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
    ))
    ea_id = add_instance(EventAction(
        name=f"{action_type.value} on {event_type.value}",
        type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
        triggers={"trigger_0": {"event_type": event_type.value}},
        actions={"action_0": {"action_type": action_type.value}},
        continue_processing=False,
    ))
    link_rule_to_ruleset(rs_id, ea_id, order_index=0)
    return rs_id


def _account_and_rec(positions):
    acct_def = create_account_definition()
    account = MockAccount(acct_def.id)
    account._positions = positions
    ei = create_expert_instance(account_id=acct_def.id)
    rec = create_recommendation(
        instance_id=ei.id, recommended_action=OrderRecommendation.BUY, symbol="AAPL")
    return account, rec


class _DictPosition(dict):
    """A broker whose book is dict-shaped rather than object-shaped."""


class TestNoPositionRuleDuringAnOutage:
    def test_no_buy_is_triggered_when_the_position_fetch_failed(self):
        """THE MONEY BUG: outage -> "no position" -> a duplicate BUY on a held position."""
        account, rec = _account_and_rec(None)          # None == the fetch FAILED
        rs_id = _ruleset(ExpertEventType.F_HAS_NO_POSITION_ACCOUNT, ExpertActionType.BUY)

        results = TradeActionEvaluator(account=account).evaluate("AAPL", rec, rs_id)

        assert results == [], (
            "a positions-fetch outage fired the no-position BUY rule -- this opens a "
            f"duplicate position with real money. Got: {results}")

    def test_the_buy_still_fires_when_the_account_is_genuinely_flat(self):
        """The fix must not disarm the rule for its real case: [] is a CONFIRMED flat book."""
        account, rec = _account_and_rec([])
        rs_id = _ruleset(ExpertEventType.F_HAS_NO_POSITION_ACCOUNT, ExpertActionType.BUY)

        results = TradeActionEvaluator(account=account).evaluate("AAPL", rec, rs_id)

        assert len(results) > 0, "a confirmed-flat account must still trigger the BUY"

    def test_no_close_is_triggered_when_the_position_fetch_failed(self):
        """The mirror rule must also refuse: acting on an unverified book both ways."""
        account, rec = _account_and_rec(None)
        rs_id = _ruleset(ExpertEventType.F_HAS_POSITION_ACCOUNT, ExpertActionType.CLOSE)

        results = TradeActionEvaluator(account=account).evaluate("AAPL", rec, rs_id)

        assert results == [], f"acted on an unverified position book. Got: {results}"


class TestAccountConditionsFailSafe:
    @pytest.mark.parametrize("condition_class", [
        HasNoPositionAccountCondition, HasPositionAccountCondition,
    ])
    def test_both_account_conditions_are_false_when_the_state_is_unknown(self, condition_class):
        account, rec = _account_and_rec(None)
        assert condition_class(account, "AAPL", rec).evaluate() is False

    @pytest.mark.parametrize("condition_class", [
        HasNoPositionAccountCondition, HasPositionAccountCondition,
    ])
    def test_the_display_says_unknown_rather_than_no(self, condition_class):
        """"No position" and "we could not find out" must not read identically in the UI."""
        account, rec = _account_and_rec(None)
        cond = condition_class(account, "AAPL", rec)
        cond.evaluate()
        assert "UNKNOWN" in (cond.get_actual_value_display() or "")


class TestGetCurrentPositionContract:
    def test_raises_position_fetch_failed_when_get_positions_returns_none(self):
        account, rec = _account_and_rec(None)
        with pytest.raises(PositionFetchFailed):
            HasNoPositionAccountCondition(account, "AAPL", rec).get_current_position()

    def test_raises_position_fetch_failed_when_get_positions_raises(self):
        account, rec = _account_and_rec([])

        def boom():
            raise ConnectionError("getaddrinfo failed")

        account.get_positions = boom
        with pytest.raises(PositionFetchFailed):
            HasNoPositionAccountCondition(account, "AAPL", rec).get_current_position()

    def test_returns_none_for_a_symbol_absent_from_a_confirmed_book(self):
        """The only remaining meaning of None: the fetch worked, we hold no AAPL."""
        account, rec = _account_and_rec([])
        assert HasNoPositionAccountCondition(account, "AAPL", rec).get_current_position() is None

    def test_reads_a_dict_shaped_position_book(self):
        """``hasattr(position, 'symbol')`` silently answered "no position" for every
        dict-shaped book -- a silent wrong answer of the same family."""
        account, rec = _account_and_rec([_DictPosition(symbol="AAPL", qty=10.0)])
        assert HasNoPositionAccountCondition(account, "AAPL", rec).get_current_position() == 10.0
