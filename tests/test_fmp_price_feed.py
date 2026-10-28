"""FMP real-time price feed: the shared helper and the Alpaca `fmp` data_feed.

No network: the HTTP getter is a fake and the Alpaca data client is a MagicMock that
must NEVER be touched when data_feed == "fmp" (no silent fallback to another source).
"""
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from ba2_trade_platform.core import fmp_live_price as flp
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount


from datetime import datetime, timedelta, timezone
import logging

from ba2_providers.fmp_common import FMPHermeticViolation

UTC = timezone.utc
# Wed 2026-10-21 10:00 ET (EDT) = regular session open.
NOW = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _capture_app_logger(caplog):
    """The ba2 app logger does not propagate to root, so caplog never sees its records unless
    its handler is attached directly (same pattern as the spy-regime tests, d48f5bd9)."""
    from ba2_trade_platform.logger import logger as app_logger
    app_logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        app_logger.removeHandler(caplog.handler)


def _ts(dt):
    return int(dt.timestamp())


class FakeFMP:
    """quote_fn(api_key, symbols) -> rows; records every call. ``ts`` = epoch of each row."""

    def __init__(self, book, fail=False, ts=None, fail_chunks=()):
        self.book, self.fail, self.calls = book, fail, []
        self.ts = _ts(NOW - timedelta(seconds=30)) if ts is None else ts
        self.fail_chunks = set(fail_chunks)

    def __call__(self, api_key, symbols):
        idx = len(self.calls)
        self.calls.append(list(symbols))
        if self.fail or idx in self.fail_chunks:
            raise RuntimeError(f"HTTP 500 https://fmp/quote?apikey={api_key}")
        rows = []
        for s in symbols:
            if s in self.book:
                row = {"symbol": s, "price": self.book[s]}
                if self.ts is not False:
                    row["timestamp"] = self.ts
                rows.append(row)
        return rows


def fetch(symbols, g, **kw):
    kw.setdefault("now", NOW)
    return flp.fetch_fmp_last_prices(symbols, api_key="k", quote_fn=g, **kw)


def test_single_and_batch_prices():
    g = FakeFMP({"AAPL": 190.5, "MSFT": 410.0})
    assert fetch(["AAPL", "MSFT"], g) == {"AAPL": 190.5, "MSFT": 410.0}
    assert len(g.calls) == 1


def test_batches_of_at_most_100():
    syms = [f"S{i}" for i in range(250)]
    g = FakeFMP({s: 10.0 for s in syms})
    assert len(fetch(syms, g)) == 250
    assert len(g.calls) == 3
    assert all(len(c) <= 100 for c in g.calls)


def test_missing_symbol_is_absent_and_logged(caplog):
    g = FakeFMP({"AAPL": 1.0})
    with caplog.at_level("WARNING"):
        out = fetch(["AAPL", "ZZZZ"], g)
    assert out == {"AAPL": 1.0}
    assert "ZZZZ" in caplog.text


@pytest.mark.parametrize("bad", [0, -1.0, None, "x", "12.3", True])
def test_non_positive_null_or_string_price_is_absent(bad):
    """A string price '12.3' is pinned as rejected: FMP sends numbers; anything else is a defect."""
    assert fetch(["AAPL"], FakeFMP({"AAPL": bad})) == {}


def test_http_error_yields_nothing_and_logs_once(caplog):
    with caplog.at_level("ERROR"):
        out = fetch(["AAPL"], FakeFMP({"AAPL": 1.0}, fail=True))
    assert out == {}
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and "AAPL" in errs[0].getMessage() and errs[0].exc_info is None


def test_no_api_key_raises(monkeypatch):
    monkeypatch.setattr(flp, "get_app_setting", lambda k: None)
    with pytest.raises(flp.FMPPriceError):
        flp.fetch_fmp_last_prices(["AAPL"], quote_fn=FakeFMP({}), now=NOW)


def test_api_key_never_logged_by_any_logger(caplog):
    key = "SECRETKEY123"

    def boom(api_key, symbols):
        # what fmpsdk does: logging.error on the root logger with the URL, then fails
        logging.error(f"Connection to https://fmp/quote?apikey={api_key} failed")
        logging.getLogger("fmpsdk.url_methods").error(f"x apikey={api_key}")
        raise RuntimeError(f"...?apikey={api_key}")

    with caplog.at_level("DEBUG"):
        flp.fetch_fmp_last_prices(["AAPL"], api_key=key, quote_fn=boom, now=NOW)
        logging.error(f"after install: https://fmp/quote?apikey={key}")
    assert caplog.records
    for r in caplog.records:
        assert key not in r.getMessage(), (r.name, r.getMessage())
        assert key not in str(r.exc_text or "")


def test_redaction_filter_install_is_idempotent():
    flp.install_fmpsdk_log_redaction()
    flp.install_fmpsdk_log_redaction()
    assert len([f for f in logging.getLogger().filters
                if isinstance(f, flp._RedactFilter)]) == 1


