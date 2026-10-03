"""IBKRAccount honours the account contracts: buildable, every abstract method implemented with the
interface's signature, status table total over ib_async's own strings, registry/settings-UI visible,
preview never sends an order. No network."""
import inspect

import pytest
from ib_async import OrderStatus as IBOrderStatus

from ba2_trade_platform.modules.accounts import ibkr_mapping as M
from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface
from ba2_trade_platform.core.types import OrderDirection, OrderType
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.modules.accounts import IBKRAccount, get_account_class, providers
from tests.ibkr_helpers import make_account


def abstract_names():
    names = set()
    for base in (ReadOnlyAccountInterface, AccountInterface):
        names |= set(getattr(base, "__abstractmethods__", ()))
    return names


class TestContract:
    def test_the_class_can_be_built(self):
        assert not inspect.isabstract(IBKRAccount)
        assert IBKRAccount.__abstractmethods__ == frozenset()

    def test_every_abstract_method_is_implemented_here_with_the_interfaces_parameters(self):
        for name in sorted(abstract_names()):
            impl = getattr(IBKRAccount, name)
            assert not getattr(impl, "__isabstractmethod__", False), name
            declared = None
            for base in (AccountInterface, ReadOnlyAccountInterface):
                declared = getattr(base, name, None)
                if declared is not None:
                    break
            want = [p for p in inspect.signature(declared).parameters if p != "self"]
            have = list(inspect.signature(impl).parameters)
            have = [p for p in have if p != "self"]
            # an implementation may ADD optional parameters but must accept every declared one
            for p in want:
                assert p in have or any(
                    q.kind is inspect.Parameter.VAR_KEYWORD for q in inspect.signature(impl).parameters.values()), \
                    f"{name} lacks parameter {p!r} (has {have})"

    def test_trading_flags(self):
        assert IBKRAccount.supports_trading is True
        assert IBKRAccount.supports_protective_legs is True

    def test_submit_order_is_the_templates_not_an_override(self):
        assert IBKRAccount.submit_order is AccountInterface.submit_order     # audit finding A1

    def test_registry_and_alias(self):
        assert providers["IBKR"] is IBKRAccount
        assert get_account_class("InteractiveBrokers") is IBKRAccount
        assert get_account_class("IBKR") is IBKRAccount

    def test_it_is_offered_by_the_settings_dialog(self):
        pytest.importorskip("nicegui")
        from ba2_trade_platform.ui.pages.settings import selectable_account_providers
        names, unavailable = selectable_account_providers()
        assert "IBKR" in names and "IBKR" not in unavailable

    def test_every_ib_async_status_string_is_mapped(self):
        statuses = {v for k, v in vars(IBOrderStatus).items()
                    if isinstance(v, str) and not k.startswith("_") and v}
        statuses |= set(IBOrderStatus.DoneStates) | set(IBOrderStatus.ActiveStates)
        unmapped = {s for s in statuses if s not in M.IB_STATUS_STRINGS}
        assert not unmapped, f"ib_async knows statuses the adapter does not map: {unmapped}"
        for status in statuses:
            M.map_ib_status(status)

    def test_the_ib_type_mapping_is_total_for_what_the_platform_writes(self):
        for ib_type, action, want in [("MKT", "BUY", OrderType.MARKET), ("LMT", "BUY", OrderType.BUY_LIMIT),
                                      ("LMT", "SELL", OrderType.SELL_LIMIT), ("STP", "SELL", OrderType.SELL_STOP),
                                      ("STP", "BUY", OrderType.BUY_STOP),
                                      ("STP LMT", "SELL", OrderType.SELL_STOP_LIMIT),
                                      ("STP LMT", "BUY", OrderType.BUY_STOP_LIMIT)]:
            assert IBKRAccount._map_order_type(ib_type, action) == want


class TestPreview:
    @pytest.fixture
    def world(self, monkeypatch):
        account, fake = make_account(monkeypatch)
        fake.add_stock("AAPL", 265598)
        yield account, fake
        account.close()

    def order(self, **kw):
        base = dict(account_id=1, symbol="AAPL", quantity=10.0, side=OrderDirection.BUY,
                    order_type=OrderType.MARKET)
        base.update(kw)
        return TradingOrder(**base)

    def test_what_if_maps_to_an_order_impact_and_sends_nothing(self, world):
        account, fake = world
        impact = account.preview_order_impact(self.order())
        assert impact.symbol == "AAPL"
        assert impact.margin_requirement == 5000.0 and impact.estimated_fees == 1.0
        assert impact.change_in_buying_power == -10000.0         # 5000 initial margin x Reg-T 2.0
        assert impact.bp_cost == 10000.0 and impact.accepted is True
        assert impact.raw["maint_margin_change"] == 4000.0
        assert fake.placed == []

    def test_a_cash_account_has_no_leverage_in_the_cost(self, world):
        account, fake = world
        fake.set_account_value("BuyingPower", "80000")
        assert account.preview_order_impact(self.order()).bp_cost == 5000.0

    def test_unmeasured_margin_is_no_precheck_not_a_free_order(self, world):
        account, fake = world
        fake.what_if.initMarginChange = float("nan")
        assert account.preview_order_impact(self.order()) is None

    def test_a_failed_what_if_is_none(self, world):
        account, fake = world
        fake.fail_calls["whatIfOrderAsync"] = TimeoutError("slow")
        assert account.preview_order_impact(self.order()) is None

    def test_an_order_type_it_cannot_price_is_none(self, world):
        account, fake = world
        assert account.preview_order_impact(self.order(order_type=OrderType.OCO)) is None

    def test_warning_text_is_surfaced(self, world):
        account, fake = world
        fake.what_if.warningText = "Order exceeds available funds"
        assert account.preview_order_impact(self.order()).warnings == ["Order exceeds available funds"]
