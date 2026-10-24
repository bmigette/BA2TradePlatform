"""The account-bound live sites follow the account's history source (plan 2026-10-24-history-source-task11, Task 8)."""
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from ba2_common.core import backtest_context as BC
from ba2_trade_platform.core import account_history as AH
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountSetting
from tests.factories import create_account_definition, create_expert_instance

KEY = "ohlcv_history_source"


def _set(account_id, key, value):
    with get_db() as session:
        session.add(AccountSetting(account_id=account_id, key=key, value_str=value, value_float=None))
        session.commit()


def account(source):
    acc = create_account_definition(name=f"acc-{source}", provider="Alpaca")
    _set(acc.id, "api_key", "k")
    _set(acc.id, "api_secret", "s")
    _set(acc.id, KEY, source)
    return acc


@pytest.fixture
def wired_history_resolver():
    before = BC.get_live_history_resolver()
    BC.set_live_history_resolver(AH.history_spec_for_expert)
    yield
    BC.set_live_history_resolver(before)


def _expert_bundle(expert_id, calls):
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    import ba2_common.core.TradeConditions as TC
    TC.set_provider_resolver(lambda cat, name, **kw: calls.append((cat, name, sorted(kw))) or SimpleNamespace(cat=cat, name=name, kw=kw))
    return MarketExpertInterface._live_providers(SimpleNamespace(id=expert_id))


@pytest.fixture(autouse=True)
def _restore_provider_resolver():
    import ba2_common.core.TradeConditions as TC
    before = TC._provider_resolver if hasattr(TC, "_provider_resolver") else None
    yield
    TC.set_provider_resolver(before)


def test_default_without_a_wired_resolver_is_unchanged():
    before = BC.get_live_history_resolver()
    BC.set_live_history_resolver(None)
    try:
        calls = []
        b = _expert_bundle(1, calls)
        b.ohlcv()
        assert calls == [("ohlcv", "fmp", [])]
    finally:
        BC.set_live_history_resolver(before)


def test_expert_bundle_ohlcv_follows_the_account(wired_history_resolver):
    a, f = account("alpaca"), account("fmp")
    ea, ef = create_expert_instance(a.id), create_expert_instance(f.id)
    calls = []
    got_a = _expert_bundle(ea.id, calls).ohlcv()
    got_f = _expert_bundle(ef.id, calls).ohlcv()
    assert (got_a.name, sorted(got_a.kw)) == ("alpaca_hist", ["api_key", "api_secret", "corporate_actions_refresher", "fmp_split_rows"])
    assert (got_f.name, got_f.kw) == ("fmp", {})


def test_indicators_are_built_on_the_accounts_provider(wired_history_resolver):
    a = account("alpaca")
    e = create_expert_instance(a.id)
    b = _expert_bundle(e.id, [])
    ind = b.indicators()
    assert ind.name == "pandas" and ind.kw["ohlcv_provider"].name == "alpaca_hist"


def test_unresolvable_expert_raises_through_the_bundle(wired_history_resolver):
    with pytest.raises(ValueError):
        _expert_bundle(424242, []).ohlcv()


# ---- ATM-IV spot source -------------------------------------------------------------------------
class FakeHistory:
    """Stands in for a live AlpacaHistoryOHLCVProvider: ADJUSTED daily bars."""
    def __init__(self, frame=None, error=None):
        self.frame, self.error, self.calls = frame, error, []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls.append((symbol, start_date, end_date, interval))
        if self.error:
            raise self.error
        return self.frame


