"""FMP real-time price feed: the shared helper and the Alpaca `fmp` data_feed.

No network: the HTTP getter is a fake and the Alpaca data client is a MagicMock that
must NEVER be touched when data_feed == "fmp" (no silent fallback to another source).
"""
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from ba2_trade_platform.core import fmp_live_price as flp
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


class FakeFMP:
    """quote_fn(api_key, symbols) -> rows; records every call."""

    def __init__(self, book, fail=False):
        self.book, self.fail, self.calls = book, fail, []

    def __call__(self, api_key, symbols):
        self.calls.append(list(symbols))
        if self.fail:
            raise RuntimeError(f"HTTP 500 https://fmp/quote?apikey={api_key}")
        return [{"symbol": s, "price": self.book[s]} for s in symbols if s in self.book]


def test_single_and_batch_prices():
    g = FakeFMP({"AAPL": 190.5, "MSFT": 410.0})
    out = flp.fetch_fmp_last_prices(["AAPL", "MSFT"], api_key="k", quote_fn=g)
    assert out == {"AAPL": 190.5, "MSFT": 410.0}
    assert len(g.calls) == 1


def test_batches_of_at_most_100():
    syms = [f"S{i}" for i in range(250)]
    g = FakeFMP({s: 10.0 for s in syms})
    out = flp.fetch_fmp_last_prices(syms, api_key="k", quote_fn=g)
    assert len(out) == 250
    assert len(g.calls) == 3
    assert all(len(c) <= 100 for c in g.calls)


def test_missing_symbol_is_absent_and_logged(caplog):
    g = FakeFMP({"AAPL": 1.0})
    with caplog.at_level("WARNING"):
        out = flp.fetch_fmp_last_prices(["AAPL", "ZZZZ"], api_key="k", quote_fn=g)
    assert out == {"AAPL": 1.0}
    assert "ZZZZ" in caplog.text


@pytest.mark.parametrize("bad", [0, -1.0, None, "x"])
def test_non_positive_or_null_price_is_absent(bad):
    g = FakeFMP({"AAPL": bad})
    assert flp.fetch_fmp_last_prices(["AAPL"], api_key="k", quote_fn=g) == {}


def test_http_error_yields_nothing_and_logs(caplog):
    g = FakeFMP({"AAPL": 1.0}, fail=True)
    with caplog.at_level("ERROR"):
        out = flp.fetch_fmp_last_prices(["AAPL"], api_key="k", quote_fn=g)
    assert out == {}
    assert "AAPL" in caplog.text


def test_no_api_key_raises(monkeypatch):
    monkeypatch.setattr(flp, "get_app_setting", lambda k: None)
    with pytest.raises(flp.FMPPriceError):
        flp.fetch_fmp_last_prices(["AAPL"], quote_fn=FakeFMP({}))


def test_api_key_never_logged(caplog):
    g = FakeFMP({"AAPL": 1.0}, fail=True)
    with caplog.at_level("DEBUG"):
        flp.fetch_fmp_last_prices(["AAPL"], api_key="SECRETKEY123", quote_fn=g)
    assert "SECRETKEY123" not in caplog.text


def test_class_share_symbol_translated_and_mapped_back():
    g = FakeFMP({"BRK-B": 450.0})
    out = flp.fetch_fmp_last_prices(["BRK.B"], api_key="k", quote_fn=g)
    assert out == {"BRK.B": 450.0}
    assert g.calls[0] == ["BRK-B"]


# ---------------------------------------------------------------- AlpacaAccount
def _acct(feed):
    a = object.__new__(AlpacaAccount)
    a.id = 7
    return a


def _with_feed(feed):
    return patch.object(AlpacaAccount, "settings", new_callable=PropertyMock,
                        return_value={"api_key": "a", "api_secret": "b", "data_feed": feed})


