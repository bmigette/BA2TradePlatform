"""RM-1 / B4: numeric TradingAgents settings must honor a configured 0."""
from contextlib import contextmanager
from unittest.mock import PropertyMock, patch

from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents


@contextmanager
def _expert_with_settings(settings):
    # Bypass __init__ (DB/network); _int_setting only needs .settings + the classmethod defs.
    # `settings` is a read-only property, so patch it for the duration of the test.
    expert = TradingAgents.__new__(TradingAgents)
    with patch.object(TradingAgents, "settings", new_callable=PropertyMock, return_value=settings):
        yield expert


class TestIntSettingHonorsZero:
    def test_configured_zero_is_kept(self):
        with _expert_with_settings({"memory_max_trades": 0}) as expert:
            assert expert._int_setting("memory_max_trades") == 0

    def test_configured_zero_debate_rounds_kept(self):
        with _expert_with_settings({"debates_new_positions": 0}) as expert:
            assert expert._int_setting("debates_new_positions") == 0

    def test_missing_uses_definition_default(self):
        with _expert_with_settings({}) as expert:
            default = int(TradingAgents.get_settings_definitions()["memory_max_trades"]["default"])
            assert expert._int_setting("memory_max_trades") == default

    def test_nonzero_value_passthrough(self):
        with _expert_with_settings({"news_lookback_days": 5}) as expert:
            assert expert._int_setting("news_lookback_days") == 5