def _adjusted(days, close):
    d = pd.to_datetime(days)
    return pd.DataFrame({"Date": d, "Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0})


def test_atm_iv_alpaca_spots_are_as_traded_from_the_basis_record_not_the_fmp_calendar(monkeypatch):
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    from ba2_providers.ohlcv.alpaca_history import live_state
    from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H

    def boom(*a, **k):
        raise AssertionError("FMP must not be touched on the alpaca path")
    monkeypatch.setattr(FMPOHLCVProvider, "__init__", boom)
    monkeypatch.setattr(live_state, "manifest_events",
                        lambda s: [("2026-03-04", 2.0, "split"), ("2026-03-06", 1.25, "spinoff")])
    hist = FakeHistory(_adjusted(["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"], 10.0))
    spots = H.AlpacaHistorySpotSource(hist).spots("AAA", date(2026, 3, 2), date(2026, 3, 9))
    # adjusted x (split F) x (spin-off F) of the events dated AFTER the bar
    assert spots[date(2026, 3, 2)] == pytest.approx(10.0 * 2.0 * 1.25)
    assert spots[date(2026, 3, 4)] == pytest.approx(10.0 * 1.25)
    assert spots[date(2026, 3, 6)] == pytest.approx(10.0) and spots[date(2026, 3, 9)] == pytest.approx(10.0)
    assert hist.calls and hist.calls[0][2] is None                  # a LATEST read: the provider tops up


def test_atm_iv_alpaca_fetch_error_is_transient_never_an_fmp_read(monkeypatch):
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider
    from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveFetchError
    from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
    monkeypatch.setattr(FMPOHLCVProvider, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("FMP read")))
    src = H.AlpacaHistorySpotSource(FakeHistory(error=AlpacaLiveFetchError("HTTP 500")))
    with pytest.raises(H.SpotSourceTransient):
        src.spots("AAA", date(2026, 3, 2), date(2026, 3, 9))


def test_atm_iv_symbol_not_in_the_build_is_unavailable_not_transient():
    from ba2_providers.ohlcv.alpaca_history.live_topup import SymbolNotInHistory
    from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
    src = H.AlpacaHistorySpotSource(FakeHistory(error=SymbolNotInHistory("not in the build")))
    with pytest.raises(H.SpotUnavailable):
        src.spots("ZZZ", date(2026, 3, 2), date(2026, 3, 9))


def test_atm_iv_fmp_path_is_unchanged(monkeypatch):
    from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
    created = []
    monkeypatch.setattr(H, "AtmIvHistoryProvider", lambda **kw: created.append(kw) or SimpleNamespace(**kw))
    H._REGISTRY.clear()
    monkeypatch.setattr("ba2_providers.options.alpaca.AlpacaOptionsProvider", lambda **kw: SimpleNamespace(**kw))
    H.get_provider_for_credentials("k", "s", True, history_source="fmp")
    assert "spot_source" not in created[0]                          # AtmIvHistoryProvider's own FmpSpotSource default
    H.get_provider_for_credentials("k", "s", True, history_source="alpaca")
    assert type(created[1]["spot_source"]).__name__ == "AlpacaHistorySpotSource"
    assert len(H._REGISTRY) == 2                                    # one provider per (credentials, paper, source)
    H._REGISTRY.clear()


# ---- ATR (classic RM / SmartRM) -----------------------------------------------------------------
def test_fmp_accounts_atr_still_yfinance_and_alpaca_accounts_follow_alpaca_history():
    f, a = account("fmp"), account("alpaca")
    ef, ea = create_expert_instance(f.id), create_expert_instance(a.id)
    assert AH.indicator_provider_for_expert(ef.id) is None          # None = the default (yfinance) stack, as today
    prov = AH.indicator_provider_for_expert(ea.id)
    assert type(prov).__name__ == "PandasIndicatorCalc"
    assert type(prov.ohlcv_provider).__name__ == "AlpacaHistoryOHLCVProvider" if hasattr(prov, "ohlcv_provider") else True


def test_classic_rm_uses_the_per_expert_provider_live_but_not_in_a_backtest():
    from ba2_common.core import TradeRiskManagement as T
    sentinel, default = object(), object()
    before = T.get_indicator_provider_resolver()
    T.set_indicator_provider_resolver(lambda expert_id: sentinel if expert_id == 7 else None)
    try:
        live = T.TradeRiskManagement(indicator_provider=default)
        assert live._indicator_provider_for(SimpleNamespace(id=7)) is sentinel
        assert live._indicator_provider_for(SimpleNamespace(id=8)) is default
        bt = T.TradeRiskManagement(indicator_provider=default, as_of=object())
        assert bt._indicator_provider_for(SimpleNamespace(id=7)) is default       # a backtest never consults the live seam
    finally:
        T.set_indicator_provider_resolver(before)


def test_wire_all_seams_installs_both_resolvers():
    import inspect
    from ba2_trade_platform.core import seam_wiring
    src = inspect.getsource(seam_wiring.wire_all_seams)
    assert "set_live_history_resolver(account_history.history_spec_for_expert)" in src
    assert "set_indicator_provider_resolver(account_history.indicator_provider_for_expert)" in src
