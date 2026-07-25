# backend/tests/backtest/test_fetch_options_feed.py
"""No-network regression tests: ``feed`` must NOT be sent to Alpaca's /options/bars.

CORRECTED 2026-07-25. This file previously asserted the OPPOSITE -- that ``--feed`` had to
reach ``OptionBarsRequest`` -- and a subclass existed purely to make pydantic serialize it.
That was built on an assumption about the API that was never verified against it. Verified
now, live:

    request WITH    feed -> {"message":"unexpected query parameter(s): feed"}
    request WITHOUT feed -> bars returned normally

``feed`` IS valid on the snapshot / chain / latest option endpoints, which is where the
assumption came from; the BARS endpoint rejects it. The tests below therefore pin the
corrected behaviour: no ``feed`` on the bars request, at all, regardless of what the caller
passed.

Impact of the original bug: every options fetch failed outright, for every underlying -- not
just new ones. It stayed invisible because the existing 13.7M-bar cache predates Alpaca
tightening its unknown-parameter check, so nothing re-fetched until 2026-07-25.

``feed`` is still VALIDATED (a typo fails loud before any network/credential work) and still
selects the chain/snapshot source -- those tests are unchanged and still pass."""
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


def test_feed_is_never_sent_on_the_bars_request(monkeypatch, tmp_path):
    """The bars endpoint rejects `feed` outright, so it must not appear on the request --
    whatever the caller asked for. Checks the WIRE params, which is what actually reaches
    Alpaca, not just the model attribute."""
    for i, feed in enumerate(("opra", "OPRA", "indicative", _OMIT)):
        # A FRESH cache dir per iteration: build_cache resumes by default, so reusing one
        # path would skip the symbol on every pass after the first (symbols_done=0) and the
        # loop would silently assert nothing.
        case_dir = tmp_path / f"case{i}"
        case_dir.mkdir()
        for req in _run_build_cache(monkeypatch, case_dir, feed):
            assert isinstance(req, OptionBarsRequest)
            assert "feed" not in req.to_request_fields(), (
                f"feed leaked onto the bars request for feed={feed!r}; Alpaca answers "
                f'{{"message":"unexpected query parameter(s): feed"}} and the whole fetch fails')


def test_bars_request_carries_the_fields_that_ARE_valid(monkeypatch, tmp_path):
    """Guard against over-correcting: dropping feed must not drop the real parameters."""
    req = _run_build_cache(monkeypatch, tmp_path, "opra")[0]
    fields = req.to_request_fields()
    # timeframe serializes as the SDK's TimeFrame object, not a plain string.
    assert str(fields.get("timeframe")) == "1Day"
    assert {"start", "end", "symbols"} <= set(fields)


def test_an_invalid_feed_still_fails_loud_even_though_bars_ignore_it(monkeypatch, tmp_path):
    """feed still selects the chain/snapshot source, so a typo must still error rather than
    silently fetch the wrong thing -- removing it from the BARS request does not make the
    value meaningless."""
    with pytest.raises(ValueError, match="valid values"):
        fo.build_cache(str(tmp_path / "opt.db"), ["AAPL"],
                       date(2024, 3, 1), date(2024, 3, 8), "bogus",
                       api_key="test-key", api_secret="test-secret")


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