def test_default_feed_is_fmp_and_listed():
    d = AlpacaAccount.get_settings_definitions()["data_feed"]
    assert d["default"] == "fmp"
    assert "fmp" in d["valid_values"]
    assert "delayed_sip" in d["valid_values"]
    assert "fmp" in d["tooltip"].lower() and "delayed" in d["tooltip"].lower()


@pytest.mark.parametrize("ptype", ["bid", "mid", "avg", "mark", "trade"])
def test_fmp_feed_never_touches_alpaca_and_returns_last(ptype):
    acct = _acct("fmp")
    with _with_feed("fmp"), \
         patch.object(AlpacaAccount, "_check_authentication") as auth, \
         patch("alpaca.data.historical.StockHistoricalDataClient") as client, \
         patch("ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices",
               return_value={"AAPL": 190.0}) as f:
        assert acct._get_instrument_current_price_impl("AAPL", ptype) == 190.0
        assert acct._get_instrument_current_price_impl(["AAPL", "NOPE"], ptype) == \
            {"AAPL": 190.0, "NOPE": None}
    client.assert_not_called()
    auth.assert_not_called()
    assert f.call_count == 2


def test_fmp_failure_is_none_not_alpaca():
    acct = _acct("fmp")
    with _with_feed("fmp"), \
         patch("alpaca.data.historical.StockHistoricalDataClient") as client, \
         patch("ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices",
               side_effect=flp.FMPPriceError("no key")):
        assert acct._get_instrument_current_price_impl("AAPL") is None
        assert acct._get_instrument_current_price_impl(["AAPL"]) == {"AAPL": None}
    client.assert_not_called()


def test_unknown_feed_raises():
    acct = _acct("bogus")
    with _with_feed("bogus"), pytest.raises(ValueError, match="bogus"):
        acct._get_instrument_current_price_impl("AAPL")


def test_blank_feed_means_declared_default_fmp():
    acct = _acct("")
    with _with_feed(""), patch(
            "ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices",
            return_value={"AAPL": 5.0}):
        assert acct._get_instrument_current_price_impl("AAPL") == 5.0


@pytest.mark.parametrize("ptype", ["ask"])
def test_ask_refused_loudly_with_fmp_feed(ptype):
    """FMP /quote carries no ask (and "bid" is the interface DEFAULT meaning "the price", so it is last): refuse, never answer with a last price or an Alpaca quote."""
    acct = _acct("fmp")
    with _with_feed("fmp"),          patch("alpaca.data.historical.StockHistoricalDataClient") as client,          patch("ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices") as f:
        with pytest.raises(ValueError, match="ask"):
            acct._get_instrument_current_price_impl("AAPL", ptype)
        with pytest.raises(ValueError, match="ask"):
            acct._get_instrument_current_price_impl(["AAPL"], ptype)
    client.assert_not_called()
    f.assert_not_called()


def test_non_fmp_feed_still_serves_ask():
    """Only the fmp feed refuses bid/ask; the Alpaca feeds keep their quote handling."""
    acct = _acct("delayed_sip")
    with _with_feed("delayed_sip"),          patch.object(AlpacaAccount, "_check_authentication", return_value=False):
        assert acct._get_instrument_current_price_impl("AAPL", "ask") is None


def test_penny_fmp_quotes_without_key_gives_no_prices_and_never_asks_the_account(caplog):
    """PennyMomentumTrader vendor_live_price=fmp: a missing key is an ERROR + no prices, not account prices."""
    from ba2_experts.PennyMomentumTrader.data_gathering import DataGatheringMixin

    class _P(DataGatheringMixin):
        logger = __import__("logging").getLogger("penny-test")
        instance = MagicMock()

    with patch("ba2_common.config.get_app_setting", return_value=None), \
         patch("ba2_common.core.instance_resolver.get_instance_resolver") as resolver, \
         caplog.at_level("ERROR"):
        out = _P()._get_fmp_quotes(["AAPL", "MSFT"])
    assert out == {"AAPL": None, "MSFT": None}
    resolver.assert_not_called()
    assert "FMP_API_KEY" in caplog.text
