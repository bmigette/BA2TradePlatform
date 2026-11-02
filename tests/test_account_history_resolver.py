"""ONE host resolver: account / expert / transaction -> the OHLCV history provider of that account's source
(plan 2026-10-24-history-source-task11, Task 8). A failed alpaca resolution is an error, never an FMP read."""
import pytest

from ba2_trade_platform.core import account_history as AH
from ba2_trade_platform.core.account_history import HistorySourceNeedsAlpacaKeys
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountSetting
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

KEY = "ohlcv_history_source"


def _set(account_id, key, value):
    with get_db() as session:
        session.add(AccountSetting(account_id=account_id, key=key, value_str=value, value_float=None))
        session.commit()


def alpaca_account(name="A", source="alpaca", keys=True):
    acc = create_account_definition(name=name, provider="Alpaca")
    if keys:
        _set(acc.id, "api_key", f"key-{acc.id}")
        _set(acc.id, "api_secret", f"secret-{acc.id}")
    if source is not None:
        _set(acc.id, KEY, source)
    return acc


def test_unset_account_gets_the_fmp_provider(monkeypatch):
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    monkeypatch.setattr(FMPOHLCVProvider, "__init__", lambda self, *a, **k: None)      # no FMP key in the test DB
    acc = alpaca_account(source=None)
    assert AH.history_source_for_account(acc.id) == "fmp"
    assert AH.history_spec_for_account(acc.id) == ("fmp", {})
    assert isinstance(AH.history_provider_for_account(acc.id), FMPOHLCVProvider)


def test_alpaca_account_gets_alpaca_hist_with_its_own_keys():
    acc = alpaca_account()
    name, kw = AH.history_spec_for_account(acc.id)
    assert name == "alpaca_hist" and kw["api_key"] == f"key-{acc.id}" and kw["api_secret"] == f"secret-{acc.id}"
    assert callable(kw["corporate_actions_refresher"])
    prov = AH.history_provider_for_account(acc.id)
    assert type(prov).__name__ == "AlpacaHistoryOHLCVProvider" and prov.live


def test_two_accounts_two_providers():
    a, b = alpaca_account("A", "alpaca"), alpaca_account("B", "fmp")
    assert AH.history_spec_for_account(a.id)[0] == "alpaca_hist" and AH.history_spec_for_account(b.id)[0] == "fmp"


def test_alpaca_without_keys_refuses_at_resolution():
    acc = alpaca_account(keys=False)
    with pytest.raises(HistorySourceNeedsAlpacaKeys):
        AH.history_spec_for_account(acc.id)


def test_expert_and_transaction_resolve_through_their_account():
    a, b = alpaca_account("A", "alpaca"), alpaca_account("B", "fmp")
    ea, eb = create_expert_instance(a.id), create_expert_instance(b.id)
    assert AH.history_spec_for_expert(ea.id)[0] == "alpaca_hist" and AH.history_spec_for_expert(eb.id)[0] == "fmp"
    t = create_transaction(symbol="AAA", expert_id=ea.id)
    create_trading_order(a.id, symbol="AAA", transaction_id=t.id)
    assert AH.history_spec_for_transaction(t.id)[0] == "alpaca_hist"
    assert type(AH.history_provider_for_expert(ea.id)).__name__ == "AlpacaHistoryOHLCVProvider"
    assert type(AH.history_provider_for_transaction(t.id)).__name__ == "AlpacaHistoryOHLCVProvider"


def test_unresolvable_ids_raise_no_fmp_fallback():
    for fn in (AH.history_spec_for_account, AH.history_spec_for_expert, AH.history_spec_for_transaction):
        with pytest.raises(ValueError):
            fn(99999)
    t = create_transaction(symbol="AAA")                           # no orders: no account to resolve
    with pytest.raises(ValueError, match="no order"):
        AH.history_spec_for_transaction(t.id)


def test_alpaca_resolution_never_constructs_fmp(monkeypatch):
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

    def boom(self, *a, **k):
        raise AssertionError("FMP provider must not be built for an alpaca account")
    monkeypatch.setattr(FMPOHLCVProvider, "__init__", boom)
    acc = alpaca_account()
    AH.history_provider_for_account(acc.id)
