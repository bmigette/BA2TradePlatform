"""A rule condition's daily history comes from the ACCOUNT's history source, never from a vendor it names.

2026-11-12: the realised-vol / relative-volume / recent-high conditions asked for ``"yfinance"`` live (Yahoo's
dividend-adjusted bars) while a backtest of the same rule read the run's split-adjusted history.
"""
import inspect
from types import SimpleNamespace

import pytest

from ba2_common.core import TradeConditions as TC
from ba2_common.core.backtest_context import LIVE_OHLCV_PROVIDER


@pytest.fixture
def seams():
    before = (TC._provider_resolver, TC.get_account_history_provider_resolver())
    yield
    TC.set_provider_resolver(before[0])
    TC.set_account_history_provider_resolver(before[1])


def test_wired_live_the_accounts_own_history_answers(seams):
    asked = []
    TC.set_provider_resolver(lambda *a, **k: pytest.fail("the named-vendor resolver must not be asked"))
    TC.set_account_history_provider_resolver(lambda account_id: asked.append(account_id) or f"history-of-{account_id}")
    assert TC._history_ohlcv_provider(SimpleNamespace(id=3)) == "history-of-3"
    assert asked == [3]


def test_unwired_it_asks_the_provider_resolver_for_the_live_history_key(seams):
    asked = []
    TC.set_account_history_provider_resolver(None)
    TC.set_provider_resolver(lambda category, name, **kw: asked.append((category, name, kw)) or "run-history")
    assert TC._history_ohlcv_provider(SimpleNamespace(id=3)) == "run-history"
    assert asked == [("ohlcv", LIVE_OHLCV_PROVIDER, {})]


def test_a_failing_account_resolver_propagates_with_no_other_vendor(seams):
    def boom(account_id):
        raise ValueError("ohlcv_history_source 'alpaca' needs Alpaca market-data keys")

    TC.set_account_history_provider_resolver(boom)
    TC.set_provider_resolver(lambda *a, **k: pytest.fail("no fallback to a named vendor"))
    with pytest.raises(ValueError, match="needs Alpaca"):
        TC._history_ohlcv_provider(SimpleNamespace(id=3))


def test_no_condition_names_a_price_vendor():
    src = inspect.getsource(TC)
    assert '_get_provider("ohlcv", "yfinance")' not in src
    assert src.count("_history_ohlcv_provider(self.account)") == 4
