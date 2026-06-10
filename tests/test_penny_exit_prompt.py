"""EX-3: PennyMomentumTrader exit-update prompt must expose the full condition set
and use time_after (not time_before) for end-of-day exits."""
from ba2_trade_platform.modules.experts.PennyMomentumTrader.prompts import (
    build_exit_update_prompt,
)


def _prompt():
    return build_exit_update_prompt("AAPL", {"stop_loss": {}, "take_profit": []}, "some news")


class TestExitUpdatePromptConditionTypes:
    def test_includes_previously_omitted_types(self):
        p = _prompt()
        for t in (
            "price_above_ema", "price_below_ema", "price_above_sma", "price_below_sma",
            "opening_range_breakout", "volume_above_avg", "volume_spike", "rsi_between",
            "macd_bullish_cross", "macd_bearish_cross", "ema_cross_above", "ema_cross_below",
            "time_after",
        ):
            assert t in p, f"exit-update prompt missing condition type: {t}"

    def test_eod_uses_time_after(self):
        p = _prompt()
        assert "time_after" in p
        # EOD guidance must steer away from time_before for end-of-day exits.
        assert "Do NOT use \"time_before\" for EOD" in p or "do NOT use \"time_before\"" in p.lower() \
            or "Do NOT use \"time_before\"" in p
