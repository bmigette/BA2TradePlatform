"""Tests for expert and account module registries."""
import pytest


class TestExpertRegistry:
    def test_get_expert_class_by_name(self):
        from ba2_trade_platform.modules.experts import get_expert_class
        from ba2_trade_platform.modules.experts.FMPRating import FMPRating
        assert get_expert_class("FMPRating") is FMPRating

    def test_get_expert_class_unknown_returns_none(self):
        from ba2_trade_platform.modules.experts import get_expert_class
        assert get_expert_class("NonExistentExpert") is None

    def test_all_experts_have_description(self):
        from ba2_trade_platform.modules.experts import experts
        for expert_cls in experts:
            desc = expert_cls.description()
            assert isinstance(desc, str) and len(desc) > 0, (
                f"{expert_cls.__name__}.description() is empty"
            )

    def test_all_experts_have_settings_definitions(self):
        from ba2_trade_platform.modules.experts import experts
        for expert_cls in experts:
            defs = expert_cls.get_settings_definitions()
            assert isinstance(defs, dict), (
                f"{expert_cls.__name__}.get_settings_definitions() should return dict"
            )


class TestAccountRegistry:
    def test_get_account_class_alpaca(self):
        from ba2_trade_platform.modules.accounts import get_account_class
        from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
        assert get_account_class("Alpaca") is AlpacaAccount

    def test_get_account_class_ibkr(self):
        from ba2_trade_platform.modules.accounts import get_account_class
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        assert get_account_class("IBKR") is IBKRAccount

    def test_get_account_class_alias(self):
        from ba2_trade_platform.modules.accounts import get_account_class
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        assert get_account_class("InteractiveBrokers") is IBKRAccount

    def test_get_account_class_unknown_returns_none(self):
        from ba2_trade_platform.modules.accounts import get_account_class
        assert get_account_class("NonExistentProvider") is None