def test_class_share_symbol_translated_and_mapped_back():
    g = FakeFMP({"BRK-B": 450.0})
    assert fetch(["BRK.B"], g) == {"BRK.B": 450.0}
    assert g.calls[0] == ["BRK-B"]


def test_two_spellings_of_one_symbol_are_both_priced():
    g = FakeFMP({"BRK-B": 450.0})
    assert fetch(["BRK.B", "BRK-B"], g) == {"BRK.B": 450.0, "BRK-B": 450.0}
    assert g.calls == [["BRK-B"]]


def test_failed_chunk_does_not_poison_the_next_chunk():
    syms = [f"S{i}" for i in range(150)]
    g = FakeFMP({s: float(i + 1) for i, s in enumerate(syms)}, fail_chunks=[0])
    out = fetch(syms, g)
    assert set(out) == set(syms[100:])
    assert out["S100"] == 101.0 and out["S149"] == 150.0


def test_fmpsdk_returning_none_or_empty_is_unpriced():
    assert fetch(["AAPL"], lambda k, s: None) == {}
    assert fetch(["AAPL"], lambda k, s: []) == {}


def test_hermetic_violation_is_never_swallowed():
    def hermetic(k, s):
        raise FMPHermeticViolation("backtest reached the network")
    with pytest.raises(FMPHermeticViolation):
        fetch(["AAPL"], hermetic)


def test_error_dict_payload_is_an_unpriced_chunk():
    """A 200 carrying an FMP error dict (not a retryable HTTP status) leaves the chunk unpriced."""
    assert fetch(["AAPL"], lambda k, s: {"Error Message": "Limit Reach . Please upgrade"}) == {}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.headers, self.content = status, body, {}, b"x"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fmp_gate(monkeypatch):
    """Virtual clock for the global FMP gate, so the backoff sleeps cost no wall time."""
    from ba2_providers import fmp_common as fc
    clock = [0.0]
    monkeypatch.setattr(fc, "_now", lambda: clock[0])
    monkeypatch.setattr(fc, "_GATE_UNTIL", 0.0)
    slept = []

    def sleep(s):
        slept.append(s)
        clock[0] += s
    return sleep, slept


def test_rate_limited_quote_is_retried_with_backoff(fmp_gate):
    """2026-10-28: a Monday-open 429 left ~480 analyses unpriced; the quote now waits and retries."""
    sleep, slept = fmp_gate
    answers = iter([_Resp(429), _Resp(429), _Resp(200, [{"symbol": "AAPL", "price": 190.0}])])
    urls = []

    def getter(url, params=None, timeout=None):
        urls.append(url)
        return next(answers)

    rows = flp._fmp_http_quote("k", ["AAPL", "MSFT"], getter=getter, sleep=sleep)
    assert rows == [{"symbol": "AAPL", "price": 190.0}]
    assert urls == [f"{flp.FMP_QUOTE_URL}/AAPL,MSFT"] * 3
    assert sum(slept) >= 5 + 15


def test_quote_still_failing_after_retries_is_unpriced(fmp_gate):
    sleep, slept = fmp_gate
    call = lambda k, s: flp._fmp_http_quote(k, s, getter=lambda *a, **kw: _Resp(429), sleep=sleep)
    assert fetch(["AAPL"], call) == {}
    assert sum(slept) >= 5 + 15 + 30


# ------------------------------------------------------------ staleness
def _fresh(sym_ts, now, sym="AAPL"):
    return fetch([sym], FakeFMP({sym: 10.0}, ts=_ts(sym_ts)), now=now) == {sym: 10.0}


def test_open_session_fresh_ok_and_stale_refused(caplog):
    assert _fresh(NOW - timedelta(minutes=14), NOW)
    with caplog.at_level("WARNING"):
        assert not _fresh(NOW - timedelta(minutes=16), NOW)
    assert "AAPL" in caplog.text and "stale" in caplog.text.lower()


def test_open_session_first_minutes_accept_prior_close():
    """09:30 decision: yesterday's close is the legitimate last bar (what the backtest uses)."""
    now = datetime(2026, 10, 21, 13, 30, 5, tzinfo=UTC)          # 09:30:05 ET
    prior_close = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)      # 16:00 ET Tue
    assert _fresh(prior_close, now)
    assert not _fresh(prior_close - timedelta(days=1), now)      # two sessions ago


def test_pre_market_prior_close_ok_and_two_sessions_ago_refused():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)              # 08:00 ET Wed
    assert _fresh(datetime(2026, 10, 20, 20, 0, tzinfo=UTC), now)
    assert _fresh(datetime(2026, 10, 20, 19, 56, tzinfo=UTC), now)   # within 5 min tolerance
    assert not _fresh(datetime(2026, 10, 20, 19, 50, tzinfo=UTC), now)
    assert not _fresh(datetime(2026, 10, 19, 20, 0, tzinfo=UTC), now)


