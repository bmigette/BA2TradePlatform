"""FactorRanker's OHLCV fetchers never default to a history source the job did not choose.

A point-in-time read (``as_of`` given: backtest / replay) with no supplied provider refuses; live
(``as_of`` None) reads the live registry's provider. The class is never imported: ``get_provider`` is
monkeypatched and the registry key asserted.
"""
from datetime import datetime

import pandas as pd
import pytest

from ba2_experts.FactorRanker import data


class _Frame:
    def __init__(self):
        self.calls = []

    def get_ohlcv_data(self, sym, **kwargs):
        self.calls.append((sym, kwargs))
        return pd.DataFrame({"Close": [1.0, 2.0]})


def test_point_in_time_read_without_a_provider_refuses(monkeypatch):
    import ba2_providers

    asked = []
    monkeypatch.setattr(ba2_providers, "get_provider", lambda *a, **k: asked.append(a))
    with pytest.raises(ValueError, match="refusing to fall back"):
        data.fetch_close_prices(["AAA"], as_of=datetime(2024, 3, 4))

    class _Details:      # statements are fine: the refusal must come from the missing OHLCV provider
        def _stmt(self, *a, **k):
            return {"statements": [{"weighted_average_shares_outstanding": 10.0}]}
        get_income_statement = get_balance_sheet = get_cashflow_statement = _stmt

    import sys
    import ba2_providers.fundamentals.details.FMPCompanyDetailsProvider  # noqa: F401  (loads the module)
    monkeypatch.setattr(sys.modules["ba2_providers.fundamentals.details.FMPCompanyDetailsProvider"],
                        "FMPCompanyDetailsProvider", _Details)
    with pytest.raises(data.OhlcvHistoryUnavailable, match="refusing to fall back"):
        data.fetch_value_inputs(["AAA"], as_of=datetime(2024, 3, 4))
    assert asked == []


def test_a_supplied_provider_is_used_as_is():
    provider = _Frame()
    out = data.fetch_close_prices(["AAA"], as_of=datetime(2024, 3, 4), ohlcv_provider=provider)
    assert list(out) == ["AAA"] and provider.calls[0][0] == "AAA"


def test_live_read_uses_the_live_registry_provider(monkeypatch):
    import ba2_providers

    provider = _Frame()
    asked = []
    monkeypatch.setattr(ba2_providers, "get_provider",
                        lambda category, name, **kw: asked.append((category, name)) or provider)
    out = data.fetch_close_prices(["AAA"])
    assert asked == [("ohlcv", "fmp")] and list(out) == ["AAA"]
