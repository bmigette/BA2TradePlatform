# backend/tests/backtest/test_fetch_options_feed.py
"""No-network regression tests: the --feed flag must actually reach Alpaca's /options/bars.

Dead-flag bug: ``build_cache`` accepted ``feed`` and both CLIs (``ba2-test fetch-options``,
``ba2test_launcher.py``) passed it, but the value never reached ``OptionBarsRequest`` — bars
always came from the SDK-default feed. This matters because the indicative feed's
quote-derived prints can be arbitrage-inconsistent with the underlying; users need
``--feed opra`` (trades) to actually take effect.

Complication the fix works around: the installed alpaca-py (0.43.4) gives ``OptionBarsRequest``
NO ``feed`` field (only the snapshot/chain/latest option request classes carry one) and its
pydantic models silently IGNORE unknown kwargs — so ``feed=`` on the base class is a silent
no-op. ``fetch_options.build_cache`` therefore declares the field via a subclass; these tests
capture the request object handed to ``OptionHistoricalDataClient.get_option_bars`` and assert
the requested feed is on it (and on the wire params), plus that an invalid feed fails LOUD
(ValueError) before any client/network/credential work."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from alpaca.data.enums import OptionsFeed
from alpaca.data.requests import OptionBarsRequest

import app.services.backtest.fetch_options as fo


class _Contract:
    """Enough of an Alpaca OptionContract for the build_cache row mappers."""
    symbol = "AAPL240315C00180000"
    type = SimpleNamespace(value="call")
    strike_price = 180.0
    expiration_date = date(2024, 3, 15)


class _Bar:
    """Enough of an Alpaca option bar for bar_to_row (timestamp + OHLCV)."""
    def __init__(self, d):
        self.timestamp = datetime.fromisoformat(d)
        self.open = self.high = self.low = self.close = 5.0
        self.volume = 10


_OMIT = object()  # sentinel: call build_cache WITHOUT the feed kwarg (exercise the default)


def _run_build_cache(monkeypatch, tmp_path, feed=_OMIT):
    """Run build_cache fully stubbed (no network): the fake OptionHistoricalDataClient captures
    every bars request object; contract discovery / FRED / underlying closes are canned; the
    cache is a real OptionsHistoryCache on a tmp sqlite file. Returns the captured requests."""
    captured = []

    class _FakeDataClient:
        def __init__(self, *a, **k):
            pass

        def get_option_bars(self, req):
            captured.append(req)
            return SimpleNamespace(data={_Contract.symbol: [_Bar("2024-03-04")]})

    monkeypatch.setattr("alpaca.trading.client.TradingClient",
                        lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("alpaca.data.historical.option.OptionHistoricalDataClient",
                        _FakeDataClient)
    monkeypatch.setattr("ba2_providers.get_provider", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(fo, "discover_contracts", lambda *a, **k: [_Contract()])
    monkeypatch.setattr(fo, "fetch_risk_free_rate_series", lambda *a: {})
    monkeypatch.setattr(fo, "fetch_underlying_close_series", lambda *a, **k: {})
    kwargs = {} if feed is _OMIT else {"feed": feed}
    # Dummy explicit creds so _alpaca_keys never touches the environment; the stubbed clients
    # above receive them but never make a network call.
    stats = fo.build_cache(str(tmp_path / "opt.db"), ["AAPL"],
                           date(2024, 3, 1), date(2024, 3, 8),
                           api_key="test-key", api_secret="test-secret", **kwargs)
    assert stats["symbols_done"] == 1 and stats["symbols_failed"] == 0
    assert captured, "get_option_bars was never called"
    return captured


def test_build_cache_opra_feed_reaches_option_bars_request(monkeypatch, tmp_path):
    """--feed opra must land on EVERY OptionBarsRequest the build makes (as the SDK enum),
    not be dropped on the floor."""
    for req in _run_build_cache(monkeypatch, tmp_path, "opra"):
        assert isinstance(req, OptionBarsRequest)
        assert req.feed is OptionsFeed.OPRA
        # What the wire actually carries: to_request_fields() feeds requests' params, which
        # encode the str-enum by value.
        assert req.to_request_fields()["feed"] == "opra"


def test_build_cache_feed_matching_is_case_insensitive(monkeypatch, tmp_path):
    for req in _run_build_cache(monkeypatch, tmp_path, "OPRA"):
        assert req.feed is OptionsFeed.OPRA


def test_build_cache_default_feed_is_indicative(monkeypatch, tmp_path):
    """Omitting feed keeps the historical default (indicative — free tier), now EXPLICITLY set
    on the request instead of relying on the SDK default."""
    for req in _run_build_cache(monkeypatch, tmp_path, _OMIT):
        assert req.feed is OptionsFeed.INDICATIVE


def test_options_feed_enum_values():
    assert fo._options_feed("indicative") is OptionsFeed.INDICATIVE
    assert fo._options_feed("Opra") is OptionsFeed.OPRA
    assert fo._options_feed("  opra  ") is OptionsFeed.OPRA


def test_options_feed_invalid_raises_valueerror():
    """Fail loud, no silent default: a bogus feed errors and names the valid values."""
    with pytest.raises(ValueError) as excinfo:
        fo._options_feed("bogus")
    assert "bogus" in str(excinfo.value)
    assert "indicative" in str(excinfo.value) and "opra" in str(excinfo.value)


def test_build_cache_invalid_feed_fails_before_any_client_work(monkeypatch, tmp_path):
    """The ValueError must surface BEFORE creds resolution / client construction — no network
    is touched and no Alpaca credentials are needed for the rejection."""
    def _boom(*a, **k):
        raise AssertionError("an Alpaca client was constructed despite the invalid feed")

    monkeypatch.setattr("alpaca.trading.client.TradingClient", _boom)
    monkeypatch.setattr("alpaca.data.historical.option.OptionHistoricalDataClient", _boom)
    with pytest.raises(ValueError, match="valid values"):
        fo.build_cache(str(tmp_path / "opt.db"), ["AAPL"],
                       date(2024, 3, 1), date(2024, 3, 8), "bogus")
