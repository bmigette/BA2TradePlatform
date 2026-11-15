"""Live IV chain source selection, certified pairings, bounded universes and the snapshot validity rule."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core import market_conditions as mc
from ba2_common.core.market_condition_iv_live import (
    ChainLiveReader, IvEvidence, LiveChainSource, bounded_iv_universe, clear_live_chain_readers,
    parse_live_chain_source, register_live_chain_reader,
)
from ba2_common.core.market_condition_readers import LiveUnavailableMarketConditionReader
from ba2_common.core.market_conditions import IV_PROFILE
from ba2_common.core.option_chain_quotes import (
    OptionChainQuote, OptionChainQuoteSnapshot, session_close_and_next_open,
    validate_prior_session_snapshot,
)


# --------------------------------------------------------------------------- the app setting
@pytest.mark.parametrize("raw, want", [
    (None, None), ("", None), ("  ", None),
    ("alphavantage", LiveChainSource("alphavantage", "alphavantage")),
    ("AlphaVantage", LiveChainSource("AlphaVantage", "alphavantage")),
    ("tastytrade", LiveChainSource("tastytrade", "tastytrade")),
    ("TastyTrade", LiveChainSource("TastyTrade", "tastytrade")),
])
def test_the_setting_parses(raw, want):
    assert parse_live_chain_source(raw) == want


@pytest.mark.parametrize("raw", ["account:3", "thetadata", "broker", "alpaca"])
def test_a_malformed_setting_raises(raw):
    with pytest.raises(ValueError):
        parse_live_chain_source(raw)


# --------------------------------------------------------------------------- pairings
def test_only_certified_pairings_serve_gates_trained_on_alphavantage():
    assert mc.pairing_unavailable_reason(IV_PROFILE, "alphavantage", "alphavantage") is None
    assert mc.pairing_unavailable_reason(IV_PROFILE, "alphavantage", "tastytrade") is None
    assert "not certified" in mc.pairing_unavailable_reason(IV_PROFILE, "alphavantage", "alpaca")
    assert "not certified" in mc.pairing_unavailable_reason(IV_PROFILE, "thetadata", "tastytrade")
    assert mc.pairing_unavailable_reason("ohlcv-v1", "x", "y") is None
    for pair in (("alphavantage", "alphavantage"), ("alphavantage", "tastytrade")):
        assert pair in mc.PAIRING_EVIDENCE


class _Reader(ChainLiveReader):
    option_source = "x"

    def __init__(self, profile, serving_kind, **kw):
        super().__init__(profile)
        self.serving_kind = serving_kind
        self.kw = kw

    def _evidence(self, symbol, session):
        return IvEvidence(row=None)


@pytest.fixture
def registry():
    clear_live_chain_readers()
    yield
    clear_live_chain_readers()


def _reader_for(live_source, kind="tastytrade", unavailable_reason=None, trained="alphavantage"):
    from ba2_common.core.market_condition_live import _chain_live_reader

    register_live_chain_reader("tastytrade", lambda p, **kw: _Reader(p, kind, **kw))
    register_live_chain_reader("alphavantage", lambda p, **kw: _Reader(p, "alphavantage", **kw))
    return _chain_live_reader(IV_PROFILE, trained, "root", "fmp", live_source, unavailable_reason)


def test_the_chain_reader_follows_the_setting(registry):
    r = _reader_for("tastytrade")
    assert isinstance(r, _Reader) and r.serving_kind == "tastytrade" and set(r.kw) == {"cache_root", "source_profile"}
    r = _reader_for("alphavantage")
    assert isinstance(r, _Reader) and r.serving_kind == "alphavantage"
    for raw, why in ((None, "is not set"), ("nope", "malformed")):
        r = _reader_for(raw)
        assert isinstance(r, LiveUnavailableMarketConditionReader) and why in r.reason
        assert "iv_live_chain_source" in r.reason
    r = _reader_for("tastytrade", kind="indicative")
    assert isinstance(r, LiveUnavailableMarketConditionReader) and "not certified" in r.reason
    r = _reader_for("tastytrade", unavailable_reason="expert instance 3: set screener_symbol_universe")
    assert isinstance(r, LiveUnavailableMarketConditionReader) and "screener_symbol_universe" in r.reason
    # the universe bound only matters to a captured source
    assert isinstance(_reader_for("alphavantage", unavailable_reason="x"), _Reader)
    r = _reader_for("tastytrade", trained="thetadata")
    assert isinstance(r, LiveUnavailableMarketConditionReader)


def test_a_source_that_cannot_serve_refuses_with_its_reason(registry):
    from ba2_common.core.market_condition_live import _chain_live_reader

    register_live_chain_reader("tastytrade", lambda p, **kw: LiveUnavailableMarketConditionReader(
        p, "TastyTrade market-data credentials are not set (tastytrade_market_client_secret ...)"))
    r = _chain_live_reader(IV_PROFILE, "alphavantage", "root", "fmp", "tastytrade")
    assert isinstance(r, LiveUnavailableMarketConditionReader) and "tastytrade_market_client_secret" in r.reason


# --------------------------------------------------------------------------- bounded universes
class _Expert:
    def __init__(self, instruments, universe=""):
        self._i, self.settings = instruments, {"screener_symbol_universe": universe}

    def get_enabled_instruments(self):
        return self._i


def test_bounded_universes():
    assert bounded_iv_universe(_Expert(["msft", "KO"])) == (["KO", "MSFT"], "")
    syms, why = bounded_iv_universe(_Expert(["SCREENER"], "aapl, msft;nvda"))
    assert syms == ["AAPL", "MSFT", "NVDA"] and why == ""
    syms, why = bounded_iv_universe(_Expert(["SCREENER"], ""))
    assert syms is None and "screener_symbol_universe" in why
    syms, why = bounded_iv_universe(_Expert(["DYNAMIC"]))
    assert syms is None and "DYNAMIC" in why
    assert bounded_iv_universe(_Expert([]))[0] is None


# --------------------------------------------------------------------------- validity rule edges
def _snap(session, updated, n=12, summary=None, spot=100.0):
    exp = session + timedelta(days=30)
    quotes = tuple(OptionChainQuote(f"X{exp:%y%m%d}{cp}{int(k * 1000):08d}", exp, "call" if cp == "C" else "put",
                                    k, 1.0, 1.1, summary or session, updated)
                   for k in (98.0, 99.0, 100.0, 101.0, 102.0, 103.0) for cp in ("C", "P"))[:n]
    return OptionChainQuoteSnapshot("X", "tastytrade", updated, spot, session, quotes, len(quotes))


def test_an_early_close_session_is_measured_from_its_own_close():
    s = date(2026, 11, 27)                             # the day after Thanksgiving: 13:00 ET close
    close, nxt = session_close_and_next_open(s)
    assert close == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert nxt == datetime(2026, 11, 30, 14, 30, tzinfo=timezone.utc)
    assert validate_prior_session_snapshot(_snap(s, close + timedelta(minutes=30)), s).ok
    assert validate_prior_session_snapshot(_snap(s, nxt), s).kind == "snapshot_after_session"
    assert validate_prior_session_snapshot(_snap(s, close - timedelta(minutes=5)), s).kind == "not_session_quotes"


def test_too_few_near_money_contracts_refuse():
    s = date(2026, 11, 13)
    close, _ = session_close_and_next_open(s)
    assert validate_prior_session_snapshot(_snap(s, close, n=3), s).kind == "too_few_near_money"
    assert validate_prior_session_snapshot(_snap(s, close, spot=None), s).kind == "no_spot"
