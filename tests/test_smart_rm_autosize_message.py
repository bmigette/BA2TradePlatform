"""Regression: the auto-size failure message must not contradict the caller.

When the LLM agent calls open_buy_position with no quantity but an explicit
sl_price, and the risk budget is still too small to afford even 1 share at
that stop distance, _open_position_internal returned a message that always
told the agent to "Provide an sl_price so the system can size by risk" - even
though one was already given. The agent (and a human reading the smart risk
manager transcript) has no way to tell from that message that supplying a
stop isn't the fix; the real issue is the stop distance vs. risk budget. This
is the literal ACN incident from the 2026-06-24 prod smart risk manager run
(job #31): sl_price=120.85 was passed and the tool still said "provide an
sl_price".
"""
import logging

from ba2_trade_platform.core.SmartRiskManagerToolkit import SmartRiskManagerToolkit
from ba2_trade_platform.core.types import OrderDirection


class _StubExpert:
    """Minimal expert exposing only what _auto_size_by_risk reads."""

    def __init__(self, virtual_balance=310.0, risk_per_trade_pct=2.0):
        self._virtual_balance = virtual_balance
        self._risk_per_trade_pct = risk_per_trade_pct

    def get_virtual_balance(self):
        return self._virtual_balance

    def get_available_balance(self):
        return self._virtual_balance

    def get_setting_with_interface_default(self, key, log_warning=True):
        return {
            "sizing_mode": "notional",
            "risk_per_trade_pct": self._risk_per_trade_pct,
            "atr_multiplier": 2.0,
            "atr_period": 14,
            "min_stop_loss_pct": 0.0,
            "max_virtual_equity_per_instrument_percent": 10.0,
        }.get(key)


def _make_toolkit(virtual_balance=310.0, risk_per_trade_pct=2.0, current_price=129.72):
    # Bypass the DB-backed __init__; the auto-size-failure branch returns
    # before any DB access, so this is a pure in-memory unit test.
    tk = object.__new__(SmartRiskManagerToolkit)
    tk.expert = _StubExpert(virtual_balance, risk_per_trade_pct)
    tk.logger = logging.getLogger("test_smart_rm_autosize_message")
    tk.get_current_price = lambda symbol: current_price
    return tk


class TestAutoSizeFailureMessage:
    def test_message_does_not_ask_for_sl_price_already_given(self):
        """ACN incident: equity=$310, risk%=2 -> $6.20 budget; sl_price=120.85
        against a $129.72 price -> $8.87/share. 1 share already exceeds the
        budget, so sizing fails - but the caller already supplied sl_price,
        so the message must not tell it to do so again."""
        tk = _make_toolkit()
        result = tk._open_position_internal(
            symbol="ACN", order_direction=OrderDirection.BUY, quantity=None,
            tp_price=142.0, sl_price=120.85,
        )
        assert result["success"] is False
        assert "provide an sl_price" not in result["message"].lower()

    def test_message_does_ask_for_sl_price_when_none_given_and_atr_unusable(self):
        """When no sl_price was given and ATR sizing is also unusable, asking
        for an sl_price is the correct, actionable hint - must be preserved."""
        tk = _make_toolkit()
        result = tk._open_position_internal(
            symbol="ACN", order_direction=OrderDirection.BUY, quantity=None,
            tp_price=142.0, sl_price=None,
        )
        assert result["success"] is False
        assert "sl_price" in result["message"].lower()