def test_weekend_needs_friday_close():
    now = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)              # Saturday
    assert _fresh(datetime(2026, 10, 23, 20, 0, tzinfo=UTC), now)
    assert not _fresh(datetime(2026, 10, 22, 20, 0, tzinfo=UTC), now)


def test_holiday_needs_the_session_before_the_holiday():
    now = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)              # Thanksgiving, closed
    assert _fresh(datetime(2026, 11, 25, 21, 0, tzinfo=UTC), now)     # Wed close 16:00 EST
    assert not _fresh(datetime(2026, 11, 24, 21, 0, tzinfo=UTC), now)


def test_half_day_close_is_1pm():
    now = datetime(2026, 11, 27, 19, 0, tzinfo=UTC)              # Fri after the half-day close
    assert _fresh(datetime(2026, 11, 27, 18, 0, tzinfo=UTC), now)     # 13:00 ET close
    assert not _fresh(datetime(2026, 11, 27, 17, 0, tzinfo=UTC), now)  # 12:00 ET, before close


def test_missing_or_unparseable_timestamp_is_unpriced(caplog):
    with caplog.at_level("WARNING"):
        assert fetch(["AAPL"], FakeFMP({"AAPL": 1.0}, ts=False)) == {}
        assert fetch(["AAPL"], FakeFMP({"AAPL": 1.0}, ts="soon")) == {}
        assert fetch(["AAPL"], FakeFMP({"AAPL": 1.0}, ts=0)) == {}
    assert "AAPL" in caplog.text


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


def test_no_key_logs_one_error_without_traceback(caplog, monkeypatch):
    monkeypatch.setattr(flp, "get_app_setting", lambda k: None)
    acct = _acct("fmp")
    with _with_feed("fmp"), caplog.at_level("ERROR"):
        assert acct._get_instrument_current_price_impl("AAPL", "mid") is None
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1 and errs[0].exc_info is None


def test_hermetic_violation_propagates_through_the_account():
    acct = _acct("fmp")
    with _with_feed("fmp"), patch(
            "ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices",
            side_effect=FMPHermeticViolation("x")):
        with pytest.raises(FMPHermeticViolation):
            acct._get_instrument_current_price_impl("AAPL", "mid")


def test_none_price_is_not_cached_by_the_account_price_cache():
    acct = _acct("fmp")
    AlpacaAccount._GLOBAL_PRICE_CACHE.pop(acct.id, None)
    with _with_feed("fmp"), patch(
            "ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices",
            return_value={}):
        assert acct.get_instrument_current_price("AAPL", "mid") is None
        assert acct.get_instrument_current_price(["AAPL"], "mid") == {"AAPL": None}
    assert AlpacaAccount._GLOBAL_PRICE_CACHE.get(acct.id, {}) == {}


@pytest.mark.parametrize("ptype", ["bid", "mid", "mark"])
def test_fmp_feed_shares_one_cache_entry_across_last_price_types(ptype):
    """bid/mid/mark are the same FMP last price: one FMP request serves all of them."""
    acct = _acct("fmp")
    AlpacaAccount._GLOBAL_PRICE_CACHE.pop(acct.id, None)
    with _with_feed("fmp"), patch(
            "ba2_trade_platform.modules.accounts.AlpacaAccount.fetch_fmp_last_prices",
            return_value={"AAPL": 190.0}) as f:
        assert acct.get_instrument_current_price("AAPL") == 190.0
        assert acct.get_instrument_current_price("AAPL", ptype) == 190.0
        assert acct.get_instrument_current_price(["AAPL"], ptype) == {"AAPL": 190.0}
    assert f.call_count == 1
    AlpacaAccount._GLOBAL_PRICE_CACHE.pop(acct.id, None)


def test_penny_live_prices_all_none_without_key_and_callers_use_get():
    """vendor_live_price=fmp + no key: all-None dict; every caller reads it with .get() + None check."""
    import pathlib
    import re
    from ba2_experts.PennyMomentumTrader.data_gathering import DataGatheringMixin

    class _P(DataGatheringMixin):
        logger = logging.getLogger("penny-test")
        instance = MagicMock()

        def get_setting_with_interface_default(self, *a, **k):
            return "fmp"

    with patch("ba2_common.config.get_app_setting", return_value=None):
        out = _P()._get_live_prices(["AAPL"])
    assert out == {"AAPL": None}
    base = pathlib.Path(__import__("ba2_experts.PennyMomentumTrader", fromlist=["x"]).__file__).parent
    for fname in ("monitoring.py", "screening.py"):
        src = (base / fname).read_text(encoding="utf-8")
        assert not re.search(r"live_prices\[", src), f"{fname} indexes live_prices directly"
